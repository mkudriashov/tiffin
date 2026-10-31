from __future__ import annotations

import pytest

from tiffin import ledger
from tiffin.clock import FrozenClock
from tiffin.db import Db


def test_ok_run_records_row(fixture_db: Db, frozen_clock: FrozenClock) -> None:
    with ledger.run(fixture_db, "morning_targets", "2026-09-08", 1, clock=frozen_clock) as h:
        h.note("sent 1")
    row = fixture_db.one("SELECT * FROM run_ledger WHERE job = 'morning_targets'")
    assert row is not None
    assert row["status"] == "ok" and row["detail"] == "sent 1" and row["person_id"] == 1
    assert row["started_at"] == "2026-09-07 19:30:00" and row["ended_at"] == "2026-09-07 19:30:00"
    assert ledger.is_done(fixture_db, "morning_targets", "2026-09-08", 1)


def test_second_claim_of_done_slot_raises(fixture_db: Db, frozen_clock: FrozenClock) -> None:
    with ledger.run(fixture_db, "day_close", "2026-09-07", 1, clock=frozen_clock):
        pass
    with (
        pytest.raises(ledger.AlreadyDone),
        ledger.run(fixture_db, "day_close", "2026-09-07", 1, clock=frozen_clock),
    ):
        raise AssertionError("must not run")
    assert fixture_db.scalar("SELECT COUNT(*) FROM run_ledger") == 1


def test_failure_records_error_and_allows_retry(fixture_db: Db, frozen_clock: FrozenClock) -> None:
    with (
        pytest.raises(RuntimeError),
        ledger.run(fixture_db, "icu_sync", "2026-09-07T20", 1, clock=frozen_clock) as h,
    ):
        h.degrade("wellness 429")
        raise RuntimeError("boom")
    row = fixture_db.one("SELECT * FROM run_ledger")
    assert row is not None
    assert (
        row["status"] == "failed"
        and row["error"] == "RuntimeError"
        and row["detail"] == "wellness 429"
    )
    frozen_clock.advance(minutes=5)
    with ledger.run(fixture_db, "icu_sync", "2026-09-07T20", 1, clock=frozen_clock) as h:
        h.degrade("activities 429")
    rows = fixture_db.all("SELECT status, detail FROM run_ledger")
    assert [(r["status"], r["detail"]) for r in rows] == [("degraded", "activities 429")]


def test_live_run_blocks_until_stale(fixture_db: Db, frozen_clock: FrozenClock) -> None:
    fixture_db.execute(
        "INSERT INTO run_ledger(run_id, job, slot, person_id, started_at, status) "
        "VALUES ('r1', 'backup', 'd', 0, ?, 'running')",
        (frozen_clock.utc_stamp(),),
    )
    with (
        pytest.raises(ledger.AlreadyDone, match="running"),
        ledger.run(fixture_db, "backup", "d", clock=frozen_clock),
    ):
        pass
    frozen_clock.advance(hours=2)
    with ledger.run(fixture_db, "backup", "d", clock=frozen_clock) as h:
        h.skip("nothing to do")
    assert ledger.status_of(fixture_db, "backup", "d") == "skipped"
    assert fixture_db.scalar("SELECT COUNT(*) FROM run_ledger") == 1


def test_system_and_person_slots_are_distinct(fixture_db: Db, frozen_clock: FrozenClock) -> None:
    with ledger.run(fixture_db, "sunday_summary", "2026-09-13", clock=frozen_clock):
        pass
    with ledger.run(fixture_db, "sunday_summary", "2026-09-13", 2, clock=frozen_clock):
        pass
    assert fixture_db.scalar("SELECT COUNT(*) FROM run_ledger") == 2
