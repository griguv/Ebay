import time
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
import os

# Список URL eBay для отслеживания
EBAY_URLS = [
    "https://www.ebay.com/sch/i.html?_udlo=100&_nkw=garmin+astro+320+&_sacat=0&_stpos=19720&_fcid=1",
]

# Telegram токен и chat_id
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_IDS = os.getenv("CHAT_ID", "").split(",")

# Интервалы
BASE_CHECK_INTERVAL = 180       # проверка каждые 3 минуты
REPORT_INTERVAL = 1800          # отчёт каждые 30 минут
ERROR_NOTIFY_INTERVAL = 1800    # уведомление об ошибках не чаще 30 минут

# Ретрай-параметры
MAX_RETRIES = 3
BACKOFF_FACTOR = 2

# Прокси (одинаковый для eBay и Telegram)
PROXY_URL = os.getenv("PROXY")  # например: http://user:pass@ip:port
PROXIES = {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else None

# Хранилище
seen_items = {url: set() for url in EBAY_URLS}
error_streaks = {url: 0 for url in EBAY_URLS}
last_error_time = datetime.min
last_report_time = datetime.now()
checks_count = 0
new_items_count = 0
current_check_interval = BASE_CHECK_INTERVAL

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122 Safari/537.36"),
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def fetch_listings(url):
    last_exception = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            kwargs = {"headers": HEADERS, "timeout": (10, 60)}
            if PROXIES:
                kwargs["proxies"] = PROXIES
            resp = requests.get(url, **kwargs)
            resp.raise_for_status()
            return parse_listings(resp.text)
        except requests.exceptions.RequestException as e:
            last_exception = e
            print(f"[{now_str()}] ⚠ Ошибка при загрузке {url} (попытка {attempt}/{MAX_RETRIES}): {e}")
            if attempt < MAX_RETRIES:
                sleep_for = BACKOFF_FACTOR ** attempt
                print(f"[{now_str()}] Жду {sleep_for}s перед следующей попыткой...")
                time.sleep(sleep_for)
    raise last_exception

def parse_listings(html):
    soup = BeautifulSoup(html, "html.parser")
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
        print(f"[{now_str()}] ⚠ BOT_TOKEN не задан")
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    for raw_id in CHAT_IDS:
        chat_id = raw_id.strip()
        if not chat_id:
            continue
        try:
            kwargs = {"data": {"chat_id": chat_id, "text": message}, "timeout": (10, 60)}
            if PROXIES:
                kwargs["proxies"] = PROXIES
            r = requests.post(url, **kwargs)
            if r.status_code != 200:
                print(f"[{now_str()}] ⚠ Ошибка Telegram ({chat_id}): {r.text}")
        except Exception as e:
            print(f"[{now_str()}] ⚠ Ошибка сети Telegram ({chat_id}): {e}")

print(f"[{now_str()}] 📢 eBay бот запущен. Базовый интервал: {BASE_CHECK_INTERVAL} сек.")

# Инициализация
for url in EBAY_URLS:
    try:
        listings = fetch_listings(url)
        for item in listings:
            seen_items[url].add(item["id"])
        print(f"[{now_str()}] ✅ Инициализация: {len(listings)} объявлений с {url}")
    except Exception as e:
        print(f"[{now_str()}] ⚠ Ошибка при инициализации {url}: {e}")

# Основной цикл
while True:
    checks_count += 1
    success = True

    for url in EBAY_URLS:
        try:
            listings = fetch_listings(url)
            error_streaks[url] = 0
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
                    send_telegram_message(msg)
        except Exception as e:
            success = False
            error_streaks[url] += 1
            print(f"[{now_str()}] ⚠ Ошибка при проверке {url}: {e} (подряд {error_streaks[url]})")
            if datetime.now() - last_error_time > timedelta(seconds=ERROR_NOTIFY_INTERVAL):
                send_telegram_message(f"⚠ Ошибка при проверке {url}: {e}")
                last_error_time = datetime.now()

    # Адаптивный интервал
    if any(streak >= 3 for streak in error_streaks.values()):
        current_check_interval = BASE_CHECK_INTERVAL * 3
        print(f"[{now_str()}] ⏸ Слишком много ошибок подряд — увеличиваю интервал до {current_check_interval} сек.")
    elif success:
        if current_check_interval != BASE_CHECK_INTERVAL:
            print(f"[{now_str()}] ✅ Успешная проверка — возвращаю интервал к {BASE_CHECK_INTERVAL} сек.")
        current_check_interval = BASE_CHECK_INTERVAL

    # Отчёт каждые 30 минут
    if datetime.now() - last_report_time > timedelta(seconds=REPORT_INTERVAL):
        report = (
            "📊 Отчёт за последние 30 минут\n"
            f"⏰ Время: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"🔎 Проверок: {checks_count}\n"
            f"🆕 Новых объявлений: {new_items_count}\n"
            "✅ Бот работает"
        )
        send_telegram_message(report)
        last_report_time = datetime.now()
        checks_count = 0
        new_items_count = 0

    time.sleep(current_check_interval)
