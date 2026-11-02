"""Resolve web identity before routing.

Header mode (`web.identity_mode = header`): Tailscale Serve sets
`Tailscale-User-Login` for tailnet requests and strips any client-supplied copy. Missing -> 403
without a DB read; unknown -> 403 with only sha256[:12] of the login in the log; known ->
`request.state.person`. The dev bypass `X-Dev-Person: <slug>` exists only when the env is dev and
the bind host is loopback, and is ignored in prod.

Telegram mode consumes a one-use link sent to the paired chat and sets a device session cookie.
Only hashes of link and session tokens are stored. The legacy signed-link mode remains available
for installations that already use it.

Residual, by design: any local process can put the header on 127.0.0.1 — the box's own user.
"""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import hashlib
import hmac
import html
import ipaddress
import secrets as pysecrets
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs

from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers
from starlette.requests import cookie_parser
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from .. import log
from ..app import App
from ..clock import DEFAULT_CLOCK, Clock, stamp
from ..config import Config
from ..db import Db
from ..errors import ConfigError
from ..persons import Person
from ..secrets import Secrets

logger = log.get("tiffin.web.identity")

HEADER = "tailscale-user-login"
DEV_HEADER = "x-dev-person"
SESSION_COOKIE = "tiffin_session"
MODES = ("header", "signed_link", "telegram")
EXEMPT_PATHS = frozenset({"/api/health"})
HEADER_REFUSED = (
    "web.identity_mode = header trusts a login header that any client can send unless a "
    "proxy you control sets it and strips the client's copy. Set "
    "web.trusted_header_proxy = true to say that is so, or use identity_mode = telegram"
)
NO_IDENTITY = "no identity"
INVALID_SESSION = "invalid session"
UNKNOWN_IDENTITY = "unknown identity"
#: a signed-in device is signed out after this long without a request, and after SESSION_DAYS
#: whatever it does; "seen" is written at most every SEEN_EVERY_HOURS, so a busy device costs
#: two ledger rows a day
IDLE_DAYS = 14
SESSION_DAYS = 90
SEEN_EVERY_HOURS = 12
MAX_FORM_BYTES = 2048


def login_digest(login: str) -> str:
    """The only form of a login that may reach a log line."""
    return hashlib.sha256(login.strip().lower().encode("utf-8")).hexdigest()[:12]


def is_loopback(host: str) -> bool:
    if host.strip().lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip()).is_loopback
    except ValueError:
        return False


def route_path(scope: Scope) -> str:
    """The request path without the mount prefix, however many times it arrived.

    uvicorn prepends `root_path` to every received path, so a proxy that does NOT strip the
    prefix yields `/tiffin/tiffin/api/x`. Starlette removes one copy; this removes the rest, so
    routing works whether Serve strips the mount point or not (S10 records which it is).
    """
    path: str = scope.get("path", "")
    root: str = scope.get("root_path", "").rstrip("/")
    if not root:
        return path or "/"
    while path == root or path.startswith(root + "/"):
        path = path[len(root) :]
    return path or "/"


def collapse_prefix(scope: Scope) -> None:
    """Rewrite scope['path'] to exactly one root_path prefix so Starlette routes it."""
    root: str = scope.get("root_path", "").rstrip("/")
    if root:
        scope["path"] = root + route_path(scope)


# -- tokens (signed-link mode) ----------------------------------------------------------------
def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def make_link_token(key: bytes, person_id: int, expires_at: int) -> str:
    """base64url('<person_id>:<expires_at unix seconds>') '.' hex(hmac_sha256(key, payload))."""
    payload = f"{int(person_id)}:{int(expires_at)}".encode("ascii")
    return f"{_b64(payload)}.{hmac.new(key, payload, hashlib.sha256).hexdigest()}"


def verify_link_token(key: bytes, token: str, *, now: int) -> int | None:
    """The person_id the token names, or None for anything malformed, forged or expired."""
    if len(token) > 128 or not token.isascii():
        return None
    try:
        encoded, signature = token.strip().split(".", 1)
        payload = _unb64(encoded)
        pid_text, exp_text = payload.decode("ascii").split(":", 1)
        person_id, expires_at = int(pid_text), int(exp_text)
        expected = hmac.new(key, payload, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature.lower()):
            return None
    except ValueError, TypeError, UnicodeDecodeError, binascii.Error:
        return None
    if person_id <= 0 or expires_at <= now:
        return None
    return person_id


def link_for(app: App, slug: str, days: int) -> str:
    """The URL `tiffin admin link <slug> --days N` prints to the console. Never logged."""
    key = app.secrets.get("web_hmac_key").encode("utf-8")
    db = Db.open(app.config.paths.db, clock=app.clock, migrate_first=False)
    try:
        person = app.registry(db).require(slug)
    finally:
        db.close()
    expires_at = int(app.clock.now_utc().timestamp()) + int(days) * 86400
    origin = str(app.config.get("web.origin")).rstrip("/")
    root = str(app.config.get("web.root_path", "/tiffin")).rstrip("/")
    return f"{origin}{root}/?k={make_link_token(key, person.id, expires_at)}"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def issue_login_link(app: App, db: Db, person: Person) -> str:
    """Create a five-minute, one-use link for the person identified by Telegram."""
    token = pysecrets.token_urlsafe(32)
    expires = app.clock.now_utc() + dt.timedelta(minutes=5)
    db.insert(
        "web_login_links",
        person_id=person.id,
        token_hash=_hash(token),
        expires_at=expires.strftime("%Y-%m-%d %H:%M:%S"),
    )
    origin = str(app.config.get("web.origin")).rstrip("/")
    root = str(app.config.get("web.root_path", "")).rstrip("/")
    return f"{origin}{root}/?k={token}"


def consume_login_link(
    app: App, token: str, expected_person_id: int | None = None
) -> tuple[Person, str] | None:
    if len(token) > 128 or not token.isascii():
        return None
    with Db.open(app.config.paths.db, clock=app.clock, migrate_first=False) as db, db.tx():
        row = db.one(
            "SELECT l.id, l.person_id FROM web_login_links l "
            "LEFT JOIN web_link_uses u ON u.link_id = l.id "
            "WHERE l.token_hash = ? AND l.expires_at > ? AND u.id IS NULL",
            (_hash(token), app.clock.utc_stamp()),
        )
        if row is None:
            return None
        person = app.registry(db).by_id(int(row["person_id"]))
        if (
            person is None
            or person.erased
            or (expected_person_id is not None and person.id != expected_person_id)
        ):
            return None
        db.insert("web_link_uses", link_id=row["id"], used_at=app.clock.utc_stamp())
        session = pysecrets.token_urlsafe(32)
        expires = app.clock.now_utc() + dt.timedelta(days=SESSION_DAYS)
        db.insert(
            "web_devices",
            person_id=person.id,
            token_hash=_hash(session),
            expires_at=expires.strftime("%Y-%m-%d %H:%M:%S"),
        )
        return person, session


def link_is_live(app: App, token: str) -> bool:
    """Whether a sign-in link could be used now. Reads only: a preview must not spend it."""
    if len(token) > 128 or not token.isascii():
        return False
    with Db.open(app.config.paths.db, clock=app.clock, migrate_first=False) as db:
        return (
            db.one(
                "SELECT l.id FROM web_login_links l "
                "LEFT JOIN web_link_uses u ON u.link_id = l.id "
                "WHERE l.token_hash = ? AND l.expires_at > ? AND u.id IS NULL",
                (_hash(token), app.clock.utc_stamp()),
            )
            is not None
        )


def device_for(app: App, token: str, *, touch: bool = False) -> tuple[Person, int] | None:
    """The person and device a session cookie names, or None when it is unknown, signed out,
    past its 90 days, or idle for 14. `touch` records that the device was just used."""
    if len(token) > 128 or not token.isascii():
        return None
    now = app.clock.now_utc()
    with Db.open(app.config.paths.db, clock=app.clock, migrate_first=False) as db:
        row = db.one(
            "SELECT d.id, d.person_id, COALESCE("
            "(SELECT MAX(s.created_at) FROM web_device_seen s WHERE s.device_id = d.id), "
            "d.created_at) AS last_seen "
            "FROM web_devices d "
            "LEFT JOIN web_device_events e ON e.device_id = d.id "
            "WHERE d.token_hash = ? AND d.expires_at > ? AND e.id IS NULL",
            (_hash(token), app.clock.utc_stamp()),
        )
        if row is None or str(row["last_seen"]) <= stamp(now - dt.timedelta(days=IDLE_DAYS)):
            return None
        person = app.registry(db).by_id(int(row["person_id"]))
        if person is None or person.erased:
            return None
        if touch and str(row["last_seen"]) <= stamp(now - dt.timedelta(hours=SEEN_EVERY_HOURS)):
            db.insert("web_device_seen", device_id=int(row["id"]))
        return person, int(row["id"])


# -- settings and lookups ----------------------------------------------------------------------
@dataclass(frozen=True)
class IdentitySettings:
    mode: str
    dev_bypass: bool
    root_path: str
    hmac_key: bytes | None = None
    secure_cookies: bool = True
    demo_person: Person | None = None
    origin: str = ""
    signed_out_redirect: str = ""

    @property
    def cookie_path(self) -> str:
        return self.root_path or "/"


def settings_from(config: Config, secrets: Secrets) -> IdentitySettings:
    mode = str(config.get("web.identity_mode", "telegram"))
    if mode not in MODES:
        raise ConfigError(f"web.identity_mode must be one of {MODES}, got {mode!r}")
    if mode == "header" and config.get("web.trusted_header_proxy", False) is not True:
        raise ConfigError(HEADER_REFUSED)
    host = str(config.get("web.host", "127.0.0.1"))
    key = secrets.get("web_hmac_key").encode("utf-8") if mode == "signed_link" else None
    return IdentitySettings(
        mode=mode,
        dev_bypass=config.env == "dev" and is_loopback(host),
        root_path=str(config.get("web.root_path", "/tiffin")).rstrip("/"),
        hmac_key=key,
        origin=str(config.get("web.origin", "")).rstrip("/"),
        signed_out_redirect=str(config.get("web.signed_out_redirect", "")),
        secure_cookies=str(config.get("web.origin", "")).startswith("https://")
        and (config.is_prod or bool(config.get("web.secure_cookies", True))),
    )


class Resolver:
    """Person lookups for the middleware: one short-lived Db per call, run in a worker thread."""

    def __init__(self, app: App):
        self.app = app

    def _open(self) -> Db:
        return Db.open(self.app.config.paths.db, clock=self.app.clock, migrate_first=False)

    def by_login(self, login: str) -> Person | None:
        db = self._open()
        try:
            return self.app.registry(db).by_tailscale_login(login)
        finally:
            db.close()

    def by_slug(self, slug: str) -> Person | None:
        db = self._open()
        try:
            person = self.app.registry(db).by_slug(slug)
        finally:
            db.close()
        return None if person is None or person.erased else person

    def by_id(self, person_id: int) -> Person | None:
        db = self._open()
        try:
            person = self.app.registry(db).by_id(person_id)
        finally:
            db.close()
        return None if person is None or person.erased else person


Lookup = Callable[[str], Person | None]


class IdentityMiddleware:
    """Pure ASGI: resolves the person before routing; everything except /api/health."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        resolver: Resolver,
        settings: IdentitySettings,
        clock: Clock = DEFAULT_CLOCK,
    ):
        self.app = app
        self.resolver = resolver
        self.settings = settings
        self.clock = clock

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        collapse_prefix(scope)
        path = route_path(scope)
        if path in EXEMPT_PATHS:
            await self.app(scope, receive, send)
            return
        if self.settings.demo_person is not None:
            scope.setdefault("state", {})["person"] = self.settings.demo_person
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        method = scope.get("method", "GET")
        if self.settings.mode == "telegram" and path == "/":
            if method == "POST":
                await self._sign_in(scope, receive, send)
                return
            tokens = parse_qs(scope.get("query_string", b"").decode("latin-1")).get("k")
            if tokens and method in ("GET", "HEAD"):
                await self._offer_link(scope, receive, send, tokens[0])
                return
        elif self.settings.mode == "signed_link" and path == "/":
            tokens = parse_qs(scope.get("query_string", b"").decode("latin-1")).get("k")
            if tokens:
                await self._enter_link(scope, receive, send, tokens[0])
                return
        person, error = await self._resolve(headers)
        if person is None:
            target = self.settings.signed_out_redirect
            if (
                target
                and self.settings.mode == "telegram"
                and path == "/"
                and method in ("GET", "HEAD")
                and error in (NO_IDENTITY, INVALID_SESSION)
            ):
                await RedirectResponse(target, status_code=303)(scope, receive, send)
                return
            await _reject(scope, receive, send, error, mode=self.settings.mode)
            return
        scope.setdefault("state", {})["person"] = person
        await self.app(scope, receive, send)

    async def _resolve(self, headers: Headers) -> tuple[Person | None, str]:
        s = self.settings
        if s.mode == "header":
            login = headers.get(HEADER)
            if login:
                person = await run_in_threadpool(self.resolver.by_login, login)
                if person is None:
                    logger.warning(
                        "unknown tailscale login sha256=%s",
                        login_digest(login),
                        extra={"event": "web_unknown_login"},
                    )
                    return None, UNKNOWN_IDENTITY
                return person, ""
        elif s.mode == "telegram":
            token = cookie_parser(headers.get("cookie", "")).get(SESSION_COOKIE)
            if token:
                found = await run_in_threadpool(
                    lambda: device_for(self.resolver.app, token, touch=True)
                )
                if found is not None:
                    return found[0], ""
                return None, INVALID_SESSION
        else:
            token = cookie_parser(headers.get("cookie", "")).get(SESSION_COOKIE)
            if token:
                person_id = self._person_id_of(token)
                if person_id is None:
                    return None, "invalid session"
                person = await run_in_threadpool(self.resolver.by_id, person_id)
                return (person, "") if person is not None else (None, UNKNOWN_IDENTITY)
        slug = headers.get(DEV_HEADER)
        if s.dev_bypass and slug:
            person = await run_in_threadpool(self.resolver.by_slug, slug)
            return (person, "") if person is not None else (None, UNKNOWN_IDENTITY)
        return None, NO_IDENTITY

    def _person_id_of(self, token: str) -> int | None:
        key = self.settings.hmac_key
        if key is None:
            return None
        return verify_link_token(key, token, now=int(self.clock.now_utc().timestamp()))

    async def _offer_link(self, scope: Scope, receive: Receive, send: Send, token: str) -> None:
        """A page whose button sends the token back. Opening the link spends nothing, so a
        chat app's link preview cannot use up the one use before the person does."""
        live = await run_in_threadpool(link_is_live, self.resolver.app, token)
        if not live:
            await _reject(scope, receive, send, "bad link", mode=self.settings.mode)
            return
        action = html.escape(self.settings.root_path + "/", quote=True)
        page = _CONFIRM_PAGE.format(action=action, token=html.escape(token, quote=True))
        await HTMLResponse(page, headers={"X-Robots-Tag": "noindex"})(scope, receive, send)

    async def _sign_in(self, scope: Scope, receive: Receive, send: Send) -> None:
        headers = Headers(scope=scope)
        origin = headers.get("origin")
        if (origin and origin.rstrip("/") != self.settings.origin) or (
            headers.get("sec-fetch-site", "") == "cross-site"
        ):
            await _reject(scope, receive, send, "bad link", mode=self.settings.mode)
            return
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if len(body) > MAX_FORM_BYTES:
                await _reject(scope, receive, send, "bad link", mode=self.settings.mode)
                return
            if not message.get("more_body", False):
                break
        tokens = parse_qs(body.decode("latin-1")).get("k")
        if not tokens:
            await _reject(scope, receive, send, NO_IDENTITY, mode=self.settings.mode)
            return
        cookies = cookie_parser(headers.get("cookie", ""))
        existing = cookies.get(SESSION_COOKIE)
        current = (
            await run_in_threadpool(device_for, self.resolver.app, existing) if existing else None
        )
        found = await run_in_threadpool(
            consume_login_link, self.resolver.app, tokens[0], current[0].id if current else None
        )
        if found is None:
            await _reject(scope, receive, send, "bad link", mode=self.settings.mode)
            return
        _, session = found
        response: Response = RedirectResponse(self.settings.root_path + "/", status_code=303)
        response.set_cookie(
            SESSION_COOKIE,
            session,
            max_age=SESSION_DAYS * 86400,
            path=self.settings.cookie_path,
            secure=self.settings.secure_cookies,
            httponly=True,
            samesite=SAMESITE,
        )
        await response(scope, receive, send)

    async def _enter_link(self, scope: Scope, receive: Receive, send: Send, token: str) -> None:
        """Signed-link mode only (kept for installations that already use it)."""
        person_id = self._person_id_of(token)
        person = (
            await run_in_threadpool(self.resolver.by_id, person_id)
            if person_id is not None
            else None
        )
        if person is None:
            await _reject(scope, receive, send, "bad link", mode=self.settings.mode)
            return
        now = int(self.clock.now_utc().timestamp())
        expires_at = int(token.split(".", 1)[0] and _link_expiry(token))
        response: Response = RedirectResponse(self.settings.root_path + "/", status_code=303)
        response.set_cookie(
            SESSION_COOKIE,
            token,
            max_age=max(1, expires_at - now),
            path=self.settings.cookie_path,
            secure=self.settings.secure_cookies,
            httponly=True,
            samesite=SAMESITE,
        )
        await response(scope, receive, send)


def _link_expiry(token: str) -> int:
    payload = _unb64(token.split(".", 1)[0]).decode("ascii")
    return int(payload.split(":", 1)[1])


# Browser navigation and page fetches should give the same actionable refusal.
_REFUSED_SAYS = {
    UNKNOWN_IDENTITY: (
        "This identity is not linked. Send <code>/web</code> to your bot for a new sign-in link."
    ),
    NO_IDENTITY: ("Send <code>/web</code> to your bot for a sign-in link."),
}
_HEADER_REFUSED_SAYS = {
    UNKNOWN_IDENTITY: (
        "This Tailscale login is not linked. The owner can run "
        "<code>tiffin link-tailscale &lt;person&gt; &lt;login&gt;</code>. "
        "The bot in Telegram still has <code>/menu</code> and <code>/recipe</code>."
    ),
    NO_IDENTITY: (
        "Open the page through Tailscale Serve on your tailnet to sign in. "
        "The bot in Telegram has everything: <code>/menu</code> and <code>/recipe</code>."
    ),
}
_REFUSED_PAGE = (
    '<!doctype html><html lang="en-GB"><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width, initial-scale=1">'
    "<title>Not linked yet</title></head>"
    '<body style="font:16px/1.5 system-ui,sans-serif;margin:2rem;max-width:36rem">'
    "<h1>Not linked yet</h1><p>{says}</p></body></html>"
)


_CONFIRM_PAGE = (
    '<!doctype html><html lang="en-GB"><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width, initial-scale=1">'
    '<meta name="robots" content="noindex"><meta name="referrer" content="no-referrer">'
    "<title>Sign in</title></head>"
    '<body style="font:16px/1.5 system-ui,sans-serif;margin:2rem;max-width:36rem">'
    "<h1>Sign in to Tiffin</h1>"
    "<p>This link signs this phone or computer in as you. It works once and only for a few "
    "minutes.</p>"
    '<form method="post" action="{action}">'
    '<input type="hidden" name="k" value="{token}">'
    '<button type="submit" style="font:inherit;padding:.6rem 1.2rem">Sign in on this device'
    "</button></form></body></html>"
)

#: Lax, not Strict: a link opened from a chat app is a cross-site navigation, and a Strict
#: cookie is withheld from it, so the page would greet a signed-in person as a stranger. Lax
#: still withholds the cookie from cross-site POSTs; writes also need the CSRF double-submit.
SAMESITE = "lax"


async def _reject(
    scope: Scope, receive: Receive, send: Send, error: str, *, mode: str = "header"
) -> None:
    messages = _HEADER_REFUSED_SAYS if mode == "header" else _REFUSED_SAYS
    says = messages.get(error, "This page could not tell who you are.")
    if route_path(scope).startswith("/api/"):
        await JSONResponse({"error": error, "detail": says}, status_code=403)(scope, receive, send)
        return
    await HTMLResponse(_REFUSED_PAGE.format(says=says), status_code=403)(scope, receive, send)


def person_of(state: Any) -> Person:
    """The resolved person, from request.state. Raises if the middleware was bypassed."""
    person = getattr(state, "person", None)
    if not isinstance(person, Person):
        raise LookupError("no identity on request.state (identity middleware missing)")
    return person
