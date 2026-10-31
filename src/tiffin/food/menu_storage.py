"""Store menu decisions and resolve which menu covers each day."""

from __future__ import annotations

import datetime as dt
import sqlite3
from collections.abc import Callable, Mapping
from typing import Any

from ..clock import Clock, isoday
from ..db import Db
from ..errors import NothingToBuild
from . import catalogue, recipes
from .menu import (
    SLOT_ORDER,
    STATUSES,
    MenuPlan,
    Segment,
    current_menu,
    logger,
    slots_for,
    variety_of,
)


def day_confirmed(db: Db, person_id: int, day: dt.date) -> bool:
    """Return whether the person has recorded the day as confirmed."""
    return _confirmed(db, person_id, day)


def _confirmed(db: Db, person_id: int, day: dt.date) -> bool:
    row = db.one(
        "SELECT status FROM day_confirmations WHERE person_id = ? AND day = ? "
        "ORDER BY id DESC LIMIT 1",
        (person_id, isoday(day)),
    )
    return row is not None and str(row["status"]) == "confirmed"


def _decided_on(row: sqlite3.Row, timezone: dt.tzinfo) -> dt.date:
    """Return the local day on which a menu decision was made."""
    from ..clock import parse_stamp  # noqa: PLC0415

    return parse_stamp(str(row["decided_at"])).astimezone(timezone).date()


def open_days(db: Db, person_id: int, segment: Segment, today: dt.date) -> Segment | None:
    """Return the contiguous suffix of a segment that a new menu may cover.

    Confirmed and past days are closed. Today is closed when its existing menu was decided
    before today; a menu created today can still be replaced.
    """

    def closed(day: dt.date) -> bool:
        if _confirmed(db, person_id, day):
            return True
        if day > today:
            return False
        if day < today:
            # A past day stays closed even if no menu was recorded for it.
            return True
        held = current_menu(db, person_id, day)
        if held is None:
            return False
        if day < today:
            return True
        # Today's menu is protected only when it was in force at the day's start.
        return _decided_on(held, db.clock.timezone) < today

    days_ = segment.days
    first = next((i for i, d in enumerate(days_) if not closed(d)), None)
    if first is None or any(closed(d) for d in days_[first:]):
        # a closed day after an open one (a confirmed future day) would split the menu; the
        # tail after the last closed day is the only safe run
        last_closed = max((i for i, d in enumerate(days_) if closed(d)), default=-1)
        first = last_closed + 1
        if first >= len(days_):
            return None
    return Segment(days_[first], segment.end)


def store_plan(
    db: Db,
    plan: MenuPlan,
    *,
    status: str = "proposed",
    source_menu_id: int | None = None,
    clock: Clock | None = None,
    own_choice: bool = False,
) -> int:
    """Store a plan on open days and return its menu id.

    `clock` defaults to the database's own - the one its rows are stamped with - never the
    process's: "today" decides which days are still open, and a day that has gone never is.

    Raises NothingToBuild when every day of the plan is already closed (`open_days`).
    `own_choice` is a person resizing their own plates: any day from today on that they have
    not recorded is theirs to change, a started one included - the started-day rule protects
    shopped-for food from a rebuild nobody asked for, not a person from their own decision.
    """
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    today = (clock or db.clock).today_local()
    if own_choice:
        if any(d < today or _confirmed(db, plan.person_id, d) for d in plan.segment.days):
            raise NothingToBuild("That day has gone or has been recorded.")
        segment: Segment | None = plan.segment
    else:
        segment = open_days(db, plan.person_id, plan.segment, today)
    if segment is None:
        raise NothingToBuild("Those days have already started or been recorded.")
    units = catalogue.units(db)
    with db.tx():
        menu_id = db.insert(
            "menus",
            person_id=plan.person_id,
            segment_start=isoday(segment.start),
            segment_end=isoday(segment.end),
            status=status,
            source_menu_id=source_menu_id,
            model_used=plan.model_used,
        )
        for day in segment.days:
            m = plan.day_macros[day]
            day_id = db.insert(
                "menu_days",
                menu_id=menu_id,
                day=isoday(day),
                kcal=m.kcal,
                protein_g=m.protein_g,
                fat_g=m.fat_g,
                carb_g=m.carb_g,
                fibre_g=m.fibre_g,
            )
            for key, recipe_id in sorted(plan.recipe_ids.items(), key=_slot_sort):
                if not key.startswith(isoday(day) + "|"):
                    continue
                slot = key.split("|", 1)[1]
                sm = plan.slot_macros[key]
                mult = plan.multipliers[key]
                db.insert(
                    "meal_slots",
                    menu_day_id=day_id,
                    slot=slot,
                    recipe_id=recipe_id,
                    portion_multiplier=mult,
                    # one serving, for the same reason the macros above are per serving, with the
                    # foods served whole counted whole
                    portion_g=round(recipes.plate_g(db, recipe_id, mult, units), 1),
                    kcal=sm.kcal,
                    protein_g=sm.protein_g,
                    fat_g=sm.fat_g,
                    carb_g=sm.carb_g,
                    fibre_g=sm.fibre_g,
                )
        # the options code did not choose stay one tap away, on that meal's swap list
        for slot, recipe_id in plan.spare:
            db.insert(
                "built_dishes",
                person_id=plan.person_id,
                recipe_id=recipe_id,
                day=isoday(segment.start),
                slot=slot,
                model_used=plan.model_used,
            )
    return menu_id


def _slot_sort(pair: tuple[str, int]) -> tuple[str, int]:
    key = pair[0]
    day, _, slot = key.partition("|")
    order = SLOT_ORDER.index(slot) if slot in SLOT_ORDER else len(SLOT_ORDER)
    return (day, order)


def repeat_last(
    db: Db, person_id: int, segment: Segment, *, clock: Clock | None = None
) -> int | None:
    """Copy the last accepted menu onto this segment. None when there is none.

    A plain copy: no refused-food gate here. Callers go through
    `menu_propose.repeat_less_refused`, which swaps out every dish holding a food refused since.
    The scheduled build falls back to a repeat when every attempt fails, with nobody watching
    """
    # "last accepted" now means the view's predicate: a repeat, or a latest decision of accepted -
    # Repeat a weekday menu for weekday cooking and a weekend menu for weekend cooking.
    # person with no menu of that kind yet gets the other: a menu is better than none
    weekend = all(d.weekday() >= 5 for d in segment.days)
    source = db.one(
        "SELECT * FROM v_menu_current WHERE person_id = ? "
        "ORDER BY ((strftime('%w', segment_start) IN ('0', '6') "
        "AND strftime('%w', segment_end) IN ('0', '6')) = ?) DESC, id DESC LIMIT 1",
        (person_id, int(weekend)),
    )
    if source is None:
        return None
    old_days = db.all(
        "SELECT * FROM menu_days WHERE menu_id = ? ORDER BY day", (int(source["id"]),)
    )
    if not old_days:
        return None
    # A repeat observes the same open-day rule as a new build.
    open_ = open_days(db, person_id, segment, (clock or db.clock).today_local())
    if open_ is None:
        return None
    segment = open_
    with db.tx():
        menu_id = db.insert(
            "menus",
            person_id=person_id,
            segment_start=isoday(segment.start),
            segment_end=isoday(segment.end),
            status="repeated",
            source_menu_id=int(source["id"]),
            model_used=source["model_used"],
        )
        # a batch cook eats the same lunch and dinner all week: their repeat is one day, every
        # repeated for the whole week if I am batch cooking")
        from .. import targets as targets_mod  # noqa: PLC0415 (targets imports the food layer)

        profile = targets_mod.profile_on(db, person_id, segment.start)
        same = profile is not None and set(variety_of(profile, slots_for(profile)).values()) == {
            "same"
        }
        for i, day in enumerate(segment.days):
            old = old_days[0] if same else old_days[i % len(old_days)]
            new_day = db.insert(
                "menu_days",
                menu_id=menu_id,
                day=isoday(day),
                kcal=old["kcal"],
                protein_g=old["protein_g"],
                fat_g=old["fat_g"],
                carb_g=old["carb_g"],
                fibre_g=old["fibre_g"],
            )
            for slot in db.all(
                "SELECT * FROM meal_slots WHERE menu_day_id = ? ORDER BY id", (int(old["id"]),)
            ):
                db.insert(
                    "meal_slots",
                    menu_day_id=new_day,
                    slot=slot["slot"],
                    recipe_id=slot["recipe_id"],
                    portion_multiplier=slot["portion_multiplier"],
                    portion_g=slot["portion_g"],
                    kcal=slot["kcal"],
                    protein_g=slot["protein_g"],
                    fat_g=slot["fat_g"],
                    carb_g=slot["carb_g"],
                    fibre_g=slot["fibre_g"],
                )
    return menu_id


def decide(db: Db, menu_id: int, decision: str, *, message_in_id: int | None = None) -> None:
    """accepted | rejected | lapsed. One menu_decisions row; `menus` itself never changes.

    v_menu_current reads the latest decision, so accepting a later menu for the same segment
    supersedes the earlier one without touching either row (the ledger rule).
    """
    if decision not in ("accepted", "rejected", "lapsed"):
        raise ValueError(decision)
    row = db.one(
        "SELECT id, person_id, segment_start, segment_end FROM menus WHERE id = ?", (menu_id,)
    )
    if row is None:
        raise LookupError(f"no menu {menu_id}")
    newly = refused_brought_in(db, row) if decision == "accepted" else []
    db.insert("menu_decisions", menu_id=menu_id, decision=decision, message_in_id=message_in_id)
    if newly:
        # the promise is that a refused food never comes back on a menu built after it: one that
        # does is logged with its count, never its names (logs carry person ids only), and
        # prefs-check counts them.
        logger.warning(
            "a menu came into force with a food the person refuses",
            extra={
                "event": "refused_on_menu",
                "person_id": int(row["person_id"]),
                "menu_id": menu_id,
                "foods": len(newly),
            },
        )


def newly_placed(db: Db, menu_id: int) -> list[int]:
    """The recipes this menu puts on a plate that was not already that dish: every plate of a
    fresh build, or of a repeat onto new days; of a menu made from another over the same days
    (`source_menu_id`: a plate resized, a dish swapped), only the plates that changed."""
    plates = db.all(
        "SELECT md.day, ms.slot, ms.recipe_id, rc.name FROM menu_days md "
        "JOIN meal_slots ms ON ms.menu_day_id = md.id JOIN recipes rc ON rc.id = ms.recipe_id "
        "WHERE md.menu_id = ?",
        (menu_id,),
    )
    src = db.one(
        "SELECT s.id FROM menus m JOIN menus s ON s.id = m.source_menu_id WHERE m.id = ? "
        "AND s.segment_start <= m.segment_end AND m.segment_start <= s.segment_end",
        (menu_id,),
    )
    was = (
        {
            (str(r["day"]), str(r["slot"]), str(r["name"]).casefold())
            for r in db.all(
                "SELECT md.day, ms.slot, rc.name FROM menu_days md "
                "JOIN meal_slots ms ON ms.menu_day_id = md.id "
                "JOIN recipes rc ON rc.id = ms.recipe_id WHERE md.menu_id = ?",
                (int(src["id"]),),
            )
        }
        if src is not None
        else set()
    )
    return sorted(
        {
            int(r["recipe_id"])
            for r in plates
            if (str(r["day"]), str(r["slot"]), str(r["name"]).casefold()) not in was
        }
    )


def refused_brought_in(db: Db, menu_row: Any) -> list[str]:
    """The foods a person refuses that this menu newly puts on a plate (`newly_placed`). A plate
    resized or a dish swapped on a menu built before a refusal still holds the food on its other
    plates, rightly: that menu was built first; a dish swapped in that holds it does not. Names,
    for tests and prefs-check; never for a log."""
    person_id = int(menu_row["person_id"])
    out: set[str] = set()
    for rid in newly_placed(db, int(menu_row["id"])):
        out.update(catalogue.refused_names_in_recipe(db, person_id, rid))
    return sorted(out)


def accepted_menu_id(db: Db, person_id: int, day: dt.date) -> int | None:
    """The menu whose slots feed `day`, or None when nothing is accepted for it.

    More than one menu can list `day` in its range at once (a partial replace does not blank
    the days it never touched, and a day's plates resized by the person are a one-day menu of
    their own), so the winner is whichever was DECIDED most recently - `v_menu_days_current` -
    not the one with the higher menu id: a rebuild can be created before an earlier proposal is
    actually tapped Accept. For the stretch of days a shopping list or a batch cook is for, see
    `segment_menu_id`.
    """
    row = db.one(
        "SELECT menu_id FROM v_menu_days_current WHERE person_id = ? AND day = ?",
        (person_id, isoday(day)),
    )
    return None if row is None else int(row["menu_id"])


def segment_menu_id(db: Db, person_id: int, day: dt.date) -> int | None:
    """The menu for the stretch of days covering `day` - the one a shopping list is built from.

    Never a day's resized plates (`plates.resize`), which `v_menu_current` leaves out: a list
    built from one resized Monday would buy one day of food for a five-day week.
    """
    row = current_menu(db, person_id, day)
    return None if row is None else int(row["id"])


ProposeFn = Callable[[str, Mapping[str, Any]], tuple[Mapping[str, Any], str | None]]
