# price_parsers.py
import os
import re
import json
import logging
from typing import Dict, Optional, Tuple, List

import httpx

logger = logging.getLogger("price-bot.parsers")
if not logger.handlers:
    import sys
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
    logger.addHandler(_h)
logger.setLevel(logging.INFO)

# ------------------------------------------------------------
# КОНФИГ СТРАН — ФИКСИРОВАННЫЙ СПИСОК
# ------------------------------------------------------------
COUNTRIES = ["RU", "TR", "KZ", "AE", "HK", "ES"]

ACCEPT_LANGUAGE = {
    "RU": "ru-RU,ru;q=0.9,en;q=0.8",
    "TR": "tr-TR,tr;q=0.9,en;q=0.8",
    "KZ": "ru-KZ,ru;q=0.9,en;q=0.8",
    "AE": "en-AE,en;q=0.9",
    "HK": "en-HK,en;q=0.9,zh-CN;q=0.7",
    "ES": "es-ES,es;q=0.9,en;q=0.8",
}

PROXY_ENV = {country: f"PROXY_{country}" for country in COUNTRIES}

BASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Connection": "keep-alive",
}

TIMEOUT = httpx.Timeout(30.0)

# ------------------------------------------------------------
# УТИЛИТЫ
# ------------------------------------------------------------
def _headers_for_country(country: str, referer: Optional[str] = None) -> Dict[str, str]:
    headers = dict(BASE_HEADERS)
    headers["Accept-Language"] = ACCEPT_LANGUAGE.get(country, "en-US,en;q=0.9")
    if referer:
        headers["Referer"] = referer
    return headers

def _proxy_for_country(country: str) -> Optional[str]:
    env_name = PROXY_ENV[country]
    val = os.getenv(env_name, "").strip()
    return val or None

def _build_client(country: str) -> httpx.AsyncClient:
    """
    Для httpx>=0.28 прокси задаём через transport, а не через параметр proxies.
    """
    proxy = _proxy_for_country(country)
    if proxy:
        logger.info("Использую прокси для %s из %s", country, PROXY_ENV[country])

    # <-- ключевое: прокси через транспорт
    transport = httpx.AsyncHTTPTransport(proxy=proxy) if proxy else httpx.AsyncHTTPTransport()

    return httpx.AsyncClient(
        transport=transport,
        timeout=TIMEOUT,
        follow_redirects=True,
        headers=_headers_for_country(country),
        http2=False,  # не требуем h2
    )

def _first(group_list: List[str]) -> Optional[str]:
    for x in group_list:
        if x:
            return x
    return None

# ------------------------------------------------------------
# ПАРСИНГ ЦЕНЫ
# ------------------------------------------------------------
_PRICE_RE_LIST = [
    re.compile(r'"price"\s*:\s*"(?P<price>[0-9][0-9\.\,\s]*)"', re.IGNORECASE),
    re.compile(r'"priceCurrency"\s*:\s*"(?P<ccy>[A-Z]{3})"', re.IGNORECASE),
    re.compile(r'itemprop\s*=\s*"price"[^>]*content\s*=\s*"(?P<price>[0-9][0-9\.\,\s]*)"', re.IGNORECASE),
    re.compile(r'product:price:amount"\s*content\s*=\s*"(?P<price>[0-9][0-9\.\,\s]*)"', re.IGNORECASE),
    re.compile(r'data-price\s*=\s*"(?P<price>[0-9][0-9\.\,\s]*)"', re.IGNORECASE),
]

_SYMBOL_TO_CCY = {
    "€": "EUR",
    "$": "USD",
    "£": "GBP",
    "¥": "JPY",
    "HK$": "HKD",
    "AED": "AED",
    "₺": "TRY",
    "₸": "KZT",
    "₽": "RUB",
}

_SYMBOLIC_PRICE_RE = re.compile(
    r'(?P<ccy>(HK\$|AED|EUR|USD|GBP|RUB|KZT|TRY|€|\$|£|¥))\s*'
    r'(?P<price>[0-9]{1,3}(?:[ \.,][0-9]{3})*(?:[\,\.][0-9]{2})?)',
    re.IGNORECASE
)

def _normalize_number(s: str) -> str:
    s = s.strip()
    if "," in s and "." in s:
        s = s.replace(" ", "").replace(",", "")
    else:
        if "," in s and "." not in s:
            s = s.replace(" ", "").replace(",", ".")
        else:
            s = s.replace(" ", "")
    return s

def _extract_price(html: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    found_price = None
    found_ccy = None
    for rx in _PRICE_RE_LIST:
        for m in rx.finditer(html):
            gd = m.groupdict()
            if "price" in gd and gd.get("price"):
                found_price = _normalize_number(gd["price"])
            if "ccy" in gd and gd.get("ccy"):
                found_ccy = gd["ccy"].upper()
            if found_price and found_ccy:
                return found_price, found_ccy, m.group(0)

    m2 = _SYMBOLIC_PRICE_RE.search(html)
    if m2:
        price = _normalize_number(m2.group("price"))
        ccy_raw = m2.group("ccy")
        ccy = _SYMBOL_TO_CCY.get(ccy_raw, ccy_raw.replace("$", "USD").replace("€", "EUR").replace("£", "GBP"))
        return price, ccy, m2.group(0)

    try:
        state_match = re.search(r'window\.__STATE__\s*=\s*(\{.*?\})\s*;', html, re.DOTALL)
        if state_match:
            data = json.loads(state_match.group(1))
            text = json.dumps(data)
            m_price = re.search(r'"price"\s*:\s*"(?P<price>[0-9][0-9\.\,\s]*)"', text)
            m_ccy = re.search(r'"currency"\s*:\s*"(?P<ccy>[A-Z]{3})"', text)
            if m_price:
                found_price = _normalize_number(m_price.group("price"))
            if m_ccy:
                found_ccy = m_ccy.group("ccy")
            if found_price:
                return found_price, found_ccy, "STATE_JSON"
    except Exception:
        pass

    return None, None, None

# ------------------------------------------------------------
# ПУБЛИЧНЫЕ ФУНКЦИИ
# ------------------------------------------------------------
async def fetch_price_for_country(url: str, country: str) -> Dict[str, str]:
    assert country in COUNTRIES, f"Unsupported country: {country}"

    async with _build_client(country) as client:
        try:
            # ВАЖНО: тут НЕТ параметра proxy=
            resp = await client.get(url)
        except httpx.ConnectTimeout:
            return {"country": country, "error": "ConnectTimeout"}
        except httpx.ReadTimeout:
            return {"country": country, "error": "ReadTimeout"}
        except Exception as e:
            logger.error("Иная ошибка на %s (%s): %s", url, country, e)
            return {"country": country, "error": str(e)}

    if resp.status_code >= 400:
        return {"country": country, "error": f"HTTP {resp.status_code}"}

    html = resp.text
    price, ccy, raw = _extract_price(html)
    if not price:
        if re.search(r"captcha|challenge|verification", html, re.IGNORECASE):
            return {"country": country, "error": "Captcha/Challenge"}
        return {"country": country, "error": "Price not found"}

    return {"country": country, "price": price, "currency": ccy or "", "debug": raw or ""}

async def get_prices_across_countries(url: str) -> Dict[str, Dict[str, str]]:
    result: Dict[str, Dict[str, str]] = {}
    for c in COUNTRIES:
        data = await fetch_price_for_country(url, c)
        result[c] = data
    return result

def _fmt_row(cols: List[str], widths: List[int]) -> str:
    out = []
    for i, c in enumerate(cols):
        w = widths[i]
        out.append((c or "").ljust(w))
    return " | ".join(out)

def format_prices_table(prices: Dict[str, Dict[str, str]]) -> str:
    headers = ["Country", "Price", "Currency", "Error"]
    rows = []
    for c in COUNTRIES:
        item = prices.get(c, {})
        rows.append([c, item.get("price", ""), item.get("currency", ""), item.get("error", "")])

    widths = [len(h) for h in headers]
    for r in rows:
        for i, cell in enumerate(r):
            widths[i] = max(widths[i], len(str(cell)))

    lines = []
    lines.append(_fmt_row(headers, widths))
    lines.append("-+-".join("-" * w for w in widths))
    for r in rows:
        lines.append(_fmt_row([str(x) for x in r], widths))
    return "\n".join(lines)
