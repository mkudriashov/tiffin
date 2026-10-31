"""Scheduler: due slots, catch-up windows, quiet deferral, DST, pause, double-run guards."""

from __future__ import annotations

import datetime as dt
import sqlite3

import pytest

from tiffin import ledger
from tiffin import scheduler as sc
from tiffin.app import App
from tiffin.clock import TZ, FrozenClock
from tiffin.db import Db
from tiffin.jobs import all_jobs
from tiffin.persons import Person
from tiffin.secrets import Secrets

from .fakes import FakeTelegram, read_notices


def person(pid: int, chat_id: int | None = 5) -> Person:
    return Person(pid, "owner", "Alex", "neutral", chat_id, None, False)


def profile_row(db: Db, pid: int, **over: object) -> sqlite3.Row:
    base: dict[str, object] = {
        "person_id": pid,
        "effective_from": "2026-09-01",
        "sex": "m",
        "birth_date": "1990-01-01",
        "height_cm": 183.0,
        "activity_factor": 1.5,
        "kcal_floor": 1800,
    }
    base.update(over)
    rid = db.insert("profiles", **base)
    row = db.one("SELECT * FROM profiles WHERE id = ?", (rid,))
    assert row is not None
    return row


def noop(ctx: sc.JobContext) -> None:
    ctx.handle.note("ran")


@pytest.fixture
def pid(two_persons: dict[str, int]) -> int:
    return two_persons["owner"]


def make_app(config, frozen_clock: FrozenClock, tmp_path) -> tuple[App, FakeTelegram]:
    secrets_dir = tmp_path / "s"
    secrets_dir.mkdir(exist_ok=True)
    (secrets_dir / "telegram-token-dev.txt").write_text("1:t", encoding="utf-8")
    tg = FakeTelegram()
    return App(config, Secrets(secrets_dir), clock=frozen_clock, transport=tg), tg


def test_configured_job_times_are_used(config) -> None:
    config.data["schedule"].update(
        {
            "icu_sync_minute": 25,
            "day_close": "01:15",
            "menu_propose_time": "17:45",
            "shopping_list_time": "16:30",
            "shopping_joint_time": "17:00",
            "weekly_calibration": "15:30",
            "sunday_summary": "20:00",
            "backup": "04:00",
        }
    )
    jobs = {job.name: job for job in all_jobs(config)}
    day = dt.date(2026, 9, 7)
    assert jobs["icu_sync"].times(None, day)[0] == "00:25"
    assert jobs["day_close"].times(None, day) == ["01:15"]
    assert jobs["menu_propose"].times(None, day) == ["12:00", "17:45"]
    assert jobs["shopping_list"].times(None, day) == ["16:30"]
    assert jobs["shopping_joint"].times(None, day) == ["17:00"]
    assert jobs["weekly_calibration"].times(None, day) == ["15:30"]
    assert jobs["sunday_summary"].times(None, day) == ["20:00"]
    assert jobs["backup"].times(None, day) == ["04:00"]


def test_daily_job_due_and_catch_up_window(fixture_db: Db, pid: int) -> None:
    prof = profile_row(fixture_db, pid, morning_time="07:30")
    job = sc.Job(
        "morning_targets",
        "each",
        "message",
        sc.daily("morning_time", "07:30"),
        noop,
        catch_up="12:00",
    )
    persons = [(person(pid), prof)]
    day = dt.date(2026, 9, 8)
    assert sc.due_slots([job], persons, dt.datetime(2026, 9, 8, 7, 29, tzinfo=TZ)) == []
    due = sc.due_slots([job], persons, dt.datetime(2026, 9, 8, 7, 30, tzinfo=TZ))
    assert [s.key for s in due] == ["2026-09-08T0730"]
    assert due[0].deadline == dt.datetime(2026, 9, 8, 12, 0, tzinfo=TZ)
    assert sc.due_slots([job], persons, dt.datetime(2026, 9, 8, 11, 59, tzinfo=TZ))[0].day == day
    assert sc.due_slots([job], persons, dt.datetime(2026, 9, 8, 12, 1, tzinfo=TZ)) == []


def test_always_catch_up_includes_yesterday(fixture_db: Db, pid: int) -> None:
    prof = profile_row(fixture_db, pid)
    job = sc.Job("day_close", "each", "data", sc.fixed("00:05"), noop, catch_up="always")
    due = sc.due_slots([job], [(person(pid), prof)], dt.datetime(2026, 9, 8, 15, 0, tzinfo=TZ))
    # "always" means always: one day of lookback made the contract on `Slot.deadline` ("None =
    # always run once due") unkeepable, so a box down over a weekend silently lost that week's
    # calibration, proposals and menu bias. ALWAYS_LOOKBACK_DAYS covers an outage plus the
    # morning somebody notices. A slot WITH a deadline is still finished when it passes, which
    # test_hourly_slots_and_none_catch_up below proves.
    assert [s.key for s in due] == [
        "2026-09-05T0005",
        "2026-09-06T0005",
        "2026-09-07T0005",
        "2026-09-08T0005",
    ]


def test_hourly_slots_and_none_catch_up(fixture_db: Db, pid: int) -> None:
    prof = profile_row(fixture_db, pid)
    job = sc.Job("icu_sync", "each", "data", sc.hourly(10), noop, catch_up="none")
    due = sc.due_slots([job], [(person(pid), prof)], dt.datetime(2026, 9, 8, 15, 11, tzinfo=TZ))
    assert [s.key for s in due] == ["2026-09-08T1510"]
    later = dt.datetime(2026, 9, 8, 15, 13, tzinfo=TZ)
    assert sc.due_slots([job], [(person(pid), prof)], later) == []


def test_quiet_window_defers_message_jobs_only(fixture_db: Db, pid: int) -> None:
    prof = profile_row(
        fixture_db, pid, quiet_start="22:30", quiet_end="07:00", morning_time="06:30"
    )
    msg = sc.Job(
        "morning_targets",
        "each",
        "message",
        sc.daily("morning_time", "07:30"),
        noop,
        catch_up="12:00",
    )
    data = sc.Job("icu_sync", "each", "data", sc.fixed("23:30"), noop)
    slot = sc.slots_for(msg, pid, prof, dt.date(2026, 9, 8))[0]
    assert slot.due_local == dt.datetime(2026, 9, 8, 7, 0, tzinfo=TZ)
    late = sc.slots_for(data, pid, prof, dt.date(2026, 9, 8))[0]
    assert late.due_local == dt.datetime(2026, 9, 8, 23, 30, tzinfo=TZ)
    night_msg = sc.Job("x", "each", "message", sc.fixed("23:00"), noop)
    deferred = sc.slots_for(night_msg, pid, prof, dt.date(2026, 9, 8))[0]
    assert deferred.due_local == dt.datetime(2026, 9, 9, 7, 0, tzinfo=TZ)
    assert sc.in_quiet(prof, dt.datetime(2026, 9, 8, 23, 0, tzinfo=TZ))
    assert not sc.in_quiet(prof, dt.datetime(2026, 9, 8, 12, 0, tzinfo=TZ))


def test_dst_gap_slot_is_skipped_and_ambiguous_runs_once(fixture_db: Db, pid: int) -> None:
    prof = profile_row(fixture_db, pid, quiet_start="00:00", quiet_end="00:00")
    job = sc.Job("x", "each", "data", sc.fixed("01:30"), noop)
    assert sc.slots_for(job, pid, prof, dt.date(2026, 3, 29)) == []
    autumn = sc.slots_for(job, pid, prof, dt.date(2026, 10, 25))
    assert len(autumn) == 1 and autumn[0].due_local.fold == 0


def test_weekday_filter_and_scopes(fixture_db: Db, pid: int) -> None:
    prof = profile_row(fixture_db, pid)
    sunday = sc.Job(
        "sunday_summary", "joint", "message", sc.fixed("19:00"), noop, days=frozenset({6})
    )
    sat = dt.datetime(2026, 9, 12, 20, 0, tzinfo=TZ)
    sun = dt.datetime(2026, 9, 13, 20, 0, tzinfo=TZ)
    assert sc.due_slots([sunday], [(person(pid), prof)], sat) == []
    due = sc.due_slots([sunday], [(person(pid), prof)], sun)
    assert len(due) == 1 and due[0].person_id == 0
    nochat = sc.Job("m", "each", "message", sc.fixed("08:00"), noop)
    assert sc.due_slots([nochat], [(person(pid, chat_id=None), prof)], sun) == []
    assert sc.due_slots([nochat], [(person(pid), None)], sun) == []


def test_execute_claims_slot_once_and_records(
    fixture_db: Db,
    pid: int,
    frozen_clock: FrozenClock,
    config,
    tmp_path,
) -> None:
    profile_row(fixture_db, pid)
    app, _ = make_app(config, frozen_clock, tmp_path)
    app.open_db = lambda: fixture_db  # type: ignore[method-assign]
    ran: list[str] = []

    def job_run(ctx: sc.JobContext) -> None:
        ran.append(ctx.slot)
        ctx.handle.note("ok")

    job = sc.Job("t", "each", "data", sc.fixed("20:00"), job_run, catch_up="+1h")
    dispatched: list = []
    s = sc.Scheduler(app, [job], lambda p, fn: dispatched.append((p, fn)), tick_s=1)
    assert [x.key for x in s.tick(fixture_db)] == ["2026-09-07T2000"]  # frozen at 20:30
    assert s.tick(fixture_db) == []  # inflight guard
    _p, fn = dispatched[0]
    assert fn(fixture_db) == "ok"
    assert ran == ["2026-09-07T2000"]
    assert ledger.status_of(fixture_db, "t", "2026-09-07T2000", pid) == "ok"
    assert s.tick(fixture_db) == []  # done guard


def test_paused_person_message_job_is_dropped(
    fixture_db: Db,
    pid: int,
    frozen_clock: FrozenClock,
    config,
    tmp_path,
) -> None:
    prof = profile_row(fixture_db, pid)
    fixture_db.insert("pause_periods", person_id=pid, start_day="2026-09-01", end_day="2026-09-30")
    app, tg = make_app(config, frozen_clock, tmp_path)
    job = sc.Job("morning_targets", "each", "message", sc.fixed("07:30"), noop, catch_up="always")
    s = sc.Scheduler(app, [job], lambda p, fn: None)
    slot = sc.slots_for(job, pid, prof, dt.date(2026, 9, 7))[0]
    assert s.execute(fixture_db, slot) == "skipped"
    row = fixture_db.latest("messages_out")
    assert row is not None and row["status"] == "dropped_paused" and tg.sent == []
    assert ledger.status_of(fixture_db, "morning_targets", slot.key, pid) == "skipped"


def test_failed_job_alerts_admin(
    fixture_db: Db,
    pid: int,
    frozen_clock: FrozenClock,
    config,
    tmp_path,
) -> None:
    prof = profile_row(fixture_db, pid)
    app, tg = make_app(config, frozen_clock, tmp_path)

    def boom(ctx: sc.JobContext) -> None:
        raise RuntimeError("icu down")

    job = sc.Job("icu_sync", "each", "data", sc.fixed("07:10"), boom)
    s = sc.Scheduler(app, [job], lambda p, fn: None)
    slot = sc.slots_for(job, pid, prof, dt.date(2026, 9, 7))[0]
    assert s.execute(fixture_db, slot) == "failed"
    assert ledger.status_of(fixture_db, "icu_sync", slot.key, pid) == "failed"
    assert tg.sent == [], "a failed job is a notice on the page, not a message"
    notes = read_notices(app.config.paths.state)
    assert notes and "icu_sync" in notes[-1]["text"]
    assert "RuntimeError" in notes[-1]["text"]
    assert "icu down" not in notes[-1]["text"]
