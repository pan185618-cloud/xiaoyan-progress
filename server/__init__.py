"""FastAPI loopback HTTP + SSE server for xiaoyan-progress."""

from .app import app, create_app

__all__ = ["app", "create_app"]
