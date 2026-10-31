"""Every task schema loads, accepts a good payload and rejects the shapes the rules forbid."""

from __future__ import annotations

import copy

import pytest

from tiffin.errors import ModelInvalid
from tiffin.model import schemas

GOOD = {
    "menu_proposal": {
        "days": [
            {
                "day": "2026-09-08",
                "slots": [
                    {
                        "slot": "lunch",
                        "recipe": {
                            "name": "Chicken and rice",
                            "ingredients": [{"i": 12, "g": 180}, {"i": 3, "g": 75.5}],
                            "method": ["Cook the rice.", "Fry the chicken."],
                            "prep_minutes": 25,
                        },
                    }
                ],
            }
        ]
    },
    "one_dish": {
        "options": [
            {
                "name": "Chicken and rice",
                "ingredients": [{"i": 12, "g": 180}, {"i": 3, "g": 75.5}],
                "method": ["Cook the rice.", "Fry the chicken."],
                "prep_minutes": 25,
            }
        ]
    },
    "dish_options": {
        "options": [
            {
                "name": f"Breakfast pot {n}",
                "ingredients": [{"i": 4, "g": 60}, {"i": 9, "g": 150}],
                "method": ["Stir the oats into the yoghurt."],
                "prep_minutes": 5,
            }
            for n in range(1, 13)
        ]
    },
    "offplan_estimate": {
        "items": [
            {
                "name": "flat white",
                "kcal": 120,
                "protein_g": 6,
                "fat_g": 6,
                "carb_g": 9,
                "fibre_g": 0,
            }
        ],
        "kcal": 120,
        "protein_g": 6,
        "fat_g": 6,
        "carb_g": 9,
        "fibre_g": 0,
        "confidence": "medium",
    },
    "sunday_prose": {"lines": ["Trend 99.8 kg, down 0.4 kg.", "Six of seven days confirmed."]},
    "plan_parse": {
        "sessions": [
            {"weekday": 0, "kind": "climb", "target_minutes": 90, "start_time": "18:30"},
            {"weekday": 1, "kind": "rest", "target_minutes": 0, "start_time": None},
            {"weekday": 2, "kind": "ride", "target_minutes": 60, "start_time": None},
            {"weekday": 3, "kind": "rest", "target_minutes": 0, "start_time": None},
            {"weekday": 4, "kind": "rest", "target_minutes": 0, "start_time": None},
            {"weekday": 5, "kind": "ropes", "target_minutes": 120, "start_time": "10:00"},
            {"weekday": 6, "kind": "rest", "target_minutes": 0, "start_time": None},
        ]
    },
    "intent": {"kind": "log_offplan", "payload": {"description": "two beers", "day": "2026-09-07"}},
}


@pytest.mark.parametrize("name", schemas.NAMES)
def test_every_schema_loads_and_accepts_its_good_payload(name: str) -> None:
    schema = schemas.load(name)
    assert schema["type"] == "object" and schema["additionalProperties"] is False
    assert schemas.errors(schema, GOOD[name]) == []
    assert schemas.validate(name, GOOD[name]) is GOOD[name]


def test_unknown_schema_name_is_a_key_error() -> None:
    with pytest.raises(KeyError):
        schemas.load("nope")


def test_for_model_drops_only_the_dialect_key() -> None:
    schema = schemas.load("sunday_prose")
    slim = schemas.for_model(schema)
    assert "$schema" in schema and "$schema" not in slim
    assert slim["properties"] == schema["properties"] and slim["title"] == "sunday_prose"


def _menu_with(**recipe: object) -> dict:
    payload = copy.deepcopy(GOOD["menu_proposal"])
    payload["days"][0]["slots"][0]["recipe"].update(recipe)
    return payload


@pytest.mark.parametrize(
    ("name", "bad", "needle"),
    [
        ("menu_proposal", _menu_with(kcal=500), "kcal"),
        ("menu_proposal", _menu_with(name="x" * 61), "name"),
        ("menu_proposal", _menu_with(ingredients=[{"i": 1, "g": 10}] * 13), "ingredients"),
        ("menu_proposal", _menu_with(ingredients=[{"i": 0, "g": 10}]), "ingredients/0/i"),
        ("menu_proposal", _menu_with(ingredients=[{"i": 1, "g": 0}]), "ingredients/0/g"),
        ("menu_proposal", _menu_with(ingredients=[{"i": 1, "grams": 10}]), "ingredients/0"),
        ("menu_proposal", _menu_with(method=["step"] * 13), "method"),
        ("menu_proposal", _menu_with(prep_minutes=121), "prep_minutes"),
        ("menu_proposal", {"days": []}, "days"),
        ("offplan_estimate", {**GOOD["offplan_estimate"], "confidence": "sure"}, "confidence"),
        ("offplan_estimate", {**GOOD["offplan_estimate"], "kcal": -1}, "kcal"),
        ("offplan_estimate", {**GOOD["offplan_estimate"], "items": []}, "items"),
        ("sunday_prose", {"lines": ["x" * 201]}, "lines/0"),
        ("sunday_prose", {"lines": ["a"] * 7}, "lines"),
        ("plan_parse", {"sessions": GOOD["plan_parse"]["sessions"][:6]}, "sessions"),
        (
            "plan_parse",
            {"sessions": [{**s, "kind": "yoga"} for s in GOOD["plan_parse"]["sessions"]]},
            "kind",
        ),
        (
            "plan_parse",
            {"sessions": [{**s, "start_time": "6pm"} for s in GOOD["plan_parse"]["sessions"]]},
            "start_time",
        ),
        (
            "plan_parse",
            {"sessions": [{**s, "weekday": 7} for s in GOOD["plan_parse"]["sessions"]]},
            "weekday",
        ),
        ("intent", {"kind": "delete_everything", "payload": {}}, "kind"),
        ("intent", {"kind": "set_pause"}, "payload"),
    ],
)
def test_bad_payloads_are_rejected_with_the_path(name: str, bad: dict, needle: str) -> None:
    problems = schemas.errors(schemas.load(name), bad)
    assert problems, f"{name} accepted {bad!r}"
    assert any(needle in p for p in problems), problems
    with pytest.raises(ModelInvalid, match=name):
        schemas.validate(name, bad)
