"""Multi-variant manuscript registry, prose sidecar, figure QC, audit/review & publication_gate reader.

Statically replicates the registry, prose, figure, and audit/review/ref-verify semantics from
`.codex/hooks/prose_gate.py` and `.codex/hooks/figure_gate.py` WITHOUT importing any hook modules.
Strictly read-only.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
from typing import Any

from core.read_fs import (
    bounded_walk_files,
    mtime_iso,
    mtime_ts,
    norm_rel,
    read_json_ro,
    read_text_ro,
    safe_resolve_in_root,
    sha256_file_ro,
)

# Static copies of prose_gate.py constants (NEVER import .codex/hooks/*)
ACTIVE_REGISTRY_KEYS = ("current", "current_working_manuscript", "organized_draft")
HISTORICAL_REGISTRY_KEYS = ("source_manuscript", "source_chinese_manuscript")
REGISTRY_PATH_FIELDS = ("path", "docx_path", "docx", "pdf_path", "reading_pdf")
FINAL_REGISTRY_STATUSES = frozenset({"complete_draft", "final", "submission_ready", "drafted"})
REQUIRED_PROSE_STEPS = frozenset({"writing_rules", "lessons", "checklist", "humanized"})
ML_MODULES = frozenset({
    "binary_model_comparison",
    "binary_external_validation",
    "regression_model_comparison",
    "cart_model",
    "survival_model_comparison",
    "ml_preflight",
    "clinical_ml",
    "tidymodels",
})

PASS_STATUSES = frozenset({"pass", "passed", "submission_ready", "ready", "verified"})
PEER_REVIEW_PASS_STATUSES = frozenset({
    "pass", "passed", "completed", "resolved", "accepted", "minor_revision_resolved", "verified",
})
FAIL_STATUSES = frozenset({
    "fail", "failed", "needs_fix", "needs_changes", "changes_required",
    "blocked", "not_ready", "rejected", "reject", "error",
})

FAIL_VERDICT_RE = re.compile(
    r"(?:verdict|status|gate_status|decision|recommendation|结论)\s*[:：=]\s*`?\**\s*"
    r"(?:fail|failed|blocked|needs_changes|changes_required|not_ready|needs_fix|reject|rejected)\b",
    re.I,
)
PASS_AUDIT_MD_RE = re.compile(
    r"(?:publication gate recommendation|verdict|status|audit status|结论)\s*[:：=]\s*`?\**\s*"
    r"(?:pass|passed|submission_ready|ready|通过)\b",
    re.I,
)
PASS_PEER_REVIEW_MD_RE = re.compile(
    r"(?:verdict|status|decision|review status|审稿结论|结论)\s*[:：=]\s*`?\**\s*"
    r"(?:pass|passed|completed|resolved|accepted|通过|已完成|已闭环)\b",
    re.I,
)
PASS_REF_MD_RE = re.compile(
    r"(?:status|verdict|result|核验结论|结论)\s*[:：=]\s*`?\**\s*(?:pass|passed|verified|submission_ready|全部通过|已核验)\b",
    re.I,
)
UNRESOLVED_REF_RE = re.compile(
    r"卷页待核|citation-to-verify|needs_fix|全文未取得|待补核|unverified",
    re.I,
)

AUDIT_NAME_RE = re.compile(r"manuscript_audit|七层审计|audit_report", re.I)
PEER_NAME_RE = re.compile(r"peer.?review|模拟审稿|审稿意见|manuscript_review|review_panel|review_summary", re.I)
REF_NAME_RE = re.compile(r"references?_verif|ref_verif|citation_audit|submission_audit", re.I)
OUTLINE_OR_NOTE_RE = re.compile(r"提纲|大纲|outline|笔记|note|草拟|框架|prompt|提示词|说明|清单|checklist|缺口|旁证|验证记录|审计", re.I)
SKIP_NAME_RE = re.compile(
    r"^(~\$|\.)|^(README|AGENTS|CLAUDE)\.md$|_RULES\.md$|handoff_",
    re.I,
)


def _extract_block_paths(block: dict[str, Any]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for field in REGISTRY_PATH_FIELDS:
        val = block.get(field)
        if isinstance(val, str) and val.strip():
            out.append((field, norm_rel(val)))
    return out


def _inspect_prose_sidecar(manuscript_path: Path) -> dict[str, Any]:
    candidates = [
        manuscript_path.with_name(manuscript_path.name + ".prose.json"),
        manuscript_path.with_suffix(".prose.json"),
    ]
    sidecar_path = next((p for p in candidates if p.is_file()), None)
    if sidecar_path is None:
        return {"status": "missing", "path": None, "sha_match": False, "steps": [], "exempt": False}
    data = read_json_ro(sidecar_path)
    if not isinstance(data, dict):
        return {"status": "invalid", "path": str(sidecar_path), "sha_match": False, "steps": [], "exempt": False}
    if data.get("exempt") is True:
        return {"status": "exempt", "path": str(sidecar_path), "sha_match": True, "steps": list(data.get("steps") or []), "exempt": True}
    actual_sha = sha256_file_ro(manuscript_path)
    recorded_sha = data.get("sha256")
    sha_match = bool(actual_sha and recorded_sha and actual_sha == recorded_sha)
    steps = [s for s in (data.get("steps") or []) if isinstance(s, str)]
    steps_ok = REQUIRED_PROSE_STEPS.issubset(set(steps))
    if sha_match and steps_ok:
        status = "pass"
    elif not sha_match:
        status = "stale"
    else:
        status = "incomplete"
    return {
        "status": status,
        "path": str(sidecar_path),
        "sha_match": sha_match,
        "steps": steps,
        "exempt": False,
        "marked_at": data.get("marked_at"),
    }


def inspect_manuscript_registry(project_root: Path) -> dict[str, Any]:
    """Parse V3 and/or V2 manuscript_registry.json and check prose sidecars."""
    reg_paths = [
        project_root / "07_手稿/manuscript_registry.json",
        project_root / "05_manuscript/manuscript_registry.json",
    ]
    found_path = next((p for p in reg_paths if p.is_file()), None)
    registry = read_json_ro(found_path) if found_path else None
    if not isinstance(registry, dict):
        registry = None

    active_entries: list[dict[str, Any]] = []
    historical_entries: list[dict[str, Any]] = []
    table_artifacts: list[dict[str, Any]] = []
    figure_artifacts: list[dict[str, Any]] = []
    other_artifacts: list[dict[str, Any]] = []

    if registry is not None:
        for key in ACTIVE_REGISTRY_KEYS:
            block = registry.get(key)
            if isinstance(block, dict):
                paths = _extract_block_paths(block)
                primary_rel = paths[0][1] if paths else None
                resolved = safe_resolve_in_root(project_root, primary_rel) if primary_rel else None
                exists = bool(resolved and resolved.is_file())
                prose = _inspect_prose_sidecar(resolved) if exists and resolved else {"status": "missing", "sha_match": False}
                active_entries.append({
                    "key": key,
                    "path": primary_rel,
                    "all_paths": [p for _, p in paths],
                    "exists": exists,
                    "abs_path": str(resolved) if exists and resolved else None,
                    "version": block.get("version"),
                    "status": block.get("status"),
                    "is_final": str(block.get("status") or "").strip().lower() in FINAL_REGISTRY_STATUSES,
                    "statistical_placeholders": block.get("statistical_placeholders"),
                    "prose": prose,
                    "mtime": mtime_ts(resolved) if exists and resolved else 0.0,
                    "updated_at": mtime_iso(resolved) if exists and resolved else None,
                })

        sections = registry.get("sections")
        if isinstance(sections, list):
            seen_paths = {e["path"] for e in active_entries if e.get("path")}
            for sec in sections:
                if not isinstance(sec, dict):
                    continue
                paths = _extract_block_paths(sec)
                primary_rel = paths[0][1] if paths else None
                if not primary_rel or primary_rel in seen_paths:
                    continue
                seen_paths.add(primary_rel)
                resolved = safe_resolve_in_root(project_root, primary_rel)
                exists = bool(resolved and resolved.is_file())
                prose = _inspect_prose_sidecar(resolved) if exists and resolved else {"status": "missing", "sha_match": False}
                active_entries.append({
                    "key": f"section:{sec.get('section_id', 'unnamed')}",
                    "path": primary_rel,
                    "all_paths": [p for _, p in paths],
                    "exists": exists,
                    "abs_path": str(resolved) if exists and resolved else None,
                    "version": sec.get("version"),
                    "status": sec.get("status"),
                    "is_final": str(sec.get("status") or "").strip().lower() in FINAL_REGISTRY_STATUSES,
                    "prose": prose,
                    "mtime": mtime_ts(resolved) if exists and resolved else 0.0,
                    "updated_at": mtime_iso(resolved) if exists and resolved else None,
                })

        for key in HISTORICAL_REGISTRY_KEYS:
            block = registry.get(key)
            if isinstance(block, dict):
                paths = _extract_block_paths(block)
                historical_entries.append({
                    "key": key,
                    "path": paths[0][1] if paths else None,
                    "status": block.get("status"),
                })
        if isinstance(registry.get("manuscript_versions"), list):
            for ver in registry["manuscript_versions"]:
                if isinstance(ver, dict):
                    paths = _extract_block_paths(ver)
                    historical_entries.append({
                        "key": "manuscript_versions",
                        "path": paths[0][1] if paths else None,
                        "version": ver.get("version"),
                        "status": ver.get("status"),
                    })

        if isinstance(registry.get("artifacts"), list):
            for art in registry["artifacts"]:
                if not isinstance(art, dict):
                    continue
                art_id = str(art.get("id") or "")
                rel = norm_rel(str(art.get("path") or "")) if art.get("path") else None
                resolved = safe_resolve_in_root(project_root, rel) if rel else None
                exists = bool(resolved and resolved.is_file())
                sha_ok: bool | None = None
                if exists and resolved and isinstance(art.get("sha256"), str):
                    sha_ok = sha256_file_ro(resolved) == art["sha256"]
                rec = {
                    "id": art_id,
                    "path": rel,
                    "exists": exists,
                    "evidence_id": art.get("evidence_id"),
                    "sha_match": sha_ok,
                }
                low = (art_id + " " + (rel or "")).lower()
                if "table" in low or "/tables/" in low:
                    table_artifacts.append(rec)
                elif "fig" in low or "/figures/" in low:
                    figure_artifacts.append(rec)
                else:
                    other_artifacts.append(rec)

    # Fallback: check for unregistered working drafts in 07_手稿 or 05_manuscript (bounded depth 2)
    unregistered_drafts: list[dict[str, Any]] = []
    if not active_entries:
        for rel_dir in ("07_手稿", "05_manuscript"):
            base = project_root / rel_dir
            if not base.is_dir():
                continue
            for f in bounded_walk_files(base, max_depth=2, suffixes=(".md", ".docx")):
                rel_p = norm_rel(f.relative_to(project_root).as_posix())
                if any(part in {"审阅与合规", "构建与审计", "写作输入", "tables", "figures", "review"} for part in Path(rel_p).parts):
                    continue
                if OUTLINE_OR_NOTE_RE.search(f.name) or SKIP_NAME_RE.search(f.name):
                    continue
                unregistered_drafts.append({
                    "path": rel_p,
                    "abs_path": str(f),
                    "prose": _inspect_prose_sidecar(f),
                    "mtime": mtime_ts(f),
                    "updated_at": mtime_iso(f),
                })

    primary_manuscript = next((e for e in active_entries if e["exists"]), None)
    if primary_manuscript is None and active_entries:
        primary_manuscript = active_entries[0]

    # Output manifest (for ML delivery gate)
    output_manifest_path: Path | None = None
    for cand in (
        project_root / "07_手稿/output_manifest.json",
        project_root / "07_手稿/构建与审计/output_manifest.json",
        project_root / "05_manuscript/output_manifest.json",
        project_root / "output_manifest.json",
    ):
        if cand.is_file():
            output_manifest_path = cand
            break

    return {
        "registry_path": norm_rel(found_path.relative_to(project_root).as_posix()) if found_path else None,
        "registry": registry,
        "active_entries": active_entries,
        "historical_entries": historical_entries,
        "primary_manuscript": primary_manuscript,
        "unregistered_drafts": unregistered_drafts[:10],
        "table_artifacts": table_artifacts,
        "figure_artifacts": figure_artifacts,
        "other_artifacts": other_artifacts,
        "ml_delivery_gate": registry.get("ml_delivery_gate") if isinstance(registry, dict) else None,
        "audit_and_gate_status": registry.get("audit_and_gate_status") if isinstance(registry, dict) else None,
        "output_manifest_path": (
            norm_rel(output_manifest_path.relative_to(project_root).as_posix()) if output_manifest_path else None
        ),
        "evidence_ids": (
            list(registry.get("evidence_ids") or [])
            if isinstance(registry, dict) and isinstance(registry.get("evidence_ids"), list)
            else []
        ),
    }


def inspect_figures_qc(project_root: Path) -> dict[str, Any]:
    """Scan 06_图片/** and 05_manuscript/figures/** for PDFs, .check.json, .figmeta.json, and figure_style.yaml."""
    style_path = project_root / "figure_style.yaml"
    allow_external = False
    target_profile: str | None = None
    if style_path.is_file():
        style_text = read_text_ro(style_path, max_bytes=16384) or ""
        if re.search(r"allow_external_figures\s*:\s*true", style_text, re.I):
            allow_external = True
        m = re.search(r"^\s*profile\s*:\s*['\"]?([A-Za-z0-9_.-]+)['\"]?", style_text, re.M)
        if m:
            target_profile = m.group(1)

    scan_dirs = [project_root / "06_图片", project_root / "05_manuscript/figures"]
    check_files: list[Path] = []
    pdf_files: list[Path] = []
    figmeta_stems: set[str] = set()

    for base in scan_dirs:
        if not base.is_dir() or base.is_symlink():
            continue
        for f in bounded_walk_files(base, max_depth=4):
            low = f.name.lower()
            if low.endswith(".check.json"):
                check_files.append(f)
            elif low.endswith(".pdf"):
                pdf_files.append(f)
            elif low.endswith(".figmeta.json"):
                figmeta_stems.add(str(f)[:-len(".figmeta.json")])

    pass_count = 0
    fail_count = 0
    invalid_count = 0
    failed_items: list[dict[str, Any]] = []
    checked_stems: set[str] = set()

    for cf in check_files:
        stem_key = str(cf)[:-len(".check.json")]
        checked_stems.add(stem_key)
        data = read_json_ro(cf)
        rel = norm_rel(cf.relative_to(project_root).as_posix())
        if not isinstance(data, dict):
            invalid_count += 1
            failed_items.append({"path": rel, "status": "invalid", "fails": ["unreadable_json"]})
            continue
        status = str(data.get("status") or "").upper()
        fails = data.get("fails")
        if status == "PASS" and fails in (None, [], ()):
            pass_count += 1
        else:
            fail_count += 1
            failed_items.append({
                "path": rel,
                "status": status or "FAIL",
                "fails": fails if isinstance(fails, list) else [str(fails)],
            })

    unchecked_pdfs: list[str] = []
    missing_figmeta: list[str] = []
    for pdf in pdf_files:
        stem_key = str(pdf)[:-4]
        rel = norm_rel(pdf.relative_to(project_root).as_posix())
        if stem_key not in checked_stems:
            unchecked_pdfs.append(rel)
        if not allow_external and stem_key not in figmeta_stems:
            missing_figmeta.append(rel)

    # Also check V2/V3 table directories for non-empty table files
    table_files: list[str] = []
    for tdir in (project_root / "05_manuscript/tables", project_root / "08_补充材料"):
        if tdir.is_dir():
            for f in bounded_walk_files(tdir, max_depth=2, suffixes=(".md", ".csv", ".docx", ".xlsx", ".tsv")):
                table_files.append(norm_rel(f.relative_to(project_root).as_posix()))

    return {
        "style_exists": style_path.is_file(),
        "target_profile": target_profile,
        "allow_external_figures": allow_external,
        "pdf_count": len(pdf_files),
        "check_total": len(check_files),
        "pass_count": pass_count,
        "fail_count": fail_count + invalid_count,
        "unchecked_pdf_count": len(unchecked_pdfs),
        "missing_figmeta_count": len(missing_figmeta),
        "failed_items": failed_items[:12],
        "unchecked_pdfs": unchecked_pdfs[:12],
        "table_files_count": len(table_files),
        "table_files": table_files[:12],
    }


def _json_has_fail(data: Any, depth: int = 0) -> bool:
    if depth > 8:
        return False
    if isinstance(data, dict):
        for k in ("status", "gate_status", "verdict", "decision", "recommendation", "result"):
            v = data.get(k)
            if isinstance(v, str) and v.strip().lower() in FAIL_STATUSES:
                return True
        for v in data.values():
            if isinstance(v, (dict, list)) and _json_has_fail(v, depth + 1):
                return True
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, (dict, list)) and _json_has_fail(item, depth + 1):
                return True
    return False


def _eval_report_candidate(candidate: Path, kind: str, manuscript_mtime: float) -> dict[str, Any]:
    """Evaluate one audit/peer_review/ref_verify candidate strictly read-only."""
    cand_mtime = mtime_ts(candidate)
    fresh = cand_mtime >= manuscript_mtime - 1.0 if manuscript_mtime > 0 else True
    verdict = "observed"

    if candidate.suffix.lower() == ".json":
        data = read_json_ro(candidate)
        if not isinstance(data, dict):
            verdict = "invalid"
        else:
            has_fail = _json_has_fail(data)
            if kind == "audit":
                blockers = data.get("blockers")
                if has_fail or (isinstance(blockers, list) and blockers) or (isinstance(blockers, int) and blockers > 0):
                    verdict = "fail"
                else:
                    vals = [str(data.get(k, "")).strip().lower() for k in ("status", "gate_status", "verdict", "decision", "recommendation")]
                    if any(v in PASS_STATUSES for v in vals):
                        verdict = "pass"
            elif kind == "peer_review":
                if has_fail:
                    verdict = "fail"
                else:
                    vals = [str(data.get(k, "")).strip().lower() for k in ("status", "verdict", "decision")]
                    if any(v in PEER_REVIEW_PASS_STATUSES for v in vals):
                        verdict = "pass"
            elif kind == "ref_verify":
                nf = data.get("needs_fix")
                if has_fail or (isinstance(nf, int) and nf > 0) or (isinstance(nf, list) and nf):
                    verdict = "fail"
                else:
                    vals = [str(data.get(k, "")).strip().lower() for k in ("status", "verdict", "decision")]
                    if any(v in PASS_STATUSES for v in vals):
                        verdict = "pass"
    elif candidate.suffix.lower() == ".md":
        text = read_text_ro(candidate, max_bytes=65536) or ""
        if not text.strip():
            verdict = "observed"
        elif FAIL_VERDICT_RE.search(text):
            verdict = "fail"
        elif kind == "audit" and PASS_AUDIT_MD_RE.search(text):
            verdict = "pass"
        elif kind == "peer_review" and PASS_PEER_REVIEW_MD_RE.search(text):
            verdict = "pass"
        elif kind == "ref_verify":
            if UNRESOLVED_REF_RE.search(text):
                verdict = "fail"
            elif PASS_REF_MD_RE.search(text):
                verdict = "pass"

    return {
        "path": str(candidate),
        "verdict": verdict,
        "fresh": fresh,
        "mtime": cand_mtime,
        "updated_at": mtime_iso(candidate),
    }


def _summarize_check_group(candidates: list[Path], kind: str, manuscript_mtime: float, project_root: Path) -> dict[str, Any]:
    if not candidates:
        return {"status": "missing", "fresh": False, "count": 0, "latest": None}
    evals = [_eval_report_candidate(c, kind, manuscript_mtime) for c in candidates]
    evals.sort(key=lambda x: x["mtime"], reverse=True)
    for ev in evals:
        try:
            ev["rel_path"] = norm_rel(Path(ev["path"]).relative_to(project_root).as_posix())
        except ValueError:
            ev["rel_path"] = ev["path"]

    fresh_passes = [e for e in evals if e["verdict"] == "pass" and e["fresh"]]
    stale_passes = [e for e in evals if e["verdict"] == "pass" and not e["fresh"]]
    fails = [e for e in evals if e["verdict"] == "fail"]

    if fresh_passes:
        status = "pass"
        best = fresh_passes[0]
    elif fails and fails[0]["fresh"]:
        status = "fail"
        best = fails[0]
    elif stale_passes:
        status = "stale"
        best = stale_passes[0]
    elif fails:
        status = "fail"
        best = fails[0]
    else:
        status = "observed"
        best = evals[0]

    return {
        "status": status,
        "fresh": best["fresh"],
        "count": len(evals),
        "latest": best,
    }


def inspect_audit_and_gates(project_root: Path, reg_info: dict[str, Any]) -> dict[str, Any]:
    """Inspect seven-layer audit, peer-review, reference verification, and publication_gate.json."""
    primary_ms = reg_info.get("primary_manuscript")
    ms_mtime = float(primary_ms.get("mtime") or 0.0) if isinstance(primary_ms, dict) else 0.0
    ms_abs = Path(primary_ms["abs_path"]) if isinstance(primary_ms, dict) and primary_ms.get("abs_path") else None

    # Bounded targeted scan — NEVER rglob all of 03_证据/
    review_files: list[Path] = []
    for rel, depth in (
        ("07_手稿/审阅与合规", 2),
        ("07_手稿/构建与审计", 2),
        ("05_manuscript/review", 2),
        ("06_validation", 2),
        ("99_audit/independent_review", 1),
        ("99_audit", 0),
    ):
        review_files.extend(bounded_walk_files(project_root / rel, max_depth=depth, suffixes=(".json", ".md")))

    citation_files: list[Path] = []
    for rel in ("03_证据/literature/citation_audit", "04_evidence/literature/citation_audit"):
        citation_files.extend(bounded_walk_files(project_root / rel, max_depth=2, suffixes=(".json", ".md")))

    audit_cands: list[Path] = []
    peer_cands: list[Path] = []
    ref_cands: list[Path] = list(citation_files)

    if ms_abs is not None:
        for p in (
            ms_abs.with_name(ms_abs.name + ".audit.json"),
            ms_abs.with_name(ms_abs.stem + ".audit.json"),
        ):
            if p.is_file():
                audit_cands.append(p)
        for p in (
            ms_abs.with_name(ms_abs.name + ".peer_review.json"),
            ms_abs.with_name(ms_abs.name + ".peer_review.md"),
            ms_abs.with_name(ms_abs.stem + ".peer_review.json"),
            ms_abs.with_name(ms_abs.stem + ".peer_review.md"),
        ):
            if p.is_file():
                peer_cands.append(p)
        for p in (
            ms_abs.with_name(ms_abs.name + ".ref_verify.json"),
            ms_abs.with_name(ms_abs.stem + ".ref_verify.json"),
        ):
            if p.is_file():
                ref_cands.append(p)

    for f in review_files:
        if AUDIT_NAME_RE.search(f.name):
            audit_cands.append(f)
        if PEER_NAME_RE.search(f.name):
            peer_cands.append(f)
        if REF_NAME_RE.search(f.name):
            ref_cands.append(f)

    # Publication gate
    pub_gate_path = next(
        (
            p
            for p in (
                project_root / "03_证据/publication_gate.json",
                project_root / "06_validation/publication_gate.json",
            )
            if p.is_file()
        ),
        None,
    )
    pub_gate_doc = read_json_ro(pub_gate_path) if pub_gate_path else None
    if not isinstance(pub_gate_doc, dict):
        pub_gate_doc = None

    audit_summary = _summarize_check_group(audit_cands, "audit", ms_mtime, project_root)
    peer_summary = _summarize_check_group(peer_cands, "peer_review", ms_mtime, project_root)
    ref_summary = _summarize_check_group(ref_cands, "ref_verify", ms_mtime, project_root)

    # Check submission packages under 10_投稿包/
    sub_dir = project_root / "10_投稿包"
    submission_packages: list[dict[str, Any]] = []
    if sub_dir.is_dir() and not sub_dir.is_symlink():
        try:
            for e in sorted(os.scandir(sub_dir), key=lambda x: x.name):
                if e.is_dir(follow_symlinks=False) and not e.name.startswith("."):
                    record_dir = project_root / "99_audit/投稿记录" / e.name
                    submission_packages.append({
                        "name": e.name,
                        "rel_path": norm_rel(f"10_投稿包/{e.name}"),
                        "has_audit_record": record_dir.is_dir(),
                        "updated_at": mtime_iso(Path(e.path)),
                    })
        except OSError:
            pass

    return {
        "audit": audit_summary,
        "peer_review": peer_summary,
        "ref_verify": ref_summary,
        "publication_gate": {
            "exists": pub_gate_doc is not None,
            "path": norm_rel(pub_gate_path.relative_to(project_root).as_posix()) if pub_gate_path else None,
            "gate_status": pub_gate_doc.get("gate_status") if pub_gate_doc else None,
            "citation_gates": pub_gate_doc.get("citation_gates") if pub_gate_doc else {},
            "exceptions": list(pub_gate_doc.get("exceptions") or []) if pub_gate_doc else [],
            "checked_at": pub_gate_doc.get("checked_at") if pub_gate_doc else None,
        },
        "submission_packages": submission_packages,
    }
