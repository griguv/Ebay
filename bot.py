import time
import requests
import cloudscraper
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

# Прокси (несколько через запятую)
raw_proxies = os.getenv("PROXIES", "")
proxy_list = [p.strip() for p in raw_proxies.split(",") if p.strip()]
current_proxy_index = 0

def get_current_proxy():
    if not proxy_list:
        return None
    return {"http": proxy_list[current_proxy_index], "https": proxy_list[current_proxy_index]}

# Интервалы
CHECK_INTERVAL = 180           # обычная проверка: 3 минуты
REPORT_INTERVAL = 1800         # отчёт: 30 минут
ERROR_NOTIFY_INTERVAL = 1800   # уведомления об ошибках раз в 30 минут
ERROR_THRESHOLD = 3            # сколько ошибок подряд → переключить прокси
EXTENDED_INTERVAL = 900        # при проблемах проверка каждые 15 минут

# Внутренние переменные
seen_items = {url: set() for url in EBAY_URLS}
last_error_time = datetime.min
last_report_time = datetime.now()
checks_count = 0
new_items_count = 0
fail_counter = 0
current_interval = CHECK_INTERVAL

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123 Safari/537.36")
}

def switch_proxy():
    """Переключение на следующий прокси"""
    global current_proxy_index, fail_counter
    if not proxy_list:
        return
    current_proxy_index = (current_proxy_index + 1) % len(proxy_list)
    fail_counter = 0
    send_telegram_message(f"🔄 Переключаюсь на следующий прокси: {proxy_list[current_proxy_index]}")

def fetch_listings(url):
    """Загружает страницу eBay и возвращает список объявлений"""
    global fail_counter
    proxies = get_current_proxy()
    try:
        resp = requests.get(url, headers=HEADERS, proxies=proxies, timeout=(15, 120))
        resp.raise_for_status()
        fail_counter = 0
    except Exception as e:
        fail_counter += 1
        print(f"⚠️ Ошибка запроса через requests: {e} (попытка {fail_counter})")
        if fail_counter >= 2:
            try:
                print("🔄 Переключаюсь на CloudScraper...")
                scraper = cloudscraper.create_scraper(browser={"browser": "chrome", "platform": "windows"})
                resp = scraper.get(url, headers=HEADERS, proxies=proxies, timeout=(15, 120))
                resp.raise_for_status()
                fail_counter = 0
            except Exception as e2:
                print(f"❌ Ошибка даже через CloudScraper: {e2}")
                raise
        else:
            raise

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
    proxies = get_current_proxy()
    if not BOT_TOKEN:
        print("⚠️ BOT_TOKEN не задан — пропускаю отправку.")
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    for raw_id in CHAT_IDS:
        chat_id = raw_id.strip()
        if not chat_id:
            continue
        try:
            r = requests.post(url, data={"chat_id": chat_id, "text": message}, timeout=15, proxies=proxies)
            if r.status_code != 200:
                print(f"⚠️ Ошибка отправки в Telegram ({chat_id}): {r.text}")
        except Exception as e:
            print(f"⚠️ Ошибка сети при отправке в Telegram ({chat_id}): {e}")

print(f"📢 eBay бот запущен. Проверка каждые {CHECK_INTERVAL} сек. Прокси: {proxy_list or '❌ нет'}")

# Инициализация (сохраняем текущие объявления)
for url in EBAY_URLS:
    try:
        listings = fetch_listings(url)
        for item in listings:
            seen_items[url].add(item["id"])
        print(f"✅ Инициализация: сохранено {len(listings)} объявлений с {url}")
    except Exception as e:
        print(f"⚠️ Ошибка при первой загрузке {url}: {e}")

# Основной цикл
while True:
    checks_count += 1
    for url in EBAY_URLS:
        try:
            listings = fetch_listings(url)
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
            print(f"⚠️ Ошибка при проверке {url}: {e}")
            if datetime.now() - last_error_time > timedelta(seconds=ERROR_NOTIFY_INTERVAL):
                send_telegram_message(f"⚠️ Ошибка при проверке {url}: {e}")
                last_error_time = datetime.now()

    # Если много ошибок подряд → переключаем прокси или увеличиваем интервал
    if fail_counter >= ERROR_THRESHOLD:
        if proxy_list:
            switch_proxy()
        else:
            if current_interval != EXTENDED_INTERVAL:
                current_interval = EXTENDED_INTERVAL
                send_telegram_message(f"⚠️ Много ошибок. Увеличиваю интервал до {current_interval} сек.")
    else:
        if current_interval != CHECK_INTERVAL:
            current_interval = CHECK_INTERVAL
            send_telegram_message(f"✅ Соединение восстановлено. Интервал {current_interval} сек.")

    # Периодический отчёт
    if datetime.now() - last_report_time > timedelta(seconds=REPORT_INTERVAL):
        current_proxy = proxy_list[current_proxy_index] if proxy_list else "❌ нет"
        report = (
            "📊 Отчёт за последние 30 минут\n"
            f"⏰ Время: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"🔎 Проверок: {checks_count}\n"
            f"🆕 Новых объявлений: {new_items_count}\n"
            f"⚠️ Ошибки подряд: {fail_counter}\n"
            f"⏱ Текущий интервал: {current_interval} сек\n"
            f"🌐 Прокси: {current_proxy}\n"
            "✅ Бот работает"
        )
        send_telegram_message(report)
        last_report_time = datetime.now()
        checks_count = 0
        new_items_count = 0

    time.sleep(current_interval)
