"""day_close at 00:05: yesterday becomes unconfirmed (never eaten, never zero) or paused."""

from __future__ import annotations

import datetime as dt

from .. import days
from ..scheduler import Job, JobContext, fixed
from ..telegram import intents


def run(ctx: JobContext) -> None:
    person = ctx.person
    if person is None:
        ctx.handle.skip("no person")
        return
    yesterday = ctx.day - dt.timedelta(days=1)
    # a card nobody answered in a week expires, so a late tap
    # cannot write into a week already summarised. `lapse_open` was
    # written for this and never called.
    cutoff = ctx.clock.now_utc() - dt.timedelta(days=intents.BACKDATE_DAYS)
    lapsed = intents.lapse_open(ctx.db, person.id, cutoff.strftime("%Y-%m-%d %H:%M:%S"))
    if lapsed:
        ctx.handle.note(f"{lapsed} unanswered cards expired")
    rid = days.close_day(ctx.db, person.id, yesterday)
    if rid is None:
        ctx.handle.skip("already recorded")
        return
    row = ctx.db.one("SELECT status FROM day_confirmations WHERE id = ?", (rid,))
    ctx.handle.note(f"{yesterday}: {row['status'] if row else '?'}")


JOB = Job(
    name="day_close",
    scope="each",
    kind="data",
    times=fixed("00:05"),
    run=run,
    catch_up="always",
    needs_profile=False,
    paused_ok=True,
)
