"""icu_sync: pull wellness, activities and streams from intervals.icu, then match sessions.

Two cadences, as the fetcher this replaced had (the plan's hot/warm/cold tiers):

  icu_sync       hourly at :10, a narrow window (14 days wellness, 3 days activity). Keeps the
                 daily steps and the session match fresh at ~24 calls a day per person.
  icu_sync_warm  Sunday 04:40, a wide window (90 / 30). Nothing else ever looks further back
                 than the hourly window, so without this a box that was off for four days
                 loses those activities for good, an activity edited later is never re-read,
                 and a weight added retroactively more than a fortnight back never arrives.
                 It is two extra requests a week.

A person without a key is skipped, not failed: the key arrives when they paste it to the
bot. A 429 or a spent local budget degrades the run and the next hour tries again; a
rejected key or an unreachable host raises, so the run is `failed` and the owner is alerted.
"""

from __future__ import annotations

import dataclasses
import datetime as dt

from .. import training
from ..config import Config
from ..errors import IcuRateLimited
from ..integrations import icu
from ..scheduler import Job, JobContext, fixed, hourly

MINUTE = 10  # schedule.icu_sync_minute in config/defaults.toml
SUNDAY = frozenset({6})
WARM_TIME = "04:40"


def sync(ctx: JobContext, *, wellness_days: int, activity_days: int) -> None:
    person = ctx.person
    if person is None:
        ctx.handle.skip("no person")
        return
    key = ctx.app.secrets.icu_key(person.slug)
    if not key:
        ctx.handle.skip("no icu key")
        return
    cfg: Config = ctx.app.config
    section = cfg.section("icu")
    try:
        result = icu.sync_person(
            ctx.db,
            person,
            key,
            state_dir=cfg.paths.state,
            streams_dir=cfg.paths.icu_streams,
            days_wellness=wellness_days,
            days_activity=activity_days,
            clock=ctx.clock,
            transport=ctx.app.transport,
            base_url=str(section.get("base_url", icu.BASE_URL)),
            budget_per_hour=int(section.get("budget_per_hour", 60)),
        )
    except (IcuRateLimited, icu.IcuBudgetSpent) as e:
        ctx.handle.degrade(f"stopped early: {e}; next hour retries")
    else:
        ctx.handle.note(result.summary())
    matched = 0
    for day in (ctx.day, ctx.day - dt.timedelta(days=1)):
        matched += len(training.match_day(ctx.db, person.id, day))
    ctx.handle.note(f"session matches {matched}")


def run(ctx: JobContext) -> None:
    section = ctx.app.config.section("icu")
    sync(
        ctx,
        wellness_days=int(section.get("wellness_days", 14)),
        activity_days=int(section.get("activity_days", 3)),
    )


def run_warm(ctx: JobContext) -> None:
    section = ctx.app.config.section("icu")
    sync(
        ctx,
        wellness_days=int(section.get("wellness_days_warm", 90)),
        activity_days=int(section.get("activity_days_warm", 30)),
    )


JOB = Job(
    name="icu_sync",
    scope="each",
    kind="data",
    times=hourly(MINUTE),
    run=run,
    catch_up="none",
    needs_profile=False,
)

WARM_JOB = Job(
    name="icu_sync_warm",
    scope="each",
    kind="data",
    times=fixed(WARM_TIME),
    run=run_warm,
    catch_up="always",
    days=SUNDAY,
    needs_profile=False,
)


def job_for(config: Config) -> Job:
    """JOB with the minute taken from config (schedule.icu_sync_minute)."""
    minute = int(config.get("schedule.icu_sync_minute", MINUTE))
    return JOB if minute == MINUTE else dataclasses.replace(JOB, times=hourly(minute))
