import time
import cloudscraper
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
import os
import random

# Список URL eBay для отслеживания
EBAY_URLS = [
    "https://www.ebay.com/sch/i.html?_udlo=100&_nkw=garmin+astro+320+&_sacat=0&_stpos=19720&_fcid=1",
]

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_IDS = os.getenv("CHAT_ID", "").split(",")

CHECK_INTERVAL = 180
REPORT_INTERVAL = 1800
ERROR_NOTIFY_INTERVAL = 1800

# Прокси (если заданы)
PROXIES = os.getenv("PROXIES", "").split(",") if os.getenv("PROXIES") else []

# Хранилище просмотренных
seen_items = {url: set() for url in EBAY_URLS}
last_error_time = datetime.min
last_report_time = datetime.now()

checks_count = 0
new_items_count = 0
consecutive_errors = 0

scraper = cloudscraper.create_scraper()

def fetch_listings(url):
    proxy = None
    if PROXIES:
        proxy = random.choice(PROXIES).strip()
        print(f"[{datetime.now()}] Использую прокси: {proxy}")

    proxies = {"http": proxy, "https": proxy} if proxy else None
    resp = scraper.get(url, timeout=30, proxies=proxies)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    items = []

    for card in soup.select(".s-item"):
        title_tag = card.select_one(".s-item__title")
        price_tag = card.select_one(".s-item__price")
        link_tag  = card.select_one(".s-item__link")

        if not title_tag or not price_tag or not link_tag:
            continue

        title = title_tag.get_text(strip=True)
        price = price_tag.get_text(strip=True)
        link  = link_tag.get("href", "")

        item_id = link.split("/")[-1].split("?")[0] or link
        items.append({"id": item_id, "title": title, "price": price, "link": link})

    return items

def send_telegram_message(message):
    if not BOT_TOKEN:
        print("⚠️ BOT_TOKEN не задан")
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    for raw_id in CHAT_IDS:
        chat_id = raw_id.strip()
        if not chat_id:
            continue
        try:
            r = scraper.post(url, data={"chat_id": chat_id, "text": message}, timeout=15)
            if r.status_code != 200:
                print(f"⚠️ Ошибка Telegram {chat_id}: {r.text}")
        except Exception as e:
            print(f"⚠️ Ошибка сети при отправке в Telegram ({chat_id}): {e}")

print(f"[{datetime.now()}] 📢 eBay бот запущен. Проверка каждые {CHECK_INTERVAL} сек.")

# Инициализация
for url in EBAY_URLS:
    try:
        listings = fetch_listings(url)
        for item in listings:
            seen_items[url].add(item["id"])
        print(f"[{datetime.now()}] ✅ Инициализация: сохранено {len(listings)} объявлений с {url}")
    except Exception as e:
        print(f"[{datetime.now()}] ⚠️ Ошибка при первой загрузке {url}: {e}")

# Основной цикл
while True:
    checks_count += 1
    print(f"[{datetime.now()}] 🔎 Проверка #{checks_count}")

    for url in EBAY_URLS:
        try:
            listings = fetch_listings(url)
            print(f"[{datetime.now()}] Найдено {len(listings)} объявлений по {url}")

            for item in listings:
                if item["id"] not in seen_items[url]:
                    seen_items[url].add(item["id"])
                    new_items_count += 1
                    msg = (
                        "🆕 Новое объявление на eBay!\n"
                        f"📌 {item['title']}\n"
                        f"💲 {item['price']}\n"
                        f"🔗 {item['link']}"
                    )
                    print(f"[{datetime.now()}] ➕ Новое объявление: {item['title']}")
                    send_telegram_message(msg)

            consecutive_errors = 0  # сброс если всё ок

        except Exception as e:
            print(f"[{datetime.now()}] ⚠️ Ошибка при проверке {url}: {e}")
            consecutive_errors += 1
            if datetime.now() - last_error_time > timedelta(seconds=ERROR_NOTIFY_INTERVAL):
                send_telegram_message(f"⚠️ Ошибка при проверке {url}: {e}")
                last_error_time = datetime.now()

    if datetime.now() - last_report_time > timedelta(seconds=REPORT_INTERVAL):
        report = (
            "📊 Отчёт за последние 30 минут\n"
            f"⏰ Время: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"🔎 Проверок: {checks_count}\n"
            f"🆕 Новых объявлений: {new_items_count}\n"
            "✅ Бот работает"
        )
        send_telegram_message(report)
        print(f"[{datetime.now()}] 📊 Отчёт отправлен")
        last_report_time = datetime.now()
        checks_count = 0
        new_items_count = 0

    time.sleep(CHECK_INTERVAL)
