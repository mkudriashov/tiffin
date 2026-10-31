"""Session matching: same day, same kind, duration within +/-50%; extras; idempotency."""

from __future__ import annotations

import datetime as dt

import pytest

from tiffin import training
from tiffin.db import Db

MONDAY = dt.date(2026, 9, 7)


@pytest.fixture
def pid(two_persons: dict[str, int]) -> int:
    return two_persons["owner"]


def plan(db: Db, pid: int, effective: str, *sessions: tuple[int, str, int]) -> dict[str, int]:
    plan_id = db.insert("training_plans", person_id=pid, effective_from=effective)
    ids: dict[str, int] = {}
    for weekday, kind, minutes in sessions:
        ids[f"{weekday}:{kind}"] = db.insert(
            "planned_sessions",
            plan_id=plan_id,
            person_id=pid,
            weekday=weekday,
            kind=kind,
            target_minutes=minutes,
        )
    return ids


def act(
    db: Db,
    pid: int,
    icu_id: str,
    kind: str | None,
    moving: int | None,
    elapsed: int | None,
    day: str = "2026-09-07",
) -> None:
    db.insert(
        "icu_activity",
        person_id=pid,
        icu_id=icu_id,
        day=day,
        start_utc=f"{day} 10:00:00",
        icu_type="X",
        kind=kind,
        moving_s=moving,
        elapsed_s=elapsed,
        icu_raw_id=1,
    )


def statuses(db: Db) -> list[tuple[int | None, str | None, str]]:
    return [
        (r["planned_session_id"], r["icu_id"], r["status"])
        for r in db.all("SELECT * FROM session_matches ORDER BY id")
    ]


def test_matching_extra_and_tolerance(fixture_db: Db, pid: int) -> None:
    ids = plan(fixture_db, pid, "2026-09-01", (0, "climb", 90), (0, "strength", 0), (2, "ride", 60))
    act(fixture_db, pid, "climb-ok", "climb", 80 * 60, 95 * 60)
    act(fixture_db, pid, "ride-monday", "ride", 30 * 60, 31 * 60)  # no ride planned on Monday
    act(fixture_db, pid, "gym-any", "strength", 5 * 60, None)  # target 0: any duration
    act(fixture_db, pid, "unmapped", None, 40 * 60, 40 * 60)
    rows = training.match_day(fixture_db, pid, MONDAY)
    assert [(r["icu_id"], r["status"], r["planned_session_id"], r["minutes"]) for r in rows] == [
        ("climb-ok", "matched", ids["0:climb"], 80),
        ("gym-any", "matched", ids["0:strength"], 5),
        ("ride-monday", "extra", None, 30),
        ("unmapped", "extra", None, 40),
    ]
    assert all(r["id"] for r in rows) and all(r["day"] == "2026-09-07" for r in rows)
    assert training.match_day(fixture_db, pid, MONDAY) == []  # idempotent
    assert len(statuses(fixture_db)) == 4


def test_duration_band_uses_moving_or_elapsed(fixture_db: Db, pid: int) -> None:
    ids = plan(fixture_db, pid, "2026-09-01", (0, "climb", 60))
    act(fixture_db, pid, "too-long", "climb", 100 * 60, 100 * 60)  # 91..the band is 30..90
    act(fixture_db, pid, "elapsed-in-band", "climb", 20 * 60, 45 * 60)
    rows = training.match_day(fixture_db, pid, MONDAY)
    assert [(r["icu_id"], r["status"]) for r in rows] == [
        ("elapsed-in-band", "matched"),
        ("too-long", "extra"),
    ]
    assert rows[0]["planned_session_id"] == ids["0:climb"]
    assert training.duration_ok(60, 29 * 60, None) is False
    assert training.duration_ok(60, 30 * 60, None) and training.duration_ok(60, 90 * 60, None)
    assert training.duration_ok(60, None, None) is False and training.duration_ok(0, None, None)


def test_one_planned_session_matches_once_and_rejection_is_not_reinserted(
    fixture_db: Db, pid: int
) -> None:
    ids = plan(fixture_db, pid, "2026-09-01", (0, "climb", 90))
    act(fixture_db, pid, "a", "climb", 85 * 60, 85 * 60)
    act(fixture_db, pid, "b", "climb", 88 * 60, 88 * 60)
    assert [(r["icu_id"], r["status"]) for r in training.match_day(fixture_db, pid, MONDAY)] == [
        ("a", "matched"),
        ("b", "extra"),
    ]
    fixture_db.insert(
        "session_matches",
        person_id=pid,
        day="2026-09-07",
        planned_session_id=ids["0:climb"],
        icu_id="a",
        status="rejected",
    )
    rows = training.match_day(fixture_db, pid, MONDAY)
    # The rejected pair is never re-inserted; the freed session takes b, and a - now matching
    # nothing - becomes an extra. One pass converges; the next call writes nothing.
    assert [(r["icu_id"], r["status"], r["planned_session_id"]) for r in rows] == [
        ("b", "matched", ids["0:climb"]),
        ("a", "extra", None),
    ]
    assert training.match_day(fixture_db, pid, MONDAY) == []
    assert statuses(fixture_db) == [
        (ids["0:climb"], "a", "matched"),
        (None, "b", "extra"),
        (ids["0:climb"], "a", "rejected"),
        (ids["0:climb"], "b", "matched"),
        (None, "a", "extra"),
    ]


def test_latest_plan_in_force_and_rest_days(fixture_db: Db, pid: int) -> None:
    plan(fixture_db, pid, "2026-08-01", (0, "ride", 60))
    new = plan(fixture_db, pid, "2026-09-07", (0, "climb", 60), (1, "rest", 0))
    plan(fixture_db, pid, "2026-10-01", (0, "swim", 60))  # not yet in force
    act(fixture_db, pid, "climb", "climb", 60 * 60, 60 * 60)
    act(fixture_db, pid, "ride", "ride", 60 * 60, 60 * 60)
    rows = training.match_day(fixture_db, pid, MONDAY)
    assert [(r["icu_id"], r["status"], r["planned_session_id"]) for r in rows] == [
        ("climb", "matched", new["0:climb"]),
        ("ride", "extra", None),
    ]
    act(fixture_db, pid, "tue", "climb", 60 * 60, 60 * 60, day="2026-09-08")
    tue = training.match_day(fixture_db, pid, dt.date(2026, 9, 8))
    assert [(r["icu_id"], r["status"]) for r in tue] == [("tue", "extra")]
    assert training.match_day(fixture_db, pid, dt.date(2026, 9, 9)) == []
    assert [
        r["kind"] for r in training.planned_sessions(fixture_db, pid, dt.date(2026, 9, 8))
    ] == []
