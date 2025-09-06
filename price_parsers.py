import asyncio
import math
import re
from typing import Dict, Tuple, Optional

import httpx
from bs4 import BeautifulSoup

from utils import logger, DEFAULT_UA, COUNTRIES, get_proxy_config

# ---------- ВСПОМОГАТЕЛЬНОЕ ----------

CURRENCY_SYMBOLS = {
    "$": "USD",
    "US$": "USD",
    "€": "EUR",
    "£": "GBP",
    "HK$": "HKD",
    "₸": "KZT",
    "KZT": "KZT",
    "USD": "USD",
    "EUR": "EUR",
    "GBP": "GBP",
    "HKD": "HKD",
}

# эвристика извлечения числа из текста цены
PRICE_NUM_RE = re.compile(r"(\d{1,3}(?:[\s.,]\d{3})*(?:[.,]\d{2})?)")

def _normalize_number(num_str: str) -> float:
    """
    Приводим строку с ценой к float.
    Примеры: "1 234,56" -> 1234.56 ; "1,234.56" -> 1234.56 ; "1234" -> 1234.0
    """
    s = num_str.strip()
    # если есть и '.' и ',' — смотрим, какая ближе к концу (скорее всего — десятичный разделитель)
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            # десятичный — запятая
            s = s.replace(".", "").replace(",", ".")
        else:
            # десятичная — точка
            s = s.replace(",", "")
    else:
        # только один разделитель
        if s.count(",") == 1 and s.count(".") == 0:
            s = s.replace(",", ".")
        else:
            s = s.replace(",", "")
    try:
        return float(s)
    except Exception:
        return math.nan

async def _fetch_html(client: httpx.AsyncClient, url: str, headers: dict, cookies: dict) -> Tuple[str, int]:
    """
    Достаём HTML с повторами, легкий антибот-хардениг:
    - таймауты
    - рандомный UA можно добавить при необходимости
    - несколько попыток
    """
    for attempt in range(1, 5):
        try:
            r = await client.get(url, headers=headers, cookies=cookies, timeout=20)
            # Farfetch/Yoox иногда отдают 403/429 — подождём и повторим
            if r.status_code in (403, 429, 503):
                await asyncio.sleep(1.5 * attempt)
                continue
            return r.text, r.status_code
        except Exception as e:
            logger.warning(f"GET {url} failed (attempt {attempt}): {e}")
            await asyncio.sleep(1.2 * attempt)
    return "", 0

def _extract_price_generic(html: str) -> Tuple[Optional[float], Optional[str]]:
    """
    Универсальный парсер цены:
    - ищет meta[itemprop=price], meta[property='product:price:amount'], data-price атрибуты
    - если не нашли — пробует regex по видимому тексту
    Возвращает (число, валюта|None).
    """
    soup = BeautifulSoup(html, "html.parser")

    # 1) Очевидные мета-теги
    metas = [
        ("meta", {"itemprop": "price"}, "content"),
        ("meta", {"property": "product:price:amount"}, "content"),
        ("meta", {"name": "twitter:data1"}, "content"),  # иногда Twitter card
        ("meta", {"name": "twitter:label1"}, "content"),
    ]
    for tag, attrs, attr_name in metas:
        el = soup.find(tag, attrs=attrs)
        if el and el.get(attr_name):
            num_match = PRICE_NUM_RE.search(el.get(attr_name))
            if num_match:
                value = _normalize_number(num_match.group(1))
                if not math.isnan(value):
                    # попытаемся понять валюту вокруг
                    around = el.get(attr_name)
                    currency = None
                    for sym, code in CURRENCY_SYMBOLS.items():
                        if sym in around:
                            currency = code
                            break
                    return value, currency

    # 2) data-testid/aria-label/price-like
    #    (общее эвристическое извлечение)
    candidates_text = []
    for attr in ["data-testid", "aria-label", "class", "id"]:
        for node in soup.find_all(attrs={attr: re.compile(r"(price|amount|final|sale)", re.I)}):
            candidates_text.append(node.get_text(" ", strip=True))
    # плюс добавим явные <span/itemprop="price">
    for node in soup.select('[itemprop="price"], span.price, div.price'):
        t = node.get_text(" ", strip=True)
        if t:
            candidates_text.append(t)

    # ищем внутри кандидатов
    for text in candidates_text:
        num_match = PRICE_NUM_RE.search(text)
        if not num_match:
            continue
        value = _normalize_number(num_match.group(1))
        if math.isnan(value):
            continue
        currency = None
        # попытка определить валюту по символу
        for sym, code in CURRENCY_SYMBOLS.items():
            if sym in text:
                currency = code
                break
        return value, currency

    # 3) последний шанс — regex по всему HTML (дорого, но работает как fallback)
    text = soup.get_text(" ", strip=True)[:40000]  # ограничим объём
    m = PRICE_NUM_RE.search(text)
    if m:
        value = _normalize_number(m.group(1))
        if not math.isnan(value):
            currency = None
            for sym, code in CURRENCY_SYMBOLS.items():
                if sym in text:
                    currency = code
                    break
            return value, currency

    return None, None

# ---------- САЙТОВЫЕ ТОНКОСТИ ----------

def _country_cookies_for_farfetch(country: str) -> dict:
    """
    Farfetch любит геокуки. Это простая, но часто работающая связка.
    Валюту не жёстко фиксируем — иногда приходит автоматически.
    """
    return {
        "ff_geo_country": country,
        "ff_geo_region": country,
        # Можно подсказать язык (необязательно):
        # "ff_language": "en",
    }

def _country_headers_for_farfetch(country: str) -> dict:
    # Пара фирменных подсказок + UA
    return {
        "user-agent": DEFAULT_UA,
        "accept-language": "en-US,en;q=0.9",
        "x-ff-currency": "USD" if country == "US" else "EUR",
        "x-ff-country": country,
        "x-ff-device": "desktop",
    }

def _country_cookies_for_yoox(country: str) -> dict:
    # У Yoox своя кухня, но часто хватает accept-language и пары гео-кук:
    return {
        "shippingCountry": country,
        "geoCountry": country,
    }

def _country_headers_for_yoox(country: str) -> dict:
    return {
        "user-agent": DEFAULT_UA,
        "accept-language": "en-US,en;q=0.9",
    }

async def get_price_for_country(url: str, country: str, client: httpx.AsyncClient) -> Tuple[Optional[float], Optional[str], int]:
    """
    Возвращает (цена, валюта, http_status_code) для конкретной страны.
    """
    is_farfetch = "farfetch.com" in url
    is_yoox = "yoox.com" in url

    if is_farfetch:
        headers = _country_headers_for_farfetch(country)
        cookies = _country_cookies_for_farfetch(country)
    elif is_yoox:
        headers = _country_headers_for_yoox(country)
        cookies = _country_cookies_for_yoox(country)
    else:
        return None, None, 0

    html, status = await _fetch_html(client, url, headers, cookies)
    if status == 0:
        return None, None, 0
    if status in (403, 429, 503):
        # На всякий случай — могли поймать антибот
        return None, None, status

    price, currency = _extract_price_generic(html)
    return price, currency, status

async def get_prices_across_countries(url: str) -> Dict[str, Dict]:
    """
    Для одной ссылки собираем цены по всем фиксированным странам.
    """
    proxies = get_proxy_config()
    async with httpx.AsyncClient(proxies=proxies, follow_redirects=True) as client:
        tasks = [get_price_for_country(url, c, client) for c in COUNTRIES]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    out = {}
    for country, res in zip(COUNTRIES, results):
        if isinstance(res, Exception):
            logger.error(f"[{country}] {url} -> exception: {res}")
            out[country] = {"price": None, "currency": None, "status": 0, "error": str(res)}
            continue
        price, currency, status = res
        out[country] = {"price": price, "currency": currency, "status": status}
    return out
