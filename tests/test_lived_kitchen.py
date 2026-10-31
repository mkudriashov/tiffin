"""Ten moments with the hob already on.

Every story here stands in one place at one time - a person holding a phone with wet hands,
a Sunday afternoon and one pan, a dish that turned out to be horrible - and asks the only
question that matters there: Tiffin holds the right answer, so can this person reach it
from where they are standing, and in how many steps?
"""

from __future__ import annotations

import datetime as dt

import pytest

from tiffin import targets as targets_mod
from tiffin.food import batch, recipes
from tiffin.food import menu as menu_mod
from tiffin.jobs import menu_propose
from tiffin.telegram import menuflow
from tiffin.telegram.batchflow import BatchFlow

from .test_menu import FakeGateway, world  # noqa: F401

SATURDAY = dt.date(2026, 9, 12)  # the `world` fixture's frozen today, 18:00
SUNDAY = dt.date(2026, 9, 13)
MONDAY = dt.date(2026, 9, 14)
FIVE_SLOTS = ("breakfast", "lunch", "dinner", "snack", "pre_bed")


# ---------------------------------------------------------------- kitchen props
def _ingredient(
    db,
    code: str,
    name: str,
    *,
    pack_g: float = 500.0,
    pack_label: str = "500 g pack",
    aisle: str = "meat",
    pantry: int = 0,
) -> int:
    return db.insert(
        "ingredients",
        code=code,
        version=1,
        name=name,
        kcal_100=150.0,
        protein_100=20.0,
        fat_100=5.0,
        carb_100=5.0,
        fibre_100=1.0,
        pack_g=pack_g,
        pack_label=pack_label,
        aisle=aisle,
        pantry=pantry,
        approved=1,
        person_scope="both",
    )


def _recipe(
    db,
    name: str,
    steps: list[str],
    *,
    servings: int = 1,
    keep_days: int = 3,
    freezable: int = 0,
    prep_minutes: int = 25,
) -> int:
    import json

    return db.insert(
        "recipes",
        name=name,
        servings=servings,
        prep_minutes=prep_minutes,
        method_json=json.dumps(steps),
        source="manual",
        keep_days=keep_days,
        freezable=freezable,
    )


def _menu(
    db,
    person_id: int,
    days: list[dt.date],
    plan: dict[str, tuple[int, float]],
    *,
    status: str = "accepted",
) -> int:
    menu_id = db.insert(
        "menus",
        person_id=person_id,
        segment_start=days[0].isoformat(),
        segment_end=days[-1].isoformat(),
        status=status,
    )
    for day in days:
        menu_day = db.insert(
            "menu_days",
            menu_id=menu_id,
            day=day.isoformat(),
            kcal=2200,
            protein_g=180.0,
            fat_g=70.0,
            carb_g=200.0,
            fibre_g=30.0,
        )
        for slot, (recipe_id, multiplier) in plan.items():
            db.insert(
                "meal_slots",
                menu_day_id=menu_day,
                slot=slot,
                recipe_id=recipe_id,
                portion_multiplier=multiplier,
                portion_g=400.0,
                kcal=600,
                protein_g=45.0,
                fat_g=18.0,
                carb_g=55.0,
                fibre_g=7.0,
            )
    return menu_id


def _taps(sent: dict) -> list[str]:
    markup = sent.get("reply_markup") or {}
    return [b["callback_data"] for row in markup.get("inline_keyboard", []) for b in row]


# ---------------------------------------------------------------- 1. hands wet, hob on
def test_01_a_long_recipe_card_still_says_how_long_the_food_keeps(fixture_db) -> None:
    """Twelve ingredients and twelve steps is the biggest recipe Tiffin will accept
    (recipes.MAX_INGREDIENTS, recipes.MAX_STEPS) and an ordinary curry out of a book.

    The card is one message, so something has to give when it does not fit, and what gives is
    decided by a `while len(lines) > RECIPE_MAX_LINES: lines.pop(-2)` loop that says nothing.
    Decision 102 names fridge/freezer life as part of the card; standing over the pan it is the
    line that decides whether Thursday's box is safe, and it is the first thing dropped.
    """
    db = fixture_db
    steps = [f"Step {n} of the method" for n in range(1, 13)]
    recipe_id = _recipe(db, "Thai green curry from the book", steps, keep_days=3, freezable=1)
    for n in range(1, 13):
        ing = _ingredient(db, f"thing{n}", f"Ingredient {n}")
        db.insert("recipe_ingredients", recipe_id=recipe_id, ingredient_id=ing, grams=60.0)

    card = recipes.card(db, recipe_id)
    assert len(card["ingredients"]) == recipes.MAX_INGREDIENTS
    assert len(card["steps"]) == recipes.MAX_STEPS
    assert card["keep_days"] == 3, "the card itself knows"

    text = menuflow.recipe_card_text(card)
    lost = [n for n, step in enumerate(steps, 1) if f"{n}. {step}" not in text]
    assert not lost, f"method steps {lost} never reach the phone, and nothing says so"
    assert "Keeps 3 days" in text, (
        "the card computed keep_days=3 and freezable, and the renderer dropped the line "
        "rather than say it had to"
    )


# ---------------------------------------------------------------- 2. the dish has a name
def test_02_asking_for_a_dish_by_name_does_not_answer_with_a_different_dish(world) -> None:  # noqa: F811
    """The breakfast is the one being cooked and its name is the only handle the person has.

    `/recipe` reads its argument with `arg.strip().isdigit()`: anything that is not a bare
    integer falls through to "the day's MAIN meal", so naming a dish - or asking for it doubled
    because two people are eating - hands back tonight's dinner as though that were the answer.
    The rating keyboard on that card belongs to the dinner too, so a 5 tapped for the porridge
    is written against the chilli.
    """
    pid, db, _tg, _gateways, say, _tap, _run_job = world
    oats = _ingredient(db, "jumbo_oats", "Jumbo porridge oats", aisle="grains")
    mince = _ingredient(db, "mince", "Beef mince, 5%")
    porridge = _recipe(db, "Porridge with berries", ["Cook the oats in the milk."])
    chilli = _recipe(db, "Chilli con carne", ["Brown the mince.", "Simmer for an hour."])
    db.insert("recipe_ingredients", recipe_id=porridge, ingredient_id=oats, grams=80.0)
    db.insert("recipe_ingredients", recipe_id=chilli, ingredient_id=mince, grams=225.0)
    _menu(db, pid, [SATURDAY], {"breakfast": (porridge, 1.0), "dinner": (chilli, 1.0)})

    out = say("/recipe Porridge with berries")
    assert "Chilli con carne" not in out, (
        "they named the porridge and were handed the chilli card, with nothing saying the "
        "name was ignored"
    )
    assert "Porridge" in out, "either the dish they asked for, or a word about why not"


# ---------------------------------------------------------------- 3. the whole menu
def test_03_every_dish_on_an_accepted_menu_can_be_reached_from_menu(world) -> None:  # noqa: F811
    pid, db, tg, _gateways, say, tap, _run_job = world
    wanted: list[str] = []
    for label, days in (("Saturday", [SATURDAY]), ("Monday", [MONDAY])):
        plan: dict[str, tuple[int, float]] = {}
        for slot in FIVE_SLOTS:
            name = f"{label} {slot.replace('_', ' ')}"
            plan[slot] = (_recipe(db, name, ["Cook it."]), 1.0)
            wanted.append(name)
        menu_id = _menu(db, pid, days, plan, status="proposed")
        menu_mod.decide(db, menu_id, "accepted")

    # `/menu` shows ONE day and a button per day, because grouping a whole week by slot reads
    # "Lunch: Chicken salad (Mon, Wed) - Tuna wrap (Tue)", which nobody can cook from. So the
    # bar is REACHABILITY, not presence in one message: every dish has to be openable without
    # typing a command from memory.
    out = say("/menu")
    seen = set(name for name in wanted if name in out)
    for data in [d for d in _taps(tg.sent[-1]) if d.startswith("k:mday:")]:
        seen |= {name for name in wanted if name in tap(data)}
    missing = [name for name in wanted if name not in seen]
    assert not missing, (
        f"{len(missing)} dishes on accepted menus cannot be reached from /menu, by text or by "
        f"a day button: {missing}"
    )


# ---------------------------------------------------------------- 4. Sunday, one pan
def test_04_four_boxes_of_a_recipe_that_serves_four_is_one_pot_not_four(
    fixture_db, two_persons
) -> None:
    """A stew written for four people fills four boxes with one cook.

    `_amounts` divides recipe grams by `servings`, which is why the shopping is right. `plan`
    does not: `multiples` is the bare sum of the portion multipliers, and `/batch` prints it as
    "4 times the recipe". The same message then lists the beans for one pot. One of the two
    numbers sends somebody to the shop for four times the food.
    """
    db, pid = fixture_db, two_persons["owner"]
    beans = _ingredient(db, "beans", "Black beans", aisle="tins")
    stew = _recipe(db, "Bean stew", ["Simmer for an hour."], servings=4, keep_days=4, freezable=1)
    whole_recipe_g = 800.0
    db.insert("recipe_ingredients", recipe_id=stew, ingredient_id=beans, grams=whole_recipe_g)
    days = [MONDAY + dt.timedelta(days=i) for i in range(4)]
    menu_id = _menu(db, pid, days, {"dinner": (stew, 1.0)})

    row = db.one("SELECT * FROM menus WHERE id = ?", (menu_id,))
    load = batch.plan(db, row, ("dinner",), SUNDAY)
    dish = load.dishes[0]
    assert dish.boxes == 4
    beans_needed = load.amounts[0].grams
    assert beans_needed == pytest.approx(whole_recipe_g), "one pot of a recipe that serves four"
    assert beans_needed == pytest.approx(dish.multiples * whole_recipe_g), (
        f"the cook is told {dish.multiples:g} times the recipe, which is "
        f"{dish.multiples * whole_recipe_g:.0f} g of beans, over a list that says "
        f"{beans_needed:.0f} g"
    )
    text = BatchFlow()._render(load, full=True)
    assert "4 times the recipe" not in text, text


# ---------------------------------------------------------------- 5. the method, on the day
def test_05_the_batch_plan_offers_a_way_to_read_what_it_asks_you_to_cook(world) -> None:  # noqa: F811
    """`/batch` is read standing in the kitchen at the start of the cook.

    It names the dishes, the totals, the boxes and the freezing - and carries no keyboard and
    no command that opens a method. This is the `/menu` defect again: the recipe exists, the
    buttons exist on another message, and there is no route from here to there.
    """
    pid, db, tg, _gateways, say, _tap, _run_job = world
    rice = _ingredient(db, "brown_rice", "Brown rice", aisle="grains")
    chilli = _recipe(db, "Chilli con carne", ["Brown the mince.", "Simmer."], keep_days=3)
    curry = _recipe(db, "Thai green curry", ["Fry the paste.", "Add the coconut milk."])
    db.insert("recipe_ingredients", recipe_id=chilli, ingredient_id=rice, grams=200.0)
    db.insert("recipe_ingredients", recipe_id=curry, ingredient_id=rice, grams=180.0)
    days = [MONDAY + dt.timedelta(days=i) for i in range(5)]
    _menu(db, pid, days, {"lunch": (chilli, 1.0), "dinner": (curry, 1.0)})

    text = say("/batch")
    assert "Chilli con carne" in text, "the dish it wants cooked"
    route = bool(_taps(tg.sent[-1])) or "/recipe" in text or "/menu" in text
    assert route, (
        "nothing on the cook-day message opens a method: no buttons, and the only commands "
        f"named are the ones that change the plan. Got: {text}"
    )


# ---------------------------------------------------------------- 6. that was horrible
def test_06_the_dish_that_was_horrible_can_be_rated_when_it_was_not_dinner(world) -> None:  # noqa: F811
    """Two meals a day is an answer the interview accepts (`_int_choice(2, 8)`), and then the
    menu has no dinner in it at all.

    A rating of 2 or under is the only thing that stops a dish coming back, and `/rate` asks one
    question: `AND ms.slot = 'dinner'`. With breakfast and lunch on today's menu it answers
    "Nothing to rate today", which is untrue, and the dish that was horrible has no way in from
    here - only /menu, the meal button, then the number.
    """
    pid, db, _tg, _gateways, say, _tap, _run_job = world
    beans = _ingredient(db, "beans", "Butter beans", aisle="tins")
    porridge = _recipe(db, "Porridge with berries", ["Cook the oats."])
    salad = _recipe(db, "Bean salad", ["Drain the beans.", "Dress it."])
    db.insert("recipe_ingredients", recipe_id=salad, ingredient_id=beans, grams=200.0)
    _menu(db, pid, [SATURDAY], {"breakfast": (porridge, 1.0), "lunch": (salad, 1.0)})

    out = say("/rate")
    assert "Nothing to rate" not in out, (
        "the menu has two dishes on it and the only route to a rating says there are none"
    )
    assert "Bean salad" in out or "Porridge" in out, out


# ---------------------------------------------------------------- 7. and it came back
def test_07_a_dish_rated_one_does_not_quietly_come_back_when_the_menu_repeats(world) -> None:  # noqa: F811
    pid, db, tg, gateways, _say, tap, run_job = world
    liver = _ingredient(db, "liver", "Lambs liver")
    horrible = _recipe(db, "Liver and onions", ["Fry the liver."], keep_days=2)
    db.insert("recipe_ingredients", recipe_id=horrible, ingredient_id=liver, grams=180.0)
    last_week = [dt.date(2026, 9, 7) + dt.timedelta(days=i) for i in range(5)]
    _menu(db, pid, last_week, {"dinner": (horrible, 1.0)})

    assert "1/5" in tap(f"k:rate:{horrible}:1")
    assert menu_mod.rated_names(db, pid)[1] == ["Liver and onions"], "Tiffin knows"

    gateways.append(FakeGateway([]))  # nothing usable for a fresh Mon-Fri menu
    before = len(tg.sent)
    run_job(menu_propose.JOB, SATURDAY)
    said = " ".join(m["text"] for m in tg.sent[before:])
    back = "Liver and onions" in menu_mod.meals_for_day(db, pid, MONDAY)
    assert not back or "Liver" in said, (
        "the menu it repeated is the one with the dish rated 1 in it, and the message that "
        f"carried it says nothing about that: {said}"
    )


# ---------------------------------------------------------------- 8. one lump, frozen
def test_08_the_card_says_what_pack_the_grams_have_to_come_out_of(fixture_db) -> None:
    """225 g of mince, and what is in the freezer is a 500 g brick.

    `recipes.card` puts `pack_label` and `pack_g` on every ingredient - its docstring says
    "ingredients with grams and pack sizes" - and `recipe_card_text` prints the grams alone.
    The pack is the only thing that turns 225 g into an amount you can cut off a lump.
    """
    db = fixture_db
    mince = _ingredient(db, "mince", "Beef mince, 5%", pack_g=500.0, pack_label="500 g pack")
    recipe_id = _recipe(db, "Chilli con carne", ["Brown the mince.", "Simmer."], keep_days=3)
    db.insert("recipe_ingredients", recipe_id=recipe_id, ingredient_id=mince, grams=225.0)

    card = recipes.card(db, recipe_id)
    assert card["ingredients"][0]["pack_label"] == "500 g pack", "the card knows the pack"
    text = menuflow.recipe_card_text(card)
    assert "225 g" in text
    assert "500 g" in text, "the pack the 225 g has to be cut from is dropped by the renderer"


# ---------------------------------------------------------------- 9. no air fryer today
def test_09_no_air_fryer_today_is_something_the_kitchen_list_can_be_told(world) -> None:  # noqa: F811
    """The air fryer is lent out, so tonight it is the oven and the hob.

    `/kit` is the whole route: it reads back what Tiffin thinks the kitchen holds and
    takes a new list, without redoing twenty-five interview questions.
    """
    pid, db, tg, _gateways, say, _tap, _run_job = world
    noted = say("/kit oven, hob")
    assert "oven" in noted and "hob" in noted
    assert menu_mod.equipment_of(targets_mod.profile_on(db, pid, SATURDAY)) == ["hob", "oven"]

    say("/kit")
    read_back = tg.sent[-2]["text"]
    assert "You can cook with" in read_back and "hob" in read_back
    assert "air fryer" not in read_back, "what is not in the kitchen is not offered to the menu"


# ---------------------------------------------------------------- 10. it is Thursday's dish
def test_10_thursdays_dinner_can_be_read_before_thursday(world) -> None:  # noqa: F811
    """Monday, and the plan has changed: tonight they want to cook Thursday's dinner.

    A rotating week is the default (`variety_of` falls back to "rotate"), so the five dinners
    are five different dishes. Every route to a method resolves to today: `/recipe` takes the
    day's main meal and the meal buttons carry `recipe_at(..., today)` - a function whose `day`
    argument no caller can point anywhere else. All five days are in the database and one of
    them is readable.
    """
    pid, db, tg, _gateways, say, tap, _run_job = world
    rice = _ingredient(db, "wild_rice", "Wild rice", aisle="grains")
    days = [MONDAY + dt.timedelta(days=i) for i in range(5)]
    menu_id = db.insert(
        "menus",
        person_id=pid,
        segment_start=days[0].isoformat(),
        segment_end=days[-1].isoformat(),
        status="accepted",
    )
    for day in days:
        name = f"{day:%A} dinner"
        recipe_id = _recipe(db, name, [f"Cook the {day:%A} dinner."])
        db.insert("recipe_ingredients", recipe_id=recipe_id, ingredient_id=rice, grams=150.0)
        menu_day = db.insert(
            "menu_days",
            menu_id=menu_id,
            day=day.isoformat(),
            kcal=2200,
            protein_g=180.0,
            fat_g=70.0,
            carb_g=200.0,
            fibre_g=30.0,
        )
        db.insert(
            "meal_slots",
            menu_day_id=menu_day,
            slot="dinner",
            recipe_id=recipe_id,
            portion_multiplier=1.0,
            portion_g=400.0,
            kcal=600,
            protein_g=45.0,
            fat_g=18.0,
            carb_g=55.0,
            fibre_g=7.0,
        )

    from tiffin.clock import TZ

    db.clock.set_local(dt.datetime(2026, 9, 14, 17, 30, tzinfo=TZ))
    listed = say("/menu")
    assert "Monday dinner" in listed, "tonight's is reachable"
    # a day button switches the card to that day, and its meal buttons open THAT day's recipes.
    # `recipe_at` has taken a day since it was written and no caller could point it anywhere, so
    # every Dinner button opened the first day of the segment whatever day you were looking at.
    thursday = next(d for d in _taps(tg.sent[-1]) if d.startswith("k:mday:") and d.endswith("17"))
    day_card = tap(thursday)
    assert "Thursday dinner" in day_card, f"the day opens: {day_card}"
    recipe = tap(next(d for d in _taps(tg.sent[-1]) if d.startswith("k:meal:") and ":dinner" in d))
    assert "Thursday dinner" in recipe, (
        "and the meal button on that day opens THAT day's recipe, not Monday's: " + recipe
    )
