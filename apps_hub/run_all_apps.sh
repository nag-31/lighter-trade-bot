#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="$ROOT/data/app_logs"
PYTHON_BIN="${PYTHON_BIN:-python}"
mkdir -p "$LOG_DIR"

pids=()

port_ready() {
  (echo >"/dev/tcp/127.0.0.1/$1") >/dev/null 2>&1
}

start_app() {
  local name="$1"
  local workdir="$2"
  local port="$3"
  shift 3
  if port_ready "$port"; then
    echo "Ready: $name on port $port"
    return
  fi
  local log="$LOG_DIR/$name.log"
  echo "Starting $name ..."
  ( cd "$workdir"; "$@" >"$log" 2>&1 ) &
  pids+=("$!")
  echo "  pid $!, log $log"
}

stop_all() {
  echo
  echo "Stopping apps started by this launcher ..."
  for pid in "${pids[@]:-}"; do
    kill "$pid" 2>/dev/null || true
  done
}

trap stop_all EXIT INT TERM

start_app "portfolio" "$ROOT" 8790 "$PYTHON_BIN" -B -m src.portfolio_app --host 127.0.0.1 --port 8790
start_app "pnl_analytics" "$ROOT" 8787 "$PYTHON_BIN" -B -m standalone.pnl_analytics_bot.dashboard.server --host 127.0.0.1 --port 8787
start_app "apps_hub" "$ROOT" 8800 "$PYTHON_BIN" -B -m apps_hub.access_page --host 127.0.0.1 --port 8800
start_app "trade_tracker" "$ROOT" 8080 "$PYTHON_BIN" -B -m src.dashboard

if [ -f "$ROOT/bots/full_fledged_bot/full_fledged_bot/cli.py" ]; then
  start_app "full_fledged_bot" "$ROOT/bots/full_fledged_bot" 18080 "$PYTHON_BIN" -B -m full_fledged_bot.cli --config config.example.yaml serve
fi

echo
echo "Lighter Apps Hub: http://127.0.0.1:8800/"
echo "Logs: $LOG_DIR"
echo
echo "Press Ctrl+C to stop apps started by this launcher."

while true; do
  sleep 3600 &
  wait $! || true
done
