import os
import re
import json
import math
import html
import random
import logging
import asyncio
from urllib.parse import urlparse, urlunparse, quote

import aiohttp
from bs4 import BeautifulSoup
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# -------------------------------
# CONFIG
# -------------------------------
# Токен встроен по твоей просьбе, но лучше хранить в переменной окружения TELEGRAM_BOT_TOKEN
TOKEN = "8406115807:AAGo1ECglKWEVDcb6oSy_yaVuJFmOA_U-ys"
OWNER_ID = 200156484  # только этот пользователь может пользоваться ботом. Поставь None для всех

DEFAULT_COUNTRIES = ["us", "de", "fr", "it", "es", "uk", "hk", "kz"]
DEFAULT_BASE_CCY = "USD"
REQUEST_TIMEOUT = 22
PAUSE_BETWEEN_REQUESTS = (1.2, 2.1)
MAX_RETRIES = 2

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/125.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6) Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) Chrome/124.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) Safari/604.1",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
]

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("price-bot")


# -------------------------------
# HELPERS
# -------------------------------
def extract_links(text: str) -> list[str]:
    url_re = re.compile(r'https?://[^\s<>")]+')
    return url_re.findall(text or "")

def yoox_cod10_from_link(url: str) -> str | None:
    m = re.search(r'/(\d{5,}[A-Z]{2})/item', url)
    if m:
        return m.group(1)
    m = re.search(r'[?&]cod10=([0-9A-Za-z]+)', url)
    if m:
        return m.group(1)
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

def pick_headers() -> dict:
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.8",
        "Connection": "close",
    }

async def gentle_get(session: aiohttp.ClientSession, url: str) -> tuple[int|None, str|None]:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            async with session.get(url, headers=pick_headers(), timeout=REQUEST_TIMEOUT) as r:
                status = r.status
                text = await r.text(errors="ignore")
                if status == 200 and text:
                    return status, text
                if status in (403, 429):
                    await asyncio.sleep(2.0 * attempt)
                else:
                    await asyncio.sleep(1.0)
        except Exception:
            await asyncio.sleep(1.2 * attempt)
    return None, None

def parse_price_yoox(html_text: str) -> tuple[float|None, str|None]:
    soup = BeautifulSoup(html_text, "html.parser")
    for s in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(s.string or "{}")
            if isinstance(data, dict):
                offers = data.get("offers")
                if isinstance(offers, dict):
                    price = offers.get("price")
                    ccy = offers.get("priceCurrency")
                    if price:
                        return float(str(price).replace(",", "").strip()), ccy
        except Exception:
            pass
    text = soup.get_text(" ", strip=True)
    m = re.search(r'([€$£])\s?([\d.,]+)', text)
    if m:
        sym = m.group(1)
        amt = float(m.group(2).replace(",", ""))
        ccy_map = {"€": "EUR", "$": "USD", "£": "GBP"}
        return amt, ccy_map.get(sym)
    return None, None

def parse_price_farfetch(html_text: str) -> tuple[float|None, str|None]:
    soup = BeautifulSoup(html_text, "html.parser")
    for s in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(s.string or "{}")
            if isinstance(data, dict):
                offers = data.get("offers")
                if isinstance(offers, dict):
                    price = offers.get("price")
                    ccy = offers.get("priceCurrency")
                    if price:
                        return float(str(price).replace(",", "").strip()), ccy
        except Exception:
            pass
    text = soup.get_text(" ", strip=True)
    m = re.search(r'([€$£])\s?([\d.,]+)', text)
    if m:
        sym = m.group(1)
        amt = float(m.group(2).replace(",", ""))
        ccy_map = {"€": "EUR", "$": "USD", "£": "GBP"}
        return amt, ccy_map.get(sym)
    return None, None

async def fetch_rates(base=DEFAULT_BASE_CCY) -> dict[str, float]:
    url = f"https://api.exchangerate.host/latest?base={quote(base)}"
    async with aiohttp.ClientSession() as s:
        try:
            async with s.get(url, timeout=10) as r:
                if r.status == 200:
                    data = await r.json()
                    return data.get("rates", {}) or {}
        except Exception:
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
    return amount / r

async def fetch_country_price(session: aiohttp.ClientSession, url: str, domain: str, country: str):
    target_url = set_country_in_url(url, country, domain)
    _, text = await gentle_get(session, target_url)
    await asyncio.sleep(random.uniform(*PAUSE_BETWEEN_REQUESTS))
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

    async with aiohttp.ClientSession() as session:
        for raw in links:
            url = raw if raw.startswith("http") else "https://" + raw
            host = urlparse(url).netloc.lower()
            domain = ".".join(host.split(".")[-2:])
            if domain not in ("yoox.com", "farfetch.com"):
                results.append({"url": url, "error": "Unsupported domain"})
                continue

            rows = []
            for cc in countries:
                country, price, ccy, final_url = await fetch_country_price(session, url, domain, cc)
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
            lines.append(f"❌ <code>{html.escape(item['url'])}</code> → <b>{html.escape(item['error'])}</b>")
            continue
        lines.append(f"🔗 <code>{html.escape(item['url'])}</code>")
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
# TELEGRAM BOT
# -------------------------------
HELP_TEXT = (
    "Пришлите 1+ ссылок на YOOX/FARFETCH (каждую с новой строки) — я сравню цены по странам.\n\n"
    "Команды:\n"
    "• /set_countries us,de,fr,it,es,uk,hk,kz — изменить список стран\n"
    "• /set_base USD — базовая валюта для итогов\n"
    "• /help — помощь\n"
)

STATE = {
    "countries": DEFAULT_COUNTRIES.copy(),
    "base": DEFAULT_BASE_CCY,
}

def is_allowed(user_id: int) -> bool:
    return (OWNER_ID is None) or (int(user_id) == int(OWNER_ID))

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        await update.message.reply_text("⛔ Доступ ограничен владельцем.")
        return
    await update.message.reply_text("Привет! Я аккуратно сравниваю цены на YOOX и FARFETCH.\n\n" + HELP_TEXT)

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

def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or TOKEN
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("set_countries", set_countries))
    app.add_handler(CommandHandler("set_base", set_base))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_links))
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
