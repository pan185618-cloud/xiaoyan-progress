---
name: spike-probe
description: Synthetic MCP App UI probe for testing Codex Sidebar App and Thread Panel rendering with Demo-Project-A/B.
---

# Xiaoyan MCP App Spike Probe

This is a synthetic-only test skill with zero access to real research data.

- Call `spike_open_dashboard` to test rendering the Macro Dashboard (`global` sidebar entrypoint + `ui://xiaoyan-spike/app.html`).
- Call `spike_open_progress` to test rendering the Micro Live Step Progress panel (`thread` panel entrypoint + `ui://xiaoyan-spike/app.html`).
- Call `spike_get_state` to inspect the synthetic JSON payload and live server tick counter.
