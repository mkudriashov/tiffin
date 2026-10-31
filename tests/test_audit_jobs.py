"""Every scheduled job, against the three databases that actually exist.

The jobs are what Tiffin does when nobody is looking, and each one is normally tested
against the world it was written for. These drive ALL of them, enumerated from `all_jobs()`,
against the three states a real database passes through:

  - a person who has paired and not onboarded (her, right now);
  - a person onboarded this morning, with a profile and nothing else;
  - a person mid-flight, with targets, a menu, a plan and a week of days.

A job may skip. What it may not do is raise, write a `failed` row nobody asked for, or send
something to somebody it should not. `run_ledger` is the contract: every run says what it did.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from tiffin import scheduler as sc
from tiffin.app import App
from tiffin.clock import FrozenClock, isoday
from tiffin.db import Db
from tiffin.jobs import all_jobs
from tiffin.secrets import Secrets

from .fakes import FakeTelegram

OWNER_CHAT = 1000001
MONDAY = dt.date(2026, 9, 7)


def onboard(db: Db, pid: int, *, on: dt.date = MONDAY) -> None:
    """Effective the day it is written, which is what finishing the interview actually does.

    slack is what hid the bug: targets are looked up per week, and a profile that predates the
    week start is found where a profile written today is not. Six days in seven, real
    onboarding left the week with no targets at all and the sweep saw nothing.
    """
    db.insert(
        "profiles",
        person_id=pid,
        effective_from=isoday(on),
        sex="m",
        birth_date="1990-03-15",
        height_cm=183.0,
        activity_factor=1.55,
        steps_floor=10000,
        quiet_start="22:30",
        quiet_end="07:00",
        confirm_time="20:30",
        morning_time="07:00",
        nudge_time="16:00",
        rate_pct_per_week=0.7,
        kcal_floor=1800,
        meals_per_day=4,
        pre_bed_protein=1,
        tone="neutral",
        segment_days=json.dumps([[0, 1, 2, 3, 4]]),
        variety_json=json.dumps({"all": "same"}),
    )


def a_week(db: Db, pid: int) -> None:
    """Targets, a training plan and a week of confirmed days."""
    from tiffin import body

    db.insert(
        "targets",
        person_id=pid,
        week_start="2026-09-07",
        kcal=2250,
        protein_g=200,
        fat_g=70,
        carb_g=200,
        fibre_g=30,
        source="formula",
    )
    plan = db.insert("training_plans", person_id=pid, effective_from="2026-09-01")
    db.insert(
        "planned_sessions", plan_id=plan, person_id=pid, weekday=0, kind="climb", target_minutes=90
    )
    for i in range(7):
        day = MONDAY - dt.timedelta(days=7 - i)
        body.record_weigh_in(db, pid, day, 100.5 - i * 0.1, "manual")
        db.insert(
            "day_confirmations",
            person_id=pid,
            day=day.isoformat(),
            status="confirmed",
            basis="none",
            kcal=2250,
            protein_g=200,
            fat_g=70,
            carb_g=200,
            fibre_g=30,
        )


@pytest.fixture
def runner(fixture_db: Db, two_persons, frozen_clock: FrozenClock, config, tmp_path: Path):
    secrets_dir = tmp_path / "s"
    secrets_dir.mkdir()
    (secrets_dir / "telegram-token-dev.txt").write_text("1:t", encoding="utf-8")
    tg = FakeTelegram()
    config.paths.ensure()
    # the backup job copies config.paths.db, so give it the fixture database to find
    import shutil

    shutil.copyfile(fixture_db.path, config.paths.db)
    app = App(
        config,
        Secrets(secrets_dir),
        clock=frozen_clock,
        transport=tg,
        gateway_factory=lambda _a, _d: None,  # no model: every job must cope without one
    )
    pid = two_persons["owner"]

    def run_all(day: dt.date) -> list[tuple[str, str]]:
        """Every job, at every slot it declares for `day`. Returns (job, status) pairs."""
        out: list[tuple[str, str]] = []
        sched = sc.Scheduler(app, list(all_jobs()), lambda p, fn: None)
        prof = fixture_db.one("SELECT * FROM profiles WHERE person_id = ?", (pid,))
        for job in all_jobs():
            target = pid if job.scope == "each" else sc.SYSTEM
            for slot in sc.slots_for(job, target, prof, day, fixture_db):
                out.append((job.name, sched.execute(fixture_db, slot)))
        return out

    return run_all, fixture_db, tg, pid


def statuses(results: list[tuple[str, str]]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for name, status in results:
        out.setdefault(name, set()).add(status)
    return out


# ---------------------------------------------------------------- the three databases
def test_every_job_copes_with_a_person_who_has_not_onboarded(runner) -> None:
    """Her, right now: paired, no profile, no targets, no plan, no menu."""
    run_all, db, _tg, _pid = runner
    results = run_all(MONDAY)
    assert results, "some jobs must at least be due"
    bad = [(n, s) for n, s in results if s == "failed"]
    assert not bad, f"failed with no profile: {bad}"
    detail = db.all("SELECT job, status, error FROM run_ledger WHERE status = 'failed'")
    assert not detail, [dict(r) for r in detail]


def test_every_job_copes_with_a_profile_and_nothing_else(runner) -> None:
    """Onboarded this morning: a profile, and not one row of history."""
    run_all, db, _tg, pid = runner
    onboard(db, pid)
    results = run_all(MONDAY)
    bad = [(n, s) for n, s in results if s == "failed"]
    assert not bad, f"failed on a fresh profile: {bad}"


def test_every_job_copes_with_a_week_of_real_data(runner) -> None:
    run_all, db, _tg, pid = runner
    onboard(db, pid)
    a_week(db, pid)
    results = run_all(MONDAY)
    bad = [(n, s) for n, s in results if s == "failed"]
    assert not bad, f"failed with data: {bad}"
    assert "ok" in statuses(results).get("morning_targets", set())


def test_every_job_copes_on_a_sunday(runner) -> None:
    """The heaviest day: calibration, summary, proposals, the body check and the weekly jobs."""
    run_all, db, _tg, pid = runner
    onboard(db, pid)
    a_week(db, pid)
    results = run_all(dt.date(2026, 9, 13))
    bad = [(n, s) for n, s in results if s == "failed"]
    assert not bad, f"failed on a Sunday: {bad}"
    ran = statuses(results)
    assert {"weekly_calibration", "sunday_summary", "body_check"} <= set(ran), sorted(ran)


def test_every_job_copes_while_paused(runner) -> None:
    """/pause must stop the messages without breaking the data jobs behind them."""
    run_all, db, _tg, pid = runner
    onboard(db, pid)
    a_week(db, pid)
    db.insert(
        "pause_periods",
        person_id=pid,
        start_day=MONDAY.isoformat(),
        end_day="2026-09-30",
        reason="away",
    )
    results = run_all(MONDAY)
    bad = [(n, s) for n, s in results if s == "failed"]
    assert not bad, f"failed while paused: {bad}"


def test_no_job_writes_a_message_to_a_person_with_no_chat_id(runner) -> None:
    """Her, before pairing: nothing may be addressed to somebody who cannot receive it."""
    run_all, db, _tg, pid = runner
    onboard(db, pid)
    a_week(db, pid)
    run_all(MONDAY)
    partner = db.one("SELECT id FROM persons WHERE slug = 'partner'")
    assert partner is not None
    to_partner = db.scalar(
        "SELECT COUNT(*) FROM messages_out WHERE person_id = ? AND status = 'sent'",
        (int(partner["id"]),),
    )
    assert to_partner == 0


# ---------------------------------------------------------------- the ledger contract
def test_every_run_says_what_it_did(runner) -> None:
    """ "Degrade to manual, never to silence" starts here: a run with no detail is a run
    nobody can account for afterwards."""
    run_all, db, _tg, pid = runner
    onboard(db, pid)
    a_week(db, pid)
    run_all(MONDAY)
    rows = db.all("SELECT job, status, detail FROM run_ledger")
    assert rows
    quiet = [
        str(r["job"])
        for r in rows
        if str(r["status"]) in ("ok", "degraded", "skipped") and not str(r["detail"] or "").strip()
    ]
    assert not quiet, f"finished with nothing in the ledger: {sorted(set(quiet))}"


def test_a_second_run_of_the_same_slot_is_refused(runner) -> None:
    """The whole point of the ledger: a restart or a catch-up must not send twice."""
    run_all, db, _tg, pid = runner
    onboard(db, pid)
    a_week(db, pid)
    first = run_all(MONDAY)
    sent = db.scalar("SELECT COUNT(*) FROM messages_out")
    second = run_all(MONDAY)
    assert db.scalar("SELECT COUNT(*) FROM messages_out") == sent, "nothing was sent twice"
    assert len(second) == len(first)


# ---------------------------------------------------------------- what the jobs say
def test_no_scheduled_message_leaks_internal_vocabulary(runner) -> None:
    from .test_audit_surface import JARGON

    run_all, db, tg, pid = runner
    onboard(db, pid)
    a_week(db, pid)
    run_all(MONDAY)
    run_all(dt.date(2026, 9, 13))
    problems = []
    for m in tg.sent:
        low = str(m["text"]).lower()
        problems += [f"{low[:60]}: leaked {w!r}" for w in JARGON if w in low]
    assert not problems, "\n".join(problems)


def test_every_message_job_declares_how_it_handles_quiet_hours() -> None:
    """A message job that never decided is one that will surprise somebody at 03:00."""
    for job in all_jobs():
        if job.kind == "message":
            assert job.quiet in ("defer", "drop", "send"), f"{job.name}: quiet={job.quiet!r}"
