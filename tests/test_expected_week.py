"""The missed-slot check against the real scheduler and the real morning and evening jobs.

Synthetic data, a frozen clock, a fake Telegram. Three days in 15-minute steps: the check must
stay empty all the way through (both people, one with a weigh-in, one without, one whose sends
are dropped in dev), and an outage must show up while it lasts and only when it should.
"""

from __future__ import annotations

import datetime as dt

import pytest

from tiffin import body, expected
from tiffin import scheduler as sc
from tiffin.app import App
from tiffin.clock import TZ, FrozenClock
from tiffin.db import Db
from tiffin.jobs import evening_confirm, morning_targets
from tiffin.persons import PersonRegistry
from tiffin.secrets import Secrets

from .fakes import FakeTelegram

START = dt.datetime(2026, 9, 7, 5, 0, tzinfo=TZ)  # a Monday, BST


@pytest.fixture
def week(config, fixture_db: Db, two_persons: dict[str, int], frozen_clock: FrozenClock, tmp_path):
    for pid in two_persons.values():
        fixture_db.insert(
            "profiles",
            person_id=pid,
            effective_from="2026-09-01",
            sex="m",
            birth_date="1990-01-01",
            height_cm=180.0,
            activity_factor=1.5,
            kcal_floor=1800,
        )
    for i in range(15):  # only the owner has been on the scale
        body.record_weigh_in(
            fixture_db,
            two_persons["owner"],
            dt.date(2026, 8, 25) + dt.timedelta(days=i),
            90.0,
            "manual",
        )
    secrets_dir = tmp_path / "s"
    secrets_dir.mkdir()
    (secrets_dir / "telegram-token-dev.txt").write_text("1:t", encoding="utf-8")
    tg = FakeTelegram()
    app = App(config, Secrets(secrets_dir), clock=frozen_clock, transport=tg)
    # something ran before the window, so the instance is not "fresh"
    from tiffin import ledger

    frozen_clock.set_local(START - dt.timedelta(hours=2))
    with ledger.run(fixture_db, "backup", "2026-09-06T0330", 0, clock=frozen_clock) as h:
        h.note("x")

    def run_here(_pid: int, fn) -> None:
        fn(fixture_db)

    class W:
        db = fixture_db
        ids = two_persons

        @staticmethod
        def read(when: dt.datetime) -> list[expected.MissingSlot]:
            """Look without ticking: the process is down."""
            frozen_clock.set_local(when)
            reg = PersonRegistry(fixture_db, config, clock=frozen_clock)
            return expected.missing(
                fixture_db, reg, [morning_targets.JOB, evening_confirm.JOB], frozen_clock
            )

        @staticmethod
        def tick(when: dt.datetime) -> list[expected.MissingSlot]:
            frozen_clock.set_local(when)
            jobs = [morning_targets.JOB, evening_confirm.JOB]
            sc.Scheduler(app, jobs, run_here).tick(fixture_db)
            reg = PersonRegistry(fixture_db, config, clock=frozen_clock)
            return expected.missing(fixture_db, reg, jobs, frozen_clock)

    return W


def steps(first: dt.datetime, last: dt.datetime):
    t = first
    while t <= last:
        yield t
        t += dt.timedelta(minutes=15)


def test_three_ordinary_days_never_cry_wolf(week) -> None:
    for when in steps(START, START + dt.timedelta(days=3, hours=-2)):
        assert week.tick(when) == [], when
    # the jobs really did run, so the silence above is not an empty test
    ran = week.db.scalar(
        "SELECT COUNT(*) FROM run_ledger WHERE job IN ('morning_targets', 'evening_confirm')"
    )
    assert ran >= 6


def test_an_outage_is_listed_while_it_lasts_and_clears_when_the_scheduler_catches_up(week) -> None:
    for when in steps(START, START.replace(hour=6, minute=45)):
        assert week.tick(when) == []
    # the process is down from 06:45; both mornings (06:30 UTC = 07:30 BST) come due and go unrun
    assert week.read(START.replace(hour=7, minute=40)) == []  # inside the grace
    down = week.read(START.replace(hour=8))
    assert {(m.reason, m.state, m.job) for m in down} == {("no_run", "late", "morning_targets")}
    assert {m.person_id for m in down} == set(week.ids.values())
    # back at 10:00, before the 12:00 deadline: the catch-up runs the slots and the list empties
    assert week.tick(START.replace(hour=10)) == []


def test_an_outage_past_the_deadline_stays_listed_as_missed(week) -> None:
    for when in steps(START, START.replace(hour=6, minute=45)):
        assert week.tick(when) == []
    got = week.tick(START.replace(hour=13))  # back after the 12:00 deadline: no catch-up
    assert {(m.reason, m.state) for m in got} == {("no_run", "missed")}
    assert {m.person_id for m in got} == set(week.ids.values())
    # and it stays until the day after tomorrow starts
    assert week.read(START.replace(hour=23)) != []
    assert not any(
        m.slot.startswith("2026-09-07") for m in week.read(START + dt.timedelta(days=2, hours=1))
    )
