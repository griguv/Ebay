import os
import re
import sys
import asyncio
import logging
from typing import List, Dict
from urllib.parse import urlparse

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from telegram.error import Conflict

from price_parsers import (
    get_prices_across_countries,
    format_prices_table,
)

# -----------------------------
# ЛОГИРОВАНИЕ
# -----------------------------
logger = logging.getLogger("price-bot")
if not logger.handlers:
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
    logger.addHandler(_h)
logger.setLevel(logging.INFO)

TOKEN_ENV = "БОТ_ТОКЕН"  # ИМЯ ПЕРЕМЕННОЙ ОКРУЖЕНИЯ — НЕ МЕНЯЕМ
BOT_TOKEN = os.getenv(TOKEN_ENV)
if not BOT_TOKEN:
    print(f"Переменная окружения {TOKEN_ENV} не задана", file=sys.stderr)
    sys.exit(1)

# -----------------------------
# ВСПОМОГАТЕЛЬНОЕ
# -----------------------------
URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)

def extract_links(text: str) -> List[str]:
    if not text:
        return []
    return [m.group(0).strip(" \t\r\n,") for m in URL_RE.finditer(text)]

def is_supported_host(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return ("farfetch." in host) or ("yoox." in host) or ("yoox.com" in host)

# -----------------------------
# ХЕНДЛЕРЫ
# -----------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = (
        "Отправь ссылку (или несколько через пробел/новую строку) на товар Farfetch или YOOX.\n\n"
        "Бот спарсит цену по странам: RU, TR, KZ, AE, HK и выведет таблицу.\n"
        "Если ссылок несколько, бот пройдётся по каждой и покажет блоки по ссылкам.\n\n"
        "_Подсказка_: капчи обходим заголовками и (при необходимости) прокси. "
        "Для прокси можно задать переменные PROXY_RU/TR/KZ/AE/HK."
    )
    await update.message.reply_text(msg, disable_web_page_preview=True)

async def handle_links(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text or ""
    links = extract_links(text)
    if not links:
        await update.message.reply_text("Не вижу ссылок. Пришли URL Farfetch или YOOX.")
        return

    supported = [u for u in links if is_supported_host(u)]
    unsupported = [u for u in links if u not in supported]
    if unsupported:
        await update.message.reply_text(
            "Пропущены несуппорченные ссылки:\n" + "\n".join(unsupported),
            disable_web_page_preview=True,
        )

    if not supported:
        await update.message.reply_text("Пришли ссылку на Farfetch или YOOX.")
        return

    for url in supported:
        try:
            prices_by_country: Dict[str, Dict[str, str]] = await get_prices_across_countries(url)
            table = format_prices_table(prices_by_country)
            text_out = f"<b>URL</b>: {url}\n<pre>{table}</pre>"
            await update.message.reply_text(
                text_out,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except Exception as e:
            logger.error("Ошибка парсинга %s: %s", url, e, exc_info=True)
            await update.message.reply_text(f"Ошибка парсинга: {e}")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled error: %s", context.error, exc_info=True)

# -----------------------------
# MAIN
# -----------------------------
async def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_links))
    app.add_error_handler(error_handler)

    # Важно: убираем вебхук перед polling и чистим очереди
    await app.bot.delete_webhook(drop_pending_updates=True)
    logger.info("Webhook удалён перед запуском polling.")

    try:
        # один-единственный polling-процесс
        await app.run_polling(
            allowed_updates=Update.ALL_TYPES,
            close_loop=False,                 # не трогаем глобальный loop (Render)
            stop_signals=None,                # Render сам управляет сигналами
            drop_pending_updates=True,
        )
    except Conflict as e:
        # Если уже есть другой процесс getUpdates — логируем и завершаем
        logger.error("Конфликт polling: %s. Похоже, уже запущен другой инстанс.", e)
        # мягко завершаем приложение, чтобы Render не перезапускал бесконечно
        try:
            await app.shutdown()
        finally:
            sys.exit(0)

if __name__ == "__main__":
    asyncio.run(main())
