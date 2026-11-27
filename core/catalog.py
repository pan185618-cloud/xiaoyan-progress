"""Read-only project catalog & layout discovery for Xiaoyan Research Workflow.

Discovers all top-level directories under `projects/` plus nested formal V3/V2
sub-projects, applies `project_roots.json` and `LEGACY_PROJECT_PATHS` cleanly,
attaches legacy split archives as `evidence_sub_roots`, and handles hybrid
V2/V3 layouts and stale mappings gracefully.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from core.config import RESEARCH_ROOT, load_prefs
from core.read_fs import is_within, norm_rel, read_json_ro

SYSTEM_TOP_DIRS = frozenset({
    "99_audit",
    "_topic_pool",
    "_validation",
    "codex_figure",
    "pss_qa",
    "测试",
})

V3_CORE_DIRS = (
    "01_构思",
    "02_数据",
    "03_证据",
    "04_合同",
    "05_代码",
    "06_图片",
    "07_手稿",
    "08_补充材料",
    "09_介绍信",
)
V2_CORE_DIRS = (
    "00_protocol",
    "01_data",
    "02_code",
    "03_outputs",
    "04_evidence",
    "05_manuscript",
    "06_validation",
)
STRUCTURAL_SKIP_CHILD_DIRS = frozenset(
    set(V3_CORE_DIRS)
    | set(V2_CORE_DIRS)
    | {"10_投稿包", "99_audit", "cowork_project", ".cowork", ".git", "__pycache__", "renv", "venv"}
)

# Mirrors formal_runtime.LEGACY_PROJECT_PATHS statically (never imports formal_runtime).
LEGACY_PROJECT_PATHS: dict[str, str] = {
    "刘芸芳charls课题": "projects/刘芸芳/刘芸芳charls课题",
}


def _count_present_dirs(path: Path, names: tuple[str, ...]) -> list[str]:
    found: list[str] = []
    for name in names:
        child = path / name
        if child.is_dir() and not child.is_symlink():
            found.append(name)
    return found


def _has_contract_files(path: Path) -> tuple[bool, bool]:
    """Return (has_v3_contracts, has_v2_contracts) strictly read-only."""
    has_v3 = False
    v3_proto = path / "04_合同"
    if v3_proto.is_dir() and not v3_proto.is_symlink():
        if (v3_proto / "research_contract.json").is_file():
            has_v3 = True
        else:
            cdir = v3_proto / "contracts"
            if cdir.is_dir() and any(
                e.name.endswith(".json") and e.is_file(follow_symlinks=False)
                for e in os.scandir(cdir)
            ):
                has_v3 = True
    has_v2 = False
    v2_proto = path / "00_protocol"
    if v2_proto.is_dir() and not v2_proto.is_symlink():
        if (v2_proto / "research_contract.json").is_file():
            has_v2 = True
        else:
            cdir = v2_proto / "contracts"
            if cdir.is_dir() and any(
                e.name.endswith(".json") and e.is_file(follow_symlinks=False)
                for e in os.scandir(cdir)
            ):
                has_v2 = True
    return has_v3, has_v2


def _classify_dir(path: Path, *, is_system: bool) -> dict[str, Any]:
    manifest_path = path / "project_manifest.json"
    manifest = read_json_ro(manifest_path) if manifest_path.is_file() else None
    manifest_layout = manifest.get("layout") if isinstance(manifest, dict) else None
    v3_dirs = _count_present_dirs(path, V3_CORE_DIRS)
    v2_dirs = _count_present_dirs(path, V2_CORE_DIRS)
    has_v3_contracts, has_v2_contracts = _has_contract_files(path)
    has_submission_pkg = (path / "10_投稿包").is_dir() and not (path / "10_投稿包").is_symlink()

    is_hybrid = False
    if manifest_layout == "xiaoyan-research-v3-zh":
        layout = "xiaoyan-research-v3-zh"
        uses_v3 = True
        category = "system" if is_system else "v3_formal"
        rule = "manifest:xiaoyan-research-v3-zh"
    elif manifest_layout in {"xiaoyan-research-v2.1", "xiaoyan-research-v2"}:
        layout = str(manifest_layout)
        uses_v3 = False
        is_hybrid = len(v3_dirs) >= 3 or has_submission_pkg
        category = "system" if is_system else "v2_formal"
        rule = f"manifest:{manifest_layout}" + ("+hybrid_v3_dirs" if is_hybrid else "")
    elif is_system:
        layout = "system"
        uses_v3 = False
        category = "system"
        rule = "system_dir"
    elif (
        (path / "03_证据").is_dir()
        and (path / "04_合同").is_dir()
        and not (path / "00_protocol").exists()
        and not (path / "03_outputs").exists()
    ):
        layout = "xiaoyan-research-v3-zh"
        uses_v3 = True
        category = "v3_formal"
        rule = "structure:v3_formal"
    elif has_v2_contracts or len(v2_dirs) >= 4:
        layout = "xiaoyan-research-v2.1"
        uses_v3 = False
        is_hybrid = len(v3_dirs) >= 3 or has_submission_pkg
        category = "v2_formal"
        rule = "structure:v2_contracts" if has_v2_contracts else "structure:v2_dirs>=4"
    elif has_v3_contracts or len(v3_dirs) >= 2:
        layout = "v3-partial"
        uses_v3 = True
        category = "v3_partial"
        rule = "structure:v3_partial"
    else:
        layout = "custom"
        uses_v3 = False
        category = "custom"
        rule = "custom_or_container"

    return {
        "manifest": manifest if isinstance(manifest, dict) else None,
        "manifest_layout": manifest_layout,
        "layout": layout,
        "uses_v3": uses_v3,
        "is_hybrid": is_hybrid,
        "category": category,
        "classification_rule": rule,
        "v3_dirs": v3_dirs,
        "v2_dirs": v2_dirs,
        "has_v3_contracts": has_v3_contracts,
        "has_v2_contracts": has_v2_contracts,
        "has_submission_pkg": has_submission_pkg,
    }


def _should_promote_nested(child: Path) -> bool:
    """Return True if a depth-1 child directory is a formal or partial V3/V2 sub-project."""
    if child.name.startswith(".") or child.name in STRUCTURAL_SKIP_CHILD_DIRS:
        return False
    if child.is_symlink() or not child.is_dir():
        return False
    if (child / "project_manifest.json").is_file():
        return True
    has_v3_c, has_v2_c = _has_contract_files(child)
    if has_v3_c or has_v2_c:
        return True
    v3_count = len(_count_present_dirs(child, V3_CORE_DIRS))
    v2_count = len(_count_present_dirs(child, V2_CORE_DIRS))
    return v3_count >= 3 or v2_count >= 4


def load_project_roots_map(research_root: Path = RESEARCH_ROOT) -> dict[str, str]:
    """Read `integrations/codex_multi_model/project_roots.json` safely."""
    roots_file = research_root / "integrations/codex_multi_model/project_roots.json"
    data = read_json_ro(roots_file)
    if not isinstance(data, dict):
        return {}
    mapping = data.get("projects")
    if not isinstance(mapping, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in mapping.items():
        if isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip():
            out[k.strip()] = norm_rel(v)
    return out


def discover_projects(
    research_root: Path = RESEARCH_ROOT,
    prefs: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Discover all top-level and nested formal projects under `research_root/projects/`.

    Strictly read-only. Never writes or creates anything on disk.
    """
    root = research_root.resolve()
    projects_dir = root / "projects"
    if not projects_dir.is_dir():
        return []

    eff_prefs = load_prefs() if prefs is None else prefs
    pinned_set = set(eff_prefs.get("pinned_projects") or [])
    hidden_set = set(eff_prefs.get("hidden_projects") or [])
    category_overrides = dict(eff_prefs.get("category_overrides") or {})

    roots_map = load_project_roots_map(root)

    # First pass: collect top-level directories + eligible depth-1 nested sub-projects.
    raw_entries: list[tuple[Path, bool, str | None]] = []  # (abs_path, is_nested, parent_rel)
    try:
        top_dirs = sorted(
            [Path(e.path) for e in os.scandir(projects_dir) if not e.name.startswith(".") and e.is_dir(follow_symlinks=False)],
            key=lambda p: p.name,
        )
    except OSError:
        return []

    for top in top_dirs:
        if not is_within(top, projects_dir, allow_symlinks=False):
            continue
        raw_entries.append((top, False, None))
        is_sys = top.name in SYSTEM_TOP_DIRS or top.name.startswith("runtime-synthetic-")
        if not is_sys:
            try:
                children = sorted(
                    [
                        Path(e.path)
                        for e in os.scandir(top)
                        if not e.name.startswith(".") and e.is_dir(follow_symlinks=False)
                    ],
                    key=lambda p: p.name,
                )
            except OSError:
                children = []
            for child in children:
                if _should_promote_nested(child):
                    raw_entries.append((child, True, norm_rel(f"projects/{top.name}")))

    # Build index by normalized rel_path ("projects/<...>")
    by_rel: dict[str, dict[str, Any]] = {}
    ordered_rels: list[str] = []

    for abs_path, is_nested, parent_rel in raw_entries:
        rel_path = norm_rel(abs_path.relative_to(root).as_posix())
        top_name = abs_path.relative_to(projects_dir).parts[0]
        is_system = (not is_nested) and (
            top_name in SYSTEM_TOP_DIRS or top_name.startswith("runtime-synthetic-")
        )
        info = _classify_dir(abs_path, is_system=is_system)

        # Default display name and id
        rel_under_projects = norm_rel(abs_path.relative_to(projects_dir).as_posix())
        display_name = rel_under_projects if is_nested else abs_path.name
        project_id = rel_under_projects

        entry: dict[str, Any] = {
            "id": project_id,
            "name": display_name,
            "dir_name": abs_path.name,
            "rel_path": rel_path,
            "abs_path": str(abs_path),
            "is_nested": is_nested,
            "parent_rel_path": parent_rel,
            "category": info["category"],
            "auto_category": info["category"],
            "layout": info["layout"],
            "manifest_layout": info["manifest_layout"],
            "uses_v3": info["uses_v3"],
            "is_hybrid": info["is_hybrid"],
            "classification_rule": info["classification_rule"],
            "v3_dirs": info["v3_dirs"],
            "v2_dirs": info["v2_dirs"],
            "has_submission_pkg": info["has_submission_pkg"],
            "manifest": info["manifest"],
            "runtime_ids": [],
            "aliases": [rel_under_projects, abs_path.name, rel_path],
            "evidence_sub_roots": [],
            "stale_mappings": [],
            "is_stub": False,
            "stub_target": None,
            "pinned": False,
            "hidden": False,
        }
        by_rel[rel_path] = entry
        ordered_rels.append(rel_path)

    # Apply LEGACY_PROJECT_PATHS (e.g. "刘芸芳charls课题" -> "projects/刘芸芳/刘芸芳charls课题")
    for legacy_id, target_rel in LEGACY_PROJECT_PATHS.items():
        norm_target = norm_rel(target_rel)
        if norm_target in by_rel:
            real_entry = by_rel[norm_target]
            if legacy_id not in real_entry["runtime_ids"]:
                real_entry["runtime_ids"].append(legacy_id)
            if legacy_id not in real_entry["aliases"]:
                real_entry["aliases"].append(legacy_id)
            # Mark top-level stub if a top-level dir has the same bare name but lacks formal structure
            stub_rel = norm_rel(f"projects/{legacy_id}")
            if stub_rel in by_rel and stub_rel != norm_target:
                stub_entry = by_rel[stub_rel]
                if stub_entry["category"] == "custom" and not stub_entry["v3_dirs"]:
                    stub_entry["is_stub"] = True
                    stub_entry["stub_target"] = real_entry["id"]
                    stub_entry["id"] = f"{legacy_id}__stub"
                    stub_entry["name"] = f"{legacy_id}（顶层占位）"
                    stub_entry["aliases"] = [stub_entry["id"], stub_rel]

    # Apply project_roots.json mappings:
    # - Direct match -> attach runtime_id & promote ASCII runtime_id as primary `id`
    # - Nested under an existing project (e.g., 99_audit/legacy_split_runs/*) -> attach as `evidence_sub_roots`
    # - Non-existent mapped path -> gracefully record stale mapping (and link to projects/<id> if that exists!)
    for mapped_id, mapped_rel in roots_map.items():
        norm_mapped = norm_rel(mapped_rel)
        mapped_abs = root / norm_mapped
        exists_on_disk = mapped_abs.is_dir() and is_within(mapped_abs, root, allow_symlinks=False)

        if norm_mapped in by_rel:
            target = by_rel[norm_mapped]
            if mapped_id not in target["runtime_ids"]:
                target["runtime_ids"].append(mapped_id)
            # Use clean ASCII runtime_id as canonical `id` while preserving Chinese dir name in `name` & `aliases`
            target["id"] = mapped_id
            if mapped_id not in target["aliases"]:
                target["aliases"].append(mapped_id)
            continue

        # Check if mapped path sits inside an existing discovered project (e.g. legacy_split_runs)
        matched_parent: dict[str, Any] | None = None
        for rel_candidate, entry in by_rel.items():
            prefix = rel_candidate.rstrip("/") + "/"
            if norm_mapped.startswith(prefix):
                if matched_parent is None or len(rel_candidate) > len(matched_parent["rel_path"]):
                    matched_parent = entry

        if matched_parent is not None and exists_on_disk:
            matched_parent["evidence_sub_roots"].append({
                "id": mapped_id,
                "rel_path": norm_mapped,
                "abs_path": str(mapped_abs.resolve()),
                "exists": True,
                "uses_v3": (mapped_abs / "03_证据").is_dir() or (
                    (mapped_abs / "project_manifest.json").is_file()
                    and (read_json_ro(mapped_abs / "project_manifest.json") or {}).get("layout")
                    == "xiaoyan-research-v3-zh"
                ),
            })
            continue

        # Stale mapping (e.g. yangyang-r4-ward-vitals -> projects/阳洋普刊（20260907）/r4_ward_vitals missing)
        if not exists_on_disk:
            fallback_rel = norm_rel(f"projects/{mapped_id}")
            stale_info = {
                "runtime_id": mapped_id,
                "mapped_rel_path": norm_mapped,
                "reason": "mapped_path_missing",
                "fallback_rel_path": fallback_rel if fallback_rel in by_rel else None,
            }
            if fallback_rel in by_rel:
                fb = by_rel[fallback_rel]
                fb["stale_mappings"].append(stale_info)
                if mapped_id not in fb["runtime_ids"]:
                    fb["runtime_ids"].append(mapped_id)
            elif matched_parent is not None:
                matched_parent["stale_mappings"].append(stale_info)

    # Apply user preferences (pinned, hidden, category_overrides)
    results: list[dict[str, Any]] = []
    for rel in ordered_rels:
        entry = by_rel[rel]
        # Deduplicate aliases preserving order
        seen_a: set[str] = set()
        dedup_aliases: list[str] = []
        for a in entry["aliases"]:
            if a and a not in seen_a:
                seen_a.add(a)
                dedup_aliases.append(a)
        entry["aliases"] = dedup_aliases

        pid = entry["id"]
        if pid in category_overrides:
            entry["category"] = category_overrides[pid]
        elif rel in category_overrides:
            entry["category"] = category_overrides[rel]

        entry["pinned"] = bool(
            pid in pinned_set or rel in pinned_set or entry["dir_name"] in pinned_set
        )
        entry["hidden"] = bool(
            pid in hidden_set or rel in hidden_set or entry["dir_name"] in hidden_set
        )
        results.append(entry)

    return results
