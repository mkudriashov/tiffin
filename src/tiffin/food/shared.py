"""Build paired weekend menus with shared recipes and individual portions.

Both people must consent to shared meals. The model selects dishes from foods both may
eat; code solves each person's portions against their own targets. Each person retains
their own menu ledger entries."""

from __future__ import annotations

import datetime as dt
import sqlite3
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..clock import DEFAULT_CLOCK, Clock, isoday
from ..db import Db
from ..energy import Macros
from ..errors import Infeasible, Lopsided, ModelError, ModelInvalid
from ..persons import Person
from . import catalogue, menu, planning

#: the wording both people agree to, recorded on their consents row
CONSENT_VERSION = "shared_v1"
CONSENT_KIND = "shared_meals"
SATURDAY, SUNDAY = 5, 6
#: the day both versions of a weekend are built (menu_shared, menu_propose)
THURSDAY = 3
#: one per constraint that can bind, plus one - the same budget a single menu gets
MAX_ATTEMPTS = 4


def consented(db: Db, person_id: int) -> bool:
    from ..model.gateway import consent_granted  # noqa: PLC0415 (model imports the db layer)

    return consent_granted(db, person_id, CONSENT_KIND)


def people(db: Db) -> list[int]:
    """Everyone with a profile who is not erased, in id order."""
    return [
        int(r["id"])
        for r in db.all(
            "SELECT p.id FROM persons p WHERE p.erased_at IS NULL AND EXISTS "
            "(SELECT 1 FROM profiles f WHERE f.person_id = p.id) ORDER BY p.id"
        )
    ]


def everyone_agreed(db: Db) -> bool:
    """Two people or more, and every one of them has said yes and not since said no."""
    ids = people(db)
    return len(ids) >= 2 and all(consented(db, pid) for pid in ids)


def is_weekend(segment: menu.Segment) -> bool:
    return segment.start.weekday() == SATURDAY and segment.end == segment.start + dt.timedelta(
        days=1
    )


def weekend_of(day: dt.date) -> menu.Segment:
    """The Saturday-Sunday on or after `day` (the same weekend when `day` is in it)."""
    if day.weekday() == SUNDAY:
        start = day - dt.timedelta(days=1)
    else:
        start = day + dt.timedelta(days=(SATURDAY - day.weekday()) % 7)
    return menu.Segment(start, start + dt.timedelta(days=1))


#: the weekend switch (food/weekend.py): cooked together, or each their own
MODES = ("together", "apart")


def mode(db: Db) -> str:
    """The weekend switch as it was last left - joint, either of them may flip it - and cooked
    together until somebody says otherwise."""
    row = db.one("SELECT mode FROM weekend_modes ORDER BY id DESC LIMIT 1")
    return MODES[0] if row is None else str(row["mode"])


def possible(db: Db, segment: menu.Segment, provider: str = "claude-cli") -> bool:
    """Could THIS weekend be cooked together? Both agreed, both set up with a weight, neither
    paused - whatever the switch says. Both versions of such a weekend are built."""
    from ..clock import week_start  # noqa: PLC0415
    from ..model.gateway import consent_granted  # noqa: PLC0415
    from ..targets import latest_weight, pause_active, profile_on, targets_for  # noqa: PLC0415

    if not is_weekend(segment) or not everyone_agreed(db):
        return False
    for pid in people(db):
        # The one model call reads both plates, so both grants must cover its provider.
        if not consent_granted(db, pid, provider=provider):
            return False
        if profile_on(db, pid, segment.start) is None:
            return False
        # someone who tracks only has no menus, the weekend's included (food/planning.py)
        if planning.planning_of(profile_on(db, pid, segment.start)) == "off":
            return False
        # no targets to be had (no row for the week and no weight to make one): a shared build
        # then stopped with "both of you need a weight" in HIS chat about HER gap, and left both
        # weekends unplanned. Each weekend is then their own.
        if (
            targets_for(db, pid, week_start(segment.start)) is None
            and latest_weight(db, pid, segment.end) is None
        ):
            return False
        if any(pause_active(db, pid, d) for d in segment.days):
            return False
    return True


def active(db: Db, segment: menu.Segment) -> bool:
    """Is THIS weekend cooked together: possible, and the switch says together."""
    return mode(db) == MODES[0] and possible(db, segment)


def shared_items(
    db: Db, person_ids: Sequence[int], *, shuffle_on: dt.date | None = None
) -> list[catalogue.Ingredient]:
    """The foods every one of them may eat, in the first person's catalogue order - or, with
    `shuffle_on`, in that week's own order (`catalogue.shuffled`), the same for the prompt and
    for the check of the answer.

    Built from each person's own `approved` list, which already drops what they refuse and what
    is scoped to the other person only - so a food either of them refuses is not on the list the
    model numbers from, and cannot reach the shared menu at all.
    """
    lists = [catalogue.approved(db, pid) for pid in person_ids]
    if not lists:
        return []
    allowed = set.intersection(*({i.code for i in items} for items in lists))
    out = [i for i in lists[0] if i.code in allowed]
    if shuffle_on is not None:
        out = catalogue.shuffled(out, min(person_ids), shuffle_on)
    return out


def for_both(
    db: Db, names: Sequence[str], others: Sequence[int], on: dt.date, codes: Collection[str]
) -> list[str]:
    """One person's favourites, less any dish the shared list cannot cook, or that the other is
    predicted to dislike or has had lately: the rule the shared swap list keeps (food/swap.py),
    so a build for a day cooked together never asks the model for a dish that list would not
    offer. One swap of hers put a dish he rated 5 back on both plates every weekend. A
    liked food is held to the shared list too (`menu.cookable_terms`): his "yoghurt" was
    asked for after she refused it, by the weekend build and by a new dish for one meal."""
    from . import taste  # noqa: PLC0415 (taste reads menus through this package)

    ids = {n: db.scalar("SELECT MAX(id) FROM recipes WHERE name = ?", (n,)) for n in names}
    refused = [t for other in others for t in catalogue.refused_terms(db, other)]
    words = set(
        menu.cookable_terms(db, [n for n in names if ids[n] is None], codes, refused=refused)
    )
    keep = [
        n
        for n in names
        if (n in words if ids[n] is None else menu.uses_only(db, int(ids[n]), codes))
    ]
    for other in others:
        known = taste.tastes(db, other, {n: ids[n] for n in keep}, on)
        keep = [n for n in keep if known[n].liking >= taste.VETO and not known[n].resting]
    return keep


def union_slots(profiles: Mapping[int, sqlite3.Row | None]) -> list[str]:
    """Every slot any of them has, in the day's order - each person is solved on their own."""
    wanted = {s for p in profiles.values() for s in menu.slots_for(p)}
    return [s for s in menu.SLOT_ORDER if s in wanted]


def payload_for(payload: Mapping[str, Any], slots: Sequence[str]) -> dict[str, Any]:
    """The model's answer cut down to one person's slots (the other's pre-bed is not theirs)."""
    days = []
    for entry in payload.get("days") or []:
        if not isinstance(entry, Mapping):
            continue
        kept = [
            s
            for s in (entry.get("slots") or [])
            if isinstance(s, Mapping) and s.get("slot") in slots
        ]
        days.append({**entry, "slots": kept})
    return {**payload, "days": days}


@dataclass
class Built:
    """What a shared build produced: the pair, and each person's own menu."""

    shared_id: int
    menu_ids: dict[int, int]  # person id -> their menu
    #: how far apart the portions were when every answer was uneven and the evenest was kept -
    #: a weekend both can cook, and still less than was asked for (the run says so)
    evenest: float | None = None


ProposeFn = Callable[[str, str], tuple[Mapping[str, Any], str | None]]


def build(
    db: Db,
    segment: menu.Segment,
    persons: Sequence[Person],
    profiles: Mapping[int, sqlite3.Row | None],
    targets: Mapping[int, Macros],
    ask: ProposeFn,
    *,
    prompt_for: Callable[[str], str],
    clock: Clock = DEFAULT_CLOCK,
    note: Callable[[str], None] = lambda _s: None,
    accept: bool = True,
    shuffle: bool = False,
    gap: Collection[str] = (),
    steer: Any = None,
) -> Built:
    """Ask once for the weekend's dishes, then solve each person's plate against their targets.

    `ask(prompt, hint)` is the model call; `prompt_for(hint)` builds the prompt with the retry's
    hint appended. A dish that one person's day cannot fit sends the whole weekend back with the
    reason, the same retry loop a single menu has - the hint is the model's to read, never a
    message to either person, so nothing about whose day fell short reaches anyone.

    `accept` puts the pair in force as it is stored; without it the pair is kept, built and
    undecided, for the weekend switch to put in force (food/weekend.py).
    """
    ids = [p.id for p in persons]
    items = shared_items(db, ids, shuffle_on=segment.start if shuffle else None)
    if not items:
        raise ModelInvalid("no food both of you may eat")

    def plans_of(
        data: Mapping[str, Any],
        model_used: str | None,
        max_spread: float | None,
        held: Any = None,
    ) -> dict[int, menu.MenuPlan]:
        """Both plates of one answer. A lopsided plate still lets the other be judged, so an
        attempt's spread is the worse of the two; anything else stops at once."""
        plans: dict[int, menu.MenuPlan] = {}
        uneven: list[Lopsided] = []
        for p in persons:
            profile = profiles[p.id]
            try:
                plans[p.id] = menu.build_plan(
                    db,
                    p.id,
                    segment,
                    payload_for(data, menu.slots_for(profile)),
                    targets=targets[p.id],
                    profile=profile,
                    items=items,  # the SAME numbered list for both, so index 7 is one food
                    model_used=model_used,
                    clock=clock,
                    fibre_min=int(profile["fibre_g_min"]) if profile is not None else 25,
                    weekend=True,
                    max_spread=max_spread,
                    gap=gap,
                    steer=held,
                )
            except Lopsided as e:
                uneven.append(e)
        if uneven:
            raise max(uneven, key=lambda e: e.spread)
        return plans

    hint = ""
    last: Exception | None = None
    evenest: tuple[float, Mapping[str, Any], str | None] | None = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            data, model_used = ask(prompt_for(hint), hint)
        except ModelError:
            # a quota or a timeout after an uneven but usable answer threw that answer away and
            # left no weekend at all: the evenest one so far is still a weekend
            if evenest is None:
                raise
            note(f"shared attempt {attempt + 1}: the model did not answer")
            break
        try:
            # the weekend's packs (food/packs.py): held on every attempt but the last
            held = steer
            if steer is not None and attempt == MAX_ATTEMPTS - 1:
                held = steer.relaxed()
            plans = plans_of(data, model_used, menu.WEEKEND_SPREAD, held)
        except (Infeasible, ModelInvalid) as e:
            last = e
            if isinstance(e, Lopsided) and (evenest is None or e.spread < evenest[0]):
                evenest = (e.spread, data, model_used)
            hint = str(e)[:400]
            note(f"shared attempt {attempt + 1} refused: {hint[:200]}")
            continue
        return _store(db, segment, plans, model_used, clock, accept=accept)
    if evenest is not None:
        # every attempt fitted only by uneven portions, or not at all: the evenest of them is
        # still a weekend both of them can cook, and better than none
        spread, data, model_used = evenest
        note(f"kept the evenest attempt (portions {spread:.1f}x apart)")
        built = _store(
            db,
            segment,
            plans_of(data, model_used, None, steer.relaxed() if steer is not None else None),
            model_used,
            clock,
            accept=accept,
        )
        built.evenest = spread
        return built
    raise last if last is not None else ModelInvalid("no answer")


def _store(
    db: Db,
    segment: menu.Segment,
    plans: Mapping[int, menu.MenuPlan],
    model_used: str | None,
    clock: Clock,
    *,
    accept: bool = True,
) -> Built:
    with db.tx():
        menu_ids = {
            pid: menu.store_plan(db, plan, status="proposed", clock=clock)
            for pid, plan in plans.items()
        }
        if accept:
            for mid in menu_ids.values():
                menu.decide(db, mid, "accepted")
        shared_id = db.insert(
            "shared_menus",
            segment_start=isoday(segment.start),
            segment_end=isoday(segment.end),
            model_used=model_used,
        )
        for pid, mid in menu_ids.items():
            db.insert("shared_menu_members", shared_menu_id=shared_id, person_id=pid, menu_id=mid)
    return Built(shared_id, menu_ids)


def pair_of(db: Db, menu_id: int) -> int | None:
    row = db.one("SELECT shared_menu_id FROM shared_menu_members WHERE menu_id = ?", (menu_id,))
    return None if row is None else int(row["shared_menu_id"])


def name_of(db: Db, person_id: int) -> str:
    """The name a person was given, not persons.display_name: that column is written once at
    bootstrap (hers is "Her") and a name set later lives in person_names, newest first."""
    row = db.one(
        "SELECT (SELECT pn.name FROM person_names pn WHERE pn.person_id = p.id "
        "ORDER BY pn.id DESC LIMIT 1) AS named, p.display_name, p.slug FROM persons p "
        "WHERE p.id = ?",
        (person_id,),
    )
    if row is None:
        raise LookupError(f"no person {person_id}")
    return str(row["named"] or row["display_name"] or row["slug"])


def partner_plate(
    db: Db, viewer_id: int, menu_id: int, day: dt.date, slot: str
) -> dict[str, Any] | None:
    """The other person's plate of the same dish, for the cook - or None.

    Found through the pair the VIEWER's menu belongs to, never from anything the viewer asks
    for, and only while both of them still agree: switching it off hides it at once.
    """
    shared_id = pair_of(db, menu_id)
    if shared_id is None or not everyone_agreed(db):
        return None
    row = db.one(
        "SELECT sm.person_id, ms.portion_g, ms.portion_multiplier, ms.kcal, ms.protein_g "
        "FROM shared_menu_members sm "
        # their plates in force that day: a day they resized is a one-day menu in the same pair
        "JOIN v_menu_days_current md ON md.menu_id = sm.menu_id "
        "JOIN meal_slots ms ON ms.menu_day_id = md.id "
        "WHERE sm.shared_menu_id = ? AND sm.person_id != ? AND md.day = ? AND ms.slot = ? LIMIT 1",
        (shared_id, viewer_id, isoday(day), slot),
    )
    if row is None:
        return None
    return {
        "name": name_of(db, int(row["person_id"])),
        "portion_g": float(row["portion_g"]),
        "multiplier": float(row["portion_multiplier"]),
        "kcal": int(row["kcal"]),
        "protein_g": float(row["protein_g"]),
    }


__all__ = [
    "CONSENT_KIND",
    "CONSENT_VERSION",
    "Built",
    "active",
    "build",
    "consented",
    "everyone_agreed",
    "for_both",
    "is_weekend",
    "mode",
    "name_of",
    "pair_of",
    "partner_plate",
    "payload_for",
    "people",
    "possible",
    "shared_items",
    "union_slots",
    "weekend_of",
]
