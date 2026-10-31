"""Thirty stories on the food surface: catalogue, drafts, solver, shopping, cook day, matcher.

Each one is something a person or the box does that is slightly off the happy path - a food
refused in the spelling that carries an accent, a model that says its dish serves two, a fridge
that already holds three packs of mince, a pudding named after a vegetable. Several run against
the REAL shipped catalogue (`config/ingredients.yaml` x the CoFID CSV), because both bugs found
on this surface before were found that way and neither showed up against a fixture of four foods.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from tiffin.db import Db
from tiffin.energy import Macros
from tiffin.errors import Infeasible, ModelError
from tiffin.food import batch, catalogue, menu, products, recipes, shopping, solver
from tiffin.food.solver import DayProblem, PackRule, SegmentSolution, SlotOption

from .test_food_catalogue import add_profile
from .test_food_shopping import AISLES, seed_ingredient, seed_menu, seed_recipe
from .test_menu import proposal_for, seed_catalogue

REPO = Path(__file__).resolve().parents[1]
YAML = REPO / "config" / "ingredients.yaml"
CSV = REPO / "data" / "cofid" / "cofid_2021_proximates.csv"

MONDAY = dt.date(2026, 9, 14)
FRIDAY = dt.date(2026, 9, 18)
TARGETS = Macros(2250, 200, 70, 200, 30)
FOUR = (
    SlotOption("breakfast", 520, 40, 14, 60, 8),
    SlotOption("lunch", 650, 55, 18, 70, 9),
    SlotOption("dinner", 700, 60, 22, 65, 10),
    SlotOption("snack", 300, 25, 8, 30, 4),
)
DAY_TARGET = Macros(2200, 180, 70, 220, 30)


def real_approved(db: Db, person_id: int) -> list[catalogue.Ingredient]:
    """The catalogue as shipped, built into the fixture DB, for this person."""
    catalogue.build(db, YAML, CSV)
    return catalogue.approved(db, person_id)


def codes(items: list[catalogue.Ingredient]) -> list[str]:
    return [i.code for i in items]


def profile_row(db: Db, person_id: int, **over: object):
    add_profile(db, person_id, **over)
    return db.one(
        "SELECT * FROM profiles WHERE person_id = ? ORDER BY id DESC LIMIT 1", (person_id,)
    )


def one_food_payload(day: dt.date) -> dict:
    """Four slots, one food, index 1 every time."""
    return {
        "days": [
            {
                "day": day.isoformat(),
                "slots": [
                    {
                        "slot": slot,
                        "recipe": {
                            "name": f"{slot} bowl",
                            "ingredients": [{"i": 1, "g": 200}],
                            "method": ["Cook it."],
                            "prep_minutes": 10,
                        },
                    }
                    for slot in ("breakfast", "lunch", "dinner", "snack")
                ],
            }
        ]
    }


def model_menu(db: Db, person_id: int, profile, segment: menu.Segment, *, servings: int):
    payload = proposal_for(segment)
    for day in payload["days"]:
        for slot in day["slots"]:
            slot["recipe"]["servings"] = servings
    plan = menu.build_plan(
        db,
        person_id,
        segment,
        payload,
        targets=TARGETS,
        profile=profile,
        items=catalogue.approved(db, person_id),
        model_used="claude",
        clock=db.clock,
    )
    return menu.store_plan(db, plan, status="accepted"), plan


# -- the avoid list, against the catalogue that actually ships ------------------------------


def test_01_a_food_refused_with_its_accent_on_stays_on_the_approved_list(
    fixture_db: Db, two_persons
) -> None:
    """`normalise` folds case and punctuation but not accents, and the catalogue spells its
    foods in ASCII. Someone typing the correct British spelling of a food they do not eat has
    said no, been told nothing, and will meet it again in a menu - which is the whole shape of
    the 'tomatoes' bug that left tomato ketchup approved.
    """
    pid = two_persons["owner"]
    assert "tomato_puree" in codes(real_approved(fixture_db, pid))
    add_profile(fixture_db, pid, refused_foods=json.dumps(["tomato purée"]))
    assert "tomato_puree" not in codes(catalogue.approved(fixture_db, pid)), (
        "a food refused with an accent on it is still refused"
    )


def test_03_refusing_coconut_milk_leaves_the_milk_in_partner_tea(
    fixture_db: Db, two_persons
) -> None:
    """The avoid list has to be narrow as well as wide: the de-pluralising, whole-word matcher
    that catches 'tomatoes' in 'Tomato puree' must not take out every food sharing a word."""
    pid = two_persons["owner"]
    assert {"coconut_milk", "milk_semi"} <= set(codes(real_approved(fixture_db, pid)))
    add_profile(fixture_db, pid, refused_foods=json.dumps(["coconut milk"]))
    left = codes(catalogue.approved(fixture_db, pid))
    assert "coconut_milk" not in left
    assert "milk_semi" in left


def test_04_refusing_one_food_renumbers_every_food_after_it(fixture_db: Db, two_persons) -> None:
    """Index-not-quote means the number is a position in THIS person's list at THIS moment.
    Refusing a food that sorts early shifts everything after it, so an index taken from an
    older prompt names a different dinner. Worth pinning: it is why the list and the answer
    have to come from one call.
    """
    pid = two_persons["owner"]
    before = codes(real_approved(fixture_db, pid))
    chicken = before.index("chicken_breast") + 1
    assert before.index("broccoli") < before.index("chicken_breast")
    add_profile(fixture_db, pid, refused_foods=json.dumps(["broccoli"]))
    after = codes(catalogue.approved(fixture_db, pid))
    assert "broccoli" not in after
    assert after[chicken - 1] != "chicken_breast", "the old number now names another food"
    assert "chicken_breast" in after


# -- the wall between the model and the plan -------------------------------------------------


def test_05_an_index_that_is_not_a_whole_number_is_not_quietly_rounded() -> None:
    """`int()` turns JSON true into 1 and 2.7 into 2, so a nonsense index silently becomes a
    real food. The recipe draft must refuse the value before it becomes a real food.
    """
    taken: list[str] = []
    for written, neighbour in ((True, 1), (2.7, 2)):
        try:
            draft = recipes.RecipeDraft.from_dict(
                {
                    "name": "Something",
                    "ingredients": [{"i": written, "g": 100}],
                    "method": ["Cook it."],
                    "prep_minutes": 10,
                }
            )
        except ValueError:
            continue  # refused outright is the other right answer
        if draft.ingredients[0].i == neighbour:
            taken.append(f"{written!r} became ingredient {neighbour}")
    assert not taken, f"an index that is not a whole number chose a real food: {taken}"


def test_06_a_person_with_nothing_approved_is_told_rather_than_given_an_empty_menu(
    fixture_db: Db, two_persons
) -> None:
    pid = two_persons["owner"]
    profile = profile_row(fixture_db, pid)
    with pytest.raises(ModelError, match="catalogue is empty"):
        menu.build_plan(
            fixture_db,
            pid,
            menu.Segment(MONDAY, MONDAY),
            one_food_payload(MONDAY),
            targets=TARGETS,
            profile=profile,
            items=[],
            clock=fixture_db.clock,
        )
    assert fixture_db.scalar("SELECT COUNT(*) FROM menus") == 0


def test_07_a_person_with_one_approved_food_gets_the_binding_constraint(
    fixture_db: Db, two_persons
) -> None:
    pid = two_persons["owner"]
    seed_ingredient(fixture_db, "chicken", "Chicken breast", "meat", 500, "500 g pack")
    profile = profile_row(fixture_db, pid)
    items = catalogue.approved(fixture_db, pid)
    assert len(items) == 1
    with pytest.raises(Infeasible) as caught:
        menu.build_plan(
            fixture_db,
            pid,
            menu.Segment(MONDAY, MONDAY),
            one_food_payload(MONDAY),
            targets=TARGETS,
            profile=profile,
            items=items,
            clock=fixture_db.clock,
        )
    reason = str(caught.value)
    assert reason.startswith(f"{MONDAY.isoformat()}: "), reason  # the segment names the day
    assert reason.split(": ")[1] in {"kcal", "protein", "fat", "fibre"}, reason
    assert fixture_db.scalar("SELECT COUNT(*) FROM recipes") == 0, "nothing half-stored"
    assert fixture_db.scalar("SELECT COUNT(*) FROM menus") == 0


def test_08_a_dish_owner_rated_two_cannot_come_back_in_next_weeks_menu(
    fixture_db: Db, two_persons
) -> None:
    pid = two_persons["owner"]
    seed_catalogue(fixture_db)
    profile = profile_row(fixture_db, pid)
    hated = fixture_db.insert(
        "recipes", name="Oats with whey", prep_minutes=10, method_json="[]", source="model"
    )
    fixture_db.insert("ratings", person_id=pid, recipe_id=hated, score=2)
    segment = menu.Segment(MONDAY, FRIDAY)
    with pytest.raises(ModelError, match="blocked"):
        menu.build_plan(
            fixture_db,
            pid,
            segment,
            proposal_for(segment),
            targets=TARGETS,
            profile=profile,
            items=catalogue.approved(fixture_db, pid),
            clock=fixture_db.clock,
        )
    assert fixture_db.scalar("SELECT COUNT(*) FROM menus") == 0


def test_09_the_model_says_the_dish_serves_two_and_the_shop_is_halved(
    fixture_db: Db, two_persons
) -> None:
    """`servings` is the model's own word, and two readers of it have to agree.

    A meal slot is one serving. The shopping list and the cook-day load have always divided
    recipe grams by `servings`; `build_plan` did not, so a dish the model said serves two was
    PLANNED against the whole recipe and SHOPPED as half of one. The day's kcal were twice the
    meal, and the fridge held half the food. Nothing anywhere said so, because every shipped
    fixture and the prompt example answer 1, where the two readings are identical.
    """
    pid = two_persons["owner"]
    seed_catalogue(fixture_db)
    profile = profile_row(fixture_db, pid)
    last_week = menu.Segment(dt.date(2026, 9, 7), dt.date(2026, 9, 11))
    one_id, one_plan = model_menu(fixture_db, pid, profile, last_week, servings=1)
    two_id, two_plan = model_menu(
        fixture_db, pid, profile, menu.Segment(MONDAY, FRIDAY), servings=2
    )
    for menu_id, servings in ((one_id, 1), (two_id, 2)):
        eaten = fixture_db.scalar(
            "SELECT COALESCE(SUM(ms.portion_g), 0) FROM menu_days md "
            "JOIN meal_slots ms ON ms.menu_day_id = md.id WHERE md.menu_id = ?",
            (menu_id,),
        )
        bought = sum(i.grams_total for i in shopping.build(fixture_db, menu_id, aisles=AISLES))
        assert abs(float(eaten) - bought) < 1.0, (
            f"serves {servings}: the plan says {eaten:.0f} g is eaten and the shop buys "
            f"{bought:.0f} g. One of the two readers of `servings` is wrong."
        )
    one_kcal = next(iter(one_plan.day_macros.values())).kcal
    two_kcal = next(iter(two_plan.day_macros.values())).kcal
    assert abs(one_kcal - two_kcal) <= max(one_kcal, two_kcal) * 0.02, (
        "both days are solved to the same target: what changes is the food, not the plan"
    )


# -- menus: boundaries, repeats and the ledger -----------------------------------------------


def test_10_a_cook_segment_that_runs_into_the_new_year_is_still_five_days(
    fixture_db: Db, two_persons
) -> None:
    profile = profile_row(
        fixture_db, two_persons["owner"], segment_days=json.dumps([[0, 1, 2, 3, 4]])
    )
    segments = menu.segments_for(profile, dt.date(2026, 12, 28))
    assert [(s.start, s.end) for s in segments] == [
        (dt.date(2026, 12, 28), dt.date(2027, 1, 1)),
        (dt.date(2027, 1, 2), dt.date(2027, 1, 3)),
    ]
    assert len(segments[0].days) == 5 and segments[0].days[-1].year == 2027


def test_11_a_repeat_with_nothing_to_repeat_writes_no_menu(fixture_db: Db, two_persons) -> None:
    pid = two_persons["owner"]
    assert menu.repeat_last(fixture_db, pid, menu.Segment(MONDAY, FRIDAY)) is None
    turned_down = fixture_db.insert(
        "menus",
        person_id=pid,
        segment_start="2026-09-07",
        segment_end="2026-09-11",
        status="proposed",
    )
    fixture_db.insert("menu_decisions", menu_id=turned_down, decision="rejected")
    assert menu.repeat_last(fixture_db, pid, menu.Segment(MONDAY, FRIDAY)) is None, (
        "a menu he said no to is not the menu to repeat"
    )
    assert fixture_db.scalar("SELECT COUNT(*) FROM menu_days") == 0


def test_12_repeating_a_two_day_weekend_onto_a_five_day_week_reuses_the_days_in_order(
    fixture_db: Db, two_persons
) -> None:
    pid = two_persons["owner"]
    dish = seed_recipe(fixture_db, "Salmon and potatoes", 1, {})
    weekend = fixture_db.insert(
        "menus",
        person_id=pid,
        segment_start="2026-09-12",
        segment_end="2026-09-13",
        status="accepted",
    )
    for day, kcal in (("2026-09-12", 2000), ("2026-09-13", 2100)):
        day_id = fixture_db.insert(
            "menu_days",
            menu_id=weekend,
            day=day,
            kcal=kcal,
            protein_g=180,
            fat_g=70,
            carb_g=200,
            fibre_g=30,
        )
        fixture_db.insert(
            "meal_slots",
            menu_day_id=day_id,
            slot="dinner",
            recipe_id=dish,
            portion_multiplier=1.0,
            portion_g=300,
            kcal=700,
            protein_g=50,
            fat_g=20,
            carb_g=60,
            fibre_g=8,
        )
    new_id = menu.repeat_last(fixture_db, pid, menu.Segment(MONDAY, FRIDAY))
    assert new_id is not None
    days = fixture_db.all("SELECT * FROM menu_days WHERE menu_id = ? ORDER BY day", (new_id,))
    assert [int(d["kcal"]) for d in days] == [2000, 2100, 2000, 2100, 2000]
    assert (
        fixture_db.scalar(
            "SELECT COUNT(*) FROM meal_slots ms JOIN menu_days md ON md.id = ms.menu_day_id "
            "WHERE md.menu_id = ?",
            (new_id,),
        )
        == 5
    )


def test_13_a_person_who_has_never_eaten_has_empty_lists_and_partners_are_not_their(
    fixture_db: Db, two_persons
) -> None:
    his, partners = two_persons["owner"], two_persons["partner"]
    assert menu.recent_names(fixture_db, his, MONDAY) == []
    assert menu.rated_names(fixture_db, his) == ([], [])
    dish = seed_recipe(fixture_db, "Her chilli", 1, {})
    seed_menu(fixture_db, partners, dt.date(2026, 9, 7), 2, {"dinner": (dish, 1.0)})
    fixture_db.insert("ratings", person_id=partners, recipe_id=dish, score=5)
    assert menu.recent_names(fixture_db, partners, MONDAY) == ["Her chilli"]
    assert menu.rated_names(fixture_db, partners) == (["Her chilli"], [])
    assert menu.recent_names(fixture_db, his, MONDAY) == [], "her week is not his history"
    assert menu.rated_names(fixture_db, his) == ([], [])


def test_14_a_second_menu_accepted_for_the_same_week_supersedes_the_first(
    fixture_db: Db, two_persons
) -> None:
    """The ledger rule from the outside: `menus` rows are never edited, so 'which menu is on'
    has to fall out of the view. If both stayed current every dish would be listed twice."""
    pid = two_persons["owner"]
    first_dish = seed_recipe(fixture_db, "Dish one", 1, {})
    second_dish = seed_recipe(fixture_db, "Dish two", 1, {})
    first = seed_menu(fixture_db, pid, MONDAY, 5, {"dinner": (first_dish, 1.0)})
    second = seed_menu(fixture_db, pid, MONDAY, 5, {"dinner": (second_dish, 1.0)})
    assert menu.accepted_menu_id(fixture_db, pid, MONDAY) == second
    assert menu.meals_for_day(fixture_db, pid, MONDAY) == ["Dish two"]
    assert fixture_db.scalar("SELECT status FROM menus WHERE id = ?", (first,)) == "accepted", (
        "the row it replaced is not rewritten"
    )


def test_15_a_cadence_somebody_hand_edited_into_nonsense_still_produces_a_week(
    fixture_db: Db, two_persons
) -> None:
    profile = profile_row(fixture_db, two_persons["owner"], segment_days="not json at all")
    segments = menu.segments_for(profile, MONDAY)
    assert [(s.start, s.end) for s in segments] == [
        (MONDAY, FRIDAY),
        (dt.date(2026, 9, 19), dt.date(2026, 9, 20)),
    ]


# -- the solver ------------------------------------------------------------------------------


def test_16_a_fridge_holding_more_mince_than_the_week_needs_still_gets_a_week() -> None:
    """A whole-pack ingredient is pinned to credit + whole packs by scaling the slots that use
    it, and the scale is (total - fixed) / free - what it takes to EAT the whole credit. Once
    the leftover covers the segment that scale is above 1, the multipliers leave the grid and
    the entire segment comes back infeasible. Buying nothing and carrying the rest forward is
    the answer; having food in already is not a reason to have no menu at all.
    """
    dinner = SlotOption(
        "dinner", 700, 60, 22, 65, 10, recipe_id=7, grams={"beef_mince_5": 100, "rice": 80}
    )
    slots = (FOUR[0], FOUR[1], dinner, FOUR[3])
    days = [
        DayProblem(dt.date(2026, 9, 7) + dt.timedelta(days=i), slots, DAY_TARGET) for i in range(5)
    ]
    free = solver.solve_segment(days, fat_floor_g=65)
    assert isinstance(free, SegmentSolution)
    week_needs = sum(100.0 * d.multipliers["dinner"] for d in free.days)
    solved = solver.solve_segment(
        days,
        whole_pack=[PackRule("beef_mince_5", 500)],
        leftovers={"beef_mince_5": week_needs + 100},
        fat_floor_g=65,
    )
    assert isinstance(solved, SegmentSolution), (
        f"{week_needs + 100:.0f} g in the fridge for a week needing {week_needs:.0f} g: {solved}"
    )


def test_17_portions_locked_at_exactly_one_are_refused_with_a_reason() -> None:
    """Lo == hi is a one-value grid, not a division by zero: someone who wants the portions
    as written gets either the day or the constraint that stops it."""
    refused = solver.solve_day(FOUR, DAY_TARGET, lo=1.0, hi=1.0, fat_floor_g=65)
    assert isinstance(refused, Infeasible)
    assert str(refused).startswith("fat:"), str(refused)
    solved = solver.solve_day(FOUR, DAY_TARGET, lo=1.0, hi=1.0, fat_floor_g=60)
    assert isinstance(solved, solver.DaySolution)
    assert set(solved.multipliers.values()) == {1.0}


def test_18_a_slot_with_nothing_in_it_stays_where_it_was() -> None:
    """A black coffee logged as the snack slot is a real slot with no macros. It cannot move
    the day, so it must not be moved either - a x1.6 portion of nothing is a silly thing to
    print on a menu."""
    water = SlotOption("snack", 0, 0, 0, 0, 0)
    slots = (FOUR[0], FOUR[1], FOUR[2], water)
    solved = solver.solve_day(slots, Macros(1900, 150, 50, 200, 25), fat_floor_g=50)
    assert isinstance(solved, solver.DaySolution)
    assert solved.multipliers["snack"] == 1.0


# -- the shopping list -----------------------------------------------------------------------


def test_19_an_aisle_the_list_has_never_heard_of_is_still_bought(
    fixture_db: Db, two_persons
) -> None:
    pid = two_persons["owner"]
    kimchi = seed_ingredient(fixture_db, "kimchi", "Kimchi", "world foods", 300, "300 g jar")
    chicken = seed_ingredient(fixture_db, "chk", "Chicken breast", "meat", 500, "500 g pack")
    dish = seed_recipe(fixture_db, "Chicken and kimchi", 1, {kimchi: 80, chicken: 200})
    menu_id = seed_menu(fixture_db, pid, MONDAY, 3, {"dinner": (dish, 1.0)})
    items = shopping.build(fixture_db, menu_id, aisles=AISLES)
    assert [i.aisle for i in items] == ["meat", "world foods"], "an unknown aisle sorts last"
    lines = shopping.render_lines(items, aisles=AISLES)
    assert any(line.startswith("World foods: Kimchi 1 ") for line in lines), lines


def test_20_a_weekday_menu_asked_for_its_weekend_shop_gives_an_empty_list(
    fixture_db: Db, two_persons
) -> None:
    pid = two_persons["owner"]
    chicken = seed_ingredient(fixture_db, "chk", "Chicken breast", "meat", 500, "500 g pack")
    dish = seed_recipe(fixture_db, "Chicken", 1, {chicken: 200})
    menu_id = seed_menu(fixture_db, pid, MONDAY, 5, {"dinner": (dish, 1.0)})  # Mon-Fri
    items = shopping.joint_weekend(fixture_db, [menu_id], aisles=AISLES)
    assert items == []
    assert (
        fixture_db.scalar("SELECT COUNT(*) FROM shopping_lists WHERE kind = 'joint_weekend'") == 1
    )
    assert fixture_db.scalar("SELECT COUNT(*) FROM shopping_items") == 0


def test_21_a_leftover_from_a_fortnight_ago_is_not_still_in_the_fridge(
    fixture_db: Db, two_persons
) -> None:
    """The rows are written when the list is COMPOSED, before anyone has been near a shop, so
    an unshopped week left Tiffin crediting food that was never bought - invisibly, since
    a leftover never appears on the list itself."""
    pid = two_persons["owner"]
    chicken = seed_ingredient(fixture_db, "chk", "Chicken breast", "meat", 500, "500 g pack")
    rice = seed_ingredient(fixture_db, "ric", "Rice", "grains", 1000, "1 kg bag")
    fixture_db.insert(
        "leftovers",
        person_id=pid,
        ingredient_id=chicken,
        grams=300.0,
        created_at="2026-08-20 18:00:00",
    )
    fixture_db.insert(
        "leftovers",
        person_id=pid,
        ingredient_id=rice,
        grams=400.0,
        created_at="2026-09-05 18:00:00",
    )
    assert shopping.current_leftovers(fixture_db, pid, dt.date(2026, 9, 7)) == {"ric": 400.0}


def test_22_the_weekend_leftovers_are_shared_by_the_people_who_share_the_food(
    fixture_db: Db, two_persons
) -> None:
    his, partners = two_persons["owner"], two_persons["partner"]
    salmon = seed_ingredient(fixture_db, "salmon", "Salmon fillets", "fish", 240, "2 fillets")
    dish = seed_recipe(fixture_db, "Salmon", 1, {salmon: 150})
    his_menu = seed_menu(fixture_db, his, dt.date(2026, 9, 12), 2, {"dinner": (dish, 1.0)})
    partner_menu = seed_menu(fixture_db, partners, dt.date(2026, 9, 12), 2, {"dinner": (dish, 1.0)})
    shopping.joint_weekend(fixture_db, [his_menu, partner_menu], aisles=AISLES)
    assert shopping.current_leftovers(fixture_db, his) == {"salmon": 60.0}
    assert shopping.current_leftovers(fixture_db, partners) == {"salmon": 60.0}


def test_23_a_product_whose_pack_size_the_page_never_showed_prices_nothing(
    fixture_db: Db, two_persons
) -> None:
    """ "4 Pints" and "12 Pack" are pack sizes no regex reads. Pricing the catalogue's guessed
    pack at that product's price gave a line naming one pack, a count for another, and a
    week's total out by the difference."""
    pid = two_persons["owner"]
    milk = seed_ingredient(fixture_db, "milk", "Milk, semi-skimmed", "dairy", 2000, "2 l")
    dish = seed_recipe(fixture_db, "Porridge", 1, {milk: 300})
    menu_id = seed_menu(fixture_db, pid, MONDAY, 5, {"breakfast": (dish, 1.0)})
    products.record(
        fixture_db,
        ingredient_code="milk",
        retailer="tesco",
        retailer_sku="1",
        name="Tesco British Semi Skimmed Milk 4 Pints",
        source="chrome",
        price_pence=145,
    )
    items = shopping.build(fixture_db, menu_id, aisles=AISLES)
    assert items[0].pack_g == 2000.0, "the catalogue's guess still stands"
    assert items[0].price_pence is None
    assert shopping.total_line(items) == ""


def test_24_a_week_shows_everything_it_eats_and_hides_only_what_you_asked_to_hide(
    fixture_db: Db, two_persons
) -> None:
    """The catalogue's `pantry` flag no longer hides anything from a shopping list.

    It was set on twenty-four foods - oats, both rices, pasta, quinoa, couscous, lentils, whey,
    almonds, peanut butter, alongside the salt and pepper - so a real week's shop came back with
    no carbohydrate on it and nothing said why. The flag was papering over a different bug: a
    leftover expired after ten days, so a kilo of rice used 64 g a week was re-bought every week.
    `KEEPS_DAYS` fixes that at the source, which leaves the flag hiding food people are buying.

    What hides a food now is the person saying so - `/set pantry salt` - or a pack opened earlier
    in the same shop window.
    """
    pid = two_persons["owner"]
    rice = seed_ingredient(fixture_db, "ric", "Rice", "grains", 1000, "1 kg bag", pantry=1)
    oil = seed_ingredient(fixture_db, "oil", "Olive oil", "condiments", 500, "500 ml", pantry=1)
    dish = seed_recipe(fixture_db, "Rice", 1, {rice: 100, oil: 10})
    menu_id = seed_menu(fixture_db, pid, MONDAY, 5, {"dinner": (dish, 1.0)})

    items = shopping.build(fixture_db, menu_id, aisles=AISLES)
    assert {i.code for i in items} == {"ric", "oil"}, "the catalogue flag hides nothing"

    # The planned surplus is visible to cooking, but is not assumed to be in the cupboard next week.
    credit = shopping.current_leftovers(fixture_db, pid, MONDAY + dt.timedelta(days=7))
    assert credit["ric"] == 500.0, "a grain keeps far longer than the ten days fresh food gets"
    next_menu = seed_menu(
        fixture_db, pid, MONDAY + dt.timedelta(days=7), 5, {"dinner": (dish, 1.0)}
    )
    again = shopping.build(fixture_db, next_menu, aisles=AISLES)
    assert "ric" in {i.code for i in again}, "the next shop starts without assumed stock"


# -- the cook day ----------------------------------------------------------------------------


def test_25_not_breakfast_must_not_come_back_as_breakfast_only() -> None:
    """'except', 'but' and 'not' are only read after 'all' or 'everything'. On their own the
    word is ignored and the meal named is the one KEPT, so a person who types 'not breakfast'
    is told they batch cook breakfast and nothing else - the exact opposite, silently."""
    try:
        got = batch.parse_slots("not breakfast")
    except batch.BadSlots:
        return  # being asked again is a fine answer
    assert "breakfast" not in got, f"'not breakfast' came back as {got}"


def test_26_a_dish_that_keeps_no_days_is_flagged_for_every_box_but_the_cook_days(
    fixture_db: Db, two_persons
) -> None:
    pid = two_persons["owner"]
    leaves = seed_ingredient(fixture_db, "lettuce", "Lettuce", "produce", 200, "200 g bag")
    salad = fixture_db.insert(
        "recipes",
        name="Chopped salad",
        servings=1,
        prep_minutes=10,
        method_json='["Chop it."]',
        source="manual",
        keep_days=0,
        freezable=0,
    )
    fixture_db.insert("recipe_ingredients", recipe_id=salad, ingredient_id=leaves, grams=120.0)
    menu_id = seed_menu(fixture_db, pid, MONDAY, 3, {"lunch": (salad, 1.0)})
    row = fixture_db.one("SELECT * FROM menus WHERE id = ?", (menu_id,))
    load = batch.plan(fixture_db, row, ("lunch",), MONDAY)
    assert load.cook_day == MONDAY
    late = {dish.name: days for dish, days in load.to_freeze}
    assert late["Chopped salad"] == (MONDAY + dt.timedelta(days=1), MONDAY + dt.timedelta(days=2))
    assert load.dishes[0].freezable is False, "and it does not freeze either"


def test_27_a_segment_that_starts_on_the_first_is_cooked_in_the_month_before(
    fixture_db: Db, two_persons
) -> None:
    pid = two_persons["owner"]
    rice = seed_ingredient(fixture_db, "ric", "Rice", "grains", 1000, "1 kg bag")
    dish = seed_recipe(fixture_db, "Chilli", 1, {rice: 200})
    menu_id = seed_menu(fixture_db, pid, dt.date(2026, 10, 1), 2, {"dinner": (dish, 1.0)})
    row = fixture_db.one("SELECT * FROM menus WHERE id = ?", (menu_id,))
    load = batch.plan(fixture_db, row, ("dinner",), dt.date(2026, 9, 25))
    assert load.cook_day == dt.date(2026, 9, 30)
    assert load.segment_start == dt.date(2026, 10, 1) and load.boxes == 2


def test_28_a_proposal_for_a_week_that_has_already_gone_is_not_what_the_cook_adds_up(
    fixture_db: Db, two_persons
) -> None:
    pid = two_persons["owner"]
    dish = seed_recipe(fixture_db, "Chilli", 1, {})
    old = fixture_db.insert(
        "menus",
        person_id=pid,
        segment_start="2026-08-31",
        segment_end="2026-09-04",
        status="proposed",
    )
    day_id = fixture_db.insert(
        "menu_days",
        menu_id=old,
        day="2026-08-31",
        kcal=2000,
        protein_g=180,
        fat_g=70,
        carb_g=200,
        fibre_g=30,
    )
    fixture_db.insert(
        "meal_slots",
        menu_day_id=day_id,
        slot="dinner",
        recipe_id=dish,
        portion_multiplier=1.0,
        portion_g=300,
        kcal=700,
        protein_g=50,
        fat_g=20,
        carb_g=60,
        fibre_g=8,
    )
    assert batch.menu_to_batch(fixture_db, pid, dt.date(2026, 9, 7)) is None
    ahead = seed_menu(fixture_db, pid, MONDAY, 5, {"dinner": (dish, 1.0)})
    picked = batch.menu_to_batch(fixture_db, pid, dt.date(2026, 9, 7))
    assert picked is not None and int(picked["id"]) == ahead
