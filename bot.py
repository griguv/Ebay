import os
import re
import json
import math
import html as html_mod
import time
import random
import logging
import asyncio
from urllib.parse import urlparse, urlunparse, quote

import aiohttp
from bs4 import BeautifulSoup
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import httpx

# -------------------------------
# LOGGING
# -------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("price-bot")

# -------------------------------
# CONFIG (env)
# -------------------------------
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("Set TELEGRAM_BOT_TOKEN in environment")

OWNER_ID_ENV = os.getenv("TELEGRAM_USER_ID")
OWNER_ID = int(OWNER_ID_ENV) if (OWNER_ID_ENV and OWNER_ID_ENV.isdigit()) else None  # None = разрешить всем

DEFAULT_COUNTRIES = [c.strip() for c in os.getenv(
    "DEFAULT_COUNTRIES",
    "us,de,fr,it,es,uk,hk,kz"
).split(",") if c.strip()]

DEFAULT_BASE_CCY = os.getenv("DEFAULT_BASE_CCY", "USD")

REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "35"))

# Глобальные задержки (для всех доменов)
GLOBAL_DELAY_MIN = float(os.getenv("GLOBAL_DELAY_MIN", "6.0"))
GLOBAL_DELAY_MAX = float(os.getenv("GLOBAL_DELAY_MAX", "12.0"))

MAX_RETRIES = int(os.getenv("MAX_RETRIES", "2"))

# DEBUG HTML в логах
DEBUG_HTML = os.getenv("DEBUG_HTML", "0") == "1"
DEBUG_HTML_LEN = int(os.getenv("DEBUG_HTML_LEN", "1200"))

# Прокси
GLOBAL_PROXY = os.getenv("HTTP_PROXY") or os.getenv("HTTPS_PROXY") or os.getenv("ALL_PROXY")
FARFETCH_PROXY = os.getenv("FARFETCH_PROXY")
YOOX_PROXY = os.getenv("YOOX_PROXY")

# Доменные cooldown’ы (после 403/капчи)
DOMAIN_COOLDOWN = {"farfetch.com": 0.0, "yoox.com": 0.0}
COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", "120"))

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile Safari/604.1",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
]

# -------------------------------
# GLOBAL STATE
# -------------------------------
STATE = {
    "countries": DEFAULT_COUNTRIES.copy(),
    "base": DEFAULT_BASE_CCY,
}

# Глобальная HTTP-сессия (общий CookieJar + заголовки) — выглядит как «один браузер»
_session: aiohttp.ClientSession | None = None

async def init_session():
    """Создаёт общую aiohttp-сессию с cookie и дефолтными заголовками."""
    global _session
    if _session is None:
        jar = aiohttp.CookieJar(unsafe=True)
        _session = aiohttp.ClientSession(cookie_jar=jar)

async def close_session():
    global _session
    if _session is not None:
        await _session.close()
        _session = None

# -------------------------------
# HELPERS (URLs/headers)
# -------------------------------
def extract_links(text: str) -> list[str]:
    url_re = re.compile(r'https?://[^\s<>")]+')
    return url_re.findall(text or "")

def yoox_cod10_from_link(url: str) -> str | None:
    m = re.search(r'/(\d{5,}[A-Z]{2})/item', url)
    if m: return m.group(1)
    m = re.search(r'[?&]cod10=([0-9A-Za-z]+)', url)
    if m: return m.group(1)
    return None

def farfetch_pid_from_link(url: str) -> str | None:
    m = re.search(r'-item-(\d+)\.aspx', url)
    return m.group(1) if m else None

def set_country_in_url(url: str, country: str, domain: str) -> str:
    u = urlparse(url if url.startswith("http") else "https://" + url)
    parts = [p for p in u.path.split("/") if p]
    country = country.lower().strip()

    if domain == "yoox.com":
        cod10 = yoox_cod10_from_link(url)
        if not cod10:
            return url
        new_path = "/" + country + "/" + cod10 + "/item"
        return urlunparse((u.scheme or "https", "www.yoox.com", new_path, "", "", ""))

    if domain == "farfetch.com":
        if len(parts) >= 1 and re.fullmatch(r'^[a-z]{2}$', parts[0]):
            parts[0] = country
        else:
            parts = [country] + parts
        new_path = "/" + "/".join(parts)
        return urlunparse((u.scheme or "https", "www.farfetch.com", new_path, u.params, u.query, u.fragment))

    return url

def pick_headers(country: str | None = None) -> dict:
    lang_map = {
        "it": "it-IT,it;q=0.9", "de": "de-DE,de;q=0.9", "fr": "fr-FR,fr;q=0.9",
        "es": "es-ES,es;q=0.9", "uk": "en-GB,en;q=0.9", "us": "en-US,en;q=0.9",
        "hk": "zh-HK,zh;q=0.8,en;q=0.7", "kz": "ru-RU,ru;q=0.9,en;q=0.7",
    }
    al = lang_map.get((country or "").lower(), "en-US,en;q=0.8")
    ua = random.choice(USER_AGENTS)
    return {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": al,
        "Accept-Encoding": "gzip, deflate, br",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-User": "?1",
        "Sec-Fetch-Dest": "document",
        "Connection": "close",
    }

def pick_proxy_for(url: str) -> str | None:
    host = urlparse(url).netloc.lower()
    domain = ".".join(host.split(".")[-2:])
    if domain == "farfetch.com" and FARFETCH_PROXY:
        return FARFETCH_PROXY
    if domain == "yoox.com" and YOOX_PROXY:
        return YOOX_PROXY
    return GLOBAL_PROXY

# -------------------------------
# HTTP with throttling & anti-bot
# -------------------------------
async def gentle_get(url: str, country: str | None = None) -> tuple[int|None, str|None]:
    """Осторожный GET: глобальная задержка, доменный cooldown, прокси, повторы."""
    await init_session()
    session = _session

    # Глобальная рандомная задержка для всех доменов
    await asyncio.sleep(random.uniform(GLOBAL_DELAY_MIN, GLOBAL_DELAY_MAX))

    # Доменный cooldown
    host = urlparse(url).netloc.lower()
    domain = ".".join(host.split(".")[-2:])
    now = time.time()
    if DOMAIN_COOLDOWN.get(domain, 0) > now:
        await asyncio.sleep(DOMAIN_COOLDOWN[domain] - now + 0.2)

    proxy = pick_proxy_for(url)
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            async with session.get(
                url,
                headers=pick_headers(country),
                timeout=timeout,
                proxy=proxy,
            ) as r:
                status = r.status
                text = await r.text(errors="ignore")

                if DEBUG_HTML and text:
                    head = text[:DEBUG_HTML_LEN].replace("\n", " ")[:DEBUG_HTML_LEN]
                    logger.info(f"[DEBUG HTML {status}] {url} :: {head}")

                blocked = status in (403, 429) or (text and any(k in text.lower() for k in [
                    "captcha", "access denied", "temporarily unavailable",
                    "cloudflare", "akamai", "bot detection"
                ]))
                if blocked:
                    DOMAIN_COOLDOWN[domain] = time.time() + COOLDOWN_SECONDS
                    await asyncio.sleep(1.5 * attempt)
                    continue

                if status == 200 and text:
                    return status, text

                await asyncio.sleep(1.0)
        except Exception as e:
            logger.warning(f"gentle_get error (attempt {attempt}) on {url}: {e}")
            await asyncio.sleep(1.2 * attempt)
    return None, None

# -------------------------------
# PARSERS
# -------------------------------
def _parse_number_localized(s: str) -> float | None:
    if not s:
        return None
    t = s.replace("\xa0", " ").strip()
    m = re.search(r'([\d.,]+)', t)
    if not m:
        return None
    num = m.group(1)
    if "." in num and "," in num:
        num = num.replace(".", "").replace(",", ".")
    elif "," in num and "." not in num:
        num = num.replace(",", ".")
    try:
        return float(num)
    except Exception:
        return None

def _guess_ccy(text: str) -> str | None:
    up = (text or "").upper()
    if "€" in up or "EUR" in up: return "EUR"
    if "£" in up or "GBP" in up: return "GBP"
    if "HK$" in up or "HKD" in up: return "HKD"
    if "$" in up or "USD" in up:  return "USD"
    m = re.search(r'\b([A-Z]{3})\b', up)
    return m.group(1) if m else None

def parse_price_yoox(html_text: str) -> tuple[float|None, str|None]:
    soup = BeautifulSoup(html_text, "html.parser")
    # JSON-LD
    for s in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(s.string or "{}")
        except Exception:
            continue
        nodes = data if isinstance(data, list) else [data]
        for node in nodes:
            if not isinstance(node, dict): continue
            offers = node.get("offers")
            if isinstance(offers, dict):
                price = offers.get("price") or offers.get("lowPrice")
                ccy = offers.get("priceCurrency")
                if price:
                    pn = _parse_number_localized(str(price))
                    if pn is not None: return pn, ccy
            ps = node.get("priceSpecification")
            if isinstance(ps, dict):
                price = ps.get("price")
                ccy = ps.get("priceCurrency")
                if price:
                    pn = _parse_number_localized(str(price))
                    if pn is not None: return pn, ccy
    # Internal JSON
    m = re.search(r'"(formattedFinalPrice|finalPrice|price)"\s*:\s*"?(?P<p>[\d.,]+)"?.{0,120}?"(currency|priceCurrency)"\s*:\s*"(?P<c>[A-Z]{3})"', html_text)
    if m:
        pn = _parse_number_localized(m.group("p"))
        return (pn, m.group("c")) if pn is not None else (None, None)
    # Visible
    cand = soup.select_one(".finalPrice, .price, .priceContainer span, [itemprop='price']")
    if cand:
        txt = cand.get_text(" ", strip=True)
        pn = _parse_number_localized(txt)
        if pn is not None: return pn, _guess_ccy(txt)
    # Fallback
    txt = soup.get_text(" ", strip=True)
    m2 = re.search(r'(HK\$|[€$£])\s?([\d.,]+)', txt)
    if m2:
        pn = _parse_number_localized(m2.group(0))
        return pn, _guess_ccy(m2.group(0))
    return None, None

def parse_price_farfetch(html_text: str) -> tuple[float|None, str|None]:
    soup = BeautifulSoup(html_text, "html.parser")
    # JSON-LD
    for s in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(s.string or "{}")
        except Exception:
            continue
        nodes = data if isinstance(data, list) else [data]
        for node in nodes:
            if not isinstance(node, dict): continue
            offers = node.get("offers")
            if isinstance(offers, dict):
                price = offers.get("price") or offers.get("lowPrice")
                ccy = offers.get("priceCurrency")
                if price:
                    pn = _parse_number_localized(str(price))
                    if pn is not None: return pn, ccy
            ps = node.get("priceSpecification")
            if isinstance(ps, dict):
                price = ps.get("price")
                ccy = ps.get("priceCurrency")
                if price:
                    pn = _parse_number_localized(str(price))
                    if pn is not None: return pn, ccy
    # Next.js/internal JSON
    m = re.search(r'"price"\s*:\s*"?(?P<p>[\d.,]+)"?\s*,\s*"(?:currency|priceCurrency)"\s*:\s*"(?P<c>[A-Z]{3})"', html_text)
    if m:
        pn = _parse_number_localized(m.group("p"))
        return (pn, m.group("c")) if pn is not None else (None, None)
    # Visible
    cand = soup.select_one('[data-testid="price"], [data-test="price"], ._d85b45, ._e5f6a7, .price')
    if cand:
        txt = cand.get_text(" ", strip=True)
        pn = _parse_number_localized(txt)
        if pn is not None: return pn, _guess_ccy(txt)
    # Fallback
    txt = soup.get_text(" ", strip=True)
    m2 = re.search(r'([€$£]|HK\$)\s?([\d.,]+)', txt)
    if m2:
        pn = _parse_number_localized(m2.group(0))
        return pn, _guess_ccy(m2.group(0))
    return None, None

# -------------------------------
# FX RATES
# -------------------------------
async def fetch_rates(base=DEFAULT_BASE_CCY) -> dict[str, float]:
    url = f"https://api.exchangerate.host/latest?base={quote(base)}"
    try:
        # Используем ту же глобальную сессию (через прокси, если задан)
        await init_session()
        timeout = aiohttp.ClientTimeout(total=15)
        async with _session.get(url, timeout=timeout, proxy=GLOBAL_PROXY) as r:
            if r.status == 200:
                data = await r.json()
                return data.get("rates", {}) or {}
    except Exception as e:
        logger.warning(f"fetch_rates failed: {e}")
        return {}
    return {}

def convert(amount: float, ccy_from: str, base: str, rates: dict[str, float]) -> float | None:
    if amount is None or ccy_from is None:
        return None
    ccy_from = ccy_from.upper()
    base = base.upper()
    if ccy_from == base:
        return amount
    r = rates.get(ccy_from)
    if not r:
        return None
    # rates: 1 base = r ccy? (у exchangerate.host base=X, rates[to])
    # Нам нужно amount в base: amount_in_base = amount / rate(ccy_from)
    return amount / r

# -------------------------------
# MAIN PRICE WORKFLOW
# -------------------------------
async def fetch_country_price(url: str, domain: str, country: str):
    target_url = set_country_in_url(url, country, domain)
    _, text = await gentle_get(target_url, country=country)
    if not text:
        return country, None, None, target_url
    if domain == "yoox.com":
        price, ccy = parse_price_yoox(text)
    else:
        price, ccy = parse_price_farfetch(text)
    return country, price, ccy, target_url

async def compare_links(links: list[str], countries: list[str], base_ccy=DEFAULT_BASE_CCY):
    rates = await fetch_rates(base_ccy)
    results = []
    totals = {cc: 0.0 for cc in countries}
    ok = {cc: True for cc in countries}

    for raw in links:
        url = raw if raw.startswith("http") else "https://" + raw
        host = urlparse(url).netloc.lower()
        domain = ".".join(host.split(".")[-2:])
        if domain not in ("yoox.com", "farfetch.com"):
            results.append({"url": url, "error": "Unsupported domain"})
            continue

        rows = []
        for cc in countries:
            country, price, ccy, final_url = await fetch_country_price(url, domain, cc)
            base_price = convert(price, ccy, base_ccy, rates) if (price and ccy) else None
            if base_price is None:
                ok[country] = False
            else:
                totals[country] += base_price
            rows.append({"country": country, "price": price, "ccy": ccy, "base_price": base_price, "final_url": final_url})

        results.append({"url": url, "rows": rows})

    ranking = sorted(countries, key=lambda cc: (math.inf if not ok[cc] else totals[cc]))
    return results, totals, ok, ranking, base_ccy

def friendly_cc(cc: str) -> str:
    flags = {
        "us": "🇺🇸 US", "de": "🇩🇪 DE", "fr": "🇫🇷 FR", "it": "🇮🇹 IT",
        "es": "🇪🇸 ES", "uk": "🇬🇧 UK", "hk": "🇭🇰 HK", "kz": "🇰🇿 KZ",
    }
    return flags.get(cc, cc.upper())

def fmt_money(v: float | None, ccy: str | None) -> str:
    if v is None or ccy is None:
        return "—"
    return f"{v:,.2f} {ccy}"

def fmt_base(v: float | None, base: str) -> str:
    if v is None:
        return "—"
    return f"{v:,.2f} {base}"

def build_table(per_link, totals, ok, ranking, base):
    lines = []
    for item in per_link:
        if "error" in item:
            lines.append(f"❌ <code>{html_mod.escape(item['url'])}</code> → <b>{html_mod.escape(item['error'])}</b>")
            continue
        lines.append(f"🔗 <code>{html_mod.escape(item['url'])}</code>")
        for r in item["rows"]:
            lines.append(
                f"  • {friendly_cc(r['country'])}: {fmt_money(r['price'], r['ccy'])} "
                f"(≈ {fmt_base(r['base_price'], base)})"
            )
    lines.append("")
    lines.append("<b>Итог по странам (сумма по всем ссылкам):</b>")
    for cc in totals:
        badge = "✅" if ok[cc] else "⚠️ есть пропуски"
        lines.append(f"  • {friendly_cc(cc)}: {totals[cc]:,.2f} {base} {badge}")
    lines.append("")
    lines.append("<b>Где выгоднее:</b>")
    for i, cc in enumerate(ranking, 1):
        val = "н/д" if not ok[cc] else f"{totals[cc]:,.2f} {base}"
        lines.append(f"{i}) {friendly_cc(cc)} — {val}")
    return "\n".join(lines)

# -------------------------------
# TELEGRAM BOT HANDLERS
# -------------------------------
HELP_TEXT = (
    "Пришлите 1+ ссылок на YOOX/FARFETCH (каждую с новой строки) — я сравню цены по странам.\n\n"
    "Команды:\n"
    "• /set_countries us,de,fr,it,es,uk,hk,kz — изменить список стран\n"
    "• /set_base USD — базовая валюта для итогов\n"
    "• /help — помощь\n"
)

def is_allowed(user_id: int) -> bool:
    return (OWNER_ID is None) or (int(user_id) == int(OWNER_ID))

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        await update.message.reply_text("⛔ Доступ ограничен владельцем.")
        return
    msg = "Привет! Я аккуратно сравниваю цены на YOOX и FARFETCH.\n\n" + HELP_TEXT
    px = pick_proxy_for("https://www.farfetch.com")
    gpx = GLOBAL_PROXY
    if gpx or px:
        msg += "\n\n🔌 Прокси активен."
    await update.message.reply_text(msg)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    await update.message.reply_text(HELP_TEXT)

async def set_countries(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    args = context.args or []
    raw = " ".join(args).strip()
    if not raw:
        await update.message.reply_text("Пример: /set_countries us,de,fr,it,es,uk,hk,kz")
        return
    new_list = [x.lower() for x in re.split(r'[\s,]+', raw) if x.strip()]
    STATE["countries"] = new_list
    await update.message.reply_text("Буду сравнивать для: " + ", ".join(new_list).upper())

async def set_base(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    args = context.args or []
    if not args:
        await update.message.reply_text("Пример: /set_base USD")
        return
    base = args[0].upper().strip()
    if not re.fullmatch(r'[A-Z]{3}', base):
        await update.message.reply_text("Неверный формат валюты.")
        return
    STATE["base"] = base
    await update.message.reply_text(f"Базовая валюта: {base}")

async def handle_links(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    text = update.message.text or ""
    links = extract_links(text)
    if not links:
        await update.message.reply_text("Пришли ссылки на товары.")
        return
    await update.message.reply_text("Сравниваю цены...")

    try:
        results, totals, ok, ranking, base = await compare_links(links, STATE["countries"], STATE["base"])
        table = build_table(results, totals, ok, ranking, base)
        await update.message.reply_html(table, disable_web_page_preview=True)
    except Exception as e:
        logger.exception("compare_links failed")
        await update.message.reply_text(f"Ошибка: {e}")

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Unhandled error: %s", context.error)

# -------------------------------
# MAIN
# -------------------------------
def main():
    # Отключаем вебхук ПЕРЕД стартом polling — убирает "Conflict"
    try:
        httpx.post(f"https://api.telegram.org/bot{TOKEN}/deleteWebhook", timeout=10).raise_for_status()
        logger.info("Webhook удалён перед запуском polling.")
    except Exception as e:
        logger.warning("Не удалось удалить webhook: %s", e)

    app = Application.builder().token(TOKEN).build()
    app.add_error_handler(on_error)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("set_countries", set_countries))
    app.add_handler(CommandHandler("set_base", set_base))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_links))

    # Аккуратно закрываем aiohttp-сессию при остановке
    async def _post_stop(_: Application):
        await close_session()
        logger.info("HTTP session closed.")

    app.post_shutdown = _post_stop  # PTB вызовет при остановке

    # drop_pending_updates=True — не читаем старые апдейты
    app.run_polling(drop_pending_updates=True, close_loop=False)

if __name__ == "__main__":
    main()
