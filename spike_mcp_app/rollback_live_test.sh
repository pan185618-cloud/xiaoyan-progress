#!/usr/bin/env bash
set -euo pipefail

CODEX_BIN="${CODEX_BIN:-/Users/abc/.local/bin/codex}"
CFG="$HOME/.codex/config.toml"
BAK="$HOME/.codex/config.toml.bak-mcp-spike"

echo "[rollback] Removing plugin xiaoyan-progress-spike@xiaoyan-spike (if installed)..."
"$CODEX_BIN" plugin remove xiaoyan-progress-spike@xiaoyan-spike >/dev/null 2>&1 || true

echo "[rollback] Removing marketplace xiaoyan-spike (if configured)..."
"$CODEX_BIN" plugin marketplace remove xiaoyan-spike >/dev/null 2>&1 || true

if [[ -f "$BAK" ]]; then
  echo "[rollback] Restoring $BAK -> $CFG (preserving permissions)..."
  cp -p "$BAK" "$CFG"
  rm -f "$BAK"
else
  echo "[rollback] WARNING: Backup $BAK not found; removing enable_mcp_apps manually if present."
  /usr/bin/python3 - "$CFG" <<'PY'
import pathlib, re, sys
p = pathlib.Path(sys.argv[1])
txt = p.read_text(encoding="utf-8")
new_txt = re.sub(r'(?m)^enable_mcp_apps\s*=\s*true\s*\n', '', txt)
if new_txt != txt:
    p.write_text(new_txt, encoding="utf-8")
PY
fi

echo "[rollback] Verifying rollback state..."
"$CODEX_BIN" features list | grep -E '^enable_mcp_apps\b' || true
if "$CODEX_BIN" mcp list 2>/dev/null | grep -q 'xiaoyan-progress-spike'; then
  echo "[rollback] WARNING: xiaoyan-progress-spike still appears in codex mcp list"
  exit 1
else
  echo "[rollback] Confirmed: xiaoyan-progress-spike removed and config restored."
  echo "[rollback] Note: Restart ChatGPT Work when convenient to unload any cached UI state."
fi
