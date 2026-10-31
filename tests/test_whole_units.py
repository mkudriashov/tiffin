"""Whole units on every plate, and one menu day in force per day.

food that goes on a plate whole - a slice, an egg, a bun - came out in fractions: the partner's
French
toast was 30 g of bread and the owner's smash burger 36 g of an 82 g bun. The solver now counts
those
foods as they are plated, and every reader of a plate rounds them the same way.
"""

from __future__ import annotations

import datetime as dt
import math
from pathlib import Path

import pytest

from tiffin.db import Db
from tiffin.energy import Macros
from tiffin.errors import Infeasible
from tiffin.food import catalogue, recipes, shopping, solver
from tiffin.web import queries

from .test_food_shopping import seed_ingredient, seed_menu, seed_recipe

MON = dt.date(2026, 9, 14)


@pytest.mark.parametrize(
    ("grams", "plated"),
    [(30, 40), (36, 40), (50, 40), (60, 80), (81.2, 80), (14, 40), (13, 13)],
)
def test_a_slice_is_a_slice(grams: float, plated: float) -> None:
    """Nearest whole one, halves up; under 0.35 of one is a part of one (egg wash, crumbs)."""
    assert solver.whole_units(grams, 40) == pytest.approx(plated)


def test_the_solver_counts_the_bread_as_it_is_plated() -> None:
    bread = solver.UnitPart(
        grams=30.0, unit_g=40.0, kcal=2.17, protein=0.094, fat=0.025, carb=0.42, fibre=0.07
    )
    toast = solver.SlotOption("breakfast", 300, 30, 10, 30, 5, units=(bread,))
    stew = solver.SlotOption("dinner", 600, 50, 20, 60, 8)
    sol = solver.solve_day([toast, stew], Macros(900, 70, 25, 90, 10), fat_floor_g=25, fibre_min=10)
    assert not isinstance(sol, Infeasible), sol
    m, n = sol.multipliers["breakfast"], sol.multipliers["dinner"]
    linear = 30.0 * m
    expected = 300 * m + 600 * n + (solver.whole_units(linear, 40.0) - linear) * 2.17
    assert sol.macros.kcal == math.floor(expected + 0.5), "the day holds the slice, not 30 g"


def _french_toast(db: Db) -> int:
    bread = seed_ingredient(
        db, "bread", "Wholemeal bread", "bakery", 800.0, "800 g loaf",
        kcal_100=217.0, protein_100=9.4, fat_100=2.5, carb_100=42.0, fibre_100=7.0,
    )  # fmt: skip
    egg = seed_ingredient(
        db, "eggs", "Eggs, whole", "dairy", 348.0, "6 eggs",
        kcal_100=131.0, protein_100=12.6, fat_100=9.0, carb_100=0.0, fibre_100=0.0,
    )  # fmt: skip
    db.insert("ingredient_units", code="bread", unit_name="slice", unit_g=40.0)
    db.insert("ingredient_units", code="eggs", unit_name="egg", unit_g=58.0)
    return seed_recipe(db, "Protein French toast", 1, {bread: 60.0, egg: 58.0})


def test_the_card_and_the_day_hold_whole_slices_and_eggs(fixture_db: Db) -> None:
    rid = _french_toast(fixture_db)
    card = recipes.card(fixture_db, rid, 0.5)
    by = {i["code"]: i for i in card["ingredients"]}
    assert by["bread"]["grams_per_serving"] == 40.0, "half of 60 g is 30 g: one slice"
    assert by["eggs"]["grams_per_serving"] == 58.0, "half an egg is one egg"
    assert (by["bread"]["unit_name"], by["eggs"]["unit_name"]) == ("slice", "egg")
    plate = recipes.stored_per_serving(fixture_db, rid, 0.5, catalogue.units(fixture_db))
    assert plate.kcal == round(40 * 2.17 + 58 * 1.31), "the macros are the plate's"
    assert card["per_serving"]["kcal"] == plate.kcal, "and the card says the same"


def test_the_bot_card_and_chat_count_the_plate_as_the_page_does(fixture_db: Db) -> None:
    """The page said "82 g, 1 bun" while the bot's card and chat said "Burger buns 36 g" - the
    straight-line figure, beside a kcal line that counted the whole bun."""
    from tiffin.telegram.menuflow import recipe_card_text

    rid = _french_toast(fixture_db)
    card = recipes.card(fixture_db, rid, 0.5)
    text = recipe_card_text(card)
    assert "Wholemeal bread 40 g, 1 slice" in text, text
    assert "Eggs, whole 58 g, 1 egg" in text, text
    assert "30 g" not in text and "29 g" not in text, "never the straight-line grams"
    assert recipes.unit_words("bun", 82.0, 164.0) == "2 buns"
    assert recipes.unit_words("bun", 82.0, 100.0) == "", "not a whole number of them"
    pot = {"grams": 600.0, "grams_per_serving": 150.0}
    assert recipes.card_grams(pot, 4) == 600.0, "a pot for four is cooked from the whole recipe"


def test_two_menus_over_the_same_days_draw_each_day_once(fixture_db: Db, two_persons) -> None:
    """A Wed-Fri replacement ahead of a Mon-Fri week, both still to come, drew Wednesday to
    Friday two and three times on the Menu tab, under both headings."""
    pid = two_persons["owner"]
    rid = _french_toast(fixture_db)
    seed_menu(fixture_db, pid, MON, 5, {"breakfast": (rid, 1.0)})
    seed_menu(fixture_db, pid, MON + dt.timedelta(days=2), 3, {"breakfast": (rid, 0.5)})
    tab = queries.menu(fixture_db, pid, None, MON - dt.timedelta(days=2))  # the Saturday before
    days = [d["day"] for d in tab["days"]]
    assert days == [(MON + dt.timedelta(days=i)).isoformat() for i in range(5)], days


def test_the_list_buys_the_slices_the_plates_hold(fixture_db: Db, two_persons) -> None:
    rid = _french_toast(fixture_db)
    menu_id = seed_menu(fixture_db, two_persons["owner"], MON, 2, {"breakfast": (rid, 0.5)})
    needs = shopping._needs(fixture_db, [menu_id], weekend_only=False)
    assert needs["bread"].grams == pytest.approx(80.0), "a slice a day, not 30 g a day"
    assert needs["eggs"].grams == pytest.approx(116.0)


def test_the_catalogue_names_a_unit_once(fixture_db: Db, tmp_path: Path) -> None:
    yaml_path = tmp_path / "ingredients.yaml"
    yaml_path.write_text(
        "aisles: [bakery]\n"
        "ingredients:\n"
        "  - code: bread_x\n"
        "    name: Bread\n"
        '    cofid_code: "11-1"\n'
        "    pack_g: 800\n"
        "    pack_label: 800 g loaf\n"
        "    aisle: bakery\n"
        "    unit: {name: slice, g: 40}\n",
        encoding="utf-8",
    )
    csv_path = tmp_path / "cofid.csv"
    csv_path.write_text(
        "food_code,food_name,kcal,protein_g,fat_g,carb_g,fibre_aoac_g\n"
        "11-1,Bread,217,9.4,2.5,42,7\n",
        encoding="utf-8",
    )
    first = catalogue.build(fixture_db, yaml_path, csv_path)
    again = catalogue.build(fixture_db, yaml_path, csv_path)
    assert first.units_set == ["bread_x"] and again.units_set == []
    assert catalogue.units(fixture_db) == {"bread_x": catalogue.Unit("slice", 40.0)}


def test_one_menu_day_is_in_force_for_each_day(fixture_db: Db, two_persons) -> None:
    """A later menu for part of an earlier one's days (a rebuilt Tuesday, a resized Sunday)
    left both current, and Today listed both menus' dinners for the day they shared."""
    db, pid = fixture_db, two_persons["owner"]
    rice = seed_ingredient(db, "rice", "Rice", "grains", 1000.0, "1 kg bag")
    week_dish = seed_recipe(db, "Week dish", 1, {rice: 100.0})
    tuesday_dish = seed_recipe(db, "Tuesday dish", 1, {rice: 150.0})
    week = seed_menu(db, pid, MON, 5, {"dinner": (week_dish, 1.0)})
    tuesday = seed_menu(db, pid, MON + dt.timedelta(days=1), 1, {"dinner": (tuesday_dish, 1.0)})
    rows = db.all(
        "SELECT day, menu_id FROM v_menu_days_current WHERE person_id = ? ORDER BY day", (pid,)
    )
    assert [int(r["menu_id"]) for r in rows] == [week, tuesday, week, week, week]
    names = [m["name"] for m in queries.meals(db, pid, MON + dt.timedelta(days=1))]
    assert names == ["Tuesday dish"], "one dinner on Tuesday, not both menus' dinners"
