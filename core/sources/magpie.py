"""Incremental read-only tailer for Magpie routing JSONL logs.

Verified join key:
- Exact 36-char UUID equality: rollout.session_meta.payload.session_id == magpie.session
  (Parent UUID covers both top-level and all subagent requests.)
- Filters out non-rollout ephemeral background services (memory_consolidation,
  thread_title, ambient_suggestions, ambient_suggestion_safety, web_search).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .rollout import sanitize_text

UUID_FULL_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

EPHEMERAL_SERVICES = frozenset(
    {
        "memory_consolidation",
        "thread_title",
        "ambient_suggestions",
        "ambient_suggestion_safety",
        "web_search",
    }
)

STALL_TIMEOUT_SECONDS = 600.0  # >10 min per AGENTS.md & proposal


def _parse_iso_ts(ts: str | None) -> float | None:
    if not ts or not isinstance(ts, str):
        return None
    try:
        s = ts.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


@dataclass
class MagpieSessionHealth:
    session_id: str
    last_time: str | None = None
    last_ts: float | None = None
    last_success_ts: float | None = None
    model: str | None = None
    served: str | None = None
    provider: str | None = None
    last_status: int | None = None
    last_done: bool = True
    ttft_ms: int | None = None
    latency_ms: int | None = None
    retries_429: int = 0
    recent_429_streak: int = 0
    streak_504_413: int = 0
    last_error: str | None = None
    fallback_prompt_tokens: int | None = None
    fallback_window_tokens: int | None = None
    folder_hint: str | None = None
    request_count: int = 0
    subagent_request_count: int = 0
    inflight_started_ts: float | None = None

    def to_upstream_dict(self, now_ts: float | None = None) -> dict[str, Any]:
        now = now_ts if now_ts is not None else datetime.now(timezone.utc).timestamp()
        stalled_over_10m = False
        if not self.last_done and self.inflight_started_ts is not None:
            if (now - self.inflight_started_ts) >= STALL_TIMEOUT_SECONDS:
                stalled_over_10m = True
        if (
            self.streak_504_413 >= 2
            and self.last_ts is not None
            and self.last_success_ts is not None
            and (self.last_ts - self.last_success_ts) >= STALL_TIMEOUT_SECONDS
            and (now - self.last_ts) < 3600.0
        ):
            stalled_over_10m = True

        stall_alert = stalled_over_10m or self.streak_504_413 >= 2
        alert_level = "ok"
        alert_message: str | None = None

        if stalled_over_10m:
            alert_level = "critical"
            alert_message = "上游请求超过 10 分钟无响应或持续超时，建议检查网关日志并精简上下文"
        elif self.streak_504_413 >= 3:
            alert_level = "critical"
            alert_message = f"网关连续 {self.streak_504_413} 次返回 504/413（上游超时或请求体过大）"
        elif self.streak_504_413 >= 2:
            alert_level = "warn"
            alert_message = f"网关连续 {self.streak_504_413} 次返回 504/413，请关注上下文大小"
        elif self.recent_429_streak >= 3 or self.last_status == 429:
            alert_level = "warn"
            alert_message = f"上游触发 429 限流重试（累计 {self.retries_429} 次）"
        elif self.last_status is not None and self.last_status >= 500:
            alert_level = "warn"
            alert_message = f"最近请求返回 HTTP {self.last_status}"

        return {
            "last_status": self.last_status,
            "ttft_ms": self.ttft_ms,
            "latency_ms": self.latency_ms,
            "retries_429": self.retries_429,
            "streak_504_413": self.streak_504_413,
            "stalled_over_10m": stalled_over_10m,
            "stall_alert": stall_alert,
            "alert_level": alert_level,
            "alert_message": alert_message,
            "model": self.model,
            "served": self.served,
            "provider": self.provider,
            "last_time": self.last_time,
            "request_count": self.request_count,
            "subagent_request_count": self.subagent_request_count,
        }


@dataclass
class _MagpieCursor:
    inode: int
    offset: int = 0
    partial: bytes = b""


class MagpieTailer:
    """Strictly read-only incremental (inode, byte_offset) tailer for Magpie routing logs."""

    def __init__(
        self,
        routing_root: Path | None = None,
        days_back: int = 1,
    ) -> None:
        self.routing_root = Path(
            routing_root or (Path.home() / ".config" / "magpie" / "routing")
        )
        self.days_back = max(0, int(days_back))
        self._cursors: dict[str, _MagpieCursor] = {}
        self._sessions: dict[str, MagpieSessionHealth] = {}
        self._file_sessions: dict[str, dict[str, MagpieSessionHealth]] = {}

    def _candidate_files(self, now: datetime | None = None) -> list[Path]:
        ref_local = now or datetime.now()
        ref_utc = (
            now.astimezone(timezone.utc)
            if (now is not None and now.tzinfo is not None)
            else datetime.now(timezone.utc)
        )
        names: list[str] = []
        for delta in range(self.days_back, -1, -1):
            for base in (ref_local, ref_utc):
                d = base - timedelta(days=delta)
                fname = f"{d.year:04d}-{d.month:02d}-{d.day:02d}.jsonl"
                if fname not in names:
                    names.append(fname)
        files = [self.routing_root / n for n in names if (self.routing_root / n).is_file()]
        return files

    def poll(self, now: datetime | None = None) -> dict[str, MagpieSessionHealth]:
        files = self._candidate_files(now=now)
        active_keys: set[str] = set()
        rebuilt = False

        for path in files:
            key = str(path)
            active_keys.add(key)
            if self._tail_file(path, key):
                rebuilt = True

        stale = [k for k in list(self._cursors.keys()) if k not in active_keys]
        for k in stale:
            self._cursors.pop(k, None)
            self._file_sessions.pop(k, None)
            rebuilt = True

        if rebuilt:
            self._rebuild_merged_sessions(files)
        return dict(self._sessions)

    def _tail_file(self, path: Path, key: str) -> bool:
        try:
            st = os.stat(path)
        except OSError:
            return False

        cursor = self._cursors.get(key)
        if cursor is None or cursor.inode != st.st_ino or st.st_size < cursor.offset:
            cursor = _MagpieCursor(inode=st.st_ino, offset=0, partial=b"")
            self._cursors[key] = cursor
            self._file_sessions[key] = {}

        if st.st_size == cursor.offset:
            return False

        try:
            with open(path, "rb") as fh:
                fh.seek(cursor.offset)
                chunk = fh.read()
        except OSError:
            return False

        if not chunk:
            return False

        cursor.offset += len(chunk)
        buf = cursor.partial + chunk
        lines = buf.split(b"\n")
        if not buf.endswith(b"\n"):
            cursor.partial = lines.pop()
        else:
            cursor.partial = b""
            if lines and lines[-1] == b"":
                lines.pop()

        per_file = self._file_sessions[key]
        for raw_line in lines:
            if not raw_line.strip():
                continue
            try:
                rec = json.loads(raw_line.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                self._apply_record(per_file, rec)
        return True

    def _is_ephemeral(self, rec: dict[str, Any]) -> bool:
        for field_name in ("kind", "service", "sub", "purpose"):
            val = rec.get(field_name)
            if isinstance(val, str) and val.strip().lower() in EPHEMERAL_SERVICES:
                return True
        return False

    def _apply_record(
        self, store: dict[str, MagpieSessionHealth], rec: dict[str, Any]
    ) -> None:
        agent = str(rec.get("agent") or "").strip().lower()
        if agent and agent != "codex":
            return
        if self._is_ephemeral(rec):
            return

        raw_session = rec.get("session")
        if not isinstance(raw_session, str):
            return
        session_id = raw_session.strip().lower()
        if not UUID_FULL_RE.match(session_id):
            return

        health = store.get(session_id)
        if health is None:
            health = MagpieSessionHealth(session_id=session_id)
            store[session_id] = health

        ts_str = rec.get("time")
        ts_val = _parse_iso_ts(ts_str)
        health.request_count += 1

        if rec.get("sub") or rec.get("subagentId") or rec.get("subagent"):
            health.subagent_request_count += 1

        if ts_val is not None and (health.last_ts is None or ts_val >= health.last_ts):
            health.last_ts = ts_val
            health.last_time = ts_str

        model = rec.get("model")
        if isinstance(model, str) and model.strip():
            health.model = sanitize_text(model, 80)
        served = rec.get("served")
        if isinstance(served, str) and served.strip():
            health.served = sanitize_text(served, 80)
        provider = rec.get("provider")
        if isinstance(provider, str) and provider.strip():
            health.provider = sanitize_text(provider, 64)
        folder = rec.get("folder")
        if isinstance(folder, str) and folder.strip():
            health.folder_hint = sanitize_text(folder, 200)

        ttft = rec.get("ttft")
        if isinstance(ttft, (int, float)) and ttft >= 0:
            health.ttft_ms = int(ttft)
        ms = rec.get("ms")
        if isinstance(ms, (int, float)) and ms >= 0:
            health.latency_ms = int(ms)

        # Count 429 retries and 504/413 failures inside tries[]
        tries = rec.get("tries")
        req_429 = 0
        req_504_413_tries = 0
        if isinstance(tries, list):
            for t in tries:
                if not isinstance(t, dict):
                    continue
                t_status = t.get("status")
                if t_status == 429:
                    req_429 += 1
                elif t_status in (504, 413):
                    req_504_413_tries += 1

        status = rec.get("status")
        status_int = int(status) if isinstance(status, (int, float)) else None
        if status_int == 429 and req_429 == 0:
            req_429 = 1

        health.retries_429 += req_429
        if req_429 > 0:
            health.recent_429_streak += req_429
        elif status_int == 200:
            health.recent_429_streak = 0

        done = rec.get("done")
        is_done = True if done is None else bool(done)
        health.last_done = is_done
        if not is_done:
            if health.inflight_started_ts is None:
                health.inflight_started_ts = ts_val
        else:
            health.inflight_started_ts = None

        health.last_status = status_int
        if status_int == 200 and is_done:
            health.streak_504_413 = 0
            health.last_error = None
            if ts_val is not None:
                health.last_success_ts = ts_val
        elif status_int in (504, 413):
            health.streak_504_413 += max(1, req_504_413_tries)
        elif req_504_413_tries > 0 and not is_done:
            health.streak_504_413 += req_504_413_tries

        err = rec.get("error")
        if isinstance(err, str) and err.strip():
            health.last_error = sanitize_text(err, 120)

        # Fallback context token estimate from prompt.tokens (only when no subagent overrides root,
        # or if root has not reported yet; still track latest valid positive value)
        prompt = rec.get("prompt")
        if isinstance(prompt, dict):
            ptok = prompt.get("tokens")
            if isinstance(ptok, (int, float)) and ptok > 0:
                is_sub = bool(rec.get("sub") or rec.get("subagentId") or rec.get("subagent"))
                if not is_sub or health.fallback_prompt_tokens is None:
                    health.fallback_prompt_tokens = int(ptok)
            pwin = prompt.get("window")
            if isinstance(pwin, (int, float)) and pwin > 0:
                health.fallback_window_tokens = int(pwin)

    def _rebuild_merged_sessions(self, ordered_files: list[Path]) -> None:
        merged: dict[str, MagpieSessionHealth] = {}
        for path in ordered_files:
            per_file = self._file_sessions.get(str(path), {})
            for sid, h in per_file.items():
                prev = merged.get(sid)
                if prev is None:
                    merged[sid] = MagpieSessionHealth(**h.__dict__)
                else:
                    # Merge chronologically (yesterday -> today)
                    prev.request_count += h.request_count
                    prev.subagent_request_count += h.subagent_request_count
                    prev.retries_429 += h.retries_429
                    if h.last_ts is not None and (prev.last_ts is None or h.last_ts >= prev.last_ts):
                        prev.last_time = h.last_time
                        prev.last_ts = h.last_ts
                        prev.model = h.model or prev.model
                        prev.served = h.served or prev.served
                        prev.provider = h.provider or prev.provider
                        prev.last_status = h.last_status
                        prev.last_done = h.last_done
                        prev.ttft_ms = h.ttft_ms
                        prev.latency_ms = h.latency_ms
                        prev.recent_429_streak = h.recent_429_streak
                        prev.streak_504_413 = (
                            h.streak_504_413
                            if h.last_success_ts is not None
                            else prev.streak_504_413 + h.streak_504_413
                        )
                        prev.last_error = h.last_error
                        prev.inflight_started_ts = h.inflight_started_ts
                    if h.last_success_ts is not None:
                        prev.last_success_ts = max(prev.last_success_ts or 0.0, h.last_success_ts)
                    if h.fallback_prompt_tokens is not None:
                        prev.fallback_prompt_tokens = h.fallback_prompt_tokens
                    if h.fallback_window_tokens is not None:
                        prev.fallback_window_tokens = h.fallback_window_tokens
                    if h.folder_hint is not None:
                        prev.folder_hint = h.folder_hint
        self._sessions = merged
