"""Shop from a chosen day: "if I did not shop on Sunday, then I want to
shop on
Monday ... and then cook the meals for Tuesday, Wednesday, Thursday, Friday" - the list again for
the days from the one picked, so nothing is bought for a day that will not be cooked."""

from __future__ import annotations

import datetime as dt

import pytest

from tiffin.db import Db
from tiffin.food import shopping
from tiffin.web import queries

from .conftest import FrozenClock
from .test_dash_shopping_tab import HEADER, OWNER_LOGIN, PARTNER_LOGIN, at
from .test_dash_shopping_tab import open_box as open_box  # noqa: PLC0414 (the fixture)
from .test_food_shopping import seed_ingredient, seed_menu, seed_recipe

MONDAY = dt.date(2026, 9, 28)
TUESDAY = dt.date(2026, 9, 29)


def _week(db: Db, pid: int) -> tuple[int, int]:
    """Mon-Fri, one dinner a day: 250 g chicken, 60 g rice, 60 g eggs. (menu id, eggs id)"""
    chicken = seed_ingredient(db, "chicken", "Chicken breast", "meat", 500, "500 g pack")
    rice = seed_ingredient(db, "rice", "Basmati rice", "grains", 2000, "2 kg bag")
    eggs = seed_ingredient(db, "eggs", "Eggs, whole", "dairy", 360, "6 eggs")
    dish = seed_recipe(db, "Chicken, rice and eggs", 1, {chicken: 250, rice: 60, eggs: 60})
    return seed_menu(db, pid, MONDAY, 5, {"dinner": (dish, 1.0)}), eggs


# Pinned the credit: now runs with `shopping.count_on_home` on, because the default buys everything.
@pytest.mark.usefixtures("count_on_home")
def test_the_list_again_from_tuesday_buys_nothing_for_monday(
    fixture_db: Db, two_persons, frozen_clock: FrozenClock
) -> None:
    db = fixture_db
    pid = two_persons["owner"]
    frozen_clock.set_local(dt.datetime(2026, 9, 27, 18, 0))
    mid, eggs = _week(db, pid)
    db.insert("leftovers", person_id=pid, ingredient_id=eggs, grams=360.0)  # a box from before
    first = shopping.build(db, mid)
    by = {i.code: i for i in first}
    assert by["chicken"].packs == 3 and by["rice"].packs == 1  # 1250 g; 300 g of a 2 kg bag
    db.insert("shopping_ticks", item_id=by["chicken"].item_id, person_id=pid, ticked=1)

    # Monday morning: nothing was bought yesterday, and Monday's dinner is not being cooked
    frozen_clock.set_local(dt.datetime(2026, 9, 28, 8, 0))
    new_id = shopping.build_from(db, mid, TUESDAY)
    assert new_id != first[0].list_id
    got = {i.code: i for i in shopping.read_back(db, new_id)}
    assert got["chicken"].grams_total == 1000.0 and got["chicken"].packs == 2, "four days, not five"
    # the first list's 1700 g of rice was only ever planned as spare: it is not in the cupboard
    assert got["rice"].packs == 1
    # the eggs from before are: 240 g for four days, out of the 360 g box
    assert [(i.code, i.grams_total) for i in shopping.at_home(db, new_id)] == [("eggs", 240.0)]
    ticked = shopping.ticks_for(db, new_id, pid, joint=False)
    assert got["chicken"].item_id in ticked, "what is in the trolley stays ticked"
    assert shopping.build_from(db, mid, TUESDAY) == new_id, "asked again, the same list"

    # and it is the list now: the tab's own pick, and an old message's link, both open it
    page = queries.shopping(db, pid, None, MONDAY, (), db.clock)
    assert page["list"]["id"] == new_id
    assert (
        page["list"]["segment_start"] == "2026-09-29" and page["list"]["from_day"] == "2026-09-29"
    )
    assert page["list"]["days"] == [
        "2026-09-28",
        "2026-09-29",
        "2026-09-30",
        "2026-10-01",
        "2026-10-02",
    ]
    old = queries.shopping(db, pid, first[0].list_id, MONDAY)
    assert old["list"]["id"] == new_id and old["list"]["replaced_id"] == first[0].list_id


def test_only_their_own_list_and_only_its_own_days(
    fixture_db: Db, two_persons, frozen_clock: FrozenClock
) -> None:
    db = fixture_db
    pid = two_persons["owner"]
    frozen_clock.set_local(dt.datetime(2026, 9, 27, 18, 0))
    mid, _eggs = _week(db, pid)
    list_id = shopping.build(db, mid)[0].list_id
    assert list_id is not None
    assert queries.shop_from(db, two_persons["partner"], list_id, TUESDAY) is None, "not her list"
    saturday = dt.date(2026, 10, 3)
    assert queries.shop_from(db, pid, list_id, saturday) is None, "Saturday is not on it"
    assert queries.shop_from(db, pid, list_id, MONDAY) == list_id, "from Monday it IS this list"


def test_the_page_picks_the_day_and_opens_the_list_it_built(open_box) -> None:
    box = open_box(at(dt.date(2026, 9, 14), 8, 0))
    shown = box.get("api/shopping").json()
    assert shown["list"]["from_day"] == "2026-09-14"
    assert len(shown["list"]["days"]) == 5
    csrf = box.get("api/me").json()["csrf"]
    got = box.client.post(
        "api/shopping/from",
        json={"list_id": shown["list"]["id"], "day": "2026-09-15"},
        headers={HEADER: OWNER_LOGIN, "X-CSRF-Token": csrf},
    )
    assert got.status_code == 200, got.text
    again = box.get(f"api/shopping?list={got.json()['list_id']}").json()
    assert again["list"]["segment_start"] == "2026-09-15"
    chicken = next(i for i in again["items"] if i["name"] == "Chicken breast, raw")
    assert chicken["grams_needed"] == 800.0, "four lunches of 200 g, not five"

    partner_csrf = box.get("api/me", PARTNER_LOGIN).json()["csrf"]
    refused = box.client.post(
        "api/shopping/from",
        json={"list_id": shown["list"]["id"], "day": "2026-09-15"},
        headers={HEADER: PARTNER_LOGIN, "X-CSRF-Token": partner_csrf},
    )
    assert refused.status_code == 404, "not from her phone"
