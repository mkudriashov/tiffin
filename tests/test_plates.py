"""A person's own plates for a day: resized, or a meal left out.

French toast, I can achieve that - which can mean I and the partner fully skip carbonara and
pre-bed."
The day is solved again from the same dishes and stays on target; when a floor cannot hold, the
day is still made and says what falls short.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from tiffin.db import Db
from tiffin.errors import Infeasible, NothingToBuild
from tiffin.food import menu, plates, shared, solver
from tiffin.web import queries

from .test_dash_shopping_tab import HEADER, MONDAY, OWNER_LOGIN, SUNDAY, at, open_box  # noqa: F401
from .test_food_shopping import seed_ingredient, seed_menu, seed_recipe
from .test_review_0926_weekend import HIS, PARTNERS, SUN, _agree, _build, pair  # noqa: F401

MON = dt.date(2026, 9, 14)  # the frozen clock is Monday 7 Sep, so this day is still to come

FOODS = {
    #          name, aisle, kcal, protein, fat, carb, fibre per 100 g
    "bread": ("Wholemeal bread", "bakery", 217, 9.4, 2.5, 42, 7),
    "eggs": ("Eggs", "dairy", 131, 12.6, 9, 0, 0),
    "yoghurt": ("Greek yoghurt 0%", "dairy", 57, 10, 0.4, 4, 0),
    "chicken": ("Chicken breast", "meat", 165, 31, 3.6, 0, 0),
    "oil": ("Olive oil", "condiments", 899, 0, 99.9, 0, 0),
    "oats": ("Porridge oats", "grains", 379, 13, 8, 60, 10),
    "broccoli": ("Broccoli", "produce", 35, 3.8, 0.4, 2.2, 3),
}


@pytest.fixture
def day_world(fixture_db: Db, two_persons, frozen_clock):
    db, pid = fixture_db, two_persons["owner"]
    db.insert(
        "profiles",
        person_id=pid,
        effective_from="2026-09-01",
        sex="m",
        birth_date="1990-03-15",
        height_cm=183.0,
        activity_factor=1.55,
        kcal_floor=1200,
        meals_per_day=4,
        segment_days=json.dumps([[0, 1, 2, 3, 4]]),
        fibre_g_min=10,
        # nothing batch-cooked: every meal of this weekday is the day's own to resize
        batch_slots=json.dumps([]),
    )
    db.insert(
        "targets",
        person_id=pid,
        week_start="2026-09-14",
        kcal=1500,
        protein_g=150,
        fat_g=35,
        carb_g=150,
        fibre_g=20,
        source="formula",
    )
    ids = {
        code: seed_ingredient(
            db,
            code,
            name,
            aisle,
            500.0,
            "500 g pack",
            kcal_100=k,
            protein_100=p,
            fat_100=f,
            carb_100=c,
            fibre_100=fb,
        )
        for code, (name, aisle, k, p, f, c, fb) in FOODS.items()
    }
    db.insert("ingredient_units", code="bread", unit_name="slice", unit_g=40.0)
    db.insert("ingredient_units", code="eggs", unit_name="egg", unit_g=58.0)
    g = ids.__getitem__
    toast = seed_recipe(
        db, "French toast", 1, {g("bread"): 80.0, g("eggs"): 116.0, g("yoghurt"): 150.0}
    )
    burger = seed_recipe(
        db, "Smash burger", 1, {g("chicken"): 200.0, g("oil"): 5.0, g("bread"): 40.0}
    )
    carbonara = seed_recipe(
        db,
        "Carbonara",
        1,
        {g("oats"): 80.0, g("chicken"): 150.0, g("oil"): 10.0, g("broccoli"): 100.0},
    )
    pot = seed_recipe(db, "Pre-bed pot", 1, {g("yoghurt"): 300.0})
    base = seed_menu(
        db,
        pid,
        MON,
        1,
        {
            "breakfast": (toast, 1.0),
            "lunch": (burger, 1.0),
            "dinner": (carbonara, 1.0),
            "pre_bed": (pot, 1.0),
        },
    )
    return db, pid, base, frozen_clock


def _plates_of(db: Db, menu_id: int) -> dict[str, float]:
    return {
        str(r["slot"]): float(r["portion_multiplier"])
        for r in db.all(
            "SELECT ms.slot, ms.portion_multiplier FROM menu_days md "
            "JOIN meal_slots ms ON ms.menu_day_id = md.id WHERE md.menu_id = ?",
            (menu_id,),
        )
    }


def test_no_carbonara_and_more_french_toast(day_world, config) -> None:
    db, pid, base, clock = day_world
    got = plates.resize(
        db, pid, MON, {"dinner": "skip", "breakfast": "more"}, config=config, clock=clock
    )
    assert [m["name"] for m in queries.meals(db, pid, MON)] == [
        "French toast",
        "Smash burger",
        "Pre-bed pot",
    ], "Today holds the day as resized: no carbonara"
    assert abs(got.macros.kcal - 1500) <= 1500 * 0.05, "and the day is still on its calories"
    assert got.macros.protein_g >= 150 and got.macros.fat_g >= 35
    assert not got.shortfall and "Still on target" in got.said()
    sizes = _plates_of(db, got.menu_id)
    assert set(sizes) == {"breakfast", "lunch", "pre_bed"}
    assert sizes["breakfast"] >= sizes["lunch"], "the French toast was asked for more of"
    assert menu.accepted_menu_id(db, pid, MON) == got.menu_id, "the day's plates are these"
    assert menu.segment_menu_id(db, pid, MON) == base, "the week a shopping list reads is not"
    st = plates.state(db, pid, MON)
    assert st is not None and st["base_menu_id"] == base
    assert st["levels"] == {
        "breakfast": "more",
        "lunch": "planned",
        "dinner": "skip",
        "pre_bed": "planned",
    }


def test_back_to_the_plan_is_the_plan_itself(day_world, config) -> None:
    """On 27 Sep "Back to the plan" solved the day afresh at 2% and refused both of them: the
    plan was built at 5% and before whole slices were counted, so its own plates failed the
    tighter test. This fixture's plan is 2,000 kcal against a 1,500 target, far outside any
    window - and back is still exactly the plan, plate for plate."""
    db, pid, base, clock = day_world
    plates.resize(db, pid, MON, {"dinner": "skip", "breakfast": "lots"}, config=config, clock=clock)
    back = plates.resize(db, pid, MON, {}, config=config, clock=clock)
    names = [m["name"] for m in queries.meals(db, pid, MON)]
    assert names == ["French toast", "Smash burger", "Carbonara", "Pre-bed pot"]
    assert _plates_of(db, back.menu_id) == _plates_of(db, base), "every plate as it was planned"
    planned = db.one("SELECT kcal FROM menu_days WHERE menu_id = ? AND day = ?", (base, str(MON)))
    assert back.macros.kcal == int(planned["kcal"]), "the plan's own numbers, not a new solve"
    st = plates.state(db, pid, MON)
    assert st is not None and st["base_menu_id"] == base, "measured against the plan"
    assert set(st["levels"].values()) == {"planned"}
    info = plates.day_info(db, pid, MON, MON)
    assert info is not None and set(info["levels"].values()) == {"planned"}


def test_a_day_short_of_protein_is_still_made_and_says_so(day_world, config) -> None:
    """Without the burger and the pre-bed pot, the protein is not there to be had: the person
    asked for it, so the day is made - and says how short, and what covers it."""
    db, pid, _base, clock = day_world
    got = plates.resize(
        db, pid, MON, {"lunch": "skip", "pre_bed": "skip"}, config=config, clock=clock
    )
    short = {name: (have, need) for name, have, need in got.shortfall}
    assert "protein" in short and short["protein"][0] < 150
    assert "Short: protein" in got.said() and "shake" in got.said()
    assert [m["name"] for m in queries.meals(db, pid, MON)] == ["French toast", "Carbonara"]
    st = plates.state(db, pid, MON)
    assert st is not None and str(st["shortfall"]).startswith("protein")


def test_plates_that_cannot_carry_the_day_are_refused_in_words(day_world, config) -> None:
    db, pid, _base, clock = day_world
    with pytest.raises(Infeasible, match="cannot carry the whole day") as refused:
        plates.resize(
            db,
            pid,
            MON,
            {"breakfast": "skip", "lunch": "skip", "dinner": "skip"},
            config=config,
            clock=clock,
        )
    # the solver's own diagnosis ("protein: max reachable 11 g < 200 g") is not the person's
    # to read, and named a floor when the sentence was about calories
    assert "reachable" not in str(refused.value) and "keep another meal" in str(refused.value)
    with pytest.raises(NothingToBuild, match="at least one meal"):
        plates.resize(
            db,
            pid,
            MON,
            dict.fromkeys(("breakfast", "lunch", "dinner", "pre_bed"), "skip"),
            config=config,
            clock=clock,
        )
    with pytest.raises(ValueError, match="a level is one of"):
        plates.resize(db, pid, MON, {"dinner": "none"}, config=config, clock=clock)
    with pytest.raises(ValueError, match="not a meal"):
        plates.resize(db, pid, MON, {"snack": "more"}, config=config, clock=clock)
    assert menu.accepted_menu_id(db, pid, MON) == _base, "nothing was stored"


def test_a_past_or_recorded_day_stays_as_it_was(day_world, config) -> None:
    db, pid, _base, clock = day_world
    with pytest.raises(NothingToBuild, match="gone"):
        plates.resize(db, pid, dt.date(2026, 9, 1), {}, config=config, clock=clock)
    db.insert(
        "day_confirmations", person_id=pid, day=MON.isoformat(), status="confirmed", basis="menu"
    )
    with pytest.raises(NothingToBuild, match="recorded"):
        plates.resize(db, pid, MON, {"dinner": "skip"}, config=config, clock=clock)


def test_the_menu_tab_shows_the_resized_day_and_its_levels(day_world, config) -> None:
    db, pid, _base, clock = day_world
    plates.resize(db, pid, MON, {"dinner": "skip", "lunch": "lots"}, config=config, clock=clock)
    tab = queries.menu(db, pid, MON, MON)
    day = next(d for d in tab["days"] if d["day"] == MON.isoformat())
    assert [s["name"] for s in day["slots"]] == ["French toast", "Smash burger", "Pre-bed pot"]
    assert (
        day["plates"]["levels"]["dinner"] == "skip" and day["plates"]["levels"]["lunch"] == "lots"
    )
    assert [m["name"] for m in day["plates"]["meals"]][2] == "Carbonara", "so it can come back"
    assert day["plates"]["adjustable"] is True
    assert day["plates"]["together"] is None, "a day of his own: only his plates change"


def _shared_weekend(pair):  # noqa: F811
    """Both agreed, both with targets, the weekend built for the two of them."""
    db, (him, partner), clock = pair
    for p, m in ((him, HIS), (partner, PARTNERS)):
        db.insert(
            "targets",
            person_id=p.id,
            week_start="2026-09-14",
            kcal=m.kcal,
            protein_g=m.protein_g,
            fat_g=m.fat_g,
            carb_g=m.carb_g,
            fibre_g=m.fibre_g,
            source="formula",
        )
    _agree(db, him.id)
    _agree(db, partner.id)
    db.insert("person_names", person_id=partner.id, name="Sam", set_by="operator")
    built = _build(db, [him, partner], clock)
    return db, him, partner, clock, built


def _day_plates(db: Db, pid: int, day: dt.date) -> dict[str, float]:
    """The plates in force for one person's day, by meal."""
    return {
        str(r["slot"]): float(r["portion_multiplier"])
        for r in db.all(
            "SELECT ms.slot, ms.portion_multiplier FROM v_menu_days_current v "
            "JOIN meal_slots ms ON ms.menu_day_id = v.id WHERE v.person_id = ? AND v.day = ?",
            (pid, day.isoformat()),
        )
    }


def _day_kcal(db: Db, pid: int, day: dt.date) -> int:
    return int(
        db.scalar(
            "SELECT SUM(ms.kcal) FROM v_menu_days_current v "
            "JOIN meal_slots ms ON ms.menu_day_id = v.id WHERE v.person_id = ? AND v.day = ?",
            (pid, day.isoformat()),
        )
    )


def test_a_shared_weekend_plate_shows_the_other_persons_plate_in_force(
    pair,  # noqa: F811
    config,
) -> None:
    """The owner resizes his Sunday; the partner's card of the same dish shows his plate as it now
    is, and a
    dish he skipped shows no plate of his at all."""
    db, him, partner, clock, built = _shared_weekend(pair)
    got = plates.resize(db, him.id, SUN, {"dinner": "skip"}, config=config, clock=clock)
    partners = built.menu_ids[partner.id]
    seen = shared.partner_plate(db, partner.id, partners, SUN, "lunch")
    assert seen is not None
    assert seen["multiplier"] == pytest.approx(_plates_of(db, got.menu_id)["lunch"]), (
        "her card shows his plate as he has made it"
    )
    assert shared.partner_plate(db, partner.id, partners, SUN, "dinner") is None, (
        "he is not having it"
    )
    assert shared.partner_plate(db, him.id, got.menu_id, SUN, "lunch") is not None, (
        "and his resized day is still cooked together with hers"
    )


def test_the_page_resizes_through_its_own_endpoint(open_box) -> None:  # noqa: F811
    box = open_box(at(SUNDAY, 18, 20))
    box.db.insert(
        "targets",
        person_id=box.owner_id,
        week_start=MONDAY.isoformat(),
        kcal=450,
        protein_g=30,
        fat_g=10,
        carb_g=40,
        fibre_g=5,
        source="formula",
    )
    csrf = box.get("api/me").json()["csrf"]
    headers = {HEADER: OWNER_LOGIN, "X-CSRF-Token": csrf}

    def post(body: dict):
        return box.client.post("api/menu/plates", json=body, headers=headers)

    monday = MONDAY.isoformat()
    # Monday's only meal is the lunch box batch-cooked on Sunday: eaten as it was cooked
    boxed = post({"day": monday, "levels": {"lunch": "more"}})
    assert boxed.status_code == 400 and "box" in boxed.json()["detail"], boxed.text
    ok = post({"day": monday, "levels": {}})
    assert ok.status_code == 200, ok.text
    assert ok.json()["said"].startswith("Mon 14 Sep")
    assert post({"day": monday, "levels": {"lunch": "enormous"}}).status_code == 400
    for not_levels in ([], False, 0, ""):
        assert post({"day": monday, "levels": not_levels}).status_code == 400, not_levels
    assert post({"day": monday, "levels": {}, "eaten": "lunch"}).status_code == 400
    early = post({"day": monday, "levels": {}, "eaten": ["lunch"]})
    assert early.status_code == 400 and "today" in early.json()["detail"], "Monday is ahead"
    who = post({"day": monday, "levels": {}, "person_id": 2})
    assert who.status_code in (400, 403), "a person is never read off the body"


def test_a_boxed_meal_is_eaten_as_it_was_cooked(day_world, config) -> None:
    """A lunch set to "less" listed 0.9 of the dish while the box in the fridge held a whole
    one: a meal batch-cooked for the week is not the day's to resize, and the panel says so."""
    db, pid, base, clock = day_world
    db.insert(
        "profiles",
        person_id=pid,
        effective_from="2026-09-02",
        sex="m",
        birth_date="1990-03-15",
        height_cm=183.0,
        activity_factor=1.55,
        kcal_floor=1200,
        meals_per_day=4,
        segment_days=json.dumps([[0, 1, 2, 3, 4]]),
        fibre_g_min=10,
        batch_slots=json.dumps(["lunch"]),
    )
    with pytest.raises(ValueError, match="box"):
        plates.resize(db, pid, MON, {"lunch": "less"}, config=config, clock=clock)
    got = plates.resize(
        db, pid, MON, {"pre_bed": "skip", "breakfast": "more"}, config=config, clock=clock
    )
    assert _plates_of(db, got.menu_id)["lunch"] == _plates_of(db, base)["lunch"], "as cooked"
    info = plates.day_info(db, pid, MON, MON)
    assert info is not None
    assert {m["slot"]: m["boxed"] for m in info["meals"]} == {
        "breakfast": False,
        "lunch": True,
        "dinner": False,
        "pre_bed": False,
    }
    weekend = plates.boxed_slots(db, pid, dt.date(2026, 9, 19))
    assert weekend == frozenset(), "the weekend is cooked on the day, never boxed"


def test_a_meal_already_eaten_is_held_as_it_was_eaten(day_world, config) -> None:
    """The owner changed his Sunday at 11:42 and it re-sized a breakfast already eaten - hers too. A
    meal ticked as eaten keeps its size and its level, and the rest of the day makes room."""
    db, pid, _base, clock = day_world
    clock.set_local(dt.datetime(2026, 9, 14, 12, 0))  # the day itself, after breakfast
    first = plates.resize(db, pid, MON, {"breakfast": "more"}, config=config, clock=clock)
    eaten_at = _plates_of(db, first.menu_id)["breakfast"]
    clock.advance(minutes=5)
    got = plates.resize(
        db, pid, MON, {"dinner": "skip"}, eaten=["breakfast"], config=config, clock=clock
    )
    assert _plates_of(db, got.menu_id)["breakfast"] == eaten_at
    assert got.levels["breakfast"] == "more", "recorded at the level it was eaten at"
    info = plates.day_info(db, pid, MON, MON)
    assert info is not None and info["today"] is True
    with pytest.raises(ValueError, match="not on today's plates"):
        plates.resize(db, pid, MON, {}, eaten=["dinner"], config=config, clock=clock)


def test_a_plate_asked_for_more_than_it_holds_passes_the_rest_on_and_says_so(
    day_world, config
) -> None:
    """Lots of French toast and less burger on a day neither carbonara nor pot is eaten: the
    toast stops at its largest, the burger has to carry the rest - and the result says both,
    rather than a "less" plate quietly coming out bigger."""
    db, pid, _base, clock = day_world
    db.insert(
        "targets",
        person_id=pid,
        week_start="2026-09-14",
        kcal=1300,
        protein_g=100,
        fat_g=30,
        carb_g=120,
        fibre_g=15,
        source="formula",
    )
    got = plates.resize(
        db,
        pid,
        MON,
        {"breakfast": "lots", "lunch": "less", "dinner": "skip", "pre_bed": "skip"},
        config=config,
        clock=clock,
    )
    assert abs(got.macros.kcal - 1300) <= 1300 * 0.05, "the calories still hold"
    moved = dict(got.moved)
    assert moved == {"breakfast": "largest", "lunch": "bigger"}, got.moved
    said = got.said()
    assert "Breakfast is as big as it goes" in said
    assert "Lunch is bigger than asked, to carry the day's calories" in said


def test_the_shortfall_is_the_one_the_result_states(day_world, config) -> None:
    """ "2,253 kcal, protein 193 g. Short: protein 194 g of 200 g" gave two answers to one
    question: the shortfall is read off the same stored plates as the total."""
    db, pid, _base, clock = day_world
    got = plates.resize(
        db, pid, MON, {"lunch": "skip", "pre_bed": "skip"}, config=config, clock=clock
    )
    short = {name: have for name, have, _need in got.shortfall}
    assert short["protein"] == got.macros.protein_g
    assert f"protein {got.macros.protein_g} g." in got.said()
    assert f"protein {got.macros.protein_g} g of" in got.said()


def test_partner_plates_are_not_promised_once_partner_day_is_recorded(pair, config) -> None:  # noqa: F811
    db, him, partner, clock, _built = _shared_weekend(pair)
    assert (plates.day_info(db, him.id, SUN, clock.today_local()) or {})["together"] == "Sam"
    db.insert(
        "day_confirmations",
        person_id=partner.id,
        day=SUN.isoformat(),
        status="confirmed",
        basis="menu",
    )
    assert (plates.day_info(db, him.id, SUN, clock.today_local()) or {})["together"] is None


def test_on_a_day_cooked_together_partner_plates_follow_their(pair, config) -> None:  # noqa: F811
    """
    calories properly as well? It recalced for me only?" On a day the two cook together a change
    moves both plates: the same dishes, each sized to its own person's day."""
    db, him, partner, clock, _built = _shared_weekend(pair)
    before = _day_plates(db, partner.id, SUN)
    got = plates.resize(
        db, him.id, SUN, {"dinner": "skip", "lunch": "more"}, config=config, clock=clock
    )
    after = _day_plates(db, partner.id, SUN)
    assert "dinner" in before and "dinner" not in after, "she is not having the dinner either"
    assert after["lunch"] > before["lunch"], "her share of it goes to the dish asked for more of"
    assert abs(_day_kcal(db, partner.id, SUN) - PARTNERS.kcal) <= PARTNERS.kcal * 0.05, (
        "on her own calories, not his"
    )
    assert abs(_day_kcal(db, him.id, SUN) - HIS.kcal) <= HIS.kcal * 0.05
    assert "Sam's plates follow" in got.said()
    assert got.partner_menu_id == menu.accepted_menu_id(db, partner.id, SUN)
    st = plates.state(db, partner.id, SUN)
    assert st is not None and st["levels"]["dinner"] == "skip" and st["levels"]["lunch"] == "more"
    seen = shared.partner_plate(db, him.id, got.menu_id, SUN, "lunch")
    assert seen is not None and seen["multiplier"] == pytest.approx(after["lunch"]), (
        "his card shows her plate as it now is"
    )
    assert shared.partner_plate(db, him.id, got.menu_id, SUN, "dinner") is None


def test_a_change_either_of_them_makes_moves_both(pair, config) -> None:  # noqa: F811
    db, him, partner, clock, _built = _shared_weekend(pair)
    planned = set(_day_plates(db, him.id, SUN))
    plates.resize(db, him.id, SUN, {"dinner": "skip"}, config=config, clock=clock)
    back = plates.resize(db, partner.id, SUN, {}, config=config, clock=clock)
    assert set(_day_plates(db, partner.id, SUN)) == planned
    assert set(_day_plates(db, him.id, SUN)) == planned, "her Back to the plan brings his back"
    assert "Alex's plates follow" in back.said()


def test_partner_recorded_day_stays_as_it_was(pair, config) -> None:  # noqa: F811
    db, him, partner, clock, built = _shared_weekend(pair)
    db.insert(
        "day_confirmations",
        person_id=partner.id,
        day=SUN.isoformat(),
        status="confirmed",
        basis="menu",
    )
    got = plates.resize(db, him.id, SUN, {"dinner": "skip"}, config=config, clock=clock)
    assert "dinner" not in _day_plates(db, him.id, SUN), "his own plates still change"
    assert menu.accepted_menu_id(db, partner.id, SUN) == built.menu_ids[partner.id], (
        "hers were eaten"
    )
    assert got.partner_menu_id is None
    assert "Sam's plates stay as they were: that day is already recorded" in got.said()


def test_a_change_partner_plates_cannot_carry_changes_neither(
    pair,  # noqa: F811
    config,
    monkeypatch,
) -> None:
    """Stored whole or not at all: the two are never left cooking different days."""
    db, him, partner, clock, built = _shared_weekend(pair)
    real = solver.solve_day

    def not_for_partner(options, target, **kw):
        if target.kcal == PARTNERS.kcal:
            return Infeasible("kcal out of reach")
        return real(options, target, **kw)

    monkeypatch.setattr(plates.solver, "solve_day", not_for_partner)
    with pytest.raises(Infeasible, match="cannot carry Sam's whole day"):
        plates.resize(db, him.id, SUN, {"dinner": "skip"}, config=config, clock=clock)
    assert menu.accepted_menu_id(db, him.id, SUN) == built.menu_ids[him.id], "his were not moved"
    assert menu.accepted_menu_id(db, partner.id, SUN) == built.menu_ids[partner.id]


def test_skipping_every_dish_they_share_is_refused(pair, config) -> None:  # noqa: F811
    db, him, _partner, clock, built = _shared_weekend(pair)
    shared_slots = dict.fromkeys(_day_plates(db, him.id, SUN), "skip")
    with pytest.raises(NothingToBuild):
        plates.resize(db, him.id, SUN, shared_slots, config=config, clock=clock)
    assert menu.accepted_menu_id(db, him.id, SUN) == built.menu_ids[him.id]


def test_with_the_shared_weekend_switched_off_only_their_plates_change(
    pair,  # noqa: F811
    config,
) -> None:
    db, him, partner, clock, built = _shared_weekend(pair)
    db.insert(
        "consents",
        person_id=partner.id,
        kind="shared_meals",
        granted=0,
        wording_version="shared_v1",
    )
    got = plates.resize(db, him.id, SUN, {"dinner": "skip"}, config=config, clock=clock)
    assert menu.accepted_menu_id(db, partner.id, SUN) == built.menu_ids[partner.id]
    assert got.partner_menu_id is None and "follow" not in got.said()


def test_the_menu_tab_says_whose_plates_follow(pair, config) -> None:  # noqa: F811
    db, him, partner, clock, _built = _shared_weekend(pair)
    today = clock.today_local()
    for pid, other in ((him.id, "Sam"), (partner.id, "Alex")):
        tab = queries.menu(db, pid, SUN, today)
        day = next(d for d in tab["days"] if d["day"] == SUN.isoformat())
        assert day["plates"]["together"] == other


def test_a_floor_is_never_met_by_turning_the_ask_round(day_world, config) -> None:
    """
    fibre floor could only be met from the toast, so the toast came out the bigger plate - the
    ask turned round without a word. Now the burger asked for lots stays the bigger step up,
    and the fibre it costs is said."""
    db, pid, _base, clock = day_world
    db.insert(
        "targets",
        person_id=pid,
        week_start="2026-09-14",
        kcal=1000,
        protein_g=60,
        fat_g=20,
        carb_g=100,
        fibre_g=20,
        source="formula",
    )
    got = plates.resize(
        db,
        pid,
        MON,
        {"breakfast": "more", "lunch": "lots", "dinner": "skip", "pre_bed": "skip"},
        config=config,
        clock=clock,
    )
    sizes = _plates_of(db, got.menu_id)
    # both planned at one serving, so the multipliers are the steps up
    assert sizes["lunch"] > sizes["breakfast"], sizes
    for slot, level in (("breakfast", "more"), ("lunch", "lots")):
        assert sizes[slot] <= plates.FACTOR[level] * 1000 / 1341 * 1.1 + 0.05, (slot, sizes)
    assert [name for name, _got, _need in got.shortfall] == ["fibre"]
    assert "Short: fibre" in got.said() and "fruit or a vegetable side" in got.said()
    assert abs(got.macros.kcal - 1000) <= 1000 * 0.02, "the calories still hold"


def test_near_keeps_each_preferred_plate_within_its_share() -> None:
    from tiffin.energy import Macros

    toast = solver.SlotOption("breakfast", 400, 20, 10, 50, 10)
    burger = solver.SlotOption("lunch", 450, 40, 15, 30, 2)
    target = Macros(1000, 50, 20, 100, 20)
    prefer = {"breakfast": 1.0, "lunch": 1.3}
    free = solver.solve_day(
        [toast, burger], target, fat_floor_g=20, fibre_min=15, prefer=prefer, kcal_tol_pct=5
    )
    assert not isinstance(free, solver.Infeasible)
    assert free.multipliers["breakfast"] > 1.1, "without it, the fibre pulls the toast up"
    held = solver.solve_day(
        [toast, burger],
        target,
        fat_floor_g=20,
        fibre_min=15,
        prefer=prefer,
        near=0.1,
        kcal_tol_pct=5,
        best_effort=True,
    )
    assert not isinstance(held, solver.Infeasible)
    for slot, want in prefer.items():
        assert abs(held.multipliers[slot] - want) <= want * 0.1 + 1e-9
    assert [name for name, _got, _need in held.shortfall] == ["fibre"]
