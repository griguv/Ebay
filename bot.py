import os
import re
import json
import time
import logging
import requests
import cloudscraper
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse, urlsplit, parse_qsl

# ================== НАСТРОЙКИ ==================
EBAY_URLS = [
    # можно оставлять исходные ссылки — код сам уберёт _stpos и _fcid
    "https://www.ebay.com/sch/i.html?_udlo=100&_nkw=garmin+astro+320+&_sacat=0&_fcid=1&_stpos=19720",
]

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_IDS = [c.strip() for c in os.getenv("CHAT_ID", "").split(",") if c.strip()]

CHECK_INTERVAL = 180           # каждые 3 мин
REPORT_INTERVAL = 1800         # отчёт раз в 30 мин
ERROR_NOTIFY_INTERVAL = 1800   # уведомлять об ошибках не чаще чем раз в 30 мин
REQUEST_TIMEOUT = 25
MAX_PAGES = 3                  # пагинация: до 3 страниц
RSS_FALLBACK_THRESHOLD = 5     # если HTML дал < 5 объявлений — пробуем RSS

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122 Safari/537.36"),
    "Accept-Language": "en-US,en;q=0.9",
}

# ================== ЛОГИ ==================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    force=True,
)
log = logging.getLogger("ebay-bot")

# ================== ГЛОБАЛЬНЫЕ ==================
scraper = cloudscraper.create_scraper()
seen_items = {url: set() for url in EBAY_URLS}
last_error_time = datetime.min
last_report_time = datetime.now()
checks_count = 0
new_items_count = 0

# ================== УТИЛИТЫ ==================
def _strip_params(url: str, keys_to_drop) -> str:
    """Убираем из URL указанные GET-параметры (например _stpos, _fcid)."""
    u = urlparse(url)
    q = dict(parse_qsl(u.query, keep_blank_values=True))
    for k in keys_to_drop:
        q.pop(k, None)
    new_q = urlencode(q, doseq=True)
    return urlunparse((u.scheme, u.netloc, u.path, u.params, new_q, u.fragment))

def _clean_base_url(url: str) -> str:
    """Готовим базовый URL поиска: без гео-параметров, с _ipg=240 и rt=nc."""
    url = _strip_params(url, ["_stpos", "_fcid"])
    u = urlparse(url)
    q = dict(parse_qsl(u.query, keep_blank_values=True))
    q.pop("_pgn", None)
    q["_ipg"] = "240"
    q["rt"] = "nc"
    new_q = urlencode(q, doseq=True)
    base = urlunparse((u.scheme, u.netloc, u.path, u.params, new_q, u.fragment))
    return base

def _extract_item_id(link: str) -> str:
    """
    Извлекаем стабильный id:
    1) из query (item id вроде 'mkevt' / 'epid' / 'itm' / 'hash' с item=XXXX);
    2) из пути /itm/<id>;
    3) из конца ссылки;
    4) fallback: сама ссылка целиком.
    """
    try:
        # Популярные шаблоны eBay
        # 1) .../itm/1234567890
        m = re.search(r"/itm/(\d{8,})", link)
        if m:
            return m.group(1)

        # 2) параметр 'item' в query
        qs = parse_qs(urlsplit(link).query)
        for key in ("item", "itemid", "itm", "nid"):
            if key in qs and qs[key]:
                cand = qs[key][0]
                if cand.isdigit():
                    return cand

        # 3) Хвост URL до '?'
        tail = link.split("?")[0].rstrip("/").split("/")[-1]
        if tail and any(ch.isalnum() for ch in tail):
            return tail

    except Exception:
        pass
    return link  # самый стабильный fallback — вся ссылка

def _post_telegram(text: str):
    if not BOT_TOKEN or not CHAT_IDS:
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    for cid in CHAT_IDS:
        try:
            r = requests.post(url, data={"chat_id": cid, "text": text}, timeout=15)
            if r.status_code != 200:
                log.warning(f"Ошибка Telegram ({cid}): {r.text}")
        except Exception as e:
            log.warning(f"Сетевая ошибка Telegram ({cid}): {e}")

# ================== ПАРСИНГ ==================
def _parse_json_ld(soup: BeautifulSoup):
    items = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            blob = script.string
            if not blob:
                continue
            data = json.loads(blob)
            # Вариант списка
            if isinstance(data, list):
                for node in data:
                    items.extend(_items_from_json_node(node))
            # Вариант словаря
            elif isinstance(data, dict):
                items.extend(_items_from_json_node(data))
        except Exception:
            continue
    return items

def _items_from_json_node(node):
    items = []
    # форматы бывают разные: ItemList -> itemListElement[], либо сразу Offer/Products
    try:
        if isinstance(node, dict):
            if "itemListElement" in node:
                for el in node["itemListElement"]:
                    product = el.get("item") or el.get("url") or el
                    rec = _item_from_json_product(product)
                    if rec:
                        items.append(rec)
            else:
                rec = _item_from_json_product(node)
                if rec:
                    items.append(rec)
    except Exception:
        pass
    return items

def _item_from_json_product(prod):
    try:
        if isinstance(prod, str):
            link = prod
            title = ""
            price = ""
        elif isinstance(prod, dict):
            link = prod.get("url") or prod.get("link") or ""
            title = prod.get("name") or prod.get("title") or ""
            offers = prod.get("offers") or {}
            if isinstance(offers, dict):
                price = ((offers.get("priceCurrency") or "") + " " + (offers.get("price") or "")).strip()
            else:
                price = ""
        else:
            return None

        if not link:
            return None
        item_id = _extract_item_id(link)
        if not title:
            # иногда есть "description" вместо имени
            title = (prod.get("description") or "").strip() if isinstance(prod, dict) else ""
        title = title[:300]
        return {"id": item_id, "title": title or "(no title)", "price": price or "", "link": link}
    except Exception:
        return None

def _parse_html_items(soup: BeautifulSoup):
    """
    HTML-парсинг: пробуем сразу два варианта селектора,
    чтобы охватить разные раскладки eBay.
    """
    items = []

    # Вариант 1: классический список
    nodes = soup.select("li.s-item")
    # Вариант 2: иногда eBay вкладывает внутрь .srp-results
    nodes2 = soup.select(".srp-results .s-item")
    if len(nodes2) > len(nodes):
        nodes = nodes2

    for card in nodes:
        try:
            # Иногда eBay добавляет "Sponsored", их отфильтровываем
            badge = card.select_one(".s-item__title--tagblock, .s-item__title--tag")
            if badge and "sponsored" in badge.get_text(" ").lower():
                continue

            title_tag = card.select_one(".s-item__title")
            link_tag = card.select_one("a.s-item__link, a.s-item__title")
            price_tag = card.select_one(".s-item__price")

            if not link_tag:
                continue

            link = link_tag.get("href", "").strip()
            if not link:
                continue

            title = title_tag.get_text(strip=True) if title_tag else ""
            price = price_tag.get_text(strip=True) if price_tag else ""
            item_id = _extract_item_id(link)

            items.append({"id": item_id, "title": title or "(no title)", "price": price, "link": link})
        except Exception:
            continue

    return items

def _parse_rss(url: str):
    items = []
    rss_url = url + ("&" if ("?" in url) else "?") + "_rss=1"
    try:
        resp = scraper.get(rss_url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "xml")

        for it in soup.find_all("item"):
            title = it.title.get_text(strip=True) if it.title else ""
            link = it.link.get_text(strip=True) if it.link else ""
            if not link:
                continue
            # Цена в description:
            desc = it.description.get_text(strip=True) if it.description else ""
            m = re.search(r"\$\s?[\d,]+(?:\.\d+)?", desc)
            price = m.group(0) if m else ""
            item_id = _extract_item_id(link)
            items.append({"id": item_id, "title": title or "(no title)", "price": price, "link": link})
    except Exception as e:
        log.warning(f"Ошибка RSS: {e}")

    return items

def fetch_listings(search_url: str):
    """
    Комбинация источников:
      1) JSON-LD
      2) HTML (.s-item)
      3) RSS (если HTML дал мало)
    """
    base = _clean_base_url(search_url)
    log.info(f"Base URL: {base}")

    aggregated = []
    samples_for_log = []  # соберём примеры

    for p in range(1, MAX_PAGES + 1):
        page_url = f"{base}&_pgn={p}"
        try:
            log.info(f"Загружаю страницу {p}/{MAX_PAGES}: {page_url}")
            resp = scraper.get(page_url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")

            items = _parse_json_ld(soup)
            if not items:
                items = _parse_html_items(soup)

            log.info(f"HTML p={p}: найдено {len(items)}")
            aggregated.extend(items)

            # соберём примеры для логов (только 3 шт. на страницу)
            for it in items[:3]:
                samples_for_log.append(f"• {it['title'][:60]} — {it['price']}")

            # иногда пустая следующая страница — выходим
            if len(items) == 0 and p > 1:
                break
        except Exception as e:
            log.warning(f"Ошибка HTML p={p}: {e}")

    if aggregated and samples_for_log:
        log.info("Примеры (первые 3 на каждой странице):\n" + "\n".join(samples_for_log[:9]))

    # если HTML дал слишком мало — пробуем RSS
    if len(aggregated) < RSS_FALLBACK_THRESHOLD:
        rss_items = _parse_rss(base)
        log.warning(f"HTML дал мало ({len(aggregated)}) — RSS вернул {len(rss_items)}")
        aggregated.extend(rss_items)

    # дедупликация по ID (если ID пустой — по ссылке он уже равен всей ссылке)
    uniq = {}
    for it in aggregated:
        uniq[it["id"]] = it

    log.info(f"Итог: собрано {len(uniq)} объявлений (после JSON/HTML + RSS)")
    return list(uniq.values())

# ================== ОСНОВНОЙ ЦИКЛ ==================
def main():
    global last_report_time, checks_count, new_items_count, last_error_time

    log.info(f"Сервис запущен. Интервал проверок: {CHECK_INTERVAL}s")

    # Инициализация — не уведомляем, просто запоминаем
    for url in EBAY_URLS:
        try:
            items = fetch_listings(url)
            for it in items:
                seen_items[url].add(it["id"])
            log.info(f"Инициализация: сохранено {len(items)} объявлений по {url}")
        except Exception as e:
            log.warning(f"Ошибка инициализации {url}: {e}")

    while True:
        checks_count += 1
        log.info(f"Проверка #{checks_count} начата")

        for url in EBAY_URLS:
            try:
                items = fetch_listings(url)
                new_count = 0
                for it in items:
                    if it["id"] not in seen_items[url]:
                        seen_items[url].add(it["id"])
                        new_items_count += 1
                        new_count += 1
                        msg = (
                            "🆕 Новое объявление на eBay!\n"
                            f"📌 {it['title']}\n"
                            f"💲 {it['price']}\n"
                            f"🔗 {it['link']}"
                        )
                        _post_telegram(msg)
                log.info(f"Получено {len(items)} валидных, новых={new_count} по {url}")
            except Exception as e:
                log.warning(f"Ошибка проверки {url}: {e}")
                if datetime.now() - last_error_time > timedelta(seconds=ERROR_NOTIFY_INTERVAL):
                    _post_telegram(f"⚠️ Ошибка при проверке {url}: {e}")
                    last_error_time = datetime.now()

        # отчёт раз в 30 минут
        if datetime.now() - last_report_time > timedelta(seconds=REPORT_INTERVAL):
            report = (
                "📊 Отчёт за 30 минут\n"
                f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"🔎 Проверок: {checks_count}\n"
                f"🆕 Новых объявлений: {new_items_count}\n"
                "✅ Бот работает"
            )
            _post_telegram(report)
            last_report_time = datetime.now()
            checks_count = 0
            new_items_count = 0

        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
