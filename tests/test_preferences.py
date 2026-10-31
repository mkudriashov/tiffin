"""What a person refuses stays refused, wherever a dish is placed.

that reached a plate (the partner's lentils, 7 Oct) came in by a swap - since closed: a swap
takes only a
dish today's offers hold. What was still open is the week chosen on Sunday: its options were built
in the morning, and a food refused that afternoon went onto the week's menus at 20:00. And nothing
watched for a refused food arriving on a menu at all.

The world is the swap tests' week (tests/test_swap.py): her lentil dal for next week's lunches.
"""

from __future__ import annotations

import datetime as dt
import logging

import pytest

from tiffin.clock import isoday
from tiffin.errors import NothingToBuild
from tiffin.food import choices, menu, prefs

from .test_batch import _profile as _batch_profile
from .test_batch import bot  # noqa: F401
from .test_choices import _App, _built
from .test_swap import MON, _headers, _menu, page, week  # noqa: F401
from .web_source import app_source


def _dal(db) -> int:
    return int(db.scalar("SELECT id FROM recipes WHERE name = 'Lentil dal'"))


def _refuse(db, pid, clock, food: str) -> None:
    changed = prefs.amend(db, pid, "avoid", add=[food], clock=clock)
    assert food in [t.casefold() for t in changed.terms], changed


def _row(db, menu_id: int):
    return db.one("SELECT * FROM menus WHERE id = ?", (menu_id,))


# ---------------------------------------------------------------- the watch on every menu
def test_a_menu_built_after_a_refusal_that_holds_the_food_is_caught(week, caplog) -> None:  # noqa: F811
    db, pid, _base, _r, clock = week
    _refuse(db, pid, clock, "lentils")
    later = _menu(db, pid, MON + dt.timedelta(days=7), 2, {"lunch": _dal(db)})
    assert menu.refused_brought_in(db, _row(db, later)) == ["Red lentils"]
    with caplog.at_level(logging.WARNING, logger="tiffin.menu"):
        menu.decide(db, later, "accepted")
    hits = [r for r in caplog.records if getattr(r, "event", "") == "refused_on_menu"]
    assert len(hits) == 1 and hits[0].foods == 1, "logged by count"
    assert "lentil" not in hits[0].getMessage().lower(), "never the food's name in a log"


def test_a_plate_changed_on_a_menu_built_before_the_refusal_carries_it_unflagged(week) -> None:  # noqa: F811
    db, pid, _base, _r, clock = week
    first = int(db.scalar("SELECT MAX(id) FROM menus WHERE person_id = ?", (pid,)))
    _refuse(db, pid, clock, "lentils")
    # a resize or a swap on Tuesday: a menu over the same days, made from the one in force
    resized = db.insert(
        "menus",
        person_id=pid,
        segment_start=isoday(MON + dt.timedelta(days=1)),
        segment_end=isoday(MON + dt.timedelta(days=1)),
        status="proposed",
        source_menu_id=first,
    )
    assert menu.refused_brought_in(db, _row(db, resized)) == [], "nothing newly on a plate"


def test_a_repeat_of_an_older_week_onto_new_days_is_held_to_everything(week) -> None:  # noqa: F811
    db, pid, _base, _r, clock = week
    old = _menu(db, pid, MON - dt.timedelta(days=21), 2, {"lunch": _dal(db)})
    _refuse(db, pid, clock, "lentils")
    repeat = _menu(db, pid, MON + dt.timedelta(days=14), 2, {"lunch": _dal(db)})
    # the menus ledger forbids updates: a repeat is written with its source from the start
    again = db.insert(
        "menus",
        person_id=pid,
        segment_start=isoday(MON + dt.timedelta(days=14)),
        segment_end=isoday(MON + dt.timedelta(days=15)),
        status="repeated",
        source_menu_id=old,
    )
    for d in db.all("SELECT * FROM menu_days WHERE menu_id = ?", (repeat,)):
        day_id = db.insert(
            "menu_days",
            menu_id=again,
            day=str(d["day"]),
            kcal=d["kcal"],
            protein_g=d["protein_g"],
            fat_g=d["fat_g"],
            carb_g=d["carb_g"],
            fibre_g=d["fibre_g"],
        )
        for s in db.all("SELECT * FROM meal_slots WHERE menu_day_id = ?", (int(d["id"]),)):
            db.insert(
                "meal_slots",
                menu_day_id=day_id,
                slot=s["slot"],
                recipe_id=s["recipe_id"],
                portion_multiplier=s["portion_multiplier"],
                portion_g=s["portion_g"],
                kcal=s["kcal"],
                protein_g=s["protein_g"],
                fat_g=s["fat_g"],
                carb_g=s["carb_g"],
                fibre_g=s["fibre_g"],
            )
    assert menu.refused_brought_in(db, _row(db, again)) == ["Red lentils"], "new days, new plates"


# ---------------------------------------------------------------- the week chosen on Sunday
def test_a_food_refused_after_sundays_build_never_reaches_the_chosen_week(week, config) -> None:  # noqa: F811
    db, pid, built, _model = _built(week, config)
    assert built.set_id is not None
    clock = db.clock
    options = choices.options_of(db, built.set_id)
    prawns = [o for o in options if o["name"] == "Prawn rice bowl"]
    assert prawns, "setup: the bowl she rated 5 is offered"
    # the afternoon after the build: no more prawns
    _refuse(db, pid, clock, "prawns")
    avoided = choices.avoided_options(db, pid, options)
    assert {int(o["id"]) for o in prawns} <= set(avoided)
    with pytest.raises(NothingToBuild, match="avoid now"):
        choices.pick(db, pid, built.set_id, int(prawns[0]["id"]), clock=clock)
    # the page shows it, greyed, saying why, and never as the one in force
    slots = ["breakfast", "lunch", "dinner"]
    day = str(prawns[0]["day"])
    view = choices.day_view(db, day, slots, options, {}, None, 20, avoided=avoided)
    lunch = next(s for s in view["slots"] if s["slot"] == str(prawns[0]["slot"]))
    shown = {o["id"]: o for o in lunch["options"]}
    assert shown[int(prawns[0]["id"])]["avoided"] == ["King prawns"]
    assert lunch["picked"] != int(prawns[0]["id"])
    # and the week set at 20:00 holds no prawns on any plate
    settled = choices.settle(_App(None, config), db, pid, built.set_id, how="closed", clock=clock)
    assert settled.menu_ids
    assert not [
        n for mid in settled.menu_ids for n in menu.refused_brought_in(db, _row(db, mid))
    ], "no refused food newly on a plate"


# ---------------------------------------------------------------- release 2: all of it, in view
def test_the_page_shows_everything_the_planner_reads(week) -> None:  # noqa: F811
    db, pid, _base, _r, clock = week
    prefs.amend_notes(
        db, pid, add=["less rice", "simple recipes on weekdays"], source="operator", clock=clock
    )
    got = prefs.view(db, pid, clock.today_local())
    assert got["notes"] == [
        {"text": "less rice", "half": "all"},
        {"text": "simple recipes on weekdays", "half": "weekdays"},
    ]
    for key in ("favourites", "never", "resting", "kit", "conflicts"):
        assert key in got, key
    # her stir-fry rated 1 (the swap tests' history) is never offered, and the page says so
    assert "Chicken stir-fry" in got["never"]


def test_a_wish_another_keeps_from_working_is_said_not_dropped(week) -> None:  # noqa: F811
    db, pid, _base, _r, clock = week
    prefs.amend(db, pid, "likes", add=["lentils"], clock=clock, strict=False)
    prefs.amend_notes(db, pid, add=["more lentils please"], source="chat", clock=clock)
    _refuse(db, pid, clock, "lentils")
    said = prefs.conflicts(db, pid, clock.today_local())
    assert any(s.startswith("You like lentils") for s in said), said
    assert any('"more lentils please" names lentils' in s for s in said), said


def test_notes_are_theirs_to_add_and_take_off_on_the_page(page) -> None:  # noqa: F811
    client, _db, _partners, _his, _r = page
    h = _headers(client)
    r = client.post("/api/prefs/notes", json={"add": "less rice"}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["said"].startswith('Kept: "less rice"')
    assert {"text": "less rice", "half": "all"} in r.json()["prefs"]["notes"]
    r = client.post("/api/prefs/notes", json={"remove": "less rice"}, headers=h)
    assert r.json()["said"].startswith('Taken off: "less rice"')
    assert r.json()["prefs"]["notes"] == []
    for bad in ({}, {"add": ""}, {"add": 3}, {"person": "owner", "add": "x"}):
        assert client.post("/api/prefs/notes", json=bad, headers=h).status_code == 400, bad


def test_notes_in_the_bot_listed_added_and_taken_off_by_number(bot) -> None:  # noqa: F811
    db, pid, say = bot
    _batch_profile(db, pid)
    assert "No notes yet" in say("/notes")
    assert 'Kept: "less rice"' in say("/notes + less rice")
    assert 'Kept: "no salads on weekdays"' in say("/notes no salads on weekdays")
    listed = say("/notes")
    assert "1. less rice" in listed and "2. no salads on weekdays (weekdays)" in listed
    assert 'Taken off: "less rice"' in say("/notes - 1")
    assert prefs.standing_notes(db, pid) == ["no salads on weekdays"]


# ---------------------------------------------------------------- release 3: the word on a dish
def test_a_word_on_a_dish_is_said_back_and_taken_back_exactly(page) -> None:  # noqa: F811
    client, db, partners, _his, _r = page
    h = _headers(client)
    dal = _dal(db)
    today = client.get(f"/api/today?date={isoday(MON)}", headers=h).json()
    lunch = next(m for m in today["meals"] if m["slot"] == "lunch")
    assert lunch["recipe_id"] == dal and lunch["score"] is None, "never rated: no lean"
    got = client.post("/api/dishes/rate", json={"recipe_id": dal, "score": 1}, headers=h).json()
    assert got["said"] == "Lentil dal will not be offered again." and got["previous"] is None
    assert "Lentil dal" in menu.rated_names(db, partners)[1], "off every menu from now"
    # Undo: back to no lean either way
    back = client.post("/api/dishes/rate", json={"recipe_id": dal, "score": 3}, headers=h).json()
    assert back["previous"] == 1
    assert "Lentil dal" not in menu.rated_names(db, partners)[1]
    more = client.post("/api/dishes/rate", json={"recipe_id": dal, "score": 5}, headers=h).json()
    assert more["said"] == "Lentil dal will be asked for more often."
    today = client.get(f"/api/today?date={isoday(MON)}", headers=h).json()
    assert next(m for m in today["meals"] if m["slot"] == "lunch")["score"] == 5


def test_today_offers_the_word_in_each_meal() -> None:

    js = app_source()
    assert "openDetail.append(dishWord(m));" in js
    assert '[[1, "Not again"], [5, "More like this"]]' in js
    assert "rate(before ?? 3, undo, true)" in js, "undone to exactly what it was"
