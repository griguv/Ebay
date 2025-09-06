import os
import re
import httpx
import logging
from bs4 import BeautifulSoup
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

# -------------------- ЛОГИ --------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("price-bot")

# -------------------- ПАРАМЕТРЫ --------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN")   # именно BOT_TOKEN
CHAT_IDS = os.environ.get("CHAT_IDS", "").split(",")
PROXY_URL = os.environ.get("PROXY_URL")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    )
}

# -------------------- ПРОКСИ --------------------
proxies = None
if PROXY_URL:
    proxies = {
        "http://": PROXY_URL,
        "https://": PROXY_URL,
    }

# -------------------- ПАРСИНГ --------------------
def parse_price_farfetch(html: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    price = None

    # Новый Farfetch
    span = soup.find("span", {"data-tstid": "priceInfo-original"})
    if span:
        price = span.get_text(strip=True)

    if not price:
        span = soup.find("span", string=re.compile(r"\$|€|£"))
        if span:
            price = span.get_text(strip=True)

    return price


def parse_price_ebay(html: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    price = None

    span = soup.find("span", {"itemprop": "price"})
    if span:
        price = span.get_text(strip=True)

    if not price:
        span = soup.find("span", class_=re.compile(r"^\s*ux-textspans\s*"))
        if span:
            price = span.get_text(strip=True)

    return price


def get_price(url: str) -> str | None:
    try:
        with httpx.Client(headers=HEADERS, proxies=proxies, timeout=15) as client:
            r = client.get(url)
            if r.status_code != 200:
                return None
            html = r.text

            if "farfetch" in url:
                return parse_price_farfetch(html)
            elif "ebay" in url:
                return parse_price_ebay(html)
            else:
                return None
    except Exception as e:
        logger.error("Ошибка при запросе %s: %s", url, e)
        return None

# -------------------- ХЕНДЛЕРЫ --------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Отправь ссылку на товар (Farfetch или eBay).")


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    if not url.startswith("http"):
        return

    await update.message.reply_text("Ищу цену...")

    price = get_price(url)
    if price:
        text = f"Цена: {price}\nСсылка: {url}"
    else:
        text = f"Не удалось найти цену по ссылке: {url}"

    await update.message.reply_text(text)

    for chat_id in CHAT_IDS:
        if chat_id:
            try:
                await context.bot.send_message(chat_id=chat_id.strip(), text=text)
            except Exception as e:
                logger.error("Не удалось отправить сообщение в %s: %s", chat_id, e)

# -------------------- MAIN --------------------
def main():
    if not BOT_TOKEN:
        raise RuntimeError("Не задан BOT_TOKEN в переменных окружения!")

    try:
        httpx.post(f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook", timeout=10)
        logger.info("Webhook удалён перед запуском polling.")
    except Exception as e:
        logger.warning("Ошибка при удалении webhook: %s", e)

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
