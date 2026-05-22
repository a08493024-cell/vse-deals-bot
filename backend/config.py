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

# Прокси для Telegram бота (нужен локально если Telegram заблокирован)
# Пример: socks5://127.0.0.1:10808
PROXY_URL: str = os.getenv("PROXY_URL", "")

# Прокси для парсера vseinstrumenti.ru (локально пусто, на Railway при блокировке)
PARSER_PROXY_URL: str = os.getenv("PARSER_PROXY_URL", "")

# Ключ 2captcha для автоматического решения CAPTCHA
# Получить: 2captcha.com (от $0.001 за решение)
TWOCAPTCHA_API_KEY: str = os.getenv("TWOCAPTCHA_API_KEY", "")
