"""Tests for recording reported food without losing or double-counting it."""

from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any

import pytest

from tiffin import days
from tiffin.jobs import day_close, evening_confirm
from tiffin.telegram import convstate
from tiffin.telegram.dayflow import arm_evening

from .test_journey_owner_week import TUE15, taps, week  # noqa: F401 (pytest fixture)

FRIDAY_TEXT = (
    "I didn't have pre-bed and breakfast, had 2 kinder sticks and 2 twix fingers instead, "
    "otherwise on plan"
)


def _slot_kcal(db, pid: int, day: dt.date, slot: str) -> int:
    m = days.planned_slot_macros(db, pid, day, slot)
    assert m is not None, f"no planned {slot} on {day}"
    return m.kcal


def _planned(db, pid: int, day: dt.date) -> int:
    m = days.planned_macros(db, pid, day)
    assert m is not None
    return m.kcal


def _estimates(db, pid: int) -> list[Any]:
    return db.all("SELECT * FROM estimates WHERE person_id = ? ORDER BY id", (pid,))


# ---------------------------------------------------------------- the Friday, replayed
def test_friday_typed_under_the_check_in_takes_both_skipped_meals_off(week) -> None:  # noqa: F811
    msg = week.run(evening_confirm.JOB, TUE15)
    assert msg is not None
    week.at(TUE15, "20:02")
    # typed straight under the check-in, no "Not quite" tap - exactly what he did
    asked = week.say(FRIDAY_TEXT)
    assert "breakfast and pre-bed" in asked["text"], asked["text"]
    buttons = taps(asked)
    assert "k:mslotok:20260915" in buttons and "k:mslot0:20260915" in buttons, buttons
    card = week.tap("k:mslotok:20260915")
    accept = next((d for d in taps(card) if d.startswith("k:intent:")), "")
    assert accept, f"the food card must be shown, with its estimate: {card['text']}"
    assert "650 kcal" in card["text"] and "instead of breakfast and pre-bed" in card["text"]
    said = week.tap(accept)["text"]
    assert not re.search(r"\b0 kcal", said), said
    assert "650 kcal" in said, said
    est = _estimates(week.db, week.his)
    assert len(est) == 1 and float(est[0]["kcal"]) == 650
    assert json.loads(est[0]["replaced_slots_json"]) == ["breakfast", "pre_bed"]
    row = days.latest_confirmation(week.db, week.his, TUE15)
    assert row is not None and row["status"] == "confirmed"
    honest = (
        _planned(week.db, week.his, TUE15)
        - _slot_kcal(week.db, week.his, TUE15, "breakfast")
        - _slot_kcal(week.db, week.his, TUE15, "pre_bed")
        + 650
    )
    assert abs(float(row["kcal"]) - honest) <= 1, (
        f"the plan less both skipped meals plus the food is {honest}; recorded {row['kcal']}"
    )


def test_a_food_card_without_its_estimate_is_estimated_never_stored_as_zero(week) -> None:  # noqa: F811
    """What /proposals re-sent on the Friday: a card holding only a description."""
    week.at(TUE15, "20:04")
    iid = week.db.insert(
        "intents",
        person_id=week.his,
        kind="log_offplan",
        payload_json=json.dumps(
            {"description": "2 Kinder sticks and 2 Twix fingers", "day": "2026-09-15"}
        ),
    )
    out = week.tap(f"k:intent:{iid}")
    assert all(float(e["kcal"]) > 0 for e in _estimates(week.db, week.his)), "never 0 kcal"
    assert "Logged 0 kcal" not in out["text"]
    # it goes on to the same question every food answer gets, and from there to a real number
    assert "k:mslotok:20260915" in taps(out), out["text"]
    card = week.tap("k:mslot0:20260915")
    assert "650 kcal" in card["text"], card["text"]


def test_extra_nothing_replaced_counts_the_food_on_top_of_the_whole_plan(week) -> None:  # noqa: F811
    week.run(evening_confirm.JOB, TUE15)
    week.at(TUE15, "20:10")
    week.tap("k:cfmx:20260915")
    week.say("two pints of beer as well")
    card = week.tap("k:mslot0:20260915")
    accept = next(d for d in taps(card) if d.startswith("k:intent:"))
    week.tap(accept)
    row = days.latest_confirmation(week.db, week.his, TUE15)
    assert row is not None
    assert abs(float(row["kcal"]) - (_planned(week.db, week.his, TUE15) + 650)) <= 1


def test_a_later_as_planned_does_not_count_a_replaced_meal_twice(week) -> None:  # noqa: F811
    """A daytime swap followed by the evening "As planned": plan - lunch + burrito, once."""
    week.at(TUE15, "13:00")
    iid = week.db.insert(
        "intents",
        person_id=week.his,
        kind="log_offplan",
        payload_json=json.dumps(
            {
                "description": "burrito",
                "day": "2026-09-15",
                "kcal": 650,
                "items": [{"n": 1}],
                "replaced_slots": ["lunch"],
                "finalize": False,
            }
        ),
    )
    week.tap(f"k:intent:{iid}")
    week.at(TUE15, "20:05")
    week.tap("k:cfm:20260915")
    row = days.latest_confirmation(week.db, week.his, TUE15)
    assert row is not None
    expected = _planned(week.db, week.his, TUE15) - _slot_kcal(week.db, week.his, TUE15, "lunch")
    assert abs(float(row["kcal"]) - (expected + 650)) <= 1, row["kcal"]


# ---------------------------------------------------------------- the question itself
def test_answers_that_start_like_questions_stay_answers() -> None:
    for answer in (
        "Had a kebab instead of dinner",
        "Was out for dinner, pizza",
        "Did not eat lunch",
    ):
        assert not convstate.food_answer_is_something_else(answer), answer
    for ask in ("what's for dinner tomorrow?", "send me the menu", "shopping list please"):
        assert convstate.food_answer_is_something_else(ask), ask


def test_had_a_kebab_is_recorded_not_sent_to_chat(week) -> None:  # noqa: F811
    week.run(evening_confirm.JOB, TUE15)
    week.at(TUE15, "20:05")
    week.tap("k:cfmx:20260915")
    asked = week.say("Had a kebab instead of dinner")
    assert "skipping dinner" in asked["text"], asked["text"]


def test_stray_text_while_the_meal_buttons_wait_keeps_the_food(week) -> None:  # noqa: F811
    week.run(evening_confirm.JOB, TUE15)
    week.at(TUE15, "20:05")
    week.tap("k:cfmx:20260915")
    week.say("chicken burrito at work instead of dinner")
    nudge = week.say("hello")
    assert "Tap the meals" in nudge["text"], nudge["text"]
    state = json.loads(
        week.db.scalar("SELECT state_json FROM conversation_state WHERE person_id = ?", (week.his,))
    )
    assert state["note"].startswith("chicken burrito"), "the held food was overwritten"


def test_toggling_a_meal_changes_what_comes_off(week) -> None:  # noqa: F811
    week.run(evening_confirm.JOB, TUE15)
    week.at(TUE15, "20:05")
    week.say("skipped lunch, burrito for dinner")  # names two meals; both pre-ticked
    week.tap("k:mslot:20260915:dinner")  # he did eat dinner: untick it
    card = week.tap("k:mslotok:20260915")
    assert "instead of lunch?" in card["text"], card["text"]


def test_not_food_is_never_a_confirmed_day(week, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    """ "I will start on Monday" was recorded as a confirmed day at 1600 kcal."""
    from tiffin.model.gateway import ModelResult

    from . import test_journey_owner_week as j

    monkeypatch.setattr(
        j._Gateway,
        "json_task",
        lambda self, *a, **k: ModelResult(
            text="", data={"items": [], "kcal": 0}, model_used="fake", backend="fake"
        ),
    )
    week.run(evening_confirm.JOB, TUE15)
    week.at(TUE15, "20:05")
    week.tap("k:cfmx:20260915")
    week.say("I will start on Monday")
    out = week.tap("k:mslot0:20260915")
    assert "did not read as food" in out["text"], out["text"]
    row = days.latest_confirmation(week.db, week.his, TUE15)
    assert row is None or row["status"] != "confirmed"
    assert _estimates(week.db, week.his) == []


# ---------------------------------------------------------------- old cards
def test_a_card_older_than_a_week_is_refused(week) -> None:  # noqa: F811
    week.at(TUE15 + dt.timedelta(days=10), "12:00")
    iid = week.db.insert(
        "intents",
        person_id=week.his,
        kind="confirm_day",
        payload_json=json.dumps({"day": "2026-09-15", "as_planned": True}),
    )
    out = week.tap(f"k:intent:{iid}")
    assert "too far back" in out["text"], out["text"]
    assert days.latest_confirmation(week.db, week.his, TUE15) is None


def test_day_close_expires_week_old_cards(week) -> None:  # noqa: F811
    week.at(TUE15, "09:00")
    iid = week.db.insert("intents", person_id=week.his, kind="set_pause", payload_json="{}")
    week.at(TUE15 + dt.timedelta(days=8), "00:05")
    week.run(day_close.JOB, TUE15 + dt.timedelta(days=8))
    got = week.db.scalar("SELECT decision FROM intent_decisions WHERE intent_id = ?", (iid,))
    assert got == "lapsed"


def test_the_evening_arm_never_blocks_another_flow(week) -> None:  # noqa: F811
    week.at(TUE15, "20:00")
    assert arm_evening(week.db, week.his, TUE15, clock=week.clock)
    assert convstate.open_flow(week.db, week.his, clock=week.clock) == ""
