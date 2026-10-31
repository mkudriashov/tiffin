"""Trend series and chart metadata for one signed-in person."""

from __future__ import annotations

import datetime as dt
from typing import Any

from ..clock import isoday, parse_day, week_start
from ..db import Db
from .queries import (
    TAPE_COMPARE_DEFAULT,
    TREND_METRICS,
    TREND_UNITS,
    WIDE_WINDOW_DAYS,
    _f,
    done_sessions,
    planned_sessions,
)


def _weight_projection(
    goal: float | None, goal_date: str | None, smooth: list[Any]
) -> dict[str, Any] | None:
    """Where the trend line meets the goal, extrapolated from its own slope over the window
    shown - not a second calculation the client repeats, a sentence it can just print.

    Uses the two ends of the already-fetched `ewma` window rather than a fresh query: the same
    smoothing that draws the line decides where it is heading, so the projection always matches
    what is actually on the chart, including as the window selector changes it.
    """
    if goal is None or len(smooth) < 2:
        return None
    d0, d1 = parse_day(str(smooth[0]["day"])), parse_day(str(smooth[-1]["day"]))
    v0, v1 = _f(smooth[0]["v"]), _f(smooth[-1]["v"])
    span = (d1 - d0).days
    if span <= 0 or v0 is None or v1 is None or v0 == v1:
        return None
    slope = (v1 - v0) / span  # kg/day over the window on screen; negative means losing
    gap = goal - v1
    if slope == 0 or (gap > 0) != (slope > 0):
        # the trend is flat, or moving away from the goal rather than towards it
        return {
            "eta": None,
            "on_track": False,
            "caption": "The trend is moving away from the goal, not towards it.",
        }
    days_to_goal = round(gap / slope)
    eta_day = d1 + dt.timedelta(days=days_to_goal) if days_to_goal <= 730 else None
    eta = isoday(eta_day) if eta_day is not None else None
    on_track = goal_date is None or (eta is not None and eta <= goal_date)
    per_week = abs(slope) * 7
    verb = "losing" if slope < 0 else "gaining"
    # Describe distant estimates in text so they cannot distort the chart axis.
    when = f"about {eta_day:%B %Y}" if eta_day is not None else "more than two years away"
    caption = f"{verb.capitalize()} {per_week:.2f} kg a week, the trend reaches {goal:g} kg {when}."
    return {"eta": eta, "on_track": on_track, "caption": caption}


def trend(
    db: Db, person_id: int, metric: str, days: int, today: dt.date, compare: str | None = None
) -> dict[str, Any]:
    if metric not in TREND_METRICS:
        raise ValueError(f"metric must be one of {TREND_METRICS}")
    first = today - dt.timedelta(days=max(1, min(days, 730)))
    lo, hi = isoday(first), isoday(today)
    out: dict[str, Any] = {"metric": metric, "from": lo, "to": hi, "unit": TREND_UNITS[metric]}
    if metric == "weight":
        raw = db.all(
            "SELECT day, weight_kg AS v FROM v_weigh_in_latest WHERE person_id = ? "
            "AND day BETWEEN ? AND ? ORDER BY day",
            (person_id, lo, hi),
        )
        smooth = db.all(
            "SELECT day, trend_kg AS v FROM v_trend_latest WHERE person_id = ? "
            "AND day BETWEEN ? AND ? ORDER BY day",
            (person_id, lo, hi),
        )
        out["dates"] = [str(r["day"]) for r in raw]
        out["raw"] = [_f(r["v"]) for r in raw]
        out["ewma_dates"] = [str(r["day"]) for r in smooth]
        out["ewma"] = [_f(r["v"]) for r in smooth]
        goal = db.one(
            "SELECT goal_weight_kg, goal_date FROM profiles WHERE person_id = ? "
            "AND effective_from <= ? ORDER BY effective_from DESC, id DESC LIMIT 1",
            (person_id, hi),
        )
        out["goal"] = _f(goal["goal_weight_kg"]) if goal is not None else None
        out["goal_date"] = (
            str(goal["goal_date"]) if goal is not None and goal["goal_date"] else None
        )
        out["eta"] = None
        out["projection"] = _weight_projection(out["goal"], out["goal_date"], smooth)
        if out["projection"] is not None:
            out["eta"] = out["projection"]["eta"]
        return out
    if metric == "intake":
        day_rows = db.all(
            "SELECT day, kcal AS v, status FROM v_day_latest WHERE person_id = ? "
            "AND day BETWEEN ? AND ? ORDER BY day",
            (person_id, lo, hi),
        )
        estimate_rows = db.all(
            "SELECT day, SUM(kcal) AS v FROM estimates WHERE person_id = ? "
            "AND day BETWEEN ? AND ? AND status = 'accepted' GROUP BY day",
            (person_id, lo, hi),
        )
        accepted = {str(r["day"]): _f(r["v"]) for r in estimate_rows}
        rows = [dict(r) for r in day_rows]
        known_days = {str(r["day"]) for r in rows}
        rows.extend(
            {"day": d, "v": v, "status": "estimated"}
            for d, v in accepted.items()
            if d not in known_days
        )
        rows.sort(key=lambda r: str(r["day"]))
        out["dates"] = [str(r["day"]) for r in rows]
        out["raw"] = [_f(r["v"]) for r in rows]
        out["status"] = [str(r["status"]) for r in rows]
        targets_rows = db.all(
            "SELECT week_start, kcal FROM v_targets_latest WHERE person_id = ? "
            "AND week_start <= ? ORDER BY week_start",
            (person_id, hi),
        )
        by_week = {str(r["week_start"]): int(r["kcal"]) for r in targets_rows}
        out["target"] = [
            by_week.get(isoday(week_start(dt.date.fromisoformat(d)))) for d in out["dates"]
        ]
        if days >= WIDE_WINDOW_DAYS:
            # a year of eating is 330+ bars, each a pixel wide on a phone (test_09) - averaging
            # them by eye is the arithmetic a person opened this chart to avoid doing, so the
            # rollup is stated once here rather than left for the chart to imply
            # An unconfirmed close can carry the whole plan's kcal. It is not a record of eating.
            counted = []
            for day, value, daily_target, status in zip(
                out["dates"], out["raw"], out["target"], out["status"], strict=True
            ):
                eaten = value if status == "confirmed" else accepted.get(day)
                if eaten is not None:
                    counted.append((eaten, daily_target))
            eaten = [v for v, _ in counted]
            targeted = [t for _, t in counted if t is not None]
            mean_eaten = round(sum(eaten) / len(eaten)) if eaten else None
            mean_target = round(sum(targeted) / len(targeted)) if targeted else None
            out["mean"] = mean_eaten
            out["target_mean"] = mean_target
            out["gap"] = None
            if mean_eaten is not None and mean_target is not None:
                gap = mean_target - mean_eaten
                out["gap"] = gap
                verb = "under" if gap >= 0 else "over"
                out["summary"] = (
                    f"Averaging {mean_eaten} kcal against a {mean_target} kcal target over "
                    f"{len(counted)} days with a record - {abs(gap)} kcal {verb}, on average."
                )
        return out
    if metric == "steps":
        # a row IS kept (the day was read at all - a real "no data" day never reaches this
        # table) but a stored 0 means the watch was on charge, not that nobody walked, so the
        # value is nulled the same way a missing row would be: present on the axis, no dot.
        rows = db.all(
            "SELECT day, CASE WHEN steps > 0 THEN steps ELSE NULL END AS v FROM icu_wellness "
            "WHERE person_id = ? AND day BETWEEN ? AND ? AND steps IS NOT NULL ORDER BY day",
            (person_id, lo, hi),
        )
        # every floor this person has ever set, oldest first, so each date is judged against
        # whichever one was in force on it rather than the single one in force today
        floors = db.all(
            "SELECT effective_from, steps_floor FROM profiles WHERE person_id = ? "
            "AND effective_from <= ? ORDER BY effective_from",
            (person_id, hi),
        )
        out["dates"] = [str(r["day"]) for r in rows]
        out["raw"] = [_f(r["v"]) for r in rows]
        target: list[int | None] = []
        pointer = -1
        for d in out["dates"]:
            while pointer + 1 < len(floors) and str(floors[pointer + 1]["effective_from"]) <= d:
                pointer += 1
            if pointer >= 0:
                target.append(int(floors[pointer]["steps_floor"]))
            elif floors:
                # a date older than the earliest profile row is not a date with "no floor" -
                # it is a date before the ledger started, and the earliest floor we know of is
                # still the best comparison available (phone_layout/06: a person onboarded on
                # 1 Sep with a two-month icu mirror behind them should not read as floor-less
                # for the eleven weeks the mirror covers before that)
                target.append(int(floors[0]["steps_floor"]))
            else:
                target.append(None)
        out["target"] = target
        # "how often do I clear it, and where does the average sit" - the two questions a month
        # of dots against one flat line cannot answer by itself (test_04)
        walked = [v for v in out["raw"] if v is not None]
        out["mean"] = round(sum(walked) / len(walked)) if walked else None
        out["days_over"] = sum(
            1
            for v, t in zip(out["raw"], target, strict=True)
            if v is not None and t is not None and v >= t
        )
        return out
    if metric == "sessions":
        # v_week_summary's own week list comes from v_day_latest (confirmed/closed days), so a
        # week with real training and zero confirmed days produces no row at all - the trend
        # then reads as a week that did not happen. The week list here is a calendar range
        # (every Monday in the window) unioned with any week session_matches actually touches,
        # so an away week with training still gets a bar.
        weeks: list[str] = []
        cursor = week_start(first)
        last_week = week_start(today)
        while cursor <= last_week:
            weeks.append(isoday(cursor))
            cursor += dt.timedelta(days=7)
        trained_weeks = db.all(
            "SELECT DISTINCT date(day, '-' || ((CAST(strftime('%w', day) AS INTEGER) + 6) % 7) "
            "|| ' days') AS week_start FROM session_matches WHERE person_id = ? "
            "AND day BETWEEN ? AND ?",
            (person_id, lo, hi),
        )
        weeks = sorted(set(weeks) | {str(r["week_start"]) for r in trained_weeks})
        known = {
            str(r["week_start"]): r
            for r in db.all(
                "SELECT week_start, sessions_done, sessions_planned FROM v_week_summary "
                "WHERE person_id = ? AND week_start BETWEEN ? AND ?",
                (person_id, lo, hi),
            )
        }
        sessions_raw: list[float | None] = []
        planned: list[float | None] = []
        for w in weeks:
            row = known.get(w)
            if row is not None:
                sessions_raw.append(_f(row["sessions_done"]))
                planned.append(_f(row["sessions_planned"]))
                continue
            # no v_week_summary row (nothing confirmed that week): count sessions_done and
            # sessions_planned the same way done_sessions()/planned_sessions() already do for
            # the Today tab, day by day, rather than leaving a trained week blank
            monday = parse_day(w)
            week_days = [monday + dt.timedelta(days=i) for i in range(7)]
            done = sum(len(done_sessions(db, person_id, d)) for d in week_days)
            due = sum(
                sum(1 for p in planned_sessions(db, person_id, d) if str(p["kind"]) != "rest")
                for d in week_days
            )
            sessions_raw.append(float(done))
            planned.append(float(due))
        out["dates"] = weeks
        out["raw"] = sessions_raw
        out["target"] = planned
        return out
    rows = db.all(
        "SELECT day, cm AS v FROM tape_measures WHERE person_id = ? AND site = ? "
        "AND day BETWEEN ? AND ? ORDER BY day",
        (person_id, metric, lo, hi),
    )
    out["dates"] = [str(r["day"]) for r in rows]
    out["raw"] = [_f(r["v"]) for r in rows]
    # waist and hip are taken in the same breath - one /measure message, two numbers - so the
    # natural partner rides along by default rather than needing a second tab and a memory of
    # the first chart; `compare=` picks a different one, when there ever is one.
    partner = compare or TAPE_COMPARE_DEFAULT.get(metric)
    if partner and partner != metric and partner in TAPE_COMPARE_DEFAULT:
        by_day = {
            str(r["day"]): _f(r["v"])
            for r in db.all(
                "SELECT day, cm AS v FROM tape_measures WHERE person_id = ? AND site = ? "
                "AND day BETWEEN ? AND ? ORDER BY day",
                (person_id, partner, lo, hi),
            )
        }
        out["compare_metric"] = partner
        out["compare_unit"] = TREND_UNITS[partner]
        out["compare"] = [by_day.get(d) for d in out["dates"]]
    return out
