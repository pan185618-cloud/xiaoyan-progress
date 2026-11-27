# Xiaoyan MCP App Spike (100% Synthetic)

Minimal, zero-dependency (Python 3 stdlib only) synthetic MCP App plugin to verify how `ChatGPT-26.1111.21308.app` (`com.openai.codex`) renders:

- `ui://xiaoyan-spike/app.html` (`text/html;profile=mcp-app`)
- Sidebar App entrypoint (`_meta["openai/ui"].entrypoints = [{"type": "global"}]`) via `spike_open_dashboard`
- Thread Panel entrypoint (`_meta["openai/ui"].entrypoints = [{"type": "thread"}]`) via `spike_open_progress`
- Live 2s `postMessage` JSON-RPC bridge polling (`tools/call` -> `spike_get_state`) & `localhost` fetch sandbox check

Zero real research data is read or touched (`Demo-Project-A` and `Demo-Project-B` are hardcoded synthetic objects).

## 1. Local Self-Test (No Config Changes)

```bash
/usr/bin/python3 /Users/abc/Desktop/gpt/分析软件/codex改装/xiaoyan-progress/spike_mcp_app/smoke_test.py
```

## 2. Live-Test Inside `ChatGPT-26.1111.21308.app` (100% Reversible)

### Step A — Register local marketplace & install plugin

```bash
codex plugin marketplace add /Users/abc/Desktop/gpt/分析软件/codex改装/xiaoyan-progress/spike_mcp_app
codex plugin add xiaoyan-progress-spike@xiaoyan-spike
```

### Step B — Enable `enable_mcp_apps` feature flag (recommended if default `false` does not render iframes)

Back up `~/.codex/config.toml` first, then add `enable_mcp_apps = true` under `[features]`:

```bash
cp ~/.codex/config.toml ~/.codex/config.toml.bak-mcp-spike
```

Under `[features]` in `~/.codex/config.toml`, add:

```toml
enable_mcp_apps = true
```

### Step C — Reload or Restart ChatGPT Work

- First check **Plugins Directory** in ChatGPT Work to see if `Xiaoyan Progress Spike` appears immediately.
- If the sidebar/thread entrypoints or `enable_mcp_apps` do not take effect hot, **fully quit (`Cmd+Q`) and reopen `ChatGPT-26.1111.21308.app`** (do this only when no long task is running).

### Step D — What to Observe (2-Minute Checklist)

1. **Global Sidebar (`type: "global"`)**: Does `科研大盘 (Spike)` appear in the left sidebar? Clicking it should open the synthetic Macro/Micro UI fullscreen.
2. **Thread Panel (`type: "thread"`)**: Inside a fresh test chat, does `实时进度 (Spike)` appear as a conversation panel/tab alongside the thread?
3. **Inline/Model Tool Call**: Ask the chat: `"Call spike_open_progress"` (or `"Call spike_open_dashboard"`). Does an interactive iframe render instead of raw JSON text?
4. **Live Bridge Polling**: Inside the rendered UI, watch the top-right `tick #N` badge — does it increment every 2 seconds and advance the highlighted synthetic step?
5. **Bridge 诊断 Tab**: Click `Bridge 诊断` and verify:
   - `ui/initialize` = `connected`
   - `2s Poll status` = `N ok / 0 err`
   - `Localhost fetch` = `blocked/failed as expected`
   - `hostContext` shows theme/locale/displayMode (all sensitive keys auto-redacted).

## 3. Clean Rollback (Undo Everything)

```bash
codex plugin remove xiaoyan-progress-spike@xiaoyan-spike
codex plugin marketplace remove xiaoyan-spike
mv ~/.codex/config.toml.bak-mcp-spike ~/.codex/config.toml
```

Then restart ChatGPT Work when convenient.
