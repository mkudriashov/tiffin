"""Manual recipe and menu entry for signed-in people."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from ..clock import isoday
from ..db import Db
from ..errors import NothingToBuild
from ..food import manual
from .api_common import _body, _day_arg, _person, _read, _today, _web_event, check_fence

router = APIRouter()


@router.get("/api/manual")
async def manual_library(request: Request) -> dict[str, Any]:
    person = _person(request)
    return await _read(request, manual.library, person.id)


@router.post("/api/manual/recipes")
async def manual_recipe(request: Request) -> dict[str, Any]:
    person = _person(request)
    body = await _body(request)
    check_fence(request, body, {"name", "ingredients", "method", "prep_minutes", "servings"})
    tiffin = request.app.state.tiffin

    def write(db: Db) -> dict[str, Any]:
        try:
            with db.tx():
                recipe_id = manual.add_recipe(db, person.id, body, clock=tiffin.clock)
                _web_event(db, person.id, "manual_recipe", {"recipe_id": recipe_id})
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return {"recipe_id": recipe_id, "said": "Recipe saved. Choose it for a menu below."}

    return await _read(request, write)


@router.post("/api/manual/menu")
async def manual_menu(request: Request) -> dict[str, Any]:
    person = _person(request)
    body = await _body(request)
    check_fence(request, body, {"start", "days", "meals"})
    if not isinstance(body.get("start"), str) or not isinstance(body.get("meals"), list):
        raise HTTPException(status_code=400, detail="Choose a start day and meals.")
    days = body.get("days")
    if not isinstance(days, int) or isinstance(days, bool):
        raise HTTPException(status_code=400, detail="Choose one to seven days.")
    day = _day_arg(body["start"], _today(request))
    tiffin = request.app.state.tiffin

    def write(db: Db) -> dict[str, Any]:
        try:
            with db.tx():
                menu_id, list_id = manual.save_menu(
                    db, person.id, day, days, body["meals"], clock=tiffin.clock
                )
                _web_event(db, person.id, "manual_menu", {"menu_id": menu_id})
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        except NothingToBuild as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        return {
            "menu_id": menu_id,
            "list_id": list_id,
            "said": f"Menu saved from {isoday(day)}. Its shopping list is ready.",
        }

    return await _read(request, write)
