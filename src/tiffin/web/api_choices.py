"""Choices routes for the web page."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from ..clock import parse_day
from ..db import Db
from ..errors import NothingToBuild
from .api_common import _body, _person, _read, _web_event, check_fence

router = APIRouter()


@router.get("/api/choices")
async def choices_view(request: Request) -> dict[str, Any]:
    """Weekly choices (food/choices.py): whether they are on, the week being chosen or chosen,
    and what can be asked for. Strictly the person the header names."""
    from ..food import choices as choices_mod  # noqa: PLC0415 (food imports the db layer)

    person = _person(request)
    tiffin = request.app.state.tiffin

    def read(db: Db) -> dict[str, Any]:
        return choices_mod.view(tiffin, db, person.id, clock=tiffin.clock)

    return await _read(request, read)


def _choice_int(body: Mapping[str, Any], key: str) -> int:
    value = body.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise HTTPException(status_code=400, detail=f"{key} is a whole number")
    return value


@router.post("/api/choices/pick")
async def choices_pick(request: Request) -> dict[str, Any]:
    """One meal's pick for one day of the week being chosen; the day comes back recomputed."""
    from .. import targets as targets_mod  # noqa: PLC0415
    from ..food import choices as choices_mod  # noqa: PLC0415 (food imports the db layer)
    from ..food import solver  # noqa: PLC0415

    person = _person(request)
    body = await _body(request)
    check_fence(request, body, {"set_id", "day", "slot", "option_id"})
    set_id = _choice_int(body, "set_id")
    option_id = _choice_int(body, "option_id")
    day, slot = body.get("day"), body.get("slot")
    if not isinstance(day, str) or not isinstance(slot, str):
        raise HTTPException(status_code=400, detail="day and slot are named")
    tiffin = request.app.state.tiffin

    def write(db: Db) -> dict[str, Any]:
        try:
            row = choices_mod.pick(db, person.id, set_id, option_id, clock=tiffin.clock)
        except LookupError as e:
            raise HTTPException(status_code=404, detail=str(e)) from None
        except NothingToBuild as e:
            raise HTTPException(status_code=409, detail=str(e)) from None
        if str(row["day"]) != day or str(row["slot"]) != slot:
            raise HTTPException(status_code=400, detail="That dish is for another meal.")
        _web_event(db, person.id, "choice_pick", {"slot": slot})
        sets = db.one("SELECT * FROM choice_sets WHERE id = ?", (set_id,))
        assert sets is not None  # the pick above found its option in this set
        options = choices_mod.options_of(db, set_id)
        picks = choices_mod.picks_of(db, set_id)
        first = parse_day(str(sets["first_day"]))
        target_row = targets_mod.ensure_targets(
            db, person.id, first, tiffin.config, clock=tiffin.clock
        )
        target = targets_mod.macros_of(target_row) if target_row is not None else None
        profile = targets_mod.profile_on(db, person.id, tiffin.clock.today_local())
        fibre_min = int(profile["fibre_g_min"]) if profile is not None else solver.DEFAULT_FIBRE_MIN
        slots = json.loads(str(sets["slots_json"]))
        # a dish made ahead for a cook of several days changes every one of those days
        avoided = choices_mod.avoided_options(db, person.id, options)
        days = [
            choices_mod.day_view(db, d, slots, options, picks, target, fibre_min, avoided=avoided)
            for d in choices_mod.span_of(row)
        ]
        return {
            "day": days[0],
            "days": days,
            "picked": len(picks),
            "of": len({(str(o["day"]), str(o["slot"])) for o in options}),
        }

    return await _read(request, write)


@router.post("/api/choices/confirm")
async def choices_confirm(request: Request) -> dict[str, Any]:
    """The week set from the picks (the first dish of any meal not picked): its menus, decided,
    and its shopping list."""
    from ..food import choices as choices_mod  # noqa: PLC0415 (food imports the db layer)
    from ..jobs import shop_refresh  # noqa: PLC0415 (jobs import the scheduler)

    person = _person(request)
    body = await _body(request)
    check_fence(request, body, {"set_id"})
    set_id = _choice_int(body, "set_id")
    tiffin = request.app.state.tiffin

    def write(db: Db) -> dict[str, Any]:
        try:
            settled = choices_mod.settle(
                tiffin,
                db,
                person.id,
                set_id,
                how="chosen",
                clock=tiffin.clock,
                retailers=tuple(shop_refresh.retailer_order(tiffin.config)),
            )
        except LookupError as e:
            raise HTTPException(status_code=404, detail=str(e)) from None
        except NothingToBuild as e:
            raise HTTPException(status_code=409, detail=str(e)) from None
        _web_event(db, person.id, "choices_set", {"picked": settled.chosen})
        return {
            "said": settled.said(),
            "list_url": f"?tab=shopping&list={settled.list_id}" if settled.list_id else None,
        }

    return await _read(request, write)


@router.post("/api/choices/build")
async def choices_build(request: Request) -> dict[str, Any]:
    """A week of choices for the coming weekdays, now, on the person's build lane (about ten
    minutes): the page looks again until it is in."""
    from .. import building  # noqa: PLC0415
    from ..food import choices as choices_mod  # noqa: PLC0415 (food imports the db layer)
    from ..jobs import menu_choices  # noqa: PLC0415 (jobs import the scheduler)
    from ..targets import profile_on  # noqa: PLC0415

    person = _person(request)
    body = await _body(request)
    check_fence(request, body, set())
    tiffin = request.app.state.tiffin

    def write(db: Db) -> dict[str, Any]:
        today = tiffin.clock.today_local()
        ahead = choices_mod.week_days(today)
        # the week a build now is for: choosing booked from tomorrow counts (food/planning.py)
        profile = profile_on(db, person.id, ahead[0] if ahead else today)
        if profile is None:
            raise HTTPException(status_code=409, detail="Finish /onboard in Telegram first.")
        if choices_mod.per_slot_of(profile) == 0:
            raise HTTPException(
                status_code=409, detail="Weekly choices are off: switch them on first."
            )
        now = building.current(db, person.id, clock=tiffin.clock)
        if now is not None:
            raise HTTPException(status_code=409, detail=building.busy(now, page=True))
        try:
            _token, days = menu_choices.start(tiffin, db, person, clock=tiffin.clock, asked=True)
        except NothingToBuild as e:
            raise HTTPException(status_code=409, detail=str(e)) from None
        _web_event(db, person.id, "choices_build", {"days": len(days)})
        return {
            "started": True,
            "said": f"Building your choices for {choices_mod.days_words(days)} - "
            f"{choices_mod.TAKES}.",
        }

    return await _read(request, write)


@router.post("/api/choices/setting")
async def choices_setting(request: Request) -> dict[str, Any]:
    """Weekly choices on (3 or 4 dishes a meal) or off, from tomorrow: this person's own
    setting, booked as How you plan books it (food/planning.py), so that turning it on midweek
    can build the rest of the week at once."""
    from ..food import planning  # noqa: PLC0415 (food imports the db layer)

    person = _person(request)
    body = await _body(request)
    check_fence(request, body, {"per_slot"})
    per_slot = body.get("per_slot")
    if isinstance(per_slot, bool) or per_slot not in (0, 3, 4):
        raise HTTPException(status_code=400, detail="per_slot is 0 (off), 3 or 4")
    tiffin = request.app.state.tiffin

    def write(db: Db) -> dict[str, Any]:
        try:
            with db.tx():
                planning.change(
                    db,
                    person.id,
                    today=tiffin.clock.today_local(),
                    starts="tomorrow",
                    planning="choose" if per_slot else "auto",
                    options=int(per_slot) if per_slot else None,
                )
                db.insert(
                    "onboarding_answers",
                    person_id=person.id,
                    step="chs",
                    answer_json=json.dumps(int(per_slot)),
                )
                _web_event(db, person.id, "choices_setting", {"per_slot": per_slot})
        except LookupError:
            raise HTTPException(
                status_code=409, detail="Finish /onboard in Telegram first."
            ) from None
        if not per_slot:
            return {"said": "Weekly choices are off: your menu is built as before."}
        return {
            "said": f"Weekly choices are on: {per_slot} dishes for every weekday meal, each "
            "Sunday morning for the week ahead."
        }

    return await _read(request, write)
