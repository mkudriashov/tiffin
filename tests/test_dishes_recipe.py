"""The recipe book opens a dish to cook again")."""

from __future__ import annotations

import datetime as dt

from tiffin.db import Db
from tiffin.food import recipes
from tiffin.web import queries

from .test_food_shopping import seed_ingredient, seed_menu, seed_recipe
from .web_source import app_source


def test_a_dish_in_the_book_carries_one_portion(fixture_db: Db, two_persons) -> None:
    db = fixture_db
    pid = two_persons["owner"]
    beef = seed_ingredient(db, "beef", "Beef mince 5%", "meat", 500, "500 g pack")
    buns = seed_ingredient(db, "buns", "Burger buns", "bakery", 300, "4 buns")
    dish = seed_recipe(db, "Smash burgers", 2, {beef: 300, buns: 150})
    seed_menu(db, pid, dt.date(2026, 9, 26), 1, {"dinner": (dish, 1.15)})
    book = queries.dishes(db, pid, 30, dt.date(2026, 9, 28))
    burger = next(d for d in book if d["name"] == "Smash burgers")
    assert burger["servings"] == 2, "the page divides the pot's grams by it: one portion each"
    each = recipes.stored_per_serving(db, dish, 1.0)
    assert burger["per_portion"]["kcal"] == each.kcal
    assert {i["name"]: i["grams"] for i in burger["ingredients"]} == {
        "Beef mince 5%": 300.0,
        "Burger buns": 150.0,
    }
    assert burger["method"] == []


def test_the_book_opens_a_dish_by_the_portion_and_cooks_it_step_by_step() -> None:
    js = app_source()
    start = js.index("function dishRecipe(dish)")
    body = js[start : js.index("async function renderDishes()")]
    assert "plateGrams(ing, portions, dish.servings)" in body, "the recipe's own servings"
    assert "1 portion" in body and "openCook(" in body
    assert "dishRecipe(dish)" in js[js.index("async function renderDishes()") :]


def test_the_last_day_and_rating_belong_to_the_person(fixture_db: Db, two_persons) -> None:
    db = fixture_db
    owner_id, partner_id = two_persons["owner"], two_persons["partner"]
    oats = seed_ingredient(db, "oats", "Oats", "cereals", 1000, "1 kg bag")
    dish = seed_recipe(db, "Porridge", 1, {oats: 50})
    future = seed_recipe(db, "Overnight oats", 1, {oats: 50})
    seed_menu(db, owner_id, dt.date(2026, 9, 26), 1, {"breakfast": (dish, 1.0)})
    seed_menu(db, partner_id, dt.date(2026, 9, 27), 2, {"breakfast": (dish, 1.0)})
    seed_menu(db, owner_id, dt.date(2026, 10, 2), 1, {"breakfast": (future, 1.0)})
    db.insert("ratings", person_id=partner_id, recipe_id=dish, score=5)
    owner_book = queries.dishes(db, owner_id, 30, dt.date(2026, 9, 28))
    owner_dish = next(d for d in owner_book if d["name"] == "Porridge")
    partner_dish = queries.dishes(db, partner_id, 30, dt.date(2026, 9, 28))[0]
    assert owner_dish["last_day"] == "2026-09-26"
    assert owner_dish["score"] is None
    assert next(d for d in owner_book if d["name"] == "Overnight oats")["last_day"] is None
    assert partner_dish["last_day"] == "2026-09-28"
    assert partner_dish["score"] == 5


def test_the_three_plan_views_fold_details_and_keep_nutrition_one_tap_away() -> None:
    js = app_source()
    menu = js[js.index("async function renderMenu") : js.index("function per100Toggle")]
    batch = js[
        js.index("async function renderBatch") : js.index(
            "/* ---------------------------------------------------------------- dishes */"
        )
    ]
    recipe = js[js.index("function dishRecipe") : js.index("async function renderDishes")]
    assert 'per100Toggle(slot.ingredients, box.querySelectorAll(".per100-figure"))' in menu
    assert 'class: "muted per100-figure", hidden: true' in menu
    assert "per100Toggle(dish.pot)" in batch
    assert "per100Toggle(ingredients)" in recipe
    toggle = js[js.index("function per100Toggle") : js.index("// What a person avoids")]
    assert '"aria-expanded": "false"' in toggle and "item.hidden = !expanded" in toggle
    assert "box.open = day.day === state.today" not in menu
    assert "askedFor(day.day, slot.slot)" in menu and "toSay.anchor === box.id" in menu
    assert 'class: "muted day-total"' in menu and "num(day.protein_g)" in menu
    assert 'class: "muted slot-kind"' in menu
    assert 'slot.batch_cooked ? " · from the box"' in menu
    assert menu.index("const foodSettings") < menu.index("for (const [idx, m]")
    # the last time the settings are appended, after the days (the first is a build in progress)
    assert menu.index("for (const [idx, m]") < menu.rindex(
        "body.append(buildSettings);\n  if (foodSettings"
    )
    assert "Cooking for ${dayName(data.segment_start)}" in batch
    assert "Cooking for ${dayName(data.segment_start)} to ${dayName(data.segment_end)}" in batch


def test_the_recipe_book_searches_sorts_and_prompts_for_recent_unrated_dishes() -> None:
    js = app_source()
    dishes = js[
        js.index("async function renderDishes") : js.index(
            "/* ---------------------------------------------------------------- summary */"
        )
    ]
    assert 'text: "Find a dish"' in dishes
    assert "dish.ingredients || []" in dishes and 'search.addEventListener("input"' in dishes
    assert '"Recent", "Best rated", "A to Z"' in dishes
    assert '"aria-pressed": String(dishesSort === choice)' in dishes
    assert 'localStorage.getItem("tiffin.recipes-sort")' in js
    assert 'localStorage.setItem("tiffin.recipes-sort", choice)' in dishes
    assert "Rate what you had (${recent.length})" in dishes
    assert "dish.score === null && dish.last_day" in dishes
    assert "dish.last_day >= addDays(today, -6)" in dishes
    assert "body.append(foodFormEl)" in dishes
    rating = js[js.index("function dishRating(") : js.index("async function renderDishes")]
    assert "dishRatingBusy.has(dish.id)" in rating
    assert 'api("api/dishes/rate", { body })' in rating
