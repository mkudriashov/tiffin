"""The shop queue, stood in front of: a menu changes and the list has to name real food.

The weekly refresh reads twelve foods a Sunday out of eighty-five. WHICH twelve is the whole
question, and it was answered alphabetically. Three things follow from that, and each one is a
person standing somewhere with the wrong piece of paper:

- a menu built on Saturday introducing a food late in the alphabet waits seven Sundays for its
  turn, and until then its line reads "Skyr, natural 3 x 450 g" instead of a tub;
- her catalogue is not his, so a queue that has already walked past a letter will not come back
  to it for the person who needed it;
- a food the shop simply does not stock sits at the front of that queue being asked for and
  failing, once a week, forever, and takes one of the twelve slots with it every time.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any, cast

from tiffin.db import Db
from tiffin.food import products, shopping
from tiffin.jobs import shop_refresh
from tiffin.scheduler import JobContext

from .test_food_shopping import seed_ingredient, seed_menu, seed_recipe


def _ctx(db: Db, clock: Any = None) -> JobContext:
    """The two attributes `stale_foods` and `record_misses` actually touch."""
    return cast("JobContext", type("Ctx", (), {"db": db, "clock": clock or db.clock})())


def _codes(rows: list[dict[str, Any]]) -> list[str]:
    return [str(r["code"]) for r in rows]


def _pantry_and_foods(db: Db) -> dict[str, int]:
    """A catalogue whose alphabet and whose menu disagree, which is the ordinary case."""
    return {
        "almonds": seed_ingredient(db, "almonds", "Almonds", "grains", 200.0, "200 g"),
        "apples": seed_ingredient(db, "apples", "Apples", "produce", 600.0, "6 pack"),
        "beans": seed_ingredient(db, "beans", "Black beans", "tins", 400.0, "400 g tin"),
        "cocoa": seed_ingredient(db, "cocoa", "Cocoa", "condiments", 250.0, "250 g"),
        "skyr": seed_ingredient(db, "skyr", "Skyr, natural", "dairy", 450.0, "450 g tub"),
        "tofu": seed_ingredient(db, "tofu", "Tofu, firm", "chilled", 280.0, "280 g block"),
        "oil": seed_ingredient(db, "oil", "Olive oil", "condiments", 500.0, "500 ml", pantry=1),
    }


# --------------------------------------------------------------- 1. the menu changed on Thursday
def test_a_food_a_new_menu_introduced_goes_to_the_front_of_the_queue(
    fixture_db: Db, two_persons
) -> None:
    """She replaces a dish on Thursday. The new dish wants tofu and skyr.

    Alphabetically those are sixth and fifth of six, so a twelve-a-week walk that has just done
    A-B reaches them in a month. The list goes out tomorrow. What she needs read is not the
    front of the alphabet, it is the front of HER menu.
    """
    ids = _pantry_and_foods(fixture_db)
    rid = seed_recipe(fixture_db, "Tofu bowl", 1, {ids["tofu"]: 200.0, ids["skyr"]: 150.0})
    seed_menu(
        fixture_db,
        two_persons["partner"],
        dt.date(2026, 9, 14),
        3,
        {"dinner": (rid, 1.0)},
    )

    rows = shop_refresh.stale_foods(_ctx(fixture_db), "sainsburys", limit=4)
    assert _codes(rows)[:2] == ["skyr", "tofu"], (
        "the foods on a current menu lead; alphabetically they are last"
    )
    assert "oil" not in _codes(rows), "pantry staples never reach a shopping list"


def test_partner_menu_and_their_are_both_at_the_front(fixture_db: Db, two_persons) -> None:
    """Two people, two catalogues, one queue. Favouring one leaves the other reading generic
    lines for a month, so the priority set is the union, not whoever sorts first."""
    ids = _pantry_and_foods(fixture_db)
    his = seed_recipe(fixture_db, "Bean chilli", 1, {ids["beans"]: 300.0})
    partners = seed_recipe(fixture_db, "Tofu bowl", 1, {ids["tofu"]: 200.0})
    seed_menu(fixture_db, two_persons["owner"], dt.date(2026, 9, 14), 2, {"dinner": (his, 1.0)})
    seed_menu(
        fixture_db, two_persons["partner"], dt.date(2026, 9, 14), 2, {"dinner": (partners, 1.0)}
    )

    lead = set(_codes(shop_refresh.stale_foods(_ctx(fixture_db), "sainsburys", limit=2)))
    assert lead == {"beans", "tofu"}, "both people's menus lead, not one of them"


def test_only_a_current_menu_counts_not_one_that_was_superseded(
    fixture_db: Db, two_persons
) -> None:
    """A replaced menu's foods are not what anybody is shopping for. `v_menu_current` already
    knows which menu won; the queue has to read the same answer or it prioritises last week."""
    ids = _pantry_and_foods(fixture_db)
    old = seed_recipe(fixture_db, "Old dish", 1, {ids["cocoa"]: 30.0})
    new = seed_recipe(fixture_db, "New dish", 1, {ids["tofu"]: 200.0})
    seed_menu(fixture_db, two_persons["partner"], dt.date(2026, 9, 14), 2, {"dinner": (old, 1.0)})
    seed_menu(fixture_db, two_persons["partner"], dt.date(2026, 9, 14), 2, {"dinner": (new, 1.0)})

    lead = _codes(shop_refresh.stale_foods(_ctx(fixture_db), "sainsburys", limit=1))
    assert lead == ["tofu"], "the menu that was replaced does not set the queue"


# --------------------------------------------------------------- 2. the shop does not stock it
def test_a_food_the_shop_does_not_stock_stops_being_asked_for_every_week(
    fixture_db: Db, two_persons, frozen_clock
) -> None:
    """M&S has no 5% beef mince. Asking again next Sunday does not change that.

    Before this, `not_found` was counted in a ledger line and thrown away, so the same three
    foods led the queue every week: a twelve-food batch that was permanently a nine-food batch,
    and the nine were always the same nine.
    """
    _pantry_and_foods(fixture_db)
    ctx = _ctx(fixture_db, frozen_clock)
    first = _codes(shop_refresh.stale_foods(ctx, "marks_spencer", limit=2))
    assert first == ["almonds", "apples"], "alphabetical with no menu to lead"

    n = shop_refresh.record_misses(
        ctx, "marks_spencer", [{"code": "almonds", "reason": "not stocked"}]
    )
    assert n == 1
    again = _codes(shop_refresh.stale_foods(ctx, "marks_spencer", limit=2))
    assert "almonds" not in again, "a food this shop does not have is not asked for again"
    assert again == ["apples", "beans"], "the batch spends the slot on something else"


def test_a_miss_at_one_shop_is_not_a_miss_at_the_other(
    fixture_db: Db, two_persons, frozen_clock
) -> None:
    """Sainsbury's stocking something M&S does not is the entire reason for two shops."""
    _pantry_and_foods(fixture_db)
    ctx = _ctx(fixture_db, frozen_clock)
    shop_refresh.record_misses(ctx, "marks_spencer", [{"code": "almonds", "reason": "no"}])
    assert "almonds" in _codes(shop_refresh.stale_foods(ctx, "sainsburys", limit=2))


def test_a_made_up_code_in_a_reply_cannot_silence_a_real_food(
    fixture_db: Db, two_persons, frozen_clock
) -> None:
    """The session is given the codes and answers with them, but a reply is still untrusted
    input: a hallucinated or injected code must not become a row that stops a real food ever
    being looked up again."""
    _pantry_and_foods(fixture_db)
    ctx = _ctx(fixture_db, frozen_clock)
    n = shop_refresh.record_misses(
        ctx, "sainsburys", [{"code": "not_a_food", "reason": "x"}, {"code": "", "reason": "y"}]
    )
    assert n == 0
    assert fixture_db.all("SELECT id FROM shop_misses") == []


def test_a_miss_expires_so_a_shop_that_starts_stocking_it_is_noticed(
    fixture_db: Db, two_persons, frozen_clock
) -> None:
    """A range changes. Permanent would be wrong; the row ages out after MISS_DAYS."""
    _pantry_and_foods(fixture_db)
    ctx = _ctx(fixture_db, frozen_clock)
    old = (frozen_clock.now_utc() - dt.timedelta(days=shop_refresh.MISS_DAYS + 1)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    fixture_db.insert(
        "shop_misses",
        ingredient_code="almonds",
        retailer="sainsburys",
        reason="was not stocked",
        seen_at=old,
    )
    assert "almonds" in _codes(shop_refresh.stale_foods(ctx, "sainsburys", limit=2)), (
        "a month later it is worth another look"
    )


# --------------------------------------------------------------- 3. the list names the shop
def test_the_list_prefers_the_shop_you_can_walk_to(fixture_db: Db, two_persons) -> None:
    """A real Tesco product beat a real Sainsbury's one on pack size alone.

    Every product row carries its shop and nothing read it, so a tub read months ago at a shop
    across town could be the line on a list for two people whose shops are Sainsbury's and M&S:
    a product that exists, at a price that is real, in a building they do not go to.
    """
    seed_ingredient(fixture_db, "skyr", "Skyr, natural", "dairy", 450.0, "450 g tub")
    for retailer, name in (("tesco", "Tesco Skyr 450G"), ("sainsburys", "Arla Skyr 450g")):
        products.record(
            fixture_db,
            ingredient_code="skyr",
            retailer=retailer,
            retailer_sku=f"sku-{retailer}",
            name=name,
            source="chrome",
            pack_g=450.0,
            price_pence=200,
        )
    picked = products.for_shopping(
        fixture_db, two_persons["partner"], {"skyr": 450.0}, ("sainsburys", "marks_spencer")
    )
    assert picked["skyr"].retailer == "sainsburys"


def test_a_product_someone_chose_wins_over_the_nearest_shop(fixture_db: Db, two_persons) -> None:
    """Somebody who picked a product picked it. Shop preference is a default, not an override."""
    seed_ingredient(fixture_db, "skyr", "Skyr, natural", "dairy", 450.0, "450 g tub")
    far = products.record(
        fixture_db,
        ingredient_code="skyr",
        retailer="tesco",
        retailer_sku="sku-t",
        name="Tesco Skyr 450G",
        source="chrome",
        pack_g=450.0,
    )
    products.record(
        fixture_db,
        ingredient_code="skyr",
        retailer="sainsburys",
        retailer_sku="sku-s",
        name="Arla Skyr 450g",
        source="chrome",
        pack_g=450.0,
    )
    products.choose(fixture_db, two_persons["partner"], "skyr", far)
    picked = products.for_shopping(
        fixture_db, two_persons["partner"], {"skyr": 450.0}, ("sainsburys", "marks_spencer")
    )
    assert picked["skyr"].id == far, "a choice is a choice"


def test_a_list_drawn_from_two_shops_says_which_item_is_in_which(
    fixture_db: Db, two_persons
) -> None:
    """Reading a mixed list in one shop, the M&S line has to be visibly the M&S line.

    An own-brand name carries its shop already ("Sainsbury's Broccoli 375g"); a branded one does
    not ("Arla Skyr Natural 450g"). `ShoppingItem.retailer` held the answer and the renderer
    threw it away, so the person stood in Sainsbury's looking for a tub that is in M&S.
    """
    items = [
        shopping.ShoppingItem(
            ingredient_id=1,
            code="skyr",
            name="Skyr, natural",
            aisle="dairy",
            pack_g=450.0,
            pack_label="450 g tub",
            grams_total=900.0,
            credit_g=0.0,
            grams_needed=900.0,
            packs=2,
            leftover_g=0.0,
            product_name="Arla Skyr Natural 450g",
            price_pence=400,
            retailer="marks_spencer",
        ),
        shopping.ShoppingItem(
            ingredient_id=2,
            code="chk",
            name="Chicken breast",
            aisle="meat",
            pack_g=650.0,
            pack_label="650 g",
            grams_total=650.0,
            credit_g=0.0,
            grams_needed=650.0,
            packs=1,
            leftover_g=0.0,
            product_name="Sainsbury's Chicken Breast Fillets 650g",
            price_pence=490,
            retailer="sainsburys",
        ),
    ]
    text = "\n".join(shopping.render_lines(items))
    assert "[M&S]" in text, "the shop that the product name does not carry is named"
    assert "[Sainsbury's]" not in text, "a name that already says its shop is not made longer"


def test_a_single_shop_list_does_not_repeat_the_shop_on_every_line(
    fixture_db: Db, two_persons
) -> None:
    """Everything from one shop needs saying once, in the header, not eleven times."""
    item = shopping.ShoppingItem(
        ingredient_id=1,
        code="skyr",
        name="Skyr, natural",
        aisle="dairy",
        pack_g=450.0,
        pack_label="450 g tub",
        grams_total=900.0,
        credit_g=0.0,
        grams_needed=900.0,
        packs=2,
        leftover_g=0.0,
        product_name="Arla Skyr Natural 450g",
        price_pence=400,
        retailer="sainsburys",
    )
    assert "[" not in "\n".join(shopping.render_lines([item]))


# --------------------------------------------------------------- 4. the config says where to go
def test_the_shops_read_are_the_ones_within_walking_distance(tmp_path: Path) -> None:
    """The default was Tesco, which is not one of the two shops these two people use."""
    from tiffin import config as cfg

    conf = cfg.load(root=tmp_path, environ={})
    assert shop_refresh.retailer_order(conf) == [], "new installs start with catalogue packs"
    conf.data["shop"]["retailers"] = ["sainsburys", "marks_spencer"]
    conf.data["shop"]["retailer"] = "sainsburys"
    order = shop_refresh.retailer_order(conf)
    assert order[:2] == ["sainsburys", "marks_spencer"]
    assert shop_refresh.default_retailer(conf) == "sainsburys"
    for retailer in order:
        assert shop_refresh.search_url_for(conf, retailer), f"{retailer} has nowhere to look"


def test_a_shop_nobody_listed_is_still_better_than_no_product_at_all(
    fixture_db: Db, two_persons
) -> None:
    """Preference orders; it does not exclude. A real Tesco pack and price beats "2 x 500 g"."""
    seed_ingredient(fixture_db, "skyr", "Skyr, natural", "dairy", 450.0, "450 g tub")
    products.record(
        fixture_db,
        ingredient_code="skyr",
        retailer="tesco",
        retailer_sku="sku-t",
        name="Tesco Skyr 450G",
        source="chrome",
        pack_g=450.0,
        price_pence=200,
    )
    picked = products.for_shopping(
        fixture_db, two_persons["partner"], {"skyr": 450.0}, ("sainsburys", "marks_spencer")
    )
    assert picked["skyr"].retailer == "tesco"


# --------------------------------------------------------------- 5. the gap fill before a list
def test_the_fill_asks_only_for_this_menus_missing_foods(fixture_db: Db, two_persons) -> None:
    """Six page loads in front of a message somebody is waiting for, not eighty-five."""
    ids = _pantry_and_foods(fixture_db)
    rid = seed_recipe(fixture_db, "Tofu bowl", 1, {ids["tofu"]: 200.0, ids["skyr"]: 150.0})
    mid = seed_menu(
        fixture_db, two_persons["partner"], dt.date(2026, 9, 14), 2, {"dinner": (rid, 1.0)}
    )
    assert shop_refresh.menu_codes(fixture_db, [mid]) == {"tofu", "skyr"}
    gaps = shop_refresh.stale_foods(
        _ctx(fixture_db), "sainsburys", limit=shop_refresh.FILL_LIMIT, codes={"tofu", "skyr"}
    )
    assert set(_codes(gaps)) == {"tofu", "skyr"}, "this menu's foods and nothing else"


def test_a_menu_whose_foods_are_all_known_costs_no_browser_session(
    fixture_db: Db, two_persons
) -> None:
    """The common case once the catalogue is covered: the fill finds nothing and does nothing,
    so a weekly list does not open Chrome for the sake of it."""
    ids = _pantry_and_foods(fixture_db)
    rid = seed_recipe(fixture_db, "Tofu bowl", 1, {ids["tofu"]: 200.0})
    mid = seed_menu(
        fixture_db, two_persons["partner"], dt.date(2026, 9, 14), 2, {"dinner": (rid, 1.0)}
    )
    products.record(
        fixture_db,
        ingredient_code="tofu",
        retailer="sainsburys",
        retailer_sku="sku-tofu",
        name="Cauldron Tofu 280g",
        source="chrome",
        pack_g=280.0,
    )
    wanted = shop_refresh.menu_codes(fixture_db, [mid])
    assert shop_refresh.stale_foods(_ctx(fixture_db), "sainsburys", codes=wanted) == []


# --------------------------------------------------------------- 6. the empty shelf
def test_the_person_at_the_shelf_can_say_the_shop_had_none(fixture_db: Db, two_persons) -> None:
    """`/shopping out chickpeas` writes the same row the browser session writes for a not_found.

    Every other row on every other surface opens something; the shopping row was the one dead
    end, in front of the one empty shelf. So the week kept being planned around a tin nobody
    could buy, and the refresh queue kept spending a page load asking the same shop for it.

    It records a miss, not a refusal: a food this shop lacks is not a food they will not eat,
    and `/set avoid` is how somebody says that.
    """
    _pantry_and_foods(fixture_db)
    ctx = _ctx(fixture_db)
    before = _codes(shop_refresh.stale_foods(ctx, "sainsburys", limit=2))
    assert "almonds" in before

    shop_refresh.record_misses(
        ctx, "sainsburys", [{"code": "almonds", "reason": "the person said the shelf was empty"}]
    )
    after = _codes(shop_refresh.stale_foods(ctx, "sainsburys", limit=2))
    assert "almonds" not in after, "the queue stops asking that shop for it"

    # and it is a miss at that shop only, and it is not a refusal
    from tiffin.food import catalogue

    assert "almonds" in {i.code for i in catalogue.approved(fixture_db, two_persons["partner"])}, (
        "an empty shelf is not a food she refuses"
    )
    assert "almonds" in _codes(shop_refresh.stale_foods(ctx, "marks_spencer", limit=3))


# ------------------------------------------- 4. the session that stopped, not the shop that lacked
def test_a_food_the_session_never_reached_comes_back_round(
    fixture_db: Db, two_persons, frozen_clock
) -> None:
    _pantry_and_foods(fixture_db)
    ctx = _ctx(fixture_db, frozen_clock)

    shop_refresh.record_misses(
        ctx,
        "marks_spencer",
        [
            {"code": "almonds", "kind": "unstocked", "reason": "only chocolate-coated"},
            {"code": "apples", "kind": "unread", "reason": "out of turns before this one"},
        ],
    )
    queue = _codes(shop_refresh.stale_foods(ctx, "marks_spencer", limit=3))
    assert "almonds" not in queue, "the shop does not sell it; do not ask again this month"
    assert "apples" in queue, (
        "the session ran out of turns before it read this one, and thirty days of silence is "
        f"the wrong answer to that: {queue}"
    )


def test_an_old_session_that_only_wrote_prose_is_still_read_correctly(
    fixture_db: Db, two_persons, frozen_clock
) -> None:
    """Sessions before this answered with a reason and no `kind`, and the live database is full
    of their rows. The words they actually used are what the classifier reads, so a run that
    stopped early does not have to be re-run to be understood."""
    _pantry_and_foods(fixture_db)
    ctx = _ctx(fixture_db, frozen_clock)

    shop_refresh.record_misses(
        ctx,
        "marks_spencer",
        [
            {"code": "almonds", "reason": "Sold only as flavoured; no plain pack"},
            {"code": "apples", "reason": "search incomplete due to token constraints"},
            {"code": "beans", "reason": "Not yet searched"},
        ],
    )
    kinds = {
        str(r["ingredient_code"]): str(r["kind"])
        for r in fixture_db.all("SELECT ingredient_code, kind FROM shop_misses")
    }
    assert kinds == {"almonds": "unstocked", "apples": "unread", "beans": "unread"}, kinds

    queue = _codes(shop_refresh.stale_foods(ctx, "marks_spencer", limit=4))
    assert "almonds" not in queue and {"apples", "beans"} <= set(queue), queue


def test_a_reply_that_invents_a_kind_is_treated_as_the_careful_one(
    fixture_db: Db, two_persons, frozen_clock
) -> None:
    """The session's reply is untrusted input like any other. A `kind` that is not one of the two
    words falls back to reading the reason, and a reason that says nothing lands on 'unstocked' -
    the reading that asks the shop less, not the one that drives the browser more."""
    _pantry_and_foods(fixture_db)
    ctx = _ctx(fixture_db, frozen_clock)
    shop_refresh.record_misses(
        ctx, "marks_spencer", [{"code": "almonds", "kind": "MAYBE", "reason": "hmm"}]
    )
    kind = fixture_db.scalar("SELECT kind FROM shop_misses WHERE ingredient_code = 'almonds'")
    assert kind == "unstocked", kind


def test_a_food_this_shop_keeps_failing_to_show_stops_leading_the_queue(
    fixture_db: Db, two_persons, frozen_clock
) -> None:
    """'unread' parks nothing, and back-to-back passes turn that into a loop.

    `stale_foods` is ordered, so the six foods at the front lead every pass. A miss the session
    owns is not parked, by design - but when the reason is standing (Ocado wants a postcode, a
    page will not show without signing in) "not parked" means a forty-pass backfill spends all
    forty passes on the same six foods and reads nothing else. After UNREAD_TRIES goes at one
    shop, whatever is in the way has stopped being a run of bad luck.
    """
    _pantry_and_foods(fixture_db)
    ctx = _ctx(fixture_db, frozen_clock)
    unread = {"code": "almonds", "kind": "unread", "reason": "out of turns"}

    for attempt in range(1, shop_refresh.UNREAD_TRIES):
        shop_refresh.record_misses(ctx, "marks_spencer", [dict(unread)])
        assert "almonds" in _codes(shop_refresh.stale_foods(ctx, "marks_spencer", limit=2)), (
            f"after {attempt} go(s) it is still worth asking for"
        )

    shop_refresh.record_misses(ctx, "marks_spencer", [dict(unread)])
    queue = _codes(shop_refresh.stale_foods(ctx, "marks_spencer", limit=2))
    assert "almonds" not in queue, (
        f"three passes could not show it and the fourth would have led with it again: {queue}"
    )
    assert queue == ["apples", "beans"], "the batch spends the slot on something it can read"
    assert "almonds" in _codes(shop_refresh.stale_foods(ctx, "sainsburys", limit=2)), (
        "and it is only this shop that cannot show it"
    )


def test_one_good_read_wipes_out_the_failed_tries(
    fixture_db: Db, two_persons, frozen_clock
) -> None:
    """The count is not a punishment the food carries around. A shop that starts showing it -
    the postcode set, the page fixed - makes it fresh, and fresh is what the queue reads."""
    foods = _pantry_and_foods(fixture_db)
    ctx = _ctx(fixture_db, frozen_clock)
    for _ in range(shop_refresh.UNREAD_TRIES):
        shop_refresh.record_misses(
            ctx, "marks_spencer", [{"code": "almonds", "kind": "unread", "reason": "out of turns"}]
        )
    assert "almonds" not in _codes(shop_refresh.stale_foods(ctx, "marks_spencer", limit=3))

    products.record(
        fixture_db,
        ingredient_code="almonds",
        retailer="marks_spencer",
        retailer_sku="sku-1",
        name="M&S Whole Almonds 200g",
        source="chrome",
        clock=frozen_clock,
        pack_g=200.0,
        price_pence=280,
    )
    assert foods["almonds"]
    assert "almonds" not in _codes(shop_refresh.stale_foods(ctx, "marks_spencer", limit=3)), (
        "it is read, so it is not stale either - for FRESH_DAYS, on its own merits"
    )


# ------------------------------------- 5. the right food, the wrong state of it
def test_a_cooked_label_keeps_the_pack_and_the_price(
    fixture_db: Db, two_persons, frozen_clock
) -> None:
    """Sainsbury's fresh chicken breast is published "cooked as per instructions".

    137 kcal against a catalogue that holds the food raw at 106 is not a number to store, and
    refusing it was right. Refusing the whole PRODUCT was not: the shopping list wanted the pack
    size and the price, and it never wanted the shop's macros - those come from the catalogue
    and always did. So the product is kept with its nutrition null and `nutrition_basis` saying
    why, which is a different thing from a food nobody has looked up.
    """
    _pantry_and_foods(fixture_db)
    ctx = _ctx(fixture_db, frozen_clock)
    written, notes = shop_refresh.store(
        ctx,
        "sainsburys",
        [
            {
                "code": "skyr",
                "retailer_sku": "7874927",
                "name": "Sainsbury's Skyr 450g",
                "pack_g": 450,
                "price_pence": 185,
                "kcal_100": None,
                "protein_100": None,
                "nutrition_basis": "cooked",
            }
        ],
    )
    assert written == 1, "the product was thrown away over numbers nobody was going to use"
    assert not notes, f"a missing table is not a suspect product: {notes}"

    got = products.for_ingredient(fixture_db, "skyr", "sainsburys")
    assert got and got[0].pack_g == 450.0 and got[0].price_pence == 185, got
    assert not got[0].has_nutrition, "the shop's cooked figures must not read as the raw food's"
    basis = fixture_db.scalar("SELECT nutrition_basis FROM products WHERE ingredient_code='skyr'")
    assert basis == "cooked", f"nothing says why the rows are empty: {basis!r}"

    # and it counts as read: the food is priced, so the queue moves on to something else
    assert "skyr" not in _codes(shop_refresh.stale_foods(ctx, "sainsburys", limit=9))


def test_a_cooked_label_does_not_park_the_food_it_is_about(
    fixture_db: Db, two_persons, frozen_clock
) -> None:
    """Sainsbury's HAS chicken breast. What it does not have is a raw per-100 g table.

    Filing that as "unstocked" parked the food for thirty days, which is how twenty-one
    food/shop pairs - chicken, potatoes, rice, pasta, quinoa, lentils, salmon - were left naming
    a generic line at shops that sell them. The page's figures are a third kind of answer, and
    it is not a fact about the shelf: the product, its pack and its price are all still wanted,
    so the food comes back round and the next read stores it.
    """
    _pantry_and_foods(fixture_db)
    ctx = _ctx(fixture_db, frozen_clock)
    n = shop_refresh.record_misses(
        ctx,
        "sainsburys",
        [
            {"code": "almonds", "reason": "only sold roasted and salted"},
            {"code": "apples", "reason": "nutrition label shows cooked values, not raw as needed"},
        ],
    )
    assert n == 2
    kinds = {
        str(r["ingredient_code"]): str(r["kind"])
        for r in fixture_db.all("SELECT ingredient_code, kind FROM shop_misses")
    }
    assert kinds == {"almonds": "unstocked", "apples": "basis"}, kinds

    queue = _codes(shop_refresh.stale_foods(ctx, "sainsburys", limit=4))
    assert "almonds" not in queue, "the shop genuinely does not sell that one"
    assert "apples" in queue, (
        f"a cooked label parked a food the shop sells, so nothing will price it: {queue}"
    )
