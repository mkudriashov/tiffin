"""Profiles, pause state and weekly targets rows (formula start; calibration writes later rows)."""

from __future__ import annotations

import datetime as dt
import json
import sqlite3

from . import energy
from .clock import DEFAULT_CLOCK, Clock, isoday, parse_day, week_start
from .config import Config
from .db import Db
from .guardrails import apply_guardrails


def profile_on(db: Db, person_id: int, day: dt.date) -> sqlite3.Row | None:
    """The profile row in force on `day`: latest effective_from <= day, newest row wins."""
    return db.one(
        "SELECT * FROM profiles WHERE person_id = ? AND effective_from <= ? "
        "ORDER BY effective_from DESC, id DESC LIMIT 1",
        (person_id, isoday(day)),
    )


def _copy_with(db: Db, row: sqlite3.Row, changes: dict[str, object], day: dt.date) -> int:
    # sqlite3.Row has no __contains__, so `.keys()` is the iteration, not a dict idiom
    fields = {k: row[k] for k in row.keys() if k not in ("id", "created_at")}  # noqa: SIM118
    fields.update(changes)
    fields["effective_from"] = isoday(day)
    return db.insert("profiles", **fields)


def change_profile(
    db: Db,
    person_id: int,
    changes: dict[str, object],
    *,
    today: dt.date,
    effective: dt.date | None = None,
) -> int:
    """One change to a person's settings, as new profiles rows, and the id of the row from
    `effective` (today when not given).

    A profiles row is the whole profile, so a row booked for a later day - how they plan, from
    next Monday (food/planning.py) - was copied from the profile as it stood when it was booked.
    A change made since has to be written onto that row as well, or it would hold only until
    Monday and then quietly come undone; so the same change is written on every row already
    booked for a later day, each from the row it replaces. Every writer of profiles goes through
    here. LookupError when there is no profile yet: the interview writes the first.
    """
    start = effective or today
    base = profile_on(db, person_id, start) or profile_on(db, person_id, today)
    if base is None:
        raise LookupError("no profile yet")
    first = _copy_with(db, base, changes, start)
    later = [
        parse_day(str(r["effective_from"]))
        for r in db.all(
            "SELECT DISTINCT effective_from FROM profiles WHERE person_id = ? "
            "AND effective_from > ? ORDER BY effective_from",
            (person_id, isoday(start)),
        )
    ]
    for day in later:
        row = profile_on(db, person_id, day)
        if row is not None:
            _copy_with(db, row, changes, day)
    return first


def booked(db: Db, person_id: int, today: dt.date) -> sqlite3.Row | None:
    """The profile booked for the nearest day after today, when there is one: a change of how
    they plan waiting for its Monday."""
    row = db.one(
        "SELECT effective_from FROM profiles WHERE person_id = ? AND effective_from > ? "
        "ORDER BY effective_from LIMIT 1",
        (person_id, isoday(today)),
    )
    return None if row is None else profile_on(db, person_id, parse_day(str(row["effective_from"])))


def pause_active(db: Db, person_id: int, day: dt.date) -> bool:
    row = db.one(
        "SELECT start_day, end_day FROM pause_periods WHERE person_id = ? ORDER BY id DESC LIMIT 1",
        (person_id,),
    )
    if row is None:
        return False
    return str(row["start_day"]) <= isoday(day) <= str(row["end_day"])


def latest_weight(db: Db, person_id: int, on_or_before: dt.date) -> tuple[dt.date, float] | None:
    """Trend weight when a trend exists, else the raw latest weigh-in."""
    row = db.one(
        "SELECT day, trend_kg AS kg FROM v_trend_latest WHERE person_id = ? AND day <= ? "
        "ORDER BY day DESC LIMIT 1",
        (person_id, isoday(on_or_before)),
    )
    if row is None:
        row = db.one(
            "SELECT day, weight_kg AS kg FROM v_weigh_in_latest WHERE person_id = ? AND day <= ? "
            "ORDER BY day DESC LIMIT 1",
            (person_id, isoday(on_or_before)),
        )
    if row is None:
        return None
    return parse_day(str(row["day"])), float(row["kg"])


def targets_for(db: Db, person_id: int, week: dt.date) -> sqlite3.Row | None:
    return db.one(
        "SELECT * FROM v_targets_latest WHERE person_id = ? AND week_start = ?",
        (person_id, isoday(week_start(week))),
    )


def latest_targets(db: Db, person_id: int, on_or_before: dt.date) -> sqlite3.Row | None:
    return db.one(
        "SELECT * FROM v_targets_latest WHERE person_id = ? AND week_start <= ? "
        "ORDER BY week_start DESC LIMIT 1",
        (person_id, isoday(on_or_before)),
    )


def formula_targets(
    profile: sqlite3.Row, weight_kg: float, day: dt.date, config: Config
) -> tuple[energy.Macros, dict]:
    g = config.section("guardrails")
    age = energy.age_on(parse_day(str(profile["birth_date"])), day)
    bmr = energy.bmr_mifflin(str(profile["sex"]), weight_kg, float(profile["height_cm"]), age)
    tdee = energy.tdee_formula(bmr, float(profile["activity_factor"]))
    macros = energy.weekly_targets(
        weight_kg=weight_kg,
        tdee=tdee,
        rate_pct=float(profile["rate_pct_per_week"]),
        max_loss_kg_week=float(profile["max_loss_kg_week"]),
        kcal_floor=int(profile["kcal_floor"]),
        protein_g_per_kg=float(profile["protein_g_per_kg"]),
        fat_g_per_kg_min=float(profile["fat_g_per_kg_min"]),
        fibre_min=int(profile["fibre_g_min"]),
        fibre_max=int(g.get("fibre_max", 35)),
    )
    basis = {
        "method": "mifflin",
        "weight_kg": round(weight_kg, 2),
        "age": age,
        "bmr": round(bmr, 1),
        "tdee": round(tdee, 1),
        "activity_factor": float(profile["activity_factor"]),
        "rate_pct": float(profile["rate_pct_per_week"]),
        "deficit_kcal": round(
            energy.deficit_for_rate(
                weight_kg, float(profile["rate_pct_per_week"]), float(profile["max_loss_kg_week"])
            ),
            1,
        ),
    }
    return macros, basis


def insert_targets(
    db: Db,
    person_id: int,
    week: dt.date,
    macros: energy.Macros,
    source: str,
    basis: dict,
    *,
    proposal_id: int | None = None,
) -> int:
    return db.insert(
        "targets",
        person_id=person_id,
        week_start=isoday(week_start(week)),
        kcal=macros.kcal,
        protein_g=macros.protein_g,
        fat_g=macros.fat_g,
        carb_g=macros.carb_g,
        fibre_g=macros.fibre_g,
        source=source,
        basis_json=json.dumps(basis),
        proposal_id=proposal_id,
    )


def ensure_targets(
    db: Db, person_id: int, week: dt.date, config: Config, *, clock: Clock = DEFAULT_CLOCK
) -> sqlite3.Row | None:
    """This week's targets row, creating a formula row from the profile and latest weight.

    Returns None when the person has no profile or no weight yet (onboarding incomplete).
    """
    ws = week_start(week)
    existing = targets_for(db, person_id, ws)
    if existing is not None:
        return existing
    # the profile that GOVERNS this week, not the one in force on its Monday. Onboarding
    # writes a profile effective today, so asking at the week start found nothing on any day
    # but a Monday: six days in seven, finishing the interview left the week with no targets,
    # no morning message the following day, and /cook answering "they are set on Sunday
    # evening". The weight is already looked up at the week's end for the same reason.
    week_end = ws + dt.timedelta(days=6)
    profile = profile_on(db, person_id, week_end)
    if profile is None:
        return None
    weight = latest_weight(db, person_id, week_end)
    if weight is None:
        return None
    macros, basis = formula_targets(profile, weight[1], ws, config)
    previous = latest_targets(db, person_id, ws - dt.timedelta(days=1))
    prev_macros = None
    if previous is not None:
        prev_macros = energy.Macros(
            int(previous["kcal"]),
            int(previous["protein_g"]),
            int(previous["fat_g"]),
            int(previous["carb_g"]),
            int(previous["fibre_g"]),
        )
    verdict = apply_guardrails(
        macros,
        prev_macros,
        weight_kg=weight[1],
        kcal_floor=int(profile["kcal_floor"]),
        protein_g_per_kg=float(profile["protein_g_per_kg"]),
        fat_g_per_kg_min=float(profile["fat_g_per_kg_min"]),
        max_loss_kg_week=float(profile["max_loss_kg_week"]),
        source="formula",
    )
    basis["guardrail_notes"] = verdict.notes
    # A formula week never proposes: when the previous week exists the carry-over is the safe
    # choice, and the Sunday calibration (not this path) decides larger moves.
    if prev_macros is not None and not hasattr(verdict, "source"):
        basis["carried_over"] = True
        insert_targets(db, person_id, ws, prev_macros, "formula", basis)
    else:
        insert_targets(db, person_id, ws, verdict.macros, "formula", basis)
    return targets_for(db, person_id, ws)


def macros_of(row: sqlite3.Row) -> energy.Macros:
    return energy.Macros(
        int(row["kcal"]),
        int(row["protein_g"]),
        int(row["fat_g"]),
        int(row["carb_g"]),
        int(row["fibre_g"]),
    )
