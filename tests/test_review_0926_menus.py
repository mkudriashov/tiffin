"""Tests for menus remaining in force until a new decision replaces them."""

from __future__ import annotations

import datetime as dt
import sqlite3

import pytest

from tiffin import ledger
from tiffin.food import menu as menu_mod
from tiffin.jobs import menu_propose

from .test_journey_owner_week import (  # noqa: F401 (week is a pytest fixture)
    FRI18,
    MON14,
    SAT19,
    SUN20,
    THU17,
    TUE15,
    WED16,
    week,
)


def _current_days(db, pid: int, menu_id: int) -> list[str]:
    return [
        str(r["day"])
        for r in db.all("SELECT day FROM menu_days WHERE menu_id = ? ORDER BY day", (menu_id,))
    ]


def test_a_rebuild_on_wednesday_leaves_monday_to_wednesday_alone(week) -> None:  # noqa: F811
    week.at(WED16, "17:59")
    seg = menu_mod.open_days(week.db, week.his, menu_mod.Segment(MON14, FRI18), WED16)
    assert seg == menu_mod.Segment(THU17, FRI18), seg


def test_a_repeat_never_copies_over_a_day_already_under_way(week) -> None:  # noqa: F811
    """The shape of the 16 Sep rewrite: a copy of the menu in force, over its own days."""
    week.at(WED16, "17:59")
    copy = menu_mod.repeat_last(week.db, week.his, menu_mod.Segment(MON14, FRI18), clock=week.clock)
    assert copy is not None
    assert _current_days(week.db, week.his, copy) == ["2026-09-17", "2026-09-18"]
    tuesday = menu_mod.current_menu(week.db, week.his, TUE15)
    assert tuesday is not None and tuesday["id"] == week.menu6, (
        "Tuesday is still the menu he shopped and cooked for"
    )


def test_the_database_refuses_a_menu_row_for_a_confirmed_day(week) -> None:  # noqa: F811
    week.db.insert(
        "day_confirmations",
        person_id=week.his,
        day="2026-09-15",
        status="confirmed",
        basis="menu",
        kcal=1915,
    )
    mid = week.db.insert(
        "menus",
        person_id=week.his,
        segment_start="2026-09-15",
        segment_end="2026-09-15",
        status="repeated",
    )
    with pytest.raises(sqlite3.IntegrityError, match="already confirmed"):
        week.db.insert(
            "menu_days",
            menu_id=mid,
            day="2026-09-15",
            kcal=1,
            protein_g=1,
            fat_g=1,
            carb_g=1,
            fibre_g=1,
        )


def test_a_failed_weekend_build_is_tried_again_the_next_day(week) -> None:  # noqa: F811
    """Thursday's weekend build failed; Friday 12:00 tries again before Saturday starts."""
    week.at(THU17, "18:05")
    week.db.execute(
        "INSERT INTO run_ledger(run_id, job, slot, person_id, started_at, ended_at, status, "
        "detail) VALUES ('x', 'menu_propose', '2026-09-17T1800', ?, '2026-09-17 17:00:00', "
        "'2026-09-17 17:02:00', 'degraded', 'model unavailable: budget')",
        (week.his,),
    )
    assert menu_mod.current_menu(week.db, week.his, SAT19) is None, "no weekend menu yet"
    week.run(menu_propose.JOB, FRI18)  # the first slot of Friday is 12:00
    assert menu_mod.current_menu(week.db, week.his, SAT19) is not None, (
        "Friday's run must retry the weekend that Thursday failed to build"
    )


def test_nothing_failed_means_nothing_extra_is_built(week) -> None:  # noqa: F811
    week.at(FRI18, "11:55")
    week.run(menu_propose.JOB, FRI18)
    assert menu_mod.current_menu(week.db, week.his, SAT19) is None, (
        "no failure, no retry: the weekend is the Thursday job's to build"
    )


def test_an_abandoned_run_is_closed_as_failed(week) -> None:  # noqa: F811
    week.at(WED16, "09:00")
    week.db.execute(
        "INSERT INTO run_ledger(run_id, job, slot, person_id, started_at, status) "
        "VALUES ('stuck', 'menu_propose', '2026-09-13T1800', ?, '2026-09-13 16:19:00', 'running')",
        (week.his,),
    )
    assert ledger.close_abandoned(week.db, clock=week.clock) == 1
    row = week.db.one("SELECT status, error FROM run_ledger WHERE run_id = 'stuck'")
    assert row["status"] == "failed" and "abandoned" in row["error"]


def test_menu_propose_has_a_second_daily_chance() -> None:
    times = menu_propose.JOB.times(None, dt.date(2026, 9, 17))
    assert times == ["12:00", "18:00"], times


# ---------------------------------------------------------------- a today with nothing planned
def test_a_saturday_with_no_menu_is_planned_from_today(week) -> None:  # noqa: F811
    """Sat 26 Sep: the builds had failed, and every path started from tomorrow, so the day had no
    menu at all. Before 16:00 an empty today is planned from today."""
    week.at(SAT19, "11:55")
    assert menu_mod.current_menu(week.db, week.his, SAT19) is None
    week.run(menu_propose.JOB, SAT19)  # the 12:00 slot
    assert menu_mod.current_menu(week.db, week.his, SAT19) is not None, "today got a menu"


def test_after_four_the_empty_day_is_left_alone(week) -> None:  # noqa: F811
    from tiffin import scheduler as sc
    from tiffin import targets as targets_mod

    prof = targets_mod.profile_on(week.db, week.his, SAT19)
    slot = next(
        s
        for s in sc.slots_for(menu_propose.JOB, week.his, prof, SAT19, week.db)
        if s.key.endswith("T1800")
    )
    week.clock.set_local(slot.due_local)
    sc.Scheduler(week.app, [menu_propose.JOB], lambda _p, _fn: None).execute(week.db, slot)
    assert menu_mod.current_menu(week.db, week.his, SAT19) is None, "18:00 is too late for today"


def test_the_gap_stops_at_a_day_that_already_has_a_menu(week) -> None:  # noqa: F811
    week.at(SAT19, "11:00")
    sunday_only = week.db.insert(
        "menus",
        person_id=week.his,
        segment_start="2026-09-20",
        segment_end="2026-09-20",
        status="repeated",
    )
    week.db.insert(
        "menu_days",
        menu_id=sunday_only,
        day="2026-09-20",
        kcal=1,
        protein_g=1,
        fat_g=1,
        carb_g=1,
        fibre_g=1,
    )
    gap = menu_propose.today_gap(
        week.db,
        week.his,
        week.db.one("SELECT * FROM profiles WHERE person_id = ?", (week.his,)),
        week.clock.now_local(),
    )
    assert gap == menu_mod.Segment(SAT19, SAT19), "Sunday's menu is not replaced"


# ---------------------------------------------------------------- the weekend is a treat
def test_a_weekend_is_built_on_the_treat_brief(week) -> None:  # noqa: F811
    """The owner, 26 Sep: "both sat + sun are tasty treats but very healthy (e.g. healthy
    burgers)"."""
    from typing import Any

    from . import test_journey_owner_week as j

    seen: list[str] = []
    real = j._Gateway.json_task

    def spy(self: Any, task: str, prompt: str, *a: Any, **k: Any) -> Any:
        seen.append(prompt)
        return real(self, task, prompt, *a, **k)

    j._Gateway.json_task = spy  # type: ignore[method-assign]
    try:
        week.at(SAT19, "11:55")
        week.run(menu_propose.JOB, SAT19)
    finally:
        j._Gateway.json_task = real  # type: ignore[method-assign]
    assert seen and "healthy versions of indulgent food" in seen[0]
    assert "smash burgers" in seen[0] and "tired after work" not in seen[0]


def test_a_menu_made_this_afternoon_can_be_replaced_today(week) -> None:  # noqa: F811
    """Built at 14:50 for today, then asked to be a treat instead: nothing was shopped or cooked
    from it yet, so today is still open to a new menu. One in force since before today began
    is not."""
    week.at(SAT19, "14:50")
    made_today = week.db.insert(
        "menus",
        person_id=week.his,
        segment_start="2026-09-19",
        segment_end="2026-09-20",
        status="repeated",
    )
    week.db.insert(
        "menu_days",
        menu_id=made_today,
        day="2026-09-19",
        kcal=1,
        protein_g=1,
        fat_g=1,
        carb_g=1,
        fibre_g=1,
    )
    seg = menu_mod.open_days(week.db, week.his, menu_mod.Segment(SAT19, SUN20), SAT19)
    assert seg == menu_mod.Segment(SAT19, SUN20), "today's afternoon menu can be replaced"
    week.at(SAT19 + dt.timedelta(days=1), "10:00")
    seg = menu_mod.open_days(week.db, week.his, menu_mod.Segment(SUN20, SUN20), SUN20)
    # Sunday began under it (it was decided on Saturday): Sunday is protected from a rebuild
    assert seg is None, "a menu in force when the day began is the food that was bought"
    week.at(WED16, "09:00")
    seg = menu_mod.open_days(week.db, week.his, menu_mod.Segment(WED16, FRI18), WED16)
    assert seg == menu_mod.Segment(THU17, FRI18), "menu 6 was in force when Wednesday began"
