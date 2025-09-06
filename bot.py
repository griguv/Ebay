import asyncio
import logging
import os
import re
from typing import List, Tuple

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from utils import logger, get_bot_token, COUNTRIES, is_supported_url, site_name, get_proxy_config
from price_parsers import get_prices_across_countries

# ----------------- КОМАНДЫ -----------------

HELP_TEXT = (
    "Пришлите 1 или несколько ссылок на товары с Farfetch или YOOX "
    "(каждую с новой строки или через пробел). Я сравню цены по фиксированным "
    f"странам: {', '.join(COUNTRIES)}.\n\n"
    "Если ссылок несколько — суммы по каждой стране будут просуммированы и выведены итогом."
)

def extract_urls(text: str) -> List[str]:
    # простая выборка ссылок
    urls = re.findall(r"https?://[^\s]+", text)
    # фильтруем под поддерживаемые домены
    return [u.strip(").,") for u in urls if is_supported_url(u)]

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Готов 🚀\n" + HELP_TEXT
    )

async def countries(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Фиксированный список стран:\n" + ", ".join(COUNTRIES)
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT)

# ----------------- ОБРАБОТКА ССЫЛОК -----------------

async def handle_links(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    urls = extract_urls(text)

    if not urls:
        await update.message.reply_text("Не нашёл поддерживаемых ссылок. Нужны Farfetch или YOOX.")
        return

    await update.message.reply_text(f"Принял {len(urls)} ссылок. Работаю…")

    # собираем цены по всем странам для каждой ссылки
    link_results = []
    for url in urls:
        try:
            prices = await get_prices_across_countries(url)
            link_results.append((url, prices))
        except Exception as e:
            logger.exception(f"Ошибка парсинга {url}: {e}")
            await update.message.reply_text(f"Ошибка при парсинге {url}: {e}")
            return

    # Суммируем по странам
    totals = {c: 0.0 for c in COUNTRIES}
    any_price_for_country = {c: False for c in COUNTRIES}
    lines = []

    for url, prices in link_results:
        name = site_name(url)
        lines.append(f"— [{name}] {url}")
        for c in COUNTRIES:
            info = prices.get(c, {})
            price = info.get("price")
            curr = info.get("currency")
            status = info.get("status", 0)
            if price is not None:
                any_price_for_country[c] = True
                totals[c] += float(price)
                lines.append(f"   {c}: {price:.2f} {curr or ''} (HTTP {status})")
            else:
                if status in (403, 429, 503, 0):
                    lines.append(f"   {c}: недоступно (возможно CAPTCHA/блок) (HTTP {status})")
                else:
                    lines.append(f"   {c}: не удалось извлечь цену (HTTP {status})")

    # Итоги
    lines.append("\nИТОГО по странам (сумма по всем ссылкам):")
    for c in COUNTRIES:
        if any_price_for_country[c]:
            lines.append(f"   {c}: {totals[c]:.2f} (суммарно; валюта может отличаться по ссылкам)")
        else:
            lines.append(f"   {c}: —")

    # Telegram ограничивает длину сообщений — разобьём при необходимости
    output = "\n".join(lines)
    if len(output) < 3900:
        await update.message.reply_text(output)
    else:
        # режем на части
        chunks = []
        cur = []
        size = 0
        for line in lines:
            if size + len(line) + 1 > 3900:
                chunks.append("\n".join(cur))
                cur = [line]
                size = len(line) + 1
            else:
                cur.append(line)
                size += len(line) + 1
        if cur:
            chunks.append("\n".join(cur))
        for ch in chunks:
            await update.message.reply_text(ch)

# ----------------- MAIN -----------------

def build_application() -> Application:
    token = get_bot_token()

    # Если нужен прокси для обращения к Telegram (чаще не нужен),
    # то можно настроить через переменные окружения MTProto/HTTPS прокси
    # В PTB21 прокси для самого Telegram бота настраивают через Request(..., proxy_url=...),
    # но здесь мы используем прокси только для HTTP-запросов к сайтам (price_parsers),
    # т.к. Telegram API обычно не блокируется на Render.
    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("countries", countries))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_links))

    # Логируем необработанные ошибки
    async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.exception("Unhandled error: %s", context.error)

    app.add_error_handler(error_handler)

    return app

if __name__ == "__main__":
    # ВАЖНО: не оборачиваем run_polling в asyncio.run — это и вызывало ошибки с event loop.
    app = build_application()

    # На всякий случай удаляем webhook перед polling
    # (если ранее бот был на вебхуках)
    try:
        asyncio.get_event_loop().run_until_complete(app.bot.delete_webhook(drop_pending_updates=True))
        logger.info("Webhook удалён перед запуском polling.")
    except Exception as e:
        logger.warning(f"Не удалось удалить webhook: {e}")

    # Если где-то уже крутится другой polling — Telegram вернёт 409 Conflict.
    # В этом случае просто упадём с логом, чтобы не плодить дубликаты.
    try:
        app.run_polling(allowed_updates=Update.ALL_TYPES, stop_signals=None, close_loop=False)
    except Exception as e:
        logger.exception(f"run_polling error: {e}")
        raise
