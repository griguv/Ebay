import os
import asyncio
import logging
import httpx
from bs4 import BeautifulSoup
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# 🔹 Логирование
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger("price-bot")

# 🔹 Конфигурация из переменных окружения
TOKEN = os.getenv("BOT_TOKEN")
CHAT_IDS = os.getenv("CHAT_ID", "").split(",")
PROXY_URL = os.getenv("PROXY_URL")  # https://ip:port@login:pass

if not TOKEN:
    raise RuntimeError("❌ Не найден BOT_TOKEN в переменных окружения")

# 🔹 HTTP клиент с поддержкой прокси
client_args = {}
if PROXY_URL:
    client_args["proxies"] = PROXY_URL

client = httpx.AsyncClient(**client_args, timeout=30)

# Список отслеживаемых товаров
PRODUCT_URLS = [
    "https://www.farfetch.com/us/shopping/women/christopher-esber--item-31310073.aspx?storeid=10047",
    "https://www.ebay.com/itm/166907886162",
]

# 🔹 Получение цены с сайта
async def fetch_price(url: str) -> str:
    try:
        resp = await client.get(url)
        resp.raise_for_status()
        logger.info("[DEBUG HTML %s] %s :: %s", resp.status_code, url, resp.text[:500])

        soup = BeautifulSoup(resp.text, "html.parser")

        # Farfetch
        price = soup.select_one("[data-tstid='priceInfo'] span")
        if price:
            return price.get_text(strip=True)

        # eBay
        ebay_price = soup.select_one("#prcIsum, .x-price-approx__price, .x-price-approx")
        if ebay_price:
            return ebay_price.get_text(strip=True)

        return "❓ Цена не найдена"
    except Exception as e:
        logger.error("Ошибка при запросе %s: %s", url, e)
        return f"⚠️ Ошибка: {e}"

# 🔹 Команда /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет! Я бот для отслеживания цен 🛒")

# 🔹 Проверка цен и отправка в Telegram
async def check_prices(app: Application):
    while True:
        for url in PRODUCT_URLS:
            price = await fetch_price(url)
            text = f"🔗 {url}\n💰 {price}"
            for chat_id in CHAT_IDS:
                if chat_id.strip():
                    try:
                        await app.bot.send_message(chat_id=chat_id.strip(), text=text)
                    except Exception as e:
                        logger.error("Ошибка отправки в чат %s: %s", chat_id, e)
        await asyncio.sleep(300)  # каждые 5 минут

# 🔹 Основной запуск
async def main():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))

    # Убираем вебхук и запускаем polling
    await app.bot.delete_webhook()
    logger.info("Webhook удалён перед запуском polling.")

    # Запускаем задачу проверки цен
    asyncio.create_task(check_prices(app))

    await app.run_polling()

if __name__ == "__main__":
    asyncio.run(main())
