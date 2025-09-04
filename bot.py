import os
import time
import random
import logging
import itertools
from logging.handlers import RotatingFileHandler
from datetime import datetime, timedelta
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse
import xml.etree.ElementTree as ET

import requests
import cloudscraper
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ──────────────────────────────
# Конфигурация
# ──────────────────────────────
EBAY_URLS = [
    "https://www.ebay.com/sch/i.html?_udlo=100&_nkw=garmin+astro+320+&_sacat=0&_stpos=19720&_fcid=1",
]

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_IDS = [cid.strip() for cid in os.getenv("CHAT_ID", "").split(",") if cid.strip()]
PROXIES = [p.strip() for p in os.getenv("PROXIES", "").split(",") if p.strip()]

CHECK_INTERVAL = 180
REPORT_INTERVAL = 1800
ERROR_NOTIFY_INTERVAL = 1800
BACKOFF_THRESHOLD = 3

MAX_PAGES = 3          # сколько страниц eBay обходим
MIN_HTML_ITEMS = 5     # если меньше — пробуем RSS

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122 Safari/537.36"),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}

# ──────────────────────────────
# Логи
# ──────────────────────────────
logger = logging.getLogger("ebay-bot")
logger.setLevel(logging.INFO)
fh = RotatingFileHandler("logs.txt", maxBytes=1_000_000, backupCount=5, encoding="utf-8")
fh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
ch = logging.StreamHandler()
ch.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
logger.addHandler(fh)
logger.addHandler(ch)

# ──────────────────────────────
# Состояние
# ──────────────────────────────
seen_items = {url: set() for url in EBAY_URLS}
last_error_time = datetime.min
last_report_time = datetime.now()
checks_count = 0
new_items_count = 0
consecutive_errors = 0

# ──────────────────────────────
# Вспомогательные функции
# ──────────────────────────────
def proxy_cycle(proxies_list):
    if not proxies_list:
        while True:
            yield None
    else:
        cleaned = [p for p in proxies_list if p]
        for p in itertools.cycle(cleaned):
            yield {"http": p, "https": p}

_proxy_iter = proxy_cycle(PROXIES)

def make_scraper_with_retries():
    s = cloudscraper.create_scraper()
    adapter = HTTPAdapter(
        max_retries=Retry(total=0, connect=0, read=0, redirect=0, raise_on_status=False),
        pool_connections=10,
        pool_maxsize=20,
    )
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    return s

def _with_params(url: str, extra: dict) -> str:
    u = urlparse(url)
    q = dict(parse_qsl(u.query, keep_blank_values=True))
    q.update(extra)
    new_q = urlencode(q, doseq=True)
    return urlunparse((u.scheme, u.netloc, u.path, u.params, new_q, u.fragment))

def _fetch_html_page(url: str):
    current_proxy = next(_proxy_iter)
    scraper = make_scraper_with_retries()
    resp = scraper.get(url, headers=HEADERS, proxies=current_proxy, timeout=(20, 45))
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    raw_cards = soup.select("li.s-item")
    valid_items = []
    for card in raw_cards:
        title_tag = card.select_one(".s-item__title")
        link_tag  = card.select_one(".s-item__link")
        if not title_tag or not link_tag:
            continue
        price_tag = card.select_one(".s-item__price")
        title = title_tag.get_text(strip=True)
        link  = link_tag.get("href", "")
        price = price_tag.get_text(strip=True) if price_tag else "Цена не указана"
        item_id = link.split("/")[-1].split("?")[0] or link
        valid_items.append({"id": item_id, "title": title, "price": price, "link": link})

    logger.info(f"HTML: всего карточек={len(raw_cards)}, валидных={len(valid_items)}")
    return valid_items

def _fetch_via_rss(url: str):
    rss_url = _with_params(url, {"_rss": "1"})
    current_proxy = next(_proxy_iter)
    scraper = make_scraper_with_retries()
    resp = scraper.get(rss_url, headers=HEADERS, proxies=current_proxy, timeout=(20, 45))
    resp.raise_for_status()

    items = []
    root = ET.fromstring(resp.text)
    for it in root.findall(".//item"):
        title = (it.findtext("title") or "").strip()
        link  = (it.findtext("link") or "").strip()
        if not title or not link:
            continue
        price = "Цена не указана"
        item_id = link.split("/")[-1].split("?")[0] or link
        items.append({"id": item_id, "title": title, "price": price, "link": link})

    logger.info(f"RSS: валидных элементов={len(items)}")
    return items

def fetch_listings(url: str):
    base = _with_params(url, {"_ipg": "240", "rt": "nc"})
    aggregated = []
    backoff = 3

    for p in range(1, MAX_PAGES + 1):
        page_url = _with_params(base, {"_pgn": str(p)})
        try:
            logger.info(f"Загружаю страницу {p}/{MAX_PAGES}: {page_url}")
            items = _fetch_html_page(page_url)
            aggregated.extend(items)
            if len(items) == 0:
                break
        except Exception as e:
            logger.warning(f"Ошибка HTML на p={p}: {e}; backoff {backoff}s")
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)

    if len(aggregated) < MIN_HTML_ITEMS:
        logger.warning(f"HTML дал мало ({len(aggregated)}) — пробую RSS")
        try:
            rss_items = _fetch_via_rss(url)
            known = {it["id"] for it in aggregated}
            for it in rss_items:
                if it["id"] not in known:
                    aggregated.append(it)
            logger.info(f"После RSS всего элементов: {len(aggregated)}")
        except Exception as e:
            logger.warning(f"RSS тоже не удалось: {e}")

    dedup = {it["id"]: it for it in aggregated}
    result = list(dedup.values())
    logger.info(f"Итог: собрано {len(result)} объявлений (HTML+RSS)")
    return result

def send_telegram_message(text: str):
    if not BOT_TOKEN or not CHAT_IDS:
        logger.warning("BOT_TOKEN или CHAT_ID не заданы — отправка пропущена.")
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    for chat_id in CHAT_IDS:
        try:
            r = requests.post(url, data={"chat_id": chat_id, "text": text}, timeout=20)
            if r.status_code != 200:
                logger.warning(f"Ошибка Telegram ({chat_id}): {r.text}")
        except Exception as e:
            logger.warning(f"Сетевая ошибка Telegram ({chat_id}): {e}")

# ──────────────────────────────
# Старт
# ──────────────────────────────
logger.info(f"Сервис запущен. Интервал проверок: {CHECK_INTERVAL}s. Прокси: {PROXIES or 'нет'}")
send_telegram_message("✅ Бот успешно запущен и готов отслеживать eBay 🔍")

for url in EBAY_URLS:
    try:
        listings = fetch_listings(url)
        for it in listings:
            seen_items[url].add(it["id"])
        logger.info(f"Инициализация: сохранено {len(listings)} объявлений по {url}")
    except Exception as e:
        logger.warning(f"Ошибка при первой загрузке {url}: {e}")

# ──────────────────────────────
# Основной цикл
# ──────────────────────────────
while True:
    checks_count += 1
    logger.info(f"Проверка #{checks_count} начата")

    for url in EBAY_URLS:
        try:
            listings = fetch_listings(url)
            logger.info(f"Получено {len(listings)} валидных объявлений по {url}")

            new_here = 0
            for it in listings:
                if it["id"] not in seen_items[url]:
                    seen_items[url].add(it["id"])
                    new_items_count += 1
                    new_here += 1
                    send_telegram_message(
                        "🆕 Новое объявление на eBay!\n"
                        f"📌 {it['title']}\n"
                        f"💲 {it['price']}\n"
                        f"🔗 {it['link']}"
                    )

            logger.info(f"Новых объявлений в этой проверке: {new_here}")
            consecutive_errors = 0

        except Exception as e:
            logger.warning(f"Ошибка при проверке {url}: {e}")
            consecutive_errors += 1
            if datetime.now() - last_error_time > timedelta(seconds=ERROR_NOTIFY_INTERVAL):
                send_telegram_message(f"⚠️ Ошибка при проверке {url}: {e}")
                last_error_time = datetime.now()

    if datetime.now() - last_report_time > timedelta(seconds=REPORT_INTERVAL):
        report = (
            "📊 Отчёт за последние 30 минут\n"
            f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"🔎 Проверок: {checks_count}\n"
            f"🆕 Новых объявлений: {new_items_count}\n"
            "✅ Бот работает"
        )
        send_telegram_message(report)
        logger.info("Отчёт отправлен")
        last_report_time = datetime.now()
        checks_count = 0
        new_items_count = 0

    if consecutive_errors >= BACKOFF_THRESHOLD:
        delay = min(CHECK_INTERVAL * 3, 3600)
        logger.warning(f"{consecutive_errors} ошибок подряд. Усиленная пауза: {delay} сек.")
        time.sleep(delay)
    else:
        jitter = random.randint(-30, 30)
        delay = max(60, CHECK_INTERVAL + jitter)
        time.sleep(delay)
