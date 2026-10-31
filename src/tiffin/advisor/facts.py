"""advisor.facts: everything the rules and the Sunday prose look at, computed in code (§B.9).

`compute(db, person_id, week_start)` reads the week Mon..Sun ending before the review, plus
the 28-day windows the rules need. Nothing here is a judgement; rules.py judges.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from dataclasses import dataclass, field

from .. import body, energy
from ..clock import isoday, parse_day, week_start
from ..config import Config
from ..db import Db
from ..targets import latest_targets, macros_of, profile_on

WEIGHT_WINDOW_DAYS = 28


@dataclass
class Facts:
    person_id: int
    week_start: dt.date
    week_end: dt.date
    profile: sqlite3.Row | None = None
    targets: energy.Macros | None = None
    weight_kg: float | None = None
    trend_now: float | None = None
    trend_delta_7: float | None = None
    trend_delta_14: float | None = None
    trend_delta_28: float | None = None
    # the raw weigh-ins of the WEIGHT_WINDOW_DAYS to week_end: their straight-line rate in kg a
    # week (negative is a loss), how many, how far apart the first and last, and the last day
    weight_rate_28: float | None = None
    weigh_ins_28: int = 0
    weigh_in_span_28: int = 0
    last_weigh_in: dt.date | None = None
    waist_delta_28: float | None = None
    tape_rows_28: int = 0
    intake_mean: float | None = None
    usable_days: int = 0
    confirmed_days: int = 0
    unconfirmed_days: int = 0
    paused_days: int = 0
    protein_short_days: int = 0
    fibre_short_days: int = 0
    sessions_done: int = 0
    sessions_planned: int = 0
    #: whether anything could count the week's training: the watch sent an activity in the
    #: four weeks to its end, or a session was logged by hand. Without either, "0 of 6" is
    sessions_measured: bool = True
    strength_done: int = 0
    strength_planned: int = 0
    sessions_pct_prev_week: float | None = None
    steps_mean: float | None = None
    steps_floor_misses: int = 0
    weeks_in_deficit: int = 0
    calibration_count: int = 0
    prev_week_delta_7: float | None = None
    prev_week_usable_days: int = 0
    cycle_phase: str | None = None
    day_intakes: list[energy.DayIntake] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def sessions_pct(self) -> float | None:
        if not self.sessions_planned:
            return None
        return 100.0 * self.sessions_done / self.sessions_planned


def _days(a: dt.date, b: dt.date) -> list[dt.date]:
    return [a + dt.timedelta(days=i) for i in range((b - a).days + 1)]


def day_intakes(
    db: Db,
    person_id: int,
    start: dt.date,
    end: dt.date,
    *,
    as_of: dt.date | None = None,
    since: dt.date | None = None,
) -> list[energy.DayIntake]:
    """The week's days, as intakes. `as_of` is today; `since` is the day this person began.

    A day with no row was "unconfirmed", meaning a miss, and two kinds of day have no row
    through nobody's fault. The day it is still IS: the Sunday summary goes out at 19:00, the
    check-in is at 20:30 and `day_close` runs Monday 00:05, so Sunday could never be anything
    but a miss - "6 confirmed, 1 unconfirmed" every week, for ever, to somebody who answered
    every completed day. And the days before a person existed: her first weekly review opened
    by scoring her nothing out of seven, five of those days before her profile's
    `effective_from` and before her first weigh-in, both of which are in the database.

    Neither is counted now. A day that has not finished is still included when it HAS a row -
    somebody who confirmed early gets the credit - it simply is not a miss for being unfinished.
    """
    rows = db.all(
        "SELECT day, status, kcal FROM v_day_latest WHERE person_id = ? AND day BETWEEN ? AND ?",
        (person_id, isoday(start), isoday(end)),
    )
    by_day = {str(r["day"]): r for r in rows}
    out: list[energy.DayIntake] = []
    for d in _days(start, end):
        r = by_day.get(isoday(d))
        if r is None:
            if (as_of is not None and d >= as_of) or (since is not None and d < since):
                continue
            out.append(energy.DayIntake(d, 0.0, "unconfirmed"))
            continue
        status = str(r["status"])
        kcal = float(r["kcal"]) if r["kcal"] is not None else 0.0
        if status == "confirmed" and r["kcal"] is None:
            status = "unconfirmed"  # confirmed without numbers cannot feed the calibration
        out.append(energy.DayIntake(d, kcal, status))
    return out


def _summary_row(db: Db, person_id: int, ws: dt.date) -> sqlite3.Row | None:
    return db.one(
        "SELECT * FROM v_week_summary WHERE person_id = ? AND week_start = ?",
        (person_id, isoday(ws)),
    )


def _macro_short_days(
    db: Db, person_id: int, ws: dt.date, we: dt.date, t: energy.Macros | None
) -> tuple[int, int]:
    if t is None:
        return 0, 0
    rows = db.all(
        "SELECT protein_g, fibre_g FROM v_day_latest WHERE person_id = ? AND day BETWEEN ? AND ? "
        "AND status = 'confirmed' AND kcal IS NOT NULL",
        (person_id, isoday(ws), isoday(we)),
    )
    protein_short = sum(
        1 for r in rows if r["protein_g"] is not None and float(r["protein_g"]) < t.protein_g * 0.9
    )
    fibre_short = sum(1 for r in rows if r["fibre_g"] is not None and float(r["fibre_g"]) < 25)
    return protein_short, fibre_short


def _sessions_measured(db: Db, person_id: int, ws: dt.date, we: dt.date) -> bool:
    """Whether the week's training could be counted at all (see `Facts.sessions_measured`)."""
    if (
        db.scalar(
            "SELECT 1 FROM icu_activity WHERE person_id = ? AND day BETWEEN ? AND ? LIMIT 1",
            (person_id, isoday(we - dt.timedelta(days=27)), isoday(we)),
        )
        is not None
    ):
        return True
    return (
        db.scalar(
            "SELECT 1 FROM session_matches WHERE person_id = ? AND day BETWEEN ? AND ? "
            "AND status IN ('manual', 'confirmed') LIMIT 1",
            (person_id, isoday(ws), isoday(we)),
        )
        is not None
    )


def _strength(db: Db, person_id: int, ws: dt.date, we: dt.date) -> tuple[int, int]:
    planned = db.scalar(
        "SELECT COUNT(*) FROM planned_sessions ps WHERE ps.person_id = ? AND ps.kind = 'strength' "
        "AND ps.plan_id = (SELECT id FROM training_plans tp WHERE tp.person_id = ? "
        "AND tp.effective_from <= ? ORDER BY tp.effective_from DESC, tp.id DESC LIMIT 1)",
        (person_id, person_id, isoday(we)),
    )
    done = db.scalar(
        "SELECT COUNT(*) FROM session_matches sm JOIN planned_sessions ps ON ps.id = sm.planned_session_id "
        "WHERE sm.person_id = ? AND sm.day BETWEEN ? AND ? AND ps.kind = 'strength' "
        "AND sm.status IN ('matched', 'confirmed', 'manual') AND sm.id = (SELECT MAX(id) FROM session_matches x "
        "WHERE x.person_id = sm.person_id AND x.day = sm.day "
        "AND COALESCE(x.planned_session_id, -1) = COALESCE(sm.planned_session_id, -1))",
        (person_id, isoday(ws), isoday(we)),
    )
    return int(done or 0), int(planned or 0)


def _waist_delta(db: Db, person_id: int, we: dt.date) -> tuple[float | None, int]:
    rows = db.all(
        "SELECT day, cm FROM tape_measures WHERE person_id = ? AND site = 'waist' AND day <= ? "
        "AND day >= ? ORDER BY day",
        (person_id, isoday(we), isoday(we - dt.timedelta(days=35))),
    )
    if len(rows) < 2:
        return None, len(rows)
    latest = rows[-1]
    earlier = [
        r
        for r in rows
        if parse_day(str(r["day"])) <= parse_day(str(latest["day"])) - dt.timedelta(days=21)
    ]
    if not earlier:
        return None, len(rows)
    return round(float(latest["cm"]) - float(earlier[-1]["cm"]), 1), len(rows)


def _steps(
    db: Db, person_id: int, ws: dt.date, we: dt.date, floor: int | None
) -> tuple[float | None, int]:
    rows = db.all(
        "SELECT steps FROM icu_wellness WHERE person_id = ? AND day BETWEEN ? AND ? AND steps IS NOT NULL",
        (person_id, isoday(ws), isoday(we)),
    )
    if not rows:
        return None, 0
    values = [int(r["steps"]) for r in rows]
    misses = sum(1 for v in values if floor is not None and v < floor)
    return round(sum(values) / len(values), 0), misses


def _weeks_in_deficit(db: Db, person_id: int, ws: dt.date) -> int:
    rows = db.all(
        "SELECT week_start, basis_json FROM v_targets_latest WHERE person_id = ? AND week_start <= ? "
        "ORDER BY week_start DESC LIMIT 20",
        (person_id, isoday(ws)),
    )
    n = 0
    expected = ws
    for r in rows:
        if parse_day(str(r["week_start"])) != expected:
            break
        try:
            basis = json.loads(r["basis_json"] or "{}")
        except ValueError:
            basis = {}
        if basis.get("maintenance"):
            break
        prof = profile_on(db, person_id, expected - dt.timedelta(days=1))
        if prof is not None and not float(prof["rate_pct_per_week"] or 0.0):
            break
        n += 1
        expected = expected - dt.timedelta(days=7)
    return n


def weight_cuts(db: Db, person_id: int) -> list[tuple[dt.date, int]]:
    """Every R2W cut the person accepted, as (the Monday it began, its kcal), oldest first.

    Historical accepted cuts remain in targets until a measured week replaces their estimate.
    """
    rows = db.all(
        "SELECT p.payload_json FROM proposals p WHERE p.person_id = ? AND p.kind = 'targets' "
        "AND EXISTS (SELECT 1 FROM proposal_decisions d WHERE d.proposal_id = p.id "
        "AND d.decision = 'accepted') ORDER BY p.id",
        (person_id,),
    )
    out: list[tuple[dt.date, int]] = []
    for r in rows:
        try:
            payload = json.loads(r["payload_json"] or "{}")
        except ValueError:
            continue
        if payload.get("rule") == "R2W" and payload.get("week_start"):
            out.append((parse_day(str(payload["week_start"])), int(payload.get("kcal_delta") or 0)))
    return sorted(out)


def _cycle_phase(db: Db, person_id: int, we: dt.date) -> str | None:
    # worked out from her period starts where she enters them (body.cycle_on), else the phase
    # she last tapped
    worked = body.cycle_on(db, person_id, we)
    if worked is not None:
        return worked.phase
    row = db.one(
        "SELECT phase FROM cycle_phases WHERE person_id = ? AND day_start <= ? ORDER BY day_start DESC, id DESC LIMIT 1",
        (person_id, isoday(we)),
    )
    return None if row is None else str(row["phase"])


def compute(
    db: Db, person_id: int, week: dt.date, config: Config, *, as_of: dt.date | None = None
) -> Facts:
    ws = week_start(week)
    we = ws + dt.timedelta(days=6)
    f = Facts(person_id, ws, we)
    f.profile = profile_on(db, person_id, we)
    t = latest_targets(db, person_id, we)
    f.targets = macros_of(t) if t is not None else None
    if f.profile is not None and f.profile["cycle_tracking"]:
        f.cycle_phase = _cycle_phase(db, person_id, we)

    point = body.latest_trend(db, person_id, we)
    if point is not None:
        f.trend_now = point.trend_kg
        f.weight_kg = point.trend_kg
        f.trend_delta_7 = body.trend_delta(db, person_id, we, 7)
        f.trend_delta_14 = body.trend_delta(db, person_id, we, 14)
        f.trend_delta_28 = body.trend_delta(db, person_id, we, 28)
        f.prev_week_delta_7 = body.trend_delta(db, person_id, we - dt.timedelta(days=7), 7)
    first = we - dt.timedelta(days=WEIGHT_WINDOW_DAYS - 1)
    window = [(d, kg) for d, kg, _id in body.weigh_ins(db, person_id) if first <= d <= we]
    f.weigh_ins_28 = len(window)
    if window:
        f.last_weigh_in = window[-1][0]
        f.weigh_in_span_28 = (window[-1][0] - window[0][0]).days
    f.weight_rate_28 = energy.weight_slope(window)
    f.waist_delta_28, f.tape_rows_28 = _waist_delta(db, person_id, we)

    began = parse_day(str(f.profile["effective_from"])) if f.profile is not None else None
    f.day_intakes = day_intakes(db, person_id, ws, we, as_of=as_of, since=began)
    usable = energy.usable(f.day_intakes)
    f.usable_days = len(usable)
    f.intake_mean = round(sum(d.kcal for d in usable) / len(usable), 1) if usable else None
    f.confirmed_days = sum(1 for d in f.day_intakes if d.status == "confirmed")
    f.unconfirmed_days = sum(1 for d in f.day_intakes if d.status == "unconfirmed")
    f.paused_days = sum(1 for d in f.day_intakes if d.status == "paused")
    f.prev_week_usable_days = len(
        energy.usable(
            day_intakes(db, person_id, ws - dt.timedelta(days=7), ws - dt.timedelta(days=1))
        )
    )
    f.protein_short_days, f.fibre_short_days = _macro_short_days(db, person_id, ws, we, f.targets)

    summary = _summary_row(db, person_id, ws)
    if summary is not None:
        f.sessions_done = int(summary["sessions_done"] or 0)
        f.sessions_planned = int(summary["sessions_planned"] or 0)
    prev = _summary_row(db, person_id, ws - dt.timedelta(days=7))
    if prev is not None and prev["sessions_planned"]:
        f.sessions_pct_prev_week = (
            100.0 * float(prev["sessions_done"] or 0) / float(prev["sessions_planned"])
        )
    f.strength_done, f.strength_planned = _strength(db, person_id, ws, we)
    f.sessions_measured = _sessions_measured(db, person_id, ws, we)
    floor = int(f.profile["steps_floor"]) if f.profile is not None else None
    f.steps_mean, f.steps_floor_misses = _steps(db, person_id, ws, we, floor)
    f.weeks_in_deficit = _weeks_in_deficit(db, person_id, ws)
    f.calibration_count = int(
        db.scalar(
            "SELECT COUNT(*) FROM tdee_calibrations WHERE person_id = ? AND tdee_measured IS NOT NULL",
            (person_id,),
        )
        or 0
    )
    return f
