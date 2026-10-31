"""Advisor facts and rules, the Sunday calibration, summary, proposals and lapse, the backup job."""

from __future__ import annotations

import datetime as dt
import json

import pytest

from tiffin import body, energy
from tiffin import scheduler as sc
from tiffin.advisor import facts as facts_mod
from tiffin.advisor import prose, rules
from tiffin.app import App
from tiffin.clock import TZ, FrozenClock
from tiffin.db import Db
from tiffin.jobs import backup, sunday_summary, weekly_calibration
from tiffin.secrets import Secrets
from tiffin.telegram.poller import Poller

from .fakes import FakeTelegram

OWNER_CHAT = 1000001
PARTNER_CHAT = 1000002
SUNDAY = dt.date(2026, 9, 13)


def seed_person(
    db: Db, pid: int, *, weights: list[float], kcal: int = 2250, tone: str = "neutral"
) -> None:
    db.insert(
        "profiles",
        person_id=pid,
        effective_from="2026-08-01",
        sex="m",
        birth_date="1990-03-15",
        height_cm=183.0,
        activity_factor=1.55,
        kcal_floor=1800,
        steps_floor=8000,
        tone=tone,
        goal_weight_kg=87.0,
        goal_date="2027-01-24",
        nudge_time="16:00",
    )
    d0 = dt.date(2026, 8, 24)
    for i, w in enumerate(weights):
        body.record_weigh_in(db, pid, d0 + dt.timedelta(days=i), w, "icu_wellness")
    for ws in ("2026-08-24", "2026-08-31", "2026-09-07"):
        db.insert(
            "targets",
            person_id=pid,
            week_start=ws,
            kcal=kcal,
            protein_g=202,
            fat_g=71,
            carb_g=200,
            fibre_g=30,
            source="formula",
            basis_json=json.dumps({"tdee": 3000.0, "deficit_kcal": 750}),
        )
    for i in range(7):
        day = dt.date(2026, 9, 7) + dt.timedelta(days=i)
        status = "confirmed" if i != 5 else "unconfirmed"
        db.insert(
            "day_confirmations",
            person_id=pid,
            day=day.isoformat(),
            status=status,
            basis="menu",
            kcal=kcal if status == "confirmed" else None,
            protein_g=170 if i < 3 else 205,
            fibre_g=20 if i < 4 else 30,
        )
    plan = db.insert("training_plans", person_id=pid, effective_from="2026-08-01")
    for wd, kind in ((0, "climb"), (2, "strength"), (5, "ropes")):
        db.insert(
            "planned_sessions",
            plan_id=plan,
            person_id=pid,
            weekday=wd,
            kind=kind,
            target_minutes=60,
        )
    db.insert("session_matches", person_id=pid, day="2026-09-07", status="confirmed")


@pytest.fixture
def world(fixture_db: Db, two_persons, frozen_clock: FrozenClock, config, tmp_path):
    frozen_clock.set_local(dt.datetime(2026, 9, 13, 17, 30, tzinfo=TZ))
    owner_id, partner_id = two_persons["owner"], two_persons["partner"]
    # 21 daily weigh-ins, losing ~0.6 kg/week
    seed_person(fixture_db, owner_id, weights=[101.0 - i * 0.085 for i in range(21)])
    seed_person(
        fixture_db, partner_id, weights=[68.0 - i * 0.05 for i in range(21)], kcal=1700, tone="warm"
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

    def tap(data: str) -> str:
        n["i"] += 1
        tg.queue_update(tg.callback_update(n["i"], OWNER_CHAT, data, message_id=n["i"]))
        poller.run_once()
        return tg.sent[-1]["text"]

    def run_job(job: sc.Job, day: dt.date, pid: int | None = None) -> str:
        s = sc.Scheduler(app, [job], lambda p, fn: None)
        target = pid if pid is not None else (0 if job.scope != "each" else owner_id)
        prof = (
            fixture_db.one("SELECT * FROM profiles WHERE person_id = ?", (target,))
            if target
            else None
        )
        slots = sc.slots_for(job, target, prof, day, fixture_db)
        assert slots, f"no slot for {job.name}"
        return s.execute(fixture_db, slots[0])

    return owner_id, partner_id, fixture_db, app, tg, tap, run_job


def test_facts_and_rules(world) -> None:
    owner_id, _partner_id, db, app, _tg, _tap, _run_job = world
    f = facts_mod.compute(db, owner_id, SUNDAY, app.config)
    assert f.week_start == dt.date(2026, 9, 7) and f.week_end == SUNDAY
    assert f.confirmed_days == 6 and f.unconfirmed_days == 1 and f.usable_days == 6
    assert f.intake_mean == 2250.0
    assert f.trend_delta_7 is not None and f.trend_delta_7 < 0
    assert f.protein_short_days == 3 and f.fibre_short_days == 4
    assert f.sessions_done == 1 and f.sessions_planned == 3 and f.strength_planned == 1
    assert f.weeks_in_deficit == 3
    assert f.targets is not None and f.targets.kcal == 2250
    cands = rules.candidates(f)
    kinds = {c.rule for c in cands}
    assert "R5" in kinds and "R6" in kinds  # protein and fibre short
    assert "R1" not in kinds and "R2" not in kinds  # loss under the cap, no calibration history
    assert all(len(prose.reason_line(c)) <= 120 for c in cands)
    fast = facts_mod.Facts(
        owner_id,
        f.week_start,
        f.week_end,
        targets=f.targets,
        weight_kg=100.0,
        trend_delta_14=-2.4,
        weeks_in_deficit=7,
        steps_floor_misses=5,
        profile=f.profile,
    )
    fast_kinds = {c.rule: c for c in rules.candidates(fast)}
    assert fast_kinds["R1"].auto_ok and fast_kinds["R1"].payload["kcal_delta"] == 150
    # R11 retired with the 16:00 steps nudge: nothing left to move
    assert "R11" not in fast_kinds
    guarded = facts_mod.Facts(
        owner_id,
        f.week_start,
        f.week_end,
        targets=f.targets,
        weight_kg=100.0,
        trend_delta_14=-2.4,
        cycle_phase="luteal",
    )
    g = {c.rule for c in rules.candidates(guarded)}
    assert "R9" in g and "R1" not in g


def test_weekly_calibration_writes_rows_and_next_targets(world) -> None:
    owner_id, _partner_id, db, _app, _tg, _tap, run_job = world
    assert run_job(weekly_calibration.JOB, SUNDAY) == "ok"
    cal = db.latest("tdee_calibrations", person_id=owner_id)
    assert cal is not None and cal["usable_days"] == 6 and cal["tdee_measured"] is not None
    assert cal["confidence"] == "low"
    nxt = db.one(
        "SELECT * FROM v_targets_latest WHERE person_id = ? AND week_start = '2026-09-14'",
        (owner_id,),
    )
    assert nxt is not None and nxt["source"] in ("calibration", "auto_adjust")
    basis = json.loads(nxt["basis_json"])
    assert basis["confidence"] == "low" and abs(nxt["kcal"] - 2250) <= 150
    assert json.loads(db.state_get(f"menu_bias:{owner_id}") or "[]") == ["protein", "fibre"]
    assert db.state_get(f"sunday_confidence:{owner_id}") == "low"
    assert run_job(weekly_calibration.JOB, SUNDAY) == "already"


def test_sunday_summary_has_own_block_and_partner_trend_only(world) -> None:
    _owner_id, partner_id, db, app, tg, _tap, run_job = world
    run_job(weekly_calibration.JOB, SUNDAY)
    run_job(weekly_calibration.JOB, SUNDAY, pid=partner_id)
    tg.sent.clear()
    app.config.data["env"] = {"default": "prod"}
    assert run_job(sunday_summary.JOB, SUNDAY) == "ok"
    texts = {m["chat_id"]: m["text"] for m in tg.sent if m.get("text", "").startswith("<b>Week to")}
    assert OWNER_CHAT in texts  # dev env drops her message; his still carries her block
    mine = texts[OWNER_CHAT]
    assert "Trend" in mine and "Days recorded: 6, 1 not answered" in mine and "Sessions 1/3" in mine
    assert "Next week:" in mine and "Formula still leads" in mine
    assert "Her: trend" in mine and "sessions 1/3" in mine
    assert "unconfirmed" not in mine.split("Her:")[1]  # never the partner's misses
    assert len(mine.splitlines()) <= 10
    dropped = db.latest("messages_out", kind="sunday_summary", person_id=partner_id)
    assert dropped is not None and dropped["status"] == "dropped_dev"


def test_backup_job(world) -> None:
    _owner_id, _partner_id, _db, app, _tg, _tap, run_job = world
    app.config.paths.ensure()
    Db.open(app.config.paths.db, clock=app.clock).close()
    assert run_job(backup.JOB, SUNDAY) == "ok"
    last = json.loads((app.config.paths.state / "backup_last.json").read_text(encoding="utf-8"))
    assert last["ok"] and last["day"] == "2026-09-13"
    assert (app.config.paths.backups / "2026-09-13" / "manifest.json").exists()


def test_energy_calibration_feeds_from_facts(world) -> None:
    owner_id, _partner_id, db, app, _tg, _tap, _run_job = world
    f = facts_mod.compute(db, owner_id, SUNDAY, app.config)
    cal = energy.calibrate_tdee(f.day_intakes, f.trend_delta_7, 3000.0, 0)
    assert isinstance(cal, energy.Calibration) and cal.usable_days == 6
