"""The missed-slot check: which expected morning and evening messages have not gone.

Fixtures only. The false-alarm cases matter as much as the misses: every legitimate reason not to
send must leave the list empty, or the alarm is ignored on the day it is right.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from dataclasses import replace

import pytest

from tiffin import cli, expected, ledger
from tiffin.app import App
from tiffin.clock import TZ, FrozenClock
from tiffin.db import Db
from tiffin.doctor import run_checks
from tiffin.jobs import all_jobs
from tiffin.persons import PersonRegistry
from tiffin.secrets import Secrets

MONDAY = dt.date(2026, 9, 8)


def at(h: int, m: int = 0, day: dt.date = MONDAY) -> dt.datetime:
    return dt.datetime(day.year, day.month, day.day, h, m, tzinfo=TZ)


def profile(db: Db, pid: int, **over: object) -> None:
    row: dict[str, object] = {
        "person_id": pid,
        "effective_from": "2026-09-01",
        "sex": "m",
        "birth_date": "1990-01-01",
        "height_cm": 180.0,
        "activity_factor": 1.5,
        "kcal_floor": 1800,
    }
    row.update(over)
    db.insert("profiles", **row)


@pytest.fixture
def world(fixture_db: Db, two_persons: dict[str, int], frozen_clock: FrozenClock, config):
    profile(fixture_db, two_persons["owner"])
    profile(fixture_db, two_persons["partner"])
    frozen_clock.set_local(at(6))
    # something ran before, so this is not a fresh install
    with ledger.run(fixture_db, "backup", "2026-09-01T0330", 0, clock=frozen_clock) as h:
        h.note("x")

    class W:
        db = fixture_db
        ids = two_persons
        clock = frozen_clock
        jobs = all_jobs(config)

        @staticmethod
        def registry() -> PersonRegistry:
            return PersonRegistry(fixture_db, config, clock=frozen_clock)

        @staticmethod
        def check() -> list[expected.MissingSlot]:
            return expected.missing(fixture_db, W.registry(), W.jobs, frozen_clock)

        @staticmethod
        def ran(pid: int, job: str, hhmm: str, status: str = "ok", day: dt.date = MONDAY) -> None:
            key = f"{day.isoformat()}T{hhmm.replace(':', '')}"
            with ledger.run(fixture_db, job, key, pid, clock=frozen_clock) as h:
                if status == "skipped":
                    h.skip("nothing new to say")
                elif status == "degraded":
                    h.degrade("send failed")
                elif status == "failed":
                    raise RuntimeError("boom")
                else:
                    h.note("sent")

        @staticmethod
        def message(pid: int, job: str, status: str, day: dt.date = MONDAY) -> None:
            fixture_db.insert(
                "messages_out",
                person_id=pid,
                chat_id=1,
                kind=job,
                due_key=day.isoformat(),
                text_html="x",
                status=status,
                sent_at="2026-09-08 07:30:00" if status == "sent" else None,
            )

    return W


def people_of(items: list[expected.MissingSlot]) -> set[int]:
    return {m.person_id for m in items}


# ------------------------------------------------------------------ nothing owed
def test_a_fresh_install_owes_nothing(fixture_db, two_persons, frozen_clock, config) -> None:
    profile(fixture_db, two_persons["owner"])
    frozen_clock.set_local(at(23))
    reg = PersonRegistry(fixture_db, config, clock=frozen_clock)
    assert expected.missing(fixture_db, reg, all_jobs(config), frozen_clock) == []


def test_before_the_due_time_and_inside_the_grace_nothing_is_listed(world) -> None:
    world.clock.set_local(at(7, 30) + dt.timedelta(minutes=14))
    assert world.check() == []


def test_everything_sent_is_quiet(world) -> None:
    world.clock.set_local(at(23, 59))
    for pid in world.ids.values():
        world.ran(pid, "morning_targets", "07:30")
        world.message(pid, "morning_targets", "sent")
    # evening slots are 20:30 by default
    for pid in world.ids.values():
        world.ran(pid, "evening_confirm", "20:30")
        world.message(pid, "evening_confirm", "sent")
    assert world.check() == []


# ------------------------------------------------------------------ misses
def test_one_person_missed_while_the_other_is_served(world) -> None:
    world.clock.set_local(at(8, 5))
    served, missed = world.ids["owner"], world.ids["partner"]
    world.ran(served, "morning_targets", "07:30")
    world.message(served, "morning_targets", "sent")
    got = world.check()
    assert people_of(got) == {missed}
    only = got[0]
    assert (only.job, only.reason, only.state) == ("morning_targets", "no_run", "late")
    assert only.slot == "2026-09-08T0730" and only.due == "2026-09-08 06:30:00"
    assert set(only.as_dict()) == {"job", "slot", "person_id", "due", "deadline", "reason", "state"}


def test_a_late_slot_becomes_missed_after_its_deadline(world) -> None:
    world.clock.set_local(at(11, 59))
    assert {m.state for m in world.check()} == {"late"}
    world.clock.set_local(at(12, 1))
    got = [m for m in world.check() if m.job == "morning_targets"]
    assert {m.state for m in got} == {"missed"} and len(got) == 2


def test_a_run_that_failed_is_missed_for_good(world) -> None:
    world.clock.set_local(at(8))
    pid = world.ids["owner"]
    with pytest.raises(RuntimeError):
        world.ran(pid, "morning_targets", "07:30", "failed")
    got = [m for m in world.check() if m.person_id == pid]
    assert [(m.reason, m.state) for m in got] == [("run_failed", "missed")]


def test_ok_with_no_message_recorded_is_listed(world) -> None:
    world.clock.set_local(at(8))
    pid = world.ids["owner"]
    world.ran(pid, "morning_targets", "07:30")  # said ok, wrote nothing
    got = [m for m in world.check() if m.person_id == pid]
    assert [(m.reason, m.state) for m in got] == [("ok_without_message", "missed")]


def test_only_failed_sends_is_listed_as_send_failed(world) -> None:
    world.clock.set_local(at(8))
    pid = world.ids["owner"]
    world.ran(pid, "morning_targets", "07:30", "degraded")
    world.message(pid, "morning_targets", "failed")
    got = [m for m in world.check() if m.person_id == pid]
    assert [(m.reason, m.state) for m in got] == [("send_failed", "missed")]


def test_a_run_stuck_for_an_hour_is_late_but_a_fresh_one_is_not(world) -> None:
    pid = world.ids["owner"]
    world.clock.set_local(at(7, 30))
    world.db.execute(
        "INSERT INTO run_ledger(run_id, job, slot, person_id, started_at, status) "
        "VALUES ('r1', 'morning_targets', '2026-09-08T0730', ?, ?, 'running')",
        (pid, world.clock.utc_stamp()),
    )
    world.clock.set_local(at(7, 50))
    assert [m for m in world.check() if m.person_id == pid and m.job == "morning_targets"] == []
    world.clock.set_local(at(8, 45))
    got = [m for m in world.check() if m.person_id == pid and m.job == "morning_targets"]
    assert [(m.reason, m.state) for m in got] == [("stuck", "late")]


def test_yesterdays_missed_evening_stays_listed_today_and_is_gone_the_day_after(world) -> None:
    world.clock.set_local(at(9, 0, MONDAY + dt.timedelta(days=1)))
    assert any(
        m.job == "evening_confirm" and m.slot.startswith("2026-09-08") for m in world.check()
    )
    world.clock.set_local(at(9, 0, MONDAY + dt.timedelta(days=2)))
    assert not any(m.slot.startswith("2026-09-08") for m in world.check())


# ------------------------------------------------------------------ false alarms
def test_a_skip_with_its_reason_is_not_a_miss(world) -> None:
    """A person on messages-only-when-needed, a day already answered, nothing new to say."""
    world.clock.set_local(at(23, 59))
    for pid in world.ids.values():
        world.ran(pid, "morning_targets", "07:30", "skipped")
        world.ran(pid, "evening_confirm", "20:30", "skipped")
    assert world.check() == []


def test_a_paused_person_is_not_a_miss(world) -> None:
    world.clock.set_local(at(23, 59))
    pid = world.ids["partner"]
    world.ran(pid, "morning_targets", "07:30", "skipped")
    world.message(pid, "morning_targets", "dropped_paused")
    world.ran(pid, "evening_confirm", "20:30", "skipped")
    world.message(pid, "evening_confirm", "dropped_paused")
    other = world.ids["owner"]
    for job, hhmm in (("morning_targets", "07:30"), ("evening_confirm", "20:30")):
        world.ran(other, job, hhmm)
        world.message(other, job, "sent")
    assert world.check() == []


def test_a_quiet_window_drop_is_not_a_miss(world) -> None:
    world.clock.set_local(at(23, 59))
    for pid in world.ids.values():
        world.ran(pid, "morning_targets", "07:30")
        world.message(pid, "morning_targets", "dropped_quiet")
        world.ran(pid, "evening_confirm", "20:30")
        world.message(pid, "evening_confirm", "dropped_quiet")
    assert world.check() == []


def test_a_morning_inside_the_quiet_window_is_due_at_its_end(world) -> None:
    pid = world.ids["owner"]
    world.db.insert(
        "profiles",
        person_id=pid,
        effective_from="2026-09-08",
        sex="m",
        birth_date="1990-01-01",
        height_cm=180.0,
        activity_factor=1.5,
        kcal_floor=1800,
        morning_time="06:00",
    )
    world.clock.set_local(at(7, 10))  # quiet ends 07:00; inside its grace
    assert [m for m in world.check() if m.person_id == pid and m.job == "morning_targets"] == []
    world.clock.set_local(at(7, 20))
    got = [m for m in world.check() if m.person_id == pid and m.job == "morning_targets"]
    assert len(got) == 1 and got[0].due == "2026-09-08 06:00:00"


def test_a_person_with_no_chat_id_is_not_owed_messages(world) -> None:
    reg = world.registry()
    real = reg.active
    gone = world.ids["partner"]
    reg.active = lambda: [replace(p, chat_id=None) if p.id == gone else p for p in real()]  # type: ignore[method-assign]
    world.clock.set_local(at(23, 59))
    got = expected.missing(world.db, reg, world.jobs, world.clock)
    assert gone not in people_of(got) and world.ids["owner"] in people_of(got)


def test_a_day_before_the_persons_first_profile_is_not_owed(
    fixture_db, two_persons, frozen_clock, config
) -> None:
    profile(fixture_db, two_persons["owner"])
    profile(fixture_db, two_persons["partner"], effective_from="2026-09-09")
    frozen_clock.set_local(at(6))
    with ledger.run(fixture_db, "backup", "2026-09-01T0330", 0, clock=frozen_clock) as h:
        h.note("x")
    frozen_clock.set_local(at(23, 59))
    reg = PersonRegistry(fixture_db, config, clock=frozen_clock)
    got = expected.missing(fixture_db, reg, all_jobs(config), frozen_clock)
    assert two_persons["partner"] not in people_of(got)
    assert two_persons["owner"] in people_of(got)


def test_slots_before_this_instance_ever_ran_are_not_owed(world) -> None:
    """tracked_since is the first ledger row; this morning's slot (06:30 UTC) predates it."""
    world.db.execute("DELETE FROM run_ledger")
    world.clock.set_local(at(9))  # 08:00 UTC
    with ledger.run(world.db, "backup", "2026-09-08T0330", 0, clock=world.clock) as h:
        h.note("x")
    world.clock.set_local(at(13))
    assert [m for m in world.check() if m.job == "morning_targets"] == []


# ------------------------------------------------------------------ surfaces
def test_status_json_and_doctor_carry_the_list(
    world, config, frozen_clock, monkeypatch, capsys
) -> None:
    config = replace(config, paths=replace(config.paths, db=world.db.path))
    config.paths.root.mkdir(parents=True, exist_ok=True)
    config.data["model"]["provider"] = "none"
    config.data["web"]["enabled"] = False
    secrets = Secrets(config.paths.secrets, {})
    secrets.write("telegram_token_dev", "example-token")
    app = App(config, secrets, clock=frozen_clock, transport=lambda *a, **k: None)  # type: ignore[arg-type]
    world.ran(world.ids["owner"], "morning_targets", "07:30")
    world.message(world.ids["owner"], "morning_targets", "sent")
    world.clock.set_local(at(13))  # the partner's morning is past its 12:00 deadline
    monkeypatch.setattr(cli, "_app_from_env", lambda env: app)
    assert cli.cmd_status(argparse.Namespace(env=None)) == 0
    out = json.loads(capsys.readouterr().out)
    miss = [m for m in out["missed_slots"] if m["job"] == "morning_targets"]
    assert [(m["person_id"], m["state"], m["reason"]) for m in miss] == [
        (world.ids["partner"], "missed", "no_run")
    ]
    text = json.dumps(out["missed_slots"])
    assert "Alex" not in text and "Her" not in text  # ids and times only

    def transport(url, method, data, headers, timeout):
        from tiffin.http import Response

        return Response(200, b'{"ok":true,"result":{"username":"b"}}')

    app.transport = transport  # type: ignore[assignment]
    app._api = None
    checks = {c.name: c for c in run_checks(app, online=False)}
    assert checks["expected messages"].status == "red"
    assert "person id" in checks["expected messages"].detail
    assert "Alex" not in checks["expected messages"].detail


def test_the_doctor_check_is_green_when_nothing_is_missing(world, config, frozen_clock) -> None:
    config = replace(config, paths=replace(config.paths, db=world.db.path))
    config.paths.root.mkdir(parents=True, exist_ok=True)
    config.data["model"]["provider"] = "none"
    config.data["web"]["enabled"] = False
    secrets = Secrets(config.paths.secrets, {})
    secrets.write("telegram_token_dev", "example-token")
    app = App(config, secrets, clock=frozen_clock, transport=lambda *a, **k: None)  # type: ignore[arg-type]
    checks = {c.name: c for c in run_checks(app, online=False)}  # 06:00: nothing is due yet
    assert checks["expected messages"].status == "ok"
