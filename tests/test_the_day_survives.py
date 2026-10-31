"""Four ways an answered day was lost anyway.

The evening check-in is the one interaction that must never fail quietly, because a day that
does not close is a day Sunday's calibration counts as zero kcal and nags about at 22:30. All
four of these were reachable on an ordinary evening.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from tiffin import body, days
from tiffin.app import App
from tiffin.clock import FrozenClock
from tiffin.db import Db
from tiffin.jobs import evening_confirm
from tiffin.secrets import Secrets
from tiffin.telegram.poller import Poller

from .fakes import FakeTelegram

OWNER_CHAT = 1000001
MONDAY = dt.date(2026, 9, 7)


@pytest.fixture
def world(fixture_db: Db, two_persons, frozen_clock: FrozenClock, config, tmp_path: Path):
    pid = two_persons["owner"]
    fixture_db.insert(
        "profiles",
        person_id=pid,
        effective_from="2026-09-01",
        sex="m",
        birth_date="1990-03-15",
        height_cm=183.0,
        activity_factor=1.55,
        kcal_floor=1800,
        confirm_time="20:30",
        quiet_start="22:30",
        quiet_end="07:00",
    )
    body.record_weigh_in(fixture_db, pid, dt.date(2026, 9, 6), 100.9, "manual")
    fixture_db.insert(
        "targets",
        person_id=pid,
        week_start="2026-09-07",
        kcal=2250,
        protein_g=202,
        fat_g=71,
        carb_g=200,
        fibre_g=30,
        source="formula",
    )
    plan = fixture_db.insert("training_plans", person_id=pid, effective_from="2026-09-01")
    for kind, minutes in (("ride", 60), ("strength", 30)):
        fixture_db.insert(
            "planned_sessions",
            plan_id=plan,
            person_id=pid,
            weekday=0,
            kind=kind,
            target_minutes=minutes,
        )
    secrets_dir = tmp_path / "s"
    secrets_dir.mkdir()
    (secrets_dir / "telegram-token-dev.txt").write_text("1:t", encoding="utf-8")
    tg = FakeTelegram()
    app = App(config, Secrets(secrets_dir), clock=frozen_clock, transport=tg)
    handlers = app.handlers(fixture_db)
    poller = Poller(
        app.api(),
        fixture_db,
        app.registry(fixture_db),
        handlers.handle,
        clock=frozen_clock,
        sleeper=lambda s: None,
    )
    n = {"i": 0}

    def say(text: str) -> str:
        n["i"] += 1
        tg.queue_update(tg.text_update(n["i"], OWNER_CHAT, text))
        poller.run_once()
        return tg.sent[-1]["text"]

    def tap(data: str) -> str:
        n["i"] += 1
        tg.queue_update(tg.callback_update(n["i"], OWNER_CHAT, data, message_id=n["i"]))
        poller.run_once()
        return tg.sent[-1]["text"]

    return pid, app, tg, say, tap, handlers


def test_slash_confirm_records_the_day(world, fixture_db: Db) -> None:
    """It called a keyboard edit on the PERSON's own message first, which Telegram refuses,
    so the TelegramError propagated before anything was recorded and they got "something
    went wrong" - twice of the three documented ways to close a day, dead."""
    pid, _app, _tg, say, _tap, _handlers = world
    out = say("/confirm")
    assert "recorded" in out and "went wrong" not in out
    row = days.latest_confirmation(fixture_db, pid, MONDAY)
    assert row is not None and row["status"] == "confirmed"


def test_answering_same_records_the_day(world, fixture_db: Db) -> None:
    """ "Send 'same' if it was close enough" is the check-in's own suggested answer."""
    pid, _app, _tg, say, tap, _handlers = world
    tap("k:cfmx:20260907")
    out = say("same")
    assert "recorded" in out and "went wrong" not in out
    row = days.latest_confirmation(fixture_db, pid, MONDAY)
    assert row is not None and row["status"] == "confirmed"


def test_typing_what_changed_closes_the_day_even_with_an_estimator(world, fixture_db: Db) -> None:
    """The evening estimator closes the day only after an estimate is accepted."""
    pid, _app, _tg, say, tap, handlers = world
    seen: list[str] = []

    def estimator(h, inbound, day, text, *, replaced_slots=None, finalize=False):
        seen.append(text)
        assert finalize, "the evening flow's answer always asks to be finalised"
        days.confirm_day(h.db, inbound.person.id, day, basis="mixed", macros=None, note=text[:500])
        h.outbox.reply(inbound.person, f"Log off plan on {day}: {text} (900 kcal)?")
        return "handled"

    handlers.offplan_estimator = estimator  # type: ignore[attr-defined]
    tap("k:cfmx:20260907")
    out = say("kebab and chips")
    if "k:mslotok:20260907" in str(out) or "Done" in out:
        # a planned day asks which meals it replaced before anything is estimated
        out = tap("k:mslot0:20260907")
    assert seen == ["kebab and chips"] and "900 kcal" in out
    row = days.latest_confirmation(fixture_db, pid, MONDAY)
    assert row is not None and row["status"] == "confirmed", "the day is closed"


def test_both_sessions_on_a_day_are_recorded(world, fixture_db: Db) -> None:
    """One row for the day meant the second session could never be recorded at all."""
    pid, _app, _tg, _say, tap, _handlers = world
    assert "Both sessions" in tap("k:sessm:20260907:done")
    rows = fixture_db.all(
        "SELECT planned_session_id, status FROM session_matches WHERE person_id = ?", (pid,)
    )
    assert len(rows) == 2 and {r["status"] for r in rows} == {"manual"}
    assert len({r["planned_session_id"] for r in rows}) == 2, "one per planned session"


def test_a_mis_tapped_session_can_be_put_right(world, fixture_db: Db) -> None:
    pid, _app, _tg, _say, tap, _handlers = world
    tap("k:sessm:20260907:skip")
    assert "changed to manual" in tap("k:sessm:20260907:done")
    assert str(days.session_decided(fixture_db, pid, MONDAY)["status"]) == "manual"  # type: ignore[index]


def test_confirming_the_day_closes_the_open_what_changed_question(world, fixture_db: Db) -> None:
    """It stayed armed, so the next morning's "what's for lunch?" was logged as yesterday."""
    _pid, _app, _tg, _say, tap, _handlers = world
    tap("k:cfmx:20260907")
    assert fixture_db.scalar("SELECT COUNT(*) FROM conversation_state") == 1
    tap("k:cfm:20260907")
    assert fixture_db.scalar("SELECT COUNT(*) FROM conversation_state") == 0


def test_an_overnight_what_changed_question_has_closed(world, fixture_db: Db) -> None:
    """Asked at 20:31, still armed at 08:00: "yesterday" let all of the next day through."""
    pid, _app, tg, say, tap, _handlers = world
    tap("k:cfmx:20260907")
    fixture_db.execute(
        "UPDATE conversation_state SET updated_at = ? WHERE person_id = ?",
        ("2026-09-06 19:31:00", pid),
    )
    before = len(tg.sent)
    say("morning, what's for lunch?")
    # it says so, then lets the message go on to ordinary conversation rather than eating it
    assert any("has closed" in m["text"] for m in tg.sent[before:])
    row = days.latest_confirmation(fixture_db, pid, MONDAY)
    assert row is None, "and nothing was written against that day"


def test_the_evening_message_still_works_end_to_end(world, fixture_db: Db, config) -> None:
    """The whole point: none of the above may have broken the ordinary evening."""
    from tiffin import scheduler as sc

    pid, app, tg, _say, tap, _handlers = world
    prof = fixture_db.one("SELECT * FROM profiles WHERE person_id = ?", (pid,))
    sched = sc.Scheduler(app, [evening_confirm.JOB], lambda p, fn: None)
    slots = sc.slots_for(evening_confirm.JOB, pid, prof, MONDAY)
    assert sched.execute(fixture_db, slots[0]) == "ok"
    assert "ride 60 min then strength 30 min" in tg.sent[-1]["text"]
    assert "recorded: 2250 kcal" in tap("k:cfm:20260907")
