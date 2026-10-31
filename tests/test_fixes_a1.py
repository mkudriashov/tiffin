"""Regression tests for swapping meals across planned and recorded days."""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import pytest

from tiffin import targets as targets_mod
from tiffin.clock import TZ, isoday
from tiffin.db import Db
from tiffin.errors import ModelUnavailable
from tiffin.food import menu, newdish, plates, prefs, shared, swap
from tiffin.model.gateway import ModelResult

from .test_food_shopping import seed_recipe
from .test_review_0926_weekend import (  # noqa: F401
    HIS,
    PARTNERS,
    SAT,
    SUN,
    WEEKEND,
    _agree,
    _weekend_payload,
    pair,
)
from .test_review_0926_weekend import _profile as _weekend_profile
from .test_swap import FRI, MON, _buttons, _menu, _profile, bot, page, week  # noqa: F401

TUE = MON + dt.timedelta(days=1)
WED = MON + dt.timedelta(days=2)
THU = MON + dt.timedelta(days=3)


def _ing(db: Db, code: str) -> int:
    return int(db.scalar("SELECT id FROM ingredients WHERE code = ?", (code,)))


def _no_boxes(db: Db, pid: int) -> None:
    """Her profile from September on with nothing batch-cooked: every weekday plate is hers to
    resize (a boxed lunch is eaten as it was cooked)."""
    _profile(db, pid, effective_from="2026-09-01", batch_slots=json.dumps([]))


def _slots_of(db: Db, menu_id: int, day: dt.date) -> dict[str, str]:
    return {
        str(r["slot"]): str(r["name"])
        for r in db.all(
            "SELECT ms.slot, rc.name FROM menu_days md "
            "JOIN meal_slots ms ON ms.menu_day_id = md.id JOIN recipes rc ON rc.id = ms.recipe_id "
            "WHERE md.menu_id = ? AND md.day = ?",
            (menu_id, isoday(day)),
        )
    }


def _new_dish(db: Db, pid: int, name: str, grams: dict[str, float], day: dt.date, slot: str) -> int:
    """A dish the model wrote for one meal, stored the way food/newdish.py stores one."""
    rid = seed_recipe(db, name, 1, {_ing(db, code): g for code, g in grams.items()})
    db.insert("built_dishes", person_id=pid, recipe_id=rid, day=isoday(day), slot=slot)
    return rid


# ---------------------------------------------------------------- #1 a resized day survives
def test_a_swap_keeps_a_meal_skipped_on_another_day_and_it_can_come_back(week, config) -> None:  # noqa: F811
    """A swap leaves another day's resize in force over the new menu, and
    the meal skipped there is still in the plan, so it can be brought back."""
    db, pid, _base, r, clock = week
    _no_boxes(db, pid)
    # a late start on Wednesday: no breakfast, the other two plates carry the day
    plates.resize(db, pid, WED, {"breakfast": "skip"}, config=config, clock=clock)
    swap.swap(db, pid, MON, "lunch", r["salmon"], config=config, clock=clock)

    info = plates.day_info(db, pid, WED, clock.today_local())
    assert info is not None
    assert info["levels"]["breakfast"] == "skip", "Wednesday's breakfast is still skipped"
    assert [m["slot"] for m in info["meals"]] == ["breakfast", "lunch", "dinner"]
    assert [p.name for p in swap.plates_on(db, pid, WED)] == [
        "Salmon traybake",
        "Chicken and rice",
    ], "the swap reached Wednesday's lunch, and the breakfast stays skipped"
    seg = menu.segment_menu_id(db, pid, MON)
    assert seg is not None
    assert _slots_of(db, seg, WED) == {
        "breakfast": "Porridge with yoghurt",
        "lunch": "Salmon traybake",
        "dinner": "Chicken and rice",
    }, "the shopping list and the batch plan still have Wednesday's breakfast"

    back = plates.resize(db, pid, WED, {"breakfast": "planned"}, config=config, clock=clock)
    assert back.levels["breakfast"] == "planned"
    assert [p.name for p in swap.plates_on(db, pid, WED)] == [
        "Porridge with yoghurt",
        "Salmon traybake",
        "Chicken and rice",
    ], "back to the plan is the plan with the swap in it"


def test_a_one_day_swap_leaves_another_days_levels_as_they_were(week, config) -> None:  # noqa: F811
    """A swap of Monday alone does not copy Wednesday's resized plates into
    the menu as if they were the plan."""
    db, pid, _base, r, clock = week
    _no_boxes(db, pid)
    plates.resize(db, pid, WED, {"lunch": "more", "dinner": "less"}, config=config, clock=clock)
    before = {p.slot: p.multiplier for p in swap.plates_on(db, pid, WED)}
    swap.swap(db, pid, MON, "lunch", r["salmon"], every_day=False, config=config, clock=clock)
    after = {p.slot: p.multiplier for p in swap.plates_on(db, pid, WED)}
    assert after == before, "Wednesday's plates are as she resized them"
    st = plates.state(db, pid, WED)
    assert st is not None and st["levels"] == {
        "breakfast": "planned",
        "lunch": "more",
        "dinner": "less",
    }
    seg = menu.segment_menu_id(db, pid, MON)
    assert seg is not None
    planned = db.all(
        "SELECT ms.portion_multiplier FROM menu_days md JOIN meal_slots ms "
        "ON ms.menu_day_id = md.id WHERE md.menu_id = ? AND md.day = ?",
        (seg, isoday(WED)),
    )
    assert {float(x["portion_multiplier"]) for x in planned} == {1.0}, "the plan is the plan"


def test_a_swap_through_a_resized_day_gives_it_the_plates_its_levels_give(week, config) -> None:  # noqa: F811
    """A swap that reaches a day she resized makes that
    day's plates again from the new plan at the levels she set, as a resize makes them - not the
    resized sizes nudged round the new dish, which the levels still shown no longer described."""
    db, pid, _base, r, clock = week
    _no_boxes(db, pid)
    asked = {"lunch": "less", "dinner": "more"}
    plates.resize(db, pid, WED, asked, config=config, clock=clock)
    swap.swap(db, pid, MON, "lunch", r["salmon"], config=config, clock=clock)
    st = plates.state(db, pid, WED)
    assert st is not None and st["levels"] == {"breakfast": "planned", **asked}
    got = {p.slot: (p.name, p.multiplier) for p in swap.plates_on(db, pid, WED)}
    assert got["lunch"][0] == "Salmon traybake"
    plates.resize(db, pid, WED, asked, config=config, clock=clock)
    again = {p.slot: (p.name, p.multiplier) for p in swap.plates_on(db, pid, WED)}
    assert got == again, "the same levels asked for again change nothing"


# ---------------------------------------------------------------- #2 new dishes stay offered
def test_new_dishes_stay_on_the_swap_list_past_six_dishes_already_eaten(page) -> None:  # noqa: F811
    """Dishes the model has just written for a meal are always on that meal's
    swap list, however many dishes she has eaten rank above them."""
    client, db, partners, _his, _r = page
    for i in range(7):
        rid = seed_recipe(
            db,
            f"Chicken rice bowl {i + 1}",
            1,
            {_ing(db, "chicken"): 150 + 5 * i, _ing(db, "rice"): 60, _ing(db, "broccoli"): 100},
        )
        _menu(db, partners, dt.date(2026, 8, 10) + dt.timedelta(days=i % 4), 1, {"dinner": rid})
        db.insert("ratings", person_id=partners, recipe_id=rid, score=5, note=None)
    _new_dish(
        db,
        partners,
        "Prawn and pepper rice",
        {"prawns": 200, "pepper": 150, "rice": 50},
        MON,
        "lunch",
    )
    got = client.get(
        "/api/menu/swap",
        params={"day": isoday(MON), "slot": "lunch"},
        headers={"Tailscale-User-Login": "sam@example.com"},
    )
    assert got.status_code == 200, got.text
    names = [c["name"] for c in got.json()["candidates"]]
    assert "Prawn and pepper rice" in names, names
    assert len(names) <= swap.OFFER_MAX
    assert [c["new"] for c in got.json()["candidates"]].count(True) == 1


# ---------------------------------------------------------------- #17 eaten meals held
def test_a_swap_today_leaves_the_meals_already_eaten_as_they_were(week, config) -> None:  # noqa: F811
    """Swapping tonight's dinner does not resize the breakfast and lunch
    already eaten today."""
    db, pid, _base, r, clock = week
    clock.set_local(dt.datetime(2026, 9, 16, 19, 0, tzinfo=TZ))
    before = {p.slot: (p.multiplier, p.macros.kcal) for p in swap.plates_on(db, pid, WED)}
    got = swap.swap(
        db, pid, WED, "dinner", r["chickpea"], every_day=False, config=config, clock=clock
    )
    assert got.days == (WED,)
    after = {p.slot: (p.multiplier, p.macros.kcal) for p in swap.plates_on(db, pid, WED)}
    assert after["breakfast"] == before["breakfast"], "eaten this morning"
    assert after["lunch"] == before["lunch"], "eaten at one"
    assert [p.name for p in swap.plates_on(db, pid, WED)][2] == "Chickpea salad"


# ---------------------------------------------------------------- #24 boxes stay cooked
def test_a_recorded_day_does_not_unhold_the_boxes_already_cooked(week, config) -> None:  # noqa: F811
    """Once Wednesday is recorded, a swap of Thursday's dinner still holds
    the lunch box cooked on Sunday and says the new dish is cooked fresh - twice in a row."""
    db, pid, _base, r, clock = week
    clock.set_local(dt.datetime(2026, 9, 16, 21, 0, tzinfo=TZ))
    db.insert("day_confirmations", person_id=pid, day=isoday(WED), status="confirmed", basis="menu")
    got = swap.swap(db, pid, THU, "dinner", r["salmon"], config=config, clock=clock)
    lunch = {p.slot: p.multiplier for p in swap.plates_on(db, pid, THU)}["lunch"]
    assert lunch == pytest.approx(1.0), "the lunch box holds what it was cooked to hold"
    assert "boxes are already cooked" in got.said()
    # a second swap the same evening: the menu the first one made starts on Thursday, and the
    # boxes were still cooked on Sunday
    again = swap.swap(db, pid, THU, "lunch", r["chickpea"], config=config, clock=clock)
    dinner = {p.slot: p.multiplier for p in swap.plates_on(db, pid, THU)}["dinner"]
    held = {p.slot: p.multiplier for p in swap.plates_on(db, pid, FRI)}["dinner"]
    assert dinner == pytest.approx(held)
    assert "The Lentil dal boxes are already cooked" in again.said()


# ---------------------------------------------------------------- #38 eaten is not still to come
def test_meals_already_eaten_today_are_not_still_on_the_menu(week, config) -> None:  # noqa: F811
    """In the evening, today's lunch is eaten - not "still on your menu" - and
    "Swap it now" leaves it as it was, swapping from tomorrow on."""
    db, pid, _base, _r, clock = week
    clock.set_local(dt.datetime(2026, 9, 16, 20, 45, tzinfo=TZ))
    prefs.amend(db, pid, "avoid", add=["lentils"], clock=clock)
    still = prefs.still_on_menu(db, pid, clock.today_local())
    assert [d["days"] for d in still] == [[isoday(THU), isoday(FRI)]]
    assert "lunch Thu to Fri 18 Sep" in prefs.still_said(still)
    cleaned = swap.swap_avoided(db, pid, config=config, clock=clock)
    assert [s.days for s in cleaned.swapped] == [(THU, FRI)]
    assert [p.name for p in swap.plates_on(db, pid, WED)][1] == "Lentil dal", "eaten at one"
    # the dish put in cooks from the chicken the week's dinners have open, not the salmon that
    # would open a pack
    assert [p.name for p in swap.plates_on(db, pid, THU)][1] == "Chickpea salad"


def test_a_meal_still_to_come_today_is_still_on_the_menu(week) -> None:  # noqa: F811
    """At four in the afternoon lunch is behind her and dinner is not."""
    db, pid, _base, _r, clock = week
    clock.set_local(dt.datetime(2026, 9, 16, 16, 0, tzinfo=TZ))
    prefs.amend(db, pid, "avoid", add=["lentils", "basmati rice"], clock=clock)
    still = {d["name"]: d["days"] for d in prefs.still_on_menu(db, pid, clock.today_local())}
    assert still["Lentil dal"][0] == isoday(THU)
    assert still["Chicken and rice"][0] == isoday(WED), "tonight's dinner is still to come"


def test_swap_a_dish_after_lunch_opens_the_lunch_dish_from_tomorrow(bot) -> None:  # noqa: F811
    """At four in the afternoon "Swap a dish" opens the
    lunch eaten at one from tomorrow, says so, and the swap leaves today's lunch as it was."""
    _send, tap, db, pid, _r, clock = bot
    clock.set_local(dt.datetime(2026, 9, 16, 16, 0, tzinfo=TZ))
    seg = menu.segment_menu_id(db, pid, WED)
    assert seg is not None
    picks = _buttons(tap(f"k:swappick:{seg}")[-1])
    assert picks["Lunch: Lentil dal"] == f"k:swapmeal:{isoday(THU)}:lunch"
    assert picks["Dinner: Chicken and rice"] == f"k:swapmeal:{isoday(WED)}:dinner", "still to come"
    offer = tap(picks["Lunch: Lentil dal"])[-1]
    assert "(lunch Thu to Fri 18 Sep)" in offer["text"]
    done = tap(_buttons(offer)["Salmon traybake"])[-1]
    assert "Salmon traybake in for Lentil dal, lunch Thu to Fri 18 Sep" in done["text"]
    assert [p.name for p in swap.plates_on(db, pid, WED)][1] == "Lentil dal", "eaten at one"
    assert [p.name for p in swap.plates_on(db, pid, THU)][1] == "Salmon traybake"


def test_the_days_a_swap_lists_after_lunch_are_the_days_it_changes(week, config) -> None:  # noqa: F811
    """After lunch the lunch dish's days on offer leave
    today out, whichever day the Swap was opened on, and a swap of every day changes just those.
    Today's lunch changes only when today alone is asked for - or is the only day it is on."""
    db, pid, _base, r, clock = week
    clock.set_local(dt.datetime(2026, 9, 16, 16, 0, tzinfo=TZ))
    today = clock.today_local()
    rest = [isoday(THU), isoday(FRI)]
    assert swap.options(db, pid, WED, "lunch", today)["days"] == rest
    assert swap.options(db, pid, THU, "lunch", today)["days"] == rest
    assert swap.options(db, pid, WED, "dinner", today)["days"] == [isoday(WED), *rest]
    got = swap.swap(db, pid, WED, "lunch", r["salmon"], config=config, clock=clock)
    assert got.days == (THU, FRI)
    assert [p.name for p in swap.plates_on(db, pid, WED)][1] == "Lentil dal", "eaten at one"
    one = swap.swap(
        db, pid, WED, "lunch", r["chickpea"], every_day=False, config=config, clock=clock
    )
    assert one.days == (WED,), "today alone, asked for"
    clock.set_local(dt.datetime(2026, 9, 18, 16, 0, tzinfo=TZ))
    assert swap.options(db, pid, FRI, "lunch", FRI)["days"] == [isoday(FRI)]
    last = swap.swap(db, pid, FRI, "lunch", r["chickpea"], config=config, clock=clock)
    assert last.days == (FRI,), "the only day it is on: the swap can mean nothing else"


@pytest.mark.parametrize("recorded", [False, True], ids=["her-check-in-time", "her-day-recorded"])
def test_a_dinner_cooked_together_is_eaten_for_both_once_either_has_had_it(
    pair,  # noqa: F811
    config,
    recorded: bool,
) -> None:
    """On a day the two cook together, today's dinner
    is eaten for both once either has had it. Each one's own check-in time decided it apart, so
    at 20:15 her Saturday dinner stayed and his was swapped: two dinners on a shared day, and a
    reply saying her plates followed. Her day recorded early is had whole, the same way."""
    from .test_plates import _shared_weekend

    db, him, partner, clock, _built = _shared_weekend(pair)
    sunday = {p.slot: p for p in swap.plates_on(db, him.id, SUN)}["dinner"]
    # Saturday's dinner made Sunday's, for both, while it is still Thursday
    swap.put(
        db, him.id, SAT, "dinner", sunday.recipe_id, sunday.name, every_day=False, config=config,
        clock=clock,
    )  # fmt: skip
    for p in (him, partner):
        assert {x.slot: x.name for x in swap.plates_on(db, p.id, SAT)}["dinner"] == sunday.name
    bake = seed_recipe(
        db,
        "Weekend chicken bake",
        1,
        {_ing(db, "chicken"): 200, _ing(db, "rice"): 80, _ing(db, "broccoli"): 200},
    )
    if recorded:
        clock.set_local(dt.datetime(2026, 9, 19, 19, 0, tzinfo=TZ))
        db.insert(
            "day_confirmations",
            person_id=partner.id,
            day=isoday(SAT),
            status="confirmed",
            basis="menu",
        )
    else:
        _weekend_profile(db, him.id, effective_from="2026-09-18", confirm_time="21:00")
        _weekend_profile(
            db,
            partner.id,
            sex="f",
            fibre_g_min=15,
            effective_from="2026-09-18",
            confirm_time="20:00",
        )
        clock.set_local(dt.datetime(2026, 9, 19, 20, 15, tzinfo=TZ))
    today = clock.today_local()
    assert swap.options(db, him.id, SUN, "dinner", today)["days"] == [isoday(SUN)]
    got = swap.put(
        db, him.id, SUN, "dinner", bake, "Weekend chicken bake", every_day=True, config=config,
        clock=clock,
    )  # fmt: skip
    assert got.days == (SUN,), got.said()
    assert "Sam's plates follow" in got.said()
    for p in (him, partner):
        dinners = {
            d: {x.slot: x.name for x in swap.plates_on(db, p.id, d)}["dinner"] for d in (SAT, SUN)
        }
        assert dinners == {SAT: sunday.name, SUN: "Weekend chicken bake"}, (p.id, dinners)


# ---------------------------------------------------------------- #40 one plate of two
def _uneven_weekend(pair):  # noqa: F811
    """The shared weekend built for the owner with four meals a day and the partner with three:
    the snack is
    his alone."""
    db, (him, partner), clock = pair
    db.insert(
        "profiles",
        person_id=partner.id,
        effective_from="2026-09-10",
        sex="f",
        birth_date="1990-03-15",
        height_cm=168.0,
        activity_factor=1.55,
        kcal_floor=1200,
        meals_per_day=3,
        fibre_g_min=15,
        segment_days=json.dumps([[0, 1, 2, 3, 4]]),
        variety_json=json.dumps({"all": "same"}),
    )
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
    profiles = {p.id: targets_mod.profile_on(db, p.id, SAT) for p in (him, partner)}
    shared.build(
        db,
        WEEKEND,
        [him, partner],
        profiles,
        {him.id: HIS, partner.id: PARTNERS},
        lambda _prompt, _hint: (_weekend_payload(), "fake"),
        prompt_for=lambda hint: "prompt" + hint,
        clock=clock,
    )
    return db, him, partner, clock


def test_their_own_snack_on_a_shared_weekend_is_swapped_for_him_alone(pair, config) -> None:  # noqa: F811
    """A plate only the owner has is his alone - her menus are not rewritten,
    he is
    not told her plates follow, and the rest of the weekend is still cooked together."""
    db, him, partner, clock = _uneven_weekend(pair)
    assert "snack" not in {p.slot for p in swap.plates_on(db, partner.id, SUN)}
    assert swap.options(db, him.id, SUN, "snack", clock.today_local())["together"] is None
    partners_before = db.scalar("SELECT COUNT(*) FROM menus WHERE person_id = ?", (partner.id,))
    pick = swap.candidates(db, him.id, SUN, "snack", clock.today_local())[0]
    got = swap.swap(
        db, him.id, SUN, "snack", pick.recipe_id, every_day=False, config=config, clock=clock
    )
    assert got.together is None and got.partner_menu_id is None
    assert "Sam" not in got.said()
    assert (
        db.scalar("SELECT COUNT(*) FROM menus WHERE person_id = ?", (partner.id,))
        == partners_before
    )
    assert swap.cooked_with(db, him.id, SUN) == (partner.id, "Sam"), (
        "lunch is still cooked together"
    )
    seen = shared.partner_plate(
        db, partner.id, menu.accepted_menu_id(db, partner.id, SUN) or 0, SUN, "lunch"
    )
    assert seen is not None and seen["name"] == "Alex"
    assert swap.options(db, him.id, SUN, "lunch", clock.today_local())["together"] == "Sam"


class _SnackModel:
    """Answers menu_one_dish with one snack from the weekend's catalogue."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def json_task(self, task: str, prompt: str, schema_name: str, *, person_id: int) -> ModelResult:
        self.prompts.append(prompt)
        data = {
            "options": [
                {
                    "name": "Oaty yoghurt pot",
                    "ingredients": [{"i": 8, "g": 200}, {"i": 1, "g": 30}],
                    "method": ["Stir the oats into the yoghurt."],
                    "prep_minutes": 2,
                }
            ]
        }
        return ModelResult(text="", data=data, model_used="fake", backend="fake")


class _App:
    def __init__(self, model: Any) -> None:
        self.model = model
        self.config = None

    def gateway(self, _db: Db) -> Any:
        return self.model


def test_new_dishes_for_their_own_snack_need_only_their_consent(pair) -> None:  # noqa: F811
    """New dishes for a plate only the owner has are written for him alone, so
    her
    withdrawn consent does not stop them and nothing of hers is sent or stored."""
    db, him, partner, clock = _uneven_weekend(pair)
    db.insert(
        "consents",
        person_id=partner.id,
        kind="anthropic_processing",
        granted=0,
        wording_version="v2",
    )
    model = _SnackModel()
    got = newdish.ask(_App(model), db, him.id, SUN, "snack", every_day=False, clock=clock)
    assert got.together is None
    assert "cooked together" not in model.prompts[0]
    assert [d["name"] for d in got.dishes] == ["Oaty yoghurt pot"]
    assert db.scalar("SELECT COUNT(*) FROM built_dishes WHERE person_id = ?", (partner.id,)) == 0
    with pytest.raises(ModelUnavailable):
        newdish.ask(_App(model), db, him.id, SUN, "lunch", every_day=False, clock=clock)


# ---------------------------------------------------------------- #51 code's choice is not hers
def test_swapping_avoided_dishes_records_no_choice_of_partners(week, config) -> None:  # noqa: F811
    """A dish code put in for an avoided one is not recorded as her swap."""
    db, pid, _base, _r, clock = week
    prefs.amend(db, pid, "avoid", add=["lentils"], clock=clock)
    cleaned = swap.swap_avoided(db, pid, config=config, clock=clock)
    assert [s.old for s in cleaned.swapped] == ["Lentil dal"]
    assert db.scalar("SELECT COUNT(*) FROM dish_events WHERE person_id = ?", (pid,)) == 0


# ---------------------------------------------------------------- #78 a way on, not a dead end
def test_a_dish_nothing_can_stand_in_for_comes_with_its_buttons(bot) -> None:  # noqa: F811
    """ "Swap it now" that finds nothing offers Change one food and New
    dishes for that meal, and points at no whole-menu rebuild."""
    send, _tap, db, pid, _r, clock = bot
    prefs.amend(db, pid, "avoid", add=["lentils", "salmon", "chickpeas"], clock=clock)
    out = send("/swap")
    text = out[-1]["text"]
    assert "Lentil dal" in text and "/cook replace" not in text and "to the bot" not in text
    buttons = _buttons(out[-1])
    assert buttons == {
        "Change one food in it": f"k:subst:{isoday(MON)}:lunch",
        "New dishes": f"k:newdish:{isoday(MON)}:lunch",
    }


# ---------------------------------------------------------------- #91 not twice in a day
def test_an_every_day_swap_never_puts_a_dish_on_a_day_twice(week, config) -> None:  # noqa: F811
    """A day that already has the dish at another meal keeps the old one, and
    the reply says so."""
    db, pid, _base, r, clock = week
    swap.swap(db, pid, TUE, "dinner", r["salmon"], every_day=False, config=config, clock=clock)
    assert swap.candidates(db, pid, MON, "lunch", clock.today_local())[0].name == "Salmon traybake"
    got = swap.swap(db, pid, MON, "lunch", r["salmon"], config=config, clock=clock)
    tue = [p.name for p in swap.plates_on(db, pid, TUE)]
    assert tue.count("Salmon traybake") == 1, tue
    assert tue[1] == "Lentil dal"
    assert TUE not in got.days and got.days == (MON, WED, THU, FRI)
    assert "Lentil dal stays on Tue 15 Sep" in got.said()


def test_the_dish_of_a_meal_skipped_on_a_day_is_still_that_days_dish(week, config) -> None:  # noqa: F811
    """The dish of a meal she skipped on a resized day is
    still on that day's plan, so it is not offered for another meal there - and putting it in
    anyway is refused before anything is stored or recorded as her choice."""
    db, pid, _base, r, clock = week
    _no_boxes(db, pid)
    plates.resize(db, pid, WED, {"lunch": "skip"}, config=config, clock=clock)
    today = clock.today_local()
    offered = [c.name for c in swap.candidates(db, pid, WED, "dinner", today, limit=None)]
    assert "Lentil dal" not in offered, offered
    menus = db.scalar("SELECT COUNT(*) FROM menus WHERE person_id = ?", (pid,))
    with pytest.raises(ValueError, match="not one of the ones on offer"):
        swap.swap(db, pid, WED, "dinner", r["dal"], every_day=False, config=config, clock=clock)
    with pytest.raises(ValueError, match="Lentil dal is already at another meal on Wed 16 Sep"):
        swap.put(
            db, pid, WED, "dinner", r["dal"], "Lentil dal", every_day=False, config=config,
            clock=clock,
        )  # fmt: skip
    assert db.scalar("SELECT COUNT(*) FROM menus WHERE person_id = ?", (pid,)) == menus
    assert db.scalar("SELECT COUNT(*) FROM dish_events WHERE person_id = ?", (pid,)) == 0


# ---------------------------------------------------------------- #92 had before, as promised
def test_swap_now_uses_eaten_dish_not_passed_option(week, config) -> None:  # noqa: F811
    """ "Swap it now" chooses from dishes she has eaten, as it promises, even
    when a model-written dish she was shown ranks higher."""
    db, pid, _base, _r, clock = week
    new_id = _new_dish(
        db,
        pid,
        "Pepper chicken rice",
        {"chicken": 150, "rice": 60, "pepper": 80},
        MON,
        "lunch",
    )
    prefs.amend(db, pid, "avoid", add=["salmon"], clock=clock)
    offered = swap.candidates(db, pid, MON, "lunch", clock.today_local())
    assert offered[0].recipe_id == new_id, "the untried dish ranks first"
    prefs.amend(db, pid, "avoid", add=["lentils"], clock=clock)
    cleaned = swap.swap_avoided(db, pid, config=config, clock=clock)
    assert [s.new for s in cleaned.swapped] == ["Chickpea salad"]


def test_swap_it_now_with_only_untried_dishes_says_so(week, config) -> None:  # noqa: F811
    """When only a model-written dish fits, the dish stays and the way to it
    is named, rather than an untried dish going on every day unasked."""
    db, pid, _base, _r, clock = week
    _new_dish(
        db, pid, "Pepper chicken rice", {"chicken": 150, "rice": 60, "pepper": 80}, MON, "lunch"
    )
    prefs.amend(db, pid, "avoid", add=["lentils", "salmon", "chickpeas"], clock=clock)
    cleaned = swap.swap_avoided(db, pid, config=config, clock=clock)
    assert cleaned.swapped == []
    assert [s["name"] for s in cleaned.stuck] == ["Lentil dal"]
    assert [p.name for p in swap.plates_on(db, pid, MON)][1] == "Lentil dal"


def test_swap_it_now_does_not_put_in_a_dish_only_on_a_menu_still_to_come(week, config) -> None:  # noqa: F811
    """A dish on an accepted menu for a later week is
    not one she has had before, and "Swap it now" does not put it on every day as if it were."""
    db, pid, _base, r, clock = week
    wrap = seed_recipe(
        db,
        "Turkey pepper wrap",
        1,
        {_ing(db, "chicken"): 150, _ing(db, "pepper"): 100, _ing(db, "rice"): 60},
    )
    _menu(db, pid, dt.date(2026, 9, 21), 5, {"breakfast": r["porridge"], "lunch": wrap})
    prefs.amend(db, pid, "avoid", add=["lentils", "salmon", "chickpeas"], clock=clock)
    offered = {c.name: c for c in swap.candidates(db, pid, MON, "lunch", clock.today_local())}
    wrapped = offered["Turkey pepper wrap"]
    assert (wrapped.why, wrapped.new, wrapped.had) == ("not had yet", False, False)
    cleaned = swap.swap_avoided(db, pid, config=config, clock=clock)
    assert cleaned.swapped == []
    assert [(s["name"], s["why"]) for s in cleaned.stuck] == [
        ("Lentil dal", "only dishes you have not had yet fit that meal")
    ]
    assert [p.name for p in swap.plates_on(db, pid, MON)][1] == "Lentil dal"


# ---------------------------------------------------------------- #10 #56 #100 #137 the words
def test_a_dish_on_partner_plate_for_two_weeks_is_not_called_not_had_yet(week) -> None:  # noqa: F811
    """ "Had" is read from what was on her plate, answered or not, so a dish
    she had two weeks running is one she often has."""
    db, pid, _base, _r, clock = week
    offered = {c.name: c for c in swap.candidates(db, pid, MON, "lunch", clock.today_local())}
    assert offered["Chickpea salad"].why == "you often have it; had lately"


def test_a_dish_swapped_away_before_it_was_eaten_was_never_had(week, config) -> None:  # noqa: F811
    """A dish swapped off her plates before its days came is not "last had"
    on the days it never reached her."""
    db, pid, _base, r, clock = week
    swap.swap(db, pid, MON, "lunch", r["salmon"], config=config, clock=clock)
    oct5 = dt.date(2026, 10, 5)
    _menu(db, pid, oct5, 5, {"breakfast": r["porridge"], "lunch": r["chickpea"]})
    clock.set_local(dt.datetime(2026, 10, 5, 9, 0, tzinfo=TZ))
    offered = {c.name: c for c in swap.candidates(db, pid, oct5, "lunch", clock.today_local())}
    assert "Lentil dal" in offered
    assert "last had" not in offered["Lentil dal"].why
    assert "had lately" not in offered["Lentil dal"].why


def test_a_dish_only_ever_swapped_in_is_not_one_partner_often_has(week, config) -> None:  # noqa: F811
    """A new dish she swapped onto a day still to come reads as chosen
    before, not as a habit."""
    db, pid, _base, _r, clock = week
    curry = _new_dish(
        db, pid, "Prawn curry", {"prawns": 200, "rice": 60, "pepper": 100}, FRI, "dinner"
    )
    swap.swap(db, pid, FRI, "dinner", curry, every_day=False, config=config, clock=clock)
    offered = {c.name: c for c in swap.candidates(db, pid, TUE, "lunch", clock.today_local())}
    assert offered["Prawn curry"].why == "you chose it before"


def test_last_had_is_counted_back_from_today(week) -> None:  # noqa: F811
    """The same dish reads the same on next Monday's and next Friday's
    swap lists: when she last had it is measured from today."""
    db, pid, _base, _r, clock = week
    today = clock.today_local()
    mon = {c.name: c for c in swap.candidates(db, pid, MON, "dinner", today)}
    fri = {c.name: c for c in swap.candidates(db, pid, FRI, "dinner", today)}
    assert mon["Salmon traybake"].why == fri["Salmon traybake"].why == "rated 5; had lately"
