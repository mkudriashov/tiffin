"""The weekly check, for someone who asked for one (food/weekplan.py `check_day`).

entering the first day of a period, and the lot in one message on Monday morning. Her weight comes
from her scale through intervals.icu, so the message only reminds her to step on it - weight is
never typed. The tape takes a typed reply, through the same flow as /measure; the period
is one tap on the day it started, or Not yet.
"""

from __future__ import annotations

import datetime as dt

from ..clock import isoday
from ..food import weekplan
from ..scheduler import Job, JobContext, fixed
from ..telegram import measures
from ..telegram.api import inline_keyboard

DAY_WORDS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def period_keyboard(today: dt.date) -> dict:
    """Started today, or on one of the six days before it, or not yet: one tap each."""
    days = [today - dt.timedelta(days=i) for i in range(1, 7)]
    rows = [[("Started today", f"k:prd:{isoday(today)}"), ("Not yet", "k:prd:no")]]
    row: list[tuple[str, str]] = []
    for d in days:
        row.append((f"{DAY_WORDS[d.weekday()]} {d.day}", f"k:prd:{isoday(d)}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return inline_keyboard(rows)


def text_for(cycle: bool, plan: weekplan.WeekPlan = weekplan.HOUSE) -> str:
    tape = (
        f"2. Tape: {measures.sites_said(plan)}. {measures.reply_like(plan)} "
        "How to take each is under Progress on the page."
        if plan.tape_sites
        else "2. Waist and hip in cm: reply like <code>84 102</code>, or <code>skip</code>."
    )
    lines = [
        "<b>Weekly check</b>",
        "1. Step on the scale this morning, if you have not yet: it comes through on its own.",
        tape,
    ]
    if cycle:
        lines.append("3. Has your period started since last week? Tap the day it started.")
    return "\n".join(lines)


def run(ctx: JobContext) -> None:
    person = ctx.person
    if person is None or ctx.profile is None:
        ctx.handle.skip("no profile")
        return
    plan = weekplan.of(ctx.db, person.id)
    if plan.check_day is None or ctx.day.weekday() != plan.check_day:
        ctx.handle.skip("no weekly check today")
        return
    flow = measures.MeasuresFlow(clock=ctx.clock)
    if flow.busy_with_something_else(ctx.db, person.id):
        # another question owns the conversation; a later slot today asks again
        ctx.handle.skip("another conversation is open")
        return
    cycle = "cycle_tracking" in ctx.profile.keys() and bool(ctx.profile["cycle_tracking"])  # noqa: SIM118
    result = ctx.outbox.send(
        person,
        "weekly_check",
        isoday(ctx.day),
        text_for(cycle, plan),
        period_keyboard(ctx.day) if cycle else None,
    )
    if result.status == "failed":
        ctx.handle.degrade(f"send failed: {result.error}")
        return
    if result.sent:
        # the tape answer is the next typed message, as after /measure
        flow.arm(ctx.db, person.id, ctx.day)
    ctx.handle.note(f"{result.status}, weekly check")


JOB = Job(
    name="weekly_check",
    scope="each",
    kind="message",
    # two slots: a morning with another question still open asks again later
    times=fixed("08:00", "11:00"),
    run=run,
    catch_up="13:00",
)
