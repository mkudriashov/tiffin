"""Build and send cook segment menus through consent-aware model routing.

Code computes macros and portions. If a proposal cannot be built, the job can repeat the last
accepted menu so the person still has a plan.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
from collections.abc import Collection, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from .. import targets
from ..clock import isoday
from ..db import Db
from ..energy import Macros
from ..errors import (
    CarbsShort,
    Infeasible,
    ModelInvalid,
    ModelUnavailable,
    TiffinError,
)
from ..food import catalogue, menu, packs, planning, prefs, recipes, weekplan
from ..model import prompts
from ..persons import Person
from ..scheduler import JobContext
from ..telegram import render as r
from ..telegram.api import button_rows

if TYPE_CHECKING:
    pass

DAYS_BEFORE = 2
# Allow separate retries for independent nutrition and pack constraints.
MAX_ATTEMPTS = 5
WEEK_EXTRA_ATTEMPTS = 2
# Minimum share of the day's carbohydrate target for a proposed menu.
CARB_FLOOR_SHARE = 0.8


def carb_floor(macros: Macros) -> float:
    """The carbohydrate a person's own day has to reach, in grams."""
    return float(round(macros.carb_g * CARB_FLOOR_SHARE))


def _targets_for(ctx: JobContext, segment: menu.Segment) -> Macros | None:
    """Calculate the current targets for a menu segment when needed."""
    row = targets.ensure_targets(
        ctx.db, ctx.person_id, segment.start, ctx.app.config, clock=ctx.clock
    ) or targets.latest_targets(ctx.db, ctx.person_id, segment.start)
    return None if row is None else targets.macros_of(row)


def dish_figures(data: Any, items: Sequence[catalogue.Ingredient], macros: Macros) -> str:
    """Summarise a proposed dish's nutrients against the day's targets."""
    from ..food import recipes  # noqa: PLC0415 (food imports the db layer)

    try:
        drafts = menu._drafts_from(data)
    except ModelInvalid, ValueError, KeyError, TypeError:
        return ""
    first = min((k.split("|", 1)[0] for k in drafts), default=None)
    if first is None:
        return ""
    parts: list[str] = []
    day = [0.0, 0.0, 0.0]
    for key in sorted(k for k in drafts if k.startswith(first)):
        draft = drafts[key]
        try:
            m = recipes.per_serving(draft, items).as_macros()
        except IndexError, KeyError, ValueError:
            return ""
        parts.append(
            f"{draft.name}: {m.kcal} kcal, {m.protein_g:.0f} g protein, {m.fat_g:.0f} g fat"
        )
        day = [day[0] + m.kcal, day[1] + m.protein_g, day[2] + m.fat_g]
    return (
        "\nYour dishes at one portion each (Tiffin scales each between 0.6 and 1.6): "
        + "; ".join(parts)
        + f". The day at one portion each: {day[0]:.0f} kcal, {day[1]:.0f} g protein, "
        f"{day[2]:.0f} g fat, against {macros.kcal} kcal with at least {macros.protein_g:.0f} g "
        f"protein and {macros.fat_g:.0f} g fat. Change the dishes so the day comes nearer."
    )


def floors_line(macros: Macros, profile: sqlite3.Row | None) -> str:
    """Describe all unmet nutrient floors in a model retry."""
    fibre = int(profile["fibre_g_min"]) if profile is not None else 25
    return (
        f"\nEvery day must still reach, all at once and within 5% of {macros.kcal} kcal: at least "
        f"{macros.protein_g:.0f} g protein, {macros.fat_g:.0f} g fat, {fibre} g fibre and "
        f"{carb_floor(macros):.0f} g carbs. Fix what failed without losing the others: lean "
        "protein in every meal, a measured fat in each, vegetables or fruit in each."
    )


#: protein has a floor and no ceiling: a first answer aimed at the floor itself came up a few grams
PROTEIN_AIM = 1.1


def meal_sizes(slots: Sequence[str], macros: Macros) -> list[str]:
    """Each meal's rough share of the day, in kcal and protein: "breakfast about 410 kcal and 43 g
    protein". The usual shares (food/choices.py DEFAULT_SHARES), never what last week's plates
    happened to be - a lopsided week would teach the next one to be lopsided."""
    from ..food.choices import DEFAULT_SHARES  # noqa: PLC0415 (choices imports this package)

    raw = {s: DEFAULT_SHARES.get(s, 0.2) for s in slots}
    total = sum(raw.values()) or 1.0
    return [
        f"{s.replace('_', '-')} about {round(macros.kcal * raw[s] / total, -1):.0f} kcal, "
        f"at least {round(macros.protein_g * PROTEIN_AIM * raw[s] / total):.0f} g protein and "
        f"{round(macros.fat_g * raw[s] / total):.0f} g fat"
        for s in slots
    ]


def _day_specs(
    segment: menu.Segment,
    slots: list[str],
    macros: Macros,
    *,
    day_targets: Mapping[dt.date, Macros] | None = None,
    plan: weekplan.WeekPlan = weekplan.HOUSE,
) -> list[dict]:
    out: list[dict] = []
    for day in segment.days:
        m = (day_targets or {}).get(day, macros)
        spec = {
            "day": isoday(day),
            "slots": ", ".join(slots),
            "kcal": m.kcal,
            "protein_g": m.protein_g,
            "fat_g": m.fat_g,
            # the model was never told the carbohydrate, and built around the protein it was
            "carb_g": m.carb_g,
        }
        also = weekplan.day_notes(plan, day, training=m is not macros)
        if also:
            spec["also"] = also
        out.append(spec)
    return out


def training_targets(
    db: Db, person_id: int, segment: menu.Segment, macros: Macros
) -> dict[dt.date, Macros]:
    """A little more food on a day they plan to train, where they asked for it (food/weekplan.py
    extra is carbohydrate: the floors stay as they are."""
    from .. import training  # noqa: PLC0415 (training reads the db layer)

    extra = weekplan.of(db, person_id).training_kcal
    if not extra:
        return {}
    return {
        day: Macros(
            macros.kcal + extra,
            macros.protein_g,
            macros.fat_g,
            macros.carb_g + round(extra / 4),
            macros.fibre_g,
        )
        for day in segment.days
        if day.weekday() < 5 and training.planned_sessions(db, person_id, day)
    }


def _bias_line(ctx: JobContext) -> str:
    """What Sunday's advisor decided the next menu should lean on.

    Two of the eleven advisor rules wrote this to `menu_bias:<person_id>` and stopped: nothing
    in the repository read that key. The system detected a repeated protein or fibre shortfall,
    resolved to act on it, and then neither acted nor said anything.
    """
    import json as _json  # noqa: PLC0415 (a one-line read, not worth a module import)

    try:
        biases = _json.loads(ctx.db.state_get(f"menu_bias:{ctx.person_id}") or "[]")
    except ValueError:
        return ""
    wanted = [str(b) for b in biases if str(b) in ("protein", "fibre")]
    if not wanted:
        return ""
    return f"lean on {' and '.join(wanted)} this segment. "


def _leftovers_for_prompt(ctx: JobContext, limit: int = 8) -> list[str]:
    return leftover_lines(ctx.db, ctx.person_id, ctx.day, limit)


def leftover_lines(db: Db, person_id: int, day: dt.date, limit: int = 8) -> list[str]:
    """What the next menu should be told to use up first: whatever goes off soonest, never a
    food the person has since refused.

    The old query read the raw `leftovers` ledger `ORDER BY id DESC LIMIT 8`, which is
    insertion order, not urgency: a week whose shop bought eight spice jars wrote eight tiny
    99 g rows AFTER the chicken and broccoli actually racing the fridge, so the last-8 cut never
    reached the two foods that mattered. This reads the same
    deduped, aged balance `/shopping` and the page already trust, orders the aisles that keep
    for days before the aisles that keep for months, and drops anything the person now avoids -
    chickpeas kept out of the ingredient list must not still be the thing the builder is told to
    use up (cook_from_fridge/12).
    """
    from ..food import shopping  # noqa: PLC0415 (jobs import the scheduler, not food, at top)

    grams = shopping.current_leftovers(db, person_id, day)
    # raw meat and fish from an earlier shop has been in the freezer since its second day open
    # (food/packs.py FRESH_OPEN_DAYS): still food to use up, and said to be frozen
    raw = {
        i.code
        for i in catalogue.latest_versions(db)
        if i.aisle in ("meat", "fish") and packs.open_days(i.name) == packs.FRESH_OPEN_DAYS
    }
    if not grams:
        return []
    terms = catalogue.refused_terms(db, person_id)
    candidates = [
        ing
        for ing in catalogue.latest_versions(db)
        if ing.code in grams and not any(catalogue.refused_matches(t, ing) for t in terms)
    ]
    candidates.sort(key=lambda ing: (shopping.keeps_days(ing.aisle), ing.name.casefold()))
    return [
        f"{ing.name} {grams[ing.code]:.0f} g" + (" (in the freezer)" if ing.code in raw else "")
        for ing in candidates[:limit]
    ]


def propose_once(
    ctx: JobContext,
    segment: menu.Segment,
    profile: sqlite3.Row | None,
    macros: Macros,
    items: list[catalogue.Ingredient],
    *,
    hint: str = "",
    note: str = "",
    carb_min: float = 0.0,
    answer: tuple[Any, str | None] | None = None,
    steer: packs.Steer | None = None,
    opened: list[str] | None = None,
    day_targets: Mapping[dt.date, Macros] | None = None,
    take_today: bool = False,
) -> int:
    """One model call plus validation and solving. Returns the stored menu id.

    `steer` holds the plan to the shop window's packs (food/packs.py); a relaxed one keeps a plan
    that opens another pack, and what it opened is added to `opened` for the message.

    `answer` builds from an answer already given - one kept for being short of carbs - with no
    model call at all. A `CarbsShort` carries the answer it was raised for.

    What the menu was weighed on is logged with the menu it was weighed for (`proposal_log`), in
    the same transaction: an answer refused, or kept and built again, logs nothing, so one menu
    is one set of rows and "chosen" is the dish that went in.
    """
    # Saturday and Sunday are treats whatever else is true of them - shared or not, a whole
    weekend = all(d.weekday() >= 5 for d in segment.days)
    data, model_used = (
        answer
        if answer is not None
        else _ask(
            ctx,
            segment,
            profile,
            macros,
            items,
            hint=hint,
            note=note,
            weekend=weekend,
            steer=steer,
            day_targets=day_targets,
        )
    )
    drawn: list[dict[str, Any]] = []
    try:
        plan = menu.build_plan(
            ctx.db,
            ctx.person_id,
            segment,
            data,
            targets=macros,
            profile=profile,
            items=items,
            model_used=model_used,
            clock=ctx.clock,
            fibre_min=int(profile["fibre_g_min"]) if profile is not None else 25,
            weekend=weekend,
            # a one-person weekend has the two-plate range too, and was held to no evenness at
            # all: the burger at half a serving that 95fd92e refuses on a shared weekend
            max_spread=menu.WEEKEND_SPREAD if weekend else menu.WEEKDAY_SPREAD,
            carb_min=carb_min,
            gap=menu.dinner_gap(ctx.db, ctx.person_id, segment.start, asked=note),
            chooser=lambda slot, options: choose(ctx, segment, items, slot, options, drawn),
            steer=steer,
            day_targets=day_targets,
        )
    except CarbsShort as e:
        e.answer = (data, model_used)
        e.dishes = dish_figures(data, items, macros)  # type: ignore[attr-defined]
        raise
    except Infeasible as e:
        # what its own dishes give, worked out in code, for the re-prompt (dish_figures)
        e.dishes = dish_figures(data, items, macros)  # type: ignore[attr-defined]
        raise
    with ctx.db.tx():
        menu_id = menu.store_plan(
            ctx.db, plan, status="proposed", clock=ctx.clock, own_choice=take_today
        )
        _log_draws(ctx, plan, drawn, menu_id)
    if opened is not None:
        opened += plan.notes
    return menu_id


def choose(
    ctx: JobContext,
    segment: menu.Segment,
    items: Sequence[catalogue.Ingredient],
    slot: str,
    options: Sequence[Any],
    drawn: list[dict[str, Any]],
) -> int:
    """Which of a slot's options goes in: one Thompson draw of each from the person's taste
    (a dish they know by its own evidence, a new one by what its foods predict), seeded by
    person, week and dish, and every draw added to `drawn` for the log. The model offers; code
    chooses."""
    from ..food import taste  # noqa: PLC0415 (food imports the db layer)

    grams = {
        o.name: {items[i.i - 1].code: float(i.g) for i in o.ingredients if 1 <= i.i <= len(items)}
        for o in options
    }
    known = taste.draft_tastes(ctx.db, ctx.person_id, segment.start, grams)
    draws = [
        known[o.name].sampled(taste.seed_for(ctx.person_id, segment.start, o.name)) for o in options
    ]
    pick = max(range(len(options)), key=lambda i: (draws[i], -i))
    drawn += [
        {
            "person_id": ctx.person_id,
            "day": isoday(segment.start),
            "slot": slot,
            "dish": o.name,
            "source": "alternative",
            "mu": round(known[o.name].liking, 4),
            "sampled": round(draws[i], 4),
        }
        for i, o in enumerate(options)
    ]
    return pick


def _log_draws(
    ctx: JobContext, plan: menu.MenuPlan, drawn: Sequence[dict[str, Any]], menu_id: int
) -> None:
    """`proposal_log` for one stored menu, `menu_id`: the favourites the prompt asked for (the
    same seeded ranking `_ask` wrote into it), and each option code weighed, chosen when it is the
    dish the menu holds - which is not always the one drawn, when the day would not plate with it.

    Every row names the menu: a weekend's own version is built and kept undecided while the
    switch says together, and is eaten only if the switch puts it in force. Without its menu, a
    row of that version read the same as a row of the menu eaten."""
    from ..food import taste  # noqa: PLC0415 (food imports the db layer)

    favourites = menu.rated_names(ctx.db, ctx.person_id)[0]
    taste.favourites_now(
        ctx.db, ctx.person_id, favourites, plan.segment.start, log=True, menu_id=menu_id
    )
    for row in drawn:
        rid = plan.recipe_ids.get(menu.slot_key(plan.segment.start, str(row["slot"])))
        held = ctx.db.scalar("SELECT name FROM recipes WHERE id = ?", (rid,))
        ctx.db.insert("proposal_log", **row, chosen=int(row["dish"] == held), menu_id=menu_id)


def example_lines(db: Db, person_id: int, on: dt.date, limit: int = 2) -> list[str]:
    """A few of their best-loved dishes written out - foods, grams, the first steps - as a
    guide to their taste for the model (few-shot): rated 4 or 5, best first, none resting."""
    from ..food import recipes, taste  # noqa: PLC0415 (food imports the db layer)

    rows = db.all(
        "SELECT rc.name AS name, MAX(rc.id) AS rid FROM ratings r "
        "JOIN recipes rc ON rc.id = r.recipe_id WHERE r.person_id = ? AND r.score >= ? "
        "AND r.id = (SELECT MAX(x.id) FROM ratings x JOIN recipes rx ON rx.id = x.recipe_id "
        "WHERE x.person_id = r.person_id AND rx.name = rc.name) GROUP BY rc.name",
        (person_id, menu.FAVOURITE_MIN),
    )
    # a dish holding a food they have since refused is no guide to their taste: it is no
    # favourite any more (menu.rated_names), and no example either
    loved = set(menu.rated_names(db, person_id)[0])
    ids = {str(r["name"]): int(r["rid"]) for r in rows if str(r["name"]) in loved}
    if not ids:
        return []
    known = taste.tastes(db, person_id, ids, on)
    best = sorted((n for n in ids if not known[n].resting), key=lambda n: (-known[n].score, n))
    out: list[str] = []
    for name in best[:limit]:
        rid = ids[name]
        foods = ", ".join(
            f"{s.ingredient.name.split(',')[0].lower()} {s.grams:.0f} g"
            for s in recipes.stored_ingredients(db, rid)
            if not recipes.is_seasoning(s.ingredient.name)
        )
        method = db.scalar("SELECT method_json FROM recipes WHERE id = ?", (rid,)) or "[]"
        try:
            steps = [str(x) for x in json.loads(str(method))][:2]
        except ValueError:
            steps = []
        out.append(f"{name} ({foods})" + (f": {' '.join(steps)}" if steps else ""))
    return out


def novelty_line(
    db: Db,
    person_id: int,
    start: dt.date,
    *,
    favourites: Collection[str] = (),
    recent: Collection[str] = (),
    slots: int = 1,
) -> str:
    """About one new dish a week: asked for when the past week brought none, held back when it
    brought one (Zandstra 2000: even weekly repetition wears; Galak 2011: spaced is welcome).

    Counted on the same kind of day as the menu: the weekend's treats are new every weekend by
    design, and are not the weekday cook's novelty. And held back only to what the prompt allows:
    last week's dishes are `recent` ("do not repeat unless a favourite"), so a model-built week
    is all new names, and from the second week on "no more than one new one" asked a person with
    no favourites yet for dishes the same prompt ruled out. Dishes they have
    had that may come back - `favourites`, or out of `recent` - for all but one of the `slots`
    are what make holding back possible.
    """
    weekend = start.weekday() >= 5
    if not menu.new_lately(db, person_id, start, weekend=weekend):
        return (
            "include one dish they have never had - a name not among their favourites or recent "
            "dishes - and keep the rest close to what they like"
        )
    asked = {n.casefold() for n in favourites}
    lately = {n.casefold() for n in recent}
    had = db.all(
        "SELECT DISTINCT rc.name FROM v_menu_days_current md "
        "JOIN meal_slots ms ON ms.menu_day_id = md.id JOIN recipes rc ON rc.id = ms.recipe_id "
        "WHERE md.person_id = ? AND md.day < ? AND (strftime('%w', md.day) IN ('0', '6')) = ?",
        (person_id, isoday(start), int(weekend)),
    )
    known = [
        str(r["name"])
        for r in had
        if str(r["name"]).casefold() in asked or str(r["name"]).casefold() not in lately
    ]
    if len(known) >= slots - 1:
        return (
            "they had a new dish this past week; keep to dishes like their favourites this time, "
            "with no more than one new one"
        )
    return (
        "most of the dishes they know were had lately, so new ones are fine this time; keep "
        "each close to what they like"
    )


def _ask(
    ctx: JobContext,
    segment: menu.Segment,
    profile: sqlite3.Row | None,
    macros: Macros,
    items: list[catalogue.Ingredient],
    *,
    hint: str,
    note: str,
    weekend: bool,
    steer: packs.Steer | None = None,
    day_targets: Mapping[dt.date, Macros] | None = None,
) -> tuple[Any, str | None]:
    """The model's answer for this segment: (the payload, which model gave it)."""
    gateway = ctx.app.gateway(ctx.db)
    plan = weekplan.HOUSE if weekend else weekplan.of(ctx.db, ctx.person_id)
    if gateway is None:
        raise ModelUnavailable("no model gateway")
    from ..food import taste  # noqa: PLC0415 (food imports the db layer)
    from ..food import weekend as weekend_mod  # noqa: PLC0415 (food imports the db layer)

    slots = menu.slots_for(profile)
    favourites, blocked = menu.rated_names(ctx.db, ctx.person_id)
    # Best first, with recently repeated dishes resting. The dishes resting join the
    # "do not repeat" list even when they were eaten more than a fortnight ago. Logged once, with
    # the menu it is stored for (`_log_draws`), not once per attempt.
    favourites = taste.favourites_now(ctx.db, ctx.person_id, favourites, segment.start)
    recent = menu.recent_names(ctx.db, ctx.person_id, segment.start)
    recent += [n for n in taste.resting(ctx.db, ctx.person_id, segment.start) if n not in recent]
    # the shop window's open packs, by their number in the list below
    open_lines, mains_line = (
        steer.prompt({ing.code: n for n, ing in enumerate(items, start=1)})
        if steer is not None
        else ([], "")
    )
    prompt = prompts.menu_propose(
        ingredients=catalogue.prompt_lines(items),
        days=_day_specs(segment, slots, macros, day_targets=day_targets, plan=plan),
        # the weekend is different each day whatever the weekday cook's variety says
        variety={s: "different" for s in slots} if weekend else menu.variety_of(profile, slots),
        favourites=favourites,
        blocked=blocked,
        recent=recent,
        leftovers=_leftovers_for_prompt(ctx),
        equipment=menu.equipment_of(profile),
        asked_for=_bias_line(ctx) + note if note else _bias_line(ctx),
        weekend=weekend,
        # treats, whereas I like more sweet treats")
        taste=weekend_mod.taste_line([weekend_mod.taste(ctx.db, ctx.person_id)]) if weekend else "",
        examples=example_lines(ctx.db, ctx.person_id, segment.start),
        dinner_gap=menu.dinner_gap(ctx.db, ctx.person_id, segment.start, asked=note),
        novelty=novelty_line(
            ctx.db,
            ctx.person_id,
            segment.start,
            favourites=favourites,
            recent=recent,
            slots=len(slots),
        ),
        notes=prefs.standing_notes(ctx.db, ctx.person_id, weekend=weekend),
        packs=open_lines,
        mains=mains_line,
        meals=meal_sizes(slots, macros),
        # made ahead or cooked on the day: the box rule says which (food/planning.py)
        batched=bool(set(planning.batched(profile)) & {"lunch", "dinner"}),
        week=[] if weekend else weekplan.week_lines(plan),
        minutes=plan.dinner_minutes or recipes.SIMPLE_MINUTES,
    )
    if hint:
        prompt += f"\n\nThe previous attempt failed: {hint}\nFix exactly that and answer again."
    result = gateway.json_task("menu_propose", prompt, "menu_proposal", person_id=ctx.person_id)
    return result.data or {}, result.model_used


SLOT_LABELS = {
    "breakfast": "Breakfast",
    "lunch": "Lunch",
    "dinner": "Dinner",
    "snack": "Snack",
    "pre_bed": "Pre-bed",
}


def slot_lines(db: Db, menu_id: int) -> tuple[list[str], list[str], dict[str, int]]:
    """(one line per meal slot, the days in order, kcal by day) for a stored menu.

    Shared by the proposal message and `/menu`, which had drifted apart in the way that matters:
    the proposal names every dish on the menu and the days each one covers, while `/menu` read
    `meals_for_day` for TODAY. On a rotating week - the default, since `variety_of` falls back to
    "rotate" - that meant Thursday's dinner was in the database, on the accepted menu, and named
    nowhere a person could reach before Thursday. Deciding on Monday to cook Thursday's dinner
    tonight had no route on the phone at all.
    """
    rows = db.all(
        "SELECT md.day, md.kcal, ms.slot, rc.name FROM menu_days md "
        "JOIN meal_slots ms ON ms.menu_day_id = md.id JOIN recipes rc ON rc.id = ms.recipe_id "
        "WHERE md.menu_id = ? ORDER BY md.day, ms.id",
        (menu_id,),
    )
    days: list[str] = []
    kcal: dict[str, int] = {}
    by_slot: dict[str, dict[str, list[str]]] = {}
    for row in rows:
        day, slot, name = str(row["day"]), str(row["slot"]), str(row["name"])
        if day not in kcal:
            days.append(day)
        kcal[day] = int(row["kcal"])
        by_slot.setdefault(slot, {}).setdefault(name, []).append(day)

    def short(day: str) -> str:
        return dt.date.fromisoformat(day).strftime("%a")

    lines: list[str] = []
    for slot, dishes in by_slot.items():
        label = SLOT_LABELS.get(slot, slot.replace("_", " ").capitalize())
        if len(dishes) == 1:
            lines.append(f"{label}: {r.esc(next(iter(dishes)))}")
        else:
            # the day-tag after a dish name is only needed to tell it apart from the OTHERS in
            # the same slot; the dish that covers the most days is the "default" one, and
            # dropping its tag - the other dish(es) still carry theirs - says exactly as much by
            # elimination while saving the one thing a real recipe name cannot spare on a phone
            # line: room for the recipe name itself (daily_messages/06, phone_reading/09).
            busiest = max(dishes.values(), key=len)
            parts = []
            for name, on in dishes.items():
                if on is busiest:
                    parts.append(r.esc(name))
                else:
                    parts.append(f"{r.esc(name)} ({', '.join(short(d) for d in on)})")
            # a rotating week can still hold more dishes in one slot than a phone line has room
            # to name even after that; join what fits and say how many more, rather than one
            # line running past what a message bubble shows at a glance (phone_reading/09)
            lines.append(r.fit_joined(f"{label}: ", parts, " · "))
    return lines, days, kcal


def message_for(
    db: Db, menu_id: int, *, repeated: bool = False, page_line: str = "", head: str = ""
) -> tuple[str, dict]:
    """One line per meal slot, not per day.

    Rendering a day at a time repeated the identical five dishes four times for anyone whose
    lines of the same food. The line count passed the ten-line check because each day was one
    very long line, which is how an unreadable message got through a readability guard.

    Grouping by slot is one line per meal whatever the variety, and the days a dish covers are
    named only when they differ.
    """
    lines, days, kcal = slot_lines(db, menu_id)

    def short(day: str) -> str:
        return dt.date.fromisoformat(day).strftime("%a")

    span = short(days[0]) if len(days) == 1 else f"{short(days[0])} to {short(days[-1])}"
    per_day = kcal[days[0]] if len(set(kcal.values())) == 1 else None
    head = head or ("<b>Menu repeated</b>" if repeated else "<b>Menu for the next cook</b>")
    sub = f"{span}, {per_day:,} kcal a day" if per_day else f"{span}, {len(days)} days"
    if page_line:
        # the dishes, the recipes and the week live on the page; the message says a menu exists
        body = "\n".join([f"{head} · {sub}", page_line])
    else:
        # no page for this person: the dish lines and the meal buttons are the only way in
        body = "\n".join([head, sub, *lines, "Tap a meal for its recipe."])
    return r.check_text(body, kind="menu_proposal"), {"menu_id": menu_id, "days": len(days)}


def change_row(menu_id: int) -> list[tuple[str, str]]:
    """The way off a menu already in force: one dish swapped for one already eaten, at once
    (food/swap.py), or a whole new menu from the model. Swap comes first - "Change" rebuilt every
    dish to replace one. Keep a small set of curated choices for a quick swap."""
    return [("Swap a dish", f"k:swappick:{menu_id}"), ("New menu", f"k:menux:{menu_id}")]


def meal_buttons(
    db: Db, menu_id: int, *, exclude: str = "", day: dt.date | None = None
) -> list[list[tuple[str, str]]]:
    """One button per meal slot, so a dish on the menu can be opened rather than just read.

    `day` travels in the payload. Without it the handler falls back to today, which is right
    for the proposal message and wrong the moment somebody asks about another day: `recipe_at`
    has taken a day since it was written and no caller could point it anywhere, so on a rotating
    week Thursday's dinner had no button anywhere that would open it.
    """
    where, args = "", (menu_id,)
    if day is not None:
        where = "AND md.day = ? "
        args = (menu_id, isoday(day))
    rows = db.all(
        "SELECT md.day, ms.slot, ms.recipe_id, ms.portion_multiplier FROM menu_days md "
        "JOIN meal_slots ms ON ms.menu_day_id = md.id "
        f"WHERE md.menu_id = ? {where}ORDER BY md.day, ms.id",
        args,
    )
    # slot -> recipe -> the first day it is on, in menu order
    dishes: dict[str, dict[int, str]] = {}
    for row in rows:
        dishes.setdefault(str(row["slot"]), {}).setdefault(int(row["recipe_id"]), str(row["day"]))
    tail = f":{day:%Y%m%d}" if day is not None else ""
    one_each = [
        (SLOT_LABELS.get(slot, slot), f"k:meal:{menu_id}:{slot}{tail}")
        for slot in dishes
        if slot != exclude
    ]
    buttons = one_each
    if day is None and any(len(by) > 1 for by in dishes.values()):
        # A proposal is sent with no day, and one button per slot opened the FIRST day's dish:
        # on a rotating week Tuesday's soup was named on the proposal ("... soup (Tue)") and no
        # tap reached it, so she could not see how much work it was before saying yes. A slot
        # holding more than one dish gets a button per dish, carrying the day it first appears.
        buttons = []
        for slot, by_recipe in dishes.items():
            if slot == exclude:
                continue
            label = SLOT_LABELS.get(slot, slot)
            if len(by_recipe) == 1:
                buttons.append((label, f"k:meal:{menu_id}:{slot}"))
                continue
            for first in by_recipe.values():
                on = dt.date.fromisoformat(first)
                buttons.append((f"{label} {on:%a}", f"k:meal:{menu_id}:{slot}:{on:%Y%m%d}"))
        if len(buttons) > MEAL_BUTTONS:
            # a different dish every day is a keyboard taller than the message; /menu has days
            buttons = one_each
    return button_rows(buttons)


#: the most meal buttons a proposal carries before it falls back to one per slot
MEAL_BUTTONS = 12


def recipe_at(
    db: Db, menu_id: int, slot: str, day: dt.date | None = None
) -> tuple[int, float] | None:
    """The recipe and portion behind one slot of a menu, for `day` when it is on the menu.

    Without the day this always answered with the FIRST day of the segment, so on a rotating
    menu the Dinner button opened Monday's dinner every day of the week - a card for food
    nobody was cooking.
    """
    if day is not None:
        row = db.one(
            "SELECT ms.recipe_id, ms.portion_multiplier FROM menu_days md "
            "JOIN meal_slots ms ON ms.menu_day_id = md.id "
            "WHERE md.menu_id = ? AND ms.slot = ? AND md.day = ? LIMIT 1",
            (menu_id, slot, isoday(day)),
        )
        if row is not None:
            return (int(row["recipe_id"]), float(row["portion_multiplier"]))
    row = db.one(
        "SELECT ms.recipe_id, ms.portion_multiplier FROM menu_days md "
        "JOIN meal_slots ms ON ms.menu_day_id = md.id "
        "WHERE md.menu_id = ? AND ms.slot = ? ORDER BY md.day LIMIT 1",
        (menu_id, slot),
    )
    return None if row is None else (int(row["recipe_id"]), float(row["portion_multiplier"]))


#: how far back a failed build still earns a retry
RETRY_LOOKBACK_DAYS = 3
#: a day with no menu at all is still worth planning before this hour - lunch and dinner are
PLAN_TODAY_BEFORE_H = 16


def segment_today(
    profile: sqlite3.Row | None, today: dt.date, *, weeks: menu.Weeks | None = None
) -> menu.Segment | None:
    """The cook segment that contains today, from today on."""
    from ..clock import week_start as monday  # noqa: PLC0415 (small helper, avoids a cycle)

    walk = weeks or menu.profile_weeks(profile)
    for base in (monday(today) - dt.timedelta(days=7), monday(today)):
        for seg in walk(base):
            if seg.start <= today <= seg.end:
                return menu.Segment(today, seg.end)
    return None


def empty_run(db: Db, person_id: int, segment: menu.Segment | None) -> menu.Segment | None:
    """The days at the front of `segment` that no menu covers, or None when the first one is."""
    if segment is None:
        return None
    gap: list[dt.date] = []
    for day in segment.days:
        if menu.current_menu(db, person_id, day) is not None:
            break
        gap.append(day)
    return menu.Segment(gap[0], gap[-1]) if gap else None


def today_gap(
    db: Db, person_id: int, profile: sqlite3.Row | None, now_local: dt.datetime
) -> menu.Segment | None:
    """Today onwards, when today has no menu at all and there is still a day to plan."""
    today = now_local.date()
    if now_local.hour >= PLAN_TODAY_BEFORE_H:
        return None
    if menu.current_menu(db, person_id, today) is not None:
        return None
    return empty_run(
        db, person_id, segment_today(profile, today, weeks=menu.weeks_of(db, person_id))
    )


def cooks_each_day(db: Db, person_id: int, day: dt.date) -> bool:
    """Is `day` a weekday they cook on the day, with menus planned for them?"""
    if day.weekday() >= 5:
        return False
    profile = targets.profile_on(db, person_id, day)
    return planning.planning_of(profile) == "auto" and not planning.cooked_ahead(profile, day)


def week_run(
    db: Db, person_id: int, segment: menu.Segment | None, *, replace: bool = False
) -> menu.Segment | None:
    """For someone who cooks each weekday on its own and has menus planned for them: the week
    from `segment`'s day to Friday, the days no menu covers yet. One build plans them all, so a
    pack bought for the week is planned across it, not opened for Monday alone and left (the owner,
    cooked on the day; however many shops buy the week (food/weekplan.py), it is one build.
    Anything else is `segment` as it was."""
    if segment is None or len(segment.days) != 1 or segment.start.weekday() >= 5:
        return segment
    run: list[dt.date] = []
    day = segment.start
    while day.weekday() < 5:
        if not cooks_each_day(db, person_id, day):
            break  # a change booked from that day plans it another way
        run.append(day)
        day += dt.timedelta(days=1)
    if len(run) < 2:
        return segment
    if replace:
        # a rebuild asked for: the days to Friday, whatever menu they have now
        return menu.Segment(run[0], run[-1])
    return empty_run(db, person_id, menu.Segment(run[0], run[-1])) or segment


def plan_date(monday: dt.date, plan_day: int) -> dt.date:
    """The day before `monday` (or the Monday's own week) its week is planned on: Sunday is the
    day before, Saturday two days before."""
    back = (-plan_day) % 7 or 7
    return monday - dt.timedelta(days=back)


def planned_ahead(
    db: Db, person_id: int, today: dt.date, ahead: menu.Segment | None
) -> tuple[menu.Segment | None, str]:
    """The segment this run plans ahead, for someone whose week is planned on a day of their
    on that day, next Monday (and so its whole week, `week_run`); before it, nothing - the week
    is not built early, a day at a time; after it, `ahead` as it was, which is how a week whose
    planning day failed is still built. Everyone else: `ahead` unchanged. And a note, or ""."""
    from ..clock import week_start  # noqa: PLC0415

    plan = weekplan.of(db, person_id)
    if plan.plan_day is None:
        return ahead, ""
    monday = week_start(today) + dt.timedelta(days=7)
    if today == plan_date(monday, plan.plan_day) and cooks_each_day(db, person_id, monday):
        return menu.Segment(monday, monday), ""
    if ahead is None or ahead.start.weekday() >= 5:
        return ahead, ""
    week = week_start(ahead.start)
    if today < plan_date(week, plan.plan_day) and cooks_each_day(db, person_id, ahead.start):
        return None, f"{isoday(ahead.start)} is planned with its week on {DAY_NAMES[plan.plan_day]}"
    return ahead, ""


DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def lunch_follows(ctx: JobContext, segment: menu.Segment, menu_id: int) -> None:
    """After a build in force, for someone whose lunch is the day before's dinner: the day after
    the build, when it is planned already, has its lunch put to the new dinner - the dish she
    actually cooked last night (food/weekplan.py `follows`). Never fatal: a lunch that will not
    plate beside that day is left and noted."""
    from ..food import swap  # noqa: PLC0415 (swap reads menus through the food layer)

    plan = weekplan.of(ctx.db, ctx.person_id)
    after = segment.end + dt.timedelta(days=1)
    if not weekplan.follows(plan, after) or menu.current_menu(ctx.db, ctx.person_id, after) is None:
        return
    row = ctx.db.one(
        "SELECT ms.recipe_id, rc.name FROM menu_days md "
        "JOIN meal_slots ms ON ms.menu_day_id = md.id JOIN recipes rc ON rc.id = ms.recipe_id "
        "WHERE md.menu_id = ? AND md.day = ? AND ms.slot = 'dinner'",
        (menu_id, isoday(segment.end)),
    )
    if row is None:
        return
    try:
        swap.put(
            ctx.db,
            ctx.person_id,
            after,
            "lunch",
            int(row["recipe_id"]),
            str(row["name"]),
            every_day=False,
            config=ctx.app.config,
            clock=ctx.clock,
        )
        ctx.handle.note(f"{isoday(after)} lunch follows the new dinner")
    except (ValueError, TiffinError, Infeasible) as e:
        ctx.handle.note(f"{isoday(after)} lunch left as it was: {str(e)[:120]}")


def first_shop_line(ctx: JobContext, person: Person, segment: menu.Segment) -> str:
    """For someone whose week is bought in shops of their own, built before it starts: the first
    shop's list, built now and said with the menu - she shops the day the week is planned
    "" for anyone else."""
    from ..food import shopping  # noqa: PLC0415 (food imports the db layer)
    from . import shop_refresh  # noqa: PLC0415

    if not weekplan.of(ctx.db, person.id).shops or segment.start <= ctx.day:
        return ""
    # the first shop's days from the build's own first day: a Wednesday built on Monday is
    # bought on its own, never Monday's and Tuesday's food again
    window = packs.window_of(ctx.db, person.id, segment.start)
    days = [d for d in window if segment.start <= d <= segment.end]
    made = shopping.range_list(
        ctx.db,
        person.id,
        ctx.day,
        retailers=shop_refresh.retailer_order(ctx.app.config),
        clock=ctx.clock,
        days=days,
    )
    if made is None:
        return ""
    items = shopping.read_back(ctx.db, int(made["id"]))
    said = f"Shopping for {packs.days_words(days)}: {r.plural(len(items), 'item')}"
    link = (
        r.shopping_link(ctx.app.config, label="the list", list_id=int(made["id"]))
        if person.tailscale_login
        else ""
    )
    later = window[-1] < segment.end
    return (
        r.esc(said)
        + (f", {link} on the page." if link and "href" in link else ". /shopping shows it.")
        + (" The rest of the week has a top-up list of its own." if later else "")
    )


def _overlaps(a: menu.Segment, b: menu.Segment) -> bool:
    return a.start <= b.end and b.start <= a.end


def _failed_lately(ctx: JobContext) -> bool:
    """Did a menu build for this person fail or fall short in the last few days?"""
    since = (ctx.clock.now_utc() - dt.timedelta(days=RETRY_LOOKBACK_DAYS)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    return (
        ctx.db.one(
            "SELECT 1 FROM run_ledger WHERE job = 'menu_propose' AND person_id = ? "
            "AND status IN ('degraded', 'failed') AND started_at >= ? LIMIT 1",
            (ctx.person_id, since),
        )
        is not None
    )


def _uncovered(ctx: JobContext, segment: menu.Segment | None) -> bool:
    return segment is not None and not any(
        menu.current_menu(ctx.db, ctx.person_id, d) is not None for d in segment.days
    )


def run(ctx: JobContext) -> None:
    """Plan today when it has nothing, the segment starting in two days, and any retry.

    Today first (before PLAN_TODAY_BEFORE_H): a day with no menu at all was left empty by every
    path, because all of them started from tomorrow - Saturday 26 Sep had nothing all day.

    The retry is the half that was missing. On Thu 24 Sep both people's weekend builds failed
    (a budget cap, then a timed-out fallback; a fibre floor, then no model), and the next day's
    run only ever looked two days ahead, so the weekend was never planned and the joint shopping
    never re-dispatched, so the retry has to live in the next slot's own question: is there a
    segment, from tomorrow, with no menu on any of its days?
    """
    person = ctx.person
    if person is None or ctx.profile is None:
        ctx.handle.skip("no profile")
        return
    from .. import building  # noqa: PLC0415
    from ..food import choices, shared  # noqa: PLC0415 (food imports the db layer)
    from ..food import weekend as weekend_mod  # noqa: PLC0415

    asked = building.current(ctx.db, person.id, clock=ctx.clock)
    if asked is not None and asked["kind"] == building.MENU and asked["asked"]:
        # a menu the person asked for is building on their build lane: two builds for the same
        # days each send a menu, and whichever ends last is in force - a plain one here could
        ctx.handle.skip("a menu asked for is being built")
        return
    wanted: list[tuple[menu.Segment, bool, bool]] = []  # segment, is a retry, in force
    # today first: a day with nothing planned is the most urgent gap there is, and every other
    # path starts from tomorrow (it was how Saturday 26 Sep stayed empty all day)
    gap = today_gap(ctx.db, ctx.person_id, ctx.profile, ctx.now_local)
    if gap is not None and not shared.active(ctx.db, shared.weekend_of(gap.start)):
        wanted.append((gap, True, True))
    weeks = menu.weeks_of(ctx.db, ctx.person_id)
    days_before = int(ctx.app.config.get("schedule.menu_propose_days_before", DAYS_BEFORE))
    ahead = menu.next_segment(ctx.profile, ctx.day, days_before=days_before, weeks=weeks)
    if ahead is not None and choices.stands_down(
        ctx.db, ctx.person_id, ctx.profile, ahead.days, ctx.day
    ):
        # a person who chooses their weekday meals (food/choices.py): those days come from the
        # week of choices, or from Sunday's build of it - a menu built here two days ahead would
        # be one more to replace, and its message one more to ignore
        ctx.handle.note(f"{isoday(ahead.start)} comes from the weekly choices")
        ahead = None
    if ahead is not None and shared.possible(ctx.db, ahead, ctx.app.config.model_scope()):
        # a weekend that could be cooked together is built both ways (the switch): menu_shared
        # builds it together, and this person's own version is built here - in force when the
        # switch says each your own, kept for it when it says together
        if ctx.person_id not in weekend_mod.versions(ctx.db, ahead).apart:
            in_force = shared.mode(ctx.db) == weekend_mod.APART
            wanted.append((ahead, False, in_force))
        ahead = None
    # someone who cooks each day: the week is planned in one build - on the day they plan it
    # where they said one (food/weekplan.py), else two days ahead of Monday
    ahead, said = planned_ahead(ctx.db, ctx.person_id, ctx.day, ahead)
    if said:
        ctx.handle.note(said)
    ahead = week_run(ctx.db, ctx.person_id, ahead)
    if (
        ahead is not None
        and _uncovered(ctx, ahead)
        and not any(_overlaps(ahead, w) for w, _r, _f in wanted)
    ):
        wanted.append((ahead, False, True))
    if _failed_lately(ctx):
        retry = segment_on_demand(ctx.profile, ctx.day, covered=None, weeks=weeks)
        retry = week_run(ctx.db, ctx.person_id, retry)
        if (
            retry is not None
            and not choices.stands_down(ctx.db, ctx.person_id, ctx.profile, retry.days, ctx.day)
            and not shared.active(ctx.db, shared.weekend_of(retry.start))
            and retry.start > ctx.day
            and _uncovered(ctx, retry)
            and not any(_overlaps(retry, w) for w, _r, _f in wanted)
        ):
            wanted.append((retry, True, True))
    if not wanted:
        ctx.handle.skip("no cook segment starting in two days, and nothing to retry")
        return
    for segment, is_retry, in_force in wanted:
        before = (ctx.handle.status, ctx.handle.detail)
        # marked while it runs, so the page does not start a build of its own beside it - a
        # mark of its own, which new dishes being written for one meal cannot stand in for. A
        # version kept for the weekend switch replaces nothing
        token = (
            building.mark(
                ctx.db,
                person.id,
                f"the menu from {segment.start:%a %d %b}",
                clock=ctx.clock,
                asked=False,
            )
            if in_force
            else None
        )
        try:
            said = _propose_for(ctx, segment, in_force=in_force)
        finally:
            if token is not None:
                building.done(ctx.db, person.id, token)
        if is_retry and ctx.handle.status == "skipped":
            # a retry with nothing to do (no targets for those days yet) must not turn a run
            # that just built the scheduled menu into "skipped", and says nothing to anyone
            ctx.handle.status, ctx.handle.detail = before
            ctx.handle.note(f"retry for {isoday(segment.start)} skipped")
            continue
        if said:
            # the scheduled job discarded this, so a Saturday that could not build a menu told
            # the person nothing at all - the admin alert was the only thing sent, and for her
            # it went to somebody else's phone. Degrade to manual, never to silence: whoever
            # the menu was for hears that there is no menu and why. One key per segment start,
            # so a retry that fails again does not repeat the same message.
            ctx.outbox.send(person, "menu_failed", isoday(segment.start), said)


#: the macro that fell short -> how to rank the catalogue for it, and what to tell the model.
#: `fat` was missing entirely, so a day that could not reach its fat floor got the bare
#: solver error three times over and failed the same way each time - the model kept reaching
#: for the leanest protein it had, which is exactly what makes fat unreachable. The tail
#: matters as much as the ranking: "use richer foods only for what is left over" is right for
#: protein and fibre and precisely backwards for fat.
_SHORTFALLS: dict = {
    "protein": (
        lambda i: i.protein_100,
        "protein per calorie",
        "Build the meals around those and use richer foods only for what is left over.",
    ),
    "fibre": (
        lambda i: i.fibre_100 or 0.0,
        "fibre per calorie",
        "Build the meals around those and use richer foods only for what is left over.",
    ),
    "fat": (
        lambda i: i.fat_100,
        "fat per calorie",
        "Work these in deliberately: a day built on white fish and chicken breast cannot "
        "reach the fat floor however the portions are scaled.",
    ),
    "carbs": (
        lambda i: i.carb_100,
        "carbohydrate per calorie",
        "Put a proper portion of one of those on every main plate: a day built on meat, eggs "
        "and dairy meets its calories with protein and leaves the carbohydrate short.",
    ),
}


_SHORTFALL_LINE = re.compile(
    r"\b(protein|fat|fibre|carbs|kcal)\s*:\s*max reachable\s*([\d.]+)\s*g?\s*<\s*"
    r"(?:floor\s*)?([\d.]+)",
    re.IGNORECASE,
)


from .menu_on_demand import (  # noqa: E402
    build_own_weekend,
    propose_now,
    rebuild_for_page,
    repeat_now,
    segment_from,
    segment_on_demand,
)
from .menu_rebuild import (  # noqa: E402
    JOB,
    _propose_for,
    leanest_for,
    note_refusal,
    note_wishes,
    refusal_for_page,
    refusal_said,
    refuse_from_note,
    remember,
    repeat_less_refused,
    why_not,
    why_not_simple,
    wishes_said,
)

__all__ = [
    "CARB_FLOOR_SHARE",
    "DAYS_BEFORE",
    "DAY_NAMES",
    "JOB",
    "MAX_ATTEMPTS",
    "MEAL_BUTTONS",
    "PLAN_TODAY_BEFORE_H",
    "PROTEIN_AIM",
    "RETRY_LOOKBACK_DAYS",
    "SLOT_LABELS",
    "WEEK_EXTRA_ATTEMPTS",
    "build_own_weekend",
    "carb_floor",
    "change_row",
    "choose",
    "cooks_each_day",
    "dish_figures",
    "empty_run",
    "example_lines",
    "first_shop_line",
    "floors_line",
    "leanest_for",
    "leftover_lines",
    "lunch_follows",
    "meal_buttons",
    "meal_sizes",
    "message_for",
    "note_refusal",
    "note_wishes",
    "novelty_line",
    "plan_date",
    "planned_ahead",
    "propose_now",
    "propose_once",
    "rebuild_for_page",
    "recipe_at",
    "refusal_for_page",
    "refusal_said",
    "refuse_from_note",
    "remember",
    "repeat_less_refused",
    "repeat_now",
    "run",
    "segment_from",
    "segment_on_demand",
    "segment_today",
    "slot_lines",
    "today_gap",
    "training_targets",
    "week_run",
    "why_not",
    "why_not_simple",
    "wishes_said",
]
