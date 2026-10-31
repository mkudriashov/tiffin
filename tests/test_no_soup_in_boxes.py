"""No soups in meal-prep boxes.

The batch-cooked lunch and dinner go into flat boxes for the week. A dish that is a soup - by its
name, or by being mostly stock whatever it is called - is sent back for a batch-cooked weekday
slot, like any other rule the model broke; the weekend is cooked on the day and never asked.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from pathlib import Path

import pytest

from tiffin.errors import ModelInvalid
from tiffin.food import catalogue, menu
from tiffin.food.recipes import RecipeDraft

from .test_food_shopping import seed_ingredient
from .test_menu import a_day
from .test_review_0926_weekend import HIS, SAT, SUN, WEEKEND, pair  # noqa: F401

MON = dt.date(2026, 9, 21)


def _renamed(day: dt.date, slot: str, name: str) -> dict:
    out = a_day(day)
    for s in out["slots"]:
        if s["slot"] == slot:
            s["recipe"]["name"] = name
    return out


def test_a_boxed_soup_is_sent_back_and_a_weekend_one_is_not(pair) -> None:  # noqa: F811
    db, (him, _partner), clock = pair
    profile = db.one("SELECT * FROM profiles WHERE person_id = ?", (him.id,))
    assert "lunch" in menu.batch_slots(profile), "the default cook boxes lunch and dinner"
    with pytest.raises(ModelInvalid, match="is a soup"):
        menu.build_plan(
            db, him.id, menu.Segment(MON, MON),
            {"days": [_renamed(MON, "lunch", "Chicken, carrot and red lentil soup")]},
            targets=HIS, profile=profile, clock=clock,
        )  # fmt: skip
    with pytest.raises(ModelInvalid, match="is a soup"):
        menu.build_plan(
            db, him.id, menu.Segment(MON, MON),
            {"days": [_renamed(MON, "dinner", "Chicken and lentil broth with cheddar")]},
            targets=HIS, profile=profile, clock=clock,
        )  # fmt: skip
    # the weekend is cooked on the day and eaten at home: a soup is the cook's business there
    weekend = {
        "days": [
            _renamed(SAT, "lunch", "Roast tomato soup with a cheese toastie"),
            _renamed(SUN, "lunch", "Smash burger"),
        ]
    }
    for slot in ("breakfast", "dinner", "snack", "pre_bed"):
        for s in weekend["days"][1]["slots"]:
            if s["slot"] == slot:
                s["recipe"]["name"] += " the Sunday way"
    try:
        menu.build_plan(
            db, him.id, WEEKEND, weekend, targets=HIS, profile=profile, clock=clock, weekend=True
        )
    except ModelInvalid as e:
        assert "soup" not in str(e), e


def test_a_soup_by_any_other_name_is_still_a_soup(pair) -> None:  # noqa: F811
    """The week's lentil "soup" was 47% stock: a dish mostly of stock is one, whatever its name."""
    db, (him, _partner), _clock = pair
    seed_ingredient(db, "chicken_stock", "Chicken stock, ready made", "tins", 500.0, "500 ml")
    cat = catalogue.approved(db, him.id)
    stock = next(i for i, ing in enumerate(cat, start=1) if ing.code == "chicken_stock")
    chicken = next(i for i, ing in enumerate(cat, start=1) if ing.code == "chicken")
    pot = RecipeDraft.from_dict(
        {
            "name": "Chicken and lentil pot",
            "ingredients": [{"i": chicken, "g": 150}, {"i": stock, "g": 300}],
            "method": ["Simmer it all."],
            "prep_minutes": 25,
        }
    )
    key = menu.slot_key(MON, "lunch")
    said = menu._soup_errors({key: pot}, cat, frozenset({"lunch", "dinner"}))
    assert len(said) == 1 and "is a soup" in said[0]
    drier = RecipeDraft.from_dict(
        {
            "name": "Chicken and lentil pot",
            "ingredients": [{"i": chicken, "g": 250}, {"i": stock, "g": 60}],
            "method": ["Simmer it down until thick."],
            "prep_minutes": 25,
        }
    )
    assert menu._soup_errors({key: drier}, cat, frozenset({"lunch", "dinner"})) == []
    assert menu._soup_errors({key: pot}, cat, frozenset({"dinner"})) == [], "lunch not boxed"


def test_a_one_pot_pasta_is_not_a_soup_and_a_lentil_pot_still_is(pair) -> None:  # noqa: F811
    """6 Oct: a one-pot carbonara was sent back as a soup and cost the partner's build an attempt.
    The
    pasta drinks the stock; the 45% of stock in the stored one-pot spaghetti (recipe 151, these
    grams) is not in the box. A lentil pot is not let off, and nor is a pasta swimming in stock."""
    db, (him, _partner), _clock = pair
    seed_ingredient(db, "chicken_stock", "Chicken stock, ready made", "tins", 500.0, "500 ml")
    seed_ingredient(
        db, "spaghetti_wholewheat", "Wholewheat spaghetti, dry", "grains", 500.0, "500 g"
    )
    seed_ingredient(db, "lentils_red", "Red lentils, dry", "grains", 500.0, "500 g")
    seed_ingredient(db, "peas_frozen", "Peas, frozen", "frozen", 900.0, "900 g")
    cat = catalogue.approved(db, him.id)
    at = {ing.code: i for i, ing in enumerate(cat, start=1)}

    def pot(grams: Mapping[str, float]) -> RecipeDraft:
        return RecipeDraft.from_dict(
            {
                "name": "Creamy chicken and bacon carbonara with peas",
                "ingredients": [{"i": at[c], "g": g} for c, g in grams.items()],
                "method": ["Simmer the spaghetti in the stock until it has drunk it."],
                "prep_minutes": 25,
            }
        )

    key = menu.slot_key(MON, "dinner")
    boxed = frozenset({"lunch", "dinner"})
    one_pot = {"chicken_stock": 300, "chicken": 100, "peas_frozen": 100, "spaghetti_wholewheat": 70}
    assert menu._soup_errors({key: pot(one_pot)}, cat, boxed) == []
    lentil = {c: g for c, g in one_pot.items() if c != "spaghetti_wholewheat"} | {"lentils_red": 70}
    assert len(menu._soup_errors({key: pot(lentil)}, cat, boxed)) == 1, "lentils thicken, not drink"
    swimming = {
        "chicken_stock": 500,
        "chicken": 100,
        "peas_frozen": 100,
        "spaghetti_wholewheat": 30,
    }
    assert len(menu._soup_errors({key: pot(swimming)}, cat, boxed)) == 1


def test_the_weekday_brief_no_longer_suggests_a_soup() -> None:
    brief = (
        Path(__file__).resolve().parents[1] / "config" / "prompts" / "menu_propose.md"
    ).read_text(encoding="utf-8")
    assert "a chilli, a soup, a salad" not in brief
    assert "No soups, broths or other liquid dishes" in brief
