"""Парсинг акций с vseinstrumenti.ru."""
import asyncio
import logging
from dataclasses import dataclass, field
from typing import Optional

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

BASE_URL = "https://www.vseinstrumenti.ru"
SALES_URL = f"{BASE_URL}/sales/"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Cache-Control": "max-age=0",
    "Referer": "https://www.google.ru/",
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


def _extract_price(text: str) -> int:
    """Извлекает целое число рублей из строки вида '8 990 ₽'."""
    cleaned = ""
    for ch in text:
        if ch.isdigit():
            cleaned += ch
    return int(cleaned) if cleaned else 0


def _parse_page(html: str, page_num: int) -> list[Product]:
    """Парсит одну HTML-страницу и возвращает список товаров."""
    soup = BeautifulSoup(html, "lxml")
    products: list[Product] = []

    # Ищем карточки товаров — несколько возможных селекторов
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
            # Название
            name_el = (
                card.select_one("a[class*='name']")
                or card.select_one("span[class*='name']")
                or card.select_one("div[class*='name']")
                or card.select_one("h3")
                or card.select_one("h2")
                or card.select_one("a[class*='title']")
                or card.select_one("p[class*='title']")
            )
            if not name_el:
                continue
            name = name_el.get_text(strip=True)
            if not name:
                continue

            # Новая цена
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
                # Попробовать из атрибута content
                new_price = int(float(new_price_el.get("content", "0") or "0"))
            if new_price == 0:
                continue

            # Старая цена
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

            # Рейтинг
            rating: Optional[float] = None
            rating_el = card.select_one("[class*='rating']") or card.select_one("[itemprop='ratingValue']")
            if rating_el:
                try:
                    rating_text = rating_el.get("content") or rating_el.get_text(strip=True)
                    if rating_text:
                        rating = float(rating_text.replace(",", "."))
                except (ValueError, TypeError):
                    pass

            # Количество отзывов
            reviews_count: Optional[int] = None
            reviews_el = (
                card.select_one("[class*='review']")
                or card.select_one("[itemprop='reviewCount']")
                or card.select_one("[class*='comment']")
            )
            if reviews_el:
                try:
                    rc_text = reviews_el.get("content") or reviews_el.get_text(strip=True)
                    reviews_count = int("".join(c for c in rc_text if c.isdigit()) or "0") or None
                except (ValueError, TypeError):
                    pass

            # URL товара
            link_el = card.select_one("a[href]")
            if not link_el:
                continue
            href = link_el.get("href", "")
            product_url = href if href.startswith("http") else BASE_URL + href

            # URL изображения
            img_el = card.select_one("img[src]") or card.select_one("img[data-src]")
            image_url = ""
            if img_el:
                image_url = img_el.get("data-src") or img_el.get("src") or ""
                if image_url and not image_url.startswith("http"):
                    image_url = BASE_URL + image_url

            products.append(Product(
                name=name,
                old_price=old_price,
                new_price=new_price,
                discount_percent=discount_percent,
                rating=rating,
                reviews_count=reviews_count,
                product_url=product_url,
                image_url=image_url,
            ))

        except Exception as exc:
            logger.debug("Ошибка при парсинге карточки: %s", exc)
            continue

    return products


async def _warmup(client: httpx.AsyncClient) -> None:
    """Заходит на главную страницу чтобы получить cookies и не выглядеть как бот."""
    try:
        await client.get(BASE_URL, headers=HEADERS, timeout=15.0, follow_redirects=True)
        await asyncio.sleep(2)
    except Exception:
        pass


async def _fetch_page(client: httpx.AsyncClient, page: int) -> str:
    """Загружает одну страницу с повторными попытками."""
    import random
    params = {"page": page} if page > 1 else {}
    last_exc: Optional[Exception] = None
    for attempt in range(1, 4):
        try:
            response = await client.get(SALES_URL, params=params, headers=HEADERS, timeout=30.0, follow_redirects=True)
            response.raise_for_status()
            return response.text
        except Exception as exc:
            last_exc = exc
            logger.warning("Страница %d, попытка %d/3 — ошибка: %s", page, attempt, exc)
            if attempt < 3:
                await asyncio.sleep(10 + random.uniform(1, 5))
    raise last_exc  # type: ignore[misc]


async def fetch_deals() -> list[dict]:
    """
    Главная функция: парсит страницы 1-5, фильтрует скидки ≥50%,
    сортирует и возвращает топ-10 в виде списка словарей.
    """
    import random
    logger.info("Запуск парсера vseinstrumenti.ru")
    all_products: list[Product] = []

    async with httpx.AsyncClient(follow_redirects=True, http2=False) as client:
        # Прогрев сессии через главную страницу
        await _warmup(client)

        for page_num in range(1, 6):
            try:
                html = await _fetch_page(client, page_num)
                products = _parse_page(html, page_num)
                all_products.extend(products)
                logger.info("Страница %d: +%d товаров (итого %d)", page_num, len(products), len(all_products))
                await asyncio.sleep(random.uniform(2, 4))  # случайная задержка
            except Exception as exc:
                logger.warning("Не удалось загрузить страницу %d: %s", page_num, exc)

    if not all_products and len(all_products) == 0:
        logger.error("Парсер не нашёл ни одного товара — структура сайта изменилась или сайт недоступен")
        raise ParseError("Сайт изменил структуру или недоступен")

    # Фильтруем: только скидка ≥ 50%
    filtered = [p for p in all_products if p.discount_percent >= 50]
    logger.info("Товаров со скидкой 50%%+: %d из %d", len(filtered), len(all_products))

    if not filtered:
        return []

    # Сортировка по убыванию скидки
    filtered.sort(key=lambda p: p.discount_percent, reverse=True)
    top10 = filtered[:10]

    return [
        {
            "name": p.name,
            "old_price": p.old_price,
            "new_price": p.new_price,
            "discount_percent": p.discount_percent,
            "rating": p.rating,
            "reviews_count": p.reviews_count,
            "product_url": p.product_url,
            "image_url": p.image_url,
        }
        for p in top10
    ]
