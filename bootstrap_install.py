"""Установка пакетов через зеркало Tsinghua (обходит блокировку feodorn.com)."""
import json
import os
import re
import socket
import ssl
import struct
import subprocess
import sys
import tempfile

SOCKS5_HOST = "127.0.0.1"
SOCKS5_PORT = 10808
MIRROR_HOST = "pypi.tuna.tsinghua.edu.cn"

PACKAGES = [
    "fastapi==0.111.0",
    "uvicorn==0.29.0",
    "aiogram==3.7.0",
    "apscheduler==3.10.4",
    "python-dotenv==1.0.1",
    "PySocks==1.7.1",
    "aiohttp==3.9.5",
    "aiofiles==23.2.1",
]


def socks5_get(host: str, path: str, port: int = 443) -> bytes:
    s = socket.create_connection((SOCKS5_HOST, SOCKS5_PORT), timeout=30)
    s.sendall(b"\x05\x01\x00")
    s.recv(2)
    hb = host.encode()
    s.sendall(b"\x05\x01\x00\x03" + bytes([len(hb)]) + hb + struct.pack("!H", port))
    s.recv(10)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    t = ctx.wrap_socket(s, server_hostname=host)
    t.sendall(f"GET {path} HTTP/1.0\r\nHost: {host}\r\nUser-Agent: pip/25\r\n\r\n".encode())
    data = b""
    while True:
        chunk = t.recv(65536)
        if not chunk:
            break
        data += chunk
    t.close()
    sep = data.find(b"\r\n\r\n")
    return data[sep + 4:] if sep != -1 else data


def get_wheel_path(package: str) -> str | None:
    """Ищет wheel в индексе Tsinghua и скачивает его."""
    if "==" in package:
        name, version = package.split("==", 1)
    else:
        name, version = package, None

    norm_name = name.lower().replace("-", "_").replace(".", "_")
    index_name = name.lower().replace("_", "-")

    print(f"  Запрос индекса: {index_name}...")
    index_body = socks5_get(MIRROR_HOST, f"/simple/{index_name}/").decode(errors="replace")

    # Выбираем нужный wheel: py3-none-any или соответствующий cp3xx-win
    py_tag = f"cp{sys.version_info.major}{sys.version_info.minor}"
    win_tag = "win_amd64"

    all_urls = re.findall(r'href="([^"]+\.whl[^"]*)"', index_body)

    def score(url: str) -> int:
        fn = url.split("/")[-1].split("#")[0].lower()
        if version and f"-{version}-" not in fn:
            return -1
        if "none-any" in fn:
            return 3
        if py_tag in fn and win_tag in fn:
            return 2
        if "cp3" in fn and win_tag in fn:
            return 1
        return 0

    ranked = sorted(all_urls, key=score, reverse=True)
    if not ranked or score(ranked[0]) < 0:
        print(f"  Wheel не найден для {name}=={version}")
        return None

    best_url = ranked[0]
    # Конвертируем относительный URL в путь на зеркале
    if best_url.startswith("../../"):
        path = best_url[5:].split("#")[0]
    elif best_url.startswith("/"):
        path = best_url.split("#")[0]
    else:
        path = "/" + best_url.split("#")[0]

    filename = path.split("/")[-1]
    print(f"  Скачиваю: {filename}")
    content = socks5_get(MIRROR_HOST, path)

    if not content.startswith(b"PK"):
        print(f"  Получен некорректный файл ({len(content)} байт): {content[:80]}")
        return None

    tmp_dir = tempfile.mkdtemp()
    dest = os.path.join(tmp_dir, filename)
    with open(dest, "wb") as f:
        f.write(content)
    print(f"  OK ({len(content) // 1024} KB)")
    return dest


def main():
    print("=" * 55)
    print("Установка через зеркало Tsinghua (обход feodorn.com)")
    print("=" * 55)

    wheels = []
    failed = []

    for i, pkg in enumerate(PACKAGES, 1):
        print(f"\n[{i}/{len(PACKAGES)}] {pkg}")
        try:
            path = get_wheel_path(pkg)
            if path:
                wheels.append(path)
            else:
                failed.append(pkg)
        except Exception as e:
            print(f"  Ошибка: {e}")
            failed.append(pkg)

    if wheels:
        print(f"\nУстанавливаю {len(wheels)} пакетов...")
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--no-index", "--no-deps", "--no-warn-script-location"] + wheels,
        )
        if result.returncode == 0:
            print("Базовые пакеты установлены. Доставляю зависимости...")
            subprocess.run(
                [sys.executable, "-m", "pip", "install",
                 "--index-url", f"https://{MIRROR_HOST}/simple/",
                 "--trusted-host", MIRROR_HOST,
                 "--no-warn-script-location",
                 "-r", "requirements.txt"]
            )

    if failed:
        print(f"\nНе удалось: {failed}")
    else:
        print("\nГотово!")


if __name__ == "__main__":
    main()
