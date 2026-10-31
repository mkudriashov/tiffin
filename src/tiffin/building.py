"""A menu build in flight, per person: what the page shows while one runs, and why a second one
is not started on top of it.

A build is minutes of model calls on the person's own build lane (`App.run_build`). The bot says
so in words when it starts; the page has no message to read, so it reads this instead and says
"Building a new menu - usually 2-4 minutes" until the build is done, then shows the new menu.
Kept in `bot_state` (state, not a ledger): the run itself is in `run_ledger` as every job is.

One mark per person covers every build asked for - a whole menu, or new dishes for one meal - so
each mark says which it is, and only its own build clears it. Each scheduled build keeps a mark
of its own beside it, shown when nothing asked for is running.
"""

from __future__ import annotations

import html
import json
import re
import uuid
from collections.abc import Callable
from typing import Any

from . import log
from .clock import Clock, isoday, parse_stamp
from .db import Db

logger = log.get("tiffin.building")

TAKES = "usually 2-4 minutes"
#: a mark older than this is a build that died with the process: forgotten, never shown
STALE_MIN = 15
MENU, DISHES = "menu", "dishes"
#: a week of choices (food/choices.py): a model call for every meal, so it outlasts STALE_MIN, and
#: its mark is kept for longer
CHOICES = "choices"
CHOICES_STALE_MIN = 45


def _key(person_id: int, scheduled: str | None = None) -> str:
    # a scheduled build has its own key: sharing the one key, it went unmarked whenever new dishes
    # were being written as it started, and once they were in, the page let a second build for
    # the same days in beside it. One key per scheduled build (its id): his own menu and the
    # shared weekend can run at once, and the first to end took the other's mark with it
    #
    return f"building:{person_id}" if scheduled is None else f"building:{person_id}:{scheduled}"


def _said_key(person_id: int) -> str:
    return f"built:{person_id}"


def _parsed(raw: str | None) -> dict[str, Any]:
    try:
        got = json.loads(raw or "")
    except ValueError:
        return {}
    return got if isinstance(got, dict) else {}


def mark(
    db: Db,
    person_id: int,
    what: str,
    *,
    clock: Clock,
    kind: str = MENU,
    day: Any = None,
    slot: str | None = None,
    menu: int | None = None,
    asked: bool = True,
) -> str:
    """Say a build has started. Returns its id: only `done` with that id clears this mark.

    `menu` is the menu whose days it rebuilds, when that is known before it runs (Change, "Just
    a new menu", the page's Build). `asked` is False for a scheduled build: the scheduled one
    waits for a build a person asked for, never for another scheduled one."""
    token = uuid.uuid4().hex[:12]
    got: dict[str, Any] = {
        "id": token,
        "since": clock.utc_stamp(),
        "what": what[:80],
        "kind": kind,
        "asked": asked,
    }
    if day is not None:
        got["day"] = isoday(day)
    if slot is not None:
        got["slot"] = slot
    if menu is not None:
        got["menu"] = menu
    db.state_set(_key(person_id, None if asked else token), json.dumps(got))
    return token


def done(db: Db, person_id: int, token: str | None = None) -> None:
    """Clear only the completed build's mark, leaving a newer build in progress."""
    if token is None:
        db.execute("DELETE FROM bot_state WHERE k = ?", (_key(person_id),))
        return
    raw = db.state_get(_key(person_id))
    if raw is not None and _parsed(raw).get("id") == token:
        db.execute("DELETE FROM bot_state WHERE k = ? AND v = ?", (_key(person_id), raw))
    db.execute("DELETE FROM bot_state WHERE k = ?", (_key(person_id, token),))


def _live(raw: str | None, clock: Clock) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        got = json.loads(raw)
        since = parse_stamp(str(got["since"]))
    except ValueError, KeyError, TypeError:
        return None
    minutes = int((clock.now_utc() - since).total_seconds() // 60)
    if minutes >= (CHOICES_STALE_MIN if got.get("kind") == CHOICES else STALE_MIN):
        return None
    return {
        "id": str(got.get("id") or ""),
        "since": str(got["since"]),
        "what": str(got.get("what") or ""),
        "minutes": minutes,
        "kind": str(got.get("kind") or MENU),
        "day": got.get("day"),
        "slot": got.get("slot"),
        "menu": got.get("menu"),
        "asked": bool(got.get("asked", True)),
    }


def current(db: Db, person_id: int, *, clock: Clock) -> dict[str, Any] | None:
    """The build in flight for this person, {id, since, what, minutes, kind, day, slot, menu,
    asked}, or None. One asked for comes first; a scheduled one shows when nothing asked for
    runs, the longest-running first."""
    now = _live(db.state_get(_key(person_id)), clock)
    if now is not None:
        return now
    scheduled = [
        _live(row["v"], clock)
        for row in db.all("SELECT v FROM bot_state WHERE k LIKE ?", (_key(person_id, "%"),))
    ]
    return min((s for s in scheduled if s is not None), key=lambda s: s["since"], default=None)


def busy(now: dict[str, Any], *, page: bool = False) -> str:
    """The answer to a build asked for while another runs, naming the one that is running."""
    if now.get("kind") == CHOICES:
        return (
            "Your choices for the week are being written (about 10 minutes). They show on the "
            "page's Menu, under Choose, when they are ready."
        )
    if now.get("kind") == DISHES:
        return (
            f"Already asking for {now['what']} (usually 1-2 minutes). Ask again once they are in."
        )
    if not now.get("asked", True):
        # a scheduled build is not the one just asked for, and the ask is not kept: "it comes
        # here" promised their own menu, and the weekend came
        return f"Building {now['what']} now ({TAKES}). Ask again once it is in."
    if page:
        return (
            f"A new menu is already being built ({TAKES}). It shows here, and comes to "
            "Telegram, when it is ready."
        )
    return f"Already working on a menu ({TAKES}). It comes here when it is ready."


def say(db: Db, person_id: int, token: str, said: str) -> None:
    """How a menu build ended, kept for the page: a failure was said only in Telegram, and the
    words, since the page shows text, not the bot's HTML; "" when the menu itself was the
    answer."""
    plain = html.unescape(re.sub(r"<[^>]+>", "", said)).strip()
    db.state_set(_said_key(person_id), json.dumps({"id": token, "said": plain[:400]}))


def told(db: Db, person_id: int) -> dict[str, str] | None:
    """{id, said} for the last menu build that ended, or None."""
    got = _parsed(db.state_get(_said_key(person_id)))
    if not got.get("id"):
        return None
    return {"id": str(got["id"]), "said": str(got.get("said") or "")}


def interrupted(now: dict[str, Any]) -> str:
    """What whoever asked for a build is told when a restart killed it."""
    if now.get("kind") == DISHES:
        return (
            f"Tiffin restarted while it was asking for {now['what']}, so they did not come. "
            "Ask for them again from that meal's Swap, or /swap shows the dishes you have had."
        )
    return (
        "Tiffin restarted while it was building your menu, so that one did not finish. "
        "/menu shows what is planned; send /cook to ask again."
    )


def forget_all(db: Db, *, clock: Clock) -> list[tuple[int, dict[str, Any]]]:
    """Clear every mark. No build survives a restart: the build lane is a daemon thread, so the
    one running died with the old process, and its mark kept /cook and the page saying a menu
    was coming for up to STALE_MIN. Returns (person_id, mark) for each one
    still current, so whoever was waiting can be told."""
    out: list[tuple[int, dict[str, Any]]] = []
    for row in db.all("SELECT k, v FROM bot_state WHERE k LIKE 'building:%'"):
        key = str(row["k"])
        try:
            person_id = int(key.split(":")[1])
        except ValueError:
            person_id = 0
        now = _live(row["v"], clock) if person_id else None
        if now is not None:
            out.append((person_id, now))
        db.execute("DELETE FROM bot_state WHERE k = ?", (key,))
    return out


def guarded(
    app: Any,
    person: Any,
    token: str,
    fn: Callable[[Db], object],
    *,
    broke: str | None,
    on_crash: Callable[[Db], object] | None = None,
) -> Callable[[Db], None]:
    """Run a build, clear its mark and report failures to the person."""

    def run(db: Db) -> None:
        with log.context(person_id=person.id):
            try:
                fn(db)
            except Exception as e:
                logger.error(
                    "build failed type=%s", type(e).__name__, extra={"event": "build_error"}
                )
                detail = f"person={person.id} {type(e).__name__}"
                if on_crash is not None:
                    try:
                        on_crash(db)
                    except Exception:
                        logger.exception("build cleanup failed", extra={"event": "build_error"})
                try:
                    if broke:
                        app.outbox(db).reply(person, broke)
                except Exception:
                    logger.exception("could not tell the person", extra={"event": "build_error"})
                try:
                    app.outbox(db).alert(app.registry(db).admin(), "build_error", detail)
                except Exception:
                    logger.exception("could not alert the admin", extra={"event": "build_error"})
            finally:
                done(db, person.id, token)

    return run


__all__ = [
    "CHOICES",
    "CHOICES_STALE_MIN",
    "DISHES",
    "MENU",
    "STALE_MIN",
    "TAKES",
    "busy",
    "current",
    "done",
    "forget_all",
    "guarded",
    "interrupted",
    "mark",
    "say",
    "told",
]
