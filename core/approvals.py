"""Strictly read-only approvals, snapshot governance, handoff decisions, and allowlisted launcher builder.

NEVER executes `open_approval_terminal.py` or any shell command.
NEVER writes inside RESEARCH_ROOT.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import shlex
from typing import Any

from core.config import RESEARCH_ROOT
from core.read_fs import (
    contract_payload_sha256,
    contract_spec_sha256,
    mtime_iso,
    mtime_ts,
    norm_rel,
    read_json_ro,
    read_text_ro,
    safe_resolve_in_root,
    sha256_file_ro,
)

DEFAULT_BASELINE_SNAPSHOTS = (
    "rt-20261126-baseline-cross-sectional",
    "rt-20261126-baseline-cohort",
    "rt-20261126-baseline-clinical-ml",
)
ZW30_SNAPSHOT_ID = "rt-20261127-zw30-ml-r1"

_HANDOFF_EMPTY_RE = re.compile(r"^\s*(?:[-*]\s*)?(?:<!--.*?-->|\(无\)|（无）|无|none|n/a|待补充|（待补充）)\s*$", re.I)


def _quote_cmd(argv: list[str]) -> str:
    return " ".join(shlex.quote(arg) for arg in argv)


def _launcher_prefix(research_root: Path) -> list[str]:
    root = research_root.resolve()
    venv_py = root / ".venv/bin/python"
    py_bin = str(venv_py) if venv_py.is_file() else "/usr/bin/env python3"
    launcher = str(root / "tools/open_approval_terminal.py")
    return [py_bin, launcher]


def inspect_runtime_snapshots(research_root: Path = RESEARCH_ROOT) -> dict[str, Any]:
    """Read `formal_policy.json`, snapshot metadata, and approval JSONs purely read-only."""
    root = research_root.resolve()
    policy_path = root / "integrations/codex_multi_model/formal_policy.json"
    approvals_dir = root / "integrations/codex_multi_model/approvals"
    releases_dir = root / "integrations/codex_multi_model/runtime_releases"

    policy = read_json_ro(policy_path)
    snapshots_policy = (policy.get("runtime_snapshots") or {}) if isinstance(policy, dict) else {}

    all_ids: set[str] = set(snapshots_policy.keys())
    if releases_dir.is_dir():
        try:
            for e in os.scandir(releases_dir):
                if e.is_dir(follow_symlinks=False) and not e.name.startswith("."):
                    all_ids.add(e.name)
        except OSError:
            pass

    items: dict[str, dict[str, Any]] = {}
    for sid in sorted(all_ids):
        entry = snapshots_policy.get(sid) if isinstance(snapshots_policy.get(sid), dict) else {}
        snap_meta_path = releases_dir / sid / "snapshot.json"
        snap_exists = snap_meta_path.is_file()
        snap_sha = sha256_file_ro(snap_meta_path) if snap_exists else None
        policy_sha = entry.get("snapshot_sha256")
        sha_matches_policy = bool(snap_sha and policy_sha and snap_sha == policy_sha)

        approval_ref = entry.get("approval") if isinstance(entry.get("approval"), dict) else None
        approval_file = approvals_dir / f"runtime_snapshot_approval_{sid}.json"
        if approval_ref and isinstance(approval_ref.get("path"), str):
            resolved_app = safe_resolve_in_root(root, approval_ref["path"])
            if resolved_app is not None:
                approval_file = resolved_app
        approval_exists = approval_file.is_file()
        approval_sha_ok = False
        if approval_exists and approval_ref and isinstance(approval_ref.get("sha256"), str):
            approval_sha_ok = sha256_file_ro(approval_file) == approval_ref["sha256"]

        policy_status = entry.get("status")
        patch_of = entry.get("patch_of")
        patch_type = entry.get("patch_type")
        allow_new_runs = entry.get("allow_new_runs", True) is not False

        if policy_status == "technical_acceptance_pending_human_approval":
            state = "pending_human_approval"
        elif sid not in snapshots_policy:
            state = "unregistered"
        elif not sha_matches_policy and snap_exists:
            state = "invalid_binding"
        elif approval_exists and (approval_sha_ok or not approval_ref):
            state = "approved_no_new_runs" if not allow_new_runs else "approved"
        elif patch_of and patch_type == "non_breaking_bugfix" and policy_status == "approved":
            state = "approved_patch_inherited"
        else:
            state = "pending_human_approval"

        items[sid] = {
            "snapshot_id": sid,
            "state": state,
            "policy_status": policy_status,
            "allow_new_runs": allow_new_runs,
            "patch_of": patch_of,
            "patch_type": patch_type,
            "snapshot_exists": snap_exists,
            "snapshot_sha12": (snap_sha or policy_sha or "")[:12] or None,
            "approval_exists": approval_exists,
            "already_bound": sid in snapshots_policy,
            "note": entry.get("note"),
            "source_path": str(snap_meta_path if snap_exists else policy_path),
        }

    # Check eligibility for ZW30 and baseline snapshot interactive launchers
    zw30 = items.get(ZW30_SNAPSHOT_ID)
    zw30_pending = bool(zw30 and zw30["snapshot_exists"] and not zw30["approval_exists"] and not zw30["already_bound"])

    baseline_entries = [items.get(sid) for sid in DEFAULT_BASELINE_SNAPSHOTS]
    baseline_pending = all(
        b is not None and b["snapshot_exists"] and not b["approval_exists"] and not b["already_bound"]
        for b in baseline_entries
    )

    pending_other = [
        info for info in items.values()
        if info["state"] == "pending_human_approval"
    ]

    return {
        "policy_exists": policy_path.is_file(),
        "production_enabled": bool(policy.get("production_enabled")) if isinstance(policy, dict) else False,
        "snapshots": items,
        "zw30_pending": zw30_pending,
        "baseline_pending": baseline_pending,
        "pending_snapshots": pending_other,
    }


def verify_contract_approval_ro(
    contract_doc: dict[str, Any],
    contract_path: Path,
    research_root: Path,
) -> dict[str, Any]:
    """Verify contract approval JSON + standing_delegated parent chain + governance conflicts strictly read-only."""
    root = research_root.resolve()
    status = str(contract_doc.get("status") or "unknown")
    approval_ref = contract_doc.get("approval")
    sci_approval = contract_doc.get("scientific_approval") if isinstance(contract_doc.get("scientific_approval"), dict) else {}
    sci_status = str(sci_approval.get("status") or "")
    approved_by_user = contract_doc.get("approved_by_user")

    governance_conflict = False
    conflict_reasons: list[str] = []

    if status == "frozen":
        if "pending" in sci_status.lower() or sci_approval.get("contract_freeze_approved") is False:
            governance_conflict = True
            conflict_reasons.append(f"frozen合同但scientific_approval.status={sci_status or 'false'}")
        if approved_by_user is False:
            governance_conflict = True
            conflict_reasons.append("frozen合同但approved_by_user=false")

    approval_valid = False
    approval_mode: str | None = None
    approved_by: str | None = None
    approval_error: str | None = None

    if isinstance(approval_ref, dict) and isinstance(approval_ref.get("path"), str):
        app_path = safe_resolve_in_root(root, approval_ref["path"])
        if app_path is None or not app_path.is_file():
            approval_error = "approval_file_missing"
        else:
            actual_sha = sha256_file_ro(app_path)
            if approval_ref.get("sha256") and actual_sha != approval_ref.get("sha256"):
                approval_error = "approval_sha_mismatch"
            else:
                decision = read_json_ro(app_path)
                if not isinstance(decision, dict):
                    approval_error = "approval_json_unreadable"
                else:
                    approval_mode = str(decision.get("approval_mode") or "explicit")
                    approved_by = decision.get("approved_by")
                    try:
                        expected_payload = contract_payload_sha256(contract_doc)
                    except (TypeError, ValueError):
                        expected_payload = None
                    if (
                        decision.get("decision") == "approved"
                        and decision.get("contract_version") == contract_doc.get("version")
                        and expected_payload is not None
                        and decision.get("contract_payload_sha256") == expected_payload
                        and bool(approved_by)
                    ):
                        if approval_mode == "standing_delegated":
                            # Verify parent contract & parent approval hashes exist and match
                            p_c_rel = decision.get("parent_contract_path")
                            p_a_rel = decision.get("parent_approval_path")
                            p_c = safe_resolve_in_root(root, p_c_rel) if isinstance(p_c_rel, str) else None
                            p_a = safe_resolve_in_root(root, p_a_rel) if isinstance(p_a_rel, str) else None
                            if (
                                p_c and p_c.is_file() and p_a and p_a.is_file()
                                and sha256_file_ro(p_c) == decision.get("parent_contract_sha256")
                                and sha256_file_ro(p_a) == decision.get("parent_approval_sha256")
                            ):
                                approval_valid = True
                            else:
                                approval_error = "delegated_parent_chain_mismatch"
                        else:
                            approval_valid = True
                    else:
                        approval_error = "approval_payload_or_version_mismatch"
    elif isinstance(approval_ref, dict) and approval_ref.get("scope") == "fixture_test":
        approval_mode = "fixture_test"
        approval_valid = True
    else:
        approval_error = "missing_approval_binding"

    if status == "frozen" and not approval_valid and approval_mode != "fixture_test":
        governance_conflict = True
        if approval_error:
            conflict_reasons.append(f"frozen合同审批校验异常:{approval_error}")

    return {
        "approval_valid": approval_valid,
        "approval_mode": approval_mode,
        "approved_by": approved_by,
        "approval_error": approval_error,
        "governance_conflict": governance_conflict,
        "conflict_reasons": conflict_reasons,
        "scientific_approval_status": sci_status or None,
    }


def match_preflight_receipts(protocol_dir: Path, contract_doc: dict[str, Any]) -> dict[str, Any]:
    """Check `preflight_receipt_*.json` inside `protocol_dir` matching this contract's spec SHA."""
    if not protocol_dir.is_dir():
        return {"found": False, "passed": False, "path": None, "n_rows": None}
    try:
        target_spec = contract_spec_sha256(contract_doc)
    except (TypeError, ValueError):
        target_spec = None

    receipts: list[Path] = []
    try:
        for e in os.scandir(protocol_dir):
            if e.is_file(follow_symlinks=False) and e.name.startswith("preflight_receipt_") and e.name.endswith(".json"):
                receipts.append(Path(e.path))
    except OSError:
        return {"found": False, "passed": False, "path": None, "n_rows": None}

    receipts.sort(key=mtime_ts, reverse=True)
    best_any: dict[str, Any] | None = None
    for rp in receipts:
        doc = read_json_ro(rp)
        if not isinstance(doc, dict):
            continue
        dp = doc.get("data_preflight") if isinstance(doc.get("data_preflight"), dict) else {}
        passed = doc.get("status") == "passed" and dp.get("passed") is True
        spec_match = bool(target_spec and doc.get("contract_spec_sha256") == target_spec)
        rec = {
            "found": True,
            "passed": passed,
            "spec_match": spec_match,
            "path": str(rp),
            "n_rows": dp.get("n_rows"),
            "analyses_checked": doc.get("analyses_checked"),
            "updated_at": mtime_iso(rp),
        }
        if spec_match:
            return rec
        if best_any is None:
            best_any = rec

    if best_any is not None:
        return best_any
    return {"found": False, "passed": False, "spec_match": False, "path": None, "n_rows": None}


def extract_handoff_decisions(project_root: Path) -> list[dict[str, Any]]:
    """Parse latest `99_audit/handoff_*.md` for `待用户决策` and `当前阻塞` items."""
    audit_dir = project_root / "99_audit"
    if not audit_dir.is_dir():
        return []
    handoffs: list[Path] = []
    try:
        for e in os.scandir(audit_dir):
            if e.is_file(follow_symlinks=False) and e.name.startswith("handoff_") and e.name.endswith(".md"):
                handoffs.append(Path(e.path))
    except OSError:
        return []
    if not handoffs:
        return []
    handoffs.sort(key=mtime_ts, reverse=True)
    latest = handoffs[0]
    text = read_text_ro(latest, max_bytes=65536)
    if not text:
        return []

    items: list[dict[str, Any]] = []
    current_section: str | None = None
    rel = norm_rel(latest.relative_to(project_root).as_posix())
    updated = mtime_iso(latest)

    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            if "待用户决策" in stripped or "待决策" in stripped:
                current_section = "decision"
            elif "当前阻塞" in stripped or "阻塞" in stripped:
                current_section = "blocker"
            else:
                current_section = None
            continue
        if current_section is None or not stripped:
            continue
        if _HANDOFF_EMPTY_RE.match(stripped):
            continue
        if stripped.startswith(("-", "*", "•")) or re.match(r"^\d+\.\s+", stripped):
            cleaned = re.sub(r"^(?:[-*•]|\d+\.)\s+", "", stripped).strip()
            if cleaned and not _HANDOFF_EMPTY_RE.match(cleaned):
                items.append({
                    "kind": current_section,
                    "text": cleaned[:240],
                    "source_path": rel,
                    "updated_at": updated,
                })
    return items[:12]


def build_approval_commands(
    macro_snapshot: dict[str, Any],
    research_root: Path = RESEARCH_ROOT,
) -> list[dict[str, Any]]:
    """Build strictly read-only allowlisted approval command descriptors from a macro snapshot.

    NEVER executes any command. Every `argv` uses ONLY `open_approval_terminal.py` with
    allowlisted flags (`--zw30-snapshot`, `--baseline-snapshots`, `--sign-contract <abs_path>`).
    """
    prefix = _launcher_prefix(research_root)
    commands: list[dict[str, Any]] = []
    snapshots_info = macro_snapshot.get("runtime_snapshots") or inspect_runtime_snapshots(research_root)

    if snapshots_info.get("zw30_pending"):
        zw = (snapshots_info.get("snapshots") or {}).get(ZW30_SNAPSHOT_ID) or {}
        argv = [*prefix, "--zw30-snapshot"]
        commands.append({
            "id": f"snapshot:{ZW30_SNAPSHOT_ID}",
            "kind": "zw30_snapshot",
            "project_id": "zw30-lep",
            "project_name": "ZW30 数据（20261030 更改数据）",
            "title": f"批准 ZW30 运行时快照 {ZW30_SNAPSHOT_ID}",
            "target_label": ZW30_SNAPSHOT_ID,
            "version": "r1",
            "sha12": zw.get("snapshot_sha12") or "",
            "preflight_passed": True,
            "governance_conflict": False,
            "executable": True,
            "argv": argv,
            "display_cmd": _quote_cmd(argv),
            "source_path": zw.get("source_path") or str(research_root / "integrations/codex_multi_model/formal_policy.json"),
        })

    if snapshots_info.get("baseline_pending"):
        argv = [*prefix, "--baseline-snapshots"]
        commands.append({
            "id": "snapshot:baseline_batch",
            "kind": "baseline_snapshots",
            "project_id": None,
            "project_name": "全局通用基线快照",
            "title": "批量批准 3 个通用基线运行时快照",
            "target_label": ", ".join(DEFAULT_BASELINE_SNAPSHOTS),
            "version": "20261126",
            "sha12": "",
            "preflight_passed": True,
            "governance_conflict": False,
            "executable": True,
            "argv": argv,
            "display_cmd": _quote_cmd(argv),
            "source_path": str(research_root / "integrations/codex_multi_model/formal_policy.json"),
        })

    for proj in macro_snapshot.get("projects") or []:
        if proj.get("category") == "system":
            continue
        pid = proj["id"]
        pname = proj["name"]
        for c in proj.get("contracts") or []:
            c_status = c.get("status")
            c_abs = c.get("abs_path")
            if not c_abs:
                continue
            gov_conflict = bool(c.get("governance_conflict"))
            preflight_passed = bool((c.get("preflight") or {}).get("passed"))
            cid = c.get("contract_id") or "canonical"
            c_ver = str(c.get("version") or "-")
            sha12 = str(c.get("sha12") or "")

            if c_status == "draft":
                argv = [*prefix, "--sign-contract", c_abs]
                commands.append({
                    "id": f"sign:{pid}:{cid}",
                    "kind": "sign_contract",
                    "project_id": pid,
                    "project_name": pname,
                    "title": f"签署并冻结 {pname} / {cid}",
                    "target_label": c.get("rel_path") or cid,
                    "version": c_ver,
                    "sha12": sha12,
                    "preflight_passed": preflight_passed,
                    "governance_conflict": False,
                    "executable": True,
                    "argv": argv,
                    "display_cmd": _quote_cmd(argv),
                    "source_path": c_abs,
                })
            elif gov_conflict:
                argv = [*prefix, "--sign-contract", c_abs]
                commands.append({
                    "id": f"conflict:{pid}:{cid}",
                    "kind": "sign_contract",
                    "project_id": pid,
                    "project_name": pname,
                    "title": f"治理冲突待核：{pname} / {cid}（已标 frozen 但含待签/冲突字段）",
                    "target_label": c.get("rel_path") or cid,
                    "version": c_ver,
                    "sha12": sha12,
                    "preflight_passed": preflight_passed,
                    "governance_conflict": True,
                    "executable": False,
                    "argv": argv,
                    "display_cmd": _quote_cmd(argv),
                    "source_path": c_abs,
                })

    return commands
