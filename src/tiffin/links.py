"""Linking two people: a code one mints and the other enters.

Nothing is shared between two people until they are linked. The link is a ledger: a `links` row
per code, and a `link_events` row for each thing that happens to it (offered, accepted,
declined, unlinked, expired). A link is active while its newest event is `accepted`.

Rules, all enforced here and not in the screens:

- A code is 8 characters from `persons.CODE_ALPHABET`, valid for 48 hours, accepted once, and
  stored as a hash. Wrong codes are counted; five misses in an hour stop further tries.
- Two people at most, so at most one link is active at a time. Minting or entering a code while
  linked is refused, and a person cannot link to themselves.
- Either person can unlink at once, without the other's consent. One event row is written; the
  other person is told in neutral words by the caller.
- Linking is not consent. What a linked person sees of the other (plates and macros of dishes
  cooked together) still needs both people's `shared_meals` consent (`food/shared.py`).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import secrets as _secrets
import sqlite3
from dataclasses import dataclass
from typing import Any

from .clock import parse_stamp, stamp
from .db import Db
from .persons import CODE_ALPHABET, normalise_code

CODE_TTL_HOURS = 48
CODE_LENGTH = 8
#: wrong codes allowed per person in WINDOW_MINUTES, then no more tries until it passes
MAX_MISSES = 5
WINDOW_MINUTES = 60
#: codes one person may make in an hour; a code is a message to someone, not a free action
MAX_MINTS = 5

METHODS = ("telegram", "web", "cli", "migrated", "system")


class LinkError(Exception):
    """A refusal in words a person can read as they are."""


@dataclass(frozen=True)
class State:
    """Where one person stands. `kind` is none, offered (their code is out) or linked."""

    kind: str
    link_id: int | None = None
    partner_id: int | None = None
    expires_at: str | None = None
    #: the other person is set up to be linked to: they have joined (a chat or a page login)
    partner_joined: bool = False


@dataclass(frozen=True)
class Unlinked:
    link_id: int
    other_id: int
    #: shopping lists closed, and how many of them were copied for each person
    closed_lists: int
    copied_lists: int


def code_hash(code: str) -> str:
    return hashlib.sha256(normalise_code(code).encode("ascii", "ignore")).hexdigest()


def pretty(code: str) -> str:
    return f"{code[:4]}-{code[4:]}"


def _latest_event(db: Db, link_id: int) -> sqlite3.Row | None:
    return db.one(
        "SELECT event, person_id, created_at FROM link_events WHERE link_id = ? "
        "ORDER BY id DESC LIMIT 1",
        (link_id,),
    )


def active_link(db: Db) -> sqlite3.Row | None:
    """The link in force, or None. Two people at most, so there is one at most."""
    return db.one(
        "SELECT l.id, l.a_person, l.b_person FROM links l "
        "WHERE (SELECT e.event FROM link_events e WHERE e.link_id = l.id "
        "ORDER BY e.id DESC LIMIT 1) = 'accepted' ORDER BY l.id DESC LIMIT 1"
    )


def active_link_id(db: Db) -> int | None:
    row = active_link(db)
    return None if row is None else int(row["id"])


def is_linked(db: Db) -> bool:
    return active_link(db) is not None


def partner_of(db: Db, person_id: int) -> int | None:
    """The person linked to this one, or None."""
    row = active_link(db)
    if row is None:
        return None
    a, b = int(row["a_person"]), int(row["b_person"])
    if person_id == a:
        return b
    if person_id == b:
        return a
    return None


def is_open(db: Db, link_id: int | None) -> bool:
    """Is what was made under this link still in force? A thing made before links existed
    (`link_id` NULL) counts only while some link is active."""
    if link_id is None:
        return active_link(db) is not None
    row = _latest_event(db, int(link_id))
    return row is not None and str(row["event"]) == "accepted"


def pair_open(db: Db, shared_menu_id: int) -> bool:
    """Is this weekend pair still cooked together? Once its link ends the two menus are ordinary
    ones: nothing of the one is shown to the other."""
    row = db.one("SELECT link_id FROM shared_menus WHERE id = ?", (shared_menu_id,))
    return row is not None and is_open(db, row["link_id"])


def _other_person(db: Db, person_id: int) -> sqlite3.Row | None:
    return db.one(
        "SELECT id FROM persons WHERE id <> ? AND erased_at IS NULL ORDER BY id LIMIT 1",
        (person_id,),
    )


def _joined(db: Db, person_id: int) -> bool:
    return (
        db.one("SELECT 1 FROM telegram_identities WHERE person_id = ?", (person_id,)) is not None
        or db.one("SELECT 1 FROM tailscale_identities WHERE person_id = ?", (person_id,))
        is not None
    )


def _pending_for(db: Db, person_id: int) -> sqlite3.Row | None:
    """The newest unexpired code this person minted, still waiting to be entered."""
    now = db.clock.utc_stamp()
    for row in db.all(
        "SELECT id, expires_at FROM links WHERE a_person = ? ORDER BY id DESC LIMIT 5",
        (person_id,),
    ):
        latest = _latest_event(db, int(row["id"]))
        if latest is not None and str(latest["event"]) == "offered":
            return row if str(row["expires_at"]) > now else None
        # the newest code was dealt with; an older one cannot still be waiting
        return None
    return None


def state(db: Db, person_id: int) -> State:
    partner = partner_of(db, person_id)
    if partner is not None:
        return State("linked", active_link_id(db), partner, None, True)
    other = _other_person(db, person_id)
    joined = other is not None and _joined(db, int(other["id"]))
    pending = _pending_for(db, person_id)
    if pending is not None:
        return State(
            "offered",
            int(pending["id"]),
            None if other is None else int(other["id"]),
            str(pending["expires_at"]),
            joined,
        )
    return State("none", None, None if other is None else int(other["id"]), None, joined)


def _event(
    db: Db, link_id: int, person_id: int | None, event: str, method: str, **kw: Any
) -> None:
    if method not in METHODS:
        raise ValueError(f"unknown method {method!r}")
    db.insert(
        "link_events", link_id=link_id, person_id=person_id, event=event, method=method, **kw
    )


def _expire_stale(db: Db) -> None:
    """Record, once, that a code ran out. Reading never needs this; the history does."""
    now = db.clock.utc_stamp()
    for row in db.all("SELECT id, expires_at FROM links WHERE expires_at <= ?", (now,)):
        latest = _latest_event(db, int(row["id"]))
        if latest is not None and str(latest["event"]) == "offered":
            _event(db, int(row["id"]), None, "expired", "system")


def _recent(db: Db, sql: str, person_id: int, minutes: int) -> int:
    since = stamp(db.clock.now_utc() - dt.timedelta(minutes=minutes))
    return int(db.scalar(sql, (person_id, since)) or 0)


def mint(db: Db, person_id: int, method: str) -> str:
    """Make a code for the other person to enter. Returns it once, as 'ABCD-EFGH'."""
    with db.tx():
        _expire_stale(db)
        if is_linked(db):
            raise LinkError(
                "You are linked already. Unlink first if you want to link to someone else."
            )
        other = _other_person(db, person_id)
        if other is None or not _joined(db, int(other["id"])):
            raise LinkError(
                "Your partner has not joined yet. They join first - whoever runs this "
                "install gives them a join code (tiffin pair partner) - and then you can link."
            )
        if (
            _recent(
                db,
                "SELECT COUNT(*) FROM links WHERE a_person = ? AND created_at > ?",
                person_id,
                WINDOW_MINUTES,
            )
            >= MAX_MINTS
        ):
            raise LinkError("That is a lot of codes for one hour. Wait a while and try again.")
        # a newer code withdraws the one before it
        old = _pending_for(db, person_id)
        if old is not None:
            _event(db, int(old["id"]), person_id, "declined", method)
        expires = db.clock.now_utc().replace(microsecond=0) + dt.timedelta(hours=CODE_TTL_HOURS)
        while True:
            code = "".join(_secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
            if db.one("SELECT 1 FROM links WHERE code_sha256 = ?", (code_hash(code),)) is None:
                break
        link_id = db.insert(
            "links",
            a_person=person_id,
            b_person=int(other["id"]),
            code_sha256=code_hash(code),
            expires_at=stamp(expires),
        )
        _event(db, link_id, person_id, "offered", method)
    return pretty(code)


def _miss(db: Db, person_id: int) -> None:
    db.insert("link_attempts", person_id=person_id, ok=0)


def accept(db: Db, person_id: int, text: str, method: str) -> int:
    """Enter a code. Returns the new link's id, or raises LinkError in plain words.

    A wrong code is written down before the refusal is raised, so that guessing is counted."""
    refusal: str | None = None
    link_id = 0
    code = normalise_code(text)
    with db.tx():
        misses = _recent(
            db,
            "SELECT COUNT(*) FROM link_attempts WHERE person_id = ? AND ok = 0 AND created_at > ?",
            person_id,
            WINDOW_MINUTES,
        )
        row = None
        if misses >= MAX_MISSES:
            refusal = (
                "Too many codes that did not fit. Wait an hour, then try again - or ask for "
                "a new code."
            )
        elif is_linked(db):
            refusal = "You are linked already. Unlink first to use another code."
        else:
            row = (
                db.one("SELECT * FROM links WHERE code_sha256 = ?", (code_hash(code),))
                if len(code) == CODE_LENGTH
                else None
            )
            if row is None or int(row["b_person"]) != person_id:
                if row is not None and int(row["a_person"]) == person_id:
                    refusal = "That is the code you made. Give it to your partner to enter."
                else:
                    _miss(db, person_id)
                    refusal = "That code is not right. Check it, or ask for a new one."
            else:
                latest = _latest_event(db, int(row["id"]))
                if latest is None or str(latest["event"]) != "offered":
                    _miss(db, person_id)
                    refusal = "That code has been used or withdrawn. Ask for a new one."
                elif str(row["expires_at"]) <= db.clock.utc_stamp():
                    _event(db, int(row["id"]), None, "expired", "system")
                    _miss(db, person_id)
                    refusal = "That code has run out (they last 48 hours). Ask for a new one."
                else:
                    try:
                        _event(db, int(row["id"]), person_id, "accepted", method)
                    except sqlite3.IntegrityError:
                        refusal = "That code has been used or withdrawn. Ask for a new one."
                    else:
                        db.insert("link_attempts", person_id=person_id, ok=1)
                        link_id = int(row["id"])
    if refusal is not None:
        raise LinkError(refusal)
    return link_id


def decline(db: Db, person_id: int, method: str) -> bool:
    """Withdraw a code you made, or turn down one made for you. False when none is waiting."""
    with db.tx():
        now = db.clock.utc_stamp()
        for row in db.all(
            "SELECT id, expires_at FROM links WHERE (a_person = ? OR b_person = ?) "
            "ORDER BY id DESC LIMIT 5",
            (person_id, person_id),
        ):
            latest = _latest_event(db, int(row["id"]))
            if latest is not None and str(latest["event"]) == "offered":
                if str(row["expires_at"]) <= now:
                    _event(db, int(row["id"]), None, "expired", "system")
                    return False
                _event(db, int(row["id"]), person_id, "declined", method)
                return True
        return False


def unlink(db: Db, person_id: int, method: str, *, today: dt.date | None = None) -> Unlinked:
    """End the link, now, from either side. One event row; the open joint list closes and each
    of them gets their own copy of its lines. Built weekends stay as two ordinary menus."""
    from .food import shopping  # noqa: PLC0415 (food imports the db layer)

    with db.tx():
        row = active_link(db)
        if row is None or person_id not in (int(row["a_person"]), int(row["b_person"])):
            raise LinkError("You are not linked to anyone.")
        link_id = int(row["id"])
        other = int(row["b_person"]) if person_id == int(row["a_person"]) else int(row["a_person"])
        _event(db, link_id, person_id, "unlinked", method)
        closed, copied = shopping.close_joint(
            db, link_id, (person_id, other), today or db.clock.today_local()
        )
    return Unlinked(link_id, other, closed, copied)


def record_migrated(db: Db, a_person: int, b_person: int) -> int:
    """Record an existing pair as linked, for an install that had two people sharing weekends
    before links existed (the importer's one call). Writes the link and its `offered` and
    `accepted` events with method 'migrated', and no consent rows: what each of them agreed to
    stays as it was. Idempotent: if these two are already linked, returns that link's id.
    """
    with db.tx():
        row = active_link(db)
        if row is not None:
            if {int(row["a_person"]), int(row["b_person"])} == {a_person, b_person}:
                return int(row["id"])
            raise LinkError("One of them is linked to someone else already.")
        if a_person == b_person:
            raise LinkError("A person cannot be linked to themselves.")
        for pid in (a_person, b_person):
            if db.one("SELECT 1 FROM persons WHERE id = ? AND erased_at IS NULL", (pid,)) is None:
                raise LinkError("Both of them need to be people on this install.")
        placeholder = _secrets.token_hex(32)  # a code nobody holds: 64 hex chars, never typed
        link_id = db.insert(
            "links",
            a_person=a_person,
            b_person=b_person,
            code_sha256=hashlib.sha256(placeholder.encode("ascii")).hexdigest(),
            expires_at=db.clock.utc_stamp(),
        )
        _event(db, link_id, a_person, "offered", "migrated")
        _event(db, link_id, b_person, "accepted", "migrated")
        return link_id


def expires_label(value: str) -> str:
    return parse_stamp(value).strftime("%a %d %b %H:%M")
