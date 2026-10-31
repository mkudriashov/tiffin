"""Render shopping list operations."""

from __future__ import annotations

import datetime as dt
from collections.abc import Collection, Iterable, Mapping, Sequence
from typing import Any

from ..clock import parse_day
from ..db import Db
from .catalogue import DEFAULT_AISLES, num
from .products import money
from .shopping import (
    _CONTAINER_WORDS,
    WEEKEND,
    ShoppingItem,
    aisle_sort_key,
    read_back,
    ticks_for,
    weekend_in_force,
    weekend_menu_ids,
    weekend_of_span,
)
from .shopping_lists import DISH_SEP, _list_owners, _menu_span


def short_dish_name(name: str) -> str:
    """ "Warm chicken" for "Warm chicken, green bean and potato salad" - the label a shared line
    falls back to once it names more than one dish (owner_week/10, two_people/07): the full name
    of even ONE dish can use most of a line's budget, so naming several in full never survives a
    phone-width line. Enough to recognise which dish, not the whole name a line naming only one
    dish still gives in full."""
    return " ".join(name.split()[:2])


def joint_dish_owners(db: Db, row: Any) -> dict[str, dict[int, list[str]]]:
    """`weekend_dish_owners` for a joint list: its owners' plates in force on each weekend day."""
    if row is None:
        return {}
    span = _menu_span(db, row["menu_id"])
    if span is None:
        return {}
    listed = _list_owners(row)
    in_force = weekend_in_force(db, *weekend_of_span(span[0]), listed)
    if not in_force:
        return weekend_dish_owners(db, weekend_menu_ids(db, int(row["menu_id"])))
    return weekend_dish_owners(db, sorted(in_force), only_days=in_force)


def weekend_dish_owners(
    db: Db, menu_ids: Sequence[int], *, only_days: Mapping[int, Collection[str]] | None = None
) -> dict[str, dict[int, list[str]]]:
    """code -> {person_id: [dish names]}, for the Saturday/Sunday slots of these menus only.

    The joint list is one basket for both plates, but a food only one of them cooks is not
    "shared" just because it sits on a shared list: her weekend menu can be chickpea-free while
    his Sunday stew still needs the tin, and a list that just says "Chickpeas" a fourth time
    reads as the same broken promise (two_people/07). `run_joint` uses this to attribute an
    item, on the copy of whichever person does NOT cook it, to whoever's dish it is actually for.
    """
    if not menu_ids:
        return {}
    marks = ",".join("?" for _ in menu_ids)
    out: dict[str, dict[int, list[str]]] = {}
    for r in db.all(
        f"SELECT md.menu_id, md.day, m.person_id, i.code, rc.name FROM menu_days md "
        f"JOIN menus m ON m.id = md.menu_id "
        f"JOIN meal_slots ms ON ms.menu_day_id = md.id "
        f"JOIN recipes rc ON rc.id = ms.recipe_id "
        f"JOIN recipe_ingredients ri ON ri.recipe_id = rc.id "
        f"JOIN ingredients i ON i.id = ri.ingredient_id "
        f"WHERE md.menu_id IN ({marks}) ORDER BY ms.id",
        tuple(menu_ids),
    ):
        if parse_day(str(r["day"])).weekday() not in WEEKEND:
            continue
        if only_days is not None and str(r["day"]) not in only_days.get(int(r["menu_id"]), ()):
            continue
        by_owner = out.setdefault(str(r["code"]), {})
        names = by_owner.setdefault(int(r["person_id"]), [])
        name = str(r["name"])
        if name not in names:
            names.append(name)
    return out


def attribution_for(
    owners_by_code: Mapping[str, Mapping[int, Sequence[str]]], reader_id: int
) -> dict[str, str]:
    """code -> dish label, for the codes NOT on `reader_id`'s own weekend menu.

    A shared dish (the traybake both of them eat) needs no attribution - it is exactly as much
    the reader's as anyone's, which is what keeps a joint list with nothing exclusive on it
    (test_01's shared salmon) identical for both phones. Only a food exclusively cooked by
    someone else's dish gets a label, and only on the copy of the person who does not cook it.
    """
    out: dict[str, str] = {}
    for code, by_owner in owners_by_code.items():
        if reader_id in by_owner:
            continue
        others = [name for owner, names in by_owner.items() if owner != reader_id for name in names]
        if not others:
            continue
        out[code] = (
            others[0] if len(others) == 1 else DISH_SEP.join(short_dish_name(n) for n in others)
        )
    return out


# ---------------------------------------------------------------- rendering
def pack_short(pack_label: str, pack_g: float) -> str:
    """'500 g pack' -> '500 g', '1 kg bag' -> '1 kg', '6 eggs' -> '6 eggs'."""
    words = pack_label.split()
    if len(words) >= 2 and words[-1].casefold() in _CONTAINER_WORDS:
        words = words[:-1]
    return " ".join(words) if words else f"{num(pack_g)} g"


def total_line(items: Iterable[ShoppingItem]) -> str:
    """ "About GBP 42.10 for the week (14 of 18 items priced)." or "" when nothing is priced.

    Deliberately "about", and deliberately not a payment: prices are what a page said when it
    was last read, some items have none, and Tiffin never buys anything. It is there so
    a week's food has a number attached before standing at a till.
    """
    buying = [i for i in items if i.packs > 0]
    priced = [i for i in buying if i.price_pence is not None]
    if not priced:
        return ""
    total = sum(int(i.price_pence or 0) for i in priced)
    tail = "" if len(priced) == len(buying) else f" ({len(priced)} of {len(buying)} priced)"
    currencies = {i.currency for i in priced}
    if len(currencies) > 1:
        return "Some products have prices in different currencies."
    return f"About {money(total, priced[0].currency)}{tail}."


#: how to say a shop on a line somebody reads walking into it
RETAILER_NAMES = {
    "sainsburys": "Sainsbury's",
    "marks_spencer": "M&S",
    "tesco": "Tesco",
    "wholefoods": "Whole Foods",
    "ocado": "Ocado",
}


def shop_name(retailer: str) -> str:
    """The shop's name as a person says it, or "" for a shop not worth naming."""
    return RETAILER_NAMES.get(retailer, retailer)


def shop_notes(
    db: Db, codes: Iterable[str], shop: str, elsewhere: Collection[str] = ()
) -> dict[str, tuple[str, str]]:
    """What `shop` said about each food, for list lines that do not name one of its products.

    code -> (words, status). Read when the list is shown, not when it was built, so a line from
    for this menu?). `found`: the shop has it after all, and names the product. `sold_out`: a
    person marked the shelf empty. `not_sold`: the shop was read and does not sell it.
    `unread`: the shop's page could not be read for it, often enough to stop trying for now.
    `unchecked`: nobody has an answer yet.

    Only the current reader's verdicts, and a person's own word at the shelf: a "not sold here"
    from a reader whose rules have since changed is asked again, so the line says "not checked"
    until it is, rather than a month of "Not at Sainsbury's" for egg whites.
    """
    from . import products  # noqa: PLC0415 (products imports the db layer, not this module)

    name = shop_name(shop) or shop
    # by Tiffin's clock, not SQLite's: see shop_refresh._since
    since = (db.clock.now_utc() - dt.timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
    out: dict[str, tuple[str, str]] = {}
    for code in dict.fromkeys(codes):
        if code in elsewhere:
            continue
        product = db.one(
            "SELECT name, seen_at FROM products WHERE ingredient_code = ? AND retailer = ? "
            "ORDER BY seen_at DESC, id DESC LIMIT 1",
            (code, shop),
        )
        # a "Shelf empty?" taken back says nothing, so the line says what it said before the tap
        keep, kept = products.standing("m")
        miss = db.one(
            "SELECT m.kind, m.reason, m.seen_at FROM shop_misses m WHERE m.ingredient_code = ? "
            "AND m.retailer = ? AND m.seen_at >= ? AND (m.reader = ? OR m.reason LIKE ?) "
            f"AND {keep} ORDER BY m.seen_at DESC, m.id DESC LIMIT 1",
            (code, shop, since, products.READER_VERSION, products.PERSON_SAID_LIKE, *kept),
        )
        unstocked = miss is not None and miss["kind"] == "unstocked"
        if product is not None and (
            not unstocked or str(product["seen_at"]) >= str(miss["seen_at"] if miss else "")
        ):
            out[code] = (f"At {name}: {product['name']}", "found")
            continue
        if miss is not None and unstocked:
            if "shelf was empty" in str(miss["reason"] or ""):
                out[code] = (f"The {name} shelf was empty last time", "sold_out")
            else:
                out[code] = (f"Not at {name}", "not_sold")
            continue
        if miss is not None and miss["kind"] == "basis":
            # the shop has it; its page gives figures only for the cooked food
            out[code] = (f"At {name} (its page gives cooked figures only)", "found")
            continue
        tries = db.one(
            "SELECT COUNT(*) AS n FROM shop_misses WHERE ingredient_code = ? AND retailer = ? "
            "AND kind = 'unread' AND reader = ? AND seen_at >= ?",
            (code, shop, products.READER_VERSION, since),
        )
        n = int(tries["n"]) if tries is not None else 0
        if miss is not None and miss["kind"] == "unread" and n >= products.UNREAD_TRIES:
            # parked: the refresh has stopped asking, so "not checked yet" would be a promise
            out[code] = (f"Could not read {name} for this ({n} tries)", "unread")
        else:
            out[code] = (f"Not checked at {name} yet", "unchecked")
    return out


def list_days(db: Db, list_id: int) -> tuple[dt.date, dt.date] | None:
    """The first and last day a list buys for, or None."""
    row = db.one("SELECT * FROM shopping_lists WHERE id = ?", (list_id,))
    if row is None:
        return None
    first, last = row["from_day"], row["to_day"]
    if last is None:
        span = db.one(
            "SELECT segment_start, segment_end FROM menus WHERE id = ?", (row["menu_id"],)
        )
        if span is None:
            return None
        first, last = first or span["segment_start"], span["segment_end"]
    return parse_day(str(first)), parse_day(str(last))


def list_heading(db: Db, list_id: int) -> str:
    """'Shopping, Mon 19 to Fri 23 Oct': the days a list buys for."""
    days = list_days(db, list_id)
    if days is None:
        return "Shopping"
    a, b = days
    return f"Shopping, {a:%a %d %b}" if a == b else f"Shopping, {a:%a %d} to {b:%a %d %b}"


#: under Telegram's 4,096 characters a message, with room for the HTML
MESSAGE_CHARS = 3800


def message_parts(text: str, limit: int = MESSAGE_CHARS) -> list[str]:
    """`text` cut at line ends into messages Telegram will take."""
    parts: list[str] = []
    current = ""
    for line in text.splitlines():
        if current and len(current) + 1 + len(line) > limit:
            parts.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        parts.append(current)
    return parts


def forward_text(db: Db, list_id: int, *, heading: str = "") -> str:
    """
    is already ticked left off, no links and no buttons - a link opens the reader's own list, not
    this one. What they ticked or said is at home is left off. HTML-escaped; `<b>` headings
    only."""
    from ..telegram.render import esc  # noqa: PLC0415 (render imports nothing of the food layer)
    from . import packs  # noqa: PLC0415 (packs imports this module)

    items = [i for i in read_back(db, list_id) if i.packs > 0]
    owner = db.one("SELECT person_id FROM shopping_lists WHERE id = ?", (list_id,))
    days = list_days(db, list_id)
    if owner is not None and owner["person_id"] is not None:
        # what is ticked, and what they said is at home ("I have it"), is not to buy
        ticked = ticks_for(db, list_id, int(owner["person_id"]), joint=False)
        have = packs.have_codes(db, int(owner["person_id"]), *days) if days else set()
        items = [i for i in items if i.item_id not in ticked and i.code not in have]
    if not items:
        return esc(heading or "Shopping") + "\nNothing left to buy."
    say_shop = len({i.retailer for i in items if i.retailer}) > 1
    lines = [f"<b>{esc(heading)}</b>"] if heading else []
    aisle = ""
    for item in sorted(items, key=aisle_sort_key(DEFAULT_AISLES)):
        if item.aisle != aisle:
            aisle = item.aisle
            lines.append(f"<b>{esc(aisle.capitalize())}</b>")
        lines.append("- " + esc(shelf_text(item, say_shop=say_shop)))
    total = total_line(items)
    if total:
        lines.append(esc(total))
    return "\n".join(lines)


def shelf_text(item: ShoppingItem, *, say_shop: bool = False) -> str:
    """Name the generic food or selected product with its purchase quantity.

    When a list spans shops, add a shop tag only if the product name does not already say it.
    """
    if not item.product_name:
        generic = f"{item.name} {item.packs} × {pack_short(item.pack_label, item.pack_g)}"  # noqa: RUF001
        return generic + _credit_note(item)
    text = f"{item.product_name} ×{item.packs}"  # noqa: RUF001
    if item.price_pence is not None:
        text += f" {money(item.price_pence, item.currency)}"
    shop = shop_name(item.retailer) if say_shop else ""
    if shop and shop.casefold() not in item.product_name.casefold():
        text += f" [{shop}]"
    return text + _credit_note(item)


def _credit_note(item: ShoppingItem) -> str:
    """What the list is counting on already being in the fridge, when it buys less for it.

    A week eating 1650 g of chicken is four 500 g packs; with 350 g credited from last segment
    the list says three, and the credit appeared nowhere on it. If the fridge is actually empty -
    an unshopped week, a holiday, somebody else ate it - the week is a pack short and there is
    nothing on the paper to notice. The ten-day cap in LEFTOVER_DAYS bounds how wrong the credit
    can be; it cannot make the person aware of it.
    """
    return f" ({num(item.credit_g)} g in)" if item.credit_g > 0 else ""


def render_lines(
    items: Iterable[ShoppingItem],
    *,
    aisles: Sequence[str] = DEFAULT_AISLES,
    max_len: int = 119,
    attribution: Mapping[str, str] | None = None,
) -> list[str]:
    """One line per aisle ('Meat: Chicken breast 2 x 500 g, Beef mince 5% 1 x 500 g'), wrapped
    so no line reaches `max_len` characters; the caller decides how many lines to send.

    `attribution` (code -> dish label) names whose food an item is, for a reader whose own
    weekend has none of it (two_people/07, via `attribution_for`) - every other caller passes
    nothing and the line reads exactly as it always has.
    """
    ordered = sorted(items, key=aisle_sort_key(aisles))
    say_shop = len({i.retailer for i in ordered if i.retailer}) > 1
    by_aisle: dict[str, list[str]] = {}
    for item in ordered:
        text = shelf_text(item, say_shop=say_shop)
        label = (attribution or {}).get(item.code)
        if label:
            text += f" - {label}"
        by_aisle.setdefault(item.aisle, []).append(text)
    lines: list[str] = []
    for aisle, texts in by_aisle.items():
        prefix = f"{aisle.capitalize()}: "
        current = ""
        for text in texts:
            candidate = text if not current else f"{current}, {text}"
            if current and len(prefix) + len(candidate) > max_len:
                lines.append(prefix + current)
                current = text
            else:
                current = candidate
        if current:
            lines.append(prefix + current)
    return lines
