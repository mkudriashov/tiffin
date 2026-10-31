"""Evening check-in, taps, reminder, day close, pause, /today — through the fake Telegram."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from tiffin import body, days
from tiffin import scheduler as sc
from tiffin.app import App
from tiffin.clock import FrozenClock
from tiffin.db import Db
from tiffin.jobs import day_close, evening_confirm
from tiffin.secrets import Secrets
from tiffin.telegram.poller import Poller

from .fakes import FakeTelegram

OWNER_CHAT = 1000001


@pytest.fixture
def world(fixture_db: Db, two_persons, frozen_clock: FrozenClock, config, tmp_path):
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
    fixture_db.insert(
        "planned_sessions", plan_id=plan, person_id=pid, weekday=0, kind="climb", target_minutes=90
    )
    secrets_dir = tmp_path / "s"
    secrets_dir.mkdir()
    (secrets_dir / "telegram-token-dev.txt").write_text("1:t", encoding="utf-8")
    # this fixture is the owner, and the owner has a real intervals.icu key (CLAUDE.md); without one
    # here
    # evening_confirm now (rightly, integrations_later/01) stops naming intervals.icu at all,
    # which broke this file's "nothing from intervals.icu yet" assertions for a person who
    # in fact has the key
    (secrets_dir / "icu-key-owner.txt").write_text("k-owner", encoding="utf-8")
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

    def run_job(job: sc.Job, day: dt.date) -> str:
        s = sc.Scheduler(app, [job], lambda p, fn: None)
        prof = fixture_db.one("SELECT * FROM profiles WHERE person_id = ?", (pid,))
        slots = sc.slots_for(job, pid, prof, day)
        return s.execute(fixture_db, slots[0])

    return pid, app, tg, say, tap, run_job


def test_evening_message_and_as_planned_tap(world, fixture_db: Db) -> None:
    pid, _app, tg, _say, tap, run_job = world
    assert run_job(evening_confirm.JOB, dt.date(2026, 9, 7)) == "ok"
    text = tg.sent[-1]["text"]
    assert text.startswith("<b>Mon 7 Sep</b>")
    assert "No menu today; the target was 2,250 kcal" in text
    assert "Planned climb 90 min; nothing from intervals.icu yet." in text
    kb = [b["callback_data"] for row in tg.sent[-1]["reply_markup"]["inline_keyboard"] for b in row]
    assert kb == [
        "k:cfm:20260907",
        "k:cfmx:20260907",
        "k:sessm:20260907:done",
        "k:sessm:20260907:skip",
    ]
    assert (
        fixture_db.scalar(
            "SELECT COUNT(*) FROM messages_out WHERE person_id = ? AND kind = 'evening_confirm' "
            "AND status = 'sent'",
            (pid,),
        )
        == 1
    )
    assert "recorded: 2250 kcal" in tap("k:cfm:20260907")
    row = days.latest_confirmation(fixture_db, pid, dt.date(2026, 9, 7))
    assert row is not None and row["status"] == "confirmed" and row["basis"] == "none"
    assert row["kcal"] == 2250 and row["note"] == "assumed target"
    assert "already recorded" in tap("k:cfm:20260907")
    assert run_job(evening_confirm.JOB, dt.date(2026, 9, 7)) == "already"
    assert "Session recorded" in tap("k:sessm:20260907:done")
    sm = fixture_db.latest("session_matches", person_id=pid)
    assert sm is not None and sm["status"] == "manual" and sm["minutes"] == 90
    # a mis-tap is correctable now: the other button supersedes rather than being refused
    assert "changed to missed" in tap("k:sessm:20260907:skip")
    assert "already recorded as missed" in tap("k:sessm:20260907:skip")


def test_not_quite_captures_a_note(world, fixture_db: Db) -> None:
    pid, _app, _tg, say, tap, run_job = world
    run_job(evening_confirm.JOB, dt.date(2026, 9, 7))
    assert "What changed" in tap("k:cfmx:20260907")
    assert "Noted for Mon 07 Sep" in say("skipped lunch, kebab and chips for dinner")
    row = days.latest_confirmation(fixture_db, pid, dt.date(2026, 9, 7))
    assert row is not None and row["status"] == "confirmed" and "kebab" in row["note"]
    assert fixture_db.scalar("SELECT COUNT(*) FROM conversation_state") == 0
    run_job(evening_confirm.JOB, dt.date(2026, 9, 8))
    tap("k:cfmx:20260908")
    assert "recorded: 2250 kcal" in say("same")


def test_matched_session_buttons(world, fixture_db: Db) -> None:
    pid, _app, tg, _say, tap, run_job = world
    ps = fixture_db.one("SELECT id FROM planned_sessions WHERE person_id = ?", (pid,))
    mid = fixture_db.insert(
        "session_matches",
        person_id=pid,
        day="2026-09-07",
        planned_session_id=ps["id"],  # type: ignore[index]
        icu_id="i123",
        status="matched",
        minutes=85,
    )
    run_job(evening_confirm.JOB, dt.date(2026, 9, 7))
    text = tg.sent[-1]["text"]
    assert "intervals.icu shows 85 min. Count it?" in text
    kb = [b["callback_data"] for row in tg.sent[-1]["reply_markup"]["inline_keyboard"] for b in row]
    assert f"k:sess:{mid}" in kb and f"k:sessx:{mid}" in kb
    assert "Session confirmed" in tap(f"k:sess:{mid}")
    latest = fixture_db.latest("session_matches", person_id=pid)
    assert latest is not None and latest["status"] == "confirmed" and latest["icu_id"] == "i123"
    assert "changed to rejected" in tap(f"k:sessx:{mid}"), "a mis-tapped Not this is correctable"
    assert "already recorded as rejected" in tap(f"k:sessx:{mid}")


def test_day_close_and_pause(world, fixture_db: Db) -> None:
    pid, _app, _tg, say, _tap, run_job = world
    assert run_job(day_close.JOB, dt.date(2026, 9, 8)) == "ok"  # closes 09-07
    row = days.latest_confirmation(fixture_db, pid, dt.date(2026, 9, 7))
    assert row is not None and row["status"] == "unconfirmed" and row["kcal"] is None
    assert run_job(day_close.JOB, dt.date(2026, 9, 8)) == "already"
    assert "Paused until Wed 09 Sep" in say("/pause 3d")
    assert run_job(evening_confirm.JOB, dt.date(2026, 9, 8)) == "skipped"
    assert fixture_db.latest("messages_out", kind="evening_confirm")["status"] == "dropped_paused"  # type: ignore[index]
    assert run_job(day_close.JOB, dt.date(2026, 9, 9)) == "ok"
    assert days.latest_confirmation(fixture_db, pid, dt.date(2026, 9, 8))["status"] == "paused"  # type: ignore[index]
    assert "Resumed" in say("/resume")
    assert "Not paused" in say("/resume")
    assert "past" in say("/pause 2020-01-01")


def test_today_command(world) -> None:
    _pid, _app, _tg, say, _tap, _run_job = world
    text = say("/today")
    assert text.startswith("<b>Mon 07 Sep</b>") and "Target 2250 kcal" in text
    assert "Day: open" in text and "Training: climb 90 min" in text and "Trend 100.9 kg" in text


def test_dry_run_previews_the_whole_day(world, fixture_db: Db, config, frozen_clock) -> None:
    """A preview asked for in the evening still shows the morning message (nothing is sent)."""
    import datetime as dt

    from tiffin import dryrun

    _pid, app, tg, _say, _tap, _run_job = world
    # the command reads the configured database, so put the seeded rows there
    app.config.paths.ensure()
    dryrun.copy_db(Path(fixture_db.path), app.config.paths.db)
    messages = dryrun.render_day(app, dt.date(2026, 9, 8))
    kinds = [m.kind for m in messages]
    assert "morning_targets" in kinds and "evening_confirm" in kinds
    assert all(m.slug == "owner" for m in messages)
    assert tg.sent == []  # a dry run never touches Telegram
    morning = next(m for m in messages if m.kind == "morning_targets")
    assert morning.text.startswith("<b>Tue 8 Sep</b>") and "2,250 kcal" in morning.text
    evening = next(m for m in messages if m.kind == "evening_confirm")
    assert "As planned" in evening.buttons
    text = dryrun.to_text(messages)
    assert "morning_targets" in text and "buttons:" in text
    html = dryrun.to_html(dt.date(2026, 9, 8), messages)
    assert "<section>" in html and "Dry run 2026-09-08" in html
    assert dryrun.render_day(app, dt.date(2026, 9, 8), person_slug="partner") == []
    # the heading is when it lands on their phone. It was the UTC stamp, so all summer the
    # preview claimed 06:30 for a message that arrives at 07:30, and in winter it was right.
    row = fixture_db.one("SELECT morning_time FROM profiles ORDER BY id DESC LIMIT 1")
    assert row is not None
    assert morning.due_local.endswith(f"{row['morning_time']}:00"), (
        f"a preview headed {morning.due_local} must be London time, not UTC"
    )


def test_answering_the_food_question_leaves_the_training_buttons(world, fixture_db: Db) -> None:
    """The evening message carries two questions on one keyboard.

    Tapping "As planned" cleared the whole markup, so the climb the person did that evening
    could never be recorded from that message and nothing brought the buttons back.
    """
    import datetime as dt

    from tiffin.jobs.evening_confirm import session_part

    pid, _app, _tg, _say, _tap, _run_job = world
    day = dt.date(2026, 9, 7)
    # Monday carries a planned session in this world, so the row exists to be preserved
    _line, buttons = session_part(fixture_db, pid, day)
    assert buttons, "the fixture's Monday has a session to confirm"
    assert all(data.startswith("k:sess") for _label, data in buttons)

    # once the session is decided there is nothing left to keep, and the keyboard goes
    fixture_db.insert("session_matches", person_id=pid, day=day.isoformat(), status="confirmed")
    _line, after = session_part(fixture_db, pid, day)
    assert after == []


def test_a_stale_what_changed_question_stops_swallowing_messages(world, fixture_db: Db) -> None:
    """The "Not quite" question never expired.

    An unrelated message days later was logged as that day's food, silently.
    """
    import datetime as dt
    import json

    pid, _app, tg, say, _tap, _run_job = world
    fixture_db.execute(
        "INSERT INTO conversation_state(person_id, state_json, updated_at) VALUES (?, ?, ?)",
        (
            pid,
            json.dumps({"flow": "offplan", "day": (dt.date(2026, 9, 1)).isoformat()}),
            "2026-09-01 20:00:00",
        ),
    )
    before = fixture_db.scalar("SELECT COUNT(*) FROM day_confirmations")
    say("what are my calories today?")
    assert any("has closed" in m["text"] for m in tg.sent), "it says the question expired"
    assert fixture_db.scalar("SELECT COUNT(*) FROM day_confirmations") == before, (
        "and does not log an unrelated message as that day's food"
    )


def test_both_of_a_days_sessions_are_named(fixture_db, two_persons, frozen_clock) -> None:
    """`Tue ride 60, Tue strength 30` is an ordinary week, and only one of them was shown.

    Reading the first planned row and stopping meant the evening message asked about a day it
    had described wrongly, and the morning message set the day up wrongly too.
    """
    import datetime as dt

    from tiffin import days as days_mod
    from tiffin.jobs import evening_confirm

    pid = two_persons["owner"]
    plan = fixture_db.insert("training_plans", person_id=pid, effective_from="2026-09-01")
    for kind, minutes in (("ride", 60), ("strength", 30)):
        fixture_db.insert(
            "planned_sessions",
            plan_id=plan,
            person_id=pid,
            weekday=1,
            kind=kind,
            target_minutes=minutes,
        )
    tuesday = dt.date(2026, 9, 8)
    rows = days_mod.planned_sessions_on(fixture_db, pid, tuesday)
    assert [r["kind"] for r in rows] == ["ride", "strength"]

    line, buttons = evening_confirm.session_part(fixture_db, pid, tuesday)
    assert "ride 60 min" in line and "strength 30 min" in line
    assert "intervals.icu" in line and "Garmin" not in line, "the box does not talk to Garmin"
    assert buttons, "and it can still be answered"
