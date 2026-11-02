"""The JSON API. Identity comes from the middleware; nothing here reads a person from input.

Writes pass a fence first (CSRF token equal to the cookie, same-origin, no unknown fields), and
each accepted write is one domain row plus one web_events row.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import secrets as pysecrets
from collections.abc import Mapping
from typing import Any

from fastapi import HTTPException, Request
from starlette.concurrency import run_in_threadpool

from ..clock import parse_day
from ..db import Db
from ..persons import Person
from .identity import person_of

CSRF_COOKIE = "tiffin_csrf"
MAX_BODY = 8192
#: SQLite stores a signed 64-bit INTEGER; anything past it overflows on the way into a query
MAX_ROW_ID = 2**63 - 1
#: how far back the page may record a day, the same week the bot's dated buttons allow
CONFIRM_BACK_DAYS = 7


def _person(request: Request) -> Person:
    return person_of(request.state)


def _db(request: Request) -> Db:
    app = request.app.state.tiffin
    return Db.open(app.config.paths.db, clock=app.clock, migrate_first=False)


async def _read(request: Request, fn, *args: Any) -> Any:
    """Run one query on a thread with its own connection (SQLite is per-thread)."""

    def call() -> Any:
        db = _db(request)
        try:
            return fn(db, *args)
        finally:
            db.close()

    return await run_in_threadpool(call)


def _today(request: Request) -> dt.date:
    return request.app.state.tiffin.clock.today_local()


def _day_arg(value: str | None, fallback: dt.date) -> dt.date:
    if not value:
        return fallback
    try:
        return parse_day(value)
    except ValueError:
        raise HTTPException(status_code=400, detail="bad date") from None


def _day_arg_opt(value: str | None) -> dt.date | None:
    """Like `_day_arg`, but an absent value means None - "no filter" - rather than a fallback
    date. `?from=` absent on the Batch tab has to mean the whole segment, not today: `_day_arg`'s
    contract (a caller always gets a real date back) is right everywhere else it is used, so
    this is its own small function rather than a change that would ripple through every one of
    them.
    """
    if not value:
        return None
    try:
        return parse_day(value)
    except ValueError:
        raise HTTPException(status_code=400, detail="bad date") from None


def _number(value: Any, field: str) -> float:
    """Parse a finite figure; domain limits belong to the caller."""
    if isinstance(value, bool):
        raise HTTPException(status_code=400, detail=f"{field} must be a number")
    try:
        number = float(value)
    except OverflowError, TypeError, ValueError:
        raise HTTPException(status_code=400, detail=f"{field} must be a number") from None
    if not math.isfinite(number):
        raise HTTPException(status_code=400, detail=f"{field} must be a finite number")
    return number


def _grams(value: Any, field: str) -> float:
    """A positive kitchen weight, capped at 20 kg to catch unit mistakes."""
    if isinstance(value, bool):
        raise HTTPException(status_code=400, detail=f"{field} must be a number")
    try:
        grams = float(value)
    except OverflowError, TypeError, ValueError:
        raise HTTPException(status_code=400, detail=f"{field} must be a number") from None
    if not (0 < grams <= 20000):
        raise HTTPException(status_code=400, detail=f"{field} must be between 0 and 20000 g")
    return grams


def _row_id(value: Any, what: str) -> int:
    """A positive row ID within SQLite's signed 64-bit integer range."""
    if isinstance(value, bool | float):
        raise HTTPException(status_code=400, detail=f"bad {what}")
    try:
        number = int(value)
    except KeyError, OverflowError, TypeError, ValueError:
        raise HTTPException(status_code=400, detail=f"bad {what}") from None
    if not 0 < number <= MAX_ROW_ID:
        raise HTTPException(status_code=400, detail=f"bad {what}")
    return number


def check_fence(request: Request, body: Mapping[str, Any], allowed: set[str]) -> None:
    """CSRF token, same-origin, and no unknown fields; all before any DB touch."""
    token = request.headers.get("x-csrf-token")
    cookie = request.cookies.get(CSRF_COOKIE)
    if (
        not token
        or not cookie
        or not token.isascii()
        or not cookie.isascii()
        or not pysecrets.compare_digest(token, cookie)
    ):
        # "bad csrf token" named the internal check, not the fix: a phone that dropped its
        # cookie needs told to reload, not handed the name of a security mechanism it cannot
        # act on - the tick still un-ticks itself, but the toast now says what to do about it
        raise HTTPException(
            status_code=403, detail="That did not save. Reload the page and try again."
        )
    origin = request.headers.get("origin")
    expected = str(request.app.state.tiffin.config.get("web.origin", ""))
    if origin and expected and origin.rstrip("/") != expected.rstrip("/"):
        raise HTTPException(status_code=403, detail="bad origin")
    if request.headers.get("sec-fetch-site", "") == "cross-site":
        raise HTTPException(status_code=403, detail="bad origin")
    unknown = sorted(set(body) - allowed)
    if unknown:
        raise HTTPException(status_code=400, detail=f"unknown fields: {', '.join(unknown)}")


async def _body(request: Request) -> dict[str, Any]:
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY:
            raise HTTPException(status_code=413, detail="body too large")
        chunks.append(chunk)
    raw = b"".join(chunks)
    try:
        data = json.loads(raw or b"{}")
    except ValueError:
        raise HTTPException(status_code=400, detail="bad json") from None
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="body must be an object")
    return data


def _web_event(db: Db, person_id: int, kind: str, payload: dict[str, Any]) -> None:
    db.insert(
        "web_events",
        person_id=person_id,
        kind=kind,
        payload_json=json.dumps(payload),
        at=db.clock.utc_stamp(),
    )


def _words(value: Any, field: str, *, longest: int | None = None) -> list[str]:
    """Parse food words from a string or a list of strings.

    Entries to remove may be longer than the entry limit, so they can always be
    removed even if they were stored before that limit was introduced."""
    if value is None:
        return []
    if isinstance(value, str):
        value = value.split(",")
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise HTTPException(status_code=400, detail=f"{field} is a food, or a list of foods")
    words = [" ".join(v.split()) for v in value if v.strip()]
    if len(words) > 20:
        raise HTTPException(
            status_code=400, detail="That is more than 20 foods at once: send them in two goes."
        )
    if longest is not None and any(len(w) > longest for w in words):
        raise HTTPException(
            status_code=400, detail=f"That is longer than a food name: {longest} letters at most."
        )
    return words


def _slot_arg(value: Any) -> str:
    from ..food import menu as menu_mod  # noqa: PLC0415 (food imports the db layer)

    if not isinstance(value, str) or value not in menu_mod.SLOT_ORDER:
        raise HTTPException(
            status_code=400, detail="slot is breakfast, lunch, dinner, snack or pre_bed"
        )
    return value
