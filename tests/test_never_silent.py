"""Interrupted runs, failure notices and manual measurement questions."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from tiffin import ledger
from tiffin.app import App
from tiffin.clock import FrozenClock
from tiffin.db import Db
from tiffin.secrets import Secrets
from tiffin.telegram import measures
from tiffin.telegram.api import TelegramApi
from tiffin.telegram.outbox import Outbox

from .fakes import FakeTelegram, RecordingSleeper, read_notices

OWNER_CHAT = 1000001
SUNDAY = dt.date(2026, 9, 13)


# ---------------------------------------------------------------- the tape question
@pytest.mark.parametrize(
    "text",
    [
        "I ran 45 minutes this morning",
        "did 20 min core and 100 press ups",
        "kebab and chips, about 900 kcal",
        "how is my week going?",
    ],
)
def test_an_ordinary_sentence_is_not_a_tape_measurement(text: str) -> None:
    """The positional branch took any 1-3 digit number anywhere in the text, so a sentence
    about a run was written into tape_measures and the flow then disarmed - the real number
    was never asked for again that week, and the advisor read the phantom against weight."""
    assert measures.parse_tape(text) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("84 102", {"waist": 84.0, "hip": 102.0}),
        ("84/102", {"waist": 84.0, "hip": 102.0}),
        ("84, 102", {"waist": 84.0, "hip": 102.0}),
        ("84.5 101.5", {"waist": 84.5, "hip": 101.5}),
        ("84,5 101,5", {"waist": 84.5, "hip": 101.5}),
        ("84cm 102 cm", {"waist": 84.0, "hip": 102.0}),
        ("84", {"waist": 84.0}),
        ("  84 102  ", {"waist": 84.0, "hip": 102.0}),
        ("waist 84 hip 102", {"waist": 84.0, "hip": 102.0}),
    ],
)
def test_a_real_measurement_still_reads(text: str, expected: dict) -> None:
    assert measures.parse_tape(text) == expected


def test_a_stale_tape_question_stops_claiming_messages(fixture_db: Db, two_persons, frozen_clock):
    flow = measures.MeasuresFlow(clock=frozen_clock)
    pid = two_persons["owner"]
    flow.arm(fixture_db, pid, SUNDAY)
    assert flow._state(fixture_db, pid) is not None
    fixture_db.execute(
        "UPDATE conversation_state SET updated_at = ? WHERE person_id = ?",
        ("2026-09-05 09:00:00", pid),
    )
    assert flow._state(fixture_db, pid) is None
    assert fixture_db.scalar("SELECT COUNT(*) FROM conversation_state") == 0


def test_two_unreadable_replies_and_it_gets_out_of_the_way(
    fixture_db: Db, two_persons, frozen_clock: FrozenClock, config, tmp_path: Path
) -> None:
    """It bounced every message with "I need centimetres" for ever, so free text - the main
    way into this system - simply stopped working."""
    from tiffin.telegram.poller import Poller

    secrets_dir = tmp_path / "s"
    secrets_dir.mkdir()
    (secrets_dir / "telegram-token-dev.txt").write_text("1:t", encoding="utf-8")
    tg = FakeTelegram()
    app = App(
        config,
        Secrets(secrets_dir),
        clock=frozen_clock,
        transport=tg,
        gateway_factory=lambda _a, _d: None,
    )
    poller = Poller(
        app.api(),
        fixture_db,
        app.registry(fixture_db),
        app.handlers(fixture_db).handle,
        clock=frozen_clock,
        sleeper=lambda s: None,
    )
    n = {"i": 0}

    def say(text: str) -> str:
        n["i"] += 1
        tg.queue_update(tg.text_update(n["i"], OWNER_CHAT, text))
        poller.run_once()
        return tg.sent[-1]["text"]

    say("/measure")
    assert "centimetres" in say("about the same as last week"), "one nudge"
    # "not sure, forgot the tape" is unreadable but names nothing else and is not shaped like a
    # question - "what should I eat tonight?" would be, and swallowing a genuine
    # question two misses in is the same bug this flow exists to avoid, just with a worse answer
    assert "stop asking" in say("not sure, forgot the tape"), "then it stands down"
    assert fixture_db.scalar("SELECT COUNT(*) FROM conversation_state") == 0
    assert fixture_db.scalar("SELECT COUNT(*) FROM tape_measures") == 0


# ---------------------------------------------------------------- the alert that was rejected
def test_an_alert_about_an_inequality_is_recorded_word_for_word(
    fixture_db: Db, two_persons, frozen_clock: FrozenClock, config, tmp_path: Path
) -> None:
    """The solver says "protein: max reachable 185 g < 201 g". Telegram's HTML parser rejects
    an unmatched "<" with 400, so the alert about the failure was itself dropped - selectively,
    for exactly the failures that most needed reporting."""
    tg = FakeTelegram()
    api = TelegramApi("1:x", transport=tg, sleeper=RecordingSleeper())
    outbox = Outbox(
        api,
        fixture_db,
        env="prod",
        admin_chat_id=OWNER_CHAT,
        state_dir=tmp_path / "state",
        clock=frozen_clock,
    )
    from tiffin.persons import PersonRegistry

    admin = PersonRegistry(fixture_db, config, clock=frozen_clock).require("owner")
    outbox.alert(admin, "menu_failed", "protein: max reachable 185 g < 201 g & short")
    # a notice, not a message: nothing for Telegram's HTML parser to reject,
    # and the page escapes on the way out, so the text is kept exactly as the solver said it
    assert tg.sent == []
    assert (
        read_notices(tmp_path / "state")[-1]["text"]
        == "protein: max reachable 185 g < 201 g & short"
    )


# ---------------------------------------------------------------- a run a restart abandoned
def test_a_run_left_running_by_a_kill_is_retried(fixture_db: Db, frozen_clock: FrozenClock) -> None:
    """Ledger.run has always been willing to take a stale row over; the scheduler skipped
    every 'running' row unconditionally, so nothing could ever reach that path."""
    fixture_db.execute(
        "INSERT INTO run_ledger(run_id, job, slot, person_id, started_at, status) "
        "VALUES (?, ?, ?, ?, ?, 'running')",
        ("r1", "menu_propose", "2026-09-13T1800", 1, "2026-09-13 17:00:00"),
    )
    assert ledger.status_of(fixture_db, "menu_propose", "2026-09-13T1800", 1) == "running"
    fixture_db.execute(
        "UPDATE run_ledger SET started_at = ? WHERE run_id = ?", ("2026-09-07 10:00:00", "r1")
    )
    assert ledger.live(
        fixture_db, "menu_propose", "2026-09-13T1800", 1, clock=frozen_clock, stale_after_s=99999
    ), "a fresh run still blocks"
    assert not ledger.live(
        fixture_db, "menu_propose", "2026-09-13T1800", 1, clock=frozen_clock, stale_after_s=60
    ), "an abandoned one does not"


def test_the_scheduler_no_longer_treats_running_as_terminal() -> None:
    import inspect

    from tiffin import scheduler

    body = inspect.getsource(scheduler.Scheduler.tick)
    assert "ledger.live(" in body
    assert '"running",' not in body, "it is not in the skip tuple any more"
