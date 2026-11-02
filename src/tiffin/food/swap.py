"""One dish on a menu swapped for another the person has already eaten - no model, no waiting.

only way to move a dish, and a Change asks the model for a whole new menu: one to four calls of
a minute or more each, every dish replaced when one was wanted, and paid for every time. The
dishes a person has eaten before are already in the database - checked for simplicity when they
were built, with their grams, their method and the score they were given - so swapping one is a
question code can answer in a second.

What can come in: a dish from this person's own menus, at the same kind of meal (lunch and
dinner are both main meals; a breakfast stays a breakfast) and the same kind of day (a weekend
treat is not a Tuesday lunch), with nothing in it they now avoid and not rated "never again".
On a weekend cooked together it has to suit both of them, and both plates change (a snack only
one of them has is theirs alone).

What changes: the dish goes in on every day still to come that the old one was on - a pot is
cooked once for its days - or on the one day, if that is what was asked. Each changed day is
solved again in code, the other plates kept as near their size as the day allows; a box already
cooked, and a meal already eaten today, stay as they were. The result is stored the way a
rebuilt menu is - one menu for the days left, decided now - so the shopping list, the batch plan
and the page all follow it; a day the person resized stays resized over it.
"""

from __future__ import annotations

import datetime as dt
import itertools
import json
from collections import defaultdict
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from .. import links
from .. import targets as targets_mod
from ..clock import Clock, isoday, local_at, meal_now, parse_day
from ..db import Db
from ..energy import Macros
from ..errors import Infeasible, NothingToBuild
from . import catalogue, menu, packs, recipes, shared, solver

#: how many dishes are offered at once: enough to choose from on a phone, few enough to read
OFFER_MAX = 6
#: how many of them are kept for dishes the model has just written for the meal: one ask's worth
NEW_KEPT = 3
#: meals that can stand in for one another: lunch and dinner are both a main meal
_FAMILY = {"lunch": ("lunch", "dinner"), "dinner": ("lunch", "dinner")}
#: how near the day's calories land when a dish is swapped in: tight first, as a resize is
_TRIES = (
    (2.0, False),
    (float(solver.DEFAULT_KCAL_TOL_PCT), False),
    (float(solver.DEFAULT_KCAL_TOL_PCT), True),
)
_LABEL = {
    "breakfast": "breakfast",
    "lunch": "lunch",
    "dinner": "dinner",
    "snack": "snack",
    "pre_bed": "pre-bed",
}


@dataclass(frozen=True)
class Candidate:
    """A dish that could go in, with one portion's numbers - computed in code, as every macro."""

    recipe_id: int
    name: str
    kcal: int
    protein_g: int
    fat_g: int
    carb_g: int
    fibre_g: int
    prep_minutes: int
    score: int | None
    last_day: str
    #: eaten lately enough that it would usually wait (food/taste.py): said, not hidden
    resting: bool = False
    #: written by the model for this meal and never eaten yet (food/newdish.py)
    new: bool = False
    #: a few words on why it is offered: "rated 5; last had 3 weeks ago" (food/taste.py)
    why: str = ""
    #: on their plate on a day before today: a dish they have had, not one only on a menu
    had: bool = False
    #: what in the kitchen it cooks from: "cooks from the open Chicken thigh fillets" (packs.py)
    kitchen: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "recipe_id": self.recipe_id,
            "name": self.name,
            "kcal": self.kcal,
            "protein_g": self.protein_g,
            "fat_g": self.fat_g,
            "carb_g": self.carb_g,
            "fibre_g": self.fibre_g,
            "prep_minutes": self.prep_minutes,
            "score": self.score,
            "last_day": self.last_day,
            "resting": self.resting,
            "new": self.new,
            "why": self.why,
            "had": self.had,
            "kitchen": self.kitchen,
        }


@dataclass(frozen=True)
class Plate:
    """One plate in force on a day."""

    slot: str
    recipe_id: int
    name: str
    multiplier: float
    macros: Macros


@dataclass(frozen=True)
class Swapped:
    old: str
    new: str
    slot: str
    days: tuple[dt.date, ...]
    menu_id: int
    #: per changed day, the day's totals and any floor it now falls short of
    totals: Mapping[dt.date, Macros]
    shortfall: Mapping[dt.date, tuple[tuple[str, int, int], ...]]
    together: str | None = None
    partner_menu_id: int | None = None
    #: the pot of the old dish was already cooked: the new one needs cooking
    cooked: bool = False
    #: days of the old dish that already had the new one at another meal, left as they were
    already: tuple[dt.date, ...] = ()

    def said(self) -> str:
        when = _days_words(self.days)
        parts = [f"{self.new} in for {self.old}, {_LABEL.get(self.slot, self.slot)} {when}."]
        kcal = sorted({m.kcal for m in self.totals.values()})
        if kcal:
            span = f"{kcal[0]:,}" if len(kcal) == 1 else f"{kcal[0]:,}-{kcal[-1]:,}"
            parts.append(f"The day comes to {span} kcal" + ("" if len(kcal) == 1 else " a day"))
        short = sorted({n for s in self.shortfall.values() for n, _g, _need in s})
        parts[-1] += (
            f", a little short of {' and '.join(short)}." if short else ", still on target."
        )
        if self.cooked:
            parts.append(f"The {self.old} boxes are already cooked: {self.new} is cooked fresh.")
        if self.already:
            that = "that day" if len(self.already) == 1 else "those days"
            parts.append(
                f"{self.old} stays {_days_words(self.already)}: {self.new} is already on {that} "
                "at another meal."
            )
        if self.together:
            parts.append(f"{self.together}'s plates follow: the same dish, sized to their day.")
        return " ".join(parts)


@dataclass
class Cleaned:
    """What `swap_avoided` did: each dish swapped, and each it could find nothing for."""

    swapped: list[Swapped] = field(default_factory=list)
    stuck: list[dict[str, Any]] = field(default_factory=list)

    def said(self) -> str:
        parts = [s.said() for s in self.swapped]
        for s in self.stuck:
            parts.append(
                f"Nothing you have eaten before can stand in for {s['name']} ({s['why']}): "
                "change one food in it, or ask for new dishes written for that meal."
            )
        return " ".join(parts) or "Nothing on your menus holds a food you avoid."


def _days_words(days: Sequence[dt.date]) -> str:
    if not days:
        return ""
    if len(days) == 1:
        return f"on {days[0]:%a %d %b}"
    run = all((b - a).days == 1 for a, b in itertools.pairwise(days))
    if run:
        return f"{days[0]:%a} to {days[-1]:%a %d %b}"
    return "on " + ", ".join(f"{d:%a}" for d in days[:-1]) + f" and {days[-1]:%a %d %b}"


# ---------------------------------------------------------------- reading the plates
def plates_on(db: Db, person_id: int, day: dt.date) -> list[Plate]:
    """The plates in force on a day, in the day's order."""
    return _plates(db, "v_menu_days_current", "md.person_id = ?", person_id, day)


def _plates_of(db: Db, menu_id: int, day: dt.date) -> list[Plate]:
    """One menu's plates on a day, in the day's order, whether or not they are in force."""
    return _plates(db, "menu_days", "md.menu_id = ?", menu_id, day)


def _plates(db: Db, source: str, where: str, key: int, day: dt.date) -> list[Plate]:
    order = {s: i for i, s in enumerate(menu.SLOT_ORDER)}
    rows = db.all(
        "SELECT ms.slot, ms.recipe_id, rc.name, ms.portion_multiplier, ms.kcal, ms.protein_g, "
        f"ms.fat_g, ms.carb_g, ms.fibre_g FROM {source} md "
        "JOIN meal_slots ms ON ms.menu_day_id = md.id JOIN recipes rc ON rc.id = ms.recipe_id "
        f"WHERE {where} AND md.day = ?",
        (key, isoday(day)),
    )
    out = [
        Plate(
            str(r["slot"]),
            int(r["recipe_id"]),
            str(r["name"]),
            float(r["portion_multiplier"]),
            Macros(
                int(r["kcal"]),
                round(float(r["protein_g"])),
                round(float(r["fat_g"])),
                round(float(r["carb_g"])),
                round(float(r["fibre_g"])),
            ),
        )
        for r in rows
    ]
    return sorted(out, key=lambda p: order.get(p.slot, len(order)))


def _day_total(db: Db, menu_id: int, day: dt.date) -> Macros | None:
    row = db.one(
        "SELECT kcal, protein_g, fat_g, carb_g, fibre_g FROM menu_days "
        "WHERE menu_id = ? AND day = ?",
        (menu_id, isoday(day)),
    )
    if row is None:
        return None
    return Macros(
        int(row["kcal"]),
        round(float(row["protein_g"])),
        round(float(row["fat_g"])),
        round(float(row["carb_g"])),
        round(float(row["fibre_g"])),
    )


def _span(db: Db, person_id: int, day: dt.date, today: dt.date) -> tuple[int, list[dt.date]]:
    """The stretch of days a swap on `day` may rewrite: the segment's menu, and its days from
    today (or the segment's first day) on, after any day already recorded. NothingToBuild says
    why a day cannot be changed."""
    if day < today:
        raise NothingToBuild("That day has gone.")
    if menu.day_confirmed(db, person_id, day):
        raise NothingToBuild("That day is already recorded.")
    seg = menu.current_menu(db, person_id, day)
    if seg is None:
        raise NothingToBuild("There is no menu for that day yet.")
    start = max(parse_day(str(seg["segment_start"])), today)
    end = parse_day(str(seg["segment_end"]))
    days = [start + dt.timedelta(days=i) for i in range((end - start).days + 1)]
    recorded = [i for i, d in enumerate(days) if menu.day_confirmed(db, person_id, d)]
    if recorded:
        days = days[max(recorded) + 1 :]
    if day not in days:
        raise NothingToBuild("A later day of that menu is already recorded, so it stays as it is.")
    return int(seg["id"]), days


def had_today(db: Db, person_id: int, now: dt.datetime) -> frozenset[str]:
    """Today's meals already behind the clock: the ones before the meal it is now (breakfast
    after 10:30, lunch after 15:00 - `clock.meal_now`), and all of them once the evening
    check-in's time has come. They are eaten, not still to come: a swap leaves them as they
    were, and holds them at the size they were eaten when the rest of the day is solved again
    (a swap of tonight's dinner made the breakfast eaten that morning 125 kcal bigger)."""
    profile = targets_mod.profile_on(db, person_id, now.date())
    at = str(profile["confirm_time"] or "20:30") if profile is not None else "20:30"
    evening = local_at(now.date(), at, now.tzinfo or db.clock.timezone)
    if evening is not None and now >= evening:
        return frozenset(menu.SLOT_ORDER)
    return frozenset(menu.SLOT_ORDER[: menu.SLOT_ORDER.index(meal_now(now))])


def _allowed_codes(db: Db, people: Sequence[int]) -> set[str]:
    """The foods every one of them may be given: each person's approved list, intersected."""
    lists = [{i.code for i in catalogue.approved(db, pid)} for pid in people]
    return set.intersection(*lists) if lists else set()


def _blocked(db: Db, people: Sequence[int]) -> set[str]:
    return {n.casefold() for pid in people for n in menu.rated_names(db, pid)[1]}


def _scores(db: Db, person_id: int) -> dict[str, int]:
    rows = db.all(
        "SELECT rc.name AS name, r.score AS score FROM ratings r "
        "JOIN recipes rc ON rc.id = r.recipe_id "
        "WHERE r.person_id = ? AND r.id = (SELECT MAX(x.id) FROM ratings x "
        "JOIN recipes rx ON rx.id = x.recipe_id "
        "WHERE x.person_id = r.person_id AND rx.name = rc.name)",
        (person_id,),
    )
    return {str(r["name"]).casefold(): int(r["score"]) for r in rows}


def cooked_with(
    db: Db, person_id: int, day: dt.date, slot: str | None = None
) -> tuple[int, str] | None:
    """The other person, on a day the two cook together (food/plates.py's own answer).

    With `slot`, only when that meal is cooked together too: their plate of it in force is the
    same dish. A snack or a pre-bed only one of them has is that person's alone - swapping it
    rewrote the other's menu and told the asker their plates followed, and asking for new
    dishes for it needed the other's consent."""
    from . import plates  # noqa: PLC0415 (plates imports menu, which imports this package)

    other = plates.cooked_with(db, person_id, day)
    if other is None or slot is None:
        return other
    mine = next((p.name for p in plates_on(db, person_id, day) if p.slot == slot), None)
    theirs = next((p.name for p in plates_on(db, other[0], day) if p.slot == slot), None)
    if mine is None or theirs is None or mine.casefold() != theirs.casefold():
        return None
    return other


def candidates(
    db: Db,
    person_id: int,
    day: dt.date,
    slot: str,
    today: dt.date,
    *,
    limit: int | None = OFFER_MAX,
) -> list[Candidate]:
    """Dishes that could replace this day's `slot`, best first.

    Best is the person's taste score for the day the dish would be eaten (food/taste.py): what
    they rated it, ate it, swapped it in or out, less how much of it - and of its main protein -
    they have had lately, so a five-star dish eaten all last week rests rather than coming top
    every time. On a day cooked together it is both people's score, and a dish either is
    predicted to dislike is not offered. Ties go to the nearest in calories to the dish going
    out (the day then moves least), then the most recently eaten. `limit=None` is every one,
    which is what a swap checks the asked-for dish against.

    The dishes the model wrote for this kind of meal lately keep their place in the `limit`: the
    page asks for them and then says "tap one to put it in", and a dish with nothing yet known of
    it ranked below six already eaten and rated well, so it was never there to tap.
    """
    from . import taste  # noqa: PLC0415 (taste reads menus through this package)

    plates = plates_on(db, person_id, day)
    current = next((p for p in plates if p.slot == slot), None)
    if current is None:
        return []
    other = cooked_with(db, person_id, day, slot)
    people = [person_id] if other is None else [person_id, other[0]]
    allowed = _allowed_codes(db, people)
    blocked = _blocked(db, people)
    on_day = {p.name.casefold() for p in plates}
    resize = _resize_on(db, person_id, day)
    if resize is not None:
        # and the day's plan: a meal she left out of a resized day is still that day's dish, and
        # a swap keeps it off the day's other meals (`_build`), so it is not offered for them
        on_day |= {p.name.casefold() for p in _plates_of(db, int(resize["base_menu_id"]), day)}
    scores = _scores(db, person_id)
    weekend = day.weekday() >= shared.SATURDAY
    family = _FAMILY.get(slot, (slot,))
    marks = ",".join("?" for _ in family)
    rows = db.all(
        # every dish of this person's that was ever in force at this kind of meal, on this kind
        # of day: one row per dish NAME (a repeated menu writes a fresh recipe row each time),
        # the newest row of it, and the last day it was on a plate
        "SELECT rc.name AS name, MAX(rc.id) AS id, MAX(md.day) AS last_day FROM menus m "
        "JOIN menu_days md ON md.menu_id = m.id JOIN meal_slots ms ON ms.menu_day_id = md.id "
        "JOIN recipes rc ON rc.id = ms.recipe_id "
        f"WHERE m.person_id = ? AND ms.slot IN ({marks}) "
        "AND (strftime('%w', md.day) IN ('0', '6')) = ? "
        "AND (m.status IN ('accepted', 'repeated') OR (SELECT d.decision FROM menu_decisions d "
        "WHERE d.menu_id = m.id ORDER BY d.id DESC LIMIT 1) = 'accepted') "
        "GROUP BY rc.name",
        (person_id, *family, 1 if weekend else 0),
    )
    from . import newdish  # noqa: PLC0415 (newdish reads plates through this module)

    # and the dishes the model wrote for a meal of this kind lately, never eaten yet
    eaten = {str(r["name"]) for r in rows}
    # on a day cooked together only a dish written for both: one written for her alone went on
    # his plate too, and her erasure then failed on his row
    together = cooked_with(db, person_id, day, slot) is not None
    fresh = [
        r
        for r in newdish.offered(db, person_id, family, weekend, today)
        if r["name"] not in eaten
        and (
            not together
            or db.scalar("SELECT person_scope FROM recipes WHERE id = ?", (int(r["id"]),)) == "both"
        )
    ]
    new_names = {str(r["name"]) for r in fresh}
    eligible: dict[str, tuple[int, str]] = {}
    mains_of: dict[str, set[str]] = {}
    for r in [*rows, *fresh]:
        name = str(r["name"])
        if name.casefold() in on_day or name.casefold() in blocked:
            continue
        rid = int(r["id"])
        codes = {
            str(c["code"])
            for c in db.all(
                "SELECT i.code FROM recipe_ingredients ri JOIN ingredients i "
                "ON i.id = ri.ingredient_id WHERE ri.recipe_id = ?",
                (rid,),
            )
        }
        if not codes or not codes <= allowed:
            continue  # a food somebody at the table avoids, or one no longer in the catalogue
        eligible[name] = (rid, str(r["last_day"]))
        mains_of[name] = packs.recipe_mains(db, rid)
    ids = {name: rid for name, (rid, _last) in eligible.items()}
    mine = taste.tastes(db, person_id, ids, day)
    # what "had" means in the words beside each: the days before today it was on their plate,
    # read as taste reads plates - not one swapped away before its day came, nor one still to
    # come - and the dishes they swapped in themselves
    had: dict[str, list[dt.date]] = defaultdict(list)
    for r in db.all(
        "SELECT md.day, rc.name FROM v_menu_days_current md "
        "JOIN meal_slots ms ON ms.menu_day_id = md.id JOIN recipes rc ON rc.id = ms.recipe_id "
        "WHERE md.person_id = ? AND md.day < ? AND md.day >= ?",
        (
            person_id,
            isoday(today),
            isoday(today - dt.timedelta(days=taste.LOOKBACK_DAYS)),
        ),
    ):
        had[str(r["name"]).casefold()].append(parse_day(str(r["day"])))
    chose = {
        str(r["dish"]).casefold()
        for r in db.all(
            "SELECT DISTINCT dish FROM dish_events WHERE person_id = ? AND kind = 'swapped_in'",
            (person_id,),
        )
    }
    theirs = taste.tastes(db, other[0], ids, day) if other is not None else None
    # fairness over time: the one the recent shared meals suited less counts for a little more
    weight = taste.fair_weight(db, person_id, other[0], day) if other is not None else 0.5
    units = catalogue.units(db)
    now_kcal = max(recipes.stored_per_serving(db, current.recipe_id, 1.0, units).kcal, 1)
    # what is in the kitchen: a dish that cooks from a pack the shop window has open, or opens
    # none, comes before one that opens another. Taste orders the rest
    have = packs.kitchen(db, person_id, day, slot, allowed=allowed)
    found: list[tuple[tuple[Any, ...], Candidate]] = []
    for name, (rid, last_day) in eligible.items():
        if theirs is not None:
            together = taste.couple_score(mine[name], theirs[name], weight)
            if together is None:
                continue  # one of the two is predicted to dislike it: never offered
            fit = together
        else:
            fit = mine[name].score
        each = recipes.stored_per_serving(db, rid, 1.0, units)
        minutes = int(db.scalar("SELECT prep_minutes FROM recipes WHERE id = ?", (rid,)) or 0)
        cand = Candidate(
            rid,
            name,
            each.kcal,
            round(each.protein_g),
            round(each.fat_g),
            round(each.carb_g),
            round(each.fibre_g),
            minutes,
            scores.get(name.casefold()),
            last_day,
            resting=mine[name].resting,
            new=name in new_names,
            # said as of today, whatever day the dish would be eaten: "last had" counts back
            # from now, and a plate between now and then has not been had yet
            why=taste.why(
                mine[name],
                score=scores.get(name.casefold()),
                last_day=max(had[name.casefold()], default=None),
                on=today,
                new=name in new_names,
                had=had[name.casefold()],
                chose=name.casefold() in chose,
            ),
            had=bool(had[name.casefold()]),
        )
        open_pack = have.uses_open(mains_of[name])
        if open_pack is not None:
            cand = replace(cand, kitchen=f"cooks from the open {packs.short(open_pack.name)}")
        key = (
            1 if have.opens_new(mains_of[name]) else 0,
            -round(fit, 2),
            round(abs(each.kcal - now_kcal) / now_kcal, 2),
            _neg_day(last_day),
            name,
        )
        found.append((key, cand))
    found.sort(key=lambda kc: kc[0])
    out = [c for _k, c in found]
    if limit is None:
        return out
    # the newest dishes written for this kind of meal keep their place; the best of the rest
    # fill the others, and the list stays in its order
    fresh_first = sorted((c for c in out if c.new), key=lambda c: -c.recipe_id)
    keep = {c.recipe_id for c in fresh_first[: min(NEW_KEPT, limit)]}
    for c in out:
        if len(keep) >= limit:
            break
        keep.add(c.recipe_id)
    return [c for c in out if c.recipe_id in keep]


def _neg_day(iso: str) -> int:
    return -parse_day(iso).toordinal()


def _days_on(
    db: Db,
    person_id: int,
    span: Sequence[dt.date],
    slot: str,
    name: str,
    *,
    today: dt.date,
    eaten: Collection[str],
) -> list[dt.date]:
    """The days of `span` with `name` in `slot` that a swap of every day changes. Today's plate
    of a meal already eaten (`eaten`, today's) is not one of them, unless it is the only day:
    the swap opened on it can then mean nothing else."""
    on = [
        d
        for d in span
        if any(p.slot == slot and p.name == name for p in plates_on(db, person_id, d))
    ]
    if slot in eaten and today in on and len(on) > 1:
        on.remove(today)
    return on


def _eaten_at_table(
    db: Db, person_id: int, day: dt.date, slot: str, now: dt.datetime
) -> frozenset[str]:
    """Today's meals behind this person (`had_today`) and, when `slot` on `day` is cooked for the
    two of them, behind the other too: what decides the days a swap of every day changes. Both
    plates follow the swap, so today's is eaten for both once either has had it. Decided apart,
    by each one's own check-in time, at 20:15 her Saturday dinner stayed and his was swapped:
    two dinners on a day cooked together. A day the other has recorded is behind them whole."""
    eaten = had_today(db, person_id, now)
    other = cooked_with(db, person_id, day, slot)
    if other is None:
        return eaten
    if menu.day_confirmed(db, other[0], now.date()):
        return frozenset(menu.SLOT_ORDER)
    return eaten | had_today(db, other[0], now)


def options(db: Db, person_id: int, day: dt.date, slot: str, today: dt.date) -> dict[str, Any]:
    """What the page's Swap needs: the dish going out, the days it is on, who else's plate
    follows, and the dishes that could come in. NothingToBuild says why there is no choice.

    The days are the ones a swap of every day changes (`put`): after lunch, the lunch eaten at
    one is not among them, whichever day the Swap was opened on - nor, on a day cooked together,
    the dinner the other has had (`_eaten_at_table`)."""
    _seg, span = _span(db, person_id, day, today)
    plates = {p.slot: p for p in plates_on(db, person_id, day)}
    if slot not in plates:
        raise ValueError(f"There is no {_LABEL.get(slot, slot)} on that day.")
    current = plates[slot]
    # the database's own clock - the one its rows are stamped with - while `today` is its today
    now = db.clock.now_local()
    eaten = _eaten_at_table(db, person_id, day, slot, now) if now.date() == today else frozenset()
    on = _days_on(db, person_id, span, slot, current.name, today=today, eaten=eaten)
    other = cooked_with(db, person_id, day, slot)
    return {
        "day": isoday(day),
        "slot": slot,
        "current": {"recipe_id": current.recipe_id, "name": current.name},
        "days": [isoday(d) for d in on],
        "together": other[1] if other is not None else None,
        "candidates": [c.as_dict() for c in candidates(db, person_id, day, slot, today)],
    }


# ---------------------------------------------------------------- swapping
def _cook_start(db: Db, menu_id: int) -> dt.date:
    """The first day of the cook a menu's boxes were made for. A swap's menu (made in code from
    the menu in force, with no model) is the same cook as the one it was made from, so its own
    first day - today, or the day after the last one recorded - is not when anything was cooked:
    once Wednesday was recorded, Thursday's boxes were treated as not cooked yet."""
    row = db.one(
        "SELECT segment_start, source_menu_id, status, model_used FROM menus WHERE id = ?",
        (menu_id,),
    )
    if row is None:
        raise LookupError(f"no menu {menu_id}")
    start = parse_day(str(row["segment_start"]))
    for _ in range(100):  # a chain of swaps, each made from the one before; bounded all the same
        if row["source_menu_id"] is None or row["model_used"] is not None:
            break
        if str(row["status"]) == "repeated":
            break  # a repeat is last week's food cooked again: a cook of its own
        row = db.one(
            "SELECT segment_start, source_menu_id, status, model_used FROM menus WHERE id = ?",
            (int(row["source_menu_id"]),),
        )
        if row is None:
            break
        start = min(start, parse_day(str(row["segment_start"])))
    return start


def _cooked(db: Db, person_id: int, day: dt.date, cook_start: dt.date, today: dt.date) -> set[str]:
    """The meals of `day` that come out of a pot already cooked: the batch-cooked slots of a
    weekday, once the week they were cooked for has begun (`_cook_start`)."""
    if day.weekday() >= shared.SATURDAY or today < cook_start:
        return set()
    from . import planning  # noqa: PLC0415 (planning reads batch, as this module does)

    profile = targets_mod.profile_on(db, person_id, day)
    if profile is None:
        return set()
    return set(planning.batched(profile))


def _solve(
    db: Db,
    person_id: int,
    day: dt.date,
    plates: Sequence[Plate],
    slot: str,
    new_id: int,
    *,
    hold: Collection[str],
    config: Any,
    clock: Clock,
) -> tuple[dict[str, int], dict[str, float], dict[str, Macros], Macros, tuple]:
    """One day solved again with `new_id` in `slot`: the other plates kept as near their size as
    the day allows, the ones in `hold` exactly as they are. Not stored."""
    row = targets_mod.ensure_targets(db, person_id, day, config, clock=clock)
    if row is None:
        raise NothingToBuild("There are no targets for that day yet.")
    target = targets_mod.macros_of(row)
    profile = targets_mod.profile_on(db, person_id, day)
    fibre_min = int(profile["fibre_g_min"]) if profile is not None else solver.DEFAULT_FIBRE_MIN
    pin_pot = bool(profile is not None and profile["pre_bed_protein"])
    weekend = day.weekday() >= shared.SATURDAY
    lo, hi = (
        (menu.WEEKEND_LO, menu.WEEKEND_HI) if weekend else (solver.DEFAULT_LO, solver.DEFAULT_HI)
    )
    units = catalogue.units(db)
    ids = {p.slot: (new_id if p.slot == slot else p.recipe_id) for p in plates}
    options = []
    fixed: dict[str, float] = {}
    prefer: dict[str, float] = {}
    for p in plates:
        rid = ids[p.slot]
        one = recipes.stored_per_serving(db, rid, 1.0).as_macros()
        options.append(
            solver.SlotOption(
                p.slot,
                one.kcal,
                one.protein_g,
                one.fat_g,
                one.carb_g,
                one.fibre_g,
                recipe_id=rid,
                units=recipes.stored_unit_parts(db, rid, units),
            )
        )
        if p.slot == solver.PRE_BED and pin_pot:
            fixed[p.slot] = 1.0 if p.slot == slot else p.multiplier
        elif p.slot in hold and p.slot != slot:
            fixed[p.slot] = p.multiplier
        else:
            prefer[p.slot] = 1.0 if p.slot == slot else p.multiplier
    sol: solver.DaySolution | None = None
    for tol, best_effort in _TRIES:
        got = solver.solve_day(
            options,
            target,
            lo=lo,
            hi=hi,
            kcal_tol_pct=tol,
            fat_floor_g=float(target.fat_g),
            fibre_min=fibre_min,
            fixed=fixed,
            prefer=prefer or None,
            best_effort=best_effort,
        )
        if not isinstance(got, Infeasible):
            sol = got
            break
    if sol is None:
        name = db.scalar("SELECT name FROM recipes WHERE id = ?", (new_id,))
        raise Infeasible(
            f"{name} does not fit {day:%a %d %b}'s calories beside the other meals: pick another "
            "dish."
        )
    mults = {p.slot: sol.multipliers[p.slot] for p in plates}
    slot_macros = {
        s: recipes.stored_per_serving(db, ids[s], m, units).as_macros() for s, m in mults.items()
    }
    total = Macros(
        sum(x.kcal for x in slot_macros.values()),
        sum(x.protein_g for x in slot_macros.values()),
        sum(x.fat_g for x in slot_macros.values()),
        sum(x.carb_g for x in slot_macros.values()),
        sum(x.fibre_g for x in slot_macros.values()),
    )
    short = tuple(
        (n, got_g, need)
        for n, got_g, need in (
            ("protein", total.protein_g, round(target.protein_g)),
            ("fat", total.fat_g, round(target.fat_g)),
            ("fibre", total.fibre_g, fibre_min),
        )
        if got_g < need
    )
    return ids, mults, slot_macros, total, short


@dataclass(frozen=True)
class _Resized:
    """A day the person resized (food/plates.py), to be put back in force over the new menu: its
    plates as they now stand, the levels asked for, and what fell short, in words."""

    plan: menu.MenuPlan
    levels_json: str
    shortfall: str | None


@dataclass
class _Built:
    person_id: int
    source_id: int
    plan: menu.MenuPlan
    changed: list[dt.date]
    totals: dict[dt.date, Macros]
    shortfall: dict[dt.date, tuple]
    cooked: bool
    already: list[dt.date] = field(default_factory=list)
    resized: list[_Resized] = field(default_factory=list)


def _resize_on(db: Db, person_id: int, day: dt.date) -> Any:
    """The day's resize (`menu_emphasis`) when the plates in force are one, else None."""
    winner = menu.accepted_menu_id(db, person_id, day)
    if winner is None:
        return None
    return db.one(
        "SELECT base_menu_id, levels_json, shortfall FROM menu_emphasis "
        "WHERE person_id = ? AND day = ? AND menu_id = ? ORDER BY id DESC LIMIT 1",
        (person_id, isoday(day), winner),
    )


def _as_is(
    plates: Sequence[Plate],
) -> tuple[dict[str, int], dict[str, float], dict[str, Macros], Macros]:
    """A day's plates copied as they are, with their total."""
    macros = {p.slot: p.macros for p in plates}
    total = Macros(
        sum(m.kcal for m in macros.values()),
        sum(m.protein_g for m in macros.values()),
        sum(m.fat_g for m in macros.values()),
        sum(m.carb_g for m in macros.values()),
        sum(m.fibre_g for m in macros.values()),
    )
    return (
        {p.slot: p.recipe_id for p in plates},
        {p.slot: p.multiplier for p in plates},
        macros,
        total,
    )


def _relevel(
    db: Db,
    person_id: int,
    day: dt.date,
    planned: tuple[dict[str, int], dict[str, float], dict[str, Macros], Macros],
    levels_json: str,
    now: Sequence[Plate],
    eaten: Collection[str],
    new_name: str,
    *,
    config: Any,
    clock: Clock,
) -> tuple[menu.MenuPlan, Macros, tuple[tuple[str, int, int], ...]] | None:
    """A day the person resized, made again from the new plan's day at the levels they asked
    for, the way food/plates.py makes one: a meal eaten today held at the size it was eaten, a
    box as it was cooked. Its one-day plan, total and shortfall - or None when every level is
    "planned" and nothing is held, when it is the plan's own day, copied."""
    from . import plates  # noqa: PLC0415 (plates imports menu, which imports this package)

    ids, mults, _macros, _total = planned
    asked = json.loads(levels_json)
    levels = {s: str(asked.get(s, "planned")) for s in ids}
    sizes = {p.slot: p.multiplier for p in now}
    held = {s: sizes[s] for s in eaten if s in sizes}
    if not held and all(v == "planned" for v in levels.values()):
        return None
    order = {s: i for i, s in enumerate(menu.SLOT_ORDER)}
    meals = [
        {"slot": s, "recipe_id": ids[s], "m": mults[s]}
        for s in sorted(ids, key=lambda s: order.get(s, len(order)))
    ]
    try:
        solved = plates._solve(
            db,
            person_id,
            day,
            0,  # read only for a day exactly as planned, which is returned above
            meals,
            levels,
            held=held,
            boxed=plates.boxed_slots(db, person_id, day),
            config=config,
            clock=clock,
        )
    except Infeasible as e:
        raise Infeasible(
            f"{new_name} does not fit {day:%a %d %b}'s calories with that day's meals sized as "
            "you set them: pick another dish."
        ) from e
    return solved.plan, solved.total, solved.shortfall


def _day_plan(
    person_id: int,
    days: Sequence[dt.date],
    plates: Mapping[dt.date, tuple[dict[str, int], dict[str, float], dict[str, Macros], Macros]],
) -> menu.MenuPlan:
    recipe_ids: dict[str, int] = {}
    multipliers: dict[str, float] = {}
    slot_macros: dict[str, Macros] = {}
    for d in days:
        ids, mults, macros, _total = plates[d]
        for s in ids:
            key = menu.slot_key(d, s)
            recipe_ids[key] = ids[s]
            multipliers[key] = mults[s]
            slot_macros[key] = macros[s]
    return menu.MenuPlan(
        person_id=person_id,
        segment=menu.Segment(days[0], days[-1]),
        recipe_ids=recipe_ids,
        multipliers=multipliers,
        day_macros={d: plates[d][3] for d in days},
        slot_macros=slot_macros,
        model_used=None,
    )


def _build(
    db: Db,
    person_id: int,
    seg_id: int,
    span: Sequence[dt.date],
    slot: str,
    old_name: str,
    new_id: int,
    new_name: str,
    change: Collection[dt.date],
    *,
    today: dt.date,
    eaten: Collection[str],
    config: Any,
    clock: Clock,
) -> _Built:
    """One person's menu for `span`: the plan for each day, with `new_id` in `slot` on the days
    in `change` that hold `old_name` there, those days solved again. Not stored.

    The plan for a day the person resized is the menu it was resized from, not the resized
    plates: copied in as the plan, a meal skipped for one evening out was gone from the week for
    good - "back to the plan" could not bring it back, and the shopping list left it out. The
    resize is kept in `resized`, the new dish in it where it changed and the day sized again at
    the levels asked for, to go back in force over the new menu. A day that already has the dish
    going in at another meal keeps the old one. `eaten` names today's meals already eaten, held
    as they are like a box already cooked."""
    days: dict[dt.date, tuple[dict[str, int], dict[str, float], dict[str, Macros], Macros]] = {}
    changed: list[dt.date] = []
    already: list[dt.date] = []
    totals: dict[dt.date, Macros] = {}
    shortfall: dict[dt.date, tuple] = {}
    resized: list[_Resized] = []
    cooked = False
    cook_start = _cook_start(db, seg_id)
    for d in span:
        now = plates_on(db, person_id, d)
        if not now:
            raise NothingToBuild(f"{d:%a %d %b} has no meals on it, so that menu cannot change.")
        resize = _resize_on(db, person_id, d)
        planned = _plates_of(db, int(resize["base_menu_id"]), d) if resize is not None else []
        if not planned:
            resize, planned = None, now
        hit = d in change and any(p.slot == slot and p.name == old_name for p in planned)
        if hit and any(
            p.slot != slot and p.name.casefold() == new_name.casefold() for p in planned
        ):
            already.append(d)  # never the same dish twice in one day
            hit = False
        boxes = _cooked(db, person_id, d, cook_start, today)
        hold = boxes | (set(eaten) if d == today else set())
        if hit:
            cooked = cooked or slot in boxes
            ids, mults, macros, total, short = _solve(
                db, person_id, d, planned, slot, new_id, hold=hold, config=config, clock=clock
            )
            days[d] = (ids, mults, macros, total)
            changed.append(d)
            totals[d] = total
            shortfall[d] = short
        else:
            ids, mults, macros, total = _as_is(planned)
            source = (
                int(resize["base_menu_id"])
                if resize is not None
                else menu.accepted_menu_id(db, person_id, d)
            )
            days[d] = (ids, mults, macros, _day_total(db, source or 0, d) or total)
        if resize is None:
            continue
        words = resize["shortfall"]
        levels_json = str(resize["levels_json"])
        if hit and any(p.slot == slot for p in now):
            # her levels applied again to the new plan's day, as food/plates.py applies them:
            # the resized sizes nudged round the new dish were no longer what the levels give
            again = _relevel(
                db,
                person_id,
                d,
                days[d],
                levels_json,
                now,
                set(eaten) - {slot} if d == today else set(),  # the meal changing is not held
                new_name,
                config=config,
                clock=clock,
            )
            if again is None:
                as_plan = _day_plan(person_id, [d], {d: days[d]})  # every level as planned
            else:
                as_plan, totals[d], shortfall[d] = again
                words = "; ".join(f"{n} {got} g of {need} g" for n, got, need in again[2]) or None
        else:
            # untouched, or the meal changed is one the person left out that day: as they were
            ids, mults, macros, total = _as_is(now)
            winner = menu.accepted_menu_id(db, person_id, d) or 0
            as_now = (ids, mults, macros, _day_total(db, winner, d) or total)
            as_plan = _day_plan(person_id, [d], {d: as_now})
            totals.pop(d, None)
            shortfall.pop(d, None)
        resized.append(_Resized(as_plan, levels_json, words))
    plan = _day_plan(person_id, list(span), days)
    return _Built(person_id, seg_id, plan, changed, totals, shortfall, cooked, already, resized)


def swap(
    db: Db,
    person_id: int,
    day: dt.date,
    slot: str,
    recipe_id: int,
    *,
    every_day: bool = True,
    config: Any,
    clock: Clock,
) -> Swapped:
    """Put `recipe_id` in `slot` on `day` - and, with `every_day`, on every day still to come in
    the same menu that holds the same dish there - and put the result in force.

    The dish has to be one `candidates` offers this person for that meal: a recipe id is never
    taken on trust from whoever asks. Raises NothingToBuild for a day that cannot change,
    ValueError for a meal or a dish that is not on offer, and Infeasible when the dish cannot
    fit the day (this person's, or on a day cooked together, the other's).
    """
    from . import taste  # noqa: PLC0415 (taste reads menus through this package)

    today = clock.today_local()
    _span(db, person_id, day, today)
    plates = {p.slot: p for p in plates_on(db, person_id, day)}
    if slot not in plates:
        raise ValueError(f"There is no {_LABEL.get(slot, slot)} on that day.")
    old = plates[slot]
    offered = {c.recipe_id: c for c in candidates(db, person_id, day, slot, today, limit=None)}
    if recipe_id not in offered:
        raise ValueError("That dish is not one of the ones on offer for this meal.")
    new_name = offered[recipe_id].name

    def record() -> None:
        # what the swap says about taste: the dish taken away and the one chosen (food/taste.py)
        taste.record_swap(db, person_id, old=old.name, new=new_name, new_id=recipe_id, day=day)

    return put(
        db,
        person_id,
        day,
        slot,
        recipe_id,
        new_name,
        every_day=every_day,
        config=config,
        clock=clock,
        record=record,
    )


def put(
    db: Db,
    person_id: int,
    day: dt.date,
    slot: str,
    recipe_id: int,
    new_name: str,
    *,
    every_day: bool,
    config: Any,
    clock: Clock,
    record: Any = None,
) -> Swapped:
    """`recipe_id` in `slot` on `day` (and, with `every_day`, on every day still to come in the
    same menu holding the same dish there), each changed day solved again, and the result put in
    force for this person and, on a day cooked together, the other. The caller has already
    decided the recipe may go in; `record`, when given, runs inside the same transaction.

    Today's meals already eaten (`had_today`) are held as they were, and today's plate of the
    meal changing is changed only when today alone is asked about, or is the only day the dish
    is on: "every day" is every day still to come, and lunch eaten at one is not - whichever
    day the swap was opened on (`options` lists the same days). On a day cooked together that
    is decided once for the two of them (`_eaten_at_table`), and the other's plates follow it."""
    today = clock.today_local()
    seg_id, span = _span(db, person_id, day, today)
    plates = {p.slot: p for p in plates_on(db, person_id, day)}
    if slot not in plates:
        raise ValueError(f"There is no {_LABEL.get(slot, slot)} on that day.")
    old = plates[slot]
    change = set(span) if every_day else {day}

    def eaten_by(pid: int, days: Collection[dt.date]) -> frozenset[str]:
        return had_today(db, pid, clock.now_local()) if today in days else frozenset()

    eaten = eaten_by(person_id, span)
    gone = _eaten_at_table(db, person_id, day, slot, clock.now_local()) if today in span else eaten
    if (
        every_day
        and slot in gone
        and today not in _days_on(db, person_id, span, slot, old.name, today=today, eaten=gone)
    ):
        change.discard(today)
    mine = _build(
        db,
        person_id,
        seg_id,
        span,
        slot,
        old.name,
        recipe_id,
        new_name,
        change,
        today=today,
        eaten=eaten,
        config=config,
        clock=clock,
    )
    if not mine.changed:
        # nothing would change: stored all the same, it read as a swap and as her own choice
        where = _days_words(mine.already) or "that day"
        raise ValueError(f"{new_name} is already at another meal {where}.")

    theirs: _Built | None = None
    other = cooked_with(db, person_id, day, slot)
    if other is not None:
        other_id, other_name = other
        try:
            their_seg, their_span = _span(db, other_id, day, today)
        except NothingToBuild as e:
            why = str(e)
            raise NothingToBuild(
                f"{other_name}'s plates cannot follow: {why[:1].lower()}{why[1:]}"
            ) from e
        their_eaten = eaten_by(other_id, their_span)
        their_change = {d for d in change if d in their_span}
        try:
            theirs = _build(
                db,
                other_id,
                their_seg,
                their_span,
                slot,
                old.name,
                recipe_id,
                new_name,
                their_change,
                today=today,
                eaten=their_eaten,
                config=config,
                clock=clock,
            )
        except Infeasible as e:
            raise Infeasible(f"{other_name}'s day cannot carry it: {e}") from e

    # the pair the day's plates belong to now, read before the new menus take the day over
    old_pair = shared.pair_of(db, menu.accepted_menu_id(db, person_id, day) or 0)
    # a plate of this person's alone (their snack) on a day the two cook together: the rest of
    # the day is still cooked together, so the new menu stays in the pair the day was in
    alone = other is None and old_pair is not None and cooked_with(db, person_id, day) is not None
    with db.tx():
        menu_id = _store(db, mine, clock)
        if record is not None:
            record()
        partner_id = _store(db, theirs, clock) if theirs is not None else None
        if theirs is not None and partner_id is not None:
            spans = [*mine.plan.segment.days, *theirs.plan.segment.days]
            pair = db.insert(
                "shared_menus",
                segment_start=isoday(min(spans)),
                segment_end=isoday(max(spans)),
                source_shared_id=old_pair,
                model_used=None,
                link_id=db.scalar("SELECT link_id FROM shared_menus WHERE id = ?", (old_pair,))
                if old_pair is not None
                else links.active_link_id(db),
            )
            for pid, mid in ((person_id, menu_id), (theirs.person_id, partner_id)):
                db.insert("shared_menu_members", shared_menu_id=pair, person_id=pid, menu_id=mid)
        elif alone and old_pair is not None:
            db.insert(
                "shared_menu_members", shared_menu_id=old_pair, person_id=person_id, menu_id=menu_id
            )
        _restore(db, mine, menu_id, clock)
        if theirs is not None and partner_id is not None:
            _restore(db, theirs, partner_id, clock)
    return Swapped(
        old=old.name,
        new=new_name,
        slot=slot,
        days=tuple(mine.changed),
        menu_id=menu_id,
        totals=mine.totals,
        shortfall=mine.shortfall,
        together=other[1] if other is not None else None,
        partner_menu_id=partner_id,
        cooked=mine.cooked,
        already=tuple(mine.already),
    )


def _store(db: Db, built: _Built, clock: Clock) -> int:
    menu_id = menu.store_plan(
        db,
        built.plan,
        status="proposed",
        source_menu_id=built.source_id,
        clock=clock,
        own_choice=True,
    )
    menu.decide(db, menu_id, "accepted")
    return menu_id


def _restore(db: Db, built: _Built, base_id: int, clock: Clock) -> None:
    """Each day the person had resized, back in force over the new menu, stored as plates.resize
    stores one: a one-day menu decided now, in the pair its menu is in, recorded in
    `menu_emphasis` against the new menu - so the levels still show, a meal left out can come
    back, and "back to the plan" is the plan with the swap in it."""
    pair = shared.pair_of(db, base_id)
    for r in built.resized:
        menu_id = menu.store_plan(
            db, r.plan, status="proposed", source_menu_id=base_id, clock=clock, own_choice=True
        )
        menu.decide(db, menu_id, "accepted")
        if pair is not None:
            db.insert(
                "shared_menu_members",
                shared_menu_id=pair,
                person_id=built.person_id,
                menu_id=menu_id,
            )
        db.insert(
            "menu_emphasis",
            person_id=built.person_id,
            day=isoday(r.plan.segment.start),
            base_menu_id=base_id,
            menu_id=menu_id,
            levels_json=r.levels_json,
            shortfall=r.shortfall,
        )


def swap_avoided(
    db: Db, person_id: int, *, config: Any, clock: Clock, within: menu.Segment | None = None
) -> Cleaned:
    """Every dish still to come that holds a food this person now avoids, swapped for the best
    dish they have eaten before that fits - one pot at a time, every day it is on.

    `within` keeps it to those days: a repeat cleans the days it copied and leaves the rest of
    past them has the dish swapped a day at a time, on those days only.

    A dish nothing can stand in for is left where it is and named, with the way to new dishes.

    Only dishes they have had come in, as the offer says ("dishes you have had before"): a dish
    the model wrote and they passed over, or one only on a menu still to come, is not put on
    every day without their choosing it. And code chose the dish, not the person, so nothing is
    recorded as their swap (food/taste.py counts a swap-in as the strongest word a person says
    without being asked).
    """
    from . import prefs  # noqa: PLC0415 (prefs reads menus through this package)

    today = clock.today_local()
    now = clock.now_local()
    out = Cleaned()
    tried: set[tuple[str, str, str]] = set()
    for _ in range(20):  # a week holds a handful of pots; this bounds a loop that cannot finish
        held = prefs.still_on_menu(db, person_id, today, now=now)
        if within is not None:
            lo, hi = isoday(within.start), isoday(within.end)
            held = [
                {**d, "days": [x for x in d["days"] if lo <= x <= hi]}
                for d in held
                if any(lo <= x <= hi for x in d["days"])
            ]
        todo = [d for d in held if (d["slot"], d["name"], d["days"][0]) not in tried]
        if not todo:
            break
        dish = todo[0]
        # keyed on the first day too: the same dish on next week's menu is its own pot
        tried.add((dish["slot"], dish["name"], dish["days"][0]))
        first = parse_day(dish["days"][0])
        offered = candidates(db, person_id, first, dish["slot"], today, limit=None)
        # had: on their plate before today - not a dish only on a menu still to come, nor one
        # the model wrote and they passed over
        had = next((c for c in offered if c.had and not c.new), None)
        if had is None:
            why = "only dishes you have not had yet fit that meal" if offered else ""
            out.stuck.append({**dish, "why": why or "nothing else fits that meal"})
            continue
        try:
            every_day = within is None or all(
                within.start <= d <= within.end for d in _span(db, person_id, first, today)[1]
            )
            got = put(
                db,
                person_id,
                first,
                dish["slot"],
                had.recipe_id,
                had.name,
                every_day=every_day,
                config=config,
                clock=clock,
            )
        except (NothingToBuild, Infeasible, ValueError) as e:
            out.stuck.append({**dish, "why": str(e).rstrip(".")})
            continue
        out.swapped.append(got)
    return out


__all__ = [
    "OFFER_MAX",
    "Candidate",
    "Cleaned",
    "Plate",
    "Swapped",
    "candidates",
    "had_today",
    "options",
    "plates_on",
    "put",
    "swap",
    "swap_avoided",
]
