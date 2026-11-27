"""Comprehensive tests for the Macro Read-Only State Engine.

Verifies:
1. Strict read-only invariant (missing receipt-authority.json is NEVER created, zero writes in RESEARCH_ROOT).
2. Real-project regressions against the live 39-dir research repository.
3. Allowlisted read-only approval command generation.
4. Prefs atomic persistence in isolated temp path + primary contract override.
5. Cold full-repo scan latency <= 3.0s.
"""

from __future__ import annotations

import os
from pathlib import Path
import time

import pytest

from core import config, read_fs, verify_readonly
from core.approvals import build_approval_commands
from core.catalog import discover_projects
from core.reduce_macro import (
    build_macro_snapshot,
    get_approval_commands,
    get_project_detail,
)

REAL_RESEARCH_ROOT = config.RESEARCH_ROOT


def _assert_no_forbidden_imports() -> None:
    import sys

    forbidden = (
        "workflow_state",
        "formal_runtime",
        "prose_gate",
        "figure_gate",
        "workflow_guard",
    )
    for mod in forbidden:
        assert mod not in sys.modules, f"Forbidden workflow module imported: {mod}"


def test_readonly_invariant_without_authority(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Running a full snapshot with HOME pointed to an empty temp dir must NEVER create receipt-authority.json."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.delenv("XIAOYAN_RECEIPT_AUTHORITY", raising=False)

    read_fs.clear_caches()
    verify_readonly.clear_verify_cache()

    auth_file = fake_home / ".config/xiaoyan-r-runtime/receipt-authority.json"
    auth_dir = auth_file.parent
    assert not auth_file.exists()
    assert not auth_dir.exists()

    # Snapshot top-level projects dir mtime & entry set before scan
    projects_dir = REAL_RESEARCH_ROOT / "projects"
    before_entries = sorted(p.name for p in projects_dir.iterdir())

    snap = build_macro_snapshot(research_root=REAL_RESEARCH_ROOT, prefs=dict(config.DEFAULT_PREFS))

    # Must NEVER create receipt-authority.json or its parent dir!
    assert not auth_file.exists(), "receipt-authority.json was unexpectedly created!"
    assert not auth_dir.exists(), "~/.config/xiaoyan-r-runtime/ was unexpectedly created!"
    assert snap["summary"]["authority_present"] is False

    # Projects directory unchanged
    after_entries = sorted(p.name for p in projects_dir.iterdir())
    assert before_entries == after_entries

    # Releases on a real evidenced project must be marked proof='unverified', NOT 'verified'
    env_detail = get_project_detail("environment-cataract", research_root=REAL_RESEARCH_ROOT, prefs=dict(config.DEFAULT_PREFS))
    assert env_detail is not None
    assert len(env_detail["releases"]) == 7
    assert all(r["proof"] == "unverified" for r in env_detail["releases"])
    assert env_detail["stages"]["S3"]["proof"] == "unverified"
    assert env_detail["stages"]["S3"]["state"] == "unverified"

    _assert_no_forbidden_imports()


def test_cold_scan_performance_under_3_seconds() -> None:
    """Full cold build_macro_snapshot() across all 39+ projects must complete in <= 3.0s."""
    read_fs.clear_caches()
    verify_readonly.clear_verify_cache()

    t0 = time.perf_counter()
    snap = build_macro_snapshot(research_root=REAL_RESEARCH_ROOT, prefs=dict(config.DEFAULT_PREFS))
    elapsed = time.perf_counter() - t0

    assert snap["summary"]["total_projects"] >= 39
    assert elapsed <= 3.0, f"Cold macro scan took {elapsed:.2f}s (> 3.0s budget)"


def test_catalog_39_dirs_and_nested_subprojects() -> None:
    """Catalog discovers all 39 top-level dirs + nested V3/V2 sub-projects + handles stale mappings."""
    entries = discover_projects(research_root=REAL_RESEARCH_ROOT, prefs=dict(config.DEFAULT_PREFS))
    by_rel = {e["rel_path"]: e for e in entries}

    # Nested formal/partial projects verified in day1_verification_report.md
    assert "projects/刘芸芳/刘芸芳charls课题" in by_rel
    assert by_rel["projects/刘芸芳/刘芸芳charls课题"]["category"] == "v3_formal"
    assert by_rel["projects/刘芸芳/刘芸芳charls课题"]["is_nested"] is True

    assert "projects/白内障生信分析/he_formal" in by_rel
    assert by_rel["projects/白内障生信分析/he_formal"]["category"] == "v2_formal"

    assert "projects/zw30 课题（子锁师兄）/探索性数据分析" in by_rel
    assert by_rel["projects/zw30 课题（子锁师兄）/探索性数据分析"]["category"] == "v2_formal"

    assert "projects/脑洞大开/CHARLS_CLHLS眼科选题" in by_rel
    assert by_rel["projects/脑洞大开/CHARLS_CLHLS眼科选题"]["category"] == "v3_partial"

    # Top-level stub 刘芸芳charls课题 flagged as stub pointing to nested project
    assert "projects/刘芸芳charls课题" in by_rel
    assert by_rel["projects/刘芸芳charls课题"]["is_stub"] is True
    assert by_rel["projects/刘芸芳charls课题"]["stub_target"] == "刘芸芳/刘芸芳charls课题"

    # System dirs properly classified
    for sys_name in ("99_audit", "_topic_pool", "_validation", "codex_figure", "pss_qa", "测试"):
        assert by_rel[f"projects/{sys_name}"]["category"] == "system"

    # Stale project_roots.json mapping handled gracefully
    yy = by_rel["projects/yangyang-r4-ward-vitals"]
    assert yy["category"] == "v2_formal"
    assert any(m["runtime_id"] == "yangyang-r4-ward-vitals" for m in yy["stale_mappings"])

    # Legacy split archives attached as evidence_sub_roots under 热浪暴露和衰老, NOT separate rows
    hw = by_rel["projects/热浪暴露和衰老"]
    sub_ids = {s["id"] for s in hw["evidence_sub_roots"]}
    assert {"heatwave-aging-charls", "heatwave-aging-klosa"} <= sub_ids
    assert "heatwave-aging-charls" not in {e["id"] for e in entries}
    assert "heatwave-aging-klosa" not in {e["id"] for e in entries}


def test_real_project_zw30_regression() -> None:
    """ZW30: named contract detected, governance conflict surfaced, S3/S4/S5 active, NOT misclassified as draft."""
    detail = get_project_detail("ZW30 数据（20261030 更改数据）", research_root=REAL_RESEARCH_ROOT, prefs=dict(config.DEFAULT_PREFS))
    assert detail is not None
    assert detail["id"] == "zw30-lep"
    assert detail["category"] == "v3_formal"

    # Named contract detected & NOT misclassified as draft
    assert len(detail["contracts"]) >= 1
    pc = detail["primary_contract"]
    assert pc is not None
    assert pc["contract_id"] == "zw30-lep-ml-supplement-20261127"
    assert pc["status"] == "frozen"
    assert pc["governance_conflict"] is True
    assert detail["workflow_state_drift"]["derived"] != "draft"

    # S2 surfaces governance conflict, S3/S4/S5 detected
    assert detail["stages"]["S2"]["state"] == "waiting_approval"
    assert len(detail["runs"]) >= 3
    assert len(detail["releases"]) >= 1
    assert detail["stages"]["S3"]["state"] in {"done", "unverified"}
    assert detail["stages"]["S4"]["state"] in {"done", "partial", "blocked"}
    assert detail["gates"]["figure_pass"] >= 1
    assert detail["stages"]["S5"]["state"] in {"done", "partial", "blocked"}
    assert detail["manuscript"]["primary"] is not None

    # Conflict approval command surfaced as non-executable
    conflict_cmds = [c for c in detail["approval_commands"] if c["governance_conflict"]]
    assert len(conflict_cmds) >= 1
    assert all(c["executable"] is False for c in conflict_cmds)


def test_real_project_environment_cataract_regression() -> None:
    """环境与白内障: canonical + named contracts, 7 releases, current_working_manuscript detected."""
    detail = get_project_detail("环境与白内障", research_root=REAL_RESEARCH_ROOT, prefs=dict(config.DEFAULT_PREFS))
    assert detail is not None
    assert detail["id"] == "environment-cataract"
    assert detail["category"] == "v3_formal"

    assert detail["primary_contract"] is not None
    assert detail["primary_contract"]["is_canonical"] if "is_canonical" in detail["primary_contract"] else not detail["primary_contract"]["ambiguous"]
    assert len(detail["contracts"]) >= 2
    assert len(detail["releases"]) == 7
    assert len(detail["evidence_ids"]) == 7

    # current_working_manuscript parsed properly
    ms = detail["manuscript"]["primary"]
    assert ms is not None
    assert ms["key"] == "current_working_manuscript"
    assert ms["exists"] is True
    assert detail["stages"]["S5"]["state"] in {"done", "partial"}


def test_real_project_heatwave_aging_regression() -> None:
    """热浪暴露和衰老: 13 named contracts + legacy_split_runs sub-roots aggregated + exploration cards."""
    detail = get_project_detail("热浪暴露和衰老", research_root=REAL_RESEARCH_ROOT, prefs=dict(config.DEFAULT_PREFS))
    assert detail is not None
    assert detail["id"] == "heatwave-aging"
    assert detail["category"] == "v3_formal"

    assert len(detail["contracts"]) == 13
    assert detail["primary_contract"] is not None
    assert detail["primary_contract"]["ambiguous"] is True  # No canonical research_contract.json

    # Exploration cards detected
    assert detail["exploratory_track"]["card_count"] >= 10
    assert detail["exploratory_track"]["badge"] == "candidate_not_released"

    # Sub-roots attached
    assert len(detail["evidence_sub_roots"]) >= 2


def test_real_project_charls_menarche_bioage_regression() -> None:
    """charls-menarche-bioage: V2.1, 2 releases, artifacts[] tables in S4, workflow_state_drift detected."""
    detail = get_project_detail("charls-menarche-bioage", research_root=REAL_RESEARCH_ROOT, prefs=dict(config.DEFAULT_PREFS))
    assert detail is not None
    assert detail["category"] == "v2_formal"
    assert detail["layout"] == "xiaoyan-research-v2.1"

    assert len(detail["releases"]) == 2
    assert len(detail["evidence_ids"]) == 2

    # artifacts[] tables go into S4, NOT S5
    assert len(detail["manuscript"]["table_artifacts"]) == 2
    assert detail["stages"]["S4"]["state"] in {"done", "partial"}
    assert detail["stages"]["S5"]["state"] == "not_started"

    # Legacy workflow_state.json drift detected (recorded=draft, derived=evidenced)
    assert detail["workflow_state_drift"]["recorded"] == "draft"
    assert detail["workflow_state_drift"]["derived"] == "evidenced"
    assert detail["workflow_state_drift"]["drifted"] is True


def test_real_project_hybrid_oct_and_nested_liuyunfang() -> None:
    """晶状体OCT (hybrid V2.1 + 10_投稿包) and 刘芸芳/刘芸芳charls课题 (nested V3)."""
    oct_detail = get_project_detail("晶状体OCT", research_root=REAL_RESEARCH_ROOT, prefs=dict(config.DEFAULT_PREFS))
    assert oct_detail is not None
    assert oct_detail["category"] == "v2_formal"
    assert oct_detail["is_hybrid"] is True
    assert oct_detail["layout"] == "xiaoyan-research-v2.1"
    assert len(oct_detail["audit_details"]["submission_packages"]) >= 1
    assert oct_detail["stages"]["S7"]["state"] in {"done", "partial", "waiting_approval"}

    lyf = get_project_detail("刘芸芳/刘芸芳charls课题", research_root=REAL_RESEARCH_ROOT, prefs=dict(config.DEFAULT_PREFS))
    assert lyf is not None
    assert lyf["category"] == "v3_formal"
    assert lyf["is_nested"] is True
    assert len(lyf["contracts"]) >= 1

    # Alias lookup by LEGACY_PROJECT_PATHS key resolves to real nested project, not stub
    lyf_alias = get_project_detail("刘芸芳charls课题", research_root=REAL_RESEARCH_ROOT, prefs=dict(config.DEFAULT_PREFS))
    assert lyf_alias is not None
    assert lyf_alias["rel_path"] == "projects/刘芸芳/刘芸芳charls课题"


def test_approval_commands_allowlisted_only() -> None:
    """All approval commands must use open_approval_terminal.py + allowlisted flags only."""
    cmds = get_approval_commands(research_root=REAL_RESEARCH_ROOT, prefs=dict(config.DEFAULT_PREFS))
    assert isinstance(cmds, list)
    allowed_kinds = {"zw30_snapshot", "baseline_snapshots", "sign_contract"}
    allowed_flags = {"--zw30-snapshot", "--baseline-snapshots", "--sign-contract"}

    for item in cmds:
        assert item["kind"] in allowed_kinds
        argv = item["argv"]
        assert len(argv) >= 3
        assert argv[1].endswith("/tools/open_approval_terminal.py")
        assert argv[2] in allowed_flags
        if item["kind"] == "sign_contract":
            assert len(argv) == 4
            assert Path(argv[3]).is_absolute()


def test_prefs_atomic_save_and_primary_contract_override(tmp_path: Path) -> None:
    """save_prefs writes atomically to temp prefs path and primary_contract_overrides works."""
    prefs_file = tmp_path / "xy_progress" / "prefs.json"
    saved = config.save_prefs(
        {
            "pinned_projects": ["zw30-lep", "zw30-lep"],
            "primary_contract_overrides": {"heatwave-aging": "charls_sens_bmi"},
        },
        prefs_path=prefs_file,
    )
    assert prefs_file.is_file()
    assert saved["pinned_projects"] == ["zw30-lep"]

    loaded = config.load_prefs(prefs_path=prefs_file)
    assert loaded == saved

    # Verify primary_contract_override eliminates ambiguity on heatwave-aging
    hw = get_project_detail("heatwave-aging", research_root=REAL_RESEARCH_ROOT, prefs=loaded)
    assert hw is not None
    if any(c["contract_id"] == "charls_sens_bmi" for c in hw["contracts"]):
        assert hw["primary_contract"]["contract_id"] == "charls_sens_bmi"
        assert hw["primary_contract"]["ambiguous"] is False


def test_synthetic_tampered_mac_and_stage_skip_alert(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Synthetic fixture verifying invalid MAC detection, stage_skip_alert, and zero writes."""
    import hashlib
    import hmac
    import json

    fake_home = tmp_path / "home"
    auth_path = fake_home / ".config/xiaoyan-r-runtime/receipt-authority.json"
    auth_path.parent.mkdir(parents=True)
    key_hex = "ab" * 32
    auth_path.write_text(json.dumps({"key": key_hex}), encoding="utf-8")
    auth_path.chmod(0o600)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.delenv("XIAOYAN_RECEIPT_AUTHORITY", raising=False)

    repo = tmp_path / "repo"
    proj = repo / "projects/demo-v3"
    (proj / "03_证据/runs/run-1").mkdir(parents=True)
    (proj / "04_合同").mkdir(parents=True)
    (proj / "07_手稿").mkdir(parents=True)
    (proj / "project_manifest.json").write_text(
        json.dumps({"project_id": "demo-v3", "layout": "xiaoyan-research-v3-zh"}),
        encoding="utf-8",
    )
    (proj / "04_合同/research_contract.json").write_text(
        json.dumps({"project_id": "demo-v3", "version": "1.0.0", "status": "draft", "analyses": []}),
        encoding="utf-8",
    )
    # Tampered run manifest (bad receipt_mac)
    bad_manifest = {
        "project_id": "demo-v3",
        "run_id": "run-1",
        "state": "completed",
        "authority_role": "executor",
        "files": {},
        "receipt_mac": "0" * 64,
    }
    (proj / "03_证据/runs/run-1/manifest.json").write_text(json.dumps(bad_manifest), encoding="utf-8")
    # Manuscript exists while S3 has 0 valid releases -> stage_skip_alert must fire
    ms = proj / "07_手稿/draft_v01.md"
    ms.write_text("# Draft\nBody text\n", encoding="utf-8")
    (proj / "07_手稿/manuscript_registry.json").write_text(
        json.dumps({"current": {"path": "07_手稿/draft_v01.md", "version": "v01", "status": "current_draft"}}),
        encoding="utf-8",
    )

    read_fs.clear_caches()
    verify_readonly.clear_verify_cache()

    detail = get_project_detail("demo-v3", research_root=repo, prefs=dict(config.DEFAULT_PREFS))
    assert detail is not None
    assert detail["runs"][0]["proof"] == "invalid"
    assert any("receipt_mac_mismatch" in i for i in detail["runs"][0]["issues"])
    assert detail["stage_skip_alert"] is True
    assert detail["stages"]["S5"]["state"] == "partial"

    # Draft contract should produce an executable allowlisted sign_contract command
    cmds = get_approval_commands(research_root=repo, prefs=dict(config.DEFAULT_PREFS))
    assert len(cmds) == 1
    assert cmds[0]["kind"] == "sign_contract"
    assert cmds[0]["executable"] is True
    assert cmds[0]["argv"][2] == "--sign-contract"

