/**
 * dashboard.js — Macro Research Dashboard ("把握整体大方向")
 * Pure ES module, zero build step, offline-ready.
 */

import {
  CATEGORY_LABELS,
  PROOF_META,
  STAGE_META,
  STAGE_STATE_META,
  STAGES_ORDER,
  apiGet,
  copyText,
  createEventStream,
  defaultPrefs,
  escapeHtml,
  fetchPrefs,
  formatDateTime,
  formatRelativeTime,
  formatTokens,
  normalizeGateInfo,
  normalizeStages,
  showToast,
  truncate,
  updatePrefs,
} from "./shared.js";

const state = {
  macro: null,
  micro: null,
  approvals: [],
  prefs: defaultPrefs(),
  sseStatus: "connecting",

  // Filters & UI state
  viewMode: "flowchart", // "flowchart" (default) | "matrix"
  search: "",
  categoryTab: "active", // active (v3+v2) | all | v3 | v2 | custom
  showSystem: false,
  onlyApprovals: false,
  onlyRisk: false,
  onlyPinned: false,
  sortBy: "default",
  queueCollapsed: false,

  // Drawer state
  selectedProjectId: null,
  selectedDetail: null,
  detailLoading: false,
};

let macroRefreshTimer = null;
let microRefreshTimer = null;

/* ==========================================================================
   DOM References
   ========================================================================== */

const dom = {
  updatedAt: document.getElementById("dash-updated-at"),
  ssePill: document.getElementById("sse-pill"),
  hmacPill: document.getElementById("hmac-pill"),
  btnTheme: document.getElementById("btn-theme"),
  btnRefresh: document.getElementById("btn-refresh"),

  kpiApprovals: document.getElementById("kpi-approvals"),
  kpiApprovalsSub: document.getElementById("kpi-approvals-sub"),
  kpiGates: document.getElementById("kpi-gates"),
  kpiGatesSub: document.getElementById("kpi-gates-sub"),
  kpiDrift: document.getElementById("kpi-drift"),
  kpiDriftSub: document.getElementById("kpi-drift-sub"),
  kpiHmac: document.getElementById("kpi-hmac"),
  kpiHmacSub: document.getElementById("kpi-hmac-sub"),
  kpiSessions: document.getElementById("kpi-sessions"),
  kpiSessionsSub: document.getElementById("kpi-sessions-sub"),
  kpiProjects: document.getElementById("kpi-projects"),
  kpiProjectsSub: document.getElementById("kpi-projects-sub"),

  queuePanel: document.getElementById("queue-panel"),
  queueToggle: document.getElementById("queue-toggle"),
  queueChevron: document.getElementById("queue-chevron"),
  queueCountBadge: document.getElementById("queue-count-badge"),
  queueBody: document.getElementById("queue-body"),

  searchInput: document.getElementById("search-input"),
  searchClear: document.getElementById("search-clear"),
  categoryTabs: document.getElementById("category-tabs"),
  viewTabs: document.getElementById("view-tabs"),
  toggleSystem: document.getElementById("toggle-system"),
  chipApprovals: document.getElementById("chip-approvals"),
  chipRisk: document.getElementById("chip-risk"),
  chipPinned: document.getElementById("chip-pinned"),
  sortSelect: document.getElementById("sort-select"),

  flowchartView: document.getElementById("flowchart-view"),
  flowCountInfo: document.getElementById("flow-count-info"),
  flowchartList: document.getElementById("flowchart-list"),

  matrixView: document.getElementById("matrix-view"),
  matrixCountInfo: document.getElementById("matrix-count-info"),
  matrixTbody: document.getElementById("matrix-tbody"),

  drawerBackdrop: document.getElementById("drawer-backdrop"),
  drawer: document.getElementById("detail-drawer"),
  drawerTitle: document.getElementById("drawer-title"),
  drawerSub: document.getElementById("drawer-sub"),
  drawerPinBtn: document.getElementById("drawer-pin-btn"),
  drawerCloseBtn: document.getElementById("drawer-close-btn"),
  drawerNav: document.getElementById("drawer-nav"),
  drawerBody: document.getElementById("drawer-body"),
};

/* ==========================================================================
   Data Fetching & SSE Synchronization
   ========================================================================== */

async function loadMacro(forceRefresh = false) {
  try {
    const url = forceRefresh ? "/api/macro?refresh=1" : "/api/macro";
    const data = await apiGet(url);
    state.macro = data;
    renderAll();
    if (state.selectedProjectId) {
      loadProjectDetail(state.selectedProjectId, { silent: true });
    }
  } catch (err) {
    console.error("[dashboard] loadMacro failed:", err);
    showToast(`宏观状态加载失败：${err.message}`, "error");
  }
}

async function loadMicro() {
  try {
    state.micro = await apiGet("/api/micro");
    renderKpis();
    renderProjectsView();
    if (state.selectedProjectId) renderDrawerHeaderMeta();
  } catch (err) {
    console.warn("[dashboard] loadMicro failed:", err);
  }
}

async function loadApprovals() {
  try {
    const data = await apiGet("/api/approvals/commands");
    state.approvals = Array.isArray(data?.items) ? data.items : Array.isArray(data) ? data : [];
    renderQueue();
    renderKpis();
  } catch (err) {
    console.warn("[dashboard] loadApprovals failed:", err);
  }
}

async function loadProjectDetail(projectId, { silent = false } = {}) {
  if (!projectId) return;
  if (!silent) {
    state.detailLoading = true;
    renderDrawer();
  }
  try {
    const encodedPath = String(projectId)
      .split("/")
      .map((seg) => encodeURIComponent(seg))
      .join("/");
    const detail = await apiGet(`/api/macro/projects/${encodedPath}`);
    if (state.selectedProjectId === projectId) {
      state.selectedDetail = detail;
      state.detailLoading = false;
      renderDrawer();
    }
  } catch (err) {
    if (state.selectedProjectId === projectId) {
      state.detailLoading = false;
      if (!silent) showToast(`加载课题详情失败：${err.message}`, "error");
      renderDrawer();
    }
  }
}

function scheduleMacroRefresh() {
  if (macroRefreshTimer) return;
  macroRefreshTimer = setTimeout(async () => {
    macroRefreshTimer = null;
    await Promise.all([loadMacro(false), loadApprovals()]);
  }, 450);
}

function scheduleMicroRefresh() {
  if (microRefreshTimer) return;
  microRefreshTimer = setTimeout(async () => {
    microRefreshTimer = null;
    await loadMicro();
  }, 250);
}

/* ==========================================================================
   Derived Helpers
   ========================================================================== */

function getActiveSessionsByProject() {
  const map = new Map();
  const sessions = state.micro?.sessions || [];
  for (const s of sessions) {
    if (!s?.project_id) continue;
    const isActive = s.status === "running" || s.status === "blocked" || s.status === "stalled";
    if (!isActive) continue;
    if (!map.has(s.project_id)) map.set(s.project_id, []);
    map.get(s.project_id).push(s);
  }
  return map;
}

function isPinned(project) {
  const set = new Set(state.prefs?.pinned_projects || []);
  return Boolean(project?.pinned || set.has(project?.id));
}

function projectHasGateFailure(p) {
  const g = p?.gates || {};
  if (Number(g.figure_fail || 0) > 0) return true;
  if (g.prose_status === "stale" || normalizeGateInfo(g.prose_status).status === "fail") return true;
  if (normalizeGateInfo(g.audit_status).status === "fail") return true;
  if (normalizeGateInfo(g.publication_gate).status === "fail") return true;
  const stages = normalizeStages(p?.stages);
  return STAGES_ORDER.some((id) => stages[id]?.state === "blocked");
}

function projectHasRisk(p) {
  return Boolean(
    p?.stage_skip_alert ||
      p?.primary_contract?.governance_conflict ||
      p?.workflow_state_drift?.drifted ||
      projectHasGateFailure(p)
  );
}

function projectApprovalCount(p) {
  const fromProject = Array.isArray(p?.pending_approvals) ? p.pending_approvals.length : 0;
  const fromCmds = state.approvals.filter((a) => a.project_id === p?.id).length;
  return Math.max(fromProject, fromCmds);
}

function getFilteredProjects() {
  const all = state.macro?.projects || [];
  const q = state.search.trim().toLowerCase();

  let list = all.filter((p) => {
    const cat = p.category || "custom";
    if (cat === "system" && !state.showSystem && state.categoryTab !== "all") {
      return false;
    }
    if (cat === "system" && !state.showSystem && state.categoryTab === "all") {
      return false;
    }

    if (state.categoryTab === "active") {
      if (cat !== "v3_formal" && cat !== "v2_formal") return false;
    } else if (state.categoryTab === "v3") {
      if (cat !== "v3_formal" && cat !== "v3_partial") return false;
    } else if (state.categoryTab === "v2") {
      if (cat !== "v2_formal") return false;
    } else if (state.categoryTab === "custom") {
      if (cat !== "custom" && cat !== "v3_partial") return false;
    }

    if (state.onlyApprovals && projectApprovalCount(p) === 0) return false;
    if (state.onlyRisk && !projectHasRisk(p)) return false;
    if (state.onlyPinned && !isPinned(p)) return false;

    if (q) {
      const hay = [
        p.name,
        p.id,
        p.rel_path,
        p.primary_contract?.contract_id,
        p.primary_contract?.sha12,
      ]
        .filter(Boolean)
        .join(" ")
        .toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });

  const activeMap = getActiveSessionsByProject();

  list = [...list].sort((a, b) => {
    if (state.sortBy === "name") {
      return String(a.name || a.id).localeCompare(String(b.name || b.id), "zh-CN");
    }
    if (state.sortBy === "risk") {
      const score = (p) =>
        (p.stage_skip_alert ? 100 : 0) +
        (p.primary_contract?.governance_conflict ? 80 : 0) +
        (projectHasGateFailure(p) ? 60 : 0) +
        projectApprovalCount(p) * 30 +
        (p.workflow_state_drift?.drifted ? 20 : 0) +
        (activeMap.has(p.id) ? 15 : 0);
      const diff = score(b) - score(a);
      if (diff !== 0) return diff;
    }
    // Default: pinned first -> active session -> recent activity -> name
    const pinDiff = Number(isPinned(b)) - Number(isPinned(a));
    if (pinDiff !== 0) return pinDiff;
    const actDiff = Number(activeMap.has(b.id)) - Number(activeMap.has(a.id));
    if (actDiff !== 0) return actDiff;
    const tA = Date.parse(a.last_activity_at || "") || 0;
    const tB = Date.parse(b.last_activity_at || "") || 0;
    if (tB !== tA) return tB - tA;
    return String(a.name || a.id).localeCompare(String(b.name || b.id), "zh-CN");
  });

  return list;
}

/* ==========================================================================
   Rendering: Header, KPIs, Queue, Matrix
   ========================================================================== */

function renderSseStatus() {
  const s = state.sseStatus;
  if (s === "connected") {
    dom.ssePill.className = "badge green";
    dom.ssePill.innerHTML = `<span class="status-dot green"></span><span>SSE 实时同步</span>`;
  } else if (s === "reconnecting") {
    dom.ssePill.className = "badge amber";
    dom.ssePill.innerHTML = `<span class="status-dot amber"></span><span>SSE 重连中</span>`;
  } else {
    dom.ssePill.className = "badge neutral";
    dom.ssePill.innerHTML = `<span class="status-dot muted"></span><span>SSE 连接中</span>`;
  }
}

function renderKpis() {
  const summary = state.macro?.summary || {};
  const projects = state.macro?.projects || [];

  if (state.macro?.generated_at) {
    dom.updatedAt.textContent = `只读快照更新于 ${formatDateTime(state.macro.generated_at)} (${formatRelativeTime(state.macro.generated_at)})`;
  }

  const pendingApprovals = Math.max(
    Number(summary.pending_approvals ?? 0),
    state.approvals.length
  );
  dom.kpiApprovals.textContent = String(pendingApprovals);
  dom.kpiApprovals.className = `kpi-value ${pendingApprovals > 0 ? "amber" : "green"}`;
  dom.kpiApprovalsSub.textContent = pendingApprovals > 0
    ? `${state.approvals.length} 条可复制终端命令`
    : "当前无待签审批";

  const gateFailures = Number(summary.gate_failures ?? projects.filter(projectHasGateFailure).length);
  dom.kpiGates.textContent = String(gateFailures);
  dom.kpiGates.className = `kpi-value ${gateFailures > 0 ? "red" : "green"}`;
  dom.kpiGatesSub.textContent = gateFailures > 0 ? "点击筛选阻断课题" : "全部已检门禁通过";

  const visibleScope = projects.filter((p) => !p.hidden && p.category !== "system");
  const skipCount = visibleScope.filter((p) => Boolean(p.stage_skip_alert)).length;
  const driftCount = visibleScope.filter((p) => Boolean(p.workflow_state_drift?.drifted)).length;
  dom.kpiDrift.textContent = `${skipCount} / ${driftCount}`;
  dom.kpiDrift.className = `kpi-value ${skipCount > 0 ? "red" : driftCount > 0 ? "amber" : "green"}`;
  dom.kpiDriftSub.textContent = `跳阶 ${skipCount} · 漂移 ${driftCount}`;

  const authPresent = summary.authority_present;
  if (authPresent === true) {
    dom.kpiHmac.textContent = "已验真";
    dom.kpiHmac.className = "kpi-value green";
    dom.kpiHmacSub.textContent = "本地 HMAC 密钥就绪";
    dom.hmacPill.className = "badge green";
    dom.hmacPill.textContent = "✓ HMAC 验真启用";
  } else if (authPresent === false) {
    dom.kpiHmac.textContent = "未验真";
    dom.kpiHmac.className = "kpi-value amber";
    dom.kpiHmacSub.textContent = "缺少本地 receipt-authority";
    dom.hmacPill.className = "badge amber";
    dom.hmacPill.textContent = "? 缺 HMAC 密钥（只读）";
  } else {
    dom.kpiHmac.textContent = "—";
    dom.kpiHmac.className = "kpi-value";
  }

  const activeCount = Number(state.micro?.active_count ?? 0);
  const totalSess = (state.micro?.sessions || []).length;
  dom.kpiSessions.textContent = String(activeCount);
  dom.kpiSessions.className = `kpi-value ${activeCount > 0 ? "blue" : ""}`;
  dom.kpiSessionsSub.textContent = totalSess > 0
    ? `近两日共追踪 ${totalSess} 个会话`
    : "等待 Codex / Work 启动";

  const filtered = getFilteredProjects();
  const totalProjects = Number(summary.total_projects ?? projects.length);
  dom.kpiProjects.textContent = `${filtered.length}`;
  dom.kpiProjects.className = "kpi-value";
  dom.kpiProjectsSub.textContent = `总发现 ${totalProjects} 项（已显示 ${filtered.length}）`;
}

function buildActionQueueItems() {
  const items = [];
  const cmdSourcePaths = new Set();
  const cmdProjectContracts = new Set();
  const cmdSnapshotKinds = new Set();

  // 1. Executable / governance-conflict TTY approval commands
  for (const cmd of state.approvals) {
    if (cmd.source_path) cmdSourcePaths.add(String(cmd.source_path));
    if (cmd.project_id && cmd.target_label) {
      cmdProjectContracts.add(`${cmd.project_id}::${cmd.target_label}`);
    }
    if (cmd.kind === "zw30_snapshot" || cmd.kind === "baseline_snapshots") {
      cmdSnapshotKinds.add(cmd.kind);
    }
    items.push({
      key: `cmd:${cmd.id}`,
      kind: cmd.governance_conflict ? "conflict" : "approval",
      badgeText: cmd.governance_conflict ? "治理冲突" : "待 TTY 签署",
      badgeTone: cmd.governance_conflict ? "red" : "amber",
      projectId: cmd.project_id || null,
      projectName: cmd.project_name || "工作流全局",
      title: cmd.title || cmd.target_label || cmd.id,
      meta: [cmd.version, cmd.sha12 ? `#${cmd.sha12}` : "", cmd.preflight_passed === false ? "预检未过" : ""]
        .filter(Boolean)
        .join(" · "),
      copyCmd: cmd.display_cmd || "",
    });
  }

  // 2. Project-level skip alerts, gate blocks, and non-command pending decisions
  const projects = state.macro?.projects || [];
  for (const p of projects) {
    if (p.category === "system" || p.hidden) continue;
    if (p.stage_skip_alert) {
      items.push({
        key: `skip:${p.id}`,
        kind: "skip",
        badgeText: "跳阶告警",
        badgeTone: "red",
        projectId: p.id,
        projectName: p.name || p.id,
        title: p.stage_skip_reason || "存在后续阶段产出但缺少前置正式已发布证据",
        meta: CATEGORY_LABELS[p.category] || p.category || "",
        copyCmd: "",
      });
    }
    if (projectHasGateFailure(p)) {
      const reasons = [];
      if (Number(p.gates?.figure_fail || 0) > 0) reasons.push(`出图失败 ${p.gates.figure_fail}`);
      if (p.gates?.prose_status === "stale") reasons.push("写作旁证过期");
      else if (normalizeGateInfo(p.gates?.prose_status).status === "fail") reasons.push("写作门禁阻断");
      if (normalizeGateInfo(p.gates?.audit_status).status === "fail") reasons.push("审计未通过");
      if (normalizeGateInfo(p.gates?.publication_gate).status === "fail") {
        reasons.push(p.gates?.publication_gate === "changes_required" ? "投稿门禁需修改" : "投稿门禁阻断");
      }
      if (reasons.length > 0) {
        items.push({
          key: `gate:${p.id}`,
          kind: "gate",
          badgeText: "门禁阻断",
          badgeTone: "red",
          projectId: p.id,
          projectName: p.name || p.id,
          title: reasons.join(" · "),
          meta: p.primary_contract?.contract_id || "",
          copyCmd: "",
        });
      }
    }
    if (Array.isArray(p.pending_approvals)) {
      for (const pa of p.pending_approvals) {
        const isObj = pa && typeof pa === "object";
        const txt = typeof pa === "string" ? pa : (pa?.title || pa?.summary || pa?.reason || "");
        if (!txt) continue;

        const paKind = isObj ? String(pa.kind || "") : "";
        const paSeverity = isObj ? String(pa.severity || "") : "";
        const paSource = isObj ? String(pa.source_path || pa.source || "") : "";
        const paContractId = isObj ? String(pa.contract_id || pa.target_label || "") : "";

        // Deduplicate against /api/approvals/commands:
        // cmd.source_path is absolute, cmd.target_label is project-relative (matching pa.source_path)
        const coveredByCmd =
          (paSource && cmdSourcePaths.has(paSource)) ||
          (paSource && cmdProjectContracts.has(`${p.id}::${paSource}`)) ||
          (paContractId && cmdProjectContracts.has(`${p.id}::${paContractId}`)) ||
          (paKind === "snapshot_approval" && cmdSnapshotKinds.size > 0) ||
          items.some((x) => x.projectId === p.id && x.title === txt);
        if (coveredByCmd) continue;

        let badgeText = "待处理";
        let badgeTone = "amber";
        let itemKind = "decision";
        if (paSeverity === "blocker" || paKind === "handoff_blocker") {
          badgeText = "阻塞项";
          badgeTone = "red";
          itemKind = "blocker";
        } else if (paSeverity === "conflict" || paKind === "governance_conflict") {
          badgeText = "治理冲突";
          badgeTone = "red";
          itemKind = "conflict";
        } else if (paKind === "publication_exception") {
          badgeText = "投稿例外";
          badgeTone = "amber";
        } else if (paKind === "unresolved_item") {
          badgeText = "合同待决";
          badgeTone = "amber";
        } else if (paSeverity === "decision" || paKind === "handoff_decision") {
          badgeText = "C 级决策";
          badgeTone = "amber";
        } else if (paKind === "sign_contract" || paKind === "snapshot_approval" || paSeverity === "action") {
          badgeText = "待审批";
          badgeTone = "amber";
          itemKind = "approval";
        }

        const shortSource = paSource ? paSource.split("/").slice(-2).join("/") : "";
        const metaStr = [paContractId, shortSource].filter(Boolean).join(" · ");

        items.push({
          key: `pa:${p.id}:${paKind}:${txt.slice(0, 24)}`,
          kind: itemKind,
          badgeText,
          badgeTone,
          projectId: p.id,
          projectName: p.name || p.id,
          title: txt,
          meta: metaStr,
          copyCmd: isObj ? (pa.display_cmd || "") : "",
        });
      }
    }
  }

  return items;
}

function renderQueue() {
  const items = buildActionQueueItems();
  dom.queueCountBadge.textContent = String(items.length);
  dom.queueCountBadge.className = `badge ${items.length > 0 ? "amber" : "green"}`;
  dom.queuePanel.classList.toggle("has-urgent", items.length > 0);
  dom.queueBody.classList.toggle("collapsed", state.queueCollapsed);
  dom.queueChevron.textContent = state.queueCollapsed ? "▸" : "▾";
  dom.queueToggle.setAttribute("aria-expanded", String(!state.queueCollapsed));

  if (items.length === 0) {
    dom.queueBody.innerHTML = `<div style="color: var(--green); font-size: 12px;">✓ 当前无待人工审批、C 级决策或门禁阻断项。</div>`;
    return;
  }

  const maxShow = 12;
  const shown = items.slice(0, maxShow);
  const rowsHtml = shown
    .map((item) => {
      const copyBtn = item.copyCmd
        ? `<button class="btn btn-xs btn-amber" type="button" data-copy-cmd="${escapeHtml(item.copyCmd)}">📋 复制终端审批命令</button>`
        : "";
      const openBtn = item.projectId
        ? `<button class="btn btn-xs" type="button" data-open-project="${escapeHtml(item.projectId)}">查看课题</button>`
        : "";
      return `
        <div class="queue-item">
          <div class="queue-item-main">
            <span class="badge ${item.badgeTone}">${escapeHtml(item.badgeText)}</span>
            <strong>${escapeHtml(item.projectName)}</strong>
            <span style="color: var(--text-2);">${escapeHtml(item.title)}</span>
            ${item.meta ? `<span class="mono" style="font-size:11px;color:var(--text-3);">${escapeHtml(item.meta)}</span>` : ""}
          </div>
          <div class="queue-item-actions">
            ${copyBtn}
            ${openBtn}
          </div>
        </div>
      `;
    })
    .join("");

  const overflowNote = items.length > maxShow
    ? `<div style="font-size:11.5px;color:var(--text-3);padding-top:2px;">仅展示前 ${maxShow} 项，共 ${items.length} 项（可点击各课题行在右侧抽屉查看完整清单）。</div>`
    : "";

  dom.queueBody.innerHTML = rowsHtml + overflowNote;
}

function renderStageCell(stageObj) {
  const meta = STAGE_STATE_META[stageObj?.state] || STAGE_STATE_META.not_started;
  const proofKey = stageObj?.proof || "unknown";
  const proofInfo = PROOF_META[proofKey] || PROOF_META.unknown;
  const stageName = stageObj?.name || STAGE_META[stageObj?.id]?.name || stageObj?.id || "";
  const tooltip = [
    `${stageObj?.id || ""} ${stageName}`,
    `状态：${meta.label}`,
    `依据：${proofInfo.label}`,
    stageObj?.summary ? `摘要：${stageObj.summary}` : "",
  ]
    .filter(Boolean)
    .join("\n");

  const showProofDot = stageObj?.state !== "not_started";
  return `
    <td>
      <span class="stage-pill ${meta.css}" title="${escapeHtml(tooltip)}">
        <span>${meta.icon}</span>
        <span>${meta.label}</span>
        ${showProofDot ? `<span class="proof-dot ${escapeHtml(proofKey)}"></span>` : ""}
      </span>
    </td>
  `;
}

function renderExploratoryCell(track) {
  const cards = Number(track?.card_count || 0);
  const runs = Number(track?.candidate_runs || 0);
  const hasActivity = Boolean(track?.has_activity || cards > 0 || runs > 0);
  if (!hasActivity) {
    return `<td><span class="explore-pill empty">—</span></td>`;
  }
  const label = track?.badge || `${cards}卡 / ${runs}跑`;
  return `
    <td>
      <span class="explore-pill" title="独立探索轨（candidate_not_released，不阻塞主线）：探索卡 ${cards} · 候选运行 ${runs}">
        ✦ ${escapeHtml(label)}
      </span>
    </td>
  `;
}

function renderFlowNode(stageObj) {
  const st = stageObj?.state || "not_started";
  const stageId = stageObj?.id || "";
  const shortName = STAGE_META[stageId]?.short || stageObj?.name || stageId;
  const fullName = stageObj?.name || STAGE_META[stageId]?.name || stageId;
  const proofKey = stageObj?.proof || "unknown";
  const proofInfo = PROOF_META[proofKey] || PROOF_META.unknown;

  let statusIcon = "○";
  let statusText = "未完成";
  if (st === "done") {
    statusIcon = "✓";
    statusText = "已完成";
  } else if (st === "partial") {
    statusIcon = "⟳";
    statusText = "进行中";
  } else if (st === "waiting_approval") {
    statusIcon = "⏸";
    statusText = "待审批";
  } else if (st === "blocked") {
    statusIcon = "✕";
    statusText = "阻断";
  } else if (st === "unverified") {
    statusIcon = "?";
    statusText = "未验真";
  }

  const tooltip = [
    `${stageId} ${fullName}`,
    `状态：${statusText}`,
    `依据：${proofInfo.label}`,
    stageObj?.summary ? `说明：${stageObj.summary}` : "",
  ]
    .filter(Boolean)
    .join("\n");

  const summaryText = stageObj?.summary
    ? truncate(stageObj.summary, 30)
    : st === "not_started"
    ? "待推进"
    : proofInfo.short;

  return `
    <div class="flow-node node-${escapeHtml(st)}" data-stage-id="${escapeHtml(stageId)}" title="${escapeHtml(tooltip)}">
      <div class="flow-node-top">
        <span class="flow-node-id">${escapeHtml(stageId)}</span>
        ${st !== "not_started" ? `<span class="proof-dot ${escapeHtml(proofKey)}"></span>` : ""}
      </div>
      <div class="flow-node-name">${escapeHtml(shortName)}</div>
      <div class="flow-node-status"><span>${statusIcon}</span><span>${statusText}</span></div>
      <div class="flow-node-summary">${escapeHtml(summaryText)}</div>
    </div>
  `;
}

function getArrowTone(prevState, nextState) {
  if (prevState === "done") {
    if (nextState === "partial") return "arrow-active";
    if (nextState === "blocked") return "arrow-blocked";
    if (nextState === "waiting_approval" || nextState === "unverified") return "arrow-warn";
    return "arrow-done";
  }
  if (prevState === "partial") return "arrow-active";
  if (prevState === "blocked") return "arrow-blocked";
  if (prevState === "waiting_approval" || prevState === "unverified") return "arrow-warn";
  return "arrow-pending";
}

function renderFlowchart() {
  const projects = getFilteredProjects();
  const total = (state.macro?.projects || []).length;
  dom.flowCountInfo.textContent = `显示 ${projects.length} / ${total} 个课题（点击任意卡片或阶段节点展开右侧详情抽屉）`;

  if (!state.macro) {
    dom.flowchartList.innerHTML = `<div class="matrix-card empty-table-state">正在加载科研课题流程图…</div>`;
    return;
  }

  if (projects.length === 0) {
    dom.flowchartList.innerHTML = `<div class="matrix-card empty-table-state">没有符合当前筛选条件的课题，可尝试切换分类标签或清除搜索词。</div>`;
    return;
  }

  const activeMap = getActiveSessionsByProject();

  dom.flowchartList.innerHTML = projects
    .map((p) => {
      const pinned = isPinned(p);
      const stages = normalizeStages(p.stages);
      const activeSessions = activeMap.get(p.id) || [];
      const primary = p.primary_contract;
      const approvalCnt = projectApprovalCount(p);
      const isSelected = state.selectedProjectId === p.id;
      const catLabel = CATEGORY_LABELS[p.category] || p.category || "未知";

      const doneStages = [];
      const activeStages = [];
      const todoStages = [];

      for (const id of STAGES_ORDER) {
        const st = stages[id];
        if (st.state === "done") doneStages.push(st);
        else if (st.state === "not_started") todoStages.push(st);
        else activeStages.push(st);
      }

      const donePct = Math.round((doneStages.length / STAGES_ORDER.length) * 100);
      const hasBlocker =
        Boolean(p.stage_skip_alert) ||
        Boolean(primary?.governance_conflict) ||
        activeStages.some((st) => st.state === "blocked" || st.state === "waiting_approval" || st.state === "unverified");

      // Horizontal S0 -> S7 nodes + directional arrows
      const flowTrackHtml = STAGES_ORDER.map((id, idx) => {
        const nodeHtml = renderFlowNode(stages[id]);
        if (idx === STAGES_ORDER.length - 1) return nodeHtml;
        const nextId = STAGES_ORDER[idx + 1];
        const arrowCls = getArrowTone(stages[id].state, stages[nextId].state);
        return `${nodeHtml}<div class="flow-arrow ${arrowCls}" aria-hidden="true">→</div>`;
      }).join("");

      // Parallel exploratory branch
      const track = p.exploratory_track || {};
      const expCards = Number(track.card_count || 0);
      const expRuns = Number(track.candidate_runs || 0);
      const hasExplore = Boolean(track.has_activity || expCards > 0 || expRuns > 0);
      const exploreHtml = hasExplore
        ? `
          <div class="flow-explore-branch">
            <span>↳ <strong>并行探索轨</strong>（<code>candidate_not_released</code>，不阻塞主线）</span>
            <span class="mono">✦ 探索卡 ${expCards} · 候选运行 ${expRuns}${track.badge ? ` · ${escapeHtml(track.badge)}` : ""}</span>
          </div>
        `
        : "";

      // Header pills
      const headerTags = [
        `<span class="badge neutral">${escapeHtml(catLabel)}</span>`,
        p.layout ? `<span class="badge neutral mono">${escapeHtml(p.layout)}</span>` : "",
      ];
      if (primary) {
        const cTone = primary.governance_conflict ? "amber" : primary.status === "frozen" ? "green" : "neutral";
        const cLabel = primary.sha12 ? `${primary.status || "contract"} #${primary.sha12.slice(0, 7)}` : (primary.status || "合同");
        headerTags.push(`<span class="badge ${cTone} mono" title="主线合同：${escapeHtml(primary.contract_id || "")}">${escapeHtml(cLabel)}</span>`);
      }
      if (p.stage_skip_alert) {
        headerTags.push(`<span class="badge red" title="${escapeHtml(p.stage_skip_reason || "存在跳阶风险")}">⚠ 跳阶</span>`);
      }
      if (primary?.governance_conflict) {
        headerTags.push(`<span class="badge amber" title="冻结状态与人工签署记录存在治理冲突">⚠ 治理冲突</span>`);
      }
      if (p.workflow_state_drift?.drifted) {
        headerTags.push(`<span class="badge amber" title="workflow_state=${escapeHtml(p.workflow_state_drift.recorded || "无")} vs 派生=${escapeHtml(p.workflow_state_drift.derived || "无")}">⇄ 状态漂移</span>`);
      }
      if (approvalCnt > 0) {
        headerTags.push(`<span class="badge amber">⏸ 待审批 ${approvalCnt}</span>`);
      }
      if (activeSessions.length > 0) {
        const liveStep = activeSessions[0].current_step_title || activeSessions[0].title || "运行中";
        headerTags.push(
          `<span class="badge blue" title="当前活跃会话：${escapeHtml(liveStep)}"><span class="live-dot" style="width:6px;height:6px;"></span> 运行中：${escapeHtml(truncate(liveStep, 20))}</span>`
        );
      }

      // 3 Color-Coded Status Breakdown Sections
      const doneChipsHtml = doneStages.length > 0
        ? doneStages
            .map((st) => `<span class="badge green">✓ ${escapeHtml(st.id)} ${escapeHtml(STAGE_META[st.id]?.short || st.name || "")}</span>`)
            .join("")
        : `<span class="flow-panel-empty">暂无已完成阶段</span>`;

      const activeItemsHtml = [];
      for (const st of activeStages) {
        const m = STAGE_STATE_META[st.state] || STAGE_STATE_META.partial;
        const tone = st.state === "blocked" ? "red" : st.state === "partial" ? "blue" : "amber";
        const detail = st.summary ? ` · ${truncate(st.summary, 18)}` : "";
        activeItemsHtml.push(
          `<span class="badge ${tone}" title="${escapeHtml(st.summary || "")}">${m.icon} ${escapeHtml(st.id)} ${escapeHtml(STAGE_META[st.id]?.short || "")} (${m.label}${escapeHtml(detail)})</span>`
        );
      }
      if (p.stage_skip_alert) {
        activeItemsHtml.push(`<span class="badge red">⚠ 跳阶：${escapeHtml(truncate(p.stage_skip_reason || "缺少 S3 正式发布证据", 24))}</span>`);
      }
      if (primary?.governance_conflict) {
        activeItemsHtml.push(`<span class="badge amber">⚠ 合同冻结与签署记录冲突</span>`);
      }
      const activeSectionHtml = activeItemsHtml.length > 0
        ? activeItemsHtml.join("")
        : `<span class="flow-panel-empty">当前无进行中或待处理项</span>`;

      const todoChipsHtml = todoStages.length > 0
        ? todoStages
            .map((st) => `<span class="badge neutral">○ ${escapeHtml(st.id)} ${escapeHtml(STAGE_META[st.id]?.short || st.name || "")}</span>`)
            .join("")
        : `<span class="flow-panel-empty" style="color:var(--green);">🎉 全流程 8 阶段均已推进</span>`;

      return `
        <article class="flow-card ${isSelected ? "is-selected" : ""} ${activeSessions.length > 0 ? "is-live" : ""}" data-project-id="${escapeHtml(p.id)}">
          <div class="flow-card-head">
            <div class="flow-card-title-row">
              <button class="pin-btn ${pinned ? "pinned" : ""}" type="button" data-pin-id="${escapeHtml(p.id)}" title="${pinned ? "取消置顶" : "置顶课题"}">${pinned ? "★" : "☆"}</button>
              <span class="flow-card-name">${escapeHtml(p.name || p.id)}</span>
              ${headerTags.filter(Boolean).join("")}
            </div>
            <div class="flow-card-right">
              <span class="flow-progress-box" title="已完成 ${doneStages.length} / ${STAGES_ORDER.length} 阶段 (${donePct}%)">
                <span>已完成 ${doneStages.length}/${STAGES_ORDER.length}</span>
                <span class="flow-progress-bar"><span class="flow-progress-fill" style="width:${donePct}%"></span></span>
              </span>
              <button class="btn btn-xs" type="button" data-open-detail="${escapeHtml(p.id)}">详情 ▸</button>
            </div>
          </div>

          <div class="flow-track-scroll">
            <div class="flow-track">${flowTrackHtml}</div>
          </div>

          ${exploreHtml}

          <div class="flow-panels">
            <div class="flow-panel panel-done">
              <div class="flow-panel-head">
                <span>✅ 已完成板块</span>
                <span>${doneStages.length} 项</span>
              </div>
              <div class="flow-panel-chips">${doneChipsHtml}</div>
            </div>

            <div class="flow-panel panel-active ${hasBlocker ? "has-blocker" : ""}">
              <div class="flow-panel-head">
                <span>${hasBlocker ? "🟠 进行中 / 待处理板块" : "🔵 进行中板块"}</span>
                <span>${activeItemsHtml.length} 项</span>
              </div>
              <div class="flow-panel-chips">${activeSectionHtml}</div>
            </div>

            <div class="flow-panel panel-todo">
              <div class="flow-panel-head">
                <span>⬜ 未完成板块</span>
                <span>${todoStages.length} 项</span>
              </div>
              <div class="flow-panel-chips">${todoChipsHtml}</div>
            </div>
          </div>
        </article>
      `;
    })
    .join("");
}

function renderProjectsView() {
  const isFlow = state.viewMode === "flowchart";
  dom.flowchartView?.classList.toggle("hidden", !isFlow);
  dom.matrixView?.classList.toggle("hidden", isFlow);
  if (dom.viewTabs) {
    for (const el of dom.viewTabs.querySelectorAll(".seg-btn")) {
      el.classList.toggle("active", el.dataset.view === state.viewMode);
    }
  }
  if (isFlow) {
    renderFlowchart();
  } else {
    renderMatrix();
  }
}

function renderMatrix() {
  const projects = getFilteredProjects();
  const total = (state.macro?.projects || []).length;
  dom.matrixCountInfo.textContent = `显示 ${projects.length} / ${total} 个课题（点击任意行展开右侧详情抽屉）`;

  if (!state.macro) {
    dom.matrixTbody.innerHTML = `<tr><td colspan="10" class="empty-table-state">正在加载课题数据…</td></tr>`;
    return;
  }

  if (projects.length === 0) {
    dom.matrixTbody.innerHTML = `<tr><td colspan="10" class="empty-table-state">没有符合当前筛选条件的课题，可尝试切换分类标签或清除搜索词。</td></tr>`;
    return;
  }

  const activeMap = getActiveSessionsByProject();

  dom.matrixTbody.innerHTML = projects
    .map((p) => {
      const pinned = isPinned(p);
      const stages = normalizeStages(p.stages);
      const activeSessions = activeMap.get(p.id) || [];
      const primary = p.primary_contract;
      const approvalCnt = projectApprovalCount(p);
      const isSelected = state.selectedProjectId === p.id;
      const catLabel = CATEGORY_LABELS[p.category] || p.category || "未知";

      const liveHtml = activeSessions.length > 0
        ? `<span class="live-dot" title="活跃会话进行中：${escapeHtml(activeSessions[0].current_step_title || activeSessions[0].title || "运行中")}"></span>`
        : "";

      const tags = [];
      tags.push(`<span class="badge neutral" style="font-size:10px;padding:1px 5px;">${escapeHtml(catLabel)}</span>`);
      if (primary) {
        const cTone = primary.governance_conflict ? "amber" : primary.status === "frozen" ? "green" : "neutral";
        const cLabel = primary.sha12 ? `${primary.status || "contract"} #${primary.sha12.slice(0, 7)}` : (primary.status || "合同");
        tags.push(`<span class="badge ${cTone} mono" style="font-size:10px;padding:1px 5px;" title="主线合同：${escapeHtml(primary.contract_id || "")}">${escapeHtml(cLabel)}</span>`);
      }
      if (p.stage_skip_alert) {
        tags.push(`<span class="badge red" style="font-size:10px;padding:1px 5px;" title="${escapeHtml(p.stage_skip_reason || "存在跳阶风险")}">跳阶</span>`);
      }
      if (primary?.governance_conflict) {
        tags.push(`<span class="badge amber" style="font-size:10px;padding:1px 5px;" title="合同冻结状态与人工签署记录存在治理冲突">冲突</span>`);
      }
      if (p.workflow_state_drift?.drifted) {
        tags.push(`<span class="badge amber" style="font-size:10px;padding:1px 5px;" title="legacy workflow_state=${escapeHtml(p.workflow_state_drift.recorded || "无")} vs 派生=${escapeHtml(p.workflow_state_drift.derived || "无")}">漂移</span>`);
      }
      if (approvalCnt > 0) {
        tags.push(`<span class="badge amber" style="font-size:10px;padding:1px 5px;">待审 ${approvalCnt}</span>`);
      }

      const stageCells = STAGES_ORDER.map((id) => renderStageCell(stages[id])).join("");

      return `
        <tr data-project-id="${escapeHtml(p.id)}" class="${isSelected ? "is-selected" : ""}">
          <td class="col-project">
            <div class="proj-cell-top">
              <button class="pin-btn ${pinned ? "pinned" : ""}" type="button" data-pin-id="${escapeHtml(p.id)}" title="${pinned ? "取消置顶" : "置顶课题"}">${pinned ? "★" : "☆"}</button>
              ${liveHtml}
              <span class="proj-name" title="${escapeHtml(p.rel_path || p.name || p.id)}">${escapeHtml(p.name || p.id)}</span>
            </div>
            <div class="proj-cell-sub">${tags.join("")}</div>
          </td>
          ${stageCells}
          ${renderExploratoryCell(p.exploratory_track)}
        </tr>
      `;
    })
    .join("");
}

/* ==========================================================================
   Rendering: Right Slide-Over Detail Drawer
   ========================================================================== */

function openDrawer(projectId) {
  if (!projectId) return;
  state.selectedProjectId = projectId;
  state.selectedDetail = null;
  dom.drawerBackdrop.classList.add("open");
  dom.drawerBackdrop.setAttribute("aria-hidden", "false");
  dom.drawer.classList.add("open");

  const url = new URL(window.location.href);
  url.searchParams.set("project", projectId);
  window.history.replaceState({}, "", url.toString());

  renderProjectsView();
  loadProjectDetail(projectId);
}

function closeDrawer() {
  state.selectedProjectId = null;
  state.selectedDetail = null;
  state.detailLoading = false;
  dom.drawerBackdrop.classList.remove("open");
  dom.drawerBackdrop.setAttribute("aria-hidden", "true");
  dom.drawer.classList.remove("open");

  const url = new URL(window.location.href);
  url.searchParams.delete("project");
  window.history.replaceState({}, "", url.toString());

  renderProjectsView();
}

function getBaseProject(projectId) {
  return (state.macro?.projects || []).find((p) => p.id === projectId) || null;
}

function renderDrawerHeaderMeta() {
  const project = state.selectedDetail || getBaseProject(state.selectedProjectId);
  if (!project) return;
  const pinned = isPinned(project);
  const activeSessions = getActiveSessionsByProject().get(project.id) || [];

  dom.drawerTitle.textContent = project.name || project.id;
  dom.drawerPinBtn.textContent = pinned ? "★ 已置顶" : "☆ 置顶";
  dom.drawerPinBtn.className = `btn btn-sm ${pinned ? "btn-amber" : ""}`;

  const subParts = [
    `<span class="badge neutral">${escapeHtml(CATEGORY_LABELS[project.category] || project.category || "未知")}</span>`,
    project.layout ? `<span class="badge neutral mono">${escapeHtml(project.layout)}</span>` : "",
    project.rel_path ? `<span class="mono">${escapeHtml(project.rel_path)}</span>` : "",
    activeSessions.length > 0
      ? `<span class="badge blue"><span class="live-dot" style="width:6px;height:6px;"></span> 活跃会话 ${activeSessions.length}</span>`
      : "",
    project.last_activity_at ? `<span>最近活动：${escapeHtml(formatRelativeTime(project.last_activity_at))}</span>` : "",
  ].filter(Boolean);

  dom.drawerSub.innerHTML = subParts.join("");
}

function renderArtifactList(artifacts) {
  if (!Array.isArray(artifacts) || artifacts.length === 0) return "";
  const rows = artifacts
    .map((a) => {
      const path = typeof a === "string" ? a : (a?.path || a?.rel_path || a?.file || a?.label || "");
      const label = typeof a === "object" && a?.label && a.label !== path ? `${a.label}: ` : "";
      const proof = typeof a === "object" && a?.proof ? a.proof : "";
      if (!path) return "";
      return `
        <li class="artifact-row">
          <span class="artifact-path" title="${escapeHtml(path)}">${escapeHtml(label)}${escapeHtml(path)}</span>
          ${proof ? `<span class="badge neutral" style="font-size:10px;padding:1px 5px;">${escapeHtml(PROOF_META[proof]?.short || proof)}</span>` : ""}
          <button class="btn btn-xs btn-ghost" type="button" data-copy-path="${escapeHtml(path)}">复制路径</button>
        </li>
      `;
    })
    .filter(Boolean)
    .join("");
  if (!rows) return "";
  return `
    <details class="artifact-details">
      <summary>查看产物路径（${artifacts.length}）</summary>
      <ul class="artifact-list">${rows}</ul>
    </details>
  `;
}

function renderDrawer() {
  const project = state.selectedDetail || getBaseProject(state.selectedProjectId);
  if (!project) {
    dom.drawerBody.innerHTML = `<div class="empty-table-state">未找到该课题（ID: ${escapeHtml(state.selectedProjectId || "")}）</div>`;
    return;
  }

  renderDrawerHeaderMeta();

  const stages = normalizeStages(project.stages);
  const contracts = Array.isArray(project.contracts) ? project.contracts : [];
  const primaryId = project.primary_contract?.contract_id || null;
  const runs = Array.isArray(project.runs) ? project.runs : [];
  const validations = Array.isArray(project.validations) ? project.validations : [];
  const releases = Array.isArray(project.releases) ? project.releases : [];
  const evidenceIds = Array.isArray(project.evidence_ids) ? project.evidence_ids : [];
  const handoffDecisions = Array.isArray(project.handoff_decisions) ? project.handoff_decisions : [];
  const approvalCommands = Array.isArray(project.approval_commands)
    ? project.approval_commands
    : state.approvals.filter((a) => a.project_id === project.id);

  // Alert banners
  const banners = [];
  if (project.stage_skip_alert) {
    banners.push(`<div class="alert-banner red"><strong>⚠ 跳阶告警：</strong><span>${escapeHtml(project.stage_skip_reason || "检测到后续阶段产物，但 S3 正式已发布证据缺失。")}</span></div>`);
  }
  if (project.primary_contract?.governance_conflict) {
    banners.push(`<div class="alert-banner amber"><strong>⚠ 治理冲突：</strong><span>主线合同标记为 frozen，但人工交互签署记录未闭环（pending_interactive_human_sign），请先核查。</span></div>`);
  }
  if (project.workflow_state_drift?.drifted) {
    banners.push(`<div class="alert-banner amber"><strong>⇄ 状态漂移：</strong><span>历史 workflow_state.json 记录为 <code>${escapeHtml(project.workflow_state_drift.recorded || "无")}</code>，与只读证据链派生阶段 <code>${escapeHtml(project.workflow_state_drift.derived || "无")}</code> 不一致（仪表盘只读不改写）。</span></div>`);
  }
  if (state.detailLoading) {
    banners.push(`<div class="alert-banner blue"><span class="spinner spinner-sm"></span><span>正在加载完整证据链与合同细节…</span></div>`);
  }

  // Section 1: Stage Timeline + Exploratory Track
  const timelineHtml = STAGES_ORDER.map((id) => {
    const st = stages[id];
    const stMeta = STAGE_STATE_META[st.state] || STAGE_STATE_META.not_started;
    const prMeta = PROOF_META[st.proof] || PROOF_META.unknown;
    return `
      <div class="timeline-item">
        <div class="timeline-head">
          <div style="display:flex;align-items:center;gap:7px;">
            <span class="timeline-stage-id">${escapeHtml(id)}</span>
            <strong>${escapeHtml(st.name || STAGE_META[id].name)}</strong>
          </div>
          <div style="display:flex;align-items:center;gap:6px;">
            <span class="badge ${stMeta.tone}">${stMeta.icon} ${stMeta.label}</span>
            <span class="badge ${prMeta.tone}" title="${escapeHtml(prMeta.label)}">${escapeHtml(prMeta.short)}</span>
          </div>
        </div>
        ${st.summary ? `<div class="timeline-summary">${escapeHtml(st.summary)}</div>` : ""}
        ${renderArtifactList(st.artifacts)}
      </div>
    `;
  }).join("");

  const track = project.exploratory_track || {};
  const exploratoryHtml = `
    <div class="timeline-item" style="border-color: var(--purple-border);">
      <div class="timeline-head">
        <div style="display:flex;align-items:center;gap:7px;">
          <span class="timeline-stage-id" style="color:var(--purple);">探索</span>
          <strong>独立探索轨（candidate_not_released）</strong>
        </div>
        <span class="badge purple">探索卡 ${Number(track.card_count || 0)} · 候选运行 ${Number(track.candidate_runs || 0)}</span>
      </div>
      <div class="timeline-summary">探索产出不阻塞主课题，也不作为论文正式发布证据。</div>
    </div>
  `;

  // Section 2: Multi-Contract Cards
  const contractsHtml = contracts.length === 0
    ? `<div style="color:var(--text-3);font-size:12px;">当前目录未发现正式研究合同（<code>research_contract.json</code> 或 <code>contracts/*.json</code>）。</div>`
    : `<div class="card-grid">
        ${contracts
          .map((c) => {
            const cid = c.contract_id || c.id || c.filename || "未命名合同";
            const isPrim = Boolean(c.is_primary || (primaryId && cid === primaryId));
            const unresolved = Array.isArray(c.unresolved_items) ? c.unresolved_items : [];
            const conflict = Boolean(c.governance_conflict);
            return `
              <div class="info-card ${isPrim ? "is-primary" : ""} ${conflict ? "has-conflict" : ""}">
                <div style="display:flex;align-items:flex-start;justify-content:space-between;gap:6px;">
                  <strong class="mono" style="font-size:12px;word-break:break-all;">${escapeHtml(cid)}</strong>
                  ${isPrim ? `<span class="badge blue">当前主线</span>` : `<button class="btn btn-xs" type="button" data-set-primary="${escapeHtml(cid)}">设为当前主线合同</button>`}
                </div>
                <div style="display:flex;align-items:center;gap:5px;flex-wrap:wrap;">
                  <span class="badge ${c.status === "frozen" ? "green" : c.status === "draft" ? "amber" : "neutral"}">${escapeHtml(c.status || "unknown")}</span>
                  ${c.version ? `<span class="badge neutral mono">v${escapeHtml(String(c.version).replace(/^v/i, ""))}</span>` : ""}
                  ${c.sha12 ? `<span class="badge neutral mono">#${escapeHtml(c.sha12)}</span>` : ""}
                  ${conflict ? `<span class="badge red">治理冲突</span>` : ""}
                </div>
                ${c.rel_path ? `<div class="mono" style="font-size:10.5px;color:var(--text-3);word-break:break-all;">${escapeHtml(c.rel_path)}</div>` : ""}
                ${
                  unresolved.length > 0
                    ? `<div style="font-size:11px;color:var(--amber);">待决项 (${unresolved.length})：${escapeHtml(
                        unresolved
                          .slice(0, 3)
                          .map((u) => (typeof u === "string" ? u : u.title || u.item || JSON.stringify(u)))
                          .join("；")
                      )}</div>`
                    : ""
                }
              </div>
            `;
          })
          .join("")}
      </div>`;

  // Section 3: Evidence Chain & Gates
  const g = project.gates || {};
  const figPass = Number(g.figure_pass || 0);
  const figFail = Number(g.figure_fail || 0);
  const figTone = figFail > 0 ? "red" : figPass > 0 ? "green" : "neutral";
  const proseInfo = normalizeGateInfo(g.prose_status);
  const auditInfo = normalizeGateInfo(g.audit_status);
  const pubInfo = normalizeGateInfo(g.publication_gate);

  const gatesHtml = `
    <div class="card-grid">
      <div class="info-card">
        <div class="kv-line"><span class="kv-key">出图质检（figure_gate）</span><span class="badge ${figTone}">${figFail > 0 ? `失败 ${figFail}` : figPass > 0 ? `通过 ${figPass}` : "暂无记录"}</span></div>
        <div style="font-size:11px;color:var(--text-3);">PASS ${figPass} · FAIL ${figFail}</div>
      </div>
      <div class="info-card">
        <div class="kv-line"><span class="kv-key">写作与去AI门禁（prose_gate）</span><span class="badge ${proseInfo.tone}">${escapeHtml(proseInfo.label)}</span></div>
        <div style="font-size:11px;color:var(--text-3);">${escapeHtml(proseInfo.detail || "核验 .prose.json 与活跃手稿哈希")}</div>
      </div>
      <div class="info-card">
        <div class="kv-line"><span class="kv-key">审计 / 模拟审稿 / 文献核验</span><span class="badge ${auditInfo.tone}">${escapeHtml(auditInfo.label)}</span></div>
        <div style="font-size:11px;color:var(--text-3);">${escapeHtml(auditInfo.detail || "七层审计与参考文献新鲜度")}</div>
      </div>
      <div class="info-card">
        <div class="kv-line"><span class="kv-key">投稿发布门（publication_gate）</span><span class="badge ${pubInfo.tone}">${escapeHtml(pubInfo.label)}</span></div>
        <div style="font-size:11px;color:var(--text-3);">${escapeHtml(pubInfo.detail || "投稿前总闸门状态")}</div>
      </div>
    </div>
  `;

  const evidenceRows = [];
  for (const r of runs.slice(0, 12)) {
    const id = typeof r === "string" ? r : (r.run_id || r.id || "run");
    const proof = typeof r === "object" ? (r.proof || r.status || "observed") : "observed";
    evidenceRows.push(`<tr><td>Run</td><td class="mono">${escapeHtml(id)}</td><td><span class="badge ${PROOF_META[proof]?.tone || "neutral"}">${escapeHtml(PROOF_META[proof]?.short || proof)}</span></td></tr>`);
  }
  for (const v of validations.slice(0, 12)) {
    const id = typeof v === "string" ? v : (v.validation_id || v.id || "val");
    const proof = typeof v === "object" ? (v.proof || v.status || "observed") : "observed";
    evidenceRows.push(`<tr><td>Validation</td><td class="mono">${escapeHtml(id)}</td><td><span class="badge ${PROOF_META[proof]?.tone || "neutral"}">${escapeHtml(PROOF_META[proof]?.short || proof)}</span></td></tr>`);
  }
  for (const rel of releases.slice(0, 12)) {
    const id = typeof rel === "string" ? rel : (rel.release_id || rel.id || "release");
    const proof = typeof rel === "object" ? (rel.proof || rel.status || "observed") : "observed";
    evidenceRows.push(`<tr><td>Release</td><td class="mono">${escapeHtml(id)}</td><td><span class="badge ${PROOF_META[proof]?.tone || "neutral"}">${escapeHtml(PROOF_META[proof]?.short || proof)}</span></td></tr>`);
  }

  const evidenceChainHtml = `
    <div style="display:flex;gap:8px;flex-wrap:wrap;font-size:11.5px;">
      <span class="badge neutral">Runs: ${runs.length}</span>
      <span class="badge neutral">Validations: ${validations.length}</span>
      <span class="badge neutral">Releases: ${releases.length}</span>
      <span class="badge green">Evidence IDs: ${evidenceIds.length}</span>
    </div>
    ${
      evidenceIds.length > 0
        ? `<div style="display:flex;gap:5px;flex-wrap:wrap;">
            ${evidenceIds
              .slice(0, 24)
              .map((ev) => {
                const label = typeof ev === "string" ? ev : (ev.evidence_id || ev.id || "");
                return `<span class="badge green mono" style="font-size:10.5px;">${escapeHtml(label)}</span>`;
              })
              .join("")}
            ${evidenceIds.length > 24 ? `<span class="badge neutral">+${evidenceIds.length - 24}</span>` : ""}
          </div>`
        : ""
    }
    ${
      evidenceRows.length > 0
        ? `<table class="mini-table">
            <thead><tr><th>类型</th><th>记录 ID</th><th>验真状态</th></tr></thead>
            <tbody>${evidenceRows.join("")}</tbody>
          </table>`
        : `<div style="color:var(--text-3);font-size:12px;">暂无已落盘的正式 Run / Validation / Release 记录。</div>`
    }
  `;

  // Section 4: Pending Approvals & Handoff Decisions (Strictly Read-Only on Web!)
  const approvalsHtml = `
    <div class="alert-banner blue">
      <span>🔒</span>
      <span>Web 大盘严格保持只读，不直接执行终端命令。请点击下方<strong>复制终端审批命令</strong>在终端签署，或通过<strong>原生 macOS HUD 菜单栏</strong>一键弹终端签署。</span>
    </div>
    ${
      approvalCommands.length > 0
        ? approvalCommands
            .map(
              (cmd) => `
                <div class="info-card ${cmd.governance_conflict ? "has-conflict" : ""}">
                  <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap;">
                    <strong>${escapeHtml(cmd.title || cmd.target_label || cmd.id)}</strong>
                    <div style="display:flex;gap:5px;">
                      ${cmd.governance_conflict ? `<span class="badge red">治理冲突</span>` : `<span class="badge amber">待人工签署</span>`}
                      ${cmd.sha12 ? `<span class="badge neutral mono">#${escapeHtml(cmd.sha12)}</span>` : ""}
                    </div>
                  </div>
                  ${cmd.display_cmd ? `<div class="code-box">${escapeHtml(cmd.display_cmd)}</div>` : ""}
                  <div style="display:flex;justify-content:flex-end;">
                    <button class="btn btn-sm btn-amber" type="button" data-copy-cmd="${escapeHtml(cmd.display_cmd || "")}" ${cmd.display_cmd ? "" : "disabled"}>
                      📋 复制终端审批命令
                    </button>
                  </div>
                </div>
              `
            )
            .join("")
        : `<div style="color:var(--text-3);font-size:12px;">该课题当前无待签署的 TTY 审批命令。</div>`
    }
    ${
      handoffDecisions.length > 0
        ? `<div class="info-card">
            <strong style="font-size:12px;">交接文档待用户决策（99_audit/handoff_*.md）</strong>
            <ul style="margin:4px 0 0;padding-left:18px;font-size:12px;color:var(--text-2);">
              ${handoffDecisions
                .map((d) => `<li>${escapeHtml(typeof d === "string" ? d : (d.text || d.title || JSON.stringify(d)))}</li>`)
                .join("")}
            </ul>
          </div>`
        : ""
    }
  `;

  dom.drawerBody.innerHTML = `
    ${banners.join("")}
    <section class="drawer-section" id="sec-stages">
      <div class="drawer-section-title">
        <span>阶段时间线（S0–S7 + 探索轨）</span>
      </div>
      <div class="timeline">
        ${timelineHtml}
        ${exploratoryHtml}
      </div>
    </section>

    <section class="drawer-section" id="sec-contracts">
      <div class="drawer-section-title">
        <span>多合同管理（${contracts.length}）</span>
        <span style="font-size:11px;font-weight:400;color:var(--text-3);">切换主线合同仅写本地 prefs.json</span>
      </div>
      ${contractsHtml}
    </section>

    <section class="drawer-section" id="sec-evidence">
      <div class="drawer-section-title">
        <span>门禁状态与正式证据链</span>
      </div>
      ${gatesHtml}
      ${evidenceChainHtml}
    </section>

    <section class="drawer-section" id="sec-approvals">
      <div class="drawer-section-title">
        <span>待审批命令与 C 级决策</span>
      </div>
      ${approvalsHtml}
    </section>
  `;
}

function renderAll() {
  renderSseStatus();
  renderKpis();
  renderQueue();
  renderProjectsView();
  if (state.selectedProjectId) {
    renderDrawer();
  }
}

/* ==========================================================================
   User Interactions & Preference Persistence
   ========================================================================== */

async function togglePin(projectId) {
  try {
    state.prefs = await updatePrefs(state.prefs, (draft) => {
      const idx = draft.pinned_projects.indexOf(projectId);
      if (idx >= 0) {
        draft.pinned_projects.splice(idx, 1);
      } else {
        draft.pinned_projects.push(projectId);
      }
      return draft;
    });
    showToast("已更新课题置顶状态", "success");
    renderProjectsView();
    if (state.selectedProjectId === projectId) renderDrawerHeaderMeta();
  } catch (err) {
    showToast(`保存置顶失败：${err.message}`, "error");
  }
}

async function setPrimaryContract(projectId, contractId) {
  try {
    state.prefs = await updatePrefs(state.prefs, (draft) => {
      draft.primary_contract_overrides[projectId] = contractId;
      return draft;
    });
    showToast(`已将 ${truncate(contractId, 28)} 设为当前主线合同`, "success");
    await loadMacro(true);
    await loadProjectDetail(projectId, { silent: false });
  } catch (err) {
    showToast(`切换主线合同失败：${err.message}`, "error");
  }
}

function toggleTheme() {
  const root = document.documentElement;
  const current = root.getAttribute("data-theme");
  const prefersDark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
  const isDarkNow = current ? current === "dark" : prefersDark;
  const next = isDarkNow ? "light" : "dark";
  root.setAttribute("data-theme", next);
  try {
    localStorage.setItem("xy_theme", next);
  } catch {
    // ignore
  }
  showToast(`已切换为${next === "dark" ? "深色" : "浅色"}主题`, "info", 1500);
}

function bindEvents() {
  dom.btnTheme.addEventListener("click", toggleTheme);
  dom.btnRefresh.addEventListener("click", async () => {
    dom.btnRefresh.disabled = true;
    try {
      await Promise.all([loadMacro(true), loadMicro(), loadApprovals()]);
      showToast("已重新扫描并刷新最新状态", "success", 1800);
    } finally {
      dom.btnRefresh.disabled = false;
    }
  });

  // KPI quick filter clicks
  document.getElementById("kpi-strip").addEventListener("click", (e) => {
    const card = e.target.closest(".kpi-card.clickable");
    if (!card) return;
    const type = card.dataset.kpi;
    if (type === "approvals") {
      state.onlyApprovals = !state.onlyApprovals;
      dom.chipApprovals.classList.toggle("active", state.onlyApprovals);
      renderProjectsView();
    } else if (type === "gates" || type === "drift") {
      state.onlyRisk = !state.onlyRisk;
      dom.chipRisk.classList.toggle("active", state.onlyRisk);
      renderProjectsView();
    }
  });

  // View mode switch (Flowchart default <-> Matrix)
  if (dom.viewTabs) {
    dom.viewTabs.addEventListener("click", (e) => {
      const btn = e.target.closest(".seg-btn");
      if (!btn) return;
      state.viewMode = btn.dataset.view === "matrix" ? "matrix" : "flowchart";
      renderProjectsView();
    });
  }

  // Collapsible Queue
  const toggleQueue = () => {
    state.queueCollapsed = !state.queueCollapsed;
    renderQueue();
  };
  dom.queueToggle.addEventListener("click", toggleQueue);
  dom.queueToggle.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      toggleQueue();
    }
  });

  dom.queueBody.addEventListener("click", async (e) => {
    const copyBtn = e.target.closest("[data-copy-cmd]");
    if (copyBtn) {
      const ok = await copyText(copyBtn.dataset.copyCmd);
      showToast(ok ? "已复制终端审批命令到剪贴板" : "复制失败，请手动选中复制", ok ? "success" : "error");
      return;
    }
    const openBtn = e.target.closest("[data-open-project]");
    if (openBtn) {
      openDrawer(openBtn.dataset.openProject);
    }
  });

  // Search & filters
  dom.searchInput.addEventListener("input", () => {
    state.search = dom.searchInput.value;
    dom.searchClear.classList.toggle("visible", state.search.length > 0);
    renderProjectsView();
    renderKpis();
  });

  dom.searchClear.addEventListener("click", () => {
    state.search = "";
    dom.searchInput.value = "";
    dom.searchClear.classList.remove("visible");
    dom.searchInput.focus();
    renderProjectsView();
    renderKpis();
  });

  dom.categoryTabs.addEventListener("click", (e) => {
    const btn = e.target.closest(".seg-btn");
    if (!btn) return;
    state.categoryTab = btn.dataset.cat;
    for (const el of dom.categoryTabs.querySelectorAll(".seg-btn")) {
      el.classList.toggle("active", el === btn);
    }
    renderProjectsView();
    renderKpis();
  });

  dom.toggleSystem.addEventListener("change", () => {
    state.showSystem = dom.toggleSystem.checked;
    renderProjectsView();
    renderKpis();
  });

  dom.chipApprovals.addEventListener("click", () => {
    state.onlyApprovals = !state.onlyApprovals;
    dom.chipApprovals.classList.toggle("active", state.onlyApprovals);
    renderProjectsView();
    renderKpis();
  });

  dom.chipRisk.addEventListener("click", () => {
    state.onlyRisk = !state.onlyRisk;
    dom.chipRisk.classList.toggle("active", state.onlyRisk);
    renderProjectsView();
    renderKpis();
  });

  dom.chipPinned.addEventListener("click", () => {
    state.onlyPinned = !state.onlyPinned;
    dom.chipPinned.classList.toggle("active", state.onlyPinned);
    renderProjectsView();
    renderKpis();
  });

  dom.sortSelect.addEventListener("change", () => {
    state.sortBy = dom.sortSelect.value;
    renderProjectsView();
  });

  // Flowchart card / stage node click & pin button
  if (dom.flowchartList) {
    dom.flowchartList.addEventListener("click", (e) => {
      const pinBtn = e.target.closest("[data-pin-id]");
      if (pinBtn) {
        e.stopPropagation();
        togglePin(pinBtn.dataset.pinId);
        return;
      }
      const openBtn = e.target.closest("[data-open-detail]");
      if (openBtn) {
        e.stopPropagation();
        openDrawer(openBtn.dataset.openDetail);
        return;
      }
      const card = e.target.closest(".flow-card[data-project-id]");
      if (card) {
        openDrawer(card.dataset.projectId);
      }
    });
  }

  // Matrix row click & pin button
  dom.matrixTbody.addEventListener("click", (e) => {
    const pinBtn = e.target.closest("[data-pin-id]");
    if (pinBtn) {
      e.stopPropagation();
      togglePin(pinBtn.dataset.pinId);
      return;
    }
    const row = e.target.closest("tr[data-project-id]");
    if (row) {
      openDrawer(row.dataset.projectId);
    }
  });

  // Drawer interactions
  dom.drawerBackdrop.addEventListener("click", closeDrawer);
  dom.drawerCloseBtn.addEventListener("click", closeDrawer);
  dom.drawerPinBtn.addEventListener("click", () => {
    if (state.selectedProjectId) togglePin(state.selectedProjectId);
  });

  dom.drawerNav.addEventListener("click", (e) => {
    const btn = e.target.closest(".drawer-nav-btn");
    if (!btn) return;
    for (const el of dom.drawerNav.querySelectorAll(".drawer-nav-btn")) {
      el.classList.toggle("active", el === btn);
    }
    const target = document.getElementById(btn.dataset.target);
    if (target) {
      target.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  });

  dom.drawerBody.addEventListener("click", async (e) => {
    const copyPathBtn = e.target.closest("[data-copy-path]");
    if (copyPathBtn) {
      const ok = await copyText(copyPathBtn.dataset.copyPath);
      showToast(ok ? "已复制路径到剪贴板" : "复制失败", ok ? "success" : "error", 1600);
      return;
    }
    const copyCmdBtn = e.target.closest("[data-copy-cmd]");
    if (copyCmdBtn) {
      const ok = await copyText(copyCmdBtn.dataset.copyCmd);
      showToast(ok ? "已复制终端审批命令到剪贴板" : "复制失败", ok ? "success" : "error");
      return;
    }
    const primaryBtn = e.target.closest("[data-set-primary]");
    if (primaryBtn && state.selectedProjectId) {
      setPrimaryContract(state.selectedProjectId, primaryBtn.dataset.setPrimary);
    }
  });

  // Keyboard shortcuts
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && state.selectedProjectId) {
      closeDrawer();
    } else if (e.key === "/" && document.activeElement !== dom.searchInput) {
      const tag = (document.activeElement?.tagName || "").toLowerCase();
      if (tag !== "input" && tag !== "textarea" && tag !== "select") {
        e.preventDefault();
        dom.searchInput.focus();
      }
    }
  });
}

/* ==========================================================================
   Boot
   ========================================================================== */

async function init() {
  bindEvents();

  const [prefs] = await Promise.all([
    fetchPrefs(),
    loadMacro(false),
    loadMicro(),
    loadApprovals(),
  ]);
  state.prefs = prefs;
  renderAll();

  // Auto-open drawer from ?project=<id>
  const params = new URLSearchParams(window.location.search);
  const initialProject = params.get("project");
  if (initialProject) {
    openDrawer(initialProject);
  }

  // Subscribe to SSE live updates
  createEventStream({
    onStatusChange: (status) => {
      state.sseStatus = status;
      renderSseStatus();
    },
    onMicro: (payload) => {
      if (payload && Array.isArray(payload.sessions)) {
        state.micro = payload;
        renderKpis();
        renderProjectsView();
        if (state.selectedProjectId) renderDrawerHeaderMeta();
      } else {
        scheduleMicroRefresh();
      }
    },
    onMacro: (payload) => {
      if (payload && Array.isArray(payload.projects)) {
        state.macro = payload;
        renderAll();
        loadApprovals();
      } else {
        scheduleMacroRefresh();
      }
    },
  });

  // Safety fallback poll every 30s if SSE is disconnected
  setInterval(() => {
    if (state.sseStatus !== "connected") {
      loadMicro();
      loadMacro(false);
    }
  }, 30000);
}

init();
