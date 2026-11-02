"""Calculate menu shopping needs and render pack-based lists in aisle order.

Lists include every ingredient unless the person marks it as always at home. A pack opened by
the plan earlier in the same shop window covers later days before another pack is bought.
Surplus from an earlier window is counted only when the person has said they have it.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .. import links
from ..clock import Clock, isoday, parse_day
from ..db import Db
from .catalogue import DEFAULT_AISLES, num
from .catalogue import units as unit_table
from .solver import whole_units

SLACK_TENTHS = 10  # 1 g: an overshoot this small does not earn another pack
WEEKEND = (5, 6)
_CONTAINER_WORDS = frozenset(
    {
        "pack",
        "bag",
        "tin",
        "can",
        "jar",
        "tub",
        "bottle",
        "carton",
        "box",
        "punnet",
        "block",
        "loaf",
        "pot",
        "bunch",
        "tray",
        "sachet",
    }
)


@dataclass
class ShoppingItem:
    ingredient_id: int
    code: str
    name: str
    aisle: str
    pack_g: float
    pack_label: str
    grams_total: float  # over the menu, before credit
    credit_g: float
    grams_needed: float  # after credit, >= 0
    packs: int
    leftover_g: float
    list_id: int | None = None
    item_id: int | None = None
    #: the real product this is, when one is known. The pack above is then the shop's pack,
    #: not the hand-typed catalogue guess, and the price is what that many of them cost.
    product_name: str = ""
    product_url: str = ""
    price_pence: int | None = None
    currency: str = "GBP"
    #: which shop it was seen in. An own-brand name carries it ("Tesco Broccoli 375G") and a
    #: branded one does not ("Arla Skyr Natural 450g"), so a list mixing two retailers needs
    #: this to say where to go.
    retailer: str = ""


@dataclass
class _Need:
    ingredient_id: int
    code: str
    name: str
    aisle: str
    pantry: bool
    pack_g: float
    pack_label: str
    pack_sizes_g: tuple[float, ...] = ()
    grams: float = 0.0


@dataclass(frozen=True)
class _GenericPack:
    pack_g: float
    pack_label: str
    name: str = ""
    price_pence: None = None
    retailer: str = ""
    url: str = ""
    currency: str = "GBP"


def packs_for(need_g: float, pack_g: float) -> tuple[int, float]:
    """(packs, leftover_g) in tenths-of-a-gram arithmetic; 1 g slack before another pack."""
    need, pack = round(need_g * 10), round(pack_g * 10)
    if need <= 0:
        return 0, round(-need / 10, 1)
    q, r = divmod(need, pack)
    packs = q if (r <= SLACK_TENTHS and q > 0) else q + 1
    return packs, round(max(0, packs * pack - need) / 10, 1)


def _needs(
    db: Db,
    menu_ids: Sequence[int],
    *,
    weekend_only: bool,
    pantry: Collection[str] | None = None,
    only_days: Mapping[int, Collection[str]] | None = None,
) -> dict[str, _Need]:
    """Grams per ingredient code over the menus' slots; the latest version id seen wins.

    `pantry` is the codes this person keeps in a cupboard, which is theirs to decide and not the
    catalogue's: `catalogue.pantry_codes` reads their own list where they have set one. Passing
    None keeps the catalogue's flag, which is what every caller with no person does.
    """
    rows = db.all(
        "SELECT md.menu_id, md.day, ri.grams, ms.portion_multiplier, rc.servings, "
        "i.id AS ingredient_id, "
        "i.code, i.name, i.aisle, i.pantry, i.pack_g, i.pack_label, i.pack_sizes_g "
        "FROM menu_days md JOIN meal_slots ms ON ms.menu_day_id = md.id "
        "JOIN recipes rc ON rc.id = ms.recipe_id "
        "JOIN recipe_ingredients ri ON ri.recipe_id = rc.id "
        "JOIN ingredients i ON i.id = ri.ingredient_id "
        f"WHERE md.menu_id IN ({','.join('?' for _ in menu_ids)}) ORDER BY i.id",
        tuple(menu_ids),
    )
    units = unit_table(db)
    needs: dict[str, _Need] = {}
    for r in rows:
        if weekend_only and parse_day(str(r["day"])).weekday() not in WEEKEND:
            continue
        if only_days is not None and str(r["day"]) not in only_days.get(int(r["menu_id"]), ()):
            continue  # another menu's plates feed that day (a rebuild of Sunday, a resize)
        code = str(r["code"])
        grams = float(r["grams"]) * float(r["portion_multiplier"]) / int(r["servings"])
        if code in units:
            # a slice is bought as a slice: the plate holds whole ones (solver.whole_units)
            grams = whole_units(grams, units[code].g)
        need = needs.get(code)
        if need is None or int(r["ingredient_id"]) > need.ingredient_id:
            prev = 0.0 if need is None else need.grams
            need = _Need(
                ingredient_id=int(r["ingredient_id"]),
                code=code,
                name=str(r["name"]),
                aisle=str(r["aisle"]),
                # ONLY what this person asked to leave off. The catalogue's own `pantry` flag no
                # longer hides anything from a shopping list: it was hiding twenty-four foods,
                # including every grain, which is how a week's shop came back with no
                # carbohydrate in it.
                pantry=code in (pantry or ()),
                pack_g=float(r["pack_g"]),
                pack_label=str(r["pack_label"]),
                pack_sizes_g=tuple(float(v) for v in json.loads(str(r["pack_sizes_g"]))),
                grams=prev,
            )
            needs[code] = need
        need.grams += grams
    return needs


def row_sort_key(aisles: Sequence[str] = DEFAULT_AISLES) -> Callable[[Any], tuple[int, str]]:
    """`aisle_sort_key` for a database row rather than a ShoppingItem.

    The sent message walks the shop, in the aisle order the catalogue YAML sets - produce, meat,
    fish, dairy, bakery, grains, tins, frozen, condiments. Both places that READ a list back
    ordered by `si.aisle, i.name`, which is alphabetical, so the same list reread in the aisle
    sent the person back past the dairy after they had reached the grains. SQL cannot know the
    YAML's order; a Python sort over twenty rows can.
    """
    order = {a: i for i, a in enumerate(aisles)}
    return lambda r: (order.get(str(r["aisle"]), len(order)), str(r["name"]).casefold())


def ticks_for(db: Db, list_id: int, person_id: int, *, joint: bool) -> set[int]:
    """The item ids ticked off this list, by whoever ticked them.

    `tick` deliberately lets either person tick a joint list ("either person may tick it"), and
    every reader then filtered the ticks by person - so one trolley, two phones, the weekend
    list: he ticks the salmon, her list still says buy it, and the second salmon goes in. The
    rows were right; only the reader was wrong. A per-person list stays per-person.
    """
    sql = (
        "SELECT t.item_id, t.ticked FROM shopping_ticks t WHERE t.item_id IN "
        "(SELECT id FROM shopping_items WHERE list_id = ?) AND t.id = "
        "(SELECT MAX(x.id) FROM shopping_ticks x WHERE x.item_id = t.item_id"
    )
    args: list[Any] = [list_id]
    if joint:
        sql += ")"
    else:
        sql += " AND x.person_id = ?) AND t.person_id = ?"
        args += [person_id, person_id]
    return {int(r["item_id"]) for r in db.all(sql, args) if int(r["ticked"]) == 1}


def aisle_sort_key(aisles: Sequence[str]) -> Callable[[ShoppingItem], tuple[int, str]]:
    order = {a: i for i, a in enumerate(aisles)}
    return lambda item: (order.get(item.aisle, len(order)), item.name.casefold())


def _items(
    needs: Mapping[str, _Need],
    credits: Mapping[str, float],
    aisles: Sequence[str],
    shop: Mapping[str, Any] | None = None,
) -> list[ShoppingItem]:
    """`shop` maps ingredient code to a products.Product whose pack replaces the guess."""
    items: list[ShoppingItem] = []
    for code, need in needs.items():
        if need.pantry:
            continue
        credit = max(0.0, float(credits.get(code, 0.0)))
        needed = round(need.grams - credit, 1)
        product = (shop or {}).get(code)
        pack_g = float(product.pack_g) if product is not None and product.pack_g else need.pack_g
        pack_label = (
            str(product.pack_label or f"{num(pack_g)} g pack")
            if product is not None and product.pack_g
            else need.pack_label
        )
        packs, leftover = packs_for(needed, pack_g)
        price = None
        # the price only belongs to the pack it is a price FOR. A product whose size could
        # not be parsed ("4 Pints", "12 Pack") keeps the catalogue's pack above, and pricing
        # that at the product's price gave a line naming one pack, a count for another, and
        # a week's total out by the difference
        if product is not None and product.pack_g and product.price_pence is not None and packs:
            price = packs * int(product.price_pence)
        items.append(
            ShoppingItem(
                ingredient_id=need.ingredient_id,
                code=code,
                name=need.name,
                aisle=need.aisle,
                pack_g=pack_g,
                pack_label=pack_label,
                grams_total=round(need.grams, 1),
                credit_g=credit,
                grams_needed=max(0.0, needed),
                packs=packs,
                leftover_g=leftover,
                product_name=str(product.name) if product is not None else "",
                product_url=str(product.url or "") if product is not None else "",
                price_pence=price,
                currency=product.currency if product is not None else "GBP",
                retailer=str(product.retailer) if product is not None else "",
            )
        )
    items.sort(key=aisle_sort_key(aisles))
    return items


def _pantry_for(db: Db, person_id: int) -> set[str]:
    """The codes this person's list leaves out. Their own list where they have set one."""
    from .catalogue import pantry_codes  # noqa: PLC0415 (catalogue imports the db layer)

    return pantry_codes(db, person_id)


def _shop_for(
    db: Db,
    person_id: int,
    needs: Mapping[str, _Need],
    retailers: Sequence[str] = (),
    credits: Mapping[str, float] | None = None,
) -> dict[str, Any]:
    """Purchase options for this list, including unbranded least-waste catalogue packs."""
    from . import products, weekplan  # noqa: PLC0415 (both import the db layer, not this module)

    wanted = {code: need.pack_g for code, need in needs.items() if not need.pantry}
    if not wanted:
        return {}
    need = (
        {
            code: max(0.0, n.grams - float((credits or {}).get(code, 0.0)))
            for code, n in needs.items()
            if code in wanted
        }
        if weekplan.of(db, person_id).least_waste
        else None
    )
    selected: dict[str, Any] = dict(
        products.for_shopping(db, person_id, wanted, retailers, need=need)
    )
    if need is not None:
        for code, food in needs.items():
            if code in selected or food.pantry:
                continue
            sizes = food.pack_sizes_g or (food.pack_g,)
            credited = float((credits or {}).get(code, 0.0))
            grams = max(0.0, food.grams - credited)
            size = min(sizes, key=lambda value: (packs_for(grams, value)[1], value))
            if size != food.pack_g:
                words = food.pack_label.split()
                container = words[-1] if words else "pack"
                if container == "eggs" and words[0].isdigit():
                    count = round(int(words[0]) * size / food.pack_g)
                    label = f"{count} eggs"
                else:
                    label = (
                        f"{num(size)} g {container}"
                        if container in _CONTAINER_WORDS
                        else f"{num(size)} g pack"
                    )
                selected[code] = _GenericPack(size, label)
    return selected


def _write(
    db: Db,
    items: list[ShoppingItem],
    *,
    kind: str,
    menu_id: int | None,
    person_id: int | None,
    leftover_person_ids: Sequence[int],
    clock: Clock,
    owner_credits: Mapping[int, Mapping[str, float]] | None = None,
    from_day: str | None = None,
    to_day: str | None = None,
    basis: str | None = None,
) -> list[ShoppingItem]:
    with db.tx():
        list_id = db.insert(
            "shopping_lists",
            menu_id=menu_id,
            kind=kind,
            person_id=person_id,
            created_at=clock.utc_stamp(),
            # whose food a joint list is: only they read and tick it (migration 0033)
            owner_ids=json.dumps(sorted(set(leftover_person_ids))) if person_id is None else None,
            from_day=from_day,
            to_day=to_day,
            basis=basis,
            # a joint list belongs to the link it was built under (migration 0007)
            link_id=links.active_link_id(db) if person_id is None else None,
        )
        for item in items:
            item.list_id = list_id
            if item.packs == 0 and item.credit_g > 0 and item.grams_total > 0:
                db.insert(
                    "shopping_items",
                    list_id=list_id,
                    ingredient_id=item.ingredient_id,
                    grams_needed=item.grams_total,
                    packs=0,
                    leftover_g=item.leftover_g,
                    aisle=item.aisle,
                    pack_g=item.pack_g,
                    pack_label=item.pack_label,
                    product_name=item.product_name or None,
                    product_url=item.product_url or None,
                    price_pence=None,
                    currency=item.currency,
                    retailer=item.retailer or None,
                    credit_g=item.credit_g,
                )
            if item.packs > 0:
                item.item_id = db.insert(
                    "shopping_items",
                    list_id=list_id,
                    ingredient_id=item.ingredient_id,
                    grams_needed=item.grams_needed,
                    packs=item.packs,
                    leftover_g=item.leftover_g,
                    aisle=item.aisle,
                    pack_g=item.pack_g,
                    pack_label=item.pack_label,
                    product_name=item.product_name or None,
                    product_url=item.product_url or None,
                    price_pence=item.price_pence,
                    currency=item.currency,
                    retailer=item.retailer or None,
                    credit_g=item.credit_g,
                )
            if not leftover_person_ids:
                # a range list writes no leftovers: the day lists under it already wrote theirs,
                # and a second row per food would count the same spare pack twice
                continue
            if item.credit_g > 0 or item.leftover_g > 0:
                if owner_credits is not None and len(leftover_person_ids) > 1:
                    # a joint list's need was decided by SUMMING each owner's own credit
                    # Paying unused credit back has to
                    # follow the same owner it came from, not an equal split of the pool
                    _write_owner_leftovers(
                        db, item, leftover_person_ids, owner_credits, menu_id, clock
                    )
                    continue
                # SPLIT across the people the list covers. A joint weekend list wrote the whole
                # household's leftovers to `menu_ids[0]`'s person: thirteen rows, all the owner's,
                # none
                # week's list then cancelled its entire 900 g of potatoes against food that is
                # half hers, and hers was discounted by nothing at all for a weekend she also
                # ate. That is why one list came back tiny and the other came back huge.
                share = round(item.leftover_g / len(leftover_person_ids), 1)
                for owner in leftover_person_ids:
                    db.insert(
                        "leftovers",
                        person_id=owner,
                        ingredient_id=item.ingredient_id,
                        grams=share,
                        from_menu_id=menu_id,
                        created_at=clock.utc_stamp(),
                    )
    return [i for i in items if i.packs > 0]


def _write_owner_leftovers(
    db: Db,
    item: ShoppingItem,
    owners: Sequence[int],
    owner_credits: Mapping[int, Mapping[str, float]],
    menu_id: int | None,
    clock: Clock,
) -> None:
    """Pay a joint list's unused credit back to whichever owner's own stock it came from.

    Two people's own leftovers are summed to decide how many packs the joint list buys. What is
    left over from that pooling has to go back the same way it was
    drawn - first from whoever had it - or a share of somebody else's rice quietly became half
    of the fridge's, every weekend the joint list ran (two_people/05). Only the part of the
    surplus that was ALREADY somebody's own credit is attributed like this; anything bought
    beyond what either of them had is fresh and nobody's yet, so it still splits equally, as it
    always has.
    """
    total_credit = sum(float(owner_credits.get(o, {}).get(item.code, 0.0)) for o in owners)
    consumed = min(total_credit, item.grams_total)
    unused = max(0.0, total_credit - item.grams_total)
    fresh_share = round(max(0.0, item.leftover_g - unused) / len(owners), 1)
    drawn = 0.0
    for owner in owners:
        own = float(owner_credits.get(owner, {}).get(item.code, 0.0))
        take = min(own, max(0.0, consumed - drawn))
        drawn += take
        db.insert(
            "leftovers",
            person_id=owner,
            ingredient_id=item.ingredient_id,
            grams=round(own - take + fresh_share, 1),
            from_menu_id=menu_id,
            created_at=clock.utc_stamp(),
        )


def for_menu(db: Db, menu_id: int) -> list[ShoppingItem]:
    """A list already built for this menu, exactly as it was sent. Empty when there is none.

    Read back from `shopping_items`, which stores the pack, the price, the shop and the credit as
    they were at the time - so a reread says what the message said rather than re-deriving it
    against today's catalogue and today's leftovers.
    """
    row = db.one(
        "SELECT id FROM shopping_lists WHERE menu_id = ? AND kind = 'segment' "
        "ORDER BY id DESC LIMIT 1",
        (menu_id,),
    )
    if row is None:
        return []
    return read_back(db, int(row["id"]))


def read_back(db: Db, list_id: int, *, buying: bool = True) -> list[ShoppingItem]:
    """A stored list's items exactly as they were written, in aisle order: what to buy, or with
    `buying=False` what the list counts on being at home already (`at_home`)."""
    items = [
        ShoppingItem(
            ingredient_id=int(r["ingredient_id"]),
            code=str(r["code"]),
            name=str(r["name"]),
            aisle=str(r["aisle"]),
            pack_g=float(r["pack_g"] or 0.0),
            pack_label=str(r["pack_label"] or ""),
            # an at-home row keeps the week's need in grams_needed (nothing is bought for it)
            grams_total=(
                float(r["grams_needed"])
                if int(r["packs"]) == 0
                else float(r["grams_needed"]) + float(r["credit_g"] or 0.0)
            ),
            credit_g=float(r["credit_g"] or 0.0),
            grams_needed=0.0 if int(r["packs"]) == 0 else float(r["grams_needed"]),
            packs=int(r["packs"]),
            leftover_g=float(r["leftover_g"] or 0.0),
            list_id=list_id,
            item_id=int(r["id"]),
            product_name=str(r["product_name"] or ""),
            product_url=str(r["product_url"] or ""),
            price_pence=None if r["price_pence"] is None else int(r["price_pence"]),
            currency=str(r["currency"]),
            retailer=str(r["retailer"] or ""),
        )
        for r in db.all(
            "SELECT si.*, i.code, i.name FROM shopping_items si "
            "JOIN ingredients i ON i.id = si.ingredient_id WHERE si.list_id = ? AND "
            + ("si.packs > 0" if buying else "si.packs = 0"),
            (list_id,),
        )
    ]
    return sorted(items, key=aisle_sort_key(DEFAULT_AISLES))


def at_home(db: Db, list_id: int) -> list[ShoppingItem]:
    """What a list counts on being at home already, and so does not buy: said, never dropped."""
    return read_back(db, list_id, buying=False)


def at_home_line(items: Sequence[ShoppingItem], limit: int = 5) -> str:
    """ "Should be at home already: chicken breast, eggs, wholemeal bread and 3 more." or ""."""
    if not items:
        return ""
    names = [i.name.split(",")[0].casefold() for i in items]
    head = ", ".join(names[:limit])
    more = f" and {len(names) - limit} more" if len(names) > limit else ""
    return f"Should be at home already: {head}{more}."


def build_once(
    db: Db,
    menu_id: int,
    *,
    aisles: Sequence[str] = DEFAULT_AISLES,
    retailers: Sequence[str] = (),
    clock: Clock | None = None,
) -> list[ShoppingItem]:
    """The list for this menu, built if it does not exist yet and reused if it does.

    Building twice is not idempotent: `_write` appends a new list and planned surplus rows.
    Every route that wants "the list for this menu" comes through here: the 18:00 job,
    `/shopping`, and anything on demand.
    """
    # the LIST existing is what counts, not it having something to buy: a list whose every
    # food is at home already has no buying lines, and was built again on every ask
    row = db.one(
        "SELECT id FROM shopping_lists WHERE menu_id = ? AND kind = 'segment' "
        "ORDER BY id DESC LIMIT 1",
        (menu_id,),
    )
    if row is not None:
        return read_back(db, int(row["id"]))
    return build(db, menu_id, aisles=aisles, retailers=retailers, clock=clock)


def superseded_list(db: Db, person_id: int, menu_id: int) -> Any:
    """The most recent OTHER 'segment' list for this person covering the EXACT same days - the
    one a fresh build for `menu_id` replaces, because the accepted menu for those days changed
    after a list had already gone out for the one it replaces (`/cook replace`, or a proposal
    accepted over one already shopped from)."""
    span = _menu_span(db, menu_id)
    if span is None:
        return None
    start, end = span
    return db.one(
        "SELECT sl.id, sl.menu_id FROM shopping_lists sl JOIN menus m ON m.id = sl.menu_id "
        "WHERE sl.kind = 'segment' AND sl.person_id = ? AND sl.menu_id != ? "
        "AND m.segment_start = ? AND m.segment_end = ? ORDER BY sl.id DESC LIMIT 1",
        (person_id, menu_id, isoday(start), isoday(end)),
    )


def _consumed_by(db: Db, menu_id: int, code: str, today: dt.date) -> float:
    """Grams of one food that THIS menu's days already lived through - today included, since
    standing in the kitchen the day itself is already spoken for - would have eaten by its own
    recipes: what a superseded menu's past days actually ate, as against what its whole segment
    would eat if it ran its full course."""
    rows = db.all(
        "SELECT md.day, ri.grams, ms.portion_multiplier, rc.servings FROM menu_days md "
        "JOIN meal_slots ms ON ms.menu_day_id = md.id JOIN recipes rc ON rc.id = ms.recipe_id "
        "JOIN recipe_ingredients ri ON ri.recipe_id = rc.id "
        "JOIN ingredients i ON i.id = ri.ingredient_id WHERE md.menu_id = ? AND i.code = ?",
        (menu_id, code),
    )
    unit = unit_table(db).get(code)
    total = 0.0
    for r in rows:
        if parse_day(str(r["day"])) > today:
            continue
        grams = float(r["grams"]) * float(r["portion_multiplier"]) / (int(r["servings"]) or 1)
        total += whole_units(grams, unit.g) if unit is not None else grams
    return total


def _apply_supersession(
    db: Db,
    credit: dict[str, float],
    prior: Any,
    person_id: int,
    needs: Mapping[str, _Need],
    today: dt.date,
) -> dict[str, float]:
    """Add packs actually bought for a superseded menu, then subtract its past meals.

    The base credit comes from an earlier day in this shop or stock the person reported.
    An unticked pack on the superseded list adds nothing to that credit.
    """
    ticked = ticks_for(db, int(prior["id"]), person_id, joint=False)
    for row in db.all(
        "SELECT si.id, si.packs, si.pack_g, i.code FROM shopping_items si "
        "JOIN ingredients i ON i.id = si.ingredient_id WHERE si.list_id = ? AND si.packs > 0",
        (int(prior["id"]),),
    ):
        code = str(row["code"])
        if code not in needs:
            continue  # not on the new list either; nothing to credit against
        was_bought = int(row["id"]) in ticked
        bought = float(row["pack_g"] or 0.0) * int(row["packs"]) if was_bought else 0.0
        consumed = _consumed_by(db, int(prior["menu_id"]), code, today)
        credit[code] = round(max(0.0, credit.get(code, 0.0) + bought - consumed), 1)
    return credit


def _carry_ticks(
    db: Db, prior: Any, written: list[ShoppingItem], person_id: int, clock: Clock
) -> None:
    """A tick is a fact about the trolley, not about which digital list happens to describe the
    same food: the chicken already ticked on a list a `/cook replace` has just superseded is
    still the chicken in the trolley, not the chicken to buy again (list_vs_menu/04)."""
    if prior is None:
        return
    ticked_ids = ticks_for(db, int(prior["id"]), person_id, joint=False)
    if not ticked_ids:
        return
    marks = ",".join("?" for _ in ticked_ids)
    ticked_codes = {
        str(r["code"])
        for r in db.all(
            "SELECT i.code FROM shopping_items si JOIN ingredients i ON i.id = si.ingredient_id "
            f"WHERE si.id IN ({marks})",
            tuple(ticked_ids),
        )
    }
    for item in written:
        if item.code in ticked_codes and item.item_id is not None:
            db.insert(
                "shopping_ticks",
                item_id=item.item_id,
                person_id=person_id,
                ticked=1,
                created_at=clock.utc_stamp(),
            )


def _credits_for(
    db: Db,
    person_id: int,
    first: dt.date,
    retailers: Sequence[str],
    *,
    skip_menu_id: int | None = None,
) -> dict[str, float]:
    """Credit packs opened on earlier days of this shop, plus stock the person reported."""
    from . import packs  # noqa: PLC0415 (packs imports this module)

    window = packs.window_of(db, person_id, first)
    earlier = [
        day
        for day in window
        if day < first and (skip_menu_id is None or menu_id_on(db, person_id, day) != skip_menu_id)
    ]
    planned = {
        pack.code: pack.left_g
        for pack in packs.window_packs(db, person_id, earlier, retailers=retailers)
        if pack.left_g > 0
    }
    stated = current_leftovers(db, person_id, first, only_stated=True)
    # A reported pack may be the same one the plan opened, so do not add them twice.
    return {code: max(planned.get(code, 0.0), stated.get(code, 0.0)) for code in planned | stated}


def build(
    db: Db,
    menu_id: int,
    *,
    aisles: Sequence[str] = DEFAULT_AISLES,
    retailers: Sequence[str] = (),
    clock: Clock | None = None,
) -> list[ShoppingItem]:
    """One segment list for the menu's person, using only credit from its shop window."""
    menu = db.one("SELECT id, person_id, segment_start FROM menus WHERE id = ?", (menu_id,))
    if menu is None:
        raise LookupError(f"no menu with id {menu_id}")
    person_id = int(menu["person_id"])
    needs = _needs(db, [menu_id], weekend_only=False, pantry=_pantry_for(db, person_id))
    use_clock = clock or db.clock
    credit = _credits_for(db, person_id, parse_day(str(menu["segment_start"])), retailers)
    # a menu that replaces one already shopped for, for the SAME days, is not "the next
    # segment": what is at home for it is what that list's ticks say was bought, not the naive
    # planned surplus `current_leftovers` would otherwise hand back
    prior = superseded_list(db, person_id, menu_id)
    if prior is not None:
        credit = _apply_supersession(db, credit, prior, person_id, needs, use_clock.today_local())
    items = _items(needs, credit, aisles, _shop_for(db, person_id, needs, retailers, credit))
    written = _write(
        db,
        items,
        kind="segment",
        menu_id=menu_id,
        person_id=person_id,
        leftover_person_ids=[person_id],
        clock=use_clock,
    )
    _carry_ticks(db, prior, written, person_id, use_clock)
    return written


def build_from(
    db: Db,
    menu_id: int,
    from_day: dt.date,
    *,
    aisles: Sequence[str] = DEFAULT_AISLES,
    retailers: Sequence[str] = (),
    clock: Clock | None = None,
) -> int:
    """The segment's list again, buying only for the days from `from_day` on. Returns its id.

    cook the meals for Tuesday, Wednesday, Thursday, Friday" - without buying Monday's food.

    The newest list for the menu is kept when it already starts that day. Otherwise a new one is
    written, and it becomes the list everywhere (the newest for its menu). What counts as at home
    is what was there before any list for these days: each of them planned its surplus from food
    that was not bought, so no leftover row from a menu on these days - this one, or one it
    replaced - is counted. Ticks come across by food: what is in the trolley stays ticked.
    """
    menu = db.one(
        "SELECT id, person_id, segment_start, segment_end FROM menus WHERE id = ?", (menu_id,)
    )
    if menu is None:
        raise LookupError(f"no menu with id {menu_id}")
    person_id = int(menu["person_id"])
    start, end = parse_day(str(menu["segment_start"])), parse_day(str(menu["segment_end"]))
    if not start <= from_day <= end:
        raise ValueError(f"{isoday(from_day)} is not one of this list's days")
    wanted = None if from_day == start else isoday(from_day)
    prior = db.one(
        "SELECT id, from_day FROM shopping_lists WHERE menu_id = ? AND kind = 'segment' "
        "ORDER BY id DESC LIMIT 1",
        (menu_id,),
    )
    if prior is not None and (prior["from_day"] or None) == wanted:
        return int(prior["id"])
    use_clock = clock or db.clock
    days = {isoday(d) for d in _days(from_day, end)}
    needs = _needs(
        db,
        [menu_id],
        weekend_only=False,
        pantry=_pantry_for(db, person_id),
        only_days={menu_id: days},
    )
    credit = _credits_for(db, person_id, from_day, retailers, skip_menu_id=menu_id)
    items = _items(needs, credit, aisles, _shop_for(db, person_id, needs, retailers, credit))
    written = _write(
        db,
        items,
        kind="segment",
        menu_id=menu_id,
        person_id=person_id,
        leftover_person_ids=[person_id],
        clock=use_clock,
        from_day=wanted,
    )
    _carry_ticks(db, prior, written, person_id, use_clock)
    new_id = segment_list_id(db, menu_id)
    assert new_id is not None  # just written
    return new_id


def weekend_of_span(start: dt.date) -> tuple[dt.date, dt.date]:
    """The Saturday and Sunday of the weekend a span starting on `start` reaches: its own for a
    Saturday or a Sunday, the next one for a weekday (a Friday-to-Sunday menu's weekend)."""
    if start.weekday() == WEEKEND[1]:
        sat = start - dt.timedelta(days=1)
    else:
        sat = start + dt.timedelta(days=(WEEKEND[0] - start.weekday()) % 7)
    return sat, sat + dt.timedelta(days=1)


def weekend_in_force(
    db: Db, sat: dt.date, sun: dt.date, owners: Collection[int] | None = None
) -> dict[int, set[str]]:
    """menu id -> the weekend days it feeds, read off the plates in force on each day. A Change on
    the Saturday rebuilt Sunday alone, and a day resized by hand is a one-day menu: the list read
    only menus with the weekend's exact dates, so neither ever reached it."""
    out: dict[int, set[str]] = {}
    for r in db.all(
        "SELECT menu_id, day, person_id FROM v_menu_days_current WHERE day BETWEEN ? AND ?",
        (isoday(sat), isoday(sun)),
    ):
        if owners is not None and int(r["person_id"]) not in owners:
            continue
        out.setdefault(int(r["menu_id"]), set()).add(str(r["day"]))
    return out


def _owners_of(db: Db, menu_ids: Sequence[int]) -> list[int]:
    if not menu_ids:
        return []
    marks = ",".join("?" for _ in menu_ids)
    return [
        int(r["person_id"])
        for r in db.all(
            f"SELECT DISTINCT person_id FROM menus WHERE id IN ({marks}) ORDER BY person_id",
            tuple(menu_ids),
        )
    ]


def joint_once(
    db: Db,
    menu_ids: Sequence[int],
    *,
    retailers: Sequence[str] = (),
    clock: Clock | None = None,
    today: dt.date | None = None,
) -> list[ShoppingItem]:
    """Reuse the current joint weekend list or build a new one."""
    span = _menu_span(db, menu_ids[0]) if menu_ids else None
    use_clock = clock or db.clock
    if span is None:
        return joint_weekend(db, menu_ids, retailers=retailers, clock=use_clock)
    sat, sun = weekend_of_span(span[0])
    owners = _owners_of(db, menu_ids)
    joint = _joint_for(db, sat, sun)
    listed = _list_owners(joint) if joint is not None else None
    # still the list: nothing on the weekend decided since, and the same people's food - a list
    # built from his menu alone is not the list once hers is on the weekend too
    if (
        joint is not None
        and not _joint_stale(db, joint, sat, sun)
        and (listed is None or listed == set(owners))
    ):
        return read_back(db, int(joint["id"]))
    # the weekend's own menus: whatever lists were built for them PLANNED surplus from them
    ours = {
        int(r["id"])
        for r in db.all(
            "SELECT id FROM menus WHERE segment_start >= ? AND segment_end <= ?",
            (isoday(sat), isoday(sun)),
        )
    }
    on = today or use_clock.today_local()
    owner_leftovers = {
        pid: current_leftovers(db, pid, on, exclude_menus=ours, only_stated=True) for pid in owners
    }
    written = joint_weekend(
        db, menu_ids, retailers=retailers, clock=use_clock, owner_leftovers=owner_leftovers
    )
    if joint is not None:
        _carry_joint_ticks(db, int(joint["id"]), written, use_clock)
    return written


def _carry_joint_ticks(
    db: Db, old_list_id: int, written: Sequence[ShoppingItem], clock: Clock
) -> None:
    """A tick is a fact about the trolley: what was ticked on a weekend list that has since been
    built again is ticked on the new one, by whoever ticked it."""
    ticked = {
        str(r["code"]): int(r["person_id"])
        for r in db.all(
            "SELECT i.code, t.person_id FROM shopping_ticks t "
            "JOIN shopping_items si ON si.id = t.item_id "
            "JOIN ingredients i ON i.id = si.ingredient_id "
            "WHERE si.list_id = ? AND t.ticked = 1 AND t.id = "
            "(SELECT MAX(x.id) FROM shopping_ticks x WHERE x.item_id = t.item_id)",
            (old_list_id,),
        )
    }
    for item in written:
        if item.code in ticked and item.item_id is not None:
            db.insert(
                "shopping_ticks",
                item_id=item.item_id,
                person_id=ticked[item.code],
                ticked=1,
                created_at=clock.utc_stamp(),
            )


def joint_weekend(
    db: Db,
    menu_ids: Sequence[int],
    *,
    aisles: Sequence[str] = DEFAULT_AISLES,
    retailers: Sequence[str] = (),
    clock: Clock | None = None,
    owner_leftovers: Mapping[int, Mapping[str, float]] | None = None,
) -> list[ShoppingItem]:
    """Combine both people's weekend menus into one shopping list."""
    if not menu_ids:
        raise ValueError("joint_weekend needs at least one menu id")
    first = db.one("SELECT id, person_id, segment_start FROM menus WHERE id = ?", (menu_ids[0],))
    if first is None:
        raise LookupError(f"no menu with id {menu_ids[0]}")
    owners = _owners_of(db, menu_ids) or [int(first["person_id"])]
    if owner_leftovers is None:
        on = clock.today_local() if clock else db.clock.today_local()
        owner_leftovers = {
            owner: current_leftovers(db, owner, on, only_stated=True) for owner in owners
        }
    credits: dict[str, float] = {}
    for owner in owners:
        for code, grams in owner_leftovers.get(owner, {}).items():
            credits[code] = credits.get(code, 0.0) + float(grams)
    # everyone's cupboard, not only the first owner's: a staple either of them keeps is a
    # staple in the one kitchen they cook the weekend in
    pantry: set[str] = set()
    for owner in owners:
        pantry |= _pantry_for(db, owner)
    # each day from the plates in force on it, the owners' own: a Sunday rebuilt on the
    # Saturday, or a day resized by hand, is what that day eats and so what the list buys
    sat, sun = weekend_of_span(parse_day(str(first["segment_start"])))
    in_force = weekend_in_force(db, sat, sun, owners)
    if in_force:
        needs = _needs(db, sorted(in_force), weekend_only=True, pantry=pantry, only_days=in_force)
    else:
        needs = _needs(db, menu_ids, weekend_only=True, pantry=pantry)
    shop = _shop_for(db, int(first["person_id"]), needs, retailers, credits)
    from . import products  # noqa: PLC0415

    # A shared basket can use either person's own product. The first menu's owner takes
    # precedence if both entered one for the same food.
    for owner in reversed(owners):
        shop.update(products.manual_for(db, owner, tuple(needs)))
    items = _items(needs, credits, aisles, shop)
    return _write(
        db,
        items,
        kind="joint_weekend",
        menu_id=int(first["id"]),
        person_id=None,
        leftover_person_ids=owners,
        clock=clock or db.clock,
        owner_credits=owner_leftovers,
    )


#: How long fresh planned surplus stays relevant to cooking prompts. A shopping list credits
#: planned packs only within its own shop window; this age limit does not imply stock at home.
LEFTOVER_DAYS = 10
#: Long-life foods remain relevant to cooking prompts and to stock the person reported.
KEEPS_DAYS = {
    "produce": LEFTOVER_DAYS,
    "meat": LEFTOVER_DAYS,
    "fish": LEFTOVER_DAYS,
    "dairy": LEFTOVER_DAYS,
    "chilled": LEFTOVER_DAYS,
    "bakery": LEFTOVER_DAYS,
    "frozen": 90,
    "tins": 180,
    "grains": 180,
    "condiments": 180,
}


def weekend_menu_ids(db: Db, menu_id: int) -> list[int]:
    """Every menu (both people) sharing exactly this one's segment dates.

    A joint list's `menu_id` foreign key names only whichever person's menu happened to build
    it first (`joint_weekend`'s own docstring); attributing an item to "whose dish is this"
    needs BOTH people's weekend menus, the same way `_joint_for` looks a joint list up by dates
    rather than by owner - the weekend is one basket for two plates, and one foreign key cannot
    name two people (two_people/05).
    """
    span = _menu_span(db, menu_id)
    if span is None:
        return [menu_id]
    start, end = span
    # the menus IN FORCE for those dates, not every row that ever had them: a Change or a
    # rebuild leaves the superseded menu behind with the same dates, and its dishes were being
    # attributed and bought beside the real ones. Read per day
    # since the 27 Sep review, so a Sunday rebuilt on its own, or resized, is found too.
    if all(d.weekday() in WEEKEND for d in _days(start, end)):
        in_force = weekend_in_force(db, *weekend_of_span(start))
        if in_force:
            return sorted(in_force)
    current = [
        int(r["id"])
        for r in db.all(
            "SELECT id FROM v_menu_current WHERE segment_start = ? AND segment_end = ? ORDER BY id",
            (isoday(start), isoday(end)),
        )
    ]
    return current or [menu_id]


from .shopping_lists import (  # noqa: E402
    DISH_SEP,
    RANGE_LAST_WEEKDAY,
    _days,
    _joint_for,
    _joint_stale,
    _list_owners,
    _menu_span,
    close_joint,
    cooks_daily,
    current_leftovers,
    item_readable,
    keeps_days,
    leftover_lines,
    list_owners,
    list_row,
    list_to_show,
    menu_id_on,
    newest_readable,
    other_lists,
    range_basis,
    range_days,
    range_list,
    range_plates,
    readable,
    replaced_by,
    segment_list_id,
    shop_days,
    ticked_uncooked,
    weekend_days,
    weekend_list,
)
from .shopping_render import (  # noqa: E402
    MESSAGE_CHARS,
    RETAILER_NAMES,
    attribution_for,
    forward_text,
    joint_dish_owners,
    list_days,
    list_heading,
    message_parts,
    pack_short,
    render_lines,
    shelf_text,
    shop_name,
    shop_notes,
    short_dish_name,
    total_line,
    weekend_dish_owners,
)

__all__ = [
    "DISH_SEP",
    "KEEPS_DAYS",
    "LEFTOVER_DAYS",
    "MESSAGE_CHARS",
    "RANGE_LAST_WEEKDAY",
    "RETAILER_NAMES",
    "SLACK_TENTHS",
    "WEEKEND",
    "ShoppingItem",
    "aisle_sort_key",
    "at_home",
    "at_home_line",
    "attribution_for",
    "build",
    "build_from",
    "build_once",
    "close_joint",
    "cooks_daily",
    "current_leftovers",
    "for_menu",
    "forward_text",
    "item_readable",
    "joint_dish_owners",
    "joint_once",
    "joint_weekend",
    "keeps_days",
    "leftover_lines",
    "list_days",
    "list_heading",
    "list_owners",
    "list_row",
    "list_to_show",
    "menu_id_on",
    "message_parts",
    "newest_readable",
    "other_lists",
    "pack_short",
    "packs_for",
    "range_basis",
    "range_days",
    "range_list",
    "range_plates",
    "read_back",
    "readable",
    "render_lines",
    "replaced_by",
    "row_sort_key",
    "segment_list_id",
    "shelf_text",
    "shop_days",
    "shop_name",
    "shop_notes",
    "short_dish_name",
    "superseded_list",
    "ticked_uncooked",
    "ticks_for",
    "total_line",
    "weekend_days",
    "weekend_dish_owners",
    "weekend_in_force",
    "weekend_list",
    "weekend_menu_ids",
    "weekend_of_span",
]
