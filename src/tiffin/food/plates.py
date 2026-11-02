"""Resize the meals on one day while preserving the person's chosen dishes.

Fixed batch portions and meals already eaten remain fixed. The solver redistributes the
remaining calories within meal and nutrient limits, and reports any target it cannot
meet. A shared day changes both people's portions only while both consent and neither
has recorded that day."""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Any

from .. import targets as targets_mod
from ..clock import Clock, isoday, meal_now
from ..db import Db
from ..energy import Macros
from ..errors import Infeasible, NothingToBuild
from . import batch as batch_mod
from . import catalogue, menu, recipes, shared, solver

LEVELS = ("skip", "less", "planned", "more", "lots")
#: what each level asks of a plate, against its planned size
FACTOR = {"skip": 0.0, "less": 0.7, "planned": 1.0, "more": 1.35, "lots": 1.7}
#: how near the day's calories stay: tighter than a built menu's 5%, because moving a plate on
#: purpose should not quietly move the day. Widened to the menu's own 5% only when 2% cannot hold.
KCAL_TOL_PCT = 2.0
#: toast and lots of burger, with the carbonara and the pot gone, came out with her toast the
#: bigger plate - her fibre floor could only be met from the toast, so the solver met it by
#: turning the ask round. Within 10% the levels keep their order (the step from one to the next
#: is at least 26%): a floor is met from the room the ask leaves, and what it cannot meet is said
NEAR = 0.1
#: what covers a floor a resized day falls short of, in a line a person can act on
_COVER = {
    "protein": "a protein shake or the pre-bed pot covers it",
    "fat": "a handful of nuts covers it",
    "fibre": "fruit or a vegetable side covers it",
}
_LABEL = {
    "breakfast": "Breakfast",
    "lunch": "Lunch",
    "dinner": "Dinner",
    "snack": "The snack",
    "pre_bed": "Pre-bed",
}
#: how a plate that ended away from its ask is said: never moved without a word
_MOVED = {
    "largest": "is as big as it goes",
    "smallest": "is as small as it goes",
    "bigger": "is bigger than asked, to carry the day's calories",
    "smaller": "is smaller than asked, to stay on the day's calories",
}


#: whether `menu_emphasis` row `e` followed the other person's change: stored straight after
#: theirs in the same transaction (`resize` stores the asker's first), for the same day and the
#: same pair. A row from before `own_json` was recorded can be told to be a follower by this
#: alone - and the same moment keeps her second change, stored straight after his plates followed
#: her first, her own
FOLLOWED = (
    "EXISTS (SELECT 1 FROM menu_emphasis f "
    "JOIN shared_menu_members a ON a.menu_id = f.menu_id "
    "JOIN shared_menu_members b ON b.shared_menu_id = a.shared_menu_id AND b.menu_id = e.menu_id "
    "WHERE f.id = e.id - 1 AND f.day = e.day AND f.person_id != e.person_id "
    "AND f.created_at = e.created_at)"
)


@dataclass(frozen=True)
class Resized:
    menu_id: int
    day: dt.date
    levels: dict[str, str]
    macros: Macros
    shortfall: tuple[tuple[str, int, int], ...]
    #: on a day cooked together, what became of the other person's plates, in words - never
    #: their numbers, which are theirs to read (a shortfall of theirs is not the asker's to see)
    together: str | None = None
    #: the other person's menu for the day, when their plates followed
    partner_menu_id: int | None = None
    #: plates that ended away from what was asked of them, and which way ("bigger", "smaller")
    moved: tuple[tuple[str, str], ...] = ()
    #: each plate's kcal before and after, in the day's order: what a preview shows
    plates: tuple[tuple[str, int, int], ...] = ()

    def preview_said(self) -> str:
        """ "Dinner 620 to 0 kcal, lunch 540 to 690. The day: 1,850 kcal, still on target." -
        what Apply would do, before anything is stored."""
        parts = [self.changes_said() or "No plate changes size."]
        tail = self.said().split(": ", 1)[1] if ": " in self.said() else self.said()
        parts.append(f"The day: {tail}")
        return " ".join(parts)

    def changes_said(self) -> str:
        """ "Dinner 620 to 434, lunch 540 to 726 kcal." - every plate that changed size, or "".
        A one-tap change on the bot said only the day's total, so the lunch it made bigger to
        make room was moved without a word."""
        moving = [(s, a, b) for s, a, b in self.plates if a != b]
        if not moving:
            return ""
        words = [
            f"{_LABEL.get(s, s) if i == 0 else _LABEL.get(s, s).lower()} {a:,} to {b:,}"
            for i, (s, a, b) in enumerate(moving)
        ]
        return ", ".join(words) + " kcal."

    def said(self) -> str:
        parts = [
            f"{self.day:%a %d %b}: {self.macros.kcal:,} kcal, protein {self.macros.protein_g} g."
        ]
        if self.shortfall:
            gaps = "; ".join(
                f"{name} {got} g of {need} g - {self._cover(name)}"
                for name, got, need in self.shortfall
            )
            parts.append(f"Short: {gaps}.")
        else:
            parts.append("Still on target.")
        for slot, way in self.moved:
            parts.append(f"{_LABEL.get(slot, slot)} {_MOVED[way]}.")
        if self.together:
            parts.append(self.together)
        return " ".join(parts)

    def _cover(self, name: str) -> str:
        if name == "protein" and self.levels.get(solver.PRE_BED, "skip") == "skip":
            # the pre-bed pot cannot cover it on a day it was the plate left out
            return "a protein shake covers it"
        return _COVER.get(name, "worth covering")


@dataclass(frozen=True)
class _Solved:
    """One person's day solved again (or copied from the plan), not yet stored."""

    person_id: int
    base_id: int
    levels: dict[str, str]
    plan: menu.MenuPlan
    total: Macros
    shortfall: tuple[tuple[str, int, int], ...]
    moved: tuple[tuple[str, str], ...] = ()


def state(db: Db, person_id: int, day: dt.date) -> dict[str, Any] | None:
    """The day's plates as they stand - the menu they come from, the levels asked for, the meals
    whose level this person chose (`own`: not one that followed the other's change) and what
    fell short - or None when no menu feeds the day."""
    winner = menu.accepted_menu_id(db, person_id, day)
    if winner is None:
        return None
    row = db.one(
        f"SELECT e.base_menu_id, e.levels_json, e.own_json, e.shortfall, {FOLLOWED} AS followed "
        "FROM menu_emphasis e WHERE e.person_id = ? AND e.day = ? AND e.menu_id = ? "
        "ORDER BY e.id DESC LIMIT 1",
        (person_id, isoday(day), winner),
    )
    if row is None:
        return {"base_menu_id": winner, "levels": {}, "own": set(), "shortfall": None}
    levels = dict(json.loads(str(row["levels_json"])))
    if row["own_json"]:
        own = set(json.loads(str(row["own_json"])))
    else:
        # a row from before this was recorded: none of it theirs when it followed the other's
        # change (her next change made the dinner his skip skipped hers, and it counted against
        # her), every level theirs otherwise, as it was then read
        own = set() if row["followed"] else set(levels)
    return {
        "base_menu_id": int(row["base_menu_id"]),
        "levels": levels,
        "own": own,
        "shortfall": row["shortfall"],
    }


def base_meals(db: Db, base_menu_id: int, day: dt.date) -> list[dict[str, Any]]:
    """The meals of the day as planned, in the day's order: the ones a level applies to."""
    order = {s: i for i, s in enumerate(menu.SLOT_ORDER)}
    rows = db.all(
        "SELECT ms.slot, ms.recipe_id, ms.portion_multiplier, rc.name FROM menu_days md "
        "JOIN meal_slots ms ON ms.menu_day_id = md.id JOIN recipes rc ON rc.id = ms.recipe_id "
        "WHERE md.menu_id = ? AND md.day = ?",
        (base_menu_id, isoday(day)),
    )
    meals = [
        {
            "slot": str(r["slot"]),
            "recipe_id": int(r["recipe_id"]),
            "m": float(r["portion_multiplier"]),
            "name": str(r["name"]),
        }
        for r in rows
    ]
    return sorted(meals, key=lambda m: order.get(m["slot"], len(order)))


def boxed_slots(db: Db, person_id: int, day: dt.date) -> frozenset[str]:
    """Return meals fixed at their already cooked portion size.

    Weekend meals and meals prepared on the day have no boxed portion to preserve.
    """
    from ..clock import week_start  # noqa: PLC0415 (a small helper)

    if day.weekday() >= 5:
        return frozenset()
    profile = targets_mod.profile_on(db, person_id, day)
    if profile is None or "batch_slots" not in profile.keys():  # noqa: SIM118
        return frozenset()
    cook = next(
        (s for s in menu.week_segments(db, person_id, week_start(day)) if s.start <= day <= s.end),
        None,
    )
    if cook is None or cook.start == cook.end:
        return frozenset()
    return frozenset(batch_mod.read_slots(profile["batch_slots"]))


def cooked_with(db: Db, person_id: int, day: dt.date) -> tuple[int, str] | None:
    """The other person and their name, on a day the two cook together - or None.

    Found through the pair this person's plates that day belong to, never from anything asked,
    and only while both still agree to the shared weekend: switching it off makes each plate
    its own person's again at once.
    """
    winner = menu.accepted_menu_id(db, person_id, day)
    if winner is None or not shared.linked_and_both_consented(db):
        return None
    pair = shared.pair_of(db, winner)
    if pair is None:
        return None
    row = db.one(
        # their plates in force that day, in the same pair: a day they resized is a one-day menu
        # added to it, so this finds them either way
        "SELECT sm.person_id FROM shared_menu_members sm "
        "JOIN v_menu_days_current v ON v.menu_id = sm.menu_id "
        "WHERE sm.shared_menu_id = ? AND sm.person_id != ? AND v.day = ? LIMIT 1",
        (pair, person_id, isoday(day)),
    )
    if row is None:
        return None
    other = int(row["person_id"])
    return other, shared.name_of(db, other)


def past_meals(db: Db, person_id: int, day: dt.date, clock: Clock) -> frozenset[str]:
    """Today's plates the clock has passed (`clock.meal_now`): eaten, as far as a one-tap change
    can know. Less on tonight's dinner card at 19:00 sized the breakfast and lunch already eaten
    again, and the evening's "As planned" then recorded the day 125 kcal off what was eaten. The
    day's panel asks instead, with its "Already eaten" ticks. A plate left out is not here: it
    was not eaten.

    `meal_now` says "dinner" for the whole evening, so dinner is behind the clock from the
    evening check-in ("ate as planned?"): a Skip on the pre-bed card at 21:30 made the dinner
    already eaten bigger, and "As planned" then recorded it."""
    if day != clock.today_local():
        return frozenset()
    now = clock.now_local()
    upto = menu.SLOT_ORDER.index(meal_now(now))
    if menu.SLOT_ORDER[upto] == "dinner" and (now.hour, now.minute) >= _check_in_at(
        db, person_id, day
    ):
        upto += 1
    before = menu.SLOT_ORDER[:upto]
    return frozenset(s for s in _in_force(db, person_id, day) if s in before)


def _check_in_at(db: Db, person_id: int, day: dt.date) -> tuple[int, int]:
    """(hour, minute) of the evening check-in on `day`: the profile's `confirm_time`, 20:30
    unless it says otherwise."""
    profile = targets_mod.profile_on(db, person_id, day)
    when = str(profile["confirm_time"] or "20:30") if profile is not None else "20:30"
    try:
        hh, mm = (int(x) for x in when.split(":", 1))
    except ValueError:
        return 20, 30
    return hh, mm


def _movable(
    meals: list[dict[str, Any]],
    levels: Mapping[str, str],
    *,
    held: Collection[str],
    boxed: Collection[str],
    pin_pot: bool,
) -> list[str]:
    """The plates kept whose size the solve may move: not eaten, not out of the box, and not the
    profile's pre-bed pot asked for as written."""
    return [
        m["slot"]
        for m in meals
        if levels[m["slot"]] != "skip"
        and m["slot"] not in held
        and m["slot"] not in boxed
        and not (pin_pot and m["slot"] == solver.PRE_BED and levels[m["slot"]] == "planned")
    ]


def _pins_pot(profile: Any) -> bool:
    return bool(profile is not None and profile["pre_bed_protein"])


def one_tap(db: Db, person_id: int, day: dt.date, clock: Clock) -> frozenset[str]:
    """The plates whose own card offers Less, More and Skip: not out of the box, not already
    behind the clock today, and with another plate free to make room. The only plate left that
    can move carries the day whatever it is asked (`_solve` refuses it), so Less on a boxed
    weekday's breakfast made it 55% bigger and the page then drew "Less" as pressed."""
    st = state(db, person_id, day)
    if st is None:
        return frozenset()
    meals = base_meals(db, int(st["base_menu_id"]), day)
    levels = {m["slot"]: str(st["levels"].get(m["slot"], "planned")) for m in meals}
    held = past_meals(db, person_id, day, clock)
    boxed = boxed_slots(db, person_id, day)
    free = _movable(
        meals,
        levels,
        held=held,
        boxed=boxed,
        pin_pot=_pins_pot(targets_mod.profile_on(db, person_id, day)),
    )
    return frozenset(
        m["slot"]
        for m in meals
        if m["slot"] not in held
        and m["slot"] not in boxed
        and any(other != m["slot"] for other in free)
    )


def resettable(db: Db, person_id: int, day: dt.date, clock: Clock) -> frozenset[str]:
    """The plates whose own card offers As planned: set to another level, not out of the box and
    not behind the clock today. Wider than `one_tap`: the only plate left that can move takes no
    Less, More or Skip, but it can always go back to the plan, and a refusal that leaves it the
    only one says "put dinner back to as planned first" - with the pre-bed pot kept as written,
    dinner's card had no button for that."""
    st = state(db, person_id, day)
    if st is None:
        return frozenset()
    held = past_meals(db, person_id, day, clock)
    boxed = boxed_slots(db, person_id, day)
    return frozenset(
        str(slot)
        for slot, level in st["levels"].items()
        if level != "planned" and slot not in held and slot not in boxed
    )


def whole_menu_of(db: Db, menu_id: int) -> int:
    """The menu a resized day was cut from, or `menu_id` itself when it is not a resized day.

    A button that shows, swaps in or rebuilds "the menu" means the stretch of days: Back to the
    menu from a card on a resized Tuesday listed Tuesday alone, and its New menu rebuilt that one
    day through the model when the person meant the week.
    """
    row = db.one(
        "SELECT base_menu_id FROM menu_emphasis WHERE menu_id = ? ORDER BY id DESC LIMIT 1",
        (menu_id,),
    )
    return menu_id if row is None else int(row["base_menu_id"])


def day_info(
    db: Db, person_id: int, day: dt.date, today: dt.date, *, clock: Clock | None = None
) -> dict[str, Any] | None:
    """What the page's control for one day needs, or None when there is nothing to change.

    `clock` places today's meals against the time of day; the page's own connection carries the
    app's clock, which is the default."""
    if day < today:
        return None
    st = state(db, person_id, day)
    if st is None:
        return None
    meals = base_meals(db, int(st["base_menu_id"]), day)
    if not meals:
        return None
    other = cooked_with(db, person_id, day)
    boxed = boxed_slots(db, person_id, day)
    clock = clock or db.clock
    past = past_meals(db, person_id, day, clock)
    tap = one_tap(db, person_id, day, clock)
    back = resettable(db, person_id, day, clock)
    return {
        "adjustable": not menu.day_confirmed(db, person_id, day),
        "levels": {m["slot"]: str(st["levels"].get(m["slot"], "planned")) for m in meals},
        "meals": [
            {
                "slot": m["slot"],
                "name": m["name"],
                "boxed": m["slot"] in boxed,
                # behind the clock today: a one-tap change on another card holds it as eaten
                "past": m["slot"] in past,
                # the card's own Less, More and Skip are offered
                "sizable": m["slot"] in tap,
                # the card's own As planned is offered, Less, More and Skip or not
                "resettable": m["slot"] in back,
            }
            for m in meals
        ],
        "shortfall": st["shortfall"],
        # whose plates follow a change here, said before anyone presses Apply - and only while
        # they can: a day the other has recorded stays as it was
        "together": (
            other[1] if other is not None and not menu.day_confirmed(db, other[0], day) else None
        ),
        # today's meals can be marked as eaten, and are then held as they are
        "today": day == today,
    }


def _changeable(
    db: Db, person_id: int, day: dt.date, today: dt.date
) -> tuple[int, list[dict[str, Any]]]:
    """The menu a day's plates are measured against and its meals, or NothingToBuild saying why
    the day cannot be changed."""
    if day < today:
        raise NothingToBuild("That day has gone.")
    if menu.day_confirmed(db, person_id, day):
        raise NothingToBuild("That day is already recorded.")
    st = state(db, person_id, day)
    if st is None:
        raise NothingToBuild("There is no menu for that day yet.")
    base_id = int(st["base_menu_id"])
    meals = base_meals(db, base_id, day)
    if not meals:
        raise NothingToBuild("That day's menu has no meals on it.")
    return base_id, meals


def _in_force(db: Db, person_id: int, day: dt.date) -> dict[str, float]:
    """slot -> the multiplier of the plate in force for that day."""
    winner = menu.accepted_menu_id(db, person_id, day)
    if winner is None:
        return {}
    return {
        str(r["slot"]): float(r["portion_multiplier"])
        for r in db.all(
            "SELECT ms.slot, ms.portion_multiplier FROM menu_days md "
            "JOIN meal_slots ms ON ms.menu_day_id = md.id WHERE md.menu_id = ? AND md.day = ?",
            (winner, isoday(day)),
        )
    }


def _follow(
    levels: Mapping[str, str],
    meals: list[dict[str, Any]],
    theirs: list[dict[str, Any]],
    had: Mapping[str, str],
) -> dict[str, str]:
    """The other person's levels on a day cooked together: a dish the two share takes the level
    asked for it, and a plate that is theirs alone keeps the level it had."""
    dish = {m["slot"]: m["recipe_id"] for m in meals}
    return {
        m["slot"]: (
            str(levels[m["slot"]])
            if dish.get(m["slot"]) == m["recipe_id"]
            else str(had.get(m["slot"], "planned"))
        )
        for m in theirs
    }


def resize(
    db: Db,
    person_id: int,
    day: dt.date,
    asked: Mapping[str, str],
    *,
    eaten: Collection[str] = (),
    config: Any,
    clock: Clock,
    dry_run: bool = False,
) -> Resized:
    """Solve one day again from its planned dishes at the levels asked for, and put it in force.

    `dry_run` solves the same and stores nothing: the page's preview before Apply.

    Levels not named are "planned". `eaten` names meals of today already eaten, held at the size
    they were eaten. On a day cooked together the other person's plates follow (see the module's
    docstring). Raises NothingToBuild for a day that cannot be changed or a level on the only
    plate left that can move, ValueError for a level or a meal that does not exist or is not this
    day's to move, and Infeasible when the plates kept cannot carry the day's calories at all -
    this person's, or the other's.
    """
    today = clock.today_local()
    base_id, meals = _changeable(db, person_id, day, today)
    names = {m["slot"] for m in meals}
    unknown = sorted((set(asked) | set(eaten)) - names)
    if unknown:
        raise ValueError(f"not a meal on that day: {', '.join(unknown)}")
    if eaten and day != today:
        raise ValueError("only today's meals can have been eaten")
    st = state(db, person_id, day) or {}
    had = st.get("levels", {})
    levels = {m["slot"]: str(asked.get(m["slot"], "planned")) for m in meals}
    for slot in eaten:
        # a meal already eaten keeps the level it was eaten at: the record is of what happened
        levels[slot] = str(had.get(slot, "planned"))
    # the meals whose level is this person's choice: the ones this asks to move, and the ones
    # they chose before and leave as they are. Not a level carried over from the other's change:
    # the page and the bot send every level the day has, so "skip" on a dinner that only followed
    # the other's came back as asked, and read as this person's own dislike of the dish
    own = set(st.get("own", ())) | {s for s, v in levels.items() if v != had.get(s, "planned")}
    bad = sorted({v for v in levels.values() if v not in LEVELS})
    if bad:
        raise ValueError(f"a level is one of {', '.join(LEVELS)}, not {', '.join(bad)}")
    boxed = boxed_slots(db, person_id, day)
    moved_box = sorted(s for s in boxed if levels.get(s, "planned") != "planned")
    if moved_box:
        raise ValueError(
            f"{', '.join(moved_box)} comes out of the box cooked for the week: it is eaten as "
            "it was cooked"
        )
    now = _in_force(db, person_id, day)
    gone = sorted(s for s in eaten if s not in now)
    if gone:
        raise ValueError(f"{', '.join(gone)} is not on today's plates, so it was not eaten")
    held = {s: now[s] for s in eaten}
    # what this change asks, against the levels the day had: a refusal is said about that
    changed = frozenset(s for s, v in levels.items() if v != str(had.get(s, "planned")))
    mine = _solve(
        db, person_id, day, base_id, meals, levels, held=held, boxed=boxed, config=config,
        clock=clock, changed=changed,
    )  # fmt: skip

    theirs: _Solved | None = None
    their_own: set[str] = set()
    together: str | None = None
    other = cooked_with(db, person_id, day)
    if other is not None:
        other_id, name = other
        try:
            their_base, their_meals = _changeable(db, other_id, day, today)
        except NothingToBuild as e:
            # in practice a day they have already recorded: what was eaten stays as it was
            why = str(e)
            together = f"{name}'s plates stay as they were: {why[:1].lower()}{why[1:]}"
        else:
            their_st = state(db, other_id, day) or {}
            their_had = their_st.get("levels", {})
            their_levels = _follow(levels, meals, their_meals, their_had)
            if all(v == "skip" for v in their_levels.values()):
                raise NothingToBuild(
                    f"That leaves {name} no meal on the day: keep one of the dishes you cook "
                    "together."
                )
            # a shared dish eaten together was eaten by both: held on their plate too, at the
            # size theirs was - unless theirs was not on the table at all
            shared_dish = {m["slot"] for m in meals} & {m["slot"] for m in their_meals}
            their_now = _in_force(db, other_id, day)
            their_held = {s: their_now[s] for s in eaten if s in shared_dish and s in their_now}
            for slot in their_held:
                their_levels[slot] = str(their_had.get(slot, "planned"))
            # theirs stay theirs only where this left the level as they had it
            their_own = {
                s
                for s in their_st.get("own", ())
                if their_levels.get(s) == their_had.get(s, "planned")
            }
            try:
                theirs = _solve(
                    db,
                    other_id,
                    day,
                    their_base,
                    their_meals,
                    their_levels,
                    held=their_held,
                    boxed=boxed_slots(db, other_id, day),
                    config=config,
                    clock=clock,
                )
            except Infeasible as e:
                raise Infeasible(str(e).replace("the whole day", f"{name}'s whole day")) from e
            except NothingToBuild as e:
                raise NothingToBuild(f"{name}'s plates cannot follow that: {e}") from e
            together = f"{name}'s plates follow: the same dishes, sized to {name}'s own day."

    after = {
        slot: mine.plan.slot_macros[key].kcal
        for slot in levels
        if (key := menu.slot_key(day, slot)) in mine.plan.slot_macros
    }
    before = {str(r["slot"]): int(r["kcal"]) for r in _in_force_kcal(db, person_id, day)}
    order = {s: i for i, s in enumerate(menu.SLOT_ORDER)}
    sizes = tuple(
        (slot, before.get(slot, 0), after.get(slot, 0))
        for slot in sorted(set(before) | set(after), key=lambda s: order.get(s, 9))
    )
    if dry_run:
        return Resized(
            0, day, levels, mine.total, mine.shortfall, together, None, mine.moved, sizes
        )
    with db.tx():
        menu_id = _store(db, mine, day, clock, own=own)
        partner_menu_id = (
            _store(db, theirs, day, clock, own=their_own) if theirs is not None else None
        )
    return Resized(
        menu_id,
        day,
        levels,
        mine.total,
        mine.shortfall,
        together,
        partner_menu_id,
        mine.moved,
        sizes,
    )


def _in_force_kcal(db: Db, person_id: int, day: dt.date) -> list[Any]:
    """The plates in force on a day, with their kcal: what a preview measures from."""
    return db.all(
        "SELECT ms.slot, ms.kcal FROM v_menu_days_current md "
        "JOIN meal_slots ms ON ms.menu_day_id = md.id WHERE md.person_id = ? AND md.day = ?",
        (person_id, isoday(day)),
    )


def _share(
    free: list[tuple[solver.SlotOption, dict[str, Any]]],
    levels: Mapping[str, str],
    kcal: float,
    lo: float,
    hi: float,
) -> dict[str, float]:
    """What each free plate is asked to carry, as a multiplier: `kcal` shared out in proportion to
    each plate's planned size times its level. A plate that would pass its largest (or smallest)
    size is held there and the others share what it could not carry - more French toast than a
    plate can hold makes the rest bigger, not the day short."""
    weight = {opt.slot: opt.kcal * m["m"] * FACTOR[levels[m["slot"]]] for opt, m in free}
    per = {opt.slot: max(opt.kcal, 1.0) for opt, _m in free}
    out: dict[str, float] = {}
    left = dict(weight)
    budget = kcal
    while left:
        share = budget / max(sum(left.values()), 1.0)
        want = {slot: w * share / per[slot] for slot, w in left.items()}
        over = {slot: (hi if v > hi else lo) for slot, v in want.items() if v > hi or v < lo}
        if not over:
            out.update(want)
            break
        for slot, held in over.items():
            out[slot] = held
            budget -= held * per[slot]
            del left[slot]
    return out


def _solve(
    db: Db,
    person_id: int,
    day: dt.date,
    base_id: int,
    meals: list[dict[str, Any]],
    levels: Mapping[str, str],
    *,
    held: Mapping[str, float],
    boxed: Collection[str],
    config: Any,
    clock: Clock,
    changed: Collection[str] = (),
) -> _Solved:
    """One person's day solved again from its planned dishes at these levels, not stored.

    `changed` names the plates whose level this change sets, so a refusal can be said about
    the ask rather than about a plate it did not touch."""
    kept = [m for m in meals if levels[m["slot"]] != "skip"]
    if not kept:
        raise NothingToBuild("Keep at least one meal on the day.")
    if not held and all(levels[m["slot"]] == "planned" for m in meals):
        return _as_planned(db, person_id, day, base_id, levels)

    row = targets_mod.ensure_targets(db, person_id, day, config, clock=clock)
    if row is None:
        raise NothingToBuild("There are no targets for that day yet.")
    target = targets_mod.macros_of(row)
    profile = targets_mod.profile_on(db, person_id, day)
    fibre_min = int(profile["fibre_g_min"]) if profile is not None else solver.DEFAULT_FIBRE_MIN
    weekend = day.weekday() >= 5
    lo, hi = (
        (menu.WEEKEND_LO, menu.WEEKEND_HI) if weekend else (solver.DEFAULT_LO, solver.DEFAULT_HI)
    )
    units = catalogue.units(db)
    options = []
    for m in kept:
        one = recipes.stored_per_serving(db, m["recipe_id"], 1.0).as_macros()
        options.append(
            solver.SlotOption(
                m["slot"],
                one.kcal,
                one.protein_g,
                one.fat_g,
                one.carb_g,
                one.fibre_g,
                recipe_id=m["recipe_id"],
                units=recipes.stored_unit_parts(db, m["recipe_id"], units),
            )
        )
    # the plates that are not this day's to move: eaten, boxed, or the profile's pot as written
    movable = _movable(kept, levels, held=held, boxed=boxed, pin_pot=_pins_pot(profile))
    fixed = {
        m["slot"]: float(held.get(m["slot"], m["m"])) for m in kept if m["slot"] not in movable
    }
    # the one plate left to move carries the rest of the day whatever it is asked: its share is
    # the whole remainder, so "less" came out bigger, and said nothing about it
    lone = movable[0] if len(movable) == 1 and levels[movable[0]] != "planned" else None
    if lone is not None:
        slot = lone
        others = [str(m["slot"]) for m in meals if m["slot"] in changed and m["slot"] != slot]
        if slot not in changed and others:
            # the ask was another plate's: a Skip on dinner's card left a lunch already set to
            # less as the only plate to move, and the words were about eating less lunch. Said
            # about the ask, and the way out named only when it works (the plate's card offers
            # As planned, `resettable`): a lunch that could not carry the day even as planned
            # is refused here for that reason instead
            _solve(
                db, person_id, day, base_id, meals, {**levels, slot: "planned"}, held=held,
                boxed=boxed, config=config, clock=clock,
            )  # fmt: skip
            said = " and ".join(
                f"{_LABEL.get(s, s).lower()} "
                + {"skip": "left out", "planned": "as planned"}.get(levels[s], levels[s])
                for s in others
            )
            name = _LABEL.get(slot, slot).lower()
            raise NothingToBuild(
                f"With {said}, {name} is the only plate on {day:%a %d %b} left to change size, "
                f"and it is set to {levels[slot]}. Put {name} back to as planned first."
            )
    free = [(opt, m) for opt, m in zip(options, kept, strict=True) if opt.slot not in fixed]
    left = target.kcal - sum(opt.kcal * fixed[opt.slot] for opt in options if opt.slot in fixed)
    prefer = _share(free, levels, left, lo, hi)
    # the ask as said, before any plate was held at its largest or smallest: what "moved" means
    plain = _share(free, levels, left, 0.0, float("inf"))

    sol: solver.DaySolution | None = None
    last: Infeasible | None = None
    for tol, best_effort, near in (
        (KCAL_TOL_PCT, False, NEAR),
        # the calories are the promise: a floor short at 2% is said, never bought with 5% more
        # food (her Sunday went 75 kcal over to find 2 g of fibre when 5% came first)
        (KCAL_TOL_PCT, True, NEAR),
        (solver.DEFAULT_KCAL_TOL_PCT, False, NEAR),
        (solver.DEFAULT_KCAL_TOL_PCT, True, NEAR),
        # the ask is let go of only when the plates cannot reach the day's calories near it;
        # whatever moves then is named in the result
        (solver.DEFAULT_KCAL_TOL_PCT, False, None),
        (solver.DEFAULT_KCAL_TOL_PCT, True, None),
    ):
        got = solver.solve_day(
            options,
            target,
            lo=lo,
            hi=hi,
            kcal_tol_pct=tol,
            fat_floor_g=float(target.fat_g),
            fibre_min=fibre_min,
            fixed=fixed,
            prefer=prefer,
            near=near,
            best_effort=best_effort,
        )
        if isinstance(got, Infeasible):
            last = got
            continue
        sol = got
        break
    if sol is None:
        # the last try ignores the floors, so what failed it can only be the calories
        if last is not None and "min reachable" in str(last):
            raise Infeasible(
                "Those plates come to more than the whole day, even at their smallest: leave "
                "another one out."
            )
        raise Infeasible(
            "Those plates cannot carry the whole day, even at their largest: keep another meal."
        )
    if lone is not None:
        # said once the solve has shown the plate does carry the day: More on a lunch left alone
        # on a day hundreds of kcal short was told "more of it would put the day over". One free
        # plate's size is the remainder whatever its level, so this is the solve it would get
        eat = "less" if levels[lone] == "less" else "more"
        which = "leave the day short" if eat == "less" else "put the day over"
        raise NothingToBuild(
            f"{_LABEL.get(lone, lone)} is the only plate on {day:%a %d %b} that can change "
            f"size, so it carries the day's calories as it is: {eat} of it would {which}. If "
            f"you eat {eat}, tap Not quite at that evening's check-in."
        )

    keyed = {menu.slot_key(day, m["slot"]): m for m in kept}
    slot_macros = {
        key: recipes.stored_per_serving(
            db, m["recipe_id"], sol.multipliers[m["slot"]], units
        ).as_macros()
        for key, m in keyed.items()
    }
    total = Macros(
        sum(x.kcal for x in slot_macros.values()),
        sum(x.protein_g for x in slot_macros.values()),
        sum(x.fat_g for x in slot_macros.values()),
        sum(x.carb_g for x in slot_macros.values()),
        sum(x.fibre_g for x in slot_macros.values()),
    )
    # what falls short is read off the plates as stored, the same numbers the result states: the
    # solver's own totals round differently, and "protein 193 g ... Short: protein 194 g" read as
    # two answers to one question
    shortfall = tuple(
        (name, got, need)
        for name, got, need in (
            ("protein", total.protein_g, round(target.protein_g)),
            ("fat", total.fat_g, round(target.fat_g)),
            ("fibre", total.fibre_g, fibre_min),
        )
        if got < need
    )
    moved: list[tuple[str, str]] = []
    for slot, want in plain.items():
        size = sol.multipliers[slot]
        if abs(size - want) <= want * NEAR + solver.DEFAULT_STEP:
            continue
        if size < want and want > hi:
            moved.append((slot, "largest"))  # asked for more than any plate of it holds
        elif size > want and want < lo:
            moved.append((slot, "smallest"))
        else:
            moved.append((slot, "bigger" if size > want else "smaller"))
    plan = menu.MenuPlan(
        person_id=person_id,
        segment=menu.Segment(day, day),
        recipe_ids={key: int(m["recipe_id"]) for key, m in keyed.items()},
        multipliers={key: sol.multipliers[m["slot"]] for key, m in keyed.items()},
        day_macros={day: total},
        slot_macros=slot_macros,
        model_used=None,
    )
    return _Solved(person_id, base_id, dict(levels), plan, total, shortfall, tuple(moved))


def _as_planned(
    db: Db, person_id: int, day: dt.date, base_id: int, levels: Mapping[str, str]
) -> _Solved:
    """Copy the original planned portions without solving against newer targets."""
    row = db.one(
        "SELECT id, kcal, protein_g, fat_g, carb_g, fibre_g FROM menu_days "
        "WHERE menu_id = ? AND day = ?",
        (base_id, isoday(day)),
    )
    if row is None:
        raise NothingToBuild("That day's menu has no meals on it.")
    slots = db.all(
        "SELECT slot, recipe_id, portion_multiplier, kcal, protein_g, fat_g, carb_g, fibre_g "
        "FROM meal_slots WHERE menu_day_id = ?",
        (int(row["id"]),),
    )

    def macros(r: Any) -> Macros:
        return Macros(
            round(r["kcal"]),
            round(r["protein_g"]),
            round(r["fat_g"]),
            round(r["carb_g"]),
            round(r["fibre_g"]),
        )

    keyed = {menu.slot_key(day, str(s["slot"])): s for s in slots}
    total = macros(row)
    plan = menu.MenuPlan(
        person_id=person_id,
        segment=menu.Segment(day, day),
        recipe_ids={key: int(s["recipe_id"]) for key, s in keyed.items()},
        multipliers={key: float(s["portion_multiplier"]) for key, s in keyed.items()},
        day_macros={day: total},
        slot_macros={key: macros(s) for key, s in keyed.items()},
        model_used=None,
    )
    return _Solved(person_id, base_id, dict(levels), plan, total, ())


def _store(db: Db, solved: _Solved, day: dt.date, clock: Clock, *, own: Collection[str]) -> int:
    """Put one solved day in force, inside the caller's transaction; returns its menu. `own` is
    the meals whose level the person chose (`state`)."""
    menu_id = menu.store_plan(
        db,
        solved.plan,
        status="proposed",
        source_menu_id=solved.base_id,
        clock=clock,
        own_choice=True,
    )
    menu.decide(db, menu_id, "accepted")
    pair = shared.pair_of(db, solved.base_id)
    if pair is not None:
        db.insert(
            "shared_menu_members",
            shared_menu_id=pair,
            person_id=solved.person_id,
            menu_id=menu_id,
        )
    short_words = "; ".join(f"{n} {got} g of {need} g" for n, got, need in solved.shortfall)
    db.insert(
        "menu_emphasis",
        person_id=solved.person_id,
        day=isoday(day),
        base_menu_id=solved.base_id,
        menu_id=menu_id,
        levels_json=json.dumps(solved.levels, sort_keys=True),
        shortfall=short_words or None,
        own_json=json.dumps(sorted(own)),
    )
    return menu_id


@dataclass(frozen=True)
class Dropped:
    """One meal skipped, or brought back, with the rest of the day exactly as it was."""

    menu_id: int
    day: dt.date
    slot: str
    name: str
    kept: bool
    total: Macros
    target_kcal: int | None

    def said(self) -> str:
        meal = _LABEL.get(self.slot, self.slot)
        lead = f"{meal} is back: {self.name}." if self.kept else f"{meal} skipped: {self.name}."
        under = (self.target_kcal or 0) - self.total.kcal
        tail = (
            f" {self.day:%a %d %b} comes to {self.total.kcal:,} kcal, {under:,} under your "
            f"{self.target_kcal:,}; nothing else moved."
            if self.target_kcal and under > 0
            else f" {self.day:%a %d %b} comes to {self.total.kcal:,} kcal."
        )
        return lead + tail


def drop(db: Db, person_id: int, day: dt.date, slot: str, *, config: Any, clock: Clock) -> Dropped:
    """Skip one meal and leave every other plate of the day exactly as it is - no solving again,
    so the day comes in under its calories and says by how much (`skip_mode` "drop": the owner for
    Stored as resize stores a day: a one-day menu in force, recorded in `menu_emphasis` with the
    meal "skip", so the panel, the check-in and every shopping list read the same day.

    Raises NothingToBuild for a day that cannot change, the last meal of a day, a meal already
    eaten, or a day cooked together (both plates move there, through `resize`); ValueError for a
    meal the day does not have."""
    plates, base_id, st, eaten = _droppable(db, person_id, day, clock)
    if slot not in plates:
        raise ValueError(f"not a meal on that day: {slot}")
    if slot in eaten:
        raise NothingToBuild(f"{_LABEL.get(slot, slot)} has been eaten already today.")
    kept = {s: r for s, r in plates.items() if s != slot}
    if not kept:
        raise NothingToBuild("Keep at least one meal on the day.")
    levels = {**st.get("levels", {}), slot: "skip"}
    own = set(st.get("own", ())) | {slot}
    return _store_plates(
        db, person_id, day, base_id, kept, levels, own, slot=slot, name=str(plates[slot]["name"]),
        kept_back=False, config=config, clock=clock,
    )  # fmt: skip


def keep(db: Db, person_id: int, day: dt.date, slot: str, *, config: Any, clock: Clock) -> Dropped:
    """A skipped meal back on the day, as planned, and every other plate exactly as it is: the
    Undo of `drop`."""
    plates, base_id, st, _eaten = _droppable(db, person_id, day, clock)
    if slot in plates:
        raise NothingToBuild(f"{_LABEL.get(slot, slot)} is already on the day.")
    planned = {
        str(r["slot"]): r
        for r in db.all(
            "SELECT ms.slot, ms.recipe_id, ms.portion_multiplier, ms.kcal, ms.protein_g, "
            "ms.fat_g, ms.carb_g, ms.fibre_g, rc.name FROM menu_days md "
            "JOIN meal_slots ms ON ms.menu_day_id = md.id JOIN recipes rc ON rc.id = ms.recipe_id "
            "WHERE md.menu_id = ? AND md.day = ?",
            (base_id, isoday(day)),
        )
    }
    if slot not in planned:
        raise ValueError(f"not a meal on that day: {slot}")
    levels = {**st.get("levels", {}), slot: "planned"}
    own = set(st.get("own", ())) - {slot}
    return _store_plates(
        db, person_id, day, base_id, {**plates, slot: planned[slot]}, levels, own, slot=slot,
        name=str(planned[slot]["name"]), kept_back=True, config=config, clock=clock,
    )  # fmt: skip


def skip_mode(db: Db, person_id: int, day: dt.date) -> str:
    """What Skip does to this person's day: "share" its calories over the plates left (the
    default) or "drop" it and leave the rest as planned (`/set skip`)."""
    profile = targets_mod.profile_on(db, person_id, day)
    if profile is None or "skip_mode" not in profile.keys():  # noqa: SIM118
        return "share"
    return str(profile["skip_mode"] or "share")


def skip_meal(
    db: Db, person_id: int, day: dt.date, slot: str, skip: bool, *, config: Any, clock: Clock
) -> str:
    """Skip one meal, or have it back, the way this person's Skip works - the shopping list's
    Skip, and the one-tap Skip on a meal's card - and say what became of the day."""
    if skip_mode(db, person_id, day) == "drop":
        done = (drop if skip else keep)(db, person_id, day, slot, config=config, clock=clock)
        return done.said()
    st = state(db, person_id, day) or {}
    levels = {**st.get("levels", {}), slot: "skip" if skip else "planned"}
    with db.tx():
        got = resize(db, person_id, day, levels, config=config, clock=clock)
    return got.said()


def one_skip(had: Mapping[str, str], asked: Mapping[str, str]) -> tuple[str, bool] | None:
    """(the meal, skipped or back) when `asked` changes exactly one meal's level, to or from
    "skip", and nothing else; None for any other change - Less, More, two meals at once."""
    slots = set(had) | set(asked)
    changed = [s for s in slots if had.get(s, "planned") != asked.get(s, "planned")]
    if len(changed) != 1:
        return None
    slot = changed[0]
    before, after = had.get(slot, "planned"), asked.get(slot, "planned")
    if after == "skip":
        return slot, True
    if before == "skip" and after == "planned":
        return slot, False
    return None


def _droppable(
    db: Db, person_id: int, day: dt.date, clock: Clock
) -> tuple[dict[str, Any], int, dict[str, Any], frozenset[str]]:
    """(the plates in force by meal, the menu they are measured against, the day's state, the
    meals of today already eaten), or NothingToBuild saying why the day's meals cannot be
    skipped one by one."""
    from . import swap  # noqa: PLC0415 (swap reads plates through this package)

    today = clock.today_local()
    base_id, _meals = _changeable(db, person_id, day, today)
    if cooked_with(db, person_id, day) is not None:
        raise NothingToBuild(
            "You cook that day together: Less, More and Skip on the day's panel move both plates."
        )
    winner = menu.accepted_menu_id(db, person_id, day)
    rows = db.all(
        "SELECT ms.slot, ms.recipe_id, ms.portion_multiplier, ms.kcal, ms.protein_g, ms.fat_g, "
        "ms.carb_g, ms.fibre_g, rc.name FROM menu_days md "
        "JOIN meal_slots ms ON ms.menu_day_id = md.id JOIN recipes rc ON rc.id = ms.recipe_id "
        "WHERE md.menu_id = ? AND md.day = ?",
        (winner, isoday(day)),
    )
    eaten = swap.had_today(db, person_id, clock.now_local()) if day == today else frozenset()
    plates = {str(r["slot"]): r for r in rows}
    return plates, base_id, state(db, person_id, day) or {}, eaten


def _store_plates(
    db: Db,
    person_id: int,
    day: dt.date,
    base_id: int,
    plates: Mapping[str, Any],
    levels: Mapping[str, str],
    own: Collection[str],
    *,
    slot: str,
    name: str,
    kept_back: bool,
    config: Any,
    clock: Clock,
) -> Dropped:
    """These plates, as they are, put in force for the day: no meal solved again."""

    def macros(r: Any) -> Macros:
        return Macros(
            round(r["kcal"]),
            round(r["protein_g"]),
            round(r["fat_g"]),
            round(r["carb_g"]),
            round(r["fibre_g"]),
        )

    keyed = {menu.slot_key(day, s): r for s, r in plates.items()}
    each = {key: macros(r) for key, r in keyed.items()}
    total = Macros(
        sum(m.kcal for m in each.values()),
        sum(m.protein_g for m in each.values()),
        sum(m.fat_g for m in each.values()),
        sum(m.carb_g for m in each.values()),
        sum(m.fibre_g for m in each.values()),
    )
    plan = menu.MenuPlan(
        person_id=person_id,
        segment=menu.Segment(day, day),
        recipe_ids={key: int(r["recipe_id"]) for key, r in keyed.items()},
        multipliers={key: float(r["portion_multiplier"]) for key, r in keyed.items()},
        day_macros={day: total},
        slot_macros=each,
        model_used=None,
    )
    row = targets_mod.ensure_targets(db, person_id, day, config, clock=clock)
    target = int(targets_mod.macros_of(row).kcal) if row is not None else None
    with db.tx():
        menu_id = _store(db, _Solved(person_id, base_id, dict(levels), plan, total, ()), day,
                         clock, own=own)  # fmt: skip
    return Dropped(menu_id, day, slot, name, kept_back, total, target)


__all__ = [
    "FACTOR",
    "KCAL_TOL_PCT",
    "LEVELS",
    "NEAR",
    "Dropped",
    "Resized",
    "base_meals",
    "boxed_slots",
    "cooked_with",
    "day_info",
    "drop",
    "keep",
    "one_skip",
    "one_tap",
    "past_meals",
    "resettable",
    "resize",
    "skip_meal",
    "skip_mode",
    "state",
    "whole_menu_of",
]
