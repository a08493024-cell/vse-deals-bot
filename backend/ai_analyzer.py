"""ИИ-анализ товаров через Google Gemini API."""
import asyncio
import json
import logging
from datetime import datetime
from typing import Any

import google.generativeai as genai

from .config import GEMINI_API_KEY

logger = logging.getLogger(__name__)

# Глобальное хранилище последнего результата
last_result: dict[str, Any] = {
    "updated_at": None,
    "total_found": 0,
    "deals": [],
}

SYSTEM_PROMPT = (
    "Ты — эксперт по перепродаже инструментов в России (Авито, Ozon, Wildberries). "
    "Отвечай ТОЛЬКО валидным JSON без markdown, без пояснений, без ```json."
)

USER_PROMPT_TEMPLATE = """Проанализируй товар для перепродажи:

Название: {name}
Цена покупки: {new_price} руб.
Скидка: {discount_percent}%
Рейтинг: {rating} ({reviews_count} отзывов)

Верни JSON строго такой структуры:
{{
  "resale_price_min": <int, нижняя граница цены перепродажи на Авито/Ozon>,
  "resale_price_max": <int, верхняя граница цены перепродажи>,
  "margin_percent": <int, ожидаемая чистая маржа в % после учёта комиссий площадки>,
  "demand": "высокий" | "средний" | "низкий",
  "recommendation": "брать" | "подумать" | "пропустить",
  "reasoning": "<строка до 120 символов, главный аргумент>"
}}"""

_AI_STUB = {
    "resale_price_min": 0,
    "resale_price_max": 0,
    "margin_percent": 0,
    "demand": "неизвестно",
    "recommendation": "нет данных",
    "reasoning": "Анализ недоступен",
}


def _init_gemini() -> genai.GenerativeModel | None:
    """Инициализирует клиент Gemini, возвращает None если ключ не задан."""
    if not GEMINI_API_KEY:
        logger.warning("GEMINI_API_KEY не задан — ИИ-анализ будет пропущен")
        return None
    genai.configure(api_key=GEMINI_API_KEY)
    return genai.GenerativeModel(
        model_name="gemini-2.0-flash",
        system_instruction=SYSTEM_PROMPT,
    )


async def _analyze_one(model: genai.GenerativeModel, product: dict) -> dict:
    """Анализирует один товар через Gemini и возвращает JSON-словарь."""
    prompt = USER_PROMPT_TEMPLATE.format(
        name=product["name"],
        new_price=product["new_price"],
        discount_percent=product["discount_percent"],
        rating=product.get("rating") or "нет данных",
        reviews_count=product.get("reviews_count") or "нет данных",
    )
    try:
        # Gemini SDK синхронный — запускаем в отдельном потоке
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: model.generate_content(prompt),
        )
        raw = response.text.strip()
        # Убираем возможные markdown-обёртки на случай непослушного ответа
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1]
        if raw.endswith("```"):
            raw = raw.rsplit("```", 1)[0]
        result = json.loads(raw)
        logger.debug("Gemini ответил для '%s': %s", product["name"][:40], result.get("recommendation"))
        return result
    except json.JSONDecodeError as exc:
        logger.warning("Не удалось распарсить JSON от Gemini для '%s': %s", product["name"][:40], exc)
        return _AI_STUB.copy()
    except Exception as exc:
        logger.warning("Ошибка Gemini для '%s': %s", product["name"][:40], exc)
        return _AI_STUB.copy()


async def analyze_and_store(raw_deals: list[dict], total_found: int) -> dict:
    """
    Принимает список товаров, запускает параллельный ИИ-анализ,
    сохраняет результат в last_result и возвращает его.
    """
    global last_result

    model = _init_gemini()

    if model is not None:
        logger.info("Запуск ИИ-анализа для %d товаров (параллельно)", len(raw_deals))
        ai_results = await asyncio.gather(
            *[_analyze_one(model, product) for product in raw_deals],
            return_exceptions=True,
        )
    else:
        ai_results = [_AI_STUB.copy() for _ in raw_deals]

    deals = []
    for rank, (product, ai) in enumerate(zip(raw_deals, ai_results), start=1):
        if isinstance(ai, Exception):
            logger.warning("Исключение в анализе товара %d: %s", rank, ai)
            ai = _AI_STUB.copy()
        deals.append({
            "rank": rank,
            **product,
            "ai": ai,
        })

    now = datetime.now().isoformat(timespec="seconds")
    last_result = {
        "updated_at": now,
        "total_found": total_found,
        "deals": deals,
    }
    logger.info("ИИ-анализ завершён. Обновлено в %s", now)
    return last_result
