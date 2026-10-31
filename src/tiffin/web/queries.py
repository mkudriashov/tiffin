"""Every read the page makes, each scoped to one person.

House rule, enforced by a test that greps this module: every SQL string carries `person_id = ?`
and every function takes `person_id` as its first argument after the connection. Nothing here
takes a person from the request; the caller passes `request.state.person.id`.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Mapping, Sequence
from typing import Any

from ..clock import isoday, parse_day, week_start
from ..db import Db

TREND_METRICS = (
    "weight",
    "intake",
    "steps",
    "sessions",
    "waist",
    "hip",
    "chest",
    "neck",
    "overarm",
    "shoulder",
    "sleeve",
)
SLOT_ORDER = {"breakfast": 0, "lunch": 1, "dinner": 2, "snack": 3, "pre_bed": 4}
#: what the numbers up the side of each chart are - a chart screenshotted to a coach is useless
#: if 99.4 could be kg or lb, so every trend payload names its own axis rather than leaving the
#: unit to memory
TREND_UNITS = {
    "weight": "kg",
    "intake": "kcal",
    "steps": "steps",
    "sessions": "sessions",
    "waist": "cm",
    "hip": "cm",
    "chest": "cm",
    "neck": "cm",
    "overarm": "cm",
    "shoulder": "cm",
    "sleeve": "cm",
}
#: waist and hip are taken in the same breath (one /measure message, two numbers) - the natural
#: partner to show alongside either one, unless a caller names a different `compare=`
TAPE_COMPARE_DEFAULT = {"waist": "hip", "hip": "waist"}
#: below this window a daily bar is still a bar you can point at; above it, a year of pixel-wide
#: bars answers nothing on its own and needs the mean/gap stated in words instead
WIDE_WINDOW_DAYS = 180


def _f(value: Any) -> float | None:
    return None if value is None else float(value)


def targets(db: Db, person_id: int, day: dt.date) -> dict[str, Any] | None:
    row = db.one(
        "SELECT week_start, kcal, protein_g, fat_g, carb_g, fibre_g, source FROM v_targets_latest "
        "WHERE person_id = ? AND week_start <= ? ORDER BY week_start DESC LIMIT 1",
        (person_id, isoday(day)),
    )
    return None if row is None else dict(row)


def day_record(db: Db, person_id: int, day: dt.date) -> dict[str, Any] | None:
    row = db.one(
        "SELECT day, status, basis, kcal, protein_g, fat_g, carb_g, fibre_g, note "
        "FROM v_day_latest WHERE person_id = ? AND day = ?",
        (person_id, isoday(day)),
    )
    return None if row is None else dict(row)


def estimates(db: Db, person_id: int, day: dt.date) -> list[dict[str, Any]]:
    rows = db.all(
        "SELECT description, kcal, protein_g, fat_g, carb_g, fibre_g FROM estimates "
        "WHERE person_id = ? AND day = ? AND status = 'accepted' ORDER BY id",
        (person_id, isoday(day)),
    )
    return [dict(r) for r in rows]


def trend_point(db: Db, person_id: int, day: dt.date) -> dict[str, Any] | None:
    row = db.one(
        "SELECT day, trend_kg, n FROM v_trend_latest WHERE person_id = ? AND day <= ? "
        "ORDER BY day DESC LIMIT 1",
        (person_id, isoday(day)),
    )
    return None if row is None else dict(row)


def weigh_in(db: Db, person_id: int, day: dt.date) -> dict[str, Any] | None:
    row = db.one(
        "SELECT day, weight_kg, source FROM v_weigh_in_latest WHERE person_id = ? AND day <= ? "
        "ORDER BY day DESC LIMIT 1",
        (person_id, isoday(day)),
    )
    return None if row is None else dict(row)


def wellness(db: Db, person_id: int, day: dt.date) -> dict[str, Any] | None:
    """The day's watch numbers, with when they last arrived.

    Steps of 0 are a watch on charge, not a day spent still - `week()` and `trend()` already read
    them as "not counted", and this did not, so Today said "9,000 short" on a day nobody had
    counted. `synced_at` is what the old 16:00 nudge never knew: 274 steps
    at 16:00 was a sync from the morning, and the card now says how old its number is.
    """
    row = db.one(
        "SELECT w.day, CASE WHEN w.steps > 0 THEN w.steps END AS steps, w.sleep_s, w.hrv, "
        "w.resting_hr, w.ctl, w.atl, w.tsb, r.fetched_at AS synced_at FROM icu_wellness w "
        "LEFT JOIN icu_raw r ON r.id = w.icu_raw_id WHERE w.person_id = ? AND w.day = ?",
        (person_id, isoday(day)),
    )
    return None if row is None else dict(row)


def activity_last(db: Db, person_id: int, day: dt.date) -> str | None:
    """The last day up to `day` the watch sent any training, or None: a watch nobody wears says
    nothing about whether they trained."""
    last = db.scalar(
        "SELECT MAX(day) FROM icu_activity WHERE person_id = ? AND day <= ?",
        (person_id, isoday(day)),
    )
    return str(last) if last else None


def steps_last(db: Db, person_id: int, day: dt.date) -> str | None:
    """The last day up to `day` the watch counted any steps. Today's card said "no steps from the
    watch yet today" for fifteen days running while the watch sent nothing at all:
    "yet" is only true for a watch that counted yesterday."""
    last = db.scalar(
        "SELECT MAX(day) FROM icu_wellness WHERE person_id = ? AND day <= ? AND steps > 0",
        (person_id, isoday(day)),
    )
    return str(last) if last else None


def profile(db: Db, person_id: int, day: dt.date) -> dict[str, Any] | None:
    row = db.one(
        "SELECT steps_floor, morning_time, confirm_time, goal_weight_kg, goal_date, "
        "rate_pct_per_week, kcal_floor, rhythm FROM profiles WHERE person_id = ? "
        "AND effective_from <= ? "
        "ORDER BY effective_from DESC, id DESC LIMIT 1",
        (person_id, isoday(day)),
    )
    return None if row is None else dict(row)


def planned_session(db: Db, person_id: int, day: dt.date) -> dict[str, Any] | None:
    """The first planned session, kept for the shape the page expects."""
    rows = planned_sessions(db, person_id, day)
    return rows[0] if rows else None


def planned_sessions(db: Db, person_id: int, day: dt.date) -> list[dict[str, Any]]:
    """All of them: a day can hold two, and LIMIT 1 named whichever was typed first."""
    return [
        dict(r)
        for r in db.all(
            "SELECT ps.kind, ps.target_minutes, ps.start_time FROM planned_sessions ps "
            "WHERE ps.person_id = ? AND ps.weekday = ? AND ps.plan_id = ("
            "  SELECT id FROM training_plans WHERE person_id = ? AND effective_from <= ? "
            "  ORDER BY effective_from DESC, id DESC LIMIT 1) ORDER BY ps.id",
            (person_id, day.weekday(), person_id, isoday(day)),
        )
    ]


def done_sessions(db: Db, person_id: int, day: dt.date) -> list[dict[str, Any]]:
    rows = db.all(
        "SELECT sm.status, sm.minutes, sm.icu_id, ia.kind, ia.name FROM session_matches sm "
        "LEFT JOIN icu_activity ia ON ia.icu_id = sm.icu_id AND ia.person_id = sm.person_id "
        "WHERE sm.person_id = ? AND sm.day = ? "
        "AND sm.status IN ('matched', 'confirmed', 'manual', 'extra') "
        "ORDER BY sm.id",
        (person_id, isoday(day)),
    )
    return [dict(r) for r in rows]


def _recipe_extras(db: Db, person_id: int, recipe_id: int, method_json: Any) -> dict[str, Any]:
    """Return the method and ingredients for a recipe shown on the page."""
    try:
        method = json.loads(str(method_json or "[]"))
    except ValueError:
        method = []
    ingredients = [
        dict(r)
        for r in db.all(
            # Pack, unit and nutrition values keep the card consistent with menu calculations.
            "SELECT i.name, i.code, ri.grams, i.pack_label, i.pack_g, i.kcal_100, "
            "i.protein_100, i.fat_100, i.carb_100, i.fibre_100, u.unit_name, u.unit_g "
            "FROM recipe_ingredients ri JOIN ingredients i ON i.id = ri.ingredient_id "
            "LEFT JOIN ingredient_units u ON u.id = "
            "(SELECT MAX(q.id) FROM ingredient_units q WHERE q.code = i.code) "
            "WHERE ri.recipe_id = ? AND ? = ?",
            (recipe_id, person_id, person_id),
        )
    ]
    return {"method": method, "ingredients": ingredients}


def meals(db: Db, person_id: int, day: dt.date) -> list[dict[str, Any]]:
    """Return the day's meals with their methods, ingredients and latest ratings."""
    rows = db.all(
        "SELECT ms.slot, ms.recipe_id, rc.name, ms.portion_g, ms.portion_multiplier, ms.kcal, "
        "ms.protein_g, ms.fat_g, ms.carb_g, ms.fibre_g, rc.prep_minutes, rc.method_json, "
        # the menu day in force: a day's plates resized (menu_emphasis) or a part-week rebuild
        # leaves the earlier menu current for its other days, and both covered this one
        "rc.servings, rc.keep_days, rc.freezable FROM v_menu_days_current md "
        "JOIN meal_slots ms ON ms.menu_day_id = md.id JOIN recipes rc ON rc.id = ms.recipe_id "
        "WHERE md.person_id = ? AND md.day = ?",
        (person_id, isoday(day)),
    )
    if not rows:
        return []
    ingredients: dict[int, list[dict[str, Any]]] = {}
    for ingredient in db.all(
        "SELECT DISTINCT ri.id, ri.recipe_id, i.name, i.code, ri.grams, i.pack_label, "
        "i.pack_g, i.kcal_100, i.protein_100, i.fat_100, i.carb_100, i.fibre_100, "
        "u.unit_name, u.unit_g FROM v_menu_days_current md "
        "JOIN meal_slots ms ON ms.menu_day_id = md.id "
        "JOIN recipe_ingredients ri ON ri.recipe_id = ms.recipe_id "
        "JOIN ingredients i ON i.id = ri.ingredient_id "
        "LEFT JOIN ingredient_units u ON u.id = "
        "(SELECT MAX(q.id) FROM ingredient_units q WHERE q.code = i.code) "
        "WHERE md.person_id = ? AND md.day = ? ORDER BY ri.id",
        (person_id, isoday(day)),
    ):
        recipe_id = int(ingredient["recipe_id"])
        item = dict(ingredient)
        item.pop("id")
        item.pop("recipe_id")
        ingredients.setdefault(recipe_id, []).append(item)
    names = tuple({str(row["name"]) for row in rows})
    ratings: dict[str, int] = {}
    for rating in db.all(
        "SELECT rc.name, r.score FROM ratings r "
        "JOIN recipes rc ON rc.id = r.recipe_id "
        f"WHERE r.person_id = ? AND rc.name IN ({','.join('?' for _ in names)}) "
        "ORDER BY r.id DESC",
        (person_id, *names),
    ):
        ratings.setdefault(str(rating["name"]), int(rating["score"]))
    out = []
    for r in rows:
        entry = dict(r)
        entry.pop("method_json", None)
        try:
            method = json.loads(str(r["method_json"] or "[]"))
        except ValueError:
            method = []
        entry.update({"method": method, "ingredients": ingredients.get(int(r["recipe_id"]), [])})
        entry["score"] = ratings.get(str(r["name"]))
        out.append(entry)
    out.sort(key=lambda r: (SLOT_ORDER.get(str(r["slot"]), 9), r["recipe_id"]))
    return out


def week(db: Db, person_id: int, monday: dt.date) -> dict[str, Any]:
    start = week_start(monday)
    end = start + dt.timedelta(days=6)
    summary_row = db.one(
        "SELECT * FROM v_week_summary WHERE person_id = ? AND week_start = ?",
        (person_id, isoday(start)),
    )
    summary = dict(summary_row) if summary_row is not None else None
    # a stored 0 (watch on charge) reads no differently from a day icu never wrote at all -
    # both are "not counted", never "walked nowhere" - so the CASE nulls it the same as trend()
    days = db.all(
        "SELECT d.day, d.status, d.basis, d.kcal, d.protein_g, "
        "CASE WHEN w.steps IS NOT NULL AND w.steps > 0 THEN w.steps ELSE NULL END AS steps "
        "FROM v_day_latest d LEFT JOIN icu_wellness w "
        "ON w.person_id = d.person_id AND w.day = d.day "
        "WHERE d.person_id = ? AND d.day BETWEEN ? AND ? ORDER BY d.day",
        (person_id, isoday(start), isoday(end)),
    )
    by_day = {str(r["day"]): dict(r) for r in days}
    # every day BETWEEN the earliest and latest one this person actually has a row for, not
    # only the days a row exists for: the box being down overnight meant day_close never wrote
    # Wednesday, and reading only rows that exist made it vanish from the table entirely rather
    # than reading as a day nobody can tell "not yet confirmed" from "the system never ran"
    # (week_tab/02). Never extended past the recorded span - a week with nothing in it at all
    # (Monday morning, before today's own day closes) stays the empty table it really is
    # (week_tab/10), not a week's worth of blank rows invented ahead of any real data.
    day_dicts: list[dict[str, Any]] = []
    if by_day:
        known = sorted(by_day)
        cursor, last = parse_day(known[0]), parse_day(known[-1])
        while cursor <= last:
            iso = isoday(cursor)
            day_dicts.append(
                by_day.get(
                    iso,
                    {
                        "day": iso,
                        "status": None,
                        "basis": None,
                        "kcal": None,
                        "protein_g": None,
                        "steps": None,
                    },
                )
            )
            cursor += dt.timedelta(days=1)
    if summary is not None:
        walked = [d["steps"] for d in day_dicts if d["steps"] is not None]
        floor_row = db.one(
            "SELECT steps_floor FROM profiles WHERE person_id = ? AND effective_from <= ? "
            "ORDER BY effective_from DESC, id DESC LIMIT 1",
            (person_id, isoday(end)),
        )
        floor = int(floor_row["steps_floor"]) if floor_row is not None else None
        summary["steps_floor"] = floor
        summary["steps_avg"] = round(sum(walked) / len(walked)) if walked else None
        summary["steps_days_over"] = (
            sum(1 for s in walked if floor is not None and s >= floor) if walked else 0
        )
    return {
        "week_start": isoday(start),
        "week_end": isoday(end),
        "summary": summary,
        "days": day_dicts,
    }


def trend(
    db: Db, person_id: int, metric: str, days: int, today: dt.date, compare: str | None = None
) -> dict[str, Any]:
    """Return a dated trend series and chart metadata for one metric."""
    from .trend_queries import trend as read_trend  # noqa: PLC0415  # Loads after queries.

    return read_trend(db, person_id, metric, days, today, compare)


#: how far past today the Menu tab looks for the accepted segments after the current one
MENU_AHEAD_DAYS = 7


def menu(
    db: Db,
    person_id: int,
    segment_start: dt.date | None,
    today: dt.date,
    retailers: Any = (),
    *,
    provider: str = "claude-cli",
) -> dict[str, Any]:
    """The menu covering today and the accepted segments after it, or one covering a named day.

    covering today. On Monday her Thursday lunch was accepted, in the database, and no tap on
    the page reached it. With no segment asked for, the segments that start within the next
    week come too, the newest menu for each, the way `/menu` walks ahead in the bot.

    A link from the bot names a `day`, not a segment's own start date - the owner's link to "last
    Wednesday's lunch" named 2026-09-09, the middle of a Mon-Fri segment that started on the
    7th - so an exact match against `segment_start` silently found nothing and the tab fell
    back to today's segment without a word. The lookup below covers any day inside the segment,
    so the caller (`renderMenu()`, via `?segment=`) only has to forward whatever day it was
    given, not resolve segment boundaries itself.
    """
    later: list[Any] = []
    if segment_start is None:
        row = db.one(
            "SELECT id, segment_start, segment_end FROM v_menu_current WHERE person_id = ? "
            "AND segment_start <= ? AND segment_end >= ? ORDER BY id DESC LIMIT 1",
            (person_id, isoday(today), isoday(today)),
        )
        if row is not None:
            later = db.all(
                "SELECT id, segment_start, segment_end FROM v_menu_current WHERE person_id = ? "
                "AND segment_start > ? AND segment_start <= ? ORDER BY segment_start, id DESC",
                (
                    person_id,
                    str(row["segment_end"]),
                    isoday(today + dt.timedelta(days=MENU_AHEAD_DAYS)),
                ),
            )
    else:
        row = db.one(
            "SELECT id, segment_start, segment_end FROM v_menu_current WHERE person_id = ? "
            "AND segment_start <= ? AND segment_end >= ? ORDER BY segment_start DESC, id DESC "
            "LIMIT 1",
            (person_id, isoday(segment_start), isoday(segment_start)),
        )
    covers_today = row is not None
    if row is None and segment_start is None:
        # nothing for today is not nothing at all: on Saturday 26 Sep the owner had Sunday and next
        # week planned and the tab said only "No menu covers today" - the menus he was asking
        # about were in the database and no tap reached them. Show what is coming up instead.
        upcoming = db.all(
            "SELECT id, segment_start, segment_end FROM v_menu_current WHERE person_id = ? "
            "AND segment_start > ? AND segment_start <= ? ORDER BY segment_start, id DESC",
            (person_id, isoday(today), isoday(today + dt.timedelta(days=MENU_AHEAD_DAYS))),
        )
        if upcoming:
            row, later = upcoming[0], upcoming[1:]
    from ..food import weekend as weekend_mod  # noqa: PLC0415 (food imports the db layer)

    # the weekend's switch and this person's treats, whether or not a menu covers today: the
    # switch is flipped before a weekend exists as often as after
    weekend_panel = weekend_mod.page(db, person_id, today, provider)
    if row is None:
        return {"menu": None, "days": [], "covers_today": False, "weekend": weekend_panel}
    menus = [row]
    starts = {str(row["segment_start"])}
    for r in later:  # newest first within a segment, so the first seen is the one that counts
        if str(r["segment_start"]) not in starts:
            starts.add(str(r["segment_start"]))
            menus.append(r)
    out_days = []
    seen_days: set[str] = set()
    for m_row in menus:
        for d in _menu_days(db, person_id, int(m_row["id"]), today, retailers=retailers):
            # two menus can list one day (a Wed-Fri replacement ahead of a Mon-Fri week, both
            # still to come): each day comes once, with the plates in force that day, and the
            # tab drew Wednesday to Friday two and three times over
            if str(d["day"]) in seen_days:
                continue
            seen_days.add(str(d["day"]))
            out_days.append(d)
    from ..food import catalogue as catalogue_mod  # noqa: PLC0415 (only the menu needs it)

    # the bot says "This menu still has Chickpeas in it, which you now avoid"; the page showing
    # the same menu said nothing, so the page read as if the refusal had been ignored
    refused = catalogue_mod.refused_names_on(db, person_id, [int(m["id"]) for m in menus])
    from ..food import packs as packs_mod  # noqa: PLC0415 (food imports the db layer)

    # each shop window the days shown fall in, once, with its packs and the days they are for
    windows: list[dict[str, Any]] = []
    for d in out_days:
        on = parse_day(str(d["day"]))
        if on < today or any(w["from"] <= isoday(on) <= w["to"] for w in windows):
            continue
        # the shop's own packs, as the list buys them: the same line on Menu and on Shopping
        view = packs_mod.window_view(db, person_id, on, retailers=retailers)
        if view["lines"]:
            windows.append(view)
    return {
        "menu": dict(row),
        "menus": [dict(m) for m in menus],
        "days": out_days,
        "refused_still_on_it": refused,
        "covers_today": covers_today or segment_start is not None,
        "weekend": weekend_panel,
        "packs": windows[:2],
        # someone whose week is planned in one build may rebuild one day of it alone
        "rebuild_days": rebuild_days(db, person_id, out_days, today),
        # their own word on how many meats or fish a week; asked until they give one
        "mains": packs_mod.mains_answer(db, person_id),
        "mains_question": packs_mod.MAINS_QUESTION,
    }


def cycle(db: Db, person_id: int, day: dt.date) -> dict[str, Any] | None:
    """Where `day` falls in their cycle, for someone tracking it: worked out from the period
    starts they enter (body.cycle_on); {"day": None} when none is entered yet; None when they do
    not track it."""
    from .. import body  # noqa: PLC0415
    from .. import targets as targets_mod  # noqa: PLC0415

    profile = targets_mod.profile_on(db, person_id, day)
    if profile is None or not profile["cycle_tracking"]:
        return None
    now = body.cycle_on(db, person_id, day)
    if now is None:
        return {"day": None, "phase": None, "start": None, "length": None}
    return {"day": now.day, "phase": now.phase, "start": isoday(now.start), "length": now.length}


def rebuild_days(
    db: Db, person_id: int, days: Sequence[Mapping[str, Any]], today: dt.date
) -> list[dict[str, str]]:
    """The weekdays shown that one day alone may be rebuilt for, after today, for someone whose
    week is planned in one build (food/weekplan.py `plan_day`); [] for anyone else."""
    from ..food import weekplan  # noqa: PLC0415 (food imports the db layer)

    if weekplan.of(db, person_id).plan_day is None:
        return []
    out: list[dict[str, str]] = []
    for d in days:
        on = parse_day(str(d["day"]))
        if on > today and on.weekday() < 5:
            out.append({"day": isoday(on), "label": f"{on:%a %d %b}"})
    return out


def _menu_days(
    db: Db, person_id: int, menu_id: int, today: dt.date, *, retailers: Sequence[str] = ()
) -> list[dict[str, Any]]:
    """One menu's days, each slot decorated with its recipe and, for a batch-cooked slot,
    which day the pot was actually cooked on.

    A recipe row is shared by every day it repeats FROM THE SAME COOK (`food/batch.py`'s own
    comment: one pot, one `recipes` row, portioned across days) - unlike a dish accepted afresh
    each day, which gets a fresh row (`dishes()`'s docstring). So the first day a (slot, recipe)
    pair appears in this menu is the cook day; a later day showing the same pair is a box out of
    that same pot, and the page has to say so or a reheated Thursday lunch reads as a method to
    start now, in an office with no hob (menu_recipes/05).
    """
    from ..food import planning  # noqa: PLC0415 (only the batch-cooked flag needs it)

    prof = db.one(
        "SELECT * FROM profiles WHERE person_id = ? AND effective_from <= ? "
        "ORDER BY effective_from DESC, id DESC LIMIT 1",
        (person_id, isoday(today)),
    )
    batch_slots = planning.batched(prof)
    freezes = bool(prof["freezes"]) if prof is not None else False
    days = db.all(
        "SELECT md.id, md.day, md.kcal, md.protein_g, md.fat_g, md.carb_g, md.fibre_g "
        "FROM menu_days md JOIN menus m ON m.id = md.menu_id "
        "WHERE m.person_id = ? AND md.menu_id = ? ORDER BY md.day",
        (person_id, menu_id),
    )
    out_days = []
    cooked_on: dict[tuple[str, int], dt.date] = {}  # (slot, recipe_id) -> its first day here
    from ..food import packs as packs_mod  # noqa: PLC0415 (food imports the db layer)
    from ..food import plates as plates_mod  # noqa: PLC0415 (food imports the db layer)
    from ..food import shared  # noqa: PLC0415 (food imports the db layer)

    # where each meal's meat or fish comes from: the pack, what this meal takes, what is left
    from_packs = packs_mod.meal_lines(
        db, person_id, [parse_day(str(d["day"])) for d in days], retailers=retailers
    )

    for plan_day in days:
        # the plates in force that day: a day the person resized, or a later part-week menu,
        # is its own menu day; the week around it is still this menu's
        d = (
            db.one(
                "SELECT id, day, kcal, protein_g, fat_g, carb_g, fibre_g, menu_id "
                "FROM v_menu_days_current WHERE person_id = ? AND day = ?",
                (person_id, str(plan_day["day"])),
            )
            or plan_day
        )
        day_menu = int(d["menu_id"]) if "menu_id" in d.keys() else menu_id  # noqa: SIM118
        # a shared weekend is cooked together on the day, never out of a box from a batch cook
        together = shared.pair_of(db, day_menu) is not None
        slots = db.all(
            "SELECT ms.slot, ms.recipe_id, rc.name, ms.portion_g, ms.portion_multiplier, ms.kcal, "
            "ms.protein_g, ms.fat_g, ms.carb_g, ms.fibre_g, rc.prep_minutes, rc.method_json, "
            "rc.servings, rc.keep_days, rc.freezable "
            "FROM meal_slots ms JOIN recipes rc ON rc.id = ms.recipe_id "
            "JOIN menu_days md ON md.id = ms.menu_day_id JOIN menus m ON m.id = md.menu_id "
            "WHERE m.person_id = ? AND ms.menu_day_id = ?",
            (person_id, int(d["id"])),
        )
        day = parse_day(str(d["day"]))
        items = []
        for s in slots:
            entry = dict(s)
            entry["slot_order"] = SLOT_ORDER.get(str(s["slot"]), 9)
            entry.pop("method_json", None)
            entry.update(_recipe_extras(db, person_id, int(s["recipe_id"]), s["method_json"]))
            # the score he already gave this dish, so the card shows it rather than five blank
            # buttons that invite a second, contradictory rating
            entry["score"] = _latest_rating(db, person_id, int(s["recipe_id"])).get("score")
            slot_name = str(s["slot"])
            key = (slot_name, int(s["recipe_id"]))
            first_day = cooked_on.setdefault(key, day)
            in_batch = slot_name in batch_slots and not together
            # the other person's plate of the same dish, found through the pair THIS menu is in
            # and shown only while both still agree (food/shared.partner_plate)
            entry["together"] = (
                shared.partner_plate(db, person_id, day_menu, day, slot_name) if together else None
            )
            entry["cook_day"] = isoday(first_day) if in_batch else None
            entry["batch_cooked"] = in_batch and day > first_day
            # the person's own "yes I freeze" OR'd with the model's guess, which is what
            # `menuflow._freezer_facts` and `/batch` both use. Reading `recipes.freezable` raw
            # here put a second answer about the same box on a third surface.
            entry["freezable"] = bool(s["freezable"]) or (freezes and in_batch)
            entry["from_packs"] = [] if together else from_packs.get((isoday(day), slot_name), [])
            items.append(entry)
        items.sort(key=lambda r: r["slot_order"])
        row = {k: d[k] for k in ("id", "day", "kcal", "protein_g", "fat_g", "carb_g", "fibre_g")}
        plates = plates_mod.day_info(db, person_id, day, today)
        out_days.append({**row, "slots": items, "plates": plates})
    return out_days


from .query_actions import (  # noqa: E402
    EXPORT_CHILDREN,
    EXPORT_LEAVES_OUT,
    RHYTHMS,
    SHELF_RETRY,
    _latest_rating,
    batch,
    confirm_today,
    export,
    foods,
    latest_score,
    list_span,
    mark_have,
    mark_shelf_empty,
    progress,
    rate_dish,
    record_yield,
    set_rhythm,
    shop_from,
    tick_item,
    weekly_summary_text,
)
from .query_shopping import (  # noqa: E402
    LONG_LIFE_DAYS,
    REGULAR_LISTS,
    dishes,
    list_meals,
    shopping,
)

__all__ = [
    "EXPORT_CHILDREN",
    "EXPORT_LEAVES_OUT",
    "LONG_LIFE_DAYS",
    "MENU_AHEAD_DAYS",
    "REGULAR_LISTS",
    "RHYTHMS",
    "SHELF_RETRY",
    "SLOT_ORDER",
    "TAPE_COMPARE_DEFAULT",
    "TREND_METRICS",
    "TREND_UNITS",
    "WIDE_WINDOW_DAYS",
    "activity_last",
    "batch",
    "confirm_today",
    "cycle",
    "day_record",
    "dishes",
    "done_sessions",
    "estimates",
    "export",
    "foods",
    "latest_score",
    "list_meals",
    "list_span",
    "mark_have",
    "mark_shelf_empty",
    "meals",
    "menu",
    "planned_session",
    "planned_sessions",
    "profile",
    "progress",
    "rate_dish",
    "rebuild_days",
    "record_yield",
    "set_rhythm",
    "shop_from",
    "shopping",
    "steps_last",
    "targets",
    "tick_item",
    "trend",
    "trend_point",
    "week",
    "weekly_summary_text",
    "weigh_in",
    "wellness",
]
