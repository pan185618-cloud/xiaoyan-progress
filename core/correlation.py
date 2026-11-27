"""Deterministic 6-tier session-to-project attribution for xiaoyan-progress.

Priority order:
1. session_meta.cwd (or latest turn_context.cwd) inside projects/<X>/
   (longest canonical prefix match, recognizing nested projects such as
   刘芸芳/刘芸芳charls课题) -> high
2. Manual user override in prefs.session_project_overrides[session_id] -> manual
3. Parent thread inheritance via parent_thread_id / session_id -> inherited
4. Explicit tool-call target paths or project_roots.json short IDs
   (vote >= 60% and >= 2 hits) -> medium
5. Recent 99_audit/delegation/*.md matching subagent agent_path / nickname -> medium
6. Otherwise project_id = None, attribution_confidence = "unattributed"
   with top candidate hints (NEVER misattribute!).
"""

from __future__ import annotations

import json
import os
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .sources.rollout import RolloutThreadState

KNOWN_NESTED_PROJECTS = (
    "刘芸芳/刘芸芳charls课题",
    "白内障生信分析/he_formal",
    "zw30 课题（子锁师兄）/探索性数据分析",
    "脑洞大开/CHARLS_CLHLS眼科选题",
)

LEGACY_PROJECT_ALIASES = {
    "刘芸芳charls课题": "刘芸芳/刘芸芳charls课题",
}

DELEGATION_MAX_AGE_SECONDS = 48 * 3600


@dataclass
class ProjectInfo:
    id: str
    name: str
    rel_path: str  # relative to RESEARCH_ROOT, e.g. "projects/热浪暴露和衰老"
    abs_path: Path
    aliases: set[str] = field(default_factory=set)
    category: str = "custom"


@dataclass
class AttributionResult:
    project_id: str | None
    project_name: str | None
    project_rel_path: str | None
    confidence: str  # high | manual | inherited | medium | unattributed
    source: str
    candidate_projects: list[dict[str, str]] = field(default_factory=list)


class ProjectCorrelator:
    """Strictly read-only project index and deterministic session attributor."""

    def __init__(self, research_root: Path) -> None:
        self.research_root = Path(research_root)
        self._projects: dict[str, ProjectInfo] = {}
        self._alias_to_id: dict[str, str] = {}
        self._prefix_list: list[tuple[str, str]] = []  # (normalized_abs_prefix, project_id)
        self._delegation_index: dict[str, str] = {}  # lower stem -> project_id
        self._last_loaded: float = 0.0
        self.reload()

    def reload(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and self._projects and (now - self._last_loaded) < 30.0:
            return
        self._projects = {}
        self._alias_to_id = {}
        self._prefix_list = []
        self._delegation_index = {}

        loaded_from_catalog = self._load_from_catalog()
        if not loaded_from_catalog:
            self._load_fallback_fs()
        else:
            # Ensure known nested projects present on disk are indexed even in minimal fixtures
            projects_dir = self.research_root / "projects"
            for nested in KNOWN_NESTED_PROJECTS:
                p = projects_dir / nested
                rel = f"projects/{nested}"
                if p.is_dir() and self._find_project_id_by_rel(rel) is None:
                    leaf = Path(nested).name
                    self._register_project(
                        ProjectInfo(
                            id=nested,
                            name=leaf,
                            rel_path=rel,
                            abs_path=p.resolve(),
                            aliases={nested, leaf, rel},
                        )
                    )

        self._load_project_roots_aliases()
        self._build_prefix_index()
        self._scan_recent_delegations()
        self._last_loaded = now

    def _register_project(self, info: ProjectInfo) -> None:
        existing = self._projects.get(info.id)
        if existing is not None:
            existing.aliases.update(info.aliases)
            return
        self._projects[info.id] = info
        for alias in info.aliases | {info.id, info.name, info.rel_path}:
            if alias:
                self._alias_to_id[alias.lower()] = info.id

    def _load_from_catalog(self) -> bool:
        try:
            from . import catalog as catalog_mod  # type: ignore[import-not-found]
        except Exception:
            return False
        discover = getattr(catalog_mod, "discover_projects", None)
        if not callable(discover):
            return False
        try:
            items = discover(research_root=self.research_root)
        except Exception:
            return False
        if not isinstance(items, list) or not items:
            return False

        for item in items:
            if not isinstance(item, dict):
                continue
            pid = str(item.get("id") or "").strip()
            name = str(item.get("name") or pid).strip()
            rel_path = str(item.get("rel_path") or f"projects/{pid}").strip()
            if not pid:
                continue
            abs_p = (self.research_root / rel_path).resolve()
            aliases = {pid, name, rel_path}
            if rel_path.startswith("projects/"):
                aliases.add(rel_path[len("projects/") :])
            for extra in (
                list(item.get("aliases") or [])
                + list(item.get("short_ids") or [])
                + list(item.get("runtime_ids") or [])
            ):
                if isinstance(extra, str) and extra.strip():
                    aliases.add(extra.strip())
            self._register_project(
                ProjectInfo(
                    id=pid,
                    name=name,
                    rel_path=rel_path,
                    abs_path=abs_p,
                    aliases=aliases,
                    category=str(item.get("category") or "custom"),
                )
            )
        return bool(self._projects)

    def _load_fallback_fs(self) -> None:
        projects_dir = self.research_root / "projects"
        if not projects_dir.is_dir():
            return
        try:
            for entry in sorted(projects_dir.iterdir(), key=lambda p: p.name):
                if not entry.is_dir() or entry.name.startswith("."):
                    continue
                rel = f"projects/{entry.name}"
                self._register_project(
                    ProjectInfo(
                        id=entry.name,
                        name=entry.name,
                        rel_path=rel,
                        abs_path=entry.resolve(),
                        aliases={entry.name, rel},
                    )
                )
        except OSError:
            pass

        for nested in KNOWN_NESTED_PROJECTS:
            p = projects_dir / nested
            if p.is_dir():
                rel = f"projects/{nested}"
                leaf = Path(nested).name
                self._register_project(
                    ProjectInfo(
                        id=nested,
                        name=leaf,
                        rel_path=rel,
                        abs_path=p.resolve(),
                        aliases={nested, leaf, rel},
                    )
                )

    def _load_project_roots_aliases(self) -> None:
        pr_path = self.research_root / "integrations" / "codex_multi_model" / "project_roots.json"
        if pr_path.is_file():
            try:
                with open(pr_path, "rb") as fh:
                    data = json.loads(fh.read().decode("utf-8", errors="replace"))
                mapping = data.get("projects") if isinstance(data, dict) else None
                if isinstance(mapping, dict):
                    for short_id, raw_rel in mapping.items():
                        if not isinstance(short_id, str) or not isinstance(raw_rel, str):
                            continue
                        target_rel = raw_rel.strip().rstrip("/")
                        # Map legacy split runs under their parent project
                        if "/99_audit/legacy_split_runs/" in target_rel:
                            target_rel = target_rel.split("/99_audit/legacy_split_runs/", 1)[0]
                        matched_id = self._find_project_id_by_rel(target_rel)
                        if matched_id is None and (self.research_root / "projects" / short_id).is_dir():
                            matched_id = self._find_project_id_by_rel(f"projects/{short_id}")
                        if matched_id is not None:
                            self._projects[matched_id].aliases.add(short_id)
                            self._alias_to_id[short_id.lower()] = matched_id
            except (OSError, json.JSONDecodeError):
                pass

        for legacy_alias, canonical_nested in LEGACY_PROJECT_ALIASES.items():
            matched_id = self._find_project_id_by_rel(f"projects/{canonical_nested}")
            if matched_id is not None:
                self._projects[matched_id].aliases.add(legacy_alias)
                self._alias_to_id[legacy_alias.lower()] = matched_id

    def _find_project_id_by_rel(self, rel_path: str) -> str | None:
        norm = rel_path.strip().rstrip("/")
        for pid, info in self._projects.items():
            if info.rel_path.rstrip("/") == norm:
                return pid
            if f"projects/{pid}".rstrip("/") == norm:
                return pid
        return None

    def _build_prefix_index(self) -> None:
        items: list[tuple[str, str]] = []
        for pid, info in self._projects.items():
            p_str = str(info.abs_path).rstrip("/")
            if p_str:
                items.append((p_str, pid))
            # Also include non-resolved path string (in case symlink differs)
            raw_str = str((self.research_root / info.rel_path)).rstrip("/")
            if raw_str and raw_str != p_str:
                items.append((raw_str, pid))
        # Longest prefix first so nested projects ALWAYS beat their parent container!
        items.sort(key=lambda kv: len(kv[0]), reverse=True)
        self._prefix_list = items

    def _scan_recent_delegations(self) -> None:
        cutoff = time.time() - DELEGATION_MAX_AGE_SECONDS
        for pid, info in self._projects.items():
            deleg_dir = info.abs_path / "99_audit" / "delegation"
            if not deleg_dir.is_dir():
                continue
            try:
                for entry in deleg_dir.iterdir():
                    if not entry.is_file() or not entry.name.endswith(".md"):
                        continue
                    try:
                        if entry.stat().st_mtime < cutoff:
                            continue
                    except OSError:
                        continue
                    stem = entry.stem.lower()
                    if stem:
                        self._delegation_index[stem] = pid
            except OSError:
                continue

    def get_project(self, project_id: str | None) -> ProjectInfo | None:
        if not project_id:
            return None
        if project_id in self._projects:
            return self._projects[project_id]
        mapped = self._alias_to_id.get(project_id.strip().lower())
        return self._projects.get(mapped) if mapped else None

    def match_path(self, path_str: str | None) -> ProjectInfo | None:
        """Match an absolute or workspace-relative path to its longest canonical project root."""
        if not path_str or not isinstance(path_str, str):
            return None
        cleaned = path_str.strip().rstrip("/")
        if not cleaned:
            return None

        # 1. Exact/prefix match against absolute paths (longest first)
        for prefix, pid in self._prefix_list:
            if cleaned == prefix or cleaned.startswith(prefix + "/"):
                return self._projects[pid]

        # 2. Try resolving relative to research_root if relative
        if not cleaned.startswith("/"):
            candidate_abs = str((self.research_root / cleaned)).rstrip("/")
            for prefix, pid in self._prefix_list:
                if candidate_abs == prefix or candidate_abs.startswith(prefix + "/"):
                    return self._projects[pid]

        # 3. Extract segment after "projects/"
        m = re.search(r"(?:^|/)projects/(.+)$", cleaned)
        if m:
            tail = m.group(1).strip("/")
            parts = tail.split("/")
            # Try 2-level nested project first (e.g., 刘芸芳/刘芸芳charls课题)
            if len(parts) >= 2:
                two = f"{parts[0]}/{parts[1]}"
                info = self.get_project(two)
                if info is not None:
                    return info
            if parts:
                info = self.get_project(parts[0])
                if info is not None:
                    return info
        return None

    def attribute_session(
        self,
        root_thread: RolloutThreadState,
        subagent_threads: list[RolloutThreadState] | None = None,
        prefs: dict[str, Any] | None = None,
        parent_project_id: str | None = None,
    ) -> AttributionResult:
        """Apply exact 6-tier deterministic attribution priority."""
        subs = subagent_threads or []
        prefs_dict = prefs if isinstance(prefs, dict) else {}
        overrides = prefs_dict.get("session_project_overrides")
        if not isinstance(overrides, dict):
            overrides = {}

        # Tier 1: session_meta.cwd (or latest turn_context.cwd) inside projects/<X>/
        cwd_match = self.match_path(root_thread.cwd)
        if cwd_match is not None:
            return AttributionResult(
                project_id=cwd_match.id,
                project_name=cwd_match.name,
                project_rel_path=cwd_match.rel_path,
                confidence="high",
                source="cwd",
            )

        # Tier 2: Manual user override in prefs.session_project_overrides[session_id]
        manual_val = overrides.get(root_thread.session_id) or overrides.get(root_thread.thread_id)
        if isinstance(manual_val, str) and manual_val.strip():
            manual_proj = self.get_project(manual_val.strip())
            if manual_proj is not None:
                return AttributionResult(
                    project_id=manual_proj.id,
                    project_name=manual_proj.name,
                    project_rel_path=manual_proj.rel_path,
                    confidence="manual",
                    source="manual_override",
                )
            # Even if project isn't in catalog, honor explicit manual override label
            return AttributionResult(
                project_id=manual_val.strip(),
                project_name=manual_val.strip(),
                project_rel_path=None,
                confidence="manual",
                source="manual_override",
            )

        # Tier 3: Parent thread inheritance via parent_thread_id / session_id
        if parent_project_id:
            parent_proj = self.get_project(parent_project_id)
            if parent_proj is not None:
                return AttributionResult(
                    project_id=parent_proj.id,
                    project_name=parent_proj.name,
                    project_rel_path=parent_proj.rel_path,
                    confidence="inherited",
                    source="parent_thread",
                )

        # Tier 4: Explicit tool-call target paths or project_roots.json short IDs
        # (vote >= 60% and >= 2 hits -> medium)
        votes: Counter[str] = Counter()
        all_threads = [root_thread, *subs]
        for th in all_threads:
            if th is not root_thread and th.cwd:
                m = self.match_path(th.cwd)
                if m is not None:
                    votes[m.id] += 1
            for call in th.tool_calls:
                if call.project_id:
                    proj = self.get_project(call.project_id)
                    if proj is not None:
                        votes[proj.id] += 2
                if call.contract_path:
                    proj = self.match_path(call.contract_path)
                    if proj is not None:
                        votes[proj.id] += 2
                for tp in call.target_paths:
                    proj = self.match_path(tp)
                    if proj is not None:
                        votes[proj.id] += 1

        total_votes = sum(votes.values())
        candidates = [
            {
                "project_id": pid,
                "project_name": self._projects[pid].name,
                "votes": str(cnt),
            }
            for pid, cnt in votes.most_common(3)
            if pid in self._projects
        ]

        if total_votes >= 2:
            top_pid, top_cnt = votes.most_common(1)[0]
            if top_cnt >= 2 and (top_cnt / total_votes) >= 0.60:
                proj = self._projects[top_pid]
                return AttributionResult(
                    project_id=proj.id,
                    project_name=proj.name,
                    project_rel_path=proj.rel_path,
                    confidence="medium",
                    source="tool_votes",
                    candidate_projects=candidates,
                )

        # Tier 5: Recent 99_audit/delegation/*.md matching subagent agent_path / nickname
        if self._delegation_index:
            deleg_votes: Counter[str] = Counter()
            for th in all_threads:
                for token in (th.agent_path, th.agent_nickname):
                    if not token:
                        continue
                    t_low = token.lower().strip("/")
                    t_leaf = t_low.split("/")[-1]
                    for stem, pid in self._delegation_index.items():
                        if t_leaf and len(t_leaf) >= 3 and (t_leaf in stem or stem in t_leaf):
                            deleg_votes[pid] += 1
            if deleg_votes:
                top_pid, _ = deleg_votes.most_common(1)[0]
                proj = self._projects[top_pid]
                return AttributionResult(
                    project_id=proj.id,
                    project_name=proj.name,
                    project_rel_path=proj.rel_path,
                    confidence="medium",
                    source="delegation_brief",
                    candidate_projects=candidates,
                )

        # Tier 6: Unattributed (never misattribute!)
        return AttributionResult(
            project_id=None,
            project_name=None,
            project_rel_path=None,
            confidence="unattributed",
            source="none",
            candidate_projects=candidates,
        )
