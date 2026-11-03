"""`tiffin status`: liveness and last runs, as JSON for scripts and as a summary for people.

The JSON is what monitors read (`docker exec <container> tiffin status`, with no terminal): its
keys, their order and the `indent=1` layout are pinned by tests and must not change. It is printed
whenever the output is not a terminal, and always with `--json`. A terminal gets the summary.
Neither carries a name, a chat or a message: people are `owner` and `partner` here too.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import TYPE_CHECKING, Any

from .term import Style, plural, table

if TYPE_CHECKING:
    from .app import App

#: plain words for the outbox statuses in the summary (the JSON keeps the stored words)
SENT_WORDS = {
    "sent": "sent",
    "failed": "failed",
    "dropped_quiet": "held back in quiet hours",
    "dropped_paused": "held back while paused",
    "dropped_dev": "held back by development mode",
}
RUN_MARKS = {"ok": "ok", "skipped": "info", "running": "info", "degraded": "warn", "failed": "fail"}


def gather(app: App) -> tuple[dict[str, Any], dict[int, str]]:
    """The status JSON's content, and person ids by slug for the summary (not in the JSON)."""
    from .clock import stamp  # noqa: PLC0415
    from .heartbeat import Heartbeat, PidFile  # noqa: PLC0415

    paths = app.config.paths
    hb = Heartbeat(paths.state, app.clock)
    out: dict[str, Any] = {
        "env": app.config.env,
        "version": app.version,
        "db": str(paths.db),
        "db_exists": paths.db.exists(),
        "heartbeat_age_s": hb.age_s(),
        "pid": (PidFile(paths.state).read() or {}).get("pid"),
    }
    slugs: dict[int, str] = {}
    if paths.db.exists():
        db = app.open_db()
        try:
            active = app.registry(db).active()
            slugs = {p.id: p.slug for p in active}
            out["persons"] = [
                {"slug": p.slug, "has_chat_id": p.chat_id is not None, "erased": p.erased}
                for p in active
            ]
            out["last_runs"] = [
                dict(r)
                for r in db.all(
                    "SELECT job, slot, person_id, status, detail FROM run_ledger "
                    "ORDER BY started_at DESC LIMIT 10"
                )
            ]
            from . import expected  # noqa: PLC0415
            from .jobs import all_jobs  # noqa: PLC0415

            # every expected morning and evening message that has not gone by its grace deadline:
            # slot, internal person id and due time (UTC), a reason and `late` or `missed`.
            # Empty when all is well. No names, no content.
            out["missed_slots"] = [
                m.as_dict()
                for m in expected.missing(db, app.registry(db), all_jobs(app.config), app.clock)
            ]
            out["messages_out_24h"] = dict(
                (r["status"], r["n"])
                for r in db.all(
                    "SELECT status, COUNT(*) AS n FROM messages_out "
                    "WHERE created_at >= ? GROUP BY status",
                    (stamp(app.clock.now_utc() - dt.timedelta(days=1)),),
                )
            )
        finally:
            db.close()
    return out, slugs


def json_text(out: dict[str, Any]) -> str:
    return json.dumps(out, indent=1, default=str)


def summary_lines(
    out: dict[str, Any], slugs: dict[int, str], style: Style, *, stale_s: int = 180
) -> list[str]:
    """The same facts in words, for a person at a terminal."""
    mode = "production" if out.get("env") == "prod" else "development"
    lines = [style.heading("Tiffin status"), style.dim(f"{mode}, version {out.get('version')}")]
    age = out.get("heartbeat_age_s")
    pid = out.get("pid")
    if age is None:
        running = style.line("warn", "not running; start it with tiffin run")
    elif age < stale_s:
        running = style.line("ok", f"running; it last checked in {age:.0f} s ago")
        running += f" (process {pid})" if pid else ""
    else:
        running = style.line("fail", f"not answering: it last checked in {_ago(age)} ago")
    lines += ["", running]
    if not out.get("db_exists"):
        lines.append(style.line("warn", f"no database yet at {out.get('db')}"))
        lines += ["", style.dim("For scripts: tiffin status --json")]
        return lines
    lines.append(style.dim(f"database {out.get('db')}"))

    people = [
        [p["slug"], "erased" if p.get("erased") else "paired" if p["has_chat_id"] else "not paired"]
        for p in out.get("persons", [])
    ]
    if people:
        lines += ["", style.bold("People"), *table(people)]

    sent = out.get("messages_out_24h") or {}
    lines += ["", style.bold("Messages in the last 24 hours")]
    if sent:
        words = [f"{n} {SENT_WORDS.get(status, status)}" for status, n in sorted(sent.items())]
        mark = "fail" if sent.get("failed") else "ok"
        lines.append("  " + style.line(mark, ", ".join(words)))
    else:
        lines.append("  " + style.line("info", "none"))

    lines += ["", style.bold("Expected messages")]
    missed = out.get("missed_slots") or []
    if not missed:
        lines.append(
            "  " + style.line("ok", "every morning and evening message has gone, or was held back")
        )
    for slot in missed:
        who = slugs.get(slot.get("person_id"), f"person {slot.get('person_id')}")
        state = "fail" if slot.get("state") == "missed" else "warn"
        lines.append(
            "  "
            + style.line(
                state,
                f"{slot.get('job')} ({slot.get('slot')}) for {who}: {slot.get('state')}, "
                f"due {slot.get('due')} UTC, {str(slot.get('reason', '')).replace('_', ' ')}",
            )
        )

    runs = out.get("last_runs") or []
    if runs:
        lines += ["", style.bold("Last runs, newest first")]
        rows = [
            [
                RUN_MARKS.get(str(r.get("status")), "info"),
                str(r.get("job")),
                slugs.get(r.get("person_id"), "everyone")
                if r.get("person_id") is not None
                else "everyone",
                str(r.get("status")),
                str(r.get("detail") or ""),
            ]
            for r in runs
        ]
        marks = [row[0] for row in rows]
        lines += table(
            [["x", *row[1:]] for row in rows],
            paint=lambda r, c, text: style.mark(marks[r]) if c == 0 else text,
        )
    lines += ["", style.dim("For scripts: tiffin status --json")]
    return lines


def _ago(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 60:
        return plural(max(minutes, 1), "minute")
    hours = minutes // 60
    if hours < 48:
        return plural(hours, "hour")
    return plural(hours // 24, "day")
