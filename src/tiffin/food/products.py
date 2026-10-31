"""Optional purchase options layered over the generic food catalogue.

Nutrition remains attached to the catalogue food. A product supplies a pack, and may supply
a price, shop and link. Product observations and per-person manual entries are append-only;
shopping-list snapshots keep the pack and price used when they were made.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

from ..clock import Clock
from ..db import Db

#: "other" is the escape hatch for a shop with no search URL configured; everything else
#: must also appear in [shop.search_urls] or the reader has nowhere to look
RETAILERS = ("tesco", "sainsburys", "wholefoods", "marks_spencer", "ocado", "other")
SOURCES = ("chrome", "openfoodfacts", "manual")
#: a label and a laboratory table disagreeing by more than this is worth a human looking
SUSPECT_KCAL_RATIO = 0.35
_PACK = re.compile(r"(\d+(?:\.\d+)?)\s*(kg|g|ml|l)\b", re.I)
ZERO_DECIMAL_CURRENCIES = frozenset(
    {
        "BIF",
        "CLP",
        "DJF",
        "GNF",
        "ISK",
        "JPY",
        "KMF",
        "KRW",
        "PYG",
        "RWF",
        "UGX",
        "VND",
        "VUV",
        "XAF",
        "XOF",
        "XPF",
    }
)
MAX_PRICE_HUNDREDTHS = 10_000_000


def price_decimals(currency: str) -> int:
    """Display precision for supported zero-decimal currencies; other prices use hundredths."""
    return 0 if currency in ZERO_DECIMAL_CURRENCIES else 2


def price_hundredths(value: str, currency: str) -> int:
    """Parse a manual major-unit price into the existing hundredths storage unit."""
    decimals = price_decimals(currency)
    try:
        amount = Decimal(value)
    except InvalidOperation:
        raise ValueError(f"Price must have at most {decimals} decimal places.") from None
    exponent = amount.as_tuple().exponent
    if (
        not amount.is_finite()
        or amount < 0
        or not isinstance(exponent, int)
        or exponent < -decimals
    ):
        raise ValueError(f"Price must be non-negative with at most {decimals} decimal places.")
    if amount > Decimal(MAX_PRICE_HUNDREDTHS) / 100:
        raise ValueError("Price is too large.")
    return int(amount * 100)


def money(minor: int, currency: str = "GBP") -> str:
    symbol = {"GBP": "£", "EUR": "€", "USD": "$"}.get(currency)
    digits = price_decimals(currency) if minor % 100 == 0 else 2
    return f"{symbol or currency + ' '}{minor / 100:.{digits}f}"


@dataclass(frozen=True)
class Product:
    id: int
    ingredient_code: str
    retailer: str
    retailer_sku: str
    name: str
    pack_g: float | None
    pack_label: str | None
    price_pence: int | None
    kcal_100: float | None
    protein_100: float | None
    fat_100: float | None
    carb_100: float | None
    fibre_100: float | None
    url: str | None = None
    currency: str = "GBP"

    @property
    def price(self) -> str:
        return "-" if self.price_pence is None else money(self.price_pence, self.currency)

    @property
    def has_nutrition(self) -> bool:
        return self.kcal_100 is not None and self.protein_100 is not None

    def cost_for(self, grams: float) -> int | None:
        """Pence for this many grams, rounded up to whole packs, or None without a price."""
        if self.price_pence is None or not self.pack_g:
            return None
        packs = max(1, -(-round(grams) // round(self.pack_g)))
        return packs * self.price_pence


def parse_pack_grams(text: str) -> float | None:
    """Grams from a product name's pack size: "... 650G" -> 650, "... 1.5kg" -> 1500.

    Millilitres are taken as grams, which is right for milk and stock and near enough for
    everything else the catalogue holds; oil is the one place it is wrong and oil is pantry.
    """
    best: float | None = None
    for value, unit in _PACK.findall(text or ""):
        grams = float(value) * (1000.0 if unit.lower() in ("kg", "l") else 1.0)
        if 1 <= grams <= 20_000 and (best is None or grams > best):
            best = grams
    return best


def _row_to_product(row) -> Product:
    return Product(
        id=int(row["id"]),
        ingredient_code=str(row["ingredient_code"]),
        retailer=str(row["retailer"]),
        retailer_sku=str(row["retailer_sku"]),
        name=str(row["name"]),
        pack_g=row["pack_g"],
        pack_label=row["pack_label"],
        price_pence=row["price_pence"],
        kcal_100=row["kcal_100"],
        protein_100=row["protein_100"],
        fat_100=row["fat_100"],
        carb_100=row["carb_100"],
        fibre_100=row["fibre_100"],
        url=row["url"],
    )


def record(
    db: Db,
    *,
    ingredient_code: str,
    retailer: str,
    retailer_sku: str,
    name: str,
    source: str,
    clock: Clock | None = None,
    **fields,
) -> int:
    """Append one sighting of a product. A later price is another row, never an edit."""
    if retailer not in RETAILERS:
        raise ValueError(f"retailer must be one of {RETAILERS}")
    if source not in SOURCES:
        raise ValueError(f"source must be one of {SOURCES}")
    clk = clock or db.clock
    pack_g = fields.pop("pack_g", None) or parse_pack_grams(name)
    # a shelf price per kilo is only sometimes on the page; where it is not, it is division,
    # and division in code is fine - it is the model that never does arithmetic
    price = fields.get("price_pence")
    if not fields.get("price_per_kg_pence") and price and pack_g:
        fields["price_per_kg_pence"] = round(float(price) * 1000.0 / float(pack_g))
    return db.insert(
        "products",
        ingredient_code=ingredient_code,
        retailer=retailer,
        retailer_sku=str(retailer_sku)[:80],
        name=name[:200],
        pack_g=pack_g,
        seen_at=clk.utc_stamp(),
        source=source,
        **fields,
    )


def for_ingredient(db: Db, ingredient_code: str, retailer: str = "") -> list[Product]:
    sql = "SELECT * FROM v_products_latest WHERE ingredient_code = ?"
    args: list = [ingredient_code]
    if retailer:
        sql += " AND retailer = ?"
        args.append(retailer)
    order = " ORDER BY price_per_kg_pence IS NULL, price_per_kg_pence, id"
    return [_row_to_product(r) for r in db.all(sql + order, args)]


def choose(db: Db, person_id: int, ingredient_code: str, product_id: int | None) -> int:
    """Say which product this person buys. None means "none, use the catalogue"."""
    return db.insert(
        "product_choices",
        person_id=person_id,
        ingredient_code=ingredient_code,
        product_id=product_id,
    )


def chosen(db: Db, person_id: int) -> dict[str, Product]:
    """ingredient_code -> the product this person buys, for the ones they have chosen."""
    rows = db.all("SELECT * FROM v_product_choice_latest WHERE person_id = ?", (person_id,))
    return {str(r["ingredient_code"]): _row_to_product(r) for r in rows}


def record_manual(
    db: Db,
    person_id: int,
    *,
    ingredient_code: str,
    name: str,
    pack_g: float,
    price_pence: int | None = None,
    shop: str = "",
    url: str = "",
    currency: str = "GBP",
) -> int:
    """Append a person's preferred product. The newest row for a food is used."""
    from . import catalogue  # noqa: PLC0415

    if not any(i.code == ingredient_code for i in catalogue.latest_versions(db)):
        raise ValueError("Choose a food from the catalogue.")
    name, shop, url = name.strip(), shop.strip(), url.strip()
    if not name or len(name) > 200:
        raise ValueError("Product name must be 1-200 characters.")
    if not math.isfinite(pack_g) or not 0 < pack_g <= 20_000:
        raise ValueError("Pack size must be between 0 and 20,000 g.")
    if price_pence is not None and (
        not isinstance(price_pence, int)
        or isinstance(price_pence, bool)
        or price_pence < 0
        or price_pence > MAX_PRICE_HUNDREDTHS
    ):
        raise ValueError("Price must be a non-negative amount in hundredths of the currency.")
    if len(shop) > 80:
        raise ValueError("Shop name must be at most 80 characters.")
    if len(currency) != 3 or not currency.isalpha() or currency != currency.upper():
        raise ValueError("Currency must be a three-letter uppercase code.")
    if url and (len(url) > 1000 or urlsplit(url).scheme != "https" or not urlsplit(url).hostname):
        raise ValueError("Product link must be an HTTPS URL.")
    return db.insert(
        "manual_products",
        person_id=person_id,
        ingredient_code=ingredient_code,
        name=name,
        pack_g=pack_g,
        price_pence=price_pence,
        currency=currency,
        shop=shop or None,
        url=url or None,
        created_at=db.clock.utc_stamp(),
    )


def manual_for(db: Db, person_id: int, codes: Sequence[str] = ()) -> dict[str, Product]:
    """Latest manually entered option per food, scoped to the signed-in person."""
    rows = db.all(
        "SELECT m.* FROM manual_products m WHERE m.person_id = ? AND m.id = "
        "(SELECT MAX(x.id) FROM manual_products x WHERE x.person_id = m.person_id "
        "AND x.ingredient_code = m.ingredient_code)",
        (person_id,),
    )
    return {
        str(r["ingredient_code"]): Product(
            id=int(r["id"]),
            ingredient_code=str(r["ingredient_code"]),
            retailer=str(r["shop"] or ""),
            retailer_sku="",
            name=str(r["name"]),
            pack_g=float(r["pack_g"]),
            pack_label=f"{r['pack_g']:g} g pack",
            price_pence=r["price_pence"],
            kcal_100=None,
            protein_100=None,
            fat_100=None,
            carb_100=None,
            fibre_100=None,
            url=r["url"],
            currency=str(r["currency"]),
        )
        for r in rows
        if not codes or str(r["ingredient_code"]) in codes
    }


#: which reader made a verdict about a shop (`shop_misses.reader`). Bumped whenever the reader's
#: rules change what it would decide: a "not sold here" from an older reader is asked again rather
#: than believed for thirty days. Egg whites were filed "only pasteurised liquid egg white, not
#: raw" and half-fat creme fraiche "could not read" three times, and nothing could reopen either
#:; every miss recorded before this constant existed has no reader at all.
READER_VERSION = "2026-09-27"
#: a person at the shelf, not a reader (`/shopping out`, the page's "Shelf empty?"): their word
#: stands whichever reader is current. Matched with LIKE against `shop_misses.reason`.
PERSON_SAID_LIKE = "the person said%"
#: the page's Undo on "Shelf empty?" (`web.queries.mark_shelf_empty`): written after the person's
#: own mark, and read by `standing()` as taking one mark back
PERSON_TOOK_BACK = "the person said it was there after all, from the page"
#: how many times running a food may be reported 'unread' at one shop before it is left alone
#: like an unstocked one (`jobs.shop_refresh`), and before a list line stops saying "not checked
#: yet" as if a check were coming
UNREAD_TRIES = 3


def standing(alias: str) -> tuple[str, tuple[str, ...]]:
    """Select a shop verdict only while it is still current."""
    a = alias
    same = f"n.retailer = {a}.retailer AND n.ingredient_code = {a}.ingredient_code AND n.id"
    return (
        f"COALESCE({a}.reason, '') <> ? AND NOT ({a}.kind = 'unstocked' "
        f"AND COALESCE({a}.reason, '') LIKE ? AND EXISTS (SELECT 1 FROM shop_misses u "
        f"WHERE u.retailer = {a}.retailer AND u.ingredient_code = {a}.ingredient_code "
        f"AND u.id > {a}.id AND u.reason = ? "
        f"AND (SELECT COUNT(*) FROM shop_misses n WHERE {same} > {a}.id AND n.id <= u.id "
        "AND n.reason = ?) "
        f">= (SELECT COUNT(*) FROM shop_misses n WHERE {same} >= {a}.id AND n.id < u.id "
        "AND n.kind = 'unstocked' AND n.reason LIKE ?)))",
        (PERSON_TOOK_BACK, PERSON_SAID_LIKE, PERSON_TOOK_BACK, PERSON_TOOK_BACK, PERSON_SAID_LIKE),
    )


def for_shopping(
    db: Db,
    person_id: int,
    wanted: dict[str, float],
    retailers: Sequence[str] = (),
    *,
    need: Mapping[str, float] | None = None,
) -> dict[str, Product]:
    """Choose purchase options for the requested catalogue foods.

    Manual entries take priority, then explicit product choices. With no enabled retailer,
    remaining foods use catalogue packs. Enabled retailers are preferred in order; within
    a shop the closest pack to the catalogue size wins unless `need` requests least waste.
    """
    picked = {code: p for code, p in chosen(db, person_id).items() if code in wanted}
    picked.update(manual_for(db, person_id, tuple(wanted)))
    rest = [code for code in wanted if code not in picked]
    if not rest or not retailers:
        return picked
    rows = db.all(
        # A product with no weight still supplies its name; packing uses the catalogue size.
        f"SELECT * FROM v_products_latest WHERE ingredient_code IN ({','.join('?' for _ in rest)})",
        tuple(rest),
    )
    rank = {name: n for n, name in enumerate(retailers)}
    # Existing products at an unlisted shop remain available to configured households.
    far = len(rank)
    by_code: dict[str, list[Product]] = {}
    for row in rows:
        by_code.setdefault(str(row["ingredient_code"]), []).append(_row_to_product(row))
    for code, options in by_code.items():
        assumed = wanted[code]
        grams = float((need or {}).get(code) or 0.0)
        if grams > 0:
            # for someone who would rather pay a little more than throw food away
            # (food/weekplan.py `least_waste`): at the nearest shop that sells it, the pack that
            # leaves least over for what is needed, then the smaller one
            from .shopping import packs_for  # noqa: PLC0415 (shopping imports this module)

            near = min(rank.get(p.retailer, far) for p in options)
            sized = [p for p in options if rank.get(p.retailer, far) == near and p.pack_g]
            if sized:
                picked[code] = min(
                    sized,
                    key=lambda p: (
                        packs_for(grams, float(p.pack_g or 0.0))[1],
                        p.pack_g or 0.0,
                        p.price_pence or 10**9,
                        p.id,
                    ),
                )
                continue
        picked[code] = min(
            options,
            key=lambda p: (
                rank.get(p.retailer, far),
                abs((p.pack_g or 0.0) - assumed),
                p.price_pence or 10**9,
                p.id,
            ),
        )
    return picked


def pack_sizes(
    db: Db, person_id: int, codes: Sequence[str], retailers: Sequence[str] = ()
) -> dict[str, tuple[float, ...]]:
    """code -> every pack size the nearest shop that sells it has, smallest first; a product the
    person chose is the only size it has. Codes no shop was read for are left out."""
    if not codes:
        return {}
    chosen_now = {c: p for c, p in chosen(db, person_id).items() if c in set(codes)}
    chosen_now.update(manual_for(db, person_id, codes))
    out: dict[str, tuple[float, ...]] = {
        c: (float(p.pack_g),) for c, p in chosen_now.items() if p.pack_g
    }
    rest = [c for c in codes if c not in chosen_now]
    if not rest:
        return out
    if retailers:
        marks = ",".join("?" for _ in rest)
        rows = db.all(
            f"SELECT * FROM v_products_latest WHERE ingredient_code IN ({marks})",
            tuple(rest),
        )
        rank = {name: n for n, name in enumerate(retailers)}
        far = len(rank)
        by_code: dict[str, list[Product]] = {}
        for row in rows:
            by_code.setdefault(str(row["ingredient_code"]), []).append(_row_to_product(row))
        for code, options in by_code.items():
            near = min(rank.get(p.retailer, far) for p in options)
            sizes = sorted(
                {float(p.pack_g) for p in options if rank.get(p.retailer, far) == near and p.pack_g}
            )
            if sizes:
                out[code] = tuple(sizes)
    missing = [code for code in rest if code not in out]
    if not missing:
        return out
    for row in db.all(
        f"SELECT i.code, i.pack_sizes_g FROM ingredients i WHERE i.code IN "
        f"({','.join('?' for _ in missing)}) AND i.id = "
        "(SELECT MAX(x.id) FROM ingredients x WHERE x.code = i.code)",
        tuple(missing),
    ):
        code = str(row["code"])
        out[code] = tuple(float(v) for v in json.loads(str(row["pack_sizes_g"])))
    return out


def suspect(product: Product, cofid_kcal_100: float) -> str:
    """Why a product's label and the tables disagree enough to be worth a look, or ''.

    A mismatched product is usually a mis-picked one: cooked chicken matched to raw, or a
    breaded fillet matched to a plain one. Saying so beats silently planning against it.
    """
    if product.kcal_100 is None or cofid_kcal_100 <= 0:
        return ""
    ratio = abs(product.kcal_100 - cofid_kcal_100) / cofid_kcal_100
    if ratio <= SUSPECT_KCAL_RATIO:
        return ""
    return (
        f"{product.name}: {product.kcal_100:.0f} kcal/100 g against the food table's "
        f"{cofid_kcal_100:.0f}. Probably a different form of the food."
    )


__all__ = [
    "RETAILERS",
    "SOURCES",
    "Product",
    "choose",
    "chosen",
    "for_ingredient",
    "for_shopping",
    "manual_for",
    "money",
    "pack_sizes",
    "parse_pack_grams",
    "record",
    "record_manual",
    "suspect",
]
