"""Incremental read-only tailer for Codex rollout-*.jsonl session logs.

Strictly whitelisted field extraction:
- NEVER stores prompt text, user/assistant messages, reasoning text,
  encrypted_content, raw tool arguments, raw tool outputs, or secrets.
- Uses payload.info.last_token_usage.total_tokens as the verified current
  context window token occupancy (handling post-compaction input=0/output=0
  snapshots accurately) and retains the last non-null model_context_window.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
PROJECT_PATH_RE = re.compile(r"(?:^|[\s/\\'\"=])projects/([^/\"'\s]+(?:/[^/\"'\s]+)?)")
CONTRACT_PATH_RE = re.compile(
    r"(?:04_合同|00_protocol)/(?:contracts/)?[A-Za-z0-9_.\-\u4e00-\u9fff]+\.json"
)
RUN_ID_RE = re.compile(r"\b(?:run|val|rel|EV)[_-][A-Za-z0-9_-]{3,48}\b")

SECRET_PATTERNS = [
    re.compile(r"gAAAAA[A-Za-z0-9_\-=]{10,}"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{12,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{8,}\b", re.IGNORECASE),
    re.compile(r"\bAIza[A-Za-z0-9_\-]{16,}\b"),
    re.compile(r"(?:api[_-]?key|token|secret|password)\s*[:=]\s*[^\s,;\"']+", re.IGNORECASE),
    re.compile(r"[A-Za-z0-9+/=_\-]{48,}"),
]

ACTION_KEYWORDS = (
    ("batch_formal_pipeline.py", "batch_formal_pipeline"),
    ("open_approval_terminal.py", "open_approval_terminal"),
    ("check_figure.py", "check_figure"),
    ("figure_gate.py", "figure_gate"),
    ("prose_mark.py", "prose_mark"),
    ("prose_gate.py", "prose_gate"),
    ("handoff_builder.py", "handoff_builder"),
    ("sign_contract_batch.py", "sign_contract"),
    ("approve_baseline_snapshots.py", "approve_snapshots"),
    ("approve_zw30_snapshot.py", "approve_snapshots"),
    ("cmd_summary.py", "cmd_summary"),
    ("exploration_card.py", "exploration_card"),
    ("formal_validate", "formal_validate"),
    ("evidence_publish", "evidence_publish"),
    ("formal_seal", "formal_seal"),
    ("formal_run", "formal_run"),
    ("preflight", "preflight"),
)

MAX_CALLS_PER_THREAD = 80
MAX_PATHS_PER_CALL = 4
MAX_LABEL_LEN = 120


def sanitize_text(text: str | None, max_len: int = MAX_LABEL_LEN) -> str:
    """Strip secret-like tokens and bound length safely."""
    if not text:
        return ""
    cleaned = str(text).replace("\r", " ").replace("\n", " ").strip()
    for pat in SECRET_PATTERNS:
        cleaned = pat.sub("[REDACTED]", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) > max_len:
        cleaned = cleaned[: max_len - 1] + "…"
    return cleaned


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


def _extract_uuid_from_filename(path: Path) -> str | None:
    matches = UUID_RE.findall(path.name)
    return matches[-1].lower() if matches else None


def _normalize_plan_status(raw: str | None) -> str:
    val = (raw or "").strip().lower()
    if val in ("completed", "done", "passed"):
        return "done"
    if val in ("in_progress", "running", "active"):
        return "running"
    if val in ("blocked",):
        return "blocked"
    if val in ("failed", "error"):
        return "failed"
    if val in ("skipped", "cancelled"):
        return "skipped"
    return "pending"


READ_ONLY_SHELL_RE = re.compile(
    r"^\s*(?:sed|cat|head|tail|nl|rg|grep|egrep|fgrep|ls|wc|stat|file|find|diff|git\s+(?:status|diff|log|show))\b",
    re.IGNORECASE,
)


def _detect_action_hint(tool_name: str, text_hints: list[str], raw_cmd: str = "") -> str:
    lower_name = (tool_name or "").lower()
    for kw, action in ACTION_KEYWORDS:
        if kw.lower() in lower_name:
            return action
    if lower_name in ("spawn_agent", "invoke_subagent", "send_message", "wait_agent"):
        return lower_name
    if lower_name == "update_plan":
        return "update_plan"
    # If this is a shell command performing a read-only inspection (e.g. `sed -n ... open_approval_terminal.py`),
    # do NOT misclassify it as executing that workflow script!
    if raw_cmd and READ_ONLY_SHELL_RE.match(raw_cmd):
        return "read_file"
    combined = " ".join(text_hints).lower()
    for kw, action in ACTION_KEYWORDS:
        if kw.lower() in combined:
            return action
    return lower_name or "tool"


def _extract_safe_call_metadata(
    tool_name: str, raw_args: Any
) -> tuple[str, dict[str, Any], list[dict[str, str]] | None]:
    """Extract ONLY whitelisted metadata from tool call arguments in memory.

    Returns (action_hint, safe_meta, optional_plan_steps).
    Never returns raw arguments or prompts.
    """
    parsed: dict[str, Any] | None = None
    raw_str = ""
    if isinstance(raw_args, dict):
        parsed = raw_args
    elif isinstance(raw_args, str):
        raw_str = raw_args
        stripped = raw_args.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                candidate = json.loads(stripped)
                if isinstance(candidate, dict):
                    parsed = candidate
            except json.JSONDecodeError:
                parsed = None

    plan_steps: list[dict[str, str]] | None = None
    if tool_name == "update_plan" and isinstance(parsed, dict):
        raw_plan = parsed.get("plan") or parsed.get("steps")
        if isinstance(raw_plan, list):
            plan_steps = []
            for idx, item in enumerate(raw_plan[:40]):
                if isinstance(item, dict):
                    title = sanitize_text(
                        item.get("step") or item.get("title") or f"步骤 {idx + 1}",
                        max_len=120,
                    )
                    status = _normalize_plan_status(item.get("status"))
                    plan_steps.append({"title": title, "status": status})

    text_hints: list[str] = []
    target_paths: list[str] = []
    project_id: str | None = None
    contract_path: str | None = None
    run_id: str | None = None
    analysis_group: str | None = None
    target_summary: str | None = None

    raw_cmd_str = ""
    if isinstance(parsed, dict):
        for key in ("project_id", "project", "project_name"):
            v = parsed.get(key)
            if isinstance(v, str) and v.strip() and len(v.strip()) <= 120:
                project_id = sanitize_text(v.strip(), 100)
                break
        for key in ("contract_path", "contract", "contract_id"):
            v = parsed.get(key)
            if isinstance(v, str) and v.strip():
                contract_path = sanitize_text(v.strip(), 160)
                text_hints.append(v)
                break
        for key in ("run_id", "validation_id", "release_id", "evidence_id"):
            v = parsed.get(key)
            if isinstance(v, str) and v.strip():
                run_id = sanitize_text(v.strip(), 64)
                break
        ag = parsed.get("analysis_group")
        if isinstance(ag, str) and ag.strip():
            analysis_group = sanitize_text(ag.strip(), 80)

        for key in ("path", "file_path", "Path", "target_path", "cwd", "workdir", "directory", "script"):
            v = parsed.get(key)
            if isinstance(v, str) and v.strip():
                text_hints.append(v)
                if len(target_paths) < MAX_PATHS_PER_CALL:
                    target_paths.append(sanitize_text(v, 220))

        for key in ("cmd", "command", "CommandLine"):
            v = parsed.get(key)
            if isinstance(v, str) and v.strip():
                raw_cmd_str = v.strip()
                text_hints.append(v)
                # Extract only project paths, contract paths, and safe script basenames
                for m in PROJECT_PATH_RE.findall(v):
                    cleaned_p = f"projects/{m.rstrip('.,;:)')}"
                    if cleaned_p not in target_paths and len(target_paths) < MAX_PATHS_PER_CALL:
                        target_paths.append(sanitize_text(cleaned_p, 180))
                if not contract_path:
                    cm = CONTRACT_PATH_RE.search(v)
                    if cm:
                        contract_path = sanitize_text(cm.group(0), 160)
                if not run_id:
                    rm = RUN_ID_RE.search(v)
                    if rm:
                        run_id = rm.group(0)
                # Extract safe script/tool basename only (never full shell text)
                script_matches = re.findall(
                    r"([A-Za-z0-9_.\-]+\.(?:py|R|r|sh|swift|js|json|md))", v
                )
                if script_matches:
                    target_summary = sanitize_text(script_matches[0], 64)
            elif isinstance(v, list):
                joined = " ".join(str(x) for x in v[:12])
                text_hints.append(joined)
                for m in PROJECT_PATH_RE.findall(joined):
                    cleaned_p = f"projects/{m.rstrip('.,;:)')}"
                    if cleaned_p not in target_paths and len(target_paths) < MAX_PATHS_PER_CALL:
                        target_paths.append(sanitize_text(cleaned_p, 180))

        if not target_summary:
            for key in ("agent_type", "role", "nickname", "name"):
                v = parsed.get(key)
                if isinstance(v, str) and v.strip():
                    target_summary = sanitize_text(v.strip(), 64)
                    break
            if not target_summary and target_paths:
                target_summary = sanitize_text(Path(target_paths[0]).name, 64)
    elif raw_str:
        # e.g., apply_patch custom_tool_call input: extract only file path headers
        text_hints.append(raw_str[:400])
        for line in raw_str.splitlines()[:40]:
            for prefix in ("*** Update File: ", "*** Add File: ", "*** Delete File: "):
                if line.startswith(prefix):
                    fp = line[len(prefix) :].strip()
                    if fp and len(target_paths) < MAX_PATHS_PER_CALL:
                        target_paths.append(sanitize_text(fp, 220))
        if target_paths:
            target_summary = sanitize_text(Path(target_paths[0]).name, 64)
        for m in PROJECT_PATH_RE.findall(raw_str[:2000]):
            cleaned_p = f"projects/{m.rstrip('.,;:)')}"
            if cleaned_p not in target_paths and len(target_paths) < MAX_PATHS_PER_CALL:
                target_paths.append(sanitize_text(cleaned_p, 180))

    action_hint = _detect_action_hint(tool_name, text_hints, raw_cmd=raw_cmd_str)
    safe_meta: dict[str, Any] = {
        "project_id": project_id,
        "contract_path": contract_path,
        "run_id": run_id,
        "analysis_group": analysis_group,
        "target_paths": target_paths,
        "target_summary": target_summary,
    }
    return action_hint, safe_meta, plan_steps


def _classify_tool_output(payload: dict[str, Any]) -> tuple[str, str | None]:
    """Classify tool output status without retaining any output body."""
    raw_status = str(payload.get("status") or "").strip().lower()
    if raw_status in ("failed", "error"):
        return "failed", None
    if raw_status in ("blocked",):
        return "blocked", None

    output = payload.get("output")
    spawned_uuid: str | None = None
    if isinstance(output, str):
        head = output[:600]
        # Check JSON-encoded tool outputs (e.g., spawn_agent or structured MCP response)
        stripped = output.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                obj = json.loads(stripped)
                if isinstance(obj, dict):
                    for k in ("agent_id", "thread_id", "child_thread_id"):
                        val = obj.get(k)
                        if isinstance(val, str) and UUID_RE.fullmatch(val.strip()):
                            spawned_uuid = val.strip().lower()
                            break
                    if obj.get("decision") == "block" or obj.get("status") == "blocked":
                        return "blocked", spawned_uuid
                    if obj.get("ok") is False or obj.get("status") in ("error", "failed"):
                        return "failed", spawned_uuid
                    if isinstance(obj.get("exit_code"), int) and obj["exit_code"] != 0:
                        return "failed", spawned_uuid
            except json.JSONDecodeError:
                pass
        # Check Codex shell tool header ("Process exited with code N")
        m_exit = re.search(r"Process exited with code (\d+)", head)
        if m_exit and int(m_exit.group(1)) != 0:
            return "failed", spawned_uuid
        if '"decision": "block"' in head or '"decision":"block"' in head:
            return "blocked", spawned_uuid

    return "done", spawned_uuid


@dataclass
class ToolCallRecord:
    call_id: str
    name: str
    action_hint: str
    started_at: str | None = None
    started_ts: float | None = None
    ended_at: str | None = None
    ended_ts: float | None = None
    duration_ms: int | None = None
    status: str = "running"  # running | done | failed | blocked
    turn_id: str | None = None
    project_id: str | None = None
    contract_path: str | None = None
    run_id: str | None = None
    analysis_group: str | None = None
    target_paths: list[str] = field(default_factory=list)
    target_summary: str | None = None
    spawned_thread_id: str | None = None


@dataclass
class RolloutThreadState:
    file_path: str
    thread_id: str
    session_id: str  # Root session UUID (equals thread_id for top-level threads)
    is_subagent: bool = False
    parent_thread_id: str | None = None
    agent_nickname: str | None = None
    agent_role: str | None = None
    agent_path: str | None = None
    cwd: str | None = None
    originator: str | None = None
    model: str | None = None
    effort: str | None = None
    started_at: str | None = None
    started_ts: float | None = None
    updated_at: str | None = None
    updated_ts: float | None = None
    current_turn_id: str | None = None
    root_turn_id: str | None = None
    turn_active: bool = False
    used_tokens: int | None = None
    cumulative_tokens: int | None = None
    window_tokens: int | None = None
    token_updated_at: str | None = None
    compacted_count: int = 0
    plan_steps: list[dict[str, str]] = field(default_factory=list)
    plan_updated_at: str | None = None
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    _call_index: dict[str, ToolCallRecord] = field(default_factory=dict, repr=False)

    def add_tool_call(self, rec: ToolCallRecord) -> None:
        if rec.call_id and rec.call_id in self._call_index:
            return
        self.tool_calls.append(rec)
        if rec.call_id:
            self._call_index[rec.call_id] = rec
        if len(self.tool_calls) > MAX_CALLS_PER_THREAD:
            evicted = self.tool_calls.pop(0)
            if evicted.call_id and self._call_index.get(evicted.call_id) is evicted:
                self._call_index.pop(evicted.call_id, None)

    def complete_tool_call(
        self,
        call_id: str,
        status: str,
        ended_at: str | None,
        ended_ts: float | None,
        spawned_thread_id: str | None = None,
    ) -> None:
        rec = self._call_index.get(call_id)
        if rec is None:
            return
        rec.status = status
        rec.ended_at = ended_at
        rec.ended_ts = ended_ts
        if spawned_thread_id:
            rec.spawned_thread_id = spawned_thread_id
        if rec.started_ts is not None and ended_ts is not None and ended_ts >= rec.started_ts:
            rec.duration_ms = int((ended_ts - rec.started_ts) * 1000)


@dataclass
class _FileCursor:
    inode: int
    offset: int = 0
    partial: bytes = b""


class RolloutTailer:
    """Strictly read-only incremental (inode, byte_offset) tailer for Codex rollouts."""

    def __init__(
        self,
        sessions_root: Path | None = None,
        days_back: int = 1,
    ) -> None:
        self.sessions_root = Path(
            sessions_root or (Path.home() / ".codex" / "sessions")
        )
        self.days_back = max(0, int(days_back))
        self._cursors: dict[str, _FileCursor] = {}
        self._threads: dict[str, RolloutThreadState] = {}  # keyed by resolved file path

    def _candidate_date_dirs(self, now: datetime | None = None) -> list[Path]:
        ref_local = now or datetime.now()
        ref_utc = (
            now.astimezone(timezone.utc)
            if (now is not None and now.tzinfo is not None)
            else datetime.now(timezone.utc)
        )
        dates: list[tuple[int, int, int]] = []
        for delta in range(self.days_back + 1):
            for base in (ref_local, ref_utc):
                d = base - timedelta(days=delta)
                key = (d.year, d.month, d.day)
                if key not in dates:
                    dates.append(key)
        dirs: list[Path] = []
        for y, m, d in dates:
            p = self.sessions_root / f"{y:04d}" / f"{m:02d}" / f"{d:02d}"
            if p.is_dir():
                dirs.append(p)
        return dirs

    def discover_files(self, now: datetime | None = None) -> list[Path]:
        files: list[Path] = []
        for day_dir in self._candidate_date_dirs(now=now):
            try:
                for entry in day_dir.iterdir():
                    if entry.is_file() and entry.name.startswith("rollout-") and entry.name.endswith(".jsonl"):
                        files.append(entry)
            except OSError:
                continue
        # Also allow flat test directories where rollout-*.jsonl sit directly in sessions_root
        if not files and self.sessions_root.is_dir():
            try:
                for entry in self.sessions_root.iterdir():
                    if entry.is_file() and entry.name.startswith("rollout-") and entry.name.endswith(".jsonl"):
                        files.append(entry)
            except OSError:
                pass
        files.sort(key=lambda p: p.name)
        return files

    def poll(self, now: datetime | None = None) -> dict[str, RolloutThreadState]:
        """Incrementally read new bytes from discovered rollout files."""
        files = self.discover_files(now=now)
        active_keys: set[str] = set()
        for path in files:
            key = str(path)
            active_keys.add(key)
            self._tail_file(path, key)

        # Prune files that disappeared or fell outside the active date window
        stale = [k for k in list(self._cursors.keys()) if k not in active_keys]
        for k in stale:
            self._cursors.pop(k, None)
            self._threads.pop(k, None)

        return {t.thread_id: t for t in self._threads.values()}

    def _tail_file(self, path: Path, key: str) -> None:
        try:
            st = os.stat(path)
        except OSError:
            return

        cursor = self._cursors.get(key)
        if cursor is None or cursor.inode != st.st_ino or st.st_size < cursor.offset:
            # New file, rotated inode, or truncated file -> re-read cleanly from 0
            cursor = _FileCursor(inode=st.st_ino, offset=0, partial=b"")
            self._cursors[key] = cursor
            fallback_id = _extract_uuid_from_filename(path) or path.stem
            self._threads[key] = RolloutThreadState(
                file_path=key,
                thread_id=fallback_id,
                session_id=fallback_id,
            )

        if st.st_size == cursor.offset:
            return

        try:
            with open(path, "rb") as fh:
                fh.seek(cursor.offset)
                chunk = fh.read()
        except OSError:
            return

        if not chunk:
            return

        cursor.offset += len(chunk)
        buf = cursor.partial + chunk
        lines = buf.split(b"\n")
        if not buf.endswith(b"\n"):
            cursor.partial = lines.pop()
        else:
            cursor.partial = b""
            # Trailing empty item after final newline
            if lines and lines[-1] == b"":
                lines.pop()

        state = self._threads[key]
        for raw_line in lines:
            if not raw_line.strip():
                continue
            try:
                text = raw_line.decode("utf-8", errors="replace")
                record = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                self._apply_record(state, record)

    def _apply_record(self, state: RolloutThreadState, record: dict[str, Any]) -> None:
        ts_str = record.get("timestamp")
        ts_val = _parse_iso_ts(ts_str)
        if ts_val is not None:
            if state.started_ts is None or ts_val < state.started_ts:
                state.started_ts = ts_val
                state.started_at = ts_str
            if state.updated_ts is None or ts_val >= state.updated_ts:
                state.updated_ts = ts_val
                state.updated_at = ts_str

        rec_type = record.get("type")
        payload = record.get("payload")
        if not isinstance(payload, dict):
            if rec_type == "compacted":
                state.compacted_count += 1
            return

        if rec_type == "session_meta":
            tid = payload.get("id")
            sid = payload.get("session_id")
            if isinstance(tid, str) and tid.strip():
                state.thread_id = tid.strip().lower()
            if isinstance(sid, str) and sid.strip():
                state.session_id = sid.strip().lower()
            elif state.thread_id:
                state.session_id = state.thread_id

            state.is_subagent = state.thread_id != state.session_id
            cwd = payload.get("cwd")
            if isinstance(cwd, str) and cwd.strip():
                state.cwd = cwd.strip()
            orig = payload.get("originator")
            if isinstance(orig, str) and orig.strip():
                state.originator = sanitize_text(orig, 64)

            # Subagent metadata (top-level or nested source.subagent.thread_spawn)
            spawn_meta: dict[str, Any] = {}
            src = payload.get("source")
            if isinstance(src, dict):
                sub = src.get("subagent")
                if isinstance(sub, dict):
                    ts_meta = sub.get("thread_spawn")
                    if isinstance(ts_meta, dict):
                        spawn_meta = ts_meta

            parent_tid = payload.get("parent_thread_id") or spawn_meta.get("parent_thread_id")
            if isinstance(parent_tid, str) and parent_tid.strip():
                state.parent_thread_id = parent_tid.strip().lower()
                state.is_subagent = True

            nick = payload.get("agent_nickname") or spawn_meta.get("agent_nickname")
            if isinstance(nick, str) and nick.strip():
                state.agent_nickname = sanitize_text(nick, 64)

            role = payload.get("agent_role") or spawn_meta.get("agent_role")
            if isinstance(role, str) and role.strip():
                state.agent_role = sanitize_text(role, 48)

            apath = payload.get("agent_path") or spawn_meta.get("agent_path")
            if isinstance(apath, str) and apath.strip():
                state.agent_path = sanitize_text(apath, 120)

            meta_ts = payload.get("timestamp")
            meta_ts_val = _parse_iso_ts(meta_ts)
            if meta_ts_val is not None and (state.started_ts is None or meta_ts_val < state.started_ts):
                state.started_ts = meta_ts_val
                state.started_at = meta_ts
            return

        if rec_type == "turn_context":
            turn_id = payload.get("turn_id")
            if isinstance(turn_id, str) and turn_id.strip():
                state.current_turn_id = turn_id.strip()
            root_tid = payload.get("root_turn_id")
            if isinstance(root_tid, str) and root_tid.strip():
                state.root_turn_id = root_tid.strip()
            cwd = payload.get("cwd")
            if isinstance(cwd, str) and cwd.strip():
                state.cwd = cwd.strip()
            model = payload.get("model")
            if not model:
                cm = payload.get("collaboration_mode")
                if isinstance(cm, dict) and isinstance(cm.get("settings"), dict):
                    model = cm["settings"].get("model")
            if isinstance(model, str) and model.strip():
                state.model = sanitize_text(model, 80)
            effort = payload.get("effort")
            if isinstance(effort, str) and effort.strip():
                state.effort = sanitize_text(effort, 32)
            return

        if rec_type == "compacted":
            state.compacted_count += 1
            return

        if rec_type == "event_msg":
            ev_type = payload.get("type")
            if ev_type == "task_started":
                state.turn_active = True
                turn_id = payload.get("turn_id")
                if isinstance(turn_id, str) and turn_id.strip():
                    state.current_turn_id = turn_id.strip()
            elif ev_type in ("task_complete", "turn_aborted"):
                state.turn_active = False
            elif ev_type == "token_count":
                info = payload.get("info")
                if isinstance(info, dict):
                    last_usage = info.get("last_token_usage")
                    if isinstance(last_usage, dict):
                        last_tot = last_usage.get("total_tokens")
                        if isinstance(last_tot, (int, float)) and last_tot >= 0:
                            state.used_tokens = int(last_tot)
                            state.token_updated_at = ts_str
                    total_usage = info.get("total_token_usage")
                    if isinstance(total_usage, dict):
                        cum_tot = total_usage.get("total_tokens")
                        if isinstance(cum_tot, (int, float)) and cum_tot >= 0:
                            state.cumulative_tokens = int(cum_tot)
                    win = info.get("model_context_window")
                    if isinstance(win, (int, float)) and win > 0:
                        state.window_tokens = int(win)
            return

        if rec_type == "response_item":
            item_type = payload.get("type")
            if item_type in ("function_call", "custom_tool_call"):
                tool_name = sanitize_text(str(payload.get("name") or "tool"), 64)
                call_id = str(payload.get("call_id") or payload.get("id") or "")
                raw_args = payload.get("arguments") if item_type == "function_call" else payload.get("input")
                action_hint, safe_meta, plan_steps = _extract_safe_call_metadata(tool_name, raw_args)
                if plan_steps is not None:
                    state.plan_steps = plan_steps
                    state.plan_updated_at = ts_str
                rec = ToolCallRecord(
                    call_id=call_id,
                    name=tool_name,
                    action_hint=action_hint,
                    started_at=ts_str,
                    started_ts=ts_val,
                    status="running",
                    turn_id=state.current_turn_id,
                    project_id=safe_meta["project_id"],
                    contract_path=safe_meta["contract_path"],
                    run_id=safe_meta["run_id"],
                    analysis_group=safe_meta["analysis_group"],
                    target_paths=safe_meta["target_paths"],
                    target_summary=safe_meta["target_summary"],
                )
                state.add_tool_call(rec)
                state.turn_active = True
            elif item_type in ("function_call_output", "custom_tool_call_output"):
                call_id = str(payload.get("call_id") or payload.get("id") or "")
                if call_id:
                    status, spawned_uuid = _classify_tool_output(payload)
                    state.complete_tool_call(
                        call_id=call_id,
                        status=status,
                        ended_at=ts_str,
                        ended_ts=ts_val,
                        spawned_thread_id=spawned_uuid,
                    )
            return
