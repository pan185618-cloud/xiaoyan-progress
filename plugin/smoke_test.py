#!/usr/bin/env python3
"""End-to-end stdio smoke test for the production Xiaoyan Research Progress MCP App plugin.

Verifies:
1. initialize / tools/list / resources/list / resources/read (`text/html;profile=mcp-app`)
   - Both `ui://xiaoyan-progress/dashboard.html` and `ui://xiaoyan-progress/live_panel.html`
   - `open_live_progress` -> `ui://xiaoyan-progress/live_panel.html` (`entrypoints: [{"type": "thread"}]`)
   - `open_research_dashboard` -> `ui://xiaoyan-progress/dashboard.html` (`entrypoints: [{"type": "global"}]`)
   - `live_panel.html` offline integrity (`ui/initialize`, `thread_context`, zero external http/https links)
2. `get_research_progress` returns real projects & sessions (with `cwd`), warm latency < 1.0s
3. `get_project_detail("zw30-lep")` returns contracts, evidence, gates, approval commands
4. Thread `cwd` binding:
   - `cwd = projects/环境与白内障` -> `matched_project_id == "environment-cataract"`,
     `match_reason in ("cwd_exact", "project_match")`, `approval_commands` present
   - `cwd = "/"` -> `matched_project_id is None`, `match_reason in ("fallback_global", "none")`
5. Zero writes to RESEARCH_ROOT and zero prompt/reasoning/secret leakage
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

PROJECT_ROOT = Path("/Users/abc/Desktop/gpt/分析软件/codex改装/xiaoyan-progress")
VENV_PYTHON = PROJECT_ROOT / ".venv" / "bin" / "python"
SERVER = PROJECT_ROOT / "plugin" / "plugins" / "xiaoyan-research-progress" / "server.py"
RESEARCH_ROOT = Path("/Users/abc/Desktop/本地工作流（小研）/科研工作流")
CATARACT_CWD = RESEARCH_ROOT / "projects" / "环境与白内障"

DASHBOARD_URI = "ui://xiaoyan-progress/dashboard.html"
LIVE_PANEL_URI = "ui://xiaoyan-progress/live_panel.html"
RESOURCE_MIME = "text/html;profile=mcp-app"

FORBIDDEN_LEAK_RE = re.compile(
    r"(encrypted_content|sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{20,}|Bearer [A-Za-z0-9._-]{20,})"
)
EXTERNAL_LINK_RE = re.compile(r"https?://", re.IGNORECASE)


def call(proc: subprocess.Popen[str], req: dict) -> dict | None:
    assert proc.stdin and proc.stdout
    proc.stdin.write(json.dumps(req, ensure_ascii=False) + "\n")
    proc.stdin.flush()
    if "id" not in req:
        return None
    line = proc.stdout.readline()
    if not line:
        err = proc.stderr.read() if proc.stderr else ""
        raise RuntimeError(f"EOF from MCP server; stderr={err}")
    return json.loads(line)


@contextmanager
def spawn_server(cwd: str | Path | None = None) -> Iterator[subprocess.Popen[str]]:
    proc = subprocess.Popen(
        [str(VENV_PYTHON), "-u", str(SERVER)],
        cwd=str(cwd) if cwd is not None else None,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        r_init = call(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "prod-smoke", "version": "1.0.0"},
                },
            },
        )
        assert r_init and r_init["result"]["serverInfo"]["name"] == "xiaoyan-research-progress", r_init
        call(proc, {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        yield proc
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()


def main() -> None:
    before_mtime = RESEARCH_ROOT.stat().st_mtime_ns
    projects_before_mtime = (RESEARCH_ROOT / "projects").stat().st_mtime_ns
    payloads_to_scan: list[object] = []

    # 1. Core protocol, resources, dashboard + live_panel HTML, latency, project detail
    with spawn_server() as proc:
        r_tools = call(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        tools = {t["name"]: t for t in r_tools["result"]["tools"]}
        expected_tools = {
            "open_research_dashboard",
            "open_live_progress",
            "get_research_progress",
            "get_project_detail",
            "update_progress_prefs",
        }
        assert set(tools.keys()) == expected_tools, tools.keys()
        assert tools["open_research_dashboard"]["_meta"]["ui"]["resourceUri"] == DASHBOARD_URI
        assert tools["open_research_dashboard"]["_meta"]["openai/ui"]["entrypoints"] == [{"type": "global"}]
        assert tools["open_live_progress"]["_meta"]["ui"]["resourceUri"] == LIVE_PANEL_URI
        assert tools["open_live_progress"]["_meta"]["openai/ui"]["entrypoints"] == [{"type": "thread"}]

        r_res_list = call(proc, {"jsonrpc": "2.0", "id": 3, "method": "resources/list", "params": {}})
        resources = {r["uri"]: r for r in r_res_list["result"]["resources"]}
        assert set(resources.keys()) == {DASHBOARD_URI, LIVE_PANEL_URI}, resources.keys()
        for uri in (DASHBOARD_URI, LIVE_PANEL_URI):
            assert resources[uri]["mimeType"] == RESOURCE_MIME, resources[uri]

        r_dash_read = call(
            proc,
            {"jsonrpc": "2.0", "id": 4, "method": "resources/read", "params": {"uri": DASHBOARD_URI}},
        )
        dash_html = r_dash_read["result"]["contents"][0]["text"]
        assert "科研总览流程图" in dash_html and "当前运行步骤（实时）" in dash_html and "ui/initialize" in dash_html

        r_live_read = call(
            proc,
            {"jsonrpc": "2.0", "id": 5, "method": "resources/read", "params": {"uri": LIVE_PANEL_URI}},
        )
        live_content = r_live_read["result"]["contents"][0]
        assert live_content["mimeType"] == RESOURCE_MIME, live_content
        live_html = live_content["text"]
        assert "<!doctype html>" in live_html.lower() and "</html>" in live_html.lower()
        assert "ui/initialize" in live_html, "live_panel.html missing ui/initialize"
        assert "thread_context" in live_html, "live_panel.html missing thread_context"
        ext_match = EXTERNAL_LINK_RE.search(live_html)
        assert ext_match is None, f"live_panel.html contains external URL: {ext_match.group(0)}"

        # Cold get_research_progress
        t0 = time.monotonic()
        r_prog1 = call(
            proc,
            {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "get_research_progress", "arguments": {}}},
        )
        cold_s = round(time.monotonic() - t0, 3)
        sc1 = r_prog1["result"]["structuredContent"]
        projects = sc1["macro"]["projects"]
        sessions = sc1["micro"]["sessions"]
        assert len(projects) >= 39, f"Expected >=39 projects, got {len(projects)}"
        assert any(p["id"] == "zw30-lep" for p in projects), "zw30-lep missing"
        assert any(p["id"] == "heatwave-aging" for p in projects), "heatwave-aging missing"
        assert any(p["id"] == "environment-cataract" for p in projects), "environment-cataract missing"
        assert all("cwd" in s for s in sessions), "Some micro sessions are missing 'cwd' field"
        assert isinstance(sc1.get("thread_context"), dict), "thread_context missing from get_research_progress"

        # Warm get_research_progress (< 1.0s)
        t1 = time.monotonic()
        r_prog2 = call(
            proc,
            {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "get_research_progress", "arguments": {}}},
        )
        warm_s = round(time.monotonic() - t1, 3)
        sc2 = r_prog2["result"]["structuredContent"]
        assert sc2["tick"] == sc1["tick"] + 1
        assert warm_s < 1.0, f"Warm latency too slow: {warm_s}s"

        # Project detail zw30-lep
        r_det = call(
            proc,
            {"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": {"name": "get_project_detail", "arguments": {"project_id": "zw30-lep"}}},
        )
        det = r_det["result"]["structuredContent"]["project"]
        assert det["id"] == "zw30-lep" and det["contracts"], "zw30-lep detail invalid"
        assert "approval_commands" in det and "gates" in det

        payloads_to_scan.extend([sc1, det])

    # 2. Spawn server with project cwd -> environment-cataract binding
    assert CATARACT_CWD.is_dir(), f"Expected project directory missing: {CATARACT_CWD}"
    with spawn_server(cwd=CATARACT_CWD) as proc_proj:
        r_live_proj = call(
            proc_proj,
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "open_live_progress", "arguments": {}}},
        )
        sc_proj = r_live_proj["result"]["structuredContent"]
        tc_proj = sc_proj.get("thread_context")
        assert isinstance(tc_proj, dict), f"Missing thread_context: {sc_proj.keys()}"
        assert tc_proj["matched_project_id"] == "environment-cataract", tc_proj
        assert tc_proj["match_reason"] in ("cwd_exact", "project_match"), tc_proj
        assert isinstance(tc_proj.get("approval_commands"), list), tc_proj
        payloads_to_scan.append(sc_proj)

    # 3. Spawn server with cwd="/" -> must NOT falsely match any project
    with spawn_server(cwd="/") as proc_root:
        r_live_root = call(
            proc_root,
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "open_live_progress", "arguments": {}}},
        )
        sc_root = r_live_root["result"]["structuredContent"]
        tc_root = sc_root.get("thread_context")
        assert isinstance(tc_root, dict), f"Missing thread_context: {sc_root.keys()}"
        assert tc_root["matched_project_id"] is None, tc_root
        assert tc_root["match_reason"] in ("fallback_global", "none"), tc_root
        assert tc_root["matching_session_ids"] == [], tc_root
        assert isinstance(tc_root.get("approval_commands"), list), tc_root
        payloads_to_scan.append(sc_root)

    # 4. Secret / prompt leak check across all returned payloads
    blob = json.dumps(payloads_to_scan, ensure_ascii=False)
    m = FORBIDDEN_LEAK_RE.search(blob)
    assert m is None, f"Forbidden secret/leak pattern matched: {m.group(0)[:20]}"

    # 5. Verify RESEARCH_ROOT untouched
    assert RESEARCH_ROOT.stat().st_mtime_ns == before_mtime
    assert (RESEARCH_ROOT / "projects").stat().st_mtime_ns == projects_before_mtime

    print(
        "PROD_PLUGIN_SMOKE_PASS",
        json.dumps(
            {
                "tools": sorted(tools.keys()),
                "resources": sorted(resources.keys()),
                "dashboard_html_bytes": len(dash_html.encode("utf-8")),
                "live_panel_html_bytes": len(live_html.encode("utf-8")),
                "projects_total": len(projects),
                "visible_projects": sc1["macro"]["summary"].get("visible_projects"),
                "sessions_total": len(sessions),
                "active_sessions": sc1["micro"].get("active_count"),
                "cold_sec": cold_s,
                "warm_sec": warm_s,
                "zw30_contracts": len(det["contracts"]),
                "zw30_approval_cmds": len(det["approval_commands"]),
                "cataract_match_reason": tc_proj["match_reason"],
                "cataract_matching_sessions": len(tc_proj["matching_session_ids"]),
                "cataract_approval_cmds": len(tc_proj["approval_commands"]),
                "root_match_reason": tc_root["match_reason"],
                "root_matched_project_id": tc_root["matched_project_id"],
                "research_root_untouched": True,
            },
            ensure_ascii=False,
        ),
    )


if __name__ == "__main__":
    main()
