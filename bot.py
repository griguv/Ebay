# bot.py
import os
import time
import logging
import random
import re
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

import cloudscraper
from bs4 import BeautifulSoup

# =============== НАСТРОЙКИ ===============
EBAY_URLS = [
    # твой исходный поиск
    "https://www.ebay.com/sch/i.html?_udlo=100&_nkw=garmin+astro+320+&_sacat=0&_stpos=19720&_fcid=1",
]

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "180"))   # 3 мин
PAGES_TO_SCAN = 3                                         # pgn=1..3
REQUEST_TIMEOUT = 20
RETRIES_PER_PAGE = 2
RETRY_SLEEP = (2, 4)     # случайная пауза между ретраями
UA_ROTATE = [
    # пара популярных UA; можно добавить свои
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
]

# Telegram можно подключить позже; сейчас не шлём сообщения
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID", "")

# =============== ЛОГИ ===============
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("ebay")

# =============== ВСПОМОГАТЕЛЬНОЕ ===============
def normalize_search_url(url: str) -> str:
    """
    Чистим URL от мусорных параметров и фиксируем нужные (_ipg=240, rt=nc, _pgn=N).
    Оставляем только «белый список» фильтров eBay.
    """
    allowed = {
        "_nkw", "_sacat", "_dcat", "_udlo", "_udhi", "_stpos", "_fcid", "_sop", "_nqc",
    }
    u = urlparse(url)
    params = dict(parse_qsl(u.query, keep_blank_values=True))

    # выбрасываем мусорные параметры
    cleaned = {k: v for k, v in params.items() if k in allowed}

    # базовые параметры для стабильной выдачи
    cleaned["_ipg"] = "240"
    cleaned["rt"] = "nc"

    # собираем обратно (без _pgn)
    q = urlencode(cleaned, doseq=True)
    base = urlunparse((u.scheme, u.netloc, u.path, "", q, ""))
    return base

def make_scraper():
    # cloudscraper умеет обходить cloudflare/js-челленджи
    scraper = cloudscraper.create_scraper(
        browser={
            "browser": "chrome",
            "platform": "windows",
            "desktop": True
        }
    )
    # базовые заголовки
    headers = {
        "User-Agent": random.choice(UA_ROTATE),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "keep-alive",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Upgrade-Insecure-Requests": "1",
    }
    scraper.headers.update(headers)
    return scraper

def fetch_page_html(scraper, url) -> str:
    """
    Скачиваем HTML с ретраями. Если 503/403 — меняем UA и ждём.
    """
    last_err = None
    for attempt in range(1, RETRIES_PER_PAGE + 1):
        try:
            scraper.headers["User-Agent"] = random.choice(UA_ROTATE)
            r = scraper.get(url, timeout=REQUEST_TIMEOUT)
            if r.status_code >= 500:
                raise RuntimeError(f"{r.status_code} Server Error")
            if r.status_code in (403, 429):
                raise RuntimeError(f"{r.status_code} Rate/Captcha")
            return r.text
        except Exception as e:
            last_err = e
            log.warning(f"Ошибка HTML на {url} (attempt {attempt}/{RETRIES_PER_PAGE}): {e}")
            time.sleep(random.uniform(*RETRY_SLEEP))
    # упало окончательно
    raise last_err or RuntimeError("Неизвестная ошибка загрузки")

def parse_items_from_html(html: str):
    """
    Парсим карточки из HTML.
    1) основной селектор: li.s-item
    2) запасной: div.s-item__wrapper
    Пытаемся извлечь: id (itemId из ссылки), title, price, link
    """
    soup = BeautifulSoup(html, "html.parser")

    # простая проверка на капчу/челлендж
    text_low = soup.get_text(" ", strip=True).lower()
    if "verify you're a human" in text_low or "captcha" in text_low:
        return [], True  # капча

    items = soup.select("li.s-item")
    if len(items) < 5:
        # fallback
        alt = soup.select("div.s-item__wrapper")
        if len(alt) > len(items):
            items = alt

    parsed = []
    for it in items:
        a = it.select_one("a.s-item__link")
        title_tag = it.select_one("h3.s-item__title")
        price_tag = it.select_one(".s-item__price")
        if not a or not title_tag:
            continue
        link = a.get("href", "").strip()
        title = title_tag.get_text(strip=True)

        # цена может отсутствовать на части карточек (реклама/витрина)
        price = price_tag.get_text(strip=True) if price_tag else ""

        # вытаскиваем itemId из ссылки (если есть)
        m = re.search(r"/(\d{9,})\?", link)
        item_id = m.group(1) if m else link  # fallback — вся ссылка

        parsed.append({
            "id": item_id,
            "title": title,
            "price": price,
            "link": link,
        })
    return parsed, False

# память о уже увиденных ID (на каждую ссылку)
seen = {}

def crawl_search(url: str):
    """
    Грузим до PAGES_TO_SCAN страниц поисковой выдачи. Если HTML даёт мало карточек,
    это почти наверняка заглушка/капча — логируем и сохраняем HTML для отладки.
    """
    base = normalize_search_url(url)
    log.info(f"Base URL: {base}")
    scraper = make_scraper()

    all_items = []
    human_check_detected = False

    for p in range(1, PAGES_TO_SCAN + 1):
        page_url = f"{base}&_pgn={p}"
        log.info(f"Загружаю страницу {p}/{PAGES_TO_SCAN}: {page_url}")
        try:
            html = fetch_page_html(scraper, page_url)
        except Exception as e:
            log.warning(f"Ошибка HTML p={p}: {e}")
            continue

        items, found_captcha = parse_items_from_html(html)
        log.info(f"HTML p={p}: найдено {len(items)} карточек")
        all_items.extend(items)
        human_check_detected = human_check_detected or found_captcha

        # если карточек совсем мало — сохраним HTML для отладки
        if len(items) < 5:
            try:
                dump_path = f"/opt/render/project/src/ebay_debug_p{p}.html"
                with open(dump_path, "w", encoding="utf-8") as f:
                    f.write(html)
                log.warning(f"Сохранён HTML дамп для отладки: {dump_path}")
            except Exception as e:
                log.warning(f"Не удалось сохранить дамп HTML: {e}")

        # небольшая рандомная пауза между страницами
        time.sleep(random.uniform(1.0, 2.5))

    # Удалим дубликаты по id
    uniq = {}
    for it in all_items:
        uniq[it["id"]] = it
    all_items = list(uniq.values())

    if human_check_detected:
        log.warning("Похоже на защитную страницу (captcha/human check). Кол-во карточек может быть занижено.")

    log.info(f"Итог: собрано {len(all_items)} объявлений (после чистки дубликатов)")
    return all_items

def main():
    global seen
    # Инициализация: загружаем текущую выдачу и помечаем как уже виденную
    for url in EBAY_URLS:
        try:
            items = crawl_search(url)
            seen[url] = {it["id"] for it in items}
            log.info(f"Инициализация: сохранено {len(seen[url])} объявлений по {url}")
        except Exception as e:
            log.warning(f"Инициализация не удалась для {url}: {e}")
            seen[url] = set()

    check_num = 0
    while True:
        check_num += 1
        log.info(f"Проверка #{check_num} начата")
        for url in EBAY_URLS:
            try:
                items = crawl_search(url)
                new_items = [it for it in items if it["id"] not in seen[url]]
                log.info(f"Получено {len(items)} объявлений, новых={len(new_items)} по {url}")

                # Обновляем «увиденные»
                for it in new_items:
                    seen[url].add(it["id"])

                # здесь позже можно включить отправку в Telegram
                # for it in new_items:
                #     send_telegram_message(f"🆕 {it['title']}\n{it['price']}\n{it['link']}")

            except Exception as e:
                log.warning(f"Ошибка проверки для {url}: {e}")

            # небольшая пауза между разными ссылками
            time.sleep(random.uniform(1.0, 2.5))

        # Пауза между циклами
        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    log.info(f"Сервис запущен. Интервал проверок: {CHECK_INTERVAL}s")
    main()
