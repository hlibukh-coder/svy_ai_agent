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

# PID процесу, що слухає порт (через lsof) — джерело істини, надійніше за pidfile.
port_pid() { lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | head -1 || true; }

is_running() { [ -n "$(port_pid)" ]; }

start() {
  if is_running; then
    echo "✓ Сервер уже працює (PID $(port_pid)) на ${BASE}"
    return 0
  fi
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
