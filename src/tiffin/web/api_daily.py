"""Daily routes for the web page."""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from .. import logbook
from ..clock import isoday, parse_day
from ..db import Db
from . import queries
from .api_common import CONFIRM_BACK_DAYS, _body, _person, _read, _today, _web_event, check_fence

router = APIRouter()


@router.post("/api/today/confirm")
async def confirm_today(request: Request) -> dict[str, Any]:
    """Confirm the day shown on the page, after checking that date."""
    person = _person(request)
    body = await _body(request)
    check_fence(request, body, {"as_planned", "day", "with_open"})
    if not isinstance(body.get("as_planned"), bool):
        raise HTTPException(status_code=400, detail="as_planned must be true or false")
    with_open = body.get("with_open", False)
    if not isinstance(with_open, bool):
        raise HTTPException(status_code=400, detail="with_open must be true or false")
    today = _today(request)
    day = today
    if body.get("day") is not None:
        try:
            day = parse_day(str(body["day"]))
        except ValueError:
            raise HTTPException(status_code=400, detail="day must be YYYY-MM-DD") from None
    if day > today:
        raise HTTPException(status_code=400, detail="That day has not happened yet.")
    if (today - day).days > CONFIRM_BACK_DAYS:
        raise HTTPException(status_code=400, detail="That day is too far back to change now.")
    tiffin = request.app.state.tiffin

    def write(db: Db) -> dict[str, Any]:
        from .. import days as days_mod  # noqa: PLC0415 (days imports the food layer)
        from ..jobs import sunday_summary  # noqa: PLC0415 (jobs import the scheduler)

        with db.tx():
            # the earlier days still open, found again here, never taken from the body: one tap
            # answers the week, and only days nobody has answered
            earlier = (
                [
                    d
                    for d in days_mod.unanswered_days(db, person.id, day)
                    if (today - d).days <= CONFIRM_BACK_DAYS
                ]
                if with_open and body["as_planned"]
                else []
            )
            for d in earlier:
                queries.confirm_today(db, person.id, d, True)
            out = queries.confirm_today(db, person.id, day, bool(body["as_planned"]))
            if out is None:
                raise HTTPException(
                    status_code=400,
                    detail="Tell the bot what changed and it will record the day for you.",
                )
            out["also"] = [isoday(d) for d in earlier]
            _web_event(
                db,
                person.id,
                "day_confirm",
                {"day": out["day"], "already": out["already"], "also": out["also"]},
            )
            # and the week's review on the Summary tab says so, not what it said at 19:00
            sunday_summary.refresh_review(
                db, tiffin.config, tiffin.registry(db), person, [*earlier, day]
            )
        return out

    return await _read(request, write)


def _recent_day(request: Request, body: Mapping[str, Any]) -> dt.date:
    """The day a page record is about: today or up to a week back, never the future - the same
    window "Record as planned" keeps."""
    today = _today(request)
    raw = body.get("day")
    try:
        day = parse_day(str(raw)) if raw else today
    except ValueError:
        raise HTTPException(status_code=400, detail="day must be YYYY-MM-DD") from None
    if day > today:
        raise HTTPException(status_code=400, detail="That day has not happened yet.")
    if (today - day).days > CONFIRM_BACK_DAYS:
        raise HTTPException(status_code=400, detail="That day is too far back to change now.")
    return day


@router.post("/api/today/different")
async def today_different(request: Request) -> dict[str, Any]:
    """
    counted for that day, the rest of the day left as it is (logbook.record_different)."""
    person = _person(request)
    body = await _body(request)
    check_fence(request, body, {"day", "slot", "text"})
    text = body.get("text")
    if not isinstance(text, str) or not text.strip() or len(text) > logbook.MAX_TEXT:
        raise HTTPException(status_code=400, detail="Say what you ate, in a few words.")
    slot = body.get("slot")
    day = _recent_day(request, body)
    tiffin = request.app.state.tiffin

    def write(db: Db) -> dict[str, Any]:
        from .. import days as days_mod  # noqa: PLC0415 (days imports the food layer)

        if (
            not isinstance(slot, str)
            or days_mod.planned_slot_macros(db, person.id, day, slot) is None
        ):
            raise HTTPException(status_code=400, detail="That meal is not on that day.")
        out = logbook.record_different(tiffin, db, person, day, slot, text)
        if out["ok"]:
            _web_event(db, person.id, "ate_different", {"day": isoday(day), "slot": slot})
        return out

    return await _read(request, write)


@router.post("/api/today/session")
async def today_session(request: Request) -> dict[str, Any]:
    """A training session she did, or a planned one skipped, recorded on the page."""
    person = _person(request)
    body = await _body(request)
    check_fence(request, body, {"day", "minutes", "done"})
    done = body.get("done")
    if not isinstance(done, bool):
        raise HTTPException(status_code=400, detail="done must be true or false")
    minutes = body.get("minutes")
    if minutes is not None and (
        not isinstance(minutes, int) or isinstance(minutes, bool) or not 1 <= minutes <= 600
    ):
        raise HTTPException(status_code=400, detail="minutes is 1 to 600")
    day = _recent_day(request, body)

    def write(db: Db) -> dict[str, Any]:
        said = logbook.record_session(db, person, day, minutes=minutes, done=done)
        _web_event(db, person.id, "session", {"day": isoday(day), "done": done})
        return {"said": said}

    return await _read(request, write)


@router.post("/api/cycle/start")
async def cycle_start(request: Request) -> dict[str, Any]:
    """
    day"); the phase is worked out from it (body.cycle_on). Up to six weeks back."""
    person = _person(request)
    body = await _body(request)
    check_fence(request, body, {"day"})
    today = _today(request)
    try:
        day = parse_day(str(body.get("day") or ""))
    except ValueError:
        raise HTTPException(status_code=400, detail="day must be YYYY-MM-DD") from None
    if day > today or (today - day).days > 42:
        raise HTTPException(status_code=400, detail="A day in the last six weeks, please.")

    def write(db: Db) -> dict[str, Any]:
        from .. import body as body_mod  # noqa: PLC0415

        with db.tx():
            body_mod.record_period_start(db, person.id, day)
            _web_event(db, person.id, "period_start", {})
        return {"cycle": queries.cycle(db, person.id, today)}

    return await _read(request, write)


@router.post("/api/rhythm")
async def rhythm(request: Request) -> dict[str, Any]:
    """How often the bot messages this person: light, daily or weekly (migration 0035). Each
    person chooses their own; the header says who, never the body."""
    person = _person(request)
    body = await _body(request)
    check_fence(request, body, {"rhythm"})
    wanted = body.get("rhythm")
    if wanted not in queries.RHYTHMS:
        raise HTTPException(status_code=400, detail="rhythm is light, daily or weekly")
    today = _today(request)

    def write(db: Db) -> dict[str, Any]:
        with db.tx():
            if not queries.set_rhythm(db, person.id, str(wanted), today):
                raise HTTPException(status_code=409, detail="Finish /onboard in Telegram first.")
            _web_event(db, person.id, "rhythm", {"rhythm": wanted})
        return {"rhythm": wanted, "said": RHYTHM_SAID[str(wanted)]}

    return await _read(request, write)


#: what each rhythm means, said back when it is chosen
RHYTHM_SAID = {
    "light": "Light: the evening check-in each day, and a morning message only when the day's "
    "food is new.",
    "daily": "Daily: a morning message and the evening check-in, every day.",
    "weekly": "Weekly: one check-in on Sunday evening for the whole week, and a morning "
    "message only when the day's food is new.",
}
