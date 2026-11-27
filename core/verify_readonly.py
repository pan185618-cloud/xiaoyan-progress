"""Pure read-only cryptographic HMAC & formal evidence-chain verifier.

CRITICAL INVARIANT:
- NEVER creates `~/.config/xiaoyan-r-runtime/receipt-authority.json` or its parent directory.
- NEVER calls `formal_runtime.authenticate()`, `formal_runtime.key()`, or `formal_runtime.figure_mirror_path()`.
- Replicates exact canonical JSON HMAC-SHA256 verification and file/chain cross-binding checks
  purely using `open(..., "rb")`.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from pathlib import Path
import re
from typing import Any

from core.config import get_authority_path
from core.read_fs import (
    canonical_json_bytes,
    is_within,
    mtime_iso,
    read_bytes_ro,
    read_json_ro,
    sha256_file_ro,
)

SAFE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}")
FIGURE_DIR_RE = re.compile(r"a\d{2,}_figures")
FIGURE_FILE_RE = re.compile(
    r"a\d{2,}_figures/[A-Za-z0-9_.-]+\.(?:pdf|png|figmeta\.json|check\.json)"
)
RELEASED_DECISIONS = frozenset({"released", "released_with_minor_notes"})

# Verification result cache keyed by (kind, dir_str, key_sig, authority_sig)
_VERIFY_CACHE: dict[tuple[Any, ...], dict[str, Any]] = {}
_MAX_VERIFY_CACHE = 4096


def clear_verify_cache() -> None:
    _VERIFY_CACHE.clear()


def _load_authority_ro(authority_path: Path | None = None) -> tuple[bool, bytes | None, tuple[str, int, int] | None, str | None]:
    """Read receipt authority key strictly read-only. Never creates directories or files.

    Returns (authority_present, key_bytes, stat_sig, error_reason).
    """
    path = authority_path or get_authority_path()
    try:
        if path.is_symlink():
            return False, None, None, "receipt_authority_symlink_forbidden"
        if not path.exists():
            return False, None, None, "receipt_authority_missing"
        if not path.is_file():
            return False, None, None, "receipt_authority_not_regular_file"
        st = path.stat()
        if st.st_mode & 0o077:
            return False, None, (str(path), st.st_mtime_ns, st.st_size), "receipt_authority_bad_permissions"
        raw = read_json_ro(path)
        if not isinstance(raw, dict) or not isinstance(raw.get("key"), str):
            return False, None, (str(path), st.st_mtime_ns, st.st_size), "receipt_authority_malformed"
        key_bytes = bytes.fromhex(raw["key"])
        return True, key_bytes, (str(path), st.st_mtime_ns, st.st_size), None
    except (OSError, ValueError):
        return False, None, None, "receipt_authority_unreadable"


def authority_status(authority_path: Path | None = None) -> dict[str, Any]:
    present, _, _, err = _load_authority_ro(authority_path)
    return {"present": present, "error": err, "path": str(authority_path or get_authority_path())}


def _verify_mac(doc: dict[str, Any], role: str, key_bytes: bytes | None) -> tuple[str, str | None]:
    """Verify HMAC-SHA256 receipt_mac against exact formal_runtime.encoded() canonical form.

    Returns (proof, error_reason):
    - ("verified", None) if authority key present and MAC + role valid
    - ("unverified", "receipt_authority_missing") if authority missing (still checks role field)
    - ("invalid", reason) if role mismatch, missing MAC, or MAC mismatch
    """
    if doc.get("authority_role") != role:
        return "invalid", f"authority_role_mismatch(expected={role},got={doc.get('authority_role')})"
    mac = doc.get("receipt_mac")
    if not isinstance(mac, str) or not mac:
        return "invalid", "missing_receipt_mac"
    if key_bytes is None:
        return "unverified", "receipt_authority_missing"
    body = {k: v for k, v in doc.items() if k != "receipt_mac"}
    try:
        expected = hmac.new(key_bytes, canonical_json_bytes(body), hashlib.sha256).hexdigest()
    except (TypeError, ValueError):
        return "invalid", "canonical_encoding_error"
    if not hmac.compare_digest(expected, mac):
        return "invalid", "receipt_mac_mismatch"
    return "verified", None


def _verify_files_map_ro(
    directory: Path,
    expected_files: Any,
    exclude: tuple[str, ...] = (),
    *,
    skip_renv: bool = False,
) -> tuple[bool, str | None]:
    """Read-only equivalent of formal_runtime.verify_files().

    When `skip_renv=True` (used for run bundles during fast macro scans), skips re-hashing
    thousands of R package files under `renv/` while still verifying 100% of scientific
    artifacts (contract.json, results.json, figures, tables, logs) and HMAC signatures.
    Release bundles ALWAYS pass `skip_renv=False` to verify every single released file.
    """
    if not isinstance(expected_files, dict):
        return False, "invalid_files_manifest"
    try:
        dir_resolved = directory.resolve()
    except OSError:
        return False, "bundle_dir_unreadable"

    renv_checked = False
    for rel, expected_sha in expected_files.items():
        if not isinstance(rel, str) or not isinstance(expected_sha, str):
            return False, "malformed_files_entry"
        if rel in exclude:
            continue
        if skip_renv and (rel.startswith("renv/") or "/renv/" in rel):
            if not renv_checked:
                renv_dir = directory / "renv"
                if renv_dir.is_symlink() or not renv_dir.is_dir():
                    return False, "missing_bundle_renv_dir"
                renv_checked = True
            continue
        if rel.startswith("/") or ".." in Path(rel).parts or "\x00" in rel:
            return False, f"unsafe_bundle_path:{rel}"
        target = directory / rel
        if target.is_symlink() or not target.is_file():
            return False, f"missing_bundle_file:{rel}"
        try:
            target.resolve().relative_to(dir_resolved)
        except (OSError, ValueError):
            return False, f"bundle_path_escape:{rel}"
        actual_sha = sha256_file_ro(target)
        if actual_sha != expected_sha:
            return False, f"sha_mismatch:{rel}"
    return True, None


def _figure_artifacts_ro(directory: Path) -> dict[str, str] | None:
    """Pure read-only equivalent of formal_runtime.figure_artifacts()."""
    if directory.is_symlink():
        return None
    if not directory.is_dir():
        return {}
    result: dict[str, str] = {}
    try:
        for entry in sorted(os.scandir(directory), key=lambda e: e.name):
            if entry.is_symlink():
                return None
            if entry.is_dir(follow_symlinks=False) and FIGURE_DIR_RE.fullmatch(entry.name):
                sub = Path(entry.path)
                for path in sorted(sub.rglob("*")):
                    if path.is_symlink():
                        return None
                    if path.is_dir():
                        continue
                    rel = path.relative_to(directory).as_posix()
                    if not FIGURE_FILE_RE.fullmatch(rel):
                        return None
                    digest = sha256_file_ro(path)
                    if digest is None:
                        return None
                    result[rel] = digest
    except OSError:
        return None
    return result


def _stat_pair(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _evidence_dirs_for_root(root_path: Path, uses_v3: bool) -> dict[str, Path]:
    """Return canonical runs/validations/released/board paths for a project root or sub-root."""
    if uses_v3 or ((root_path / "03_证据").is_dir() and not (root_path / "03_outputs").is_dir()):
        base = root_path / "03_证据"
        return {
            "runs": base / "runs",
            "validations": base / "validation_runs",
            "released": base / "released",
            "board": base / "evidence_board.json",
        }
    return {
        "runs": root_path / "03_outputs" / "runs",
        "validations": root_path / "03_outputs" / "validation_runs",
        "released": root_path / "03_outputs" / "released",
        "board": root_path / "04_evidence" / "evidence_board.json",
    }


def _verify_single_run(
    run_dir: Path,
    key_bytes: bytes | None,
    authority_sig: tuple[str, int, int] | None,
    *,
    source_root_id: str | None = None,
) -> dict[str, Any]:
    manifest_path = run_dir / "manifest.json"
    seal_path = run_dir / "seal.json"
    legacy_seal_path = run_dir / "run_seal.json"

    cache_key = (
        "run",
        str(run_dir),
        _stat_pair(manifest_path),
        _stat_pair(seal_path),
        _stat_pair(legacy_seal_path),
        authority_sig,
    )
    if cache_key in _VERIFY_CACHE:
        return _VERIFY_CACHE[cache_key]

    manifest = read_json_ro(manifest_path) if manifest_path.is_file() else None
    seal = read_json_ro(seal_path) if seal_path.is_file() else None
    has_legacy_seal = legacy_seal_path.is_file() and not seal_path.is_file()

    run_id = run_dir.name
    issues: list[str] = []
    mirror_warnings: list[str] = []
    proof = "observed"
    sealed = False

    if isinstance(manifest, dict) and ("receipt_mac" in manifest or "authority_role" in manifest):
        m_proof, m_err = _verify_mac(manifest, "executor", key_bytes)
        if m_err and m_proof == "invalid":
            issues.append(f"manifest:{m_err}")
        if manifest.get("run_id") and manifest.get("run_id") != run_id:
            issues.append("manifest_run_id_mismatch")
        files_ok, files_err = _verify_files_map_ro(
            run_dir, manifest.get("files"), ("manifest.json", "seal.json"), skip_renv=True
        )
        if not files_ok and files_err:
            issues.append(files_err)

        if isinstance(seal, dict):
            sealed = seal.get("state") == "sealed"
            s_proof, s_err = _verify_mac(seal, "executor", key_bytes)
            if s_err and s_proof == "invalid":
                issues.append(f"seal:{s_err}")
            if seal.get("state") != "sealed":
                issues.append("seal_state_not_sealed")
            if manifest.get("state") != "completed":
                issues.append("manifest_state_not_completed")
            if seal.get("manifest_sha256") != sha256_file_ro(manifest_path):
                issues.append("seal_manifest_sha_mismatch")
            if issues:
                proof = "invalid"
            elif m_proof == "verified" and s_proof == "verified":
                proof = "verified"
            else:
                proof = "unverified"
        else:
            if issues:
                proof = "invalid"
            elif m_proof == "verified":
                proof = "verified"
            else:
                proof = "unverified"
    elif has_legacy_seal:
        sealed = True
        proof = "observed"
    else:
        proof = "observed"

    result = {
        "run_id": run_id,
        "path": str(run_dir),
        "source_root_id": source_root_id,
        "project_id": manifest.get("project_id") if isinstance(manifest, dict) else None,
        "engine": manifest.get("engine") if isinstance(manifest, dict) else ("legacy" if has_legacy_seal else None),
        "state": manifest.get("state") if isinstance(manifest, dict) else ("sealed_legacy" if has_legacy_seal else "unknown"),
        "sealed": sealed,
        "has_seal_file": seal_path.is_file() or has_legacy_seal,
        "is_legacy": has_legacy_seal,
        "evidence_scope": manifest.get("evidence_scope") if isinstance(manifest, dict) else None,
        "contract_sha256": manifest.get("contract_sha256") if isinstance(manifest, dict) else None,
        "contract_relpath": manifest.get("contract_relpath") if isinstance(manifest, dict) else None,
        "analysis_group": manifest.get("analysis_group") if isinstance(manifest, dict) else None,
        "executed_runtime_snapshot": (
            manifest.get("executed_runtime_snapshot") or manifest.get("runtime_snapshot")
            if isinstance(manifest, dict)
            else None
        ),
        "proof": proof,
        "issues": issues,
        "updated_at": mtime_iso(seal_path if seal_path.is_file() else (manifest_path if manifest_path.is_file() else run_dir)),
    }
    if len(_VERIFY_CACHE) > _MAX_VERIFY_CACHE:
        _VERIFY_CACHE.clear()
    _VERIFY_CACHE[cache_key] = result
    return result


def _verify_single_validation(
    val_dir: Path,
    runs_dir: Path,
    key_bytes: bytes | None,
    authority_sig: tuple[str, int, int] | None,
    *,
    source_root_id: str | None = None,
) -> dict[str, Any]:
    report_path = val_dir / "validation_report.json"
    val_id = val_dir.name
    report = read_json_ro(report_path) if report_path.is_file() else None
    run_id = report.get("run_id") if isinstance(report, dict) else None
    run_dir = runs_dir / run_id if isinstance(run_id, str) and SAFE_ID_RE.fullmatch(run_id) else None

    cache_key = (
        "val",
        str(val_dir),
        _stat_pair(report_path),
        _stat_pair(run_dir / "seal.json") if run_dir else None,
        _stat_pair(run_dir / "manifest.json") if run_dir else None,
        authority_sig,
    )
    if cache_key in _VERIFY_CACHE:
        return _VERIFY_CACHE[cache_key]

    issues: list[str] = []
    mirror_warnings: list[str] = []
    proof = "observed"

    if isinstance(report, dict) and ("receipt_mac" in report or "authority_role" in report):
        v_proof, v_err = _verify_mac(report, "validator", key_bytes)
        if v_err and v_proof == "invalid":
            issues.append(f"validation:{v_err}")
        if report.get("validation_id") and report.get("validation_id") != val_id:
            issues.append("validation_id_mismatch")
        if report.get("decision") != "validated":
            issues.append(f"decision:{report.get('decision')}")
        if report.get("validator_service") and report.get("validator_service") == report.get("executor_service"):
            issues.append("validator_equals_executor_service")
        if run_dir and run_dir.is_dir():
            seal_p = run_dir / "seal.json"
            manifest_p = run_dir / "manifest.json"
            if seal_p.is_file() and report.get("seal_sha256") != sha256_file_ro(seal_p):
                issues.append("validation_seal_sha_mismatch")
            if manifest_p.is_file():
                rm = read_json_ro(manifest_p)
                if isinstance(rm, dict) and report.get("contract_sha256") != rm.get("contract_sha256"):
                    issues.append("validation_contract_sha_mismatch")
        else:
            issues.append("referenced_run_missing")

        if issues:
            proof = "invalid"
        elif v_proof == "verified":
            proof = "verified"
        else:
            proof = "unverified"
    elif isinstance(report, dict):
        proof = "observed"
    else:
        proof = "invalid"
        issues.append("validation_report_missing_or_unreadable")

    result = {
        "validation_id": val_id,
        "path": str(val_dir),
        "source_root_id": source_root_id,
        "project_id": report.get("project_id") if isinstance(report, dict) else None,
        "run_id": run_id,
        "decision": report.get("decision") if isinstance(report, dict) else None,
        "evidence_scope": report.get("evidence_scope") if isinstance(report, dict) else None,
        "contract_sha256": report.get("contract_sha256") if isinstance(report, dict) else None,
        "proof": proof,
        "issues": issues,
        "updated_at": mtime_iso(report_path if report_path.is_file() else val_dir),
    }
    if len(_VERIFY_CACHE) > _MAX_VERIFY_CACHE:
        _VERIFY_CACHE.clear()
    _VERIFY_CACHE[cache_key] = result
    return result


def _verify_single_release(
    rel_dir: Path,
    root_path: Path,
    dirs: dict[str, Path],
    runs_by_id: dict[str, dict[str, Any]],
    vals_by_id: dict[str, dict[str, Any]],
    board_doc: dict[str, Any] | None,
    board_stat: tuple[int, int] | None,
    key_bytes: bytes | None,
    authority_sig: tuple[str, int, int] | None,
    *,
    source_root_id: str | None = None,
) -> dict[str, Any]:
    rel_manifest_path = rel_dir / "release_manifest.json"
    release_id = rel_dir.name
    rel = read_json_ro(rel_manifest_path) if rel_manifest_path.is_file() else None

    cache_key = (
        "release",
        str(rel_dir),
        _stat_pair(rel_manifest_path),
        board_stat,
        authority_sig,
    )
    if cache_key in _VERIFY_CACHE:
        return _VERIFY_CACHE[cache_key]

    issues: list[str] = []
    mirror_warnings: list[str] = []
    proof = "observed"
    is_formal = isinstance(rel, dict) and ("receipt_mac" in rel or "authority_role" in rel)

    run_id = rel.get("run_id") if isinstance(rel, dict) else None
    val_id = rel.get("validation_id") if isinstance(rel, dict) else None
    evidence_id = rel.get("evidence_id") if isinstance(rel, dict) else None
    evidence_scope = rel.get("evidence_scope") if isinstance(rel, dict) else None
    board_status: str | None = None

    if is_formal and isinstance(rel, dict):
        p_proof, p_err = _verify_mac(rel, "publisher", key_bytes)
        if p_err and p_proof == "invalid":
            issues.append(f"release_manifest:{p_err}")
        if rel.get("release_id") != release_id:
            issues.append("release_id_mismatch")

        files_ok, files_err = _verify_files_map_ro(rel_dir, rel.get("files"), ("release_manifest.json",))
        if not files_ok and files_err:
            issues.append(files_err)

        # Verify copied validation_report inside release dir (publisher-authenticated)
        rel_val_path = rel_dir / "validation_report.json"
        if rel_val_path.is_file():
            rel_val = read_json_ro(rel_val_path)
            if isinstance(rel_val, dict):
                if "receipt_mac" in rel_val or "authority_role" in rel_val:
                    rv_proof, rv_err = _verify_mac(rel_val, "publisher", key_bytes)
                    if rv_err and rv_proof == "invalid":
                        issues.append(f"release_val:{rv_err}")
                if rel_val.get("decision") not in RELEASED_DECISIONS:
                    issues.append(f"release_val_decision:{rel_val.get('decision')}")
                if rel_val.get("run_id") != run_id or rel_val.get("validation_id") != val_id:
                    issues.append("release_val_id_mismatch")
        else:
            issues.append("release_validation_report_missing")

        # Cross-link to original run bundle
        run_rec = runs_by_id.get(run_id) if isinstance(run_id, str) else None
        if run_rec is None:
            issues.append("linked_run_missing")
        else:
            if not run_rec["sealed"]:
                issues.append("linked_run_not_sealed")
            if run_rec["proof"] == "invalid":
                issues.append("linked_run_invalid")
            if rel.get("contract_sha256") != run_rec.get("contract_sha256"):
                issues.append("release_run_contract_sha_mismatch")
            run_dir = Path(run_rec["path"])
            if (rel_dir / "contract.json").is_file() and (run_dir / "contract.json").is_file():
                if sha256_file_ro(rel_dir / "contract.json") != sha256_file_ro(run_dir / "contract.json"):
                    issues.append("release_run_contract_copy_mismatch")
            if (rel_dir / "seal.json").is_file() and (run_dir / "seal.json").is_file():
                if sha256_file_ro(rel_dir / "seal.json") != sha256_file_ro(run_dir / "seal.json"):
                    issues.append("release_run_seal_copy_mismatch")

        # Cross-link to original validator report
        val_rec = vals_by_id.get(val_id) if isinstance(val_id, str) else None
        if val_rec is None:
            issues.append("linked_validation_missing")
        else:
            if val_rec["proof"] == "invalid":
                issues.append("linked_validation_invalid")
            if val_rec.get("decision") != "validated":
                issues.append("linked_validation_not_validated")
            if val_rec.get("run_id") != run_id:
                issues.append("linked_validation_run_mismatch")

        # Optional figure_artifacts (immutable release bundle = hard check) & convenience mirror (warning)
        mirror_warnings: list[str] = []
        if "figure_artifacts" in rel:
            expected_figs = rel.get("figure_artifacts")
            actual_figs = _figure_artifacts_ro(rel_dir)
            if not isinstance(expected_figs, dict) or actual_figs != expected_figs:
                issues.append("release_figure_artifacts_mismatch")
            mirror = rel.get("figure_mirror")
            if mirror is not None:
                if not isinstance(mirror, str) or not mirror:
                    mirror_warnings.append("release_figure_mirror_invalid")
                else:
                    mirror_path = root_path / mirror
                    if not is_within(mirror_path, root_path, allow_symlinks=False):
                        mirror_warnings.append("release_figure_mirror_outside_project")
                    elif mirror_path.is_dir():
                        mirror_figs = _figure_artifacts_ro(mirror_path)
                        if mirror_figs is None:
                            mirror_warnings.append("release_figure_mirror_unreadable")
                        elif isinstance(expected_figs, dict) and mirror_figs != expected_figs:
                            non_check_diffs = [
                                k for k in (set(expected_figs) | set(mirror_figs))
                                if not k.endswith(".check.json") and expected_figs.get(k) != mirror_figs.get(k)
                            ]
                            if non_check_diffs:
                                mirror_warnings.append("release_figure_mirror_content_drift")
                            else:
                                mirror_warnings.append("release_figure_mirror_check_json_refreshed")

        # Cross-link to evidence_board.json
        if not isinstance(board_doc, dict) or not isinstance(board_doc.get("evidence"), list):
            issues.append("evidence_board_missing")
        else:
            matches = [
                item for item in board_doc["evidence"]
                if isinstance(item, dict) and item.get("evidence_id") == evidence_id
            ]
            if len(matches) != 1:
                issues.append(f"evidence_board_match_count:{len(matches)}")
            else:
                entry = matches[0]
                board_status = entry.get("status")
                if board_status not in {"released", "test_only"}:
                    issues.append(f"evidence_board_status:{board_status}")
                if entry.get("release_manifest_sha256") != sha256_file_ro(rel_manifest_path):
                    issues.append("evidence_board_manifest_sha_mismatch")
                if entry.get("run_id") != run_id:
                    issues.append("evidence_board_run_id_mismatch")
                if entry.get("evidence_scope") != evidence_scope:
                    issues.append("evidence_board_scope_mismatch")
                results_path = rel_dir / "results.json"
                if not results_path.is_file() or entry.get("results_sha256") != sha256_file_ro(results_path):
                    issues.append("evidence_board_results_sha_mismatch")

        if issues:
            proof = "invalid"
        elif (
            p_proof == "verified"
            and (run_rec is not None and run_rec["proof"] == "verified")
            and (val_rec is not None and val_rec["proof"] == "verified")
        ):
            proof = "verified"
        else:
            proof = "unverified"
    elif isinstance(rel, dict):
        proof = "observed"
    else:
        proof = "invalid"
        issues.append("release_manifest_missing_or_unreadable")

    result = {
        "release_id": release_id,
        "evidence_id": evidence_id,
        "path": str(rel_dir),
        "source_root_id": source_root_id,
        "project_id": rel.get("project_id") if isinstance(rel, dict) else None,
        "run_id": run_id,
        "validation_id": val_id,
        "engine": rel.get("engine") if isinstance(rel, dict) else None,
        "evidence_scope": evidence_scope,
        "board_status": board_status,
        "contract_sha256": rel.get("contract_sha256") if isinstance(rel, dict) else None,
        "contract_relpath": rel.get("contract_relpath") if isinstance(rel, dict) else None,
        "analysis_group": rel.get("analysis_group") if isinstance(rel, dict) else None,
        "has_figures": bool(isinstance(rel, dict) and rel.get("figure_artifacts")),
        "figure_mirror": rel.get("figure_mirror") if isinstance(rel, dict) else None,
        "proof": proof,
        "issues": issues,
        "warnings": mirror_warnings,
        "is_research_valid": (
            proof in {"verified", "unverified"}
            and not issues
            and evidence_scope != "synthetic_test"
            and (board_status in {None, "released"})
        ),
        "updated_at": mtime_iso(rel_manifest_path if rel_manifest_path.is_file() else rel_dir),
    }
    if len(_VERIFY_CACHE) > _MAX_VERIFY_CACHE:
        _VERIFY_CACHE.clear()
    _VERIFY_CACHE[cache_key] = result
    return result


def _scan_one_evidence_root(
    root_path: Path,
    uses_v3: bool,
    key_bytes: bytes | None,
    authority_sig: tuple[str, int, int] | None,
    *,
    source_root_id: str | None = None,
) -> dict[str, Any]:
    dirs = _evidence_dirs_for_root(root_path, uses_v3)
    runs: list[dict[str, Any]] = []
    validations: list[dict[str, Any]] = []
    releases: list[dict[str, Any]] = []

    runs_dir = dirs["runs"]
    if runs_dir.is_dir() and not runs_dir.is_symlink():
        try:
            for e in sorted(os.scandir(runs_dir), key=lambda x: x.name):
                if e.is_dir(follow_symlinks=False) and not e.name.startswith("."):
                    runs.append(_verify_single_run(Path(e.path), key_bytes, authority_sig, source_root_id=source_root_id))
        except OSError:
            pass
    runs_by_id = {r["run_id"]: r for r in runs}

    vals_dir = dirs["validations"]
    if vals_dir.is_dir() and not vals_dir.is_symlink():
        try:
            for e in sorted(os.scandir(vals_dir), key=lambda x: x.name):
                if e.is_dir(follow_symlinks=False) and not e.name.startswith("."):
                    validations.append(
                        _verify_single_validation(Path(e.path), runs_dir, key_bytes, authority_sig, source_root_id=source_root_id)
                    )
        except OSError:
            pass
    vals_by_id = {v["validation_id"]: v for v in validations}

    board_path = dirs["board"]
    board_stat = _stat_pair(board_path)
    board_doc = read_json_ro(board_path) if board_path.is_file() else None

    rel_root = dirs["released"]
    if rel_root.is_dir() and not rel_root.is_symlink():
        try:
            for e in sorted(os.scandir(rel_root), key=lambda x: x.name):
                if e.is_dir(follow_symlinks=False) and not e.name.startswith("."):
                    releases.append(
                        _verify_single_release(
                            Path(e.path),
                            root_path,
                            dirs,
                            runs_by_id,
                            vals_by_id,
                            board_doc,
                            board_stat,
                            key_bytes,
                            authority_sig,
                            source_root_id=source_root_id,
                        )
                    )
        except OSError:
            pass

    return {
        "runs": runs,
        "validations": validations,
        "releases": releases,
        "board_exists": board_path.is_file(),
        "board_entries": len(board_doc.get("evidence") or []) if isinstance(board_doc, dict) else 0,
    }


def verify_project_evidence(
    project_entry: dict[str, Any],
    *,
    authority_path: Path | None = None,
) -> dict[str, Any]:
    """Verify all runs, validations, releases, and evidence boards across project root + sub-roots."""
    present, key_bytes, authority_sig, auth_err = _load_authority_ro(authority_path)
    root_path = Path(project_entry["abs_path"])
    uses_v3 = bool(project_entry.get("uses_v3"))

    primary = _scan_one_evidence_root(root_path, uses_v3, key_bytes, authority_sig, source_root_id=None)

    # For hybrid V2+V3 projects, also check the alternate evidence layout if present and distinct
    if project_entry.get("is_hybrid"):
        alt = _scan_one_evidence_root(root_path, not uses_v3, key_bytes, authority_sig, source_root_id="hybrid_alt")
        seen_runs = {r["run_id"] for r in primary["runs"]}
        seen_vals = {v["validation_id"] for v in primary["validations"]}
        seen_rels = {r["release_id"] for r in primary["releases"]}
        primary["runs"].extend([r for r in alt["runs"] if r["run_id"] not in seen_runs])
        primary["validations"].extend([v for v in alt["validations"] if v["validation_id"] not in seen_vals])
        primary["releases"].extend([r for r in alt["releases"] if r["release_id"] not in seen_rels])

    sub_root_runs: list[dict[str, Any]] = []
    sub_root_vals: list[dict[str, Any]] = []
    sub_root_rels: list[dict[str, Any]] = []
    for sub in project_entry.get("evidence_sub_roots") or []:
        if not sub.get("exists"):
            continue
        sub_path = Path(sub["abs_path"])
        sub_res = _scan_one_evidence_root(
            sub_path,
            bool(sub.get("uses_v3")),
            key_bytes,
            authority_sig,
            source_root_id=sub.get("id"),
        )
        sub_root_runs.extend(sub_res["runs"])
        sub_root_vals.extend(sub_res["validations"])
        sub_root_rels.extend(sub_res["releases"])

    all_runs = primary["runs"] + sub_root_runs
    all_vals = primary["validations"] + sub_root_vals
    all_rels = primary["releases"] + sub_root_rels

    evidence_ids = sorted({r["evidence_id"] for r in all_rels if r.get("evidence_id")})
    verified_releases = [r for r in all_rels if r["proof"] == "verified" and r["is_research_valid"]]
    unverified_valid_releases = [r for r in all_rels if r["proof"] == "unverified" and r["is_research_valid"]]
    invalid_releases = [r for r in all_rels if r["proof"] == "invalid"]

    return {
        "authority_present": present,
        "authority_error": auth_err,
        "runs": all_runs,
        "validations": all_vals,
        "releases": all_rels,
        "primary_counts": {
            "runs": len(primary["runs"]),
            "sealed_runs": sum(1 for r in primary["runs"] if r["sealed"]),
            "validations": len(primary["validations"]),
            "releases": len(primary["releases"]),
        },
        "sub_root_counts": {
            "runs": len(sub_root_runs),
            "sealed_runs": sum(1 for r in sub_root_runs if r["sealed"]),
            "validations": len(sub_root_vals),
            "releases": len(sub_root_rels),
        },
        "totals": {
            "runs": len(all_runs),
            "sealed_runs": sum(1 for r in all_runs if r["sealed"]),
            "validations": len(all_vals),
            "validated": sum(1 for v in all_vals if v.get("decision") == "validated" and v["proof"] != "invalid"),
            "releases": len(all_rels),
            "verified_releases": len(verified_releases),
            "unverified_valid_releases": len(unverified_valid_releases),
            "invalid_releases": len(invalid_releases),
        },
        "evidence_ids": evidence_ids,
        "board_exists": primary["board_exists"],
    }
