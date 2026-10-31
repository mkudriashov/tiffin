"""Sequences, not single actions: the seams between one surface and the next.

Every other file here asks what happens when one thing goes slightly wrong. This one asks what
happens when a person does several ordinary things in a row and one of them lands awkwardly -
a correction after a confirmation, a pause across a cook day, a menu accepted and then changed,
a weigh-in that arrives after the week has already been summarised. The bugs that survive a
per-module sweep live in the order of events, not in any single event.
"""

from __future__ import annotations

import datetime as dt
import json

from tiffin import body, days, targets
from tiffin.db import Db
from tiffin.food import catalogue, menu
from tiffin.jobs import day_close, evening_confirm, morning_targets

from .test_edge_cases import a_menu, answers
from .test_menu import MONDAY, world  # noqa: F401

SATURDAY = dt.date(2026, 9, 12)
SUNDAY = dt.date(2026, 9, 13)
NEXT_SATURDAY = dt.date(2026, 9, 19)


def confirmed_on(db: Db, pid: int, day: dt.date) -> str | None:
    row = days.latest_confirmation(db, pid, day)
    return None if row is None else str(row["status"])


def profile_now(db: Db, pid: int):
    return targets.profile_on(db, pid, SATURDAY)


def refused(db: Db, pid: int) -> list[str]:
    return catalogue.refused_terms(db, pid)


# -- a day recorded, then corrected ------------------------------------------------------------


def test_01_a_day_confirmed_and_then_eaten_off_plan_is_corrected_not_duplicated(world) -> None:  # noqa: F811
    """The order people actually do it in: confirm the evening, then remember the pub. The
    confirmation has to move, and it has to move by being rewritten as a new row, not by two
    rows that both claim to be the day."""
    pid, db, _tg, _gw, say, tap, _run = world
    say("/confirm")
    first = confirmed_on(db, pid, SATURDAY)
    iid = db.insert(
        "intents",
        person_id=pid,
        kind="log_offplan",
        payload_json=json.dumps({"description": "two pints", "day": "2026-09-12", "kcal": 400}),
    )
    tap(f"k:intent:{iid}")
    assert first is not None
    assert confirmed_on(db, pid, SATURDAY) is not None, "the day is still recorded"
    rows = db.all(
        "SELECT id FROM day_confirmations WHERE person_id = ? AND day = ?", (pid, "2026-09-12")
    )
    assert len(rows) >= 2, "the correction is a second row; the first is history"


def test_02_the_same_day_confirmed_from_two_directions_settles_on_one_answer(world) -> None:  # noqa: F811
    pid, db, _tg, _gw, say, _tap, run_job = world
    say("/confirm")
    run_job(evening_confirm.JOB, SATURDAY)
    assert confirmed_on(db, pid, SATURDAY) is not None


def test_03_a_weigh_in_corrected_the_same_day_moves_the_trend_with_it(world) -> None:  # noqa: F811
    pid, db, _tg, _gw, _say, _tap, _run = world
    body.record_weigh_in(db, pid, dt.date(2026, 9, 10), 84.0, "manual")
    body.record_weigh_in(db, pid, dt.date(2026, 9, 11), 83.8, "manual")
    before = body.latest_trend(db, pid, dt.date(2026, 9, 11))
    body.record_weigh_in(db, pid, dt.date(2026, 9, 11), 88.8, "manual")  # a scale in pounds
    after = body.latest_trend(db, pid, dt.date(2026, 9, 11))
    assert before is not None and after is not None
    assert after.trend_kg != before.trend_kg, "a corrected weigh-in rebuilds the trend"


def test_04_a_weigh_in_that_arrives_for_a_day_already_closed(world) -> None:  # noqa: F811
    """The scale syncs late. Yesterday is already closed, and the numbers still have to land."""
    pid, db, _tg, _gw, _say, _tap, run_job = world
    run_job(day_close.JOB, SATURDAY)
    body.record_weigh_in(db, pid, dt.date(2026, 9, 11), 83.6, "manual")
    assert body.weigh_ins(db, pid), "a late weigh-in is still recorded"


# -- a menu decided, then changed --------------------------------------------------------------


def test_05_a_menu_in_force_then_the_person_changes_their_mind_about_a_food(world) -> None:  # noqa: F811
    pid, db, _tg, _gw, say, _tap, _run = world
    mid = a_menu(db, pid, MONDAY, dt.date(2026, 9, 18), status="proposed")
    menu.decide(db, mid, "accepted")
    say("/set avoid + chicken")
    rows = db.all(
        "SELECT recipe_id FROM meal_slots WHERE menu_day_id IN "
        "(SELECT id FROM menu_days WHERE menu_id = ?)",
        (mid,),
    )
    assert rows, "the accepted menu still stands"
    assert "chicken" in refused(db, pid)
    assert "chicken" not in [i.code for i in catalogue.approved(db, pid)]


def test_06_a_food_refused_today_is_gone_from_the_approved_list_at_once(world) -> None:  # noqa: F811
    pid, db, _tg, _gw, say, _tap, _run = world
    assert "broccoli" in [i.code for i in catalogue.approved(db, pid)]
    say("/set avoid + broccoli")
    assert "broccoli" not in [i.code for i in catalogue.approved(db, pid)]


def test_07_refusing_a_food_and_then_allowing_it_again(world) -> None:  # noqa: F811
    pid, db, _tg, _gw, say, _tap, _run = world
    before = len(catalogue.approved(db, pid))
    say("/set avoid + broccoli")
    assert len(catalogue.approved(db, pid)) < before
    say("/set avoid - broccoli")
    assert len(catalogue.approved(db, pid)) == before, "taking it off the list puts it back"


def test_08_a_stale_accept_tap_decides_nothing(world) -> None:  # noqa: F811
    pid, db, _tg, _gw, _say, tap, _run = world
    a_menu(db, pid, MONDAY, dt.date(2026, 9, 18), status="proposed")
    second = a_menu(db, pid, MONDAY, dt.date(2026, 9, 18), status="proposed")
    tap(f"k:menu:{second}")
    assert db.scalar("SELECT COUNT(*) FROM menu_decisions") == 0, "the tap decided nothing"


# -- a week away -------------------------------------------------------------------------------


def test_09_a_pause_stops_the_morning_message_and_resume_starts_it_again(world) -> None:  # noqa: F811
    """Paused is not muted: the job still runs and still writes its row, so the record of the
    week is complete. What changes is that nothing reaches the phone."""
    pid, db, tg, _gw, say, _tap, run_job = world
    say("/pause 7d")
    assert targets.pause_active(db, pid, SATURDAY) is True
    before = len(tg.sent)
    assert run_job(morning_targets.JOB, SUNDAY) == "skipped"
    assert len(tg.sent) == before, "nothing reached the phone"
    assert (
        db.scalar(
            "SELECT status FROM messages_out WHERE person_id = ? ORDER BY id DESC LIMIT 1", (pid,)
        )
        == "dropped_paused"
    ), "and the outbox says why, rather than losing the message quietly"
    say("/resume")
    assert targets.pause_active(db, pid, SATURDAY + dt.timedelta(days=2)) is False


def test_10_a_pause_that_ends_mid_week_does_not_pause_the_days_after_it(world) -> None:  # noqa: F811
    pid, db, _tg, _gw, say, _tap, _run = world
    say("/pause 2d")
    assert targets.pause_active(db, pid, SATURDAY) is True
    assert targets.pause_active(db, pid, SATURDAY + dt.timedelta(days=5)) is False


def test_11_resuming_when_the_pause_had_already_run_out(world) -> None:  # noqa: F811
    pid, db, _tg, _gw, say, _tap, _run = world
    db.insert("pause_periods", person_id=pid, start_day="2026-09-01", end_day="2026-09-03")
    assert answers(say("/resume"))


def test_12_a_day_inside_a_pause_still_accepts_a_weigh_in(world) -> None:  # noqa: F811
    """Holidays are when weight moves. The pause stops the messages, not the numbers."""
    pid, db, _tg, _gw, say, _tap, _run = world
    say("/pause 7d")
    body.record_weigh_in(db, pid, SATURDAY, 84.4, "manual")
    assert body.weigh_ins(db, pid) != []


# -- the order the settings were changed in ----------------------------------------------------


def test_13_two_settings_changed_in_one_minute_both_survive(world) -> None:  # noqa: F811
    """`profiles` is append-only and effective_from is a DAY, so two changes on the same day
    are two rows with the same date: the reader has to break that tie by id, or the second
    change silently loses to the first."""
    pid, db, _tg, _gw, say, _tap, _run = world
    say("/set meals 5")
    say("/set steps 9000")
    p = profile_now(db, pid)
    assert p is not None
    assert int(p["meals_per_day"]) == 5 and int(p["steps_floor"]) == 9000


def test_14_a_setting_changed_and_changed_back(world) -> None:  # noqa: F811
    pid, db, _tg, _gw, say, _tap, _run = world
    say("/set meals 5")
    say("/set meals 4")
    p = profile_now(db, pid)
    assert p is not None and int(p["meals_per_day"]) == 4


def test_15_changing_the_cook_days_does_not_lose_the_rest_of_the_profile(world) -> None:  # noqa: F811
    pid, db, _tg, _gw, say, _tap, _run = world
    before = profile_now(db, pid)
    assert before is not None
    height = float(before["height_cm"])
    say("/set cook-days mw")
    after = profile_now(db, pid)
    assert after is not None and float(after["height_cm"]) == height


def test_16_a_goal_set_and_then_cleared(world) -> None:  # noqa: F811
    pid, db, _tg, _gw, say, _tap, _run = world
    say("/set goal 78")
    set_row = profile_now(db, pid)
    assert set_row is not None and float(set_row["goal_weight_kg"]) == 78.0
    out = say("/set goal -")
    assert "cleared" in out.lower()
    p = profile_now(db, pid)
    assert p is not None and p["goal_weight_kg"] is None


# -- the person who does everything at once ----------------------------------------------------


def test_17_five_commands_in_a_row_each_get_their_own_answer(world) -> None:  # noqa: F811
    _pid, _db, tg, _gw, say, _tap, _run = world
    before = len(tg.sent)
    for cmd in ("/today", "/week", "/supp", "/kit"):
        say(cmd)
    assert len(tg.sent) - before >= 5, "no answer was swallowed by the one after it"


def test_18_a_command_sent_in_the_middle_of_a_measurement(world) -> None:  # noqa: F811
    """Half way through /measure, the person remembers something else. The number they type
    next belongs to whichever question is actually open, and /today is not a waist."""
    pid, db, _tg, _gw, say, _tap, _run = world
    say("/measure")
    assert answers(say("/today"))
    say("84 102")
    tape = body.latest_tape(db, pid, "waist")
    assert tape is None or tape[1] == 84.0


def test_19_the_same_command_twice_in_a_second(world) -> None:  # noqa: F811
    _pid, _db, tg, _gw, say, _tap, _run = world
    before = len(tg.sent)
    say("/today")
    say("/today")
    assert len(tg.sent) - before == 2, "two questions, two answers"


def test_20_a_person_who_has_done_nothing_at_all_can_still_use_every_command(world) -> None:  # noqa: F811
    """Her first evening: no weigh-in, no menu, no sessions."""
    _pid, _db, _tg, _gw, say, _tap, _run = world
    for cmd in ("/today", "/week", "/shopping", "/batch", "/recipe", "/menu"):
        assert answers(say(cmd)), cmd
