"""Tests for manual measurements and scale reminders."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from tiffin import body
from tiffin.app import App
from tiffin.clock import FrozenClock
from tiffin.db import Db
from tiffin.jobs import body_check, evening_confirm
from tiffin.secrets import Secrets
from tiffin.telegram import measures
from tiffin.telegram.poller import Poller

from .fakes import FakeTelegram

SUNDAY = dt.date(2026, 9, 13)


# ---------------------------------------------------------------- parsing
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("84 102", {"waist": 84.0, "hip": 102.0}),
        ("84/102", {"waist": 84.0, "hip": 102.0}),
        ("84, 102", {"waist": 84.0, "hip": 102.0}),
        ("84.5 101.5", {"waist": 84.5, "hip": 101.5}),
        ("84,5 101,5", {"waist": 84.5, "hip": 101.5}),
        ("84cm 102 cm", {"waist": 84.0, "hip": 102.0}),
        ("waist 84 hip 102", {"waist": 84.0, "hip": 102.0}),
        ("hip 102, waist 84", {"waist": 84.0, "hip": 102.0}),
        ("84", {"waist": 84.0}),
    ],
)
def test_parse_tape_reads_what_people_actually_type(text, expected) -> None:
    assert measures.parse_tape(text) == expected


@pytest.mark.parametrize("text", ["", "about the same as last week", "84 102 96 88"])
def test_parse_tape_declines_what_is_not_a_measurement(text) -> None:
    assert measures.parse_tape(text) is None


def test_a_labelled_hip_is_not_read_as_a_waist() -> None:
    # positional order would call 102 the waist; the label has to win
    assert measures.parse_tape("hip 102") == {"hip": 102.0}


# ---------------------------------------------------------------- storage
def test_record_tape_refuses_an_impossible_number(fixture_db: Db, two_persons) -> None:
    pid = two_persons["owner"]
    body.record_tape(fixture_db, pid, SUNDAY, "waist", 84.0)
    with pytest.raises(ValueError, match="outside"):
        body.record_tape(fixture_db, pid, SUNDAY, "waist", 840.0)
    with pytest.raises(ValueError, match="not a tape site"):
        body.record_tape(fixture_db, pid, SUNDAY, "ankle", 22.0)


def test_a_correction_is_another_row_and_the_latest_wins(fixture_db: Db, two_persons) -> None:
    pid = two_persons["owner"]
    body.record_tape(fixture_db, pid, SUNDAY, "waist", 84.0)
    body.record_tape(fixture_db, pid, SUNDAY, "waist", 83.5)
    assert fixture_db.scalar("SELECT COUNT(*) FROM tape_measures") == 2
    latest = body.latest_tape(fixture_db, pid, "waist")
    assert latest == (SUNDAY, 83.5)


def test_cycle_phase_refuses_anything_off_the_list(fixture_db: Db, two_persons) -> None:
    pid = two_persons["partner"]
    body.record_cycle_phase(fixture_db, pid, SUNDAY, "luteal")
    with pytest.raises(ValueError, match="not a cycle phase"):
        body.record_cycle_phase(fixture_db, pid, SUNDAY, "probably fine")


# ---------------------------------------------------------------- the conversation
@pytest.fixture
def bot(fixture_db: Db, two_persons, frozen_clock: FrozenClock, config, tmp_path: Path):
    secrets_dir = tmp_path / "s"
    secrets_dir.mkdir()
    (secrets_dir / "telegram-token-dev.txt").write_text("1:t", encoding="utf-8")
    tg = FakeTelegram()
    app = App(
        config,
        Secrets(secrets_dir),
        clock=frozen_clock,
        transport=tg,
        gateway_factory=lambda _app, _db: None,  # a tape reply must never need a model
    )
    poller = Poller(
        app.api(),
        fixture_db,
        app.registry(fixture_db),
        app.handlers(fixture_db).handle,
        clock=frozen_clock,
        sleeper=lambda s: None,
    )
    chat_id = 1000001
    state = {"n": 0}

    def say(text: str) -> str:
        state["n"] += 1
        tg.queue_update(tg.text_update(state["n"], chat_id, text))
        poller.run_once()
        return tg.sent[-1]["text"] if tg.sent else ""

    def tap(data: str) -> str:
        state["n"] += 1
        tg.queue_update(tg.callback_update(state["n"], chat_id, data, message_id=state["n"]))
        poller.run_once()
        return tg.sent[-1]["text"] if tg.sent else ""

    return app, tg, say, tap, two_persons["owner"]


def test_a_bare_number_does_nothing_until_the_flow_is_armed(bot, fixture_db: Db) -> None:
    _app, _tg, say, _tap, _pid = bot
    say("84 102")
    assert fixture_db.scalar("SELECT COUNT(*) FROM tape_measures") == 0


def test_measure_arms_then_records_then_disarms(bot, fixture_db: Db) -> None:
    _app, _tg, say, _tap, _pid = bot
    assert "waist and hip" in say("/measure")
    assert "Recorded" in say("84 102")
    rows = fixture_db.all("SELECT site, cm FROM tape_measures ORDER BY site")
    assert [(r["site"], r["cm"]) for r in rows] == [("hip", 102.0), ("waist", 84.0)]
    assert fixture_db.scalar("SELECT COUNT(*) FROM conversation_state") == 0
    # and the next bare number is ordinary conversation again
    say("84 102")
    assert fixture_db.scalar("SELECT COUNT(*) FROM tape_measures") == 2


def test_measure_with_the_numbers_inline_skips_the_round_trip(bot, fixture_db: Db) -> None:
    _app, _tg, say, _tap, _pid = bot
    assert "Recorded" in say("/measure 84 102")
    assert fixture_db.scalar("SELECT COUNT(*) FROM tape_measures") == 2


def test_skip_leaves_the_week_without_a_row(bot, fixture_db: Db) -> None:
    _app, _tg, say, _tap, _pid = bot
    say("/measure")
    assert "Left for now" in say("skip")
    assert fixture_db.scalar("SELECT COUNT(*) FROM tape_measures") == 0
    assert fixture_db.scalar("SELECT COUNT(*) FROM conversation_state") == 0


def test_an_unreadable_reply_keeps_the_flow_open(bot, fixture_db: Db) -> None:
    _app, _tg, say, _tap, _pid = bot
    say("/measure")
    assert "centimetres" in say("about the same")
    assert fixture_db.scalar("SELECT COUNT(*) FROM conversation_state") == 1
    assert "Recorded" in say("84 102")


def test_measure_with_numbers_does_not_destroy_an_interview(bot, fixture_db: Db) -> None:
    """`/measure 84 102` armed the flow before checking, wiping conversation_state.

    Mid-onboarding that lost the whole interview, and the numbers went with it.
    """
    _app, _tg, say, _tap, pid = bot
    fixture_db.execute(
        "INSERT INTO conversation_state(person_id, state_json, updated_at) VALUES (?, ?, ?)",
        (pid, json.dumps({"flow": "onboarding", "step": "sex"}), "2026-09-13 09:00:00"),
    )
    assert "Answer the question above" in say("/measure 84 102")
    state = fixture_db.one("SELECT state_json FROM conversation_state WHERE person_id = ?", (pid,))
    assert state is not None
    assert json.loads(state["state_json"])["flow"] == "onboarding", "the interview survives"
    assert fixture_db.scalar("SELECT COUNT(*) FROM tape_measures") == 0


def test_measure_refuses_to_interrupt_another_conversation(bot, fixture_db: Db) -> None:
    _app, _tg, say, _tap, pid = bot
    fixture_db.execute(
        "INSERT INTO conversation_state(person_id, state_json, updated_at) VALUES (?, ?, ?)",
        (pid, json.dumps({"flow": "onboarding", "step": "sex"}), "2026-09-13 09:00:00"),
    )
    assert "Answer the question above" in say("/measure")
    state = fixture_db.one("SELECT state_json FROM conversation_state WHERE person_id = ?", (pid,))
    assert state is not None
    assert json.loads(state["state_json"])["flow"] == "onboarding", "onboarding survives"


def test_a_cycle_tap_records_a_phase(bot, fixture_db: Db) -> None:
    _app, _tg, _say, tap, pid = bot
    tap("k:cyc:luteal")
    row = fixture_db.one("SELECT phase FROM cycle_phases WHERE person_id = ?", (pid,))
    assert row is not None and row["phase"] == "luteal"


# ---------------------------------------------------------------- the Sunday job
def test_the_cycle_job_has_sunday_attempts() -> None:
    assert body_check.JOB.days == frozenset({6})
    # A skipped slot can be retried after another conversation finishes.
    assert body_check.JOB.times(None, SUNDAY) == ["10:00", "13:00", "16:00", "19:00"]
    assert body_check.JOB.catch_up == "20:00"


# ---------------------------------------------------------------- scale reminder
def test_the_scale_nudge_waits_three_days(fixture_db: Db, two_persons) -> None:
    pid = two_persons["owner"]
    body.record_weigh_in(fixture_db, pid, dt.date(2026, 9, 12), 100.4, "icu_wellness")
    assert evening_confirm.scale_line(fixture_db, pid, dt.date(2026, 9, 14)) == ""
    line = evening_confirm.scale_line(fixture_db, pid, dt.date(2026, 9, 15))
    assert "No weigh-in for 3 days" in line


def test_the_scale_nudge_asks_once_per_dry_spell(fixture_db: Db, two_persons) -> None:
    pid = two_persons["owner"]
    body.record_weigh_in(fixture_db, pid, dt.date(2026, 9, 12), 100.4, "icu_wellness")
    assert evening_confirm.scale_line(fixture_db, pid, dt.date(2026, 9, 15)) != ""
    # every following evening in the same gap stays quiet
    assert evening_confirm.scale_line(fixture_db, pid, dt.date(2026, 9, 16)) == ""
    assert evening_confirm.scale_line(fixture_db, pid, dt.date(2026, 9, 17)) == ""
    # they weigh in, then stop again: that is a new gap and earns one more ask
    body.record_weigh_in(fixture_db, pid, dt.date(2026, 9, 18), 100.1, "icu_wellness")
    assert evening_confirm.scale_line(fixture_db, pid, dt.date(2026, 9, 21)) != ""


def test_someone_who_has_never_weighed_in_is_asked_once(fixture_db: Db, two_persons) -> None:
    pid = two_persons["partner"]
    line = evening_confirm.scale_line(fixture_db, pid, dt.date(2026, 9, 15))
    assert "No weigh-in recorded yet" in line
    assert evening_confirm.scale_line(fixture_db, pid, dt.date(2026, 9, 16)) == ""
