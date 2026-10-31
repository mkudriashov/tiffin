"""Ask the weekly cycle question for a person who opted into cycle tracking."""

from __future__ import annotations

from ..clock import isoday
from ..food import weekplan
from ..scheduler import Job, JobContext, fixed
from ..telegram import measures

SUNDAY = frozenset({6})


def run(ctx: JobContext) -> None:
    person = ctx.person
    if person is None or ctx.profile is None:
        ctx.handle.skip("no profile")
        return
    if weekplan.of(ctx.db, person.id).check_day is not None:
        # their weekly check asks it, on the day they chose (jobs/weekly_check.py)
        ctx.handle.skip("asked in the weekly check")
        return
    flow = measures.MeasuresFlow(clock=ctx.clock)
    if flow.busy_with_something_else(ctx.db, person.id):
        # Onboarding or an off-plan question owns the conversation; avoid another prompt.
        ctx.handle.skip("another conversation is open")
        return

    cols = ctx.profile.keys()  # sqlite3.Row: `in` on the Row itself tests values, not keys
    cycle = "cycle_tracking" in cols and bool(ctx.profile["cycle_tracking"])
    if not cycle:
        ctx.handle.skip("cycle tracking off")
        return
    result = ctx.outbox.send(
        person,
        "body_check",
        isoday(ctx.day),
        "Where are you in your cycle?",
        flow.phase_keyboard(),
    )
    if result.status == "failed":
        ctx.handle.degrade(f"send failed: {result.error}")
        return
    ctx.handle.note(f"{result.status}, cycle asked")


JOB = Job(
    name="body_check",
    scope="each",
    kind="message",
    # Several slots allow the cycle question after an earlier conversation has finished.
    times=fixed("10:00", "13:00", "16:00", "19:00"),
    run=run,
    catch_up="20:00",
    days=SUNDAY,
)
