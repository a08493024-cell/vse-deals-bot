"""Telegram-бот на aiogram 3.x."""
import asyncio
import io
import logging
from datetime import datetime

from aiogram import Bot, Dispatcher, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import Command
from aiogram.types import (
    BufferedInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)

from .config import ALLOWED_USERS, APP_URL, PROXY_URL, TELEGRAM_BOT_TOKEN
from .parser import ParseError, fetch_deals
from . import ai_analyzer, parser as _parser_module

logger = logging.getLogger(__name__)

router = Router()


# ─── Фильтр белого списка ───

def _is_allowed(message: Message) -> bool:
    """Проверяет, есть ли отправитель в белом списке. Если список пуст — доступ закрыт."""
    if not ALLOWED_USERS:
        return False
    return message.from_user is not None and message.from_user.id in ALLOWED_USERS


async def _deny(message: Message) -> None:
    await message.answer("⛔ У вас нет доступа к этому боту.")


# ─── Клавиатура ───

def _make_bot() -> Bot:
    """Создаёт экземпляр Bot с прокси если задан PROXY_URL."""
    session = AiohttpSession(proxy=PROXY_URL) if PROXY_URL else None
    return Bot(token=TELEGRAM_BOT_TOKEN, session=session)


def _webapp_keyboard(label: str = "🔥 Открыть все акции") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=label, web_app=WebAppInfo(url=APP_URL))
    ]])


# ─── Форматирование сообщения ───

def _format_deals_message(result: dict) -> str:
    """Форматирует топ-3 для отправки в Telegram."""
    now = datetime.now().strftime("%d.%m.%Y")
    lines = [
        f"🔥 Горячие акции ВсеИнструменты · {now}",
        "Скидки от 50% · Анализ ИИ",
        "",
    ]

    deals = result.get("deals", [])[:3]
    if not deals:
        return "Сегодня акций 50%+ не найдено 🤷"

    for deal in deals:
        rank = deal["rank"]
        name = deal["name"]
        old_price = deal["old_price"]
        new_price = deal["new_price"]
        discount = deal["discount_percent"]
        rating = deal.get("rating")
        reviews = deal.get("reviews_count")
        url = deal.get("product_url", "")
        ai = deal.get("ai", {})

        rec = ai.get("recommendation", "нет данных").upper()
        rmin = ai.get("resale_price_min", 0)
        rmax = ai.get("resale_price_max", 0)
        margin = ai.get("margin_percent", 0)

        rating_str = f"⭐ {rating}" if rating else ""
        reviews_str = f"({reviews} отзывов)" if reviews else ""

        lines.append(f"{rank}. {name}")
        lines.append(f"   💰 {old_price:,} → {new_price:,} ₽  [-{discount}%]".replace(",", " "))
        if rating_str or reviews_str:
            lines.append(f"   {rating_str} {reviews_str}".strip())
        lines.append(f"   🤖 {rec} · перепродажа {rmin:,}–{rmax:,} ₽ · маржа ~{margin}%".replace(",", " "))
        lines.append(f"   🔗 {url}")
        lines.append("")

    total = result.get("total_found", 0)
    lines.append("━━━━━━━━━━━━━━━")
    lines.append(f"📊 Всего акций 50%+: {total}")

    return "\n".join(lines)


# ─── Ежедневная рассылка всем из белого списка ───

async def run_parse_and_notify() -> dict:
    """Запускает парсинг + ИИ-анализ и рассылает результат всем из ALLOWED_USERS."""
    try:
        raw_deals = await fetch_deals()
        total_found = len(raw_deals)
        result = await ai_analyzer.analyze_and_store(raw_deals, total_found)
        text = _format_deals_message(result)
        kb = _webapp_keyboard("🔥 Все 10 товаров с анализом ИИ") if result.get("deals") else None
    except ParseError:
        text = "⚠️ Парсер не смог получить данные. Возможно, сайт изменил структуру."
        result = ai_analyzer.last_result
        kb = None

    if not TELEGRAM_BOT_TOKEN or not ALLOWED_USERS:
        logger.warning("Рассылка пропущена: токен или список пользователей не заданы")
        return result

    bot = _make_bot()
    sent = 0
    for uid in ALLOWED_USERS:
        try:
            await bot.send_message(chat_id=uid, text=text, reply_markup=kb)
            sent += 1
        except Exception as exc:
            logger.warning("Не удалось отправить сообщение пользователю %d: %s", uid, exc)
    await bot.session.close()
    logger.info("Рассылка завершена: %d/%d получателей", sent, len(ALLOWED_USERS))
    return result


# ─── Обработчики команд ───

@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    if not _is_allowed(message):
        await _deny(message)
        return
    text = (
        "Привет! 🔧 Я слежу за акциями ВсеИнструменты и нахожу лучшие сделки для перепродажи.\n\n"
        "Каждый день в 10:00 МСК присылаю топ-3 выгодных товара с ИИ-анализом."
    )
    await message.answer(text, reply_markup=_webapp_keyboard())


@router.message(Command("check"))
async def cmd_check(message: Message) -> None:
    if not _is_allowed(message):
        await _deny(message)
        return
    await message.answer("⏳ Запускаю парсинг, подождите...")
    try:
        raw_deals = await fetch_deals()
        total_found = len(raw_deals)
        result = await ai_analyzer.analyze_and_store(raw_deals, total_found)
        text = _format_deals_message(result)
        reply_markup = _webapp_keyboard("🔥 Все 10 товаров с анализом ИИ") if result.get("deals") else None
    except ParseError:
        text = "⚠️ Парсер не смог получить данные. Возможно, сайт изменил структуру."
        reply_markup = None
    await message.answer(text, reply_markup=reply_markup)


@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    if not _is_allowed(message):
        await _deny(message)
        return
    result = ai_analyzer.last_result
    updated = result.get("updated_at") or "нет данных"
    total = result.get("total_found", 0)
    await message.answer(
        f"Последнее обновление: {updated}\n"
        f"Найдено акций 50%+: {total}"
    )


@router.message(Command("myid"))
async def cmd_myid(message: Message) -> None:
    """Служебная команда: показывает свой Telegram ID (доступна всем для первоначальной настройки)."""
    uid = message.from_user.id if message.from_user else "неизвестно"
    await message.answer(
        f"Ваш Telegram ID: `{uid}`\n\n"
        "Добавьте этот ID в переменную ALLOWED\\_USERS в .env через запятую.",
        parse_mode="Markdown",
    )


# ─── Telegram CAPTCHA solver (бесплатный Вариант Б) ───

# Future для ожидания ответа администратора
_captcha_future: asyncio.Future | None = None
_captcha_bot: Bot | None = None


async def _telegram_captcha_callback(image_bytes: bytes) -> int | None:
    """
    Шлёт CAPTCHA-картинку первому администратору в белом списке,
    ждёт ответа `/captcha <угол>` до 3 минут.
    """
    global _captcha_future, _captcha_bot

    if not ALLOWED_USERS or not TELEGRAM_BOT_TOKEN:
        return None

    admin_id = next(iter(ALLOWED_USERS))
    bot = _captcha_bot or _make_bot()

    loop = asyncio.get_event_loop()
    _captcha_future = loop.create_future()

    try:
        await bot.send_photo(
            chat_id=admin_id,
            photo=BufferedInputFile(image_bytes, filename="captcha.png"),
            caption=(
                "🔐 Требуется решить CAPTCHA!\n\n"
                "Посмотри на картинку — на сколько градусов нужно повернуть её ПРОТИВ часовой стрелки "
                "чтобы она стала горизонтальной?\n\n"
                "Ответь: `/captcha <угол>` (например `/captcha 90`)"
            ),
        )
        logger.info("CAPTCHA отправлена администратору %d", admin_id)
        # Ждём ответа до 3 минут
        angle = await asyncio.wait_for(_captcha_future, timeout=180)
        return angle
    except asyncio.TimeoutError:
        logger.warning("Таймаут ожидания ответа на CAPTCHA")
        return None
    except Exception as e:
        logger.warning("Ошибка отправки CAPTCHA в Telegram: %s", e)
        return None
    finally:
        _captcha_future = None


# Регистрируем callback в модуле парсера при импорте
_parser_module._telegram_captcha_callback = _telegram_captcha_callback


@router.message(Command("captcha"))
async def cmd_captcha(message: Message) -> None:
    """Получает угол решения CAPTCHA от администратора."""
    global _captcha_future
    if not _is_allowed(message):
        return

    parts = (message.text or "").strip().split()
    if len(parts) < 2:
        await message.answer("Использование: /captcha <угол от 0 до 360>")
        return

    try:
        angle = int(parts[1])
        if not 0 <= angle <= 360:
            raise ValueError
    except ValueError:
        await message.answer("Угол должен быть числом от 0 до 360")
        return

    if _captcha_future and not _captcha_future.done():
        _captcha_future.set_result(angle)
        await message.answer(f"✅ Принято! Применяю угол {angle}°...")
    else:
        await message.answer("Сейчас нет ожидающей CAPTCHA.")


def create_dispatcher() -> Dispatcher:
    dp = Dispatcher()
    dp.include_router(router)
    return dp


async def start_polling() -> None:
    """Запускает polling бота (блокирующий вызов)."""
    if not TELEGRAM_BOT_TOKEN:
        logger.warning("TELEGRAM_BOT_TOKEN не задан — бот не запущен")
        return
    bot = _make_bot()
    dp = create_dispatcher()
    logger.info("Бот запущен. Белый список: %d пользователей", len(ALLOWED_USERS))
    await dp.start_polling(bot)
