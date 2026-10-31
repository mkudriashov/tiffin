"""Recipes and menus chosen by a person without a model."""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Mapping, Sequence
from typing import Any

from ..clock import Clock
from ..db import Db
from ..energy import Macros
from . import catalogue, menu, recipes, shopping
from .recipes import DraftIngredient, RecipeDraft


def library(db: Db, person_id: int) -> dict[str, list[dict[str, Any]]]:
    foods = catalogue.approved(db, person_id)
    slug = db.scalar("SELECT slug FROM persons WHERE id = ?", (person_id,))
    dishes = db.all(
        "SELECT id, name, servings FROM recipes WHERE person_scope IN ('both', ?) "
        "AND EXISTS (SELECT 1 FROM recipe_ingredients WHERE recipe_id = recipes.id) "
        "ORDER BY name, id",
        (slug,),
    )
    return {
        "foods": [{"code": food.code, "name": food.name} for food in foods],
        "recipes": [
            {"id": int(row["id"]), "name": str(row["name"]), "servings": int(row["servings"])}
            for row in dishes
            if not catalogue.refused_names_in_recipe(db, person_id, int(row["id"]))
        ],
    }


def add_recipe(db: Db, person_id: int, body: Mapping[str, Any], *, clock: Clock) -> int:
    foods = catalogue.approved(db, person_id)
    indexes = {food.code: i for i, food in enumerate(foods, 1)}
    raw = body.get("ingredients")
    method = body.get("method")
    if not isinstance(raw, list) or not isinstance(method, list):
        raise ValueError("Ingredients and method must be lists.")
    ingredients: list[DraftIngredient] = []
    for item in raw:
        if not isinstance(item, dict) or set(item) != {"code", "g"}:
            raise ValueError("Each ingredient needs a food and grams.")
        code = item["code"]
        if not isinstance(code, str) or code not in indexes:
            raise ValueError("Choose a food from your available catalogue.")
        grams = _real(item["g"], "Ingredient grams")
        ingredients.append(DraftIngredient(indexes[code], grams))
    name = body.get("name")
    if not isinstance(name, str) or not all(isinstance(step, str) for step in method):
        raise ValueError("Name and method steps must be text.")
    prep = _integer(body.get("prep_minutes"), "Preparation time")
    servings = _integer(body.get("servings"), "Servings")
    draft = RecipeDraft(
        name=name.strip(),
        ingredients=tuple(ingredients),
        method=tuple(step.strip() for step in method),
        prep_minutes=prep,
        servings=servings,
    )
    slug = db.scalar("SELECT slug FROM persons WHERE id = ?", (person_id,))
    if slug is None:
        raise LookupError("No person for this recipe.")
    return recipes.store(db, draft, foods, "manual", None, str(slug), clock=clock)


def save_menu(
    db: Db,
    person_id: int,
    start: dt.date,
    days: int,
    meals: Sequence[Mapping[str, Any]],
    *,
    clock: Clock,
) -> tuple[int, int | None]:
    if start < clock.today_local():
        raise ValueError("Choose today or a later day.")
    if not 1 <= days <= 7:
        raise ValueError("Choose one to seven days.")
    if not 1 <= len(meals) <= len(menu.SLOT_ORDER):
        raise ValueError("Choose at least one meal.")
    slug = db.scalar("SELECT slug FROM persons WHERE id = ?", (person_id,))
    if slug is None:
        raise LookupError("No person for this menu.")
    selected: dict[str, tuple[int, float, Macros]] = {}
    units = catalogue.units(db)
    for meal in meals:
        if not isinstance(meal, Mapping) or set(meal) != {"slot", "recipe_id", "portion"}:
            raise ValueError("Each meal needs a slot, recipe and portion.")
        slot, recipe_id = meal["slot"], _integer(meal["recipe_id"], "Recipe")
        portion = _real(meal["portion"], "Portion")
        if slot not in menu.SLOT_ORDER or slot in selected:
            raise ValueError("Choose each meal slot once.")
        if not 0.25 <= portion <= 4:
            raise ValueError("Portion must be between 0.25 and 4 servings.")
        row = db.one(
            "SELECT id FROM recipes WHERE id = ? AND person_scope IN ('both', ?) "
            "AND EXISTS (SELECT 1 FROM recipe_ingredients WHERE recipe_id = recipes.id)",
            (recipe_id, slug),
        )
        if row is None or catalogue.refused_names_in_recipe(db, person_id, recipe_id):
            raise ValueError("Choose a recipe from your available dishes.")
        values = recipes.stored_per_serving(db, recipe_id, portion, units).as_macros()
        selected[str(slot)] = recipe_id, portion, values
    first = start
    last = start + dt.timedelta(days=days - 1)
    recipe_ids: dict[str, int] = {}
    multipliers: dict[str, float] = {}
    slot_macros: dict[str, Macros] = {}
    day_macros: dict[dt.date, Macros] = {}
    total = Macros(0, 0, 0, 0, 0)
    for _recipe_id, _portion, values in selected.values():
        total = Macros(
            total.kcal + values.kcal,
            total.protein_g + values.protein_g,
            total.fat_g + values.fat_g,
            total.carb_g + values.carb_g,
            total.fibre_g + values.fibre_g,
        )
    for offset in range(days):
        day = first + dt.timedelta(days=offset)
        day_macros[day] = total
        for slot, (recipe_id, portion, values) in selected.items():
            key = menu.slot_key(day, slot)
            recipe_ids[key] = recipe_id
            multipliers[key] = portion
            slot_macros[key] = values
    plan = menu.MenuPlan(
        person_id=person_id,
        segment=menu.Segment(first, last),
        recipe_ids=recipe_ids,
        multipliers=multipliers,
        day_macros=day_macros,
        slot_macros=slot_macros,
    )
    with db.tx():
        menu_id = menu.store_plan(db, plan, status="accepted", clock=clock, own_choice=True)
        shopping.build(db, menu_id, clock=clock)
        list_id = shopping.segment_list_id(db, menu_id)
    return menu_id, list_id


def _integer(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be a whole number.")
    return value


def _real(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{name} must be a number.")
    try:
        number = float(value)
    except OverflowError:
        raise ValueError(f"{name} is too large.") from None
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite.")
    return number
