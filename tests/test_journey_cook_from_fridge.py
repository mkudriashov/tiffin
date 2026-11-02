"""Cook from the fridge: "I already bought it."

Tiffin has no idea what is in the kitchen. What it has instead is three half-signals:

- `leftovers` rows, written when a shopping list is COMPOSED - packs x pack size minus the
  week's grams - before anybody has been near a shop;
- `shopping_ticks`, which a person taps in the shop (and which, on the live box, nobody has ever
  tapped: zero ticked rows against 107 leftovers rows);
- `shopping.current_leftovers`, the latest leftover per food, aged by aisle.

Each story is one person at one moment wanting one thing about food that is already at home,
and asks whether Tiffin's answer matches the kitchen rather than the paperwork.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from tiffin.clock import TZ
from tiffin.food import menu as menu_mod
from tiffin.food import shopping
from tiffin.jobs import menu_propose, shopping_list
from tiffin.model import tools
from tiffin.web import queries

from .test_food_shopping import seed_ingredient, seed_menu, seed_recipe
from .test_menu import FakeGateway, proposal_for, world  # noqa: F401

SUN_6 = dt.date(2026, 9, 6)
MON_7 = dt.date(2026, 9, 7)
FRI_11 = dt.date(2026, 9, 11)
SAT_12 = dt.date(2026, 9, 12)
SUN_13 = dt.date(2026, 9, 13)
MON_14 = dt.date(2026, 9, 14)


# ---------------------------------------------------------------- props
def _id(db, code: str) -> int:
    return int(
        db.scalar("SELECT id FROM ingredients WHERE code = ? ORDER BY id DESC LIMIT 1", (code,))
    )


def _at(db, day: dt.date, hour: int = 11, minute: int = 0) -> None:
    db.clock.set_local(dt.datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ))


def _accepted(db, person_id: int, start: dt.date, days: int, slots: dict) -> int:
    menu_id = seed_menu(db, person_id, start, days, slots)
    menu_mod.decide(db, menu_id, "accepted")
    return menu_id


def _line(text: str, needle: str) -> str:
    return next((ln for ln in text.splitlines() if needle in ln), "")


def _buttons(sent: dict) -> list[tuple[str, str]]:
    markup = sent.get("reply_markup") or {}
    rows = markup.get("inline_keyboard", [])
    return [(b["text"], b["callback_data"]) for row in rows for b in row]


def _chicken_dish(db, name: str, chicken_g: float, *, rice_g: float = 80.0) -> int:
    return seed_recipe(db, name, 1, {_id(db, "chicken"): chicken_g, _id(db, "rice"): rice_g})


# ---------------------------------------------------------------- 1. ticked in the aisle
def test_01_ticking_chicken_in_the_shop_shows_on_the_list_when_reread(world) -> None:  # noqa: F811
    """Sunday 11:00, trolley in one hand: the owner picks up the chicken, taps it on /shopping, and
    opens the list again at the till to see what is left."""
    pid, db, tg, _gw, say, tap, _run = world
    _accepted(db, pid, MON_14, 5, {"dinner": (_chicken_dish(db, "Chicken and rice", 180), 1.0)})
    _at(db, SUN_13)

    say("/shopping")
    chicken = next(d for t, d in _buttons(tg.sent[-1]) if t.startswith("Chicken"))
    tap(chicken)
    again = say("/shopping")
    assert _line(again, "Chicken breast").startswith("x "), again
    assert _line(again, "Basmati").startswith("- "), "only what he ticked"


# ---------------------------------------------------------------- 2. bought, then it changed
# Pinned the credit: now runs with `shopping.count_on_home` on, because the default buys everything.
@pytest.mark.usefixtures("count_on_home")
def test_02_a_menu_changed_after_the_shop_does_not_send_him_back_for_the_same_chicken(
    world,  # noqa: F811
) -> None:
    """Sunday 15:00. He shopped this morning from the list, ticked every line, and the chicken
    is in the fridge. Then he swapped the week's dinner for a stir fry - still 180 g of chicken a
    day - and opens /shopping to see what the new menu needs."""
    pid, db, tg, _gw, say, tap, _run = world
    _accepted(db, pid, MON_14, 5, {"dinner": (_chicken_dish(db, "Chicken and rice", 180), 1.0)})
    _at(db, SUN_13, 10)
    say("/shopping")
    for _text, data in _buttons(tg.sent[-1]):
        tap(data)  # everything on the list went into the trolley: 2 x 500 g of chicken
    bought = shopping.ticks_for(
        db, int(db.scalar("SELECT MAX(id) FROM shopping_lists")), pid, joint=False
    )
    assert bought, "Tiffin was told, line by line, what came home"

    stir_fry = seed_recipe(
        db, "Chicken stir fry", 1, {_id(db, "chicken"): 180, _id(db, "broccoli"): 100}
    )
    _accepted(db, pid, MON_14, 5, {"dinner": (stir_fry, 1.0)})
    _at(db, SUN_13, 15)
    new_list = say("/shopping")
    assert "Broccoli" in new_list, "the new food is on it"
    chicken = _line(new_list, "Chicken breast")
    assert not chicken, (
        "1000 g of chicken was ticked this morning and nothing has been cooked; the stir fry "
        f"needs 900 g. The new list still says buy it: {chicken!r}"
    )


# ---------------------------------------------------------------- 3. the shelf was empty
def test_03_potatoes_the_shop_did_not_have_are_not_counted_as_at_home_next_week(
    world,  # noqa: F811
) -> None:
    """Sunday 6 Sep, in the shop: no potatoes. He sends `/shopping out potatoes` and ticks the
    rest. A week later he opens the next list and expects to buy potatoes for the whole week,
    because none came home last time."""
    pid, db, tg, _gw, say, tap, _run = world
    potato = seed_ingredient(db, "potato", "Potatoes, raw", "produce", 1000, "1 kg bag")
    dish = seed_recipe(db, "Chicken and potatoes", 1, {_id(db, "chicken"): 180, potato: 250})
    _accepted(db, pid, MON_7, 5, {"dinner": (dish, 1.0)})
    _at(db, SUN_6)
    last = say("/shopping")
    assert "2 x 1 kg bag" in _line(last, "Potatoes"), last  # 1250 g -> two bags, 750 g spare
    said = say("/shopping out potatoes")
    assert "Noted" in said, said
    for text, data in _buttons(tg.sent[-2]):
        if not text.startswith("Potatoes"):
            tap(data)

    _accepted(db, pid, MON_14, 5, {"dinner": (dish, 1.0)})
    _at(db, SUN_13)
    this = say("/shopping")
    potatoes = _line(this, "Potatoes")
    assert "2 x 1 kg bag" in potatoes, (
        "he told Tiffin the shelf was empty and did not tick them, so there are no "
        "potatoes at home and the week needs 1250 g. The list counts last week's never-bought "
        f"750 g as in the cupboard and says: {potatoes!r}"
    )


# ---------------------------------------------------------------- 4. half a bag of rice
# Pinned the credit: now runs with `shopping.count_on_home` on, because the default buys everything.
@pytest.mark.usefixtures("count_on_home")
def test_04_a_half_used_bag_of_rice_is_not_bought_again(world) -> None:  # noqa: F811
    """A person who reports the remaining rice does not buy another bag."""
    pid, db, tg, _gw, say, tap, _run = world
    dish = _chicken_dish(db, "Chicken and rice", 180, rice_g=80)
    _accepted(db, pid, MON_7, 5, {"dinner": (dish, 1.0)})
    _at(db, SUN_6)
    last = say("/shopping")
    assert "Basmati" in last
    for _text, data in _buttons(tg.sent[-1]):
        tap(data)

    _accepted(db, pid, MON_14, 5, {"dinner": (dish, 1.0)})
    _at(db, SUN_13)
    db.insert("leftovers", person_id=pid, ingredient_id=_id(db, "rice"), grams=600.0)
    this = say("/shopping")
    assert "Chicken breast" in this, this
    assert "Basmati" not in this, f"600 g is still in the bag: {this}"


# ---------------------------------------------------------------- 5. the menu hears of the fridge
def test_05_the_next_menu_is_told_about_the_chicken_in_the_fridge_not_the_salt(
    world,  # noqa: F811
) -> None:
    """Saturday 18:00, the proposal for next week is being built. There are 100 g of chicken
    and 200 g of broccoli left from last week, and a cupboard of spices. What he wants is a menu
    that uses up the food that goes off - so the builder has to be told about it."""
    pid, db, _tg, gateways, say, _tap, run_job = world
    spices = [
        seed_ingredient(db, code, name, "condiments", 100, "100 g jar")
        for code, name in (
            ("salt", "Salt"),
            ("pepper", "Black pepper"),
            ("paprika", "Paprika"),
            ("cumin", "Ground cumin"),
            ("garlic", "Garlic granules"),
            ("chilli", "Chilli flakes"),
            ("oregano", "Dried oregano"),
            ("turmeric", "Turmeric"),
        )
    ]
    grams = {_id(db, "chicken"): 180.0, _id(db, "broccoli"): 100.0} | {s: 1.0 for s in spices}
    dish = seed_recipe(db, "Spiced chicken and broccoli", 1, grams)
    _accepted(db, pid, MON_7, 5, {"dinner": (dish, 1.0)})
    _at(db, SUN_6)
    say("/shopping")  # 2 x 500 g chicken for 900 g; two 350 g heads of broccoli for 500 g
    home = shopping.current_leftovers(db, pid, SAT_12)
    assert home.get("chicken") == 100.0 and home.get("broccoli") == 200.0, home

    _at(db, SAT_12, 18)
    gateways.append(FakeGateway([proposal_for(menu_mod.Segment(MON_14, dt.date(2026, 9, 18)))]))
    run_job(menu_propose.JOB, SAT_12)
    assert gateways[0].calls, "the menu builder was asked"
    told = _line(gateways[0].calls[0], "Leftovers to use up first:")
    assert "Chicken" in told and "Broccoli" in told, (
        "the fridge holds 100 g of chicken and 200 g of broccoli, both going off in days. The "
        f"menu builder was told only about the jars: {told!r}"
    )


# ---------------------------------------------------------------- 6. what can I make
def test_06_i_have_chicken_and_rice_what_can_i_make_has_something_to_read(
    fixture_db, two_persons
) -> None:
    """Wednesday evening, fridge door open: "I have chicken and rice, what can I make?" goes to
    the chat. To answer, the chat can only call its tools - so one of them has to know what the
    tiffin believes is already in the kitchen."""
    db, pid = fixture_db, two_persons["owner"]
    chicken = seed_ingredient(db, "chicken", "Chicken breast", "meat", 500, "500 g pack")
    rice = seed_ingredient(db, "rice", "Basmati rice", "grains", 1000, "1 kg bag")
    dish = seed_recipe(db, "Chicken and rice", 1, {chicken: 180, rice: 80})
    menu_id = _accepted(db, pid, MON_7, 5, {"dinner": (dish, 1.0)})
    shopping.build_once(db, menu_id)
    assert shopping.current_leftovers(db, pid, MON_7) == {"chicken": 100.0, "rice": 600.0}

    outputs: dict[str, str] = {}
    for name in tools.READ_TOOLS:
        try:
            outputs[name] = json.dumps(tools.call(db, pid, name, {}), default=str)
        except Exception as e:  # a tool that cannot run without args is still a tool
            outputs[name] = f"error: {e}"
    knows = [n for n, out in outputs.items() if "leftover" in out.lower() or "600" in out]
    assert knows, (
        "Tiffin holds 600 g of rice and 100 g of chicken as in the kitchen, and none of "
        f"the chat's {len(outputs)} read tools ({', '.join(outputs)}) returns it"
    )


# ---------------------------------------------------------------- 7 & 8. why one pack, not two
def _chicken_two_weeks(world) -> tuple:  # noqa: F811
    """Last week's list leaves 250 g of chicken; this week needs 1000 g, so it buys one pack
    fewer than the week eats."""
    pid, db, tg, _gw, say, tap, _run = world
    _accepted(db, pid, MON_7, 5, {"dinner": (_chicken_dish(db, "Chicken and rice", 150), 1.0)})
    _at(db, SUN_6)
    say("/shopping")  # 750 g -> 2 packs, 250 g spare
    for _text, data in _buttons(tg.sent[-1]):
        tap(data)
    _accepted(db, pid, MON_14, 5, {"dinner": (_chicken_dish(db, "Chicken traybake", 200), 1.0)})
    _at(db, SUN_13)
    return pid, db, say


# Pinned the credit: now runs with `shopping.count_on_home` on, because the default buys everything.
@pytest.mark.usefixtures("count_on_home")
def test_07_the_list_reread_in_the_shop_says_it_is_counting_on_chicken_at_home(
    world,  # noqa: F811
) -> None:
    """Sunday in the shop. The person reports 250 g of chicken, so /shopping buys one pack fewer
    than the week eats. The list must show that credit - if the
    chicken went in the bin, he has to buy another pack now, not find out on Thursday."""
    pid, db, say = _chicken_two_weeks(world)
    db.insert("leftovers", person_id=pid, ingredient_id=_id(db, "chicken"), grams=250.0)
    text = say("/shopping")
    row = db.one(
        "SELECT si.packs, si.credit_g FROM shopping_items si JOIN ingredients i "
        "ON i.id = si.ingredient_id WHERE i.code = 'chicken' ORDER BY si.id DESC LIMIT 1"
    )
    assert row is not None and float(row["credit_g"]) == 250.0 and int(row["packs"]) == 2
    chicken = _line(text, "Chicken breast")
    assert "250 g" in chicken, (
        "the stored list credits 250 g from the fridge (the 18:00 message says '(250 g in)'); "
        f"the /shopping reread drops it: {chicken!r}"
    )


# Pinned the credit: now runs with `shopping.count_on_home` on, because the default buys everything.
@pytest.mark.usefixtures("count_on_home")
def test_08_the_web_list_says_what_it_is_counting_on_being_at_home(world) -> None:  # noqa: F811
    """Same Sunday, the list open on the tailnet page on his phone. The page is where the long
    list is read, and it should say the same thing about the fridge that the bot does."""
    pid, db, say = _chicken_two_weeks(world)
    db.insert("leftovers", person_id=pid, ingredient_id=_id(db, "chicken"), grams=250.0)
    say("/shopping")
    page = queries.shopping(db, pid, None)
    chicken = next(i for i in page["items"] if str(i["name"]).startswith("Chicken"))
    assert float(chicken.get("credit_g") or 0) == 250.0, (
        f"the web list item carries no credit, so the page cannot say '250 g in': {chicken}"
    )


# ---------------------------------------------------------------- 9. changed before the shop
def test_09_a_menu_changed_before_the_shop_does_not_count_food_nobody_bought(
    world,  # noqa: F811
) -> None:
    """Sunday. He looked at the list for the first menu at 10:00 and did not go out. At noon he
    swapped it for a bigger chicken traybake, 240 g a day. There is no chicken in the house.
    He wants the list to buy the week's chicken."""
    pid, db, _tg, _gw, say, _tap, _run = world
    _accepted(db, pid, MON_14, 5, {"dinner": (_chicken_dish(db, "Chicken and rice", 150), 1.0)})
    _at(db, SUN_13, 10)
    first = say("/shopping")
    assert "2 x 500 g pack" in _line(first, "Chicken breast"), first  # 750 g, 250 g spare

    _accepted(db, pid, MON_14, 5, {"dinner": (_chicken_dish(db, "Chicken traybake", 240), 1.0)})
    _at(db, SUN_13, 12)
    second = say("/shopping")
    chicken = _line(second, "Chicken breast")
    assert "3 x 500 g pack" in chicken, (
        "the week needs 1200 g and nothing was bought - no tick, no shop. The new list credits "
        f"the 250 g the FIRST list only planned to have spare, and Friday is short: {chicken!r}"
    )


# ---------------------------------------------------------------- 10. two boxes in the freezer
def test_10_boxes_frozen_for_days_owner_was_away_are_counted_before_cooking_more(
    world,  # noqa: F811
) -> None:
    """Sunday 13 Sep, about to batch cook. Last Sunday /batch told him to freeze Thursday's and
    Friday's chilli, then he was away Thursday to Friday with tracking paused. Two boxes of
    chilli are in the freezer and this week is the same chilli. He wants /batch to cook three."""
    pid, db, _tg, _gw, say, _tap, _run = world
    base = db.one("SELECT * FROM profiles WHERE person_id = ?", (pid,))
    cols = {k: base[k] for k in base.keys() if k not in ("id", "created_at")}  # noqa: SIM118
    db.insert("profiles", **(cols | {"effective_from": "2026-09-02", "freezes": 1}))
    mince = seed_ingredient(db, "mince", "Beef mince 5%", "meat", 500, "500 g pack")
    chilli = seed_recipe(db, "Chilli con carne", 1, {mince: 150})
    _accepted(db, pid, MON_7, 5, {"dinner": (chilli, 1.0)})
    _at(db, SUN_6, 14)
    last = say("/batch")
    assert "Freeze" in last and "Thu" in last and "Fri" in last, last
    db.insert(
        "pause_periods",
        person_id=pid,
        start_day="2026-09-10",
        end_day="2026-09-11",
        reason="away",
        created_at="2026-09-09 18:00:00",
    )

    _accepted(db, pid, MON_14, 5, {"dinner": (chilli, 1.0)})
    _at(db, SUN_13, 14)
    this = say("/batch")
    assert "freezer" in this.lower() or "Chilli con carne: 3 boxes" in this, (
        "Tiffin told him to freeze two chilli boxes and knows he was not there to eat "
        f"them; this week's cook asks for five new ones as if the freezer were empty: {this}"
    )


# ---------------------------------------------------------------- 11. the shared weekend bag
# Pinned the credit: now runs with `shopping.count_on_home` on, because the default buys everything.
@pytest.mark.usefixtures("count_on_home")
def test_11_the_weekend_list_counts_both_halves_of_the_potatoes_at_home(world) -> None:  # noqa: F811
    """Both people report 400 g of potatoes at home. The next joint weekend shop credits both
    shares against its 1600 g need."""
    pid, db, tg, _gw, _say, _tap, run_job = world
    partner = int(db.scalar("SELECT id FROM persons WHERE slug = 'partner'"))
    potato = seed_ingredient(db, "potato", "Potatoes, raw", "produce", 1000, "1 kg bag")
    roast = seed_recipe(db, "Roast chicken and potatoes", 1, {potato: 500})
    _accepted(db, pid, dt.date(2026, 9, 5), 2, {"dinner": (roast, 0.7)})
    _accepted(db, partner, dt.date(2026, 9, 5), 2, {"dinner": (roast, 0.5)})
    _at(db, dt.date(2026, 9, 4), 18)
    run_job(shopping_list.JOINT_JOB, dt.date(2026, 9, 4))  # 1200 g -> 2 bags, 800 g spare
    shares = (
        shopping.current_leftovers(db, pid, FRI_11).get("potato"),
        shopping.current_leftovers(db, partner, FRI_11).get("potato"),
    )
    assert shares == (400.0, 400.0), shares

    _accepted(db, pid, SAT_12, 2, {"dinner": (roast, 1.0)})
    _accepted(db, partner, SAT_12, 2, {"dinner": (roast, 0.6)})
    _at(db, FRI_11, 18)
    for owner in (pid, partner):
        db.insert("leftovers", person_id=owner, ingredient_id=potato, grams=400.0)
    tg.sent.clear()
    run_job(shopping_list.JOINT_JOB, FRI_11)
    assert tg.sent, "the weekend list went out"
    text = tg.sent[-1]["text"]
    potatoes = _line(text, "Potatoes")
    assert "1 × 1 kg" in potatoes, (  # noqa: RUF001 - the list's own multiplication sign
        "800 g are in the shared cupboard and the weekend eats 1600 g: one bag. The joint list "
        f"credits only Alex's 400 g half and says: {potatoes!r}"
    )


# ---------------------------------------------------------------- 12. the refused tin
def test_12_the_next_menu_is_not_told_to_use_up_chickpeas_that_are_refused(
    world,  # noqa: F811
) -> None:
    """Saturday 18:00. Last week's lunch bought two tins of chickpeas for 250 g, so 230 g are
    "left over" - and since then chickpeas went on the avoid list. The live box holds exactly
    this: 189 g of her refused chickpeas and 69 g of his. Next week's builder must not be told
    to use them up."""
    pid, db, _tg, gateways, say, _tap, run_job = world
    beans = seed_ingredient(db, "chickpeas", "Chickpeas, tinned, drained", "tins", 240, "400 g tin")
    salad = seed_recipe(db, "Chickpea salad", 1, {beans: 50.0, _id(db, "chicken"): 150.0})
    _accepted(db, pid, MON_7, 5, {"lunch": (salad, 1.0)})
    _at(db, SUN_6)
    say("/shopping")  # 250 g of chickpeas -> 2 x 240 g, 230 g spare
    assert shopping.current_leftovers(db, pid, SAT_12).get("chickpeas") == 230.0

    base = db.one("SELECT * FROM profiles WHERE person_id = ?", (pid,))
    cols = {k: base[k] for k in base.keys() if k not in ("id", "created_at")}  # noqa: SIM118
    db.insert(
        "profiles",
        **(cols | {"effective_from": "2026-09-08", "refused_foods": json.dumps(["Chickpea"])}),
    )

    _at(db, SAT_12, 18)
    gateways.append(FakeGateway([proposal_for(menu_mod.Segment(MON_14, dt.date(2026, 9, 18)))]))
    run_job(menu_propose.JOB, SAT_12)
    assert gateways[0].calls, "the menu builder was asked"
    prompt = gateways[0].calls[0]
    offered = [ln for ln in prompt.splitlines() if ln[:1].isdigit() and "Chickpea" in ln]
    assert not offered, f"the catalogue half already obeys the avoid list: {offered}"
    told = _line(prompt, "Leftovers to use up first:")
    assert "Chickpea" not in told, (
        "chickpeas are refused and are kept out of the ingredient list, yet the same prompt "
        f"tells the builder to use them up first: {told!r}"
    )
