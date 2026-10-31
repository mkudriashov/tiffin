"""Match planned training sessions to recorded intervals.icu activities."""

from __future__ import annotations

import datetime as dt
import sqlite3
from typing import Any

from .clock import isoday
from .db import Db

TOLERANCE = 0.5  # +/-50% of target_minutes


def planned_sessions(db: Db, person_id: int, day: dt.date) -> list[sqlite3.Row]:
    """The non-rest sessions planned for `day`'s weekday under the plan in force on `day`
    (latest effective_from <= day, newest row wins)."""
    return db.all(
        "SELECT ps.* FROM planned_sessions ps WHERE ps.person_id = ? AND ps.weekday = ? "
        "AND ps.kind <> 'rest' AND ps.plan_id = ("
        "  SELECT id FROM training_plans WHERE person_id = ? AND effective_from <= ? "
        "  ORDER BY effective_from DESC, id DESC LIMIT 1) ORDER BY ps.id",
        (person_id, day.weekday(), person_id, isoday(day)),
    )


def duration_ok(target_minutes: int, moving_s: int | None, elapsed_s: int | None) -> bool:
    """Either duration within the tolerance band; a target of 0 (none set) accepts any."""
    if target_minutes <= 0:
        return True
    lo, hi = target_minutes * (1 - TOLERANCE), target_minutes * (1 + TOLERANCE)
    return any(lo <= s / 60 <= hi for s in (moving_s, elapsed_s) if s)


def latest_matches(
    db: Db, person_id: int, day: dt.date
) -> dict[tuple[int | None, str | None], sqlite3.Row]:
    """The latest session_matches row per (planned_session_id, icu_id) for the day."""
    out: dict[tuple[int | None, str | None], sqlite3.Row] = {}
    for r in db.all(
        "SELECT * FROM session_matches WHERE person_id = ? AND day = ? ORDER BY id",
        (person_id, isoday(day)),
    ):
        pid = r["planned_session_id"]
        out[(int(pid) if pid is not None else None, r["icu_id"])] = r
    return out


def match_day(db: Db, person_id: int, day: dt.date) -> list[dict[str, Any]]:
    """Match `day`'s icu_activity rows to the planned sessions. Returns the rows inserted."""
    planned = planned_sessions(db, person_id, day)
    activities = db.all(
        "SELECT icu_id, kind, moving_s, elapsed_s FROM icu_activity "
        "WHERE person_id = ? AND day = ? ORDER BY start_utc, icu_id",
        (person_id, isoday(day)),
    )
    existing = latest_matches(db, person_id, day)
    standing = ("matched", "confirmed", "manual")
    taken = {k[0] for k, r in existing.items() if k[0] is not None and r["status"] in standing}
    matched = {k[1] for k, r in existing.items() if k[0] is not None and r["status"] in standing}
    inserted: list[dict[str, Any]] = []

    def insert(act: sqlite3.Row, planned_id: int | None, status: str) -> None:
        seconds = act["moving_s"] or act["elapsed_s"] or 0
        row: dict[str, Any] = {
            "person_id": person_id,
            "day": isoday(day),
            "planned_session_id": planned_id,
            "icu_id": str(act["icu_id"]),
            "status": status,
            "minutes": round(seconds / 60) if seconds else None,
        }
        row["id"] = db.insert("session_matches", **row)
        inserted.append(row)

    with db.tx():
        # Pass 1: matches. A pair the person already rejected is skipped in favour of the
        # next planned session of that kind, so a rejection frees the session for another
        # activity and is never overwritten.
        for act in activities:
            icu_id = str(act["icu_id"])
            if icu_id in matched or act["kind"] is None:
                continue
            for p in planned:
                planned_id = int(p["id"])
                if (
                    planned_id in taken
                    or p["kind"] != act["kind"]
                    or not duration_ok(int(p["target_minutes"]), act["moving_s"], act["elapsed_s"])
                    or (planned_id, icu_id) in existing
                ):
                    continue
                insert(act, planned_id, "matched")
                taken.add(planned_id)
                matched.add(icu_id)
                break
        # Pass 2: everything still unmatched is an extra, once.
        for act in activities:
            icu_id = str(act["icu_id"])
            if icu_id in matched or (None, icu_id) in existing:
                continue
            insert(act, None, "extra")
    return inserted


__all__ = ["TOLERANCE", "duration_ok", "latest_matches", "match_day", "planned_sessions"]
