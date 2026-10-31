"""Replace one ingredient in a dish using code-selected substitutes.

Candidates must fit the ingredient's role, preferences and shared cooking rules.
Code calculates grams and writes a new recipe row, leaving the earlier recipe
intact. Affected days are solved again through the normal swap path."""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..clock import Clock, isoday
from ..db import Db
from ..errors import NothingToBuild
from . import catalogue, recipes, swap
from .catalogue import Ingredient

#: how many foods are offered at once
OFFER_MAX = 5
#: the weights of the three parts of a food's fit
W_AFFINITY = 0.5
W_MACROS = 0.3
W_TOGETHER = 0.2
#: how far the grams may move from the food going out, either way
MAX_FACTOR = 4.0
#: grams are rounded to this, as a cook weighs
ROUND_G = 5.0

_PULSE = (
    "lentil",
    "chickpea",
    "bean",
    "edamame",
    "pea",
    "split pea",
)
_NOT_PULSE = ("green bean", "coffee", "vanilla", "cocoa", "jelly bean")
_PROTEIN_WORDS = ("tofu", "tempeh", "quorn", "seitan", "egg", "whey", "prawn")
_DAIRY_BASE = ("yoghurt", "yogurt", "skyr", "quark", "cottage cheese", "fromage frais", "kefir")
_CHEESE = (
    "cheese",
    "cheddar",
    "mozzarella",
    "feta",
    "halloumi",
    "parmesan",
    "ricotta",
    "paneer",
    "creme fraiche",
)
_FRUIT = (
    "banana",
    "apple",
    "blueberry",
    "strawberry",
    "orange",
    "raspberry",
    "berry",
    "mango",
    "pear",
    "grape",
    "kiwi",
    "melon",
    "pineapple",
    "peach",
    "plum",
)
_STARCH = (
    "potato",
    "sweet potato",
    "rice",
    "pasta",
    "spaghetti",
    "noodle",
    "couscous",
    "quinoa",
    "bread",
    "wrap",
    "oat",
    "bagel",
    "muffin",
    "bun",
    "tortilla",
    "pitta",
    "gnocchi",
)
_FAT = ("oil", "butter")
_ROLE_WORDS = {
    "pulse": "pulse",
    "protein": "protein",
    "dairy": "yoghurt or soft cheese",
    "cheese": "cheese",
    "fat": "fat",
    "fruit": "fruit",
    "starch": "starch",
    "veg": "vegetable",
}


def _words(name: str) -> str:
    return f" {catalogue.fold_term(name)} "


def _has(name: str, words: Sequence[str]) -> bool:
    folded = _words(name)
    return any(f" {catalogue.fold_term(w)} " in folded for w in words)


def _shares(ing: Ingredient) -> tuple[float, float, float]:
    """Protein, fat and carbohydrate as shares of the food's energy."""
    p, f, c = 4 * ing.protein_100, 9 * ing.fat_100, 4 * ing.carb_100
    total = p + f + c
    if total <= 0:
        return (0.0, 0.0, 0.0)
    return (p / total, f / total, c / total)


def role_of(ing: Ingredient) -> str:
    """The part a food plays in a dish, in code: what may stand in for it is a food playing the
    same part. The aisle alone is not it - lentils are shelved with the grains and chickpeas with
    the tins - and the name alone is not it either; the two together, with the macros, are."""
    name = ing.name.split(",")[0]
    p_share, f_share, c_share = _shares(ing)
    if _has(name, _PULSE) and not _has(name, _NOT_PULSE) and not _has(name, ("butter",)):
        return "pulse"
    if ing.aisle in ("meat", "fish") or _has(name, _PROTEIN_WORDS):
        return "protein"
    if _has(name, _DAIRY_BASE):
        return "dairy"
    if _has(name, _CHEESE):
        return "cheese"
    if _has(name, _FAT) and f_share > 0.8:
        return "fat"
    if _has(name, _FRUIT) and not _has(name, ("tomato",)):
        return "fruit"
    if p_share >= 0.5 and ing.kcal_100 >= 100:
        return "protein"
    if ing.aisle in ("grains", "bakery") or _has(name, _STARCH):
        return "starch" if c_share >= 0.5 else ing.aisle
    # a vegetable is low in calories whatever its shares: broccoli's are mostly protein
    if ing.aisle in ("produce", "frozen", "tins") and ing.kcal_100 < 100:
        return "veg"
    return ing.aisle


def role_words(role: str) -> str:
    return _ROLE_WORDS.get(role, role)


def grams_for(old: Ingredient, grams: float, new: Ingredient, unit: catalogue.Unit | None) -> float:
    """How much of `new` stands in for `grams` of `old`: protein for protein, a starch or a pulse
    calorie for calorie, a vegetable or fruit gram for gram; held within MAX_FACTOR either way,
    rounded as a cook weighs, and in whole ones for a food served whole. Code, never the model."""
    role = role_of(old)
    if role == "protein" and old.protein_100 > 0 and new.protein_100 > 0:
        out = grams * old.protein_100 / new.protein_100
    elif role in ("veg", "fruit"):
        out = grams
    elif old.kcal_100 > 0 and new.kcal_100 > 0:
        out = grams * old.kcal_100 / new.kcal_100
    else:
        out = grams
    out = min(max(out, grams / MAX_FACTOR), grams * MAX_FACTOR)
    if unit is not None and unit.g > 0:
        return max(1, round(out / unit.g)) * unit.g
    return max(ROUND_G, round(out / ROUND_G) * ROUND_G)


def macro_distance(old: Ingredient, new: Ingredient) -> float:
    """0 (the same macros, calorie for calorie) to 1 (nothing alike): half the L1 distance of
    the energy shares, which is what is left once the grams match the calories."""
    a, b = _shares(old), _shares(new)
    share = sum(abs(x - y) for x, y in zip(a, b, strict=True)) / 2.0
    fibre_a = (old.fibre_100 or 0.0) / max(old.kcal_100, 1.0)
    fibre_b = (new.fibre_100 or 0.0) / max(new.kcal_100, 1.0)
    fibre = min(abs(fibre_a - fibre_b) * 10.0, 1.0)
    return min(0.85 * share + 0.15 * fibre, 1.0)


@dataclass(frozen=True)
class Option:
    """A food that could stand in, with what it does to one portion of the dish."""

    id: int
    code: str
    name: str
    grams: float
    fit: float
    #: a few words on why it is offered ("you rate dishes with it well; often with chicken")
    why: str
    kcal_delta: int
    protein_delta: int

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass(frozen=True)
class _Dish:
    plate: swap.Plate
    servings: int
    parts: list[recipes.StoredIngredient]
    people: list[int]
    together: str | None


def _dish(db: Db, person_id: int, day: dt.date, slot: str, today: dt.date) -> _Dish:
    swap._span(db, person_id, day, today)
    plates = {p.slot: p for p in swap.plates_on(db, person_id, day)}
    if slot not in plates:
        raise ValueError(f"There is no {swap._LABEL.get(slot, slot)} on that day.")
    plate = plates[slot]
    servings = int(db.scalar("SELECT servings FROM recipes WHERE id = ?", (plate.recipe_id,)) or 1)
    other = swap.cooked_with(db, person_id, day, slot)  # as swap.put decides whose plates change
    people = [person_id] if other is None else [person_id, other[0]]
    return _Dish(
        plate,
        servings,
        recipes.stored_ingredients(db, plate.recipe_id),
        people,
        other[1] if other is not None else None,
    )


def _part(dish: _Dish, ingredient: int | str) -> recipes.StoredIngredient:
    for s in dish.parts:
        if ingredient in (s.ingredient.id, s.ingredient.code):
            return s
    raise ValueError("That food is not in this dish.")


def _together_counts(db: Db, person_id: int) -> tuple[dict[str, set[int]], int]:
    """code -> the recipes of this person's own that held it, for co-occurrence."""
    out: dict[str, set[int]] = {}
    for r in db.all(
        "SELECT DISTINCT ri.recipe_id, i.code FROM menus m JOIN menu_days md ON md.menu_id = m.id "
        "JOIN meal_slots ms ON ms.menu_day_id = md.id "
        "JOIN recipe_ingredients ri ON ri.recipe_id = ms.recipe_id "
        "JOIN ingredients i ON i.id = ri.ingredient_id WHERE m.person_id = ?",
        (person_id,),
    ):
        out.setdefault(str(r["code"]), set()).add(int(r["recipe_id"]))
    return out, len({rid for ids in out.values() for rid in ids})


def options(
    db: Db,
    person_id: int,
    day: dt.date,
    slot: str,
    ingredient: int | str,
    today: dt.date,
    *,
    limit: int | None = OFFER_MAX,
) -> dict[str, Any]:
    """What can stand in for one food in the dish at `slot` on `day`, best first; `limit=None`
    is every one, which is what a change checks the asked-for food against.

    NothingToBuild says why the day cannot change (food/swap.py's rules), ValueError that the
    meal or the food is not there."""
    from . import taste  # noqa: PLC0415 (taste reads menus through this package)

    dish = _dish(db, person_id, day, slot, today)
    going = _part(dish, ingredient)
    old = going.ingredient
    role = role_of(old)
    allowed = set.intersection(*({i.code for i in catalogue.approved(db, p)} for p in dish.people))
    in_dish = {s.ingredient.code for s in dish.parts}
    lean = taste.affinity(db, person_id, day)
    if len(dish.people) > 1:
        theirs = taste.affinity(db, dish.people[1], day)
        lean = {c: (lean.get(c, 0.0) + theirs.get(c, 0.0)) / 2 for c in set(lean) | set(theirs)}
    held, n_recipes = _together_counts(db, person_id)
    others = [
        s.ingredient.code
        for s in dish.parts
        if s.ingredient.code != old.code and not recipes.is_seasoning(s.ingredient.name)
    ]
    units = catalogue.units(db)
    found: list[Option] = []
    for new in catalogue.latest_versions(db):
        if new.code == old.code or new.code not in allowed or new.code in in_dish:
            continue
        if recipes.is_seasoning(new.name) != recipes.is_seasoning(old.name):
            continue
        if role_of(new) != role:
            continue
        grams = grams_for(old, going.grams, new, units.get(new.code))
        affinity = 0.5 + 0.5 * math.tanh(lean.get(new.code, 0.0))
        near = 1.0 - macro_distance(old, new)
        mine = held.get(new.code, set())
        together = (
            sum(1 for o in others if mine & held.get(o, set())) / len(others) if others else 0.0
        )
        fit = W_AFFINITY * affinity + W_MACROS * near + W_TOGETHER * together
        per = dish.servings or 1
        kcal_delta = round((grams * new.kcal_100 - going.grams * old.kcal_100) / 100 / per)
        protein_delta = round((grams * new.protein_100 - going.grams * old.protein_100) / 100 / per)
        found.append(
            Option(
                new.id,
                new.code,
                new.name.split(",")[0],
                grams,
                round(fit, 4),
                _why(lean.get(new.code, 0.0), near, together, mine, n_recipes, others),
                kcal_delta,
                protein_delta,
            )
        )
    found.sort(key=lambda o: (-o.fit, o.name))
    return {
        "day": isoday(day),
        "slot": slot,
        "dish": dish.plate.name,
        "food": {
            "id": old.id,
            "code": old.code,
            "name": old.name.split(",")[0],
            "grams": going.grams,
        },
        "role": role_words(role),
        "together": dish.together,
        "options": [o.as_dict() for o in (found if limit is None else found[:limit])],
    }


def _why(
    lean: float,
    near: float,
    together: float,
    mine: set[int],
    n_recipes: int,
    others: Sequence[str],
) -> str:
    """A few plain words on why a food is offered: a rationale is part of a choice that is the
    person's own (Ntoumanis 2021), and the three numbers mean nothing said as numbers."""
    parts: list[str] = []
    if lean > 0.15:
        parts.append("in dishes you rate well")
    elif lean < -0.15:
        parts.append("in dishes you rated lower")
    if near >= 0.85:
        parts.append("much the same macros")
    elif near < 0.6:
        parts.append("different macros")
    if together >= 0.5 and others and mine:
        parts.append("you have had it with these before")
    elif not mine and n_recipes:
        parts.append("new to your menus")
    return "; ".join(parts[:2])


def _renamed(text: str, old: Ingredient, new: Ingredient) -> str:
    """The old food's name changed for the new one's in a dish name or a step, matching the
    singular and the plural ("chickpea" and "chickpeas") and the first word ("lentils" for "Red
    lentils"). A word that does not appear leaves the text as it was."""
    old_short = old.name.split(",")[0].strip()
    new_short = new.name.split(",")[0].strip()
    forms = {old_short, catalogue.fold_term(old_short)}
    words = old_short.split()
    if len(words) > 1:
        forms.add(words[-1])
        forms.add(catalogue.fold_term(words[-1]))
    out = text
    for form in sorted(forms, key=len, reverse=True):
        if not form:
            continue
        pattern = re.compile(rf"\b{re.escape(form)}s?\b", re.I)
        if pattern.search(out):

            def put_case(m: re.Match[str]) -> str:
                said = m.group(0)
                # "Lentil dal" stays singular ("Chickpea dal"); "the lentils" stays plural
                plural = said.casefold().endswith("s")
                out = new_short if plural else _single(new_short)
                return out[:1].upper() + out[1:] if said[:1].isupper() else out.lower()

            return pattern.sub(put_case, out)
    return out


def _single(name: str) -> str:
    """ "Kidney beans" -> "Kidney bean": the last word in the singular, the case kept."""
    words = name.split()
    if not words:
        return name
    last = catalogue.fold_term(words[-1])
    if words[-1][:1].isupper():
        last = last[:1].upper() + last[1:]
    return " ".join([*words[:-1], last])


def draft_with(
    db: Db, recipe_id: int, old: Ingredient, new: Ingredient, grams: float
) -> tuple[recipes.RecipeDraft, list[Ingredient]]:
    """The recipe with `new` in place of `old`, as a draft over its own numbered foods (the
    other foods keep the catalogue version they were cooked with)."""
    row = db.one("SELECT * FROM recipes WHERE id = ?", (recipe_id,))
    if row is None:
        raise LookupError(f"no recipe with id {recipe_id}")
    method = [str(s) for s in json.loads(str(row["method_json"] or "[]"))]
    if not method:
        # a dish written down without its steps (typed in by hand) still says what changed
        old_short = old.name.split(",")[0].strip().lower()
        method = [f"As before, with {new.name.split(',')[0].strip().lower()} for the {old_short}."]
    foods: list[Ingredient] = []
    grams_of: dict[int, float] = {}
    for s in recipes.stored_ingredients(db, recipe_id):
        food = new if s.ingredient.code == old.code else s.ingredient
        g = grams if s.ingredient.code == old.code else s.grams
        at = next((k for k, f in enumerate(foods) if f.code == food.code), None)
        if at is None:
            foods.append(food)
            grams_of[len(foods)] = g
        else:
            grams_of[at + 1] += g
    name = _renamed(str(row["name"]), old, new)
    if name == str(row["name"]):
        with_it = f"{name} with {new.name.split(',')[0].strip().lower()}"
        name = with_it if len(with_it) <= recipes.MAX_NAME else name
    draft = recipes.RecipeDraft(
        name=name[: recipes.MAX_NAME],
        ingredients=tuple(recipes.DraftIngredient(i, g) for i, g in grams_of.items()),
        method=tuple(_renamed(step, old, new) for step in method),
        prep_minutes=int(row["prep_minutes"]),
        servings=int(row["servings"]),
        keep_days=int(row["keep_days"]),
        freezable=bool(row["freezable"]),
    )
    return draft, foods


def substitute(
    db: Db,
    person_id: int,
    day: dt.date,
    slot: str,
    ingredient: int | str,
    new_ingredient: int | str,
    *,
    every_day: bool = True,
    config: Any,
    clock: Clock,
) -> swap.Swapped:
    """`new_ingredient` in place of `ingredient` in the dish at `slot` on `day` (and every day
    the same dish is on, with `every_day`), the days solved again and put in force.

    The new food must be one `options` offers: an ingredient id is never taken on trust from
    whoever asks. Raises what `swap.put` raises, and ValueError for a food not on offer."""
    today = clock.today_local()
    # the shortlist is the best few; any food playing the same part is still allowed
    got = options(db, person_id, day, slot, ingredient, today, limit=None)
    offered: dict[Any, Mapping[str, Any]] = {o["id"]: o for o in got["options"]}
    offered.update({o["code"]: o for o in got["options"]})
    pick = offered.get(new_ingredient)
    if pick is None:
        raise ValueError("That food is not one that can stand in here.")
    dish = _dish(db, person_id, day, slot, today)
    going = _part(dish, ingredient)
    new = next(i for i in catalogue.latest_versions(db) if i.code == pick["code"])
    draft, foods = draft_with(db, dish.plate.recipe_id, going.ingredient, new, float(pick["grams"]))
    # on a day cooked together the new dish goes on both plates, so it is both people's (#75)
    shared_day = swap.cooked_with(db, person_id, day, slot) is not None
    scope = (
        "both"
        if shared_day
        else str(
            db.scalar("SELECT person_scope FROM recipes WHERE id = ?", (dish.plate.recipe_id,))
            or "both"
        )
    )
    try:
        new_id = recipes.store(db, draft, foods, "manual", None, scope, clock=clock)
    except ValueError as e:
        raise NothingToBuild(f"That change would not make a dish I can store: {e}") from e

    def record() -> None:
        for code, kind in ((going.ingredient.code, "swapped_out"), (new.code, "swapped_in")):
            db.insert(
                "ingredient_events",
                person_id=person_id,
                code=code,
                recipe_id=new_id,
                kind=kind,
                day=isoday(day),
            )

    return swap.put(
        db,
        person_id,
        day,
        slot,
        new_id,
        draft.name,
        every_day=every_day,
        config=config,
        clock=clock,
        record=record,
    )


__all__ = [
    "Option",
    "draft_with",
    "grams_for",
    "macro_distance",
    "options",
    "role_of",
    "substitute",
]
