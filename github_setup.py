"""
Скрипт для создания GitHub-репозитория и первого пуша.

Использование:
    python github_setup.py --token <GITHUB_TOKEN> --repo vse-deals-bot

Требует: pip install requests
"""
import argparse
import json
import os
import subprocess
import sys

import requests


def run(cmd: str, check: bool = True) -> subprocess.CompletedProcess:
    """Выполняет shell-команду и выводит её в консоль."""
    print(f"  $ {cmd}")
    result = subprocess.run(cmd, shell=True, capture_output=False, text=True)
    if check and result.returncode != 0:
        print(f"[ОШИБКА] Команда завершилась с кодом {result.returncode}")
        sys.exit(result.returncode)
    return result


def create_github_repo(token: str, repo_name: str, private: bool, description: str) -> str:
    """Создаёт репозиторий на GitHub и возвращает SSH/HTTPS URL."""
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
    }
    payload = {
        "name": repo_name,
        "description": description,
        "private": private,
        "auto_init": False,
    }
    resp = requests.post("https://api.github.com/user/repos", headers=headers, json=payload, timeout=15)
    if resp.status_code == 422:
        data = resp.json()
        errors = data.get("errors", [])
        if any(e.get("message") == "name already exists on this account" for e in errors):
            print(f"[INFO] Репозиторий '{repo_name}' уже существует, получаем URL...")
            repo_resp = requests.get(f"https://api.github.com/repos/{_get_login(token)}/{repo_name}",
                                     headers=headers, timeout=15)
            repo_resp.raise_for_status()
            return repo_resp.json()["clone_url"]
    resp.raise_for_status()
    clone_url: str = resp.json()["clone_url"]
    print(f"[OK] Репозиторий создан: {clone_url}")
    return clone_url


def _get_login(token: str) -> str:
    resp = requests.get(
        "https://api.github.com/user",
        headers={"Authorization": f"token {token}", "Accept": "application/vnd.github+json"},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["login"]


def embed_token_in_url(clone_url: str, token: str) -> str:
    """Встраивает токен в HTTPS-URL для пуша без отдельной аутентификации."""
    return clone_url.replace("https://", f"https://{token}@")


def main() -> None:
    parser = argparse.ArgumentParser(description="Создать GitHub-репо и сделать первый пуш")
    parser.add_argument("--token", required=True, help="GitHub Personal Access Token (scope: repo)")
    parser.add_argument("--repo", default="vse-deals-bot", help="Имя репозитория (по умолчанию: vse-deals-bot)")
    parser.add_argument("--private", action="store_true", default=False, help="Сделать репозиторий приватным")
    parser.add_argument(
        "--description",
        default="Telegram-бот + веб-приложение для анализа акций ВсеИнструменты с ИИ на Gemini",
    )
    args = parser.parse_args()

    project_root = os.path.dirname(os.path.abspath(__file__))
    print(f"\n[1/5] Корень проекта: {project_root}")
    os.chdir(project_root)

    print("\n[2/5] Создание репозитория на GitHub...")
    clone_url = create_github_repo(args.token, args.repo, args.private, args.description)
    remote_url = embed_token_in_url(clone_url, args.token)

    print("\n[3/5] Инициализация git...")
    if not os.path.exists(".git"):
        run("git init")
        run('git config user.email "bot@vseinstrumenti.local"')
        run('git config user.name "VseDeals Bot"')
    else:
        print("  .git уже существует, пропускаем git init")

    print("\n[4/5] Первый коммит...")
    run("git add .")
    result = run('git commit -m "feat: initial project setup"', check=False)
    if result.returncode != 0:
        print("  [INFO] Коммит не создан (возможно, нечего коммитить или уже есть)")

    print("\n[5/5] Пуш в GitHub...")
    # Устанавливаем remote (перезаписываем если уже есть)
    run("git remote remove origin", check=False)
    run(f'git remote add origin "{remote_url}"')
    run("git branch -M main")
    run("git push -u origin main")

    print(f"\n✅ Готово! Репозиторий: {clone_url}")
    print("Следующий шаг: зайдите на render.com и подключите этот репозиторий.")


if __name__ == "__main__":
    main()
