"""Can the menu be bought at Sainsbury's?

"Build a good menu and then understand if Sainsbury's has the produce to support that menu" - the
weekend's pizza was the question. The list's own top-up read six foods at most, a batch that ran
out of budget came back with nothing and was forgotten, and a food the shop does not sell looked
like any other line on the list.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import pytest

from tiffin.errors import TiffinError
from tiffin.food import products, shopping
from tiffin.integrations.retailers import claude_chrome
from tiffin.jobs import shop_refresh

from .test_dash_shopping_tab import SUNDAY, at, open_box  # noqa: F401
from .test_food_shopping import seed_ingredient, seed_menu, seed_recipe
from .test_stories_jobs import MONDAY, Box, box  # noqa: F401


def _menu_of(db: Any, person_id: int, codes: list[str]) -> None:
    ids = {c: seed_ingredient(db, c, c.replace("_", " "), "dairy", 500.0, "500 g") for c in codes}
    rid = seed_recipe(db, "Weekend dish", 1, dict.fromkeys(ids.values(), 100.0))
    seed_menu(db, person_id, MONDAY + dt.timedelta(days=1), 2, {"dinner": (rid, 1.0)})


def _found(db: Any, code: str, name: str = "") -> None:
    products.record(
        db,
        ingredient_code=code,
        retailer="sainsburys",
        retailer_sku=f"s-{code}",
        name=name or f"Sainsbury's {code}",
        source="chrome",
    )


def test_every_food_on_a_coming_menu_is_asked_about_until_it_has_an_answer(
    box: Box,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codes = ["pizza_base", "egg_whites", "eggs", "cottage_cheese", "rump_steak", "creme", "whey"]
    _menu_of(box.db, box.owner, codes)
    _found(box.db, "eggs")
    asked: list[list[str]] = []

    class Reader:
        def __init__(self, *_a: Any, **_k: Any) -> None: ...

        def run_json(self, prompt: str) -> tuple[dict, dict]:
            foods = [f["code"] for f in json.loads(prompt)["foods"]]
            asked.append(foods)
            return {
                "products": [
                    {"code": "pizza_base", "retailer_sku": "1", "name": "Sainsbury's Pizza Bases"}
                ],
                "not_found": [
                    {"code": c, "kind": "unstocked", "reason": "not sold"}
                    for c in foods
                    if c != "pizza_base"
                ],
            }, {}

    monkeypatch.setattr(claude_chrome, "ChromeJob", Reader)
    # a household that buys its whey somewhere else, as `shop.bought_elsewhere` says
    monkeypatch.setattr(shop_refresh, "bought_elsewhere", lambda _config: frozenset({"whey"}))
    elsewhere = shop_refresh.bought_elsewhere(box.app.config)
    todo = shop_refresh.unanswered(box.db, "sainsburys", MONDAY, elsewhere)
    assert "eggs" not in todo, "found already"
    assert "whey" not in todo, "bought elsewhere by choice"
    assert len(todo) == 5
    assert box.run(shop_refresh.CHECK_JOB, MONDAY) == "ok"
    assert asked == [todo], "one batch, of exactly the foods still without an answer"
    assert shop_refresh.unanswered(box.db, "sainsburys", MONDAY, elsewhere) == []
    assert box.run(shop_refresh.CHECK_JOB, MONDAY, index=1) == "skipped", "nothing left to ask"
    assert len(asked) == 1, "and no browser for it"


def test_a_batch_that_comes_back_empty_is_asked_again_three_times_at_most(
    box: Box,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """26 Sep: the first of three batches ran out of budget and handed back nothing at all. Its
    foods have to be asked again - but not every hour for ever."""
    _menu_of(box.db, box.owner, ["pizza_base"])
    calls: list[str] = []

    class Empty:
        def __init__(self, *_a: Any, **_k: Any) -> None: ...

        def run_json(self, prompt: str) -> tuple[dict, dict]:
            calls.append(prompt)
            raise TiffinError("the browser session returned no result object: ran out")

    monkeypatch.setattr(claude_chrome, "ChromeJob", Empty)
    for hour in range(4):
        box.run(shop_refresh.CHECK_JOB, MONDAY, index=hour)
    assert len(calls) == shop_refresh.UNREAD_TRIES, "asked again, then left alone"
    note, status = shopping.shop_notes(box.db, ["pizza_base"], "sainsburys")["pizza_base"]
    # and the list says the shop could not be read for it - not "not checked yet", as if a
    # check were still coming
    assert status == "unread" and "(3 tries)" in note, (status, note)


def test_a_product_reported_from_the_search_results_is_kept_without_its_id(
    box: Box,  # noqa: F811
) -> None:
    """26 Sep: product pages errored, so the session reported what the results list showed - with
    no product id, and `store` dropped every one. The link or the name keys it instead."""
    for code in ("rump_steak", "egg_whites", "creme"):
        seed_ingredient(box.db, code, code, "meat", 500.0, "500 g")
    ctx: Any = type("Ctx", (), {"db": box.db, "clock": box.db.clock})()
    url = "https://www.sainsburys.co.uk/gol-ui/product/sainsburys-rump-steak-225g"
    written, _notes = shop_refresh.store(
        ctx,
        "sainsburys",
        [
            {"code": "rump_steak", "name": "Sainsbury's Rump Steak 225g", "url": url},
            {"code": "egg_whites", "name": "Two Chicks Liquid Egg White 500g"},
            {"code": "creme", "retailer_sku": "", "url": "", "name": ""},
        ],
    )
    assert written == 2, "a product with no id is kept; one with no name is not a product"
    skus = {
        str(r["ingredient_code"]): str(r["retailer_sku"])
        for r in box.db.all("SELECT ingredient_code, retailer_sku FROM products")
    }
    assert skus["rump_steak"] == "sainsburys-rump-steak-225g"
    assert skus["egg_whites"] == "name:two chicks liquid egg white 500g"


def test_the_list_says_what_sainsburys_said_about_each_food(box: Box) -> None:  # noqa: F811
    db = box.db
    for code in ("pizza_base", "cottage_cheese", "egg_whites", "tofu", "whey"):
        seed_ingredient(db, code, code, "dairy", 500.0, "500 g")
    _found(db, "pizza_base", "Sainsbury's Pizza Bases x2 300g")
    ctx: Any = type("Ctx", (), {"db": db, "clock": db.clock})()
    shelf = "the person said the shelf was empty, from the page"
    shop_refresh.record_misses(
        ctx,
        "sainsburys",
        [
            {"code": "cottage_cheese", "kind": "unstocked", "reason": shelf},
            {"code": "egg_whites", "kind": "unstocked", "reason": "not sold here"},
        ],
    )
    codes = ["pizza_base", "cottage_cheese", "egg_whites", "tofu", "whey"]
    notes = shopping.shop_notes(db, codes, "sainsburys", {"whey"})
    assert notes["pizza_base"] == ("At Sainsbury's: Sainsbury's Pizza Bases x2 300g", "found")
    assert notes["cottage_cheese"] == ("The Sainsbury's shelf was empty last time", "sold_out")
    assert notes["egg_whites"] == ("Not at Sainsbury's", "not_sold")
    assert notes["tofu"] == ("Not checked at Sainsbury's yet", "unchecked")
    assert "whey" not in notes, "bought elsewhere is not a gap"


def test_the_newer_answer_about_a_food_is_the_one_on_the_list(box: Box) -> None:  # noqa: F811
    """ "Shelf empty?" was hidden for good behind "At Sainsbury's: ..." once the shop had ever been
    seen selling the food: whichever answer is newer is the one that is said."""
    db = box.db
    seed_ingredient(db, "potatoes", "potatoes", "produce", 2000.0, "2 kg")
    _found(db, "potatoes", "Sainsbury's White Potatoes 2kg")
    ctx: Any = type("Ctx", (), {"db": db, "clock": db.clock})()
    box.clock.advance(minutes=5)
    shop_refresh.record_misses(
        ctx,
        "sainsburys",
        [
            {
                "code": "potatoes",
                "kind": "unstocked",
                "reason": "the person said the shelf was empty",
            }
        ],
    )
    assert shopping.shop_notes(db, ["potatoes"], "sainsburys")["potatoes"][1] == "sold_out"
    box.clock.advance(minutes=5)
    _found(db, "potatoes", "Sainsbury's White Potatoes 2kg")
    assert shopping.shop_notes(db, ["potatoes"], "sainsburys")["potatoes"][1] == "found"
    # and the window is Tiffin's clock, not SQLite's: 31 days on, the miss is past it
    box.clock.advance(minutes=5)
    shop_refresh.record_misses(
        ctx, "sainsburys", [{"code": "potatoes", "kind": "unstocked", "reason": "not sold"}]
    )
    box.clock.advance(days=31)
    assert shopping.shop_notes(db, ["potatoes"], "sainsburys")["potatoes"][1] == "found"


def test_the_page_puts_it_on_every_line_without_a_sainsburys_product(open_box) -> None:  # noqa: F811
    page = open_box(at(SUNDAY, 18, 20))
    items = page.get("api/shopping").json()["items"]
    theirs = [i for i in items if i.get("retailer") != "sainsburys"]
    assert theirs, "setup: some lines name another shop's product or none"
    assert all("Sainsbury's" in str(i.get("shop_note")) for i in theirs), theirs
    assert all("shop_note" not in i for i in items if i.get("retailer") == "sainsburys")
    render = page.render_shopping()
    assert "item.shop_note" in render and '"shop-note' in render.replace("`", '"')
