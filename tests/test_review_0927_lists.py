"""Tests for shopping credit and list explanations."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from tiffin.db import Db
from tiffin.food import products, shopping
from tiffin.model import tools
from tiffin.web import queries

from .conftest import FrozenClock
from .test_food_shopping import seed_ingredient, seed_menu, seed_recipe

MONDAY = dt.date(2026, 9, 28)
FRIDAY = dt.date(2026, 10, 2)
SATURDAY = dt.date(2026, 10, 3)
SUNDAY = dt.date(2026, 10, 4)


# Pinned the credit: now runs with `shopping.count_on_home` on, because the default buys everything.
@pytest.mark.usefixtures("count_on_home")
def test_a_food_counted_as_at_home_is_kept_on_the_list_and_said(
    fixture_db: Db, two_persons
) -> None:
    """26 of the 45 foods on the 26 Sep weekend list were not on it at all: each was credited in
    full from an earlier list nobody ticked, and a fully credited food vanished without a word."""
    db = fixture_db
    pid = two_persons["owner"]
    eggs = seed_ingredient(db, "eggs", "Eggs, whole", "dairy", 360, "6 eggs")
    chicken = seed_ingredient(db, "chicken", "Chicken breast", "meat", 500, "500 g pack")
    dish = seed_recipe(db, "Chicken and eggs", 1, {eggs: 60, chicken: 150})
    mid = seed_menu(db, pid, MONDAY, 2, {"lunch": (dish, 1.0)})

    db.insert(
        "leftovers",
        person_id=pid,
        ingredient_id=eggs,
        grams=360.0,
        created_at="2026-09-27 12:00:00",
    )
    bought = shopping.build(db, mid)
    assert [i.code for i in bought] == ["chicken"], "nothing is bought for the eggs"
    list_id = bought[0].list_id
    assert list_id is not None
    home = shopping.at_home(db, list_id)
    assert [(i.code, i.grams_total, i.packs) for i in home] == [("eggs", 120.0, 0)]
    assert shopping.at_home_line(home) == "Should be at home already: eggs."

    # asked again, it is the same list - one to buy, one at home - and not a second build
    assert [i.code for i in shopping.build_once(db, mid)] == ["chicken"]
    assert db.scalar("SELECT COUNT(*) FROM shopping_lists WHERE menu_id = ?", (mid,)) == 1

    page = queries.shopping(db, pid, list_id)
    assert [i["name"] for i in page["items"]] == ["Chicken breast"]
    assert page["at_home"] == [{"name": "Eggs, whole", "grams": 120.0, "aisle": "dairy"}]


# Pinned the credit: now runs with `shopping.count_on_home` on, because the default buys everything.
@pytest.mark.usefixtures("count_on_home")
def test_a_list_with_everything_at_home_is_built_once(fixture_db: Db, two_persons) -> None:
    """`build_once` asked "is there a list with something to buy", so a list whose every food
    was at home was built again on every ask, each build crediting what the last only planned."""
    db = fixture_db
    pid = two_persons["owner"]
    eggs = seed_ingredient(db, "eggs", "Eggs, whole", "dairy", 360, "6 eggs")
    dish = seed_recipe(db, "Eggs", 1, {eggs: 60})
    mid = seed_menu(db, pid, MONDAY, 2, {"breakfast": (dish, 1.0)})
    db.insert(
        "leftovers",
        person_id=pid,
        ingredient_id=eggs,
        grams=360.0,
        created_at="2026-09-27 12:00:00",
    )
    assert shopping.build_once(db, mid) == []
    assert shopping.build_once(db, mid) == []
    assert db.scalar("SELECT COUNT(*) FROM shopping_lists WHERE menu_id = ?", (mid,)) == 1


# Pinned the credit: now runs with `shopping.count_on_home` on, because the default buys everything.
@pytest.mark.usefixtures("count_on_home")
def test_a_replacement_menu_still_counts_what_the_old_list_had_at_home(
    fixture_db: Db, two_persons, frozen_clock: FrozenClock
) -> None:
    """A replacement reads what the list it replaces BOUGHT. Its at-home line bought nothing, and
    reading it as a line zeroed the eggs' credit: the new list bought eggs already in the fridge."""
    db = fixture_db
    pid = two_persons["owner"]
    eggs = seed_ingredient(db, "eggs", "Eggs, whole", "dairy", 360, "6 eggs")
    chicken = seed_ingredient(db, "chicken", "Chicken breast", "meat", 500, "500 g pack")
    tofu = seed_ingredient(db, "tofu", "Tofu, firm", "chilled", 280, "280 g block")
    old = seed_recipe(db, "Chicken and eggs", 1, {eggs: 60, chicken: 150})
    new = seed_recipe(db, "Tofu and eggs", 1, {eggs: 60, tofu: 140})
    frozen_clock.set_local(dt.datetime(2026, 9, 26, 18, 0))
    first = seed_menu(db, pid, MONDAY, 2, {"lunch": (old, 1.0)})
    db.insert("leftovers", person_id=pid, ingredient_id=eggs, grams=360.0)
    shopping.build(db, first)
    frozen_clock.set_local(dt.datetime(2026, 9, 27, 10, 0))
    second = seed_menu(db, pid, MONDAY, 2, {"lunch": (new, 1.0)})
    bought = shopping.build(db, second)
    assert [i.code for i in bought] == ["tofu"], "the eggs are still at home"


def test_the_weekend_a_span_reaches() -> None:
    assert shopping.weekend_of_span(FRIDAY) == (SATURDAY, SUNDAY)
    assert shopping.weekend_of_span(SATURDAY) == (SATURDAY, SUNDAY)
    assert shopping.weekend_of_span(SUNDAY) == (SATURDAY, SUNDAY)
    assert shopping.weekend_of_span(MONDAY) == (SATURDAY, SUNDAY)


def test_the_weekend_list_follows_a_sunday_rebuilt_on_its_own(
    fixture_db: Db, two_persons, frozen_clock: FrozenClock
) -> None:
    """A Change on the Saturday rebuilds Sunday alone, as a one-day menu. The list read only
    menus with the weekend's exact dates, so it never saw Sunday's new dish; rebuilt, it counted
    its own unshopped surplus as food at home, and every tick on it was lost."""
    db = fixture_db
    owner_id = two_persons["owner"]
    salmon = seed_ingredient(db, "salmon", "Salmon fillets", "fish", 240, "2 fillets")
    beef = seed_ingredient(db, "beef", "Beef mince", "meat", 500, "500 g pack")
    potato = seed_ingredient(db, "potato", "Potatoes", "produce", 2000, "2 kg bag")
    fish = seed_recipe(db, "Salmon and potatoes", 1, {salmon: 150, potato: 300})
    mince = seed_recipe(db, "Beef and potatoes", 1, {beef: 200, potato: 300})
    frozen_clock.set_local(dt.datetime(2026, 10, 2, 18, 0))
    weekend = seed_menu(db, owner_id, SATURDAY, 2, {"dinner": (fish, 1.0)})
    first = shopping.joint_once(db, [weekend])
    assert {i.code: i.grams_total for i in first} == {"salmon": 300.0, "potato": 600.0}
    spuds = next(i for i in first if i.code == "potato")
    db.insert("shopping_ticks", item_id=spuds.item_id, person_id=owner_id, ticked=1)

    frozen_clock.set_local(dt.datetime(2026, 10, 3, 9, 0))
    seed_menu(db, owner_id, SUNDAY, 1, {"dinner": (mince, 1.0)})
    again = shopping.joint_once(db, [weekend])
    by = {i.code: i for i in again}
    assert by["salmon"].grams_total == 150.0, "Saturday's dinner only"
    assert by["beef"].grams_total == 200.0, "Sunday's new dish is on it"
    # not 600 g less the 1400 g the first list only PLANNED to have spare from its own bag
    assert by["potato"].grams_total == 600.0 and by["potato"].packs == 1
    row = shopping.weekend_list(db, SATURDAY)
    assert row is not None and int(row["id"]) == again[0].list_id
    ticked = shopping.ticks_for(db, int(row["id"]), owner_id, joint=True)
    assert by["potato"].item_id in ticked, "the potatoes in the trolley are still in it"
    # asked again with nothing changed, it is the same list
    assert shopping.joint_once(db, [weekend])[0].list_id == again[0].list_id


def test_a_weekend_list_of_their_food_alone_is_their_alone(
    fixture_db: Db, two_persons, frozen_clock: FrozenClock
) -> None:
    """Her build failed and the list was built from his menu only. It was served to her as
    "Weekend, both of you" - his steak night on her Shopping tab, tickable from her phone."""
    db = fixture_db
    owner_id, partner_id = two_persons["owner"], two_persons["partner"]
    steak = seed_ingredient(db, "steak", "Sirloin steak", "meat", 450, "2 steaks")
    dish = seed_recipe(db, "Steak night", 1, {steak: 225})
    frozen_clock.set_local(dt.datetime(2026, 10, 2, 18, 0))
    his = seed_menu(db, owner_id, SATURDAY, 2, {"dinner": (dish, 1.0)})
    seed_menu(db, partner_id, SATURDAY, 2, {"dinner": (dish, 0.8)})
    items = shopping.joint_once(db, [his])
    row = shopping.weekend_list(db, SATURDAY)
    assert row is not None and shopping.list_owners(row) == {owner_id}
    assert shopping.weekend_list(db, SATURDAY, partner_id) is None
    assert items[0].item_id is not None
    assert not queries.tick_item(db, partner_id, items[0].item_id, True), "not from her phone"
    assert queries.shopping(db, partner_id, int(row["id"]))["list"] is None, "nor on her page"
    his_view = queries.shopping(db, owner_id, int(row["id"]))
    assert his_view["list"]["for_both"] is False, "and not 'both of you' on his either"
    assert queries.tick_item(db, owner_id, items[0].item_id, True)
    shown = shopping.list_to_show(db, partner_id, FRIDAY, clock=db.clock)
    assert shown is not None and shown["person_id"] == partner_id, "her own list, for her own food"


def test_an_old_link_to_a_replaced_list_opens_the_list_now_in_force(
    fixture_db: Db, two_persons, frozen_clock: FrozenClock
) -> None:
    """A message's link names the list it was sent with. After a Change that list is for dishes
    nobody is cooking, and the page showed it as if it were still the list."""
    db = fixture_db
    pid = two_persons["owner"]
    chicken = seed_ingredient(db, "chicken", "Chicken breast", "meat", 500, "500 g pack")
    tofu = seed_ingredient(db, "tofu", "Tofu, firm", "chilled", 280, "280 g block")
    old_dish = seed_recipe(db, "Chicken tray", 1, {chicken: 150})
    new_dish = seed_recipe(db, "Tofu tray", 1, {tofu: 140})
    frozen_clock.set_local(dt.datetime(2026, 9, 26, 18, 0))
    first = seed_menu(db, pid, MONDAY, 3, {"dinner": (old_dish, 1.0)})
    old = shopping.build(db, first)[0].list_id
    frozen_clock.set_local(dt.datetime(2026, 9, 27, 10, 0))
    second = seed_menu(db, pid, MONDAY, 3, {"dinner": (new_dish, 1.0)})
    new = shopping.build(db, second)[0].list_id
    assert old is not None and new is not None and new != old

    page = queries.shopping(db, pid, old)
    assert page["list"]["id"] == new and page["list"]["replaced_id"] == old
    assert [i["name"] for i in page["items"]] == ["Tofu, firm"]
    assert queries.shopping(db, pid, new)["list"]["replaced_id"] is None


def test_a_list_first_built_from_chat_uses_the_shops_within_reach(
    fixture_db: Db, two_persons, frozen_clock: FrozenClock
) -> None:
    """ "What do I buy?" in chat could build the week's list before 18:00 - with no shops at all,
    so it named a Tesco pack for somebody whose shops are Sainsbury's and M&S (S11)."""
    db = fixture_db
    pid = two_persons["owner"]
    chicken = seed_ingredient(db, "chicken", "Chicken breast", "meat", 500, "500 g pack")
    dish = seed_recipe(db, "Chicken tray", 1, {chicken: 150})
    frozen_clock.set_local(dt.datetime(2026, 9, 26, 12, 0))
    seed_menu(db, pid, MONDAY, 3, {"dinner": (dish, 1.0)})
    # the Tesco pack is the catalogue's own size, so with no shops named it is the one picked
    products.record(
        db,
        ingredient_code="chicken",
        retailer="tesco",
        retailer_sku="t1",
        name="Tesco Chicken Breast Fillets 500G",
        source="chrome",
        pack_g=500,
        price_pence=450,
    )
    products.record(
        db,
        ingredient_code="chicken",
        retailer="sainsburys",
        retailer_sku="s1",
        name="Sainsbury's British Chicken Breast Fillets 640g",
        source="chrome",
        pack_g=640,
        price_pence=599,
    )
    said = tools.BoundTools(db, pid, retailers=("sainsburys", "marks_spencer")).call(
        "get_shopping", {}
    )
    assert said["list"] is not None and "owner_ids" not in said["list"]
    assert said["at_home"] == []
    shop = db.scalar(
        "SELECT retailer FROM shopping_items WHERE list_id = ? AND packs > 0",
        (said["list"]["id"],),
    )
    assert shop == "sainsburys"


# Pinned the credit: now runs with `shopping.count_on_home` on, because the default buys everything.
@pytest.mark.usefixtures("count_on_home")
def test_a_list_with_nothing_to_buy_still_says_what_should_be_at_home(
    fixture_db: Db, two_persons, frozen_clock: FrozenClock, config
) -> None:
    """It was skipped as "nothing to buy": no message at all, and the eggs it was counting on
    were never checked."""
    from tiffin import scheduler as sc
    from tiffin.app import App
    from tiffin.jobs import shopping_list
    from tiffin.secrets import Secrets

    from .fakes import FakeTelegram

    pid = two_persons["owner"]
    eggs = seed_ingredient(fixture_db, "eggs", "Eggs, whole", "dairy", 360.0, "6 eggs")
    dish = seed_recipe(fixture_db, "Eggs on toast", 1, {eggs: 60.0})
    frozen_clock.set_local(dt.datetime(2026, 9, 11, 9, 0))
    seed_menu(fixture_db, pid, dt.date(2026, 9, 14), 5, {"breakfast": (dish, 1.0)})
    fixture_db.insert("leftovers", person_id=pid, ingredient_id=eggs, grams=360.0)
    fixture_db.insert(
        "profiles",
        person_id=pid,
        effective_from="2026-09-01",
        sex="m",
        birth_date="1990-01-01",
        height_cm=183.0,
        activity_factor=1.5,
        steps_floor=10000,
        quiet_start="22:30",
        quiet_end="07:00",
        confirm_time="20:00",
        morning_time="07:00",
        nudge_time="16:00",
        rate_pct_per_week=0.5,
        kcal_floor=1800,
        segment_days=json.dumps([[0, 1, 2, 3, 4]]),
        meals_per_day=3,
        pre_bed_protein=0,
        tone="neutral",
    )
    secrets_dir = Path(str(fixture_db.path)).parent / "s"
    secrets_dir.mkdir(exist_ok=True)
    (secrets_dir / "telegram-token-dev.txt").write_text("1:t", encoding="utf-8")
    tg = FakeTelegram()
    config.paths.ensure()
    app = App(config, Secrets(secrets_dir), clock=frozen_clock, transport=tg)
    prof = fixture_db.one("SELECT * FROM profiles WHERE person_id = ?", (pid,))
    saturday = dt.date(2026, 9, 12)
    sched = sc.Scheduler(app, [shopping_list.JOB], lambda p, fn: None)
    slots = sc.slots_for(shopping_list.JOB, pid, prof, saturday)
    assert sched.execute(fixture_db, slots[0]) == "ok"
    text = tg.sent[-1]["text"]
    assert "Nothing to buy." in text, text
    assert "Should be at home already: eggs." in text, text
