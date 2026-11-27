"""FastAPI loopback-only HTTP + SSE server for xiaoyan-progress.

Security & read-only guarantees:
- Loopback ONLY (127.0.0.1 / localhost / [::1]) enforced via Host & Origin middleware.
- 100% read-only wrt research repo, ~/.codex/, ~/.config/magpie/, and receipt authority.
- NEVER executes open_approval_terminal.py or any shell/subprocess command.
- Only POST /api/prefs writes ~/.config/xy_progress/prefs.json via strict Pydantic validation.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import mimetypes
import os
from pathlib import Path
import tempfile
import threading
import time
from typing import Any, AsyncIterator, Callable
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from core.config import (
    CODEX_SESSIONS_ROOT,
    MAGPIE_ROUTING_ROOT,
    PREFS_PATH,
    RESEARCH_ROOT,
    TMP_ROOT,
    get_authority_path,
    load_prefs,
    save_prefs,
)
from core.reduce_micro import MicroEngine
from core.sources.fswatch import WorkspaceWatcher

APP_VERSION = "0.1.0"
ALLOWED_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})
DEFAULT_WEB_DIR = Path(__file__).resolve().parent.parent / "web"


class PrefsUpdate(BaseModel):
    """Strict schema for POST /api/prefs (all fields optional for partial merge)."""

    model_config = ConfigDict(extra="forbid")

    pinned_projects: list[str] | None = Field(default=None, max_length=200)
    hidden_projects: list[str] | None = Field(default=None, max_length=200)
    category_overrides: dict[str, str] | None = None
    primary_contract_overrides: dict[str, str] | None = None
    session_project_overrides: dict[str, str] | None = None


def _extract_host_only(host_header: str) -> str:
    raw = (host_header or "").strip().lower()
    if not raw:
        return ""
    if raw.startswith("["):
        end = raw.find("]")
        return raw[: end + 1] if end != -1 else raw
    return raw.split(":", 1)[0]


def _is_allowed_origin(origin: str) -> bool:
    val = origin.strip().lower()
    if not val or val == "null":
        # WKWebView / opaque local origins send Origin: null
        return True
    parsed = urlparse(val)
    if parsed.scheme not in ("http", "https"):
        return False
    hostname = (parsed.hostname or "").lower()
    return hostname in ("127.0.0.1", "localhost", "::1")


def _default_macro_snapshot(research_root: Path, prefs: dict[str, Any]) -> dict[str, Any]:
    from core.reduce_macro import build_macro_snapshot

    return build_macro_snapshot(research_root=research_root, prefs=prefs)


def _default_project_detail(
    project_id: str, research_root: Path, prefs: dict[str, Any]
) -> dict[str, Any] | None:
    from core.reduce_macro import get_project_detail

    return get_project_detail(project_id, research_root=research_root, prefs=prefs)


def _default_approval_commands(research_root: Path, prefs: dict[str, Any]) -> list[dict[str, Any]]:
    from core.reduce_macro import get_approval_commands

    return get_approval_commands(research_root=research_root, prefs=prefs)


class _SSEHub:
    """Thread-safe async broadcaster for SSE subscribers."""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._subscribers: set[asyncio.Queue[str]] = set()
        self._lock = threading.Lock()

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        with self._lock:
            self._loop = loop

    def subscribe(self) -> asyncio.Queue[str]:
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=32)
        with self._lock:
            self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[str]) -> None:
        with self._lock:
            self._subscribers.discard(q)

    def notify(self, kind: str) -> None:
        with self._lock:
            loop = self._loop
            subs = list(self._subscribers)
        if loop is None or not subs:
            return

        def _push() -> None:
            for q in subs:
                try:
                    q.put_nowait(kind)
                except asyncio.QueueFull:
                    pass

        try:
            loop.call_soon_threadsafe(_push)
        except RuntimeError:
            pass


class _ServerState:
    def __init__(
        self,
        research_root: Path,
        codex_sessions_root: Path,
        magpie_routing_root: Path,
        tmp_root: Path,
        prefs_path: Path,
        web_dir: Path,
        enable_watcher: bool,
        macro_builder: Callable[[Path, dict[str, Any]], dict[str, Any]],
        detail_builder: Callable[[str, Path, dict[str, Any]], dict[str, Any] | None],
        approvals_builder: Callable[[Path, dict[str, Any]], list[dict[str, Any]]],
    ) -> None:
        self.research_root = research_root
        self.codex_sessions_root = codex_sessions_root
        self.magpie_routing_root = magpie_routing_root
        self.tmp_root = tmp_root
        self.prefs_path = prefs_path
        self.web_dir = web_dir
        self.enable_watcher = enable_watcher
        self.macro_builder = macro_builder
        self.detail_builder = detail_builder
        self.approvals_builder = approvals_builder

        self.started_monotonic = time.monotonic()
        self.micro_engine = MicroEngine(
            research_root=research_root,
            codex_sessions_root=codex_sessions_root,
            magpie_routing_root=magpie_routing_root,
            tmp_root=tmp_root,
        )
        self.sse_hub = _SSEHub()

        self._macro_lock = threading.Lock()
        self._macro_cache: dict[str, Any] | None = None
        self._macro_cache_ts: float = 0.0
        self._macro_ttl_s: float = 30.0
        self._macro_seq: int = 0

        self._micro_lock = threading.Lock()
        self.watcher = WorkspaceWatcher(
            research_root=research_root,
            codex_sessions_root=codex_sessions_root,
            magpie_routing_root=magpie_routing_root,
            tmp_root=tmp_root,
            on_macro_change=self._on_macro_fs_change,
            on_micro_change=self._on_micro_fs_change,
            debounce_s=0.25,
        )

    def _on_macro_fs_change(self, _paths: set[str]) -> None:
        self.invalidate_macro()
        self.sse_hub.notify("macro")

    def _on_micro_fs_change(self, _paths: set[str]) -> None:
        self.sse_hub.notify("micro")

    def invalidate_macro(self) -> None:
        with self._macro_lock:
            self._macro_cache = None
            self._macro_cache_ts = 0.0

    def get_prefs(self) -> dict[str, Any]:
        return load_prefs(self.prefs_path)

    def update_prefs(self, patch: PrefsUpdate) -> dict[str, Any]:
        current = self.get_prefs()
        data = patch.model_dump(exclude_none=True)
        for key in ("pinned_projects", "hidden_projects"):
            if key in data:
                current[key] = data[key]
        for key in ("category_overrides", "primary_contract_overrides", "session_project_overrides"):
            if key in data:
                # Merge dict keys; empty string value deletes an override
                merged = dict(current.get(key) or {})
                for k, v in data[key].items():
                    if v == "":
                        merged.pop(k, None)
                    else:
                        merged[k] = v
                current[key] = merged
        saved = save_prefs(current, self.prefs_path)
        self.invalidate_macro()
        self.sse_hub.notify("macro")
        self.sse_hub.notify("micro")
        return saved

    def get_macro(self, force_refresh: bool = False) -> dict[str, Any]:
        now = time.monotonic()
        with self._macro_lock:
            if (
                not force_refresh
                and self._macro_cache is not None
                and (now - self._macro_cache_ts) < self._macro_ttl_s
            ):
                return self._macro_cache
        prefs = self.get_prefs()
        snap = self.macro_builder(self.research_root, prefs)
        with self._macro_lock:
            self._macro_cache = snap
            self._macro_cache_ts = time.monotonic()
            self._macro_seq += 1
        return snap

    def get_micro(self) -> dict[str, Any]:
        prefs = self.get_prefs()
        approvals_by_proj: dict[str, int] = {}
        global_pending = 0
        with self._macro_lock:
            cached_macro = self._macro_cache
        if isinstance(cached_macro, dict):
            summary = cached_macro.get("summary")
            if isinstance(summary, dict):
                global_pending = int(summary.get("pending_approvals") or 0)
            for proj in cached_macro.get("projects") or []:
                if isinstance(proj, dict) and proj.get("id"):
                    pa = proj.get("pending_approvals")
                    if isinstance(pa, list):
                        approvals_by_proj[str(proj["id"])] = len(pa)

        with self._micro_lock:
            return self.micro_engine.build_snapshot(
                prefs=prefs,
                approvals_by_project=approvals_by_proj,
                global_pending_approvals=global_pending,
            )


def _format_sse(event: str, data: Any, event_id: str | None = None) -> str:
    lines: list[str] = []
    if event_id:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event}")
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    lines.append(f"data: {payload}")
    lines.append("")
    lines.append("")
    return "\n".join(lines)


def create_app(
    *,
    research_root: Path | None = None,
    codex_sessions_root: Path | None = None,
    magpie_routing_root: Path | None = None,
    tmp_root: Path | None = None,
    prefs_path: Path | None = None,
    web_dir: Path | None = None,
    enable_watcher: bool = True,
    macro_builder: Callable[[Path, dict[str, Any]], dict[str, Any]] | None = None,
    detail_builder: Callable[[str, Path, dict[str, Any]], dict[str, Any] | None] | None = None,
    approvals_builder: Callable[[Path, dict[str, Any]], list[dict[str, Any]]] | None = None,
) -> FastAPI:
    state = _ServerState(
        research_root=Path(research_root) if research_root is not None else RESEARCH_ROOT,
        codex_sessions_root=Path(codex_sessions_root)
        if codex_sessions_root is not None
        else CODEX_SESSIONS_ROOT,
        magpie_routing_root=Path(magpie_routing_root)
        if magpie_routing_root is not None
        else MAGPIE_ROUTING_ROOT,
        tmp_root=Path(tmp_root)
        if tmp_root is not None
        else Path(os.environ.get("TMPDIR") or tempfile.gettempdir() or TMP_ROOT),
        prefs_path=Path(prefs_path) if prefs_path is not None else PREFS_PATH,
        web_dir=Path(web_dir) if web_dir is not None else DEFAULT_WEB_DIR,
        enable_watcher=enable_watcher,
        macro_builder=macro_builder or _default_macro_snapshot,
        detail_builder=detail_builder or _default_project_detail,
        approvals_builder=approvals_builder or _default_approval_commands,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        state.sse_hub.attach_loop(asyncio.get_running_loop())
        if state.enable_watcher:
            state.watcher.start()
        try:
            yield
        finally:
            state.watcher.stop()

    fastapi_app = FastAPI(
        title="xiaoyan-progress",
        version=APP_VERSION,
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    fastapi_app.state.xy = state

    @fastapi_app.middleware("http")
    async def loopback_guard_middleware(request: Request, call_next: Callable[..., Any]) -> Any:
        host_header = request.headers.get("host", "")
        host_only = _extract_host_only(host_header)
        client_host = request.client.host if request.client else ""

        host_ok = host_only in ALLOWED_LOOPBACK_HOSTS or (
            host_only == "testserver" and client_host == "testclient"
        )
        if not host_ok:
            return JSONResponse(
                status_code=403,
                content={"detail": "Forbidden: loopback Host required"},
            )

        origin = request.headers.get("origin")
        if origin is not None and not _is_allowed_origin(origin):
            return JSONResponse(
                status_code=403,
                content={"detail": "Forbidden: non-local Origin rejected"},
            )

        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        return response

    def _serve_web_file(filename: str, fallback_title: str) -> Any:
        target = (state.web_dir / filename).resolve()
        try:
            target.relative_to(state.web_dir.resolve())
        except ValueError:
            raise HTTPException(status_code=403, detail="Forbidden path")
        if target.is_file():
            return FileResponse(target, headers={"Cache-Control": "no-store"})
        return HTMLResponse(
            content=f"<!doctype html><html><head><meta charset='utf-8'><title>{fallback_title}</title></head>"
            f"<body><p>{fallback_title} 资源尚未就绪（缺少 {filename}）。</p></body></html>",
            status_code=200,
        )

    @fastapi_app.get("/")
    async def index_page() -> Any:
        return _serve_web_file("index.html", "小研科研进度总览")

    @fastapi_app.get("/hud")
    async def hud_page() -> Any:
        return _serve_web_file("hud.html", "小研实时进度浮窗")

    @fastapi_app.get("/static/{rel_path:path}")
    async def static_asset(rel_path: str) -> Any:
        if ".." in Path(rel_path).parts:
            raise HTTPException(status_code=403, detail="Invalid static path")
        web_root = state.web_dir.resolve()
        candidate = (web_root / rel_path).resolve()
        try:
            candidate.relative_to(web_root)
        except ValueError:
            raise HTTPException(status_code=403, detail="Path outside static root")
        if not candidate.is_file():
            raise HTTPException(status_code=404, detail="Static file not found")
        media_type, _ = mimetypes.guess_type(str(candidate))
        return FileResponse(
            candidate,
            media_type=media_type or "application/octet-stream",
            headers={"Cache-Control": "no-cache"},
        )

    @fastapi_app.get("/api/health")
    async def api_health() -> dict[str, Any]:
        auth_path = get_authority_path()
        return {
            "status": "ok",
            "version": APP_VERSION,
            "research_root": str(state.research_root),
            "tmp_root": str(state.tmp_root),
            "uptime_s": round(time.monotonic() - state.started_monotonic, 2),
            "authority_present": auth_path.is_file(),
        }

    @fastapi_app.get("/api/macro")
    async def api_macro(refresh: int = Query(default=0)) -> dict[str, Any]:
        return await asyncio.to_thread(state.get_macro, bool(refresh))

    @fastapi_app.get("/api/macro/projects/{project_id:path}")
    async def api_macro_project_detail(project_id: str) -> dict[str, Any]:
        prefs = state.get_prefs()
        detail = await asyncio.to_thread(state.detail_builder, project_id, state.research_root, prefs)
        if detail is None:
            raise HTTPException(status_code=404, detail=f"Project not found: {project_id}")
        return detail

    @fastapi_app.get("/api/micro")
    async def api_micro() -> dict[str, Any]:
        return await asyncio.to_thread(state.get_micro)

    @fastapi_app.get("/api/approvals/commands")
    async def api_approval_commands() -> dict[str, Any]:
        prefs = state.get_prefs()
        items = await asyncio.to_thread(state.approvals_builder, state.research_root, prefs)
        return {"items": items}

    @fastapi_app.get("/api/prefs")
    async def api_get_prefs() -> dict[str, Any]:
        return state.get_prefs()

    @fastapi_app.post("/api/prefs")
    async def api_post_prefs(payload: PrefsUpdate) -> dict[str, Any]:
        return await asyncio.to_thread(state.update_prefs, payload)

    @fastapi_app.get("/api/events")
    async def api_events(request: Request, once: int = Query(default=0)) -> StreamingResponse:
        async def event_stream() -> AsyncIterator[str]:
            state.sse_hub.attach_loop(asyncio.get_running_loop())
            queue = state.sse_hub.subscribe()
            last_micro_seq = -1
            last_macro_seq = -1
            last_ping = time.monotonic()
            try:
                # Initial snapshots
                micro = await asyncio.to_thread(state.get_micro)
                last_micro_seq = micro["seq"]
                yield _format_sse(
                    "micro_update",
                    micro,
                    event_id=f"{micro['epoch']}:{micro['seq']}",
                )
                macro = await asyncio.to_thread(state.get_macro, False)
                last_macro_seq = state._macro_seq
                yield _format_sse(
                    "macro_update",
                    macro,
                    event_id=f"{micro['epoch']}:m{last_macro_seq}",
                )
                if once:
                    return

                while True:
                    if await request.is_disconnected():
                        break
                    trigger: str | None = None
                    try:
                        trigger = await asyncio.wait_for(queue.get(), timeout=2.0)
                    except asyncio.TimeoutError:
                        trigger = "poll"

                    if await request.is_disconnected():
                        break

                    if trigger in ("micro", "poll"):
                        micro = await asyncio.to_thread(state.get_micro)
                        if micro["seq"] != last_micro_seq:
                            last_micro_seq = micro["seq"]
                            yield _format_sse(
                                "micro_update",
                                micro,
                                event_id=f"{micro['epoch']}:{micro['seq']}",
                            )

                    if trigger == "macro":
                        macro = await asyncio.to_thread(state.get_macro, True)
                        last_macro_seq = state._macro_seq
                        yield _format_sse(
                            "macro_update",
                            macro,
                            event_id=f"{state.micro_engine.epoch}:m{last_macro_seq}",
                        )

                    now = time.monotonic()
                    if (now - last_ping) >= 15.0:
                        last_ping = now
                        yield _format_sse(
                            "ping",
                            {"ts": int(time.time()), "epoch": state.micro_engine.epoch},
                            event_id=f"{state.micro_engine.epoch}:p{int(time.time())}",
                        )
            except asyncio.CancelledError:
                return
            finally:
                state.sse_hub.unsubscribe(queue)

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return fastapi_app


app = create_app()
