"""Offer one optional profile question on a later, awake day."""

from __future__ import annotations

from ..onboarding import Onboarding
from ..scheduler import Job, JobContext, fixed


def run(ctx: JobContext) -> None:
    if ctx.person is None or ctx.profile is None:
        ctx.handle.skip("no profile")
        return
    flow = Onboarding(ctx.db, ctx.outbox, ctx.app.registry(ctx.db), ctx.app.config, clock=ctx.clock)
    try:
        offered = flow.ask_followup(ctx.person, scheduled=True)
    except RuntimeError as e:
        ctx.handle.degrade(str(e))
        return
    if not offered:
        ctx.handle.skip("no follow-up due")


JOB = Job(
    name="profile_followup",
    scope="each",
    kind="message",
    times=fixed("11:00"),
    run=run,
    catch_up="18:00",
)
