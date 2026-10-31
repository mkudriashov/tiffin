"""Latest-row reads, created_at fill, transactions, and the v_* views."""

from __future__ import annotations

import sqlite3

import pytest

from tiffin.db import Db, latest


def test_insert_fills_created_at_from_clock(fixture_db: Db, two_persons: dict[str, int]) -> None:
    row = fixture_db.latest("persons", slug="owner")
    assert row is not None
    assert row["created_at"] == "2026-09-07 19:30:00"  # 20:30 BST == 19:30 UTC


def test_latest_returns_newest_row_for_keys(fixture_db: Db, two_persons: dict[str, int]) -> None:
    pid = two_persons["owner"]
    for w in (100.5, 100.2, 100.9):
        fixture_db.insert(
            "weigh_ins", person_id=pid, day="2026-09-07", weight_kg=w, source="manual"
        )
    fixture_db.insert(
        "weigh_ins", person_id=pid, day="2026-09-06", weight_kg=101.0, source="manual"
    )
    got = fixture_db.latest("weigh_ins", person_id=pid, day="2026-09-07")
    assert got is not None and got["weight_kg"] == 100.9
    assert latest(fixture_db.con, "weigh_ins", person_id=pid, day="2026-09-05") is None
    newest = fixture_db.latest("weigh_ins")
    assert newest is not None and newest["day"] == "2026-09-06"


def test_latest_rejects_bad_identifiers(fixture_db: Db) -> None:
    with pytest.raises(ValueError):
        fixture_db.latest("weigh_ins; DROP TABLE persons", person_id=1)
    with pytest.raises(ValueError):
        fixture_db.latest("weigh_ins", **{"day = '' OR 1": 1})


def test_tx_rolls_back_on_error(fixture_db: Db, two_persons: dict[str, int]) -> None:
    pid = two_persons["owner"]
    with pytest.raises(sqlite3.IntegrityError), fixture_db.tx():
        fixture_db.insert(
            "weigh_ins", person_id=pid, day="2026-09-07", weight_kg=100.0, source="manual"
        )
        fixture_db.insert(
            "weigh_ins", person_id=pid, day="2026-09-07", weight_kg=10.0, source="manual"
        )
    assert fixture_db.scalar("SELECT COUNT(*) FROM weigh_ins") == 0


def test_tx_commits(fixture_db: Db, two_persons: dict[str, int]) -> None:
    pid = two_persons["owner"]
    with fixture_db.tx():
        fixture_db.insert(
            "weigh_ins", person_id=pid, day="2026-09-07", weight_kg=100.0, source="manual"
        )
    other = Db(fixture_db.path)
    try:
        assert other.scalar("SELECT COUNT(*) FROM weigh_ins") == 1
    finally:
        other.close()


def test_v_day_latest_takes_the_correction(fixture_db: Db, two_persons: dict[str, int]) -> None:
    pid = two_persons["owner"]
    fixture_db.insert(
        "day_confirmations", person_id=pid, day="2026-09-07", status="unconfirmed", basis="none"
    )
    fixture_db.insert(
        "day_confirmations",
        person_id=pid,
        day="2026-09-07",
        status="confirmed",
        basis="menu",
        kcal=2100,
    )
    fixture_db.insert(
        "day_confirmations",
        person_id=pid,
        day="2026-09-06",
        status="confirmed",
        basis="menu",
        kcal=2300,
    )
    rows = fixture_db.all(
        "SELECT day, status, kcal FROM v_day_latest WHERE person_id = ? ORDER BY day", (pid,)
    )
    assert [(r["day"], r["status"], r["kcal"]) for r in rows] == [
        ("2026-09-06", "confirmed", 2300),
        ("2026-09-07", "confirmed", 2100),
    ]


def test_v_targets_latest_and_profile_current(fixture_db: Db, two_persons: dict[str, int]) -> None:
    pid = two_persons["owner"]
    for kcal, src in ((2400, "formula"), (2250, "calibration")):
        fixture_db.insert(
            "targets",
            person_id=pid,
            week_start="2026-09-07",
            kcal=kcal,
            protein_g=200,
            fat_g=75,
            carb_g=200,
            fibre_g=30,
            source=src,
        )
    got = fixture_db.one("SELECT kcal, source FROM v_targets_latest WHERE person_id = ?", (pid,))
    assert got is not None and (got["kcal"], got["source"]) == (2250, "calibration")
    prof = {
        "person_id": pid,
        "sex": "m",
        "birth_date": "1990-01-01",
        "height_cm": 183.0,
        "activity_factor": 1.5,
        "kcal_floor": 1800,
    }
    fixture_db.insert("profiles", effective_from="2026-09-01", steps_floor=8000, **prof)
    fixture_db.insert("profiles", effective_from="2026-09-05", steps_floor=9000, **prof)
    fixture_db.insert("profiles", effective_from="2099-01-01", steps_floor=1, **prof)
    cur = fixture_db.one("SELECT steps_floor FROM v_profile_current WHERE person_id = ?", (pid,))
    assert cur is not None and cur["steps_floor"] == 9000


def test_v_week_summary_counts(fixture_db: Db, two_persons: dict[str, int]) -> None:
    pid = two_persons["owner"]
    days = [
        "2026-09-07",
        "2026-09-08",
        "2026-09-09",
        "2026-09-10",
        "2026-09-11",
        "2026-09-12",
        "2026-09-13",
    ]
    for i, d in enumerate(days):
        status = "confirmed" if i < 5 else ("unconfirmed" if i == 5 else "paused")
        fixture_db.insert("day_confirmations", person_id=pid, day=d, status=status, basis="menu")
    fixture_db.insert(
        "estimates",
        person_id=pid,
        day="2026-09-12",
        description="kebab",
        kcal=900,
        status="accepted",
    )
    w0 = fixture_db.insert(
        "weigh_ins", person_id=pid, day="2026-09-06", weight_kg=101.0, source="manual"
    )
    w1 = fixture_db.insert(
        "weigh_ins", person_id=pid, day="2026-09-13", weight_kg=100.2, source="manual"
    )
    fixture_db.insert(
        "trend_points",
        person_id=pid,
        day="2026-09-06",
        trend_kg=101.0,
        alpha=0.1,
        n=1,
        weigh_in_id=w0,
    )
    fixture_db.insert(
        "trend_points",
        person_id=pid,
        day="2026-09-13",
        trend_kg=100.3,
        alpha=0.1,
        n=2,
        weigh_in_id=w1,
    )
    plan = fixture_db.insert("training_plans", person_id=pid, effective_from="2026-09-01")
    for wd, kind in ((0, "climb"), (2, "ride"), (5, "ropes"), (6, "rest")):
        fixture_db.insert(
            "planned_sessions",
            plan_id=plan,
            person_id=pid,
            weekday=wd,
            kind=kind,
            target_minutes=60,
        )
    fixture_db.insert("session_matches", person_id=pid, day="2026-09-07", status="confirmed")
    fixture_db.insert("session_matches", person_id=pid, day="2026-09-09", status="matched")
    fixture_db.insert("session_matches", person_id=pid, day="2026-09-09", status="rejected")
    row = fixture_db.one(
        "SELECT * FROM v_week_summary WHERE person_id = ? AND week_start = '2026-09-07'", (pid,)
    )
    assert row is not None
    assert row["confirmed_days"] == 5
    assert row["usable_days"] == 6
    assert row["paused_days"] == 1
    assert round(row["trend_delta"], 2) == -0.7
    assert row["sessions_done"] == 1  # the 09-09 match was corrected to rejected by a later row
    assert row["sessions_planned"] == 3
