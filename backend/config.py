"""Загрузка конфигурации из переменных окружения."""
import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
REFRESH_SECRET: str = os.getenv("REFRESH_SECRET", "secret")
APP_URL: str = os.getenv("APP_URL", "http://localhost:8000")

# Белый список Telegram user_id через запятую: "123456789,987654321"
_raw_allowed = os.getenv("ALLOWED_USERS", "")
ALLOWED_USERS: set[int] = {
    int(uid.strip()) for uid in _raw_allowed.split(",") if uid.strip().isdigit()
}
