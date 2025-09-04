import time
import os
from datetime import datetime, timedelta
import cloudscraper
from bs4 import BeautifulSoup
import requests

# 🔹 Список URL eBay для отслеживания
EBAY_URLS = [
    "https://www.ebay.com/sch/i.html?_udlo=100&_nkw=garmin+astro+320+&_sacat=0&_stpos=19720&_fcid=1",
]

# 🔹 Telegram токен и chat_id
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_IDS = os.getenv("CHAT_ID", "").split(",")  # пример: "200156484,6367892874"

# 🔹 Настройки интервалов
CHECK_INTERVAL = 180          # проверка каждые 3 минуты
REPORT_INTERVAL = 1800        # отчёт каждые 30 минут
ERROR_NOTIFY_INTERVAL = 1800  # уведомление об ошибках не чаще 30 минут

# 🔹 Хранилище просмотренных объявлений
seen_items = {url: set() for url in EBAY_URLS}
last_error_time = datetime.min
last_report_time = datetime.now()
consecutive_errors = 0  # счётчик подряд идущих ошибок

# 🔹 Счётчики для отчёта
checks_count = 0
new_items_count = 0

# 🔹 Cloudscraper (обходит защиту Cloudflare)
scraper = cloudscraper.create_scraper()

def fetch_listings(url):
    """Загружает страницу eBay и возвращает список объявлений"""
    resp = scraper.get(url, timeout=20)
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
    """Отправляет сообщение всем chat_id"""
    if not BOT_TOKEN:
        print("⚠️ BOT_TOKEN не задан — пропускаю отправку.")
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    for raw_id in CHAT_IDS:
        chat_id = raw_id.strip()
        if not chat_id:
            continue
        payload = {"chat_id": chat_id, "text": message}
        try:
            r = requests.post(url, data=payload, timeout=15)
            if r.status_code != 200:
                print(f"⚠️ Ошибка отправки в Telegram ({chat_id}): {r.text}")
        except Exception as e:
            print(f"⚠️ Ошибка сети при отправке в Telegram ({chat_id}): {e}")

print(f"📢 eBay бот запущен. Проверка каждые {CHECK_INTERVAL} сек.")
send_telegram_message("✅ Бот успешно запущен и готов отслеживать eBay 🔍")

# 🔹 Инициализация
for url in EBAY_URLS:
    try:
        listings = fetch_listings(url)
        for item in listings:
            seen_items[url].add(item["id"])
        print(f"✅ Инициализация: сохранено {len(listings)} объявлений с {url}")
    except Exception as e:
        print(f"⚠️ Ошибка при первой загрузке {url}: {e}")

# 🔹 Основной цикл
while True:
    checks_count += 1

    for url in EBAY_URLS:
        try:
            listings = fetch_listings(url)
            print(f"🔎 {datetime.now().strftime('%H:%M:%S')} — найдено {len(listings)} объявлений на {url}")

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

            consecutive_errors = 0  # сброс ошибок при успешной проверке

        except Exception as e:
            print(f"⚠️ Ошибка при проверке {url}: {e}")
            consecutive_errors += 1
            if datetime.now() - last_error_time > timedelta(seconds=ERROR_NOTIFY_INTERVAL):
                send_telegram_message(f"⚠️ Ошибка при проверке {url}: {e}")
                last_error_time = datetime.now()

            # Если подряд 3+ ошибки → увеличиваем интервал проверок
            if consecutive_errors >= 3:
                CHECK_INTERVAL = min(CHECK_INTERVAL * 2, 3600)  # максимум 1 час
                send_telegram_message(
                    f"⚠️ Обнаружены {consecutive_errors} подряд ошибок. "
                    f"Интервал проверок временно увеличен до {CHECK_INTERVAL} секунд."
                )
                consecutive_errors = 0

    # 🔹 Периодический отчёт
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
