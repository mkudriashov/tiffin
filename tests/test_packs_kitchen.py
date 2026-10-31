"""Swaps, one-meal dishes and a week of choices, ranked and built by what is in the kitchen
chicken and rice every dinner, a salmon traybake she rated 5 two weeks ago."""

from __future__ import annotations

import datetime as dt

from tiffin.food import catalogue, newdish, packs, swap
from tiffin.model import prompts

from .test_swap import MON, week  # noqa: F401

WED = MON + dt.timedelta(days=2)
TODAY = dt.date(2026, 9, 7)


def test_a_swap_offers_what_cooks_from_the_open_pack_before_one_that_opens_another(week) -> None:  # noqa: F811
    db, pid, _base, _r, _clock = week
    got = swap.candidates(db, pid, WED, "dinner", TODAY, limit=None)
    names = [c.name for c in got]
    # the salmon traybake she rated 5 would open a pack of salmon; the chickpea salad cooks from
    # the chicken the other four dinners have open, so it comes first and says so
    assert names.index("Chickpea salad") < names.index("Salmon traybake"), names
    salad = next(c for c in got if c.name == "Chickpea salad")
    assert salad.kitchen == "cooks from the open Chicken breast"
    # the dish they rated 1 is still never offered, kitchen or not
    assert "Chicken stir-fry" not in names


def test_week_mains_takes_an_open_pack_first_then_what_they_cook(week) -> None:  # noqa: F811
    db, pid, _base, _r, _clock = week
    allowed = {i.code for i in catalogue.approved(db, pid)}
    nxt = [MON + dt.timedelta(days=7 + i) for i in range(5)]
    # chicken was in more of her lunches and dinners than salmon these eight weeks
    assert packs.week_mains(db, pid, nxt, allowed=allowed) == ["chicken", "salmon"]
    # a food she now avoids is never one of them
    assert packs.week_mains(db, pid, nxt, allowed=allowed - {"salmon"}) == ["chicken"]


def test_a_new_choice_on_another_protein_is_refused_and_said(week) -> None:  # noqa: F811
    db, pid, _base, _r, _clock = week
    items = catalogue.approved(db, pid)
    n = {i.code: k for k, i in enumerate(items, start=1)}
    option = {
        "name": "Prawn rice bowl",
        "ingredients": [{"i": n["prawns"], "g": 150}, {"i": n["rice"], "g": 70}],
        "method": ["Cook the rice.", "Fry the prawns."],
        "prep_minutes": 20,
    }
    kept, why = newdish.check_options(
        [option],
        items,
        slot="dinner",
        day=WED,
        profile=None,
        blocked=[],
        taken=set(),
        mains={"chicken", "salmon"},
    )
    assert kept == [] and "not one of this week's main proteins" in why[0]
    kept, why = newdish.check_options(
        [option], items, slot="dinner", day=WED, profile=None, blocked=[], taken=set()
    )
    assert [d.name for d in kept] == ["Prawn rice bowl"], why


def test_the_prompts_carry_the_packs_and_the_weeks_proteins() -> None:
    said = prompts.menu_choices(
        ingredients=["1. Chicken breast, raw", "2. Salmon fillets, raw"],
        meal="Dinner, Monday to Friday",
        count=4,
        target="about 500 kcal",
        mains=["1. Chicken breast, raw", "2. Salmon fillets, raw"],
    )
    assert "Build every dish on these main proteins and no other meat or fish: 1. Chicken" in said
    assert "Vary the kinds of dish, the vegetables and the flavours, not the packs" in said
    plain = prompts.menu_choices(
        ingredients=["1. Oats"], meal="Breakfast", count=4, target="about 300 kcal"
    )
    assert "spread the main ingredients and the kinds of dish" in plain
    one = prompts.menu_one_dish(
        ingredients=["1. Chicken breast, raw"],
        meal="Dinner Wed",
        target="about 500 kcal",
        going="Chicken and rice",
        packs=["1. Chicken breast, raw: 280 g left of a 500 g pack (opened for Mon, Tue)"],
    )
    assert "build at least one option on one of these: 1. Chicken breast, raw: 280 g left" in one


def test_a_days_meals_reach_the_model_as_words_not_letters() -> None:
    """Found measuring this review: since 7 Sep every menu prompt's day line spelled its meals
    out a letter at a time, "b, r, e, a, k, f, a, s, t", because the meals arrive joined."""
    joined = prompts._day_line({"day": "2026-10-19", "slots": "breakfast, lunch, dinner"})
    assert joined.startswith("2026-10-19: breakfast, lunch, dinner")
    listed = prompts._day_line({"day": "2026-10-19", "slots": ["breakfast", "lunch"]})
    assert listed.startswith("2026-10-19: breakfast, lunch")
