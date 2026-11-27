"""Micro live telemetry reducer producing the MicroSnapshot contract.

Combines:
- RolloutTailer (Codex session & subagent rollout-*.jsonl)
- MagpieTailer (Magpie routing logs joined on exact 36-char session UUID)
- HooksTmpReader ($TMPDIR hook gate caches & cmd_summary statuses)
- ProjectCorrelator (6-tier deterministic attribution)

Zero prompts, reasoning text, encrypted_content, raw tool outputs, or secrets
ever enter MicroSnapshot.
"""

from __future__ import annotations

import hashlib
import json
import shlex
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .correlation import ProjectCorrelator
from .sources.hooks_tmp import HooksTmpReader
from .sources.magpie import MagpieSessionHealth, MagpieTailer
from .sources.rollout import RolloutTailer, RolloutThreadState, ToolCallRecord, sanitize_text

MAIN_SOFT_WARN_TOKENS = 180_000
MAIN_HANDOFF_TARGET_TOKENS = 220_000
SUBAGENT_SOFT_WARN_TOKENS = 120_000
DEFAULT_WINDOW_TOKENS = 250_000
ACTIVE_IDLE_THRESHOLD_S = 180.0
MAX_SESSIONS_RETURNED = 30

# Map action_hint -> (friendly Chinese label, kind, role)
ACTION_LABELS: dict[str, tuple[str, str, str]] = {
    "update_plan": ("更新任务计划", "plan", "main"),
    "formal_run": ("执行受控 R 统计", "run", "executor"),
    "formal_seal": ("封存统计结果", "seal", "executor"),
    "formal_validate": ("独立复跑验证", "validate", "validator"),
    "evidence_publish": ("发布正式证据", "publish", "publisher"),
    "batch_formal_pipeline": ("批量受控流水线", "run", "executor"),
    "preflight": ("数据与全网格预检", "preflight", "executor"),
    "check_figure": ("图表出版质检", "figure_gate", "verifier"),
    "figure_gate": ("出图门禁检查", "figure_gate", "verifier"),
    "prose_mark": ("稿件去 AI 味处理", "prose_gate", "main"),
    "prose_gate": ("稿件门禁检查", "prose_gate", "verifier"),
    "open_approval_terminal": ("等待用户 TTY 终端签署", "approval", "main"),
    "sign_contract": ("合同签署检查", "approval", "main"),
    "approve_snapshots": ("运行时快照审批检查", "approval", "main"),
    "handoff_builder": ("生成阶段交接摘要", "handoff", "main"),
    "exploration_card": ("记录探索性实验卡片", "tool", "explorer"),
    "cmd_summary": ("运行受控命令摘要", "tool", "executor"),
    "spawn_agent": ("委派子代理协作", "agent", "subagent"),
    "invoke_subagent": ("委派子代理协作", "agent", "subagent"),
    "wait_agent": ("等待子代理完成", "agent", "main"),
    "send_message": ("协调子代理任务", "agent", "main"),
    "apply_patch": ("编辑课题文件", "tool", "main"),
    "write_file": ("写入课题文件", "tool", "main"),
    "edit_file": ("修改课题文件", "tool", "main"),
    "exec_command": ("执行工作区命令", "tool", "main"),
    "shell_command": ("执行工作区命令", "tool", "main"),
    "read_file": ("读取与核查文件", "tool", "explorer"),
    "view_image": ("核查图表缩略图", "figure_gate", "verifier"),
}

GROUPABLE_ACTIONS = frozenset(
    {"exec_command", "shell_command", "read_file", "apply_patch", "edit_file", "write_file", "view_image"}
)


def _classify_role(raw_role: str | None, fallback: str = "subagent") -> str:
    val = (raw_role or "").strip().lower()
    if not val:
        return fallback
    if "valid" in val:
        return "validator"
    if "publish" in val:
        return "publisher"
    if "exec" in val:
        return "executor"
    if "verif" in val or "review" in val or "audit" in val:
        return "verifier"
    if "explor" in val or "scout" in val:
        return "explorer"
    if "main" in val or "coord" in val:
        return "main"
    return fallback


def _map_tool_call(
    call: ToolCallRecord,
    *,
    is_latest: bool = False,
    now_ts: float | None = None,
) -> tuple[str, str, str, str]:
    hint = call.action_hint or call.name.lower()
    base_label, kind, role = ACTION_LABELS.get(hint, (f"调用工具 {sanitize_text(call.name, 32)}", "tool", "main"))
    title = base_label
    if call.target_summary and hint not in ("open_approval_terminal", "handoff_builder"):
        title = f"{base_label} · {call.target_summary}"
    elif call.analysis_group:
        title = f"{base_label} ({call.analysis_group})"

    status = call.status
    if hint == "open_approval_terminal":
        is_recent = (
            now_ts is not None
            and call.started_ts is not None
            and (now_ts - call.started_ts) <= 600.0
        )
        if status == "running" or (is_latest and is_recent):
            status = "waiting_approval"
    return sanitize_text(title, 120), kind, role, status


def _build_observed_steps(
    root: RolloutThreadState,
    subagents: list[RolloutThreadState],
    now_ts: float,
) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    recent_calls = root.tool_calls[-40:]
    sub_by_id = {s.thread_id: s for s in subagents}

    for idx, call in enumerate(recent_calls):
        if call.name == "update_plan":
            continue
        title, kind, role, status = _map_tool_call(
            call,
            is_latest=(idx == len(recent_calls) - 1),
            now_ts=now_ts,
        )
        elapsed_ms = call.duration_ms
        if elapsed_ms is None and status == "running" and call.started_ts is not None:
            elapsed_ms = max(0, int((now_ts - call.started_ts) * 1000))

        artifact_ref = call.run_id or call.contract_path or (call.target_paths[0] if call.target_paths else None)
        detail_parts: list[str] = []
        if call.analysis_group:
            detail_parts.append(f"分析组: {call.analysis_group}")
        if call.run_id:
            detail_parts.append(call.run_id)
        detail = " · ".join(detail_parts) if detail_parts else call.target_summary

        child_entry = {
            "id": call.call_id or f"call-{idx}",
            "title": title,
            "kind": kind,
            "role": role,
            "status": status,
            "elapsed_ms": elapsed_ms,
            "detail": detail,
            "artifact_ref": artifact_ref,
        }

        # Enrich spawn_agent steps with child subagent info
        if call.action_hint in ("spawn_agent", "invoke_subagent") and call.spawned_thread_id in sub_by_id:
            sub = sub_by_id[call.spawned_thread_id]
            sub_label = sub.agent_nickname or sub.agent_role or sub.thread_id[:8]
            title = f"委派子代理 · {sub_label}"
            role = _classify_role(sub.agent_role, "subagent")
            status = "running" if sub.turn_active else ("done" if sub.tool_calls else status)

        # Group consecutive generic tool calls of the same action_hint for readability
        if (
            steps
            and call.action_hint in GROUPABLE_ACTIONS
            and steps[-1].get("_group_key") == call.action_hint
            and len(steps[-1]["children"]) < 12
        ):
            prev = steps[-1]
            prev["children"].append(child_entry)
            cnt = len(prev["children"])
            base_label = ACTION_LABELS.get(call.action_hint, (call.name, "tool", "main"))[0]
            prev["title"] = f"{base_label} ({cnt} 次)"
            if status in ("running", "blocked", "failed", "waiting_approval"):
                prev["status"] = status
            elif prev["status"] == "running" and status == "done":
                prev["status"] = "done"
            if elapsed_ms is not None:
                prev["elapsed_ms"] = (prev["elapsed_ms"] or 0) + elapsed_ms
            if artifact_ref:
                prev["artifact_ref"] = artifact_ref
        else:
            step_obj: dict[str, Any] = {
                "id": call.call_id or f"step-{idx}",
                "title": title,
                "kind": kind,
                "role": role,
                "status": status,
                "elapsed_ms": elapsed_ms,
                "detail": detail,
                "artifact_ref": artifact_ref,
                "children": [child_entry] if call.action_hint in GROUPABLE_ACTIONS else [],
                "_group_key": call.action_hint if call.action_hint in GROUPABLE_ACTIONS else None,
            }
            steps.append(step_obj)

    # Strip internal grouping key and cap to latest 16 steps for HUD crispness
    trimmed = steps[-16:]
    for st in trimmed:
        st.pop("_group_key", None)
    return trimmed


def _build_plan_steps(
    root: RolloutThreadState,
    observed_steps: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    running_idx: int | None = None
    for idx, item in enumerate(root.plan_steps):
        st_status = item.get("status") or "pending"
        if st_status == "running" and running_idx is None:
            running_idx = idx
        steps.append(
            {
                "id": f"plan-{idx}",
                "title": item.get("title") or f"步骤 {idx + 1}",
                "kind": "plan",
                "role": "main",
                "status": st_status,
                "elapsed_ms": None,
                "detail": None,
                "artifact_ref": None,
                "children": [],
            }
        )
    # Attach last few observed actions under the active plan item for drill-down context
    if steps and observed_steps:
        target = running_idx if running_idx is not None else len(steps) - 1
        steps[target]["children"] = [
            {
                "id": obs["id"],
                "title": obs["title"],
                "kind": obs["kind"],
                "role": obs["role"],
                "status": obs["status"],
                "elapsed_ms": obs["elapsed_ms"],
                "detail": obs["detail"],
                "artifact_ref": obs["artifact_ref"],
            }
            for obs in observed_steps[-6:]
        ]
    return steps


def _build_context_health(
    root: RolloutThreadState,
    magpie: MagpieSessionHealth | None,
    project_rel_or_name: str | None,
) -> dict[str, Any]:
    source = "none"
    used_tokens = 0
    if root.used_tokens is not None:
        used_tokens = root.used_tokens
        source = "rollout"
    elif magpie is not None and magpie.fallback_prompt_tokens is not None:
        used_tokens = magpie.fallback_prompt_tokens
        source = "magpie_fallback"

    cumulative_tokens = root.cumulative_tokens if root.cumulative_tokens is not None else used_tokens
    window_tokens = (
        root.window_tokens
        or (magpie.fallback_window_tokens if magpie is not None else None)
        or DEFAULT_WINDOW_TOKENS
    )
    pct = round((used_tokens / window_tokens) * 100.0, 1) if window_tokens > 0 else 0.0
    if used_tokens >= MAIN_HANDOFF_TARGET_TOKENS or pct >= 85.0:
        level = "critical"
    elif used_tokens >= MAIN_SOFT_WARN_TOKENS or pct >= 70.0:
        level = "warn"
    else:
        level = "ok"

    handoff_cmd: str | None = None
    if project_rel_or_name:
        target_arg = project_rel_or_name
        if target_arg.startswith("projects/"):
            target_arg = target_arg[len("projects/") :]
        handoff_cmd = f"python3 tools/handoff_builder.py {shlex.quote(target_arg)} --topic checkpoint"

    return {
        "used_tokens": used_tokens,
        "cumulative_tokens": cumulative_tokens,
        "window_tokens": window_tokens,
        "pct": pct,
        "soft_warn_tokens": MAIN_SOFT_WARN_TOKENS,
        "handoff_target_tokens": MAIN_HANDOFF_TARGET_TOKENS,
        "level": level,
        "source": source,
        "compacted_count": root.compacted_count,
        "handoff_cmd": handoff_cmd,
    }


def _build_subagent_entry(sub: RolloutThreadState, now_ts: float) -> dict[str, Any]:
    used = sub.used_tokens or 0
    win = sub.window_tokens or DEFAULT_WINDOW_TOKENS
    if used >= SUBAGENT_SOFT_WARN_TOKENS:
        level = "critical"
    elif used >= int(SUBAGENT_SOFT_WARN_TOKENS * 0.8):
        level = "warn"
    else:
        level = "ok"

    last_call = sub.tool_calls[-1] if sub.tool_calls else None
    current_step = "等待指令"
    if last_call is not None:
        current_step, _, _, _ = _map_tool_call(last_call)

    is_recent = sub.updated_ts is not None and (now_ts - sub.updated_ts) <= ACTIVE_IDLE_THRESHOLD_S
    status = "running" if (sub.turn_active and is_recent) else "idle"
    if last_call is not None and last_call.status in ("failed", "blocked") and not sub.turn_active:
        status = last_call.status

    elapsed_ms: int | None = None
    if sub.started_ts is not None:
        end_ts = now_ts if status == "running" else (sub.updated_ts or now_ts)
        if end_ts >= sub.started_ts:
            elapsed_ms = int((end_ts - sub.started_ts) * 1000)

    return {
        "thread_id": sub.thread_id,
        "nickname": sub.agent_nickname or sub.agent_role or sub.thread_id[:8],
        "role": _classify_role(sub.agent_role, "subagent"),
        "status": status,
        "current_step": current_step,
        "used_tokens": used,
        "cumulative_tokens": sub.cumulative_tokens or used,
        "window_tokens": win,
        "soft_warn_tokens": SUBAGENT_SOFT_WARN_TOKENS,
        "level": level,
        "elapsed_ms": elapsed_ms,
        "updated_at": sub.updated_at,
    }


def _default_upstream() -> dict[str, Any]:
    return {
        "last_status": None,
        "ttft_ms": None,
        "latency_ms": None,
        "retries_429": 0,
        "streak_504_413": 0,
        "stalled_over_10m": False,
        "stall_alert": False,
        "alert_level": "ok",
        "alert_message": None,
        "model": None,
        "served": None,
        "provider": None,
        "last_time": None,
        "request_count": 0,
        "subagent_request_count": 0,
    }


class MicroEngine:
    """Stateful incremental telemetry reducer for live Codex/Magpie/hook monitoring."""

    def __init__(
        self,
        research_root: Path,
        codex_sessions_root: Path,
        magpie_routing_root: Path,
        tmp_root: Path,
        days_back: int = 1,
    ) -> None:
        self.research_root = Path(research_root)
        self.rollout_tailer = RolloutTailer(sessions_root=codex_sessions_root, days_back=days_back)
        self.magpie_tailer = MagpieTailer(routing_root=magpie_routing_root, days_back=days_back)
        self.hooks_reader = HooksTmpReader(tmp_root=tmp_root)
        self.correlator = ProjectCorrelator(research_root=self.research_root)
        self.epoch = uuid.uuid4().hex[:8]
        self.seq = 0
        self._last_fingerprint: str | None = None

    def build_snapshot(
        self,
        prefs: dict[str, Any] | None = None,
        approvals_by_project: dict[str, int] | None = None,
        global_pending_approvals: int = 0,
        now: datetime | None = None,
        force_reload_catalog: bool = False,
    ) -> dict[str, Any]:
        now_dt = now or datetime.now(timezone.utc)
        now_ts = now_dt.timestamp()
        self.correlator.reload(force=force_reload_catalog)

        threads = self.rollout_tailer.poll(now=now_dt)
        magpie_map = self.magpie_tailer.poll(now=now_dt)
        hooks_data = self.hooks_reader.read_all()

        # Group threads by root session_id
        roots: dict[str, RolloutThreadState] = {}
        subagents_by_session: dict[str, list[RolloutThreadState]] = {}

        for th in threads.values():
            sid = th.session_id
            if not th.is_subagent and th.thread_id == sid:
                roots[sid] = th
            else:
                subagents_by_session.setdefault(sid, []).append(th)

        # Ensure orphan subagents (whose parent rollout fell outside date window) still appear
        for sid, subs in subagents_by_session.items():
            if sid not in roots:
                first_sub = sorted(subs, key=lambda s: s.started_ts or 0.0)[0]
                roots[sid] = RolloutThreadState(
                    file_path="",
                    thread_id=sid,
                    session_id=sid,
                    cwd=first_sub.cwd,
                    originator=first_sub.originator,
                    model=first_sub.model,
                    started_at=first_sub.started_at,
                    started_ts=first_sub.started_ts,
                    updated_at=max((s.updated_at for s in subs if s.updated_at), default=None),
                    updated_ts=max((s.updated_ts for s in subs if s.updated_ts is not None), default=None),
                    turn_active=any(s.turn_active for s in subs),
                )

        # Pre-compute attributions (top-level sessions first, then any inherited)
        attributions: dict[str, Any] = {}
        for sid, root in roots.items():
            subs = subagents_by_session.get(sid, [])
            parent_pid = None
            if root.parent_thread_id and root.parent_thread_id in attributions:
                parent_pid = attributions[root.parent_thread_id].project_id
            attributions[sid] = self.correlator.attribute_session(
                root_thread=root,
                subagent_threads=subs,
                prefs=prefs,
                parent_project_id=parent_pid,
            )

        fig_gate = hooks_data["figure_gate"]
        prose_gate = hooks_data["prose_gate"]
        wf_guard = hooks_data["workflow_guard"]
        approvals_map = approvals_by_project or {}

        sessions_out: list[dict[str, Any]] = []
        for sid, root in roots.items():
            subs = sorted(
                subagents_by_session.get(sid, []),
                key=lambda s: s.updated_ts or 0.0,
                reverse=True,
            )
            attr = attributions[sid]
            magpie = magpie_map.get(sid)  # Exact 36-char UUID join!
            upstream = magpie.to_upstream_dict(now_ts=now_ts) if magpie is not None else _default_upstream()

            observed_steps = _build_observed_steps(root, subs, now_ts)
            if root.plan_steps:
                plan_source = "update_plan"
                steps = _build_plan_steps(root, observed_steps)
            else:
                plan_source = "observed"
                steps = observed_steps

            steps_total = len(steps)
            steps_done = sum(1 for st in steps if st["status"] == "done")
            current_step_title = "等待新指令"
            for st in reversed(steps):
                if st["status"] in ("running", "waiting_approval", "blocked", "failed"):
                    current_step_title = st["title"]
                    break
            else:
                if steps:
                    current_step_title = steps[-1]["title"]

            subagent_entries = [_build_subagent_entry(s, now_ts) for s in subs[:20]]
            context_health = _build_context_health(
                root=root,
                magpie=magpie,
                project_rel_or_name=attr.project_rel_path or attr.project_name,
            )

            # Session-specific gate signals from $TMPDIR
            fig_blocks = int(fig_gate["session_counts"].get(sid, 0))
            prose_blocks = int(prose_gate["session_counts"].get(sid, 0))
            prose_hard = prose_gate["hard_states"].get(sid) or {}
            hard_total_blocks = int(prose_hard.get("total_blocks", 0))
            polling_count = int(wf_guard["polling_counts"].get(sid, 0))

            fig_status = "blocked" if fig_blocks > 0 else ("pass" if fig_gate["pass_count"] > 0 else "ok")
            prose_status = (
                "blocked"
                if (hard_total_blocks > 0 or prose_blocks > 0)
                else ("pass" if prose_gate["pass_count"] > 0 else "ok")
            )
            proj_approvals = (
                approvals_map.get(attr.project_id, 0) if attr.project_id else global_pending_approvals
            )

            gates = {
                "figure": {
                    "status": fig_status,
                    "session_blocks": fig_blocks,
                    "pass_count": fig_gate["pass_count"],
                    "fail_count": fig_gate["fail_count"],
                },
                "prose": {
                    "status": prose_status,
                    "session_blocks": prose_blocks,
                    "circuit_breaker_blocks": hard_total_blocks,
                    "pass_count": prose_gate["pass_count"],
                    "fail_count": prose_gate["fail_count"],
                },
                "approvals_pending": proj_approvals,
                "polling_count": polling_count,
            }

            # Effective updated timestamp across root, subagents, and Magpie
            eff_updated_ts = max(
                [
                    ts
                    for ts in [
                        root.updated_ts,
                        *(s.updated_ts for s in subs),
                        magpie.last_ts if magpie is not None else None,
                    ]
                    if ts is not None
                ],
                default=0.0,
            )
            eff_updated_at = (
                datetime.fromtimestamp(eff_updated_ts, tz=timezone.utc).isoformat()
                if eff_updated_ts > 0
                else root.updated_at
            )

            # Derive session status
            any_running_step = any(st["status"] == "running" for st in steps)
            any_running_sub = any(s["status"] == "running" for s in subagent_entries)
            any_blocked_step = any(st["status"] in ("blocked", "waiting_approval") for st in steps)
            is_fresh = eff_updated_ts > 0 and (now_ts - eff_updated_ts) <= ACTIVE_IDLE_THRESHOLD_S

            if upstream["stalled_over_10m"] or upstream["streak_504_413"] >= 3:
                sess_status = "stalled"
            elif hard_total_blocks > 0 or any_blocked_step:
                sess_status = "blocked"
            elif (root.turn_active or any_running_step or any_running_sub) and is_fresh:
                sess_status = "running"
            elif is_fresh and magpie is not None and not magpie.last_done:
                sess_status = "running"
            else:
                sess_status = "idle"

            title = attr.project_name or (f"工作区会话 · {sid[:8]}")
            model_display = root.model or upstream.get("model") or upstream.get("served")

            sessions_out.append(
                {
                    "session_id": sid,
                    "title": title,
                    "project_id": attr.project_id,
                    "project_name": attr.project_name,
                    "cwd": root.cwd,
                    "attribution_confidence": attr.confidence,
                    "attribution_source": attr.source,
                    "candidate_projects": attr.candidate_projects,
                    "originator": root.originator or "codex",
                    "model": model_display,
                    "status": sess_status,
                    "started_at": root.started_at,
                    "updated_at": eff_updated_at,
                    "_sort_ts": eff_updated_ts,
                    "current_step_title": current_step_title,
                    "steps_done": steps_done,
                    "steps_total": steps_total,
                    "plan_source": plan_source,
                    "steps": steps,
                    "subagents": subagent_entries,
                    "context_health": context_health,
                    "upstream": upstream,
                    "gates": gates,
                }
            )

        status_rank = {"stalled": 0, "blocked": 1, "running": 2, "idle": 3}
        sessions_out.sort(key=lambda s: (status_rank.get(s["status"], 9), -(s["_sort_ts"] or 0.0)))
        sessions_out = sessions_out[:MAX_SESSIONS_RETURNED]
        for s in sessions_out:
            s.pop("_sort_ts", None)

        active_sessions = [s for s in sessions_out if s["status"] in ("running", "blocked", "stalled")]
        active_session_id = (
            active_sessions[0]["session_id"]
            if active_sessions
            else (sessions_out[0]["session_id"] if sessions_out else None)
        )

        # Fingerprint for monotonic seq bump only when meaningful state changed
        fp_payload = json.dumps(
            {
                "active": active_session_id,
                "sessions": [
                    (
                        s["session_id"],
                        s["project_id"],
                        s["status"],
                        s["updated_at"],
                        s["steps_done"],
                        s["steps_total"],
                        s["context_health"]["used_tokens"],
                        s["upstream"]["last_status"],
                        s["upstream"]["streak_504_413"],
                    )
                    for s in sessions_out
                ],
                "cmds": len(hooks_data["cmd_summaries"]),
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        fp = hashlib.sha1(fp_payload.encode("utf-8")).hexdigest()
        if fp != self._last_fingerprint:
            self.seq += 1
            self._last_fingerprint = fp

        return {
            "generated_at": now_dt.isoformat(),
            "epoch": self.epoch,
            "seq": self.seq,
            "active_session_id": active_session_id,
            "active_count": len(active_sessions),
            "sessions": sessions_out,
            "hooks_summary": {
                "figure_gate": {
                    "present": fig_gate["present"],
                    "pass_count": fig_gate["pass_count"],
                    "fail_count": fig_gate["fail_count"],
                },
                "prose_gate": {
                    "present": prose_gate["present"],
                    "pass_count": prose_gate["pass_count"],
                    "fail_count": prose_gate["fail_count"],
                    "hard_blocks": sum(
                        int(v.get("total_blocks", 0)) for v in prose_gate["hard_states"].values()
                    ),
                },
                "workflow_guard": {
                    "present": wf_guard["present"],
                    "total_polls": sum(wf_guard["polling_counts"].values()),
                },
            },
            "recent_commands": hooks_data["cmd_summaries"][:12],
        }
