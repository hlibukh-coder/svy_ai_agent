#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# run.sh — керування AI-агентом (FastAPI + дашборд + Telegram) через bash.
#
#   ./run.sh start     запустити сервер у фоні
#   ./run.sh stop      зупинити сервер
#   ./run.sh restart   перезапустити
#   ./run.sh status    перевірити, що всі ендпоінти дашборда відповідають
#   ./run.sh logs      показати останні рядки логу (live: ./run.sh logs -f)
#   ./run.sh test      прогнати pytest
#
# ВАЖЛИВО: примусовий UTF-8 (PYTHONUTF8/LC_*) — папка проекту має кириличну
# назву («Новая папка»), і без UTF-8 SQLite не може відкрити data/history.db
# («unable to open database file»). Саме через це падали /api/chats та статистика.
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

# Прив'язка до власної папки → не залежить від поточного каталогу запуску.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ── Примусовий UTF-8 (лікує баг із кириличним шляхом) ──────────────────────────
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8
export LANG="${LANG:-en_US.UTF-8}"
export LC_ALL="${LC_ALL:-en_US.UTF-8}"

HOST="127.0.0.1"
PORT="8000"
PY=".venv/bin/python"
UVICORN=".venv/bin/uvicorn"
PIDFILE=".server.pid"
LOGFILE="server.log"
BASE="http://${HOST}:${PORT}"

[ -x "$PY" ] || { echo "❌ Немає $PY — створіть venv: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"; exit 1; }

# Перший запуск без .env → створюємо з .env.example (робочі Telegram app-креди вже
# всередині — лишається тільки QR; OPENAI_API_KEY/DATABASE_URL за потреби впишіть потім).
[ -f .env ] || { cp .env.example .env; echo "✓ Створив .env з .env.example (Telegram/WhatsApp працюють одразу — тільки QR)"; }

# PID процесу, що слухає порт (через lsof) — джерело істини, надійніше за pidfile.
port_pid() { lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | head -1 || true; }

is_running() { [ -n "$(port_pid)" ]; }

# ── WAHA (WhatsApp gateway) auto-start у Docker — щоб WhatsApp працював "з коробки".
# Оператору лишається тільки відсканувати QR у дашборді. Вимкнути: WAHA_AUTOSTART=false.
# WAHA відповідає (навіть 401 без ключа) = сервер піднявся. -f падає на 401, тож
# перевіряємо код: будь-яка HTTP-відповідь (не 000) означає, що WAHA слухає порт.
waha_up() {
  local code; code="$(curl -s -o /dev/null -w '%{http_code}' -m 2 "${1}/" 2>/dev/null || echo 000)"
  [ "$code" != "000" ]
}

# Стабільний WAHA_API_KEY: новий образ WAHA генерує новий ключ на кожен старт, якщо
# його не задати → збережена в акаунті авторизація «протухає». Тримаємо ключ у .env.
ensure_waha_key() {
  local key; key="$(grep -E '^WAHA_API_KEY=' .env 2>/dev/null | head -1 | cut -d= -f2-)"
  if [ -z "$key" ]; then
    key="$(LC_ALL=C tr -dc 'a-f0-9' </dev/urandom 2>/dev/null | head -c 32)"
    [ -z "$key" ] && key="$("$PY" -c 'import secrets;print(secrets.token_hex(16))' 2>/dev/null)"
    # drop any empty WAHA_API_KEY= line, then append the generated one
    grep -vE '^WAHA_API_KEY=$' .env > .env.tmp 2>/dev/null && mv .env.tmp .env
    printf 'WAHA_API_KEY=%s\n' "$key" >> .env
  fi
  echo "$key"
}

# Apple Silicon: образ devlikeapro/waha не має arm64-збірки → потрібна емуляція amd64.
waha_platform() {
  case "$(uname -m)" in arm64|aarch64) echo "--platform=linux/amd64" ;; *) echo "" ;; esac
}

# Docker daemon present AND responding.
docker_ready() { command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; }

# Try to install/start Docker automatically (best-effort). macOS → colima via brew
# (headless, no admin/GUI); Linux → official get.docker.com. Disable: DOCKER_AUTOINSTALL=false.
ensure_docker() {
  docker_ready && return 0
  [ "${DOCKER_AUTOINSTALL:-true}" = "true" ] || { echo "⚠ Docker недоступний (DOCKER_AUTOINSTALL=false)."; return 1; }
  echo "▶ Docker не знайдено/не запущено — пробую підняти автоматично…"
  if [ "$(uname -s)" = "Darwin" ]; then
    if ! command -v colima >/dev/null 2>&1 || ! command -v docker >/dev/null 2>&1; then
      if command -v brew >/dev/null 2>&1; then brew install colima docker || true
      else echo "⚠ Немає Homebrew — постав Docker Desktop: https://www.docker.com/products/docker-desktop"; return 1; fi
    fi
    command -v colima >/dev/null 2>&1 && { colima status >/dev/null 2>&1 || colima start || true; }
  else
    if command -v curl >/dev/null 2>&1; then
      if [ "$(id -u)" = "0" ]; then curl -fsSL https://get.docker.com | sh || true
      elif command -v sudo >/dev/null 2>&1; then curl -fsSL https://get.docker.com | sudo sh || true
      else echo "⚠ Постав Docker: https://docs.docker.com/engine/install/"; return 1; fi
    else echo "⚠ Постав Docker: https://docs.docker.com/engine/install/"; return 1; fi
  fi
  for _ in $(seq 1 60); do docker_ready && { echo "✓ Docker готовий."; return 0; }; sleep 2; done
  echo "⚠ Docker ще не готовий — запусти Docker Desktop / \`colima start\` і повтори."
  return 1
}

ensure_waha() {
  [ "${WAHA_AUTOSTART:-true}" = "true" ] || return 0
  local url="${WAHA_URL:-http://localhost:3000}"; url="${url%/}"
  local name="${WAHA_CONTAINER:-svy_waha}" image="${WAHA_IMAGE:-devlikeapro/waha}"
  local engine="${WHATSAPP_DEFAULT_ENGINE:-NOWEB}"
  local key; key="$(ensure_waha_key)"
  local plat; plat="$(waha_platform)"
  local port="${url##*:}"; [[ "$port" =~ ^[0-9]+$ ]] || port=3000

  if ! ensure_docker; then
    if waha_up "$url"; then echo "✓ WAHA вже працює на ${url}"; return 0; fi
    echo "⚠ WhatsApp офлайн — Docker недоступний. Усе інше працює."
    return 0
  fi

  # Пересоздаємо контейнер, якщо його двигун/ключ/платформа не збігаються з бажаними
  # (напр. старий WEBJS-контейнер, що падав на Apple Silicon). Сесія WhatsApp живе у
  # томі svy_waha_data → перескан QR потрібен лише коли дійсно змінюємо налаштування.
  local cid; cid="$(docker ps -aq -f name=^${name}$ 2>/dev/null)"
  if [ -n "$cid" ]; then
    local cur_engine cur_key
    cur_engine="$(docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$name" 2>/dev/null | grep -E '^WHATSAPP_DEFAULT_ENGINE=' | cut -d= -f2-)"
    cur_key="$(docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$name" 2>/dev/null | grep -E '^WAHA_API_KEY=' | cut -d= -f2-)"
    if [ "$cur_engine" != "$engine" ] || [ "$cur_key" != "$key" ]; then
      echo "▶ Перестворюю WAHA-контейнер (двигун/ключ змінились: ${cur_engine:-?}→${engine})…"
      docker rm -f "$name" >/dev/null 2>&1 || true
      cid=""
    fi
  fi

  if [ -n "$cid" ]; then
    echo "▶ Запускаю наявний WAHA-контейнер «${name}»…"; docker start "$name" >/dev/null 2>&1 || true
  else
    echo "▶ Піднімаю WAHA у Docker (${image}, двигун ${engine}${plat:+, ${plat}}) на :${port} (перший раз тягне образ ~1–2 хв)…"
    docker run -d --name "$name" --restart unless-stopped ${plat} \
      -e WAHA_API_KEY="$key" -e WHATSAPP_DEFAULT_ENGINE="$engine" \
      -v svy_waha_data:/app/.sessions \
      --add-host host.docker.internal:host-gateway \
      -p "${port}:3000" "$image" >/dev/null 2>&1 || echo "⚠ Не вдалося запустити WAHA (docker logs ${name})."
  fi
  for _ in $(seq 1 90); do
    if waha_up "$url"; then echo "✓ WAHA піднявся на ${url} (QR у дашборді → Налаштування)"; return 0; fi
    sleep 1
  done
  echo "⚠ WAHA ще не відповів (можливо, тягне образ під емуляцією): docker logs ${name}"
}

start() {
  if is_running; then
    echo "✓ Сервер уже працює (PID $(port_pid)) на ${BASE}"
    return 0
  fi
  ensure_waha
  echo "▶ Запуск сервера на ${BASE} (PYTHONUTF8=1)…"
  nohup "$UVICORN" main:app --host "$HOST" --port "$PORT" >> "$LOGFILE" 2>&1 &
  echo $! > "$PIDFILE"
  disown || true
  # Чекаємо, поки підніметься (до ~20 с).
  for _ in $(seq 1 40); do
    if curl -fsS -m 2 "${BASE}/" >/dev/null 2>&1; then
      echo "✓ Готово. Дашборд: ${BASE}/dashboard"
      return 0
    fi
    sleep 0.5
  done
  echo "❌ Не піднявся за 20 с — дивіться: ./run.sh logs"
  return 1
}

stop() {
  local pid; pid="$(port_pid)"
  if [ -z "$pid" ]; then echo "• Сервер не запущено"; rm -f "$PIDFILE"; return 0; fi
  echo "■ Зупинка PID ${pid} …"
  kill "$pid" 2>/dev/null || true
  for _ in $(seq 1 20); do is_running || break; sleep 0.3; done
  if is_running; then echo "  …форс kill -9"; kill -9 "$(port_pid)" 2>/dev/null || true; fi
  rm -f "$PIDFILE"
  echo "✓ Зупинено"
}

# Перевірка кожного ендпоінта дашборда: HTTP-код + чи не порожня відповідь.
check() {
  local path="$1" name="$2"
  local code; code="$(curl -s -o /tmp/_svy_body -w '%{http_code}' -m 15 "${BASE}${path}" 2>/dev/null || echo 000)"
  local head; head="$(head -c 90 /tmp/_svy_body | LC_ALL=C tr '\n\r' '  ')"
  if [ "$code" = "200" ]; then
    printf '  ✓ %-22s [200] %s\n' "$name" "$head"
  else
    printf '  ✗ %-22s [%s] %s\n' "$name" "$code" "$head"
    return 1
  fi
}

status() {
  if ! is_running; then echo "• Сервер не запущено. ./run.sh start"; return 1; fi
  echo "Сервер: PID $(port_pid) · ${BASE}"
  echo "Перевірка ендпоінтів адмінки:"
  local ok=0
  check "/"                      "root"            || ok=1
  check "/api/agent/state"       "agent/state"     || ok=1
  check "/api/telegram/status"   "telegram/status" || ok=1
  check "/api/config"            "config"          || ok=1
  check "/api/stats/overview"    "stats/overview"  || ok=1
  check "/api/stats/active-dialogs" "active-dialogs" || ok=1
  check "/api/stats/opportunities"  "opportunities"  || ok=1
  check "/api/stats/channels"    "stats/channels"  || ok=1
  check "/api/stats/recent-actions" "recent-actions" || ok=1
  check "/api/chats"             "chats (діалоги)" || ok=1
  check "/api/clients?limit=1"   "clients"         || ok=1
  check "/api/campaigns/preview?kind=reorder" "campaign/preview" || ok=1
  echo
  # Підказка щодо Telegram (чат працює лише після підключення).
  if curl -s -m 10 "${BASE}/api/telegram/status" | grep -q '"authorized"'; then
    echo "Telegram: ✅ підключено — приймання/відправлення в «Діалогах» працює"
  else
    echo "Telegram: ⚪ не підключено → Налаштування → «Підключити через QR»"
  fi
  [ "$ok" = 0 ] && echo "РЕЗУЛЬТАТ: усе ОК ✅" || echo "РЕЗУЛЬТАТ: є проблеми ❌ (див. вище)"
  return "$ok"
}

logs() { tail "${1:--n}" "${2:-80}" "$LOGFILE"; }

case "${1:-}" in
  start)   start ;;
  stop)    stop ;;
  restart) stop; sleep 1; start ;;
  status)  status ;;
  logs)    shift; tail "${@:--n 80}" "$LOGFILE" ;;
  test)    PYTHONPATH="$SCRIPT_DIR" "$PY" -m pytest -q ;;
  *) echo "Використання: ./run.sh {start|stop|restart|status|logs|test}"; exit 1 ;;
esac
