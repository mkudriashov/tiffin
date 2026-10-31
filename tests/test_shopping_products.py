"""The shopping list buys real packs, and says what the week costs.

shop_refresh has been reading Tesco and Sainsbury's into `products` since it was written, and
its own docstring said "the shopping list prefers a chosen product's real pack" - which nothing
did. The list was still rounding to pack sizes typed in by hand, so a 500 g guess bought three
packs where the actual 650 g pack buys two, and a week of food had no number attached to it.

Nothing here orders anything. A product decides the pack the list rounds to and the price it
totals, and that is the whole of it.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from tiffin.db import Db
from tiffin.food import products, shopping

from .test_food_shopping import seed_ingredient, seed_menu, seed_recipe

MONDAY = dt.date(2026, 9, 14)


@pytest.fixture
def kitchen(fixture_db: Db, two_persons):
    """One food, one recipe, a five-day menu needing 1100 g of it."""
    iid = seed_ingredient(fixture_db, "chk", "Chicken breast", "meat", 500.0, "500 g pack")
    rid = seed_recipe(fixture_db, "Chicken and rice", 1, {iid: 220.0})
    menu_id = seed_menu(fixture_db, two_persons["owner"], MONDAY, 5, {"dinner": (rid, 1.0)})
    return fixture_db, two_persons["owner"], menu_id, iid


def a_product(db: Db, **kw) -> int:
    base = {
        "ingredient_code": "chk",
        "retailer": "tesco",
        "retailer_sku": "123",
        "name": "Tesco British Chicken Breast Fillets 650G",
        "source": "chrome",
        "pack_g": 650.0,
        "pack_label": "650 g pack",
        "price_pence": 490,
    }
    base.update(kw)
    return products.record(db, **base)


def test_without_a_product_nothing_changes(kitchen) -> None:
    db, _pid, menu_id, _iid = kitchen
    items = shopping.build(db, menu_id, retailers=("tesco",))
    assert len(items) == 1
    assert items[0].pack_g == 500.0 and items[0].packs == 3, "1100 g against a 500 g guess"
    assert items[0].price_pence is None and shopping.total_line(items) == ""


def test_a_known_product_supplies_the_pack_and_the_price(kitchen) -> None:
    db, _pid, menu_id, _iid = kitchen
    a_product(db)
    items = shopping.build(db, menu_id, retailers=("tesco",))
    assert items[0].pack_g == 650.0, "the shop's pack, not the hand-typed guess"
    assert items[0].packs == 2, "1100 g is two 650 g packs, not three 500 g ones"
    assert items[0].pack_label == "650 g pack"
    assert items[0].price_pence == 980
    assert shopping.total_line(items) == "About £9.80."


def test_a_chosen_product_beats_the_default(kitchen) -> None:
    db, pid, menu_id, _iid = kitchen
    a_product(db)
    other = a_product(
        db,
        retailer_sku="999",
        name="Tesco Chicken Breast Fillets 1KG",
        pack_g=1000.0,
        pack_label="1 kg pack",
        price_pence=700,
    )
    products.choose(db, pid, "chk", other)
    items = shopping.build(db, menu_id)
    assert items[0].pack_g == 1000.0 and items[0].packs == 2 and items[0].price_pence == 1400


def test_the_default_is_the_closest_pack_not_the_cheapest_per_kilo(kitchen) -> None:
    """Cheapest per kilo is how a week's chicken becomes a five kilogram catering bag."""
    db, pid, _menu_id, _iid = kitchen
    a_product(db)  # 650 g at GBP 4.90 = GBP 7.54/kg
    a_product(db, retailer_sku="bulk", name="Catering Chicken 5KG", pack_g=5000.0, price_pence=2500)
    picked = products.for_shopping(db, pid, {"chk": 500.0}, ("tesco",))
    assert picked["chk"].pack_g == 650.0


def test_the_list_records_what_it_was_built_against(kitchen) -> None:
    """A price that moves must not rewrite an old list."""
    db, _pid, menu_id, _iid = kitchen
    a_product(db)
    shopping.build(db, menu_id, retailers=("tesco",))
    row = db.one("SELECT * FROM shopping_items ORDER BY id DESC LIMIT 1")
    assert row is not None
    assert row["pack_g"] == 650.0 and row["price_pence"] == 980
    assert "Tesco" in str(row["product_name"])
    a_product(db, price_pence=590)  # next Sunday's read
    again = db.one("SELECT price_pence FROM shopping_items ORDER BY id DESC LIMIT 1")
    assert again is not None and again["price_pence"] == 980, "the old list still says GBP 9.80"


def test_a_part_priced_list_says_how_much_of_it_is_priced(kitchen) -> None:
    """Half a total presented as the total is worse than no total."""
    db, pid, _menu_id, chicken = kitchen
    rice = seed_ingredient(db, "ric", "Rice", "grains", 1000.0, "1 kg bag")
    both = seed_recipe(db, "Chicken and rice", 1, {chicken: 220.0, rice: 100.0})
    menu_id = seed_menu(db, pid, MONDAY, 5, {"dinner": (both, 1.0)})
    a_product(db)  # chicken only; nothing has read a shop for rice yet
    items = shopping.build(db, menu_id, retailers=("tesco",))
    assert len(items) == 2
    assert shopping.total_line(items) == "About £9.80 (1 of 2 priced)."


def test_a_product_with_no_price_still_sets_the_pack(kitchen) -> None:
    db, _pid, menu_id, _iid = kitchen
    a_product(db, price_pence=None)
    items = shopping.build(db, menu_id, retailers=("tesco",))
    assert items[0].pack_g == 650.0 and items[0].packs == 2
    assert items[0].price_pence is None and shopping.total_line(items) == ""


def test_a_product_with_no_pack_size_is_ignored_for_rounding(kitchen) -> None:
    db, _pid, menu_id, _iid = kitchen
    a_product(db, name="Tesco Chicken Breast Fillets", pack_g=None, pack_label=None)
    items = shopping.build(db, menu_id)
    assert items[0].pack_g == 500.0, "no pack means the catalogue's guess still stands"


def test_a_price_per_kilo_is_worked_out_when_the_page_did_not_show_one(fixture_db: Db) -> None:
    """/shop orders by price per kilo, and NULLs sort lowest in SQLite: an unpriced product
    was offered first, above one that had a real price."""
    a_product(fixture_db)  # 650 g at GBP 4.90
    row = fixture_db.one("SELECT price_per_kg_pence FROM products ORDER BY id DESC LIMIT 1")
    assert row is not None and row["price_per_kg_pence"] == 754

    a_product(fixture_db, retailer_sku="np", name="Chicken Breast 600G", price_pence=None)
    options = products.for_ingredient(fixture_db, "chk")
    assert options[0].price_pence == 490, "the priced one first"
    assert options[-1].price_pence is None, "the unpriced one last"


def test_the_list_names_the_thing_on_the_shelf(kitchen) -> None:
    """Standing in Tesco looking for "Chicken breast, raw" is the whole complaint.

    `product_name` was already on the item and already stored on the `shopping_items` row.
    `render_lines` built its text from the generic catalogue name and never read it, so a list
    that knew it wanted "Tesco British Chicken Breast Fillets 650G" for GBP 4.90 printed
    "Chicken breast 2 x 500 g".
    """
    db, _pid, menu_id, _iid = kitchen
    a_product(db)
    items = shopping.build(db, menu_id, aisles=("meat",), retailers=("tesco",))
    line = shopping.render_lines(items, aisles=("meat",))[0]
    assert "Tesco British Chicken Breast Fillets 650G" in line
    assert "×2" in line, "the count is still what you put in the basket"  # noqa: RUF001
    assert "£9.80" in line, "and what two of them cost"
    assert "Chicken breast 2" not in line, "the generic name is not what is on the shelf"


def test_a_food_with_no_product_keeps_its_honest_generic_line(kitchen) -> None:
    """A shortened guess is worse than an honest one: with nothing known about the shop, the
    catalogue name and the assumed pack are still the best available answer."""
    db, _pid, menu_id, _iid = kitchen
    items = shopping.build(db, menu_id, aisles=("meat",), retailers=("tesco",))
    line = shopping.render_lines(items, aisles=("meat",))[0]
    assert "Chicken breast 3 × 500 g" in line  # noqa: RUF001
    assert "£" not in line


def test_a_product_with_no_price_is_named_without_inventing_one(kitchen) -> None:
    db, _pid, menu_id, _iid = kitchen
    a_product(db, price_pence=None)
    items = shopping.build(db, menu_id, aisles=("meat",), retailers=("tesco",))
    line = shopping.render_lines(items, aisles=("meat",))[0]
    assert "Tesco British Chicken Breast Fillets 650G ×2" in line  # noqa: RUF001
    assert "£" not in line


def test_the_item_remembers_which_shop_it_was_seen_in(kitchen) -> None:
    """An own-brand name carries the retailer and a branded one does not, so a list drawing on
    two shops cannot say where to go without this."""
    db, _pid, menu_id, _iid = kitchen
    a_product(db, retailer="sainsburys", name="Arla Skyr Natural 450g")
    item = next(
        i
        for i in shopping.build(db, menu_id, aisles=("meat",), retailers=("sainsburys",))
        if i.code == "chk"
    )
    assert item.retailer == "sainsburys"
    assert item.product_name == "Arla Skyr Natural 450g"


def test_every_configured_shop_can_be_stored(fixture_db: Db) -> None:
    """A shop the reader can search must be a shop the schema will accept. The two lists are in
    different files, so nothing else notices when one gains an entry and the other does not."""
    import pathlib

    from tiffin import config as cfg

    seed_ingredient(fixture_db, "chk", "Chicken breast", "meat", 500.0, "500 g pack")
    conf = cfg.load(root=pathlib.Path(fixture_db.path).parent, environ={})
    urls = conf.get("shop.search_urls", {})
    assert set(urls) >= {"tesco", "sainsburys", "wholefoods", "marks_spencer", "ocado"}
    for retailer, url in urls.items():
        assert retailer in products.RETAILERS, f"{retailer} has a URL but the schema refuses it"
        assert "{q}" in url, f"{retailer}: nowhere to put the search terms"
        products.record(
            fixture_db,
            ingredient_code="chk",
            retailer=retailer,
            retailer_sku=f"sku-{retailer}",
            name=f"{retailer} chicken",
            source="chrome",
        )
    stored = {str(r["retailer"]) for r in fixture_db.all("SELECT DISTINCT retailer FROM products")}
    assert stored == set(urls)


def test_a_shop_with_nowhere_to_search_is_refused_not_guessed(tmp_path: Path) -> None:
    """ "other" exists for a product typed in by hand. Sending a browser session at it with no
    URL would have it invent a site, and file whatever it found under the wrong shop."""
    from tiffin import config as cfg
    from tiffin.jobs import shop_refresh

    conf = cfg.load(root=tmp_path, environ={})
    assert shop_refresh.search_url_for(conf, "other") == ""
    assert shop_refresh.search_url_for(conf, "nonsense") == ""
    assert shop_refresh.search_url_for(conf, "marks_spencer").startswith("https://")
