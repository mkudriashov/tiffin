"""The cook-day load: one shop, one cook, N boxes.

A segment that repeats the same menu Monday to Friday does not mean cooking on Monday evening
for Tuesday. It means standing in the kitchen once - usually the Sunday before - and coming
out with a row of boxes. What that person needs at that moment is not a per-day menu: it is
how much of each thing to cook in total, how many boxes to divide it into, and which of those
boxes will not survive in the fridge until the day they are for.

So this reads the accepted menu for the segment, keeps only the slots the person batch cooks
(breakfast is usually made fresh), and sums:

  grams = recipe grams x portion_multiplier / servings          <- the same arithmetic the
                                                                   shopping list uses
  boxes = one per meal slot
  multiples = the total portion_multiplier, i.e. how many times to make the recipe

and then checks each box's day against the recipe's `keep_days` from the cook day, because a
Friday box cooked on Sunday is five days old and the fridge does not care what the menu says.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, replace
from typing import Any

from .. import targets as targets_mod
from ..clock import isoday, parse_day
from ..db import Db
from .catalogue import units as unit_table
from .solver import whole_units

#: the slots a menu can fill, in the order a day runs
SLOTS: tuple[str, ...] = ("breakfast", "lunch", "dinner", "snack", "pre_bed")
#: what most people batch: the two meals that are cooked rather than assembled
DEFAULT_SLOTS: tuple[str, ...] = ("lunch", "dinner")
#: how a slot reads in a sentence
LABELS = {
    "breakfast": "breakfast",
    "lunch": "lunch",
    "dinner": "dinner",
    "snack": "snacks",
    "pre_bed": "the pre-bed meal",
}


class BadSlots(ValueError):
    """What a person typed after /set batch did not name any meal."""


def parse_slots(text: str) -> tuple[str, ...]:
    """`lunch dinner`, `all`, `none`, `all except breakfast` - however they said it.

    "Everything except breakfast" is how people describe this out loud, so it has to be a
    thing they can type; making them enumerate four slots would be Tiffin asking them to
    speak its language rather than the other way round.
    """
    lowered = text.strip().lower().replace(",", " ")
    if not lowered:
        raise BadSlots("Name the meals you batch cook, or 'none'.")
    if lowered in ("none", "off", "nothing"):
        return ()
    words = lowered.split()
    negative = {"except", "but", "apart", "excluding", "without", "not", "minus", "no"}
    everything = words[0] in ("all", "everything")
    # A negative word ANYWHERE means an exclusion, not just after "all". "not breakfast" used
    # to fall past this branch into the positive list, where `_named` found the one meal the
    # person had just ruled out and returned exactly that: they were told they batch cook
    # breakfast and nothing else, and it was written to the profile without a word.
    excluding = bool(negative & set(words))
    if everything or excluding:
        skip = negative | {"all", "everything", "from"}
        rest = [w for w in words if w not in skip]
        dropped = {s for s in SLOTS if _named(s, rest)}
        if excluding and not dropped:
            raise BadSlots(
                "I did not recognise a meal to leave out. Try 'all except breakfast', "
                "'lunch dinner', or 'none'."
            )
        return tuple(s for s in SLOTS if s not in dropped)
    picked = tuple(s for s in SLOTS if _named(s, words))
    if not picked:
        raise BadSlots(
            "I did not recognise a meal there. Try 'lunch dinner', "
            "'all except breakfast', or 'none'."
        )
    return picked


def _named(slot: str, words: list[str]) -> bool:
    plain = slot.replace("_", " ")
    aliases = {slot, plain, plain.replace(" ", ""), f"{slot}s", f"{plain}s"}
    if slot == "pre_bed":
        aliases |= {"prebed", "bed", "supper"}
    if slot == "snack":
        aliases |= {"snacks"}
    return any(w in aliases for w in words)


def read_slots(value: Any) -> tuple[str, ...]:
    """The stored setting, tolerant of an old or hand-edited row."""
    try:
        parsed = json.loads(str(value or "[]"))
    except ValueError:
        return DEFAULT_SLOTS
    if not isinstance(parsed, list):
        return DEFAULT_SLOTS
    return tuple(s for s in SLOTS if s in {str(p) for p in parsed})


def describe(slots: tuple[str, ...]) -> str:
    """ "lunch and dinner", "everything", "nothing"."""
    if not slots:
        return "nothing"
    if set(slots) == set(SLOTS):
        return "every meal"
    names = [LABELS[s] for s in slots]
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " and " + names[-1]


@dataclass(frozen=True)
class Dish:
    """One recipe, and every box of it this cook produces."""

    recipe_id: int
    name: str
    boxes: int
    multiples: float
    keep_days: int
    freezable: bool
    days: tuple[dt.date, ...]
    #: the ONE recipe's own prep time - not multiplied by boxes, because a bigger pot on the
    #: same hob does not take longer to cook: this orders dishes and sizes the sitting.
    prep_minutes: int = 0

    def beyond_keeping(self, cook_day: dt.date) -> tuple[dt.date, ...]:
        """The days whose box will be older than the recipe keeps by the time it is eaten."""
        return tuple(d for d in self.days if (d - cook_day).days > self.keep_days)


@dataclass(frozen=True)
class DishShare:
    """How much of one ingredient goes into one dish, and across how many boxes."""

    dish: str
    grams: float
    boxes: int
    uniform: bool
    #: the per-box amounts, when they are not all the same - a soup made 0.8x for Monday and
    #: 0.7x for Wednesday is two boxes of ONE dish that do not match, and "2 boxes" alone left
    #: the split to whoever was standing at the counter with the scales.
    box_grams: tuple[float, ...] = ()

    @property
    def per_box(self) -> float:
        return self.grams / self.boxes if self.boxes else self.grams


@dataclass(frozen=True)
class Amount:
    """One ingredient, and how it is divided up.

    The total on its own is a shopping figure. `boxes` and `per_box` are the cooking figure:
    two kilograms of rice across eight boxes is 250 g in each, which is the number somebody
    standing at the counter with a spoon actually needs.
    """

    name: str
    grams: float
    pantry: bool
    boxes: int
    #: whether every box gets the same amount, so "each" is a true statement
    uniform: bool = True
    #: the same total broken down by dish, which is what makes a non-uniform amount usable
    by_dish: tuple[DishShare, ...] = ()

    @property
    def per_box(self) -> float:
        return self.grams / self.boxes if self.boxes else self.grams


@dataclass(frozen=True)
class Load:
    cook_day: dt.date
    segment_start: dt.date
    segment_end: dt.date
    slots: tuple[str, ...]
    fresh: tuple[str, ...]
    dishes: tuple[Dish, ...]
    amounts: tuple[Amount, ...]

    @property
    def boxes(self) -> int:
        return sum(d.boxes for d in self.dishes)

    @property
    def total_minutes(self) -> int:
        """Sum preparation time once per dish in the cooking session."""
        return sum(d.prep_minutes for d in self.dishes)

    @property
    def to_freeze(self) -> tuple[tuple[Dish, tuple[dt.date, ...]], ...]:
        out = []
        for dish in self.dishes:
            late = dish.beyond_keeping(self.cook_day)
            if late:
                out.append((dish, late))
        return tuple(out)


def menu_to_batch(db: Db, person_id: int, today: dt.date) -> Any:
    """The segment this cook is for: the next one if there is one, else the one running now.

    Asked on a Sunday the answer is the week ahead, which is the whole point. Asked on a
    Wednesday, when there is no next menu yet, it is the week being eaten - so a person who
    wants to know what they cooked, or who is re-cooking mid-week, still gets an answer
    instead of "no menu".

    A proposal nobody has tapped yet counts. Refusing to add it up until it was accepted was
    exactly backwards: the shopping and the size of the cook are most of what somebody wants
    to know BEFORE they say yes to it, and the only menu in production was a proposal sitting
    in a chat with no decision on it - so the honest answer was there and the command said
    "no menu to cook from". `status` on the row says which it is; the caller says so.
    """
    # `menus` is append-only (trg_menus_noupd) and `menu.decide` writes a `menu_decisions` row,
    # so NOTHING anywhere ever sets status='accepted' - grep the source and the migrations. This
    # query could therefore only match a 'repeated' menu, and the ordinary week is: proposal on
    # Saturday, tap Accept, `/batch` on Sunday to cook. That menu starts in the future, so the
    # `v_menu_current` branch below (which wants a segment covering TODAY) missed it too, and so
    # did the undecided-proposal branch, because it HAS a decision. All three missed, and the
    # command answered "No menu to cook from yet" over an accepted menu - on the one day of the
    # week it exists to serve. Acceptance lives in the decision row; ask that.
    # A day whose plates were resized by hand (`menu_emphasis`) is a one-day menu too, and never a
    # cook: the owner resized Sunday 27 Sep and his Batch tab planned a one-day burger cook for it
    # instead of the week ahead - the week `v_menu_current` leaves those out for.
    accepted = db.one(
        "SELECT * FROM menus m WHERE m.person_id = ? AND m.segment_start >= ? AND ("
        "  m.status IN ('accepted', 'repeated') OR ("
        "    SELECT d.decision FROM menu_decisions d WHERE d.menu_id = m.id "
        "    ORDER BY d.id DESC LIMIT 1) = 'accepted') "
        "AND NOT EXISTS (SELECT 1 FROM menu_emphasis e WHERE e.menu_id = m.id) "
        "ORDER BY m.segment_start, m.id DESC LIMIT 1",
        (person_id, isoday(today)),
    )
    if accepted is not None:
        return accepted
    running = db.one(
        "SELECT * FROM v_menu_current WHERE person_id = ? AND segment_start <= ? "
        "AND segment_end >= ?",
        (person_id, isoday(today), isoday(today)),
    )
    if running is not None:
        return running
    return db.one(
        "SELECT * FROM menus WHERE person_id = ? AND status = 'proposed' AND segment_end >= ? "
        "AND id NOT IN (SELECT menu_id FROM menu_decisions) "
        "ORDER BY segment_start, id DESC LIMIT 1",
        (person_id, isoday(today)),
    )


def _frozen_carryover(
    db: Db, person_id: int, names: set[str], cook_day: dt.date, freezes: bool
) -> dict[str, int]:
    """Boxes already in the freezer from the cook before this one, because a pause meant nobody
    was there to eat the days they were for: cooking the same dish again must count them, not
    read the freezer as empty.

    Only the segment immediately before this cook - a batch this recent is the whole plausible
    freezer, and there is nothing recorded anywhere to say a box was ever taken out of it, so
    going back further risks counting the same box twice.
    """
    if not names:
        return {}
    past = db.one(
        # the last COOK: a menu in force (`v_menu_current` also leaves out a day resized by
        # hand), and never a weekend, which is cooked on the day and never boxed. Reading `menus`
        # raw, a resized Saturday or a weekend menu stood in for the week that filled the freezer
        "SELECT id, segment_start FROM v_menu_current WHERE person_id = ? AND segment_end < ? "
        "AND NOT (strftime('%w', segment_start) IN ('0', '6') "
        "AND strftime('%w', segment_end) IN ('0', '6')) "
        "ORDER BY segment_end DESC, decided_at DESC, id DESC LIMIT 1",
        (person_id, isoday(cook_day)),
    )
    if past is None:
        return {}
    past_cook_day = parse_day(str(past["segment_start"])) - dt.timedelta(days=1)
    carried: dict[str, int] = {}
    for r in db.all(
        "SELECT md.day, rc.name, rc.keep_days, rc.freezable FROM menu_days md "
        "JOIN meal_slots ms ON ms.menu_day_id = md.id JOIN recipes rc ON rc.id = ms.recipe_id "
        "WHERE md.menu_id = ?",
        (int(past["id"]),),
    ):
        name = str(r["name"])
        if name not in names or not (bool(r["freezable"]) or freezes):
            continue
        day = parse_day(str(r["day"]))
        if (day - past_cook_day).days <= int(r["keep_days"]):
            continue  # that box was eaten fresh, in date - never went in the freezer
        if not targets_mod.pause_active(db, person_id, day):
            continue  # somebody was there to eat it; the freezer does not hold it
        carried[name] = carried.get(name, 0) + 1
    return carried


def _less_frozen(dish: Dish, carried: int) -> Dish:
    """Boxes this cook does not need to make because the freezer already holds them - dropping
    the SAME (latest) days `beyond_keeping` would pick, so a day already covered by a frozen box
    is not also asked for fresh."""
    if carried <= 0 or dish.boxes <= 0:
        return dish
    carried = min(carried, dish.boxes)
    boxes = dish.boxes - carried
    days = dish.days[:-carried] if carried < len(dish.days) else ()
    multiples = round(dish.multiples * boxes / dish.boxes, 2) if dish.boxes else 0.0
    return replace(dish, boxes=boxes, multiples=multiples, days=days)


def plan(
    db: Db,
    menu: Any,
    slots: tuple[str, ...],
    today: dt.date,
    *,
    freezes: bool = False,
    from_day: dt.date | None = None,
) -> Load:
    """The cook-day load for one menu.

    `freezes` is the person's own answer to "do you freeze what you batch cook?". Without
    it this read `recipes.freezable` - the MODEL's guess per dish, and 0 for every dish on
    a real Mon-Fri menu - and told somebody whose whole reason for batch cooking is one cook
    a week to cook Thursday's and Friday's portions fresh. Four cooks in a week he had
    already decided to cook once.

    The cook day is the day before the segment starts, or today if the segment has already
    begun - cooking is not something that can be scheduled into the past.

    started cooking on the Sunday it was built for is not five days of boxes on Wednesday
    evening, it is three. It moves the cook day itself, not only which boxes are counted -
    a Wednesday cook is dated Wednesday, not the Sunday the untouched segment would give it -
    and every box's age (`beyond_keeping`) is measured from that evening.
    """
    start, end = parse_day(str(menu["segment_start"])), parse_day(str(menu["segment_end"]))
    # a `from_day` outside the segment cannot date the cook: the page always sends one (today,
    # so opening the tab on a Wednesday costs no taps) while the bot sends none, and the two
    # then dated the same cook differently - which is not a label, because `beyond_keeping`
    # measures every box's age from it and the page grew a freeze line the bot did not have.
    wanted = min(max(from_day or start, start), end)
    cook_day = max(today, wanted - dt.timedelta(days=1))
    rows = db.all(
        "SELECT md.day, ms.slot, ms.portion_multiplier, rc.id AS recipe_id, rc.name, "
        "rc.servings, rc.keep_days, rc.freezable, rc.prep_minutes "
        "FROM menu_days md JOIN meal_slots ms ON ms.menu_day_id = md.id "
        "JOIN recipes rc ON rc.id = ms.recipe_id WHERE md.menu_id = ? ORDER BY md.day, ms.id",
        (int(menu["id"]),),
    )
    kept = [
        r
        for r in rows
        if str(r["slot"]) in slots and (from_day is None or parse_day(str(r["day"])) >= from_day)
    ]
    # Group by the DISH, not the row. `recipes.store` now writes one row per distinct dish, but
    # every menu built before that has one row per day-slot - twenty-five rows for five dishes on
    # a Mon-Fri menu whose variety is "same all segment". Grouping by recipe_id then reported
    # twenty-five dishes of one box each, which is the opposite of batch cooking and is what the
    # cook is holding tonight. A cook calls two pots of the same thing the same dish; so does
    # this. Keyed by (name, servings) because that is what appears on the message.
    dishes: dict[tuple[str, int], dict[str, Any]] = {}
    for r in kept:
        rid = int(r["recipe_id"])
        dish = dishes.setdefault(
            (str(r["name"]), int(r["servings"])),
            {
                "name": str(r["name"]),
                "recipe_id": rid,  # the newest row for this dish; any of them opens the card
                "boxes": 0,
                "multiples": 0.0,
                "keep_days": int(r["keep_days"]),
                # their answer wins over the model's guess, and only for what they batch: a dish
                # made fresh on the day was never going in the freezer either way
                "freezable": bool(r["freezable"]) or freezes,
                "prep_minutes": int(r["prep_minutes"]),
                "days": [],
            },
        )
        dish["recipe_id"] = rid
        dish["boxes"] += 1
        # a box is a SERVING; "times the recipe" is what goes in the pot. A stew written to
        # serve four, filling four boxes, is one pot - and the multipliers summed to 4.0, so the
        # message said "4 times the recipe" directly above an amounts list holding the beans for
        # one. Two numbers in one message, out by `servings`, with the person in the kitchen
        # deciding which to believe. `_amounts` has divided by servings since it was written;
        # this is the same expression, so the two halves of the message can no longer disagree.
        dish["multiples"] += float(r["portion_multiplier"]) / (int(r["servings"]) or 1)
        dish["days"].append(parse_day(str(r["day"])))
    made = tuple(
        Dish(
            recipe_id=int(d["recipe_id"]),
            name=str(d["name"]),
            boxes=int(d["boxes"]),
            multiples=round(float(d["multiples"]), 2),
            keep_days=int(d["keep_days"]),
            freezable=bool(d["freezable"]),
            days=tuple(sorted(d["days"])),
            prep_minutes=int(d["prep_minutes"]),
        )
        # longest first: the thing that decides whether the sitting fits before
        # lunch is the 35-minute oven tray, not the 3-minute yoghurt, and it is also the one
        # to put on first so everything else happens around it, not after it. A tie (sort is
        # stable) falls back to the order the dishes were first cooked in, same as before.
        for _key, d in sorted(
            dishes.items(), key=lambda kv: (-kv[1]["prep_minutes"], -kv[1]["boxes"])
        )
    )
    carried = _frozen_carryover(
        db, int(menu["person_id"]), {d.name for d in made}, cook_day, freezes
    )
    if carried:
        made = tuple(_less_frozen(d, carried.get(d.name, 0)) for d in made)
    return Load(
        cook_day=cook_day,
        segment_start=start,
        segment_end=end,
        # what this menu actually batches, not what the setting says in the abstract: the
        # header read "lunch and dinner" over a menu that had no dinner in it
        slots=tuple(s for s in slots if any(str(r["slot"]) == s for r in rows)),
        fresh=tuple(s for s in SLOTS if s not in slots and any(str(r["slot"]) == s for r in rows)),
        dishes=made,
        amounts=_amounts(db, int(menu["id"]), slots, from_day),
    )


def _amounts(
    db: Db, menu_id: int, slots: tuple[str, ...], from_day: dt.date | None = None
) -> tuple[Amount, ...]:
    """Grams per ingredient over the batched slots, heaviest first.

    The same expression the shopping list uses, so the two can never disagree about how much
    rice a week takes. Ordering by weight is what makes the list readable: the four things
    that fill a shopping bag come first and the teaspoon of paprika comes last.

    `md.day` is selected so `from_day` can filter here too (§0.5/0.9 of the build spec): this
    SELECT did not carry it at all before, so a from-day cook and its amounts could disagree
    about which days they were even counting.
    """
    rows = db.all(
        "SELECT md.day, ms.slot, ri.grams, ms.portion_multiplier, rc.servings, rc.name AS dish, "
        "i.id AS ingredient_id, i.name, i.pantry, i.code "
        "FROM menu_days md JOIN meal_slots ms ON ms.menu_day_id = md.id "
        "JOIN recipes rc ON rc.id = ms.recipe_id "
        "JOIN recipe_ingredients ri ON ri.recipe_id = rc.id "
        "JOIN ingredients i ON i.id = ri.ingredient_id WHERE md.menu_id = ? ORDER BY i.id",
        (menu_id,),
    )
    seen: dict[int, dict[str, Any]] = {}
    units = unit_table(db)
    for r in rows:
        if str(r["slot"]) not in slots:
            continue
        if from_day is not None and parse_day(str(r["day"])) < from_day:
            continue
        servings = int(r["servings"]) or 1
        grams = float(r["grams"]) * float(r["portion_multiplier"]) / servings
        if str(r["code"]) in units:
            # a box gets whole slices and whole eggs, the way its day was counted
            grams = whole_units(grams, units[str(r["code"])].g)
        key = int(r["ingredient_id"])
        # one row per meal slot that uses it, which is one box
        item = seen.setdefault(
            key, {"name": str(r["name"]), "pantry": bool(r["pantry"]), "each": [], "dish": {}}
        )
        item["each"].append(grams)
        item["dish"].setdefault(str(r["dish"]), []).append(grams)
    made = []
    for item in seen.values():
        each = item["each"]
        # ...and where it is NOT uniform, say it per DISH. "Potatoes 900 g across 10 boxes" with
        # no split is a total and an instruction to work it out yourself, at the counter, with
        # scales in your hand - which is the one moment this message exists for. Two dishes using
        # the same potato in different amounts is the ordinary case, not an edge one.
        by_dish = tuple(
            DishShare(
                dish=dish,
                grams=round(sum(gs), 1),
                boxes=len(gs),
                uniform=len({round(g, 1) for g in gs}) == 1,
                # unequal boxes of the SAME dish (a soup made 0.8x for Monday, 0.7x for
                # Wednesday): "2 boxes" alone is a total and a box count, and leaves the split
                # to whoever is standing at the counter with the scales
                box_grams=tuple(sorted((round(g, 1) for g in gs), reverse=True)),
            )
            for dish, gs in sorted(item["dish"].items(), key=lambda kv: -sum(kv[1]))
        )
        made.append(
            Amount(
                name=item["name"],
                grams=round(sum(each), 1),
                pantry=item["pantry"],
                boxes=len(each),
                # two dishes can use the same ingredient in different amounts, and then there is
                # no "each": saying 250 g a box when it is 200 in one and 300 in another is a
                # number that would be wrong in every box it described
                uniform=len({round(g, 1) for g in each}) == 1,
                by_dish=by_dish,
            )
        )
    return tuple(sorted(made, key=lambda a: (-a.grams, a.name.casefold())))


def weigh(grams: float) -> str:
    """Kitchen units. Nobody weighs out 1620 g of chicken; they buy 1.6 kg of it."""
    if grams >= 1000:
        return f"{grams / 1000:.1f} kg".replace(".0 kg", " kg")
    if grams >= 10:
        return f"{grams:.0f} g"
    return f"{grams:.1f} g".replace(".0 g", " g")
