"""Tests for keeping onboarding answers consistent across interrupted flows."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tiffin import onboarding as ob
from tiffin.app import App
from tiffin.clock import FrozenClock
from tiffin.db import Db
from tiffin.secrets import Secrets
from tiffin.telegram import convstate
from tiffin.telegram.poller import Poller

from .fakes import FakeTelegram


# ---------------------------------------------------------------- the training week
def test_a_second_session_on_one_day_survives() -> None:
    """`Tue ride 60, Tue strength 30` kept only the strength: a dict keyed by weekday."""
    week = ob.parse_plan("Mon climb 90, Tue ride 60, Tue strength 30", {})
    tue = [s for s in week if s["weekday"] == 1]
    assert len(tue) == 2
    assert {s["kind"] for s in tue} == {"ride", "strength"}
    assert sum(s["target_minutes"] for s in tue) == 90


def test_a_word_it_cannot_read_is_refused_not_turned_into_rest() -> None:
    with pytest.raises(ob.Invalid) as e:
        ob.parse_plan("Mon climb 90, Tue bouldering 60", {})
    assert "Tue bouldering 60" in str(e.value)
    assert "other" in str(e.value), "it names the escape hatch"


def test_the_days_it_can_read_are_still_read() -> None:
    week = ob.parse_plan("Mon climb 90 at 18:30, Wed ride 60, Sat ropes 120", {})
    assert len(week) == 7, "four rest days plus three sessions"
    assert [s["kind"] for s in week if s["kind"] != "rest"] == ["climb", "ride", "ropes"]
    assert next(s for s in week if s["weekday"] == 0)["start_time"] == "18:30"


def test_a_day_named_as_rest_stays_rest() -> None:
    week = ob.parse_plan("Mon climb 90, Tue rest", {})
    assert next(s for s in week if s["weekday"] == 1)["kind"] == "rest"


def test_text_with_no_day_in_it_is_still_the_old_error() -> None:
    with pytest.raises(ob.Invalid, match="could not read a session"):
        ob.parse_plan("I train quite a lot", {})


def test_plan_lines_reads_a_week_with_two_on_one_day() -> None:
    lines = ob.plan_lines(ob.parse_plan("Tue ride 60, Tue strength 30", {}))
    assert sum(1 for line in lines if line.startswith("Tue")) == 2


# ---------------------------------------------------------------- the interview itself
@pytest.fixture
def interview(fixture_db: Db, two_persons, frozen_clock: FrozenClock, config, tmp_path: Path):
    secrets_dir = tmp_path / "s"
    secrets_dir.mkdir()
    (secrets_dir / "telegram-token-dev.txt").write_text("1:t", encoding="utf-8")
    tg = FakeTelegram()
    app = App(config, Secrets(secrets_dir), clock=frozen_clock, transport=tg)
    poller = Poller(
        app.api(),
        fixture_db,
        app.registry(fixture_db),
        app.handlers(fixture_db).handle,
        clock=frozen_clock,
        sleeper=lambda s: None,
    )
    state = {"n": 0}

    def say(text: str) -> str:
        state["n"] += 1
        tg.queue_update(tg.text_update(state["n"], 1000001, text))
        poller.run_once()
        return tg.sent[-1]["text"] if tg.sent else ""

    def tap(step: str, value: str) -> str:
        state["n"] += 1
        tg.queue_update(
            tg.callback_update(state["n"], 1000001, f"k:ob:{step}:{value}", message_id=state["n"])
        )
        poller.run_once()
        return tg.sent[-1]["text"] if tg.sent else ""

    return tg, say, tap, two_persons["owner"], fixture_db


def test_back_takes_one_answer_off_and_asks_it_again(interview) -> None:
    tg, say, tap, pid, db = interview
    say("/onboard")
    first = tg.sent[-1]["text"]
    assert "1/" in first, "the interview starts at question one"
    tap("cons", "0")
    tap("sex", "m")
    second = tg.sent[-1]["text"]
    assert "3/" in second
    assert '"k:ob:' in json.dumps(tg.sent[-1].get("reply_markup") or {})
    assert ':back"' in json.dumps(tg.sent[-1].get("reply_markup") or {}), "a Back button"

    back = say("back")
    assert "2/" in back, "sex again"
    state = db.one("SELECT state_json FROM conversation_state WHERE person_id = ?", (pid,))
    assert state is not None
    assert "sex" not in json.loads(state["state_json"])["answers"], "the answer is off the sheet"
    # and the ledger still has the original answer: corrections are new rows, not edits
    assert db.scalar("SELECT COUNT(*) FROM onboarding_answers WHERE step = 'sex'") == 1


def test_the_first_question_has_no_back_button(interview) -> None:
    tg, say, _tap, _pid, _db = interview
    say("/onboard")
    assert ':back"' not in json.dumps(tg.sent[-1].get("reply_markup") or {})


def test_back_on_the_first_question_says_so_rather_than_breaking(interview) -> None:
    _tg, say, _tap, _pid, _db = interview
    say("/onboard")
    assert "first question" in say("back")


def test_later_parks_the_interview_and_frees_the_conversation(interview) -> None:
    """ "Later" held the row that means "a question is open", which blocked /measure."""
    _tg, say, tap, pid, db = interview
    say("/onboard")
    tap("cons", "0")
    tap("sex", "m")
    assert "Paused" in say("later")
    assert db.scalar("SELECT COUNT(*) FROM conversation_state") == 0, "nothing is waiting"
    assert convstate.open_flow(db, pid) == ""

    # which is the whole point: another flow can now run
    assert "waist and hip" in say("/measure")
    assert "Recorded" in say("84 102")

    # and the interview is still there, with its answers
    resumed = say("/onboard")
    assert "3/" in resumed, "it continues where it stopped, not from the top"


def test_a_paused_interview_is_not_lost_by_the_flow_that_ran_meanwhile(interview) -> None:
    _tg, say, tap, pid, db = interview
    say("/onboard")
    tap("cons", "0")
    tap("sex", "m")
    say("later")
    say("/measure")  # starts a separate flow in the conversation state
    say("/onboard")
    state = db.one("SELECT state_json FROM conversation_state WHERE person_id = ?", (pid,))
    assert state is not None
    st = json.loads(state["state_json"])
    assert st["flow"] == "onboarding" and st["answers"]["sex"] == "m"
