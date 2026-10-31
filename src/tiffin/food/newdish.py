"""Offer new dishes for one meal without rebuilding the whole menu.

The model proposes a few candidates using indexes into the person's approved
foods. Code checks ingredients, portions and cooking constraints before storing
each accepted candidate. A person chooses through the ordinary swap flow."""

from __future__ import annotations

import datetime as dt
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..clock import Clock, isoday
from ..db import Db
from ..errors import ModelUnavailable, NothingToBuild
from . import catalogue, menu, packs, prefs, recipes, shared, swap
from .recipes import RecipeDraft

#: what the person is told this takes: one dish is a smaller ask than a week
TAKES = "usually 1-2 minutes"
#: how long a dish written for a meal stays on that meal's swap list
OFFER_DAYS = 14
#: one more call when every option failed the checks, with the reasons
MAX_CALLS = 2
_LABEL = {
    "breakfast": "breakfast",
    "lunch": "lunch",
    "dinner": "dinner",
    "snack": "snack",
    "pre_bed": "pre-bed",
}


@dataclass
class Built:
    """What one ask produced: the dishes stored, and why any the model wrote were refused."""

    day: dt.date
    slot: str
    days: tuple[dt.date, ...]
    dishes: list[dict[str, Any]] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)
    together: str | None = None
    model_used: str | None = None
    today: dt.date | None = None

    def said(self) -> str:
        meal = _LABEL.get(self.slot, self.slot)
        if not self.dishes:
            return (
                f"No new {meal} dish passed the checks this time. "
                f"{swap_command(self.day, meal, self.today or self.day)} "
                "shows the dishes you have had."
            )
        names = ", ".join(str(d["name"]) for d in self.dishes)
        # the days the swap changes, "on Mon, Wed and Fri" for a rotating lunch (#109)
        when = swap._days_words(self.days)
        return f"New dishes for {meal} {when}: {names}. Tap one to put it in."


def swap_command(day: dt.date, meal: str, today: dt.date) -> str:
    """The /swap a person can send for that meal. A weekday name always means the next one
    (telegram/menuflow.day_named), so "/swap Mon dinner" sent on a Monday opened next week's
    dinner: today's meal is "today"."""
    ahead = (day - today).days
    when = "today" if ahead == 0 else f"{day:%a}" if 0 < ahead <= 7 else isoday(day)
    return f"/swap {when} {meal}"


def _kind(day: dt.date, slot: str) -> str:
    if day.weekday() >= 5:
        return "weekend"
    return "small" if slot in recipes.SMALL_SLOTS else "main"


def _consented(db: Db, people: Sequence[int], provider: str = "claude-cli") -> bool:
    from ..model.gateway import consent_granted  # noqa: PLC0415 (model imports the db layer)

    return all(consent_granted(db, pid, provider=provider) for pid in people)


def ask(
    app: Any,
    db: Db,
    person_id: int,
    day: dt.date,
    slot: str,
    *,
    note: str = "",
    every_day: bool = True,
    clock: Clock,
) -> Built:
    """Ask the model for two or three dishes for `slot` on `day` (and the other days the same
    dish is on, with `every_day`); store the ones that pass. Raises NothingToBuild for a day that
    cannot change and ModelUnavailable when there is no model to ask - the caller degrades to the
    dishes already eaten."""
    from . import taste  # noqa: PLC0415 (taste reads menus through this package)

    today = clock.today_local()
    _seg, span = swap._span(db, person_id, day, today)
    plates = {p.slot: p for p in swap.plates_on(db, person_id, day)}
    if slot not in plates:
        raise ValueError(f"There is no {_LABEL.get(slot, slot)} on that day.")
    going = plates[slot]
    # the days a swap of every day changes: today's lunch eaten at one is not one of them
    now = clock.now_local()
    eaten = swap._eaten_at_table(db, person_id, day, slot, now) if today in span else frozenset()
    days = (
        tuple(swap._days_on(db, person_id, span, slot, going.name, today=today, eaten=eaten))
        if every_day
        else (day,)
    )
    other = swap.cooked_with(db, person_id, day, slot)  # a snack only one has is theirs alone
    people = [person_id] if other is None else [person_id, other[0]]
    config = getattr(app, "config", None)
    scope = config.model_scope() if config is not None else "claude-cli"
    if not _consented(db, people, scope):
        # her private context leaves the box only while her consent stands; on a day cooked
        # together the one call writes both plates, so both must have said yes
        raise ModelUnavailable("menu_one_dish: no consent for every plate it would write")
    gateway = app.gateway(db)
    if gateway is None:
        raise ModelUnavailable("menu_one_dish: no model gateway")
    allowed = set.intersection(*({i.code for i in catalogue.approved(db, p)} for p in people))
    items = [i for i in catalogue.approved(db, person_id) if i.code in allowed]
    if config is not None and config.get("models.shuffle_catalogue", True):
        # the week's own order, the same for the prompt and the check (catalogue.shuffled)
        items = catalogue.shuffled(items, person_id, day)
    if not items:
        raise NothingToBuild("There are no foods left to build a dish from.")
    from .. import targets as targets_mod  # noqa: PLC0415 (targets imports this package)

    profile = targets_mod.profile_on(db, person_id, day)
    blocked = sorted({n for p in people for n in menu.rated_names(db, p)[1]})
    favourites = taste.favourites_now(db, person_id, menu.rated_names(db, person_id)[0], day)
    if other is not None:
        # one dish, two plates: held to the foods both may have and to the other's taste, as the
        # weekend build's favourites are - his liked yoghurt was asked for after she refused it
        favourites = shared.for_both(db, favourites, [other[0]], day, allowed)
    rest = [f"{_LABEL.get(p.slot, p.slot)}: {p.name}" for p in plates.values() if p.slot != slot]
    on_menu = {p.name.casefold() for p in plates.values()}
    # the dishes already on the menu are named as the rest of the day, not as "do not repeat":
    # only the dish going out is ruled out by name
    recent = [
        n
        for n in menu.recent_names(db, person_id, day) + taste.resting(db, person_id, day)
        if n.casefold() not in on_menu
    ]
    kind = _kind(day, slot)
    meal = f"{_LABEL.get(slot, slot)} {swap._days_words(days)}" + (
        f", cooked together with {other[1]} (one dish, two plates)" if other else ""
    )
    target = f"about {going.macros.kcal} kcal and {going.macros.protein_g} g protein a plate"
    from ..model import prompts  # noqa: PLC0415 (model imports the db layer)

    prompt = prompts.menu_one_dish(
        ingredients=catalogue.prompt_lines(items),
        meal=meal,
        target=target,
        going=going.name,
        rest=rest,
        equipment=menu.equipment_of(profile),
        favourites=favourites[:8],
        blocked=blocked,
        recent=recent[:20],
        asked_for=note,
        kind=kind,
        # the shop window's open packs by number
        packs=packs.prompt_lines(
            list(packs.kitchen(db, person_id, day, slot, allowed=allowed).open.values()),
            {ing.code: n for n, ing in enumerate(items, start=1)},
        ),
        # everyone eating the dish: on a day cooked together it is two plates of one dish
        notes=list(
            dict.fromkeys(
                n for p in people for n in prefs.standing_notes(db, p, weekend=kind == "weekend")
            )
        ),
    )
    built = Built(day, slot, days, together=other[1] if other else None, today=clock.today_local())
    # a dish the swap list already offers under that name: one written under a favourite's name
    # was stored, then refused on tap as "not one of the ones on offer"
    had = {c.name.casefold() for c in swap.candidates(db, person_id, day, slot, today, limit=None)}
    hint = ""
    for _ in range(MAX_CALLS):
        result = gateway.json_task(
            "menu_one_dish",
            prompt + (f"\n\nThe previous answer failed: {hint}\nFix exactly that." if hint else ""),
            "one_dish",
            person_id=person_id,
        )
        built.model_used = result.model_used
        drafts, refused = check_options(
            (result.data or {}).get("options") or [],
            items,
            slot=slot,
            day=day,
            profile=profile,
            blocked=blocked,
            taken={going.name.casefold(), *on_menu, *had},
        )
        built.refused += refused
        if drafts:
            _store(db, built, drafts, items, people, clock=clock)
            return built
        hint = "; ".join(refused[:3]) or "no usable option"
    return built


def check_options(
    options: Sequence[Any],
    items: Sequence[catalogue.Ingredient],
    *,
    slot: str,
    day: dt.date,
    profile: Any,
    blocked: Sequence[str],
    taken: set[str],
    mains: Collection[str] | None = None,
) -> tuple[list[RecipeDraft], list[str]]:
    """The options that pass every check a menu's dishes pass, and why the others did not.
    The week's choices (food/choices.py) put their new dishes through the same."""
    weekend = day.weekday() >= 5
    boxed = frozenset(menu.batch_slots(profile) if profile is not None else ("lunch", "dinner"))
    ok: list[RecipeDraft] = []
    refused: list[str] = []
    names: set[str] = set()
    for raw in options:
        try:
            draft = recipes.honest_minutes(RecipeDraft.from_dict(raw))
        except ValueError as e:
            refused.append(str(e))
            continue
        why = recipes.validate(draft, items, blocked_names=blocked)
        why += recipes.simplicity_errors(draft, items, slot, weekend=weekend)
        if not weekend:
            why += menu.soup_errors({menu.slot_key(day, slot): draft}, items, boxed)
        if draft.name.casefold() in taken:
            why.append(f"{draft.name!r} is already on the menu")
        if draft.name.casefold() in names:
            why.append(f"{draft.name!r} twice")
        if mains is not None:
            # a week of choices keeps to its main proteins (food/packs.py week_mains)
            other = sorted(
                {
                    items[i.i - 1].name
                    for i in draft.ingredients
                    if 1 <= i.i <= len(items)
                    and items[i.i - 1].aisle in packs.MAIN_AISLES
                    and items[i.i - 1].code not in mains
                }
            )
            if other:
                why.append(f"cooks {', '.join(other)}, not one of this week's main proteins")
        if why:
            refused.append(f"{draft.name!r}: {'; '.join(why[:2])}")
            continue
        names.add(draft.name.casefold())
        ok.append(draft)
    return ok, refused


def _store(
    db: Db,
    built: Built,
    drafts: Sequence[RecipeDraft],
    items: Sequence[catalogue.Ingredient],
    people: Sequence[int],
    *,
    clock: Clock,
) -> None:
    if len(people) > 1:
        scope = "both"
    else:
        scope = str(db.scalar("SELECT slug FROM persons WHERE id = ?", (people[0],)) or "both")
        scope = scope if scope in ("owner", "partner") else "both"
    units = catalogue.units(db)
    with db.tx():
        for draft in drafts:
            rid = recipes.store(db, draft, items, "model", built.model_used, scope, clock=clock)
            for pid in people:
                db.insert(
                    "built_dishes",
                    person_id=pid,
                    recipe_id=rid,
                    day=isoday(built.day),
                    slot=built.slot,
                    model_used=built.model_used,
                )
            each = recipes.stored_per_serving(db, rid, 1.0, units)
            built.dishes.append(
                {
                    "recipe_id": rid,
                    "name": draft.name,
                    "kcal": each.kcal,
                    "protein_g": round(each.protein_g),
                    "prep_minutes": draft.prep_minutes,
                }
            )


def offered(db: Db, person_id: int, family: Sequence[str], weekend: bool, today: dt.date) -> list:
    """The dishes written for this person's meals of this kind lately, newest first: the swap
    list offers them beside the dishes already eaten."""
    marks = ",".join("?" for _ in family)
    return db.all(
        "SELECT rc.name AS name, MAX(b.recipe_id) AS id, MAX(b.day) AS last_day "
        "FROM built_dishes b JOIN recipes rc ON rc.id = b.recipe_id "
        f"WHERE b.person_id = ? AND b.slot IN ({marks}) "
        "AND (strftime('%w', b.day) IN ('0', '6')) = ? AND b.day >= ? GROUP BY rc.name",
        (person_id, *family, 1 if weekend else 0, isoday(today - dt.timedelta(days=OFFER_DAYS))),
    )


__all__ = ["OFFER_DAYS", "TAKES", "Built", "ask", "check_options", "offered"]
