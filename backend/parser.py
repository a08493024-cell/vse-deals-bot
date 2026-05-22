"""
Парсинг акций с vseinstrumenti.ru.
Использует Playwright для обхода servicepipe.ru anti-bot защиты.
Rotation CAPTCHA решается через 2captcha API.
"""
import asyncio
import base64
import logging
import re
import time
from dataclasses import dataclass
from typing import Optional

import httpx
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, Page, BrowserContext

from .config import TWOCAPTCHA_API_KEY, PARSER_PROXY_URL

logger = logging.getLogger(__name__)

BASE_URL = "https://www.vseinstrumenti.ru"
SALES_URL = f"{BASE_URL}/sales/"

BROWSER_HEADERS = {
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
}


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


# ─── 2captcha ───

async def _solve_rotate_captcha(image_bytes: bytes) -> Optional[int]:
    """Отправляет изображение в 2captcha, возвращает угол поворота."""
    if not TWOCAPTCHA_API_KEY:
        logger.warning("TWOCAPTCHA_API_KEY не задан — CAPTCHA не решается")
        return None

    img_b64 = base64.b64encode(image_bytes).decode()

    async with httpx.AsyncClient(timeout=120) as client:
        # Шаг 1: отправить задачу
        r = await client.post(
            "https://2captcha.com/in.php",
            data={
                "key": TWOCAPTCHA_API_KEY,
                "method": "rotatecaptcha",
                "body": img_b64,
                "json": 1,
            },
        )
        resp = r.json()
        if resp.get("status") != 1:
            logger.warning("2captcha отклонил задачу: %s", resp)
            return None

        task_id = resp["request"]
        logger.info("2captcha: задача %s создана", task_id)

        # Шаг 2: ждём решения (до 90 сек)
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

    logger.warning("2captcha: таймаут решения")
    return None


# ─── Playwright — обход servicepipe ───

async def _handle_servicepipe(page: Page, context: BrowserContext) -> bool:
    """
    Обнаруживает challenge servicepipe.ru и проходит его.
    Возвращает True если страница прошла проверку.
    """
    # Безопасно читаем контент (страница может ещё navigating)
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

    logger.info("servicepipe.ru challenge обнаружен, ждём JS-redirect на /xpvnsulc/...")

    # JS-challenge запускается в фоне и редиректит на /xpvnsulc/ для верификации
    try:
        await page.wait_for_url(re.compile(r"/xpvnsulc/"), timeout=30000)
        logger.info("JS-challenge прошёл, URL: %s", page.url[:80])
    except Exception as e:
        logger.warning("wait_for_url(/xpvnsulc/) timeout: %s", e)
        if "vseinstrumenti.ru/sales" in page.url:
            return True
        logger.error("Challenge не прошёл, URL: %s", page.url)
        return False

    # Ждём загрузки страницы верификации
    try:
        await page.wait_for_load_state("networkidle", timeout=10000)
    except Exception:
        pass

    # Читаем контент страницы верификации
    content = ""
    for _ in range(3):
        try:
            content = await page.content()
            break
        except Exception:
            await asyncio.sleep(1)

    # Нет CAPTCHA — ждём авто-редиректа на sales
    if "sp_rotated_captcha" not in content and "rndcaptcha" not in content:
        logger.info("CAPTCHA не нужна, ожидаем редирект на sales...")
        try:
            await page.wait_for_url(
                re.compile(r"vseinstrumenti\.ru/(?!xpvnsulc)"),
                timeout=15000,
            )
            logger.info("Авто-редирект: %s", page.url)
            return True
        except Exception:
            if "xpvnsulc" not in page.url:
                return True
            logger.warning("Редирект не произошёл, URL: %s", page.url)
            return False

    logger.info("Ротационная CAPTCHA обнаружена")

    if not TWOCAPTCHA_API_KEY:
        logger.error("TWOCAPTCHA_API_KEY не задан — решение CAPTCHA невозможно")
        return False

    # Скачиваем изображение CAPTCHA
    img_el = await page.query_selector(".captcha-img img")
    if not img_el:
        img_el = await page.query_selector("img[src*='get_image']")
    if not img_el:
        logger.error("Элемент CAPTCHA-изображения не найден")
        return False

    img_src = await img_el.get_attribute("src")
    if not img_src.startswith("http"):
        base = page.url.rsplit("/", 1)[0]
        img_url = base + "/" + img_src.lstrip("./")
    else:
        img_url = img_src

    logger.info("Загружаем CAPTCHA-изображение: %s", img_url)
    cookies = await context.cookies()
    cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)

    async with httpx.AsyncClient() as client:
        img_resp = await client.get(img_url, headers={"Cookie": cookie_str}, timeout=15)
        image_bytes = img_resp.content

    if len(image_bytes) < 500:
        logger.error("CAPTCHA-изображение слишком мало (%d байт)", len(image_bytes))
        return False

    # Решаем через 2captcha
    angle = await _solve_rotate_captcha(image_bytes)
    if angle is None:
        return False

    # Двигаем слайдер на нужный угол (track 275px, angle 0-360 → x 0-275)
    control_el = await page.query_selector(".captcha-control")
    button_el = await page.query_selector(".captcha-control-button")
    if not (control_el and button_el):
        logger.error("Слайдер CAPTCHA не найден")
        return False

    ctrl_box = await control_el.bounding_box()
    btn_box = await button_el.bounding_box()
    if not (ctrl_box and btn_box):
        logger.error("bounding_box слайдера недоступен")
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

    # Ждём редиректа после решения CAPTCHA
    try:
        await page.wait_for_url(
            re.compile(r"vseinstrumenti\.ru/(?!xpvnsulc)"),
            timeout=20000,
        )
        logger.info("CAPTCHA решена, переход на: %s", page.url)
        return True
    except Exception:
        await asyncio.sleep(2)
        if "xpvnsulc" not in page.url:
            logger.info("CAPTCHA решена (URL check): %s", page.url)
            return True
        logger.warning("Редирект после CAPTCHA не произошёл, URL: %s", page.url)
        return False


async def _fetch_page_playwright(page: Page, context: BrowserContext, page_num: int) -> str:
    """Загружает одну страницу через Playwright."""
    url = SALES_URL if page_num == 1 else f"{SALES_URL}?page={page_num}"

    for attempt in range(1, 4):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)

            # Проверяем и обходим challenge если нужно (ждёт редиректа внутри)
            if not await _handle_servicepipe(page, context):
                raise Exception("Не удалось пройти CAPTCHA")

            # Ждём загрузки продуктов (React/Next.js рендеринг)
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            await asyncio.sleep(2)

            # Пробуем прокрутить для lazy-load контента
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
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            locale="ru-RU",
            extra_http_headers=BROWSER_HEADERS,
        )
        # Скрываем следы автоматизации
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
