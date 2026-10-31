"""Real supermarket products behind the generic foods: pack sizes, prices and label nutrition.

CoFID is right for a raw whole food and cannot say what a pack costs. These tests are mostly
about not letting a product quietly replace a food it is not.
"""

from __future__ import annotations

import sqlite3

import pytest

from tiffin.db import Db
from tiffin.food import products
from tiffin.jobs import shop_refresh


def test_manual_price_precision_preserves_hundredths_storage() -> None:
    assert products.price_hundredths("4.50", "GBP") == 450
    assert products.price_hundredths("450", "JPY") == 45_000
    assert products.money(45_000, "JPY") == "JPY 450"
    assert products.money(450, "GBP") == "£4.50"
    assert products.money(45_050, "JPY") == "JPY 450.50"  # older snapshot stays legible
    with pytest.raises(ValueError, match="0 decimal places"):
        products.price_hundredths("450.50", "JPY")
    with pytest.raises(ValueError, match="2 decimal places"):
        products.price_hundredths("4.501", "GBP")
    with pytest.raises(ValueError, match="too large"):
        products.price_hundredths("1e999999", "GBP")


@pytest.mark.parametrize(
    ("name", "grams"),
    [
        ("Tesco British Chicken Breast Fillets 650G", 650.0),
        ("Tesco Chicken Breast Fillet 1Kg", 1000.0),
        ("Butchers Choice Chicken Fillet 1.2 kg", 1200.0),
        ("Milk, semi-skimmed 2L", 2000.0),
        ("Tesco Chicken Breast Fillets 320g", 320.0),
        ("Tesco Finest 2 Cornfed Chicken Breast Fillets", None),
    ],
)
def test_pack_size_is_read_out_of_the_product_name(name, grams) -> None:
    assert products.parse_pack_grams(name) == grams


def test_the_largest_number_wins_when_a_name_carries_two() -> None:
    """ "6 x 58g eggs, 348g" should buy the pack, not one egg."""
    assert products.parse_pack_grams("Free Range Eggs 6 x 58g 348g") == 348.0


def test_a_price_covers_whole_packs_only(fixture_db: Db) -> None:
    p = products.Product(
        id=1,
        ingredient_code="chicken",
        retailer="tesco",
        retailer_sku="1",
        name="Chicken 650G",
        pack_g=650.0,
        pack_label="650 g",
        price_pence=490,
        kcal_100=115.0,
        protein_100=21.5,
        fat_100=3.3,
        carb_100=None,
        fibre_100=None,
    )
    assert p.cost_for(650) == 490
    assert p.cost_for(700) == 980, "you cannot buy 1.08 packs"
    assert p.cost_for(1300) == 980
    assert p.price == "£4.90"


def test_a_product_with_no_price_costs_nothing_knowable() -> None:
    p = products.Product(
        id=1,
        ingredient_code="x",
        retailer="tesco",
        retailer_sku="1",
        name="X 100g",
        pack_g=100.0,
        pack_label=None,
        price_pence=None,
        kcal_100=None,
        protein_100=None,
        fat_100=None,
        carb_100=None,
        fibre_100=None,
    )
    assert p.cost_for(500) is None and p.price == "-"
    assert not p.has_nutrition


def test_a_product_far_from_the_food_table_is_flagged_not_used_silently() -> None:
    """A mismatch is usually a mis-picked product: breaded chicken matched to a plain fillet."""
    plain = products.Product(
        id=1,
        ingredient_code="chicken",
        retailer="tesco",
        retailer_sku="1",
        name="Chicken Breast Fillets 650G",
        pack_g=650.0,
        pack_label=None,
        price_pence=490,
        kcal_100=115.0,
        protein_100=21.5,
        fat_100=3.3,
        carb_100=None,
        fibre_100=None,
    )
    breaded = products.Product(
        **{**plain.__dict__, "name": "Southern Fried Fillets", "kcal_100": 240.0}
    )
    assert products.suspect(plain, 106.0) == "", "a label near the table is fine"
    why = products.suspect(breaded, 106.0)
    assert "different form" in why and "240" in why


def test_recording_a_product_is_append_only_and_the_newest_wins(fixture_db: Db) -> None:
    first = products.record(
        fixture_db,
        ingredient_code="chicken",
        retailer="tesco",
        retailer_sku="294007923",
        name="Tesco British Chicken Breast Fillets 650G",
        source="chrome",
        price_pence=490,
        kcal_100=115.0,
        protein_100=21.5,
    )
    products.record(
        fixture_db,
        ingredient_code="chicken",
        retailer="tesco",
        retailer_sku="294007923",
        name="Tesco British Chicken Breast Fillets 650G",
        source="chrome",
        price_pence=520,
        kcal_100=115.0,
        protein_100=21.5,
    )
    latest = products.for_ingredient(fixture_db, "chicken")
    assert len(latest) == 1, "one row per SKU, the newest"
    assert latest[0].price_pence == 520
    assert latest[0].pack_g == 650.0, "the pack size came out of the name"
    with pytest.raises(sqlite3.IntegrityError):
        fixture_db.execute("UPDATE products SET price_pence = 1 WHERE id = ?", (first,))


def test_a_bad_retailer_or_source_is_refused(fixture_db: Db) -> None:
    base = {
        "ingredient_code": "x",
        "retailer": "tesco",
        "retailer_sku": "1",
        "name": "X 100g",
        "source": "chrome",
    }
    for bad in ({"retailer": "aldi"}, {"source": "guessed"}):
        merged: dict = {**base, **bad}
        with pytest.raises(ValueError):
            products.record(fixture_db, **merged)


def test_choosing_a_product_is_per_person_and_reversible(fixture_db: Db, two_persons) -> None:
    pid, partner = two_persons["owner"], two_persons["partner"]
    a = products.record(
        fixture_db,
        ingredient_code="chicken",
        retailer="tesco",
        retailer_sku="1",
        name="Chicken 650G",
        source="chrome",
        price_pence=490,
    )
    b = products.record(
        fixture_db,
        ingredient_code="chicken",
        retailer="tesco",
        retailer_sku="2",
        name="Chicken 1Kg",
        source="chrome",
        price_pence=700,
    )
    products.choose(fixture_db, pid, "chicken", a)
    assert products.chosen(fixture_db, pid)["chicken"].id == a
    assert "chicken" not in products.chosen(fixture_db, partner), "her choice is her own"
    products.choose(fixture_db, pid, "chicken", b)
    assert products.chosen(fixture_db, pid)["chicken"].id == b, "changing your mind is a new row"
    products.choose(fixture_db, pid, "chicken", None)
    assert "chicken" not in products.chosen(fixture_db, pid), "back to the catalogue"


# ---------------------------------------------------------------- the weekly job
def test_the_job_is_weekly_and_never_runs_on_the_message_path() -> None:
    assert shop_refresh.JOB.days == frozenset({6})
    assert shop_refresh.JOB.kind == "data", "a dry run must never drive a browser"
    assert shop_refresh.JOB.times(None, __import__("datetime").date(2026, 9, 13)) == ["05:00"]


def test_a_product_the_session_invented_for_an_unknown_food_is_dropped() -> None:
    """The model returns codes from the list it was given; anything else is not stored."""
    import inspect

    body = inspect.getsource(shop_refresh.store)
    assert "known.get(code)" in body and "if item is None" in body


def test_blank_nutrition_stays_blank() -> None:
    assert shop_refresh._num(None) is None
    assert shop_refresh._num(True) is None, "a boolean is not a measurement"
    assert shop_refresh._num(0) == 0.0, "a real zero survives"
    assert shop_refresh._int(4.6) == 5
