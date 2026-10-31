"""Menu routes for the web page."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from ..clock import isoday, parse_day
from ..db import Db
from ..errors import NothingToBuild
from . import queries
from .api_common import _body, _day_arg, _person, _read, _slot_arg, _today, _web_event, check_fence

router = APIRouter()


@router.get("/api/plan")
async def plan_get(request: Request) -> dict[str, Any]:
    """How this person plans now, and any change booked for a later day."""
    from ..food import planning  # noqa: PLC0415 (food imports the db layer)

    person = _person(request)

    def read(db: Db) -> dict[str, Any]:
        got = planning.view(db, person.id, _today(request))
        if got is None:
            raise HTTPException(status_code=409, detail="Finish /onboard in Telegram first.")
        return got

    return await _read(request, read)


@router.post("/api/plan")
async def plan_set(request: Request) -> dict[str, Any]:
    """Change how this person plans - menus built for them, chosen by them, or tracking only;
    cooking each day or in batches - from next Monday (the week in hand stays as planned) or from
    tomorrow (every day not yet planned). Only what is sent changes."""
    from ..food import planning  # noqa: PLC0415 (food imports the db layer)

    person = _person(request)
    body = await _body(request)
    check_fence(request, body, {"planning", "options", "cadence", "batch", "freezes", "starts"})
    starts = body.get("starts", "monday")
    wanted: dict[str, Any] = {}
    for key, kind in (("planning", str), ("options", int), ("cadence", str), ("freezes", bool)):
        value = body.get(key)
        if value is None:
            continue
        # a bool is an int in Python: options must be a number, freezes a true or false
        if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
            raise HTTPException(status_code=400, detail=f"{key} has the wrong type")
        wanted[key] = value
    batch = body.get("batch")
    if batch is not None:
        if not isinstance(batch, list) or not all(isinstance(b, str) for b in batch):
            raise HTTPException(status_code=400, detail="batch is a list of meals")
        wanted["batch"] = batch
    if not isinstance(starts, str):
        raise HTTPException(status_code=400, detail="starts is monday or tomorrow")
    tiffin = request.app.state.tiffin

    def write(db: Db) -> dict[str, Any]:
        today = tiffin.clock.today_local()
        try:
            with db.tx():
                start = planning.change(db, person.id, today=today, starts=starts, **wanted)
                steps = {
                    "planning": "pln",
                    "options": "chs",
                    "cadence": "segs",
                    "batch": "batch",
                    "freezes": "frz",
                }
                for key, value in wanted.items():
                    db.insert(
                        "onboarding_answers",
                        person_id=person.id,
                        step=steps[key],
                        answer_json=json.dumps(value),
                    )
                _web_event(db, person.id, "plan", {k: wanted[k] for k in sorted(wanted)})
        except planning.BadPlan as e:
            raise HTTPException(status_code=400, detail=str(e)) from None
        except LookupError:
            raise HTTPException(
                status_code=409, detail="Finish /onboard in Telegram first."
            ) from None
        view = planning.view(db, person.id, today)
        when = f"From {start:%a} {start.day} {start:%b}"
        if starts == "tomorrow":
            when = "From tomorrow"
        booked = (view or {}).get("booked") or (view or {}).get("now") or {}
        note = ""
        if starts == "tomorrow":
            # what is already planned may already be bought or cooked: it stays
            note = (
                "Days that already have a menu keep it, and no new ones are planned."
                if wanted.get("planning") == "off"
                else "Days that already have a menu keep it; rebuild one on Menu to change it."
            )
        said = f"{when}: {booked.get('said', '')}" + (f" {note}" if note else "")
        # `said` whole, for anything that shows only the answer; `note` is what the page adds to
        # the booked line it already draws
        return {"said": said, "note": note, "plan": view}

    return await _read(request, write)


@router.get("/api/menu")
async def menu(request: Request) -> dict[str, Any]:
    from .. import building  # noqa: PLC0415

    person = _person(request)
    raw = request.query_params.get("segment")
    segment = _day_arg(raw, _today(request)) if raw else None
    clock = request.app.state.tiffin.clock

    from ..jobs import shop_refresh  # noqa: PLC0415 (jobs import the scheduler)

    retailers = shop_refresh.retailer_order(request.app.state.tiffin.config)

    def read(db: Db) -> dict[str, Any]:
        out = queries.menu(
            db,
            person.id,
            segment,
            _today(request),
            retailers,
            provider=request.app.state.tiffin.config.model_scope(),
        )
        # a build in flight: the page says so and looks again, there is no message to read
        now = building.current(db, person.id, clock=clock)
        if now is not None:
            out["building"] = now
        # how the last menu build ended, for the page that started or watched it (rebuildPanel)
        ended = building.told(db, person.id)
        if ended is not None:
            out["built"] = ended
        return out

    return await _read(request, read)


@router.post("/api/menu/rebuild")
async def menu_rebuild(request: Request) -> dict[str, Any]:
    """A new menu from the model, for the days of the menu covering `day` (or the next cook),
    with an optional word on what to change - the page's own /cook replace. It runs on the
    person's build lane (usually 2-4 minutes); the current menu stays until the new one is in
    force, and the page looks again until it is."""
    from .. import building  # noqa: PLC0415
    from ..food import menu as menu_mod  # noqa: PLC0415 (food imports the db layer)
    from ..jobs import menu_propose  # noqa: PLC0415 (jobs import the scheduler)
    from ..targets import profile_on  # noqa: PLC0415

    person = _person(request)
    body = await _body(request)
    check_fence(request, body, {"note", "day", "only_day"})
    note = body.get("note")
    # said every time, if only as "": a body with nothing in it is not a request to rebuild
    if not isinstance(note, str) or len(note) > 200:
        raise HTTPException(status_code=400, detail="note is a few words (or empty), at most 200")
    raw_day = body.get("day")
    day = _day_arg(raw_day, _today(request)) if isinstance(raw_day, str) and raw_day else None
    raw_one = body.get("only_day")
    only_day = _day_arg(raw_one, _today(request)) if isinstance(raw_one, str) and raw_one else None
    tiffin = request.app.state.tiffin

    def refused(db: Db) -> str:
        # a refusal in the note stands whether or not a menu is built, and is said with the
        # verification). Only when no build starts - a build writes and says its own
        said = menu_propose.refusal_for_page(db, person, note.strip(), clock=tiffin.clock)
        return f" {said}" if said else ""

    def write(db: Db) -> dict[str, Any]:
        now = building.current(db, person.id, clock=tiffin.clock)
        if now is not None:
            kept = refused(db)
            raise HTTPException(
                status_code=409,
                detail=building.busy(now, page=True)
                + (f"{kept} Anything else in that change was not started." if kept else ""),
            )
        today = tiffin.clock.today_local()
        profile = profile_on(db, person.id, today)
        if profile is None:
            raise HTTPException(status_code=409, detail="Finish /onboard in Telegram first.")
        # the menu for the stretch of days covering `day`, never that day's resized plates: those
        # are a one-day menu, and the rebuild covered that day alone
        for_menu = menu_mod.segment_menu_id(db, person.id, day) if day is not None else None
        tapped = (
            db.one("SELECT segment_start, segment_end FROM menus WHERE id = ?", (for_menu,))
            if for_menu is not None
            else None
        )
        if tapped is not None:
            stretch = menu_mod.Segment(
                parse_day(str(tapped["segment_start"])), parse_day(str(tapped["segment_end"]))
            )
            if menu_mod.open_days(db, person.id, stretch, today) is None:
                raise HTTPException(
                    status_code=409,
                    detail="Every day of this menu has begun, so there is nothing left on it to "
                    "rebuild. Send /cook in Telegram to plan the days after it." + refused(db),
                )
        token = building.mark(
            db, person.id, note.strip() or "a new menu", clock=tiffin.clock, menu=for_menu
        )
        _web_event(db, person.id, "menu_rebuild", {"note": bool(note.strip())})

        def build(bdb: Db) -> None:
            menu_propose.rebuild_for_page(
                tiffin,
                bdb,
                person,
                note=note.strip(),
                for_menu=for_menu,
                clock=tiffin.clock,
                token=token,
                only_day=only_day,
            )

        def broke(bdb: Db) -> None:
            building.say(
                bdb,
                person.id,
                token,
                "Something went wrong building that menu and it did not finish. The menu you "
                "have stays; build again, or send /cook in Telegram.",
            )

        background = tiffin.run_build(
            person.id,
            building.guarded(
                tiffin,
                person,
                token,
                build,
                broke="Something went wrong building the menu asked for on the page, and it did "
                "not finish. /menu shows what is planned; send /cook to try again.",
                on_crash=broke,
            ),
            db,
        )
        return {
            "started": True,
            "takes": building.TAKES,
            "id": token,
            "building": building.current(db, person.id, clock=tiffin.clock) if background else None,
        }

    return await _read(request, write)


@router.post("/api/menu/new-dish")
async def menu_new_dish(request: Request) -> dict[str, Any]:
    """Two or three new dishes for one meal, written by the model, when nothing already eaten
    fits (food/newdish.py). They join that meal's swap list; one tap puts one in. Runs on the
    person's build lane (usually 1-2 minutes)."""
    from .. import building  # noqa: PLC0415
    from ..errors import ModelUnavailable  # noqa: PLC0415
    from ..food import newdish  # noqa: PLC0415 (food imports the db layer)

    person = _person(request)
    body = await _body(request)
    check_fence(request, body, {"day", "slot", "every_day", "note"})
    raw_day = body.get("day")
    if not isinstance(raw_day, str) or not raw_day:
        raise HTTPException(status_code=400, detail="day is required")
    day = _day_arg(raw_day, _today(request))
    slot = _slot_arg(body.get("slot"))
    every_day = body.get("every_day", True)
    if not isinstance(every_day, bool):
        raise HTTPException(status_code=400, detail="every_day is true or false")
    note = body.get("note") or ""
    if not isinstance(note, str) or len(note) > 200:
        raise HTTPException(status_code=400, detail="note is a few words, at most 200 characters")
    tiffin = request.app.state.tiffin

    def write(db: Db) -> dict[str, Any]:
        now = building.current(db, person.id, clock=tiffin.clock)
        if now is not None:
            raise HTTPException(status_code=409, detail=building.busy(now, page=True))
        # said on the page as the bot says it: "new pre-bed dishes", never the slot's code
        token = building.mark(
            db,
            person.id,
            f"new {slot.replace('_', '-')} dishes",
            clock=tiffin.clock,
            kind=building.DISHES,
            day=day,
            slot=slot,
        )
        db.execute("DELETE FROM bot_state WHERE k = ?", (f"newdish_said:{person.id}",))
        _web_event(db, person.id, "new_dishes", {"day": isoday(day), "slot": slot})
        said: dict[str, Any] = {}

        def build(bdb: Db) -> None:
            try:
                got = newdish.ask(
                    tiffin,
                    bdb,
                    person.id,
                    day,
                    slot,
                    note=note.strip(),
                    every_day=every_day,
                    clock=tiffin.clock,
                )
                said["said"] = got.said()
            except ModelUnavailable:
                said["said"] = "No model to ask right now: the dishes you have had are below."
            except (NothingToBuild, ValueError) as e:
                said["said"] = str(e)
            keep(bdb)

        def keep(bdb: Db) -> None:
            # kept with the meal it answers, so whichever swap area for that meal is opened next
            # says it - not only the one element that asked, which any redraw replaces. Written
            # before `building.guarded` ends the build, so no look reads "not building, nothing
            # said" and calls a finished ask unfinished
            kept = {"day": isoday(day), "slot": slot, "said": str(said.get("said", ""))[:300]}
            bdb.state_set(f"newdish_said:{person.id}", json.dumps(kept))

        def broke(bdb: Db) -> None:
            # the meal's swap list is where the page looks for the answer: never left empty
            said["said"] = (
                "Something went wrong asking for new dishes. The dishes you have had are below."
            )
            keep(bdb)

        background = tiffin.run_build(
            person.id,
            building.guarded(tiffin, person, token, build, broke=None, on_crash=broke),
            db,
        )
        return {
            "started": True,
            "takes": newdish.TAKES,
            "said": None if background else said.get("said"),
        }

    return await _read(request, write)


@router.post("/api/menu/plates")
async def menu_plates(request: Request) -> dict[str, Any]:
    """Resize or skip one day's meals and solve the day again: this person's, and on a day the
    two cook together the other's plates too, each sized to its own day (food/plates.py).

    and French toast, I can achieve that". The person is the header's, never the body's.
    """
    from ..errors import Infeasible  # noqa: PLC0415
    from ..food import plates  # noqa: PLC0415

    person = _person(request)
    body = await _body(request)
    check_fence(request, body, {"day", "levels", "eaten", "preview"})
    raw_day = body.get("day")
    if not isinstance(raw_day, str) or not raw_day:
        raise HTTPException(status_code=400, detail="day is required")
    day = _day_arg(raw_day, _today(request))
    # a preview solves the same and stores nothing: what Apply would do, said before it is done
    preview = body.get("preview", False)
    if not isinstance(preview, bool):
        raise HTTPException(status_code=400, detail="preview is true or false")
    levels = body.get("levels", {})
    if not isinstance(levels, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in levels.items()
    ):
        raise HTTPException(
            status_code=400, detail="levels maps a meal to skip, less, planned, more or lots"
        )
    eaten = body.get("eaten", [])
    if not isinstance(eaten, list) or not all(isinstance(s, str) for s in eaten):
        raise HTTPException(status_code=400, detail="eaten lists the meals already eaten")
    tiffin = request.app.state.tiffin

    def write(db: Db) -> dict[str, Any]:
        # one meal skipped or brought back, by someone whose Skip just drops the meal (`/set skip
        # drop`): the rest of the day stays exactly as it was, not solved again
        had = (plates.state(db, person.id, day) or {}).get("levels", {})
        one = plates.one_skip(had, levels) if not preview else None
        if one is not None and plates.skip_mode(db, person.id, day) == "drop":
            try:
                said = plates.skip_meal(
                    db, person.id, day, one[0], one[1], config=tiffin.config, clock=tiffin.clock
                )
            except NothingToBuild as e:
                raise HTTPException(status_code=409, detail=str(e)) from None
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e)) from None
            _web_event(db, person.id, "meal_skipped", {"day": isoday(day), "skip": one[1]})
            return {"said": said, "day": isoday(day)}
        with db.tx():
            try:
                got = plates.resize(
                    db,
                    person.id,
                    day,
                    levels,
                    eaten=eaten,
                    config=tiffin.config,
                    clock=tiffin.clock,
                    dry_run=preview,
                )
            except NothingToBuild as e:
                raise HTTPException(status_code=409, detail=str(e)) from None
            except Infeasible as e:
                raise HTTPException(status_code=422, detail=str(e)) from None
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e)) from None
            if preview:
                return {
                    "said": got.preview_said(),
                    "day": isoday(day),
                    "preview": True,
                    "plates": [{"slot": s, "before": a, "after": b} for s, a, b in got.plates],
                }
            event: dict[str, Any] = {"day": isoday(day), "menu_id": got.menu_id}
            if got.partner_menu_id is not None:
                event["partner_menu_id"] = got.partner_menu_id
            _web_event(db, person.id, "plates_resized", event)
        return {"said": got.said(), "day": isoday(day)}

    return await _read(request, write)
