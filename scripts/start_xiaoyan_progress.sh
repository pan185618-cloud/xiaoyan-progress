#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

PORT="${XY_PROGRESS_PORT:-3460}"
HOST="127.0.0.1"
HEALTH_URL="http://${HOST}:${PORT}/api/health"

RUNTIME_DIR="$HOME/.config/xy_progress/runtime"
SERVER_PID_FILE="$RUNTIME_DIR/server.pid"
SERVER_LOG_FILE="$RUNTIME_DIR/server.log"
HUD_PID_FILE="$RUNTIME_DIR/hud.pid"

VENV_PYTHON="$PROJECT_ROOT/.venv/bin/python"
CLI_SCRIPT="$PROJECT_ROOT/cli.py"
APP_BUNDLE="$PROJECT_ROOT/macos/build/XiaoyanProgressHUD.app"
BUILD_SCRIPT="$PROJECT_ROOT/macos/build_app.sh"

mkdir -p "$RUNTIME_DIR"

is_healthy() {
  curl -fsS --max-time 2 "$HEALTH_URL" >/dev/null 2>&1
}

if is_healthy; then
  echo "[server] Backend already healthy at $HEALTH_URL"
else
  if [[ -f "$SERVER_PID_FILE" ]]; then
    OLD_PID="$(cat "$SERVER_PID_FILE" 2>/dev/null || true)"
    if [[ -n "$OLD_PID" ]] && kill -0 "$OLD_PID" 2>/dev/null; then
      echo "[server] Stopping unresponsive previous server (pid=$OLD_PID)..."
      kill "$OLD_PID" 2>/dev/null || true
      sleep 0.5
    fi
    rm -f "$SERVER_PID_FILE"
  fi

  if [[ ! -x "$VENV_PYTHON" ]]; then
    echo "[error] Missing isolated Python venv: $VENV_PYTHON" >&2
    exit 1
  fi
  if [[ ! -f "$CLI_SCRIPT" ]]; then
    echo "[error] Missing backend CLI entry: $CLI_SCRIPT" >&2
    exit 1
  fi

  echo "[server] Starting xiaoyan-progress backend on ${HOST}:${PORT}..."
  (
    cd "$PROJECT_ROOT"
    nohup "$VENV_PYTHON" "$CLI_SCRIPT" serve --host "$HOST" --port "$PORT" >> "$SERVER_LOG_FILE" 2>&1 </dev/null &
    echo $! > "$SERVER_PID_FILE"
  )

  SERVER_PID="$(cat "$SERVER_PID_FILE")"
  READY=0
  for _ in $(seq 1 50); do
    if is_healthy; then
      READY=1
      break
    fi
    if ! kill -0 "$SERVER_PID" 2>/dev/null; then
      echo "[error] Backend process (pid=$SERVER_PID) exited prematurely. Recent log:" >&2
      tail -n 30 "$SERVER_LOG_FILE" >&2 || true
      rm -f "$SERVER_PID_FILE"
      exit 1
    fi
    sleep 0.3
  done

  if [[ "$READY" -ne 1 ]]; then
    echo "[error] Timed out waiting for $HEALTH_URL. Recent log:" >&2
    tail -n 30 "$SERVER_LOG_FILE" >&2 || true
    exit 1
  fi
  echo "[server] Backend ready (pid=$SERVER_PID, log=$SERVER_LOG_FILE)"
fi

if [[ ! -d "$APP_BUNDLE" || ! -x "$APP_BUNDLE/Contents/MacOS/XiaoyanProgressHUD" ]]; then
  echo "[hud] App bundle not found; building XiaoyanProgressHUD.app..."
  bash "$BUILD_SCRIPT"
fi

if pgrep -x "XiaoyanProgressHUD" >/dev/null 2>&1; then
  HUD_PID="$(pgrep -x "XiaoyanProgressHUD" | head -n 1)"
  echo "$HUD_PID" > "$HUD_PID_FILE"
  echo "[hud] XiaoyanProgressHUD is already running (pid=$HUD_PID)"
else
  echo "[hud] Launching $APP_BUNDLE..."
  XY_PROGRESS_PORT="$PORT" open --env "XY_PROGRESS_PORT=$PORT" "$APP_BUNDLE"
  for _ in $(seq 1 20); do
    if pgrep -x "XiaoyanProgressHUD" >/dev/null 2>&1; then
      HUD_PID="$(pgrep -x "XiaoyanProgressHUD" | head -n 1)"
      echo "$HUD_PID" > "$HUD_PID_FILE"
      echo "[hud] Launched XiaoyanProgressHUD (pid=$HUD_PID)"
      break
    fi
    sleep 0.2
  done
fi

echo ""
echo "小研科研进度监控已启动："
echo "  宏观大盘: http://${HOST}:${PORT}/"
echo "  浮窗页面: http://${HOST}:${PORT}/hud"
echo "  服务日志: $SERVER_LOG_FILE"
