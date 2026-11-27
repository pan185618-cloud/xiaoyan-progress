"""Command-line interface for xiaoyan-progress.

Subcommands:
- serve --host 127.0.0.1 --port 3460
- snapshot --macro | --micro | --all [--json]
- check-readonly
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any

# Ensure package root is importable when invoked as `python cli.py`
_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.config import (
    CODEX_SESSIONS_ROOT,
    MAGPIE_ROUTING_ROOT,
    RESEARCH_ROOT,
    TMP_ROOT,
    get_authority_path,
    load_prefs,
)
from core.reduce_micro import MicroEngine

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _dir_fingerprint(root: Path, max_entries: int = 5000) -> dict[str, tuple[int, int]]:
    """Capture a lightweight (mtime_ns, size) snapshot of files under root."""
    out: dict[str, tuple[int, int]] = {}
    if not root.exists():
        return out
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".git")]
        for fname in filenames:
            if count >= max_entries:
                return out
            fp = Path(dirpath) / fname
            try:
                st = fp.lstat()
                out[str(fp)] = (st.st_mtime_ns, st.st_size)
                count += 1
            except OSError:
                continue
    return out


def _cmd_serve(args: argparse.Namespace) -> int:
    host = (args.host or "127.0.0.1").strip()
    if host not in LOOPBACK_HOSTS:
        print(
            f"ERROR: Refusing to bind non-loopback host {host!r}. Only 127.0.0.1 / localhost / ::1 allowed.",
            file=sys.stderr,
        )
        return 2

    import uvicorn
    from server.app import create_app

    fastapi_app = create_app(enable_watcher=not args.no_watcher)
    uvicorn.run(
        fastapi_app,
        host=host,
        port=int(args.port),
        log_level=args.log_level,
    )
    return 0


def _cmd_snapshot(args: argparse.Namespace) -> int:
    want_macro = bool(args.macro or args.all)
    want_micro = bool(args.micro or args.all)
    if not want_macro and not want_micro:
        want_macro = True
        want_micro = True

    prefs = load_prefs()
    payload: dict[str, Any] = {}

    if want_macro:
        from core.reduce_macro import build_macro_snapshot

        payload["macro"] = build_macro_snapshot(research_root=RESEARCH_ROOT, prefs=prefs)

    if want_micro:
        engine = MicroEngine(
            research_root=RESEARCH_ROOT,
            codex_sessions_root=CODEX_SESSIONS_ROOT,
            magpie_routing_root=MAGPIE_ROUTING_ROOT,
            tmp_root=TMP_ROOT,
        )
        payload["micro"] = engine.build_snapshot(prefs=prefs)

    if args.json:
        out_obj: Any = payload
        if want_macro and not want_micro:
            out_obj = payload["macro"]
        elif want_micro and not want_macro:
            out_obj = payload["micro"]
        print(json.dumps(out_obj, ensure_ascii=False, indent=2))
        return 0

    if want_macro:
        macro = payload["macro"]
        summ = macro.get("summary") or {}
        print(
            f"[MACRO] projects={summ.get('total_projects', 0)} "
            f"visible={summ.get('visible_projects', 0)} "
            f"pending_approvals={summ.get('pending_approvals', 0)} "
            f"gate_failures={summ.get('gate_failures', 0)} "
            f"authority_present={summ.get('authority_present')}"
        )
    if want_micro:
        micro = payload["micro"]
        print(
            f"[MICRO] sessions={len(micro.get('sessions', []))} "
            f"active={micro.get('active_count', 0)} "
            f"active_session={micro.get('active_session_id')}"
        )
        for s in micro.get("sessions", [])[:8]:
            ctx = s.get("context_health") or {}
            print(
                f"  - {s['session_id'][:8]} [{s['status']}] "
                f"proj={s.get('project_name') or 'unattributed'} ({s.get('attribution_confidence')}) "
                f"tokens={ctx.get('used_tokens', 0)}/{ctx.get('window_tokens', 0)} "
                f"step={s.get('current_step_title')}"
            )
    return 0


def _cmd_check_readonly(_args: argparse.Namespace) -> int:
    """Self-test verifying zero writes to research repo, authority, and telemetry dirs."""
    with tempfile.TemporaryDirectory(prefix="xy_ro_check_") as tmp_home:
        old_home = os.environ.get("HOME")
        old_auth = os.environ.get("XIAOYAN_RECEIPT_AUTHORITY")
        fake_auth = Path(tmp_home) / ".config" / "xiaoyan-r-runtime" / "receipt-authority.json"
        os.environ["HOME"] = tmp_home
        os.environ["XIAOYAN_RECEIPT_AUTHORITY"] = str(fake_auth)

        # Snapshot key governance & project dirs before read
        research_projects = RESEARCH_ROOT / "projects"
        before_auth_exists = fake_auth.exists()
        before_policy = _dir_fingerprint(RESEARCH_ROOT / "integrations" / "codex_multi_model", max_entries=500)
        before_projects_dirs = sorted(p.name for p in research_projects.iterdir()) if research_projects.is_dir() else []

        errors: list[str] = []
        try:
            from core.reduce_macro import build_macro_snapshot

            macro = build_macro_snapshot(research_root=RESEARCH_ROOT, prefs={})
            if macro.get("summary", {}).get("authority_present") is not False:
                errors.append("Expected authority_present=False under isolated HOME")
        except Exception as exc:
            errors.append(f"Macro snapshot raised: {exc}")

        try:
            engine = MicroEngine(
                research_root=RESEARCH_ROOT,
                codex_sessions_root=CODEX_SESSIONS_ROOT,
                magpie_routing_root=MAGPIE_ROUTING_ROOT,
                tmp_root=TMP_ROOT,
            )
            micro = engine.build_snapshot(prefs={})
            if not isinstance(micro.get("sessions"), list):
                errors.append("Micro snapshot missing sessions list")
        except Exception as exc:
            errors.append(f"Micro snapshot raised: {exc}")
        finally:
            if old_home is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = old_home
            if old_auth is None:
                os.environ.pop("XIAOYAN_RECEIPT_AUTHORITY", None)
            else:
                os.environ["XIAOYAN_RECEIPT_AUTHORITY"] = old_auth

        after_auth_exists = fake_auth.exists()
        after_policy = _dir_fingerprint(RESEARCH_ROOT / "integrations" / "codex_multi_model", max_entries=500)
        after_projects_dirs = sorted(p.name for p in research_projects.iterdir()) if research_projects.is_dir() else []

        if before_auth_exists != after_auth_exists or after_auth_exists:
            errors.append("CRITICAL: receipt-authority.json was created during read-only check!")
        if before_policy != after_policy:
            errors.append("CRITICAL: files under integrations/codex_multi_model changed during read-only check!")
        if before_projects_dirs != after_projects_dirs:
            errors.append("CRITICAL: top-level directories under projects/ changed during read-only check!")

        if errors:
            print("FAIL: check-readonly detected violations:")
            for err in errors:
                print(f"  - {err}")
            return 1

        print(
            f"PASS: check-readonly verified ZERO writes "
            f"(projects={len(after_projects_dirs)}, authority_present={get_authority_path().is_file()})"
        )
        return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="xiaoyan-progress",
        description="Read-only dual-layer research progress dashboard & live HUD server.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_serve = sub.add_parser("serve", help="Start loopback-only FastAPI HTTP/SSE server")
    p_serve.add_argument("--host", default="127.0.0.1", help="Loopback bind host (default: 127.0.0.1)")
    p_serve.add_argument("--port", type=int, default=3460, help="Port (default: 3460)")
    p_serve.add_argument("--log-level", default="info", help="Uvicorn log level")
    p_serve.add_argument("--no-watcher", action="store_true", help="Disable FSEvents watcher")
    p_serve.set_defaults(func=_cmd_serve)

    p_snap = sub.add_parser("snapshot", help="Print macro and/or micro snapshot")
    p_snap.add_argument("--macro", action="store_true", help="Include macro project matrix snapshot")
    p_snap.add_argument("--micro", action="store_true", help="Include micro live sessions snapshot")
    p_snap.add_argument("--all", action="store_true", help="Include both macro and micro snapshots")
    p_snap.add_argument("--json", action="store_true", help="Emit raw JSON")
    p_snap.set_defaults(func=_cmd_snapshot)

    p_ro = sub.add_parser("check-readonly", help="Verify zero writes to research repo and authority")
    p_ro.set_defaults(func=_cmd_check_readonly)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
