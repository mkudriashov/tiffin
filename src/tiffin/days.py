"""Day records: what was planned, what was confirmed, how a day closes (items 21, 79).

A day is confirmed by a tap, stored unconfirmed at 00:05 when nobody answered (never eaten,
never zero), or paused inside a pause period. Corrections are new rows; v_day_latest wins.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3

from .clock import isoday
from .db import Db
from .energy import Macros
from .targets import latest_targets, macros_of, pause_active, profile_on

DAY_STATUSES = ("confirmed", "unconfirmed", "paused")


def latest_confirmation(db: Db, person_id: int, day: dt.date) -> sqlite3.Row | None:
    return db.one(
        "SELECT * FROM v_day_latest WHERE person_id = ? AND day = ?", (person_id, isoday(day))
    )


def planned_macros(db: Db, person_id: int, day: dt.date) -> Macros | None:
    """The accepted menu's macros for the day, when a menu covers it."""
    row = db.one(
        "SELECT md.kcal, md.protein_g, md.fat_g, md.carb_g, md.fibre_g "
        "FROM v_menu_days_current md WHERE md.person_id = ? AND md.day = ?",
        (person_id, isoday(day)),
    )
    if row is None:
        return None
    return Macros(
        int(row["kcal"]),
        round(float(row["protein_g"])),
        round(float(row["fat_g"])),
        round(float(row["carb_g"])),
        round(float(row["fibre_g"])),
    )


def planned_slot_macros(db: Db, person_id: int, day: dt.date, slot: str) -> Macros | None:
    """One planned slot's own macros, for "the day minus the meal that was replaced".

    An off-plan answer that names the meal it replaced - "skipped the turkey dinner, had a
    burrito instead" - was still costed against the WHOLE day's planned macros, so the missed
    dinner and the burrito both landed in the total: a day that was actually about right read as
    the full plan plus a takeaway, and that number feeds the measured TDEE and next week's
    targets.
    """
    row = db.one(
        "SELECT ms.kcal, ms.protein_g, ms.fat_g, ms.carb_g, ms.fibre_g "
        "FROM v_menu_days_current md JOIN meal_slots ms ON ms.menu_day_id = md.id "
        "WHERE md.person_id = ? AND md.day = ? AND ms.slot = ?",
        (person_id, isoday(day), slot),
    )
    if row is None:
        return None
    return Macros(
        int(row["kcal"]),
        round(float(row["protein_g"])),
        round(float(row["fat_g"])),
        round(float(row["carb_g"])),
        round(float(row["fibre_g"])),
    )


def macros_minus_slot(whole_day: Macros, slot: Macros) -> Macros:
    """`whole_day` with one planned slot's macros removed - never below zero."""
    return Macros(
        max(whole_day.kcal - slot.kcal, 0),
        max(whole_day.protein_g - slot.protein_g, 0),
        max(whole_day.fat_g - slot.fat_g, 0),
        max(whole_day.carb_g - slot.carb_g, 0),
        max(whole_day.fibre_g - slot.fibre_g, 0),
    )


def target_macros(db: Db, person_id: int, day: dt.date) -> Macros | None:
    row = latest_targets(db, person_id, day)
    return None if row is None else macros_of(row)


def accepted_estimates(db: Db, person_id: int, day: dt.date) -> list[sqlite3.Row]:
    """Latest row per estimate chain that is accepted, for the day."""
    rows = db.all(
        "SELECT e.* FROM estimates e WHERE e.person_id = ? AND e.day = ? AND e.status = 'accepted' "
        "AND e.id = (SELECT MAX(id) FROM estimates x WHERE x.person_id = e.person_id "
        "AND COALESCE(x.source_estimate_id, x.id) = COALESCE(e.source_estimate_id, e.id)) "
        "ORDER BY e.id",
        (person_id, isoday(day)),
    )
    return rows


def replaced_slots(estimates: list[sqlite3.Row]) -> list[str]:
    """The planned meals these accepted estimates replaced, each once, in first-named order."""
    out: list[str] = []
    for e in estimates:
        try:
            named = json.loads(str(e["replaced_slots_json"] or "[]"))
        except ValueError, IndexError, KeyError:
            named = []
        for slot in named if isinstance(named, list) else []:
            if isinstance(slot, str) and slot not in out:
                out.append(slot)
    return out


def confirm_day(
    db: Db,
    person_id: int,
    day: dt.date,
    *,
    status: str = "confirmed",
    basis: str,
    macros: Macros | None,
    note: str | None = None,
    message_in_id: int | None = None,
) -> int:
    if status not in DAY_STATUSES:
        raise ValueError(status)
    extras = accepted_estimates(db, person_id, day)
    kcal = protein = fat = carb = fibre = None
    if macros is not None:
        for slot in replaced_slots(extras):
            slot_macros = planned_slot_macros(db, person_id, day, slot)
            if slot_macros is not None:
                macros = macros_minus_slot(macros, slot_macros)
    if macros is not None:
        kcal, protein, fat, carb, fibre = (
            float(macros.kcal),
            float(macros.protein_g),
            float(macros.fat_g),
            float(macros.carb_g),
            float(macros.fibre_g),
        )
        for e in extras:
            kcal += float(e["kcal"] or 0)
            protein += float(e["protein_g"] or 0)
            fat += float(e["fat_g"] or 0)
            carb += float(e["carb_g"] or 0)
            fibre += float(e["fibre_g"] or 0)
        if extras and basis in ("menu", "none"):
            basis = "mixed"
    return db.insert(
        "day_confirmations",
        person_id=person_id,
        day=isoday(day),
        status=status,
        basis=basis,
        kcal=kcal,
        protein_g=protein,
        fat_g=fat,
        carb_g=carb,
        fibre_g=fibre,
        note=note,
        message_in_id=message_in_id,
    )


#: how far back the evening check-in offers to answer days left open: the week a person can
#: still remember, and the same week the bot's dated buttons accept (dayflow.BACKDATE_DAYS)
CATCH_UP_DAYS = 6


def unanswered_days(
    db: Db, person_id: int, before: dt.date, back: int = CATCH_UP_DAYS
) -> list[dt.date]:
    """The days in the `back` days before `before` that had a menu and were never answered -
    not confirmed, not paused - oldest first.

    Two of twelve days were ever answered, and a day nobody answered by midnight
    was lost for good: the week's calibration needs five and never once had them. One tap on
    the evening check-in now answers these as well. A silent day is never assumed
    to have gone to plan; it is offered back, by name, and stays unknown until somebody says.
    """
    out: list[dt.date] = []
    for i in range(back, 0, -1):
        day = before - dt.timedelta(days=i)
        latest = latest_confirmation(db, person_id, day)
        if latest is not None and str(latest["status"]) in ("confirmed", "paused"):
            continue
        if pause_active(db, person_id, day) or planned_macros(db, person_id, day) is None:
            continue
        out.append(day)
    return out


def unanswered_without_menu(
    db: Db, person_id: int, before: dt.date, back: int = CATCH_UP_DAYS
) -> list[dt.date]:
    """The days in the `back` days before `before` that had targets but no menu and were never
    answered, oldest first: the days `unanswered_days` leaves out.

    A weekly rhythm's Sunday check-in is the only ask its week gets, so a Saturday with no menu
    was never mentioned at all and stayed unrecorded every week. The daily
    check-in asks such a day as "the target was N kcal"; these are offered the same way, by name.
    """
    out: list[dt.date] = []
    for i in range(back, 0, -1):
        day = before - dt.timedelta(days=i)
        latest = latest_confirmation(db, person_id, day)
        if latest is not None and str(latest["status"]) in ("confirmed", "paused"):
            continue
        if pause_active(db, person_id, day) or planned_macros(db, person_id, day) is not None:
            continue
        # a week's targets reach back past the day somebody began; those days are nobody's
        if profile_on(db, person_id, day) is None or target_macros(db, person_id, day) is None:
            continue
        out.append(day)
    return out


def close_day(db: Db, person_id: int, day: dt.date) -> int | None:
    """Write unconfirmed (or paused) for `day` when nothing was confirmed. None if a row exists."""
    if latest_confirmation(db, person_id, day) is not None:
        return None
    paused = pause_active(db, person_id, day)
    return db.insert(
        "day_confirmations",
        person_id=person_id,
        day=isoday(day),
        status="paused" if paused else "unconfirmed",
        basis="none",
    )


def matched_session(db: Db, person_id: int, day: dt.date) -> sqlite3.Row | None:
    """Today's latest matched-but-unconfirmed session, with the planned kind and minutes."""
    return db.one(
        "SELECT sm.*, ps.kind AS planned_kind, ps.target_minutes FROM session_matches sm "
        "LEFT JOIN planned_sessions ps ON ps.id = sm.planned_session_id "
        "WHERE sm.person_id = ? AND sm.day = ? AND sm.status = 'matched' "
        "AND sm.id = (SELECT MAX(id) FROM session_matches x WHERE x.person_id = sm.person_id "
        "AND x.day = sm.day AND COALESCE(x.planned_session_id, -1) = "
        "COALESCE(sm.planned_session_id, -1) AND COALESCE(x.icu_id, '') = COALESCE(sm.icu_id, ''))",
        (person_id, isoday(day)),
    )


def planned_session(db: Db, person_id: int, day: dt.date) -> sqlite3.Row | None:
    rows = planned_sessions_on(db, person_id, day)
    return rows[0] if rows else None


def planned_sessions_on(db: Db, person_id: int, day: dt.date) -> list[sqlite3.Row]:
    """Every session planned for the weekday, not just the first.

    A day can hold two: `Tue ride 60, Tue strength 30` is an ordinary week. Reading only the
    first meant the morning message and the evening check-in named one of them and quietly
    behaved as though the other did not exist.
    """
    return db.all(
        "SELECT ps.* FROM planned_sessions ps WHERE ps.person_id = ? AND ps.weekday = ? "
        "AND ps.plan_id = (SELECT id FROM training_plans WHERE person_id = ? "
        "AND effective_from <= ? "
        "ORDER BY effective_from DESC, id DESC LIMIT 1) ORDER BY ps.id",
        (person_id, day.weekday(), person_id, isoday(day)),
    )


def session_decided(db: Db, person_id: int, day: dt.date) -> sqlite3.Row | None:
    """Any confirmed/manual/missed/rejected decision already taken for the day's planned session."""
    return db.one(
        "SELECT * FROM session_matches WHERE person_id = ? AND day = ? "
        "AND status IN ('confirmed', 'manual', 'missed', 'rejected') ORDER BY id DESC LIMIT 1",
        (person_id, isoday(day)),
    )
