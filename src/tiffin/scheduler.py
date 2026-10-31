"""Scheduler: per-person and system jobs on household wall-clock slots.

A Job declares when it is due for a person (`times(profile, day)` -> ["HH:MM", ...]), how long
a missed slot may still run (`catch_up`), and whether it is a message job (quiet window and
pause apply) or a data job. Every slot is claimed through ledger.run(), so run_ledger's
UNIQUE(job, slot, person_id) guards double runs and ux_messages_out_sent guards double sends.
DST: a non-existent spring-forward slot is skipped; an ambiguous autumn slot runs once.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
import threading
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from . import ledger, log
from .clock import TZ, Clock, isoday, local_at
from .db import Db
from .persons import Person

if TYPE_CHECKING:
    from .app import App
    from .telegram.outbox import Outbox

logger = log.get("tiffin.scheduler")

TimesFn = Callable[[sqlite3.Row | None, dt.date], list[str]]
TimesDbFn = Callable[[Db, int, sqlite3.Row | None, dt.date], list[str]]
CatchUp = str | Callable[[sqlite3.Row | None, dt.date, dt.datetime], dt.datetime | None]
SYSTEM = 0


@dataclass
class JobContext:
    app: Any
    db: Db
    job: Job
    person: Person | None
    profile: sqlite3.Row | None
    day: dt.date
    due_local: dt.datetime
    now_local: dt.datetime
    slot: str
    handle: ledger.RunHandle
    outbox: Outbox
    clock: Clock
    paused: bool = False
    dry: bool = False

    @property
    def person_id(self) -> int:
        return self.person.id if self.person else SYSTEM

    @property
    def tone(self) -> str:
        return self.person.tone if self.person else "neutral"


@dataclass(frozen=True)
class Job:
    name: str
    scope: str  # each | joint | system
    kind: str  # message | data
    times: TimesFn
    run: Callable[[JobContext], None]
    catch_up: CatchUp = "always"
    days: frozenset[int] | None = None  # weekdays 0=Mon..6=Sun; None = daily
    paused_ok: bool = False  # run even when the person is paused
    needs_profile: bool = True
    # message jobs and the quiet window:
    #   defer  hold until quiet_end (the default: a morning brief is still wanted on waking)
    #   drop   send nothing; the job itself checks in_quiet and records dropped_quiet
    #   send   deliver anyway, for a time the person chose themselves
    quiet: str = "defer"
    times_db: TimesDbFn | None = None  # times that need the DB
    default_answers: tuple[tuple[str, str], ...] = ()  # optional step and the default in force

    def __post_init__(self) -> None:
        if self.scope not in ("each", "joint", "system") or self.kind not in ("message", "data"):
            raise ValueError(f"bad job {self.name}: scope={self.scope} kind={self.kind}")


@dataclass(frozen=True)
class Slot:
    job: Job
    person_id: int
    day: dt.date
    due_local: dt.datetime
    key: str
    deadline: dt.datetime | None  # None = always run once due


# ---------------------------------------------------------------- helpers
def daily(field_name: str, default: str) -> TimesFn:
    """Times from a profile column (e.g. morning_time), falling back to `default`."""

    def fn(profile: sqlite3.Row | None, day: dt.date) -> list[str]:
        if profile is None:
            return [default]
        value = profile[field_name] if field_name in profile.keys() else None  # noqa: SIM118 (Row:
        # `in` tests values)
        return [str(value or default)]

    return fn


def fixed(*hhmm: str) -> TimesFn:
    return lambda profile, day: list(hhmm)


def hourly(minute: int) -> TimesFn:
    return lambda profile, day: [f"{h:02d}:{minute:02d}" for h in range(24)]


def catch_up_deadline(
    spec: CatchUp, profile: sqlite3.Row | None, day: dt.date, due: dt.datetime
) -> dt.datetime | None:
    if callable(spec):
        return spec(profile, day, due)
    if spec == "always":
        return None
    if spec == "none":
        return due + dt.timedelta(minutes=2)
    if spec.startswith("+"):
        hours = float(spec[1:].rstrip("h"))
        return due + dt.timedelta(hours=hours)
    deadline = local_at(day, spec, due.tzinfo or TZ)
    if deadline is None:
        return due + dt.timedelta(hours=2)
    if deadline < due:  # deadline on the next day (e.g. shopping_joint until Sat 10:00)
        deadline = local_at(day + dt.timedelta(days=1), spec, due.tzinfo or TZ) or deadline
    return deadline


def quiet_window(profile: sqlite3.Row | None) -> tuple[str, str]:
    if profile is None:
        return ("22:30", "07:00")
    return (str(profile["quiet_start"]), str(profile["quiet_end"]))


def _hm(value: str) -> tuple[int, int]:
    h, m = value.split(":")
    return int(h), int(m)


def in_quiet(profile: sqlite3.Row | None, t: dt.datetime) -> bool:
    start, end = quiet_window(profile)
    s, e = _hm(start), _hm(end)
    now = (t.hour, t.minute)
    if s <= e:
        return s <= now < e
    return now >= s or now < e  # crosses midnight


def defer_to_quiet_end(profile: sqlite3.Row | None, t: dt.datetime) -> dt.datetime:
    """The next quiet_end at or after t (same day if the window ends later today, else tomorrow)."""
    _, end = quiet_window(profile)
    candidate = local_at(t.date(), end, t.tzinfo or TZ)
    if candidate is None or candidate <= t:
        candidate = local_at(t.date() + dt.timedelta(days=1), end, t.tzinfo or TZ)
    return candidate or t


def slots_for(
    job: Job,
    person_id: int,
    profile: sqlite3.Row | None,
    day: dt.date,
    db: Db | None = None,
) -> list[Slot]:
    if job.days is not None and day.weekday() not in job.days:
        return []
    if job.needs_profile and profile is None and job.scope == "each":
        return []
    out: list[Slot] = []
    if job.times_db is not None and db is not None:
        hhmms = job.times_db(db, person_id, profile, day)
    else:
        hhmms = job.times(profile, day)
    for hhmm in hhmms:
        due = local_at(day, hhmm, db.clock.timezone if db is not None else TZ)
        if due is None:
            logger.info(
                "slot skipped (DST gap) %s %s %s", job.name, day, hhmm, extra={"event": "dst_skip"}
            )
            continue
        if job.kind == "message" and job.quiet == "defer" and in_quiet(profile, due):
            due = defer_to_quiet_end(profile, due)
        deadline = catch_up_deadline(job.catch_up, profile, day, due)
        key = f"{isoday(day)}T{hhmm.replace(':', '')}"
        out.append(Slot(job, person_id, day, due, key, deadline))
    return out


#: how far back a slot with no catch-up deadline is still worth running. A weekend outage plus
#: the Monday morning somebody notices is the case this covers.
ALWAYS_LOOKBACK_DAYS = 3


def due_slots(
    jobs: Sequence[Job],
    persons: Iterable[tuple[Person, sqlite3.Row | None]],
    now_local: dt.datetime,
    *,
    lookback_days: int = 1,
    db: Db | None = None,
) -> list[Slot]:
    """Every slot whose due time has passed and whose catch-up deadline has not.

    `lookback_days` bounds the ordinary sweep, and a slot with a DEADLINE is genuinely finished
    once that deadline passes. A slot with `catch_up="always"` has no deadline - `Slot.deadline`
    documents None as "always run once due" - and one day of lookback made that promise
    unkeepable: a box down over a weekend never ran Sunday's calibration, its proposals or the
    menu bias, wrote no run_ledger row, and (being data jobs with no marker) went unseen by the
    health report too. That week's targets then came from the formula as if nobody had ever weighed
    anything, with nothing saying so. Deadline-less slots get the longer look back.
    """
    out: list[Slot] = []
    reach = max(lookback_days, ALWAYS_LOOKBACK_DAYS)
    days = [now_local.date() - dt.timedelta(days=i) for i in range(reach, -1, -1)]
    plist = list(persons)
    for job in jobs:
        targets: list[tuple[int, sqlite3.Row | None]]
        if job.scope == "each":
            targets = [(p.id, prof) for p, prof in plist if p.chat_id is not None]
        else:
            targets = [(SYSTEM, None)]
        for pid, profile in targets:
            for day in days:
                stale = (now_local.date() - day).days > lookback_days
                for slot in slots_for(job, pid, profile, day, db):
                    if stale and slot.deadline is not None:
                        continue  # a deadline that has passed is a deadline; leave it alone
                    if slot.due_local <= now_local and (
                        slot.deadline is None or now_local <= slot.deadline
                    ):
                        out.append(slot)
    return out


# ---------------------------------------------------------------- runtime piece
class Scheduler:
    def __init__(
        self,
        app: App,
        jobs: Sequence[Job],
        dispatch: Callable[[int, Callable[[Db], object]], None],
        *,
        tick_s: int = 30,
    ):
        self.app = app
        self.jobs = list(jobs)
        self.dispatch = dispatch
        self.tick_s = tick_s
        self.inflight: set[str] = set()
        self._lock = threading.Lock()
        self.last_tick: str | None = None
        self.ticks = 0

    def _slot_id(self, slot: Slot) -> str:
        return f"{slot.job.name}|{slot.key}|{slot.person_id}"

    def persons_with_profiles(
        self, db: Db, day: dt.date
    ) -> list[tuple[Person, sqlite3.Row | None]]:
        from .targets import profile_on  # noqa: PLC0415 (circular)

        return [(p, profile_on(db, p.id, day)) for p in self.app.registry(db).active()]

    def tick(self, db: Db) -> list[Slot]:
        now_local = self.app.clock.now_local()
        self.last_tick = self.app.clock.utc_stamp()
        self.ticks += 1
        persons = self.persons_with_profiles(db, now_local.date())
        dispatched: list[Slot] = []
        for slot in due_slots(self.jobs, persons, now_local, db=db):
            sid = self._slot_id(slot)
            with self._lock:
                if sid in self.inflight:
                    continue
                # "running" is not terminal: a process killed mid-job leaves the row that way
                # for ever, and skipping it unconditionally made ledger.run's stale-run
                # takeover unreachable. Only a run that is genuinely still in flight blocks.
                if ledger.status_of(db, slot.job.name, slot.key, slot.person_id) in (
                    *ledger.DONE_STATUSES,
                    "failed",
                ) or ledger.live(db, slot.job.name, slot.key, slot.person_id, clock=self.app.clock):
                    continue
                self.inflight.add(sid)
            self.dispatch(slot.person_id, lambda wdb, s=slot: self.execute(wdb, s))
            dispatched.append(slot)
        return dispatched

    def run_forever(self, stop: threading.Event) -> None:
        db = self.app.open_db()
        try:
            while not stop.is_set():
                try:
                    self.tick(db)
                except Exception:
                    logger.exception("tick failed", extra={"event": "tick_error"})
                stop.wait(self.tick_s)
        finally:
            db.close()

    def execute(self, db: Db, slot: Slot) -> str:
        """Runs on the person's worker thread. Returns the run_ledger status."""
        from .targets import pause_active, profile_on  # noqa: PLC0415 (circular)

        sid = self._slot_id(slot)
        job = slot.job
        person = self.app.registry(db).by_id(slot.person_id) if slot.person_id else None
        status = "skipped"
        try:
            with log.context(person_id=slot.person_id or None):
                try:
                    with ledger.run(
                        db, job.name, slot.key, slot.person_id, clock=self.app.clock
                    ) as h:
                        profile = profile_on(db, slot.person_id, slot.day) if person else None
                        paused = bool(person and pause_active(db, person.id, slot.day))
                        ctx = JobContext(
                            app=self.app,
                            db=db,
                            job=job,
                            person=person,
                            profile=profile,
                            day=slot.day,
                            due_local=slot.due_local,
                            now_local=self.app.clock.now_local(),
                            slot=slot.key,
                            handle=h,
                            outbox=self.app.outbox(db),
                            clock=self.app.clock,
                            paused=paused,
                        )
                        if paused and job.kind == "message" and not job.paused_ok and person:
                            ctx.outbox.send(
                                person, job.name, isoday(slot.day), "", drop="dropped_paused"
                            )
                            h.skip("paused")
                        else:
                            job.run(ctx)
                            if person and h.status in ("ok", "degraded") and job.default_answers:
                                self._report_defaults(ctx)
                        status = h.status
                except ledger.AlreadyDone:
                    status = "already"
                except Exception as e:
                    status = "failed"
                    code = getattr(e, "status", None)
                    fault = type(e).__name__
                    if isinstance(code, int) and 400 <= code < 600:
                        fault += f" HTTP {code}"
                    logger.error(
                        "job %s failed type=%s",
                        job.name,
                        fault,
                        extra={"event": "job_failed"},
                    )
                    self._alert(
                        db,
                        f"job_failed:{job.name}",
                        f"{job.name} {slot.key} person={slot.person_id}: {fault}",
                    )
        finally:
            with self._lock:
                self.inflight.discard(sid)
        return status

    @staticmethod
    def _report_defaults(ctx: JobContext) -> None:
        from .onboarding import provided_followups  # noqa: PLC0415

        if not ctx.db.state_get(f"followup_started:{ctx.person_id}"):
            return
        answered = provided_followups(ctx.db, ctx.person_id)
        missing = [(key, said) for key, said in ctx.job.default_answers if key not in answered]
        if not missing:
            return
        ctx.handle.degrade("using profile defaults: " + ", ".join(key for key, _ in missing))
        if ctx.person is not None and not in_quiet(ctx.profile, ctx.now_local):
            description = "; ".join(said for _, said in missing)
            ctx.outbox.send(
                ctx.person,
                "profile_default",
                ctx.job.name,
                f"{ctx.job.name.replace('_', ' ')} is using defaults: {description}. "
                "You can answer later with /followup or /set.",
            )

    def _alert(self, db: Db, event_kind: str, text: str) -> None:
        try:
            admin = self.app.registry(db).admin()
            self.app.outbox(db).alert(admin, event_kind, text)
        except Exception:
            logger.exception("alert failed", extra={"event": "alert_failed"})


__all__ = [
    "Job",
    "JobContext",
    "Scheduler",
    "Slot",
    "catch_up_deadline",
    "daily",
    "defer_to_quiet_end",
    "due_slots",
    "fixed",
    "hourly",
    "in_quiet",
    "slots_for",
]
