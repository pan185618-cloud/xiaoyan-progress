#!/usr/bin/env python3
"""End-to-end stdio smoke test for the synthetic MCP App spike server."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

SERVER = Path(__file__).resolve().parent / "plugins" / "xiaoyan-progress-spike" / "server.py"


def call(proc: subprocess.Popen[str], req: dict) -> dict | None:
    assert proc.stdin and proc.stdout
    proc.stdin.write(json.dumps(req) + "\n")
    proc.stdin.flush()
    if "id" not in req:
        return None
    line = proc.stdout.readline()
    if not line:
        raise RuntimeError("EOF from MCP server")
    return json.loads(line)


def main() -> None:
    proc = subprocess.Popen(
        [sys.executable, "-u", str(SERVER)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        r1 = call(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "smoke", "version": "0.1"}}})
        assert r1 and r1["result"]["serverInfo"]["name"] == "xiaoyan-progress-spike", r1

        call(proc, {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

        r2 = call(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        tools = {t["name"]: t for t in r2["result"]["tools"]}
        assert set(tools.keys()) == {"spike_open_dashboard", "spike_open_progress", "spike_get_state"}, tools.keys()
        assert tools["spike_open_dashboard"]["_meta"]["ui"]["resourceUri"] == "ui://xiaoyan-spike/app.html"
        assert tools["spike_open_dashboard"]["_meta"]["openai/ui"]["entrypoints"] == [{"type": "global"}]
        assert tools["spike_open_progress"]["_meta"]["openai/ui"]["entrypoints"] == [{"type": "thread"}]

        r3 = call(proc, {"jsonrpc": "2.0", "id": 3, "method": "resources/list", "params": {}})
        resources = r3["result"]["resources"]
        assert len(resources) == 1 and resources[0]["mimeType"] == "text/html;profile=mcp-app", resources

        r4 = call(proc, {"jsonrpc": "2.0", "id": 4, "method": "resources/read", "params": {"uri": "ui://xiaoyan-spike/app.html"}})
        html = r4["result"]["contents"][0]["text"]
        assert "ui/initialize" in html and "spike_get_state" in html and "127.0.0.1" in html

        r5 = call(proc, {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "spike_open_dashboard", "arguments": {}}})
        r6 = call(proc, {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "spike_get_state", "arguments": {}}})
        t1 = r5["result"]["structuredContent"]["tick"]
        t2 = r6["result"]["structuredContent"]["tick"]
        assert t2 == t1 + 1, (t1, t2)

        print("SMOKE_TEST_PASS", json.dumps({
            "tools": sorted(tools.keys()),
            "resource_uri": resources[0]["uri"],
            "resource_mime": resources[0]["mimeType"],
            "html_bytes": len(html.encode("utf-8")),
            "ticks": [t1, t2],
        }, ensure_ascii=False))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    main()
