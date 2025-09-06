import os
import re
import asyncio
import logging
from bs4 import BeautifulSoup
import httpx
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

# --- Логирование ---
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("price-bot")

# --- Настройки ---
TOKEN = os.getenv("BOT_TOKEN", "ТОКЕН_ТУТ")  # токен берём из переменной окружения Render
CHAT_ID = os.getenv("CHAT_ID", "")           # айди чатов через запятую
PROXY = os.getenv("PROXY")                   # если нужно – задаём в Render

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0.0.0 Safari/537.36"
}

# --- HTTP клиент ---
client_opts = {"headers": HEADERS, "timeout": 20.0}
if PROXY:
    client_opts["proxies"] = {"all://": PROXY}

http_client = httpx.AsyncClient(**client_opts)

# --- Парсинг цены ---
def extract_price(soup: BeautifulSoup) -> str | None:
    """Извлекает цену с разных сайтов."""
    text = soup.get_text(" ", strip=True)

    # Farfetch
    ff_price = soup.select_one("p[data-tstid='finalPrice']")
    if ff_price:
        return ff_price.get_text(strip=True)

    # eBay
    ebay_price = soup.select_one("#prcIsum, .x-price-approx__price, .x-price-approx")
    if ebay_price:
        return ebay_price.get_text(strip=True)

    # OutdoorDogSupply (In stock)
    if "in stock" in text.lower():
        return "In stock"

    # Универсальный поиск цифр
    m = re.search(r"(\d[\d\s.,]*)(?:\$|USD|руб|₽|€)", text)
    if m:
        return m.group(0)

    return None

# --- Команды ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Отправь ссылку на товар, я проверю цену.")

async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    if not url.startswith("http"):
        await update.message.reply_text("⚠️ Это не ссылка")
        return

    try:
        r = await http_client.get(url, follow_redirects=True)
        log.info("[DEBUG HTML %s] %s :: %s", r.status_code, url, r.text[:300])
        soup = BeautifulSoup(r.text, "html.parser")
        price = extract_price(soup)
        if price:
            await update.message.reply_text(f"💰 Цена: {price}")
        else:
            await update.message.reply_text("❌ Не удалось найти цену на странице.")
    except Exception as e:
        log.error("Ошибка парсинга %s: %s", url, e)
        await update.message.reply_text("⚠️ Ошибка при загрузке страницы.")

# --- Основной запуск ---
async def main():
    app = Application.builder().token(TOKEN).build()

    try:
        await app.bot.delete_webhook(drop_pending_updates=False)
        log.info("Webhook удалён перед запуском polling.")
    except Exception:
        pass

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))

    # Последовательность для PTB 21
    await app.initialize()
    await app.start()
    await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)

    # Заменено .wait() на .idle()
    await app.updater.idle()

    await app.stop()
    await app.shutdown()
    await http_client.aclose()
    log.info("HTTP session closed.")

if __name__ == "__main__":
    asyncio.run(main())
