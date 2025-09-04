import time
import random
import os
import cloudscraper
from bs4 import BeautifulSoup
from datetime import datetime, timedelta

# =============================
# Настройки
# =============================

EBAY_URLS = [
    "https://www.ebay.com/sch/i.html?_udlo=100&_nkw=garmin+astro+320+&_sacat=0&_stpos=19720&_fcid=1",
]

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_IDS = os.getenv("CHAT_ID", "").split(",")
PROXIES = os.getenv("PROXIES", "").split(",")

CHECK_INTERVAL = 180          # базовый интервал (3 мин)
REPORT_INTERVAL = 1800        # отчёт каждые 30 мин
ERROR_NOTIFY_INTERVAL = 1800  # уведомление об ошибке не чаще 30 мин

seen_items = {url: set() for url in EBAY_URLS}
last_error_time = datetime.min
last_report_time = datetime.now()

checks_count = 0
new_items_count = 0
consecutive_errors = 0

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122 Safari/537.36"),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive"
}

# =============================
# Функции
# =============================

def get_scraper():
    """Создаёт scraper с случайным прокси"""
    scraper = cloudscraper.create_scraper()
    if PROXIES:
        proxy = random.choice(PROXIES).strip()
        return scraper, {"http": proxy, "https": proxy}
    return scraper, None

def fetch_listings(url):
    scraper, proxy = get_scraper()
    resp = scraper.get(url, headers=HEADERS, proxies=proxy, timeout=20)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
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
            import requests
            r = requests.post(url, data={"chat_id": chat_id, "text": message}, timeout=15)
            if r.status_code != 200:
                print(f"⚠️ Ошибка Telegram {chat_id}: {r.text}")
        except Exception as e:
            print(f"⚠️ Ошибка сети Telegram ({chat_id}): {e}")

# =============================
# Запуск
# =============================

print(f"📢 eBay бот запущен. Проверка каждые {CHECK_INTERVAL} сек.")

for url in EBAY_URLS:
    try:
        listings = fetch_listings(url)
        for item in listings:
            seen_items[url].add(item["id"])
        print(f"✅ Инициализация: сохранено {len(listings)} объявлений с {url}")
    except Exception as e:
        print(f"⚠️ Ошибка при первой загрузке {url}: {e}")

while True:
    checks_count += 1
    try:
        for url in EBAY_URLS:
            try:
                listings = fetch_listings(url)
                for item in listings:
                    if item["id"] not in seen_items[url]:
                        seen_items[url].add(item["id"])
                        new_items_count += 1
                        send_telegram_message(
                            f"🆕 Новое объявление на eBay!\n"
                            f"📌 {item['title']}\n"
                            f"💲 {item['price']}\n"
                            f"🔗 {item['link']}"
                        )
                consecutive_errors = 0  # сброс ошибок при успехе

            except Exception as e:
                print(f"⚠️ Ошибка при проверке {url}: {e}")
                consecutive_errors += 1
                if datetime.now() - last_error_time > timedelta(seconds=ERROR_NOTIFY_INTERVAL):
                    send_telegram_message(f"⚠️ Ошибка при проверке {url}: {e}")
                    last_error_time = datetime.now()

        # Отчёт
        if datetime.now() - last_report_time > timedelta(seconds=REPORT_INTERVAL):
            send_telegram_message(
                "📊 Отчёт за последние 30 минут\n"
                f"⏰ Время: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"🔎 Проверок: {checks_count}\n"
                f"🆕 Новых объявлений: {new_items_count}\n"
                "✅ Бот работает"
            )
            last_report_time = datetime.now()
            checks_count = 0
            new_items_count = 0

    except Exception as e:
        print(f"⚠️ Общая ошибка цикла: {e}")

    # если много ошибок подряд → временно увеличиваем паузу
    if consecutive_errors >= 3:
        delay = CHECK_INTERVAL * 3
        print(f"⚠️ {consecutive_errors} ошибок подряд. Ждём {delay} сек.")
        time.sleep(delay)
    else:
        # обычная пауза с рандомом
        jitter = random.randint(-30, 30)
        delay = max(60, CHECK_INTERVAL + jitter)
        time.sleep(delay)
