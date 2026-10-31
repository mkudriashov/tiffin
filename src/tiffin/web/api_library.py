"""Library routes for the web page."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response

from ..db import Db
from ..food import catalogue
from . import queries
from .api_common import (
    _body,
    _day_arg,
    _day_arg_opt,
    _grams,
    _number,
    _person,
    _read,
    _row_id,
    _today,
    _web_event,
    check_fence,
)

router = APIRouter()


@router.get("/api/batch")
async def batch(request: Request) -> dict[str, Any]:
    """
    absent, it means the whole segment - the ordinary Sunday cook - exactly as before."""
    person = _person(request)
    from_day = _day_arg_opt(request.query_params.get("from"))
    return await _read(request, queries.batch, person.id, from_day, _today(request))


@router.post("/api/batch/yield")
async def batch_yield(request: Request) -> dict[str, Any]:
    """ "600 g of raw turkey came out of the air fryer at 400 g" - written down once, so the
    next cook can offer the split before the pan is on rather than only after."""
    person = _person(request)
    body = await _body(request)
    fields = {"recipe_id", "ingredient_code", "raw_g", "cooked_g"}
    check_fence(request, body, fields)
    if not fields <= set(body):
        raise HTTPException(status_code=400, detail=f"{', '.join(sorted(fields))} are required")
    recipe_id = _row_id(body["recipe_id"], "recipe_id")
    code = str(body["ingredient_code"])[:80]
    raw_g, cooked_g = _grams(body["raw_g"], "raw_g"), _grams(body["cooked_g"], "cooked_g")

    def write(db: Db) -> dict[str, Any]:
        with db.tx():
            got = queries.record_yield(
                db, person.id, recipe_id, code, raw_g, cooked_g, _today(request)
            )
            if got is None:
                raise HTTPException(status_code=404, detail="not a food in this cook")
            _web_event(db, person.id, "cook_yield", {"recipe_id": recipe_id, "code": code})
        return got

    return await _read(request, write)


@router.get("/api/foods")
async def foods(request: Request) -> dict[str, Any]:
    """The aisles a food can go in, and the foods already in this person's roster."""
    person = _person(request)

    def read(db: Db) -> dict[str, Any]:
        return queries.foods(db, person.id)

    return await _read(request, read)


@router.post("/api/foods")
async def add_food(request: Request) -> dict[str, Any]:
    """Add or re-version an approved food in this person's catalogue."""
    person = _person(request)
    body = await _body(request)
    need = {"name", "kcal_100", "protein_100", "fat_100", "carb_100", "pack_g", "aisle"}
    allowed = need | {"fibre_100", "pack_label", "person_scope", "pantry", "use_whole_pack"}
    check_fence(request, body, allowed)
    if not need <= set(body):
        raise HTTPException(status_code=400, detail=f"{', '.join(sorted(need))} are required")
    if not isinstance(body["name"], str) or not isinstance(body["aisle"], str):
        raise HTTPException(status_code=400, detail="Name and aisle must be text")
    if "pack_label" in body and not isinstance(body["pack_label"], str):
        raise HTTPException(status_code=400, detail="Pack label must be text")
    if any(key in body and not isinstance(body[key], bool) for key in ("pantry", "use_whole_pack")):
        raise HTTPException(
            status_code=400, detail="Pantry and whole-pack choices must be true or false"
        )
    kcal = _number(body["kcal_100"], "kcal_100")
    protein = _number(body["protein_100"], "protein_100")
    fat = _number(body["fat_100"], "fat_100")
    carb = _number(body["carb_100"], "carb_100")
    raw_fibre = body.get("fibre_100")
    fibre = None if raw_fibre in (None, "") else _number(raw_fibre, "fibre_100")
    scope = str(body.get("person_scope") or "both")
    if scope not in ("both", "me"):
        # "partner" or "owner" named a person from the body, and was stored as that person's own
        # food: only "both" or "just me" can be asked for here, and only the header says who "me" is
        raise HTTPException(status_code=400, detail="person_scope is both or me")
    if scope == "me":
        # the page says "just me" and only the header knows who that is: a person_scope is never
        # read off the body, the rule every other write on this page follows
        scope = person.slug

    def write(db: Db) -> dict[str, Any]:
        with db.tx():
            try:
                got = catalogue.add_food(
                    db,
                    name=body["name"][:120],
                    fibre_100=fibre,
                    pack_g=_number(body["pack_g"], "pack_g"),
                    pack_label=str(body.get("pack_label") or "")[:80],
                    aisle=body["aisle"],
                    person_scope=scope,
                    pantry=body.get("pantry", False),
                    use_whole_pack=body.get("use_whole_pack", False),
                    kcal_100=kcal,
                    protein_100=protein,
                    fat_100=fat,
                    carb_100=carb,
                )
            except catalogue.FoodRejected as e:
                raise HTTPException(status_code=400, detail=str(e)) from None
            _web_event(db, person.id, "food_added", {"code": got["code"], "v": got["version"]})
        return got

    return await _read(request, write)


@router.get("/api/dishes")
async def dishes(request: Request) -> dict[str, Any]:
    person = _person(request)
    try:
        recent = int(request.query_params.get("recent", "30"))
    except ValueError:
        raise HTTPException(status_code=400, detail="bad recent") from None
    recent = max(1, min(recent, 3650))  # a window, not an arbitrary number of days
    rows = await _read(request, queries.dishes, person.id, recent, _today(request))
    return {"dishes": rows}


@router.post("/api/dishes/rate")
async def rate(request: Request) -> dict[str, Any]:
    person = _person(request)
    body = await _body(request)
    check_fence(request, body, {"recipe_id", "score", "note"})
    if "recipe_id" not in body or "score" not in body:
        raise HTTPException(status_code=400, detail="recipe_id and score are required")
    recipe_id = _row_id(body["recipe_id"], "recipe_id")
    try:
        score = int(body["score"])
    except TypeError, ValueError:
        raise HTTPException(status_code=400, detail="score must be a number") from None
    if not 1 <= score <= 5:
        raise HTTPException(status_code=400, detail="score must be 1..5")
    note = body.get("note")
    if note is not None and not isinstance(note, str):
        raise HTTPException(status_code=400, detail="note must be text")

    def write(db: Db) -> dict[str, Any]:
        with db.tx():
            # the score before this one, so a tap can be taken back exactly (None: never rated)
            previous = queries.latest_score(db, person.id, recipe_id)
            if not queries.rate_dish(db, person.id, recipe_id, score, note):
                raise HTTPException(status_code=404, detail="no such dish")
            _web_event(db, person.id, "dish_rating", {"recipe_id": recipe_id, "score": score})
        name = str(db.scalar("SELECT name FROM recipes WHERE id = ?", (recipe_id,)) or "That dish")
        # said back: what the rating now does to their menus
        if score <= 2:
            said = f"{name} will not be offered again."
        elif score >= 4:
            said = f"{name} will be asked for more often."
        else:
            said = f"Noted: {name} neither more nor less often."
        return {"recipe_id": recipe_id, "score": score, "previous": previous, "said": said}

    return await _read(request, write)


@router.get("/api/summary/weekly")
async def weekly(request: Request) -> dict[str, Any]:
    person = _person(request)
    start = _day_arg(request.query_params.get("week"), _today(request))
    return await _read(request, queries.weekly_summary_text, person.id, start)


@router.get("/api/export")
async def export(request: Request) -> Response:
    """Everything Tiffin holds about the person asking, as a JSON file to keep. The
    person is the header's; the other person's rows never come with it."""
    person = _person(request)
    tiffin = request.app.state.tiffin
    data = await _read(request, lambda db: queries.export(db, person.id, clock=tiffin.clock))
    day = tiffin.clock.today_local().isoformat()
    return Response(
        content=json.dumps(data, ensure_ascii=False, indent=1, default=str),
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="tiffin-{day}.json"',
            "Cache-Control": "private, no-store",
        },
    )


@router.get("/api/whoami")
async def whoami(request: Request) -> dict[str, Any]:
    """Return the identity resolved by the configured web sign-in mode."""
    person = _person(request)
    return {"slug": person.slug, "person_id": person.id}
