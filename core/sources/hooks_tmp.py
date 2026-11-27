"""Strictly read-only reader for $TMPDIR hook caches and cmd_summary statuses.

Verified macOS $TMPDIR parity:
- Resolves Path(os.environ.get("TMPDIR") or tempfile.gettempdir())
- Reads ONLY existing files under:
  - xy_figure_gate/ (fp_cache.json, *.count)
  - xy_prose_gate/ (fp_cache.json, *.count, *.hard_state.json)
  - xy_workflow_guard/ (*.count, *.json)
  - xiaoyan_cmd_summary/ (*.status.json)
- NEVER creates directories or writes anything.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .rollout import PROJECT_PATH_RE, sanitize_text

UUID_PREFIX_RE = re.compile(
    r"^([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)


def default_tmp_root() -> Path:
    return Path(os.environ.get("TMPDIR") or tempfile.gettempdir())


def _safe_read_json(path: Path) -> Any:
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
        if not raw.strip():
            return None
        return json.loads(raw.decode("utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return None


def _safe_read_int(path: Path) -> int | None:
    try:
        with open(path, "rb") as fh:
            txt = fh.read(64).decode("utf-8", errors="replace").strip()
        return int(txt)
    except (OSError, ValueError):
        return None


def _is_temp_test_path(path_str: str) -> bool:
    lower = path_str.lower()
    return "/var/folders/" in lower or "/tmp/" in lower or "pytest" in lower


class HooksTmpReader:
    """Read-only collector for $TMPDIR hook gate caches and cmd_summary statuses."""

    def __init__(self, tmp_root: Path | None = None, max_cmd_summaries: int = 25) -> None:
        self.tmp_root = Path(tmp_root) if tmp_root is not None else default_tmp_root()
        self.max_cmd_summaries = max_cmd_summaries

    def read_all(self) -> dict[str, Any]:
        return {
            "tmp_root": str(self.tmp_root),
            "figure_gate": self._read_figure_gate(),
            "prose_gate": self._read_prose_gate(),
            "workflow_guard": self._read_workflow_guard(),
            "cmd_summaries": self._read_cmd_summaries(),
        }

    def _read_fp_cache(self, cache_path: Path) -> tuple[int, int, list[dict[str, Any]]]:
        data = _safe_read_json(cache_path)
        if not isinstance(data, dict):
            return 0, 0, []
        pass_count = 0
        fail_count = 0
        recent: list[dict[str, Any]] = []
        for file_path, entry in data.items():
            if not isinstance(entry, dict):
                continue
            # Filter out ephemeral unit-test temp fixtures from real dashboard counts
            if _is_temp_test_path(str(file_path)):
                continue
            status = str(entry.get("status") or "UNKNOWN").upper()
            if status == "PASS":
                pass_count += 1
            elif status in ("FAIL", "BLOCK", "BLOCKED", "ERROR"):
                fail_count += 1
            ts = entry.get("ts")
            proj_match = PROJECT_PATH_RE.search(str(file_path))
            recent.append(
                {
                    "file": sanitize_text(Path(str(file_path)).name, 80),
                    "project_hint": proj_match.group(1) if proj_match else None,
                    "status": status,
                    "ts": float(ts) if isinstance(ts, (int, float)) else None,
                }
            )
        recent.sort(key=lambda x: x.get("ts") or 0.0, reverse=True)
        return pass_count, fail_count, recent[:15]

    def _read_session_counts(self, directory: Path) -> dict[str, int]:
        counts: dict[str, int] = {}
        if not directory.is_dir():
            return counts
        try:
            for entry in directory.iterdir():
                if not entry.is_file() or not entry.name.endswith(".count"):
                    continue
                val = _safe_read_int(entry)
                if val is None:
                    continue
                stem = entry.name[: -len(".count")]
                m = UUID_PREFIX_RE.match(stem)
                key = m.group(1).lower() if m else sanitize_text(stem, 64)
                counts[key] = val
        except OSError:
            pass
        return counts

    def _read_figure_gate(self) -> dict[str, Any]:
        d = self.tmp_root / "xy_figure_gate"
        if not d.is_dir():
            return {
                "present": False,
                "pass_count": 0,
                "fail_count": 0,
                "session_counts": {},
                "recent": [],
            }
        pass_c, fail_c, recent = self._read_fp_cache(d / "fp_cache.json")
        return {
            "present": True,
            "pass_count": pass_c,
            "fail_count": fail_c,
            "session_counts": self._read_session_counts(d),
            "recent": recent,
        }

    def _read_prose_gate(self) -> dict[str, Any]:
        d = self.tmp_root / "xy_prose_gate"
        if not d.is_dir():
            return {
                "present": False,
                "pass_count": 0,
                "fail_count": 0,
                "session_counts": {},
                "hard_states": {},
                "recent": [],
            }
        pass_c, fail_c, recent = self._read_fp_cache(d / "fp_cache.json")
        hard_states: dict[str, dict[str, Any]] = {}
        try:
            for entry in d.iterdir():
                if not entry.is_file() or not entry.name.endswith(".hard_state.json"):
                    continue
                stem = entry.name[: -len(".hard_state.json")]
                # Skip synthetic unit test states unless they are real UUIDs or explicitly non-test
                if stem.startswith("test_hard_"):
                    continue
                data = _safe_read_json(entry)
                if isinstance(data, dict):
                    m = UUID_PREFIX_RE.match(stem)
                    key = m.group(1).lower() if m else sanitize_text(stem, 64)
                    hard_states[key] = {
                        "identical_streak": int(data.get("identical_streak") or 0),
                        "total_blocks": int(data.get("total_blocks") or 0),
                    }
        except OSError:
            pass
        return {
            "present": True,
            "pass_count": pass_c,
            "fail_count": fail_c,
            "session_counts": self._read_session_counts(d),
            "hard_states": hard_states,
            "recent": recent,
        }

    def _read_workflow_guard(self) -> dict[str, Any]:
        d = self.tmp_root / "xy_workflow_guard"
        if not d.is_dir():
            return {"present": False, "polling_counts": {}}
        counts = self._read_session_counts(d)
        try:
            for entry in d.iterdir():
                if not entry.is_file() or not entry.name.endswith(".json"):
                    continue
                data = _safe_read_json(entry)
                if isinstance(data, dict):
                    stem = entry.stem
                    m = UUID_PREFIX_RE.match(stem)
                    key = m.group(1).lower() if m else sanitize_text(stem, 64)
                    val = data.get("list_agents_count") or data.get("count") or data.get("polls")
                    if isinstance(val, int):
                        counts[key] = val
        except OSError:
            pass
        return {"present": True, "polling_counts": counts}

    def _read_cmd_summaries(self) -> list[dict[str, Any]]:
        d = self.tmp_root / "xiaoyan_cmd_summary"
        if not d.is_dir():
            return []
        candidates: list[tuple[float, Path]] = []
        try:
            for entry in d.iterdir():
                if not entry.is_file() or not entry.name.endswith(".status.json"):
                    continue
                try:
                    mtime = entry.stat().st_mtime
                except OSError:
                    continue
                candidates.append((mtime, entry))
        except OSError:
            return []

        candidates.sort(key=lambda item: item[0], reverse=True)
        items: list[dict[str, Any]] = []
        for mtime, path in candidates[: self.max_cmd_summaries]:
            data = _safe_read_json(path)
            if not isinstance(data, dict):
                continue
            raw_cmd = data.get("command")
            project_hint: str | None = None
            script_hint: str | None = None
            if isinstance(raw_cmd, list):
                joined = " ".join(str(x) for x in raw_cmd[:16])
                pm = PROJECT_PATH_RE.search(joined)
                if pm:
                    project_hint = pm.group(1)
                for tok in raw_cmd:
                    s = str(tok)
                    if s.endswith((".py", ".R", ".r", ".sh")):
                        script_hint = sanitize_text(Path(s).name, 64)
                        break
            items.append(
                {
                    "id": path.stem,
                    "name": sanitize_text(str(data.get("name") or script_hint or "cmd"), 64),
                    "status": sanitize_text(str(data.get("status") or "unknown"), 24),
                    "exit_code": data.get("exit_code") if isinstance(data.get("exit_code"), int) else None,
                    "elapsed_s": round(float(data["elapsed_s"]), 2)
                    if isinstance(data.get("elapsed_s"), (int, float))
                    else None,
                    "script": script_hint,
                    "project_hint": project_hint,
                    "updated_at": datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat(),
                    "updated_ts": mtime,
                }
            )
        return items
