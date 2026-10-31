"""Weigh-ins and the EWMA trend. Trend rows are a ledger: a recomputed value
for a day is a new trend_points row; readers take v_trend_latest."""

from __future__ import annotations

import datetime as dt
import itertools
from dataclasses import dataclass

from . import energy
from .clock import isoday, parse_day
from .db import Db

ALPHA = 0.10


def weigh_ins(db: Db, person_id: int) -> list[tuple[dt.date, float, int | None]]:
    rows = db.all(
        "SELECT id, day, weight_kg FROM v_weigh_in_latest WHERE person_id = ? ORDER BY day",
        (person_id,),
    )
    return [(parse_day(str(r["day"])), float(r["weight_kg"]), int(r["id"])) for r in rows]


def trend_points(db: Db, person_id: int) -> list[energy.TrendPoint]:
    rows = db.all(
        "SELECT day, trend_kg, alpha, n, weigh_in_id FROM v_trend_latest WHERE person_id = ? "
        "ORDER BY day",
        (person_id,),
    )
    return [
        energy.TrendPoint(
            parse_day(str(r["day"])),
            float(r["trend_kg"]),
            float(r["alpha"]),
            int(r["n"]),
            r["weigh_in_id"],
        )
        for r in rows
    ]


def rebuild_trend(db: Db, person_id: int, *, alpha: float = ALPHA) -> list[energy.TrendPoint]:
    """Recompute the EWMA over every latest-per-day weigh-in; insert rows only where changed."""
    computed = energy.trend_series(weigh_ins(db, person_id), alpha)
    current = {p.day: p for p in trend_points(db, person_id)}
    with db.tx():
        for p in computed:
            have = current.get(p.day)
            if have is not None and abs(have.trend_kg - p.trend_kg) < 0.0005 and have.n == p.n:
                continue
            db.insert(
                "trend_points",
                person_id=person_id,
                day=isoday(p.day),
                trend_kg=p.trend_kg,
                alpha=p.alpha,
                n=p.n,
                weigh_in_id=p.weigh_in_id,
            )
    return computed


def record_weigh_in(
    db: Db,
    person_id: int,
    day: dt.date,
    weight_kg: float,
    source: str,
    *,
    icu_raw_id: int | None = None,
    alpha: float = ALPHA,
) -> int:
    """Append a weigh-in (a repeat of the same day/value is skipped) and refresh the trend."""
    latest = db.latest("weigh_ins", person_id=person_id, day=isoday(day))
    if latest is not None and abs(float(latest["weight_kg"]) - weight_kg) < 0.005:
        return int(latest["id"])
    wid = db.insert(
        "weigh_ins",
        person_id=person_id,
        day=isoday(day),
        weight_kg=round(weight_kg, 2),
        source=source,
        icu_raw_id=icu_raw_id,
    )
    rebuild_trend(db, person_id, alpha=alpha)
    return wid


def latest_trend(db: Db, person_id: int, on_or_before: dt.date) -> energy.TrendPoint | None:
    return energy.trend_at(trend_points(db, person_id), on_or_before)


def trend_delta(db: Db, person_id: int, end: dt.date, days: int = 7) -> float | None:
    return energy.trend_delta(trend_points(db, person_id), end, days)


#: waist, seat and sleeve, weekly - what a jacket and trousers are sized from). The seat is the
#: fullest part of the buttocks, which is what `hip` has always held
TAPE_SITES = ("waist", "hip", "chest", "neck", "overarm", "shoulder", "sleeve")
#: how each site is said back
TAPE_WORDS = {
    "waist": "waist",
    "hip": "seat",
    "chest": "chest",
    "neck": "neck",
    "overarm": "overarm",
    "shoulder": "shoulder width",
    "sleeve": "sleeve",
}
CM_PER_INCH = 2.54
CYCLE_PHASES = ("menstrual", "follicular", "ovulation", "luteal")
TAPE_CM_MIN, TAPE_CM_MAX = 15.0, 200.0


def record_tape(db: Db, person_id: int, day: dt.date, site: str, cm: float) -> int:
    """Append a tape measurement. A ledger: a correction is another row."""
    if site not in TAPE_SITES:
        raise ValueError(f"not a tape site: {site!r}")
    if not TAPE_CM_MIN <= cm <= TAPE_CM_MAX:
        raise ValueError(f"{cm} cm is outside {TAPE_CM_MIN:.0f}-{TAPE_CM_MAX:.0f}")
    return db.insert(
        "tape_measures", person_id=person_id, day=isoday(day), site=site, cm=round(cm, 1)
    )


def latest_tape(db: Db, person_id: int, site: str) -> tuple[dt.date, float] | None:
    row = db.one(
        "SELECT day, cm FROM tape_measures WHERE person_id = ? AND site = ? "
        "ORDER BY day DESC, id DESC LIMIT 1",
        (person_id, site),
    )
    return None if row is None else (parse_day(str(row["day"])), float(row["cm"]))


def record_cycle_phase(db: Db, person_id: int, day_start: dt.date, phase: str) -> int:
    """Append a cycle phase. Read by the advisor's R9 guard only."""
    if phase not in CYCLE_PHASES:
        raise ValueError(f"not a cycle phase: {phase!r}")
    return db.insert("cycle_phases", person_id=person_id, day_start=isoday(day_start), phase=phase)


#: a cycle's length when fewer than two starts say otherwise, and the range a measured one may be
CYCLE_DAYS = 28
CYCLE_RANGE = (21, 40)
#: a start older than this says nothing about today: a period not entered is not a long cycle
CYCLE_STALE_DAYS = 45


@dataclass(frozen=True)
class CycleDay:
    start: dt.date
    day: int
    length: int
    phase: str


def record_period_start(db: Db, person_id: int, day: dt.date) -> int:
    """
    period's first day"): a menstrual phase from that day, which every reader of `cycle_phases`
    already understands."""
    return record_cycle_phase(db, person_id, day, "menstrual")


def cycle_on(db: Db, person_id: int, day: dt.date) -> CycleDay | None:
    """Where `day` falls in the cycle, worked out from the latest period start on or before it:
    days 1-5 menstrual, ovulation about 14 days before the next start, follicular before it and
    luteal after. The length is the average of her own cycles when two starts or more say so,
    else 28. None with no start in the last six weeks - then a tapped phase is all there is."""
    rows = db.all(
        "SELECT DISTINCT day_start FROM cycle_phases WHERE person_id = ? AND phase = 'menstrual' "
        "AND day_start <= ? ORDER BY day_start",
        (person_id, isoday(day)),
    )
    starts = [parse_day(str(r["day_start"])) for r in rows]
    # two entries within a week are one period said twice
    kept: list[dt.date] = []
    for s in starts:
        if not kept or (s - kept[-1]).days >= 14:
            kept.append(s)
    if not kept or (day - kept[-1]).days > CYCLE_STALE_DAYS:
        return None
    gaps = [
        (b - a).days
        for a, b in itertools.pairwise(kept)
        if CYCLE_RANGE[0] <= (b - a).days <= CYCLE_RANGE[1]
    ]
    length = round(sum(gaps) / len(gaps)) if gaps else CYCLE_DAYS
    n = (day - kept[-1]).days + 1
    ovulation = length - 14
    if n <= 5:
        phase = "menstrual"
    elif n < ovulation - 1:
        phase = "follicular"
    elif n <= ovulation + 1:
        phase = "ovulation"
    else:
        phase = "luteal"
    return CycleDay(kept[-1], n, length, phase)


def days_since_weigh_in(db: Db, person_id: int, today: dt.date) -> int | None:
    row = db.one(
        "SELECT MAX(day) AS d FROM v_weigh_in_latest WHERE person_id = ? AND day <= ?",
        (person_id, isoday(today)),
    )
    if row is None or row["d"] is None:
        return None
    return (today - parse_day(str(row["d"]))).days
