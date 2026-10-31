"""Changing a menu starts a build and preserves the current cook until ready."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tiffin.app import App
from tiffin.clock import FrozenClock
from tiffin.db import Db
from tiffin.secrets import Secrets
from tiffin.telegram import menuflow
from tiffin.telegram.poller import Poller

from .fakes import FakeTelegram


# ---------------------------------------------------------------- the change note
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
        gateway_factory=lambda _app, _db: None,
    )
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

    def tap(data: str) -> str:
        state["n"] += 1
        tg.queue_update(tg.callback_update(state["n"], 1000001, data, message_id=state["n"]))
        poller.run_once()
        return tg.sent[-1]["text"] if tg.sent else ""

    return app, tg, say, tap, two_persons["owner"]


def a_menu(db: Db, person_id: int) -> int:
    return db.insert(
        "menus",
        person_id=person_id,
        segment_start="2026-09-14",
        segment_end="2026-09-18",
        status="proposed",
    )


def test_change_asks_and_then_listens(bot, fixture_db: Db) -> None:
    _app, _tg, say, tap, pid = bot
    menu_id = a_menu(fixture_db, pid)
    assert "what to change" in tap(f"k:menux:{menu_id}")
    state = fixture_db.one("SELECT state_json FROM conversation_state WHERE person_id = ?", (pid,))
    assert state is not None
    assert json.loads(state["state_json"])["flow"] == menuflow.CHANGE_FLOW

    # the reply is acted on, not filed: no gateway here, so it fails loudly rather than silently
    out = say("more fish, no rice")
    assert "Free text is not wired" not in out, "the change note must not fall through to chat"
    assert "onboard" in out or "what to cook" in out, "it goes to the menu builder"
    assert fixture_db.scalar("SELECT COUNT(*) FROM conversation_state") == 0, "and it disarms"


def test_skip_leaves_the_menu_alone(bot, fixture_db: Db) -> None:
    """It said "Left it" and deleted the menu: the Change tap had already written 'rejected',
    and this test only ever looked at the conversation state."""
    _app, _tg, say, tap, pid = bot
    menu_id = a_menu(fixture_db, pid)
    fixture_db.insert("menu_decisions", menu_id=menu_id, decision="accepted")
    tap(f"k:menux:{menu_id}")
    assert "Kept the menu" in say("skip")
    assert fixture_db.scalar("SELECT COUNT(*) FROM conversation_state") == 0
    current = fixture_db.one("SELECT id FROM v_menu_current WHERE id = ?", (menu_id,))
    assert current is not None, "the menu is still in force after skip"
    decisions = [
        r["decision"]
        for r in fixture_db.all("SELECT decision FROM menu_decisions WHERE menu_id = ?", (menu_id,))
    ]
    assert "rejected" not in decisions, "Change never rejects before a replacement exists"


def test_change_does_not_interrupt_onboarding(bot, fixture_db: Db) -> None:
    _app, _tg, _say, tap, pid = bot
    fixture_db.execute(
        "INSERT INTO conversation_state(person_id, state_json, updated_at) VALUES (?, ?, ?)",
        (pid, json.dumps({"flow": "onboarding", "step": "sex"}), "2026-09-07 09:00:00"),
    )
    out = tap(f"k:menux:{a_menu(fixture_db, pid)}")
    assert "Answer the question above" in out
    state = fixture_db.one("SELECT state_json FROM conversation_state WHERE person_id = ?", (pid,))
    assert state is not None
    assert json.loads(state["state_json"])["flow"] == "onboarding"


def test_a_stale_change_question_stops_listening(fixture_db: Db, two_persons, frozen_clock) -> None:
    """Armed on Monday, answered on Wednesday: that message is not a menu change."""
    flow = menuflow.MenuFlow(None, clock=frozen_clock)
    pid = two_persons["owner"]
    flow.arm_change(fixture_db, pid)
    assert flow._change_state(fixture_db, pid) is not None
    fixture_db.execute(
        "UPDATE conversation_state SET updated_at = ? WHERE person_id = ?",
        ("2026-09-05 09:00:00", pid),
    )
    assert flow._change_state(fixture_db, pid) is None
    assert fixture_db.scalar("SELECT COUNT(*) FROM conversation_state") == 0


def test_the_note_reaches_the_prompt_and_the_failure_hint_does_not() -> None:
    """`hint` renders as "the previous attempt failed"; a preference must not go through it."""
    from tiffin.model import prompts

    text = prompts.menu_propose(
        ingredients=["Cod 75 kcal"],
        days=[
            {"day": "2026-09-14", "slots": "dinner", "kcal": 2000, "protein_g": 180, "fat_g": 60}
        ],
        variety={"dinner": "same"},
        asked_for="more fish, no rice",
    )
    assert "more fish, no rice" in text
    assert "previous attempt failed" not in text


def test_cook_carries_words_that_are_not_replace() -> None:
    cases = {
        "": ("", False),
        "replace": ("", True),
        "again": ("", True),
        "new": ("", True),
        "more fish": ("more fish", False),
        "replace no chickpeas": ("no chickpeas", True),
        "  Replace   more fish  ": ("more fish", True),
        "no rice": ("no rice", False),
    }
    for arg, (want_note, want_replace) in cases.items():
        words = arg.strip().split()
        replace = bool(words) and words[0].lower() in ("replace", "again", "new")
        note = " ".join(words[1:] if replace else words)
        assert (note, replace) == (want_note, want_replace), arg
