# bot.py
import asyncio
import logging
import os
import random
import re
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

import httpx
from telegram.ext import Application, ApplicationBuilder, CommandHandler, MessageHandler, filters

# ---------- ЛОГИ ----------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("price-bot")

# ---------- НАСТРОЙКИ И ОКРУЖЕНИЕ ----------

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Set TELEGRAM_BOT_TOKEN (или BOT_TOKEN) в переменных окружения")

ADMIN_ID = os.getenv("TELEGRAM_USER_ID")

DEBUG_HTML = os.getenv("DEBUG_HTML", "0") == "1"
DEBUG_HTML_LEN = int(os.getenv("DEBUG_HTML_LEN", "1200"))

# Прокси для httpx (используются и при билде, и в рантайме)
HTTP_PROXY = os.getenv("HTTP_PROXY")
HTTPS_PROXY = os.getenv("HTTPS_PROXY") or os.getenv("HTTPS_PROXY".lower())  # на всякий

PROXIES = {}
if HTTP_PROXY:
    PROXIES["http://"] = HTTP_PROXY
if HTTPS_PROXY:
    PROXIES["https://"] = HTTPS_PROXY

# Пул реальных UA + ротация языка
UA_POOL = [
    # Chrome Win
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.1 Safari/537.36",
    # Chrome Mac
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.1 Safari/537.36",
    # Firefox
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0",
    # Safari
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
]

LANG_POOL = [
    "en-US,en;q=0.9",
    "de-DE,de;q=0.9,en;q=0.8",
    "fr-FR,fr;q=0.9,en;q=0.8",
    "it-IT,it;q=0.9,en;q=0.8",
]

# Для Farfetch – альтернативные storeid'ы
FARFETCH_STOREIDS = ["10047", "10039", "10035", "10037"]  # US, DE, FR, IT и т.п.

# ---------- УТИЛИТЫ URL/FARFETCH ----------

FARFETCH_CC_RE = re.compile(r"/(us|de|fr|it)/", re.IGNORECASE)

def _rotate_farfetch_region(url: str) -> list[str]:
    """
    Вернёт список альтернативных URL Farfetch с ротацией страны.
    Пример: /us/ -> /de/ -> /fr/ -> /it/ -> без кода страны.
    """
    if "farfetch.com" not in url:
        return []

    candidates = []
    m = FARFETCH_CC_RE.search(url)
    variants = ["us", "de", "fr", "it"]

    if m:
        current = m.group(1).lower()
        order = [cc for cc in variants if cc != current] + [""]
        for cc in order:
            if cc:
                candidates.append(FARFETCH_CC_RE.sub(f"/{cc}/", url, count=1))
            else:
                # убрать код страны совсем
                candidates.append(FARFETCH_CC_RE.sub("/", url, count=1))
    else:
        # если кода страны нет — попробуем добавить разные
        for cc in variants:
            candidates.append(url.replace("farfetch.com/", f"farfetch.com/{cc}/", 1))

    return candidates


def _rotate_farfetch_storeid(url: str) -> list[str]:
    """
    Вернёт список URL с разными storeid.
    Если storeid уже есть — переставим на другой; если нет — добавим.
    """
    if "farfetch.com" not in url:
        return []

    u = urlparse(url)
    q = dict(parse_qsl(u.query, keep_blank_values=True))
    current = q.get("storeid")
    ids = FARFETCH_STOREIDS.copy()
    random.shuffle(ids)

    cand = []
    if current:
        ids = [x for x in ids if x != current] + [current]
    for sid in ids:
        q["storeid"] = sid
        cand.append(urlunparse(u._replace(query=urlencode(q))))
    return cand


def _referer_for(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}/"

# ---------- HTTP КЛИЕНТ ----------

def _headers_for(url: str) -> dict:
    return {
        "User-Agent": random.choice(UA_POOL),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": random.choice(LANG_POOL),
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": _referer_for(url),
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }

def _is_retryable(status: int) -> bool:
    # 403/429 — часто антибот; 5xx — нестабильность
    return status in (403, 429) or 500 <= status < 600

async def fetch_html(url: str, client: httpx.AsyncClient, max_retries: int = 4, timeout: float = 30.0) -> tuple[int, str]:
    """
    Возвращает (status_code, text). Делает ретраи с бэкоффом, меняя заголовки,
    при Farfetch пробует альтернативные регионы и storeid.
    """
    # Очередь кандидатов: исходный URL + возможные варианты для Farfetch
    queue: list[str] = [url]
    if "farfetch.com" in url:
        # чередуем region и storeid, чтобы увеличить шанс прохода
        queue += _rotate_farfetch_region(url)[:3]
        queue += _rotate_farfetch_storeid(url)[:3]

    tried = set()
    attempt = 0
    last_exc: Exception | None = None

    while queue and attempt < max_retries:
        current_url = queue.pop(0)
        if current_url in tried:
            continue
        tried.add(current_url)
        attempt += 1

        headers = _headers_for(current_url)

        try:
            r = await client.get(current_url, headers=headers, timeout=timeout)
            status = r.status_code
            text = r.text

            if DEBUG_HTML:
                # Логируем срез HTML, чтобы не заливать логи
                short = text[:DEBUG_HTML_LEN].replace("\n", "")
                log.info("[DEBUG HTML %s] %s :: %s", status, current_url, short)

            if _is_retryable(status) and attempt < max_retries:
                # Эвристика: если это farfetch и есть ещё варианты — подкинем их в хвост
                if "farfetch.com" in current_url:
                    queue += _rotate_farfetch_region(current_url)[:2]
                    queue += _rotate_farfetch_storeid(current_url)[:2]
                # Бэкофф 0.6..1.2 * 2^(attempt-1)
                delay = (0.6 + random.random() * 0.6) * (2 ** (attempt - 1))
                await asyncio.sleep(delay)
                continue

            return status, text

        except httpx.HTTPError as e:
            last_exc = e
            if attempt < max_retries:
                delay = (0.6 + random.random() * 0.6) * (2 ** (attempt - 1))
                await asyncio.sleep(delay)
                continue
            raise

    # Если сюда дошли — либо пустая очередь, либо исчерпали попытки
    if last_exc:
        raise last_exc
    return 520, ""  # "неопределённая" сетевой сбой

# ---------- TELEGRAM-ХЕНДЛЕРЫ ----------

URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)

async def start(update, context):
    msg = (
        "Привет! Пришлите ссылку — я попробую получить HTML.\n\n"
        "Заголовки и язык ротуются, есть ретраи при 403/429/5xx.\n"
        "Прокси берётся из переменных HTTP_PROXY / HTTPS_PROXY."
    )
    await update.message.reply_text(msg)

async def handle_text(update, context):
    text = (update.message.text or "").strip()
    m = URL_RE.search(text)
    if not m:
        await update.message.reply_text("Пришлите URL (http/https).")
        return

    url = m.group(0)

    # Каждый апдейт — свежая сессия httpx, чтобы не утыкаться в кэш
    limits = httpx.Limits(max_connections=20, max_keepalive_connections=10)
    async with httpx.AsyncClient(proxies=PROXIES or None, follow_redirects=True, limits=limits) as client:
        try:
            status, html = await fetch_html(url, client)
        except Exception as e:
            log.exception("fetch failed")
            await update.message.reply_text(f"Запрос упал: {type(e).__name__}: {e}")
            return

    # Короткий отчёт в TG
    length = len(html)
    preview = html[:400].replace("\n", " ") if html else ""
    reply = f"HTTP {status}, bytes={length}\n\n{preview}"
    await update.message.reply_text(reply or f"HTTP {status}")

# ---------- MAIN ----------

async def on_startup(app: Application):
    # Гарантированно уберём вебхук перед polling, чтобы исключить 409
    from telegram import Bot
    bot = Bot(BOT_TOKEN)
    await bot.delete_webhook(drop_pending_updates=False)
    log.info("Webhook удалён перед запуском polling.")

def main():
    app: Application = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.post_init = on_startup

    # Стартуем polling
    log.info("Application starting…")
    app.run_polling(allowed_updates=["message"], stop_signals=None)  # без сигналов, как у Render

if __name__ == "__main__":
    main()
