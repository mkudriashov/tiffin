"""Expected messages that have not gone: the missed-slot check.

"Degrade to manual, never to silence." A run that fails is already in the run ledger and on the
owner's status strip. What nothing reported was the slot that left no trace, a job that said it
was done and recorded no message, or one person being missed while the other is served. This reads
`run_ledger` and `messages_out` only (no schema change, nothing stored) and lists each expected
morning and evening message that has not gone by a grace deadline.

What counts as a legitimate reason not to send is whatever the job itself recorded. A job that has
nothing to say writes a `skipped` run with its reason (nothing new to say, a weekly rhythm, a day
already answered, no menu due, no weight yet); a paused person gets a `dropped_paused` row and a
skipped run; a quiet-window drop is a `dropped_quiet` row. None of those is listed. Not listed
either: a person without a chat id, an erased person, a day before they had a profile, a slot
skipped by a clock change, and any slot before this instance recorded its first run (a fresh
install owes nothing for yesterday).

An older design kept an expected-slots file and one marker file per sent message; this reads the
ledgers instead, which already say why a slot was skipped.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from .clock import UTC, Clock, isoday, parse_stamp, stamp
from .db import Db
from .persons import PersonRegistry
from .scheduler import Job, slots_for

#: the messages every active person is owed a decision about each day
EXPECTED_JOBS: tuple[str, ...] = ("morning_targets", "evening_confirm")
#: how long after its due time a slot may still be on its way (the scheduler ticks every 30 s)
GRACE = dt.timedelta(minutes=15)
#: a run still 'running' this long after it started is stuck; the scheduler retries it after an hour
STUCK = dt.timedelta(minutes=60)
#: used when a job has no catch-up deadline
NO_DEADLINE_AFTER = dt.timedelta(hours=12)
#: today and yesterday: a miss stays listed until the day after
LOOKBACK_DAYS = 1

#: `late`: not gone yet but still inside its catch-up window; `missed`: past it
LATE = "late"
MISSED = "missed"


@dataclass(frozen=True)
class MissingSlot:
    job: str
    slot: str
    person_id: int
    due: str  # UTC space-form
    deadline: str | None  # UTC space-form
    reason: str  # no_run | run_failed | stuck | ok_without_message | send_failed
    state: str  # late | missed

    def as_dict(self) -> dict[str, Any]:
        return {
            "job": self.job,
            "slot": self.slot,
            "person_id": self.person_id,
            "due": self.due,
            "deadline": self.deadline,
            "reason": self.reason,
            "state": self.state,
        }


def _message_state(db: Db, person_id: int, job: str, day: dt.date) -> str:
    """'ok' when a message was sent or deliberately withheld, 'failed' when only failed sends
    exist, 'none' when the job recorded nothing at all."""
    statuses = {
        str(r["status"])
        for r in db.all(
            "SELECT DISTINCT status FROM messages_out WHERE person_id = ? AND kind = ? "
            "AND due_key = ?",
            (person_id, job, isoday(day)),
        )
    }
    if not statuses:
        return "none"
    if statuses - {"failed"}:
        return "ok"  # sent, dropped_quiet, dropped_paused, dropped_dev
    return "failed"


def missing(
    db: Db,
    registry: PersonRegistry,
    jobs: Sequence[Job],
    clock: Clock,
    *,
    names: Sequence[str] = EXPECTED_JOBS,
) -> list[MissingSlot]:
    """Every expected message that has not gone, oldest first. Empty when all is well."""
    from .targets import profile_on  # noqa: PLC0415 (circular)

    first = db.scalar("SELECT MIN(started_at) FROM run_ledger")
    if first is None:
        return []  # nothing has ever run here: nothing is owed
    tracked_since = parse_stamp(str(first))
    now = clock.now_utc()
    today = clock.today_local()
    wanted = [j for j in jobs if j.name in names]
    out: list[MissingSlot] = []
    for person in registry.active():
        if person.chat_id is None:
            continue  # the scheduler does not run person jobs for them either
        for back in range(LOOKBACK_DAYS, -1, -1):
            day = today - dt.timedelta(days=back)
            profile = profile_on(db, person.id, day)
            for job in wanted:
                for s in slots_for(job, person.id, profile, day, db):
                    due = s.due_local.astimezone(UTC)
                    if due < tracked_since or now < due + GRACE:
                        continue
                    deadline = (
                        s.deadline.astimezone(UTC)
                        if s.deadline is not None
                        else due + NO_DEADLINE_AFTER
                    )
                    state = MISSED if now > deadline else LATE
                    reason = _why_not_gone(db, person.id, job.name, s.key, day, now)
                    if reason is None:
                        continue
                    if reason == "stuck":
                        state = LATE
                    elif reason in {"run_failed", "send_failed", "ok_without_message"}:
                        state = MISSED  # the scheduler will not run a finished slot again
                    out.append(
                        MissingSlot(
                            job.name,
                            s.key,
                            person.id,
                            stamp(due),
                            stamp(deadline) if s.deadline is not None else None,
                            reason,
                            state,
                        )
                    )
    out.sort(key=lambda m: (m.due, m.person_id, m.job))
    return out


def _why_not_gone(
    db: Db, person_id: int, job: str, key: str, day: dt.date, now: dt.datetime
) -> str | None:
    row = db.one(
        "SELECT status, started_at FROM run_ledger WHERE job = ? AND slot = ? AND person_id = ?",
        (job, key, person_id),
    )
    if row is None:
        return "no_run"
    status = str(row["status"])
    if status == "skipped":
        return None  # the job said why it had nothing to send
    if status == "running":
        age = now - parse_stamp(str(row["started_at"]))
        return "stuck" if age >= STUCK else None
    if status == "failed":
        return "run_failed"
    seen = _message_state(db, person_id, job, day)  # ok or degraded
    if seen == "ok":
        return None
    return "send_failed" if seen == "failed" else "ok_without_message"


def summary(items: Sequence[MissingSlot]) -> tuple[str, str]:
    """(doctor status, one line) for the missed-slot check."""
    if not items:
        return (
            "ok",
            "every expected morning and evening message has gone or was withheld on purpose",
        )
    missed = [m for m in items if m.state == MISSED]
    people = sorted({m.person_id for m in items})
    line = (
        f"{len(missed)} missed and {len(items) - len(missed)} late, for person id "
        + ", ".join(str(p) for p in people)
        + "; see `tiffin status` (missed_slots)"
    )
    return ("red" if missed else "warn"), line
