import os
import time
import json
import logging
import requests
import cloudscraper
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

# ================= НАСТРОЙКИ =================
EBAY_URLS = [
    "https://www.ebay.com/sch/i.html?_udlo=100&_nkw=garmin+astro+320+&_sacat=0&_fcid=1",
]

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_IDS = os.getenv("CHAT_ID", "").split(",")

CHECK_INTERVAL = 180          # каждые 3 мин
REPORT_INTERVAL = 1800        # каждые 30 мин
ERROR_NOTIFY_INTERVAL = 1800  # раз в 30 мин при ошибках
MAX_PAGES = 3                 # до 3 страниц на каждый поиск
REQUEST_TIMEOUT = 20

# ================= ЛОГИ =================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    force=True
)

# ================= ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ =================
scraper = cloudscraper.create_scraper()
seen_items = {url: set() for url in EBAY_URLS}
last_error_time = datetime.min
last_report_time = datetime.now()
checks_count = 0
new_items_count = 0

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122 Safari/537.36")
}

# ================= УТИЛИТЫ =================
def clean_base_url(url):
    """Удаляем лишние параметры и добавляем _ipg=240&rt=nc"""
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    qs.pop("_pgn", None)
    qs["_ipg"] = ["240"]
    qs["rt"] = ["nc"]
    new_query = urlencode(qs, doseq=True)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", new_query, ""))

def send_telegram_message(message):
    if not BOT_TOKEN:
        logging.warning("BOT_TOKEN не задан")
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    for raw_id in CHAT_IDS:
        chat_id = raw_id.strip()
        if not chat_id:
            continue
        try:
            r = requests.post(url, data={"chat_id": chat_id, "text": message}, timeout=10)
            if r.status_code != 200:
                logging.warning(f"Ошибка Telegram ({chat_id}): {r.text}")
        except Exception as e:
            logging.warning(f"Ошибка сети Telegram ({chat_id}): {e}")

# ================= ПАРСИНГ =================
def parse_json_ld(soup):
    """Парсинг JSON внутри <script type=application/ld+json>"""
    items = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string.strip())
            if isinstance(data, dict) and "itemListElement" in data:
                for el in data["itemListElement"]:
                    node = el.get("item", {})
                    if not node:
                        continue
                    title = node.get("name")
                    link = node.get("url")
                    offers = node.get("offers", {})
                    price = offers.get("priceCurrency", "") + " " + offers.get("price", "")
                    if title and link:
                        item_id = link.split("/")[-1].split("?")[0]
                        items.append({"id": item_id, "title": title, "price": price, "link": link})
        except Exception:
            continue
    return items

def parse_html_items(soup):
    """Парсинг старым способом: .s-item"""
    items = []
    for card in soup.select(".s-item"):
        title_tag = card.select_one(".s-item__title")
        price_tag = card.select_one(".s-item__price")
        link_tag = card.select_one(".s-item__link")
        if not title_tag or not price_tag or not link_tag:
            continue
        title = title_tag.get_text(strip=True)
        price = price_tag.get_text(strip=True)
        link = link_tag.get("href", "")
        item_id = link.split("/")[-1].split("?")[0] or link
        items.append({"id": item_id, "title": title, "price": price, "link": link})
    return items

def parse_rss(url):
    """Парсинг RSS ленты eBay"""
    rss_url = url + "&_rss=1"
    items = []
    try:
        resp = scraper.get(rss_url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "xml")
        for item in soup.find_all("item"):
            title = item.title.get_text(strip=True) if item.title else None
            link = item.link.get_text(strip=True) if item.link else None
            price = ""
            desc = item.description.get_text(strip=True) if item.description else ""
            if "$" in desc:
                price = desc.split("$")[-1].split("<")[0]
                price = "$" + price
            if title and link:
                item_id = link.split("/")[-1].split("?")[0]
                items.append({"id": item_id, "title": title, "price": price, "link": link})
    except Exception as e:
        logging.warning(f"Ошибка RSS: {e}")
    return items

def fetch_listings(base_url):
    """Комбинированная загрузка: JSON-LD → HTML → RSS"""
    all_items = []
    base_url = clean_base_url(base_url)
    logging.info(f"Base URL: {base_url}")

    for page in range(1, MAX_PAGES + 1):
        page_url = f"{base_url}&_pgn={page}"
        try:
            logging.info(f"Загружаю страницу {page}/{MAX_PAGES}: {page_url}")
            resp = scraper.get(page_url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")

            items = parse_json_ld(soup)
            if not items:
                items = parse_html_items(soup)

            logging.info(f"HTML p={page}: {len(items)} объявлений")
            all_items.extend(items)
        except Exception as e:
            logging.warning(f"Ошибка HTML p={page}: {e}")

    # fallback: если мало объявлений, пробуем RSS
    if len(all_items) < 5:
        rss_items = parse_rss(base_url)
        logging.warning(f"HTML дал мало ({len(all_items)}) — пробую RSS: {len(rss_items)}")
        all_items.extend(rss_items)

    uniq = {it["id"]: it for it in all_items}
    logging.info(f"Итог: собрано {len(uniq)} объявлений (HTML+RSS)")
    return list(uniq.values())

# ================= ОСНОВНОЙ ЦИКЛ =================
if __name__ == "__main__":
    logging.info(f"Сервис запущен. Интервал проверок: {CHECK_INTERVAL}s")

    # первичная инициализация
    for url in EBAY_URLS:
        try:
            listings = fetch_listings(url)
            for item in listings:
                seen_items[url].add(item["id"])
            logging.info(f"Инициализация: сохранено {len(listings)} объявлений по {url}")
        except Exception as e:
            logging.warning(f"Ошибка инициализации {url}: {e}")

    while True:
        checks_count += 1
        logging.info(f"Проверка #{checks_count} начата")

        for url in EBAY_URLS:
            try:
                listings = fetch_listings(url)
                new_count = 0
                for item in listings:
                    if item["id"] not in seen_items[url]:
                        seen_items[url].add(item["id"])
                        new_items_count += 1
                        new_count += 1
                        msg = (
                            "🆕 Новое объявление!\n"
                            f"📌 {item['title']}\n"
                            f"💲 {item['price']}\n"
                            f"🔗 {item['link']}"
                        )
                        send_telegram_message(msg)
                logging.info(f"Получено {len(listings)} валидных, новых={new_count} по {url}")
            except Exception as e:
                logging.warning(f"Ошибка проверки {url}: {e}")
                if datetime.now() - last_error_time > timedelta(seconds=ERROR_NOTIFY_INTERVAL):
                    send_telegram_message(f"⚠️ Ошибка при проверке {url}: {e}")
                    last_error_time = datetime.now()

        # отчёт каждые 30 мин
        if datetime.now() - last_report_time > timedelta(seconds=REPORT_INTERVAL):
            report = (
                "📊 Отчёт за 30 минут\n"
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
