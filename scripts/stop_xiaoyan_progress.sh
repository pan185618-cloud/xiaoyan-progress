#!/usr/bin/env bash
set -euo pipefail

RUNTIME_DIR="$HOME/.config/xy_progress/runtime"
SERVER_PID_FILE="$RUNTIME_DIR/server.pid"
HUD_PID_FILE="$RUNTIME_DIR/hud.pid"

stop_pid_gracefully() {
  local pid="$1"
  local label="$2"
  if [[ -z "$pid" ]] || ! kill -0 "$pid" 2>/dev/null; then
    return 0
  fi
  echo "[stop] Sending SIGTERM to $label (pid=$pid)..."
  kill -TERM "$pid" 2>/dev/null || true
  for _ in $(seq 1 25); do
    if ! kill -0 "$pid" 2>/dev/null; then
      echo "[stop] $label (pid=$pid) stopped."
      return 0
    fi
    sleep 0.2
  done
  echo "[stop] $label (pid=$pid) still alive; sending SIGKILL..."
  kill -KILL "$pid" 2>/dev/null || true
}

# 1. Stop XiaoyanProgressHUD macOS app
HUD_STOPPED=0
if [[ -f "$HUD_PID_FILE" ]]; then
  HUD_PID="$(cat "$HUD_PID_FILE" 2>/dev/null || true)"
  if [[ -n "$HUD_PID" ]] && kill -0 "$HUD_PID" 2>/dev/null; then
    CMDLINE="$(ps -p "$HUD_PID" -o comm= 2>/dev/null || true)"
    if [[ "$CMDLINE" == *"XiaoyanProgressHUD"* ]]; then
      stop_pid_gracefully "$HUD_PID" "XiaoyanProgressHUD"
      HUD_STOPPED=1
    fi
  fi
  rm -f "$HUD_PID_FILE"
fi

if pgrep -x "XiaoyanProgressHUD" >/dev/null 2>&1; then
  while IFS= read -r pid; do
    [[ -n "$pid" ]] && stop_pid_gracefully "$pid" "XiaoyanProgressHUD"
    HUD_STOPPED=1
  done < <(pgrep -x "XiaoyanProgressHUD" || true)
fi

if [[ "$HUD_STOPPED" -eq 0 ]]; then
  echo "[stop] XiaoyanProgressHUD was not running."
fi

# 2. Stop backend server by PID files (verify command matches cli.py serve)
SERVER_STOPPED=0
for pid_file in "$SERVER_PID_FILE" "$RUNTIME_DIR/backend.pid"; do
  if [[ -f "$pid_file" ]]; then
    SERVER_PID="$(cat "$pid_file" 2>/dev/null || true)"
    if [[ -n "$SERVER_PID" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
      CMDLINE="$(ps -p "$SERVER_PID" -o args= 2>/dev/null || true)"
      if [[ "$CMDLINE" == *"cli.py"* && "$CMDLINE" == *"serve"* ]]; then
        stop_pid_gracefully "$SERVER_PID" "xiaoyan-progress server"
        SERVER_STOPPED=1
      else
        echo "[warn] PID $SERVER_PID in $pid_file does not match xiaoyan-progress server ('$CMDLINE'); skipping kill." >&2
      fi
    fi
    rm -f "$pid_file"
  fi
done

if [[ "$SERVER_STOPPED" -eq 0 ]]; then
  echo "[stop] Backend server was not running (or no matching PID file)."
fi
