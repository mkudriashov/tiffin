"""What a person wants from every menu, kept, and read by every build.

ideally), canned tuna, not just feta cheese ... focus on cooked warm vegetables - if raw vegetable
only 1-2 types per dish (so no salads), chia puddings (ideally), fruits and berries, less avocado,
no broccoli, no liver, no chickpeas, no lentils, no beans, sweet potato is welcome, less rice,
simple recipes". A food to keep off is the avoid list and a food to have more is the likes list;
"less rice" and "no salads" are neither, and the one place that could hold them - the standing
notes Tiffin keeps - was read by no menu build. Now every build reads them, and `tiffin prefs`
writes all three on her word from the shell.
"""

from __future__ import annotations

import datetime as dt
import json
import re

import pytest

from tiffin import cli
from tiffin.db import Db
from tiffin.food import catalogue, newdish, prefs
from tiffin.jobs import menu_propose
from tiffin.model import prompts

from .test_cli import env  # noqa: F401
from .test_fixes_e2 import _shared_app
from .test_food_shopping import seed_ingredient
from .test_menu import FakeGateway, world  # noqa: F401
from .test_model_builds import OneDishModel, _App, _consent
from .test_review_0926_weekend import HIS, PARTNERS, _agree, _weekend_payload, pair  # noqa: F401
from .test_swap import MON, _ing, week  # noqa: F401

NOTES = ("less rice", "cooked warm vegetables, raw ones only 1-2 kinds per dish, so no salads")


def _notes_line(prompt: str) -> str:
    m = re.search(r"^What (?:they|either of them) wants? from every menu[^:]*: (.*)$", prompt, re.M)
    assert m, "the prompt has no standing-notes line"
    return m.group(1)


# ---------------------------------------------------------------- the notes themselves
def test_notes_are_a_ledger_added_taken_off_and_back(world) -> None:  # noqa: F811
    pid, db, *_ = world
    clock = db.clock
    got = prefs.amend_notes(db, pid, add=NOTES, source="operator", clock=clock)
    assert got.added == NOTES and got.notes == NOTES
    again = prefs.amend_notes(db, pid, add=["Less rice."], source="operator", clock=clock)
    assert again.already == ("Less rice.",) and not again.added, "the same note, said again"
    off = prefs.amend_notes(db, pid, drop=["LESS RICE"], source="operator", clock=clock)
    assert off.dropped == ("less rice",)
    assert prefs.standing_notes(db, pid) == [NOTES[1]]
    gone = prefs.amend_notes(db, pid, drop=["more rice"], source="operator", clock=clock)
    assert gone.missing == ("more rice",)
    back = prefs.amend_notes(db, pid, add=["less rice"], source="operator", clock=clock)
    assert back.added == ("less rice",)
    assert set(prefs.standing_notes(db, pid)) == set(NOTES)
    # append-only: taking a note off and putting it back are rows of their own
    rows = db.scalar(
        "SELECT COUNT(*) FROM person_notes WHERE person_id = ? AND text = 'less rice'", (pid,)
    )
    assert rows == 3
    with pytest.raises(ValueError, match="source"):
        prefs.amend_notes(db, pid, add=["x"], source="somebody", clock=clock)


def test_no_notes_reads_as_none() -> None:
    text = prompts.menu_one_dish(ingredients=["1. Chicken"], meal="Lunch", target="", going="X")
    assert _notes_line(text) == "(none)"
    whole = prompts.notes_line(["raw ones only 1-2 kinds per dish, so no salads", "less rice"])
    assert whole == "raw ones only 1-2 kinds per dish, so no salads; less rice", (
        "a note keeps its own commas, and is never cut where `listing` would cut it"
    )


# ---------------------------------------------------------------- every build reads them
def test_every_segment_build_reads_the_standing_notes(world) -> None:  # noqa: F811
    pid, db, _tg, gateways, _say, _tap, run_job = world
    prefs.amend_notes(db, pid, add=NOTES, source="operator", clock=db.clock)
    gateways.append(FakeGateway([]))
    run_job(menu_propose.JOB, dt.date(2026, 9, 12))
    assert gateways[0].calls, "the build asked the model"
    line = _notes_line(gateways[0].calls[0])
    assert "less rice" in line and "so no salads" in line, line


def test_the_weekend_cooked_together_reads_both_peoples_notes(pair, config, tmp_path) -> None:  # noqa: F811
    from tiffin.jobs import menu_shared

    db, (him, partner), clock = pair
    _agree(db, him.id)
    _agree(db, partner.id)
    for p, m in ((him, HIS), (partner, PARTNERS)):
        db.insert(
            "targets", person_id=p.id, week_start="2026-09-14", kcal=m.kcal,
            protein_g=m.protein_g, fat_g=m.fat_g, carb_g=m.carb_g, fibre_g=m.fibre_g,
            source="formula",
        )  # fmt: skip
    weekdays = "simple recipes on weekdays: fast to cook"
    prefs.amend_notes(db, partner.id, add=[*NOTES, weekdays], source="operator", clock=clock)
    prefs.amend_notes(db, him.id, add=["more fish"], source="operator", clock=clock)
    gw = FakeGateway([_weekend_payload() for _ in range(8)])
    menu_shared.build_now(_shared_app(db, clock, config, tmp_path, gw), db, clock=clock)
    line = _notes_line(gw.calls[0])
    assert "so no salads" in line and "more fish" in line, "both eat every dish: " + line
    assert weekdays not in line, "the note applies to weekend meals only"


def test_a_new_dish_for_one_meal_reads_the_standing_notes(week) -> None:  # noqa: F811
    db, pid, _base, _r, clock = week
    _consent(db, pid)
    prefs.amend_notes(db, pid, add=NOTES, source="operator", clock=clock)
    model = OneDishModel()
    newdish.ask(_App(model), db, pid, MON, "lunch", clock=clock)
    assert "less rice" in _notes_line(model.prompts[0])


# ---------------------------------------------------------------- "no beans"
@pytest.mark.parametrize(
    ("name", "hit"),
    [
        ("Green beans, raw", False),
        ("Runner beans", False),
        ("Coffee beans", False),
        ("Kidney beans, tinned, drained", True),
        ("Baked beans in tomato sauce", True),
        ("Edamame beans, frozen", True),
        ("Butter beans, tinned", True),
        ("Mixed beans in water", True),
    ],
)
def test_no_beans_is_the_pulses_not_the_green_ones(name: str, hit: bool) -> None:
    """She asked for cooked warm vegetables in the same breath as "no beans": the plain word,
    matched as a whole word, kept green beans off with the kidney beans."""
    assert catalogue.refused_matches(catalogue.normalise("beans"), _ing(name, "tins")) is hit


# ---------------------------------------------------------------- `tiffin prefs`
def test_tiffin_prefs_writes_partner_lists_and_notes_from_the_shell(env, capsys) -> None:  # noqa: F811
    config, _secrets, clock = env
    db = Db.open(config.paths.db, clock=clock)
    try:
        partner = int(db.scalar("SELECT id FROM persons WHERE slug = 'partner'"))
        db.insert(
            "profiles", person_id=partner, effective_from="2026-09-01", sex="f",
            birth_date="1994-05-01", height_cm=165.0, activity_factor=1.4, kcal_floor=1400,
            liked_foods='["Eggs", "avocado"]',
        )  # fmt: skip
        for code, name, aisle in (
            ("green_beans", "Green beans, raw", "produce"),
            ("kidney_beans", "Kidney beans, tinned, drained", "tins"),
            ("lentils_red", "Red lentils, dry", "grains"),
            ("avocado", "Avocado, Hass", "produce"),
            ("sweet_potato", "Sweet potato, raw", "produce"),
        ):
            seed_ingredient(db, code, name, aisle, 400.0, "400 g")
    finally:
        db.close()
    argv = [
        "prefs", "partner", "--avoid", "lentils, beans", "--unlike", "avocado",
        "--like", "sweet potato", "--note", "less rice", "--note", "simple recipes",
    ]  # fmt: skip
    assert cli.main(argv) == 0
    out = capsys.readouterr().out
    assert "beans (Kidney beans)" in out, out
    assert "notes added: less rice; simple recipes" in out, out
    db = Db.open(config.paths.db, clock=clock)
    try:
        offered = {i.code for i in catalogue.approved(db, partner)}
        assert "green_beans" in offered, "a cooked vegetable she asked for more of"
        assert not {"kidney_beans", "lentils_red"} & offered
        likes = [t.casefold() for t in catalogue.liked_terms(db, partner)]
        assert "sweet potato" in likes and "avocado" not in likes
        assert prefs.standing_notes(db, partner) == ["less rice", "simple recipes"]
        assert db.scalar(
            "SELECT DISTINCT source FROM person_notes WHERE person_id = ?", (partner,)
        ) == ("operator")
        rows = (
            db.scalar("SELECT COUNT(*) FROM profiles WHERE person_id = ?", (partner,)),
            db.scalar("SELECT COUNT(*) FROM person_notes WHERE person_id = ?", (partner,)),
        )
    finally:
        db.close()
    # said twice, nothing new is written
    assert cli.main(argv) == 0
    out = capsys.readouterr().out
    assert "already standing: less rice; simple recipes" in out, out
    db = Db.open(config.paths.db, clock=clock)
    try:
        assert rows == (
            db.scalar("SELECT COUNT(*) FROM profiles WHERE person_id = ?", (partner,)),
            db.scalar("SELECT COUNT(*) FROM person_notes WHERE person_id = ?", (partner,)),
        )
    finally:
        db.close()


def test_tiffin_prefs_fine_answers_the_made_from_question_for_good(env, capsys) -> None:  # noqa: F811
    config, _secrets, clock = env
    db = Db.open(config.paths.db, clock=clock)
    try:
        partner = int(db.scalar("SELECT id FROM persons WHERE slug = 'partner'"))
        db.insert(
            "profiles", person_id=partner, effective_from="2026-09-01", sex="f",
            birth_date="1994-05-01", height_cm=165.0, activity_factor=1.4, kcal_floor=1400,
            refused_foods='["chickpea"]',
        )  # fmt: skip
        seed_ingredient(db, "chickpeas", "Chickpeas, tinned, drained", "tins", 400.0, "400 g")
        seed_ingredient(db, "houmous", "Houmous", "condiments", 200.0, "200 g")
        assert [a["name"] for a in prefs.asks(db, partner, clock.today_local())] == ["Houmous"]
    finally:
        db.close()
    assert cli.main(["prefs", "partner"]) == 0
    assert "still to ask them: Houmous is made from" in capsys.readouterr().out
    assert cli.main(["prefs", "partner", "--fine", "houmous"]) == 0
    out = capsys.readouterr().out
    assert "fine: Houmous stays on your menus." in out, out
    assert "still to ask them" not in out
    db = Db.open(config.paths.db, clock=clock)
    try:
        assert prefs.asks(db, partner, clock.today_local()) == []
        offered = {i.code for i in catalogue.approved(db, partner)}
        assert "houmous" in offered and "chickpeas" not in offered
    finally:
        db.close()


def test_tiffin_set_changes_a_setting_the_way_the_bot_does(env, capsys) -> None:  # noqa: F811
    """
    one a day, a new profiles row from today, every other answer carried over."""
    config, _secrets, clock = env
    db = Db.open(config.paths.db, clock=clock)
    try:
        partner = int(db.scalar("SELECT id FROM persons WHERE slug = 'partner'"))
        db.insert(
            "profiles", person_id=partner, effective_from="2026-09-01", sex="f",
            birth_date="1994-05-01", height_cm=165.0, activity_factor=1.4, kcal_floor=1400,
            segment_days="[[0, 1, 2], [3, 4]]", refused_foods='["broccoli"]',
        )  # fmt: skip
    finally:
        db.close()
    assert cli.main(["set", "partner", "cook-days", "daily"]) == 0
    assert "cook-days" in capsys.readouterr().out
    db = Db.open(config.paths.db, clock=clock)
    try:
        rows = db.all("SELECT * FROM profiles WHERE person_id = ? ORDER BY id", (partner,))
        assert len(rows) == 2, "a new row, never an edit"
        assert json.loads(rows[-1]["segment_days"]) == [[0], [1], [2], [3], [4]]
        assert rows[-1]["refused_foods"] == '["broccoli"]', "the rest carried over"
    finally:
        db.close()
    assert cli.main(["set", "partner", "cook-days", "fortnightly"]) == 2, (
        "the interview's own check"
    )
    assert cli.main(["set", "partner", "avoid", "olives"]) == 2, "a list is `tiffin prefs`"
    assert cli.main(["set", "partner", "shoe-size", "5"]) == 2
