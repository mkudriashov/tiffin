"""Tests for carbohydrate floors in individual menus."""

from __future__ import annotations

import datetime as dt
import json

import pytest

from tiffin.energy import Macros
from tiffin.errors import CarbsShort, Infeasible
from tiffin.food import catalogue, menu, solver
from tiffin.jobs import menu_propose, menu_shared
from tiffin.model import prompts

from .test_menu import MONDAY, FakeGateway, proposal_for, seed_catalogue
from .test_menu import world as world  # noqa: PLC0414 (the fixture, requested by name)

OATS = solver.SlotOption("breakfast", 300, 10, 6, 50, 6)
CHICKEN = solver.SlotOption("dinner", 700, 75, 25, 15, 5)
DAY = Macros(1000, 80, 20, 100, 10)


def test_the_solver_holds_a_carbohydrate_floor_when_asked() -> None:
    loose = solver.solve_day([OATS, CHICKEN], DAY, fat_floor_g=20, fibre_min=10)
    held = solver.solve_day([OATS, CHICKEN], DAY, fat_floor_g=20, fibre_min=10, carb_min=70)
    assert not isinstance(loose, Infeasible) and not isinstance(held, Infeasible)
    assert loose.macros.carb_g == 65, "one serving of each, as before: no floor asked for"
    assert held.macros.carb_g >= 70 and held.macros.protein_g >= 80
    assert held.multipliers["breakfast"] > 1.0 > held.multipliers["dinner"], "more of the oats"
    assert isinstance(
        solver.solve_day([OATS, CHICKEN], DAY, fat_floor_g=20, fibre_min=10, carb_min=150),
        Infeasible,
    )
    short = solver.solve_day(
        [OATS, CHICKEN], DAY, fat_floor_g=20, fibre_min=10, carb_min=150, best_effort=True
    )
    assert not isinstance(short, Infeasible)
    assert "carbs" in [name for name, _got, _need in short.shortfall], "and says what fell short"


def _plan_args(db, pid):
    seed_catalogue(db)
    db.insert(
        "profiles",
        person_id=pid,
        effective_from="2026-09-01",
        sex="m",
        birth_date="1990-03-15",
        height_cm=183.0,
        activity_factor=1.55,
        kcal_floor=1800,
        meals_per_day=4,
        segment_days=json.dumps([[0, 1, 2, 3, 4]]),
        variety_json=json.dumps({"all": "same"}),
    )
    return db.one("SELECT * FROM profiles WHERE person_id = ?", (pid,))


def test_a_menu_short_only_of_carbohydrate_says_so_and_the_day_it_is(
    fixture_db, two_persons
) -> None:
    pid = two_persons["owner"]
    profile = _plan_args(fixture_db, pid)
    one = menu.Segment(MONDAY, MONDAY)
    targets = Macros(2250, 200, 70, 200, 30)
    with pytest.raises(CarbsShort) as e:
        menu.build_plan(
            fixture_db, pid, one, proposal_for(one), targets=targets, profile=profile, carb_min=160
        )
    # said as the carbohydrate alone - the solver's own words named protein first, and the retry
    # then offered the leanest protein in the catalogue
    assert str(e.value).startswith("2026-09-14: carbs: ") and "< 160 g" in str(e.value)
    hint = menu_propose.leanest_for(str(e.value), catalogue.approved(fixture_db, pid))
    assert "carbohydrate per calorie" in hint
    # without the floor the same answer is a menu, and nothing was stored by the refusal
    assert (
        menu.build_plan(fixture_db, pid, one, proposal_for(one), targets=targets, profile=profile)
        .day_macros[MONDAY]
        .carb_g
        < 160
    )


def test_a_menu_that_fails_for_another_reason_says_that_reason(fixture_db, two_persons) -> None:
    pid = two_persons["owner"]
    profile = _plan_args(fixture_db, pid)
    one = menu.Segment(MONDAY, MONDAY)
    with pytest.raises(Infeasible) as e:
        menu.build_plan(
            fixture_db,
            pid,
            one,
            proposal_for(one),
            targets=Macros(2250, 200, 180, 200, 30),
            profile=profile,
            carb_min=160,
        )
    assert not isinstance(e.value, CarbsShort) and "fat" in str(e.value)


def _richer(db, pid, segment: menu.Segment) -> dict:
    """The fixture's week with a proper portion of rice at lunch and dinner."""
    rice = next(n for n, i in enumerate(catalogue.approved(db, pid), 1) if i.code == "rice")
    payload = proposal_for(segment)
    for day in payload["days"]:
        for slot in day["slots"]:
            if slot["slot"] in ("lunch", "dinner"):
                for ing in slot["recipe"]["ingredients"]:
                    if ing["i"] == rice:
                        ing["g"] = 40
    return payload


def test_one_more_ask_for_carbohydrate_and_the_better_answer_is_the_menu(world) -> None:
    pid, db, _tg, gateways, _say, _tap, run_job = world
    segment = menu.Segment(MONDAY, dt.date(2026, 9, 18))
    gw = FakeGateway([proposal_for(segment), _richer(db, pid, segment)])
    gateways.append(gw)
    assert run_job(menu_propose.JOB, dt.date(2026, 9, 12)) == "ok"
    assert len(gw.calls) == 2
    assert "200 g carbs" in gw.calls[0], "the model is told the carbohydrate"
    assert "carbs:" in gw.calls[1] and "carbohydrate per calorie" in gw.calls[1]
    mid = int(
        db.scalar("SELECT id FROM menus WHERE person_id = ? ORDER BY id DESC LIMIT 1", (pid,))
    )
    carbs = db.scalar("SELECT MIN(carb_g) FROM menu_days WHERE menu_id = ?", (mid,))
    assert carbs is not None and float(carbs) >= 160


def test_an_answer_short_of_carbohydrate_twice_is_kept_after_one_more_ask(world) -> None:
    pid, db, _tg, gateways, _say, _tap, run_job = world
    segment = menu.Segment(MONDAY, dt.date(2026, 9, 18))
    gw = FakeGateway([proposal_for(segment)] * 4)
    gateways.append(gw)
    assert run_job(menu_propose.JOB, dt.date(2026, 9, 12)) == "ok"
    assert len(gw.calls) == 2, "one more ask, and no third"
    assert db.scalar("SELECT COUNT(*) FROM menus WHERE person_id = ?", (pid,)) == 1


def test_a_second_try_that_goes_wrong_another_way_keeps_the_first(world) -> None:
    pid, db, _tg, gateways, _say, _tap, run_job = world
    segment = menu.Segment(MONDAY, dt.date(2026, 9, 18))
    gw = FakeGateway([proposal_for(segment), {"days": []}])
    gateways.append(gw)
    assert run_job(menu_propose.JOB, dt.date(2026, 9, 12)) == "ok"
    assert len(gw.calls) == 2
    names = {
        str(r["name"])
        for r in db.all(
            "SELECT rc.name FROM menus m JOIN menu_days md ON md.menu_id = m.id "
            "JOIN meal_slots ms ON ms.menu_day_id = md.id JOIN recipes rc ON rc.id = ms.recipe_id "
            "WHERE m.person_id = ?",
            (pid,),
        )
    }
    assert "Oats with whey" in names, "the first answer is the menu"


def test_the_day_line_names_the_carbohydrate() -> None:
    line = prompts._day_line(
        {
            "day": "2026-09-28",
            "slots": "lunch, dinner",
            "kcal": 1914,
            "protein_g": 200,
            "fat_g": 70,
            "carb_g": 121,
            "fibre_g": 30,
        }
    )
    assert "121 g carbs" in line


def test_a_shared_weekend_is_not_held_to_it() -> None:
    """His 200 g of protein in 1914 kcal and her 162 g of carbohydrate in 1600, as floors on the
    same dishes, come to more calories than there are: the shared figures leave carbohydrate out."""
    segment = menu.Segment(dt.date(2026, 10, 3), dt.date(2026, 10, 4))
    days = menu_shared._scale_days(
        segment,
        {1: None, 2: None},
        {1: Macros(1914, 200, 71, 118, 30), 2: Macros(1600, 133, 47, 162, 25)},
        {1: 30, 2: 25},
    )
    assert all("carb_g" not in d for d in days)
