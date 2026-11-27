/**
 * hud.js — Cowork-Style Top-Right Live Micro Progress HUD ("精细把控")
 * Runs cleanly inside macOS WKWebView NSPanel (290x42 collapsed / 376x540 expanded)
 * AND previewable standalone in any browser at http://127.0.0.1:3460/hud.
 * Pure ES module, zero build step, 100% offline.
 */

import {
  apiGet,
  copyText,
  createEventStream,
  defaultPrefs,
  escapeHtml,
  fetchPrefs,
  formatDuration,
  formatRelativeTime,
  formatTokens,
  isNativeHud,
  normalizeGateInfo,
  normalizeStepStatus,
  postNativeMessage,
  roleLabel,
  showToast,
  stepStatusIconHtml,
  truncate,
  updatePrefs,
} from "./shared.js";

const NATIVE = isNativeHud();

const state = {
  mode: "collapsed", // "collapsed" | "expanded"
  micro: null,
  projects: [], // lightweight list from /api/macro for attribution & short names
  approvals: [],
  prefs: defaultPrefs(),
  sseStatus: "connecting",

  // User UI state preserved across live SSE ticks
  selectedSessionMode: "auto", // "auto" | <session_id>
  expandedStepIds: new Set(),
  showAttributionPicker: false,
  showApprovalTray: false,
};

let microRefreshTimer = null;
let macroRefreshTimer = null;

const dom = {
  root: document.getElementById("hud-root"),
  previewNote: document.getElementById("hud-preview-note"),
  previewToggle: document.getElementById("preview-toggle-mode"),
};

/* ==========================================================================
   Mode Switching & Native WKWebView Bridge
   ========================================================================== */

function applyMode(nextMode, { notifyNative = true } = {}) {
  const normalized = nextMode === "expanded" ? "expanded" : "collapsed";
  state.mode = normalized;
  if (normalized === "collapsed") {
    state.showAttributionPicker = false;
    state.showApprovalTray = false;
  }
  render();
  if (notifyNative && NATIVE) {
    postNativeMessage({ type: "setMode", mode: normalized });
  }
}

// Exposed for macOS menu bar (`window.__xyHudSetMode?.("collapsed" | "expanded")`)
window.__xyHudSetMode = (mode) => {
  applyMode(mode, { notifyNative: false });
};

function openDashboard(projectId = null) {
  if (NATIVE) {
    postNativeMessage({ type: "openDashboard", projectId: projectId || null });
    return;
  }
  const url = projectId ? `/?project=${encodeURIComponent(projectId)}` : "/";
  window.open(url, "_blank", "noopener");
}

/* ==========================================================================
   Data Loading
   ========================================================================== */

async function loadMicro() {
  try {
    state.micro = await apiGet("/api/micro");
    render();
  } catch (err) {
    console.warn("[hud] loadMicro failed:", err);
  }
}

async function loadProjectsCatalog() {
  try {
    const macro = await apiGet("/api/macro");
    state.projects = Array.isArray(macro?.projects)
      ? macro.projects.filter((p) => p.category !== "system")
      : [];
    render();
  } catch (err) {
    console.warn("[hud] loadProjectsCatalog failed:", err);
  }
}

async function loadApprovals() {
  try {
    const data = await apiGet("/api/approvals/commands");
    state.approvals = Array.isArray(data?.items) ? data.items : Array.isArray(data) ? data : [];
    render();
  } catch (err) {
    console.warn("[hud] loadApprovals failed:", err);
  }
}

function scheduleMicroRefresh() {
  if (microRefreshTimer) return;
  microRefreshTimer = setTimeout(async () => {
    microRefreshTimer = null;
    await loadMicro();
  }, 200);
}

function scheduleMacroRefresh() {
  if (macroRefreshTimer) return;
  macroRefreshTimer = setTimeout(async () => {
    macroRefreshTimer = null;
    await Promise.all([loadProjectsCatalog(), loadApprovals()]);
  }, 600);
}

/* ==========================================================================
   Session Selection & Derived Metrics
   ========================================================================== */

function getCurrentSession() {
  const sessions = state.micro?.sessions || [];
  if (sessions.length === 0) return null;

  if (state.selectedSessionMode !== "auto") {
    const exact = sessions.find((s) => s.session_id === state.selectedSessionMode);
    if (exact) return exact;
  }

  if (state.micro?.active_session_id) {
    const active = sessions.find((s) => s.session_id === state.micro.active_session_id);
    if (active) return active;
  }

  const running = sessions.find((s) => s.status === "running" || s.status === "blocked" || s.status === "stalled");
  return running || sessions[0];
}

function getContextLevel(ctx) {
  if (!ctx) return { level: "ok", pct: 0, used: 0, window: 258000, softWarn: 180000, handoffTarget: 220000 };
  const used = Number(ctx.used_tokens || 0);
  const win = Number(ctx.window_tokens || 258000) || 258000;
  const softWarn = Number(ctx.soft_warn_tokens || 180000);
  const handoffTarget = Number(ctx.handoff_target_tokens || 220000);
  const pct = ctx.pct !== undefined && ctx.pct !== null
    ? Math.min(100, Math.max(0, Math.round(Number(ctx.pct))))
    : Math.min(100, Math.round((used / win) * 100));

  let level = ctx.level || "ok";
  if (used >= handoffTarget) level = "critical";
  else if (used >= softWarn && level === "ok") level = "warn";

  return { level, pct, used, window: win, softWarn, handoffTarget };
}

function getUpstreamAlert(upstream) {
  if (!upstream) return null;
  const streak = Number(upstream.streak_504_413 || 0);
  const retries429 = Number(upstream.retries_429 || 0);
  const stalled = Boolean(upstream.stalled_over_10m);
  const lvl = String(upstream.alert_level || "").toLowerCase();

  if (stalled || streak >= 2 || lvl === "critical" || lvl === "error") {
    const msg =
      upstream.alert_message ||
      (stalled
        ? "上游请求超过 10 分钟无响应，建议检查 Magpie 路由或重启精简线程"
        : `连续 ${streak} 次 504/413 上游失败，建议先查网关日志并避免继续膨胀上下文`);
    return { level: "critical", message: msg };
  }
  if (retries429 > 0 || lvl === "warn" || lvl === "warning") {
    const msg = upstream.alert_message || `上游限流重试（429 × ${retries429}），当前响应可能偏慢`;
    return { level: "warn", message: msg };
  }
  return null;
}

function getRelevantApprovals(session) {
  const pid = session?.project_id || null;
  const sorted = [...state.approvals].sort((a, b) => {
    const aMatch = pid && a.project_id === pid ? 1 : 0;
    const bMatch = pid && b.project_id === pid ? 1 : 0;
    return bMatch - aMatch;
  });
  return sorted;
}

function getAlertCount(session) {
  let count = 0;
  const approvals = getRelevantApprovals(session);
  const projApprovals = Number(session?.gates?.approvals_pending ?? 0);
  count += Math.max(approvals.length, projApprovals);

  if (session) {
    if (getUpstreamAlert(session.upstream)) count += 1;
    if (normalizeGateInfo(session.gates?.figure).status === "fail") count += 1;
    if (normalizeGateInfo(session.gates?.prose).status === "fail") count += 1;
  }
  return count;
}

/* ==========================================================================
   Rendering: Collapsed Pill (290×42)
   ========================================================================== */

function renderCollapsedPill(session) {
  const alertCount = getAlertCount(session);
  const ctx = getContextLevel(session?.context_health);
  const upAlert = getUpstreamAlert(session?.upstream);

  if (!session) {
    return `
      <div class="hud-pill" data-action="expand" title="点击展开小研实时进度卡">
        <div class="hud-pill-status">
          <span class="status-dot ${state.sseStatus === "connected" ? "green" : "muted"}"></span>
        </div>
        <div class="hud-pill-text">
          <div class="hud-pill-top">
            <span class="hud-pill-proj">小研进度</span>
            <span class="hud-pill-count">空闲</span>
          </div>
          <div class="hud-pill-step">等待 Codex / ChatGPT Work 会话…</div>
        </div>
        <div class="hud-pill-right">
          ${alertCount > 0 ? `<span class="hud-alert-dot amber" title="待审批 ${alertCount} 项">${alertCount}</span>` : ""}
          <button class="hud-icon-btn" type="button" data-action="expand" aria-label="展开进度卡">⌄</button>
        </div>
      </div>
    `;
  }

  const st = session.status || "idle";
  let statusIcon = `<span class="status-dot muted"></span>`;
  if (st === "running") statusIcon = `<span class="spinner"></span>`;
  else if (st === "blocked" || st === "stalled") statusIcon = `<span class="status-dot red"></span>`;
  else if (alertCount > 0) statusIcon = `<span class="status-dot amber"></span>`;
  else statusIcon = `<span class="status-dot green"></span>`;

  const pillTone =
    st === "blocked" || st === "stalled" || upAlert?.level === "critical"
      ? "state-blocked"
      : ctx.level !== "ok" || upAlert?.level === "warn"
      ? "state-warn"
      : "";

  const projLabel = session.project_name || (session.project_id ? session.project_id : "未归属会话");
  const stepLabel = session.current_step_title || session.title || (st === "idle" ? "会话空闲" : "执行中…");
  const totalSteps = Number(session.steps_total ?? (session.steps?.length || 0));
  const doneSteps = Number(session.steps_done ?? 0);
  const countStr = totalSteps > 0 ? `${doneSteps}/${totalSteps}` : "";

  const tokenClass = ctx.level === "critical" ? "critical" : ctx.level === "warn" ? "warn" : "";
  const tokenText = ctx.used > 0 ? `${ctx.pct}%` : "0%";
  const tokenTitle = `上下文 ${formatTokens(ctx.used)} / ${formatTokens(ctx.window)} (${ctx.pct}%)`;

  return `
    <div class="hud-pill ${pillTone}" data-action="expand" title="点击展开详细进度">
      <div class="hud-pill-status">${statusIcon}</div>
      <div class="hud-pill-text">
        <div class="hud-pill-top">
          <span class="hud-pill-proj">${escapeHtml(truncate(projLabel, 14))}</span>
          ${countStr ? `<span class="hud-pill-count">${escapeHtml(countStr)}</span>` : ""}
        </div>
        <div class="hud-pill-step">${escapeHtml(stepLabel)}</div>
      </div>
      <div class="hud-pill-right">
        <span class="hud-token-chip ${tokenClass}" title="${escapeHtml(tokenTitle)}">${escapeHtml(tokenText)}</span>
        ${alertCount > 0 ? `<span class="hud-alert-dot ${upAlert?.level === "critical" || st === "blocked" ? "" : "amber"}" title="告警/待审 ${alertCount}">${alertCount}</span>` : ""}
        <button class="hud-icon-btn" type="button" data-action="expand" aria-label="展开进度卡">⌄</button>
      </div>
    </div>
  `;
}

/* ==========================================================================
   Rendering: Expanded Cowork Card (376×540)
   ========================================================================== */

function stepBadgeInfo(normStatus) {
  if (normStatus === "done") return { text: "✓ 已完成", cls: "done" };
  if (normStatus === "running") return { text: "⟳ 进行中", cls: "running" };
  if (normStatus === "waiting_approval") return { text: "⏸ 待审批", cls: "waiting_approval" };
  if (normStatus === "blocked") return { text: "✕ 阻断", cls: "blocked" };
  if (normStatus === "failed") return { text: "✕ 失败", cls: "failed" };
  if (normStatus === "skipped") return { text: "– 已跳过", cls: "skipped" };
  return { text: "○ 未完成", cls: "pending" };
}

function renderStepItem(step, idx) {
  const stepId = step.id || `step-${idx}`;
  const normStatus = normalizeStepStatus(step.status);
  const badge = stepBadgeInfo(normStatus);
  const children = Array.isArray(step.children) ? step.children : [];
  const isOpen = state.expandedStepIds.has(stepId);
  const elapsed = step.elapsed_ms ? formatDuration(step.elapsed_ms) : "";
  const roleText = roleLabel(step.role);

  const childrenHtml = children.length > 0
    ? `
      <button class="hud-children-toggle" type="button" data-toggle-step="${escapeHtml(stepId)}">
        <span>${isOpen ? "▾" : "▸"}</span>
        <span>${children.length} 项子操作</span>
      </button>
      ${
        isOpen
          ? `<div class="hud-children">
              ${children
                .map((ch) => {
                  const cStatus = normalizeStepStatus(ch.status);
                  const cElapsed = ch.elapsed_ms ? formatDuration(ch.elapsed_ms) : "";
                  return `
                    <div class="hud-child-row">
                      <div style="display:flex;align-items:center;gap:5px;min-width:0;flex:1;">
                        ${stepStatusIconHtml(cStatus)}
                        <span class="truncate" title="${escapeHtml(ch.title || ch.detail || "")}">${escapeHtml(ch.title || ch.kind || "子调用")}</span>
                      </div>
                      ${cElapsed ? `<span class="mono" style="font-size:10px;color:var(--text-3);flex-shrink:0;">${escapeHtml(cElapsed)}</span>` : ""}
                    </div>
                  `;
                })
                .join("")}
            </div>`
          : ""
      }
    `
    : "";

  return `
    <div class="hud-step status-${normStatus}">
      <div class="hud-step-row">
        ${stepStatusIconHtml(normStatus)}
        <div class="hud-step-body">
          <div class="hud-step-head">
            <div class="hud-step-title">${escapeHtml(step.title || "未命名步骤")}</div>
            <span class="hud-step-badge ${badge.cls}">${badge.text}</span>
          </div>
          <div class="hud-step-meta">
            ${roleText ? `<span class="badge neutral" style="font-size:9.5px;padding:0 5px;">${escapeHtml(roleText)}</span>` : ""}
            ${step.kind ? `<span class="mono">${escapeHtml(step.kind)}</span>` : ""}
            ${elapsed ? `<span class="mono">${escapeHtml(elapsed)}</span>` : ""}
            ${step.artifact_ref ? `<span class="mono truncate" style="max-width:150px;" title="${escapeHtml(step.artifact_ref)}">${escapeHtml(step.artifact_ref)}</span>` : ""}
          </div>
          ${step.detail ? `<div class="hud-step-detail">${escapeHtml(truncate(step.detail, 120))}</div>` : ""}
          ${childrenHtml}
        </div>
      </div>
    </div>
  `;
}

function renderSubagentCards(subagents) {
  if (!Array.isArray(subagents) || subagents.length === 0) return "";
  const cards = subagents
    .map((sa) => {
      const used = Number(sa.used_tokens || 0);
      const budget = 120000;
      const pct = Math.min(100, Math.round((used / budget) * 100));
      const fillTone = used >= 110000 ? "critical" : used >= 90000 ? "warn" : "";
      const norm = normalizeStepStatus(sa.status);
      return `
        <div class="hud-subagent-card">
          <div class="hud-subagent-top">
            <div style="display:flex;align-items:center;gap:5px;min-width:0;">
              ${stepStatusIconHtml(norm)}
              <span class="truncate">${escapeHtml(sa.nickname || sa.thread_id?.slice(0, 8) || "子代理")}</span>
              ${sa.role ? `<span class="badge neutral" style="font-size:9.5px;padding:0 5px;">${escapeHtml(roleLabel(sa.role))}</span>` : ""}
            </div>
            <span class="mono" style="font-size:10px;color:${fillTone === "critical" ? "var(--red)" : fillTone === "warn" ? "var(--amber)" : "var(--text-3)"};">
              ${formatTokens(used)} / 120k
            </span>
          </div>
          ${sa.current_step ? `<div class="truncate" style="font-size:10.5px;color:var(--text-3);">${escapeHtml(sa.current_step)}</div>` : ""}
          <div class="hud-subagent-bar" title="子代理预算上限 120k tokens (${pct}%)">
            <div class="hud-subagent-fill ${fillTone}" style="width:${pct}%"></div>
          </div>
        </div>
      `;
    })
    .join("");

  return `
    <div class="hud-section-bar" style="margin-top:4px;">
      <span>并发子代理（${subagents.length}）</span>
      <span>预算上限 120k</span>
    </div>
    <div class="hud-step-list">${cards}</div>
  `;
}

function renderApprovalTray(session) {
  if (!state.showApprovalTray) return "";
  const items = getRelevantApprovals(session);
  if (items.length === 0) {
    return `
      <div class="hud-approval-tray">
        <div style="font-size:11px;color:var(--green);text-align:center;padding:6px 0;">✓ 当前无待签署的审批项</div>
      </div>
    `;
  }

  const cards = items
    .map((item) => {
      const conflict = Boolean(item.governance_conflict);
      const canLaunchNative = NATIVE && item.executable !== false && !conflict;
      let actionHtml = "";
      if (conflict) {
        actionHtml = `
          <span class="badge red" style="font-size:10px;">治理冲突·需先核查</span>
          ${item.display_cmd ? `<button class="btn btn-xs" type="button" data-copy-cmd="${escapeHtml(item.display_cmd)}">复制命令</button>` : ""}
        `;
      } else if (canLaunchNative) {
        actionHtml = `
          <button class="btn btn-xs btn-primary" type="button" data-launch-approval="${escapeHtml(item.id)}">弹终端签署</button>
          ${item.display_cmd ? `<button class="btn btn-xs btn-ghost" type="button" data-copy-cmd="${escapeHtml(item.display_cmd)}">复制</button>` : ""}
        `;
      } else {
        actionHtml = `
          <button class="btn btn-xs btn-amber" type="button" data-copy-cmd="${escapeHtml(item.display_cmd || "")}" ${item.display_cmd ? "" : "disabled"}>复制命令</button>
        `;
      }

      return `
        <div class="hud-approval-card ${conflict ? "conflict" : ""}">
          <div class="hud-approval-title">${escapeHtml(item.title || item.target_label || item.id)}</div>
          <div class="hud-approval-meta">
            <span>${escapeHtml(item.project_name || "工作流全局")}</span>
            ${item.version ? `<span class="mono">v${escapeHtml(String(item.version).replace(/^v/i, ""))}</span>` : ""}
            ${item.sha12 ? `<span class="mono">#${escapeHtml(item.sha12)}</span>` : ""}
          </div>
          <div style="display:flex;align-items:center;justify-content:flex-end;gap:5px;">
            ${actionHtml}
          </div>
        </div>
      `;
    })
    .join("");

  return `
    <div class="hud-approval-tray">
      <div style="display:flex;align-items:center;justify-content:space-between;font-size:10.5px;color:var(--text-3);">
        <strong>待人工审批（${items.length}）</strong>
        <button class="hud-icon-btn" type="button" data-action="toggle-approvals" aria-label="收起审批">✕</button>
      </div>
      ${cards}
    </div>
  `;
}

function renderExpandedCard(session) {
  const sessions = state.micro?.sessions || [];
  const approvals = getRelevantApprovals(session);

  // Topbar session options
  const sessionOptions = [
    `<option value="auto" ${state.selectedSessionMode === "auto" ? "selected" : ""}>自动跟随 (${sessions.length})</option>`,
    ...sessions.map((s) => {
      const icon = s.status === "running" ? "●" : s.status === "blocked" || s.status === "stalled" ? "!" : "○";
      const label = `${icon} ${truncate(s.project_name || s.title || s.session_id.slice(0, 8), 22)}`;
      return `<option value="${escapeHtml(s.session_id)}" ${state.selectedSessionMode === s.session_id ? "selected" : ""}>${escapeHtml(label)}</option>`;
    }),
  ].join("");

  // Body content
  let bodyHtml = "";
  let contextBlockHtml = "";
  let figInfo = { label: "出图", tone: "neutral", status: "unknown" };
  let proseInfo = { label: "写作", tone: "neutral", status: "unknown" };

  if (!session) {
    bodyHtml = `
      <div class="hud-empty">
        <div class="hud-empty-icon">研</div>
        <div style="font-size:12.5px;font-weight:600;color:var(--text-1);">当前无活跃会话</div>
        <div style="font-size:11.5px;line-height:1.5;">等待 Codex / ChatGPT Work 启动。<br/>后台 SSE ${state.sseStatus === "connected" ? "已连接，启动后将自动呈现步骤进度" : "正在重连中…"}。</div>
        <button class="btn btn-sm btn-primary" type="button" data-action="open-dashboard">打开宏观总览大盘 ↗</button>
      </div>
    `;
  } else {
    const ctx = getContextLevel(session.context_health);
    const upAlert = getUpstreamAlert(session.upstream);
    const isUnattributed = !session.project_id || session.attribution_confidence === "unattributed";
    const handoffCmd = session.context_health?.handoff_cmd || "";
    const showProminentHandoff = Boolean(handoffCmd && (ctx.level === "warn" || ctx.level === "critical" || ctx.used >= 180000));

    const warnTickPct = Math.min(98, Math.round((ctx.softWarn / ctx.window) * 100));
    const criticalTickPct = Math.min(99, Math.round((ctx.handoffTarget / ctx.window) * 100));

    const attrPickerHtml = (isUnattributed || state.showAttributionPicker)
      ? `
        <select class="hud-attr-select" id="hud-attr-select" aria-label="手动指定所属课题">
          <option value="">${isUnattributed ? "⚠ 未归属课题 — 点击手动关联…" : "选择所属课题…"}</option>
          ${state.projects
            .map(
              (p) =>
                `<option value="${escapeHtml(p.id)}" ${p.id === session.project_id ? "selected" : ""}>${escapeHtml(p.name || p.id)}</option>`
            )
            .join("")}
        </select>
      `
      : "";

    contextBlockHtml = `
      <div class="hud-context-block">
        <div class="hud-attr-row">
          <div style="display:flex;align-items:center;gap:5px;min-width:0;">
            <span class="hud-proj-pill ${isUnattributed ? "unattributed" : ""}" data-action="open-project" title="${isUnattributed ? "点击下方下拉框手动归属课题" : "点击在宏观大盘查看该课题"}">
              <span class="truncate">${escapeHtml(session.project_name || (isUnattributed ? "未归属课题" : session.project_id))}</span>
            </span>
            <button class="hud-icon-btn" type="button" data-action="toggle-attr" title="修改会话所属课题">✎</button>
          </div>
          <span class="mono" style="font-size:10px;color:var(--text-3);" title="最近更新">
            ${escapeHtml(truncate(session.model || session.originator || "", 16))}
          </span>
        </div>

        ${attrPickerHtml}

        <div>
          <div class="hud-meter-header">
            <span>上下文 <strong class="mono">${formatTokens(ctx.used)}</strong> / ${formatTokens(ctx.window)} (${ctx.pct}%)</span>
            ${
              showProminentHandoff
                ? `<button class="btn btn-xs btn-amber" type="button" data-copy-cmd="${escapeHtml(handoffCmd)}">📋 复制交接命令</button>`
                : handoffCmd
                ? `<button class="btn btn-xs btn-ghost" type="button" data-copy-cmd="${escapeHtml(handoffCmd)}" title="复制交接脚本命令">交接命令</button>`
                : `<span style="font-size:10px;color:var(--text-3);">${formatRelativeTime(session.updated_at)}</span>`
            }
          </div>
          <div class="hud-meter-track" title="软提醒 180k · 建议交接 220k">
            <div class="hud-meter-fill ${ctx.level !== "ok" ? ctx.level : ""}" style="width:${ctx.pct}%"></div>
            <div class="hud-meter-tick" style="left:${warnTickPct}%" title="180k 软提醒"></div>
            <div class="hud-meter-tick" style="left:${criticalTickPct}%" title="220k 交接阈值"></div>
          </div>
        </div>

        ${
          upAlert
            ? `<div class="hud-upstream-banner ${upAlert.level}">
                <span>${upAlert.level === "critical" ? "⚠" : "◔"}</span>
                <span>${escapeHtml(upAlert.message)}</span>
              </div>`
            : ""
        }
      </div>
    `;

    const steps = Array.isArray(session.steps) ? session.steps : [];
    const stepStatuses = steps.map((s) => normalizeStepStatus(s.status));
    const doneSteps = stepStatuses.filter((st) => st === "done").length;
    const activeSteps = stepStatuses.filter((st) => st === "running" || st === "waiting_approval" || st === "blocked" || st === "failed").length;
    const alertStepExists = stepStatuses.some((st) => st === "waiting_approval" || st === "blocked" || st === "failed");
    const todoSteps = stepStatuses.filter((st) => st === "pending").length;
    const totalSteps = Number(session.steps_total ?? steps.length);
    const planSourceLabel = session.plan_source === "update_plan" ? "计划驱动" : "动作观测";

    const flowStepsHtml = steps
      .map((s, i) => {
        const st = stepStatuses[i];
        const arrow = i < steps.length - 1
          ? `<div class="hud-step-arrow ${st}" aria-hidden="true">↓</div>`
          : "";
        return `${renderStepItem(s, i)}${arrow}`;
      })
      .join("");

    bodyHtml = `
      <div class="hud-scroll">
        <div class="hud-summary-strip" aria-label="步骤状态汇总">
          <div class="hud-summary-pill done">✓ 已完成 ${doneSteps}</div>
          <div class="hud-summary-pill running ${alertStepExists ? "has-alert" : ""}">${alertStepExists ? "⏸ 待处理" : "⟳ 进行中"} ${activeSteps}</div>
          <div class="hud-summary-pill todo">○ 未完成 ${todoSteps}</div>
        </div>
        <div class="hud-section-bar">
          <span>执行流程图（${doneSteps}/${totalSteps}）</span>
          <span class="badge neutral" style="font-size:9.5px;padding:1px 6px;">${planSourceLabel}</span>
        </div>
        ${
          steps.length > 0
            ? `<div class="hud-flow-list">${flowStepsHtml}</div>`
            : `<div style="padding:18px 8px;text-align:center;color:var(--text-3);font-size:11.5px;">暂无结构化步骤，等待下一轮工具调用…</div>`
        }
        ${renderSubagentCards(session.subagents)}
      </div>
    `;

    figInfo = normalizeGateInfo(session.gates?.figure);
    proseInfo = normalizeGateInfo(session.gates?.prose);
  }

  const approvalCount = Math.max(approvals.length, Number(session?.gates?.approvals_pending || 0));

  return `
    <div class="hud-card">
      <div class="hud-topbar">
        <span class="hud-drag-grip" title="可拖动浮窗">⋮⋮</span>
        <select id="hud-session-select" class="hud-session-select" aria-label="切换追踪会话">
          ${sessionOptions}
        </select>
        <button class="btn btn-xs" type="button" data-action="open-dashboard" title="在浏览器打开宏观大盘">大盘</button>
        <button class="hud-icon-btn" type="button" data-action="collapse" title="折叠为胶囊">⌃</button>
        ${NATIVE ? `<button class="hud-icon-btn" type="button" data-action="hide" title="隐藏浮窗（可从菜单栏重新打开）">×</button>` : ""}
      </div>

      ${contextBlockHtml}
      ${bodyHtml}
      ${renderApprovalTray(session)}

      <div class="hud-footer">
        <div class="hud-gate-pills">
          <span class="hud-gate-btn ${figInfo.tone === "neutral" ? "" : figInfo.tone}" title="出图质检门禁：${escapeHtml(figInfo.detail || figInfo.label)}">
            图 ${escapeHtml(figInfo.label)}
          </span>
          <span class="hud-gate-btn ${proseInfo.tone === "neutral" ? "" : proseInfo.tone}" title="写作去AI门禁：${escapeHtml(proseInfo.detail || proseInfo.label)}">
            文 ${escapeHtml(proseInfo.label)}
          </span>
          <button
            class="hud-gate-btn ${approvalCount > 0 ? "amber" : ""} ${state.showApprovalTray ? "active" : ""}"
            type="button"
            data-action="toggle-approvals"
            title="查看待人工审批项"
          >
            待审批 ${approvalCount}
          </button>
        </div>
        <span class="status-dot ${state.sseStatus === "connected" ? "green" : state.sseStatus === "reconnecting" ? "amber" : "muted"}" title="SSE 状态：${state.sseStatus}"></span>
      </div>
    </div>
  `;
}

function render() {
  const session = getCurrentSession();
  if (state.mode === "collapsed") {
    dom.root.innerHTML = renderCollapsedPill(session);
  } else {
    dom.root.innerHTML = renderExpandedCard(session);
  }
}

/* ==========================================================================
   Event Delegation
   ========================================================================== */

function bindEvents() {
  if (NATIVE) {
    dom.root.addEventListener("mousedown", (e) => {
      if (e.button === 0 && e.target.closest(".hud-drag-grip")) {
        postNativeMessage({ type: "startDrag" });
      }
    });
  }

  dom.root.addEventListener("click", async (e) => {
    const toggleStepBtn = e.target.closest("[data-toggle-step]");
    if (toggleStepBtn) {
      e.stopPropagation();
      const sid = toggleStepBtn.dataset.toggleStep;
      if (state.expandedStepIds.has(sid)) state.expandedStepIds.delete(sid);
      else state.expandedStepIds.add(sid);
      render();
      return;
    }

    const copyBtn = e.target.closest("[data-copy-cmd]");
    if (copyBtn) {
      e.stopPropagation();
      const ok = await copyText(copyBtn.dataset.copyCmd);
      showToast(ok ? "已复制命令到剪贴板" : "复制失败", ok ? "success" : "error", 1800);
      return;
    }

    const launchBtn = e.target.closest("[data-launch-approval]");
    if (launchBtn) {
      e.stopPropagation();
      if (NATIVE) {
        postNativeMessage({ type: "launchApproval", approvalId: launchBtn.dataset.launchApproval });
      }
      return;
    }

    const actionEl = e.target.closest("[data-action]");
    if (!actionEl) return;
    const action = actionEl.dataset.action;

    if (action === "expand") {
      applyMode("expanded", { notifyNative: true });
    } else if (action === "collapse") {
      applyMode("collapsed", { notifyNative: true });
    } else if (action === "hide") {
      postNativeMessage({ type: "hideHud" });
    } else if (action === "open-dashboard") {
      const s = getCurrentSession();
      openDashboard(s?.project_id || null);
    } else if (action === "open-project") {
      const s = getCurrentSession();
      if (s?.project_id) openDashboard(s.project_id);
      else {
        state.showAttributionPicker = true;
        render();
      }
    } else if (action === "toggle-attr") {
      state.showAttributionPicker = !state.showAttributionPicker;
      render();
    } else if (action === "toggle-approvals") {
      state.showApprovalTray = !state.showApprovalTray;
      render();
    }
  });

  dom.root.addEventListener("change", async (e) => {
    if (e.target.id === "hud-session-select") {
      state.selectedSessionMode = e.target.value;
      state.showAttributionPicker = false;
      render();
      return;
    }
    if (e.target.id === "hud-attr-select") {
      const projectId = e.target.value;
      const session = getCurrentSession();
      if (!session || !projectId) return;
      try {
        state.prefs = await updatePrefs(state.prefs, (draft) => {
          draft.session_project_overrides[session.session_id] = projectId;
          return draft;
        });
        state.showAttributionPicker = false;
        showToast("已更新会话所属课题", "success", 1800);
        await loadMicro();
      } catch (err) {
        showToast(`保存归属失败：${err.message}`, "error");
      }
    }
  });

  if (!NATIVE && dom.previewToggle) {
    dom.previewToggle.addEventListener("click", () => {
      applyMode(state.mode === "collapsed" ? "expanded" : "collapsed", { notifyNative: false });
    });
  }
}

/* ==========================================================================
   Boot
   ========================================================================== */

async function init() {
  if (!NATIVE) {
    document.body.classList.add("is-browser-preview");
    dom.previewNote?.classList.remove("hidden");
  }

  const params = new URLSearchParams(window.location.search);
  const initialMode = params.get("mode");
  if (initialMode === "expanded" || initialMode === "collapsed") {
    state.mode = initialMode;
  }

  bindEvents();
  render();

  const [prefs] = await Promise.all([
    fetchPrefs(),
    loadMicro(),
    loadProjectsCatalog(),
    loadApprovals(),
  ]);
  state.prefs = prefs;
  render();

  createEventStream({
    onStatusChange: (status) => {
      state.sseStatus = status;
      render();
    },
    onMicro: (payload) => {
      if (payload && Array.isArray(payload.sessions)) {
        state.micro = payload;
        render();
      } else {
        scheduleMicroRefresh();
      }
    },
    onMacro: () => {
      scheduleMacroRefresh();
    },
  });

  // Fallback poll if SSE temporarily drops
  setInterval(() => {
    if (state.sseStatus !== "connected") {
      loadMicro();
    }
  }, 15000);
}

init();
