#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCES_DIR="$SCRIPT_DIR/Sources"
INFO_PLIST="$SCRIPT_DIR/Info.plist"
BUILD_DIR="$SCRIPT_DIR/build"
APP_BUNDLE="$BUILD_DIR/XiaoyanProgressHUD.app"
CONTENTS_DIR="$APP_BUNDLE/Contents"
MACOS_DIR="$CONTENTS_DIR/MacOS"
RESOURCES_DIR="$CONTENTS_DIR/Resources"
BINARY_PATH="$MACOS_DIR/XiaoyanProgressHUD"

if ! command -v swiftc >/dev/null 2>&1; then
  echo "[error] swiftc not found. Please install macOS Command Line Tools." >&2
  exit 1
fi

COMPILER_VER="$(swiftc --version 2>&1 | head -n 1)"
echo "[build] Compiler: $COMPILER_VER"
echo "[build] Packaging: $APP_BUNDLE"

rm -rf "$APP_BUNDLE"
mkdir -p "$MACOS_DIR" "$RESOURCES_DIR"
cp "$INFO_PLIST" "$CONTENTS_DIR/Info.plist"

SWIFT_FILES=(
  "$SOURCES_DIR/ApprovalLauncher.swift"
  "$SOURCES_DIR/BackendSupervisor.swift"
  "$SOURCES_DIR/WebBridge.swift"
  "$SOURCES_DIR/HudPanel.swift"
  "$SOURCES_DIR/AppDelegate.swift"
  "$SOURCES_DIR/main.swift"
)

for src in "${SWIFT_FILES[@]}"; do
  if [[ ! -f "$src" ]]; then
    echo "[error] Missing Swift source: $src" >&2
    exit 1
  fi
done

swiftc \
  -O \
  "${SWIFT_FILES[@]}" \
  -framework AppKit \
  -framework SwiftUI \
  -framework WebKit \
  -framework Foundation \
  -framework QuartzCore \
  -o "$BINARY_PATH"

chmod +x "$BINARY_PATH"

echo "[sign] Ad-hoc signing $APP_BUNDLE"
codesign --force --deep --sign - "$APP_BUNDLE"

echo "[verify] Checking binary architecture & signature"
file "$BINARY_PATH"
codesign --verify --deep --strict "$APP_BUNDLE"
echo "[done] Built and verified: $APP_BUNDLE"
