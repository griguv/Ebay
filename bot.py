# bot.py
import asyncio
import json
import logging
import os
import re
from typing import Optional, Tuple

import httpx
from bs4 import BeautifulSoup
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

# ----------------------- ЛОГГИРОВАНИЕ -----------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("price-bot")

# ----------------------- ENV -----------------------
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("Set TELEGRAM_BOT_TOKEN or BOT_TOKEN in environment")

ALLOWED_USER_ID = os.getenv("TELEGRAM_USER_ID")  # необязательно
DEBUG_HTML = os.getenv("DEBUG_HTML", "0") == "1"
try:
    DEBUG_HTML_LEN = int(os.getenv("DEBUG_HTML_LEN", "1800"))
except Exception:
    DEBUG_HTML_LEN = 1800

# Прокси оставляю как есть (используем системные переменные).
HTTP_PROXY = os.getenv("HTTP_PROXY") or os.getenv("http_proxy")
HTTPS_PROXY = os.getenv("HTTPS_PROXY") or os.getenv("https_proxy")

PROXIES = None
if HTTP_PROXY or HTTPS_PROXY:
    PROXIES = {
        "http://": HTTP_PROXY or HTTPS_PROXY,
        "https://": HTTPS_PROXY or HTTP_PROXY,
    }

# Таймауты HTTP
TIMEOUT = httpx.Timeout(25.0, connect=25.0, read=25.0)


# ======================= ПАРСИНГ ЦЕН =======================

def _meta_price(soup: BeautifulSoup) -> Optional[Tuple[str, str]]:
    """Ищем price через meta itemprop."""
    price_tag = soup.find("meta", {"itemprop": "price"})
    curr_tag = soup.find("meta", {"itemprop": "priceCurrency"})
    if price_tag and curr_tag and price_tag.get("content"):
        return price_tag["content"].strip(), curr_tag.get("content", "").strip()
    # альтернатива: og:price / product:price
    og_price = soup.find("meta", {"property": "product:price:amount"}) or soup.find(
        "meta", {"property": "og:price:amount"}
    )
    og_curr = soup.find("meta", {"property": "product:price:currency"}) or soup.find(
        "meta", {"property": "og:price:currency"}
    )
    if og_price and og_curr and og_price.get("content"):
        return og_price["content"].strip(), og_curr.get("content", "").strip()
    return None


def _farfetch_next_data(soup: BeautifulSoup) -> Optional[Tuple[str, str]]:
    """Парсим цену из __NEXT_DATA__ на Farfetch."""
    script = soup.find("script", {"id": "__NEXT_DATA__"})
    if not script or not script.string:
        return None
    try:
        data = json.loads(script.string)
        # Встречающиеся структуры:
        # props.pageProps.product.price.{value,currency}
        # props.pageProps.product.prices.{price,finalPrice,currency}
        pp = data.get("props", {}).get("pageProps", {})
        product = pp.get("product", {}) or pp.get("productData", {}) or {}
        # вариант 1
        p1 = product.get("price") or {}
        if isinstance(p1, dict) and ("value" in p1 or "amount" in p1):
            value = p1.get("value") or p1.get("amount")
            curr = p1.get("currency") or p1.get("currencyCode")
            if value and curr:
                return str(value), str(curr)
        # вариант 2
        p2 = product.get("prices") or {}
        if isinstance(p2, dict):
            value = p2.get("finalPrice") or p2.get("price")
            curr = p2.get("currency") or p2.get("currencyCode")
            if value and curr:
                return str(value), str(curr)
    except Exception:
        return None
    return None


def _fallback_guess(html: str) -> Optional[str]:
    """Грубый резервный поиск: 123.45 USD/€/$ или символ перед/после числа."""
    # $ 1,234.56 / € 1.234,56 / 1234 USD / 1 234,56 EUR
    pattern = r"(?:(?:USD|EUR|GBP|RUB|UAH|PLN|KZT|CHF|CAD|AUD)\b|[$€£₽₴zł₸])\s*[\d\s.,]+|[\d\s.,]+\s*(?:USD|EUR|GBP|RUB|UAH|PLN|KZT|CHF|CAD|AUD)\b"
    m = re.search(pattern, html, re.IGNORECASE)
    if m:
        return m.group(0).strip()
    return None


def extract_price(html: str, url: str) -> Optional[str]:
    """Единая точка извлечения цены из HTML. Возвращает строку '123 EUR' или '$123'."""
    soup = BeautifulSoup(html, "html.parser")

    # 1) meta-признаки (универсальный безопасный путь)
    meta = _meta_price(soup)
    if meta:
        value, curr = meta
        if value and curr:
            return f"{value} {curr}"

    # 2) Farfetch: JSON __NEXT_DATA__
    if "farfetch.com" in url:
        ff = _farfetch_next_data(soup)
        if ff:
            value, curr = ff
            return f"{value} {curr}"

    # 3) Схемы schema.org (на всякий случай)
    #   <span itemprop="price" content="...">, <meta itemprop="priceCurrency" ...>
    span_price = soup.find(attrs={"itemprop": "price"})
    span_curr = soup.find(attrs={"itemprop": "priceCurrency"})
    if span_price:
        pv = (span_price.get("content") or span_price.get_text(strip=True) or "").strip()
        cv = ""
        if span_curr:
            cv = (span_curr.get("content") or span_curr.get_text(strip=True) or "").strip()
        if pv and cv:
            return f"{pv} {cv}"
        if pv:
            return pv

    # 4) Fallback – эвристика по тексту страницы
    rough = _fallback_guess(html)
    if rough:
        return rough

    return None


# ======================= HTTP =======================

async def fetch_html(url: str) -> Tuple[str, int]:
    headers = {
        # максимально нейтральный UA
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    async with httpx.AsyncClient(proxies=PROXIES, timeout=TIMEOUT, follow_redirects=True, headers=headers) as client:
        r = await client.get(url)
        status = r.status_code
        html = r.text

        if DEBUG_HTML:
            sample = html[:DEBUG_HTML_LEN].replace("\n", "")
            log.info("[DEBUG HTML %s] %s :: %s", status, url, sample)

        return html, status


# ======================= TELEGRAM HANDLERS =======================

def _ensure_allowed(update: Update) -> bool:
    """Пускаем только заданного user_id (если он указан)."""
    if not ALLOWED_USER_ID:
        return True
    try:
        return str(update.effective_user.id) == str(ALLOWED_USER_ID)
    except Exception:
        return False


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _ensure_allowed(update):
        return
    await update.message.reply_text("Пришлите ссылку на товар — отвечу ценой.")


async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _ensure_allowed(update):
        return
    text = (update.message.text or "").strip()

    # Берём первую ссылку из сообщения
    m = re.search(r"https?://\S+", text)
    if not m:
        await update.message.reply_text("Не вижу ссылки. Пришлите URL на страницу товара.")
        return

    url = m.group(0)

    try:
        html, status = await fetch_html(url)
    except Exception as e:
        log.exception("fetch failed")
        await update.message.reply_text(f"Ошибка загрузки страницы: {e}")
        return

    if status >= 400:
        await update.message.reply_text(f"HTTP {status} при загрузке страницы.")
        return

    price = extract_price(html, url)
    if price:
        await update.message.reply_html(f"💸 <b>Цена:</b> <code>{price}</code>\n🔗 <a href=\"{url}\">страница</a>")
    else:
        await update.message.reply_text("Не удалось найти цену 😔. Включи DEBUG_HTML_LEN побольше и пришли лог — допилю селектор.")


# ======================= MAIN =======================

async def main():
    # Старт Telegram
    app = Application.builder().token(TOKEN).build()

    # Снимем webhook (на всякий случай)
    try:
        await app.bot.delete_webhook(drop_pending_updates=False)
        log.info("Webhook удалён перед запуском polling.")
    except Exception:
        pass

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))

    # run_polling сам создаёт event loop, но мы уже в async main → используем start/idle
    await app.start()
    await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    await app.updater.wait()
    await app.stop()
    log.info("HTTP session closed.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
