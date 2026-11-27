"""Pure read-only filesystem helpers with strict path containment and caching.

All reads open files in read-only binary mode (`open(..., "rb")`).
Never creates directories, never touches permissions, never writes.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import time
from typing import Any
import unicodedata

# In-memory SHA-256 cache keyed by (abs_path, mtime_ns, size)
_SHA_CACHE: dict[tuple[str, int, int], str] = {}
_JSON_CACHE: dict[tuple[str, int, int], Any] = {}
_MAX_CACHE_ENTRIES = 8192


def _prune_cache(cache: dict) -> None:
    if len(cache) > _MAX_CACHE_ENTRIES:
        # Evict oldest half (insertion-ordered dict)
        drop = len(cache) // 2
        for k in list(cache.keys())[:drop]:
            cache.pop(k, None)


def norm_rel(raw: str) -> str:
    """Normalize a relative POSIX path string (NFC, forward slashes, no leading ./)."""
    return unicodedata.normalize("NFC", raw.strip().replace("\\", "/").lstrip("./"))


def is_within(path: Path, root: Path, *, allow_symlinks: bool = False) -> bool:
    """Return True iff `path` resolves inside `root` without escaping (and optionally no symlinks)."""
    try:
        root_resolved = root.resolve()
        if not allow_symlinks:
            # Check path itself and all components between root and path for symlinks
            if path.is_symlink():
                return False
            resolved = path.resolve()
            resolved.relative_to(root_resolved)
            # Also verify no symlink along the relative chain inside root
            try:
                rel = path.relative_to(root)
                cur = root
                for part in rel.parts:
                    cur = cur / part
                    if cur.is_symlink():
                        return False
            except ValueError:
                # path wasn't lexically under root (e.g. root itself had a symlink like /var -> /private/var)
                if path.resolve() != path.absolute():
                    # Check relative to resolved root
                    rel = resolved.relative_to(root_resolved)
                    cur = root_resolved
                    for part in rel.parts:
                        cur = cur / part
                        if cur.is_symlink():
                            return False
            return True
        resolved = path.resolve()
        resolved.relative_to(root_resolved)
        return True
    except (OSError, ValueError):
        return False


def safe_resolve_in_root(root: Path, relative: str | Path, *, allow_symlinks: bool = False) -> Path | None:
    """Resolve `relative` inside `root` safely. Rejects absolute escape, `..`, null bytes, and symlinks."""
    raw = str(relative)
    if not raw or "\x00" in raw:
        return None
    rel_path = Path(raw)
    if rel_path.is_absolute():
        candidate = rel_path
    else:
        candidate = root / rel_path
    if not is_within(candidate, root, allow_symlinks=allow_symlinks):
        return None
    try:
        return candidate.resolve()
    except OSError:
        return None


def read_bytes_ro(path: Path, *, max_bytes: int | None = None) -> bytes | None:
    """Safely read bytes from an existing regular file strictly read-only."""
    try:
        if not path.is_file():
            return None
        with path.open("rb") as fh:
            return fh.read(max_bytes) if max_bytes is not None else fh.read()
    except OSError:
        return None


def read_text_ro(path: Path, *, max_bytes: int | None = None, errors: str = "replace") -> str | None:
    """Safely read UTF-8 text from an existing regular file strictly read-only."""
    data = read_bytes_ro(path, max_bytes=max_bytes)
    if data is None:
        return None
    return data.decode("utf-8", errors=errors)


def read_json_ro(path: Path, *, retries: int = 2) -> Any | None:
    """Read and parse JSON strictly read-only with mtime-keyed cache and brief half-write retry."""
    try:
        st = path.stat()
    except OSError:
        return None
    key = (str(path), st.st_mtime_ns, st.st_size)
    if key in _JSON_CACHE:
        return _JSON_CACHE[key]

    for attempt in range(retries + 1):
        data = read_bytes_ro(path)
        if data is None:
            return None
        try:
            parsed = json.loads(data.decode("utf-8"))
            _prune_cache(_JSON_CACHE)
            _JSON_CACHE[key] = parsed
            return parsed
        except (ValueError, UnicodeDecodeError):
            if attempt < retries:
                time.sleep(0.015)
                try:
                    st = path.stat()
                    key = (str(path), st.st_mtime_ns, st.st_size)
                except OSError:
                    return None
            else:
                return None
    return None


def sha256_file_ro(path: Path) -> str | None:
    """Compute hex SHA-256 of a regular file using read-only streaming + stat cache."""
    try:
        st = path.stat()
    except OSError:
        return None
    key = (str(path), st.st_mtime_ns, st.st_size)
    cached = _SHA_CACHE.get(key)
    if cached is not None:
        return cached
    h = hashlib.sha256()
    try:
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(chunk)
    except OSError:
        return None
    digest = h.hexdigest()
    _prune_cache(_SHA_CACHE)
    _SHA_CACHE[key] = digest
    return digest


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_json_bytes(value: Any) -> bytes:
    """Match formal_runtime.encoded() exact JSON serialization for HMAC verification."""
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def contract_payload_sha256(contract: dict[str, Any]) -> str:
    """Match formal_runtime.contract_payload_sha256() exact canonical digest."""
    payload = {k: v for k, v in contract.items() if k != "approval"}
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def contract_spec_sha256(contract: dict[str, Any]) -> str:
    """Match formal_runtime.contract_spec_sha256() exact runnable spec digest."""
    spec_fields = {
        k: contract.get(k)
        for k in ("project_id", "source", "source_sha256", "columns", "missing_policy", "runtime_snapshot", "analyses")
    }
    canonical = json.dumps(spec_fields, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def mtime_iso(path: Path) -> str | None:
    try:
        st = path.stat()
        return datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat()
    except OSError:
        return None


def mtime_ts(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def bounded_walk_files(root: Path, *, max_depth: int = 3, suffixes: tuple[str, ...] | None = None) -> list[Path]:
    """Bounded read-only walk returning regular non-symlink files up to `max_depth`."""
    if not root.is_dir() or root.is_symlink():
        return []
    results: list[Path] = []
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        cur, depth = stack.pop()
        try:
            with os.scandir(cur) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError:
            continue
        for entry in entries:
            if entry.name.startswith("."):
                continue
            try:
                if entry.is_symlink():
                    continue
                if entry.is_file(follow_symlinks=False):
                    if suffixes is None or entry.name.lower().endswith(suffixes):
                        results.append(Path(entry.path))
                elif entry.is_dir(follow_symlinks=False) and depth < max_depth:
                    stack.append((Path(entry.path), depth + 1))
            except OSError:
                continue
    return results


def clear_caches() -> None:
    """Clear in-memory SHA/JSON caches (for tests)."""
    _SHA_CACHE.clear()
    _JSON_CACHE.clear()
