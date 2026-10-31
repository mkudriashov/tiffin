"""Tests for current shop reader verdicts and product selection."""

from __future__ import annotations

import datetime as dt
from typing import Any, cast

from tiffin.db import Db
from tiffin.food import products, shopping
from tiffin.jobs import shop_refresh
from tiffin.scheduler import JobContext

from .conftest import FrozenClock
from .test_lived_backfill import _codes, _ctx, _pantry_and_foods


def _miss(db: Db, code: str, reason: str, *, kind: str = "unstocked", reader: Any = None) -> None:
    db.insert(
        "shop_misses",
        ingredient_code=code,
        retailer="sainsburys",
        reason=reason,
        kind=kind,
        seen_at=db.clock.utc_stamp(),
        reader=reader,
    )


def test_a_verdict_from_an_older_reader_is_asked_again(
    fixture_db: Db, two_persons, frozen_clock: FrozenClock
) -> None:
    """Egg whites were filed "only pasteurised liquid egg white, not raw", and nothing could
    reopen it for thirty days: the verdict was trusted whatever rules had made it."""
    _pantry_and_foods(fixture_db)
    ctx = _ctx(fixture_db, frozen_clock)
    _miss(fixture_db, "apples", "only sold as a bag of six")  # no reader: before the column
    shop_refresh.record_misses(ctx, "sainsburys", [{"code": "almonds", "reason": "not sold"}])
    queue = _codes(shop_refresh.stale_foods(ctx, "sainsburys", limit=50))
    assert "apples" in queue, "asked again under today's rules"
    assert "almonds" not in queue, "today's reader said no: that stands"
    notes = shopping.shop_notes(fixture_db, ["apples", "almonds"], "sainsburys")
    assert notes["apples"] == ("Not checked at Sainsbury's yet", "unchecked")
    assert notes["almonds"] == ("Not at Sainsbury's", "not_sold")


def test_a_person_at_the_shelf_is_believed_whatever_the_reader(
    fixture_db: Db, two_persons, frozen_clock: FrozenClock
) -> None:
    """ "/shopping out chickpeas and I will stop asking" - a person's row has no reader, and a
    reader filter that dropped it would have had the refresh asking again the next hour."""
    _pantry_and_foods(fixture_db)
    ctx = _ctx(fixture_db, frozen_clock)
    _miss(fixture_db, "skyr", "the person said the shelf was empty")
    assert "skyr" not in _codes(shop_refresh.stale_foods(ctx, "sainsburys", limit=50))
    note = shopping.shop_notes(fixture_db, ["skyr"], "sainsburys")["skyr"]
    assert note == ("The Sainsbury's shelf was empty last time", "sold_out")


def test_only_a_person_cancels_food_at_home(
    fixture_db: Db, two_persons, frozen_clock: FrozenClock
) -> None:
    """The reader's own "not sold here" is a judgement about a web page. It cancelled a 380 g
    carton of egg whites at home, and would have cancelled the whey tub."""
    ids = _pantry_and_foods(fixture_db)
    pid = two_persons["owner"]
    fixture_db.insert("leftovers", person_id=pid, ingredient_id=ids["skyr"], grams=380.0)
    frozen_clock.set_local(dt.datetime(2026, 9, 8, 12, 0))
    _miss(fixture_db, "skyr", "only the flavoured one", reader=products.READER_VERSION)
    today = dt.date(2026, 9, 8)
    assert shopping.current_leftovers(fixture_db, pid, today) == {"skyr": 380.0}
    frozen_clock.set_local(dt.datetime(2026, 9, 8, 13, 0))
    _miss(fixture_db, "skyr", "the person said the shelf was empty, from the page")
    assert shopping.current_leftovers(fixture_db, pid, today) == {}


def test_cooked_figures_are_asked_about_three_times_and_then_left(
    fixture_db: Db, two_persons, frozen_clock: FrozenClock
) -> None:
    """The shop has it; its page gives figures only for the cooked food. Asked again, because
    the next read may store the product (16 Sep) - but not every hour for ever."""
    _pantry_and_foods(fixture_db)
    ctx = _ctx(fixture_db, frozen_clock)
    cooked = [{"code": "apples", "reason": "nutrition label shows cooked values, not raw"}]
    for n in range(products.UNREAD_TRIES):
        assert "apples" in _codes(shop_refresh.stale_foods(ctx, "sainsburys", limit=50)), n
        shop_refresh.record_misses(ctx, "sainsburys", cooked)
    assert "apples" not in _codes(shop_refresh.stale_foods(ctx, "sainsburys", limit=50))
    note = shopping.shop_notes(fixture_db, ["apples"], "sainsburys")["apples"]
    assert note == ("At Sainsbury's (its page gives cooked figures only)", "found")


def test_a_food_bought_elsewhere_is_never_read(
    fixture_db: Db, two_persons, frozen_clock: FrozenClock
) -> None:
    """The owner's own whey: the Sunday read still queued it, and its "not sold" then cancelled
    the tub
    at home."""
    _pantry_and_foods(fixture_db)
    app = type("App", (), {"config": {"shop.bought_elsewhere": ["skyr"]}})()
    ctx = cast(
        "JobContext",
        type("Ctx", (), {"db": fixture_db, "clock": frozen_clock, "app": app})(),
    )
    assert "skyr" not in _codes(shop_refresh.stale_foods(ctx, "sainsburys", limit=50))
    assert "skyr" in _codes(
        shop_refresh.stale_foods(_ctx(fixture_db, frozen_clock), "sainsburys", limit=50)
    )


def test_a_session_that_never_got_going_is_not_a_session_that_fell_short() -> None:
    """Only a session that ran and stopped short parks its foods as unread: a usage limit, a
    lost login or a disconnected extension says nothing about any food."""
    assert shop_refresh.session_fell_short("ran out of turns before the last three foods")
    assert shop_refresh.session_fell_short("the browser session returned no result object")
    assert not shop_refresh.session_fell_short("Claude usage limit reached")
    assert not shop_refresh.session_fell_short("Chrome extension not connected")
    assert not shop_refresh.session_fell_short("ft.com says: please log in")


def test_a_product_with_no_weight_on_its_label_is_still_the_shops(
    fixture_db: Db, two_persons
) -> None:
    """Eggs by the dozen have no grams in the name. Leaving such products out sent the eggs to
    Ocado while the line underneath said "At Sainsbury's: Sainsbury's Free Range Eggs x12"."""
    pid = two_persons["owner"]
    products.record(
        fixture_db,
        ingredient_code="eggs",
        retailer="sainsburys",
        retailer_sku="e12",
        name="Sainsbury's Free Range Eggs x12",
        source="chrome",
        price_pence=390,
    )
    products.record(
        fixture_db,
        ingredient_code="eggs",
        retailer="ocado",
        retailer_sku="o6",
        name="Ocado Free Range Eggs 6 x 58g 348g",
        source="chrome",
        pack_g=348,
        price_pence=250,
    )
    got = products.for_shopping(fixture_db, pid, {"eggs": 348.0}, ["sainsburys", "ocado"])
    assert got["eggs"].retailer == "sainsburys"
