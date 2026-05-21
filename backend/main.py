"""FastAPI приложение: точка входа, роуты, CORS."""
import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from .config import REFRESH_SECRET
from .parser import ParseError, fetch_deals
from . import ai_analyzer
from .scheduler import create_scheduler
from .bot import start_polling

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

FRONTEND_PATH = Path(__file__).parent.parent / "frontend" / "index.html"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Запускаем фоновый парсинг при старте
    asyncio.create_task(_initial_refresh())

    # Запускаем планировщик
    scheduler = create_scheduler()
    scheduler.start()
    logger.info("Планировщик запущен")

    # Запускаем бота в фоне (polling)
    asyncio.create_task(start_polling())

    yield

    scheduler.shutdown(wait=False)
    logger.info("Планировщик остановлен")


async def _initial_refresh() -> None:
    """Первоначальный парсинг при запуске приложения."""
    logger.info("Первоначальный парсинг при старте...")
    try:
        raw_deals = await fetch_deals()
        total_found = len(raw_deals)
        await ai_analyzer.analyze_and_store(raw_deals, total_found)
    except ParseError as exc:
        logger.error("Первоначальный парсинг не удался: %s", exc)
    except Exception as exc:
        logger.error("Непредвиденная ошибка при первоначальном парсинге: %s", exc)


app = FastAPI(title="VseDeals API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/", include_in_schema=False)
async def serve_frontend():
    """Отдаёт Telegram Mini App HTML."""
    if not FRONTEND_PATH.exists():
        raise HTTPException(status_code=404, detail="Frontend не найден")
    return FileResponse(FRONTEND_PATH, media_type="text/html")


@app.get("/api/deals")
async def get_deals():
    """Возвращает последний результат парсинга + ИИ-анализа."""
    return JSONResponse(content=ai_analyzer.last_result)


@app.post("/api/refresh")
async def refresh_deals(x_secret_key: str = Header(default="")):
    """Запускает парсинг + ИИ-анализ. Требует заголовок X-Secret-Key."""
    if x_secret_key != REFRESH_SECRET:
        raise HTTPException(status_code=403, detail="Неверный секретный ключ")

    try:
        raw_deals = await fetch_deals()
        total_found = len(raw_deals)
        result = await ai_analyzer.analyze_and_store(raw_deals, total_found)
        return JSONResponse(content={"status": "ok", "total_found": total_found, "updated_at": result["updated_at"]})
    except ParseError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@app.get("/health")
async def health():
    """Проверка здоровья сервиса."""
    return {
        "status": "ok",
        "last_updated": ai_analyzer.last_result.get("updated_at"),
    }
