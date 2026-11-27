#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LABEL="com.xiaoyan.progress.hud"
PLIST_PATH="$HOME/Library/LaunchAgents/${LABEL}.plist"
GUI_DOMAIN="gui/$(id -u)"

echo "[autolink] Unloading LaunchAgent (${LABEL})..."
launchctl bootout "${GUI_DOMAIN}/${LABEL}" 2>/dev/null || \
  launchctl bootout "${GUI_DOMAIN}" "$PLIST_PATH" 2>/dev/null || true

if [[ -f "$PLIST_PATH" ]]; then
  rm -f "$PLIST_PATH"
  echo "[autolink] Removed $PLIST_PATH"
else
  echo "[autolink] Plist already absent: $PLIST_PATH"
fi

if [[ -x "$SCRIPT_DIR/stop_xiaoyan_progress.sh" ]]; then
  bash "$SCRIPT_DIR/stop_xiaoyan_progress.sh"
fi

echo "[done] ChatGPT 联动自启已卸载并停止相关进程。"
