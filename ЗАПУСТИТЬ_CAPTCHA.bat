@echo off
chcp 65001 > nul
title Обновление сессии vseinstrumenti.ru
echo.
echo ======================================
echo  Открываем браузер для решения CAPTCHA
echo ======================================
echo.
echo Сейчас откроется Chrome с сайтом vseinstrumenti.ru
echo.
echo Что нужно сделать:
echo  1. Дождись загрузки CAPTCHA в браузере
echo  2. Перетащи слайдер чтобы картинка стала горизонтальной
echo  3. Браузер закроется сам, окно напишет "Готово!"
echo.
cd /d "%~dp0"
python renew_session.py
echo.
if exist session_state.json (
    echo [OK] Сессия сохранена! Можно запускать бота.
) else (
    echo [!] Сессия не сохранена. Попробуй ещё раз.
)
echo.
pause
