"""Telling Tiffin not to give you a food, and being believed.

the partner sent `/cook avoid chickpea` at 16:19 on a Sunday. Nothing was written down, and her menu
had asked three separate times across three days, in three different ways.

The reason is the shape worth remembering: the note reached the MODEL as a line of prose while
the catalogue still handed it chickpeas as a numbered index to pick from. The whole design of
this system is that the model chooses from a list built in code - "index-not-quote" - so the way
to honour "no chickpeas" is to take chickpeas off the list, not to ask nicely alongside it.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from tiffin.db import Db
from tiffin.food import catalogue

from .test_food_shopping import seed_ingredient

TODAY = dt.date(2026, 9, 13)


@pytest.mark.parametrize(
    ("said", "expect"),
    [
        ("no chickpeas", ["chickpeas"]),
        ("avoid chickpea", ["chickpea"]),
        ("replace no chickpeas", ["chickpeas"]),  # the /cook replace prefix is already stripped
        ("Avoid chickpeas pls", ["chickpeas"]),
        ("no rice or chickpeas", ["rice", "chickpeas"]),
        ("no beef, no pork", ["beef", "pork"]),
        ("skip the liver, please", ["liver"]),
        ("without mushrooms", ["mushrooms"]),
        ("avoid all mushrooms", ["mushrooms"]),
        ("no chickpeas and more fish", ["chickpeas"]),
    ],
)
def test_the_ways_people_say_keep_that_out(said: str, expect: list[str]) -> None:
    assert catalogue.avoid_terms(said) == expect


@pytest.mark.parametrize(
    "said",
    ["more fish", "lighter dinners", "replace", "no", "something warmer", "more veg please"],
)
def test_a_note_about_shape_is_not_a_refusal(said: str) -> None:
    """ "More fish" asks for a different menu, not a smaller catalogue. Reading it as a refusal
    would quietly delete foods nobody objected to, which is worse than ignoring the note."""
    assert catalogue.avoid_terms(said) == []


def _catalogue(db: Db) -> None:
    seed_ingredient(db, "chickpeas", "Chickpeas, tinned, drained", "tins", 400.0, "400 g tin")
    seed_ingredient(db, "chicken_breast", "Chicken breast, raw", "meat", 500.0, "500 g pack")
    seed_ingredient(db, "rice_basmati", "Basmati rice, dry", "grains", 1000.0, "1 kg bag")
    seed_ingredient(db, "feta", "Feta cheese", "dairy", 200.0, "200 g")


def test_a_refused_food_leaves_the_catalogue_the_model_picks_from(
    fixture_db: Db, two_persons
) -> None:
    """The guarantee. A food on the refused list is not in `approved`, so it is not in the
    numbered list the model is given, so it cannot come back however the prose reads."""
    _catalogue(fixture_db)
    pid = two_persons["partner"]
    fixture_db.insert(
        "profiles",
        person_id=pid,
        effective_from="2026-09-01",
        sex="f",
        birth_date="1992-05-02",
        height_cm=168.0,
        activity_factor=1.4,
        kcal_floor=1400,
        refused_foods=json.dumps(["Liver", "broccoli"]),
    )
    before = {i.code for i in catalogue.approved(fixture_db, pid)}
    assert "chickpeas" in before, "this is the state she was actually in"

    # what `_refuse_from_note` writes, in the same shape: a new profiles row, terms appended
    current = catalogue.refused_terms(fixture_db, pid)
    row = fixture_db.one(
        "SELECT * FROM profiles WHERE person_id = ? ORDER BY effective_from DESC, id DESC LIMIT 1",
        (pid,),
    )
    assert row is not None
    fields = {k: row[k] for k in row.keys() if k not in ("id", "created_at")}  # noqa: SIM118
    fields["effective_from"] = "2026-09-13"
    fields["refused_foods"] = json.dumps([*current, *catalogue.avoid_terms("avoid chickpea")])
    fixture_db.insert("profiles", **fields)

    after = {i.code for i in catalogue.approved(fixture_db, pid)}
    assert "chickpeas" not in after, "off the list the model is handed, not merely discouraged"
    assert before - after == {"chickpeas"}, "and nothing else went with it"
    assert catalogue.refused_terms(fixture_db, pid) == ["liver", "broccoli", "chickpea"], (
        "the earlier refusals survive: profiles is a ledger and this is a new row"
    )


def test_a_term_that_would_empty_the_catalogue_is_refused(fixture_db: Db, two_persons) -> None:
    """A note is loose text. "no food" or a term matching everything must not leave somebody
    with nothing to eat, so a term is only taken when it hits something AND leaves something."""
    _catalogue(fixture_db)
    pid = two_persons["partner"]
    have = catalogue.approved(fixture_db, pid)
    for term in catalogue.avoid_terms("no raw"):  # "raw" is in three of the four names
        hit = [i for i in have if catalogue.refused_matches(term, i)]
        assert not (hit and len(hit) >= len(have)), f"{term} would empty the catalogue"


def test_a_term_matching_nothing_is_not_written(fixture_db: Db, two_persons) -> None:
    """ "No faff" names no food. Writing it would leave a refusal nobody can explain later."""
    _catalogue(fixture_db)
    pid = two_persons["partner"]
    have = catalogue.approved(fixture_db, pid)
    for term in catalogue.avoid_terms("no faff"):
        assert not [i for i in have if catalogue.refused_matches(term, i)]
