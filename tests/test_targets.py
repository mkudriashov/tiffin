"""Targets rows from profile + weight, trend maintenance, and the morning_targets job."""

from __future__ import annotations

import datetime as dt
import json

import pytest

from tiffin import body, targets
from tiffin import scheduler as sc
from tiffin.app import App
from tiffin.clock import FrozenClock
from tiffin.db import Db
from tiffin.jobs import morning_targets
from tiffin.secrets import Secrets

from .fakes import FakeTelegram

OWNER_CHAT = 1000001


def add_profile(db: Db, pid: int, **over: object) -> int:
    base = {
        "person_id": pid,
        "effective_from": "2026-09-01",
        "sex": "m",
        "birth_date": "1990-03-15",
        "height_cm": 183.0,
        "activity_factor": 1.55,
        "kcal_floor": 1800,
        "steps_floor": 9000,
        "goal_weight_kg": 87.0,
        "goal_date": "2027-01-24",
    }
    base.update(over)
    return db.insert("profiles", **base)


def test_weigh_in_trend_and_delta(fixture_db: Db, two_persons) -> None:
    pid = two_persons["owner"]
    d0 = dt.date(2026, 9, 1)
    for i, w in enumerate([101.0, 100.2, 100.8, 99.9, 100.4, 100.0, 99.6, 99.4]):
        body.record_weigh_in(fixture_db, pid, d0 + dt.timedelta(days=i), w, "manual")
    pts = body.trend_points(fixture_db, pid)
    assert len(pts) == 8 and pts[0].trend_kg == 101.0 and pts[-1].n == 8
    assert body.latest_trend(fixture_db, pid, dt.date(2026, 9, 8)).day == dt.date(2026, 9, 8)  # type: ignore[union-attr]
    delta = body.trend_delta(fixture_db, pid, dt.date(2026, 9, 8), 7)
    assert delta is not None and delta < 0
    # same value again on the same day is not a new row; a corrected value is
    wid = body.record_weigh_in(fixture_db, pid, dt.date(2026, 9, 8), 99.4, "manual")
    assert fixture_db.scalar("SELECT COUNT(*) FROM weigh_ins") == 8
    body.record_weigh_in(fixture_db, pid, dt.date(2026, 9, 8), 98.9, "manual")
    assert fixture_db.scalar("SELECT COUNT(*) FROM weigh_ins") == 9
    tp = fixture_db.latest("trend_points", person_id=pid, day="2026-09-08")
    assert tp is not None and tp["weigh_in_id"] != wid
    assert body.days_since_weigh_in(fixture_db, pid, dt.date(2026, 9, 11)) == 3
    assert (
        body.days_since_weigh_in(fixture_db, two_persons["partner"], dt.date(2026, 9, 11)) is None
    )


def test_ensure_targets_formula_and_idempotent(fixture_db: Db, two_persons, config) -> None:
    pid = two_persons["owner"]
    assert (
        targets.ensure_targets(fixture_db, pid, dt.date(2026, 9, 8), config) is None
    )  # no profile
    add_profile(fixture_db, pid)
    assert targets.ensure_targets(fixture_db, pid, dt.date(2026, 9, 8), config) is None  # no weight
    body.record_weigh_in(fixture_db, pid, dt.date(2026, 9, 6), 100.9, "manual")
    row = targets.ensure_targets(fixture_db, pid, dt.date(2026, 9, 8), config)
    assert row is not None and row["week_start"] == "2026-09-07" and row["source"] == "formula"
    basis = json.loads(row["basis_json"])
    assert basis["method"] == "mifflin" and basis["age"] == 36 and basis["weight_kg"] == 100.9
    assert row["protein_g"] == 202 and row["fat_g"] == 71 and row["kcal"] >= 1800
    again = targets.ensure_targets(fixture_db, pid, dt.date(2026, 9, 10), config)
    assert again is not None and again["id"] == row["id"]
    assert fixture_db.scalar("SELECT COUNT(*) FROM targets") == 1
    # a manual row later in the same week wins
    targets.insert_targets(
        fixture_db, pid, dt.date(2026, 9, 9), targets.macros_of(row), "manual", {}
    )
    manual = targets.targets_for(fixture_db, pid, dt.date(2026, 9, 9))
    assert manual is not None and manual["source"] == "manual"


def test_profile_on_and_pause(fixture_db: Db, two_persons) -> None:
    pid = two_persons["owner"]
    add_profile(fixture_db, pid, effective_from="2026-09-01", steps_floor=8000)
    add_profile(fixture_db, pid, effective_from="2026-09-10", steps_floor=10000)
    assert targets.profile_on(fixture_db, pid, dt.date(2026, 9, 5))["steps_floor"] == 8000  # type: ignore[index]
    assert targets.profile_on(fixture_db, pid, dt.date(2026, 9, 10))["steps_floor"] == 10000  # type: ignore[index]
    assert targets.profile_on(fixture_db, pid, dt.date(2026, 8, 1)) is None
    assert not targets.pause_active(fixture_db, pid, dt.date(2026, 9, 5))
    fixture_db.insert("pause_periods", person_id=pid, start_day="2026-09-04", end_day="2026-09-06")
    assert targets.pause_active(fixture_db, pid, dt.date(2026, 9, 5))
    fixture_db.insert("pause_periods", person_id=pid, start_day="2026-09-04", end_day="2026-09-04")
    assert not targets.pause_active(fixture_db, pid, dt.date(2026, 9, 5))  # ended early = new row


def test_morning_targets_job_sends_one_message(
    fixture_db: Db, two_persons, frozen_clock: FrozenClock, config, tmp_path
) -> None:
    pid = two_persons["owner"]
    add_profile(fixture_db, pid, morning_time="07:30")
    body.record_weigh_in(fixture_db, pid, dt.date(2026, 9, 6), 100.9, "manual")
    plan = fixture_db.insert("training_plans", person_id=pid, effective_from="2026-09-01")
    fixture_db.insert(
        "planned_sessions",
        plan_id=plan,
        person_id=pid,
        weekday=0,
        kind="climb",
        target_minutes=90,
        start_time="18:30",
    )
    secrets_dir = tmp_path / "s"
    secrets_dir.mkdir()
    (secrets_dir / "telegram-token-dev.txt").write_text("1:t", encoding="utf-8")
    tg = FakeTelegram()
    app = App(config, Secrets(secrets_dir), clock=frozen_clock, transport=tg)
    s = sc.Scheduler(app, [morning_targets.JOB], lambda p, fn: None)
    slot = sc.slots_for(
        morning_targets.JOB,
        pid,
        targets.profile_on(fixture_db, pid, dt.date(2026, 9, 7)),
        dt.date(2026, 9, 7),
    )[0]
    assert s.execute(fixture_db, slot) == "ok"
    assert len(tg.sent) == 1
    text = tg.sent[0]["text"]
    # two lines since 26 Sep: the target, and the day's food.
    # Training, trend and steps are on the page's Today tab, not in the message.
    assert text.startswith("<b>Mon 7 Sep</b>") and "protein at least 202 g" in text
    assert "No menu for today yet. /cook builds one." in text
    assert len(text.splitlines()) == 2
    for gone in ("Training", "Trend", "Steps floor"):
        assert gone not in text, gone
    assert len(text.splitlines()) <= 10
    assert (
        fixture_db.scalar(
            "SELECT COUNT(*) FROM messages_out WHERE person_id = ? AND kind = 'morning_targets' "
            "AND status = 'sent'",
            (pid,),
        )
        == 1
    )
    assert s.execute(fixture_db, slot) == "already" and len(tg.sent) == 1


# ---------------------------------------------------------------- onboarding on a Wednesday
def test_onboarding_midweek_still_gets_targets(fixture_db: Db, two_persons, config, frozen_clock):
    """The owner onboarded at 07:31 on a Wednesday and had no targets at all.

    Targets are per week, and the lookup asked for the profile in force on that week's MONDAY.
    A profile written today is effective today, so on any day but a Monday there was no
    profile at the week start and ensure_targets returned None - no targets, no morning
    message the next day, and /cook refusing until Sunday. The profile that governs a week is
    the one in force during it, which is why the weight is already looked up at the week's end.
    """
    from tiffin import body, targets

    db, pid = fixture_db, two_persons["owner"]
    wednesday = dt.date(2026, 9, 9)
    assert wednesday.weekday() == 2
    db.insert(
        "profiles",
        person_id=pid,
        effective_from=wednesday.isoformat(),
        sex="m",
        birth_date="1990-03-15",
        height_cm=183.0,
        activity_factor=1.55,
        kcal_floor=1800,
    )
    body.record_weigh_in(db, pid, wednesday, 100.8, "manual")
    row = targets.ensure_targets(db, pid, wednesday, config, clock=frozen_clock)
    assert row is not None, "onboarding on a Wednesday left the week with no targets"
    assert int(row["kcal"]) > 0
    assert str(row["week_start"]) == "2026-09-07", "they belong to the week that contains today"


@pytest.mark.parametrize("weekday", range(7))
def test_targets_appear_whatever_day_you_onboard(
    fixture_db: Db, two_persons, config, frozen_clock, weekday: int
):
    """Every day of the week, not just the one that happened to be tested."""
    from tiffin import body, targets

    db, pid = fixture_db, two_persons["owner"]
    day = dt.date(2026, 9, 7) + dt.timedelta(days=weekday)
    db.insert(
        "profiles",
        person_id=pid,
        effective_from=day.isoformat(),
        sex="m",
        birth_date="1990-03-15",
        height_cm=183.0,
        activity_factor=1.55,
        kcal_floor=1800,
    )
    body.record_weigh_in(db, pid, day, 100.8, "manual")
    assert targets.ensure_targets(db, pid, day, config, clock=frozen_clock) is not None, day
