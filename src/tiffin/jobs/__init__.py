"""Scheduled jobs for meals, training, shopping and operations.

The evening check-in can answer every open day of the week, and steps are shown on Today.
"""

from __future__ import annotations

from dataclasses import replace

from ..config import Config
from ..scheduler import Job, fixed


def all_jobs(config: Config | None = None) -> list[Job]:
    from . import (  # noqa: PLC0415 (jobs import the scheduler)
        backup,
        body_check,
        day_close,
        evening_confirm,
        icu_sync,
        menu_choices,
        menu_propose,
        menu_shared,
        morning_targets,
        profile_followup,
        shop_refresh,
        shopping_list,
        sunday_summary,
        weekly_calibration,
        weekly_check,
    )

    jobs = [
        morning_targets.JOB,
        icu_sync.JOB,
        profile_followup.JOB,
        icu_sync.WARM_JOB,
        evening_confirm.JOB,
        day_close.JOB,
        weekly_calibration.JOB,
        # after day_close: in one catch-up tick the Sunday is written before it is measured
        weekly_calibration.LATE_JOB,
        sunday_summary.JOB,
        backup.JOB,
        body_check.JOB,
        # the weekly check, for someone who asked for one (food/weekplan.py `check_day`)
        weekly_check.JOB,
        menu_propose.JOB,
        menu_shared.JOB,
        # weekly choices: Sunday's build for the coming weekdays, and the evening that sets a week
        menu_choices.JOB,
        menu_choices.CLOSE_JOB,
        shop_refresh.JOB,
        shop_refresh.CHECK_JOB,
        shopping_list.JOB,
        shopping_list.JOINT_JOB,
    ]
    if config is None:
        return jobs
    times = {
        "day_close": (str(config.get("schedule.day_close")),),
        "menu_propose": ("12:00", str(config.get("schedule.menu_propose_time"))),
        "shopping_list": (str(config.get("schedule.shopping_list_time")),),
        "shopping_joint": (str(config.get("schedule.shopping_joint_time")),),
        "weekly_calibration": (str(config.get("schedule.weekly_calibration")),),
        "sunday_summary": (str(config.get("schedule.sunday_summary")),),
        "backup": (str(config.get("schedule.backup")),),
    }
    configured: list[Job] = []
    for job in jobs:
        if job.name == "icu_sync":
            configured.append(icu_sync.job_for(config))
        elif job.name in times:
            configured.append(replace(job, times=fixed(*times[job.name])))
        else:
            configured.append(job)
    return configured
