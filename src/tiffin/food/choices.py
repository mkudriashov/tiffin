"""Offer meal choices for the coming weekdays and settle the selected dishes into menus.

The first option stands until a person chooses another. Dishes are sized in code, and
unchosen options remain available as swaps. A pending week settles from the current
picks before it starts."""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..clock import Clock, isoday, parse_day
from ..db import Db
from ..energy import Macros
from ..errors import Infeasible, ModelUnavailable, NothingToBuild
from . import catalogue, menu, packs, prefs, recipes, solver

#: each meal's share of the day's calories when four weeks of their own menus cannot say: about
#: what a day of theirs has looked like, normalised over the meals they have
DEFAULT_SHARES = {"breakfast": 0.24, "lunch": 0.32, "dinner": 0.30, "snack": 0.12, "pre_bed": 0.14}
#: how far back their own menus are read for the shares, and the fewest days that counts
SHARE_DAYS = 28
SHARE_MIN_DAYS = 5
#: at most this share of a meal's dishes in a week are ones they have had: the rest are new,
#: which is the point - a week of only what they already know is the menu they complained of
KNOWN_SHARE = 0.4
#: a dish they know is offered on at most this many days of the week, never two days running
KNOWN_REPEAT = 2
#: the most new dishes one model call is asked for (the dish_options schema's own cap)
NEW_PER_CALL = 12
#: one more call for a meal whose dishes mostly failed the checks, with the reasons
NEW_CALLS = 2
#: a dish is offered only when it can be sized within this share of its meal's calories...
KCAL_REACH = 0.15
#: ...and brings at least this share of its meal's protein
PROTEIN_REACH = 0.6
#: the fewest dishes a meal may be offered on a day; fewer and the week cannot be built
MIN_PER_SLOT = 2
#: what a person is told a week of choices takes: four model calls, one a meal
TAKES = "about 10 minutes"
#: when an unsettled week is settled from the picks so far: the evening before it starts
CLOSE_AT = "20:00"
#: the slots, in the order a day is eaten
SLOT_ORDER = ("breakfast", "lunch", "dinner", "snack", "pre_bed")
_LABEL = {
    "breakfast": "breakfast",
    "lunch": "lunch",
    "dinner": "dinner",
    "snack": "snack",
    "pre_bed": "pre-bed",
}
#: meals that can stand in for one another: lunch and dinner are both a main meal
_FAMILY = {"lunch": ("lunch", "dinner"), "dinner": ("lunch", "dinner")}
#: how near the day's calories land when a week's picks are solved: tight first
_TRIES = ((2.0, False), (float(solver.DEFAULT_KCAL_TOL_PCT), False), (5.0, True))


# ---------------------------------------------------------------- which days
def week_days(today: dt.date) -> list[dt.date]:
    """The weekdays a set built today is for: from Friday on, next Monday to Friday; earlier in
    the week, tomorrow to Friday (a person who switches choices on midweek can start at once)."""
    wd = today.weekday()
    if wd >= 4:
        monday = today + dt.timedelta(days=7 - wd)
        return [monday + dt.timedelta(days=i) for i in range(5)]
    return [today + dt.timedelta(days=i) for i in range(1, 5 - wd)]


def days_words(days: Sequence[dt.date]) -> str:
    """ "Mon 19 to Fri 23 Oct", "Fri 16 Oct": the days as a person reads them."""
    if not days:
        return ""
    first, last = min(days), max(days)
    if first == last:
        return f"{first:%a} {first.day} {first:%b}"
    head = f"{first:%a} {first.day}" + ("" if first.month == last.month else f" {first:%b}")
    return f"{head} to {last:%a} {last.day} {last:%b}"


def per_slot_of(profile: Any) -> int:
    """How many dishes each meal is offered; 0 when they do not choose (food/planning.py)."""
    from . import planning  # noqa: PLC0415 (planning reads batch, which reads targets)

    if profile is None or planning.planning_of(profile) != "choose":
        return 0
    return planning.options_of(profile)


def span_of(o: Any) -> list[str]:
    """The days an option is for: its own day, or every day of the cook it is made ahead for."""
    first = parse_day(str(o["day"]))
    last = parse_day(str(o["last_day"])) if o["last_day"] else first
    return [isoday(first + dt.timedelta(days=i)) for i in range((last - first).days + 1)]


# ---------------------------------------------------------------- shares
def shares(db: Db, person_id: int, slots: Sequence[str], today: dt.date) -> dict[str, float]:
    """Each meal's share of the day's calories, from the last four weeks of their own weekday
    menus with exactly these meals; the defaults, normalised, when there are too few such days."""
    rows = db.all(
        "SELECT md.day, ms.slot, ms.kcal FROM v_menu_days_current md "
        "JOIN meal_slots ms ON ms.menu_day_id = md.id "
        "WHERE md.person_id = ? AND md.day BETWEEN ? AND ? "
        "AND strftime('%w', md.day) NOT IN ('0', '6')",
        (person_id, isoday(today - dt.timedelta(days=SHARE_DAYS)), isoday(today)),
    )
    days: dict[str, dict[str, float]] = {}
    for r in rows:
        days.setdefault(str(r["day"]), {})[str(r["slot"])] = float(r["kcal"] or 0)
    sums = dict.fromkeys(slots, 0.0)
    counted = 0
    for meals in days.values():
        total = sum(meals.values())
        if set(meals) != set(slots) or total <= 0:
            continue
        counted += 1
        for s in slots:
            sums[s] += meals[s] / total
    raw = (
        {s: sums[s] / counted for s in slots}
        if counted >= SHARE_MIN_DAYS
        else {s: DEFAULT_SHARES.get(s, 0.2) for s in slots}
    )
    whole = sum(raw.values()) or 1.0
    return {s: round(raw[s] / whole, 4) for s in slots}


def slot_target(day: Macros, share: float) -> Macros:
    """One meal's part of the day's targets."""
    return Macros(
        round(day.kcal * share),
        round(day.protein_g * share),
        round(day.fat_g * share),
        round(day.carb_g * share),
        round(day.fibre_g * share),
    )


# ---------------------------------------------------------------- the dishes
@dataclass
class Dish:
    """A dish that could be offered for a meal: its recipe, how it is sized to the meal's share,
    and what to say about it."""

    recipe_id: int
    name: str
    new: bool
    score: float
    why: str
    multiplier: float = 1.0
    macros: Macros = field(default_factory=lambda: Macros(0, 0, 0, 0, 0))


def known_dishes(
    db: Db,
    person_id: int,
    slot: str,
    on: dt.date,
    *,
    allowed: Collection[str],
    blocked: Collection[str],
    today: dt.date,
) -> list[Dish]:
    """Weekday dishes they have had at this kind of meal, best liked first: none resting, none
    rated "never again", none holding a food they now avoid."""
    from . import swap, taste  # noqa: PLC0415 (both read menus through this package)

    family = _FAMILY.get(slot, (slot,))
    marks = ",".join("?" for _ in family)
    rows = db.all(
        "SELECT rc.name AS name, MAX(rc.id) AS rid, MAX(md.day) AS last_day "
        "FROM v_menu_days_current md JOIN meal_slots ms ON ms.menu_day_id = md.id "
        "JOIN recipes rc ON rc.id = ms.recipe_id "
        f"WHERE md.person_id = ? AND ms.slot IN ({marks}) AND md.day >= ? AND md.day < ? "
        "AND strftime('%w', md.day) NOT IN ('0', '6') GROUP BY rc.name",
        (person_id, *family, isoday(today - dt.timedelta(days=120)), isoday(today)),
    )
    blocked_names = {b.casefold() for b in blocked}
    pairs = {
        str(r["name"]): (int(r["rid"]), str(r["last_day"]))
        for r in rows
        if str(r["name"]).casefold() not in blocked_names
        and menu.uses_only(db, int(r["rid"]), allowed)
    }
    if not pairs:
        return []
    known = taste.tastes(db, person_id, {n: rid for n, (rid, _d) in pairs.items()}, on)
    scores = swap._scores(db, person_id)
    out = []
    for name, (rid, last) in pairs.items():
        t = known[name]
        if t.resting:
            continue
        out.append(
            Dish(
                rid,
                name,
                False,
                round(t.score, 4),
                taste.why(
                    t,
                    score=scores.get(name.casefold()),
                    last_day=parse_day(last),
                    on=today,
                    new=False,
                ),
            )
        )
    out.sort(key=lambda d: (-d.score, d.name))
    return out


def size(
    db: Db,
    recipe_id: int,
    slot: str,
    target: Macros,
    *,
    pin: bool,
    units: Mapping[str, Any],
) -> tuple[float, Macros] | None:
    """The portion of a dish that best meets one meal's share of the day, and its numbers - or
    None when no portion comes within reach of it. A pre-bed pot pinned at one portion stays one
    portion, as on every menu."""
    one = recipes.stored_per_serving(db, recipe_id, 1.0).as_macros()
    option = solver.SlotOption(
        slot,
        one.kcal,
        one.protein_g,
        one.fat_g,
        one.carb_g,
        one.fibre_g,
        recipe_id=recipe_id,
        units=recipes.stored_unit_parts(db, recipe_id, units),
    )
    fixed = {slot: 1.0} if pin and slot == solver.PRE_BED else None
    got: solver.DaySolution | None = None
    for tol in (5.0, 10.0, KCAL_REACH * 100):
        answer = solver.solve_day(
            [option],
            target,
            kcal_tol_pct=tol,
            fat_floor_g=0.0,
            fibre_min=0,
            fixed=fixed,
            best_effort=True,
        )
        if not isinstance(answer, Infeasible):
            got = answer
            break
    if got is None:
        if fixed is None:
            return None
        # a pinned pot is what it is: offered if it is near enough, at one portion
        got_m = 1.0
    else:
        got_m = got.multipliers[slot]
    m = round(got_m, 4)
    macros = recipes.stored_per_serving(db, recipe_id, m, units).as_macros()
    if abs(macros.kcal - target.kcal) > target.kcal * KCAL_REACH + 1:
        return None
    if macros.protein_g < target.protein_g * PROTEIN_REACH:
        return None
    return m, macros


#: the days one set of dishes is offered for: a day, or a cook of several days (first, last)
Unit = tuple[dt.date, dt.date]


def _units(days: Sequence[dt.date], cooks: Sequence[menu.Segment], made_ahead: bool) -> list[Unit]:
    """The days one meal is offered for, a set of dishes each: every day on its own, or - for a
    meal made ahead - each cook of more than one day as one, since one pot fills its boxes."""
    if not made_ahead:
        return [(d, d) for d in days]
    out: list[Unit] = []
    inside: set[dt.date] = set()
    for seg in cooks:
        span = [d for d in seg.days if d in days]
        if len(span) > 1:
            out.append((span[0], span[-1]))
            inside.update(span)
    out += [(d, d) for d in days if d not in inside]
    return sorted(out)


def _span_days(unit: Unit) -> list[dt.date]:
    first, last = unit
    return [first + dt.timedelta(days=i) for i in range((last - first).days + 1)]


def _place(
    dishes: list[Dish],
    units: Sequence[Unit],
    per_slot: int,
    *,
    on_day: dict[dt.date, set[str]],
    uses: dict[str, int],
) -> dict[Unit, list[Dish]]:
    """Each day's dishes for one meal, best first: a new dish once a week, one they know on at
    most two days and never two days running, and nothing that day's other meals already offer -
    lunch and dinner draw on the same dishes, and Tuesday's lunch and dinner were once the same
    wrap. `on_day` and `uses` are shared by every meal of the week, so a dish counts once however
    many meals it could go to. A cook of several days is one unit: its dishes are offered once
    for all of them. The first of each is the one that stands until another is picked."""
    placed: dict[Unit, list[Dish]] = {}
    yesterday: set[str] = set()
    for unit in units:
        days = _span_days(unit)
        taken: set[str] = set()
        for day in days:
            taken |= on_day.setdefault(day, set())
        today_names: set[str] = set()
        chosen: list[Dish] = []
        for repeat in (False, True):
            ranked = sorted(
                dishes, key=lambda x: (uses.get(x.name.casefold(), 0), -x.score, x.name)
            )
            for d in ranked:
                if len(chosen) >= per_slot:
                    break
                key = d.name.casefold()
                if key in today_names or key in yesterday or key in taken:
                    continue
                cap = KNOWN_REPEAT if not d.new else 1
                used = uses.get(key, 0)
                if (used >= cap and not repeat) or (repeat and used >= cap + 1):
                    continue
                chosen.append(d)
                today_names.add(key)
                uses[key] = used + 1
            if len(chosen) >= per_slot:
                break
        chosen.sort(key=lambda x: (-x.score, x.name))
        placed[unit] = chosen
        for day in days:
            on_day[day] |= today_names
        yesterday = today_names
    return placed


# ---------------------------------------------------------------- building a week
@dataclass
class Built:
    """What one build produced: the set, and why any dish was left out."""

    set_id: int | None
    days: list[dt.date]
    offered: int = 0
    refused: list[str] = field(default_factory=list)
    failed: str = ""

    def said(self) -> str:
        if self.set_id is None or self.failed:
            return self.failed or "Nothing could be built."
        return (
            f"Your choices for {days_words(self.days)} are ready: {self.offered} dishes to choose "
            "from. Pick on the page; whatever you pick, each day adds up to your targets."
        )


def _new_dishes(
    app: Any,
    db: Db,
    person_id: int,
    slot: str,
    want: int,
    *,
    items: list[catalogue.Ingredient],
    profile: Any,
    day: dt.date,
    known: Sequence[str],
    blocked: Sequence[str],
    favourites: Sequence[str],
    recent: Sequence[str],
    target: Macros,
    clock: Clock,
    mains: Sequence[str] = (),
) -> tuple[list[tuple[int, str]], list[str], str | None]:
    """New dishes for one meal from the model, checked as every menu's dishes are and stored.
    Returns (recipe id, name) pairs, why any were refused, and the model that wrote them."""
    from ..model import prompts  # noqa: PLC0415 (model imports the db layer)
    from . import newdish  # noqa: PLC0415

    gateway = app.gateway(db)
    if gateway is None:
        raise ModelUnavailable("menu_choices: no model gateway")
    kind = "small" if slot in recipes.SMALL_SLOTS else "main"
    prompt = prompts.menu_choices(
        ingredients=catalogue.prompt_lines(items),
        meal=f"{_LABEL.get(slot, slot)}, Monday to Friday",
        count=min(want, NEW_PER_CALL),
        target=f"about {target.kcal} kcal and {target.protein_g} g protein a plate",
        known=known,
        equipment=menu.equipment_of(profile),
        favourites=favourites[:8],
        blocked=blocked,
        recent=recent[:20],
        kind=kind,
        notes=prefs.standing_notes(db, person_id, weekend=False),
        mains=[
            f"{n}. {ing.name}"
            for n, ing in enumerate(items, start=1)
            if kind == "main" and ing.code in set(mains)
        ],
    )
    scope = str(db.scalar("SELECT slug FROM persons WHERE id = ?", (person_id,)) or "both")
    scope = scope if scope in ("owner", "partner") else "both"
    out: list[tuple[int, str]] = []
    refused: list[str] = []
    model_used: str | None = None
    taken = {n.casefold() for n in known}
    hint = ""
    for _ in range(NEW_CALLS):
        still = min(want - len(out), NEW_PER_CALL)
        if still <= 0:
            break
        again = f"\n\nThe previous answer failed: {hint}\nWrite {still} more, fixing exactly that."
        ask = prompt + again if hint else prompt
        result = gateway.json_task("menu_choices", ask, "dish_options", person_id=person_id)
        model_used = result.model_used
        drafts, why = newdish.check_options(
            (result.data or {}).get("options") or [],
            items,
            slot=slot,
            day=day,
            profile=profile,
            blocked=blocked,
            taken=taken,
            mains=set(mains) if kind == "main" and mains else None,
        )
        refused += why
        for draft in drafts[:still]:
            rid = recipes.store(db, draft, items, "model", model_used, scope, clock=clock)
            out.append((rid, draft.name))
            taken.add(draft.name.casefold())
        hint = "; ".join(why[:3])
        if not why:
            break
    return out, refused, model_used


def build(app: Any, db: Db, person_id: int, *, clock: Clock, days: Sequence[dt.date] = ()) -> Built:
    """Write a week of choices for `days` (the coming weekdays when not given) and store it.

    Raises NothingToBuild when there is nothing to build for (no profile, choices off, no days,
    no targets, no foods) and ModelUnavailable when there is no model to ask and too few dishes
    they know to fill the week - the caller degrades to the menu built as before."""
    from .. import targets as targets_mod  # noqa: PLC0415 (targets imports this package)
    from . import (  # noqa: PLC0415 (both read menus through this package)
        newdish,
        planning,
        taste,
    )

    today = clock.today_local()
    days = sorted(days) or week_days(today)
    days = [d for d in days if d > today and d.weekday() < 5]
    # how they plan the week being built, not today: choosing booked for next Monday is what
    # Sunday's build is for (food/planning.py)
    profile = targets_mod.profile_on(db, person_id, days[0] if days else today)
    per_slot = per_slot_of(profile)
    if profile is None or per_slot == 0:
        raise NothingToBuild("Weekly choices are off.")
    if not days:
        raise NothingToBuild("There are no weekdays ahead to choose for.")
    row = targets_mod.ensure_targets(db, person_id, days[0], app.config, clock=clock)
    if row is None:
        raise NothingToBuild("There are no targets yet: finish /onboard first.")
    day_target = targets_mod.macros_of(row)
    slots = menu.slots_for(profile)
    split = shares(db, person_id, slots, today)
    items = catalogue.approved(db, person_id)
    if app.config.get("models.shuffle_catalogue", True):
        items = catalogue.shuffled(items, person_id, days[0])
    if not items:
        raise NothingToBuild("There are no foods left to build a dish from.")
    allowed = {i.code for i in items}
    liked, blocked = menu.rated_names(db, person_id)
    favourites = taste.favourites_now(db, person_id, liked, days[0])
    recent = list(
        dict.fromkeys(
            menu.recent_names(db, person_id, days[0]) + taste.resting(db, person_id, days[0])
        )
    )
    units = catalogue.units(db)
    pin = bool(profile["pre_bed_protein"])
    # a meal they make ahead is offered once for each cook of several days (one pot, its boxes)
    cooks = menu.week_segments(db, person_id, days[0] - dt.timedelta(days=days[0].weekday()))
    made_ahead = set(planning.batched(profile))
    # what every meal of each day already offers, and how often each dish is offered in the week
    on_day: dict[dt.date, set[str]] = {}
    uses: dict[str, int] = {}
    placed: dict[str, dict[Unit, list[Dish]]] = {}
    refused: list[str] = []
    model_used: str | None = None
    # the week's main proteins: whatever is picked from the lunches and dinners cooks from those
    # packs - a kilo of chicken bought on Sunday is the week's, not Tuesday's alone
    mains = packs.week_mains(db, person_id, days, allowed=allowed)
    # her private context leaves the box only while her consent stands: without it, the week
    # is built from the dishes she knows, or not at all
    config = getattr(app, "config", None)
    scope = config.model_scope() if config is not None else "claude-cli"
    consented = newdish._consented(db, [person_id], scope)
    gateway_ok = consented and app.gateway(db) is not None
    for slot in slots:
        target = slot_target(day_target, split[slot])
        slot_units = _units(days, cooks, slot in made_ahead)
        want_total = len(slot_units) * per_slot
        known = known_dishes(
            db, person_id, slot, days[0], allowed=allowed, blocked=blocked, today=today
        )
        if mains and slot in packs.MAIN_MEALS:
            # the dishes they know on the week's packs first; others only when too few are left
            fits = {d.recipe_id: packs.recipe_mains(db, d.recipe_id) <= set(mains) for d in known}
            known = [d for d in known if fits[d.recipe_id]] + [
                d for d in known if not fits[d.recipe_id]
            ]
        # up to KNOWN_SHARE of the week's places go to dishes they know, each once where it can;
        # with no model to ask, every one they know, repeated where the week needs it
        cap_known = round(want_total * KNOWN_SHARE) if gateway_ok else want_total
        pool: list[Dish] = []
        for d in known:
            if len(pool) >= cap_known:
                break
            got = size(db, d.recipe_id, slot, target, pin=pin, units=units)
            if got is not None:
                d.multiplier, d.macros = got
                pool.append(d)

        # a dish another meal already offers on every day this one is offered is no use here: a
        # batch's lunch fills the whole week, so its dishes cannot be the week's dinner as well
        def usable(d: Dish, units: Sequence[Unit] = slot_units) -> bool:
            key = d.name.casefold()
            return any(
                all(key not in on_day.get(day, set()) for day in _span_days(u)) for u in units
            )

        need_new = max(0, want_total - sum(1 for d in pool if usable(d)))
        # ...and the model is told every dish already offered, so it writes none of them again
        offered = sorted({name for names in on_day.values() for name in names})
        if gateway_ok and need_new > 0:
            try:
                fresh, why, used = _new_dishes(
                    app,
                    db,
                    person_id,
                    slot,
                    need_new,
                    items=items,
                    profile=profile,
                    day=days[0],
                    known=[d.name for d in pool] + [d.name for d in known] + offered,
                    blocked=blocked,
                    favourites=favourites,
                    recent=recent,
                    target=target,
                    clock=clock,
                    mains=mains,
                )
            except ModelUnavailable:
                fresh, why, used = [], ["the model could not be asked"], None
            refused += [f"{_LABEL.get(slot, slot)}: {w}" for w in why]
            model_used = used or model_used
            drafted = taste.draft_tastes(
                db,
                person_id,
                days[0],
                {name: _grams_of(db, rid) for rid, name in fresh},
            )
            for rid, name in fresh:
                got = size(db, rid, slot, target, pin=pin, units=units)
                if got is None:
                    refused.append(f"{name!r} could not be sized to {_LABEL.get(slot, slot)}")
                    continue
                d = Dish(rid, name, True, round(drafted[name].score, 4), "new")
                d.multiplier, d.macros = got
                pool.append(d)
        by_day = _place(pool, slot_units, per_slot, on_day=on_day, uses=uses)
        if any(len(v) < min(MIN_PER_SLOT, per_slot) for v in by_day.values()):
            reason = (
                f"too few {_LABEL.get(slot, slot)} dishes could be written and sized "
                f"({len(pool)} for {len(days)} days)"
            )
            return _failed(db, person_id, days, per_slot, slots, split, model_used, reason, refused)
        placed[slot] = by_day
    with db.tx():
        set_id = db.insert(
            "choice_sets",
            person_id=person_id,
            week_start=isoday(days[0] - dt.timedelta(days=days[0].weekday())),
            first_day=isoday(days[0]),
            last_day=isoday(days[-1]),
            per_slot=per_slot,
            slots_json=json.dumps(list(slots)),
            shares_json=json.dumps(split),
            model_used=model_used,
            detail="; ".join(refused)[:1000] or None,
        )
        offered = 0
        for slot, by_day in placed.items():
            for (day, last), dishes in by_day.items():
                for position, d in enumerate(dishes, start=1):
                    db.insert(
                        "choice_options",
                        set_id=set_id,
                        day=isoday(day),
                        last_day=isoday(last) if last > day else None,
                        slot=slot,
                        position=position,
                        recipe_id=d.recipe_id,
                        portion_multiplier=d.multiplier,
                        kcal=d.macros.kcal,
                        protein_g=d.macros.protein_g,
                        fat_g=d.macros.fat_g,
                        carb_g=d.macros.carb_g,
                        fibre_g=d.macros.fibre_g,
                        origin="new" if d.new else "known",
                        why=d.why or None,
                    )
                    offered += 1
    return Built(set_id, list(days), offered=offered, refused=refused)


def _grams_of(db: Db, recipe_id: int) -> dict[str, float]:
    """code -> grams of one serving, what `taste.draft_tastes` predicts a new dish's liking from."""
    return {
        str(r["code"]): float(r["grams"])
        for r in db.all(
            "SELECT i.code, ri.grams FROM recipe_ingredients ri JOIN ingredients i "
            "ON i.id = ri.ingredient_id WHERE ri.recipe_id = ?",
            (recipe_id,),
        )
    }


def _failed(
    db: Db,
    person_id: int,
    days: Sequence[dt.date],
    per_slot: int,
    slots: Sequence[str],
    split: Mapping[str, float],
    model_used: str | None,
    reason: str,
    refused: Sequence[str],
) -> Built:
    """A week that could not be built, recorded as such: the menu is then built as before for its
    days, and the person is told why (degrade to manual, never to silence)."""
    said = (
        f"I could not put your choices for {days_words(days)} together: {reason}. Your menu for "
        "those days is built as before, and you can still swap any dish."
    )
    with db.tx():
        set_id = db.insert(
            "choice_sets",
            person_id=person_id,
            week_start=isoday(days[0] - dt.timedelta(days=days[0].weekday())),
            first_day=isoday(days[0]),
            last_day=isoday(days[-1]),
            per_slot=per_slot,
            slots_json=json.dumps(list(slots)),
            shares_json=json.dumps(dict(split)),
            model_used=model_used,
            detail="; ".join([reason, *refused])[:1000],
        )
        db.insert("choice_outcomes", set_id=set_id, how="failed", detail=said)
    return Built(set_id, list(days), refused=list(refused), failed=said)


# ---------------------------------------------------------------- reading a set
def outcome(db: Db, set_id: int) -> Any:
    return db.one(
        "SELECT * FROM choice_outcomes WHERE set_id = ? ORDER BY id DESC LIMIT 1", (set_id,)
    )


def latest_set(db: Db, person_id: int, today: dt.date) -> Any:
    """Their newest set whose days are not all gone, or None."""
    return db.one(
        "SELECT * FROM choice_sets WHERE person_id = ? AND last_day >= ? ORDER BY id DESC LIMIT 1",
        (person_id, isoday(today)),
    )


def covering(db: Db, person_id: int, day: dt.date) -> Any:
    """The newest set that covers `day` and did not fail, or None: a week being chosen, or
    chosen, which the ordinary menu build leaves alone (jobs/menu_propose.py)."""
    rows = db.all(
        "SELECT * FROM choice_sets WHERE person_id = ? AND first_day <= ? AND last_day >= ? "
        "ORDER BY id DESC",
        (person_id, isoday(day), isoday(day)),
    )
    for row in rows:
        done = outcome(db, int(row["id"]))
        if done is None or str(done["how"]) != "failed":
            return row
    return None


def settled_on(db: Db, person_id: int, day: dt.date) -> bool:
    """Whether `day`'s menu came from a week of choices that was set: its shopping was done for
    the week, on the Sunday (jobs/shopping_list.py leaves it alone)."""
    return (
        db.one(
            "SELECT 1 FROM choice_sets s JOIN choice_outcomes o ON o.set_id = s.id "
            "WHERE s.person_id = ? AND s.first_day <= ? AND s.last_day >= ? "
            "AND o.how IN ('chosen', 'kept', 'closed') LIMIT 1",
            (person_id, isoday(day), isoday(day)),
        )
        is not None
    )


def failed_on(db: Db, person_id: int, day: dt.date) -> bool:
    """Whether a week of choices covering `day` could not be built: its menu is built as before."""
    return (
        db.one(
            "SELECT 1 FROM choice_sets s JOIN choice_outcomes o ON o.set_id = s.id "
            "WHERE s.person_id = ? AND s.first_day <= ? AND s.last_day >= ? AND o.how = 'failed' "
            "LIMIT 1",
            (person_id, isoday(day), isoday(day)),
        )
        is not None
    )


def stands_down(
    db: Db, person_id: int, profile: Any, days: Sequence[dt.date], today: dt.date
) -> bool:
    """Whether the ordinary menu build leaves these days to the weekly choices: weekdays a week of
    choices covers (being chosen, or chosen), or - for a person who chooses - weekdays of a week
    after this one, which Sunday's build writes. Never when that week's choices failed: then the
    menu is built as before (degrade to manual, never to silence)."""
    from .. import targets as targets_mod  # noqa: PLC0415 (targets imports this package)

    if not days or any(d.weekday() >= 5 for d in days):
        return False
    if any(covering(db, person_id, d) is not None for d in days):
        return True
    # whether they choose on those days, not today: a change booked for next Monday counts there
    profile = targets_mod.profile_on(db, person_id, min(days)) or profile
    if per_slot_of(profile) == 0 or any(failed_on(db, person_id, d) for d in days):
        return False
    this_monday = today - dt.timedelta(days=today.weekday())
    return min(days) >= this_monday + dt.timedelta(days=7)


def settled_week(db: Db, person_id: int, today: dt.date) -> list[dt.date]:
    """The days of a week settled ahead of `today` (Sunday's shop for Monday to Friday), or []."""
    row = db.one(
        "SELECT s.* FROM choice_sets s JOIN choice_outcomes o ON o.set_id = s.id "
        "WHERE s.person_id = ? AND s.first_day > ? AND o.how IN ('chosen', 'kept', 'closed') "
        "ORDER BY s.id DESC LIMIT 1",
        (person_id, isoday(today)),
    )
    if row is None:
        return []
    first, last = parse_day(str(row["first_day"])), parse_day(str(row["last_day"]))
    return [first + dt.timedelta(days=i) for i in range((last - first).days + 1)]


def options_of(db: Db, set_id: int) -> list[Any]:
    return db.all(
        "SELECT o.*, rc.name AS name, rc.prep_minutes AS prep_minutes, "
        "rc.method_json AS method_json, rc.servings AS servings "
        "FROM choice_options o JOIN recipes rc ON rc.id = o.recipe_id "
        "WHERE o.set_id = ? ORDER BY o.day, o.slot, o.position",
        (set_id,),
    )


def picks_of(db: Db, set_id: int) -> dict[tuple[str, str], int]:
    """(day, slot) -> the option picked last for it."""
    out: dict[tuple[str, str], int] = {}
    for r in db.all(
        "SELECT p.option_id, o.day, o.slot FROM choice_picks p JOIN choice_options o "
        "ON o.id = p.option_id WHERE p.set_id = ? ORDER BY p.id",
        (set_id,),
    ):
        out[(str(r["day"]), str(r["slot"]))] = int(r["option_id"])
    return out


def in_force(
    options: Sequence[Any],
    picks: Mapping[tuple[str, str], int],
    avoided: Collection[int] = (),
) -> dict[tuple[str, str], Any]:
    """(day, slot) -> the option in force: the one picked, or the first. An option in `avoided`
    holds a food refused since the week was built, so it is never the one in force while another
    is free of it: the next, by position, stands instead."""
    by_key: dict[tuple[str, str], list[Any]] = {}
    for o in options:
        by_key.setdefault((str(o["day"]), str(o["slot"])), []).append(o)
    out = {}
    for key, opts in by_key.items():
        ranked = sorted(opts, key=lambda o: int(o["position"]))
        clean = [o for o in ranked if int(o["id"]) not in avoided] or ranked
        chosen = picks.get(key)
        out[key] = next((o for o in clean if int(o["id"]) == chosen), None) or clean[0]
    return out


def avoided_options(db: Db, person_id: int, options: Sequence[Any]) -> dict[int, list[str]]:
    """option id -> the foods in it this person refuses now: a refusal made after Sunday's build
    still holds for the week being chosen."""
    out: dict[int, list[str]] = {}
    seen: dict[int, list[str]] = {}
    for o in options:
        rid = int(o["recipe_id"])
        if rid not in seen:
            seen[rid] = catalogue.refused_names_in_recipe(db, person_id, rid)
        if seen[rid]:
            out[int(o["id"])] = seen[rid]
    return out


def in_force_by_day(
    options: Sequence[Any],
    picks: Mapping[tuple[str, str], int],
    avoided: Collection[int] = (),
) -> dict[tuple[str, str], Any]:
    """(day, slot) -> the option in force on that day: one made ahead for a cook counts on each
    of its days."""
    out: dict[tuple[str, str], Any] = {}
    for (_first, slot), o in in_force(options, picks, avoided).items():
        for day in span_of(o):
            out[(day, slot)] = o
    return out


# ---------------------------------------------------------------- picking
def pick(db: Db, person_id: int, set_id: int, option_id: int, *, clock: Clock) -> Any:
    """Record a pick. Raises LookupError for an option that is not theirs in that set, and
    NothingToBuild for a week already set or a day already gone."""
    row = db.one(
        "SELECT o.*, s.person_id AS owner FROM choice_options o JOIN choice_sets s "
        "ON s.id = o.set_id WHERE o.id = ? AND o.set_id = ?",
        (option_id, set_id),
    )
    if row is None or int(row["owner"]) != person_id:
        raise LookupError("That is not one of your options.")
    done = outcome(db, set_id)
    if done is not None:
        raise NothingToBuild(
            "This week is already set. Swap a dish on the menu to change it."
            if str(done["how"]) != "failed"
            else "This week could not be built, so there is nothing to pick."
        )
    if parse_day(str(row["day"])) <= clock.today_local():
        raise NothingToBuild("That day has begun, so its meals stay as they are.")
    held = catalogue.refused_names_in_recipe(db, person_id, int(row["recipe_id"]))
    if held:
        raise NothingToBuild(
            f"That dish holds {', '.join(held).lower()}, which you avoid now. Pick another."
        )
    db.insert("choice_picks", set_id=set_id, option_id=option_id)
    return row


# ---------------------------------------------------------------- settling
@dataclass
class Settled:
    """A week made menus: which, how many meals were picked, and what to say."""

    menu_ids: list[int]
    days: list[dt.date]
    chosen: int
    kept: int
    short: list[str] = field(default_factory=list)
    list_id: int | None = None

    def said(self) -> str:
        if not self.menu_ids:
            return "There was nothing left of that week to set."
        parts = [f"{days_words(self.days)} is set:"]
        if self.chosen and self.kept:
            parts.append(f"{self.chosen} meals you picked and {self.kept} first choices.")
        elif self.chosen:
            parts.append(f"the {self.chosen} meals you picked.")
        else:
            parts.append("the first choices for every meal.")
        parts += self.short
        parts.append("The shopping list is ready." if self.list_id else "")
        return " ".join(p for p in parts if p)


def _solve_day(
    db: Db,
    person_id: int,
    day: dt.date,
    chosen: Mapping[str, Any],
    *,
    config: Any,
    clock: Clock,
    boxed: Collection[str] = (),
) -> tuple[dict[str, float], dict[str, Macros], Macros, list[str]]:
    """One day of picks solved again against the day's targets, each dish kept near the size it
    was offered at; best effort when the picks cannot meet every floor - they are the person's
    own choice, and what falls short is said, never hidden. A meal in `boxed` comes out of a pot
    cooked for several days, so it stays at the size offered - every box the same - and the day's
    other meals do the moving."""
    from .. import targets as targets_mod  # noqa: PLC0415 (targets imports this package)

    row = targets_mod.ensure_targets(db, person_id, day, config, clock=clock)
    if row is None:
        raise NothingToBuild("There are no targets for that day yet.")
    target = targets_mod.macros_of(row)
    profile = targets_mod.profile_on(db, person_id, day)
    fibre_min = int(profile["fibre_g_min"]) if profile is not None else solver.DEFAULT_FIBRE_MIN
    pin = bool(profile is not None and profile["pre_bed_protein"])
    units = catalogue.units(db)
    options = []
    fixed: dict[str, float] = {}
    prefer: dict[str, float] = {}
    for slot, o in chosen.items():
        rid = int(o["recipe_id"])
        one = recipes.stored_per_serving(db, rid, 1.0).as_macros()
        options.append(
            solver.SlotOption(
                slot,
                one.kcal,
                one.protein_g,
                one.fat_g,
                one.carb_g,
                one.fibre_g,
                recipe_id=rid,
                units=recipes.stored_unit_parts(db, rid, units),
            )
        )
        if (slot == solver.PRE_BED and pin) or slot in boxed:
            fixed[slot] = float(o["portion_multiplier"])
        else:
            prefer[slot] = float(o["portion_multiplier"])
    mults: dict[str, float] | None = None
    for tol, best_effort in _TRIES:
        got = solver.solve_day(
            options,
            target,
            kcal_tol_pct=tol,
            fat_floor_g=float(target.fat_g),
            fibre_min=fibre_min,
            fixed=fixed or None,
            prefer=prefer or None,
            near=0.25,
            best_effort=best_effort,
        )
        if not isinstance(got, Infeasible):
            mults = dict(got.multipliers)
            break
    if mults is None:
        # nothing within reach of the day: the sizes they were offered at, as they are
        mults = {slot: float(o["portion_multiplier"]) for slot, o in chosen.items()}
    slot_macros = {
        s: recipes.stored_per_serving(db, int(chosen[s]["recipe_id"]), m, units).as_macros()
        for s, m in mults.items()
    }
    total = Macros(
        sum(x.kcal for x in slot_macros.values()),
        sum(x.protein_g for x in slot_macros.values()),
        sum(x.fat_g for x in slot_macros.values()),
        sum(x.carb_g for x in slot_macros.values()),
        sum(x.fibre_g for x in slot_macros.values()),
    )
    short = []
    if total.protein_g < target.protein_g:
        short.append(
            f"{day:%a} comes to {total.protein_g} g protein, under your {target.protein_g} g."
        )
    if total.fibre_g < fibre_min:
        short.append(f"{day:%a} comes to {total.fibre_g} g fibre, under your {fibre_min} g.")
    return mults, slot_macros, total, short


def settle(
    app: Any,
    db: Db,
    person_id: int,
    set_id: int,
    *,
    how: str,
    clock: Clock,
    retailers: Sequence[str] = (),
) -> Settled:
    """The picks made menus, from today on: one menu per cook of theirs (a day each, for a daily
    cook), decided at once, the dishes not picked kept on each meal's swap list. Then the
    shopping list for the week. `how` is chosen (on the page), kept (the first choices, from the
    bot) or closed (the evening before, nothing tapped)."""
    from . import shopping  # noqa: PLC0415 (shopping imports menu, which imports this package)

    if how not in ("chosen", "kept", "closed"):
        raise ValueError(how)
    row = db.one("SELECT * FROM choice_sets WHERE id = ? AND person_id = ?", (set_id, person_id))
    if row is None:
        raise LookupError("That is not one of your weeks.")
    done = outcome(db, set_id)
    if done is not None:
        if str(done["how"]) == "failed":
            raise NothingToBuild("That week could not be built, so there is nothing to set.")
        raise NothingToBuild("That week is already set.")
    today = clock.today_local()
    options = options_of(db, set_id)
    picks = picks_of(db, set_id) if how != "kept" else {}
    avoided = avoided_options(db, person_id, options)
    chosen_now = in_force(options, picks, avoided)
    by_day: dict[dt.date, dict[str, Any]] = {}
    boxed: dict[dt.date, set[str]] = {}
    for (day_iso, slot), o in in_force_by_day(options, picks, avoided).items():
        day = parse_day(day_iso)
        if day > today or (day == today and how != "closed"):
            by_day.setdefault(day, {})[slot] = o
            if o["last_day"]:
                boxed.setdefault(day, set()).add(slot)
    menu_ids: list[int] = []
    # every option of a meal holding a food refused since Sunday: kept, because a day needs the
    # meal, and said - never silently (degrade to manual)
    shorts: list[str] = [
        f"{o['name']} holds {', '.join(avoided[int(o['id'])]).lower()}, which you avoid now: "
        "swap it on the menu."
        for o in {int(o["id"]): o for o in chosen_now.values()}.values()
        if int(o["id"]) in avoided
    ]
    spare_of: dict[tuple[str, str], list[tuple[str, int]]] = {
        (str(o["day"]), str(o["slot"])): [] for o in options
    }
    for o in options:
        key = (str(o["day"]), str(o["slot"]))
        if int(o["id"]) != int(chosen_now[key]["id"]) and int(o["id"]) not in avoided:
            spare_of[key].append((str(o["slot"]), int(o["recipe_id"])))
    week_monday = parse_day(str(row["week_start"]))
    # one menu per cook, each day under the profile in force on it (menu.week_segments)
    groups = [
        [d for d in seg.days if d in by_day]
        for seg in menu.week_segments(db, person_id, week_monday)
    ]
    with db.tx():
        for group in groups:
            if not group:
                continue
            ids: dict[str, int] = {}
            mults: dict[str, float] = {}
            slot_macros: dict[str, Macros] = {}
            day_macros: dict[dt.date, Macros] = {}
            spare: list[tuple[str, int]] = []
            for day in group:
                m, sm, total, short = _solve_day(
                    db,
                    person_id,
                    day,
                    by_day[day],
                    config=app.config,
                    clock=clock,
                    boxed=boxed.get(day, set()),
                )
                shorts += short
                day_macros[day] = total
                for slot, o in by_day[day].items():
                    key = menu.slot_key(day, slot)
                    ids[key] = int(o["recipe_id"])
                    mults[key] = round(m[slot], 4)
                    slot_macros[key] = sm[slot]
                    spare += spare_of[(str(o["day"]), slot)]
            plan = menu.MenuPlan(
                person_id,
                menu.Segment(group[0], group[-1]),
                ids,
                mults,
                day_macros,
                slot_macros,
                model_used=row["model_used"],
                spare=list(dict.fromkeys(spare)),
            )
            menu_id = menu.store_plan(db, plan, status="proposed", clock=clock, own_choice=True)
            menu.decide(db, menu_id, "accepted")
            menu_ids.append(menu_id)
        db.insert(
            "choice_outcomes",
            set_id=set_id,
            how=how,
            menu_ids=json.dumps(menu_ids),
            detail=" ".join(shorts)[:1000] or None,
        )
    days = sorted(by_day)
    # a meal chosen once for a whole cook is one choice, however many days it fills
    live = {
        key: o for key, o in chosen_now.items() if any(parse_day(d) in by_day for d in span_of(o))
    }
    chosen_count = sum(1 for key, o in live.items() if picks.get(key) == int(o["id"]))
    total_meals = len(live)
    listed = None
    if menu_ids:
        listed = shopping.list_to_show(db, person_id, today, retailers=retailers, clock=clock)
    return Settled(
        menu_ids,
        days,
        chosen=chosen_count,
        kept=total_meals - chosen_count,
        short=shorts,
        list_id=int(listed["id"]) if listed is not None else None,
    )


# ---------------------------------------------------------------- what the page reads
def _ingredients(db: Db, recipe_id: int, multiplier: float, servings: int) -> list[dict[str, Any]]:
    """One plate's foods at the size offered, foods served whole counted whole."""
    out = []
    for r in db.all(
        "SELECT i.name, ri.grams, u.unit_name, u.unit_g FROM recipe_ingredients ri "
        "JOIN ingredients i ON i.id = ri.ingredient_id LEFT JOIN ingredient_units u ON u.id = "
        "(SELECT MAX(q.id) FROM ingredient_units q WHERE q.code = i.code) WHERE ri.recipe_id = ?",
        (recipe_id,),
    ):
        grams = float(r["grams"]) * multiplier / max(int(servings or 1), 1)
        units = ""
        if r["unit_g"]:
            grams = solver.whole_units(grams, float(r["unit_g"]))
            n = grams / float(r["unit_g"])
            if n >= 1 and abs(n - round(n)) < 0.01 and r["unit_name"]:
                count = round(n)
                units = f"{count} {r['unit_name']}{'' if count == 1 else 's'}"
        out.append({"name": str(r["name"]), "grams": round(grams), "units": units})
    return out


def _option_view(db: Db, o: Any) -> dict[str, Any]:
    try:
        method = json.loads(str(o["method_json"] or "[]"))
    except ValueError:
        method = []
    return {
        "id": int(o["id"]),
        "name": str(o["name"]),
        "kcal": int(o["kcal"]),
        "protein_g": round(float(o["protein_g"])),
        "fat_g": round(float(o["fat_g"])),
        "carb_g": round(float(o["carb_g"])),
        "fibre_g": round(float(o["fibre_g"])),
        "prep_minutes": int(o["prep_minutes"] or 0),
        "why": str(o["why"] or ""),
        "new": str(o["origin"]) == "new",
        "ingredients": _ingredients(
            db, int(o["recipe_id"]), float(o["portion_multiplier"]), int(o["servings"] or 1)
        ),
        "method": [str(s) for s in method],
    }


def day_view(
    db: Db,
    day_iso: str,
    slots: Sequence[str],
    options: Sequence[Any],
    picks: Mapping[tuple[str, str], int],
    target: Macros | None,
    fibre_min: int,
    *,
    only_picked: bool = False,
    avoided: Mapping[int, list[str]] | None = None,
) -> dict[str, Any]:
    """One day of a set as the page draws it: each meal's dishes, the one in force, and the day
    as currently picked against its targets. A meal made ahead for a cook of several days is
    offered on the cook's first day (`batch`: its days); on its other days it shows only what is
    in force, `from_batch` naming the day it was chosen on. An option in `avoided` (id -> the
    foods refused since the week was built) is never in force while another is free of it, and
    carries `avoided` for the page."""
    force = in_force_by_day(options, picks, avoided or {})
    out_slots = []
    for slot in sorted(slots, key=lambda s: SLOT_ORDER.index(s) if s in SLOT_ORDER else 9):
        current = force.get((day_iso, slot))
        if current is None:
            continue
        key = (str(current["day"]), slot)
        opts = [o for o in options if (str(o["day"]), str(o["slot"])) == key]
        span = span_of(current)
        later = len(span) > 1 and day_iso != span[0]
        shown = [current] if only_picked or later else opts
        entry: dict[str, Any] = {
            "slot": slot,
            "picked": int(current["id"]),
            "chosen": picks.get(key) == int(current["id"]),
            "options": [
                {**_option_view(db, o), "avoided": list((avoided or {}).get(int(o["id"]), []))}
                for o in shown
            ],
        }
        if len(span) > 1:
            entry["batch"] = {"from": span[0], "to": span[-1], "days": len(span)}
            if later:
                entry["from_batch"] = span[0]
        out_slots.append(entry)
    total = {
        "kcal": sum(int(force[(day_iso, s["slot"])]["kcal"]) for s in out_slots),
        "protein_g": round(sum(float(force[(day_iso, s["slot"])]["protein_g"]) for s in out_slots)),
        "fibre_g": round(sum(float(force[(day_iso, s["slot"])]["fibre_g"]) for s in out_slots)),
    }
    short = []
    if target is not None and total["protein_g"] < target.protein_g:
        short.append("protein")
    if total["fibre_g"] < fibre_min:
        short.append("fibre")
    return {"day": day_iso, "total": total, "short": short, "slots": out_slots}


def _stamp_words(stamp: str, timezone: dt.tzinfo) -> str:
    from ..clock import parse_stamp  # noqa: PLC0415

    at = parse_stamp(stamp).astimezone(timezone)
    return f"{at:%a} {at.day} {at:%b}, {at:%H:%M}"


def view(app: Any, db: Db, person_id: int, *, clock: Clock) -> dict[str, Any]:
    """GET /api/choices: whether weekly choices are on, the set the page shows, and what can be
    asked for. Strictly this person's."""
    from .. import building  # noqa: PLC0415
    from .. import targets as targets_mod  # noqa: PLC0415 (targets imports this package)

    today = clock.today_local()
    ahead = week_days(today)
    # whether they choose the days a build now would be for: choosing booked from tomorrow or
    # next Monday shows the screen for that week (food/planning.py)
    profile = targets_mod.profile_on(db, person_id, ahead[0] if ahead else today)
    per_slot = per_slot_of(profile)
    now = building.current(db, person_id, clock=clock)
    building_now = now is not None and now.get("kind") == building.CHOICES
    row = latest_set(db, person_id, today)
    taken = any(covering(db, person_id, d) is not None for d in ahead)
    out: dict[str, Any] = {
        "enabled": per_slot > 0,
        "per_slot": per_slot or 3,
        "building": building_now,
        "can_build": per_slot > 0
        and not building_now
        and now is None
        and not taken
        and bool(ahead),
        "build_for": days_words(ahead) if ahead and not taken else None,
        "set": None,
    }
    if row is None:
        return out
    set_id = int(row["id"])
    done = outcome(db, set_id)
    first, last = parse_day(str(row["first_day"])), parse_day(str(row["last_day"]))
    status = "open" if done is None else ("failed" if str(done["how"]) == "failed" else "chosen")
    target_row = targets_mod.ensure_targets(db, person_id, first, app.config, clock=clock)
    target = targets_mod.macros_of(target_row) if target_row is not None else None
    fibre_min = int(profile["fibre_g_min"]) if profile is not None else solver.DEFAULT_FIBRE_MIN
    options = options_of(db, set_id)
    picks = picks_of(db, set_id)
    avoided = avoided_options(db, person_id, options)
    slots = json.loads(str(row["slots_json"]))
    days = sorted({d for o in options for d in span_of(o)})
    shown: dict[str, Any] = {
        "id": set_id,
        "start": isoday(first),
        "end": isoday(last),
        "status": status,
        "deadline": None,
        "chosen_on": None,
        "picked": len(picks),
        "of": len({(str(o["day"]), str(o["slot"])) for o in options}),
        "target": None
        if target is None
        else {"kcal": target.kcal, "protein_g": target.protein_g, "fibre_g": fibre_min},
        "days": [
            day_view(
                db,
                d,
                slots,
                options,
                picks,
                target,
                fibre_min,
                only_picked=status == "chosen",
                avoided=avoided,
            )
            for d in days
        ],
    }
    if status == "open":
        eve = first - dt.timedelta(days=1)
        shown["deadline"] = f"{eve:%a} {eve.day} {eve:%b}, {CLOSE_AT}"
    elif done is not None and status == "chosen":
        shown["chosen_on"] = _stamp_words(str(done["created_at"]), db.clock.timezone)
        listed = db.one(
            "SELECT id FROM shopping_lists WHERE person_id = ? AND created_at >= ? "
            "ORDER BY id DESC LIMIT 1",
            (person_id, str(done["created_at"])),
        )
        shown["list_url"] = f"?tab=shopping&list={int(listed['id'])}" if listed else None
    elif done is not None:
        shown["said"] = str(done["detail"] or "This week could not be built.")
    out["set"] = shown
    if status in ("open", "chosen"):
        out["can_build"] = False
        out["build_for"] = None
    return out


__all__ = [
    "CLOSE_AT",
    "DEFAULT_SHARES",
    "TAKES",
    "Built",
    "Dish",
    "Settled",
    "avoided_options",
    "build",
    "covering",
    "day_view",
    "days_words",
    "failed_on",
    "in_force",
    "in_force_by_day",
    "known_dishes",
    "latest_set",
    "options_of",
    "outcome",
    "per_slot_of",
    "pick",
    "picks_of",
    "settle",
    "settled_on",
    "settled_week",
    "shares",
    "size",
    "slot_target",
    "span_of",
    "stands_down",
    "view",
    "week_days",
]
