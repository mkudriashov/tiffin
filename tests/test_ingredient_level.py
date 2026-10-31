"""Change food at the level of one food: a food refused by itself, one food changed inside a dish,
and the foods made from a refused food asked about once.
"""

from __future__ import annotations

import datetime as dt

import pytest

from tiffin.food import catalogue, prefs, recipes, subst, swap
from tiffin.food.catalogue import Ingredient

from .test_food_shopping import seed_ingredient
from .test_swap import (  # noqa: F401 (fixtures)
    FRI,
    MON,
    _buttons,
    _headers,
    bot,
    page,
    week,
)


def _food(name: str, aisle: str, k: float, p: float, f: float, c: float, fb: float = 0.0):
    return Ingredient(
        id=0,
        code=name.lower().replace(" ", "_"),
        version=1,
        name=name,
        cofid_code=None,
        kcal_100=k,
        protein_100=p,
        fat_100=f,
        carb_100=c,
        fibre_100=fb,
        pack_g=500,
        pack_label="500 g",
        aisle=aisle,
        pantry=False,
        use_whole_pack=False,
        approved=True,
        person_scope="both",
    )


# ---------------------------------------------------------------- the part a food plays
@pytest.mark.parametrize(
    ("name", "aisle", "macros", "role"),
    [
        ("Red lentils, dry", "grains", (318, 24, 1.3, 50), "pulse"),
        ("Chickpeas, tinned, drained", "tins", (115, 7, 2.6, 13), "pulse"),
        ("Kidney beans, tinned, drained", "tins", (100, 7, 0.5, 13), "pulse"),
        ("Green beans, raw", "produce", (30, 2, 0.2, 4), "veg"),
        ("Peanut butter, smooth", "condiments", (600, 25, 50, 15), "condiments"),
        ("Chicken breast, raw", "meat", (120, 24, 2, 0), "protein"),
        ("Tofu, firm", "produce", (120, 13, 7, 1), "protein"),
        ("Whey protein powder", "grains", (380, 78, 6, 6), "protein"),
        ("Basmati rice, dry", "grains", (350, 8, 1, 78), "starch"),
        ("Potatoes, raw", "produce", (75, 2, 0.2, 17), "starch"),
        ("Greek yoghurt 0% fat", "dairy", (57, 10, 0.4, 4), "dairy"),
        ("Cottage cheese", "dairy", (98, 11, 4, 3), "dairy"),
        ("Feta cheese", "dairy", (264, 14, 21, 4), "cheese"),
        ("Olive oil", "condiments", (899, 0, 100, 0), "fat"),
        ("Bananas", "produce", (89, 1, 0.3, 23), "fruit"),
        ("Cherry tomatoes, raw", "produce", (18, 1, 0.2, 3), "veg"),
    ],
)
def test_each_food_plays_a_part_and_only_a_food_playing_it_can_stand_in(
    name, aisle, macros, role
) -> None:
    """Lentils are shelved with the grains and chickpeas with the tins: the aisle alone would
    never offer one for the other. The part is read from the name, the aisle and the macros."""
    assert subst.role_of(_food(name, aisle, *macros)) == role


def test_grams_follow_the_part_not_the_model() -> None:
    lentils = _food("Red lentils, dry", "grains", 318, 24, 1.3, 50)
    beans = _food("Kidney beans, tinned, drained", "tins", 100, 7, 0.5, 13)
    chicken = _food("Chicken breast, raw", "meat", 120, 24, 2, 0)
    turkey = _food("Turkey breast, raw", "meat", 105, 30, 1, 0)
    broccoli = _food("Broccoli, raw", "produce", 35, 4, 0.4, 2)
    beans_g = subst.grams_for(lentils, 80, beans, None)
    assert beans_g == 255, "a pulse calorie for calorie: 80 g dry lentils is ~255 g tinned beans"
    assert subst.grams_for(chicken, 180, turkey, None) == 145, "a protein, protein for protein"
    assert subst.grams_for(broccoli, 150, _food("Green beans", "produce", 30, 2, 0, 4), None) == 150
    egg = catalogue.Unit("egg", 50.0)
    assert subst.grams_for(chicken, 100, _food("Eggs", "dairy", 140, 12.5, 10, 0), egg) == 200, (
        "a food served whole, in whole ones: 4 eggs"
    )
    assert subst.grams_for(broccoli, 100, _food("Spinach", "produce", 5, 3, 0, 1), None) <= 400, (
        "never more than four times the food it replaces"
    )


def test_nearer_macros_are_a_smaller_distance() -> None:
    chicken = _food("Chicken breast, raw", "meat", 120, 24, 2, 0)
    turkey = _food("Turkey breast, raw", "meat", 105, 30, 1, 0)
    salmon = _food("Salmon fillets, raw", "fish", 200, 20, 13, 0)
    assert subst.macro_distance(chicken, turkey) < subst.macro_distance(chicken, salmon)
    assert subst.macro_distance(chicken, chicken) == 0


# ---------------------------------------------------------------- one food changed inside a dish
def test_the_foods_that_can_stand_in_play_the_same_part_and_are_partners(week) -> None:  # noqa: F811
    db, pid, _base, _r, clock = week
    today = clock.today_local()
    lentils = db.scalar("SELECT code FROM ingredients WHERE code = 'lentils'")
    got = subst.options(db, pid, MON, "lunch", lentils, today)
    assert got["dish"] == "Lentil dal" and got["role"] == "pulse"
    assert [o["name"] for o in got["options"]] == ["Chickpeas"]
    beans = got["options"][0]
    assert beans["grams"] > 80, "tinned for dry, calorie for calorie"
    prefs.amend(db, pid, "avoid", add=["chickpeas"], clock=clock)
    assert subst.options(db, pid, MON, "lunch", "lentils", today)["options"] == [], (
        "a food she avoids never stands in"
    )


def test_changing_one_food_writes_a_new_dish_and_solves_each_day_again(week, config) -> None:  # noqa: F811
    db, pid, _base, r, clock = week
    old_ids = {p.recipe_id for p in swap.plates_on(db, pid, MON)}
    got = subst.substitute(
        db, pid, MON, "lunch", "lentils", "chickpeas", config=config, clock=clock
    )
    assert got.new == "Chickpea dal"
    assert got.days == tuple(MON + dt.timedelta(days=i) for i in range(5))
    plates = {p.slot: p for p in swap.plates_on(db, pid, FRI)}
    lunch = plates["lunch"]
    assert lunch.name == "Chickpea dal" and lunch.recipe_id not in old_ids
    codes = {s.ingredient.code for s in recipes.stored_ingredients(db, lunch.recipe_id)}
    assert codes == {"chickpeas", "broccoli", "pepper"}
    assert {s.ingredient.code for s in recipes.stored_ingredients(db, r["dal"])} >= {"lentils"}, (
        "the old dish is untouched: a ledger"
    )
    target = 1300
    for d, total in got.totals.items():
        assert abs(total.kcal - target) <= target * 0.05, (d, total)
    events = db.all("SELECT code, kind FROM ingredient_events WHERE person_id = ?", (pid,))
    assert {(e["code"], e["kind"]) for e in events} == {
        ("lentils", "swapped_out"),
        ("chickpeas", "swapped_in"),
    }


def test_a_food_not_on_offer_is_refused(week, config) -> None:  # noqa: F811
    db, pid, _base, _r, clock = week
    with pytest.raises(ValueError, match="can stand in here"):
        subst.substitute(db, pid, MON, "lunch", "lentils", "salmon", config=config, clock=clock)
    with pytest.raises(ValueError, match="not in this dish"):
        subst.substitute(db, pid, MON, "lunch", "salmon", "chicken", config=config, clock=clock)


def test_the_food_put_in_leans_partner_taste_to_it(week, config) -> None:  # noqa: F811
    from tiffin.food import taste

    db, pid, _base, _r, clock = week
    before = taste.affinity(db, pid, MON).get("chickpeas", 0.0)
    subst.substitute(db, pid, MON, "lunch", "lentils", "chickpeas", config=config, clock=clock)
    after = taste.affinity(db, pid, MON + dt.timedelta(days=1))
    assert after["chickpeas"] > before
    assert after["lentils"] < 0


def test_a_name_without_the_food_gets_it_added() -> None:
    old = _food("Red lentils, dry", "grains", 318, 24, 1.3, 50)
    new = _food("Chickpeas, tinned", "tins", 115, 7, 2.6, 13)
    assert subst._renamed("Red lentil soup", old, new) == "Chickpea soup"
    assert subst._renamed("Rinse the lentils.", old, new) == "Rinse the chickpeas."
    assert subst._renamed("Dal", old, new) == "Dal"


# ---------------------------------------------------------------- refused by itself
def test_a_food_refused_by_itself_reaches_no_further_than_the_food(week) -> None:  # noqa: F811
    """ "Eggs" on the avoid list also keeps egg whites off; a tap on Eggs keeps off eggs."""
    db, pid, _base, _r, clock = week
    eggs = seed_ingredient(db, "eggs", "Eggs, whole, raw", "dairy", 600.0, "6 eggs")
    whites = seed_ingredient(db, "egg_whites", "Egg whites, raw", "dairy", 500.0, "500 g")
    assert eggs and whites
    got = prefs.refuse_food(db, pid, "eggs", clock=clock)
    assert got.changed and got.said() == "Kept off your menus from now on: Eggs."
    allowed = {i.code for i in catalogue.approved(db, pid)}
    assert "eggs" not in allowed and "egg_whites" in allowed
    view = prefs.view(db, pid, clock.today_local())
    assert [f["name"] for f in view["avoid_foods"]] == ["Eggs"]
    assert prefs.refuse_food(db, pid, "eggs", clock=clock).changed is False
    back = prefs.allow_food(db, pid, "eggs", clock=clock)
    assert back.said() == "Allowed back: Eggs."
    assert "eggs" in {i.code for i in catalogue.approved(db, pid)}
    with pytest.raises(LookupError):
        prefs.refuse_food(db, pid, "no_such_food", clock=clock)


def test_a_dish_holding_a_food_refused_by_itself_is_still_on_the_menu_named(week) -> None:  # noqa: F811
    db, pid, _base, _r, clock = week
    prefs.refuse_food(db, pid, "lentils", clock=clock)
    still = prefs.still_on_menu(db, pid, clock.today_local())
    assert [d["name"] for d in still] == ["Lentil dal"]
    assert still[0]["holds"] == ["Red lentils"]


# ---------------------------------------------------------------- houmous, asked once
def test_refusing_chickpeas_asks_once_about_houmous(week) -> None:  # noqa: F811
    db, pid, _base, _r, clock = week
    seed_ingredient(db, "houmous", "Houmous", "condiments", 200.0, "200 g tub")
    got = prefs.amend(db, pid, "avoid", add=["chickpeas"], clock=clock)
    assert [a["name"] for a in got.asks] == ["Houmous"]
    assert prefs.ask_said(got.asks[0]) == (
        "Houmous is made from chickpeas: keep it off too, or is it fine?"
    )
    today = clock.today_local()
    assert [a["code"] for a in prefs.asks(db, pid, today)] == ["houmous"], "the page asks too"
    assert "houmous" in {i.code for i in catalogue.approved(db, pid)}, "not assumed either way"
    prefs.answer(db, pid, "houmous", keep_off=False, clock=clock)
    assert prefs.asks(db, pid, today) == [], "asked once: the answer is kept"
    assert "houmous" in {i.code for i in catalogue.approved(db, pid)}
    again = prefs.amend(db, pid, "avoid", add=["chickpea salad"], strict=False, clock=clock)
    assert again.asks == ()


def test_saying_keep_it_off_refuses_houmous_by_itself(week) -> None:  # noqa: F811
    db, pid, _base, _r, clock = week
    seed_ingredient(db, "houmous", "Houmous", "condiments", 200.0, "200 g tub")
    prefs.amend(db, pid, "avoid", add=["chickpeas"], clock=clock)
    prefs.answer(db, pid, "houmous", keep_off=True, clock=clock)
    assert "houmous" not in {i.code for i in catalogue.approved(db, pid)}
    assert prefs.asks(db, pid, clock.today_local()) == []


def test_pulses_on_the_list_already_keep_houmous_off_so_nothing_is_asked(week) -> None:  # noqa: F811
    db, pid, _base, _r, clock = week
    seed_ingredient(db, "houmous", "Houmous", "condiments", 200.0, "200 g tub")
    got = prefs.amend(db, pid, "avoid", add=["pulses"], strict=False, clock=clock)
    assert got.asks == ()


# ---------------------------------------------------------------- the bot
def test_the_bot_changes_one_food_in_a_dish_from_the_swap_list(bot) -> None:  # noqa: F811
    send, tap, db, pid, _r, _clock = bot
    out = send("/swap monday lunch")
    one = _buttons(out[0])["Change one food in it"]
    pick = tap(one)
    assert "Change one food in Lentil dal" in pick[-1]["text"]
    foods = _buttons(pick[-1])
    assert set(foods) == {"Red lentils", "Broccoli", "Red pepper"}
    offer = tap(foods["Red lentils"])
    text = offer[-1]["text"]
    assert "Instead of Red lentils" in text and "Chickpeas" in text
    choices = _buttons(offer[-1])
    assert "Never red lentils again" in choices
    done = tap(choices["Chickpeas"])
    assert "Chickpea dal in for Lentil dal" in done[-1]["text"]
    assert [p.name for p in swap.plates_on(db, pid, FRI)][1] == "Chickpea dal"


def test_never_again_from_the_bot_keeps_that_one_food_off(bot) -> None:  # noqa: F811
    send, tap, db, pid, _r, _clock = bot
    out = send("/swap monday lunch")
    pick = tap(_buttons(out[0])["Change one food in it"])
    offer = tap(_buttons(pick[-1])["Red lentils"])
    never = tap(_buttons(offer[-1])["Never red lentils again"])
    text = never[-1]["text"]
    assert "Kept off your menus from now on: Red lentils." in text
    assert "Lentil dal" in text, "the dish still holding it is named"
    assert "lentils" in catalogue.refused_codes(db, pid)
    undo = tap(_buttons(never[-1])["Undo"])
    assert "Allowed back: Red lentils." in undo[-1]["text"]
    assert "lentils" not in catalogue.refused_codes(db, pid)


def test_avoid_lists_everything_kept_off_with_a_way_back(bot) -> None:  # noqa: F811
    send, tap, db, pid, _r, clock = bot
    assert "Nothing is kept off" in send("/avoid")[0]["text"]
    added = send("/avoid salmon, mushrooms")
    assert "Kept off your menus from now on: salmon" in added[0]["text"]
    prefs.refuse_food(db, pid, "lentils", clock=clock)
    out = send("/avoid")
    text = out[0]["text"]
    assert "Kept off your menus" in text and "salmon" in text and "By itself: Red lentils" in text
    buttons = _buttons(out[0])
    assert {"Allow mushrooms", "Allow salmon", "Allow Red lentils"} <= set(buttons)
    back = tap(buttons["Allow salmon"])
    assert "Allowed back: salmon" in back[-1]["text"]
    assert catalogue.refused_terms(db, pid) == ["mushrooms"]
    again = tap(buttons["Allow salmon"])
    assert "Already back on your menus." in again[-1]["text"]


def test_the_bot_asks_once_about_houmous(bot) -> None:  # noqa: F811
    send, tap, db, pid, _r, _clock = bot
    seed_ingredient(db, "houmous", "Houmous", "condiments", 200.0, "200 g tub")
    out = send("I do not want chickpeas")
    assert "Houmous is made from chickpeas: keep it off too, or is it fine?" in out[0]["text"]
    buttons = _buttons(out[0])
    fine = tap(buttons["Houmous is fine"])
    assert "Houmous stays on your menus." in fine[-1]["text"]
    assert "houmous" in {i.code for i in catalogue.approved(db, pid)}
    later = send("/avoid")
    assert "Houmous is made from" not in later[0]["text"], "asked once"


# ---------------------------------------------------------------- the page
def test_the_page_changes_one_food_in_a_dish(page) -> None:  # noqa: F811
    client, db, partners, _his, _r = page
    h = _headers(client)
    got = client.get(
        "/api/menu/swap-food",
        params={"day": "2026-09-14", "slot": "lunch", "food": "lentils"},
        headers=h,
    )
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["food"]["name"] == "Red lentils" and body["role"] == "pulse"
    assert [o["code"] for o in body["options"]] == ["chickpeas"]
    done = client.post(
        "/api/menu/swap-food",
        json={"day": "2026-09-14", "slot": "lunch", "food": "lentils", "instead": "chickpeas"},
        headers=h,
    )
    assert done.status_code == 200, done.text
    assert "Chickpea dal in for Lentil dal" in done.json()["said"]
    assert [p.name for p in swap.plates_on(db, partners, FRI)][1] == "Chickpea dal"
    bad = client.post(
        "/api/menu/swap-food",
        json={"day": "2026-09-14", "slot": "lunch", "food": "chickpeas", "instead": "salmon"},
        headers=h,
    )
    assert bad.status_code == 400
    junk = client.get(
        "/api/menu/swap-food",
        params={"day": "2026-09-14", "slot": "lunch", "food": "x'; drop"},
        headers=h,
    )
    assert junk.status_code == 400


def test_the_page_refuses_one_food_by_itself_and_has_it_back(page) -> None:  # noqa: F811
    client, db, partners, _his, _r = page
    h = _headers(client)
    got = client.post("/api/prefs/food", json={"food": "lentils", "refused": True}, headers=h)
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["said"] == "Kept off your menus from now on: Red lentils."
    assert [f["code"] for f in body["prefs"]["avoid_foods"]] == ["lentils"]
    assert catalogue.refused_terms(db, partners) == [], "no word written: the food itself"
    back = client.post("/api/prefs/food", json={"food": "lentils", "refused": False}, headers=h)
    assert back.json()["prefs"]["avoid_foods"] == []
    other = client.post(
        "/api/prefs/food",
        json={"food": "lentils", "refused": True, "person_id": 1},
        headers=h,
    )
    assert other.status_code == 400, "a person never comes from the body"
    nope = client.post("/api/prefs/food", json={"food": "nope", "refused": True}, headers=h)
    assert nope.status_code == 404
