"""Validate model recipe drafts against numbered catalogue entries.

Nutrients are calculated from ingredient grams in code."""

from __future__ import annotations

import dataclasses
import json
import math
import re
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..clock import Clock
from ..db import Db
from ..energy import Macros, round_int
from .catalogue import Ingredient, Unit
from .solver import UnitPart, whole_units
from .text import normalise

MAX_NAME = 60
MAX_INGREDIENTS = 12
MAX_STEPS = 12
MAX_PREP_MINUTES = 120
MAX_GRAMS = 5000.0
MAX_SERVINGS = 20
SOURCES = ("model", "manual")


# ---------------------------------------------------------------- draft
@dataclass(frozen=True)
class DraftIngredient:
    i: int
    g: float


@dataclass(frozen=True)
class RecipeDraft:
    name: str
    ingredients: tuple[DraftIngredient, ...]
    method: tuple[str, ...]
    prep_minutes: int
    servings: int = 1
    keep_days: int = 3
    freezable: bool = False

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> RecipeDraft:
        """Parse the model's JSON object; anything of the wrong shape raises ValueError."""
        try:
            name = str(d["name"])
            raw_ings = d["ingredients"]
            method = d.get("method") or []
            prep = int(d.get("prep_minutes", 0))
            servings = int(d.get("servings", 1))
            keep_days = int(d.get("keep_days", 3))
            freezable = bool(d.get("freezable", False))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"recipe draft: {exc}") from exc
        if not isinstance(raw_ings, list) or not isinstance(method, list):
            raise ValueError("recipe draft: ingredients and method must be lists")
        ings: list[DraftIngredient] = []
        for entry in raw_ings:
            if not isinstance(entry, Mapping) or "i" not in entry or "g" not in entry:
                raise ValueError("recipe draft: each ingredient needs i and g")
            index, grams = entry["i"], entry["g"]
            # index-not-quote only works if the index is an index. `int()` turned JSON true
            # into ingredient 1 and 2.7 into ingredient 2, both of which then validated
            # cleanly and were cooked. `matching.match_one` guards the same job explicitly;
            # this is the other wall and it was not checking.
            if isinstance(index, bool) or isinstance(grams, bool):
                raise ValueError(f"recipe draft: ingredient {entry!r}: true is not a number")
            try:
                whole = int(index)  # a model answering "2" for an index is still answering 2
                if whole != float(index):
                    raise ValueError(f"index {index!r} is not a whole number")
                ings.append(DraftIngredient(whole, float(grams)))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"recipe draft: ingredient {entry!r}: {exc}") from exc
        return cls(
            name=name.strip(),
            ingredients=tuple(ings),
            method=tuple(str(s).strip() for s in method),
            prep_minutes=prep,
            servings=servings,
            keep_days=keep_days,
            freezable=freezable,
        )


def validate(
    draft: RecipeDraft, catalogue: Sequence[Ingredient], *, blocked_names: Iterable[str] = ()
) -> list[str]:
    """Every reason the draft is unusable; empty means valid."""
    errors: list[str] = []
    name = draft.name.strip()
    if not name:
        errors.append("name: empty")
    elif len(name) > MAX_NAME:
        errors.append(f"name: {len(name)} chars > {MAX_NAME}")
    n = len(catalogue)
    if not draft.ingredients:
        errors.append("ingredients: none")
    elif len(draft.ingredients) > MAX_INGREDIENTS:
        errors.append(f"ingredients: {len(draft.ingredients)} > {MAX_INGREDIENTS}")
    seen: set[int] = set()
    for ing in draft.ingredients:
        if not 1 <= ing.i <= n:
            errors.append(f"ingredient index {ing.i}: out of range 1..{n}")
            continue
        item = catalogue[ing.i - 1]
        if not item.approved:
            errors.append(f"ingredient {ing.i} ({item.name}): not approved")
        if ing.i in seen:
            errors.append(f"ingredient {ing.i} ({item.name}): listed twice")
        seen.add(ing.i)
        if not (ing.g > 0 and math.isfinite(ing.g)) or ing.g > MAX_GRAMS:
            errors.append(f"ingredient {ing.i} ({item.name}): {ing.g} g outside 0..{MAX_GRAMS:.0f}")
    if not draft.method:
        errors.append("method: no steps")
    elif len(draft.method) > MAX_STEPS:
        errors.append(f"method: {len(draft.method)} steps > {MAX_STEPS}")
    if not 0 <= draft.prep_minutes <= MAX_PREP_MINUTES:
        errors.append(f"prep_minutes: {draft.prep_minutes} outside 0..{MAX_PREP_MINUTES}")
    if not 1 <= draft.servings <= MAX_SERVINGS:
        errors.append(f"servings: {draft.servings} outside 1..{MAX_SERVINGS}")
    blocked = {normalise(b) for b in blocked_names}
    if name and normalise(name) in blocked:
        errors.append(f"name: {name!r} is blocked (rated 2 or under)")
    return errors


# ---------------------------------------------------------------- simple
# healthy." That breakfast was seven ingredients, six steps, an air fryer AND a hob, and a
# "meanwhile" - a two-pan juggle before work. The caps above are what a photographed recipe may
# hold; these are what a menu the model writes may hold, because somebody cooks it after work.
# They apply to model menus only (`menu.build_plan`): a recipe someone photographed is theirs.
SIMPLE_INGREDIENTS = 8  # to shop for: salt, pepper and oil are not counted
SIMPLE_STEPS = 6
SIMPLE_MINUTES = 30
SIMPLE_APPLIANCES = 2
#: the small slots are made half-awake or at a desk: one appliance, four steps, ten minutes,
#: six things to shop for. Every real breakfast, snack and pre-bed that passed on 14 Sep sat
#: inside these (the sweep measured all 21 dishes on the box), and nothing stopped a 25-minute
#: "breakfast" before. One appliance may be a pan: plain eggs on toast is simple, and she likes
#: eggs; what was too complex was eggs WITH bacon, two appliances and a "meanwhile".
SMALL_SLOTS = ("breakfast", "snack", "pre_bed")
SMALL_STEPS = 4
SMALL_MINUTES = 10
SMALL_INGREDIENTS = 6

APPLIANCES: dict[str, re.Pattern[str]] = {
    "air fryer": re.compile(r"\bair[- ]?fr", re.I),
    "oven": re.compile(r"\boven\b|\broast\b|baking tray|\bbake\b", re.I),
    "hob": re.compile(r"\bhob\b|\bpan\b|\bsimmer|\bboil|(?<!air )(?<!air-)\bfr(?:y|ied)\b", re.I),
    "microwave": re.compile(r"\bmicrowave", re.I),
}
#: water boiled in a kettle and poured: no hob, on a day nothing is cooked
_KETTLE = re.compile(
    r"\b(?:freshly |just )?boil(?:ed|ing)? water\b|\bkettle\b|\bboiled in a kettle\b", re.I
)


def cooking_without_kettle(draft: RecipeDraft) -> list[str]:
    """Find appliances used by a method, excluding kettle water from hob use."""
    out: list[str] = []
    for name, pattern in APPLIANCES.items():
        if any(pattern.search(_KETTLE.sub(" ", step)) for step in draft.method):
            out.append(name)
    return out


#: a cooking vessel a step introduces ("in a large pan"); "the pan" is the same one again
_NEW_PAN = re.compile(
    r"\ba (?:large |small |big |deep |non-stick |dry |frying )*(?:sauce)?pan\b", re.I
)
#: two things on the go at once - what a nervous cook cannot do
_JUGGLING = re.compile(
    r"\bmeanwhile\b|\bat the same time\b|\bwhile (?:it|that|they|the \w+) "
    r"(?:cooks?|simmers?|bakes?|roasts?|boils?)\b|\bin the (?:last|final) \d+ minutes?\b",
    re.I,
)
_MINUTES = re.compile(r"(\d+)(?:\s*(?:to|-)\s*(\d+))?\s*(?:minutes?|mins?)\b", re.I)
_HOURS = re.compile(r"(\d+)(?:\s*(?:to|-)\s*(\d+))?\s*hours?\b", re.I)


def is_seasoning(name: str) -> bool:
    """Salt, pepper and oil: nobody shops for them and nobody finds a card harder for them."""
    low = name.casefold().strip()
    return low.startswith(("salt", "black pepper", "pepper, black")) or low.endswith(" oil")


@dataclass(frozen=True)
class Effort:
    """A card judged the way a person cooking after work judges it."""

    to_shop: int
    steps: int
    appliances: tuple[str, ...]
    pans: int
    juggling: int
    #: the minutes the method itself names, one step after another
    method_minutes: int


def effort(draft: RecipeDraft, catalogue: Sequence[Ingredient]) -> Effort:
    n = len(catalogue)
    names = [catalogue[i.i - 1].name for i in draft.ingredients if 1 <= i.i <= n]
    return Effort(
        to_shop=sum(1 for name in names if not is_seasoning(name)),
        steps=len(draft.method),
        appliances=tuple(
            a for a, rx in APPLIANCES.items() if any(rx.search(s) for s in draft.method)
        ),
        pans=sum(len(_NEW_PAN.findall(s)) for s in draft.method),
        juggling=sum(1 for s in draft.method if _JUGGLING.search(s)),
        method_minutes=method_minutes(draft.method),
    )


#: a stretch of a step nobody is working through: it sits in the fridge, sets, soaks, rests
_PASSIVE = re.compile(
    r"\b(?:chill|set|sets|soak|soaks|refrigerat\w*|fridge|overnight|marinat\w*|rest|rests|"
    r"cool|cools|thicken|thickens|freez\w*|stand|stands)\b",
    re.I,
)
#: ...unless the same stretch is also cooking
_ACTIVE = re.compile(
    r"\b(?:bake|roast|fry|fries|simmer|boil|cook|grill|air[- ]?fry|microwave|heat|toast|"
    r"stir[- ]?fry|poach|steam|sear|brown)\w*\b",
    re.I,
)
#: where a step's stretches part: "Stir, then chill for 2 hours"
_CLAUSE = re.compile(r"[.;,]|\bthen\b|\band\b", re.I)


def method_minutes(method: Iterable[str]) -> int:
    """Every minute the steps name, added up, taking the top of a range - but not the minutes a
    dish only sits: "leave to set in the fridge for 2 hours" is no work, and a chia pudding was
    refused as 126 minutes of cooking for a person who asks for chia puddings. A
    stretch that also cooks counts in full."""
    total = 0
    for step in method:
        if _PASSIVE.search(step) and not _ACTIVE.search(step):
            continue  # the whole step only waits: "set in the fridge overnight, 8 hours"
        for clause in _CLAUSE.split(step):
            if _PASSIVE.search(clause) and not _ACTIVE.search(clause):
                continue
            for lo, hi in _MINUTES.findall(clause):
                total += int(hi or lo)
            for lo, hi in _HOURS.findall(clause):
                total += 60 * int(hi or lo)
    return total


def honest_minutes(draft: RecipeDraft) -> RecipeDraft:
    """prep_minutes raised to what the method itself says.

    A card said "10 min" over a method that roasts for 35: the model's number was a guess and
    the method's was the recipe. Somebody plans their evening around the number on the card, so
    the card carries the larger. The model never does arithmetic; this is code adding up.
    """
    stated = method_minutes(draft.method)
    if stated <= draft.prep_minutes:
        return draft
    return dataclasses.replace(draft, prep_minutes=stated)


def simplicity_errors(
    draft: RecipeDraft,
    catalogue: Sequence[Ingredient],
    slot: str,
    *,
    weekend: bool = False,
    main_minutes: int = 0,
) -> list[str]:
    """Every way this dish is more than a tired person should be handed; empty means simple.

    weekend if it's really good food and happy to spend time really cooking"). Weekend limits of
    8 steps and 60 minutes turned away egg and bacon breakfast muffins with a "meanwhile" and
    cornflake chicken with chips on the first treat weekend. What still binds is `validate`:
    what a card and the `recipes` table hold (MAX_INGREDIENTS, MAX_STEPS, MAX_PREP_MINUTES).

    `main_minutes` is a person's own longest lunch or dinner, prep included (food/weekplan.py
    """
    if weekend:
        return []
    e = effort(draft, catalogue)
    small = slot in SMALL_SLOTS
    max_steps = SMALL_STEPS if small else SIMPLE_STEPS
    max_appliances = 1 if small else SIMPLE_APPLIANCES
    max_ingredients = SMALL_INGREDIENTS if small else SIMPLE_INGREDIENTS
    max_minutes = SMALL_MINUTES if small else (main_minutes or SIMPLE_MINUTES)
    errors: list[str] = []
    if e.to_shop > max_ingredients:
        errors.append(
            f"{e.to_shop} ingredients > {max_ingredients} (salt, pepper and oil not counted)"
        )
    if e.steps > max_steps:
        errors.append(f"{e.steps} steps > {max_steps}")
    if len(e.appliances) > max_appliances:
        errors.append(
            f"needs {' and '.join(e.appliances)}; at most {max_appliances} "
            f"appliance{'s' if max_appliances > 1 else ''}"
        )
    if e.pans > 1:
        errors.append(f"{e.pans} pans; one pan or one tray")
    if e.juggling:
        errors.append("two things at once (meanwhile); one thing at a time")
    minutes = max(draft.prep_minutes, e.method_minutes)
    if minutes > max_minutes:
        errors.append(f"{minutes} minutes by its own method > {max_minutes}")
    return errors


# ---------------------------------------------------------------- macros
@dataclass(frozen=True)
class RecipeMacros:
    """Integer kcal, one-decimal grams. `as_macros()` is the integer boundary type."""

    kcal: int
    protein_g: float
    fat_g: float
    carb_g: float
    fibre_g: float
    total_g: float

    def as_macros(self) -> Macros:
        return Macros(
            self.kcal,
            round_int(self.protein_g),
            round_int(self.fat_g),
            round_int(self.carb_g),
            round_int(self.fibre_g),
        )


def _exact(
    pairs: Iterable[tuple[Ingredient, float]],
) -> tuple[float, float, float, float, float, float]:
    kcal = protein = fat = carb = fibre = grams = 0.0
    for item, g in pairs:
        f = g / 100.0
        kcal += item.kcal_100 * f
        protein += item.protein_100 * f
        fat += item.fat_100 * f
        carb += item.carb_100 * f
        fibre += (item.fibre_100 or 0.0) * f
        grams += g
    return kcal, protein, fat, carb, fibre, grams


def _rounded(exact: tuple[float, ...], factor: float = 1.0) -> RecipeMacros:
    kcal, protein, fat, carb, fibre, grams = (v * factor for v in exact)
    return RecipeMacros(
        kcal=round_int(kcal),
        protein_g=round(protein, 1),
        fat_g=round(fat, 1),
        carb_g=round(carb, 1),
        fibre_g=round(fibre, 1),
        total_g=round(grams, 1),
    )


def _pairs(draft: RecipeDraft, catalogue: Sequence[Ingredient]) -> list[tuple[Ingredient, float]]:
    return [(catalogue[ing.i - 1], ing.g) for ing in draft.ingredients]


def totals(draft: RecipeDraft, catalogue: Sequence[Ingredient]) -> RecipeMacros:
    """The whole recipe (all servings)."""
    return _rounded(_exact(_pairs(draft, catalogue)))


def plated(
    pairs: Iterable[tuple[Ingredient, float]], factor: float, units: Mapping[str, Unit]
) -> list[tuple[Ingredient, float]]:
    """Each ingredient's grams on one plate: scaled by `factor`, then a food served whole put
    down in whole ones (`solver.whole_units`) - the way the solver counted the day."""
    out: list[tuple[Ingredient, float]] = []
    for item, g in pairs:
        grams = g * factor
        unit = units.get(item.code)
        out.append((item, whole_units(grams, unit.g) if unit is not None else grams))
    return out


def unit_words(unit_name: str | None, unit_g: float | None, grams: float) -> str:
    """ "2 slices" for a food served whole at a whole number of them, "" otherwise. The page's
    `unitWords`, so the card in the chat and the card on the page count one plate the same way."""
    if not unit_name or not unit_g:
        return ""
    n = grams / unit_g
    whole = round(n)
    if whole < 1 or abs(n - whole) > 0.01:
        return ""
    return f"{whole} {unit_name if whole == 1 else unit_name + 's'}"


def card_grams(ingredient: Mapping[str, Any], servings: int) -> float:
    """The grams a card line gives: one plate as it is plated (whole slices, eggs and buns) for a
    one-serving dish, the whole recipe's grams for a pot cooked for several. The bot printed the
    straight-line grams - "Burger buns 36 g" beside a kcal line that counted the whole bun."""
    if servings == 1 and ingredient.get("grams_per_serving") is not None:
        return float(ingredient["grams_per_serving"])
    return float(ingredient["grams"])


def per_serving(
    draft: RecipeDraft,
    catalogue: Sequence[Ingredient],
    multiplier: float = 1.0,
    units: Mapping[str, Unit] | None = None,
) -> RecipeMacros:
    """One serving x multiplier, rounded once from the exact totals. With `units`, a food served
    whole (bread by the slice, eggs, buns) counts in whole ones, as it is plated."""
    if not units:
        return _rounded(_exact(_pairs(draft, catalogue)), multiplier / draft.servings)
    return _rounded(_exact(plated(_pairs(draft, catalogue), multiplier / draft.servings, units)))


def _unit_parts(
    per_serving_pairs: Iterable[tuple[Ingredient, float]], units: Mapping[str, Unit]
) -> tuple[UnitPart, ...]:
    out: list[UnitPart] = []
    for item, g in per_serving_pairs:
        unit = units.get(item.code)
        if unit is None or g <= 0:
            continue
        out.append(
            UnitPart(
                grams=g,
                unit_g=unit.g,
                kcal=item.kcal_100 / 100.0,
                protein=item.protein_100 / 100.0,
                fat=item.fat_100 / 100.0,
                carb=item.carb_100 / 100.0,
                fibre=(item.fibre_100 or 0.0) / 100.0,
            )
        )
    return tuple(out)


def draft_unit_parts(
    draft: RecipeDraft, catalogue: Sequence[Ingredient], units: Mapping[str, Unit]
) -> tuple[UnitPart, ...]:
    """The whole-unit foods of one serving of a draft, for the solver's table."""
    return _unit_parts(((i, g / draft.servings) for i, g in _pairs(draft, catalogue)), units)


def macros(draft: RecipeDraft, catalogue: Sequence[Ingredient]) -> Macros:
    """Whole-recipe integer macros (grams/100 x per-100 g, summed)."""
    return totals(draft, catalogue).as_macros()


# ---------------------------------------------------------------- storage
def store(
    db: Db,
    draft: RecipeDraft,
    catalogue: Sequence[Ingredient],
    source: str,
    model_used: str | None,
    person_scope: str,
    *,
    clock: Clock | None = None,
) -> int:
    """Write recipes + recipe_ingredients (pinned to the catalogue's ingredient version ids)."""
    if source not in SOURCES:
        raise ValueError(f"source must be one of {SOURCES}")
    errors = validate(draft, catalogue)
    if errors:
        raise ValueError("invalid recipe: " + "; ".join(errors))
    clk = clock or db.clock
    same = identical(db, draft, catalogue, person_scope)
    if same is not None:
        # The same dish on five days is ONE dish. `build_plan` calls this once per day-slot, so a
        # chose - wrote five byte-identical `recipes` rows per meal and twenty-five in total for
        # five dishes. Everything that then asks "how many of this dish?" by recipe_id counts
        # one: `/batch` told him to cook 20 boxes of 20 different things, when the week is 5
        # dishes at 4 boxes each, which is the entire point of batch cooking.
        #
        # Identity is the name, the method, the servings and the exact ingredient grams - what a
        # cook would call the same dish. Not the row id.
        return same
    with db.tx():
        recipe_id = db.insert(
            "recipes",
            name=draft.name.strip(),
            servings=draft.servings,
            prep_minutes=draft.prep_minutes,
            method_json=json.dumps(list(draft.method)),
            source=source,
            model_used=model_used,
            person_scope=person_scope,
            keep_days=draft.keep_days,
            freezable=int(draft.freezable),
            created_at=clk.utc_stamp(),
        )
        for ing in draft.ingredients:
            db.insert(
                "recipe_ingredients",
                recipe_id=recipe_id,
                ingredient_id=catalogue[ing.i - 1].id,
                grams=float(ing.g),
            )
    return recipe_id


@dataclass(frozen=True)
class StoredIngredient:
    ingredient: Ingredient
    grams: float


def identical(
    db: Db, draft: RecipeDraft, catalogue: Sequence[Ingredient], person_scope: str
) -> int | None:
    """An existing recipe a cook would call the same dish, or None.

    Name, servings and method first (cheap and highly selective), then the exact ingredient
    grams against the same catalogue version ids. Two dishes that share a name but differ by a
    gram are different dishes and both get a row - the point is not to merge anything, only to
    stop writing the same row five times.
    """
    wanted = [(catalogue[ing.i - 1].id, float(ing.g)) for ing in draft.ingredients]
    for row in db.all(
        "SELECT id FROM recipes WHERE name = ? AND servings = ? AND method_json = ? "
        "AND person_scope = ? ORDER BY id DESC LIMIT 20",
        (draft.name.strip(), draft.servings, json.dumps(list(draft.method)), person_scope),
    ):
        have = [
            (int(r["ingredient_id"]), float(r["grams"]))
            for r in db.all(
                "SELECT ingredient_id, grams FROM recipe_ingredients WHERE recipe_id = ? "
                "ORDER BY id",
                (int(row["id"]),),
            )
        ]
        if have == wanted:
            return int(row["id"])
    return None


def stored_ingredients(db: Db, recipe_id: int) -> list[StoredIngredient]:
    rows = db.all(
        "SELECT ri.grams, i.* FROM recipe_ingredients ri JOIN ingredients i "
        "ON i.id = ri.ingredient_id WHERE ri.recipe_id = ? ORDER BY ri.id",
        (recipe_id,),
    )
    return [StoredIngredient(Ingredient.from_row(r), float(r["grams"])) for r in rows]


def stored_totals(db: Db, recipe_id: int) -> RecipeMacros:
    return _rounded(_exact((s.ingredient, s.grams) for s in stored_ingredients(db, recipe_id)))


def stored_per_serving(
    db: Db, recipe_id: int, multiplier: float = 1.0, units: Mapping[str, Unit] | None = None
) -> RecipeMacros:
    row = _recipe_row(db, recipe_id)
    pairs = [(s.ingredient, s.grams) for s in stored_ingredients(db, recipe_id)]
    factor = multiplier / int(row["servings"])
    if not units:
        return _rounded(_exact(pairs), factor)
    return _rounded(_exact(plated(pairs, factor, units)))


def stored_unit_parts(db: Db, recipe_id: int, units: Mapping[str, Unit]) -> tuple[UnitPart, ...]:
    """The whole-unit foods of one serving of a stored recipe, for the solver's table."""
    servings = int(_recipe_row(db, recipe_id)["servings"]) or 1
    return _unit_parts(
        ((s.ingredient, s.grams / servings) for s in stored_ingredients(db, recipe_id)), units
    )


def plate_g(db: Db, recipe_id: int, multiplier: float, units: Mapping[str, Unit]) -> float:
    """The grams on one plate of a stored recipe at `multiplier`, whole units whole."""
    servings = int(_recipe_row(db, recipe_id)["servings"]) or 1
    pairs = [(s.ingredient, s.grams) for s in stored_ingredients(db, recipe_id)]
    return sum(g for _i, g in plated(pairs, multiplier / servings, units))


def _recipe_row(db: Db, recipe_id: int) -> sqlite3.Row:
    row = db.one("SELECT * FROM recipes WHERE id = ?", (recipe_id,))
    if row is None:
        raise LookupError(f"no recipe with id {recipe_id}")
    return row


def card(
    db: Db, recipe_id: int, portion_multiplier: float = 1.0, *, person_id: int | None = None
) -> dict[str, Any]:
    """The full recipe card: name, servings, per-serving macros at the portion
    multiplier, ingredients with grams and pack sizes, steps, prep time, fridge/freezer life.

    `person_id`, when given, swaps a generic ingredient's pack for the product this person has
    actually chosen to buy (numbers_units/11): the card is read at the counter with the pack
    that came home from the shop in hand, and the catalogue's default pack is not always it.
    """
    from .catalogue import units as unit_table  # noqa: PLC0415 (avoids a module cycle)
    from .products import chosen as chosen_products  # noqa: PLC0415 (avoids a module cycle)

    row = _recipe_row(db, recipe_id)
    servings = int(row["servings"])
    stored = stored_ingredients(db, recipe_id)
    exact = _exact((s.ingredient, s.grams) for s in stored)
    whole = _rounded(exact, portion_multiplier)
    units = unit_table(db)
    on_plate = plated(
        ((s.ingredient, s.grams) for s in stored), portion_multiplier / servings, units
    )
    serving = _rounded(_exact(on_plate))
    picked = chosen_products(db, person_id) if person_id is not None else {}

    def _pack(s: StoredIngredient) -> tuple[float, str]:
        product = picked.get(s.ingredient.code)
        if product is not None and product.pack_g:
            return product.pack_g, (product.pack_label or s.ingredient.pack_label)
        return s.ingredient.pack_g, s.ingredient.pack_label

    return {
        "recipe_id": recipe_id,
        "name": str(row["name"]),
        "servings": servings,
        "portion_multiplier": portion_multiplier,
        "prep_minutes": int(row["prep_minutes"]),
        "steps": list(json.loads(str(row["method_json"]))),
        "keep_days": int(row["keep_days"]),
        "freezable": bool(row["freezable"]),
        "source": str(row["source"]),
        "model_used": row["model_used"],
        "person_scope": str(row["person_scope"]),
        "ingredients": [
            {
                "code": s.ingredient.code,
                "name": s.ingredient.name,
                "grams": round(s.grams * portion_multiplier, 1),
                "grams_per_serving": round(plate, 1),
                "pack_g": _pack(s)[0],
                "pack_label": _pack(s)[1],
                "unit_name": units[s.ingredient.code].name if s.ingredient.code in units else None,
                "unit_g": units[s.ingredient.code].g if s.ingredient.code in units else None,
            }
            for s, (_item, plate) in zip(stored, on_plate, strict=True)
        ],
        "per_serving": {
            "kcal": serving.kcal,
            "protein_g": serving.protein_g,
            "fat_g": serving.fat_g,
            "carb_g": serving.carb_g,
            "fibre_g": serving.fibre_g,
        },
        "portion_g": serving.total_g,
        "total": {
            "kcal": whole.kcal,
            "protein_g": whole.protein_g,
            "fat_g": whole.fat_g,
            "carb_g": whole.carb_g,
            "fibre_g": whole.fibre_g,
            "grams": whole.total_g,
        },
    }
