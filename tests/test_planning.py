"""How each person plans and cooks, changed whenever they like (food/planning.py).

want to do something else. the partner as well. What if at one point she decides to batch cook?
What if
I at one point decide to cook meals daily? ... if we were to add more users, for example, or
scale."

The world is the swap tests' (tests/test_swap.py): her Monday-to-Friday batch cook of lunch and
dinner, next week's menu in force, the frozen clock on Monday 7 Sep.
"""

from __future__ import annotations

import datetime as dt
import json
from types import SimpleNamespace
from typing import Any

from tiffin import targets
from tiffin.clock import isoday
from tiffin.food import choices, menu, planning, plates, shared, shopping
from tiffin.jobs import menu_propose, morning_targets
from tiffin.onboarding import STEP_BY_ID
from tiffin.telegram import settings

from .test_choices import SUNDAY, ChoicesModel, _App, _consent, _liked_long_ago
from .test_swap import (  # noqa: F401
    FRI,
    MON,
    OWNER_LOGIN,
    _headers,
    _profile,
    page,
    week,
)
from .web_source import app_source

TODAY = dt.date(2026, 9, 7)  # the frozen Monday
DAILY = json.dumps([[0], [1], [2], [3], [4]])


def _spans(segments: list[menu.Segment]) -> list[tuple[str, str]]:
    return [(isoday(s.start), isoday(s.end)) for s in segments]


def _day(n: int) -> str:
    return isoday(MON + dt.timedelta(days=n))


# ---------------------------------------------------------------- when a change counts
def test_a_change_booked_for_next_monday_leaves_this_week_as_planned(week) -> None:  # noqa: F811
    db, pid, _base, _r, _clock = week
    start = planning.change(db, pid, today=TODAY, cadence="daily")
    assert start == MON, "the next week nothing of is planned"
    assert _spans(menu.week_segments(db, pid, TODAY)) == [
        ("2026-09-07", "2026-09-11"),
        ("2026-09-12", "2026-09-13"),
    ], "this week's batch, as it was planned and maybe bought"
    assert _spans(menu.week_segments(db, pid, MON))[:5] == [(_day(i), _day(i)) for i in range(5)]


def test_a_change_from_tomorrow_splits_the_cook_it_lands_in(week) -> None:  # noqa: F811
    db, pid, _base, _r, _clock = week
    tuesday = dt.date(2026, 9, 8)
    planning.change(db, pid, today=tuesday, starts="tomorrow", cadence="daily")
    assert _spans(menu.week_segments(db, pid, TODAY)) == [
        ("2026-09-07", "2026-09-08"),
        ("2026-09-09", "2026-09-09"),
        ("2026-09-10", "2026-09-10"),
        ("2026-09-11", "2026-09-11"),
        ("2026-09-12", "2026-09-13"),
    ], "Monday and Tuesday stay the batch's own; from Wednesday a cook a day"


def test_a_setting_changed_midweek_that_is_not_how_they_cook_never_splits_a_cook(week) -> None:  # noqa: F811
    db, pid, _base, _r, _clock = week
    targets.change_profile(db, pid, {"rhythm": "weekly"}, today=dt.date(2026, 9, 9))
    assert _spans(menu.week_segments(db, pid, TODAY))[0] == ("2026-09-07", "2026-09-11")


def test_a_change_made_today_still_holds_after_one_booked_for_monday(week) -> None:  # noqa: F811
    db, pid, _base, _r, _clock = week
    planning.change(db, pid, today=TODAY, planning="choose")
    targets.change_profile(db, pid, {"rhythm": "weekly"}, today=TODAY)
    now, monday = targets.profile_on(db, pid, TODAY), targets.profile_on(db, pid, MON)
    assert now is not None and monday is not None
    assert now["rhythm"] == "weekly" and planning.planning_of(now) == "auto"
    assert monday["rhythm"] == "weekly", "the change made since the booking is not undone on Monday"
    assert planning.planning_of(monday) == "choose" and planning.options_of(monday) == 3


def test_set_books_how_they_plan_from_monday_and_everything_else_from_today(week) -> None:  # noqa: F811
    db, pid, _base, _r, clock = week
    start = settings.write_setting(db, pid, settings.BY_NAME["cook-days"], "daily", clock=clock)
    assert start == MON
    assert planning.cadence_of(targets.profile_on(db, pid, TODAY)) == "batch"
    assert planning.cadence_of(targets.profile_on(db, pid, MON)) == "daily"
    assert (
        settings.write_setting(db, pid, settings.BY_NAME["rhythm"], "weekly", clock=clock) == TODAY
    )
    # a number of dishes a meal is choosing them, and none is the menu built as before
    settings.write_setting(db, pid, settings.BY_NAME["choices"], 4, clock=clock)
    assert planning.plan_of(targets.profile_on(db, pid, MON)).planning == "choose"
    settings.write_setting(db, pid, settings.BY_NAME["choices"], 0, clock=clock)
    assert planning.plan_of(targets.profile_on(db, pid, MON)).planning == "auto"


def test_a_row_from_before_the_setting_reads_the_way_it_was_written(week) -> None:  # noqa: F811
    db, pid, _base, _r, _clock = week
    _profile(db, pid, effective_from="2026-09-07", choices=3)
    assert planning.planning_of(targets.profile_on(db, pid, TODAY)) == "choose"
    _profile(db, pid, effective_from="2026-09-07", choices=3, planning="off")
    assert planning.planning_of(targets.profile_on(db, pid, TODAY)) == "off", (
        "said in so many words"
    )


# ---------------------------------------------------------------- cooking each day
def test_a_day_cooked_on_its_own_is_never_a_box_whatever_the_batch_setting_says(week) -> None:  # noqa: F811
    db, pid, _base, _r, _clock = week
    tuesday = dt.date(2026, 9, 8)
    assert plates.boxed_slots(db, pid, tuesday) == {"lunch", "dinner"}, "a Mon-Fri batch"
    _profile(db, pid, effective_from="2026-09-07", segment_days=DAILY)
    assert plates.boxed_slots(db, pid, tuesday) == frozenset(), "a cook a day: nothing in a box"
    profile = targets.profile_on(db, pid, TODAY)
    assert profile is not None
    assert planning.batched(profile) == ()
    assert menu.batch_slots(profile) == ()
    assert "you cook each day" in planning.plan_of(profile).said()


# ---------------------------------------------------------------- tracking only
def test_tracking_only_plans_nothing_and_says_so_rather_than_a_missing_menu(week, config) -> None:  # noqa: F811
    db, pid, _base, _r, _clock = week
    _profile(db, pid, effective_from="2026-09-07", planning="off")
    profile = targets.profile_on(db, pid, TODAY)
    assert menu.week_segments(db, pid, MON) == [] and menu.segments_for(profile, MON) == []
    assert menu_propose.segment_on_demand(profile, TODAY, weeks=menu.weeks_of(db, pid)) is None, (
        "/cook finds nothing to plan"
    )
    assert not shared.possible(db, shared.weekend_of(TODAY)), "no shared weekend either"
    # a menu already planned stays (it may be bought): a day after it has none, by choice
    ctx: Any = SimpleNamespace(db=db, person_id=pid, day=dt.date(2026, 9, 22), profile=profile)
    assert morning_targets.meals_line(ctx, None, linked=False).startswith("Tracking only")
    t = targets.latest_targets(db, pid, dt.date(2026, 9, 22))
    assert t is not None
    assert morning_targets.nothing_new(ctx, None, t), "the same targets as yesterday: no morning"
    assert "Tracking only" in planning.plan_of(profile).said()


# ---------------------------------------------------------------- choosing, and batch cooking
def _choosing_batch(week, config, segments: str | None = None):  # noqa: F811
    db, pid, _base, _r, clock = week
    clock.set_local(SUNDAY)
    _liked_long_ago(db, pid)
    extra = {"segment_days": segments} if segments else {}
    _profile(db, pid, effective_from="2026-09-13", planning="choose", choices=3, **extra)
    _consent(db, pid)
    built = choices.build(_App(ChoicesModel(), config), db, pid, clock=clock)
    assert built.set_id is not None and not built.failed, built.refused
    return db, pid, clock, built


def test_a_batch_cook_chooses_each_meal_made_ahead_once_for_the_whole_cook(week, config) -> None:  # noqa: F811
    db, pid, clock, built = _choosing_batch(week, config)
    assert built.set_id is not None
    options = choices.options_of(db, built.set_id)
    lunch = sorted((o for o in options if o["slot"] == "lunch"), key=lambda o: int(o["position"]))
    assert len(lunch) == 3, "three dishes for the week's one pot"
    assert all(o["day"] == _day(0) and o["last_day"] == _day(4) for o in lunch)
    breakfast = {str(o["day"]) for o in options if o["slot"] == "breakfast"}
    assert breakfast == {_day(i) for i in range(5)}, "made fresh, so offered each day"
    assert all(o["last_day"] is None for o in options if o["slot"] == "breakfast")
    # the page offers the pot on its first day and names it on the others
    picks = choices.picks_of(db, built.set_id)
    slots = ["breakfast", "lunch", "dinner"]
    tuesday = choices.day_view(db, _day(1), slots, options, picks, None, 20)
    pot = next(s for s in tuesday["slots"] if s["slot"] == "lunch")
    assert pot["from_batch"] == _day(0) and len(pot["options"]) == 1
    monday = choices.day_view(db, _day(0), slots, options, picks, None, 20)
    first = next(s for s in monday["slots"] if s["slot"] == "lunch")
    assert first["batch"] == {"from": _day(0), "to": _day(4), "days": 5}
    assert len(first["options"]) == 3
    # one pick is the whole week's lunch, every box the same size
    second = lunch[1]
    choices.pick(db, pid, built.set_id, int(second["id"]), clock=clock)
    settled = choices.settle(_App(None, config), db, pid, built.set_id, how="chosen", clock=clock)
    assert len(settled.menu_ids) == 1, "one cook, one menu"
    assert settled.chosen == 1
    rows = db.all(
        "SELECT ms.recipe_id, ms.portion_multiplier FROM v_menu_days_current md "
        "JOIN meal_slots ms ON ms.menu_day_id = md.id "
        "WHERE md.person_id = ? AND ms.slot = 'lunch' AND md.day BETWEEN ? AND ?",
        (pid, _day(0), _day(4)),
    )
    assert len(rows) == 5
    assert {int(r["recipe_id"]) for r in rows} == {int(second["recipe_id"])}
    assert len({round(float(r["portion_multiplier"]), 4) for r in rows}) == 1


def test_a_week_chosen_by_a_two_batch_cook_is_bought_in_one_list(week, config) -> None:  # noqa: F811
    db, pid, clock, built = _choosing_batch(week, config, json.dumps([[0, 1, 2], [3, 4]]))
    assert built.set_id is not None
    settled = choices.settle(_App(None, config), db, pid, built.set_id, how="kept", clock=clock)
    assert len(settled.menu_ids) == 2, "a menu for each batch"
    shown = shopping.list_to_show(db, pid, dt.date(2026, 9, 13), clock=clock)
    assert shown is not None and shown["kind"] == "range"
    assert (shown["from_day"], shown["to_day"]) == (_day(0), _day(4)), "both batches, one shop"


# ---------------------------------------------------------------- the interview
def test_the_interview_asks_the_cooking_questions_only_where_they_apply() -> None:
    def asks(step: str, **answers: object) -> bool:
        return STEP_BY_ID[step].applies(dict(answers), None)  # type: ignore[arg-type]

    assert not asks("segs", pln="off") and not asks("vary", pln="off"), "tracking only"
    assert asks("segs", pln="auto") and asks("segs", pln="choose")
    assert not asks("batch", pln="auto", segs="daily"), "a cook a day has nothing to batch"
    assert asks("batch", pln="auto", segs="mf")
    assert not asks("frz", pln="auto", segs="mf", batch=[]), "nothing made ahead, nothing frozen"
    assert asks("frz", pln="auto", segs="mw", batch=["dinner"])


# ---------------------------------------------------------------- the page
def test_the_page_books_how_they_plan_and_says_from_when(page) -> None:  # noqa: F811
    client, _db, _partners, _his, _r = page
    h = _headers(client)
    got = client.get("/api/plan", headers=h).json()
    assert got["now"]["planning"] == "auto" and got["now"]["cadence"] == "batch"
    assert got["booked"] is None
    r = client.post("/api/plan", json={"planning": "off", "starts": "monday"}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["said"].startswith("From Mon 14 Sep: Tracking only")
    me = client.get("/api/me", headers=h).json()
    assert me["plan"]["now"]["planning"] == "auto", "this week stays as it was planned"
    assert (
        me["plan"]["booked"]["planning"] == "off" and me["plan"]["booked"]["from"] == "2026-09-14"
    )
    # tomorrow, and a cook a day, said with what happens to the days already planned
    r = client.post("/api/plan", json={"cadence": "daily", "starts": "tomorrow"}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["said"].startswith("From tomorrow:")
    # only the answers there are, and never a person from the body
    for bad in (
        {"planning": "sometimes"},
        {"options": True},
        {"options": 7},
        {"cadence": "fortnightly"},
        {"batch": "lunch"},
        {"starts": "yesterday", "planning": "auto"},
        {"person": "owner", "planning": "off"},
    ):
        assert client.post("/api/plan", json=bad, headers=h).status_code == 400, bad
    # his is his own, and he has no profile in this world
    assert client.get("/api/plan", headers=_headers(client, OWNER_LOGIN)).status_code == 409


def test_the_page_offers_how_you_plan_in_more() -> None:

    js = app_source()
    assert 'api("api/plan", { body: { ...changes, starts: draft.starts } })' in js
    for label in ('"Planned for me"', '"I choose"', '"Track only"', '"Each day"', '"Two batches"'):
        assert label in js, label
    assert "You track only, so no menus are planned." in js
    assert "function choicesPanel" not in js, "the old switch is How you plan now"
