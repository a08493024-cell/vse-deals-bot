"""APScheduler — ежедневная рассылка в 10:00 МСК."""
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from .bot import run_parse_and_notify

logger = logging.getLogger(__name__)


def create_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone="Europe/Moscow")

    scheduler.add_job(
        run_parse_and_notify,
        trigger=CronTrigger(hour=10, minute=0, timezone="Europe/Moscow"),
        id="daily_deals",
        name="Ежедневная рассылка топ-3 акций",
        replace_existing=True,
        misfire_grace_time=300,  # допустимое опоздание 5 минут
    )

    logger.info("Планировщик создан: ежедневно в 10:00 МСК")
    return scheduler
