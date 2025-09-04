import time
import os
import logging
from datetime import datetime, timedelta
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse
import xml.etree.ElementTree as ET

import cloudscraper
from bs4 import BeautifulSoup
import requests

# ---------------- Конфиг ----------------
EBAY_URLS = [
    "https://www.ebay.com/sch/i.html?_udlo=100&_nkw=garmin+astro+320+&_sacat=0",
]

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_IDS = os.getenv("CHAT_ID", "").split(",")

CHECK_INTERVAL = 180       # каждые 3 минуты
REPORT_INTERVAL = 1800     # каждые 30 минут
MAX_PAGES = 3              # сколько страниц пагинации смотреть
MIN_HTML_ITEMS = 5         # если меньше — пробуем RSS

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122 Safari/537.36"
    )
}

# ---------------- Логирование ----------------
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger()

# ---------------- Прокси ----------------
PROXIES = []  # сюда можно добавить список прокси
def _proxy_iter_cycle():
    while True:
        for p in [None] + PROXIES:
            if not p:
                yield None
            else:
                yield {"http": p, "https": p}
_proxy_iter = _proxy_iter_cycle()

# ---------------- Вспомогательные функции ----------------
def make_scraper_with_retries():
    return cloudscraper.create_scraper(browser="chrome")

def _with_params(url: str, extra: dict) -> str:
    u = urlparse(url)
    q = dict(parse_qsl(u.query, keep_blank_values=True))
    q.update(extra)
    new_q = urlencode(q, doseq=True)
    return urlunparse((u.scheme, u.netloc, u.path, u.params, new_q, u.fragment))

def _strip_params(url: str, keys_to_drop: list[str]) -> str:
    u = urlparse(url)
    q = dict(parse_qsl(u.query, keep_blank_values=True))
    for k in keys_to_drop:
        q.pop(k, None)
    new_q = urlencode(q, doseq=True)
    return urlunparse((u.scheme, u.netloc, u.path, u.params, new_q, u.fragment))

def _fetch_html_page(url: str):
    scraper = make_scraper_with_retries()
    current_proxy = next(_proxy_iter)
    resp = scraper.get(url, headers=HEADERS, proxies=current_proxy, timeout=(20, 45))
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    raw_cards = soup.select("li.s-item")
    valid_items = []
    for card in raw_cards:
        title_tag = card.select_one(".s-item__title")
        link_tag = card.select_one(".s-item__link")
        if not title_tag or not link_tag:
            continue
        price_tag = card.select_one(".s-item__price")
        title = title_tag.get_text(strip=True)
        link = link_tag.get("href", "")
        price = price_tag.get_text(strip=True) if price_tag else "Цена не указана"
        item_id = link.split("/")[-1].split("?")[0] or link
        valid_items.append({"id": item_id, "title": title, "price": price, "link": link})
    logger.info(f"HTML: всего карточек={len(raw_cards)}, валидных={len(valid_items)}")
    return valid_items

def _fetch_via_rss(url: str):
    rss_url = _with_params(url, {"_rss": "1"})
    scraper = make_scraper_with_retries()
    current_proxy = next(_proxy_iter)
    resp = scraper.get(rss_url, headers=HEADERS, proxies=current_proxy, timeout=(20, 45))
    resp.raise_for_status()
    root = ET.fromstring(resp.text)
    items = []
    for it in root.findall(".//item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        if not title or not link:
            continue
        price = "Цена не указана"
        item_id = link.split("/")[-1].split("?")[0] or link
        items.append({"id": item_id, "title": title, "price": price, "link": link})
    logger.info(f"RSS: валидных элементов={len(items)} (url: {rss_url})")
    return items

def fetch_listings(url: str):
    clean_url = _strip_params(url, ["_stpos", "_fcid"])
    base = _with_params(clean_url, {"_ipg": "240", "rt": "nc"})
    logger.info(f"Base URL after cleanup: {base}")

    aggregated = []
    for p in range(1, MAX_PAGES + 1):
        page_url = _with_params(base, {"_pgn": str(p)})
        try:
            logger.info(f"Загружаю страницу {p}/{MAX_PAGES}: {page_url}")
            items = _fetch_html_page(page_url)
            aggregated.extend(items)
            if len(items) == 0:
                break
        except Exception as e:
            logger.warning(f"Ошибка HTML на p={p}: {e}")
            time.sleep(3)

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
            logger.warning(f"RSS тоже не удалось получить: {e}")

    dedup = {}
    for it in aggregated:
        dedup[it["id"]] = it
    result = list(dedup.values())
    logger.info(f"Итог: собрано {len(result)} объявлений (HTML+RSS)")
    return result

# ---------------- Telegram ----------------
def send_telegram_message(message):
    if not BOT_TOKEN:
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    for raw_id in CHAT_IDS:
        chat_id = raw_id.strip()
        if not chat_id:
            continue
        try:
            r = requests.post(url, data={"chat_id": chat_id, "text": message}, timeout=15)
            if r.status_code != 200:
                logger.warning(f"Ошибка Telegram ({chat_id}): {r.text}")
        except Exception as e:
            logger.warning(f"Ошибка Telegram для {chat_id}: {e}")

# ---------------- Основная логика ----------------
seen_items = {url: set() for url in EBAY_URLS}
last_report_time = datetime.now()
checks_count = 0
new_items_count = 0

logger.info(f"Сервис запущен. Интервал проверок: {CHECK_INTERVAL}s. Прокси: {PROXIES or 'нет'}")

for url in EBAY_URLS:
    try:
        listings = fetch_listings(url)
        for item in listings:
            seen_items[url].add(item["id"])
        logger.info(f"Инициализация: сохранено {len(listings)} объявлений по {url}")
    except Exception as e:
        logger.warning(f"Ошибка при инициализации {url}: {e}")

while True:
    checks_count += 1
    logger.info(f"Проверка #{checks_count} начата")

    for url in EBAY_URLS:
        try:
            listings = fetch_listings(url)
            logger.info(f"Получено {len(listings)} валидных объявлений по {url}")
            new_for_url = 0
            for item in listings:
                if item["id"] not in seen_items[url]:
                    seen_items[url].add(item["id"])
                    new_items_count += 1
                    new_for_url += 1
                    msg = (
                        "🆕 Новое объявление на eBay!\n"
                        f"📌 {item['title']}\n"
                        f"💲 {item['price']}\n"
                        f"🔗 {item['link']}"
                    )
                    send_telegram_message(msg)
            logger.info(f"Новых объявлений в этой проверке: {new_for_url}")
        except Exception as e:
            logger.warning(f"Ошибка при проверке {url}: {e}")

    if datetime.now() - last_report_time > timedelta(seconds=REPORT_INTERVAL):
        report = (
            "📊 Отчёт за последние 30 минут\n"
            f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"🔎 Проверок: {checks_count}\n"
            f"🆕 Новых объявлений: {new_items_count}\n"
            "✅ Бот работает"
        )
        send_telegram_message(report)
        last_report_time = datetime.now()
        checks_count = 0
        new_items_count = 0

    time.sleep(CHECK_INTERVAL)
