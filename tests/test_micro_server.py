"""Comprehensive tests for Micro Live Telemetry Engine, FastAPI Loopback/SSE Server, and CLI."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from cli import main as cli_main
from core.correlation import ProjectCorrelator
from core.reduce_micro import MicroEngine
from core.sources.hooks_tmp import HooksTmpReader
from core.sources.magpie import MagpieTailer
from core.sources.rollout import RolloutTailer
from server.app import create_app

ROOT_SID = "01a21c70-4572-7c90-bbeb-80ae9a74f831"
SUB_TID = "01a21c70-aaaa-7c90-bbeb-80ae9a74f999"
OTHER_SID = "01a21c70-1111-7c90-bbeb-80ae9a74f000"


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as fh:
        for rec in records:
            fh.write((json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8"))


def _make_fake_research_repo(base: Path) -> Path:
    repo = base / "科研工作流"
    (repo / "projects" / "热浪暴露和衰老" / "99_audit" / "delegation").mkdir(parents=True)
    (repo / "projects" / "环境与白内障" / "04_合同").mkdir(parents=True)
    (repo / "projects" / "刘芸芳" / "刘芸芳charls课题" / "04_合同").mkdir(parents=True)
    (repo / "integrations" / "codex_multi_model").mkdir(parents=True)
    (repo / "integrations" / "codex_multi_model" / "project_roots.json").write_text(
        json.dumps(
            {
                "version": "1.0",
                "projects": {
                    "heatwave-aging": "projects/热浪暴露和衰老",
                    "environment-cataract": "projects/环境与白内障",
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return repo


# ---------------------------------------------------------------------------
# 1. Incremental Rollout Parsing Tests
# ---------------------------------------------------------------------------


def test_rollout_post_compaction_tokens_null_window_subagent_plan_and_no_leak(tmp_path: Path) -> None:
    now = datetime(2026, 11, 27, 12, 0, 0, tzinfo=timezone.utc)
    day_dir = tmp_path / "sessions" / "2026" / "11" / "27"
    root_file = day_dir / f"rollout-2026-11-27T11-50-00-{ROOT_SID}.jsonl"
    sub_file = day_dir / f"rollout-2026-11-27T11-55-00-{SUB_TID}.jsonl"

    secret_token = "sk-SUPERSECRETKEY1234567890ABCDEF"
    fernet_secret = "gAAAAABnSecretEncryptedPayload1234567890"
    user_prompt_secret = "PRIVATE_USER_PROMPT_DO_NOT_LEAK"
    reasoning_secret = "PRIVATE_CHAIN_OF_THOUGHT_DO_NOT_LEAK"

    _write_jsonl(
        root_file,
        [
            {
                "timestamp": "2026-11-27T11:50:00Z",
                "type": "session_meta",
                "payload": {
                    "id": ROOT_SID,
                    "session_id": ROOT_SID,
                    "cwd": "/tmp/repo/projects/热浪暴露和衰老",
                    "originator": "codex_exec",
                    "timestamp": "2026-11-27T11:50:00Z",
                },
            },
            {
                "timestamp": "2026-11-27T11:50:01Z",
                "type": "turn_context",
                "payload": {
                    "turn_id": "turn-1",
                    "root_turn_id": "turn-1",
                    "cwd": "/tmp/repo/projects/热浪暴露和衰老",
                    "model": "gpt-6.2-sol",
                    "effort": "high",
                },
            },
            # Must NEVER be extracted or leaked
            {
                "timestamp": "2026-11-27T11:50:02Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": user_prompt_secret}],
                },
            },
            {
                "timestamp": "2026-11-27T11:50:03Z",
                "type": "response_item",
                "payload": {
                    "type": "reasoning",
                    "summary": [{"text": reasoning_secret}],
                    "encrypted_content": fernet_secret,
                },
            },
            # Normal token_count establishing model_context_window
            {
                "timestamp": "2026-11-27T11:51:00Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": {
                            "input_tokens": 40000,
                            "output_tokens": 500,
                            "total_tokens": 40500,
                        },
                        "total_token_usage": {"total_tokens": 40500},
                        "model_context_window": 248900,
                    },
                },
            },
            # Post-compaction synthetic snapshot: input=0, output=0, total_tokens=119276, window=None!
            {
                "timestamp": "2026-11-27T11:55:00Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": {
                            "input_tokens": 0,
                            "output_tokens": 0,
                            "total_tokens": 119276,
                        },
                        "total_token_usage": {"total_tokens": 185000},
                        "model_context_window": None,
                    },
                },
            },
            # update_plan call
            {
                "timestamp": "2026-11-27T11:56:00Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "call_id": "call-plan-1",
                    "name": "update_plan",
                    "arguments": json.dumps(
                        {
                            "plan": [
                                {"step": "数据预检与清洗", "status": "completed"},
                                {"step": f"受控统计 {secret_token}", "status": "in_progress"},
                                {"step": "图表出版质检", "status": "pending"},
                            ]
                        }
                    ),
                },
            },
            {
                "timestamp": "2026-11-27T11:56:01Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "call-plan-1",
                    "output": f"ok {secret_token} {fernet_secret}",
                },
            },
            # formal_run tool call with secret in unrelated raw output
            {
                "timestamp": "2026-11-27T11:57:00Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "call_id": "call-run-1",
                    "name": "formal_run",
                    "arguments": json.dumps(
                        {
                            "project_id": "heatwave-aging",
                            "analysis_group": "primary_cox",
                            "run_id": "run_20261127_01",
                            "api_key": secret_token,
                        }
                    ),
                },
            },
            {
                "timestamp": "2026-11-27T11:57:10Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "call-run-1",
                    "output": json.dumps({"ok": True, "secret": secret_token}),
                },
            },
        ],
    )

    _write_jsonl(
        sub_file,
        [
            {
                "timestamp": "2026-11-27T11:55:10Z",
                "type": "session_meta",
                "payload": {
                    "id": SUB_TID,
                    "session_id": ROOT_SID,
                    "parent_thread_id": ROOT_SID,
                    "agent_nickname": "r-validator",
                    "agent_role": "validator",
                    "agent_path": "/root/r-validator",
                    "cwd": "/tmp/repo/projects/热浪暴露和衰老",
                },
            },
            {
                "timestamp": "2026-11-27T11:58:00Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 98000},
                        "total_token_usage": {"total_tokens": 110000},
                        "model_context_window": 248900,
                    },
                },
            },
        ],
    )

    tailer = RolloutTailer(sessions_root=tmp_path / "sessions", days_back=1)
    threads = tailer.poll(now=now)
    assert ROOT_SID in threads
    assert SUB_TID in threads

    root = threads[ROOT_SID]
    assert root.is_subagent is False
    # Exact verified post-compaction occupancy & retained window
    assert root.used_tokens == 119276
    assert root.cumulative_tokens == 185000
    assert root.window_tokens == 248900
    assert len(root.plan_steps) == 3
    assert [p["status"] for p in root.plan_steps] == ["done", "running", "pending"]
    assert secret_token not in root.plan_steps[1]["title"]

    sub = threads[SUB_TID]
    assert sub.is_subagent is True
    assert sub.session_id == ROOT_SID
    assert sub.parent_thread_id == ROOT_SID
    assert sub.agent_nickname == "r-validator"
    assert sub.used_tokens == 98000

    # Ensure zero leakage in reduced MicroSnapshot JSON
    repo = _make_fake_research_repo(tmp_path)
    engine = MicroEngine(
        research_root=repo,
        codex_sessions_root=tmp_path / "sessions",
        magpie_routing_root=tmp_path / "magpie",
        tmp_root=tmp_path / "tmp",
    )
    snap = engine.build_snapshot(now=now)
    dumped = json.dumps(snap, ensure_ascii=False)
    for forbidden in (secret_token, fernet_secret, user_prompt_secret, reasoning_secret):
        assert forbidden not in dumped

    assert len(snap["sessions"]) == 1
    sess = snap["sessions"][0]
    assert sess["session_id"] == ROOT_SID
    assert sess["plan_source"] == "update_plan"
    assert sess["context_health"]["used_tokens"] == 119276
    assert sess["context_health"]["window_tokens"] == 248900
    assert len(sess["subagents"]) == 1
    assert sess["subagents"][0]["thread_id"] == SUB_TID
    assert sess["subagents"][0]["soft_warn_tokens"] == 120000


def test_rollout_partial_utf8_and_truncation_recovery(tmp_path: Path) -> None:
    now = datetime(2026, 11, 27, 12, 0, 0, tzinfo=timezone.utc)
    day_dir = tmp_path / "sessions" / "2026" / "11" / "27"
    day_dir.mkdir(parents=True)
    f = day_dir / f"rollout-2026-11-27T11-00-00-{ROOT_SID}.jsonl"

    line1 = json.dumps(
        {
            "timestamp": "2026-11-27T11:00:00Z",
            "type": "session_meta",
            "payload": {"id": ROOT_SID, "session_id": ROOT_SID, "cwd": "/tmp/热浪"},
        },
        ensure_ascii=False,
    ).encode("utf-8") + b"\n"

    line2 = json.dumps(
        {
            "timestamp": "2026-11-27T11:01:00Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": {"total_tokens": 55555},
                    "total_token_usage": {"total_tokens": 55555},
                    "model_context_window": 250000,
                },
            },
        },
        ensure_ascii=False,
    ).encode("utf-8") + b"\n"

    # Write line1 + partial half of line2 (mid multi-byte or incomplete JSON)
    split_at = len(line2) // 2
    f.write_bytes(line1 + line2[:split_at])

    tailer = RolloutTailer(sessions_root=tmp_path / "sessions", days_back=1)
    t1 = tailer.poll(now=now)
    assert t1[ROOT_SID].used_tokens is None

    # Append remainder of line2
    with open(f, "ab") as fh:
        fh.write(line2[split_at:])
    t2 = tailer.poll(now=now)
    assert t2[ROOT_SID].used_tokens == 55555

    # Now truncate file and replace with smaller updated content (truncation recovery)
    short_line = json.dumps(
        {
            "timestamp": "2026-11-27T11:05:00Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": {"total_tokens": 12345},
                    "model_context_window": 250000,
                },
            },
        }
    ).encode("utf-8") + b"\n"
    f.write_bytes(short_line)
    t3 = tailer.poll(now=now)
    assert t3[ROOT_SID].used_tokens == 12345


# ---------------------------------------------------------------------------
# 2. Magpie Exact 36-Char UUID Join, 429 & 504 Streak, >10m Stall Tests
# ---------------------------------------------------------------------------


def test_magpie_exact_uuid_join_subagent_and_alerts(tmp_path: Path) -> None:
    now = datetime(2026, 11, 27, 12, 20, 0, tzinfo=timezone.utc)
    magpie_dir = tmp_path / "magpie"
    magpie_file = magpie_dir / "2026-11-27.jsonl"

    _write_jsonl(
        magpie_file,
        [
            # Ephemeral background service -> MUST be ignored
            {
                "time": "2026-11-27T12:00:00Z",
                "agent": "codex",
                "kind": "memory_consolidation",
                "session": OTHER_SID,
                "status": 504,
                "done": True,
            },
            # Claude agent -> MUST be ignored
            {
                "time": "2026-11-27T12:00:01Z",
                "agent": "claude",
                "session": OTHER_SID,
                "status": 504,
                "done": True,
            },
            # Parent session normal 200 with 429 retries inside tries[]
            {
                "time": "2026-11-27T12:01:00Z",
                "agent": "codex",
                "session": ROOT_SID,
                "provider": "openai",
                "model": "gpt-6.2-sol",
                "served": "gpt-6.2-sol",
                "status": 200,
                "done": True,
                "ms": 4200,
                "ttft": 850,
                "tries": [
                    {"status": 429, "ms": 300},
                    {"status": 429, "ms": 400},
                    {"status": 200, "ms": 3500},
                ],
                "prompt": {"tokens": 185000, "window": 250000},
            },
            # Subagent request sharing parent ROOT_SID + consecutive 504s
            {
                "time": "2026-11-27T12:05:00Z",
                "agent": "codex",
                "session": ROOT_SID,
                "sub": "executor",
                "subagentId": SUB_TID,
                "provider": "qianfan",
                "model": "group/codex-sub",
                "status": 504,
                "done": True,
                "ms": 60000,
                "tries": [{"status": 504, "ms": 60000}],
            },
            {
                "time": "2026-11-27T12:06:00Z",
                "agent": "codex",
                "session": ROOT_SID,
                "sub": "executor",
                "status": 413,
                "done": True,
                "ms": 1200,
                "tries": [{"status": 413, "ms": 1200}],
            },
            {
                "time": "2026-11-27T12:07:00Z",
                "agent": "codex",
                "session": ROOT_SID,
                "status": 504,
                "done": False,  # Still in flight > 10 minutes before `now` (12:20:00Z)!
                "ms": 0,
            },
        ],
    )

    tailer = MagpieTailer(routing_root=magpie_dir, days_back=1)
    sessions = tailer.poll(now=now)
    assert OTHER_SID not in sessions
    assert ROOT_SID in sessions

    h = sessions[ROOT_SID]
    assert h.retries_429 == 2
    assert h.subagent_request_count == 2
    assert h.streak_504_413 >= 3
    assert h.fallback_prompt_tokens == 185000

    up = h.to_upstream_dict(now_ts=now.timestamp())
    assert up["stall_alert"] is True
    assert up["stalled_over_10m"] is True
    assert up["alert_level"] == "critical"
    assert up["retries_429"] == 2

    # Join with rollout that has NO token_count -> uses Magpie prompt.tokens fallback!
    day_dir = tmp_path / "sessions" / "2026" / "11" / "27"
    _write_jsonl(
        day_dir / f"rollout-2026-11-27T12-00-00-{ROOT_SID}.jsonl",
        [
            {
                "timestamp": "2026-11-27T12:00:00Z",
                "type": "session_meta",
                "payload": {"id": ROOT_SID, "session_id": ROOT_SID, "cwd": "/tmp"},
            }
        ],
    )
    repo = _make_fake_research_repo(tmp_path)
    engine = MicroEngine(
        research_root=repo,
        codex_sessions_root=tmp_path / "sessions",
        magpie_routing_root=magpie_dir,
        tmp_root=tmp_path / "tmp",
    )
    snap = engine.build_snapshot(now=now)
    sess = snap["sessions"][0]
    assert sess["status"] == "stalled"
    assert sess["context_health"]["used_tokens"] == 185000
    assert sess["context_health"]["source"] == "magpie_fallback"
    assert sess["context_health"]["level"] == "warn"


# ---------------------------------------------------------------------------
# 3. $TMPDIR Hook Cache & cmd_summary Reading Tests
# ---------------------------------------------------------------------------


def test_hooks_tmp_reader_and_corrupt_json_tolerance(tmp_path: Path) -> None:
    tmp_root = tmp_path / "var_tmp"
    (tmp_root / "xy_figure_gate").mkdir(parents=True)
    (tmp_root / "xy_prose_gate").mkdir(parents=True)
    (tmp_root / "xy_workflow_guard").mkdir(parents=True)
    (tmp_root / "xiaoyan_cmd_summary").mkdir(parents=True)

    (tmp_root / "xy_figure_gate" / "fp_cache.json").write_text(
        json.dumps(
            {
                "/Users/abc/Desktop/科研工作流/projects/热浪暴露和衰老/06_图片/fig1.pdf": {
                    "fp": "abc",
                    "status": "PASS",
                    "ts": 1700000000.0,
                },
                "/Users/abc/Desktop/科研工作流/projects/热浪暴露和衰老/06_图片/fig2.pdf": {
                    "fp": "def",
                    "status": "FAIL",
                    "ts": 1700000010.0,
                },
            }
        ),
        encoding="utf-8",
    )
    (tmp_root / "xy_figure_gate" / f"{ROOT_SID}.count").write_text("2", encoding="utf-8")

    (tmp_root / "xy_prose_gate" / f"{ROOT_SID}.hard_state.json").write_text(
        json.dumps({"last_fp": "123", "identical_streak": 2, "total_blocks": 3}),
        encoding="utf-8",
    )
    (tmp_root / "xy_workflow_guard" / f"{ROOT_SID}.count").write_text("7", encoding="utf-8")

    (tmp_root / "xiaoyan_cmd_summary" / "20261127T100000Z_formal_run.status.json").write_text(
        json.dumps(
            {
                "name": "formal_run",
                "command": ["python3", "tools/batch_formal_pipeline.py", "projects/热浪暴露和衰老"],
                "exit_code": 0,
                "status": "ok",
                "elapsed_s": 12.345,
            }
        ),
        encoding="utf-8",
    )
    # Half-written corrupt JSON must be tolerated silently
    (tmp_root / "xiaoyan_cmd_summary" / "20261127T100005Z_partial.status.json").write_text(
        '{"name": "half_writ',
        encoding="utf-8",
    )

    reader = HooksTmpReader(tmp_root=tmp_root)
    data = reader.read_all()

    assert data["figure_gate"]["pass_count"] == 1
    assert data["figure_gate"]["fail_count"] == 1
    assert data["figure_gate"]["session_counts"][ROOT_SID] == 2
    assert data["prose_gate"]["hard_states"][ROOT_SID]["total_blocks"] == 3
    assert data["workflow_guard"]["polling_counts"][ROOT_SID] == 7
    assert len(data["cmd_summaries"]) == 1
    assert data["cmd_summaries"][0]["name"] == "formal_run"
    assert data["cmd_summaries"][0]["project_hint"] == "热浪暴露和衰老"


# ---------------------------------------------------------------------------
# 4. 6-Tier Deterministic Attribution Tests
# ---------------------------------------------------------------------------


def test_deterministic_attribution_tiers(tmp_path: Path) -> None:
    repo = _make_fake_research_repo(tmp_path)
    deleg_file = repo / "projects" / "热浪暴露和衰老" / "99_audit" / "delegation" / "klosa_worker.md"
    deleg_file.write_text("# delegation brief\n", encoding="utf-8")

    corr = ProjectCorrelator(research_root=repo)

    # Tier 1: Nested project cwd (刘芸芳/刘芸芳charls课题 beats container 刘芸芳!)
    from core.sources.rollout import RolloutThreadState, ToolCallRecord

    t1 = RolloutThreadState(
        file_path="",
        thread_id=ROOT_SID,
        session_id=ROOT_SID,
        cwd=str(repo / "projects" / "刘芸芳" / "刘芸芳charls课题" / "05_代码"),
    )
    r1 = corr.attribute_session(t1)
    assert r1.project_id == "刘芸芳/刘芸芳charls课题"
    assert r1.confidence == "high"

    # Tier 2: Manual override when cwd is repo root
    t2 = RolloutThreadState(file_path="", thread_id=ROOT_SID, session_id=ROOT_SID, cwd=str(repo))
    r2 = corr.attribute_session(
        t2,
        prefs={"session_project_overrides": {ROOT_SID: "environment-cataract"}},
    )
    assert r2.project_name == "环境与白内障"
    assert r2.confidence == "manual"

    # Tier 4: Tool-call votes (>=60% & >=2 hits)
    t4 = RolloutThreadState(file_path="", thread_id=ROOT_SID, session_id=ROOT_SID, cwd=str(repo))
    t4.add_tool_call(ToolCallRecord(call_id="c1", name="formal_run", action_hint="formal_run", project_id="heatwave-aging"))
    t4.add_tool_call(ToolCallRecord(call_id="c2", name="read_file", action_hint="read_file", target_paths=["projects/热浪暴露和衰老/04_合同/a.json"]))
    r4 = corr.attribute_session(t4)
    assert r4.project_name == "热浪暴露和衰老"
    assert r4.confidence == "medium"

    # Tier 4 ambiguous 50/50 -> MUST stay unattributed (never misattribute!)
    t_amb = RolloutThreadState(file_path="", thread_id=ROOT_SID, session_id=ROOT_SID, cwd=str(repo))
    t_amb.add_tool_call(ToolCallRecord(call_id="a1", name="read_file", action_hint="read_file", target_paths=["projects/热浪暴露和衰老/a.txt"]))
    t_amb.add_tool_call(ToolCallRecord(call_id="a2", name="read_file", action_hint="read_file", target_paths=["projects/环境与白内障/b.txt"]))
    r_amb = corr.attribute_session(t_amb)
    assert r_amb.project_id is None
    assert r_amb.confidence == "unattributed"
    assert len(r_amb.candidate_projects) == 2

    # Tier 5: Recent 99_audit/delegation/*.md matching subagent agent_path
    t5 = RolloutThreadState(file_path="", thread_id=ROOT_SID, session_id=ROOT_SID, cwd=str(repo))
    sub5 = RolloutThreadState(
        file_path="",
        thread_id=SUB_TID,
        session_id=ROOT_SID,
        agent_path="/root/klosa_worker",
    )
    r5 = corr.attribute_session(t5, subagent_threads=[sub5])
    assert r5.project_name == "热浪暴露和衰老"
    assert r5.confidence == "medium"
    assert r5.source == "delegation_brief"


# ---------------------------------------------------------------------------
# 5. FastAPI Loopback Server, Prefs, Host Rejection, and SSE Tests
# ---------------------------------------------------------------------------


def test_fastapi_endpoints_security_prefs_and_sse(tmp_path: Path) -> None:
    repo = _make_fake_research_repo(tmp_path)
    prefs_path = tmp_path / "xy_progress" / "prefs.json"
    web_dir = tmp_path / "web"
    web_dir.mkdir()
    (web_dir / "index.html").write_text("<html><body>Dashboard</body></html>", encoding="utf-8")
    (web_dir / "hud.html").write_text("<html><body>HUD</body></html>", encoding="utf-8")
    (web_dir / "shared.js").write_text("export const ok = true;\n", encoding="utf-8")

    macro_calls = {"count": 0}

    def fake_macro(_root: Path, _prefs: dict[str, Any]) -> dict[str, Any]:
        macro_calls["count"] += 1
        return {
            "generated_at": "2026-11-27T12:00:00Z",
            "summary": {
                "total_projects": 2,
                "visible_projects": 2,
                "pending_approvals": 1,
                "gate_failures": 0,
                "drift_count": 0,
                "authority_present": False,
            },
            "projects": [
                {
                    "id": "刘芸芳/刘芸芳charls课题",
                    "name": "刘芸芳charls课题",
                    "pending_approvals": [{"id": "apr-1"}],
                }
            ],
        }

    def fake_detail(pid: str, _root: Path, _prefs: dict[str, Any]) -> dict[str, Any] | None:
        if pid == "刘芸芳/刘芸芳charls课题":
            return {"id": pid, "name": "刘芸芳charls课题", "runs": []}
        return None

    def fake_approvals(_root: Path, _prefs: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            {
                "id": "zw30_snapshot",
                "kind": "zw30_snapshot",
                "project_id": "zw30-lep",
                "executable": True,
                "argv": ["/venv/bin/python", "/repo/tools/open_approval_terminal.py", "--zw30-snapshot"],
                "display_cmd": ".venv/bin/python tools/open_approval_terminal.py --zw30-snapshot",
            }
        ]

    app = create_app(
        research_root=repo,
        codex_sessions_root=tmp_path / "sessions",
        magpie_routing_root=tmp_path / "magpie",
        tmp_root=tmp_path / "tmp",
        prefs_path=prefs_path,
        web_dir=web_dir,
        enable_watcher=False,
        macro_builder=fake_macro,
        detail_builder=fake_detail,
        approvals_builder=fake_approvals,
    )

    with TestClient(app, base_url="http://127.0.0.1:3460") as client:
        # Health
        r = client.get("/api/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert body["version"] == "0.1.0"
        assert body["research_root"] == str(repo)
        assert "authority_present" in body

        # Static & HTML routes
        assert client.get("/").status_code == 200
        assert client.get("/hud").status_code == 200
        assert client.get("/static/shared.js").status_code == 200
        assert client.get("/static/missing.js").status_code == 404

        # Macro caching & ?refresh=1
        assert client.get("/api/macro").status_code == 200
        assert client.get("/api/macro").status_code == 200
        assert macro_calls["count"] == 1
        assert client.get("/api/macro?refresh=1").status_code == 200
        assert macro_calls["count"] == 2

        # Project detail (nested slash ID + 404)
        d_ok = client.get("/api/macro/projects/刘芸芳/刘芸芳charls课题")
        assert d_ok.status_code == 200
        assert d_ok.json()["name"] == "刘芸芳charls课题"
        assert client.get("/api/macro/projects/nonexistent-project").status_code == 404

        # Micro
        m = client.get("/api/micro")
        assert m.status_code == 200
        mj = m.json()
        assert {"generated_at", "epoch", "seq", "active_session_id", "active_count", "sessions"} <= set(mj.keys())

        # Approvals commands (read-only metadata)
        apr = client.get("/api/approvals/commands")
        assert apr.status_code == 200
        assert len(apr.json()["items"]) == 1

        # Prefs GET & POST (isolated temp path, strict Pydantic validation)
        p0 = client.get("/api/prefs")
        assert p0.status_code == 200
        assert p0.json()["pinned_projects"] == []

        p_bad = client.post("/api/prefs", json={"evil_field": "nope"})
        assert p_bad.status_code == 422

        p1 = client.post(
            "/api/prefs",
            json={
                "pinned_projects": ["热浪暴露和衰老"],
                "session_project_overrides": {ROOT_SID: "热浪暴露和衰老"},
            },
        )
        assert p1.status_code == 200
        assert p1.json()["pinned_projects"] == ["热浪暴露和衰老"]
        assert p1.json()["session_project_overrides"][ROOT_SID] == "热浪暴露和衰老"
        assert prefs_path.is_file()

        # Security: reject non-local Host & Origin, allow WKWebView Origin: null
        r_evil_host = client.get("/api/health", headers={"Host": "evil.example.com"})
        assert r_evil_host.status_code == 403

        r_evil_origin = client.get("/api/health", headers={"Origin": "https://evil.example.com"})
        assert r_evil_origin.status_code == 403

        r_wk_origin = client.get("/api/health", headers={"Origin": "null"})
        assert r_wk_origin.status_code == 200

        # SSE framing test (?once=1 emits initial micro_update + macro_update cleanly)
        with client.stream("GET", "/api/events?once=1") as resp:
            assert resp.status_code == 200
            assert "text/event-stream" in resp.headers.get("content-type", "")
            raw_sse = "".join(resp.iter_text())
        assert "event: micro_update\n" in raw_sse
        assert "event: macro_update\n" in raw_sse
        assert "id: " in raw_sse
        # Parse data lines to confirm valid JSON framing
        data_lines = [ln[len("data: ") :] for ln in raw_sse.splitlines() if ln.startswith("data: ")]
        assert len(data_lines) >= 2
        parsed_micro = json.loads(data_lines[0])
        assert "epoch" in parsed_micro and "seq" in parsed_micro


def test_cli_refuses_non_loopback_host() -> None:
    rc = cli_main(["serve", "--host", "0.0.0.0", "--port", "3460"])
    assert rc == 2
