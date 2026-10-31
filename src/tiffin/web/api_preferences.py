"""Preferences routes for the web page."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from ..clock import isoday
from ..db import Db
from ..errors import NothingToBuild
from .api_common import (
    _body,
    _day_arg,
    _person,
    _read,
    _row_id,
    _slot_arg,
    _today,
    _web_event,
    _words,
    check_fence,
)

router = APIRouter()


@router.get("/api/prefs")
async def food_prefs(request: Request) -> dict[str, Any]:
    """This person's own food lists - what they avoid, like and keep at home - with what each
    avoided entry keeps out, and the dishes still to come that hold one (food/prefs.py)."""
    from ..food import prefs  # noqa: PLC0415 (food imports the db layer)

    person = _person(request)
    return await _read(request, prefs.view, person.id, _today(request))


@router.post("/api/prefs")
async def food_prefs_write(request: Request) -> dict[str, Any]:
    """
    want lentils and chickpeas - and it really remembers"). The same door the bot uses, so the
    two cannot disagree; the person is the header's, never the body's."""
    from ..food import prefs  # noqa: PLC0415 (food imports the db layer)

    person = _person(request)
    body = await _body(request)
    check_fence(request, body, {"list", "add", "remove"})
    name = body.get("list", "avoid")
    # a list or an object is no key of LISTS: asked as one, it was a TypeError and a 500 (#118)
    if not isinstance(name, str) or name not in prefs.LISTS:
        raise HTTPException(status_code=400, detail="list is avoid, likes or pantry")
    add = _words(body.get("add"), "add", longest=prefs.MAX_TERM)
    remove = _words(body.get("remove"), "remove")
    if not add and not remove:
        raise HTTPException(status_code=400, detail="name a food to add or to remove")
    tiffin = request.app.state.tiffin

    def write(db: Db) -> dict[str, Any]:
        with db.tx():
            try:
                got = prefs.amend(
                    db, person.id, name, add=add, remove=remove, clock=tiffin.clock, strict=False
                )
            except LookupError:
                raise HTTPException(
                    status_code=409, detail="Finish /onboard in Telegram first."
                ) from None
            if got.changed:
                _web_event(
                    db,
                    person.id,
                    "food_list",
                    {"list": name, "add": got.added, "remove": got.removed},
                )
        return {
            "said": got.said(),
            "added": list(got.added),
            "prefs": prefs.view(db, person.id, tiffin.clock.today_local()),
        }

    return await _read(request, write)


@router.post("/api/prefs/notes")
async def food_notes_write(request: Request) -> dict[str, Any]:
    """Add a standing note ("less rice", "no salads on weekdays") or take one off: what every
    menu is asked for, in their own words, read by every build (food/prefs.py). The person is
    the header's, never the body's; the note is their own (source 'chat': their own words)."""
    from ..food import prefs  # noqa: PLC0415 (food imports the db layer)
    from ..telegram.notesflow import noted_said  # noqa: PLC0415

    person = _person(request)
    body = await _body(request)
    check_fence(request, body, {"add", "remove"})
    add, remove = body.get("add"), body.get("remove")
    for value in (add, remove):
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise HTTPException(status_code=400, detail="a note is a few words")
    if not add and not remove:
        raise HTTPException(status_code=400, detail="say a note to add or to take off")
    tiffin = request.app.state.tiffin

    def write(db: Db) -> dict[str, Any]:
        with db.tx():
            got = prefs.amend_notes(
                db,
                person.id,
                add=[add] if add else (),
                drop=[remove] if remove else (),
                source="chat",
                clock=tiffin.clock,
            )
            _web_event(
                db, person.id, "food_note", {"add": bool(got.added), "drop": bool(got.dropped)}
            )
        # the same sentence the bot's /notes says (telegram/notesflow.py)
        return {
            "said": noted_said(got),
            "prefs": prefs.view(db, person.id, tiffin.clock.today_local()),
        }

    return await _read(request, write)


@router.get("/api/menu/swap")
async def swap_options(request: Request) -> dict[str, Any]:
    """The dishes that could stand in for one meal: this person's own, eaten before, nothing in
    them they avoid, none rated "never again" (food/swap.py). No model is asked anything."""
    from ..food import swap  # noqa: PLC0415 (food imports the db layer)

    person = _person(request)
    raw_day = request.query_params.get("day")
    if not raw_day:
        raise HTTPException(status_code=400, detail="day is required")
    day = _day_arg(raw_day, _today(request))
    slot = _slot_arg(request.query_params.get("slot"))
    today = _today(request)

    clock = request.app.state.tiffin.clock

    def read(db: Db) -> dict[str, Any]:
        from .. import building  # noqa: PLC0415

        try:
            out = swap.options(db, person.id, day, slot, today)
        except NothingToBuild as e:
            raise HTTPException(status_code=409, detail=str(e)) from None
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from None
        out["building"] = building.current(db, person.id, clock=clock)
        out["new_said"] = _new_said(db, person.id, slot, out["days"])
        return out

    return await _read(request, read)


def _new_said(db: Db, person_id: int, slot: str, days: list[str]) -> str | None:
    """What came of the last ask for new dishes, when it was for this meal: this slot, on one of
    the days the dish is on (a pot asked about from Monday's card is the same pot on Tuesday's).
    Never another meal's - "No new dinner dish passed the checks" under a lunch answers nothing.
    """
    raw = db.state_get(f"newdish_said:{person_id}")
    try:
        kept = json.loads(raw) if raw else None
    except ValueError:
        return None  # one written before it said which meal: not this meal's to show
    if not isinstance(kept, dict) or kept.get("slot") != slot or kept.get("day") not in days:
        return None
    return str(kept.get("said") or "") or None


@router.post("/api/menu/swap")
async def swap_dish(request: Request) -> dict[str, Any]:
    """Put one of the offered dishes in for a meal - on every day still to come it was on, or on
    the one day - and solve those days again in code. On a day cooked together the other plate
    follows. The person is the header's, and the dish must be one offered to them."""
    from ..errors import Infeasible  # noqa: PLC0415
    from ..food import swap  # noqa: PLC0415 (food imports the db layer)

    person = _person(request)
    body = await _body(request)
    check_fence(request, body, {"day", "slot", "recipe_id", "every_day"})
    raw_day = body.get("day")
    if not isinstance(raw_day, str) or not raw_day:
        raise HTTPException(status_code=400, detail="day is required")
    day = _day_arg(raw_day, _today(request))
    slot = _slot_arg(body.get("slot"))
    recipe_id = _row_id(body.get("recipe_id"), "recipe_id")
    every_day = body.get("every_day", True)
    if not isinstance(every_day, bool):
        raise HTTPException(status_code=400, detail="every_day is true or false")
    tiffin = request.app.state.tiffin

    def write(db: Db) -> dict[str, Any]:
        with db.tx():
            try:
                got = swap.swap(
                    db,
                    person.id,
                    day,
                    slot,
                    recipe_id,
                    every_day=every_day,
                    config=tiffin.config,
                    clock=tiffin.clock,
                )
            except NothingToBuild as e:
                raise HTTPException(status_code=409, detail=str(e)) from None
            except Infeasible as e:
                raise HTTPException(status_code=422, detail=str(e)) from None
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e)) from None
            event: dict[str, Any] = {"day": isoday(day), "slot": slot, "menu_id": got.menu_id}
            if got.partner_menu_id is not None:
                event["partner_menu_id"] = got.partner_menu_id
            _web_event(db, person.id, "dish_swapped", event)
        return {"said": got.said(), "day": isoday(day), "slot": slot}

    return await _read(request, write)


def _food_arg(value: Any) -> str:
    """A catalogue code: lowercase letters, digits and underscores, as `ingredients.code` is."""
    if not isinstance(value, str) or not value or len(value) > 60:
        raise HTTPException(status_code=400, detail="food is a food's code")
    if not all(c.isalnum() or c == "_" for c in value):
        raise HTTPException(status_code=400, detail="food is a food's code")
    return value


@router.get("/api/menu/swap-food")
async def swap_food_options(request: Request) -> dict[str, Any]:
    """What could stand in for one food inside one dish (food/subst.py): the same part in the
    dish, foods this person may have, best first by their own lean, the macros and the company
    it keeps. Code chooses and counts; no model is asked anything."""
    from ..food import subst  # noqa: PLC0415 (food imports the db layer)

    person = _person(request)
    raw_day = request.query_params.get("day")
    if not raw_day:
        raise HTTPException(status_code=400, detail="day is required")
    day = _day_arg(raw_day, _today(request))
    slot = _slot_arg(request.query_params.get("slot"))
    food = _food_arg(request.query_params.get("food"))
    today = _today(request)

    def read(db: Db) -> dict[str, Any]:
        try:
            return subst.options(db, person.id, day, slot, food, today)
        except NothingToBuild as e:
            raise HTTPException(status_code=409, detail=str(e)) from None
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from None

    return await _read(request, read)


@router.post("/api/menu/swap-food")
async def swap_food(request: Request) -> dict[str, Any]:
    """One food inside a dish changed for another of the ones offered: a new dish written, each
    day it is on solved again. The person is the header's, the food must be one on offer."""
    from ..errors import Infeasible  # noqa: PLC0415
    from ..food import subst  # noqa: PLC0415 (food imports the db layer)

    person = _person(request)
    body = await _body(request)
    check_fence(request, body, {"day", "slot", "food", "instead", "every_day"})
    raw_day = body.get("day")
    if not isinstance(raw_day, str) or not raw_day:
        raise HTTPException(status_code=400, detail="day is required")
    day = _day_arg(raw_day, _today(request))
    slot = _slot_arg(body.get("slot"))
    food = _food_arg(body.get("food"))
    instead = _food_arg(body.get("instead"))
    every_day = body.get("every_day", True)
    if not isinstance(every_day, bool):
        raise HTTPException(status_code=400, detail="every_day is true or false")
    tiffin = request.app.state.tiffin

    def write(db: Db) -> dict[str, Any]:
        with db.tx():
            try:
                got = subst.substitute(
                    db,
                    person.id,
                    day,
                    slot,
                    food,
                    instead,
                    every_day=every_day,
                    config=tiffin.config,
                    clock=tiffin.clock,
                )
            except NothingToBuild as e:
                raise HTTPException(status_code=409, detail=str(e)) from None
            except Infeasible as e:
                raise HTTPException(status_code=422, detail=str(e)) from None
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e)) from None
            _web_event(
                db,
                person.id,
                "food_swapped",
                {"day": isoday(day), "slot": slot, "menu_id": got.menu_id},
            )
        return {"said": got.said(), "day": isoday(day), "slot": slot}

    return await _read(request, write)


@router.post("/api/prefs/food")
async def food_pref(request: Request) -> dict[str, Any]:
    """One food kept off, or allowed back, by itself: the "Never again" chip, the made-from
    question (houmous after chickpeas) and the way back. By code, so it reaches no further than
    the food tapped; the person is the header's."""
    from ..food import prefs  # noqa: PLC0415 (food imports the db layer)

    person = _person(request)
    body = await _body(request)
    check_fence(request, body, {"food", "refused"})
    food = _food_arg(body.get("food"))
    refused = body.get("refused")
    if not isinstance(refused, bool):
        raise HTTPException(status_code=400, detail="refused is true or false")
    tiffin = request.app.state.tiffin

    def write(db: Db) -> dict[str, Any]:
        with db.tx():
            try:
                got = prefs.answer(db, person.id, food, keep_off=refused, clock=tiffin.clock)
            except LookupError:
                raise HTTPException(
                    status_code=404, detail="That is not one of your foods."
                ) from None
            _web_event(db, person.id, "food_pref", {"food": food, "refused": refused})
        return {
            "said": got.said(),
            "prefs": prefs.view(db, person.id, tiffin.clock.today_local()),
        }

    return await _read(request, write)


@router.post("/api/menu/swap-avoided")
async def swap_avoided(request: Request) -> dict[str, Any]:
    """Every dish still to come holding a food this person now avoids, swapped for the best one
    they have eaten before - one tap after "I do not want lentils", no model, no waiting."""
    from ..food import swap  # noqa: PLC0415 (food imports the db layer)

    person = _person(request)
    body = await _body(request)
    check_fence(request, body, set())
    tiffin = request.app.state.tiffin

    def write(db: Db) -> dict[str, Any]:
        with db.tx():
            got = swap.swap_avoided(db, person.id, config=tiffin.config, clock=tiffin.clock)
            if got.swapped:
                _web_event(
                    db, person.id, "dishes_swapped", {"menus": [s.menu_id for s in got.swapped]}
                )
        return {"said": got.said(), "swapped": len(got.swapped), "stuck": len(got.stuck)}

    return await _read(request, write)


@router.post("/api/weekend/mode")
async def weekend_mode(request: Request) -> dict[str, Any]:
    """
    whether it's shared menu or not shared, and we keep both menus"). Joint - either of them may
    flip it - and the flipper is the header's person, never the body's. Both versions are built
    ahead, so a flip decides that version's menus again and nothing is rebuilt; a version not
    built yet is queued on the worker of the person it is missing for (food/weekend.py)."""
    from ..food import weekend  # noqa: PLC0415

    person = _person(request)
    body = await _body(request)
    check_fence(request, body, {"mode"})
    mode = body.get("mode")
    if mode not in ("together", "apart"):
        raise HTTPException(status_code=400, detail="mode is together or apart")
    tiffin = request.app.state.tiffin
    today = _today(request)

    def write(db: Db) -> dict[str, Any]:
        with db.tx():
            try:
                got = weekend.flip(
                    db,
                    str(mode),
                    person_id=person.id,
                    today=today,
                    provider=tiffin.config.model_scope(),
                )
            except NothingToBuild as e:
                raise HTTPException(status_code=409, detail=str(e)) from None
            _web_event(
                db, person.id, "weekend_mode", {"mode": got.mode, "missing": list(got.missing)}
            )
        return {"said": got.said, "mode": got.mode, "missing": list(got.missing)}

    out = await _read(request, write)
    dispatch = getattr(request.app.state, "dispatch", None)
    if out["missing"] and dispatch is not None:
        from ..jobs import menu_propose, menu_shared  # noqa: PLC0415 (jobs are heavy)

        if mode == "together":
            dispatch(person.id, lambda db: menu_shared.build_quietly(tiffin, db))
        else:
            for pid in out["missing"]:
                dispatch(pid, lambda db, pid=pid: menu_propose.build_own_weekend(tiffin, db, pid))
    return out


@router.post("/api/weekend/taste")
async def weekend_taste(request: Request) -> dict[str, Any]:
    """What this person's weekend treats lean to - sweet, savoury or both - from the next weekend
    built. Their own, from the header: the other's is theirs to set."""
    from ..food import weekend  # noqa: PLC0415

    person = _person(request)
    body = await _body(request)
    check_fence(request, body, {"taste"})
    taste = body.get("taste")
    if taste not in weekend.TASTES:
        raise HTTPException(status_code=400, detail="taste is sweet, savoury or both")

    def write(db: Db) -> dict[str, Any]:
        with db.tx():
            weekend.set_taste(db, person.id, str(taste))
            _web_event(db, person.id, "treat_taste", {"taste": taste})
        return {"taste": taste, "said": weekend.TASTE_SAID[str(taste)]}

    return await _read(request, write)
