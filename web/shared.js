/**
 * shared.js — Common helpers, schema normalizers, API client, SSE manager,
 * clipboard & native macOS WKWebView bridge for 小研科研进度监控.
 * Pure ES module, zero build step, 100% offline.
 */

export const STAGES_ORDER = ["S0", "S1", "S2", "S3", "S4", "S5", "S6", "S7"];

export const STAGE_META = {
  S0: { id: "S0", short: "构思", name: "选题与构思" },
  S1: { id: "S1", short: "数据", name: "数据与变量就绪" },
  S2: { id: "S2", short: "合同", name: "研究合同冻结" },
  S3: { id: "S3", short: "证据", name: "正式证据链" },
  S4: { id: "S4", short: "图表", name: "图表与质检" },
  S5: { id: "S5", short: "手稿", name: "手稿撰写与去AI" },
  S6: { id: "S6", short: "审计", name: "七层审计与复核" },
  S7: { id: "S7", short: "投稿", name: "投稿包与发布门" },
};

export const STAGE_STATE_META = {
  done: { icon: "✓", label: "已完成", tone: "green", css: "done" },
  partial: { icon: "◐", label: "进行中", tone: "blue", css: "partial" },
  waiting_approval: { icon: "⏸", label: "待审批", tone: "amber", css: "waiting_approval" },
  blocked: { icon: "✕", label: "阻断", tone: "red", css: "blocked" },
  unverified: { icon: "?", label: "未验真", tone: "amber", css: "unverified" },
  not_started: { icon: "○", label: "未开始", tone: "neutral", css: "not_started" },
};

export const PROOF_META = {
  verified: { label: "HMAC 已验真", short: "已验真", tone: "green" },
  observed: { label: "文件已观测", short: "已观测", tone: "blue" },
  inferred: { label: "规则推断", short: "推断", tone: "neutral" },
  unverified: { label: "未验真（缺本地密钥）", short: "未验真", tone: "amber" },
  invalid: { label: "HMAC/链校验失败", short: "校验失败", tone: "red" },
  unknown: { label: "状态未知", short: "未知", tone: "neutral" },
};

export const CATEGORY_LABELS = {
  v3_formal: "V3 标准",
  v2_formal: "V2 标准",
  v3_partial: "V3 部分",
  custom: "非标准",
  system: "系统目录",
};

export const ROLE_LABELS = {
  main: "主控",
  coordinator: "主控",
  executor: "执行",
  "r-executor": "R执行",
  validator: "验证",
  "r-validator": "R验证",
  publisher: "发布",
  "evidence-publisher": "发布",
  explorer: "探索",
  verifier: "核验",
  subagent: "子代理",
};

export function escapeHtml(value) {
  if (value === null || value === undefined) return "";
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

export function truncate(str, maxLen = 40) {
  if (!str) return "";
  const s = String(str);
  return s.length > maxLen ? `${s.slice(0, Math.max(1, maxLen - 1))}…` : s;
}

export function formatTokens(n) {
  const num = Number(n);
  if (!Number.isFinite(num) || num <= 0) return "0";
  if (num >= 1_000_000) return `${(num / 1_000_000).toFixed(2)}M`;
  if (num >= 1_000) return `${(num / 1_000).toFixed(num >= 100_000 ? 0 : 1)}k`;
  return String(Math.round(num));
}

export function formatDuration(ms) {
  const n = Number(ms);
  if (!Number.isFinite(n) || n < 0) return "";
  if (n < 1000) return "<1s";
  const sec = Math.floor(n / 1000);
  if (sec < 60) return `${sec}s`;
  const min = Math.floor(sec / 60);
  const remSec = sec % 60;
  if (min < 60) return remSec ? `${min}m ${remSec}s` : `${min}m`;
  const hr = Math.floor(min / 60);
  const remMin = min % 60;
  return `${hr}h ${remMin}m`;
}

export function formatRelativeTime(value) {
  if (!value) return "—";
  const ts = typeof value === "number"
    ? (value < 1e12 ? value * 1000 : value)
    : Date.parse(value);
  if (!Number.isFinite(ts)) return String(value);
  const diffSec = Math.max(0, Math.floor((Date.now() - ts) / 1000));
  if (diffSec < 10) return "刚刚";
  if (diffSec < 60) return `${diffSec}秒前`;
  const diffMin = Math.floor(diffSec / 60);
  if (diffMin < 60) return `${diffMin}分钟前`;
  const diffHr = Math.floor(diffMin / 60);
  if (diffHr < 24) return `${diffHr}小时前`;
  const diffDay = Math.floor(diffHr / 24);
  if (diffDay < 30) return `${diffDay}天前`;
  const d = new Date(ts);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

export function formatDateTime(value) {
  if (!value) return "—";
  const ts = typeof value === "number"
    ? (value < 1e12 ? value * 1000 : value)
    : Date.parse(value);
  if (!Number.isFinite(ts)) return String(value);
  const d = new Date(ts);
  const pad = (x) => String(x).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

/**
 * Normalize project.stages whether backend returns dict {S0: {...}} or list [{id:"S0",...}]
 */
export function normalizeStages(stages) {
  const out = {};
  if (Array.isArray(stages)) {
    for (const st of stages) {
      if (st && st.id) out[st.id] = st;
    }
  } else if (stages && typeof stages === "object") {
    for (const [k, v] of Object.entries(stages)) {
      if (v && typeof v === "object") {
        out[v.id || k] = { id: k, ...v };
      }
    }
  }
  for (const id of STAGES_ORDER) {
    if (!out[id]) {
      out[id] = {
        id,
        name: STAGE_META[id].name,
        state: "not_started",
        proof: "unknown",
        summary: "",
        details: null,
        artifacts: [],
      };
    }
  }
  return out;
}

/**
 * Normalize gate value which may be string ("pass"/"fail"/"warn"/"blocked"/"ok") or object
 */
export function normalizeGateInfo(raw) {
  if (raw === null || raw === undefined || raw === "") {
    return { status: "unknown", label: "未检", tone: "neutral", detail: "" };
  }
  if (typeof raw === "string") {
    const s = raw.toLowerCase();
    if (s === "submission_ready") {
      return { status: "pass", label: "可投稿", tone: "green", detail: raw };
    }
    if (s === "exempt") {
      return { status: "pass", label: "豁免", tone: "green", detail: raw };
    }
    if (s === "ready_with_exceptions") {
      return { status: "warn", label: "有条件放行", tone: "amber", detail: raw };
    }
    if (s === "changes_required") {
      return { status: "fail", label: "需修改", tone: "red", detail: raw };
    }
    if (s === "stale") {
      return { status: "fail", label: "旁证过期", tone: "red", detail: raw };
    }
    if (s === "not_ready") {
      return { status: "unknown", label: "未就绪", tone: "neutral", detail: raw };
    }
    if (s === "missing") {
      return { status: "unknown", label: "缺失", tone: "neutral", detail: raw };
    }
    if (["pass", "passed", "ok", "clean", "verified", "ready"].includes(s)) {
      return { status: "pass", label: "通过", tone: "green", detail: raw };
    }
    if (["fail", "failed", "blocked", "error", "invalid"].includes(s)) {
      return { status: "fail", label: "阻断", tone: "red", detail: raw };
    }
    if (["warn", "warning", "pending", "partial", "waiting"].includes(s)) {
      return { status: "warn", label: "需关注", tone: "amber", detail: raw };
    }
    if (["none", "n/a", "not_applicable", "unknown", "idle"].includes(s)) {
      return { status: "unknown", label: "未检", tone: "neutral", detail: raw };
    }
    return { status: s, label: raw, tone: "neutral", detail: raw };
  }
  if (typeof raw === "object") {
    const statusStr = raw.status || raw.gate_status || raw.state || "";
    const failCount = Number(raw.fail ?? raw.fail_count ?? raw.failed ?? 0);
    const passCount = Number(raw.pass ?? raw.pass_count ?? raw.passed ?? 0);
    const blocked = Boolean(raw.blocked || raw.hard_blocked || failCount > 0);
    if (blocked) {
      return {
        status: "fail",
        label: failCount > 0 ? `失败 ${failCount}` : "阻断",
        tone: "red",
        detail: raw.message || raw.summary || statusStr || "",
      };
    }
    if (statusStr) {
      const base = normalizeGateInfo(statusStr);
      if (passCount > 0 && base.status === "pass") {
        base.label = `通过 ${passCount}`;
      }
      base.detail = raw.message || raw.summary || base.detail;
      return base;
    }
    if (passCount > 0) {
      return {
        status: "pass",
        label: `通过 ${passCount}`,
        tone: "green",
        detail: raw.summary || "",
      };
    }
    return { status: "unknown", label: "未检", tone: "neutral", detail: raw.summary || "" };
  }
  return { status: "unknown", label: "未检", tone: "neutral", detail: "" };
}

/**
 * Normalize step status (supports update_plan & micro-server enums)
 */
export function normalizeStepStatus(raw) {
  const s = String(raw || "pending").toLowerCase();
  if (s === "in_progress" || s === "running" || s === "active") return "running";
  if (s === "completed" || s === "done" || s === "success") return "done";
  if (s === "waiting_approval" || s === "waiting") return "waiting_approval";
  if (s === "blocked") return "blocked";
  if (s === "failed" || s === "error") return "failed";
  if (s === "skipped" || s === "cancelled") return "skipped";
  return "pending";
}

export function stepStatusIconHtml(status) {
  const norm = normalizeStepStatus(status);
  if (norm === "running") {
    return `<span class="spinner spinner-sm" aria-label="进行中"></span>`;
  }
  if (norm === "done") return `<span class="hud-step-icon done" title="已完成">✓</span>`;
  if (norm === "waiting_approval") return `<span class="hud-step-icon waiting_approval" title="等待审批">⏸</span>`;
  if (norm === "blocked" || norm === "failed") return `<span class="hud-step-icon ${norm}" title="阻断/失败">✕</span>`;
  if (norm === "skipped") return `<span class="hud-step-icon skipped" title="已跳过">–</span>`;
  return `<span class="hud-step-icon pending" title="待执行">○</span>`;
}

export function roleLabel(role) {
  if (!role) return "";
  const key = String(role).toLowerCase();
  return ROLE_LABELS[key] || String(role);
}

/* ==========================================================================
   Native macOS WKWebView Bridge & Clipboard
   ========================================================================== */

export function isNativeHud() {
  return Boolean(
    typeof window !== "undefined" &&
      window.webkit &&
      window.webkit.messageHandlers &&
      window.webkit.messageHandlers.xyHud
  );
}

export function postNativeMessage(payload) {
  if (!isNativeHud()) return false;
  try {
    window.webkit.messageHandlers.xyHud.postMessage(payload);
    return true;
  } catch (err) {
    console.warn("[xyHud] postMessage failed:", err);
    return false;
  }
}

export async function copyText(text) {
  if (!text) return false;
  const str = String(text);
  if (isNativeHud()) {
    postNativeMessage({ type: "copyText", text: str });
    return true;
  }
  if (navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
    try {
      await navigator.clipboard.writeText(str);
      return true;
    } catch {
      // Fall through to textarea fallback
    }
  }
  try {
    const ta = document.createElement("textarea");
    ta.value = str;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.left = "-9999px";
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(ta);
    return ok;
  } catch {
    return false;
  }
}

/* ==========================================================================
   HTTP API Client (100% same-origin loopback)
   ========================================================================== */

export async function apiGet(path, { timeoutMs = 15000 } = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(path, {
      method: "GET",
      headers: { Accept: "application/json" },
      cache: "no-store",
      signal: controller.signal,
    });
    if (!res.ok) {
      const txt = await res.text().catch(() => "");
      throw new Error(`HTTP ${res.status}: ${txt.slice(0, 160)}`);
    }
    return await res.json();
  } finally {
    clearTimeout(timer);
  }
}

export async function apiPost(path, body, { timeoutMs = 10000 } = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(path, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "application/json",
      },
      body: JSON.stringify(body),
      signal: controller.signal,
    });
    if (!res.ok) {
      const txt = await res.text().catch(() => "");
      throw new Error(`HTTP ${res.status}: ${txt.slice(0, 160)}`);
    }
    return await res.json();
  } finally {
    clearTimeout(timer);
  }
}

export function defaultPrefs() {
  return {
    pinned_projects: [],
    hidden_projects: [],
    category_overrides: {},
    primary_contract_overrides: {},
    session_project_overrides: {},
  };
}

export async function fetchPrefs() {
  try {
    const data = await apiGet("/api/prefs");
    return { ...defaultPrefs(), ...(data || {}) };
  } catch {
    return defaultPrefs();
  }
}

export async function updatePrefs(currentPrefs, updater) {
  const base = {
    pinned_projects: [...(currentPrefs?.pinned_projects || [])],
    hidden_projects: [...(currentPrefs?.hidden_projects || [])],
    category_overrides: { ...(currentPrefs?.category_overrides || {}) },
    primary_contract_overrides: { ...(currentPrefs?.primary_contract_overrides || {}) },
    session_project_overrides: { ...(currentPrefs?.session_project_overrides || {}) },
  };
  const next = typeof updater === "function" ? updater(base) || base : { ...base, ...updater };
  const saved = await apiPost("/api/prefs", next);
  return { ...defaultPrefs(), ...(saved || next) };
}

/* ==========================================================================
   SSE Event Stream with Graceful Reconnect
   ========================================================================== */

export function createEventStream({ onMicro, onMacro, onPing, onStatusChange } = {}) {
  let es = null;
  let closed = false;
  let retryDelay = 1500;
  let retryTimer = null;

  const setStatus = (state) => {
    if (typeof onStatusChange === "function") onStatusChange(state);
  };

  const safeParse = (raw) => {
    if (!raw) return null;
    try {
      return JSON.parse(raw);
    } catch {
      return null;
    }
  };

  const connect = () => {
    if (closed) return;
    setStatus("connecting");
    try {
      es = new EventSource("/api/events");
    } catch {
      scheduleReconnect();
      return;
    }

    es.onopen = () => {
      retryDelay = 1500;
      setStatus("connected");
    };

    es.addEventListener("micro_update", (ev) => {
      setStatus("connected");
      if (typeof onMicro === "function") onMicro(safeParse(ev.data));
    });

    es.addEventListener("macro_update", (ev) => {
      setStatus("connected");
      if (typeof onMacro === "function") onMacro(safeParse(ev.data));
    });

    es.addEventListener("ping", (ev) => {
      setStatus("connected");
      if (typeof onPing === "function") onPing(safeParse(ev.data));
    });

    es.onerror = () => {
      if (closed) return;
      setStatus("reconnecting");
      if (es) {
        try {
          es.close();
        } catch {
          // ignore
        }
        es = null;
      }
      scheduleReconnect();
    };
  };

  const scheduleReconnect = () => {
    if (closed || retryTimer) return;
    retryTimer = setTimeout(() => {
      retryTimer = null;
      retryDelay = Math.min(retryDelay * 1.6, 12000);
      connect();
    }, retryDelay);
  };

  connect();

  return {
    close() {
      closed = true;
      if (retryTimer) {
        clearTimeout(retryTimer);
        retryTimer = null;
      }
      if (es) {
        try {
          es.close();
        } catch {
          // ignore
        }
        es = null;
      }
    },
  };
}

/* ==========================================================================
   Toast Feedback
   ========================================================================== */

let toastStackEl = null;

export function showToast(message, tone = "info", durationMs = 2400) {
  if (typeof document === "undefined") return;
  if (!toastStackEl) {
    toastStackEl = document.createElement("div");
    toastStackEl.className = "toast-stack";
    document.body.appendChild(toastStackEl);
  }
  const el = document.createElement("div");
  el.className = `toast ${tone}`;
  el.textContent = message;
  toastStackEl.appendChild(el);
  setTimeout(() => {
    el.style.opacity = "0";
    el.style.transition = "opacity 0.18s ease";
    setTimeout(() => el.remove(), 200);
  }, durationMs);
}
