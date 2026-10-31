"""Shopping data for the web page."""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Mapping
from typing import Any

from ..clock import isoday, parse_day, week_start
from ..db import Db
from ..food import shopping as shopping_mod
from .queries import _recipe_extras


def shopping(
    db: Db,
    person_id: int,
    list_id: int | None,
    today: dt.date | None = None,
    retailers: Any = (),
    clock: Any = None,
    elsewhere: Any = (),
) -> dict[str, Any]:
    replaced: int | None = None
    if list_id is None and today is not None and clock is not None:
        # the list for the cook she is about to shop for - the bot's answer, the same function
        row = shopping_mod.list_to_show(db, person_id, today, retailers=retailers, clock=clock)
    elif list_id is None:
        row = shopping_mod.newest_readable(db, person_id)
    else:
        # only a list with this person's food on it: her own, or a joint one she is an owner of
        row = shopping_mod.list_row(db, list_id, person_id)
        # a message's link to a list a Change or a rebuild has since replaced: the list now in
        # force for those days, and a word saying so
        now = shopping_mod.replaced_by(db, row, person_id) if row is not None else None
        if now is not None and row is not None:
            replaced = int(row["id"])
            row = now
    if row is None:
        return {"list": None, "items": []}
    items = db.all(
        # the pack the list was BUILT against. si.packs was computed from the real product's
        # pack, and printing the catalogue's generic guess beside it told the person to buy
        # 2 x 300 g where the menu needs 1300 g - and contradicted the bot for the same list.
        # i.code rides along only to key the attribution lookup below; it is dropped again
        # before the item goes out. si.retailer says which shop a product line was seen in,
        # same as the bot's own "[M&S]" (shopping_tab/09).
        "SELECT si.id, i.code, i.name, COALESCE(si.pack_label, i.pack_label) AS pack_label, "
        "si.packs, si.grams_needed, si.leftover_g, si.credit_g, si.aisle, si.pack_g, "
        "si.product_name, si.product_url, "
        "si.price_pence, si.currency, si.retailer, "
        # what the food is per 100 g, so a tub picked off a shelf can be compared with the one
        "i.kcal_100, i.protein_100, i.fat_100, i.carb_100, i.fibre_100 "
        "FROM shopping_items si JOIN ingredients i ON i.id = si.ingredient_id "
        "JOIN shopping_lists sl ON sl.id = si.list_id "
        "WHERE si.list_id = ? AND (sl.person_id = ? OR sl.person_id IS NULL)",
        (int(row["id"]), person_id),
    )
    items = sorted(items, key=shopping_mod.row_sort_key())
    home = [
        {"name": str(i["name"]), "grams": float(i["grams_needed"]), "aisle": str(i["aisle"])}
        for i in items
        if int(i["packs"]) == 0
    ]
    items = [i for i in items if int(i["packs"]) > 0]
    # what the first shop said about every line that does not name one of its products
    head = str(retailers[0]) if retailers else ""
    notes = (
        shopping_mod.shop_notes(
            db,
            [str(i["code"]) for i in items if str(i["retailer"] or "") != head],
            head,
            elsewhere,
        )
        if head
        else {}
    )
    ticked = shopping_mod.ticks_for(db, int(row["id"]), person_id, joint=row["person_id"] is None)
    attribution: dict[str, str] = {}
    if row["person_id"] is None and row["menu_id"] is not None:
        # a joint list is one basket for two plates, but a food only ONE of them cooks is not
        # "shared" just because it sits on a shared list - her weekend can be chickpea-free
        # while his Sunday stew still needs the tin (two_people/05)
        # the plates in force on each weekend day, the list's owners' own
        owners = shopping_mod.joint_dish_owners(db, row)
        attribution = shopping_mod.attribution_for(owners, person_id)
    # which dishes each food is for, the days this list buys for: "for Lentil dal" beside the
    # red lentils, so a substitution at the shelf is made knowing what it goes into
    for_dishes = _dishes_by_code(db, person_id, row)
    # how often this person's lists have carried it: the regulars, said as such
    seen = _list_frequency(db, person_id, int(row["id"]))
    # the lines the person said the shelf was empty for: the row's "Marked ... Undo" lived only
    # on the row tapped, and a redraw put "Shelf empty?" back with no way to take the mark back
    codes = [str(i["code"]) for i in items]
    shelf_out = _shelf_marked(db, person_id, codes, head) if head else set()
    out_items = []
    code_of: dict[int, str] = {}
    for i in items:
        entry = dict(i)
        code = str(entry.pop("code"))
        code_of[int(entry["id"])] = code
        entry["for_dishes"] = sorted(for_dishes.get(code, ()))[:3]
        # a food that keeps for months is usually at home already: checked first, before the
        entry["long_life"] = shopping_mod.keeps_days(str(i["aisle"])) >= LONG_LIFE_DAYS
        if code in seen:
            entry["regular"] = seen[code]
        entry["ticked"] = int(i["id"]) in ticked
        entry["out"] = code in shelf_out
        if code in notes:
            entry["shop_note"], entry["shop_status"] = notes[code]
        whose = attribution.get(code)
        if whose:
            entry["whose"] = whose
        out_items.append(entry)
    # the bot's own header names which days this list is for ("Shopping Mon 14 to Fri 18 Sep");
    # the page's list carried no day at all, so Friday's weekend list and Monday's segment list
    # read identically (shopping_tab/07)
    list_dict = dict(row)
    # the day it buys from, when it was built again from a chosen day (migration 0034)
    from_day = db.scalar("SELECT from_day FROM shopping_lists WHERE id = ?", (int(row["id"]),))
    listed = shopping_mod.list_owners(row)
    list_dict.pop("owner_ids", None)
    # a joint list with one person's food on it is not "both of you"
    list_dict["for_both"] = row["person_id"] is None and (listed is None or len(listed) > 1)
    list_dict["replaced_id"] = replaced
    list_dict.pop("basis", None)
    if row["kind"] == "range":
        # today to Friday over each day's own menu: its own first and last day
        list_dict["segment_start"] = str(row["from_day"])
        list_dict["segment_end"] = str(row["to_day"])
    elif row["menu_id"] is not None:
        span = db.one(
            "SELECT segment_start, segment_end FROM menus WHERE id = ?", (row["menu_id"],)
        )
        if span is not None:
            list_dict["segment_start"] = str(from_day or span["segment_start"])
            list_dict["segment_end"] = str(span["segment_end"])
            if row["kind"] == "segment":
                # the days it could start from, for "I shop on Monday and cook from Tuesday"
                first = parse_day(str(span["segment_start"]))
                last = parse_day(str(span["segment_end"]))
                list_dict["days"] = [
                    isoday(first + dt.timedelta(days=i)) for i in range((last - first).days + 1)
                ]
                list_dict["from_day"] = str(from_day or span["segment_start"])
    # the other list still live beside this one - the weekend's and Monday's, on a Saturday
    also = (
        shopping_mod.other_lists(db, person_id, today, int(row["id"])) if today is not None else []
    )
    meals = list_meals(db, person_id, row, today, clock) if today is not None else []
    # each pack the shop opens and the days that cook from it: their
    # own plates, the window around the list's first day
    from ..food import packs as packs_mod  # noqa: PLC0415 (food imports the db layer)

    first = list_dict.get("segment_start")
    last = list_dict.get("segment_end")
    packs = (
        packs_mod.window_view(db, person_id, parse_day(str(first)), retailers=retailers)
        if first
        else None
    )
    if first and last and row["person_id"] is not None:
        # "I have it": theirs to say, for exactly these days, and kept when the list is built
        # again for them; and where a pack an earlier day opened should cover a line, said beside
        start, end = parse_day(str(first)), parse_day(str(last))
        have = packs_mod.have_codes(db, person_id, start, end)
        cover = packs_mod.covered(
            db,
            person_id,
            start,
            {
                code_of[int(e["id"])]: float(e["grams_needed"]) + float(e.get("credit_g") or 0)
                for e in out_items
            },
            retailers=retailers,
        )
        for e in out_items:
            code = code_of[int(e["id"])]
            e["have"] = code in have
            if code in cover:
                e["covered"] = cover[code]
    return {
        "list": list_dict,
        "items": out_items,
        "also": also,
        "at_home": home,
        "meals": meals,
        "packs": packs,
    }


#: a food that keeps at least this long (food/shopping.py KEEPS_DAYS) is checked at home first
LONG_LIFE_DAYS = 90
#: how many of a person's weeks of lists, before this list's week, say how regular a food is
REGULAR_LISTS = 4


def _shelf_marked(db: Db, person_id: int, codes: list[str], retailer: str) -> set[str]:
    """The codes this person marked "the shelf was empty" on the page, still in force: their own
    latest word on the food (`shopping_out`, a mark or its Undo) is a mark, and the shop's LATEST
    row for it is still a shelf-empty one, inside the window the mark parks a food for
    (`shop_refresh.MISS_DAYS`). Whatever came after - an Undo, or the shop read again - ends the
    mark, however that later row reads. `shop_misses` has no person: read alone, the other one's
    mark on their own list was drawn here as this person's, and this person's Undo took it back.
    """
    from ..jobs.shop_refresh import MISS_DAYS  # noqa: PLC0415 (jobs import the scheduler)

    if not codes:
        return set()
    since = (db.clock.now_utc() - dt.timedelta(days=MISS_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    said = [
        json.loads(str(e["payload_json"]))
        for e in db.all(
            "SELECT payload_json FROM web_events WHERE person_id = ? AND kind = 'shopping_out' "
            "AND at >= ? ORDER BY id",
            (person_id, since),
        )
    ]
    if not said:
        return set()
    # an item is on one list; the mark is about the food, on whichever list it is now
    ids = sorted({int(e["item_id"]) for e in said})
    code_of = {
        int(r["id"]): str(r["code"])
        for r in db.all(
            "SELECT si.id, i.code FROM shopping_items si "
            "JOIN ingredients i ON i.id = si.ingredient_id "
            f"WHERE si.id IN ({','.join('?' for _ in ids)})",
            tuple(ids),
        )
    }
    mine: dict[str, bool] = {}
    for e in said:  # oldest first: the last word on each food stands
        code = code_of.get(int(e["item_id"]))
        if code is not None:
            mine[code] = not e.get("undo")
    marked = [c for c in codes if mine.get(c)]
    if not marked:
        return set()
    marks = ",".join("?" for _ in marked)
    return {
        str(r["ingredient_code"])
        for r in db.all(
            "SELECT m.ingredient_code, m.reason FROM shop_misses m WHERE m.retailer = ? "
            f"AND m.ingredient_code IN ({marks}) AND m.seen_at >= ? "
            "AND m.id = (SELECT MAX(x.id) FROM shop_misses x "
            "WHERE x.retailer = m.retailer AND x.ingredient_code = m.ingredient_code)",
            (retailer, *marked, since),
        )
        if str(r["reason"] or "").startswith("the person said the shelf was empty")
    }


def _dishes_by_code(db: Db, person_id: int, row: Any) -> dict[str, set[str]]:
    """code -> the dishes on this person's plates, on the days the list buys for, holding it."""
    days = _list_days(db, row)
    if days is None:
        return {}
    out: dict[str, set[str]] = {}
    for r in db.all(
        "SELECT DISTINCT i.code, rc.name FROM v_menu_days_current md "
        "JOIN meal_slots ms ON ms.menu_day_id = md.id JOIN recipes rc ON rc.id = ms.recipe_id "
        "JOIN recipe_ingredients ri ON ri.recipe_id = rc.id "
        "JOIN ingredients i ON i.id = ri.ingredient_id "
        "WHERE md.person_id = ? AND md.day BETWEEN ? AND ?",
        (person_id, isoday(days[0]), isoday(days[1])),
    ):
        out.setdefault(str(r["code"]), set()).add(str(r["name"]))
    return out


def _list_days(db: Db, row: Any) -> tuple[dt.date, dt.date] | None:
    """(first, last) day a list buys for: a range list's own, or its menu's from the day it was
    built again from."""
    if row["kind"] == "range":
        return parse_day(str(row["from_day"])), parse_day(str(row["to_day"]))
    if row["menu_id"] is None:
        return None
    span = db.one("SELECT segment_start, segment_end FROM menus WHERE id = ?", (row["menu_id"],))
    if span is None:
        return None
    start = db.scalar("SELECT from_day FROM shopping_lists WHERE id = ?", (int(row["id"]),))
    return parse_day(str(start or span["segment_start"])), parse_day(str(span["segment_end"]))


def list_meals(
    db: Db, person_id: int, row: Any, today: dt.date, clock: Any
) -> list[dict[str, Any]]:
    """The list by meal: each day still to come it buys for, each meal with its dish and what it
    needs at this person's size - and the meals skipped, with what they would have needed - so a
    shopping list on the meal level ... I skip this meal and it already removes it").

    A person's own list only: on a joint weekend list the plates are two people's."""
    from ..food import catalogue  # noqa: PLC0415 (food imports the db layer)
    from ..food import menu as menu_mod  # noqa: PLC0415
    from ..food import plates as plates_mod  # noqa: PLC0415
    from ..food import swap as swap_mod  # noqa: PLC0415

    if row["person_id"] is None:
        return []
    days = _list_days(db, row)
    if days is None:
        return []
    first, last = max(days[0], today), days[1]
    units = catalogue.units(db)
    order = {s: i for i, s in enumerate(menu_mod.SLOT_ORDER)}
    eaten = swap_mod.had_today(db, person_id, clock.now_local()) if clock is not None else set()
    out: list[dict[str, Any]] = []
    day = first
    while day <= last:
        winner = menu_mod.accepted_menu_id(db, person_id, day)
        if winner is None:
            day += dt.timedelta(days=1)
            continue
        st = plates_mod.state(db, person_id, day) or {}
        levels = st.get("levels", {})
        locked = menu_mod.day_confirmed(db, person_id, day) or (
            plates_mod.cooked_with(db, person_id, day) is not None
        )
        shown = _plates_with_food(db, winner, day, units)
        on_day = set(shown)
        base = st.get("base_menu_id")
        if base is not None and int(base) != int(winner):
            # skipped meals: what the plan had, said as left out, so it can be brought back
            for slot, plate in _plates_with_food(db, int(base), day, units).items():
                if slot not in on_day and levels.get(slot) == "skip":
                    shown[slot] = {**plate, "skipped": True}
        for slot in sorted(shown, key=lambda s: order.get(s, 9)):
            plate = shown[slot]
            skipped = bool(plate.get("skipped"))
            out.append(
                {
                    "day": isoday(day),
                    "slot": slot,
                    "name": plate["name"],
                    "kcal": 0 if skipped else plate["kcal"],
                    "skipped": skipped,
                    "ingredients": plate["ingredients"],
                    "can_change": not locked
                    and not (day == today and slot in eaten)
                    and (skipped or len(on_day) > 1),
                }
            )
        day += dt.timedelta(days=1)
    return out


def _plates_with_food(
    db: Db, menu_id: int, day: dt.date, units: Mapping[str, Any]
) -> dict[str, dict[str, Any]]:
    """slot -> the dish on one menu's day, its kcal and its ingredients at that plate's size, in
    grams as the shopping list counts them (whole units bought whole)."""
    from ..food.solver import whole_units  # noqa: PLC0415

    plates: dict[str, dict[str, Any]] = {}
    for r in db.all(
        "SELECT ms.slot, ms.kcal, rc.name AS dish, ri.grams, ms.portion_multiplier, rc.servings, "
        "i.code, i.name FROM menu_days md JOIN meal_slots ms ON ms.menu_day_id = md.id "
        "JOIN recipes rc ON rc.id = ms.recipe_id "
        "JOIN recipe_ingredients ri ON ri.recipe_id = rc.id "
        "JOIN ingredients i ON i.id = ri.ingredient_id "
        "WHERE md.menu_id = ? AND md.day = ? ORDER BY ri.grams DESC",
        (menu_id, isoday(day)),
    ):
        plate = plates.setdefault(
            str(r["slot"]),
            {"name": str(r["dish"]), "kcal": round(float(r["kcal"])), "ingredients": []},
        )
        grams = float(r["grams"]) * float(r["portion_multiplier"]) / int(r["servings"])
        if str(r["code"]) in units:
            grams = whole_units(grams, units[str(r["code"])].g)
        plate["ingredients"].append({"name": str(r["name"]).split(",")[0], "grams": round(grams)})
    return plates


def _list_frequency(db: Db, person_id: int, list_id: int) -> dict[str, str]:
    """code -> "every week" / "3 of your last 4 weeks", for the foods on most of this person's

    Weeks, not list rows. Every swap, resize and "shop from" writes the week's list again, so
    the last four lists could all be one week's, and a first week's food read "every
    week". A week holds what the LATEST list of each of its segments carried - the
    list shopped from, not the ones it replaced - and only weeks before this list's own count.
    """
    shown = db.scalar(
        "SELECT m.segment_start FROM shopping_lists sl JOIN menus m ON m.id = sl.menu_id "
        "WHERE sl.id = ?",
        (list_id,),
    )
    if shown is None:
        return {}
    this_week = week_start(parse_day(str(shown)))
    latest: dict[str, int] = {}  # segment start -> its latest list, in list order
    for r in db.all(
        "SELECT sl.id, m.segment_start FROM shopping_lists sl JOIN menus m ON m.id = sl.menu_id "
        "WHERE sl.person_id = ? ORDER BY sl.id",
        (person_id,),
    ):
        latest[str(r["segment_start"])] = int(r["id"])
    week_of = {
        list_no: week_start(parse_day(start))
        for start, list_no in latest.items()
        if week_start(parse_day(start)) < this_week
    }
    weeks = sorted(set(week_of.values()), reverse=True)[:REGULAR_LISTS]
    if len(weeks) < REGULAR_LISTS:
        return {}
    lists = [n for n, week in week_of.items() if week in weeks]
    marks = ",".join("?" for _ in lists)
    seen: dict[str, set[dt.date]] = {}
    for r in db.all(
        "SELECT DISTINCT si.list_id, i.code FROM shopping_items si "
        f"JOIN ingredients i ON i.id = si.ingredient_id WHERE si.list_id IN ({marks})",
        tuple(lists),
    ):
        seen.setdefault(str(r["code"]), set()).add(week_of[int(r["list_id"])])
    n = len(weeks)
    return {
        code: "every week" if len(on) == n else f"{len(on)} of your last {n} weeks"
        for code, on in seen.items()
        if len(on) >= n - 1
    }


def dishes(db: Db, person_id: int, recent_days: int, today: dt.date) -> list[dict[str, Any]]:
    """Return one recipe book row per dish name.

    Repeated menus can create several recipe rows with the same name. `MAX(rc.id)` selects the
    latest row, and SQLite takes the other recipe columns from that row when it is the query's
    sole aggregate.
    """
    from ..food import catalogue as catalogue_mod  # noqa: PLC0415 (only this query needs it)
    from ..food import recipes as recipes_mod  # noqa: PLC0415 (only this query needs it)

    # as far back as the page asks, "All" included: a recipe kept to cook again another time
    since = isoday(today - dt.timedelta(days=max(1, min(recent_days, 3650))))
    rows = db.all(
        "SELECT rc.name, MAX(rc.id) AS id, rc.prep_minutes, rc.servings, rc.method_json "
        "FROM v_menu_current m "
        "JOIN menu_days md ON md.menu_id = m.id JOIN meal_slots ms ON ms.menu_day_id = md.id "
        "JOIN recipes rc ON rc.id = ms.recipe_id "
        "WHERE m.person_id = ? AND md.day >= ? GROUP BY rc.name ORDER BY rc.name",
        (person_id, since),
    )
    last_on_menu = {
        str(r["name"]): str(r["last_day"])
        for r in db.all(
            "SELECT rc.name, MAX(md.day) AS last_day FROM v_menu_days_current md "
            "JOIN meal_slots ms ON ms.menu_day_id = md.id "
            "JOIN recipes rc ON rc.id = ms.recipe_id "
            "WHERE md.person_id = ? AND md.day <= ? GROUP BY rc.name",
            (person_id, isoday(today)),
        )
    }
    # keyed on the dish NAME, not the recipe id it happens to be reading: a repeated menu
    # writes a fresh `recipes` row every time it repeats (food/batch.py), so a rating made
    # against last week's row was invisible against this week's - the recipe book went blank
    # every Sunday and he re-rated the same dish for ever (dishes_summary/02). The note comes
    # along for the same reason a score does: it is the one place the reason he typed is ever
    # read back (dishes_summary/05).
    scored = {
        str(r["name"]): (int(r["score"]), r["note"])
        for r in db.all(
            "SELECT rc.name AS name, r.score AS score, r.note AS note FROM ratings r "
            "JOIN recipes rc ON rc.id = r.recipe_id "
            "WHERE r.person_id = ? AND r.id = (SELECT MAX(x.id) FROM ratings x "
            "JOIN recipes rx ON rx.id = x.recipe_id "
            "WHERE x.person_id = r.person_id AND rx.name = rc.name)",
            (person_id,),
        )
    }
    units = catalogue_mod.units(db)
    out = []
    for r in rows:
        entry = dict(r)
        entry["last_day"] = last_on_menu.get(str(r["name"]))
        # Use the same per-serving figures as the menu, including whole units.
        each = recipes_mod.stored_per_serving(db, int(r["id"]), 1.0, units)
        entry["per_portion"] = {
            "kcal": each.kcal,
            "protein_g": each.protein_g,
            "fat_g": each.fat_g,
            "carb_g": each.carb_g,
            "fibre_g": each.fibre_g,
        }
        score, note = scored.get(str(r["name"]), (None, None))
        entry["score"] = score
        entry["note"] = note
        entry.update(_recipe_extras(db, person_id, int(r["id"]), r["method_json"]))
        out.append(entry)
    return out
