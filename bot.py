# bot.py
import os
import time
import logging
import random
import re
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

import cloudscraper
from bs4 import BeautifulSoup

# ================== НАСТРОЙКИ ==================
EBAY_URLS = [
    "https://www.ebay.com/sch/i.html?_udlo=100&_nkw=garmin+astro+320+&_sacat=0&_stpos=19720&_fcid=1",
]

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "180"))  # каждые 3 мин
PAGES_TO_SCAN = 3
REQUEST_TIMEOUT = 20
RETRIES_PER_PAGE = 3
RETRY_SLEEP = (2.0, 4.0)

UA_ROTATE = [
    # Пара актуальных UA; можно расширить список
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
]

# Telegram пока отключаем (шумно при отладке), включим позже
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID", "")

# ================== ЛОГИ ==================
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("ebay")

# ================== УТИЛИТЫ ==================
def normalize_search_url(url: str) -> str:
    """
    Чистим входной URL от трекинга, оставляем только «белый список»,
    фиксируем _ipg=240 и rt=nc. _pgn будем добавлять позже.
    """
    allowed = {
        "_nkw", "_sacat", "_dcat", "_udlo", "_udhi", "_stpos", "_fcid", "_sop", "_nqc",
    }
    u = urlparse(url)
    params = dict(parse_qsl(u.query, keep_blank_values=True))

    cleaned = {k: v for k, v in params.items() if k in allowed}
    cleaned["_ipg"] = "240"
    cleaned["rt"] = "nc"

    q = urlencode(cleaned, doseq=True)
    return urlunparse((u.scheme, u.netloc, u.path, "", q, ""))

def make_scraper():
    s = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "desktop": True}
    )
    # Базовые «браузерные» заголовки
    s.headers.update({
        "User-Agent": random.choice(UA_ROTATE),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "keep-alive",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Upgrade-Insecure-Requests": "1",
        # Дополнительные client hints — часто помогают миновать заглушки
        "Sec-CH-UA": "\"Chromium\";v=\"126\", \"Not=A?Brand\";v=\"24\"",
        "Sec-CH-UA-Mobile": "?0",
        "Sec-CH-UA-Platform": "\"Windows\"",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-User": "?1",
    })
    return s

def looks_like_human_check(soup: BeautifulSoup, full_text_low: str) -> bool:
    # Текстовые маркеры
    if ("verify you're a human" in full_text_low or
        "to continue to ebay" in full_text_low or
        "access denied" in full_text_low or
        "captcha" in full_text_low):
        return True
    # Типичные DOM-шаблоны проверок
    if soup.select_one("#challenge-form, form[action*='challenge']"):
        return True
    if soup.select_one("iframe[src*='captcha'], img[alt*='captcha']"):
        return True
    return False

def fetch_page_html(scraper, page_url: str, referer: str) -> str:
    """
    Тянем HTML с ретраями. При 503/403 меняем UA, ждём и пробуем снова.
    Добавляем рандомный параметр, чтобы избежать кэша / одинаковых следов.
    """
    last_err = None
    for attempt in range(1, RETRIES_PER_PAGE + 1):
        try:
            scraper.headers["User-Agent"] = random.choice(UA_ROTATE)
            scraper.headers["Referer"] = referer
            # bust cache / vary fingerprint
            rnd = str(random.randint(10**6, 10**7 - 1))
            sep = "&" if "?" in page_url else "?"
            url_with_rnd = f"{page_url}{sep}_rnd={rnd}"

            r = scraper.get(url_with_rnd, timeout=REQUEST_TIMEOUT, allow_redirects=True)
            status = r.status_code
            if status >= 500:
                raise RuntimeError(f"{status} Server Error")
            if status in (403, 429):
                raise RuntimeError(f"{status} Rate/Captcha")
            return r.text
        except Exception as e:
            last_err = e
            log.warning(f"Ошибка HTML на {page_url} (attempt {attempt}/{RETRIES_PER_PAGE}): {e}")
            time.sleep(random.uniform(*RETRY_SLEEP))
    raise last_err or RuntimeError("Неизвестная ошибка загрузки")

def parse_items_from_html(html: str):
    """
    Парсинг карточек:
      - основной: li.s-item (eBay classic)
      - запасной: div.s-item__wrapper или [data-testid='item-card'] (новая верстка)
    """
    soup = BeautifulSoup(html, "html.parser")
    full_text_low = soup.get_text(" ", strip=True).lower()

    if looks_like_human_check(soup, full_text_low):
        return [], True

    # Основной список
    nodes = soup.select("li.s-item")
    # Фоллбэки
    if len(nodes) < 5:
        alt = soup.select("div.s-item__wrapper, [data-testid='item-card']")
        if len(alt) > len(nodes):
            nodes = alt

    items = []
    for n in nodes:
        a = n.select_one("a.s-item__link") or n.select_one("a[href*='/itm/']")
        title_tag = n.select_one("h3.s-item__title") or n.select_one("[data-testid='item-title']")
        price_tag = n.select_one(".s-item__price") or n.select_one("[data-testid='item-price']")
        if not a or not title_tag:
            continue

        link = a.get("href", "").strip()
        title = title_tag.get_text(strip=True)
        price = price_tag.get_text(strip=True) if price_tag else ""

        # itemId из ссылки /itm/1234567890?
        m = re.search(r"/itm/(\d{9,})\b", link) or re.search(r"/(\d{9,})\?", link)
        item_id = m.group(1) if m else link

        items.append({"id": item_id, "title": title, "price": price, "link": link})

    return items, False

# Уже увиденные ID по ссылке поиска
seen = {}

def crawl_search(url: str):
    base = normalize_search_url(url)
    log.info(f"Base URL: {base}")
    scraper = make_scraper()

    all_items = []
    saw_human_check = False

    for p in range(1, PAGES_TO_SCAN + 1):
        page_url = f"{base}&_pgn={p}"
        log.info(f"Загружаю страницу {p}/{PAGES_TO_SCAN}: {page_url}")

        try:
            html = fetch_page_html(scraper, page_url, referer=base)
        except Exception as e:
            log.warning(f"Ошибка HTML p={p}: {e}")
            continue

        items, is_human = parse_items_from_html(html)
        log.info(f"HTML p={p}: найдено {len(items)} карточек")
        all_items.extend(items)
        saw_human_check = saw_human_check or is_human

        if len(items) < 5:  # подозрительно мало — сохраним дамп
            try:
                dump_path = f"/opt/render/project/src/ebay_debug_p{p}.html"
                with open(dump_path, "w", encoding="utf-8") as f:
                    f.write(html)
                log.warning(f"Сохранён HTML дамп для отладки: {dump_path}")
            except Exception as e:
                log.warning(f"Не удалось сохранить дамп HTML: {e}")

        time.sleep(random.uniform(1.2, 2.8))

    # Уникализируем
    uniq = {}
    for it in all_items:
        uniq[it["id"]] = it
    all_items = list(uniq.values())

    if saw_human_check:
        log.warning("Похоже, отдана защитная страница (captcha/human check).")

    log.info(f"Итог: собрано {len(all_items)} объявлений (после чистки дубликатов)")
    return all_items

def main():
    global seen

    # Инициализация
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

                # Отмечаем увиденные
                for it in new_items:
                    seen[url].add(it["id"])

                # Здесь позже вернём Telegram
                # for it in new_items:
                #     send_telegram_message(f"🆕 {it['title']}\n{it['price']}\n{it['link']}")

            except Exception as e:
                log.warning(f"Ошибка проверки для {url}: {e}")

            time.sleep(random.uniform(1.0, 2.0))

        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    log.info(f"Сервис запущен. Интервал проверок: {CHECK_INTERVAL}s")
    main()
