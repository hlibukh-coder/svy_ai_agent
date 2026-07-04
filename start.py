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
    """Перший запуск без .env → копіюємо приклад. Telegram app-креди вже в прикладі,
    тож канали підключаються одразу через QR; ключі BAS/OpenAI/DATABASE_URL — за потреби."""
    env_file, example = ROOT / ".env", ROOT / ".env.example"
    if not env_file.exists() and example.exists():
        env_file.write_bytes(example.read_bytes())
        print("✓ Створив .env з .env.example — Telegram/WhatsApp працюють одразу (тільки QR). "
              "Для AI-відповідей і даних БАС впишіть OPENAI_API_KEY / DATABASE_URL.")


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


def _docker_ready() -> bool:
    """docker CLI present AND the daemon actually responds."""
    from shutil import which
    if which("docker") is None:
        return False
    try:
        return subprocess.run(["docker", "info"], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=25).returncode == 0
    except Exception:
        return False


def _run_soft(cmd, timeout=None, shell=False) -> bool:
    """Run a command, print it, never raise. Returns True on exit code 0."""
    print("· " + (cmd if shell else " ".join(str(c) for c in cmd)))
    try:
        return subprocess.run(cmd, timeout=timeout, shell=shell).returncode == 0
    except Exception as e:
        print(f"  (не вдалося: {e})")
        return False


def ensure_docker() -> bool:
    """Make sure a Docker daemon is available; try to install/start it if not.
    Docker is required for WAHA (WhatsApp). Best-effort with honest fallbacks —
    Windows/macOS may need a one-time admin prompt, reboot, or first app launch
    that no script can skip. Disable with DOCKER_AUTOINSTALL=false."""
    if _docker_ready():
        return True
    if os.getenv("DOCKER_AUTOINSTALL", "true").lower() != "true":
        print("⚠ Docker недоступний, а DOCKER_AUTOINSTALL=false — пропускаю встановлення.")
        return False

    from shutil import which
    plat = sys.platform
    print("▶ Docker не знайдено або не запущено — пробую підняти автоматично…")

    if plat == "darwin":
        # Headless path: colima gives a docker daemon WITHOUT the Docker Desktop
        # GUI / admin rights. If colima+docker aren't there, install via Homebrew.
        if which("colima") is None or which("docker") is None:
            if which("brew"):
                _run_soft(["brew", "install", "colima", "docker"], timeout=1800)
            else:
                print("⚠ Немає Homebrew — не можу поставити Docker сам. Один рядок постав його:")
                print('  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"')
                print("  або Docker Desktop: https://www.docker.com/products/docker-desktop  — і запусти знову.")
                return False
        if which("colima"):
            _run_soft(["colima", "start"], timeout=600)

    elif plat.startswith("win"):
        if which("winget"):
            print("▶ Встановлюю Docker Desktop через winget (можливий запит прав адміністратора)…")
            _run_soft(["winget", "install", "-e", "--id", "Docker.DockerDesktop",
                       "--accept-package-agreements", "--accept-source-agreements"], timeout=2400)
            print("ℹ Docker Desktop встановлено. Часто потрібно ОДИН раз перезавантажити ПК і")
            print("  запустити Docker Desktop — після цього WhatsApp підніметься сам.")
        else:
            print("⚠ Немає winget — постав Docker Desktop вручну один раз:")
            print("  https://www.docker.com/products/docker-desktop  — і запусти знову.")
            return False

    else:  # Linux / other
        if which("curl"):
            is_root = getattr(os, "geteuid", lambda: 1)() == 0
            prefix = "" if is_root else ("sudo " if which("sudo") else "")
            _run_soft(f"curl -fsSL https://get.docker.com | {prefix}sh", timeout=1800, shell=True)
        else:
            print("⚠ Немає curl — постав Docker: https://docs.docker.com/engine/install/")
            return False

    # Give a freshly installed/launched daemon time to come up.
    import time
    for _ in range(60):
        if _docker_ready():
            print("✓ Docker готовий.")
            return True
        time.sleep(2)
    print("⚠ Docker ще не готовий. Якщо щойно встановився — запусти Docker Desktop "
          "(або виконай `colima start`) і запусти цей скрипт знову.")
    return False


def _read_env_file() -> dict:
    """Parse .env into a dict (start.py doesn't load dotenv itself)."""
    env_file = ROOT / ".env"
    out = {}
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    return out


def _waha_key(envf: dict) -> str:
    """Stable WAHA_API_KEY: the new WAHA image regenerates a key on every start
    unless pinned → the saved WhatsApp login would 'expire'. Read it from .env,
    else generate one and append so it persists across restarts (mirror run.sh)."""
    key = os.getenv("WAHA_API_KEY") or envf.get("WAHA_API_KEY", "")
    if not key:
        import secrets
        key = secrets.token_hex(16)
        env_file = ROOT / ".env"
        with env_file.open("a", encoding="utf-8") as fh:
            fh.write(f"\nWAHA_API_KEY={key}\n")
    return key


def _waha_platform_args() -> list:
    """Apple Silicon: devlikeapro/waha has no arm64 build → emulate amd64."""
    import platform
    if platform.machine().lower() in ("arm64", "aarch64") and sys.platform == "darwin":
        return ["--platform=linux/amd64"]
    return []


def _container_env_val(name: str, key: str) -> str:
    """Read one env var baked into an existing container (for drift detection)."""
    try:
        out = subprocess.run(
            ["docker", "inspect", "--format",
             "{{range .Config.Env}}{{println .}}{{end}}", name],
            capture_output=True, text=True).stdout
        for line in out.splitlines():
            if line.startswith(key + "="):
                return line.split("=", 1)[1]
    except Exception:
        pass
    return ""


def ensure_waha():
    """Auto-start a local WAHA server (WhatsApp gateway) in Docker so WhatsApp works
    out of the box — the operator only scans the QR in the dashboard. Non-fatal: if
    Docker can't be brought up we continue (WhatsApp stays offline, everything else
    works). Disable with WAHA_AUTOSTART=false.

    Mirrors run.sh: NOWEB engine (WEBJS/Chromium crash-loops under amd64 emulation),
    pinned WAHA_API_KEY, session volume, and container recreate on engine/key drift."""
    if os.getenv("WAHA_AUTOSTART", "true").lower() != "true":
        return
    envf = _read_env_file()
    waha_url = (os.getenv("WAHA_URL") or envf.get("WAHA_URL")
                or "http://localhost:3000").rstrip("/")
    if _http_reachable(waha_url + "/") or _http_reachable(waha_url + "/api/sessions"):
        print(f"✓ WAHA (WhatsApp) вже працює на {waha_url}")
        return

    if not ensure_docker():
        print("⚠ WhatsApp офлайн — Docker недоступний. Усе інше працює як завжди.")
        return

    name = os.getenv("WAHA_CONTAINER", "svy_waha")
    image = os.getenv("WAHA_IMAGE", "devlikeapro/waha")
    engine = (os.getenv("WHATSAPP_DEFAULT_ENGINE")
              or envf.get("WHATSAPP_DEFAULT_ENGINE") or "NOWEB")
    key = _waha_key(envf)
    plat = _waha_platform_args()
    tail = waha_url.rsplit(":", 1)[-1]
    port = tail if tail.isdigit() else "3000"
    try:
        exists = subprocess.run(["docker", "ps", "-aq", "-f", f"name=^{name}$"],
                                capture_output=True, text=True).stdout.strip()
        # Recreate a stale container whose engine/key differs (e.g. an old WEBJS one
        # that crash-loops on Apple Silicon). Session lives in the svy_waha_data volume.
        if exists:
            drift = (_container_env_val(name, "WHATSAPP_DEFAULT_ENGINE") != engine
                     or _container_env_val(name, "WAHA_API_KEY") != key)
            if drift:
                print("▶ Перестворюю WAHA-контейнер (двигун/ключ змінились)…")
                subprocess.run(["docker", "rm", "-f", name],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                exists = ""
        if exists:
            print(f"▶ Запускаю наявний WAHA-контейнер «{name}»…")
            subprocess.run(["docker", "start", name], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            print(f"▶ Піднімаю WAHA (WhatsApp) у Docker: {image}, двигун {engine}"
                  f"{' ' + plat[0] if plat else ''} на :{port} "
                  f"(перший раз тягне образ ~1–2 хв)…")
            subprocess.run(
                ["docker", "run", "-d", "--name", name, "--restart", "unless-stopped",
                 *plat,
                 "-e", f"WAHA_API_KEY={key}",
                 "-e", f"WHATSAPP_DEFAULT_ENGINE={engine}",
                 "-v", "svy_waha_data:/app/.sessions",
                 "--add-host", "host.docker.internal:host-gateway",
                 "-p", f"{port}:3000", image],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    except subprocess.CalledProcessError as e:
        print(f"⚠ Не вдалося запустити WAHA у Docker ({e}). WhatsApp буде офлайн.")
        return

    import time
    for _ in range(90):
        if _http_reachable(waha_url + "/") or _http_reachable(waha_url + "/api/sessions"):
            print(f"✓ WAHA піднявся на {waha_url} (QR у дашборді → Налаштування)")
            return
        time.sleep(1)
    print(f"⚠ WAHA ще не відповів на {waha_url} (можливо, ще тягне образ під емуляцією). "
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
