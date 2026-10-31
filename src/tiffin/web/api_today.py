"""Today routes for the web page."""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from ..clock import isoday, meal_now, parse_day, week_start
from ..db import Db
from . import queries
from .api_common import CONFIRM_BACK_DAYS, _day_arg, _person, _read, _today

router = APIRouter()


@router.get("/api/today")
async def today(request: Request) -> dict[str, Any]:
    person = _person(request)
    tiffin = request.app.state.tiffin
    today_date = _today(request)
    day = _day_arg(request.query_params.get("date"), today_date)
    # a bare `wellness: null` covers two different facts - no intervals.icu key at all (steps
    # are never counted for this person) vs a key that simply has not synced yet today - and
    # the Steps card cannot tell them apart, or say which, without this flag alongside it
    has_icu_key = bool(tiffin.secrets.icu_key(person.slug))
    # the bot has known "bare /recipe means whichever meal it is now" for months; the tab that
    # opens by default listed the day's meals with nothing marking the one to eat next - only
    # for the real day the clock is on, never for a `?date=` look back at yesterday
    next_slot = meal_now(tiffin.clock.now_local()) if day == today_date else None

    def read(db: Db) -> dict[str, Any]:
        return {
            "day": isoday(day),
            "next_slot": next_slot,
            "targets": queries.targets(db, person.id, day),
            "record": queries.day_record(db, person.id, day),
            "estimates": queries.estimates(db, person.id, day),
            "trend": queries.trend_point(db, person.id, day),
            "weigh_in": queries.weigh_in(db, person.id, day),
            "wellness": queries.wellness(db, person.id, day),
            "steps_last": queries.steps_last(db, person.id, day),
            "activity_last": queries.activity_last(db, person.id, day),
            "has_icu_key": has_icu_key,
            "profile": queries.profile(db, person.id, day),
            "planned_session": queries.planned_session(db, person.id, day),
            "planned_sessions": queries.planned_sessions(db, person.id, day),
            "sessions": queries.done_sessions(db, person.id, day),
            "meals": queries.meals(db, person.id, day),
            # what goes in the freezer tonight: a pack of meat or fish opened two days ago that a
            # later day still cooks from (food/packs.py FRESH_OPEN_DAYS)
            "freeze": packs_mod.freeze_tonight(db, person.id, day) if day == today_date else [],
            # the days before this one still unanswered: one tap records them with it. Only the
            # ones the tap can still record (the same window /api/today/confirm keeps): looking
            # back at a past day, "All 5 days" recorded three and the other two stayed open with
            # nothing on the page saying so
            "open_days": [
                isoday(d)
                for d in days_mod.unanswered_days(db, person.id, day)
                if (today_date - d).days <= CONFIRM_BACK_DAYS
            ],
            # where the day falls in her cycle, worked out from the period starts she enters
            "cycle": queries.cycle(db, person.id, day),
        }

    from .. import days as days_mod  # noqa: PLC0415 (days imports the food layer)
    from ..food import packs as packs_mod  # noqa: PLC0415 (food imports the db layer)

    return await _read(request, read)


@router.get("/api/week")
async def week(request: Request) -> dict[str, Any]:
    person = _person(request)
    start = _day_arg(request.query_params.get("start"), _today(request))
    return await _read(request, queries.week, person.id, start)


@router.get("/api/trends")
async def trends(request: Request) -> dict[str, Any]:
    person = _person(request)
    metric = request.query_params.get("metric", "weight")
    if metric not in queries.TREND_METRICS:
        raise HTTPException(status_code=400, detail="unknown metric")
    try:
        days = int(request.query_params.get("days", "90"))
    except ValueError:
        raise HTTPException(status_code=400, detail="bad days") from None
    compare = request.query_params.get("compare")
    if compare is not None and compare not in ("waist", "hip"):
        raise HTTPException(status_code=400, detail="unknown compare metric")
    return await _read(request, queries.trend, person.id, metric, days, _today(request), compare)


@router.get("/api/measurements")
async def measurements(request: Request) -> dict[str, Any]:
    """The latest of each tape measure, how it is taken, and what it reads as (sizing.py)."""
    person = _person(request)
    from .. import sizing  # noqa: PLC0415

    return await _read(request, sizing.card, person.id)


@router.get("/api/progress")
async def progress(request: Request) -> dict[str, Any]:
    person = _person(request)
    try:
        weeks = int(request.query_params.get("weeks", "8"))
    except ValueError:
        raise HTTPException(status_code=422, detail="weeks must be 4, 8, 12 or 26") from None
    if weeks not in (4, 8, 12, 26):
        raise HTTPException(status_code=422, detail="weeks must be 4, 8, 12 or 26")
    today = _today(request)
    value = request.query_params.get("week")
    try:
        if value is not None and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError(value)
        week_day = parse_day(value) if value is not None else today
    except ValueError:
        raise HTTPException(status_code=422, detail="week must be an ISO date") from None
    if week_start(week_day) > week_start(today):
        raise HTTPException(status_code=422, detail="week cannot be in the future")
    return await _read(request, queries.progress, person.id, weeks, week_day, today)
