#!/usr/bin/env bash
set -euo pipefail

CODEX_BIN="${CODEX_BIN:-/Users/abc/.local/bin/codex}"
CFG="$HOME/.codex/config.toml"
BAK="$HOME/.codex/config.toml.bak-mcp-spike"

echo "[rollback] Removing plugin xiaoyan-research-progress@xiaoyan-progress (if installed)..."
"$CODEX_BIN" plugin remove xiaoyan-research-progress@xiaoyan-progress >/dev/null 2>&1 || true

echo "[rollback] Removing marketplace xiaoyan-progress (if configured)..."
"$CODEX_BIN" plugin marketplace remove xiaoyan-progress >/dev/null 2>&1 || true

if [[ "${1:-}" == "--restore-config" && -f "$BAK" ]]; then
  echo "[rollback] Restoring $BAK -> $CFG (preserving permissions)..."
  cp -p "$BAK" "$CFG"
  echo "[rollback] Restored original pre-spike config.toml (backup preserved at $BAK)."
else
  echo "[rollback] Plugin & marketplace removed. (Tip: pass --restore-config to also restore $BAK and disable enable_mcp_apps.)"
fi

if "$CODEX_BIN" mcp list 2>/dev/null | grep -q 'xiaoyan-research-progress'; then
  echo "[rollback] WARNING: xiaoyan-research-progress still listed in codex mcp list"
  exit 1
fi
echo "[rollback] Confirmed: xiaoyan-research-progress cleanly uninstalled."
