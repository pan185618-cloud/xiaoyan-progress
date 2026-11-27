#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

LABEL="com.xiaoyan.progress.hud"
PLIST_DIR="$HOME/Library/LaunchAgents"
PLIST_PATH="$PLIST_DIR/${LABEL}.plist"
RUNTIME_DIR="$HOME/.config/xy_progress/runtime"
LOG_PATH="$RUNTIME_DIR/hud_agent.log"

APP_BUNDLE="$PROJECT_ROOT/macos/build/XiaoyanProgressHUD.app"
APP_BINARY="$APP_BUNDLE/Contents/MacOS/XiaoyanProgressHUD"
BUILD_SCRIPT="$PROJECT_ROOT/macos/build_app.sh"
GUI_DOMAIN="gui/$(id -u)"

if [[ ! -x "$APP_BINARY" ]]; then
  echo "[autolink] Building XiaoyanProgressHUD.app..."
  bash "$BUILD_SCRIPT"
fi

mkdir -p "$PLIST_DIR" "$RUNTIME_DIR"

# Preserve PATH cleanly while intentionally omitting TMPDIR so launchd injects
# the live per-session macOS TMPDIR matching Codex hooks.
DEFAULT_PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${HOME}/.local/bin"

cat > "$PLIST_PATH" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${APP_BINARY}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>Crashed</key>
        <true/>
    </dict>
    <key>StandardOutPath</key>
    <string>${LOG_PATH}</string>
    <key>StandardErrorPath</key>
    <string>${LOG_PATH}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>${DEFAULT_PATH}</string>
    </dict>
</dict>
</plist>
EOF

plutil -lint "$PLIST_PATH"

echo "[autolink] Reloading LaunchAgent (${LABEL}) in ${GUI_DOMAIN}..."
launchctl bootout "${GUI_DOMAIN}/${LABEL}" 2>/dev/null || \
  launchctl bootout "${GUI_DOMAIN}" "$PLIST_PATH" 2>/dev/null || true
launchctl bootstrap "${GUI_DOMAIN}" "$PLIST_PATH"

echo "[done] ChatGPT 联动自启已安装并生效："
echo "  LaunchAgent: $PLIST_PATH"
echo "  Agent Log:   $LOG_PATH"
echo "  默认模式:    随 ChatGPT 课题自动显隐（关闭 ChatGPT 自动休眠释放后端）"
