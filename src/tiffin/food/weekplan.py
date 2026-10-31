"""A person's weekly cooking and shopping preferences.

The latest `week_prefs` row wins. Unset fields keep the default planning rhythm.
`plan_day` and `shops` set the build day and shopping windows. `topup_list_day`
sets the later list's delivery day. `freeze_packs`, `nocook_day`,
`lunch_from_dinner`, `breakfasts`, `dinner_minutes`, `full_steps`,
`tinned_fish`, `same_food` and `steak_day` shape the meals. `check_day`,
`training_kcal`, `quiet`, `tape_sites` and `tape_unit` shape reminders.
`least_waste` chooses pack sizes with less surplus where available."""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import asdict, dataclass, field, fields
from typing import Any

from ..db import Db

SOURCES = ("page", "chat", "operator")


@dataclass(frozen=True)
class WeekPlan:
    plan_day: int | None = None
    shops: tuple[tuple[int, ...], ...] = field(default_factory=tuple)
    topup_list_day: int | None = None
    freeze_packs: bool = True
    nocook_day: int | None = None
    lunch_from_dinner: bool = False
    breakfasts: int = 0
    dinner_minutes: int = 0
    full_steps: bool = False
    tinned_fish: int | None = None
    check_day: int | None = None
    training_kcal: int = 0
    quiet: bool = False
    least_waste: bool = False
    #: weekday groups that eat the same lunch and the same dinner, cooked once as portions
    same_food: tuple[tuple[int, ...], ...] = field(default_factory=tuple)
    #: the weekday whose dinner is a steak (sirloin or rump), the one thing cooked on a no-cook day
    steak_day: int | None = None
    #: each `same_food` group cooks a different meat or fish from the others
    rotate_mains: bool = False
    #: the tape sites the weekly check asks for, in the order asked; () is waist and hip
    tape_sites: tuple[str, ...] = field(default_factory=tuple)
    #: what a tape is read in: "cm" or "in" (stored in centimetres either way)
    tape_unit: str = "cm"

    def sites(self) -> tuple[str, ...]:
        return self.tape_sites or ("waist", "hip")

    def shop_of(self, weekday: int) -> tuple[int, ...] | None:
        """The weekdays bought in the same shop as `weekday`, when shops are set."""
        return next((g for g in self.shops if weekday in g), None)


HOUSE = WeekPlan()


def _weekday(v: Any) -> int | None:
    if v is None:
        return None
    n = int(v)
    if not 0 <= n <= 6:
        raise ValueError(f"a weekday is 0 (Monday) to 6 (Sunday), not {v!r}")
    return n


def _clean(data: dict[str, Any]) -> WeekPlan:
    """A WeekPlan from stored or given JSON; unknown keys are ignored, bad values refused."""
    known = {f.name for f in fields(WeekPlan)}
    got: dict[str, Any] = {k: v for k, v in data.items() if k in known}
    if "same_food" in got:
        groups = tuple(tuple(sorted({int(d) for d in g})) for g in got["same_food"] or ())
        days = [d for g in groups for d in g]
        if len(days) != len(set(days)) or any(not 0 <= d <= 4 for d in days):
            raise ValueError("same_food is groups of weekdays 0-4, each in one group")
        got["same_food"] = tuple(g for g in groups if len(g) > 1)
    for key in ("plan_day", "topup_list_day", "nocook_day", "check_day", "steak_day"):
        if key in got:
            got[key] = _weekday(got[key])
    if "shops" in got:
        shops = tuple(tuple(sorted({int(d) for d in g})) for g in got["shops"] or ())
        days = [d for g in shops for d in g]
        if len(days) != len(set(days)) or any(not 0 <= d <= 4 for d in days):
            raise ValueError("shops are groups of weekdays 0-4, each in one group")
        got["shops"] = shops
    for key in (
        "freeze_packs",
        "lunch_from_dinner",
        "full_steps",
        "quiet",
        "least_waste",
        "rotate_mains",
    ):
        if key in got:
            got[key] = bool(got[key])
    for key, top in (("breakfasts", 5), ("dinner_minutes", 120), ("training_kcal", 600)):
        if key in got:
            n = int(got[key] or 0)
            if not 0 <= n <= top:
                raise ValueError(f"{key} is 0 to {top}, not {n}")
            got[key] = n
    if "tape_sites" in got:
        from .. import body  # noqa: PLC0415 (body imports the db layer, not the food layer)

        sites = tuple(str(x) for x in got["tape_sites"] or ())
        if any(x not in body.TAPE_SITES for x in sites) or len(set(sites)) != len(sites):
            raise ValueError(f"tape sites are some of {body.TAPE_SITES}, each once")
        got["tape_sites"] = sites
    if "tape_unit" in got and got["tape_unit"] not in ("cm", "in"):
        raise ValueError("tape_unit is cm or in")
    if "tinned_fish" in got and got["tinned_fish"] is not None:
        got["tinned_fish"] = max(0, int(got["tinned_fish"]))
    return WeekPlan(**got)


def of(db: Db, person_id: int) -> WeekPlan:
    """Their week, or the house way when they have said nothing."""
    row = db.one(
        "SELECT prefs_json FROM week_prefs WHERE person_id = ? ORDER BY id DESC LIMIT 1",
        (person_id,),
    )
    if row is None:
        return HOUSE
    try:
        return _clean(json.loads(str(row["prefs_json"])))
    except ValueError, TypeError:
        return HOUSE


def as_json(plan: WeekPlan) -> str:
    data = asdict(plan)
    data["shops"] = [list(g) for g in plan.shops]
    data["tape_sites"] = list(plan.tape_sites)
    data["same_food"] = [list(g) for g in plan.same_food]
    return json.dumps(data, sort_keys=True)


def change(db: Db, person_id: int, *, source: str, **changes: Any) -> WeekPlan:
    """A new row: what they had, with `changes`. Refuses a bad value before writing anything."""
    if source not in SOURCES:
        raise ValueError(f"source is one of {SOURCES}")
    now = asdict(of(db, person_id))
    plan = _clean({**now, **changes})
    db.insert("week_prefs", person_id=person_id, prefs_json=as_json(plan), source=source)
    return plan


__all__ = [
    "HOUSE",
    "STEAK_CODES",
    "WeekPlan",
    "as_json",
    "change",
    "day_notes",
    "follows",
    "of",
    "week_lines",
]

#: what a steak day's dinner is cooked from
STEAK_CODES = ("sirloin_steak", "rump_steak")
#: what a steak day's other meals are built on
STEAK_DAY_FISH_WORDS = ("sardine", "smoked salmon", "salmon, smoked", "tuna")


DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
#: the tinned fish a week is held to (`tinned_fish`), by what the food is called
TINNED_FISH_WORDS = ("tuna", "sardine", "mackerel", "salmon", "anchov", "pilchard")


def follows(plan: WeekPlan, day: dt.date) -> bool:
    """Is `day`'s lunch the day before's dinner, reheated? Never the first weekday (cooked that
    morning), never after the no-cook day (nothing was cooked), and never across two shops (the
    food for it was bought in the other)."""
    if not plan.lunch_from_dinner or not 1 <= day.weekday() <= 4:
        return False
    before = day.weekday() - 1
    if plan.nocook_day is not None and before == plan.nocook_day:
        return False
    return not plan.shops or plan.shop_of(before) == plan.shop_of(day.weekday())


def week_lines(plan: WeekPlan) -> list[str]:
    """How their week runs, said to the model in sentences; [] for the house way."""
    out: list[str] = []
    if plan.lunch_from_dinner:
        out.append(
            "A day marked 'lunch is yesterday's dinner' has that lunch made the evening before "
            "and reheated: write it as exactly the same dish as the day before's dinner, so those "
            "dinners must keep in the fridge overnight and reheat well. Any other lunch is cooked "
            "that morning."
        )
    for group in plan.same_food:
        names = " and ".join(DAY_NAMES[d] for d in group)
        out.append(
            f"{names} eat the same lunch and the same dinner, each cooked once as portions for "
            "those days: write those days' lunch, and their dinner, as the same dish."
        )
    if plan.rotate_mains and len(plan.same_food) > 1:
        out.append(
            "Each of those stretches cooks a different meat or fish from the other: beef one, "
            "chicken the other, say."
        )
    if plan.nocook_day is not None:
        steak = plan.steak_day == plan.nocook_day
        out.append(
            f"{DAY_NAMES[plan.nocook_day]} is a no-cook day: nothing on the hob or in the oven, "
            "every dish put together (the microwave is fine)"
            + (
                " - except dinner, which is a sirloin or rump steak, simply pan-fried or grilled. "
                "Its other meals are put together from no-cook foods - tinned tuna, sardines, "
                "smoked salmon, skyr, cottage cheese, wraps, fruit - with fish in at least one."
                if steak
                else "."
            )
        )
    if plan.steak_day is not None and plan.steak_day != plan.nocook_day:
        out.append(f"{DAY_NAMES[plan.steak_day]}'s dinner is a sirloin or rump steak.")
    if plan.breakfasts:
        out.append(f"Breakfasts: {plan.breakfasts} different ones, taking turns over the days.")
    if plan.full_steps:
        out.append(
            "Write every method step in full - amounts, heat and times - like 'Heat 1 tsp oil in "
            "a large pan on a medium-high heat. Add the mince, break it up and fry for 6 to 8 "
            "minutes until browned.'"
        )
    if plan.tinned_fish is not None:
        out.append(f"Tinned fish (tuna, sardines) in at most {plan.tinned_fish} meals a week.")
    if plan.training_kcal:
        out.append("A day marked 'training day' has a little more food: its targets say so.")
    return out


def day_notes(plan: WeekPlan, day: dt.date, *, training: bool = False) -> str:
    """The words on one day's line of the prompt: "no cooking", "lunch is yesterday's dinner"."""
    parts: list[str] = []
    if plan.nocook_day is not None and day.weekday() == plan.nocook_day:
        parts.append(
            "a no-cook day, nothing on the hob or in the oven"
            + (", but dinner is a steak" if plan.steak_day == day.weekday() else "")
        )
    elif plan.steak_day is not None and day.weekday() == plan.steak_day:
        parts.append("dinner is a sirloin or rump steak")
    group = next((g for g in plan.same_food if day.weekday() in g), None)
    if group is not None:
        others = [DAY_NAMES[d][:3] for d in group if d != day.weekday()]
        parts.append(f"same lunch and dinner as {', '.join(others)}")
    if follows(plan, day):
        parts.append("lunch is yesterday's dinner, the same dish")
    if training:
        parts.append("training day")
    return "; ".join(parts)
