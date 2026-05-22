"""
Парсинг акций с vseinstrumenti.ru.
Playwright + servicepipe.ru bypass.

Приоритет решения CAPTCHA:
  1. Сохранённые куки (session_state.json, < SESSION_MAX_AGE сек) — бесплатно
  2. 2captcha API (TWOCAPTCHA_API_KEY) — платно
  3. Telegram-бот: шлём картинку админу, ждём ответа с углом — бесплатно

Для обновления куки вручную: python renew_session.py
"""
import asyncio
import base64
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Optional

import httpx
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, Page, BrowserContext

import google.generativeai as genai

from .config import TWOCAPTCHA_API_KEY, PARSER_PROXY_URL, GEMINI_API_KEY

logger = logging.getLogger(__name__)

BASE_URL = "https://www.vseinstrumenti.ru"
SALES_URL = f"{BASE_URL}/sales/"

SESSION_FILE = os.path.join(os.path.dirname(__file__), "..", "session_state.json")
SESSION_MAX_AGE = 20 * 3600  # 20 часов

BROWSER_HEADERS = {
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
}

# Callback для Telegram-решения CAPTCHA: set(_telegram_captcha_callback) из bot.py
_telegram_captcha_callback: Optional[object] = None


class ParseError(Exception):
    pass


@dataclass
class Product:
    name: str
    old_price: int
    new_price: int
    discount_percent: int
    rating: Optional[float]
    reviews_count: Optional[int]
    product_url: str
    image_url: str


# ─── Сохранение / загрузка сессии ───

def _load_session_state() -> Optional[dict]:
    """Загружает сохранённые куки если они свежие."""
    if not os.path.exists(SESSION_FILE):
        return None
    age = time.time() - os.path.getmtime(SESSION_FILE)
    if age > SESSION_MAX_AGE:
        logger.info("Сохранённая сессия устарела (%.0fч), нужно обновить", age / 3600)
        return None
    try:
        with open(SESSION_FILE, encoding="utf-8") as f:
            state = json.load(f)
        logger.info("Загружена сохранённая сессия (возраст %.0fмин)", age / 60)
        return state
    except Exception as e:
        logger.warning("Не удалось загрузить session_state.json: %s", e)
        return None


async def _save_session_state(context: BrowserContext) -> None:
    """Сохраняет текущие куки для повторного использования."""
    try:
        state = await context.storage_state()
        with open(SESSION_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f)
        logger.info("Сессия сохранена в session_state.json")
    except Exception as e:
        logger.warning("Не удалось сохранить сессию: %s", e)


# ─── Gemini Vision CAPTCHA solver (бесплатно) ───

async def _solve_rotate_captcha_gemini(image_bytes: bytes) -> Optional[int]:
    """
    Отправляет CAPTCHA-картинку в Gemini Vision и получает угол поворота.
    Использует уже настроенный GEMINI_API_KEY — полностью бесплатно.
    Делает до 3 попыток с паузой при 429 rate-limit.
    """
    if not GEMINI_API_KEY:
        return None

    genai.configure(api_key=GEMINI_API_KEY)

    # Пробуем несколько моделей (у разных может быть разная квота)
    models_to_try = ["gemini-1.5-flash", "gemini-2.0-flash", "gemini-1.5-flash-latest"]
    img_b64 = base64.b64encode(image_bytes).decode()
    image_part = {"mime_type": "image/png", "data": img_b64}
    prompt = (
        "This image shows an object that has been rotated from its natural horizontal position. "
        "How many degrees CLOCKWISE must it be rotated to appear horizontal and upright? "
        "Reply with ONLY a number 0-360."
    )

    for model_name in models_to_try:
        for attempt in range(3):
            try:
                model = genai.GenerativeModel(model_name)
                loop = asyncio.get_event_loop()
                response = await loop.run_in_executor(
                    None, lambda m=model: m.generate_content([image_part, prompt])
                )
                text = (response.text or "").strip()
                m_match = re.search(r"\d+", text)
                if m_match:
                    angle = max(0, min(360, int(m_match.group())))
                    logger.info("Gemini (%s) решил CAPTCHA: угол %d°", model_name, angle)
                    return angle
                logger.warning("Gemini (%s) не вернул число: %r", model_name, text)
                break
            except Exception as e:
                err = str(e)
                if "429" in err or "quota" in err.lower() or "rate" in err.lower():
                    # Ждём рекомендованную задержку или 15 секунд
                    import re as _re
                    delay_m = _re.search(r"retry.*?(\d+)\s*s", err, _re.I)
                    delay = int(delay_m.group(1)) + 2 if delay_m else 15
                    if attempt < 2:
                        logger.info("Gemini rate limit, ждём %ds...", delay)
                        await asyncio.sleep(delay)
                        continue
                    logger.warning("Gemini (%s) rate limit после 3 попыток", model_name)
                else:
                    logger.warning("Gemini (%s) ошибка: %s", model_name, err[:100])
                break

    return None


# ─── 2captcha ───

async def _solve_rotate_captcha(image_bytes: bytes) -> Optional[int]:
    """Отправляет изображение в 2captcha, возвращает угол поворота."""
    if not TWOCAPTCHA_API_KEY:
        return None

    img_b64 = base64.b64encode(image_bytes).decode()

    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(
            "https://2captcha.com/in.php",
            data={"key": TWOCAPTCHA_API_KEY, "method": "rotatecaptcha", "body": img_b64, "json": 1},
        )
        resp = r.json()
        if resp.get("status") != 1:
            logger.warning("2captcha отклонил задачу: %s", resp)
            return None

        task_id = resp["request"]
        logger.info("2captcha: задача %s создана", task_id)

        for _ in range(18):
            await asyncio.sleep(5)
            r2 = await client.get(
                "https://2captcha.com/res.php",
                params={"key": TWOCAPTCHA_API_KEY, "action": "get", "id": task_id, "json": 1},
            )
            resp2 = r2.json()
            if resp2.get("status") == 1:
                angle = int(resp2["request"])
                logger.info("2captcha решил: угол %d°", angle)
                return angle
            if resp2.get("request") != "CAPCHA_NOT_READY":
                logger.warning("2captcha ошибка: %s", resp2)
                return None

    logger.warning("2captcha: таймаут")
    return None


# ─── Telegram CAPTCHA (бесплатный вариант Б) ───

async def _solve_via_telegram(image_bytes: bytes) -> Optional[int]:
    """
    Шлёт CAPTCHA-картинку администратору в Telegram, ждёт ответа с углом (0-360).
    Работает только если _telegram_captcha_callback установлен из bot.py.
    """
    if _telegram_captcha_callback is None:
        return None
    try:
        logger.info("Отправляем CAPTCHA в Telegram для ручного решения...")
        angle = await _telegram_captcha_callback(image_bytes)
        if angle is not None:
            logger.info("Telegram-решение: угол %d°", angle)
        return angle
    except Exception as e:
        logger.warning("Telegram CAPTCHA callback ошибка: %s", e)
        return None


# ─── Скачивание CAPTCHA-картинки ───

async def _download_captcha_image(page: Page, context: BrowserContext) -> Optional[bytes]:
    """Скачивает изображение CAPTCHA через куки текущей сессии."""
    img_el = await page.query_selector(".captcha-img img")
    if not img_el:
        img_el = await page.query_selector("img[src*='get_image']")
    if not img_el:
        logger.error("Элемент CAPTCHA-изображения не найден")
        return None

    img_src = await img_el.get_attribute("src")
    if not img_src:
        return None

    if not img_src.startswith("http"):
        base = page.url.rsplit("/", 1)[0]
        img_url = base + "/" + img_src.lstrip("./")
    else:
        img_url = img_src

    logger.info("Загружаем CAPTCHA: %s", img_url)
    cookies = await context.cookies()
    cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)

    async with httpx.AsyncClient() as client:
        resp = await client.get(img_url, headers={"Cookie": cookie_str}, timeout=15)
        image_bytes = resp.content

    if len(image_bytes) < 500:
        logger.error("CAPTCHA-изображение слишком мало (%d байт)", len(image_bytes))
        return None
    return image_bytes


# ─── Playwright — обход servicepipe ───

async def _handle_servicepipe(page: Page, context: BrowserContext) -> bool:
    """
    Обнаруживает challenge servicepipe.ru и проходит его.
    Порядок: сохранённые куки (уже применены) → 2captcha → Telegram.
    """
    # Безопасно читаем контент
    content = ""
    for _ in range(5):
        try:
            content = await page.content()
            break
        except Exception:
            await asyncio.sleep(1)

    # Нет challenge — уже на целевой странице
    if "servicepipe" not in content and "spsn" not in content:
        return True

    logger.info("servicepipe.ru: ждём JS-redirect на /xpvnsulc/...")

    try:
        await page.wait_for_url(re.compile(r"/xpvnsulc/"), timeout=30000)
        logger.info("JS-challenge прошёл: %s", page.url[:80])
    except Exception as e:
        logger.warning("wait_for_url(/xpvnsulc/) timeout: %s", e)
        if "vseinstrumenti.ru/sales" in page.url:
            return True
        return False

    try:
        await page.wait_for_load_state("networkidle", timeout=10000)
    except Exception:
        pass

    content = ""
    for _ in range(3):
        try:
            content = await page.content()
            break
        except Exception:
            await asyncio.sleep(1)

    # Нет CAPTCHA — авто-редирект
    if "sp_rotated_captcha" not in content and "rndcaptcha" not in content:
        logger.info("CAPTCHA нет, ждём редирект на sales...")
        try:
            await page.wait_for_url(
                re.compile(r"vseinstrumenti\.ru/(?!xpvnsulc)"), timeout=15000
            )
            await _save_session_state(context)
            return True
        except Exception:
            if "xpvnsulc" not in page.url:
                await _save_session_state(context)
                return True
            return False

    logger.info("Ротационная CAPTCHA обнаружена")

    # Скачиваем картинку
    image_bytes = await _download_captcha_image(page, context)
    if image_bytes is None:
        return False

    # Приоритет 1: Gemini Vision (бесплатно, уже настроен)
    angle: Optional[int] = await _solve_rotate_captcha_gemini(image_bytes)

    # Приоритет 2: 2captcha (если задан ключ)
    if angle is None:
        angle = await _solve_rotate_captcha(image_bytes)

    # Приоритет 3: Telegram — шлём картинку администратору
    if angle is None:
        angle = await _solve_via_telegram(image_bytes)

    if angle is None:
        logger.error(
            "Нет способа решить CAPTCHA. "
            "Задайте GEMINI_API_KEY или TWOCAPTCHA_API_KEY."
        )
        return False

    # Двигаем слайдер
    control_el = await page.query_selector(".captcha-control")
    button_el = await page.query_selector(".captcha-control-button")
    if not (control_el and button_el):
        logger.error("Слайдер CAPTCHA не найден")
        return False

    ctrl_box = await control_el.bounding_box()
    btn_box = await button_el.bounding_box()
    if not (ctrl_box and btn_box):
        return False

    ctrl_left = ctrl_box["x"]
    ctrl_width = ctrl_box["width"] or 275
    ratio = angle / 360.0
    target_x = ctrl_left + ratio * ctrl_width
    slider_y = btn_box["y"] + btn_box["height"] / 2
    start_x = btn_box["x"] + btn_box["width"] / 2

    await page.mouse.move(start_x, slider_y)
    await page.mouse.down()
    await page.mouse.move(target_x, slider_y, steps=25)
    await page.mouse.up()
    await asyncio.sleep(1.5)

    # Ждём редиректа
    try:
        await page.wait_for_url(
            re.compile(r"vseinstrumenti\.ru/(?!xpvnsulc)"), timeout=20000
        )
        logger.info("CAPTCHA решена: %s", page.url)
        await _save_session_state(context)
        return True
    except Exception:
        await asyncio.sleep(2)
        if "xpvnsulc" not in page.url:
            await _save_session_state(context)
            return True
        logger.warning("Редирект после CAPTCHA не произошёл: %s", page.url)
        return False


async def _fetch_page_playwright(page: Page, context: BrowserContext, page_num: int) -> str:
    """Загружает одну страницу через Playwright."""
    url = SALES_URL if page_num == 1 else f"{SALES_URL}?page={page_num}"

    for attempt in range(1, 4):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)

            if not await _handle_servicepipe(page, context):
                raise Exception("Не удалось пройти CAPTCHA")

            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            await asyncio.sleep(2)

            await page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2)")
            await asyncio.sleep(1)

            return await page.content()

        except Exception as exc:
            logger.warning("Страница %d, попытка %d/3: %s", page_num, attempt, exc)
            if attempt < 3:
                await asyncio.sleep(10)

    raise ParseError(f"Не удалось загрузить страницу {page_num} после 3 попыток")


# ─── Парсинг HTML ───

def _extract_price(text: str) -> int:
    cleaned = "".join(c for c in text if c.isdigit())
    return int(cleaned) if cleaned else 0


def _parse_page(html: str, page_num: int) -> list[Product]:
    soup = BeautifulSoup(html, "lxml")
    products: list[Product] = []

    cards = (
        soup.select("div.product-card")
        or soup.select("article.product-item")
        or soup.select("div[class*='product-card']")
        or soup.select("li[class*='product']")
        or soup.select("div[class*='ProductCard']")
        or soup.select("div[data-product-id]")
    )

    logger.debug("Страница %d: найдено %d карточек", page_num, len(cards))

    for card in cards:
        try:
            name_el = (
                card.select_one("a[class*='name']")
                or card.select_one("span[class*='name']")
                or card.select_one("div[class*='name']")
                or card.select_one("h3") or card.select_one("h2")
                or card.select_one("a[class*='title']")
            )
            if not name_el:
                continue
            name = name_el.get_text(strip=True)
            if not name:
                continue

            new_price_el = (
                card.select_one("span[class*='current']")
                or card.select_one("span[class*='new']")
                or card.select_one("div[class*='price-current']")
                or card.select_one("span[class*='price']")
                or card.select_one("[itemprop='price']")
            )
            if not new_price_el:
                continue
            new_price = _extract_price(new_price_el.get_text())
            if new_price == 0:
                new_price = int(float(new_price_el.get("content", "0") or "0"))
            if new_price == 0:
                continue

            old_price_el = (
                card.select_one("span[class*='old']")
                or card.select_one("s[class*='price']")
                or card.select_one("del")
                or card.select_one("span[class*='crossed']")
                or card.select_one("div[class*='price-old']")
            )
            old_price = _extract_price(old_price_el.get_text()) if old_price_el else 0
            if old_price == 0 or old_price <= new_price:
                continue

            discount_percent = round((1 - new_price / old_price) * 100)

            rating: Optional[float] = None
            rating_el = card.select_one("[class*='rating']") or card.select_one("[itemprop='ratingValue']")
            if rating_el:
                try:
                    rt = rating_el.get("content") or rating_el.get_text(strip=True)
                    if rt:
                        rating = float(rt.replace(",", "."))
                except (ValueError, TypeError):
                    pass

            reviews_count: Optional[int] = None
            reviews_el = (
                card.select_one("[class*='review']")
                or card.select_one("[itemprop='reviewCount']")
                or card.select_one("[class*='comment']")
            )
            if reviews_el:
                try:
                    rc = reviews_el.get("content") or reviews_el.get_text(strip=True)
                    reviews_count = int("".join(c for c in rc if c.isdigit()) or "0") or None
                except (ValueError, TypeError):
                    pass

            link_el = card.select_one("a[href]")
            if not link_el:
                continue
            href = link_el.get("href", "")
            product_url = href if href.startswith("http") else BASE_URL + href

            img_el = card.select_one("img[src]") or card.select_one("img[data-src]")
            image_url = ""
            if img_el:
                image_url = img_el.get("data-src") or img_el.get("src") or ""
                if image_url and not image_url.startswith("http"):
                    image_url = BASE_URL + image_url

            products.append(Product(
                name=name, old_price=old_price, new_price=new_price,
                discount_percent=discount_percent, rating=rating,
                reviews_count=reviews_count, product_url=product_url, image_url=image_url,
            ))

        except Exception as exc:
            logger.debug("Ошибка парсинга карточки: %s", exc)
            continue

    return products


# ─── Главная функция ───

async def fetch_deals() -> list[dict]:
    """
    Парсит страницы 1-5 через Playwright (обходит anti-bot),
    фильтрует скидки ≥50%, возвращает топ-10.
    """
    logger.info("Запуск парсера vseinstrumenti.ru (Playwright)")
    all_products: list[Product] = []

    proxy_cfg = None
    if PARSER_PROXY_URL:
        proxy_cfg = {"server": PARSER_PROXY_URL}

    # Пробуем загрузить сохранённую сессию
    saved_state = _load_session_state()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            proxy=proxy_cfg,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
            ],
        )

        # Если есть свежие куки — используем их (CAPTCHA не нужна)
        context_kwargs = dict(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            locale="ru-RU",
            extra_http_headers=BROWSER_HEADERS,
        )
        if saved_state:
            context_kwargs["storage_state"] = saved_state

        context = await browser.new_context(**context_kwargs)
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
        """)

        page = await context.new_page()

        for page_num in range(1, 6):
            try:
                html = await _fetch_page_playwright(page, context, page_num)
                products = _parse_page(html, page_num)
                all_products.extend(products)
                logger.info("Страница %d: +%d товаров (итого %d)", page_num, len(products), len(all_products))
                await asyncio.sleep(2)
            except ParseError:
                logger.warning("Пропускаем страницу %d", page_num)

        await browser.close()

    if not all_products:
        logger.error("Парсер не нашёл ни одного товара")
        raise ParseError("Сайт изменил структуру или недоступен")

    filtered = [p for p in all_products if p.discount_percent >= 50]
    logger.info("Товаров со скидкой 50%%+: %d из %d", len(filtered), len(all_products))

    if not filtered:
        return []

    filtered.sort(key=lambda p: p.discount_percent, reverse=True)

    return [
        {
            "name": p.name, "old_price": p.old_price, "new_price": p.new_price,
            "discount_percent": p.discount_percent, "rating": p.rating,
            "reviews_count": p.reviews_count, "product_url": p.product_url,
            "image_url": p.image_url,
        }
        for p in filtered[:10]
    ]
