"""Tests for shopping lists across planned days and existing stock."""

from __future__ import annotations

import datetime as dt

from tiffin.food import shopping
from tiffin.jobs import shopping_list
from tiffin.web import queries

from .test_food_shopping import seed_ingredient, seed_recipe
from .test_journey_cook_from_fridge import (
    FRI_11,
    MON_7,
    MON_14,
    SAT_12,
    SUN_6,
    SUN_13,
    _accepted,
    _at,
    _buttons,
    _chicken_dish,
    _chicken_two_weeks,
    _line,
)
from .test_menu import world  # noqa: F401


def test_a_half_used_bag_of_rice_is_on_the_list_again(world) -> None:  # noqa: F811
    """The same Sunday as the old story: last week used 400 g of a 1 kg bag. They check the
    cupboard themselves, so the list names the rice and says nothing about what is left."""
    pid, db, tg, _gw, say, tap, _run = world
    dish = _chicken_dish(db, "Chicken and rice", 180, rice_g=80)
    _accepted(db, pid, MON_7, 5, {"dinner": (dish, 1.0)})
    _at(db, SUN_6)
    say("/shopping")
    for _text, data in _buttons(tg.sent[-1]):
        tap(data)
    _accepted(db, pid, MON_14, 5, {"dinner": (dish, 1.0)})
    _at(db, SUN_13)
    this = say("/shopping")
    assert "Basmati" in this, this
    assert "at home" not in this.lower(), this


def test_the_list_buys_the_whole_week_and_says_nothing_is_at_home(world) -> None:  # noqa: F811
    """The week eats 1000 g of chicken with 250 g planned spare from last week: two packs of
    500 g, not one pack more than nothing - and no line, here or on the page, counting on any."""
    pid, db, say = _chicken_two_weeks(world)
    text = say("/shopping")
    row = db.one(
        "SELECT si.packs, si.credit_g FROM shopping_items si JOIN ingredients i "
        "ON i.id = si.ingredient_id WHERE i.code = 'chicken' ORDER BY si.id DESC LIMIT 1"
    )
    assert row is not None and float(row["credit_g"]) == 0.0 and int(row["packs"]) == 2
    assert "at home" not in text.lower(), text
    page = queries.shopping(db, pid, None)
    assert page["at_home"] == [], page["at_home"]
    chicken = next(i for i in page["items"] if str(i["name"]).startswith("Chicken"))
    assert float(chicken.get("credit_g") or 0) == 0.0
    assert shopping.current_leftovers(db, pid, SUN_13), (
        "the leftovers are still kept: the next menu is still told what to use up first"
    )


def test_the_weekend_list_buys_both_peoples_potatoes(world) -> None:  # noqa: F811
    """Last weekend's 2 kg of potatoes left 800 g; this weekend eats 1600 g together. Two bags,
    and the list leaves the cupboard to them."""
    pid, db, tg, _gw, _say, _tap, run_job = world
    partner = int(db.scalar("SELECT id FROM persons WHERE slug = 'partner'"))
    potato = seed_ingredient(db, "potato", "Potatoes, raw", "produce", 1000, "1 kg bag")
    roast = seed_recipe(db, "Roast chicken and potatoes", 1, {potato: 500})
    _accepted(db, pid, dt.date(2026, 9, 5), 2, {"dinner": (roast, 0.7)})
    _accepted(db, partner, dt.date(2026, 9, 5), 2, {"dinner": (roast, 0.5)})
    _at(db, dt.date(2026, 9, 4), 18)
    run_job(shopping_list.JOINT_JOB, dt.date(2026, 9, 4))
    _accepted(db, pid, SAT_12, 2, {"dinner": (roast, 1.0)})
    _accepted(db, partner, SAT_12, 2, {"dinner": (roast, 0.6)})
    _at(db, FRI_11, 18)
    tg.sent.clear()
    run_job(shopping_list.JOINT_JOB, FRI_11)
    text = tg.sent[-1]["text"]
    assert "2 × 1 kg" in _line(text, "Potatoes"), text  # noqa: RUF001 - the list's own sign
    assert "at home" not in text.lower(), text
