#!/usr/bin/env python3
"""
100% Synthetic MCP App Spike Server (Python 3 stdlib only, stdio JSON-RPC 2.0).

Zero access to real research data or workspace files.
Exposes:
  - Resource: ui://xiaoyan-spike/app.html (text/html;profile=mcp-app)
  - Tool: spike_open_dashboard (_meta.ui.resourceUri + openai/ui global entrypoint)
  - Tool: spike_open_progress  (_meta.ui.resourceUri + openai/ui thread entrypoint)
  - Tool: spike_get_state      (synthetic Demo-Project-A/B S0-S7 + live tick counter)
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from typing import Any

RESOURCE_URI = "ui://xiaoyan-spike/app.html"
RESOURCE_MIME = "text/html;profile=mcp-app"

START_TIME = time.monotonic()
TICK_COUNTER = 0

STAGES = [
    ("S0", "构思立项"),
    ("S1", "数据整理"),
    ("S2", "探索分析"),
    ("S3", "合同冻结"),
    ("S4", "正式统计"),
    ("S5", "图表质检"),
    ("S6", "稿件审计"),
    ("S7", "投稿封包"),
]

STEP_TEMPLATES = [
    "读取合成合同快照 (demo_contract_v1)",
    "运行全网格预检 (preflight check)",
    "执行受控 R 统计沙箱 (synthetic run)",
    "独立验证器复跑比对 (validator diff=0)",
    "图表质检 .check.json 全部 PASS",
    "等待人工终端签字确认 (C-level gate)",
]


def build_synthetic_state(view: str = "auto") -> dict[str, Any]:
    global TICK_COUNTER
    TICK_COUNTER += 1
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    uptime_s = round(time.monotonic() - START_TIME, 1)

    active_idx = (TICK_COUNTER - 1) % len(STEP_TEMPLATES)
    live_steps = []
    for i, label in enumerate(STEP_TEMPLATES):
        if i < active_idx:
            status = "done"
        elif i == active_idx:
            status = "in_progress"
        elif i == len(STEP_TEMPLATES) - 1:
            status = "needs_approval"
        else:
            status = "pending"
        live_steps.append(
            {
                "id": f"step-{i + 1}",
                "title": label,
                "status": status,
                "detail": f"synthetic tick #{TICK_COUNTER}" if status == "in_progress" else "",
            }
        )

    return {
        "synthetic": True,
        "view": view,
        "tick": TICK_COUNTER,
        "server_time_utc": now_iso,
        "uptime_sec": uptime_s,
        "projects": [
            {
                "id": "demo-a",
                "name": "Demo-Project-A (Synthetic Cohort)",
                "current_stage": "S4",
                "stages": {
                    "S0": "done",
                    "S1": "done",
                    "S2": "done",
                    "S3": "done",
                    "S4": "in_progress",
                    "S5": "pending",
                    "S6": "pending",
                    "S7": "pending",
                },
                "gate_badge": "待终端签署 (synthetic)",
                "context_tokens": 112400 + (TICK_COUNTER * 350) % 15000,
                "context_budget": 180000,
            },
            {
                "id": "demo-b",
                "name": "Demo-Project-B (Synthetic ML)",
                "current_stage": "S6",
                "stages": {
                    "S0": "done",
                    "S1": "done",
                    "S2": "done",
                    "S3": "done",
                    "S4": "done",
                    "S5": "done",
                    "S6": "in_progress",
                    "S7": "pending",
                },
                "gate_badge": "文献核验中 (synthetic)",
                "context_tokens": 64200,
                "context_budget": 180000,
            },
        ],
        "live_run": {
            "project": "Demo-Project-A (Synthetic Cohort)",
            "agent": "codex-main -> r-executor (synthetic)",
            "active_step_index": active_idx,
            "steps": live_steps,
        },
        "stage_labels": [{"code": c, "label": l} for c, l in STAGES],
    }


APP_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<title>Xiaoyan Progress MCP App Spike</title>
<style>
  :root {
    --bg: #0f172a;
    --panel: #1e293b;
    --border: #334155;
    --text: #f1f5f9;
    --muted: #94a3b8;
    --accent: #38bdf8;
    --ok: #22c55e;
    --warn: #f59e0b;
    --err: #ef4444;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    padding: 12px;
    font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "PingFang SC", sans-serif;
    background: var(--bg);
    color: var(--text);
    font-size: 13px;
    line-height: 1.45;
  }
  .header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 8px;
    padding-bottom: 10px;
    border-bottom: 1px solid var(--border);
    margin-bottom: 10px;
  }
  .title { font-weight: 700; font-size: 14px; }
  .badge {
    font-size: 11px;
    padding: 2px 7px;
    border-radius: 999px;
    border: 1px solid var(--border);
    color: var(--accent);
    background: rgba(56, 189, 248, 0.1);
  }
  .tabs { display: flex; gap: 6px; margin-bottom: 10px; }
  .tab {
    border: 1px solid var(--border);
    background: var(--panel);
    color: var(--muted);
    padding: 4px 10px;
    border-radius: 6px;
    cursor: pointer;
    font-size: 12px;
  }
  .tab.active { color: var(--text); border-color: var(--accent); }
  .card {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 10px;
    margin-bottom: 10px;
  }
  .card h3 { margin: 0 0 6px 0; font-size: 13px; }
  .stages { display: flex; gap: 4px; flex-wrap: wrap; margin: 6px 0; }
  .stage {
    font-size: 11px;
    padding: 2px 6px;
    border-radius: 4px;
    border: 1px solid var(--border);
    color: var(--muted);
  }
  .stage.done { background: rgba(34, 197, 94, 0.18); border-color: var(--ok); color: #bbf7d0; }
  .stage.in_progress { background: rgba(56, 189, 248, 0.2); border-color: var(--accent); color: #bae6fd; font-weight: 600; }
  .step {
    display: flex;
    align-items: flex-start;
    gap: 8px;
    padding: 5px 0;
    border-bottom: 1px dashed rgba(148,163,184,0.18);
  }
  .step:last-child { border-bottom: none; }
  .dot {
    width: 10px; height: 10px; border-radius: 50%; margin-top: 4px; flex-shrink: 0;
    background: var(--muted);
  }
  .dot.done { background: var(--ok); }
  .dot.in_progress { background: var(--accent); box-shadow: 0 0 0 3px rgba(56,189,248,0.25); }
  .dot.needs_approval { background: var(--warn); }
  .muted { color: var(--muted); font-size: 11px; }
  pre {
    margin: 6px 0 0 0;
    padding: 8px;
    background: #020617;
    border: 1px solid var(--border);
    border-radius: 6px;
    overflow: auto;
    max-height: 150px;
    font-size: 11px;
    color: #cbd5e1;
  }
  .kv { display: grid; grid-template-columns: 140px 1fr; gap: 4px 8px; font-size: 12px; }
  .ok { color: var(--ok); }
  .warn { color: var(--warn); }
  .err { color: var(--err); }
</style>
</head>
<body>
  <div class="header">
    <div>
      <div class="title">Xiaoyan Progress — MCP App Spike (Synthetic)</div>
      <div class="muted" id="subline">Initializing MCP Apps postMessage bridge...</div>
    </div>
    <span class="badge" id="tickBadge">tick #0</span>
  </div>

  <div class="tabs">
    <button class="tab active" id="tabMicro" onclick="setTab('micro')">Micro 实时步骤</button>
    <button class="tab" id="tabMacro" onclick="setTab('macro')">Macro 项目大盘</button>
    <button class="tab" id="tabDiag" onclick="setTab('diag')">Bridge 诊断</button>
  </div>

  <div id="viewMicro">
    <div class="card">
      <h3 id="liveTitle">Progress (Demo-Project-A)</h3>
      <div class="muted" id="liveAgent">Waiting for bridge poll...</div>
      <div id="stepsList" style="margin-top:8px;"></div>
    </div>
  </div>

  <div id="viewMacro" style="display:none;">
    <div id="projectsList"></div>
  </div>

  <div id="viewDiag" style="display:none;">
    <div class="card">
      <h3>MCP Apps Bridge Diagnostics</h3>
      <div class="kv">
        <div class="muted">ui/initialize</div><div id="diagInit">pending...</div>
        <div class="muted">2s Poll status</div><div id="diagPoll">0 ok / 0 err</div>
        <div class="muted">Localhost fetch</div><div id="diagFetch">probing...</div>
        <div class="muted">Window origin</div><div id="diagOrigin"></div>
      </div>
      <div class="muted" style="margin-top:8px;">hostContext (redacted):</div>
      <pre id="diagHostCtx">{}</pre>
      <div class="muted" style="margin-top:8px;">Recent bridge events:</div>
      <pre id="diagLog">[]</pre>
    </div>
  </div>

<script>
(() => {
  const REDACT_RE = /(token|secret|key|auth|cookie|bearer|password|session)/i;
  let nextId = 1;
  const pending = new Map();
  let pollOk = 0, pollErr = 0;
  let currentTab = 'micro';
  const eventLog = [];

  document.getElementById('diagOrigin').textContent = window.location.origin + ' | href=' + window.location.href.slice(0, 60);

  function logEvent(msg) {
    const ts = new Date().toISOString().slice(11, 19);
    eventLog.unshift(`[${ts}] ${msg}`);
    if (eventLog.length > 20) eventLog.pop();
    document.getElementById('diagLog').textContent = eventLog.join('\n');
  }

  function redact(obj, depth = 0) {
    if (depth > 5 || obj === null || obj === undefined) return obj;
    if (Array.isArray(obj)) return obj.slice(0, 20).map(x => redact(x, depth + 1));
    if (typeof obj === 'object') {
      const out = {};
      for (const [k, v] of Object.entries(obj)) {
        if (REDACT_RE.test(k)) out[k] = '<REDACTED>';
        else out[k] = redact(v, depth + 1);
      }
      return out;
    }
    if (typeof obj === 'string' && obj.length > 160) return obj.slice(0, 80) + '...<TRUNCATED>';
    return obj;
  }

  window.setTab = function(tab) {
    currentTab = tab;
    document.getElementById('viewMicro').style.display = tab === 'micro' ? '' : 'none';
    document.getElementById('viewMacro').style.display = tab === 'macro' ? '' : 'none';
    document.getElementById('viewDiag').style.display = tab === 'diag' ? '' : 'none';
    document.getElementById('tabMicro').classList.toggle('active', tab === 'micro');
    document.getElementById('tabMacro').classList.toggle('active', tab === 'macro');
    document.getElementById('tabDiag').classList.toggle('active', tab === 'diag');
  };

  function rpcRequest(method, params) {
    return new Promise((resolve, reject) => {
      const id = nextId++;
      const timer = setTimeout(() => {
        pending.delete(id);
        reject(new Error(`timeout waiting for ${method}`));
      }, 5000);
      pending.set(id, { resolve, reject, timer });
      window.parent.postMessage({ jsonrpc: '2.0', id, method, params }, '*');
    });
  }

  function rpcNotify(method, params) {
    window.parent.postMessage({ jsonrpc: '2.0', method, params }, '*');
  }

  function renderState(state) {
    if (!state || !state.live_run) return;
    document.getElementById('tickBadge').textContent = `tick #${state.tick}`;
    document.getElementById('subline').textContent = `Server UTC ${state.server_time_utc} · uptime ${state.uptime_sec}s`;
    document.getElementById('liveTitle').textContent = `Progress — ${state.live_run.project}`;
    document.getElementById('liveAgent').textContent = state.live_run.agent;

    const stepsEl = document.getElementById('stepsList');
    stepsEl.innerHTML = '';
    for (const s of state.live_run.steps) {
      const row = document.createElement('div');
      row.className = 'step';
      row.innerHTML = `<span class="dot ${s.status}"></span><div><div>${s.title}</div><div class="muted">${s.status}${s.detail ? ' · ' + s.detail : ''}</div></div>`;
      stepsEl.appendChild(row);
    }

    const projEl = document.getElementById('projectsList');
    projEl.innerHTML = '';
    for (const p of state.projects || []) {
      const card = document.createElement('div');
      card.className = 'card';
      const stagesHtml = (state.stage_labels || []).map(st => {
        const cls = p.stages[st.code] || 'pending';
        return `<span class="stage ${cls}">${st.code} ${st.label}</span>`;
      }).join('');
      const pct = Math.round((p.context_tokens / p.context_budget) * 100);
      card.innerHTML = `
        <h3>${p.name} <span class="badge">${p.current_stage}</span></h3>
        <div class="muted">Gate: ${p.gate_badge} · Context: ${(p.context_tokens/1000).toFixed(1)}k / ${(p.context_budget/1000).toFixed(0)}k (${pct}%)</div>
        <div class="stages">${stagesHtml}</div>
      `;
      projEl.appendChild(card);
    }
  }

  function extractStateFromToolResult(res) {
    if (!res) return null;
    if (res.structuredContent && res.structuredContent.synthetic) return res.structuredContent;
    if (Array.isArray(res.content)) {
      for (const item of res.content) {
        if (item && item.type === 'text' && item.text) {
          try {
            const parsed = JSON.parse(item.text);
            if (parsed && parsed.synthetic) return parsed;
          } catch (_) {}
        }
      }
    }
    return null;
  }

  window.addEventListener('message', (ev) => {
    const msg = ev.data;
    if (!msg || typeof msg !== 'object' || msg.jsonrpc !== '2.0') return;
    if (msg.id !== undefined && pending.has(msg.id)) {
      const entry = pending.get(msg.id);
      pending.delete(msg.id);
      clearTimeout(entry.timer);
      if (msg.error) entry.reject(new Error(msg.error.message || JSON.stringify(msg.error)));
      else entry.resolve(msg.result);
      return;
    }
    if (msg.method) {
      logEvent(`notify ${msg.method}`);
      if (msg.method === 'ui/notifications/tool-result') {
        const st = extractStateFromToolResult(msg.params);
        if (st) renderState(st);
      } else if (msg.method === 'ui/notifications/host-context-changed') {
        document.getElementById('diagHostCtx').textContent = JSON.stringify(redact(msg.params), null, 2);
      }
    }
  });

  async function probeLocalhostFetch() {
    const el = document.getElementById('diagFetch');
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), 1500);
    try {
      await fetch('http://127.0.0.1:1/health', { signal: ctrl.signal });
      el.innerHTML = '<span class="warn">unexpectedly resolved</span>';
    } catch (e) {
      el.innerHTML = `<span class="ok">blocked/failed as expected</span> (${String(e).slice(0, 80)})`;
    } finally {
      clearTimeout(t);
    }
  }

  async function pollOnce() {
    const t0 = performance.now();
    try {
      const res = await rpcRequest('tools/call', { name: 'spike_get_state', arguments: { view: currentTab } });
      const ms = Math.round(performance.now() - t0);
      pollOk++;
      document.getElementById('diagPoll').innerHTML = `<span class="ok">${pollOk} ok</span> / ${pollErr} err (last ${ms}ms)`;
      const st = extractStateFromToolResult(res);
      if (st) renderState(st);
    } catch (e) {
      pollErr++;
      document.getElementById('diagPoll').innerHTML = `${pollOk} ok / <span class="err">${pollErr} err</span> (${String(e).slice(0, 60)})`;
      logEvent('poll error: ' + String(e));
    }
  }

  async function boot() {
    probeLocalhostFetch();
    try {
      const initRes = await rpcRequest('ui/initialize', {
        protocolVersion: '2026-01-26',
        appInfo: { name: 'xiaoyan-progress-spike', version: '0.1.0' },
        appCapabilities: {}
      });
      rpcNotify('ui/notifications/initialized', {});
      document.getElementById('diagInit').innerHTML = '<span class="ok">connected</span>';
      document.getElementById('diagHostCtx').textContent = JSON.stringify(redact(initRes?.hostContext || initRes), null, 2);
      logEvent('ui/initialize OK');
      await pollOnce();
      setInterval(pollOnce, 2000);
    } catch (e) {
      document.getElementById('diagInit').innerHTML = `<span class="err">failed</span> (${String(e).slice(0, 80)})`;
      logEvent('ui/initialize failed: ' + String(e));
    }
  }

  boot();
})();
</script>
</body>
</html>
"""


TOOLS = [
    {
        "name": "spike_open_dashboard",
        "title": "科研大盘 (Spike)",
        "description": "Open the synthetic Macro Research Progress Dashboard (Demo-Project-A/B, S0-S7).",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        "annotations": {
            "title": "科研大盘 (Spike)",
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
        "name": "spike_open_progress",
        "title": "实时进度 (Spike)",
        "description": "Open the synthetic Cowork-style live step progress thread panel.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        "annotations": {
            "title": "实时进度 (Spike)",
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
                "entrypoints": [{"type": "thread"}],
                "preferredModelDisplayMode": "inline",
            },
        },
    },
    {
        "name": "spike_get_state",
        "title": "Get Synthetic State",
        "description": "Return synthetic Demo-Project-A/B S0-S7 stages, live step checklist, and ticking counter.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "view": {"type": "string", "description": "Optional view name (macro/micro/diag)."}
            },
            "additionalProperties": False,
        },
        "annotations": {
            "title": "Get Synthetic State",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": False,
        },
        "_meta": {
            "ui": {
                "visibility": ["model", "app"],
            }
        },
    },
]

RESOURCES = [
    {
        "uri": RESOURCE_URI,
        "name": "xiaoyan-spike-app",
        "title": "Xiaoyan Progress Spike UI",
        "description": "Self-contained synthetic MCP App HTML/JS probe.",
        "mimeType": RESOURCE_MIME,
        "_meta": {
            "ui": {
                "prefersBorder": True,
            }
        },
    }
]


def send(msg: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def handle_request(req: dict[str, Any]) -> None:
    req_id = req.get("id")
    method = req.get("method", "")
    params = req.get("params") or {}

    if req_id is None:
        # Notification (e.g., notifications/initialized, notifications/cancelled)
        return

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
                        "name": "xiaoyan-progress-spike",
                        "version": "0.1.0",
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
                                "text": APP_HTML,
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
        if name == "spike_open_dashboard":
            state = build_synthetic_state(view="macro")
        elif name == "spike_open_progress":
            state = build_synthetic_state(view="micro")
        elif name == "spike_get_state":
            state = build_synthetic_state(view=str(args.get("view") or "auto"))
        else:
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
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(state, ensure_ascii=False),
                        }
                    ],
                    "structuredContent": state,
                    "isError": False,
                },
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


def main() -> None:
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as exc:
            sys.stderr.write(f"[xiaoyan-progress-spike] JSON decode error: {exc}\n")
            continue
        handle_request(req)


if __name__ == "__main__":
    main()
