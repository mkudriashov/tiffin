"""run_ledger: one row per (job, slot, person_id). Guards double runs; records the outcome.

Contract (brain run_job.py idiom): a job ends ok, degraded (did less than asked and said so),
failed (raised), or skipped (nothing to do). A job that cannot do its work writes a row
saying so; silence is never an outcome. run_ledger is a state table, so its rows may be
updated in place.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from .clock import DEFAULT_CLOCK, Clock, parse_stamp
from .db import Db
from .errors import TiffinError

DONE_STATUSES = ("ok", "degraded", "skipped")
SYSTEM = 0  # person_id for system and joint jobs


class AlreadyDone(TiffinError):
    """The slot already has a finished (or live) run."""


@dataclass
class RunHandle:
    run_id: str
    job: str
    slot: str
    person_id: int
    status: str = "ok"
    detail: str | None = None
    notes: list[str] = field(default_factory=list)

    def degrade(self, detail: str) -> None:
        self.status = "degraded"
        self.notes.append(detail)
        self.detail = "; ".join(self.notes)

    def skip(self, detail: str) -> None:
        self.status = "skipped"
        self.detail = detail

    def note(self, detail: str) -> None:
        self.notes.append(detail)
        self.detail = "; ".join(self.notes)


def live(
    db: Db,
    job: str,
    slot: str,
    person_id: int = SYSTEM,
    *,
    clock: Clock = DEFAULT_CLOCK,
    stale_after_s: int = 3600,
) -> bool:
    """Is this slot genuinely still running, rather than abandoned by a killed process?

    `ledger.run` has always been willing to take a stale 'running' row over, but the scheduler
    skipped every 'running' row unconditionally - so a reboot or supervisor restart after a hung
    tiffin, left the row saying 'running' for ever and the job was never dispatched again. It
    silently cost that day's menu proposal (and the shopping list built from it), or the
    morning brief, with no alert and no record of a problem. The recovery path existed and was
    tested; nothing could reach it.
    """
    row = db.one(
        "SELECT status, started_at FROM run_ledger WHERE job = ? AND slot = ? AND person_id = ?",
        (job, slot, person_id),
    )
    if row is None or str(row["status"]) != "running":
        return False
    age = (clock.now_utc() - parse_stamp(str(row["started_at"]))).total_seconds()
    return age < stale_after_s


def status_of(db: Db, job: str, slot: str, person_id: int = SYSTEM) -> str | None:
    row = db.one(
        "SELECT status FROM run_ledger WHERE job = ? AND slot = ? AND person_id = ?",
        (job, slot, person_id),
    )
    return None if row is None else str(row[0])


def is_done(db: Db, job: str, slot: str, person_id: int = SYSTEM) -> bool:
    return status_of(db, job, slot, person_id) in DONE_STATUSES


def new_run_id(job: str, clock: Clock = DEFAULT_CLOCK) -> str:
    return f"{job}-{clock.now_utc().strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"


@contextmanager
def run(
    db: Db,
    job: str,
    slot: str,
    person_id: int = SYSTEM,
    *,
    clock: Clock = DEFAULT_CLOCK,
    stale_after_s: int = 3600,
) -> Iterator[RunHandle]:
    """Claim the slot, yield a handle, record the outcome.

    Raises AlreadyDone if the slot finished before or a run is still live and not stale.
    A failed or stale-running row is taken over (the row is replaced).
    """
    existing = db.one(
        "SELECT run_id, status, started_at FROM run_ledger "
        "WHERE job = ? AND slot = ? AND person_id = ?",
        (job, slot, person_id),
    )
    now = clock.now_utc()
    if existing is not None:
        if existing["status"] in DONE_STATUSES:
            raise AlreadyDone(f"{job} {slot} person={person_id} already {existing['status']}")
        if existing["status"] == "running":
            age = (now - parse_stamp(existing["started_at"])).total_seconds()
            if age < stale_after_s:
                raise AlreadyDone(
                    f"{job} {slot} person={person_id} running since {existing['started_at']}"
                )
        db.execute("DELETE FROM run_ledger WHERE run_id = ?", (existing["run_id"],))
    handle = RunHandle(new_run_id(job, clock), job, slot, person_id)
    db.execute(
        "INSERT INTO run_ledger(run_id, job, slot, person_id, started_at, status) "
        "VALUES (?, ?, ?, ?, ?, 'running')",
        (handle.run_id, job, slot, person_id, clock.utc_stamp()),
    )
    try:
        yield handle
    except BaseException as e:
        db.execute(
            "UPDATE run_ledger SET status = 'failed', ended_at = ?, detail = ?, error = ? "
            "WHERE run_id = ?",
            (clock.utc_stamp(), handle.detail, type(e).__name__, handle.run_id),
        )
        raise
    else:
        db.execute(
            "UPDATE run_ledger SET status = ?, ended_at = ?, detail = ? WHERE run_id = ?",
            (handle.status, clock.utc_stamp(), handle.detail, handle.run_id),
        )


#: a run still 'running' after this long belongs to a process that no longer exists
ABANDONED_AFTER_S = 6 * 3600


def close_abandoned(
    db: Db, *, clock: Clock = DEFAULT_CLOCK, older_than_s: int = ABANDONED_AFTER_S
) -> int:
    """Close every run a killed process left 'running'. Returns how many were closed.

    `run` takes a stale row over only when the SAME slot comes round again, and most slots never
    progress on every view of the ledger. A process that dies mid-run
    is a failure, and saying so is the ledger's whole job.
    """
    cutoff = (clock.now_utc() - dt.timedelta(seconds=older_than_s)).strftime("%Y-%m-%d %H:%M:%S")
    rows = db.all(
        "SELECT run_id FROM run_ledger WHERE status = 'running' AND started_at < ?", (cutoff,)
    )
    for r in rows:
        db.execute(
            "UPDATE run_ledger SET status = 'failed', ended_at = ?, "
            "error = 'abandoned: the process stopped before the run finished' WHERE run_id = ?",
            (clock.utc_stamp(), r["run_id"]),
        )
    return len(rows)


def recent(db: Db, *, since: dt.datetime, statuses: tuple[str, ...] | None = None) -> list[dict]:
    sql = "SELECT * FROM run_ledger WHERE started_at >= ?"
    params: list[object] = [since.strftime("%Y-%m-%d %H:%M:%S")]
    if statuses:
        sql += f" AND status IN ({','.join('?' for _ in statuses)})"
        params.extend(statuses)
    return [dict(r) for r in db.all(sql + " ORDER BY started_at DESC", params)]
