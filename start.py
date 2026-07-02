#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Універсальний запуск AI-агента однією командою (Windows / macOS / Linux).

    python start.py            # створити venv → встановити залежності → запустити сервер
    python start.py setup      # лише підготувати venv + залежності (без запуску)
    python start.py test       # прогнати pytest
    python start.py --port 9000   # інший порт

На Windows можна просто двічі клікнути run.bat (він викликає цей файл).

ВАЖЛИВО: проект лежить у папці з кириличною назвою («Новая папка»). Без UTF-8
SQLite не може відкрити data/history.db. Тому тут примусово вмикається PYTHONUTF8=1
(на Windows це критично — типове кодування там cp1251).
"""
import os
import sys
import subprocess
import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
REQ = ROOT / "requirements.txt"
DEPS_STAMP = VENV / ".deps.sha256"   # хеш requirements.txt, з яким востаннє ставили
HOST_DEFAULT = "127.0.0.1"
PORT_DEFAULT = "8000"
IS_WIN = os.name == "nt"


def venv_python() -> Path:
    return VENV / ("Scripts" if IS_WIN else "bin") / ("python.exe" if IS_WIN else "python")


def utf8_env() -> dict:
    """Середовище з примусовим UTF-8 — лікує баг із кириличним шляхом до SQLite."""
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("LANG", "en_US.UTF-8" if not IS_WIN else env.get("LANG", "C.UTF-8"))
    if not IS_WIN:
        env.setdefault("LC_ALL", "en_US.UTF-8")
    return env


def run(cmd, **kw):
    print("· " + " ".join(str(c) for c in cmd))
    subprocess.run(cmd, check=True, cwd=str(ROOT), env=utf8_env(), **kw)


def ensure_python_version():
    if sys.version_info < (3, 10):
        sys.exit(f"❌ Потрібен Python 3.10+, а зараз {sys.version.split()[0]}. "
                 f"Встановіть свіжий Python із python.org і запустіть знову.")


def req_hash() -> str:
    return hashlib.sha256(REQ.read_bytes()).hexdigest() if REQ.exists() else ""


def ensure_venv_and_deps():
    """Створити .venv (якщо нема) і поставити залежності (якщо змінилися)."""
    if not venv_python().exists():
        print("▶ Створюю віртуальне середовище .venv …")
        run([sys.executable, "-m", "venv", str(VENV)])

    current = req_hash()
    installed = DEPS_STAMP.read_text().strip() if DEPS_STAMP.exists() else ""
    if current and current == installed:
        print("✓ Залежності вже встановлені (requirements.txt не змінювався).")
        return

    print("▶ Встановлюю залежності з requirements.txt …")
    py = str(venv_python())
    run([py, "-m", "pip", "install", "--upgrade", "pip"])
    run([py, "-m", "pip", "install", "-r", str(REQ)])
    DEPS_STAMP.write_text(current)
    print("✓ Залежності готові.")


def ensure_env_file():
    """Перший запуск без .env → копіюємо приклад і попереджаємо заповнити ключі."""
    env_file, example = ROOT / ".env", ROOT / ".env.example"
    if not env_file.exists() and example.exists():
        env_file.write_bytes(example.read_bytes())
        print("⚠ Створив .env з .env.example — впишіть туди ключі (BAS, Telegram, DATABASE_URL) "
              "перед роботою з реальними даними.")


def ensure_database():
    """Create the PostgreSQL DB + apply schema if needed (no-op on USE_MOCK / no DB).
    Runs with the venv python so asyncpg/dotenv are available. Never fatal."""
    py = venv_python()
    if not py.exists():
        return
    print("▶ Перевіряю базу даних (PostgreSQL)…")
    try:
        run([str(py), "-m", "sync.bootstrap_db"])
    except subprocess.CalledProcessError:
        print("⚠ Налаштування БД не вдалося — сервер усе одно запуститься (дані можуть бути порожні).")


def _http_reachable(url: str, timeout: float = 2.0) -> bool:
    """True if anything answers at url — any HTTP status counts as 'server is up'."""
    import urllib.request
    import urllib.error
    try:
        urllib.request.urlopen(url, timeout=timeout)
        return True
    except urllib.error.HTTPError:
        return True  # got an HTTP response (e.g. 404) → the server is running
    except Exception:
        return False


def ensure_waha():
    """Auto-start a local WAHA server (WhatsApp gateway) in Docker so WhatsApp works
    out of the box — the operator only scans the QR in the dashboard. Non-fatal: if
    Docker is missing we explain how to get it and continue (WhatsApp just stays
    offline, everything else works). Disable with WAHA_AUTOSTART=false."""
    if os.getenv("WAHA_AUTOSTART", "true").lower() != "true":
        return
    waha_url = os.getenv("WAHA_URL", "http://localhost:3000").rstrip("/")
    if _http_reachable(waha_url + "/") or _http_reachable(waha_url + "/api/sessions"):
        print(f"✓ WAHA (WhatsApp) вже працює на {waha_url}")
        return

    from shutil import which
    if which("docker") is None:
        print("⚠ WhatsApp: Docker не знайдено — WAHA не запущено.")
        print("  Встанови Docker Desktop (https://www.docker.com/products/docker-desktop),")
        print("  тоді WhatsApp підніметься сам. Без нього все інше працює як завжди.")
        return

    name = os.getenv("WAHA_CONTAINER", "svy_waha")
    image = os.getenv("WAHA_IMAGE", "devlikeapro/waha")
    tail = waha_url.rsplit(":", 1)[-1]
    port = tail if tail.isdigit() else "3000"
    try:
        exists = subprocess.run(["docker", "ps", "-aq", "-f", f"name=^{name}$"],
                                capture_output=True, text=True).stdout.strip()
        if exists:
            print(f"▶ Запускаю наявний WAHA-контейнер «{name}»…")
            subprocess.run(["docker", "start", name], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            print(f"▶ Піднімаю WAHA (WhatsApp) у Docker: {image} на :{port} "
                  f"(перший раз тягне образ ~1–2 хв)…")
            subprocess.run(
                ["docker", "run", "-d", "--name", name, "--restart", "unless-stopped",
                 "--add-host", "host.docker.internal:host-gateway",
                 "-p", f"{port}:3000", image],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    except subprocess.CalledProcessError as e:
        print(f"⚠ Не вдалося запустити WAHA у Docker ({e}). WhatsApp буде офлайн.")
        return

    import time
    for _ in range(90):
        if _http_reachable(waha_url + "/") or _http_reachable(waha_url + "/api/sessions"):
            print(f"✓ WAHA піднявся на {waha_url}")
            return
        time.sleep(1)
    print(f"⚠ WAHA ще не відповів на {waha_url} (можливо, ще тягне образ). "
          f"Перевір: docker logs {name}")


def serve(host: str, port: str):
    ensure_python_version()
    ensure_venv_and_deps()
    ensure_env_file()
    ensure_database()
    ensure_waha()
    print(f"\n▶ Запуск сервера на http://{host}:{port}  (Ctrl+C — зупинити)")
    print(f"  Дашборд:  http://{host}:{port}/dashboard\n")
    # -m uvicorn працює однаково на всіх ОС (не залежить від шляху до консольного скрипта)
    run([str(venv_python()), "-m", "uvicorn", "main:app", "--host", host, "--port", port])


def main():
    args = sys.argv[1:]
    host, port = HOST_DEFAULT, PORT_DEFAULT
    cmd = "serve"
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("setup", "test", "serve", "run", "start"):
            cmd = "serve" if a in ("serve", "run", "start") else a
        elif a == "--port" and i + 1 < len(args):
            port = args[i + 1]; i += 1
        elif a == "--host" and i + 1 < len(args):
            host = args[i + 1]; i += 1
        elif a in ("-h", "--help"):
            print(__doc__); return
        else:
            sys.exit(f"Невідомий аргумент: {a}\n{__doc__}")
        i += 1

    if cmd == "setup":
        ensure_python_version(); ensure_venv_and_deps(); ensure_env_file(); ensure_database()
        print("✓ Готово. Запуск: python start.py")
    elif cmd == "test":
        ensure_python_version(); ensure_venv_and_deps()
        env = utf8_env(); env["PYTHONPATH"] = str(ROOT)
        subprocess.run([str(venv_python()), "-m", "pytest", "-q"],
                       cwd=str(ROOT), env=env)
    else:
        try:
            serve(host, port)
        except KeyboardInterrupt:
            print("\n■ Зупинено.")
        except subprocess.CalledProcessError as e:
            sys.exit(f"❌ Команда впала (код {e.returncode}). Дивіться вивід вище.")


if __name__ == "__main__":
    main()
