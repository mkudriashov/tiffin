"""Twenty-five stories about the body: weight, the trend, the days they land on, the targets
they feed, the sessions beside them, and what intervals.icu sends.

Each one is a thing that happened - a weight typed twice, a month away from the scale, the
morning the clocks moved, a ride three times longer than the plan - put through the real
readers. The rules under test are the house ones: corrections are new rows and the latest row
wins, a day is a London day whatever the clock did, and her rows are never in his numbers.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from tiffin import body, days, energy, targets, training
from tiffin import clock as ck
from tiffin.clock import FrozenClock, isoday
from tiffin.db import Db
from tiffin.energy import Macros
from tiffin.http import Response
from tiffin.integrations import icu
from tiffin.jobs import evening_confirm
from tiffin.persons import Person

from .fakes import ScriptedTransport

MONDAY = dt.date(2026, 9, 7)
SUNDAY = dt.date(2026, 9, 6)
A_DAY = Macros(2250, 202, 71, 200, 30)


# ---------------------------------------------------------------- builders
def a_profile(db: Db, person_id: int, **over: Any) -> int:
    base: dict[str, Any] = {
        "person_id": person_id,
        "effective_from": "2026-09-01",
        "sex": "m",
        "birth_date": "1990-03-15",
        "height_cm": 183.0,
        "activity_factor": 1.55,
        "kcal_floor": 1800,
        "goal_weight_kg": 87.0,
        "goal_date": "2027-01-24",
    }
    base.update(over)
    return db.insert("profiles", **base)


def a_plan(db: Db, person_id: int, *sessions: tuple[int, str, int], effective: str = "2026-09-01"):
    plan_id = db.insert("training_plans", person_id=person_id, effective_from=effective)
    for weekday, kind, minutes in sessions:
        db.insert(
            "planned_sessions",
            plan_id=plan_id,
            person_id=person_id,
            weekday=weekday,
            kind=kind,
            target_minutes=minutes,
        )
    return plan_id


def an_activity_row(db: Db, person_id: int, icu_id: str, kind: str | None, moving_s: int) -> None:
    db.insert(
        "icu_activity",
        person_id=person_id,
        icu_id=icu_id,
        day=isoday(MONDAY),
        start_utc="2026-09-07 10:00:00",
        icu_type="X",
        kind=kind,
        moving_s=moving_s,
        elapsed_s=moving_s + 120,
        icu_raw_id=1,
    )


def resp(payload: Any) -> Response:
    return Response(200, json.dumps(payload).encode("utf-8"), {})


def an_activity(aid: str, start_utc: str, **over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": aid,
        "type": "RockClimbing",
        "name": f"session {aid}",
        "start_date": start_utc,
        "moving_time": 4800,
        "elapsed_time": 5000,
        "stream_types": [],
    }
    row.update(over)
    return row


def a_wellness(day: str, **over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {"id": day, "weight": None, "steps": None, "ctl": 5.0, "atl": 1.5}
    row.update(over)
    return row


def a_sync(
    db: Db, person_id: int, script: list[Response], tmp_path: Path, clock: FrozenClock
) -> icu.SyncResult:
    """One hourly pull for the owner, with intervals.icu scripted and every path under tmp."""
    who = Person(person_id, "owner", "Alex", "neutral", 1000001, None, False)
    return icu.sync_person(
        db,
        who,
        "not-a-real-key",
        state_dir=tmp_path / "state",
        streams_dir=tmp_path / "streams",
        clock=clock,
        transport=ScriptedTransport(list(script)),
        sleeper=lambda _s: None,
    )


# ---------------------------------------------------------------- weight and the trend
def test_01_a_weigh_in_corrected_at_breakfast_carries_the_whole_trend_with_it(
    fixture_db: Db, two_persons
) -> None:
    """The trend is an EWMA, so every later day is computed from the corrected one. If the
    rebuild only touched the day that changed, Tuesday's typo would still be inside Friday's
    number for ever, and Friday's number is what the targets are built from."""
    pid = two_persons["owner"]
    week = [dt.date(2026, 9, 1) + dt.timedelta(days=i) for i in range(5)]
    for day, kg in zip(week, [101.0, 100.6, 100.4, 100.2, 100.0], strict=True):
        body.record_weigh_in(fixture_db, pid, day, kg, "manual")
    before = body.latest_trend(fixture_db, pid, week[-1])
    body.record_weigh_in(fixture_db, pid, week[1], 97.6, "manual")  # 100.6 was a slipped finger
    after = body.latest_trend(fixture_db, pid, week[-1])
    assert before is not None and after is not None
    assert after.trend_kg < before.trend_kg, "the correction reaches the last day"
    assert [(d, kg) for d, kg, _ in body.weigh_ins(fixture_db, pid)][1] == (week[1], 97.6)
    assert fixture_db.scalar("SELECT COUNT(*) FROM weigh_ins") == 6, "both rows are kept"
    per_day = {
        str(r["day"]): int(r["c"])
        for r in fixture_db.all(
            "SELECT day, COUNT(*) AS c FROM trend_points WHERE person_id = ? GROUP BY day", (pid,)
        )
    }
    assert per_day[isoday(week[0])] == 1, "the day before the correction did not move"
    assert [per_day[isoday(d)] for d in week[1:]] == [2, 2, 2, 2]


def test_02_two_weigh_ins_on_one_day_from_two_sources_leave_one_weight_for_the_day(
    fixture_db: Db, two_persons
) -> None:
    pid = two_persons["owner"]
    body.record_weigh_in(fixture_db, pid, SUNDAY, 100.4, "icu_wellness")
    body.record_weigh_in(fixture_db, pid, SUNDAY, 99.8, "manual")
    assert [(d, kg) for d, kg, _ in body.weigh_ins(fixture_db, pid)] == [(SUNDAY, 99.8)]
    assert fixture_db.scalar("SELECT COUNT(*) FROM weigh_ins") == 2
    latest = fixture_db.latest("weigh_ins", person_id=pid, day=isoday(SUNDAY))
    assert latest is not None and latest["source"] == "manual"
    points = body.trend_points(fixture_db, pid)
    assert len(points) == 1 and points[0].trend_kg == 99.8, "one trend point for the day"


def test_03_the_scale_puts_its_own_number_back_over_one_typed_by_hand(
    fixture_db: Db, two_persons, tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    """Onboarding takes a weight the person types; after that the scale is the source and the
    bot asks for a weigh-in, never for a number. So the mirror re-asserting its own reading
    over a hand-typed one is the design. What must hold is that nothing is edited away, and
    that it settles - a sync that wrote a fresh row every hour would be a ledger of nothing."""
    pid = two_persons["owner"]
    scale = a_wellness("2026-09-06", weight=100.4)
    a_sync(fixture_db, pid, [resp([]), resp([scale])], tmp_path, frozen_clock)
    assert [kg for _d, kg, _i in body.weigh_ins(fixture_db, pid)] == [100.4]
    body.record_weigh_in(fixture_db, pid, SUNDAY, 99.0, "manual")
    a_sync(fixture_db, pid, [resp([]), resp([scale])], tmp_path, frozen_clock)
    assert [kg for _d, kg, _i in body.weigh_ins(fixture_db, pid)] == [100.4]
    rows = fixture_db.all("SELECT source FROM weigh_ins WHERE person_id = ? ORDER BY id", (pid,))
    assert [str(r["source"]) for r in rows] == ["icu_wellness", "manual", "icu_wellness"]
    settled = a_sync(fixture_db, pid, [resp([]), resp([scale])], tmp_path, frozen_clock)
    assert settled.weights_new == 0, "and the next hour writes nothing at all"


def test_04_a_month_away_from_the_scale_leaves_the_trend_where_it_was(
    fixture_db: Db, two_persons
) -> None:
    """Three kilos arrive as one weigh-in after four weeks. The EWMA takes a tenth of it, and
    `trend_delta(end, 7)` compares against the last point on or before end-7 - the month-old
    one - so the "over 7 days" line in the morning message and the delta Sunday's calibration
    divides by seven both come from two points 28 days apart. Pinned deliberately: the guard
    against a dry spell is the evening scale nudge, not this reader."""
    pid = two_persons["owner"]
    body.record_weigh_in(fixture_db, pid, dt.date(2026, 8, 3), 101.0, "manual")
    body.record_weigh_in(fixture_db, pid, dt.date(2026, 8, 31), 98.0, "manual")
    assert [(p.day, p.trend_kg, p.n) for p in body.trend_points(fixture_db, pid)] == [
        (dt.date(2026, 8, 3), 101.0, 1),
        (dt.date(2026, 8, 31), 100.7, 2),
    ], "the trend moved a tenth of what the scale moved"
    assert body.trend_delta(fixture_db, pid, dt.date(2026, 8, 31), 7) == -0.3
    assert body.trend_delta(fixture_db, pid, dt.date(2026, 8, 24), 7) is None, (
        "with one point and nothing since, no weekly figure is claimed at all"
    )
    assert body.days_since_weigh_in(fixture_db, pid, dt.date(2026, 8, 30)) == 27
    assert evening_confirm.SCALE_GAP_DAYS <= 7, "the dry spell is chased by the evening nudge"


def test_05_the_very_first_weigh_in_is_its_own_trend_and_has_nothing_to_compare_with(
    fixture_db: Db, two_persons
) -> None:
    pid = two_persons["owner"]
    body.record_weigh_in(fixture_db, pid, MONDAY, 100.9, "manual")
    point = body.latest_trend(fixture_db, pid, MONDAY)
    assert point is not None and point.trend_kg == 100.9 and point.n == 1
    assert body.trend_delta(fixture_db, pid, MONDAY, 7) is None
    assert body.latest_trend(fixture_db, pid, MONDAY - dt.timedelta(days=1)) is None
    assert targets.latest_weight(fixture_db, pid, MONDAY) == (MONDAY, 100.9)
    assert body.days_since_weigh_in(fixture_db, pid, MONDAY) == 0


def test_06_a_weigh_in_remembered_from_last_week_renumbers_the_trend_it_precedes(
    fixture_db: Db, two_persons
) -> None:
    """A weight typed in days later is a new row with an older day. The EWMA is a walk through
    the days in order, so the older row has to become the seed and everything after it has to
    be recomputed - not appended to the end, where it would read as a sudden drop."""
    pid = two_persons["owner"]
    body.record_weigh_in(fixture_db, pid, MONDAY, 100.0, "manual")
    body.record_weigh_in(fixture_db, pid, dt.date(2026, 9, 1), 101.0, "manual")
    points = body.trend_points(fixture_db, pid)
    assert [(p.day, p.n) for p in points] == [(dt.date(2026, 9, 1), 1), (MONDAY, 2)]
    assert points[0].trend_kg == 101.0, "the earliest weigh-in is the raw seed"
    assert points[1].trend_kg == 100.9
    assert fixture_db.scalar("SELECT COUNT(*) FROM trend_points") == 3, "Monday was rewritten"


# ---------------------------------------------------------------- days and boundaries
def test_07_the_day_the_clocks_go_forward_is_one_day_and_closes_once(
    fixture_db: Db, two_persons
) -> None:
    """29 March 2026 is 23 hours long. A job slot at 01:30 has no wall clock to run at, and the
    day itself must still be a single date that closes exactly once."""
    pid = two_persons["owner"]
    spring = dt.date(2026, 3, 29)
    assert ck.local_at(spring, "01:30") is None, "the hour that does not exist is skipped"
    assert ck.local_at(spring, "07:30") is not None
    days.confirm_day(fixture_db, pid, spring, basis="menu", macros=A_DAY)
    assert days.close_day(fixture_db, pid, spring) is None, "an answered day is not re-closed"
    row = days.latest_confirmation(fixture_db, pid, spring)
    assert row is not None and str(row["day"]) == "2026-03-29" and row["status"] == "confirmed"
    assert fixture_db.scalar("SELECT COUNT(*) FROM day_confirmations") == 1


def test_08_the_repeated_hour_when_the_clocks_go_back_is_still_one_day(
    fixture_db: Db, two_persons, frozen_clock: FrozenClock
) -> None:
    """25 October 2026 has 01:30 twice. Local time cannot tell the two apart; the UTC stamp
    can, which is the whole reason ledger rows are stamped in UTC."""
    pid = two_persons["owner"]
    autumn = dt.date(2026, 10, 25)
    frozen_clock.set_local(dt.datetime(2026, 10, 25, 0, 30, tzinfo=dt.UTC))  # 01:30 BST
    assert frozen_clock.today_local() == autumn and frozen_clock.now_local().hour == 1
    body.record_weigh_in(fixture_db, pid, autumn, 100.4, "manual")
    frozen_clock.set_local(dt.datetime(2026, 10, 25, 1, 30, tzinfo=dt.UTC))  # 01:30 GMT, again
    assert frozen_clock.today_local() == autumn and frozen_clock.now_local().hour == 1
    body.record_weigh_in(fixture_db, pid, autumn, 100.1, "manual")
    stamps = [str(r["created_at"]) for r in fixture_db.all("SELECT created_at FROM weigh_ins")]
    assert stamps == ["2026-10-25 00:30:00", "2026-10-25 01:30:00"], "UTC never repeats an hour"
    assert [(d, kg) for d, kg, _ in body.weigh_ins(fixture_db, pid)] == [(autumn, 100.1)]


def test_09_either_side_of_local_midnight_are_two_days_and_the_later_stamp_is_yesterdays(
    fixture_db: Db, two_persons, frozen_clock: FrozenClock
) -> None:
    """A day confirmed at 00:01 on 1 October is stamped 30 September in UTC, because British
    Summer Time is an hour ahead. Both are right, and the day is the London one."""
    pid = two_persons["owner"]
    frozen_clock.set_local(dt.datetime(2026, 9, 30, 23, 59))
    assert frozen_clock.today_local() == dt.date(2026, 9, 30)
    days.confirm_day(fixture_db, pid, frozen_clock.today_local(), basis="menu", macros=A_DAY)
    frozen_clock.set_local(dt.datetime(2026, 10, 1, 0, 1))
    assert frozen_clock.today_local() == dt.date(2026, 10, 1)
    days.confirm_day(fixture_db, pid, frozen_clock.today_local(), basis="menu", macros=A_DAY)
    rows = fixture_db.all("SELECT id, day, created_at FROM day_confirmations ORDER BY id")
    assert [str(r["day"]) for r in rows] == ["2026-09-30", "2026-10-01"]
    assert str(rows[1]["created_at"]) == "2026-09-30 23:01:00"
    september = days.latest_confirmation(fixture_db, pid, dt.date(2026, 9, 30))
    assert september is not None and september["id"] == rows[0]["id"], "October did not eat it"


def test_10_a_birthday_on_29_february_does_not_age_a_year_early(
    fixture_db: Db, two_persons
) -> None:
    """Age is a term in the BMR, so getting it a day early moves someone's calorie target by
    5 kcal on a date that only exists every fourth year."""
    pid = two_persons["owner"]
    born = dt.date(2004, 2, 29)
    assert energy.age_on(born, dt.date(2026, 2, 28)) == 21
    assert energy.age_on(born, dt.date(2026, 3, 1)) == 22
    assert energy.age_on(born, dt.date(2028, 2, 29)) == 24, "the birthday itself, in a leap year"
    early = energy.bmr_mifflin("f", 62.0, 168.0, energy.age_on(born, dt.date(2026, 2, 28)))
    after = energy.bmr_mifflin("f", 62.0, 168.0, energy.age_on(born, dt.date(2026, 3, 1)))
    assert early - after == 5.0
    leap = dt.date(2028, 2, 29)
    body.record_weigh_in(fixture_db, pid, leap, 99.0, "manual")
    assert [(d, kg) for d, kg, _ in body.weigh_ins(fixture_db, pid)] == [(leap, 99.0)]
    assert ck.week_start(leap) == dt.date(2028, 2, 28), "an ordinary Tuesday in an ordinary week"


def test_11_a_week_that_crosses_the_new_year_belongs_to_its_december_monday(
    fixture_db: Db, two_persons, config
) -> None:
    pid = two_persons["owner"]
    new_year = dt.date(2027, 1, 1)
    assert new_year.weekday() == 4, "a Friday"
    a_profile(fixture_db, pid, effective_from="2026-12-01")
    body.record_weigh_in(fixture_db, pid, dt.date(2026, 12, 30), 99.0, "manual")
    row = targets.ensure_targets(fixture_db, pid, new_year, config)
    assert row is not None and str(row["week_start"]) == "2026-12-28"
    later = targets.targets_for(fixture_db, pid, dt.date(2027, 1, 3))  # the Sunday of that week
    assert later is not None and later["id"] == row["id"]
    assert fixture_db.scalar("SELECT COUNT(*) FROM targets") == 1
    basis = json.loads(str(row["basis_json"]))
    assert basis["age"] == 36, "the age on the Monday the week starts, not on New Year's Day"


# ---------------------------------------------------------------- targets
def test_12_their_targets_need_their_own_weight_and_partners_will_not_do(
    fixture_db: Db, two_persons, config
) -> None:
    pid, partner = two_persons["owner"], two_persons["partner"]
    a_profile(fixture_db, pid)
    body.record_weigh_in(fixture_db, partner, SUNDAY, 61.0, "manual")
    assert targets.latest_weight(fixture_db, pid, MONDAY) is None
    assert body.days_since_weigh_in(fixture_db, pid, MONDAY) is None
    assert targets.ensure_targets(fixture_db, pid, MONDAY, config) is None
    assert fixture_db.scalar("SELECT COUNT(*) FROM targets") == 0
    body.record_weigh_in(fixture_db, pid, MONDAY, 100.9, "manual")
    row = targets.ensure_targets(fixture_db, pid, MONDAY, config)
    assert row is not None and row["person_id"] == pid
    assert targets.targets_for(fixture_db, partner, MONDAY) is None, "and she got none of his"


def test_13_the_kcal_floor_binds_and_the_basis_still_shows_what_the_rate_asked_for(
    fixture_db: Db, two_persons, config
) -> None:
    """A 1800 kcal floor typed at onboarding is above this person's whole estimated TDEE, so
    the target is a surplus. The floor wins - it is a guardrail - but the row has to record the
    TDEE and the deficit it overrode, or nobody can ever see why the number stopped moving."""
    pid = two_persons["owner"]
    a_profile(
        fixture_db,
        pid,
        sex="f",
        height_cm=160.0,
        activity_factor=1.1,
        rate_pct_per_week=1.0,
        kcal_floor=1800,
    )
    body.record_weigh_in(fixture_db, pid, MONDAY, 55.0, "manual")
    row = targets.ensure_targets(fixture_db, pid, MONDAY, config)
    assert row is not None and int(row["kcal"]) == 1800
    basis = json.loads(str(row["basis_json"]))
    assert basis["tdee"] < 1800, "the floor is above the whole estimate"
    assert basis["tdee"] - basis["deficit_kcal"] < 1800, "what the rate asked for is recorded"
    assert (int(row["protein_g"]), int(row["fat_g"])) == (110, 39), "the g/kg floors still hold"


def test_14_a_goal_date_that_has_already_passed_is_not_a_rate_of_infinity_to_the_person(
    fixture_db: Db, two_persons
) -> None:
    """With no weeks left the required rate really is infinite. The advisor's R10 has to catch
    that before it writes a sentence, so the number it carries must be recognisably infinite
    and the projected date must still be a real one."""
    today = MONDAY
    verdict = energy.pace_check(100.9, 87.0, dt.date(2026, 9, 1), today, 0.8)
    assert verdict.weeks_left == 0.0 and verdict.feasible is False
    assert verdict.required_kg_week == math.inf
    assert verdict.projected_date == today + dt.timedelta(days=math.ceil(13.9 / 0.8 * 7))
    assert verdict.projected_date is not None and verdict.projected_date > today
    on_the_day = energy.pace_check(100.9, 87.0, today, today, 0.8)
    assert on_the_day.weeks_left == 0.0 and not on_the_day.feasible


def test_15_a_goal_weight_above_todays_weight_asks_for_nothing_but_the_deficit_stays(
    fixture_db: Db, two_persons, config
) -> None:
    """He is already under the goal. The pace note goes quiet, and the weekly targets are
    computed from the rate alone, so they keep planning a loss. Worth pinning for the
    current target calculation."""
    pid = two_persons["owner"]
    verdict = energy.pace_check(86.0, 87.0, dt.date(2027, 1, 24), MONDAY, 0.8)
    assert verdict.feasible and verdict.required_kg_week == 0.0
    assert verdict.projected_date is None
    a_profile(fixture_db, pid, goal_weight_kg=87.0)
    body.record_weigh_in(fixture_db, pid, MONDAY, 86.0, "manual")
    row = targets.ensure_targets(fixture_db, pid, MONDAY, config)
    assert row is not None
    basis = json.loads(str(row["basis_json"]))
    assert basis["deficit_kcal"] > 0, "the goal is met and the week still plans a loss"


def test_16_a_profile_at_the_edges_of_the_schema_works_and_one_step_beyond_is_refused(
    fixture_db: Db, two_persons, config
) -> None:
    """The CHECKs are the last line before a formula: an activity factor of 2.5 or a rate of
    1.5 % a week would compute happily and hand someone a number that starves them."""
    pid = two_persons["owner"]
    a_profile(
        fixture_db,
        pid,
        height_cm=120.0,
        activity_factor=1.1,
        rate_pct_per_week=0.0,
        kcal_floor=1200,
    )
    body.record_weigh_in(fixture_db, pid, MONDAY, 30.0, "manual")  # the lightest the schema takes
    row = targets.ensure_targets(fixture_db, pid, MONDAY, config)
    assert row is not None and int(row["kcal"]) >= 1200
    tall = a_profile(
        fixture_db, pid, effective_from="2026-09-08", height_cm=230.0, activity_factor=2.0
    )
    profile = fixture_db.one("SELECT * FROM profiles WHERE id = ?", (tall,))
    assert profile is not None
    macros, basis = targets.formula_targets(profile, 250.0, MONDAY, config)
    assert macros.kcal >= 1800 and basis["tdee"] > basis["deficit_kcal"]
    for bad in (
        {"activity_factor": 2.1},
        {"activity_factor": 1.0},
        {"height_cm": 231.0},
        {"rate_pct_per_week": 1.5},
        {"rate_pct_per_week": -0.1},
        {"sex": "x"},
    ):
        with pytest.raises(sqlite3.IntegrityError):
            a_profile(fixture_db, pid, **bad)


def test_17_a_half_gram_of_carbs_rounds_up_not_to_the_nearest_even_number() -> None:
    """`food.recipes.round_int` exists with the comment "round() is banker's" because the house
    rule is half-up. The energy model is the other half of the same arithmetic and uses the
    bare builtin, so 142.5 g of carbs becomes 142 g here and 143 g in a recipe."""
    got = (energy.macro_split(2000, 100.0).carb_g, energy.macro_split(2000.5, 100.0).kcal)
    assert got == (143, 2001), "half rounds up, the way the food code already does it"


# ---------------------------------------------------------------- training
def test_18_a_session_on_a_day_with_no_plan_at_all_is_an_extra_not_a_crash(
    fixture_db: Db, two_persons
) -> None:
    pid, partner = two_persons["owner"], two_persons["partner"]
    an_activity_row(fixture_db, pid, "ride-1", "ride", 45 * 60)
    an_activity_row(fixture_db, partner, "her-ride", "ride", 50 * 60)
    rows = training.match_day(fixture_db, pid, MONDAY)
    assert [(r["icu_id"], r["status"], r["planned_session_id"], r["minutes"]) for r in rows] == [
        ("ride-1", "extra", None, 45)
    ]
    assert training.planned_sessions(fixture_db, pid, MONDAY) == []
    assert days.matched_session(fixture_db, pid, MONDAY) is None, "nothing to confirm"
    assert training.match_day(fixture_db, pid, MONDAY) == [], "and it is written once"
    people = [r["person_id"] for r in fixture_db.all("SELECT person_id FROM session_matches")]
    assert people == [pid], "her ride is not in his day"


def test_19_a_planned_session_that_never_happened_leaves_no_row_behind(
    fixture_db: Db, two_persons
) -> None:
    """A miss is the absence of a match, never an invented row. The evening check-in still has
    to know the session was planned, so it can ask whether it happened off-Garmin."""
    pid = two_persons["owner"]
    a_plan(fixture_db, pid, (0, "climb", 90))
    assert training.match_day(fixture_db, pid, MONDAY) == []
    assert fixture_db.all("SELECT * FROM session_matches") == []
    assert days.matched_session(fixture_db, pid, MONDAY) is None
    assert days.session_decided(fixture_db, pid, MONDAY) is None
    assert [str(r["kind"]) for r in days.planned_sessions_on(fixture_db, pid, MONDAY)] == ["climb"]


def test_20_a_session_three_times_longer_than_planned_is_not_that_session(
    fixture_db: Db, two_persons
) -> None:
    pid = two_persons["owner"]
    a_plan(fixture_db, pid, (0, "climb", 30))
    an_activity_row(fixture_db, pid, "epic", "climb", 150 * 60)
    rows = training.match_day(fixture_db, pid, MONDAY)
    assert [(r["status"], r["planned_session_id"], r["minutes"]) for r in rows] == [
        ("extra", None, 150)
    ], "an extra, with its minutes, so the person still sees the afternoon they had"
    assert training.duration_ok(30, 150 * 60, 152 * 60) is False
    assert days.matched_session(fixture_db, pid, MONDAY) is None


def test_21_a_session_kind_the_plan_does_not_know_never_lands(fixture_db: Db, two_persons) -> None:
    """`kind` is the join between a plan and what intervals.icu saw, through icu_type_map.
    A kind outside the vocabulary would match nothing for ever and say nothing about it."""
    pid = two_persons["owner"]
    plan_id = fixture_db.insert("training_plans", person_id=pid, effective_from="2026-09-01")
    for bad in ("yoga", "spin", "", "CLIMB"):
        with pytest.raises(sqlite3.IntegrityError):
            fixture_db.insert(
                "planned_sessions",
                plan_id=plan_id,
                person_id=pid,
                weekday=0,
                kind=bad,
                target_minutes=30,
            )
    fixture_db.insert(
        "planned_sessions",
        plan_id=plan_id,
        person_id=pid,
        weekday=0,
        kind="mobility",  # which is how yoga is spelled here
        target_minutes=30,
    )
    for bad_field in ({"weekday": 7}, {"target_minutes": 601}, {"target_minutes": -1}):
        row: dict[str, Any] = {
            "plan_id": plan_id,
            "person_id": pid,
            "weekday": 0,
            "kind": "climb",
            "target_minutes": 30,
        }
        row.update(bad_field)
        with pytest.raises(sqlite3.IntegrityError):
            fixture_db.insert("planned_sessions", **row)


# ---------------------------------------------------------------- intervals.icu
def test_22_a_day_with_no_activities_syncs_quietly_and_still_records_the_run(
    fixture_db: Db, two_persons, tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    """A rest day returns two empty lists. Nothing is archived, and the run is still written
    down - an hour that found nothing has to be distinguishable from an hour that never ran."""
    pid = two_persons["owner"]
    result = a_sync(fixture_db, pid, [resp([]), resp([])], tmp_path, frozen_clock)
    assert result == icu.SyncResult(requests=2)
    assert fixture_db.scalar("SELECT COUNT(*) FROM icu_raw") == 0
    assert fixture_db.scalar("SELECT COUNT(*) FROM icu_activity") == 0
    assert training.match_day(fixture_db, pid, frozen_clock.today_local()) == []
    state = fixture_db.one("SELECT v FROM icu_sync_state WHERE k = ?", (f"last_sync:{pid}",))
    assert state is not None and json.loads(str(state["v"]))["requests"] == 2


def test_23_an_activity_with_no_duration_is_still_recorded_with_no_minutes(
    fixture_db: Db, two_persons, tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    """A session typed into intervals.icu by hand can carry no times at all. It cannot be
    matched on duration, so it lands as an extra the person can still claim by tapping."""
    pid = two_persons["owner"]
    a_plan(fixture_db, pid, (0, "climb", 90))
    naked = an_activity("i5", "2026-09-07T09:00:00Z", moving_time=None, elapsed_time=None)
    a_sync(fixture_db, pid, [resp([naked]), resp([])], tmp_path, frozen_clock)
    row = fixture_db.one("SELECT * FROM icu_activity WHERE icu_id = 'i5'")
    assert row is not None and str(row["day"]) == "2026-09-07"
    assert row["moving_s"] is None and row["elapsed_s"] is None
    assert training.duration_ok(90, None, None) is False
    rows = training.match_day(fixture_db, pid, MONDAY)
    assert [(r["status"], r["minutes"]) for r in rows] == [("extra", None)]


def test_24_the_same_activity_id_twice_in_one_response_is_archived_once(
    fixture_db: Db, two_persons, tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    """Icu_raw is keyed (person, resource, source_id, revision) and the head revisions are read
    once before the loop, so a list that repeats an id asks for revision 1 twice. A glitch
    upstream must not take the hour's wellness and weights down with it."""
    pid = two_persons["owner"]
    twice = an_activity("i7", "2026-09-06T07:00:00Z")
    scale = a_wellness("2026-09-06", weight=100.4)
    result = a_sync(
        fixture_db, pid, [resp([twice, dict(twice)]), resp([scale])], tmp_path, frozen_clock
    )
    assert result.activities_new == 1
    assert fixture_db.scalar("SELECT COUNT(*) FROM icu_raw WHERE resource = 'activity'") == 1
    assert fixture_db.scalar("SELECT COUNT(*) FROM icu_activity") == 1
    assert [kg for _d, kg, _i in body.weigh_ins(fixture_db, pid)] == [100.4], "the weight landed"


def test_25_the_per_second_samples_never_reach_the_database(
    fixture_db: Db, two_persons, tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    """Sample arrays are refused everywhere by design: they go to a content-addressed sidecar
    under the archive and the database keeps only their lengths. A thousand-point heart-rate
    stream in icu_raw would be the database growing by a megabyte an activity."""
    pid = two_persons["owner"]
    ride = an_activity("i8", "2026-09-06T07:00:00Z", type_="Ride", stream_types=["time", "hr"])
    samples = [
        {"type": "time", "data": list(range(1000))},
        {"type": "heartrate", "data": [120 + (i % 40) for i in range(1000)]},
    ]
    a_sync(fixture_db, pid, [resp([ride]), resp(samples), resp([])], tmp_path, frozen_clock)
    index = fixture_db.one("SELECT * FROM icu_stream_index WHERE icu_activity_id = 'i8'")
    assert index is not None and int(index["samples"]) == 1000
    sidecar = Path(str(index["sidecar"]))
    assert sidecar.is_file() and str(tmp_path) in str(sidecar)
    assert json.loads(sidecar.read_text(encoding="utf-8"))[0]["data"][:3] == [0, 1, 2]
    archive = "".join(str(r["payload"]) for r in fixture_db.all("SELECT payload FROM icu_raw"))
    assert '"data"' not in archive and '"data2"' not in archive
    assert len(archive) < 4000, "the archive row is a summary, not the samples"
    summary = json.loads(
        str(
            fixture_db.scalar(
                "SELECT payload FROM icu_raw WHERE resource = 'streams_index' AND source_id = 'i8'"
            )
        )
    )
    assert {int(s["n"]) for s in summary["streams"]} == {1000}
