"""Actions data for the web page."""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

from ..clock import isoday, parse_day, stamp, week_start
from ..db import Db
from ..food import shopping as shopping_mod
from .queries import _recipe_extras


def weekly_summary_text(db: Db, person_id: int, monday: dt.date) -> dict[str, Any]:
    """The Sunday summary this person received, never one composed across people.

    Asking only for this week's meant the tab was empty from Monday to Saturday - six days in
    seven - because the summary is written on Sunday evening. It falls back to the most recent
    one at or before the week asked for, and says which week that was, so the page has
    something to show on a Tuesday.
    """
    sunday = week_start(monday) + dt.timedelta(days=6)
    kept = db.one(
        "SELECT week_end, full_html, created_at FROM weekly_reviews WHERE person_id = ? "
        "AND week_end <= ? ORDER BY week_end DESC, id DESC LIMIT 1",
        (person_id, isoday(sunday)),
    )
    if kept is not None:
        return {
            "week_start": isoday(week_start(monday)),
            "text": {
                "text_html": str(kept["full_html"]),
                "sent_at": str(kept["created_at"]),
                "current": str(kept["week_end"]) == isoday(sunday),
            },
        }
    row = db.one(
        "SELECT text_html, sent_at, due_key, payload_json FROM messages_out "
        "WHERE person_id = ? AND kind = 'sunday_summary' AND status = 'sent' AND due_key <= ? "
        "ORDER BY due_key DESC, id DESC LIMIT 1",
        (person_id, isoday(sunday)),
    )
    text = None if row is None else dict(row)
    if text is not None:
        text["current"] = str(text.pop("due_key")) == isoday(sunday)
        # since 26 Sep the message is a short headline and the whole review rides in its
        # payload; the tab shows the whole review whichever one was sent
        try:
            full = json.loads(str(text.pop("payload_json") or "{}")).get("full_html")
        except ValueError:
            full = None
        if full:
            text["text_html"] = full
    return {"week_start": isoday(week_start(monday)), "text": text}


def shop_from(
    db: Db,
    person_id: int,
    list_id: int,
    day: dt.date,
    retailers: Any = (),
    clock: Any = None,
) -> int | None:
    """Build a list from this person's segment starting on day, or return None."""
    row = shopping_mod.list_row(db, list_id, person_id)
    if row is None or row["kind"] != "segment" or row["person_id"] is None:
        return None
    if row["menu_id"] is None:
        return None
    try:
        return shopping_mod.build_from(
            db, int(row["menu_id"]), day, retailers=retailers, clock=clock
        )
    except ValueError:
        return None


def mark_have(db: Db, person_id: int, item_id: int, have: bool) -> str | None:
    """ "I have it" on a line of their own list, for exactly that list's days: the line goes to
    "At home" and the list stops buying it; taken back, it is bought again. The food's name, or
    None when the item is not on a list of theirs (a joint list is two people's basket, and one
    person's cupboard is not the other's)."""
    row = db.one(
        "SELECT i.code, i.name, sl.id AS list_id FROM shopping_items si "
        "JOIN shopping_lists sl ON sl.id = si.list_id "
        "JOIN ingredients i ON i.id = si.ingredient_id "
        "WHERE si.id = ? AND sl.person_id = ?",
        (item_id, person_id),
    )
    if row is None:
        return None
    span = list_span(db, person_id, int(row["list_id"]))
    if span is None:
        return None
    from ..food import packs as packs_mod  # noqa: PLC0415 (food imports the db layer)

    packs_mod.mark_have(db, person_id, str(row["code"]), span[0], span[1], have)
    return str(row["name"]).split(",")[0]


def list_span(db: Db, person_id: int, list_id: int) -> tuple[dt.date, dt.date] | None:
    """The first and last day a list buys for, as the page shows them: a range list's own two,
    a segment list's menu from the day it was built from."""
    row = db.one(
        "SELECT sl.kind, sl.from_day, sl.to_day, m.segment_start, m.segment_end "
        "FROM shopping_lists sl LEFT JOIN menus m ON m.id = sl.menu_id "
        "WHERE sl.id = ? AND sl.person_id = ?",
        (list_id, person_id),
    )
    if row is None:
        return None
    if row["kind"] == "range" and row["from_day"] and row["to_day"]:
        return parse_day(str(row["from_day"])), parse_day(str(row["to_day"]))
    if row["segment_start"] is None:
        return None
    first = str(row["from_day"] or row["segment_start"])
    return parse_day(first), parse_day(str(row["segment_end"]))


def tick_item(db: Db, person_id: int, item_id: int, ticked: bool) -> bool:
    """Append a shopping_ticks row. False when the item is not on a list this person may see:
    their own, or a joint one with their food on it."""
    row = db.one(
        "SELECT si.id FROM shopping_items si JOIN shopping_lists sl ON sl.id = si.list_id "
        "WHERE si.id = ? AND (sl.person_id = ? OR sl.person_id IS NULL)",
        (item_id, person_id),
    )
    if row is None or not shopping_mod.item_readable(db, item_id, person_id):
        return False
    db.insert("shopping_ticks", item_id=item_id, person_id=person_id, ticked=1 if ticked else 0)
    return True


#: how soon a "Shelf empty?" on a food the same person has just marked is that tap sent again
#: (its answer lost on weak signal, Retry tapped) rather than a new word about the shelf
SHELF_RETRY = dt.timedelta(minutes=10)


def _last_tap(db: Db, person_id: int, code: str) -> dict[str, Any] | None:
    """This person's latest "Shelf empty?" or Undo on the food, on any list: {"undo", "at"}, or
    None. `shop_misses` has no person, so who said what is read from their own taps
    (`web_events`), never from the shop's rows: "the last row is a mark made today" swallowed the
    other person's own mark as a retry, so their Undo took back hers, and "the last row is an
    Undo" swallowed the other person's Undo, so one of two marks both taken back stood."""
    for r in db.all(
        "SELECT payload_json, at FROM web_events WHERE person_id = ? AND kind = 'shopping_out' "
        "ORDER BY id DESC",
        (person_id,),
    ):
        tap = json.loads(str(r["payload_json"]))
        on = db.scalar(
            "SELECT i.code FROM shopping_items si JOIN ingredients i ON i.id = si.ingredient_id "
            "WHERE si.id = ?",
            (int(tap["item_id"]),),
        )
        if on == code:
            return {"undo": bool(tap.get("undo")), "at": str(r["at"])}
    return None


def mark_shelf_empty(
    db: Db, person_id: int, item_id: int, retailer: str, *, undo: bool = False
) -> bool:
    """`shop_misses`, the same row `/shopping out chickpeas` writes from Telegram: the tick was
    the one dead end on the page - it means bought, and there was no way to say the shelf was
    empty (shopping_tab/10). `shop_misses` has no `person_id` (it is about the shop, not either
    person - reset.py), so the scoping check below is the same "on a list this person may see"
    guard `tick_item` uses, not a person-owned row.
    """
    from ..food import products  # noqa: PLC0415 (only this query needs it)

    row = db.one(
        "SELECT i.code FROM shopping_items si "
        "JOIN shopping_lists sl ON sl.id = si.list_id "
        "JOIN ingredients i ON i.id = si.ingredient_id "
        "WHERE si.id = ? AND (sl.person_id = ? OR sl.person_id IS NULL)",
        (item_id, person_id),
    )
    # and a joint list only when their food is on it
    if row is None or not shopping_mod.item_readable(db, item_id, person_id):
        return False
    code = str(row["code"])
    tap = _last_tap(db, person_id, code)
    if undo:
        # taken back: the Undo and a mark both stop counting (`products.standing` pairs them), so
        # whatever stood before the tap stands again - the shop's own "Not at Sainsbury's", the
        # food's fridge credit, or no answer yet. Only after the person's own mark: an Undo with
        # none of theirs would take back a verdict nobody made.
        if tap is not None and tap["undo"]:
            return True  # taken back already: their Undo sent again, nothing more to write
        keep, kept = products.standing("m")
        marked = db.scalar(
            "SELECT 1 FROM shop_misses m WHERE m.ingredient_code = ? AND m.retailer = ? "
            f"AND m.kind = 'unstocked' AND m.reason LIKE ? AND {keep} LIMIT 1",
            (code, retailer, products.PERSON_SAID_LIKE, *kept),
        )
        if tap is None or marked is None:
            return False
        db.insert(
            "shop_misses",
            ingredient_code=code,
            retailer=retailer,
            reason=products.PERSON_TOOK_BACK,
            seen_at=db.clock.utc_stamp(),
            kind="unread",
        )
        return True
    if (
        tap is not None
        and not tap["undo"]
        and tap["at"] >= stamp(db.clock.now_utc() - SHELF_RETRY)
        and db.scalar(
            "SELECT 1 FROM leftovers l JOIN ingredients i ON i.id = l.ingredient_id "
            "WHERE l.person_id = ? AND i.code = ? AND l.created_at > ? LIMIT 1",
            (person_id, code, tap["at"]),
        )
        is None
    ):
        # their own mark sent again (its answer lost on the way back, Retry tapped): no second
        # mark, because one Undo takes back one mark and a second would go on cancelling the
        # fridge credit and the shop's own verdict. The other person's tap,
        # a tap after a credit made since (a list rebuilt), or a later one is a new word.
        return True
    db.insert(
        "shop_misses",
        ingredient_code=code,
        retailer=retailer,
        reason="the person said the shelf was empty, from the page",
        seen_at=db.clock.utc_stamp(),
    )
    return True


def batch(db: Db, person_id: int, from_day: dt.date | None, today: dt.date) -> dict[str, Any]:
    """The Batch tab: the same cook `/batch` gives in Telegram, with a day to start it from.

    Reads `batch_mod.menu_to_batch()` directly, never `batchflow.cmd_batch`'s wrapper: the bot
    refuses outright once the cook day it would have used has already passed, and Wednesday
    evening - needing Wed-Fri out of a menu shaped for a Sunday cook - is exactly that moment
    """
    from ..food import batch as batch_mod  # noqa: PLC0415 (only this query needs it)
    from ..food import catalogue as catalogue_mod  # noqa: PLC0415 (only this query needs it)
    from ..food import planning  # noqa: PLC0415 (only this query needs it)

    empty: dict[str, Any] = {
        "cook_day": None,
        "segment_start": None,
        "segment_end": None,
        "boxes": 0,
        "total_minutes": 0,
        "slots": [],
        "fresh": [],
        "dishes": [],
        "amounts": [],
        "to_freeze": [],
        "refused_still_on_it": [],
    }

    prof = db.one(
        "SELECT * FROM profiles WHERE person_id = ? AND effective_from <= ? "
        "ORDER BY effective_from DESC, id DESC LIMIT 1",
        (person_id, isoday(today)),
    )
    if prof is None:
        return empty
    # nothing to batch for someone who cooks each day, whatever the batch setting still says
    slots = planning.batched(prof)
    if not slots:
        return empty
    menu = batch_mod.menu_to_batch(db, person_id, today)
    if menu is None:
        return empty
    load = batch_mod.plan(db, menu, slots, today, freezes=bool(prof["freezes"]), from_day=from_day)
    # the latest weighing per food: `cook_yields` is append-only, so a second go at the same
    # turkey is a new row and MAX(id) is the one that counts
    yields = {
        str(r["ingredient_code"]): {
            "raw_g": float(r["raw_g"]),
            "cooked_g": float(r["cooked_g"]),
            "pct": round(100.0 * float(r["cooked_g"]) / float(r["raw_g"])),
        }
        for r in db.all(
            "SELECT ingredient_code, raw_g, cooked_g FROM cook_yields WHERE person_id = ? "
            "AND id IN (SELECT MAX(id) FROM cook_yields WHERE person_id = ? "
            "GROUP BY ingredient_code)",
            (person_id, person_id),
        )
    }
    dishes = []
    for d in load.dishes:
        method_json = db.scalar("SELECT method_json FROM recipes WHERE id = ?", (d.recipe_id,))
        extras = _recipe_extras(db, person_id, d.recipe_id, method_json)
        # the whole pot's grams, not one recipe's - the same "times the recipe" scaling the
        # bot's pot card uses, divided into the boxes this cook actually makes
        raw_grams = sum(float(i["grams"]) for i in extras["ingredients"])
        per_box_g = round(raw_grams * d.multiples / d.boxes, 1) if d.boxes else 0.0
        # What to weigh out for THIS pot: the recipe's own grams times the scaling this cook
        # needs. The tab had the method and not this, which is the wrong half - a cook standing
        # at the counter can infer "cut the potatoes into wedges" from the dish's name, and
        # are a shopping figure by comparison: they answer "how much chicken does the sitting
        # need", not "how much goes in this pan". Rounded through `weigh` here rather than in
        # the page, so the bot's kitchen units and the tab's are the same units.
        packs = {str(i["name"]): i for i in extras["ingredients"]}
        pot = []
        for a in load.amounts:
            for share in a.by_dish:
                if share.dish != d.name:
                    continue
                pack = packs.get(a.name, {})
                code = str(pack.get("code") or "")
                pot.append(
                    {
                        "name": a.name,
                        "code": code,
                        # what this food weighed last time it came out of the pan, so the split
                        # can be offered before the pan is on rather than only after
                        "yield": yields.get(code),
                        "grams": round(share.grams, 1),
                        "text": batch_mod.weigh(share.grams),
                        "boxes": share.boxes,
                        "uniform": share.uniform,
                        # the number somebody standing over open boxes with a spoon needs: the
                        # pot total is what goes in the pan, this is what goes in each box
                        "per_box": round(share.per_box, 1),
                        "per_box_text": batch_mod.weigh(share.per_box),
                        # and when the boxes are NOT all the same - a dish made 0.8x for Monday
                        # and 0.7x for Wednesday - the split is the only honest answer
                        "box_texts": [batch_mod.weigh(g) for g in share.box_grams],
                        # the same split as numbers: a cooked weight is apportioned by each
                        # box's share of the raw, which cannot be done from the rendered text
                        "box_grams": [round(g, 1) for g in share.box_grams],
                        "pack_g": pack.get("pack_g"),
                        "pack_label": pack.get("pack_label"),
                        # what the food is, not only how much of it goes in the pan
                        "kcal_100": pack.get("kcal_100"),
                        "protein_100": pack.get("protein_100"),
                        "fat_100": pack.get("fat_100"),
                        "carb_100": pack.get("carb_100"),
                        "fibre_100": pack.get("fibre_100"),
                    }
                )
        dishes.append(
            {
                "recipe_id": d.recipe_id,
                "name": d.name,
                "boxes": d.boxes,
                "multiples": d.multiples,
                "keep_days": d.keep_days,
                "freezable": d.freezable,
                "days": [isoday(day) for day in d.days],
                "method": extras["method"],
                "ingredients": extras["ingredients"],
                "pot": pot,
                "prep_minutes": d.prep_minutes,
                "per_box_g": per_box_g,
            }
        )
    amounts = [
        {
            "name": a.name,
            "grams": a.grams,
            # a kitchen figure, not a lab one: nobody weighs out 1000 g of chicken (§0.7)
            "text": batch_mod.weigh(a.grams),
            "pantry": a.pantry,
            "boxes": a.boxes,
            "uniform": a.uniform,
            "per_box": a.per_box,
            "by_dish": [
                {
                    "dish": s.dish,
                    "grams": s.grams,
                    "boxes": s.boxes,
                    "uniform": s.uniform,
                    "per_box": s.per_box,
                    "box_grams": list(s.box_grams),
                }
                for s in a.by_dish
            ],
        }
        for a in load.amounts
    ]
    to_freeze = [
        {
            "name": dish.name,
            "days": [isoday(day) for day in days],
            "note": "Move a box from the freezer to the fridge the night before you need it.",
        }
        for dish, days in load.to_freeze
    ]
    refused = catalogue_mod.refused_names_on(db, person_id, [int(menu["id"])])
    return {
        "cook_day": isoday(load.cook_day),
        "segment_start": isoday(load.segment_start),
        "segment_end": isoday(load.segment_end),
        "boxes": load.boxes,
        "total_minutes": load.total_minutes,
        "slots": list(load.slots),
        "fresh": list(load.fresh),
        "dishes": dishes,
        "amounts": amounts,
        "to_freeze": to_freeze,
        "refused_still_on_it": refused,
    }


def foods(db: Db, person_id: int) -> dict[str, Any]:
    """What the Add-a-food form needs: the aisles, and the foods already in this person's roster.

    The aisles come from the same tuple the shopping walk is ordered by, or a hand-added food
    lands in an aisle the list cannot place and drifts to the end of every shop. The roster is
    there because it is exactly what the menu prompt is numbered from: seeing it is how somebody
    knows a food is already in, under a name they would not have guessed ("Skyr, natural").
    """
    from ..food import catalogue as catalogue_mod  # noqa: PLC0415 (only this query needs it)

    return {
        "aisles": list(catalogue_mod.DEFAULT_AISLES),
        "foods": [
            {
                "code": i.code,
                "name": i.name,
                "kcal_100": i.kcal_100,
                "protein_100": i.protein_100,
                "aisle": i.aisle,
            }
            for i in catalogue_mod.approved(db, person_id)
        ],
    }


def record_yield(
    db: Db,
    person_id: int,
    recipe_id: int,
    ingredient_code: str,
    raw_g: float,
    cooked_g: float,
    today: dt.date,
) -> dict[str, Any] | None:
    """Append a cook_yields row and answer with the split. None when it is not this cook's dish.

    The menu comes from the person and today, never from the
    client, and the recipe has to be one of that menu's own. The ingredient has to be one the
    recipe actually uses - a code from anywhere else would write a yield for a food this person
    is not cooking, and every reader of this table then answers with it.
    """
    from ..food import batch as batch_mod  # noqa: PLC0415 (only this query needs it)

    menu = batch_mod.menu_to_batch(db, person_id, today)
    if menu is None:
        return None
    row = db.one(
        "SELECT 1 FROM menu_days md JOIN meal_slots ms ON ms.menu_day_id = md.id "
        "JOIN menus m ON m.id = md.menu_id "
        "JOIN recipe_ingredients ri ON ri.recipe_id = ms.recipe_id "
        "JOIN ingredients i ON i.id = ri.ingredient_id "
        "WHERE md.menu_id = ? AND ms.recipe_id = ? AND m.person_id = ? AND i.code = ?",
        (int(menu["id"]), recipe_id, person_id, ingredient_code),
    )
    if row is None:
        return None
    db.insert(
        "cook_yields",
        person_id=person_id,
        ingredient_code=ingredient_code,
        recipe_id=recipe_id,
        raw_g=float(raw_g),
        cooked_g=float(cooked_g),
    )
    return {
        "ingredient_code": ingredient_code,
        "raw_g": float(raw_g),
        "cooked_g": float(cooked_g),
        "pct": round(100.0 * float(cooked_g) / float(raw_g)),
    }


def confirm_today(db: Db, person_id: int, day: dt.date, as_planned: bool) -> dict[str, Any] | None:
    """Record a day as planned from the page, writing exactly what the evening check-in writes.

    Only the as-planned answer. "Not quite" is a conversation - what changed, an estimate, a tap
    to accept the number - and that belongs where the conversation is; the page offering half of
    it would leave a day open with no way to finish it here. None means "not this route".

    A day already confirmed is left alone: `day_confirmations` is append-only and a second
    identical row says nothing, while a correction after a real answer has to come from the flow
    that knows what changed.
    """
    from .. import days as days_mod  # noqa: PLC0415 (days imports the food layer)

    if not as_planned:
        return None
    existing = days_mod.latest_confirmation(db, person_id, day)
    if existing is not None and str(existing["status"]) == "confirmed":
        return {"day": isoday(day), "status": "confirmed", "already": True, "kcal": None}
    planned = days_mod.planned_macros(db, person_id, day)
    macros = planned if planned is not None else days_mod.target_macros(db, person_id, day)
    days_mod.confirm_day(
        db,
        person_id,
        day,
        basis="menu" if planned is not None else "none",
        macros=macros,
        note=None if planned is not None else "assumed target",
    )
    return {
        "day": isoday(day),
        "status": "confirmed",
        "already": False,
        "kcal": None if macros is None else macros.kcal,
    }


#: how often Tiffin messages a person (migration 0035), as the page names each one
RHYTHMS = ("light", "daily", "weekly")


def set_rhythm(db: Db, person_id: int, rhythm: str, today: dt.date) -> bool:
    """A new profiles row with the rhythm changed and everything else as it was. False when there
    is no profile yet (the interview writes the first)."""
    from .. import targets as targets_mod  # noqa: PLC0415

    if rhythm not in RHYTHMS:
        raise ValueError(rhythm)
    row = targets_mod.profile_on(db, person_id, today)
    if row is None:
        return False
    if str(row["rhythm"]) == rhythm:
        return True
    targets_mod.change_profile(db, person_id, {"rhythm": rhythm}, today=today)
    return True


def rate_dish(db: Db, person_id: int, recipe_id: int, score: int, note: str | None) -> bool:
    """Append a ratings row. False when the recipe was never on this person's menu."""
    row = db.one(
        "SELECT rc.id FROM recipes rc JOIN meal_slots ms ON ms.recipe_id = rc.id "
        "JOIN menu_days md ON md.id = ms.menu_day_id JOIN menus m ON m.id = md.menu_id "
        "WHERE rc.id = ? AND m.person_id = ? LIMIT 1",
        (recipe_id, person_id),
    )
    if row is None:
        return False
    if note is None:
        # a score from the Menu card carries no note, and the Dishes tab reads the note off the
        # LATEST rating - so a quick 4 on Menu erased "dry, it needs a sauce" typed on Dishes a
        # week earlier. The last note for this dish rides forward.
        note = _latest_rating(db, person_id, recipe_id).get("note")
    elif not note.strip():
        # an empty note is the box emptied on purpose: no note from now on. Read as "none sent",
        # a note once written could never be taken back
        note = None
    db.insert("ratings", person_id=person_id, recipe_id=recipe_id, score=score, note=note)
    return True


def latest_score(db: Db, person_id: int, recipe_id: int) -> int | None:
    """Their newest score for this dish's name, or None when they never rated it."""
    got = _latest_rating(db, person_id, recipe_id).get("score")
    return None if got is None else int(got)


def _latest_rating(db: Db, person_id: int, recipe_id: int) -> dict[str, Any]:
    """The newest score and note this person gave any recipe of this dish's NAME.

    By name, because a repeated menu writes a fresh recipe row for the same dish (`dishes()`).
    """
    row = db.one(
        "SELECT r.score, r.note FROM ratings r JOIN recipes rc ON rc.id = r.recipe_id "
        "WHERE r.person_id = ? AND rc.name = (SELECT name FROM recipes WHERE id = ?) "
        "ORDER BY r.id DESC LIMIT 1",
        (person_id, recipe_id),
    )
    return {} if row is None else {"score": int(row["score"]), "note": row["note"]}


#: tables left out of a person's own export: the ones that bind them to the bot and the page (the
#: chat, the login, the pairing), which are not something they told it, and raw payloads the
#: parsed rows already carry in readable form. Their own messages still say which chat they came
#: in on (`messages_in.chat_id` and the update in its `raw_json`, `messages_out.chat_id`): the
#: chat is theirs, and so is the message.
EXPORT_LEAVES_OUT = frozenset(
    {
        "telegram_identities",
        "tailscale_identities",
        "pairing_codes",
        "icu_raw",
        "icu_stream_index",
    }
)
#: rows with no person_id that are theirs through one of their own kept rows, beside
#: `reset.CHILDREN`: a voice note as transcribed is what they said, and the parsed diary's
#: matches are about their day
EXPORT_CHILDREN: dict[str, str] = {
    "inbound_outcomes": "message_in_id IN (SELECT id FROM messages_in WHERE person_id = ?)",
    "voice_transcripts": "message_in_id IN (SELECT id FROM messages_in WHERE person_id = ?)",
}


def export(db: Db, person_id: int, *, clock: Any) -> dict[str, Any]:
    """Everything Tiffin holds about this person, as JSON they can keep: every row of every
    table with their person_id, and every row that is theirs through one of those - the days,
    meals and decisions of their menus, their lists' items, their voice notes as transcribed.
    Never a row of the other person's (CLAUDE.md: subject-access test - everything written about

    Only the menus' days and meals came with it at first, so a menu a swap had put in force read
    "proposed" for want of its decision, and what she said in a voice note was left out."""
    from .. import reset  # noqa: PLC0415 (the table classification lives there)

    tables: dict[str, list[dict[str, Any]]] = {}
    names = [*reset.TOLD, *reset.KEPT, "menus", "menu_emphasis", "ratings", "dish_events"]
    names += ["ingredient_events", "ingredient_preferences", "built_dishes", "proposal_log"]
    names += ["weekly_reviews", "estimates", "day_confirmations", "treat_tastes"]
    for name in dict.fromkeys(names):
        if name in EXPORT_LEAVES_OUT:
            continue
        cols = db.columns(name)
        if not cols or "person_id" not in cols:
            continue
        rows = db.all(f"SELECT * FROM {name} WHERE person_id = ? ORDER BY rowid", (person_id,))
        if rows:
            tables[name] = [dict(r) for r in rows]
    for name, clause in {**reset.CHILDREN, **EXPORT_CHILDREN}.items():
        if name in EXPORT_LEAVES_OUT or not db.columns(name):
            continue
        rows = db.all(f"SELECT * FROM {name} WHERE {clause} ORDER BY rowid", (person_id,))
        if rows:
            tables[name] = [dict(r) for r in rows]
    who = db.one("SELECT display_name, slug FROM persons WHERE id = ?", (person_id,))
    return {
        "exported_at": clock.utc_stamp(),
        "person": {
            "name": str(who["display_name"]) if who else "",
            "slug": str(who["slug"]) if who else "",
        },
        "tables": tables,
    }


def progress(
    db: Db, person_id: int, weeks: int, week_day: dt.date, today: dt.date
) -> dict[str, Any]:
    """Return measured history and current menu data for the Progress page."""
    from .progress_queries import progress as read_progress  # noqa: PLC0415  # Loads after queries.

    return read_progress(db, person_id, weeks, week_day, today)
