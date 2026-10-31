"""Regression tests for menu, shopping and message edge cases."""

from __future__ import annotations

import datetime as dt
from typing import Any

from tiffin.clock import isoday
from tiffin.food import catalogue, newdish, prefs, swap
from tiffin.telegram import menuflow
from tiffin.telegram.dayflow import _days_named
from tiffin.telegram.foodflow import _dish_said

from .test_fixes_a1 import _uneven_weekend
from .test_model_builds import OneDishModel, _App, _consent
from .test_review_0926_weekend import SAT, SUN, pair  # noqa: F401
from .test_swap import bot, week  # noqa: F401

MON = dt.date(2026, 9, 7)


def test_42_the_swap_command_for_todays_meal_opens_today() -> None:
    said = newdish.swap_command(MON, "dinner", MON)
    assert said == "/swap today dinner"
    assert menuflow.day_and_meal(said.removeprefix("/swap "), MON) == (MON, "dinner")
    thu = MON + dt.timedelta(days=3)
    assert menuflow.day_and_meal(newdish.swap_command(thu, "lunch", MON)[6:], MON) == (thu, "lunch")
    far = MON + dt.timedelta(days=9)
    assert menuflow.day_and_meal(newdish.swap_command(far, "lunch", MON)[6:], MON) == (far, "lunch")


def test_12_a_day_inside_a_food_is_not_the_day_it_was_eaten() -> None:
    today = dt.date(2026, 9, 20)
    for food in (
        "Leftover Sunday roast instead of lunch",
        "Friday's leftover curry instead of lunch",
        "Sun-dried tomato pasta instead of dinner",
    ):
        assert _days_named(food, today) == set(), food
    assert _days_named("Wednesday: kebab instead of dinner", today) == {2}
    assert _days_named("Sat - pizza", today) == {5}
    assert _days_named("Tuesday was as planned", today) == {1}
    assert _days_named("had pizza on Tuesday instead of dinner", today) == {1}


def test_37_an_allergy_list_is_never_one_dish() -> None:
    said = "I'm allergic to prawns and sesame"
    assert _dish_said(None, 1, said, ["prawns", "sesame"]) == ("", [])  # type: ignore[arg-type]


def test_44_a_past_or_excepted_day_does_not_narrow_a_change_note() -> None:
    run = [dt.date(2026, 9, 14) + dt.timedelta(days=i) for i in range(5)]
    for note in (
        "a different lunch, the one yesterday was too salty",
        "a different lunch except monday",
        "different lunch but not on monday",
    ):
        assert menuflow.days_in_note(note, MON, run) == [], note
    assert menuflow.days_in_note("a different lunch on thursday", MON, run) == [run[3]]


def test_39_a_model_dish_under_a_name_already_offered_is_refused(week) -> None:  # noqa: F811
    db, pid, _base, _r, clock = week
    _consent(db, pid)
    MON14 = dt.date(2026, 9, 14)
    had = swap.candidates(db, pid, MON14, "lunch", clock.today_local(), limit=None)[0].name

    class SameName(OneDishModel):
        def json_task(self, *a: Any, **kw: Any) -> Any:
            got = super().json_task(*a, **kw)
            got.data["options"][2]["name"] = had  # the good one, under a favourite's name
            return got

    got = newdish.ask(_App(SameName()), db, pid, MON14, "lunch", clock=clock)
    assert had not in [d["name"] for d in got.dishes]
    assert any(had in r and "already on the menu" in r for r in got.refused), got.refused


def test_109_the_new_dishes_message_names_the_days_the_swap_changes() -> None:
    days = (dt.date(2026, 9, 14), dt.date(2026, 9, 16), dt.date(2026, 9, 18))
    built = newdish.Built(days[0], "lunch", days, dishes=[{"name": "Tuna rice bowl"}])
    assert "lunch on Mon, Wed and Fri 18 Sep" in built.said()


def test_35_x_and_y_comes_off_the_way_it_went_on(bot) -> None:  # noqa: F811
    send, _tap, db, pid, _r, _clock = bot
    send("/set avoid lentils and broccoli")
    assert catalogue.refused_terms(db, pid) == ["broccoli", "lentils"]
    text = send("/set avoid - lentils and broccoli")[0]["text"]
    assert catalogue.refused_terms(db, pid) == [], text
    assert "Not on the list" not in text


def _copy_of(db: Any, recipe_id: int, name: str, scope: str) -> int:
    rid = db.insert(
        "recipes",
        name=name,
        servings=1,
        prep_minutes=20,
        method_json="[]",
        source="model",
        person_scope=scope,
    )
    for r in db.all("SELECT * FROM recipe_ingredients WHERE recipe_id = ?", (recipe_id,)):
        db.insert(
            "recipe_ingredients", recipe_id=rid, ingredient_id=r["ingredient_id"], grams=r["grams"]
        )
    return rid


def test_75_a_dish_written_for_one_stays_off_a_plate_cooked_for_two(pair) -> None:  # noqa: F811
    db, _him, partner, clock = _uneven_weekend(pair)
    assert swap.cooked_with(db, partner.id, SUN, "lunch") is not None
    lunch = next(p for p in swap.plates_on(db, partner.id, SUN) if p.slot == "lunch")
    for name, scope in (("Her own lunch", "partner"), ("Lunch for two", "both")):
        rid = _copy_of(db, lunch.recipe_id, name, scope)
        db.insert(
            "built_dishes", person_id=partner.id, recipe_id=rid, day=isoday(SAT), slot="lunch"
        )
    names = {c.name for c in swap.candidates(db, partner.id, SUN, "lunch", clock.today_local())}
    assert "Lunch for two" in names
    assert "Her own lunch" not in names


def test_amend_still_says_a_phrase_that_names_nothing_listed(week) -> None:  # noqa: F811
    """The X-and-Y removal only takes parts that are all on the list."""
    db, pid, _base, _r, clock = week
    prefs.amend(db, pid, "avoid", add=["lentils"], clock=clock)
    got = prefs.amend(db, pid, "avoid", remove=["lentils and broccoli"], clock=clock)
    assert catalogue.refused_terms(db, pid) == ["lentils"]
    assert "Not on the list" in got.said()
