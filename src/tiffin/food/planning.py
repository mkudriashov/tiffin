"""Read and change a person's menu planning and cooking rhythm.

Planning may be automatic, chosen weekly or off. Cooking may be daily or in
batches. Changes are stored as new rows and take effect on their stated day."""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from .. import targets as targets_mod
from ..clock import isoday, week_start
from ..db import Db
from . import batch as batch_mod

#: auto: menus are built for them; choose: they pick each weekday meal on Sunday from a few
#: dishes (food/choices.py); off: targets and tracking only - no menus, no lists
PLANNING = ("auto", "choose", "off")
#: the weekday cooks offered by name; any other grouping set with /set stays theirs, "your own"
CADENCES: dict[str, list[list[int]]] = {
    "daily": [[0], [1], [2], [3], [4]],
    "batch": [[0, 1, 2, 3, 4]],
    "batch2": [[0, 1, 2], [3, 4]],
}
OPTIONS = (2, 3, 4)
DEFAULT_OPTIONS = 3
#: when a change counts from: the next week, or the first day not yet planned
STARTS = ("monday", "tomorrow")
_DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def planning_of(profile: sqlite3.Row | None) -> str:
    """auto, choose or off. A row from before the column read the way it was written: weekly
    choices on (`choices` above 0) is choose, anything else auto."""
    if profile is None:
        return "auto"
    keys = profile.keys()
    if "planning" in keys and profile["planning"] in PLANNING:
        return str(profile["planning"])
    if "choices" in keys and int(profile["choices"] or 0) > 0:
        return "choose"
    return "auto"


def options_of(profile: sqlite3.Row | None) -> int:
    """How many dishes each meal is offered when they choose."""
    if profile is None or "choices" not in profile.keys():  # noqa: SIM118
        return DEFAULT_OPTIONS
    n = int(profile["choices"] or 0)
    return n if n in OPTIONS else DEFAULT_OPTIONS


def groups_of(profile: sqlite3.Row | None) -> list[list[int]]:
    """The weekday cooks, each a group of weekdays (0 Monday); one Monday-to-Friday cook when
    unset or unreadable."""
    raw = profile["segment_days"] if profile is not None else None
    try:
        parsed = json.loads(str(raw)) if raw else CADENCES["batch"]
    except ValueError:
        parsed = CADENCES["batch"]
    groups: list[list[int]] = []
    if isinstance(parsed, list):
        for g in parsed:
            if isinstance(g, list):
                days = sorted({int(d) for d in g if isinstance(d, int) and 0 <= d <= 6})
                if days:
                    groups.append(days)
    return groups


def cadence_of(profile: sqlite3.Row | None) -> str:
    """daily, batch, batch2, or custom for a grouping of their own."""
    groups = sorted(groups_of(profile))
    for name, shape in CADENCES.items():
        if groups == sorted(shape):
            return name
    return "custom"


def cooked_ahead(profile: sqlite3.Row | None, day: dt.date) -> bool:
    """Return whether this weekday is covered by a cook spanning several days.

    A single-day cook and a weekend meal are prepared on the day itself.
    """
    if day.weekday() >= 5:
        return False
    for group in groups_of(profile):
        if day.weekday() in group:
            return len(group) > 1
    return False


def batched(profile: sqlite3.Row | None) -> tuple[str, ...]:
    """The meals made ahead: the batch setting, when any cook of theirs is more than a day."""
    if profile is None or not any(len(g) > 1 for g in groups_of(profile)):
        return ()
    return batch_mod.read_slots(profile["batch_slots"])


def next_monday(today: dt.date) -> dt.date:
    """The Monday after today: the first week nothing of has been planned yet."""
    return week_start(today) + dt.timedelta(days=7)


def _days_said(days: Sequence[int]) -> str:
    if len(days) == 1:
        return _DAY_NAMES[days[0]]
    if list(days) == list(range(days[0], days[-1] + 1)):
        joiner = " and " if len(days) == 2 else " to "
        return f"{_DAY_NAMES[days[0]]}{joiner}{_DAY_NAMES[days[-1]]}"
    return ", ".join(_DAY_NAMES[d] for d in days[:-1]) + " and " + _DAY_NAMES[days[-1]]


@dataclass(frozen=True)
class Plan:
    """How one person plans, as of one day."""

    planning: str
    options: int
    cadence: str
    groups: tuple[tuple[int, ...], ...]
    #: the meals made ahead in a batch; () for someone who cooks each day
    batch: tuple[str, ...]
    freezes: bool

    def cooking_said(self) -> str:
        """How they cook, in words: "you cook each day", "you cook lunch and dinner on Sunday
        for Monday to Friday"."""
        if self.cadence == "daily" or not any(len(g) > 1 for g in self.groups):
            return "you cook each day"
        if not self.batch:
            return "you cook each day (nothing is made ahead)"
        what = batch_mod.describe(self.batch)
        cooks = []
        for g in self.groups:
            if len(g) > 1:
                before = _DAY_NAMES[(g[0] - 1) % 7]
                cooks.append(f"on {before} for {_days_said(g)}")
        rest = "" if set(self.batch) >= {"breakfast", "lunch", "dinner"} else ", the rest fresh"
        return f"you cook {what} {' and '.join(cooks)}{rest}"

    def said(self) -> str:
        if self.planning == "off":
            return (
                "Tracking only: your targets, weight and progress, with no menus and no "
                "shopping lists."
            )
        if self.planning == "choose":
            return (
                f"Each Sunday you choose every weekday meal from {self.options} dishes; "
                f"{self.cooking_said()}."
            )
        return f"Menus are planned for you; {self.cooking_said()}."

    def as_dict(self) -> dict[str, Any]:
        return {
            "planning": self.planning,
            "options": self.options,
            "cadence": self.cadence,
            "batch": list(self.batch),
            "freezes": self.freezes,
            "said": self.said(),
        }


def plan_of(profile: sqlite3.Row | None) -> Plan:
    groups = tuple(tuple(g) for g in groups_of(profile))
    return Plan(
        planning=planning_of(profile),
        options=options_of(profile),
        cadence=cadence_of(profile),
        groups=groups,
        batch=batched(profile),
        freezes=bool(profile["freezes"]) if profile is not None else False,
    )


class BadPlan(ValueError):
    """A change that is not one of the answers: said to the person as it is."""


def change(
    db: Db,
    person_id: int,
    *,
    today: dt.date,
    starts: str = "monday",
    planning: str | None = None,
    options: int | None = None,
    cadence: str | None = None,
    batch: Sequence[str] | None = None,
    freezes: bool | None = None,
) -> dt.date:
    """Book a change of how someone plans, and the day it counts from: next Monday, or tomorrow.

    Only what is given changes. Choosing their meals with no number set offers the usual three.
    Tomorrow means every day not yet planned: a day that already has a menu keeps it, because it
    may already be bought or cooked - the page rebuilds it when asked. BadPlan for anything that
    is not one of the answers; LookupError when there is no profile yet."""
    if starts not in STARTS:
        raise BadPlan("starts is next Monday or tomorrow")
    current = targets_mod.profile_on(db, person_id, today)
    if current is None:
        raise LookupError("no profile yet")
    changes: dict[str, object] = {}
    if planning is not None:
        if planning not in PLANNING:
            raise BadPlan("planning is auto, choose or off")
        changes["planning"] = planning
        if planning == "choose" and options is None and int(current["choices"] or 0) == 0:
            changes["choices"] = DEFAULT_OPTIONS
    if options is not None:
        if options not in OPTIONS:
            raise BadPlan("a meal is offered 2, 3 or 4 dishes")
        changes["choices"] = options
    if cadence is not None:
        if cadence not in CADENCES:
            raise BadPlan("cooking is daily, batch or batch2")
        changes["segment_days"] = json.dumps(CADENCES[cadence])
    if batch is not None:
        meals = [str(m) for m in batch]
        if any(m not in batch_mod.SLOTS for m in meals):
            raise BadPlan("the meals made ahead are breakfast, lunch, dinner, snack or pre_bed")
        changes["batch_slots"] = json.dumps([s for s in batch_mod.SLOTS if s in meals])
    if freezes is not None:
        changes["freezes"] = 1 if freezes else 0
    if not changes:
        raise BadPlan("nothing to change")
    effective = today + dt.timedelta(days=1) if starts == "tomorrow" else next_monday(today)
    targets_mod.change_profile(db, person_id, changes, today=today, effective=effective)
    return effective


def view(db: Db, person_id: int, today: dt.date) -> dict[str, Any] | None:
    """How they plan now, and what is booked to change and from when - for the page."""
    from . import menu as menu_mod  # noqa: PLC0415 (menu imports the food layer)

    now = targets_mod.profile_on(db, person_id, today)
    if now is None:
        return None
    out: dict[str, Any] = {
        "now": plan_of(now).as_dict(),
        "booked": None,
        "meals": menu_mod.slots_for(now),
        "next_monday": isoday(next_monday(today)),
    }
    # the batch setting as stored, for the switches: a daily cook's is kept for the day they batch
    out["now"]["batch_setting"] = list(batch_mod.read_slots(now["batch_slots"]))
    later = targets_mod.booked(db, person_id, today)
    if later is not None and plan_of(later) != plan_of(now):
        start = dt.date.fromisoformat(str(later["effective_from"]))
        booked = plan_of(later).as_dict()
        booked["batch_setting"] = list(batch_mod.read_slots(later["batch_slots"]))
        booked["from"] = isoday(start)
        booked["from_said"] = f"{start:%a} {start.day} {start:%b}"
        out["booked"] = booked
    return out


__all__ = [
    "CADENCES",
    "DEFAULT_OPTIONS",
    "OPTIONS",
    "PLANNING",
    "STARTS",
    "BadPlan",
    "Plan",
    "batched",
    "cadence_of",
    "change",
    "cooked_ahead",
    "groups_of",
    "next_monday",
    "options_of",
    "plan_of",
    "planning_of",
    "view",
]
