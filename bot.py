import os
import logging
import requests
from bs4 import BeautifulSoup
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# Логирование
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("price-bot")

# Получаем токен и chat_ids из переменных окружения
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_IDS = os.getenv("CHAT_IDS", "").split(",")  # можно несколько id через запятую
PRODUCT_URLS = [
    "https://www.outdoordogsupply.com/products/used-garmin-320",
    "https://www.outdoordogsupply.com/products/refurbished-garmin-t5-collar",
    "https://www.outdoordogsupply.com/products/used-garmin-astro-t5-collars",
    "https://www.outdoordogsupply.com/products/garmin-huntview-newest-edition"
]

if not BOT_TOKEN:
    raise ValueError("Не найден BOT_TOKEN в переменных окружения!")

# ====== Функции для парсинга ======
def fetch_price(url: str) -> str:
    try:
        response = requests.get(url, timeout=15)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        # Ищем цену
        price = None
        price_tag = soup.find(class_="price")
        if price_tag:
            price = price_tag.get_text(strip=True)
        else:
            # резервный поиск по $ или цифрам
            for tag in soup.find_all(text=True):
                if "$" in tag:
                    price = tag.strip()
                    break

        return price if price else "Цена не найдена"
    except Exception as e:
        logger.error(f"Ошибка при парсинге {url}: {e}")
        return "Ошибка при парсинге"

# ====== Команды бота ======
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Бот запущен. Используй /check для проверки цен.")

async def check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result_lines = []
    for url in PRODUCT_URLS:
        price = fetch_price(url)
        result_lines.append(f"{url} → {price}")

    result = "\n".join(result_lines)
    await update.message.reply_text(result)

# ====== Основной запуск ======
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("check", check))

    # Запускаем без asyncio.run (фикс)
    app.run_polling()

if __name__ == "__main__":
    main()
