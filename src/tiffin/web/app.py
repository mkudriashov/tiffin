"""Optional FastAPI page, bound to loopback and served with local assets only.

Tailscale Serve or another reverse proxy can publish it. Identity comes from a Telegram device
session or the Tailscale header, according to configuration. The CSP forbids external assets.
"""

from __future__ import annotations

import threading
import time
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware

from .. import log
from ..errors import ConfigError
from . import vendor
from .api import router
from .assets import Assets
from .identity import IdentityMiddleware, Resolver, is_loopback, settings_from

if TYPE_CHECKING:
    from ..app import App
    from ..persons import Person

logger = log.get("tiffin.web")

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; connect-src 'self'; font-src 'self'; base-uri 'self'; "
    "form-action 'self'; frame-ancestors 'none'"
)
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "private, no-store",
}
IMMUTABLE = "public, max-age=31536000, immutable"
REVALIDATE = "private, no-cache"


async def _server_error(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("unhandled web error", extra={"event": "web_error"})
    return JSONResponse(
        {"detail": "Could not read that just now; try again in a minute, or ask the bot"},
        status_code=500,
    )


class SecurityHeaders(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        for key, value in SECURITY_HEADERS.items():
            response.headers.setdefault(key, value)
        # the mount prefix may or may not be present, so match the segment, not the start
        if response.status_code == 200 and "/static/" in request.url.path:
            name = request.url.path.rsplit("/", 1)[-1]
            if name in request.app.state.assets.by_hashed:
                response.headers["Cache-Control"] = IMMUTABLE
        return response


def create_app(app: App, *, dispatch: Any = None, demo_person: Person | None = None) -> FastAPI:
    """The page. `dispatch(person_id, fn)` queues work on that person's own worker - the lane
    their /cook runs on - for the few writes that start a build (the weekend switch); without it
    (tests, a page with no runtime behind it) such a build waits for its scheduled job."""
    config = app.config
    if demo_person is not None:
        from ..demo import DemoApp  # noqa: PLC0415

        if type(app) is not DemoApp or not is_loopback(str(config.get("web.host"))):
            raise ConfigError("demo identity is available only to the loopback demo")
    root_path = str(config.get("web.root_path", "/tiffin")).rstrip("/")
    assets = Assets(root_path)
    ok, detail = vendor.check_plotly()
    if not ok:
        logger.warning("plotly bundle: %s", detail, extra={"event": "vendor_mismatch"})

    web = FastAPI(title="tiffin", docs_url=None, redoc_url=None, openapi_url=None)
    web.state.tiffin = app
    web.state.dispatch = dispatch
    web.state.assets = assets
    web.state.started_at = app.clock.now_utc().timestamp()
    web.include_router(router)
    web.add_exception_handler(Exception, _server_error)

    @web.get("/", response_class=HTMLResponse)
    async def index() -> Response:
        return HTMLResponse(assets.index)

    @web.get("/manifest.webmanifest")
    async def manifest() -> Response:
        return Response(assets.manifest, media_type="application/manifest+json")

    @web.get("/static/{name}")
    async def static(name: str, request: Request) -> Response:
        asset = assets.by_hashed.get(name)
        module = name in assets.module_names
        if asset is None and module:
            asset = assets.by_name[name]
        if asset is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        if module:
            etag = f'"{asset.sha256}"'
            headers = {"Cache-Control": REVALIDATE, "ETag": etag}
            if request.headers.get("if-none-match") == etag:
                return Response(status_code=304, headers=headers)
            return Response(asset.body, media_type=asset.content_type, headers=headers)
        return Response(asset.body, media_type=asset.content_type)

    web.add_middleware(
        IdentityMiddleware,
        resolver=Resolver(app),
        settings=replace(settings_from(config, app.secrets), demo_person=demo_person),
        clock=app.clock,
    )
    web.add_middleware(SecurityHeaders)
    return web


def serve(
    app: App,
    stop: threading.Event,
    *,
    ready: threading.Event | None = None,
    dispatch: Any = None,
) -> None:
    """Run uvicorn until `stop` is set. Refuses any bind that is not loopback."""
    import uvicorn  # noqa: PLC0415 (heavy; only the running process needs it)

    host = str(app.config.get("web.host", "127.0.0.1"))
    if not is_loopback(host):
        raise ConfigError(f"web.host must be loopback, got {host!r}")
    port = app.config.web_port
    config = uvicorn.Config(
        create_app(app, dispatch=dispatch),
        host=host,
        port=port,
        workers=1,
        root_path=str(app.config.get("web.root_path", "/tiffin")).rstrip("/"),
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
        access_log=False,
        log_level="warning",
        # Tiffin's logging is already set up (log.setup): uvicorn's own config runs
        # logging.config.dictConfig, which closes every existing handler - the JSON log among
        # them - so every restart silenced the log until the next UTC day
        log_config=None,
        timeout_graceful_shutdown=5,
    )
    server = uvicorn.Server(config)

    def watch() -> None:
        while not stop.wait(0.5):
            if server.started and ready is not None and not ready.is_set():
                ready.set()
        server.should_exit = True

    watcher = threading.Thread(target=watch, name="web-stop", daemon=True)
    watcher.start()
    logger.info("web on 127.0.0.1:%s", port, extra={"event": "web_start"})
    try:
        server.run()
    finally:
        stop.set()
        watcher.join(timeout=2)
        logger.info("web stopped", extra={"event": "web_stop"})


def wait_until_listening(port: int, timeout_s: float = 10.0) -> bool:
    import socket  # noqa: PLC0415

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.3)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.1)
    return False


__all__ = ["Any", "create_app", "serve", "wait_until_listening"]
