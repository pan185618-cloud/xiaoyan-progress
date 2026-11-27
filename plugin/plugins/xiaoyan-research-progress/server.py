#!/usr/bin/env python3
"""Production Read-Only Native Codex Sidebar MCP App Server for Xiaoyan Research Progress.

Strict Security & Read-Only Guarantees:
- Imports core.reduce_macro & core.reduce_micro directly in-process (zero writes to RESEARCH_ROOT
  or ~/.config/xiaoyan-r-runtime/).
- NEVER executes open_approval_terminal.py or any subprocess/shell command.
- NEVER exposes prompts, reasoning text, encrypted_content, raw tool outputs, or secrets.
- Only `update_progress_prefs` may atomically update user UI preferences at
  ~/.config/xy_progress/prefs.json via strict allowlisted keys.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Ensure xiaoyan-progress project root is importable
PROJECT_ROOT = Path("/Users/abc/Desktop/gpt/分析软件/codex改装/xiaoyan-progress")
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.config import (  # noqa: E402
    CODEX_SESSIONS_ROOT,
    MAGPIE_ROUTING_ROOT,
    PREFS_PATH,
    RESEARCH_ROOT,
    TMP_ROOT,
    load_prefs,
    save_prefs,
)
from core.reduce_macro import (  # noqa: E402
    build_macro_snapshot,
    get_approval_commands,
    get_project_detail,
)
from core.reduce_micro import MicroEngine  # noqa: E402

RESOURCE_URI = "ui://xiaoyan-progress/dashboard.html"
LIVE_PANEL_URI = "ui://xiaoyan-progress/live_panel.html"
RESOURCE_MIME = "text/html;profile=mcp-app"
DASHBOARD_HTML_PATH = Path(__file__).resolve().parent / "dashboard.html"
LIVE_PANEL_HTML_PATH = Path(__file__).resolve().parent / "live_panel.html"

MACRO_TTL_SEC = 5.0
ALLOWED_PREFS_LIST_KEYS = frozenset({"pinned_projects"})
ALLOWED_PREFS_DICT_KEYS = frozenset(
    {
        "primary_contract_overrides",
        "category_overrides",
        "session_project_overrides",
    }
)
ALLOWED_PREFS_KEYS = ALLOWED_PREFS_LIST_KEYS | ALLOWED_PREFS_DICT_KEYS

_TRIVIAL_CWDS = frozenset(
    {"/", "/Users", "/private", "/tmp", "/private/tmp", "/var", "/private/var"}
)
_META_CWD_KEYS = (
    "cwd",
    "workdir",
    "workingDirectory",
    "working_directory",
    "workspace",
    "workspacePath",
    "workspace_path",
    "projectPath",
    "project_path",
    "openai/cwd",
    "codex/cwd",
)
_META_THREAD_ID_KEYS = (
    "threadId",
    "thread_id",
    "sessionId",
    "session_id",
    "conversationId",
    "conversation_id",
    "openai/threadId",
    "openai/sessionId",
    "codex/thread_id",
    "codex/session_id",
)


def _normalize_dir_path(raw: str | None) -> str:
    if not raw or not isinstance(raw, str):
        return ""
    cleaned = raw.strip()
    if not cleaned:
        return ""
    try:
        resolved = str(Path(cleaned).expanduser().resolve())
    except OSError:
        resolved = os.path.normpath(cleaned)
    norm = unicodedata.normalize("NFC", resolved)
    if norm != "/":
        norm = norm.rstrip("/")
    return norm


def _resolve_server_cwd() -> str:
    try:
        raw = str(Path.cwd().resolve())
    except OSError:
        try:
            raw = os.getcwd()
        except OSError:
            return ""
    return _normalize_dir_path(raw) or raw


def _is_trivial_cwd(norm_path: str) -> bool:
    return (not norm_path) or (norm_path in _TRIVIAL_CWDS)


def _parse_iso_ts(ts: str | None) -> float:
    if not ts or not isinstance(ts, str):
        return 0.0
    try:
        val = ts.strip()
        if val.endswith("Z"):
            val = val[:-1] + "+00:00"
        return datetime.fromisoformat(val).timestamp()
    except ValueError:
        return 0.0


def _extract_call_meta_hints(params: Any) -> tuple[str | None, str | None]:
    """Safely inspect optional MCP tools/call params._meta for cwd / thread hints."""
    if not isinstance(params, dict):
        return None, None
    meta = params.get("_meta")
    if not isinstance(meta, dict):
        return None, None

    # Gather candidate dicts (top-level _meta plus shallow nested dicts like openai/codex)
    dicts_to_scan: list[dict[str, Any]] = [meta]
    for v in meta.values():
        if isinstance(v, dict):
            dicts_to_scan.append(v)

    cwd_hint: str | None = None
    thread_id_hint: str | None = None

    for d in dicts_to_scan:
        if cwd_hint is None:
            for key in _META_CWD_KEYS:
                val = d.get(key)
                if isinstance(val, str) and val.strip():
                    norm = _normalize_dir_path(val)
                    if not _is_trivial_cwd(norm):
                        cwd_hint = norm
                        break
        if thread_id_hint is None:
            for key in _META_THREAD_ID_KEYS:
                val = d.get(key)
                if isinstance(val, str) and val.strip():
                    thread_id_hint = val.strip()
                    break

    return cwd_hint, thread_id_hint


SERVER_CWD = _resolve_server_cwd()
HOME_DIR_NORM = _normalize_dir_path(str(Path.home()))
RESEARCH_ROOT_NORM = _normalize_dir_path(str(RESEARCH_ROOT))


class ProgressStateService:
    """In-process cached macro + incremental micro state holder."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._macro_cache: dict[str, Any] | None = None
        self._macro_ts: float = 0.0
        self._approval_commands_cache: list[dict[str, Any]] | None = None
        self._approval_commands_ts: float = 0.0
        self._tick: int = 0
        self._micro_engine = MicroEngine(
            research_root=RESEARCH_ROOT,
            codex_sessions_root=CODEX_SESSIONS_ROOT,
            magpie_routing_root=MAGPIE_ROUTING_ROOT,
            tmp_root=TMP_ROOT,
        )

    def invalidate_macro(self) -> None:
        with self._lock:
            self._macro_cache = None
            self._macro_ts = 0.0
            self._approval_commands_cache = None
            self._approval_commands_ts = 0.0

    def get_macro(self, force_refresh: bool = False) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            if (
                not force_refresh
                and self._macro_cache is not None
                and (now - self._macro_ts) < MACRO_TTL_SEC
            ):
                return self._macro_cache
        prefs = load_prefs(PREFS_PATH)
        snap = build_macro_snapshot(research_root=RESEARCH_ROOT, prefs=prefs)
        with self._lock:
            self._macro_cache = snap
            self._macro_ts = time.monotonic()
        return snap

    def get_approval_commands_cached(
        self,
        force_refresh: bool = False,
        prefs: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        now = time.monotonic()
        with self._lock:
            if (
                not force_refresh
                and self._approval_commands_cache is not None
                and (now - self._approval_commands_ts) < MACRO_TTL_SEC
            ):
                return list(self._approval_commands_cache)
        eff_prefs = prefs if prefs is not None else load_prefs(PREFS_PATH)
        cmds = get_approval_commands(research_root=RESEARCH_ROOT, prefs=eff_prefs)
        with self._lock:
            self._approval_commands_cache = cmds
            self._approval_commands_ts = time.monotonic()
        return list(cmds)

    def _build_thread_context(
        self,
        micro: dict[str, Any],
        prefs: dict[str, Any],
        force_refresh: bool = False,
        cwd_hint: str | None = None,
        thread_id_hint: str | None = None,
    ) -> dict[str, Any]:
        effective_cwd = cwd_hint if cwd_hint else SERVER_CWD
        norm_target = _normalize_dir_path(effective_cwd)
        target_is_trivial = _is_trivial_cwd(norm_target)

        correlator = self._micro_engine.correlator
        matched_proj = None if target_is_trivial else correlator.match_path(norm_target)
        matched_project_id = matched_proj.id if matched_proj is not None else None
        matched_project_name = matched_proj.name if matched_proj is not None else None

        sessions = micro.get("sessions") or []
        hint_tid = (
            thread_id_hint.strip().lower()
            if isinstance(thread_id_hint, str) and thread_id_hint.strip()
            else None
        )
        active_statuses = frozenset({"running", "blocked", "stalled"})

        # Priority (a): Sessions matching effective_cwd (exact or subdir, or vice versa for project subdir)
        cwd_matches: list[tuple[bool, dict[str, Any]]] = []
        if not target_is_trivial:
            for sess in sessions:
                sess_cwd = _normalize_dir_path(sess.get("cwd"))
                if _is_trivial_cwd(sess_cwd):
                    continue
                if sess_cwd == norm_target:
                    cwd_matches.append((True, sess))
                elif sess_cwd.startswith(norm_target + "/") and norm_target != HOME_DIR_NORM:
                    cwd_matches.append((False, sess))
                elif (
                    norm_target.startswith(sess_cwd + "/")
                    and sess_cwd not in (HOME_DIR_NORM, RESEARCH_ROOT_NORM)
                ):
                    if matched_proj is not None:
                        sess_proj = correlator.match_path(sess_cwd)
                        if sess_proj is not None and sess_proj.id == matched_proj.id:
                            cwd_matches.append((False, sess))
                    else:
                        cwd_matches.append((False, sess))

        cwd_matches.sort(
            key=lambda item: (
                0 if (hint_tid and str(item[1].get("session_id") or "").lower() == hint_tid) else 1,
                0 if item[1].get("status") in active_statuses else 1,
                0 if item[0] else 1,
                -_parse_iso_ts(item[1].get("updated_at")),
            )
        )

        # Priority (b): Sessions attributed to matched_project_id
        project_matches: list[dict[str, Any]] = []
        if matched_project_id is not None:
            project_matches = [
                sess for sess in sessions if sess.get("project_id") == matched_project_id
            ]
            project_matches.sort(
                key=lambda sess: (
                    0 if (hint_tid and str(sess.get("session_id") or "").lower() == hint_tid) else 1,
                    0 if sess.get("status") in active_statuses else 1,
                    -_parse_iso_ts(sess.get("updated_at")),
                )
            )

        matching_session_ids: list[str] = []
        seen_ids: set[str] = set()
        for _, sess in cwd_matches:
            sid = sess.get("session_id")
            if sid and sid not in seen_ids:
                seen_ids.add(sid)
                matching_session_ids.append(str(sid))
        for sess in project_matches:
            sid = sess.get("session_id")
            if sid and sid not in seen_ids:
                seen_ids.add(sid)
                matching_session_ids.append(str(sid))

        if cwd_matches:
            preferred_session_id = cwd_matches[0][1].get("session_id")
            match_reason = "cwd_exact"
        elif project_matches:
            preferred_session_id = project_matches[0].get("session_id")
            match_reason = "project_match"
        elif sessions:
            hint_sess = (
                next(
                    (
                        s.get("session_id")
                        for s in sessions
                        if str(s.get("session_id") or "").lower() == hint_tid
                    ),
                    None,
                )
                if hint_tid
                else None
            )
            preferred_session_id = (
                hint_sess or micro.get("active_session_id") or sessions[0].get("session_id")
            )
            match_reason = "fallback_global"
        else:
            preferred_session_id = None
            match_reason = "none"

        # Read-only approval command metadata scoped/sorted for matched project + global items
        all_cmds = self.get_approval_commands_cached(force_refresh=force_refresh, prefs=prefs)
        if matched_project_id is not None:
            scoped_cmds = [
                cmd for cmd in all_cmds if cmd.get("project_id") in (matched_project_id, None)
            ]
            scoped_cmds.sort(
                key=lambda cmd: (
                    0 if cmd.get("project_id") == matched_project_id else 1,
                    0 if cmd.get("executable") else 1,
                )
            )
        else:
            scoped_cmds = sorted(
                all_cmds,
                key=lambda cmd: (
                    0 if cmd.get("project_id") is None else 1,
                    0 if cmd.get("executable") else 1,
                ),
            )

        return {
            "server_cwd": effective_cwd,
            "matched_project_id": matched_project_id,
            "matched_project_name": matched_project_name,
            "preferred_session_id": preferred_session_id,
            "matching_session_ids": matching_session_ids,
            "match_reason": match_reason,
            "approval_commands": scoped_cmds,
        }

    def get_combined(
        self,
        force_refresh: bool = False,
        default_tab: str | None = None,
        cwd_hint: str | None = None,
        thread_id_hint: str | None = None,
    ) -> dict[str, Any]:
        prefs = load_prefs(PREFS_PATH)
        macro = self.get_macro(force_refresh=force_refresh)

        approvals_by_proj: dict[str, int] = {}
        for proj in macro.get("projects") or []:
            pid = proj.get("id")
            if pid:
                approvals_by_proj[str(pid)] = len(proj.get("pending_approvals") or [])
        global_pending = int((macro.get("summary") or {}).get("pending_approvals") or 0)

        with self._lock:
            self._tick += 1
            tick = self._tick
            micro = self._micro_engine.build_snapshot(
                prefs=prefs,
                approvals_by_project=approvals_by_proj,
                global_pending_approvals=global_pending,
            )

        thread_context = self._build_thread_context(
            micro=micro,
            prefs=prefs,
            force_refresh=force_refresh,
            cwd_hint=cwd_hint,
            thread_id_hint=thread_id_hint,
        )

        # Compact macro projects slightly for snappy MCP bridge transfer while preserving
        # all display fields, stages, gates, contracts summary, and exploratory track.
        compact_projects = []
        for p in macro.get("projects") or []:
            compact_projects.append(
                {
                    "id": p.get("id"),
                    "name": p.get("name"),
                    "dir_name": p.get("dir_name"),
                    "rel_path": p.get("rel_path"),
                    "category": p.get("category"),
                    "auto_category": p.get("auto_category"),
                    "layout": p.get("layout"),
                    "is_hybrid": bool(p.get("is_hybrid")),
                    "is_nested": bool(p.get("is_nested")),
                    "is_stub": bool(p.get("is_stub")),
                    "stub_target": p.get("stub_target"),
                    "pinned": bool(p.get("pinned")),
                    "hidden": bool(p.get("hidden")),
                    "primary_contract": p.get("primary_contract"),
                    "contracts_count": len(p.get("contracts") or []),
                    "stages": p.get("stages") or {},
                    "exploratory_track": p.get("exploratory_track") or {},
                    "stage_skip_alert": bool(p.get("stage_skip_alert")),
                    "stage_skip_reason": p.get("stage_skip_reason"),
                    "workflow_state_drift": p.get("workflow_state_drift") or {},
                    "pending_approvals": p.get("pending_approvals") or [],
                    "gates": p.get("gates") or {},
                    "evidence_summary": p.get("evidence_summary") or {},
                    "last_activity_at": p.get("last_activity_at"),
                }
            )

        out: dict[str, Any] = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "tick": tick,
            "macro": {
                "generated_at": macro.get("generated_at"),
                "summary": macro.get("summary") or {},
                "runtime_snapshots": macro.get("runtime_snapshots") or {},
                "projects": compact_projects,
            },
            "micro": micro,
            "prefs": prefs,
            "thread_context": thread_context,
        }
        if default_tab:
            out["default_tab"] = default_tab
        return out

    def get_detail(self, project_id: str) -> dict[str, Any] | None:
        prefs = load_prefs(PREFS_PATH)
        detail = get_project_detail(project_id, research_root=RESEARCH_ROOT, prefs=prefs)
        if detail is None:
            return None
        # Ensure approval commands are attached (read-only metadata only; never executed)
        if "approval_commands" not in detail:
            all_cmds = get_approval_commands(research_root=RESEARCH_ROOT, prefs=prefs)
            detail["approval_commands"] = [
                c for c in all_cmds if c.get("project_id") == detail.get("id")
            ]
        return detail

    def update_prefs(self, patch: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(patch, dict):
            raise ValueError("patch must be an object")
        unknown = set(patch.keys()) - ALLOWED_PREFS_KEYS
        if unknown:
            raise ValueError(f"Unsupported preference keys: {sorted(unknown)}")

        current = load_prefs(PREFS_PATH)
        for key in ALLOWED_PREFS_LIST_KEYS:
            if key in patch:
                val = patch[key]
                if not isinstance(val, list) or len(val) > 200 or not all(isinstance(x, str) for x in val):
                    raise ValueError(f"{key} must be a list of <=200 strings")
                current[key] = val
        for key in ALLOWED_PREFS_DICT_KEYS:
            if key in patch:
                val = patch[key]
                if not isinstance(val, dict):
                    raise ValueError(f"{key} must be an object mapping strings to strings")
                merged = dict(current.get(key) or {})
                for k, v in val.items():
                    if not isinstance(k, str) or not isinstance(v, str):
                        raise ValueError(f"{key} entries must be string -> string")
                    if v.strip() == "":
                        merged.pop(k, None)
                    else:
                        merged[k] = v.strip()
                current[key] = merged

        saved = save_prefs(current, PREFS_PATH)
        self.invalidate_macro()
        return saved


STATE = ProgressStateService()

TOOLS = [
    {
        "name": "open_research_dashboard",
        "title": "科研大盘",
        "description": "打开小研科研总览流程图（展示全部真实课题的 S0-S7 箭头流程、已完成/进行中/未完成分色板块、门禁与证据链）。",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        "annotations": {
            "title": "科研大盘",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
        "_meta": {
            "ui": {
                "resourceUri": RESOURCE_URI,
                "visibility": ["model", "app"],
            },
            "openai/ui": {
                "entrypoints": [{"type": "global"}],
                "preferredModelDisplayMode": "fullscreen",
            },
        },
    },
    {
        "name": "open_live_progress",
        "title": "实时进度",
        "description": "打开当前运行步骤实时面板（展示活跃 Codex/ChatGPT Work 会话的垂直箭头步骤流、子代理 120k 预算、上下文 180k/220k 水位与门禁状态）。",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        "annotations": {
            "title": "实时进度",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
        "_meta": {
            "ui": {
                "resourceUri": LIVE_PANEL_URI,
                "visibility": ["model", "app"],
            },
            "openai/ui": {
                "entrypoints": [{"type": "thread"}],
                "preferredModelDisplayMode": "inline",
            },
        },
    },
    {
        "name": "get_research_progress",
        "title": "获取科研进度快照",
        "description": "只读返回全部真实课题的宏观 S0-S7 状态快照与当前活跃会话的实时运行步骤快照。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "refresh": {
                    "type": "boolean",
                    "description": "是否强制跳过 5 秒宏观缓存立即重扫目录。",
                }
            },
            "additionalProperties": False,
        },
        "annotations": {
            "title": "获取科研进度快照",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
        "_meta": {
            "ui": {
                "visibility": ["model", "app"],
            }
        },
    },
    {
        "name": "get_project_detail",
        "title": "获取单个课题详情",
        "description": "只读返回指定 project_id 的完整合同列表、受控证据链（runs/validations/releases/EV-*）、门禁明细与可复制的终端审批命令。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "课题 ID（如 zw30-lep、environment-cataract、heatwave-aging）。",
                }
            },
            "required": ["project_id"],
            "additionalProperties": False,
        },
        "annotations": {
            "title": "获取单个课题详情",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
        "_meta": {
            "ui": {
                "visibility": ["model", "app"],
            }
        },
    },
    {
        "name": "update_progress_prefs",
        "title": "保存科研大盘视图偏好",
        "description": "仅更新 ~/.config/xy_progress/prefs.json 中的置顶课题、主线合同覆写、分类覆写或会话归属覆写，不改动任何科研项目文件。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pinned_projects": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "置顶课题 ID 列表。",
                },
                "primary_contract_overrides": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                    "description": "project_id -> contract_id 映射（空字符串表示清除覆写）。",
                },
                "category_overrides": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                    "description": "project_id -> category 映射。",
                },
                "session_project_overrides": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                    "description": "session_id -> project_id 映射。",
                },
            },
            "additionalProperties": False,
        },
        "annotations": {
            "title": "保存科研大盘视图偏好",
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
        "_meta": {
            "ui": {
                "visibility": ["app"],
            }
        },
    },
]

RESOURCES = [
    {
        "uri": RESOURCE_URI,
        "name": "xiaoyan-research-progress-dashboard",
        "title": "小研科研大盘流程图",
        "description": "单文件离线 MCP App：科研总览 S0-S7 箭头流程图 + 当前运行步骤实时面板。",
        "mimeType": RESOURCE_MIME,
        "_meta": {
            "ui": {
                "prefersBorder": False,
            }
        },
    },
    {
        "uri": LIVE_PANEL_URI,
        "name": "xiaoyan-research-progress-live-panel",
        "title": "小研实时进度侧边栏",
        "description": "单文件离线窄栏 MCP App：专为 Codex 右侧栏打造的当前对话实时步骤流、上下文水位、子代理与门禁面板。",
        "mimeType": RESOURCE_MIME,
        "_meta": {
            "ui": {
                "prefersBorder": False,
            }
        },
    },
]


def _load_dashboard_html() -> str:
    return DASHBOARD_HTML_PATH.read_text(encoding="utf-8")


def _load_live_panel_html() -> str:
    return LIVE_PANEL_HTML_PATH.read_text(encoding="utf-8")


def send(msg: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _tool_result(payload: dict[str, Any], summary_text: str | None = None, is_error: bool = False) -> dict[str, Any]:
    text = summary_text if summary_text is not None else json.dumps(payload, ensure_ascii=False)
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": payload,
        "isError": is_error,
    }


def handle_request(req: dict[str, Any]) -> None:
    req_id = req.get("id")
    method = req.get("method", "")
    params = req.get("params") or {}

    if req_id is None:
        return

    try:
        if method == "initialize":
            client_ver = params.get("protocolVersion") or "2025-06-18"
            send(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "protocolVersion": client_ver,
                        "capabilities": {
                            "tools": {"listChanged": False},
                            "resources": {"subscribe": False, "listChanged": False},
                        },
                        "serverInfo": {
                            "name": "xiaoyan-research-progress",
                            "version": "1.0.0",
                        },
                    },
                }
            )
            return

        if method == "ping":
            send({"jsonrpc": "2.0", "id": req_id, "result": {}})
            return

        if method == "tools/list":
            send({"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}})
            return

        if method == "resources/list":
            send({"jsonrpc": "2.0", "id": req_id, "result": {"resources": RESOURCES}})
            return

        if method == "resources/templates/list":
            send({"jsonrpc": "2.0", "id": req_id, "result": {"resourceTemplates": []}})
            return

        if method == "resources/read":
            uri = params.get("uri")
            if uri == RESOURCE_URI:
                send(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "contents": [
                                {
                                    "uri": RESOURCE_URI,
                                    "mimeType": RESOURCE_MIME,
                                    "text": _load_dashboard_html(),
                                }
                            ]
                        },
                    }
                )
            elif uri == LIVE_PANEL_URI:
                send(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "contents": [
                                {
                                    "uri": LIVE_PANEL_URI,
                                    "mimeType": RESOURCE_MIME,
                                    "text": _load_live_panel_html(),
                                }
                            ]
                        },
                    }
                )
            else:
                send(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {"code": -32602, "message": f"Unknown resource URI: {uri}"},
                    }
                )
            return

        if method == "tools/call":
            name = params.get("name")
            args = params.get("arguments") or {}
            cwd_hint, thread_id_hint = _extract_call_meta_hints(params)

            if name == "open_research_dashboard":
                data = STATE.get_combined(
                    force_refresh=False,
                    default_tab="macro",
                    cwd_hint=cwd_hint,
                    thread_id_hint=thread_id_hint,
                )
                send({"jsonrpc": "2.0", "id": req_id, "result": _tool_result(data)})
                return

            if name == "open_live_progress":
                data = STATE.get_combined(
                    force_refresh=False,
                    default_tab="live",
                    cwd_hint=cwd_hint,
                    thread_id_hint=thread_id_hint,
                )
                send({"jsonrpc": "2.0", "id": req_id, "result": _tool_result(data)})
                return

            if name == "get_research_progress":
                refresh = bool(args.get("refresh", False))
                data = STATE.get_combined(
                    force_refresh=refresh,
                    cwd_hint=cwd_hint,
                    thread_id_hint=thread_id_hint,
                )
                send({"jsonrpc": "2.0", "id": req_id, "result": _tool_result(data)})
                return

            if name == "get_project_detail":
                pid = str(args.get("project_id") or "").strip()
                if not pid:
                    send(
                        {
                            "jsonrpc": "2.0",
                            "id": req_id,
                            "result": _tool_result({"error": "project_id is required"}, is_error=True),
                        }
                    )
                    return
                detail = STATE.get_detail(pid)
                if detail is None:
                    send(
                        {
                            "jsonrpc": "2.0",
                            "id": req_id,
                            "result": _tool_result({"error": f"Project not found: {pid}"}, is_error=True),
                        }
                    )
                    return
                send({"jsonrpc": "2.0", "id": req_id, "result": _tool_result({"project": detail})})
                return

            if name == "update_progress_prefs":
                saved = STATE.update_prefs(args)
                send({"jsonrpc": "2.0", "id": req_id, "result": _tool_result({"ok": True, "prefs": saved})})
                return

            send(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32602, "message": f"Unknown tool: {name}"},
                }
            )
            return

        send(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"},
            }
        )
    except Exception as exc:
        sys.stderr.write(f"[xiaoyan-research-progress] error handling {method}: {exc}\n")
        send(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32603, "message": f"Internal error: {exc}"},
            }
        )


def main() -> None:
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as exc:
            sys.stderr.write(f"[xiaoyan-research-progress] JSON decode error: {exc}\n")
            continue
        handle_request(req)


if __name__ == "__main__":
    main()
