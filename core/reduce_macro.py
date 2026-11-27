"""Macro Read-Only State Engine: reduces every discovered project to S0-S7 + exploratory track.

Public API:
- build_macro_snapshot(research_root=RESEARCH_ROOT, prefs=None) -> dict
- get_project_detail(project_id: str, research_root=RESEARCH_ROOT, prefs=None) -> dict | None
- get_approval_commands(research_root=RESEARCH_ROOT, prefs=None) -> list[dict]

STRICT READ-ONLY GUARANTEE:
Zero writes, zero mkdir, zero chmod, zero authority creation inside RESEARCH_ROOT or ~/.config/xiaoyan-r-runtime/.
"""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import re
from typing import Any

from core.approvals import (
    build_approval_commands,
    extract_handoff_decisions,
    inspect_runtime_snapshots,
    match_preflight_receipts,
    verify_contract_approval_ro,
)
from core.catalog import discover_projects
from core.config import RESEARCH_ROOT, load_prefs
from core.read_fs import (
    bounded_walk_files,
    mtime_iso,
    mtime_ts,
    norm_rel,
    read_json_ro,
    sha256_file_ro,
)
from core.registry import (
    ML_MODULES,
    inspect_audit_and_gates,
    inspect_figures_qc,
    inspect_manuscript_registry,
)
from core.verify_readonly import authority_status, verify_project_evidence

ARCHIVE_CONTRACT_RE = re.compile(r"v\d+\.\d+(?:\.\d+)?\.json\Z")
SENS_OR_BRANCH_RE = re.compile(r"(?:^|[_-])(?:sens|sensitivity|branch|supp|supplement|desc|descriptive)(?:$|[_-])", re.I)

STAGE_META = (
    ("S0", "ideation", "构思与选刊"),
    ("S1", "data_ready", "数据与预检"),
    ("S2", "contract_frozen", "合同与审批"),
    ("S3", "formal_evidence", "受控证据链"),
    ("S4", "figures_qc", "表格与图件"),
    ("S5", "manuscript_draft", "稿件与去AI"),
    ("S6", "audit_review", "审计与审稿"),
    ("S7", "submission", "发布与投稿"),
)


def _protocol_dirs(project_root: Path, uses_v3: bool, is_hybrid: bool) -> list[Path]:
    v3 = project_root / "04_合同"
    v2 = project_root / "00_protocol"
    ordered = [v3, v2] if uses_v3 else [v2, v3]
    out: list[Path] = []
    for d in ordered:
        if d.is_dir() and not d.is_symlink():
            out.append(d)
            if not is_hybrid:
                break
    return out


def _collect_contracts(
    project_entry: dict[str, Any],
    research_root: Path,
    snapshots_info: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Scan canonical `research_contract.json` and `contracts/*.json` (separating version archives)."""
    project_root = Path(project_entry["abs_path"])
    pdirs = _protocol_dirs(project_root, bool(project_entry.get("uses_v3")), bool(project_entry.get("is_hybrid")))

    active_contracts: list[dict[str, Any]] = []
    archived_contracts: list[dict[str, Any]] = []
    seen_abs: set[str] = set()
    snap_map = snapshots_info.get("snapshots") or {}

    for pdir in pdirs:
        candidates: list[tuple[Path, bool]] = []
        canon = pdir / "research_contract.json"
        if canon.is_file() and not canon.is_symlink():
            candidates.append((canon, True))
        cdir = pdir / "contracts"
        if cdir.is_dir() and not cdir.is_symlink():
            try:
                for e in sorted(os.scandir(cdir), key=lambda x: x.name):
                    if e.is_file(follow_symlinks=False) and e.name.endswith(".json") and not e.name.startswith("."):
                        candidates.append((Path(e.path), False))
            except OSError:
                pass

        for cpath, is_canonical_file in candidates:
            abs_str = str(cpath.resolve())
            if abs_str in seen_abs:
                continue
            seen_abs.add(abs_str)
            rel = norm_rel(cpath.relative_to(project_root).as_posix())
            sha = sha256_file_ro(cpath) or ""
            is_archive = (not is_canonical_file) and bool(ARCHIVE_CONTRACT_RE.fullmatch(cpath.name))
            doc = read_json_ro(cpath)
            if not isinstance(doc, dict):
                rec = {
                    "contract_id": "canonical" if is_canonical_file else cpath.stem,
                    "is_canonical": is_canonical_file,
                    "is_archive": is_archive,
                    "rel_path": rel,
                    "abs_path": abs_str,
                    "status": "unreadable",
                    "version": "-",
                    "sha256": sha,
                    "sha12": sha[:12],
                    "governance_conflict": False,
                    "conflict_reasons": [],
                    "unresolved_items": [],
                }
                (archived_contracts if is_archive else active_contracts).append(rec)
                continue

            cid = str(doc.get("contract_id") or ("canonical" if is_canonical_file else cpath.stem))
            app_info = verify_contract_approval_ro(doc, cpath, research_root)
            preflight = match_preflight_receipts(pdir, doc)
            rt_snap = doc.get("runtime_snapshot")
            snap_state = (snap_map.get(rt_snap) or {}).get("state") if isinstance(rt_snap, str) else None

            analyses = doc.get("analyses") if isinstance(doc.get("analyses"), list) else []
            modules = sorted({
                str(a.get("module"))
                for a in analyses
                if isinstance(a, dict) and a.get("module")
            })
            unresolved = doc.get("unresolved_items") if isinstance(doc.get("unresolved_items"), list) else []

            rec = {
                "contract_id": cid,
                "is_canonical": is_canonical_file and doc.get("canonical_contract") is not False,
                "is_archive": is_archive,
                "contract_role": doc.get("contract_role"),
                "rel_path": rel,
                "abs_path": abs_str,
                "project_id_in_contract": doc.get("project_id"),
                "status": str(doc.get("status") or "unknown"),
                "version": str(doc.get("version") or "-"),
                "analysis_engine": doc.get("analysis_engine"),
                "runtime_snapshot": rt_snap,
                "runtime_snapshot_state": snap_state,
                "sha256": sha,
                "sha12": sha[:12],
                "analyses_count": len(analyses),
                "modules": modules,
                "is_ml": bool(set(modules) & ML_MODULES),
                "unresolved_items": unresolved,
                "unresolved_count": len(unresolved),
                "approval_valid": app_info["approval_valid"],
                "approval_mode": app_info["approval_mode"],
                "approved_by": app_info["approved_by"],
                "approval_error": app_info["approval_error"],
                "scientific_approval_status": app_info["scientific_approval_status"],
                "governance_conflict": app_info["governance_conflict"],
                "conflict_reasons": app_info["conflict_reasons"],
                "preflight": preflight,
                "updated_at": mtime_iso(cpath),
            }
            (archived_contracts if is_archive else active_contracts).append(rec)

    return active_contracts, archived_contracts


def _select_primary_contract(
    project_entry: dict[str, Any],
    contracts: list[dict[str, Any]],
    prefs: dict[str, Any],
) -> tuple[dict[str, Any] | None, bool]:
    """Select primary contract following: user override -> canonical -> frozen non-sens -> highest non-draft."""
    if not contracts:
        return None, False

    overrides = prefs.get("primary_contract_overrides") or {}
    override_id = (
        overrides.get(project_entry["id"])
        or overrides.get(project_entry["rel_path"])
        or overrides.get(project_entry["dir_name"])
    )
    if override_id:
        for c in contracts:
            if c["contract_id"] == override_id or c["rel_path"] == override_id:
                return c, False

    canonical = next((c for c in contracts if c.get("is_canonical")), None)
    if canonical is not None:
        return canonical, False

    # No canonical research_contract.json -> named contracts exist
    frozen = [c for c in contracts if c.get("status") == "frozen"]
    non_sens_frozen = [
        c for c in frozen
        if c.get("contract_role") not in {"sensitivity", "branch"}
        and not SENS_OR_BRANCH_RE.search(c["contract_id"])
    ]
    if len(non_sens_frozen) == 1:
        return non_sens_frozen[0], True
    if non_sens_frozen:
        non_sens_frozen.sort(key=lambda x: (x.get("updated_at") or "", x["contract_id"]), reverse=True)
        return non_sens_frozen[0], True
    if frozen:
        frozen.sort(key=lambda x: (x.get("updated_at") or "", x["contract_id"]), reverse=True)
        return frozen[0], True

    non_draft = [c for c in contracts if c.get("status") not in {"draft", "unreadable"}]
    if non_draft:
        return non_draft[0], True
    return contracts[0], True


def _inspect_exploratory_track(project_root: Path) -> dict[str, Any]:
    """Scan `99_audit/exploration/*/exploration_card.json` and `03_证据/candidate_runs/`."""
    exp_dir = project_root / "99_audit/exploration"
    cards: list[dict[str, Any]] = []
    if exp_dir.is_dir() and not exp_dir.is_symlink():
        try:
            for e in sorted(os.scandir(exp_dir), key=lambda x: x.name):
                if e.is_dir(follow_symlinks=False):
                    cpath = Path(e.path) / "exploration_card.json"
                    if cpath.is_file():
                        doc = read_json_ro(cpath) or {}
                        cards.append({
                            "id": doc.get("exploration_id") or e.name,
                            "status": doc.get("status") or "candidate_not_released",
                            "affects_primary_contract": bool(doc.get("affects_primary_contract")),
                            "summary": doc.get("question") or doc.get("title") or e.name,
                            "rel_path": norm_rel(cpath.relative_to(project_root).as_posix()),
                            "updated_at": mtime_iso(cpath),
                        })
        except OSError:
            pass

    cand_runs = 0
    for rel in ("03_证据/candidate_runs", "03_outputs/candidate_runs"):
        cdir = project_root / rel
        if cdir.is_dir() and not cdir.is_symlink():
            try:
                cand_runs += sum(
                    1 for e in os.scandir(cdir) if not e.name.startswith(".")
                )
            except OSError:
                pass

    has_activity = bool(cards or cand_runs > 0)
    return {
        "card_count": len(cards),
        "candidate_runs": cand_runs,
        "has_activity": has_activity,
        "affects_primary": any(c["affects_primary_contract"] for c in cards),
        "badge": "candidate_not_released" if has_activity else None,
        "cards": cards[:20],
    }


def _stage_cell(
    sid: str,
    key: str,
    name: str,
    state: str,
    proof: str,
    summary: str,
    details: str = "",
    artifacts: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": sid,
        "key": key,
        "name": name,
        "state": state,
        "proof": proof,
        "summary": summary,
        "details": details,
        "artifacts": artifacts or [],
    }


def _compute_legacy_derived_stage(
    contracts: list[dict[str, Any]],
    primary: dict[str, Any] | None,
    evidence: dict[str, Any],
    reg_info: dict[str, Any],
    gates: dict[str, Any],
) -> str:
    """Compute realistic equivalent of legacy 8-stage enum (including named contracts & active manuscripts)."""
    if not contracts:
        return "draft"
    any_frozen = any(c.get("status") == "frozen" for c in contracts)
    stage = "locked" if any_frozen else "scoped"
    totals = evidence["totals"]
    if any_frozen and totals["runs"] > 0:
        stage = "explored"
    if any_frozen and totals["sealed_runs"] > 0:
        stage = "formal_done"
    valid_rels = totals["verified_releases"] + totals["unverified_valid_releases"]
    if any_frozen and valid_rels > 0:
        stage = "evidenced"
    if stage == "evidenced" and reg_info.get("primary_manuscript") and reg_info["primary_manuscript"].get("exists"):
        stage = "drafted"
    if stage == "drafted" and (gates.get("publication_gate") or {}).get("gate_status") == "submission_ready":
        stage = "submission_ready"
    return stage


def _reduce_one_project(
    entry: dict[str, Any],
    research_root: Path,
    prefs: dict[str, Any],
    snapshots_info: dict[str, Any],
    *,
    include_detail: bool = False,
) -> dict[str, Any]:
    project_root = Path(entry["abs_path"])
    contracts, archived_contracts = _collect_contracts(entry, research_root, snapshots_info)
    primary, primary_ambiguous = _select_primary_contract(entry, contracts, prefs)

    evidence = verify_project_evidence(entry)
    reg_info = inspect_manuscript_registry(project_root)
    fig_info = inspect_figures_qc(project_root)
    gate_info = inspect_audit_and_gates(project_root, reg_info)
    exp_info = _inspect_exploratory_track(project_root)
    handoff_decisions = extract_handoff_decisions(project_root)

    # --- S0: Ideation ---
    ideation_files: list[str] = []
    for rel_dir in ("01_构思", "00_protocol"):
        d = project_root / rel_dir
        if d.is_dir():
            for f in bounded_walk_files(d, max_depth=2, suffixes=(".md", ".docx", ".pdf", ".json")):
                if f.name == "research_contract.json" or f.name.startswith("preflight_receipt_"):
                    continue
                ideation_files.append(norm_rel(f.relative_to(project_root).as_posix()))
    has_workbook = (project_root / "PROJECT_WORKBOOK.md").is_file()
    if ideation_files or has_workbook or contracts:
        s0 = _stage_cell(
            "S0", "ideation", "构思与选刊",
            "done" if (ideation_files or contracts) else "partial",
            "observed",
            f"构思工件 {len(ideation_files)} 项" if ideation_files else ("已有工作簿/合同" if (has_workbook or contracts) else "无"),
            details="已发现构思/协议文档或项目工作簿",
            artifacts=ideation_files[:8],
        )
    else:
        s0 = _stage_cell("S0", "ideation", "构思与选刊", "not_started", "unknown", "无构思记录")

    # --- S1: Data Ready & Preflight ---
    data_files: list[str] = []
    for rel_dir in ("02_数据/analysis_ready", "01_data/analysis_ready"):
        d = project_root / rel_dir
        if d.is_dir():
            for f in bounded_walk_files(d, max_depth=2, suffixes=(".csv", ".rds", ".parquet", ".tsv", ".xlsx")):
                data_files.append(norm_rel(f.relative_to(project_root).as_posix()))
    passed_preflights = [c["preflight"] for c in contracts if (c.get("preflight") or {}).get("passed")]
    any_preflight = [c["preflight"] for c in contracts if (c.get("preflight") or {}).get("found")]
    if passed_preflights:
        s1 = _stage_cell(
            "S1", "data_ready", "数据与预检",
            "done", "verified",
            f"预检通过 ({len(data_files)} 分析集)",
            details=f"Full-Grid Preflight 已通过（{len(passed_preflights)} 份回执）",
            artifacts=data_files[:6],
        )
    elif data_files:
        s1 = _stage_cell(
            "S1", "data_ready", "数据与预检",
            "partial" if any_preflight else "done",
            "observed",
            f"分析数据 {len(data_files)} 份",
            details="已有 analysis_ready 数据" + ("，但预检未通过" if any_preflight else "（无预检回执）"),
            artifacts=data_files[:6],
        )
    elif any_preflight:
        s1 = _stage_cell("S1", "data_ready", "数据与预检", "blocked", "observed", "预检未通过")
    else:
        s1 = _stage_cell("S1", "data_ready", "数据与预检", "not_started", "unknown", "无分析数据")

    # --- S2: Contract Frozen & Approvals ---
    frozen_contracts = [c for c in contracts if c.get("status") == "frozen"]
    draft_contracts = [c for c in contracts if c.get("status") == "draft"]
    conflict_contracts = [c for c in contracts if c.get("governance_conflict")]
    unresolved_total = sum(int(c.get("unresolved_count") or 0) for c in contracts)
    pending_snap_contracts = [
        c for c in contracts
        if c.get("runtime_snapshot_state") in {"pending_human_approval", "invalid_binding"}
    ]

    if not contracts:
        s2 = _stage_cell("S2", "contract_frozen", "合同与审批", "not_started", "unknown", "无合同")
    elif conflict_contracts:
        s2 = _stage_cell(
            "S2", "contract_frozen", "合同与审批",
            "waiting_approval", "invalid",
            f"冻结/待签冲突 ({len(frozen_contracts)}/{len(contracts)})",
            details="; ".join(
                f"{c['contract_id']}: {', '.join(c['conflict_reasons'])}" for c in conflict_contracts[:3]
            ),
            artifacts=[c["rel_path"] for c in contracts[:6]],
        )
    elif frozen_contracts and not pending_snap_contracts and unresolved_total == 0:
        if draft_contracts:
            s2_state = "partial"
            s2_summary = f"{len(frozen_contracts)} 冻结 / {len(draft_contracts)} 待签"
        elif primary_ambiguous:
            s2_state = "partial"
            s2_summary = f"{len(frozen_contracts)} 命名冻结（主线待确认）"
        else:
            s2_state = "done"
            s2_summary = f"{len(frozen_contracts)} 份合同已冻结"
        all_app_valid = all(c.get("approval_valid") for c in frozen_contracts)
        s2 = _stage_cell(
            "S2", "contract_frozen", "合同与审批",
            s2_state,
            "verified" if all_app_valid else "observed",
            s2_summary,
            details=f"共 {len(contracts)} 份活跃合同，{len(archived_contracts)} 份历史归档",
            artifacts=[c["rel_path"] for c in contracts[:6]],
        )
    elif draft_contracts or pending_snap_contracts or unresolved_total > 0:
        reasons = []
        if draft_contracts:
            reasons.append(f"{len(draft_contracts)} 份草案待签署")
        if pending_snap_contracts:
            reasons.append(f"{len(pending_snap_contracts)} 份快照待审批")
        if unresolved_total:
            reasons.append(f"{unresolved_total} 项未决事项")
        s2 = _stage_cell(
            "S2", "contract_frozen", "合同与审批",
            "waiting_approval", "observed",
            " / ".join(reasons),
            artifacts=[c["rel_path"] for c in contracts[:6]],
        )
    else:
        s2 = _stage_cell(
            "S2", "contract_frozen", "合同与审批",
            "partial", "observed",
            f"{len(contracts)} 份合同",
            artifacts=[c["rel_path"] for c in contracts[:6]],
        )

    # --- S3: Formal Evidence Chain ---
    totals = evidence["totals"]
    auth_present = bool(evidence["authority_present"])
    if totals["invalid_releases"] > 0:
        s3 = _stage_cell(
            "S3", "formal_evidence", "受控证据链",
            "blocked", "invalid",
            f"验真失败 {totals['invalid_releases']}/{totals['releases']}",
            details="存在 MAC/SHA 或证据板链路校验失败的 release",
            artifacts=evidence["evidence_ids"][:8],
        )
    elif totals["verified_releases"] > 0:
        sub_note = f"（含子根 {evidence['sub_root_counts']['releases']}）" if evidence["sub_root_counts"]["releases"] else ""
        s3 = _stage_cell(
            "S3", "formal_evidence", "受控证据链",
            "done", "verified",
            f"{totals['verified_releases']} 发布已验真{sub_note}",
            details=f"runs={totals['runs']} (sealed={totals['sealed_runs']}), vals={totals['validations']}, releases={totals['releases']}",
            artifacts=evidence["evidence_ids"][:8],
        )
    elif totals["unverified_valid_releases"] > 0:
        s3 = _stage_cell(
            "S3", "formal_evidence", "受控证据链",
            "unverified", "unverified",
            f"{totals['unverified_valid_releases']} 发布待验真（缺密钥）",
            details="receipt-authority.json 不存在，无法完成 HMAC 验真",
            artifacts=evidence["evidence_ids"][:8],
        )
    elif totals["runs"] > 0 or totals["validations"] > 0:
        s3 = _stage_cell(
            "S3", "formal_evidence", "受控证据链",
            "partial", "verified" if auth_present else "unverified",
            f"运行 {totals['runs']} / 封存 {totals['sealed_runs']} / 验证 {totals['validations']}",
            details="已有运行或封存记录，但尚未发布正式 evidence release",
        )
    else:
        s3 = _stage_cell("S3", "formal_evidence", "受控证据链", "not_started", "unknown", "无正式运行")

    # --- S4: Tables & Figures QC ---
    has_tables = bool(reg_info["table_artifacts"] or fig_info["table_files_count"] > 0)
    is_ml_project = bool(
        any(c.get("is_ml") for c in contracts)
        or reg_info.get("ml_delivery_gate") is not None
    )
    ml_gate_doc = reg_info.get("ml_delivery_gate") if isinstance(reg_info.get("ml_delivery_gate"), dict) else None
    ml_gate_fail = bool(ml_gate_doc and str(ml_gate_doc.get("status") or "").upper() == "FAIL")

    if fig_info["fail_count"] > 0:
        s4 = _stage_cell(
            "S4", "figures_qc", "表格与图件",
            "blocked", "invalid",
            f"图件 FAIL {fig_info['fail_count']} / PASS {fig_info['pass_count']}",
            details="存在未通过 check_figure.py 的图件",
            artifacts=[x["path"] for x in fig_info["failed_items"][:6]],
        )
    elif fig_info["pass_count"] > 0 or has_tables:
        parts = []
        if fig_info["pass_count"]:
            parts.append(f"图 PASS {fig_info['pass_count']}")
        if fig_info["unchecked_pdf_count"]:
            parts.append(f"未检 {fig_info['unchecked_pdf_count']}")
        if reg_info["table_artifacts"]:
            parts.append(f"登记表 {len(reg_info['table_artifacts'])}")
        elif fig_info["table_files_count"]:
            parts.append(f"表文件 {fig_info['table_files_count']}")
        if ml_gate_fail:
            parts.append("ML交付门未过")
        s4_state = "partial" if (fig_info["unchecked_pdf_count"] > 0 or ml_gate_fail) else "done"
        s4_proof = "verified" if (fig_info["pass_count"] > 0 and fig_info["unchecked_pdf_count"] == 0) else "observed"
        s4_artifacts = [a["path"] for a in reg_info["table_artifacts"] if a.get("path")] + fig_info["table_files"][:4]
        s4 = _stage_cell(
            "S4", "figures_qc", "表格与图件",
            s4_state, s4_proof,
            " · ".join(parts),
            details=(
                f"figure_style={fig_info['target_profile'] or '未配置'}; "
                f"ML项目={'是' if is_ml_project else '否'}"
            ),
            artifacts=s4_artifacts[:8],
        )
    elif fig_info["pdf_count"] > 0:
        s4 = _stage_cell(
            "S4", "figures_qc", "表格与图件",
            "partial", "observed",
            f"{fig_info['pdf_count']} 张图未质检",
            artifacts=fig_info["unchecked_pdfs"][:6],
        )
    else:
        s4 = _stage_cell("S4", "figures_qc", "表格与图件", "not_started", "unknown", "无图件/表")

    # --- S5: Manuscript Draft & Prose Sidecar ---
    primary_ms = reg_info.get("primary_manuscript")
    prose_status = "none"
    if primary_ms and primary_ms.get("exists"):
        prose = primary_ms.get("prose") or {}
        prose_status = prose.get("status") or "missing"
        placeholders = primary_ms.get("statistical_placeholders")
        ms_ver = primary_ms.get("version") or primary_ms.get("key")
        if prose_status in {"pass", "exempt"}:
            if isinstance(placeholders, int) and placeholders > 0:
                s5_state = "partial"
                s5_sum = f"{ms_ver} · 去AI通过（待填 {placeholders} 处）"
            else:
                s5_state = "done"
                s5_sum = f"{ms_ver} · 去AI旁证通过"
            s5_proof = "verified"
        elif prose_status == "stale":
            s5_state = "blocked"
            s5_sum = f"{ms_ver} · 旁证 SHA 过期"
            s5_proof = "invalid"
        else:
            s5_state = "partial"
            s5_sum = f"{ms_ver} · 缺去AI旁证"
            s5_proof = "observed"
        s5 = _stage_cell(
            "S5", "manuscript_draft", "稿件与去AI",
            s5_state, s5_proof, s5_sum,
            details=f"当前工作稿: {primary_ms.get('path')} (status={primary_ms.get('status')})",
            artifacts=[primary_ms["path"]] if primary_ms.get("path") else [],
        )
    elif reg_info.get("unregistered_drafts"):
        draft0 = reg_info["unregistered_drafts"][0]
        prose_status = (draft0.get("prose") or {}).get("status") or "missing"
        s5 = _stage_cell(
            "S5", "manuscript_draft", "稿件与去AI",
            "partial", "observed",
            f"未登记草稿 {len(reg_info['unregistered_drafts'])} 份",
            details=f"最新: {draft0['path']}（未在 manuscript_registry.json 登记）",
            artifacts=[d["path"] for d in reg_info["unregistered_drafts"][:4]],
        )
    else:
        s5 = _stage_cell(
            "S5", "manuscript_draft", "稿件与去AI",
            "not_started", "unknown",
            "当前正文待确认" if reg_info.get("registry_path") else "无手稿",
        )

    # --- S6: Seven-layer Audit, Peer Review & Reference Verification ---
    a_st = gate_info["audit"]["status"]
    p_st = gate_info["peer_review"]["status"]
    r_st = gate_info["ref_verify"]["status"]
    tri_statuses = (a_st, p_st, r_st)
    passed_tri = sum(1 for x in tri_statuses if x == "pass")
    failed_tri = sum(1 for x in tri_statuses if x == "fail")
    present_tri = sum(1 for x in tri_statuses if x != "missing")

    audit_status_short = "none"
    if passed_tri == 3 and not ml_gate_fail:
        s6 = _stage_cell(
            "S6", "audit_review", "审计与审稿",
            "done", "verified",
            "三联质检全部 PASS",
            details="七层审计 / 同行评审 / 文献核验均通过且未过期",
        )
        audit_status_short = "pass"
    elif failed_tri > 0:
        s6 = _stage_cell(
            "S6", "audit_review", "审计与审稿",
            "blocked", "invalid",
            f"质检阻断 ({passed_tri}/3 通过, {failed_tri} 失败)",
            details=f"审计={a_st}, 审稿={p_st}, 引文={r_st}",
        )
        audit_status_short = "blocked"
    elif present_tri > 0:
        s6 = _stage_cell(
            "S6", "audit_review", "审计与审稿",
            "partial", "observed",
            f"已开展 {present_tri}/3（通过 {passed_tri}/3）",
            details=f"审计={a_st}, 审稿={p_st}, 引文={r_st}",
        )
        audit_status_short = "partial"
    else:
        s6 = _stage_cell("S6", "audit_review", "审计与审稿", "not_started", "unknown", "未开展审计")

    # --- S7: Publication Gate & Submission Package ---
    pg = gate_info["publication_gate"]
    pg_status = pg.get("gate_status")
    sub_pkgs = gate_info["submission_packages"]
    if pg_status == "submission_ready" and sub_pkgs:
        s7 = _stage_cell(
            "S7", "submission", "发布与投稿",
            "done", "verified",
            f"可投稿 · {len(sub_pkgs)} 个投稿包",
            artifacts=[p["rel_path"] for p in sub_pkgs[:6]],
        )
    elif pg_status == "submission_ready":
        s7 = _stage_cell("S7", "submission", "发布与投稿", "partial", "verified", "发布门已就绪（未建投稿包）")
    elif pg_status == "ready_with_exceptions":
        s7 = _stage_cell(
            "S7", "submission", "发布与投稿",
            "waiting_approval", "observed",
            f"有条件放行（{len(pg.get('exceptions') or [])} 例外）",
            artifacts=[p["rel_path"] for p in sub_pkgs[:6]],
        )
    elif sub_pkgs:
        s7 = _stage_cell(
            "S7", "submission", "发布与投稿",
            "partial", "observed",
            f"已有投稿包 {len(sub_pkgs)} 个（发布门={pg_status or '缺失'}）",
            artifacts=[p["rel_path"] for p in sub_pkgs[:6]],
        )
    elif pg.get("exists"):
        s7 = _stage_cell("S7", "submission", "发布与投稿", "not_started", "observed", f"发布门: {pg_status or 'not_ready'}")
    else:
        s7 = _stage_cell("S7", "submission", "发布与投稿", "not_started", "unknown", "未建投稿包")

    stages = {
        "S0": s0, "S1": s1, "S2": s2, "S3": s3,
        "S4": s4, "S5": s5, "S6": s6, "S7": s7,
    }

    # --- Stage-skip alert ---
    valid_releases = totals["verified_releases"] + totals["unverified_valid_releases"]
    later_active = [
        sid for sid in ("S5", "S6", "S7")
        if stages[sid]["state"] in {"done", "partial", "blocked", "waiting_approval"}
    ]
    stage_skip_alert = False
    stage_skip_reason: str | None = None
    if later_active and valid_releases == 0 and entry["category"] in {"v3_formal", "v2_formal"}:
        stage_skip_alert = True
        stage_skip_reason = f"已进入 {'/'.join(later_active)} 阶段，但 S3 尚无有效已发布正式证据（released=0）"

    # --- Legacy workflow_state.json drift ---
    ws_path = project_root / "workflow_state.json"
    ws_doc = read_json_ro(ws_path) if ws_path.is_file() else None
    recorded_stage = ws_doc.get("stage") if isinstance(ws_doc, dict) else None
    derived_legacy = _compute_legacy_derived_stage(contracts, primary, evidence, reg_info, gate_info)
    workflow_state_drift = {
        "exists": ws_path.is_file(),
        "drifted": bool(recorded_stage is not None and recorded_stage != derived_legacy),
        "recorded": recorded_stage,
        "derived": derived_legacy,
    }

    # --- Pending approvals & C-level decisions for this project ---
    pending_approvals: list[dict[str, Any]] = []
    for c in contracts:
        if c.get("status") == "draft":
            pending_approvals.append({
                "kind": "sign_contract",
                "severity": "action",
                "title": f"待签署合同: {c['contract_id']} (v{c['version']})",
                "contract_id": c["contract_id"],
                "preflight_passed": bool((c.get("preflight") or {}).get("passed")),
                "source_path": c["rel_path"],
            })
        if c.get("governance_conflict"):
            pending_approvals.append({
                "kind": "governance_conflict",
                "severity": "conflict",
                "title": f"合同治理冲突: {c['contract_id']}（{'; '.join(c['conflict_reasons'])}）",
                "contract_id": c["contract_id"],
                "source_path": c["rel_path"],
            })
        for u in c.get("unresolved_items") or []:
            pending_approvals.append({
                "kind": "unresolved_item",
                "severity": "decision",
                "title": str(u)[:160],
                "contract_id": c["contract_id"],
                "source_path": c["rel_path"],
            })
        if c.get("runtime_snapshot_state") == "pending_human_approval":
            pending_approvals.append({
                "kind": "snapshot_approval",
                "severity": "action",
                "title": f"待批准运行时快照: {c['runtime_snapshot']}",
                "contract_id": c["contract_id"],
                "source_path": c["rel_path"],
            })
    for hd in handoff_decisions:
        pending_approvals.append({
            "kind": f"handoff_{hd['kind']}",
            "severity": "decision" if hd["kind"] == "decision" else "blocker",
            "title": hd["text"],
            "source_path": hd["source_path"],
        })
    for exc in pg.get("exceptions") or []:
        pending_approvals.append({
            "kind": "publication_exception",
            "severity": "decision",
            "title": str(exc)[:160],
            "source_path": pg.get("path"),
        })

    # Compute last_activity_at from key timestamps
    timestamps = [
        t
        for t in (
            [c.get("updated_at") for c in contracts]
            + [r.get("updated_at") for r in evidence["runs"]]
            + [r.get("updated_at") for r in evidence["releases"]]
            + ([primary_ms.get("updated_at")] if primary_ms else [])
            + [mtime_iso(project_root)]
        )
        if isinstance(t, str) and t
    ]
    last_activity_at = max(timestamps) if timestamps else None

    primary_summary = None
    if primary is not None:
        primary_summary = {
            "contract_id": primary["contract_id"],
            "status": primary["status"],
            "version": primary["version"],
            "sha12": primary["sha12"],
            "ambiguous": primary_ambiguous,
            "governance_conflict": primary["governance_conflict"],
            "runtime_snapshot": primary.get("runtime_snapshot"),
            "rel_path": primary["rel_path"],
        }

    result: dict[str, Any] = {
        "id": entry["id"],
        "name": entry["name"],
        "dir_name": entry["dir_name"],
        "rel_path": entry["rel_path"],
        "category": entry["category"],
        "auto_category": entry["auto_category"],
        "layout": entry["layout"],
        "is_hybrid": entry["is_hybrid"],
        "is_nested": entry["is_nested"],
        "is_stub": entry["is_stub"],
        "stub_target": entry["stub_target"],
        "pinned": entry["pinned"],
        "hidden": entry["hidden"],
        "runtime_ids": entry["runtime_ids"],
        "evidence_sub_roots": entry["evidence_sub_roots"],
        "stale_mappings": entry["stale_mappings"],
        "primary_contract": primary_summary,
        "contracts": [
            {
                "contract_id": c["contract_id"],
                "is_canonical": c["is_canonical"],
                "rel_path": c["rel_path"],
                "abs_path": c["abs_path"],
                "status": c["status"],
                "version": c["version"],
                "sha12": c["sha12"],
                "runtime_snapshot": c.get("runtime_snapshot"),
                "runtime_snapshot_state": c.get("runtime_snapshot_state"),
                "analyses_count": c.get("analyses_count", 0),
                "unresolved_count": c.get("unresolved_count", 0),
                "approval_valid": c.get("approval_valid", False),
                "approval_mode": c.get("approval_mode"),
                "governance_conflict": c.get("governance_conflict", False),
                "conflict_reasons": c.get("conflict_reasons", []),
                "preflight": c.get("preflight"),
                "updated_at": c.get("updated_at"),
            }
            for c in contracts
        ],
        "archived_contracts_count": len(archived_contracts),
        "stages": stages,
        "exploratory_track": {
            "card_count": exp_info["card_count"],
            "candidate_runs": exp_info["candidate_runs"],
            "has_activity": exp_info["has_activity"],
            "affects_primary": exp_info["affects_primary"],
            "badge": exp_info["badge"],
        },
        "stage_skip_alert": stage_skip_alert,
        "stage_skip_reason": stage_skip_reason,
        "workflow_state_drift": workflow_state_drift,
        "pending_approvals": pending_approvals,
        "gates": {
            "figure_pass": fig_info["pass_count"],
            "figure_fail": fig_info["fail_count"],
            "figure_unchecked": fig_info["unchecked_pdf_count"],
            "prose_status": prose_status,
            "audit_status": audit_status_short,
            "publication_gate": pg_status or ("missing" if not pg["exists"] else "not_ready"),
            "ml_delivery_gate": (ml_gate_doc.get("status") if ml_gate_doc else ("missing" if is_ml_project else "not_applicable")),
        },
        "evidence_summary": evidence["totals"],
        "last_activity_at": last_activity_at,
    }

    if include_detail:
        result.update({
            "abs_path": entry["abs_path"],
            "contracts": contracts,
            "archived_contracts": archived_contracts,
            "runs": evidence["runs"],
            "validations": evidence["validations"],
            "releases": evidence["releases"],
            "evidence_ids": evidence["evidence_ids"],
            "manuscript": {
                "registry_path": reg_info["registry_path"],
                "primary": reg_info["primary_manuscript"],
                "active_entries": reg_info["active_entries"],
                "historical_entries": reg_info["historical_entries"],
                "unregistered_drafts": reg_info["unregistered_drafts"],
                "table_artifacts": reg_info["table_artifacts"],
                "figure_artifacts": reg_info["figure_artifacts"],
                "output_manifest_path": reg_info["output_manifest_path"],
                "ml_delivery_gate": reg_info["ml_delivery_gate"],
            },
            "figures": fig_info,
            "audit_details": gate_info,
            "exploratory_cards": exp_info["cards"],
            "handoff_decisions": handoff_decisions,
        })

    return result


def build_macro_snapshot(
    research_root: Path = RESEARCH_ROOT,
    prefs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build full macro snapshot across all discovered projects (strictly read-only)."""
    eff_prefs = load_prefs() if prefs is None else prefs
    auth = authority_status()
    snapshots_info = inspect_runtime_snapshots(research_root)
    catalog = discover_projects(research_root, eff_prefs)

    projects = [
        _reduce_one_project(entry, research_root, eff_prefs, snapshots_info, include_detail=False)
        for entry in catalog
    ]

    # Sort: pinned -> pending approvals -> category priority -> last activity desc
    cat_rank = {"v3_formal": 0, "v2_formal": 1, "v3_partial": 2, "custom": 3, "system": 4}
    projects.sort(
        key=lambda p: (
            0 if p["pinned"] else 1,
            cat_rank.get(p["category"], 9),
            0 if p["pending_approvals"] else 1,
            p["name"],
        )
    )

    non_system = [p for p in projects if p["category"] != "system" and not p["hidden"]]
    pending_total = sum(len(p["pending_approvals"]) for p in non_system) + len(snapshots_info.get("pending_snapshots") or [])
    gate_failures = sum(
        1
        for p in non_system
        if p["gates"]["figure_fail"] > 0
        or p["gates"]["prose_status"] == "stale"
        or p["gates"]["audit_status"] == "blocked"
        or p["stages"]["S3"]["state"] == "blocked"
    )
    drift_count = sum(
        1 for p in non_system if p["workflow_state_drift"]["drifted"] or p["stage_skip_alert"]
    )

    snapshot: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "research_root": str(research_root.resolve()),
        "summary": {
            "total_projects": len(projects),
            "visible_projects": len(non_system),
            "pending_approvals": pending_total,
            "gate_failures": gate_failures,
            "drift_count": drift_count,
            "authority_present": auth["present"],
            "authority_error": auth["error"],
            "pending_snapshots_count": len(snapshots_info.get("pending_snapshots") or []),
        },
        "runtime_snapshots": {
            "policy_exists": snapshots_info["policy_exists"],
            "production_enabled": snapshots_info["production_enabled"],
            "zw30_pending": snapshots_info["zw30_pending"],
            "baseline_pending": snapshots_info["baseline_pending"],
            "pending_snapshots": snapshots_info["pending_snapshots"],
            "total_registered": len(snapshots_info["snapshots"]),
        },
        "projects": projects,
    }
    return snapshot


def get_project_detail(
    project_id: str,
    research_root: Path = RESEARCH_ROOT,
    prefs: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Return full detail view for a single project (matching by id, alias, rel_path, or dir_name)."""
    eff_prefs = load_prefs() if prefs is None else prefs
    snapshots_info = inspect_runtime_snapshots(research_root)
    catalog = discover_projects(research_root, eff_prefs)

    norm_query = norm_rel(project_id)
    match: dict[str, Any] | None = None
    for entry in catalog:
        if entry["id"] == project_id or norm_rel(entry["id"]) == norm_query:
            match = entry
            break
    if match is None:
        for entry in catalog:
            candidates = [entry["rel_path"], entry["dir_name"], entry["name"], *entry["aliases"], *entry["runtime_ids"]]
            if any(c == project_id or norm_rel(c) == norm_query for c in candidates if c):
                match = entry
                break
    if match is None:
        return None

    detail = _reduce_one_project(match, research_root, eff_prefs, snapshots_info, include_detail=True)
    # Attach project-scoped approval commands
    mini_snapshot = {"projects": [detail], "runtime_snapshots": snapshots_info}
    all_cmds = build_approval_commands(mini_snapshot, research_root=research_root)
    detail["approval_commands"] = [
        cmd for cmd in all_cmds if cmd.get("project_id") in {detail["id"], None}
    ]
    return detail


def get_approval_commands(
    research_root: Path = RESEARCH_ROOT,
    prefs: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Return strictly read-only allowlisted approval command descriptors across all projects."""
    snapshot = build_macro_snapshot(research_root=research_root, prefs=prefs)
    return build_approval_commands(snapshot, research_root=research_root)
