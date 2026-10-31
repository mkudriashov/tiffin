"""Progress history, captions and chart data for one signed-in person."""

from __future__ import annotations

import datetime as dt
from typing import Any

from ..clock import isoday, parse_day, week_start
from ..db import Db
from .queries import (
    _f,
    done_sessions,
    planned_sessions,
    profile,
    targets,
    trend,
)


def _progress_mean(values: list[float | int]) -> float | None:
    return sum(values) / len(values) if len(values) >= 3 else None


def _progress_day_name(day: dt.date) -> str:
    return f"{day:%a} {day.day} {day:%b}"


def _progress_weight_caption(
    raw_dates: list[str],
    smooth_dates: list[str],
    smooth: list[float | None],
    goal: float | None,
    weeks: int,
) -> tuple[float | None, float | None, str]:
    if len(raw_dates) < 3 or len(smooth_dates) < 2 or smooth[0] is None or smooth[-1] is None:
        last = _progress_day_name(parse_day(raw_dates[-1])) if raw_dates else "none yet"
        return None, None, f"3 weigh-ins are needed for a trend; the last was {last}."
    change = round(smooth[-1] - smooth[0], 1)
    per_week = round(change / weeks, 2)
    direction = "Down" if change < 0 else "Up" if change > 0 else "Unchanged"
    caption = (
        f"{direction} {abs(change):.1f} kg over {weeks} weeks, about {abs(per_week):.2f} kg a week."
    )
    if goal is not None:
        slope = (smooth[-1] - smooth[0]) / max(
            1, (parse_day(smooth_dates[-1]) - parse_day(smooth_dates[0])).days
        )
        gap = goal - smooth[-1]
        if slope == 0 or (gap > 0) != (slope > 0):
            caption += " The trend is not moving towards the goal."
        else:
            days = round(gap / slope)
            if days > 730:
                caption += " At this rate the goal is more than two years away."
            else:
                arrival = parse_day(smooth_dates[-1]) + dt.timedelta(days=days)
                caption += f" At this rate the goal is about {arrival:%B %Y}."
    return change, per_week, caption


def progress(
    db: Db, person_id: int, weeks: int, week_day: dt.date, today: dt.date
) -> dict[str, Any]:
    """Read one person's measured history and the menu actually in force on each day."""
    first = today - dt.timedelta(days=weeks * 7 - 1)
    monday = week_start(week_day)
    sunday = monday + dt.timedelta(days=6)
    current_monday = week_start(today)
    days = [monday + dt.timedelta(days=i) for i in range(7)]
    span_first, span_last = min(first, monday), max(today, sunday)

    menu_rows = db.all(
        "SELECT day, id, kcal, protein_g, fibre_g FROM v_menu_days_current "
        "WHERE person_id = ? AND day BETWEEN ? AND ? ORDER BY day",
        (person_id, isoday(span_first), isoday(span_last)),
    )
    menus = {str(r["day"]): dict(r) for r in menu_rows}
    records = {
        str(r["day"]): dict(r)
        for r in db.all(
            "SELECT day, status, kcal FROM v_day_latest WHERE person_id = ? "
            "AND day BETWEEN ? AND ?",
            (person_id, isoday(monday), isoday(sunday)),
        )
    }
    accepted = {
        str(r["day"]): round(float(r["kcal"]))
        for r in db.all(
            "SELECT day, SUM(kcal) AS kcal FROM estimates WHERE person_id = ? "
            "AND day BETWEEN ? AND ? AND status = 'accepted' GROUP BY day",
            (person_id, isoday(monday), isoday(sunday)),
        )
    }
    weight_trend = trend(db, person_id, "weight", weeks * 7 - 1, today)
    steps_trend = trend(db, person_id, "steps", weeks * 7 - 1, today)
    session_trend = trend(db, person_id, "sessions", weeks * 7 - 1, today)
    weights = dict(zip(weight_trend["dates"], weight_trend["raw"], strict=True))
    step_rows = dict(zip(steps_trend["dates"], steps_trend["raw"], strict=True))
    step_floors = dict(zip(steps_trend["dates"], steps_trend["target"], strict=True))
    # The shown week can be older than the chart window. Read its watch days separately then.
    if monday < first:
        extra = trend(db, person_id, "steps", (today - monday).days, today)
        step_rows.update(zip(extra["dates"], extra["raw"], strict=True))
        step_floors.update(zip(extra["dates"], extra["target"], strict=True))
        for r in db.all(
            "SELECT day, weight_kg FROM v_weigh_in_latest WHERE person_id = ? "
            "AND day BETWEEN ? AND ?",
            (person_id, isoday(monday), isoday(sunday)),
        ):
            weights[str(r["day"])] = _f(r["weight_kg"])
    for day in days:
        key = isoday(day)
        if day <= today and key not in step_floors:
            current_profile = profile(db, person_id, day)
            step_floors[key] = (
                int(current_profile["steps_floor"]) if current_profile is not None else None
            )

    wellness_rows = db.all(
        "SELECT day, resting_hr, hrv, sleep_s FROM icu_wellness WHERE person_id = ? "
        "AND day BETWEEN ? AND ? ORDER BY day",
        (person_id, isoday(first), isoday(today)),
    )
    recovery = {str(r["day"]): dict(r) for r in wellness_rows}
    activities = db.all(
        "SELECT day, kind, name, moving_s FROM icu_activity WHERE person_id = ? "
        "AND day BETWEEN ? AND ? ORDER BY day",
        (person_id, isoday(span_first), isoday(today)),
    )
    by_activity: dict[str, list[str]] = {}
    for r in activities:
        minutes = round(int(r["moving_s"] or 0) / 60)
        label = str(r["kind"] or "activity")
        by_activity.setdefault(str(r["day"]), []).append(f"{label} {minutes} min")

    shown: list[dict[str, Any]] = []
    for day in days:
        key = isoday(day)
        menu = menus.get(key)
        record = records.get(key)
        status = str(record["status"]) if record else None
        recorded_kcal = None
        if record is not None and status == "confirmed" and record["kcal"] is not None:
            recorded_kcal = round(float(record["kcal"]))
        elif record is None:
            recorded_kcal = accepted.get(key)
        planned = [
            f"{p['kind']} {p['target_minutes']} min"
            for p in planned_sessions(db, person_id, day)
            if str(p["kind"]) != "rest"
        ]
        shown.append(
            {
                "day": key,
                "planned_kcal": int(menu["kcal"]) if menu else None,
                "planned_protein_g": round(float(menu["protein_g"])) if menu else None,
                "recorded": (
                    {
                        "confirmed": "recorded",
                        "unconfirmed": "not answered",
                        "paused": "paused",
                    }.get(status or "")
                    or ("recorded" if recorded_kcal is not None else None)
                ),
                # An unanswered close carries planned kcal; only an answer or accepted estimate
                # can supply a recorded figure.
                "recorded_kcal": recorded_kcal,
                "steps": round(step_rows[key])
                if key in step_rows and step_rows[key] is not None
                else None,
                "steps_floor": step_floors.get(key) if day <= today else None,
                "trained": by_activity.get(key, []) if day <= today else [],
                "planned_training": planned,
                "weight_kg": weights.get(key) if day <= today else None,
                "future": day > today,
            }
        )

    raw_dates = weight_trend["dates"]
    smooth_dates = weight_trend["ewma_dates"]
    smooth = weight_trend["ewma"]
    goal = weight_trend["goal"]
    change, per_week, weight_caption = _progress_weight_caption(
        raw_dates, smooth_dates, smooth, goal, weeks
    )
    last_weight_day = raw_dates[-1] if raw_dates else None
    weight_card = (
        {
            "trend_kg": round(smooth[-1], 1) if smooth and smooth[-1] is not None else None,
            "change_kg": change,
            "per_week_kg": per_week,
            "weigh_ins": len(raw_dates),
            "last_day": last_weight_day,
            "days_since": (today - parse_day(last_weight_day)).days if last_weight_day else None,
            "goal_kg": goal,
        }
        if raw_dates
        else None
    )
    weight_chart = (
        {
            "dates": raw_dates,
            "raw": weight_trend["raw"],
            "trend_dates": smooth_dates,
            "trend": smooth,
            "goal_kg": goal,
            "caption": weight_caption,
        }
        if raw_dates
        else None
    )

    recent = today - dt.timedelta(days=6)
    recent_steps = [
        (d, v, step_floors.get(d))
        for d, v in step_rows.items()
        if d >= isoday(recent) and d <= isoday(today) and v is not None
    ]
    mean_steps = _progress_mean([v for _, v, _ in recent_steps])
    steps_card = (
        {
            "avg": round(mean_steps) if mean_steps is not None else None,
            "floor": step_floors.get(isoday(today))
            or (recent_steps[-1][2] if recent_steps else None),
            "days_over": sum(1 for _, v, floor in recent_steps if floor is not None and v >= floor),
            "days_read": len(recent_steps),
        }
        if recent_steps
        else None
    )
    step_dates = steps_trend["dates"]
    rolling = []
    for key in step_dates:
        end = parse_day(key)
        values = [
            v
            for d, v in step_rows.items()
            if isoday(end - dt.timedelta(days=6)) <= d <= key and v is not None
        ]
        avg = _progress_mean(values)
        rolling.append(round(avg) if avg is not None else None)
    steps_chart = (
        {
            "dates": step_dates,
            "raw": steps_trend["raw"],
            "avg7": rolling,
            "floor": steps_trend["target"],
            "caption": _progress_steps_caption(
                steps_card, _progress_last_reading(db, person_id, today)
            ),
        }
        if recent_steps or any(v is not None for v in steps_trend["raw"])
        else None
    )

    week_starts = [
        isoday(first_monday)
        for first_monday in (
            week_start(first) + dt.timedelta(weeks=i)
            for i in range((week_start(today) - week_start(first)).days // 7 + 1)
        )
    ]
    minutes_by_kind: dict[str, list[int]] = {}
    for r in activities:
        day = parse_day(str(r["day"]))
        if day < first:
            continue
        kind = str(r["kind"] or "other")
        bucket = minutes_by_kind.setdefault(kind, [0] * len(week_starts))
        bucket[week_starts.index(isoday(week_start(day)))] += int(r["moving_s"] or 0)
    minutes_by_kind = {
        kind: [round(seconds / 60) for seconds in values]
        for kind, values in minutes_by_kind.items()
    }
    session_counts = dict(zip(session_trend["dates"], session_trend["raw"], strict=True))
    planned_counts = dict(zip(session_trend["dates"], session_trend["target"], strict=True))
    chart_done = [round(session_counts.get(w) or 0) for w in week_starts]
    chart_planned = [round(planned_counts.get(w) or 0) for w in week_starts]
    week_minutes = round(
        sum(
            int(r["moving_s"] or 0)
            for r in activities
            if monday <= parse_day(str(r["day"])) <= sunday
        )
        / 60
    )
    training_card = (
        {
            "done": sum(len(done_sessions(db, person_id, d)) for d in days if d <= today),
            "planned": sum(
                len(planned_sessions(db, person_id, d))
                - sum(str(p["kind"]) == "rest" for p in planned_sessions(db, person_id, d))
                for d in days
            ),
            "minutes": week_minutes,
        }
        if week_minutes or any(planned_sessions(db, person_id, d) for d in days)
        else None
    )
    training_chart = (
        {
            "weeks": week_starts,
            "minutes_by_kind": minutes_by_kind,
            "done": chart_done,
            "planned": chart_planned,
            "caption": _progress_training_caption(training_card, weeks, minutes_by_kind),
        }
        if minutes_by_kind or any(chart_planned)
        else None
    )

    recovery_dates = sorted(recovery)
    hrs = [
        round(float(recovery[d]["sleep_s"]) / 3600, 1)
        if recovery[d]["sleep_s"] is not None and recovery[d]["sleep_s"] > 0
        else None
        for d in recovery_dates
    ]
    heart = [_f(recovery[d]["resting_hr"]) for d in recovery_dates]
    hrv = [_f(recovery[d]["hrv"]) for d in recovery_dates]
    recent_sleep = [
        h for d, h in zip(recovery_dates, hrs, strict=True) if d >= isoday(recent) and h is not None
    ]
    sleep_mean = _progress_mean(recent_sleep)
    sleep_card = (
        {
            "avg_h": round(sleep_mean, 1) if sleep_mean is not None else None,
            "days_read": len(recent_sleep),
        }
        if recent_sleep
        else None
    )

    def last_week_average(series: list[float | None], start: dt.date, end: dt.date) -> float | None:
        values = [
            v
            for d, v in zip(recovery_dates, series, strict=True)
            if isoday(start) <= d <= isoday(end) and v is not None
        ]
        return _progress_mean(values)

    hr_now = last_week_average(heart, recent, today)
    hr_before = last_week_average(
        heart, recent - dt.timedelta(days=7), recent - dt.timedelta(days=1)
    )
    hrv_now = last_week_average(hrv, recent, today)
    hrv_before = last_week_average(
        hrv, recent - dt.timedelta(days=7), recent - dt.timedelta(days=1)
    )
    recovery_card = (
        {
            "resting_hr": round(hr_now) if hr_now is not None else None,
            "resting_hr_change": round(hr_now - hr_before)
            if hr_now is not None and hr_before is not None
            else None,
            "hrv": round(hrv_now) if hrv_now is not None else None,
            "hrv_change": round(hrv_now - hrv_before)
            if hrv_now is not None and hrv_before is not None
            else None,
        }
        if hr_now is not None or hrv_now is not None
        else None
    )
    recovery_chart = (
        {
            "dates": recovery_dates,
            "resting_hr": heart,
            "hrv": hrv,
            "sleep_h": hrs,
            "caption": _progress_recovery_caption(
                recovery_card, sleep_card, _progress_last_reading(db, person_id, today)
            ),
        }
        if any(v is not None for v in heart + hrv + hrs)
        else None
    )

    week_menus = [menus.get(isoday(d)) for d in days]
    planned_days = [m for m in week_menus if m is not None]
    week_targets = [targets(db, person_id, d) for d in days]
    target = week_targets[-1]
    plan_card = (
        {
            "avg_kcal": round(sum(int(m["kcal"]) for m in planned_days) / len(planned_days)),
            "target_kcal": int(target["kcal"]) if target else None,
            "avg_protein_g": round(
                sum(float(m["protein_g"]) for m in planned_days) / len(planned_days)
            ),
            "protein_floor_g": round(float(target["protein_g"])) if target else None,
            "avg_fibre_g": round(
                sum(float(m["fibre_g"]) for m in planned_days) / len(planned_days)
            ),
            "fibre_floor_g": round(float(target["fibre_g"]))
            if target and target["fibre_g"] is not None
            else None,
            "days_planned": len(planned_days),
        }
        if planned_days
        else None
    )
    plan_dates = [
        isoday(d)
        for d in (first + dt.timedelta(days=i) for i in range((today - first).days + 1))
        if isoday(d) in menus
    ]
    plan_kcal = [int(menus[d]["kcal"]) for d in plan_dates]
    plan_protein = [round(float(menus[d]["protein_g"])) for d in plan_dates]
    plan_targets = [targets(db, person_id, parse_day(d)) for d in plan_dates]
    plan_caption = ""
    if plan_card:
        target_phrase = (
            f" against a {plan_card['target_kcal']:,} target"
            if plan_card["target_kcal"] is not None
            else ""
        )
        which = "This week" if monday == current_monday else "That week"
        plan_caption = (
            f"{which}: planned {plan_card['avg_kcal']:,} kcal a day on average{target_phrase}."
        )
        protein_floor = plan_card["protein_floor_g"]
        if protein_floor is not None:
            count = sum(float(m["protein_g"]) >= protein_floor for m in planned_days)
            plan_caption += (
                f" Protein at least {protein_floor} g was planned on "
                f"{count} of {len(planned_days)} days."
            )
        fibre_floor = plan_card["fibre_floor_g"]
        if fibre_floor is not None:
            count = sum(float(m["fibre_g"]) >= fibre_floor for m in planned_days)
            plan_caption += (
                f" Fibre at least {fibre_floor} g was planned on "
                f"{count} of {len(planned_days)} days."
            )
    plan_chart = (
        {
            "dates": plan_dates,
            "kcal": plan_kcal,
            "protein_g": plan_protein,
            "target_kcal": [int(t["kcal"]) if t else None for t in plan_targets],
            "protein_floor_g": [round(float(t["protein_g"])) if t else None for t in plan_targets],
            "caption": plan_caption,
        }
        if plan_dates
        else None
    )

    dish_rows = db.all(
        "SELECT DISTINCT rc.id, rc.name FROM v_menu_days_current md "
        "JOIN meal_slots ms ON ms.menu_day_id = md.id JOIN recipes rc ON rc.id = ms.recipe_id "
        "WHERE md.person_id = ? AND md.day BETWEEN ? AND ?",
        (person_id, isoday(first), isoday(today)),
    )
    # the latest rating of each dish by NAME, as queries.dishes() keys them: a repeated menu
    # writes a fresh recipes row every time, so a rating keyed by recipe id went missing the
    # next week (food/batch.py)
    latest_by_name: dict[str, tuple[int, int]] = {}
    for r in db.all(
        "SELECT rc.name, rt.score, rt.id FROM ratings rt JOIN recipes rc ON rc.id = rt.recipe_id "
        "WHERE rt.person_id = ? ORDER BY rt.id",
        (person_id,),
    ):
        latest_by_name[str(r["name"])] = (int(r["score"]), int(r["id"]))
    on_menu = {str(r["name"]) for r in dish_rows}
    rated_dishes = sorted(
        (
            {"name": name, "score": latest_by_name[name][0], "id": latest_by_name[name][1]}
            for name in on_menu
            if name in latest_by_name
        ),
        key=lambda r: (-r["score"], -r["id"]),
    )
    food_card = (
        {
            "dishes": len({str(r["name"]) for r in dish_rows}),
            "rated": len(rated_dishes),
            "top": [{"name": r["name"], "score": r["score"]} for r in rated_dishes[:3]],
        }
        if dish_rows
        else None
    )
    elapsed = sum(d <= today for d in days)
    recorded_values = [d["recorded_kcal"] for d in shown if d["recorded_kcal"] is not None]
    recorded_avg = (
        round(sum(recorded_values) / len(recorded_values)) if len(recorded_values) >= 3 else None
    )
    # Show the last reading separately from sync status; a successful sync may bring no data.
    watch_last = db.scalar(
        "SELECT MAX(day) FROM icu_wellness WHERE person_id = ? AND day <= ? AND (steps > 0 "
        "OR sleep_s IS NOT NULL OR hrv IS NOT NULL OR resting_hr IS NOT NULL)",
        (person_id, isoday(today)),
    )
    activity_last = db.scalar(
        "SELECT MAX(day) FROM icu_activity WHERE person_id = ? AND day <= ?",
        (person_id, isoday(today)),
    )
    # a watch counts as one that went quiet only if it was sending: a week of readings in the
    # four weeks before the last one. One heart-rate reading from a borrowed watch is not a feed
    # that stopped (her page said "no readings since 21 Sep" over a single reading)
    watch_regular = (
        bool(watch_last)
        and int(
            db.scalar(
                "SELECT COUNT(*) FROM icu_wellness WHERE person_id = ? AND day BETWEEN ? AND ? "
                "AND (steps > 0 OR sleep_s IS NOT NULL OR hrv IS NOT NULL "
                "OR resting_hr IS NOT NULL)",
                (
                    person_id,
                    isoday(parse_day(str(watch_last)) - dt.timedelta(days=27)),
                    str(watch_last),
                ),
            )
            or 0
        )
        >= 7
    )
    sync = {
        "watch_regular": watch_regular,
        "watch_last": str(watch_last) if watch_last else None,
        "watch_days": (today - parse_day(str(watch_last))).days if watch_last else None,
        "activity_last": str(activity_last) if activity_last else None,
        "activity_days": (today - parse_day(str(activity_last))).days if activity_last else None,
    }
    return {
        "from": isoday(first),
        "to": isoday(today),
        "weeks": weeks,
        "week": {
            "start": isoday(monday),
            "end": isoday(sunday),
            "is_current": monday == current_monday,
            "prev": isoday(monday - dt.timedelta(days=7)),
            "next": isoday(monday + dt.timedelta(days=7)) if monday < current_monday else None,
        },
        "cards": {
            "weight": weight_card,
            "steps": steps_card,
            "training": training_card,
            "sleep": sleep_card,
            "recovery": recovery_card,
            "plan": plan_card,
            "recorded": {
                "days": sum(
                    records.get(d["day"], {}).get("status") == "confirmed"
                    for d in shown
                    if not d["future"]
                ),
                "of": elapsed,
                # what the recorded days averaged, when there are enough of them to mean anything:
                # a week fully answered is judged by this one number (week_tab/03)
                "avg_kcal": recorded_avg,
                "target_kcal": int(target["kcal"]) if target else None,
            },
            "food": food_card,
        },
        "days": shown,
        "sync": sync,
        "charts": {
            "weight": weight_chart,
            "steps": steps_chart,
            "training": training_chart,
            "recovery": recovery_chart,
            "plan": plan_chart,
        },
    }


def _progress_last_reading(db: Db, person_id: int, today: dt.date) -> str | None:
    """The day the watch last sent anything (steps, sleep, heart), as a person reads a day."""
    last = db.scalar(
        "SELECT MAX(day) FROM icu_wellness WHERE person_id = ? AND day <= ? AND (steps > 0 "
        "OR sleep_s IS NOT NULL OR hrv IS NOT NULL OR resting_hr IS NOT NULL)",
        (person_id, isoday(today)),
    )
    return _progress_day_name(parse_day(str(last))) if last else None


def _progress_steps_caption(card: dict[str, Any] | None, last: str | None = None) -> str:
    """The last seven days in one sentence: the average and the days at or over the floor."""
    if card is None:
        return (
            f"No readings in the last 7 days; the last was {last}."
            if last
            else ("No readings in the last 7 days.")
        )
    if card["avg"] is None:
        return "Fewer than three readings in the last 7 days: no average yet."
    floor = (
        f"; at or over the {card['floor']:,} floor on {card['days_over']} of them"
        if card["floor"]
        else ""
    )
    return (
        f"Averaging {card['avg']:,} steps over the last 7 days, from "
        f"{card['days_read']} readings{floor}."
    )


def _progress_training_caption(
    card: dict[str, Any] | None, weeks: int, minutes_by_kind: dict[str, list[int]]
) -> str:
    """This week against its plan, then the window's moving time."""
    total = sum(sum(v) for v in minutes_by_kind.values())
    hours = f"{total // 60} h {total % 60} min" if total >= 60 else f"{total} min"
    window = f" {hours} moving over {weeks} weeks." if total else ""
    if card is None:
        return window.strip() or "No training recorded or planned in this window."
    return f"This week {card['done']} of {card['planned']} planned sessions.{window}"


def _progress_recovery_caption(
    card: dict[str, Any] | None, sleep: dict[str, Any] | None, last: str | None = None
) -> str:
    """The last seven days' averages, each against the seven before where both are known."""

    def change(value: int | None) -> str:
        if value is None:
            return ""
        return " (same as the week before)" if value == 0 else f" ({value:+d} on the week before)"

    parts = []
    if card is not None and card["resting_hr"] is not None:
        parts.append(
            f"resting heart rate {card['resting_hr']} bpm{change(card['resting_hr_change'])}"
        )
    if card is not None and card["hrv"] is not None:
        parts.append(f"HRV {card['hrv']} ms{change(card['hrv_change'])}")
    if sleep is not None and sleep["avg_h"] is not None:
        parts.append(f"sleep {sleep['avg_h']} h a night")
    if not parts:
        return (
            f"No readings in the last 7 days; the last was {last}."
            if last
            else ("Readings from the watch, as they arrive.")
        )
    return "Last 7 days: " + ", ".join(parts) + "."
