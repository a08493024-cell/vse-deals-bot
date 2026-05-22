"""
Ручное обновление сессии vseinstrumenti.ru.

Запуск:  python renew_session.py

Откроется браузер. Реши CAPTCHA (перетащи слайдер, чтобы картинка стала горизонтальной).
После редиректа на страницу акций браузер закроется, куки сохранятся на ~20 часов.
"""
import asyncio
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(__file__))

from playwright.async_api import async_playwright

SESSION_FILE = os.path.join(os.path.dirname(__file__), "backend", "..", "session_state.json")
SESSION_FILE = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "session_state.json")
)

SALES_URL = "https://www.vseinstrumenti.ru/sales/"

# Для браузера НЕ используем Telegram-прокси (PROXY_URL), только PARSER_PROXY_URL
PROXY_URL = os.getenv("PARSER_PROXY_URL", "")


async def main() -> None:
    print("=" * 60)
    print("  Обновление сессии vseinstrumenti.ru")
    print("=" * 60)
    print()
    print("Открывается браузер Chrome...")
    print("Реши CAPTCHA вручную:")
    print("  -> перетащи слайдер так, чтобы картинка стала горизонтальной")
    print()
    print("После успешного решения браузер закроется автоматически.")
    print()

    proxy_cfg = None
    if PROXY_URL:
        proxy_cfg = {"server": PROXY_URL}
        print(f"Используется прокси: {PROXY_URL}")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=False,
            proxy=proxy_cfg,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--start-maximized",
            ],
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="ru-RU",
            no_viewport=True,
        )
        await context.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        )

        page = await context.new_page()
        await page.goto(SALES_URL, wait_until="domcontentloaded", timeout=30000)

        print("Ожидание решения CAPTCHA (до 10 минут)...")
        print("ВАЖНО: найди окно Chrome на панели задач и реши CAPTCHA там!")

        # Ждём пока пользователь решит CAPTCHA и попадёт на /sales/
        deadline = asyncio.get_event_loop().time() + 600
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(1)
            url = page.url
            if "xpvnsulc" in url:
                continue
            if "vseinstrumenti.ru/sales" in url:
                try:
                    content = await page.content()
                except Exception:
                    continue
                if len(content) > 5000 and "servicepipe" not in content:
                    print()
                    print(f"Сессия получена! URL: {url[:60]}")
                    break
        else:
            print("Таймаут 10 минут — браузер закрывается без сохранения.")
            await browser.close()
            return

        # Сохраняем куки
        state = await context.storage_state()
        with open(SESSION_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f)

        cookies_count = len(state.get("cookies", []))
        print(f"Сохранено {cookies_count} куки в session_state.json")
        print("Парсер будет использовать эту сессию ~20 часов без CAPTCHA.")
        print()
        print("Готово! Теперь можно запускать бота.")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
