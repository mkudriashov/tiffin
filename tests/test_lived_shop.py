"""Ten moments with a phone in one hand and a trolley in the other.

Not "does the function return the right number" - the numbers are covered in
`test_food_shopping.py` and `test_shopping_products.py`. This file asks the other question:
standing in the aisle, in this shop, at this moment, can the person GET to what the box
already knows, and in how many steps? A right answer down a path nobody can walk is the bug.

Every story is one place to be standing: an aisle, two shops in one trip, a weekend shop for
two people, the Sunday trip for a list that arrived on Friday.
"""

from __future__ import annotations

import datetime as dt

import pytest

from tiffin.db import Db
from tiffin.food import batch as batch_mod
from tiffin.food import products, shopping
from tiffin.web import queries

from .test_food_shopping import seed_ingredient, seed_menu, seed_recipe
from .test_menu import world  # noqa: F401

MONDAY = dt.date(2026, 9, 14)
SATURDAY = dt.date(2026, 9, 12)


def catalogue_ids(db: Db, codes: tuple[str, ...]) -> dict[str, int]:
    """The ids of the world fixture's own seeded foods, newest version of each."""
    out: dict[str, int] = {}
    for code in codes:
        found = db.scalar(
            "SELECT id FROM ingredients WHERE code = ? ORDER BY version DESC, id DESC LIMIT 1",
            (code,),
        )
        assert found is not None, f"the seeded catalogue has no {code}"
        out[code] = int(found)
    return out


def a_week_of(db: Db, person_id: int, grams: dict[int, float], *, name: str = "Tray bake") -> int:
    """A five-day dinner menu of one recipe, and the shopping list built from it."""
    recipe = seed_recipe(db, name, 1, grams)
    return seed_menu(db, person_id, MONDAY, 5, {"dinner": (recipe, 1.0)})


def tick_buttons(sent: dict) -> list[str]:
    markup = sent.get("reply_markup") or {"inline_keyboard": []}
    return [b["callback_data"] for row in markup["inline_keyboard"] for b in row]


# -- in the aisle ----------------------------------------------------------------------------


def test_01_the_list_reread_in_the_aisle_walks_the_shop_not_the_alphabet(world) -> None:  # noqa: F811
    """The Friday message walks produce, meat, dairy, grains. /shopping walks the alphabet.

    The aisle order is the whole point of the aisle field - `shopping.render_lines` sorts by
    the YAML's order so the list matches the way a person walks the shop. `/shopping`, which
    is what gets reopened once the message has scrolled away, orders by `si.aisle` in SQL:
    alphabetical. So the list that arrives and the same list reread twenty minutes later put
    the four items in two different orders, and the reread one sends the person back past the
    dairy for the yoghurt after they have reached the grains.
    """
    pid, db, tg, _gw, say, _tap, _run = world
    ids = catalogue_ids(db, ("broccoli", "chicken", "yoghurt", "oats"))
    a_week_of(db, pid, {i: 200.0 for i in ids.values()})
    items = shopping.build(db, int(db.scalar("SELECT MAX(id) FROM menus")))
    sent_order = [i.aisle for i in items]
    assert sent_order == ["produce", "meat", "dairy", "grains"], "the message walks the shop"

    text = say("/shopping")
    for name in ("Broccoli", "Chicken breast", "Greek yoghurt", "Porridge oats"):
        assert name in text, f"{name} is on the list at all"
    walk = [text.index(n) for n in ("Broccoli", "Chicken breast", "Greek yoghurt", "Porridge oats")]
    assert walk == sorted(walk), (
        "the reread list must walk the shop in the same order as the message it replaces"
    )
    assert tg.sent[-1]["text"] == text


def test_02_a_trip_to_two_shops_says_which_item_is_in_which_shop(fixture_db: Db, two_persons):
    """Half the list is a Tesco own-brand and half is a tub of Arla skyr seen in Sainsbury's.

    `ShoppingItem.retailer` exists for exactly this and its own comment says so: "an own-brand
    name carries it ("Tesco Broccoli 375G") and a branded one does not ("Arla Skyr Natural
    450g"), so a list mixing two retailers needs this to say where to go". `_items` fills it
    in, and then `_write` has no column to put it in and `shelf_text` never reads it. So the
    box knows the skyr was seen in Sainsbury's and the person standing in Tesco is told to
    look for it here.
    """
    db, pid = fixture_db, two_persons["owner"]
    chicken = seed_ingredient(db, "chk", "Chicken breast", "meat", 500.0, "500 g pack")
    skyr = seed_ingredient(db, "skyr", "Skyr, natural", "dairy", 450.0, "450 g tub")
    menu_id = a_week_of(db, pid, {chicken: 220.0, skyr: 150.0})
    products.record(
        db,
        ingredient_code="chk",
        retailer="tesco",
        retailer_sku="t1",
        name="Tesco British Chicken Breast Fillets 650G",
        source="chrome",
        pack_g=650.0,
        pack_label="650 g pack",
        price_pence=490,
    )
    products.record(
        db,
        ingredient_code="skyr",
        retailer="sainsburys",
        retailer_sku="s1",
        name="Arla Skyr Natural 450g",
        source="chrome",
        pack_g=450.0,
        pack_label="450 g tub",
        price_pence=200,
    )
    items = shopping.build(db, menu_id, retailers=("tesco", "sainsburys"))
    assert {i.code: i.retailer for i in items} == {"chk": "tesco", "skyr": "sainsburys"}, (
        "the box knows which shop each one was seen in"
    )

    text = "\n".join(shopping.render_lines(items))
    assert "Tesco British Chicken Breast Fillets 650G" in text, "the shelf name, as already fixed"
    assert "sainsbury" in text.lower(), (
        "a list drawing on two shops has to say which item is in which shop; 'Arla Skyr "
        "Natural 450g' carries no shop in its name, so the list is the only place it can say so"
    )


# -- shopping together -----------------------------------------------------------------------


def test_03_one_partners_tick_on_the_joint_list_shows_on_the_other_phone(
    fixture_db: Db, two_persons
):
    """Saturday, one trolley, two phones. He ticks the salmon; her list still says to buy it.

    The joint weekend list is deliberately shared: `shopping_lists.person_id` is NULL and
    `menuflow.tick` lets either person tick any row of it ("a joint list has no owner and
    either person may tick it"). But every surface that DISPLAYS a tick filters
    `shopping_ticks` by `person_id = ?` - `queries.shopping` and `cmd_shopping` both - so the
    tick he just made is invisible to her. Two people in one shop with one list each see two
    different lists, and the second one buys the salmon again.
    """
    db, owner_id, partner_id = fixture_db, two_persons["owner"], two_persons["partner"]
    salmon = seed_ingredient(db, "salmon", "Salmon fillets", "fish", 240.0, "2 fillets")
    potato = seed_ingredient(db, "potato", "Potatoes", "produce", 2000.0, "2 kg bag")
    recipe = seed_recipe(db, "Salmon and potatoes", 1, {salmon: 150.0, potato: 300.0})
    his = seed_menu(db, owner_id, SATURDAY, 2, {"dinner": (recipe, 1.0)})
    partners = seed_menu(db, partner_id, SATURDAY, 2, {"dinner": (recipe, 1.0)})
    items = shopping.joint_weekend(db, [his, partners])
    item_id = next(i.item_id for i in items if i.code == "salmon")
    assert item_id is not None

    assert queries.tick_item(db, owner_id, item_id, True) is True, "he may tick the joint list"
    assert queries.shopping(db, owner_id, None)["list"] is not None, "and he can see it"
    partner_view = queries.shopping(db, partner_id, None)
    assert partner_view["list"] is not None, "she can see the same shared list"
    partner_salmon = next(i for i in partner_view["items"] if "Salmon" in str(i["name"]))
    assert partner_salmon["ticked"] is True, (
        "a list they are shopping from together has to show her the row he has already put in "
        "the trolley, or the shared list is two private lists that happen to match"
    )


def test_04_the_list_is_still_ticked_when_the_phone_comes_back_out(world) -> None:  # noqa: F811
    """Screen off at the checkout, phone out again in the car park, and the ticks are still there.

    `shopping_ticks` is a ledger and the tick is a row, so this should hold across the app
    closing, across a restart, and across the two surfaces: the tap happened in Telegram and
    the web page has to agree, because the shop is where somebody switches between them.
    """
    pid, db, tg, _gw, say, tap, _run = world
    ids = catalogue_ids(db, ("broccoli", "chicken"))
    a_week_of(db, pid, {i: 200.0 for i in ids.values()})
    shopping.build(db, int(db.scalar("SELECT MAX(id) FROM menus")))

    first = say("/shopping")
    buttons = tick_buttons(tg.sent[-1])
    assert buttons and all(b.startswith("k:tick:") for b in buttons), "a button per item"
    item_id = int(buttons[0].split(":")[2])
    tap(buttons[0])

    again = say("/shopping")
    name = db.scalar(
        "SELECT i.name FROM shopping_items si JOIN ingredients i ON i.id = si.ingredient_id "
        "WHERE si.id = ?",
        (item_id,),
    )
    marked = [ln for ln in again.splitlines() if ln.startswith("x ")]
    assert len(marked) == 1 and str(name) in marked[0], f"one item marked bought: {again!r}"
    assert first != again, "reading it back after a tick is not the same text"
    on_the_page = queries.shopping(db, pid, None)
    ticked = {int(i["id"]) for i in on_the_page["items"] if i["ticked"]}
    assert ticked == {item_id}, "and the web page in the same shop agrees with the tap"


# -- what the list does not say ---------------------------------------------------------------


# Pinned the credit: now runs with `shopping.count_on_home` on, because the default buys everything.
@pytest.mark.usefixtures("count_on_home")
def test_05_a_list_buying_less_than_the_week_needs_says_what_is_already_in(fixture_db, two_persons):
    """A list buying fewer packs because the person reported stock names that credit."""
    db, pid = fixture_db, two_persons["owner"]
    chicken = seed_ingredient(db, "chk", "Chicken breast", "meat", 500.0, "500 g pack")
    menu_id = a_week_of(db, pid, {chicken: 330.0})
    first = shopping.build(db, menu_id)
    assert first[0].packs == 4 and first[0].leftover_g == 350.0, "1650 g is four 500 g packs"

    # The person reports 350 g at home; the list must make that credit visible.
    db.insert("leftovers", person_id=pid, ingredient_id=chicken, grams=350.0)
    second = shopping.build(db, menu_id)
    item = second[0]
    assert item.credit_g == 350.0 and item.grams_needed == 1300.0, "the box knows why"
    assert item.packs == 3, "and buys one pack fewer"

    line = "\n".join(shopping.render_lines(second))
    said = ("350" in line) or any(w in line.lower() for w in ("fridge", "already", "over from"))
    assert said, (
        "a list that buys less than the week eats has to say what it is counting on being in "
        f"the fridge, or the shortfall is unnoticeable in the shop: {line!r}"
    )


def test_06_the_pack_on_the_shelf_is_400_g_and_saying_so_reaches_the_list(world) -> None:  # noqa: F811
    """The 500 g mince is out, there is a 400 g pack, and the box already holds that product.

    `/shop mince` is the route to say which pack is real, and tapping it answers "Your
    shopping list will round to that pack" - present tense, about the list in the person's
    hand. It does not: nothing rebuilds a list, so /shopping still says two packs, which is
    900 g of need against 800 g of mince. Either the list follows the choice, or the reply has
    to say it applies to the next one; promising the list and changing nothing walks the
    person out of the shop 100 g short.
    """
    pid, db, _tg, _gw, say, tap, _run = world
    mince = seed_ingredient(db, "mince", "Beef mince 5%", "meat", 500.0, "500 g pack")
    menu_id = a_week_of(db, pid, {mince: 180.0}, name="Chilli")
    for sku, name, pack, label, price in (
        ("m500", "Tesco Beef Mince 5% 500G", 500.0, "500 g pack", 320),
        ("m400", "Tesco Beef Mince 5% 400G", 400.0, "400 g pack", 280),
    ):
        products.record(
            db,
            ingredient_code="mince",
            retailer="tesco",
            retailer_sku=sku,
            name=name,
            source="chrome",
            pack_g=pack,
            pack_label=label,
            price_pence=price,
        )
    built = shopping.build(db, menu_id)
    assert built[0].pack_g == 500.0 and built[0].packs == 2, "900 g against the 500 g pack"

    offered = say("/shop mince")
    assert "400 g" in offered, "the 400 g pack is offered as the one you buy"
    four_hundred = int(db.scalar("SELECT id FROM products WHERE retailer_sku = 'm400'"))
    reply = tap(f"k:prod:{four_hundred}")
    listed = say("/shopping")
    later = any(w in reply.lower() for w in ("next", "future", "from now on", "already sent"))
    assert "400" in listed or later, (
        "after being told the real pack, the list in the person's hand says two packs of the "
        f"pack that is not there; 900 g needs three 400 g packs: {listed!r} / {reply!r}"
    )


def test_07_the_list_that_arrived_on_friday_says_which_days_it_is_for(world) -> None:  # noqa: F811
    """The shop is on Sunday. /shopping shows a list with no date on it at all.

    The scheduled message is headed "Shopping for Mon 14 Sep". The reread is headed
    "Shopping (segment)" - `cmd_shopping` SELECTs `created_at` and never prints it, and the
    list's `menu_id` is right there on the row. So a list built before a /cook replace, or
    last week's list still sitting at the top of the chat, is indistinguishable from the
    current one, and the way that ends is a trolley filled from the wrong week.
    """
    pid, db, _tg, _gw, say, _tap, _run = world
    ids = catalogue_ids(db, ("chicken", "broccoli"))
    a_week_of(db, pid, {i: 200.0 for i in ids.values()})
    shopping.build(db, int(db.scalar("SELECT MAX(id) FROM menus")))

    text = say("/shopping")
    assert "Shopping" in text
    dated = any(token in text for token in ("14 Sep", "Mon 14", "2026-09", "Sep"))
    assert dated, (
        "the list has to say which days it is for, or when it was made, before somebody shops "
        f"from a list that a changed menu has already replaced: {text.splitlines()[0]!r}"
    )


def test_08_the_cupboard_staple_that_ran_out_can_be_put_back_on_the_list(fixture_db, two_persons):
    """The olive oil is empty. "Put it back on the shopping list" is a setting that goes nowhere.

    Three surfaces write `profiles.pantry_json` and all three describe it as this: `/set
    pantry` calls it "staples never on the shopping list", the model's food-preferences card
    asks "Treat {foods} as a cupboard staple, so it never reaches the shop?" and "Put {foods}
    back on the shopping list?", and onboarding asks for it. `food/shopping.py` has never read
    that column. The only pantry it knows is `ingredients.pantry`, a global catalogue flag that
    is the same for both people and cannot be reached from a phone at all - which is precisely
    why the per-person column exists. Its sibling `refused_foods` IS read, by
    `catalogue.refused_terms`, so the wiring exists for one of these lists and not the other.

    Both directions land in a shop: the staple they named is bought again every week, and the
    one they have run out of never appears however plainly they ask.
    """
    import json

    from tiffin.food import catalogue

    db, pid = fixture_db, two_persons["owner"]
    chicken = seed_ingredient(db, "chk", "Chicken breast", "meat", 500.0, "500 g pack")
    oil = seed_ingredient(db, "oil", "Olive oil", "condiments", 500.0, "500 ml bottle")
    salt = seed_ingredient(db, "salt", "Table salt", "condiments", 750.0, "750 g tub", pantry=1)
    db.insert(
        "profiles",
        person_id=pid,
        effective_from="2026-09-01",
        sex="m",
        birth_date="1990-01-01",
        height_cm=183.0,
        activity_factor=1.5,
        kcal_floor=1800,
        pantry_json=json.dumps(["olive oil"]),
        refused_foods=json.dumps(["mackerel"]),
    )
    assert catalogue.refused_terms(db, pid) != [], "the sibling food list is read from the profile"

    menu_id = a_week_of(db, pid, {chicken: 220.0, oil: 50.0, salt: 10.0})
    menu = db.one("SELECT * FROM menus WHERE id = ?", (menu_id,))
    assert menu is not None
    load = batch_mod.plan(db, menu, ("dinner",), db.clock.today_local())
    assert {a.name for a in load.amounts} >= {"Olive oil", "Table salt"}, "the cook needs both"

    codes = {i.code for i in shopping.build(db, menu_id)}
    assert codes == {"chk", "salt"}, (
        "the shopping list has to obey the cupboard list the person set: the olive oil they "
        "named as a staple should be off it and the salt they have run out of should be on it; "
        f"got {sorted(codes)}, which is the catalogue's flag and nothing they can change"
    )


# -- prices, and the one route out of the shop ------------------------------------------------


def test_09_a_part_priced_list_says_so_on_both_surfaces_and_still_names_the_food(
    fixture_db: Db, two_persons
) -> None:
    """A total with half the prices missing, read at the till. Both surfaces say how much of it.

    A number presented as the week's cost when only one item is priced is worse than no
    number. The message says "(1 of 2 priced)"; this checks the reread and the web payload say
    the same thing rather than quietly totalling what they happen to have, and that the
    unpriced food is still on the list with its pack, because an unknown price is not a reason
    to leave the rice out of the trolley.
    """
    db, pid = fixture_db, two_persons["owner"]
    chicken = seed_ingredient(db, "chk", "Chicken breast", "meat", 500.0, "500 g pack")
    rice = seed_ingredient(db, "ric", "Basmati rice", "grains", 1000.0, "1 kg bag")
    menu_id = a_week_of(db, pid, {chicken: 220.0, rice: 100.0})
    products.record(
        db,
        ingredient_code="chk",
        retailer="tesco",
        retailer_sku="t1",
        name="Tesco British Chicken Breast Fillets 650G",
        source="chrome",
        pack_g=650.0,
        pack_label="650 g pack",
        price_pence=490,
    )
    items = shopping.build(db, menu_id, retailers=("tesco",))
    total = shopping.total_line(items)
    assert "1 of 2 priced" in total and "£9.80" in total, "the message is honest about the gap"

    payload = queries.shopping(db, pid, None)
    priced = [i for i in payload["items"] if i["price_pence"] is not None]
    assert len(priced) == 1 and int(priced[0]["price_pence"]) == 980, "the page can say the same"
    rice_row = next(i for i in payload["items"] if str(i["name"]) == "Basmati rice")
    assert int(rice_row["packs"]) == 1 and str(rice_row["pack_label"]) == "1 kg bag", (
        "the unpriced food is still named, with the pack to pick up"
    )
    assert rice_row["price_pence"] is None, "and no price is invented for it"


def test_10_when_the_shop_is_out_of_chickpeas_the_list_says_what_they_were_for(world) -> None:  # noqa: F811
    """Empty shelf. Which meal breaks, and can the box be told? Neither, from where they stand.

    The list row carries the ingredient and the list carries the menu, so one join away is
    "Wednesday's chilli" - and `/shopping` offers the ingredient name, a pack count and a
    tick. Tick is the only verb: there is no way to see what an item is for and no way to say
    the shop had none, so Tiffin keeps planning a week around an ingredient nobody could
    buy and the person finds out on Wednesday.

    Everywhere else in this bot a row opens: the menu has a button per meal, a recipe card has
    "Back to the menu", `/shop` has a button per product. The shopping row is the one dead end,
    and it is the row somebody is looking at while standing in front of an empty shelf.
    """
    pid, db, tg, _gw, say, _tap, _run = world
    ids = catalogue_ids(db, ("chicken", "rice"))
    beans = seed_ingredient(db, "beans", "Chickpeas", "tins", 240.0, "400 g tin")
    menu_id = a_week_of(db, pid, {**{i: 200.0 for i in ids.values()}, beans: 100.0}, name="Chilli")
    items = shopping.build(db, menu_id)
    item_id = next(i.item_id for i in items if i.code == "beans")

    dish = db.scalar(
        "SELECT rc.name FROM shopping_items si "
        "JOIN shopping_lists sl ON sl.id = si.list_id "
        "JOIN menu_days md ON md.menu_id = sl.menu_id "
        "JOIN meal_slots ms ON ms.menu_day_id = md.id "
        "JOIN recipes rc ON rc.id = ms.recipe_id "
        "JOIN recipe_ingredients ri ON ri.recipe_id = rc.id "
        "WHERE si.id = ? AND ri.ingredient_id = si.ingredient_id LIMIT 1",
        (item_id,),
    )
    assert dish == "Chilli", "the box can name the dish the tin is for, one join from the row"
    assert menu_id == int(db.scalar("SELECT menu_id FROM shopping_lists ORDER BY id DESC LIMIT 1"))

    text = say("/shopping")
    buttons = tick_buttons(tg.sent[-1])
    assert "Chickpeas" in text, "the tin is on the list"
    route = ("Chilli" in text) or any(not b.startswith("k:tick:") for b in buttons)
    assert route, (
        "standing at an empty shelf the only thing the list can do is tick; nothing says what "
        f"the tin was for and nothing takes 'they had none': {buttons!r}"
    )
