import logging
import os
import re
from typing import Optional

# ---------- ЛОГИРОВАНИЕ ----------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

logger = logging.getLogger("price-bot")
logger.setLevel(LOG_LEVEL)
_handler = logging.StreamHandler()
_handler.setFormatter(logging.Formatter(
    "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
))
logger.addHandler(_handler)

# ---------- КОНСТАНТЫ ----------
# Страны — строго зафиксированный список
COUNTRIES = ["US", "IT", "FR", "DE", "ES", "GB", "KZ", "HK"]

# Заголовки для разных сайтов
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Telegram конфиг
def get_bot_token() -> str:
    # Основная переменная — BOT_TOKEN, запасной алиас — TELEGRAM_BOT_TOKEN
    token = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("Не задан BOT_TOKEN в переменных окружения.")
    return token

def get_proxy_config() -> Optional[dict]:
    """
    Возвращает словарь прокси для httpx, если задан PROXY_URL.
    Поддерживает приватные прокси через PROXY_USER / PROXY_PASS (необязательны).
    Формат PROXY_URL: http(s)://host:port
    """
    proxy_url = os.getenv("PROXY_URL")
    if not proxy_url:
        return None

    user = os.getenv("PROXY_USER")
    pwd = os.getenv("PROXY_PASS")
    if user and pwd:
        # Впишем креды в URL
        proxy_url = re.sub(r"^(\w+://)", rf"\1{user}:{pwd}@", proxy_url)

    return {
        "http://": proxy_url,
        "https://": proxy_url,
    }

def chunk_list(items, chunk_size=30):
    for i in range(0, len(items), chunk_size):
        yield items[i:i+chunk_size]

def is_supported_url(url: str) -> bool:
    return ("farfetch.com" in url) or ("yoox.com" in url)

def site_name(url: str) -> str:
    if "farfetch.com" in url:
        return "Farfetch"
    if "yoox.com" in url:
        return "YOOX"
    return "Unknown"
