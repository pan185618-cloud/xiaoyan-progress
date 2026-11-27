"""Configuration and user preferences for xiaoyan-progress.

STRICT READ-ONLY GUARANTEE:
- Never writes inside RESEARCH_ROOT or ~/.config/xiaoyan-r-runtime/.
- The ONLY writable state managed here is ~/.config/xy_progress/prefs.json
  (via save_prefs with atomic temp-file replace).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from typing import Any

RESEARCH_ROOT = Path("/Users/abc/Desktop/本地工作流（小研）/科研工作流")
PREFS_PATH = Path.home() / ".config/xy_progress/prefs.json"
TMP_ROOT = Path(os.environ.get("TMPDIR") or tempfile.gettempdir())
CODEX_SESSIONS_ROOT = Path.home() / ".codex/sessions"
MAGPIE_ROUTING_ROOT = Path.home() / ".config/magpie/routing"
AUTHORITY_PATH = Path.home() / ".config/xiaoyan-r-runtime/receipt-authority.json"

DEFAULT_PREFS: dict[str, Any] = {
    "pinned_projects": [],
    "hidden_projects": [],
    "category_overrides": {},
    "primary_contract_overrides": {},
    "session_project_overrides": {},
}

_LIST_KEYS = ("pinned_projects", "hidden_projects")
_DICT_KEYS = (
    "category_overrides",
    "primary_contract_overrides",
    "session_project_overrides",
)


def _normalize_prefs(raw: Any) -> dict[str, Any]:
    data = raw if isinstance(raw, dict) else {}
    out: dict[str, Any] = {}
    for key in _LIST_KEYS:
        val = data.get(key)
        if isinstance(val, list):
            seen: set[str] = set()
            items: list[str] = []
            for item in val:
                if isinstance(item, str) and item.strip() and item not in seen:
                    seen.add(item)
                    items.append(item)
            out[key] = items
        else:
            out[key] = []
    for key in _DICT_KEYS:
        val = data.get(key)
        if isinstance(val, dict):
            out[key] = {
                str(k): str(v)
                for k, v in val.items()
                if isinstance(k, str) and k.strip() and isinstance(v, str) and v.strip()
            }
        else:
            out[key] = {}
    return out


def get_authority_path() -> Path:
    """Resolve receipt authority path dynamically (respecting env override and current HOME)."""
    override = os.environ.get("XIAOYAN_RECEIPT_AUTHORITY", "").strip()
    if override:
        return Path(override).expanduser()
    # Respect dynamic HOME changes (e.g. isolated tests) while matching AUTHORITY_PATH by default.
    return Path.home() / ".config/xiaoyan-r-runtime/receipt-authority.json"


def load_prefs(prefs_path: Path | None = None) -> dict[str, Any]:
    """Read user UI preferences from PREFS_PATH without creating files."""
    path = prefs_path or PREFS_PATH
    try:
        if not path.is_file():
            return _normalize_prefs({})
        with path.open("rb") as fh:
            raw = json.loads(fh.read().decode("utf-8"))
        return _normalize_prefs(raw)
    except (OSError, ValueError, UnicodeDecodeError):
        return _normalize_prefs({})


def save_prefs(prefs: dict[str, Any], prefs_path: Path | None = None) -> dict[str, Any]:
    """Atomically save normalized preferences ONLY to prefs_path (default PREFS_PATH)."""
    path = (prefs_path or PREFS_PATH).expanduser()
    normalized = _normalize_prefs(prefs)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = (json.dumps(normalized, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    fd, tmp_name = tempfile.mkstemp(prefix=".prefs-", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        tmp_path.chmod(0o600)
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    return normalized
