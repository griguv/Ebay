import os
import re
import json
import random
import logging
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

# --------------------------------------
# Логирование
# --------------------------------------
logger = logging.getLogger("price-bot")
if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)
logger.setLevel(logging.INFO)

# --------------------------------------
# Константы стран (фиксированный набор)
# --------------------------------------
COUNTRIES = ["RU", "TR", "KZ", "AE", "HK"]

# ENV с прокси для каждой страны:
# PROXY_RU/TR/KZ/AE/HK -> http(s)://user:pass@host:port
PROXIES: Dict[str, Optional[str]] = {
    "RU": os.getenv("PROXY_RU"),
    "TR": os.getenv("PROXY_TR"),
    "KZ": os.getenv("PROXY_KZ"),
    "AE": os.getenv("PROXY_AE"),
    "HK": os.getenv("PROXY_HK"),
}

# Таймауты
REQUEST_TIMEOUT = httpx.Timeout(20.0, connect=20.0, read=20.0)

# Заголовки
UA_LIST = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36",
]

ACCEPT_LANG = {
    "RU": "ru-RU,ru;q=0.9,en;q=0.8",
    "TR": "tr-TR,tr;q=0.9,en;q=0.8",
    "KZ": "ru-RU,ru;q=0.9,en;q=0.8",
    "AE": "en-AE,en;q=0.9",
    "HK": "en-HK,en;q=0.9,zh;q=0.8",
}

COMMON_HEADERS_BASE = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "Connection": "keep-alive",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

def build_headers(country: str) -> Dict[str, str]:
    ua = random.choice(UA_LIST)
    h = dict(COMMON_HEADERS_BASE)
    h["User-Agent"] = ua
    h["Accept-Language"] = ACCEPT_LANG.get(country, "en;q=0.9")
    h["Referer"] = "https://www.google.com/"
    return h

# --------------------------------------
# Вспомогательные парсеры
# --------------------------------------
def _parse_price_from_ld_json(soup: BeautifulSoup) -> Optional[Tuple[float, str]]:
    try:
        for tag in soup.find_all("script", type=lambda t: t and "ld+json" in t):
            if not tag.string:
                continue
            data = json.loads(tag.string.strip())
            candidates = data if isinstance(data, list) else [data]
            for obj in candidates:
                offers = obj.get("offers")
                if not offers:
                    continue
                offers_list = offers if isinstance(offers, list) else [offers]
                for off in offers_list:
                    price_raw = off.get("price") or off.get("lowPrice") or off.get("highPrice")
                    currency = off.get("priceCurrency") or off.get("priceCurrencyCode")
                    if price_raw:
                        try:
                            price = float(str(price_raw).replace(",", "").replace(" ", ""))
                            return price, (currency or "").upper()
                        except Exception:
                            continue
    except Exception:
        pass
    return None

def _parse_price_by_regex(text: str) -> Optional[Tuple[float, Optional[str]]]:
    cur_re = r"(USD|EUR|GBP|TRY|AED|HKD|RUB|RUR|KZT)"
    price_re = r"(\d{1,3}(?:[ ,]\d{3})*(?:[.,]\d{2})?|\d+)"
    patterns = [
        rf'"price"\s*:\s*"{price_re}"\s*(?:,\s*"priceCurrency"\s*:\s*"{cur_re}")?',
        rf'"price"\s*:\s*{price_re}\s*(?:,\s*"priceCurrency"\s*:\s*"{cur_re}")?',
        rf'"priceCurrency"\s*:\s*"{cur_re}".*?"price"\s*:\s*{price_re}',
    ]
    for pat in patterns:
        m = re.search(pat, text, flags=re.IGNORECASE | re.DOTALL)
        if m:
            groups = [g for g in m.groups() if g is not None]
            nums = [g for g in groups if re.fullmatch(price_re, g)]
            curs = [g for g in groups if re.fullmatch(cur_re, g, flags=re.IGNORECASE)]
            if nums:
                p = float(nums[0].replace(" ", "").replace(",", "").replace("’", "").replace("٬", "").replace(" ", ""))
                c = curs[0].upper() if curs else None
                return p, c
    return None

def _host(url: str) -> str:
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return ""

def _is_farfetch(url: str) -> bool:
    return "farfetch." in _host(url)

def _is_yoox(url: str) -> bool:
    h = _host(url)
    return "yoox." in h or "yoox.com" in h

def _parse_price_farfetch(html: str) -> Optional[Tuple[float, Optional[str]]]:
    soup = BeautifulSoup(html, "html.parser")
    ld = _parse_price_from_ld_json(soup)
    if ld:
        return ld
    meta_price = soup.find("meta", {"itemprop": "price"})
    meta_curr = soup.find("meta", {"itemprop": "priceCurrency"})
    if meta_price:
        try:
            price = float(meta_price.get("content", "").replace(",", "").strip())
            currency = meta_curr.get("content", "").upper() if meta_curr else None
            return price, currency
        except Exception:
            pass
    return _parse_price_by_regex(html)

def _parse_price_yoox(html: str) -> Optional[Tuple[float, Optional[str]]]:
    soup = BeautifulSoup(html, "html.parser")
    ld = _parse_price_from_ld_json(soup)
    if ld:
        return ld
    m = re.search(r"dataLayer\s*=\s*(\[[^\]]+\])", html, flags=re.IGNORECASE)
    if m:
        try:
            arr = json.loads(m.group(1))
            if isinstance(arr, list):
                for obj in arr:
                    price = obj.get("price") or obj.get("productPrice")
                    cur = obj.get("currency") or obj.get("productCurrency")
                    if price:
                        return float(str(price).replace(",", "").replace(" ", "")), (cur or "").upper() or None
        except Exception:
            pass
    return _parse_price_by_regex(html)

def _parse_price_generic(html: str) -> Optional[Tuple[float, Optional[str]]]:
    soup = BeautifulSoup(html, "html.parser")
    ld = _parse_price_from_ld_json(soup)
    if ld:
        return ld
    return _parse_price_by_regex(html)

def parse_price_for_site(url: str, html: str) -> Optional[Tuple[float, Optional[str]]]:
    if _is_farfetch(url):
        return _parse_price_farfetch(html)
    if _is_yoox(url):
        return _parse_price_yoox(html)
    return _parse_price_generic(html)

async def _fetch_html(url: str, country: str, client: httpx.AsyncClient, proxy: Optional[str]) -> Optional[str]:
    headers = build_headers(country)
    try:
        resp = await client.get(
            url,
            headers=headers,
            timeout=REQUEST_TIMEOUT,
            proxy=proxy,  # httpx 0.28+: proxy на уровне запроса
            follow_redirects=True,
        )
        if 200 <= resp.status_code < 300 and resp.text:
            return resp.text
        logger.error("HTTP %s на %s (%s)", resp.status_code, url, country)
        return None
    except httpx.HTTPError as e:
        logger.error("HTTP ошибка на %s (%s): %s", url, country, e)
        return None
    except Exception as e:
        logger.error("Иная ошибка на %s (%s): %s", url, country, e)
        return None

# --------------------------------------
# Публичные функции
# --------------------------------------
async def get_price_for_country(url: str, country: str) -> Optional[Tuple[float, Optional[str]]]:
    proxy = PROXIES.get(country)
    async with httpx.AsyncClient(http2=True) as client:
        html = await _fetch_html(url, country, client, proxy)
        if not html:
            return None
        return parse_price_for_site(url, html)

async def get_prices_across_countries(url: str) -> Dict[str, Dict[str, Optional[str]]]:
    results: Dict[str, Dict[str, Optional[str]]] = {}
    async with httpx.AsyncClient() as client:
        for country in COUNTRIES:
            proxy = PROXIES.get(country)
            try:
                html = await _fetch_html(url, country, client, proxy)
                if not html:
                    results[country] = {"price": None, "currency": None}
                    continue
                parsed = parse_price_for_site(url, html)
                if parsed:
                    price, currency = parsed
                    results[country] = {"price": f"{price:.2f}", "currency": currency}
                else:
                    results[country] = {"price": None, "currency": None}
            except Exception as e:
                logger.error("Ошибка парсинга %s: %s", url, e, exc_info=True)
                results[country] = {"price": None, "currency": None}
    return results

def format_prices_table(prices_by_country: Dict[str, Dict[str, Optional[str]]]) -> str:
    lines: List[str] = []
    order = ["RU", "TR", "KZ", "AE", "HK"]
    for c in order:
        data = prices_by_country.get(c) or {}
        price = data.get("price")
        curr = data.get("currency")
        if price and curr:
            lines.append(f"{c}: {price} {curr}")
        else:
            lines.append(f"{c}: —")
    return "\n".join(lines)
