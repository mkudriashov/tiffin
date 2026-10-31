"""An edge-case sweep: fifty-odd user stories put through the real handler stack.

Each test is one thing a person can do that is slightly off the happy path - a command with
no argument, a number that cannot be true, the same button twice, someone else's id, a name
with a tag in it. The rule they are all testing is the house one: degrade to manual, never to
silence, and never to a crash.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import sqlite3

import pytest

from tiffin import body
from tiffin.db import Db
from tiffin.food import menu

from .test_menu import MONDAY, FakeGateway, proposal_for, world  # noqa: F401

SATURDAY = dt.date(2026, 9, 12)
SUNDAY = dt.date(2026, 9, 13)


def a_menu(db: Db, person_id: int, start: dt.date, end: dt.date, *, status: str) -> int:
    mid = db.insert(
        "menus",
        person_id=person_id,
        segment_start=start.isoformat(),
        segment_end=end.isoformat(),
        status=status,
    )
    day = db.insert(
        "menu_days",
        menu_id=mid,
        day=start.isoformat(),
        kcal=2000,
        protein_g=180,
        fat_g=70,
        carb_g=200,
        fibre_g=30,
    )
    recipe = db.insert(
        "recipes", name=f"Dish {mid}", prep_minutes=20, method_json="[]", source="model"
    )
    db.insert(
        "meal_slots",
        menu_day_id=day,
        slot="dinner",
        recipe_id=recipe,
        portion_multiplier=1.0,
        portion_g=300,
        kcal=500,
        protein_g=45,
        fat_g=18,
        carb_g=50,
        fibre_g=8,
    )
    return mid


def quiet_tap(tg, tap, data: str) -> None:
    """A tap that is meant to say nothing: someone else's id, or an id that is not a number.
    `tap` reads the last sent message, so it raises on the silence that is the right answer."""
    before = len(tg.sent)
    with contextlib.suppress(IndexError):
        tap(data)
    assert len(tg.sent) == before, f"{data} should not have answered"


def answers(out: str) -> bool:
    """Something was said, and it is not a stack trace or an empty shrug."""
    return bool(out and out.strip()) and "Traceback" not in out


# -- commands with the argument missing, wrong, or hostile ---------------------------------


def test_03_measure_with_one_number_records_the_waist(world) -> None:  # noqa: F811
    pid, db, _tg, _gw, say, _tap, _run = world
    say("/measure 84")
    assert body.latest_tape(db, pid, "waist") is not None


def test_04_measure_with_an_impossible_number_is_refused_out_loud(world) -> None:  # noqa: F811
    pid, db, _tg, _gw, say, _tap, _run = world
    out = say("/measure 4")
    assert answers(out)
    assert body.latest_tape(db, pid, "waist") is None, "4 cm is not a waist"


def test_05_measure_with_words_instead_of_numbers(world) -> None:  # noqa: F811
    _pid, _db, _tg, _gw, say, _tap, _run = world
    assert answers(say("/measure quite big"))


def test_06_rate_a_score_outside_one_to_five(world) -> None:  # noqa: F811
    _pid, _db, _tg, _gw, say, _tap, _run = world
    assert answers(say("/rate 9"))
    assert answers(say("/rate 0"))


def test_07_rate_with_no_menu_to_rate(world) -> None:  # noqa: F811
    _pid, _db, _tg, _gw, say, _tap, _run = world
    assert answers(say("/rate"))


def test_10_pause_with_nonsense_duration(world) -> None:  # noqa: F811
    _pid, _db, _tg, _gw, say, _tap, _run = world
    assert answers(say("/pause soon"))


def test_11_pause_for_zero_days(world) -> None:  # noqa: F811
    _pid, _db, _tg, _gw, say, _tap, _run = world
    assert answers(say("/pause 0d"))


def test_12_resume_when_nothing_is_paused(world) -> None:  # noqa: F811
    _pid, _db, _tg, _gw, say, _tap, _run = world
    assert answers(say("/resume"))


def test_13_week_with_an_unparseable_session(world) -> None:  # noqa: F811
    _pid, _db, _tg, _gw, say, _tap, _run = world
    assert answers(say("/week Mon climb ages"))


def test_14_week_with_a_day_that_is_not_a_day(world) -> None:  # noqa: F811
    _pid, _db, _tg, _gw, say, _tap, _run = world
    assert answers(say("/week Blursday ride 60"))


def test_15_shop_for_a_food_that_is_not_in_the_catalogue(world) -> None:  # noqa: F811
    _pid, _db, _tg, _gw, say, _tap, _run = world
    assert answers(say("/shop unicorn steak"))


def test_16_recipe_when_no_menu_covers_today(world) -> None:  # noqa: F811
    _pid, _db, _tg, _gw, say, _tap, _run = world
    assert answers(say("/recipe"))


def test_17_set_an_unknown_field(world) -> None:  # noqa: F811
    _pid, _db, _tg, _gw, say, _tap, _run = world
    assert answers(say("/set favourite colour blue"))


def test_18_set_avoid_remove_something_not_on_the_list(world) -> None:  # noqa: F811
    _pid, _db, _tg, _gw, say, _tap, _run = world
    assert answers(say("/set avoid - marzipan"))


def test_19_an_unknown_command_does_not_pass_silently(world) -> None:  # noqa: F811
    _pid, _db, _tg, _gw, say, _tap, _run = world
    assert answers(say("/wibble"))


def test_20_name_with_html_in_it_is_escaped_not_rendered(world) -> None:  # noqa: F811
    _pid, _db, _tg, _gw, say, _tap, _run = world
    out = say("/name <b>Alex</b>")
    assert "<b>Alex</b>" not in out, "a name is text, not markup"


def test_21_name_with_nothing_after_it(world) -> None:  # noqa: F811
    _pid, _db, _tg, _gw, say, _tap, _run = world
    assert answers(say("/name"))


def test_22_name_that_is_far_too_long(world) -> None:  # noqa: F811
    _pid, _db, _tg, _gw, say, _tap, _run = world
    assert answers(say("/name " + "Alex" * 200))


# -- the same button twice, and other people's buttons -------------------------------------


def test_23_an_old_accept_tap_says_the_same_thing_twice(world) -> None:  # noqa: F811
    pid, db, _tg, _gw, _say, tap, _run = world
    mid = a_menu(db, pid, MONDAY, dt.date(2026, 9, 18), status="proposed")
    menu.decide(db, mid, "accepted")  # the auto-decision every menu now arrives with
    first = tap(f"k:menu:{mid}")
    second = tap(f"k:menu:{mid}")
    assert "in force" in first.lower() and "in force" in second.lower(), (first, second)
    decisions = db.all("SELECT decision FROM menu_decisions WHERE menu_id = ?", (mid,))
    assert len(decisions) == 1, "the tap never adds a second decision"


def test_24_change_still_works_on_a_menu_that_arrived_already_accepted(world) -> None:  # noqa: F811
    pid, db, _tg, _gw, _say, tap, _run = world
    mid = a_menu(db, pid, MONDAY, dt.date(2026, 9, 18), status="proposed")
    menu.decide(db, mid, "accepted")  # the auto-decision every menu now arrives with
    out = tap(f"k:menux:{mid}")
    assert "what to change" in out.lower(), out
    # the tap opens the change and leaves the menu in force until a replacement exists: a
    # 'rejected' row written here is how skip and a failed rebuild left people with no menu at
    # all
    assert menu.decision_of(db, mid) == "accepted", "Change never rejects before a replacement"


def test_25_partner_cannot_decide_their_menu(world, two_persons) -> None:  # noqa: F811
    _pid, db, tg, _gw, _say, tap, _run = world
    mid = a_menu(db, two_persons["partner"], MONDAY, dt.date(2026, 9, 18), status="proposed")
    quiet_tap(tg, tap, f"k:menu:{mid}")  # sent from HIS chat
    assert db.all("SELECT 1 FROM menu_decisions WHERE menu_id = ?", (mid,)) == [], (
        "a callback carrying someone else's menu id decides nothing"
    )


def test_26_a_callback_with_a_junk_id_is_ignored(world) -> None:  # noqa: F811
    _pid, _db, tg, _gw, _say, tap, _run = world
    quiet_tap(tg, tap, "k:menu:notanumber")
    quiet_tap(tg, tap, "k:menu:999999")


# -- numbers that cannot be true ------------------------------------------------------------


def test_28_a_weight_that_cannot_be_true_is_refused(fixture_db: Db, two_persons) -> None:
    """A weigh-in feeds the trend, the trend feeds the targets and the targets feed every
    menu, so a 0 or a pounds-for-kilos slip would be baked into the plan. The guard is a
    schema CHECK, which is the one layer no caller can forget."""
    pid = two_persons["owner"]
    for bad in (0.0, -5.0, 12.0, 640.0):
        with pytest.raises(sqlite3.IntegrityError):
            body.record_weigh_in(fixture_db, pid, SATURDAY, bad, "manual")
    body.record_weigh_in(fixture_db, pid, SATURDAY, 30.0, "manual")
    body.record_weigh_in(fixture_db, pid, SUNDAY, 250.0, "manual")


def test_29_tape_bounds_hold_at_the_edges(fixture_db: Db, two_persons) -> None:
    pid = two_persons["owner"]
    body.record_tape(fixture_db, pid, SATURDAY, "waist", body.TAPE_CM_MIN)
    body.record_tape(fixture_db, pid, SATURDAY, "waist", body.TAPE_CM_MAX)
    with pytest.raises(ValueError):
        body.record_tape(fixture_db, pid, SATURDAY, "waist", body.TAPE_CM_MAX + 0.1)
    with pytest.raises(ValueError):
        body.record_tape(fixture_db, pid, SATURDAY, "waist", body.TAPE_CM_MIN - 0.1)


def test_30_a_tape_site_that_is_not_a_site(fixture_db: Db, two_persons) -> None:
    with pytest.raises(ValueError):
        body.record_tape(fixture_db, two_persons["owner"], SATURDAY, "elbow", 30.0)
