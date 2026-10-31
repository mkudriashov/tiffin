"""`tiffin dry-run --date D [--person S] [--out F]`: render every message due that day.

Runs the real jobs against a throwaway copy of the environment's DB with a frozen clock at
each slot's due time, and a DryOutbox that records instead of sending. Nothing touches
Telegram; the copy is discarded. Output: an HTML page (or text) the owner can read.
"""

from __future__ import annotations

import datetime as dt
import html
import sqlite3
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import ledger, log
from .app import App
from .clock import FrozenClock
from .db import Db
from .persons import Person
from .scheduler import Job, Scheduler, Slot, slots_for
from .telegram.api import Keyboard
from .telegram.outbox import Outbox, SendResult

logger = log.get("tiffin.dryrun")


@dataclass
class DryMessage:
    slug: str
    kind: str
    due_local: str
    text: str
    buttons: list[str] = field(default_factory=list)
    status: str = "sent"


class DryOutbox(Outbox):
    """Records every send; never calls the Telegram API."""

    def __init__(self, db: Db, sink: list[DryMessage], slug_of: dict[int, str], clock: Any):
        # Preview records sends locally, so it has no Telegram API instance.
        super().__init__(None, db, env="dryrun", admin_chat_id=None, clock=clock)  # type: ignore[arg-type]
        self.sink = sink
        self.slug_of = slug_of
        self.seen: set[tuple[int, str, str]] = set()

    def send(  # type: ignore[override]
        self,
        person: Person,
        kind: str,
        due_key: str,
        html_text: str,
        keyboard: Keyboard | None = None,
        *,
        payload: dict[str, Any] | None = None,
        model_used: str | None = None,
        drop: str | None = None,
    ) -> SendResult:
        # the same idempotency the real outbox applies. Without it a job with several attempt
        # slots - body_check has four, so one skipped attempt cannot cost the week - previewed
        # as four identical messages, which is not what would be sent.
        key = (person.id, kind, due_key)
        if key in self.seen:
            return SendResult(0, "already sent")
        self.seen.add(key)
        buttons = [b["text"] for row in (keyboard or {}).get("inline_keyboard", []) for b in row]
        # the field is called due_local and is read as "when this lands on their phone", so it
        # must be the London time, not the UTC stamp. Through the winter the two are equal,
        # which is why a heading an hour early all summer went unnoticed.
        self.sink.append(
            DryMessage(
                self.slug_of.get(person.id, person.slug),
                kind,
                self.clock.now_local().strftime("%Y-%m-%d %H:%M:%S"),
                html_text,
                buttons,
                drop or "sent",
            )
        )
        return SendResult(0, drop or "sent")


def copy_db(src: Path, dst: Path) -> None:
    con = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    out = sqlite3.connect(str(dst))
    try:
        con.backup(out)
    finally:
        out.close()
        con.close()


def render_day(
    app: App, day: dt.date, *, jobs: list[Job] | None = None, person_slug: str | None = None
) -> list[DryMessage]:
    if jobs is None:
        from .jobs import all_jobs  # noqa: PLC0415 (jobs import the scheduler)

        jobs = all_jobs(app.config)
    messages: list[DryMessage] = []
    app.config.paths.ensure()
    with tempfile.TemporaryDirectory(prefix="tiffin-dryrun-", dir=app.config.paths.state) as tmp:
        path = Path(tmp) / "tiffin.db"
        if app.config.paths.db.exists():
            copy_db(app.config.paths.db, path)
        clock = FrozenClock(dt.datetime.combine(day, dt.time(0, 0), tzinfo=app.clock.timezone))
        db = Db.open(path, clock=clock)
        try:
            app.registry(db).ensure_from_config()
            persons = app.registry(db).active()
            slug_of = {p.id: p.slug for p in persons}
            dry = DryOutbox(db, messages, slug_of, clock)
            app.open_db = lambda: db  # type: ignore[method-assign]
            app.outbox = lambda _db: dry  # type: ignore[method-assign]
            app.clock = clock
            sched = Scheduler(app, jobs, lambda pid, fn: None)
            profiles = sched.persons_with_profiles(db, day)
            # A preview shows the whole day, so it enumerates the day's slots rather than
            # asking what is still due: due_slots applies each job's catch-up deadline, which
            # would hide the morning message from a dry run asked for in the evening.
            slots: list[Slot] = []
            # message jobs only: a preview must not sync intervals.icu or
            # take a backup, and those are what the data jobs would do
            for job in [j for j in jobs if j.kind == "message"]:
                targets = (
                    [(p.id, prof) for p, prof in profiles if p.chat_id is not None]
                    if job.scope == "each"
                    else [(0, None)]
                )
                for pid, profile in targets:
                    if person_slug is not None and slug_of.get(pid) not in (person_slug, None):
                        continue
                    slots.extend(slots_for(job, pid, profile, day, db))
            for slot in sorted(slots, key=lambda s: s.due_local):
                clock.set_local(slot.due_local)
                # a fresh run_ledger slot every time: the copy is throwaway
                db.execute(
                    "DELETE FROM run_ledger WHERE job = ? AND slot = ? AND person_id = ?",
                    (slot.job.name, slot.key, slot.person_id),
                )
                status = sched.execute(db, slot)
                if status not in ("ok", "skipped", "already", "degraded"):
                    messages.append(
                        DryMessage(
                            slug_of.get(slot.person_id, "system"),
                            slot.job.name,
                            clock.utc_stamp(),
                            f"[{status}]",
                            status=status,
                        )
                    )
        finally:
            db.close()
    return messages


def to_html(day: dt.date, messages: list[DryMessage]) -> str:
    rows = []
    for m in messages:
        body = m.text.replace("\n", "<br>")
        buttons = " ".join(f"<kbd>{html.escape(b)}</kbd>" for b in m.buttons)
        rows.append(
            f"<section><h3>{html.escape(m.due_local)} · {html.escape(m.slug)} · "
            f"{html.escape(m.kind)}"
            f" · {html.escape(m.status)}</h3><p>{body}</p><p>{buttons}</p></section>"
        )
    return (
        "<!doctype html><meta charset='utf-8'><title>tiffin dry-run "
        f"{day}</title><style>body{{font:15px system-ui;max-width:640px;margin:2em auto}}"
        "section{border:1px solid #ccc;border-radius:8px;padding:.5em 1em;margin:1em 0}"
        "kbd{border:1px solid #888;border-radius:4px;padding:2px 6px;margin-right:4px}</style>"
        f"<h1>Dry run {day}</h1>" + "".join(rows) + ("" if rows else "<p>No messages due.</p>")
    )


def to_text(messages: list[DryMessage]) -> str:
    out = []
    for m in messages:
        out.append(f"== {m.due_local} {m.slug} {m.kind} [{m.status}]")
        out.append(m.text)
        if m.buttons:
            out.append("buttons: " + " | ".join(m.buttons))
        out.append("")
    return "\n".join(out) if out else "No messages due.\n"


def write(day: dt.date, messages: list[DryMessage], out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    content = to_html(day, messages) if out.suffix.lower() == ".html" else to_text(messages)
    out.write_text(content, encoding="utf-8")
    return out


__all__ = ["DryMessage", "DryOutbox", "ledger", "render_day", "to_html", "to_text", "write"]
