"""A complete week can be shopped and forwarded without a retailer or browser."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest

from tiffin import config
from tiffin.cli import main
from tiffin.db import Db
from tiffin.food import products, shopping, weekplan
from tiffin.telegram.menuflow import send_list_text
from tiffin.web import queries

from .test_dash_shopping_tab import HEADER, SUNDAY, at, open_box  # noqa: F401
from .test_food_shopping import seed_ingredient, seed_menu, seed_recipe

MONDAY = dt.date(2026, 9, 14)


class Outbox:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    def reply(self, person: SimpleNamespace, text: str) -> SimpleNamespace:
        self.sent.append((person.id, text))
        return SimpleNamespace(status="sent")


@pytest.mark.parametrize("household_size", [1, 2])
def test_full_week_from_catalogue_without_retailer(
    fixture_db, two_persons, tmp_path, household_size: int
) -> None:
    cfg = config.load(root=tmp_path / "fresh", environ={})
    assert cfg.get("shop.retailers") == []
    assert cfg.get("shop.retailer") == ""
    chicken = seed_ingredient(fixture_db, "chicken", "Chicken breast", "meat", 500, "500 g pack")
    broccoli = seed_ingredient(fixture_db, "broccoli", "Broccoli", "produce", 350, "350 g head")
    recipe = seed_recipe(fixture_db, "Chicken and broccoli", 1, {chicken: 180, broccoli: 100})
    outbox = Outbox()
    for pid in list(two_persons.values())[:household_size]:
        menu_id = seed_menu(fixture_db, pid, MONDAY, 7, {"dinner": (recipe, 1.0)})
        items = shopping.build(fixture_db, menu_id, retailers=())
        assert {item.code for item in items} == {"chicken", "broccoli"}
        assert [(item.aisle, item.packs) for item in items] == [("produce", 2), ("meat", 3)]
        assert all(item.grams_needed > 0 and not item.product_url for item in items)
        assert all(not item.retailer and item.price_pence is None for item in items)
        list_id = items[0].list_id
        assert list_id is not None
        page_items = queries.shopping(fixture_db, pid, list_id, MONDAY)["items"]
        assert page_items and all("shop_note" not in item for item in page_items)
        assert all(
            item["price_pence"] is None and item["product_url"] is None for item in page_items
        )
        sent = send_list_text(outbox, fixture_db, SimpleNamespace(id=pid), list_id)
        assert sent and all(row.status == "sent" for row in sent)
        forwarded = "\n".join(text for person, text in outbox.sent if person == pid)
        assert "<b>Produce</b>" in forwarded and "<b>Meat</b>" in forwarded
        assert "Broccoli 2 × 350 g" in forwarded and "Chicken breast 3 × 500 g" in forwarded  # noqa: RUF001
        assert "http" not in forwarded and "Not at" not in forwarded and "£" not in forwarded


def test_manual_product_takes_priority_without_a_retailer(fixture_db, two_persons) -> None:
    chicken = seed_ingredient(fixture_db, "chicken", "Chicken breast", "meat", 500, "500 g pack")
    recipe = seed_recipe(fixture_db, "Chicken", 1, {chicken: 180})
    owner, partner = two_persons["owner"], two_persons["partner"]
    products.record_manual(
        fixture_db,
        owner,
        ingredient_code="chicken",
        name="Chicken breast 650 g",
        pack_g=650,
        currency="EUR",
    )
    own = shopping.build(
        fixture_db, seed_menu(fixture_db, owner, MONDAY, 7, {"dinner": (recipe, 1)})
    )
    other = shopping.build(
        fixture_db, seed_menu(fixture_db, partner, MONDAY, 7, {"dinner": (recipe, 1)})
    )
    assert own[0].pack_g == 650 and own[0].packs == 2
    assert own[0].product_name == "Chicken breast 650 g"
    assert own[0].price_pence is None and own[0].retailer == ""
    assert other[0].pack_g == 500 and other[0].product_name == ""


def test_least_waste_uses_catalogue_pack_options(fixture_db, two_persons) -> None:
    owner = two_persons["owner"]
    chicken = seed_ingredient(
        fixture_db,
        "chicken",
        "Chicken breast",
        "meat",
        500,
        "500 g pack",
        pack_sizes_g="[300, 500, 650]",
    )
    weekplan.change(fixture_db, owner, source="operator", least_waste=True)
    assert products.pack_sizes(fixture_db, owner, ["chicken"]) == {"chicken": (300.0, 500.0, 650.0)}
    recipe = seed_recipe(fixture_db, "Chicken", 1, {chicken: 180})
    menu_id = seed_menu(fixture_db, owner, MONDAY, 7, {"dinner": (recipe, 1)})
    item = shopping.build(fixture_db, menu_id)[0]
    assert item.pack_g == 650 and item.packs == 2 and item.leftover_g == 40
    assert item.product_name == "" and item.pack_label == "650 g pack"


def test_manual_product_can_carry_optional_shop_price_and_link(fixture_db, two_persons) -> None:
    owner = two_persons["owner"]
    chicken = seed_ingredient(fixture_db, "chicken", "Chicken breast", "meat", 500, "500 g pack")
    products.record_manual(
        fixture_db,
        owner,
        ingredient_code="chicken",
        name="Chicken breast 500 g",
        pack_g=500,
        price_pence=450,
        shop="Example shop",
        url="https://example.com/chicken",
        currency="EUR",
    )
    recipe = seed_recipe(fixture_db, "Chicken", 1, {chicken: 50})
    item = shopping.build(
        fixture_db, seed_menu(fixture_db, owner, MONDAY, 7, {"dinner": (recipe, 1)})
    )[0]
    assert item.retailer == "Example shop" and item.product_url == "https://example.com/chicken"
    assert shopping.total_line([item]) == "About €4.50."


def test_shared_weekend_uses_either_persons_manual_pack(fixture_db, two_persons) -> None:
    owner, partner = two_persons["owner"], two_persons["partner"]
    chicken = seed_ingredient(fixture_db, "chicken", "Chicken breast", "meat", 500, "500 g pack")
    recipe = seed_recipe(fixture_db, "Chicken", 1, {chicken: 180})
    saturday = MONDAY + dt.timedelta(days=5)
    menus = [
        seed_menu(fixture_db, person, saturday, 2, {"dinner": (recipe, 1)})
        for person in (owner, partner)
    ]
    products.record_manual(
        fixture_db,
        partner,
        ingredient_code="chicken",
        name="Chicken breast 650 g",
        pack_g=650,
    )
    item = shopping.joint_weekend(fixture_db, menus)[0]
    assert item.product_name == "Chicken breast 650 g" and item.pack_g == 650


def test_page_adds_own_product_only(open_box) -> None:  # noqa: F811
    page = open_box(at(SUNDAY, 17, 40))
    item_id = page.get("api/shopping").json()["items"][0]["id"]
    csrf = page.get("api/me").json()["csrf"]
    body = {
        "item_id": item_id,
        "name": "Broccoli 500 g",
        "pack_g": 500,
        "price": "",
        "shop": "",
        "url": "",
    }
    owner = page.client.post(
        "api/shopping/product",
        json=body,
        headers={HEADER: "alex@example.com", "X-CSRF-Token": csrf},
    )
    assert owner.status_code == 200, owner.text
    saved = page.db.one("SELECT person_id, price_pence, shop, url FROM manual_products")
    assert saved is not None and saved["person_id"] == page.owner_id
    assert saved["price_pence"] is None and saved["shop"] is None and saved["url"] is None
    partner_csrf = page.get("api/me", "sam@example.com").json()["csrf"]
    partner = page.client.post(
        "api/shopping/product",
        json=body,
        headers={HEADER: "sam@example.com", "X-CSRF-Token": partner_csrf},
    )
    assert partner.status_code == 404


def test_cli_adds_owner_product_without_shop(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TIFFIN_HOME", str(tmp_path))
    with Db.open(tmp_path / "tiffin.db") as db:
        seed_ingredient(db, "chicken", "Chicken breast", "meat", 500, "500 g pack")
    assert main(["product", "add", "chicken", "Chicken breast 650 g", "--pack-g", "650"]) == 0
    with Db.open(tmp_path / "tiffin.db") as db:
        row = db.one("SELECT name, pack_g, price_pence, shop, url FROM manual_products")
        assert row is not None and row["name"] == "Chicken breast 650 g"
        assert row["pack_g"] == 650 and row["price_pence"] is None
        assert row["shop"] is None and row["url"] is None
