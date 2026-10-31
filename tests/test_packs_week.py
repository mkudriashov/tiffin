"""
the whole week, but taking into account proper pack sizes. So we do not make a mistake of buying a
1kg of chicken that has only 200g portion over the whole week").

Someone who cooks each day gets the rest of the week in one build (`menu_propose.week_run`), bought
on one list; a build that closes its shop window is held to using at least half of each meat or
fish pack it buys (`packs.Steer.wasted`), re-asked by number, and on the last attempt kept and said.

The world is tests/test_packs_build.py's: the owner cooking each day, Monday's chicken in force.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import re

import pytest

from tiffin import targets
from tiffin.clock import TZ, isoday
from tiffin.errors import ModelInvalid
from tiffin.food import catalogue, menu, packs
from tiffin.jobs import menu_propose, shopping_list

from .test_menu import world  # noqa: F401
from .test_packs_build import MON, WED, PackModel, _day, daily  # noqa: F401

THU, FRI = WED + dt.timedelta(days=1), WED + dt.timedelta(days=2)
SAT, SUN = MON - dt.timedelta(days=2), MON - dt.timedelta(days=1)
CHICKEN = ("Chicken breast, raw", "meat")


def _profile(db, pid, effective_from: str, **changed) -> None:
    """Another profiles row, as the daily fixture's but for `changed`."""
    row = dict(db.one("SELECT * FROM profiles WHERE person_id = ? ORDER BY id DESC", (pid,)))
    for key in ("id", "created_at"):
        row.pop(key, None)
    db.insert("profiles", **{**row, "effective_from": effective_from, **changed})


def _numbers(db, pid) -> dict[str, int]:
    items = catalogue.approved(db, pid)
    return {re.findall(r"[a-z]+", i.name.lower())[0]: k for k, i in enumerate(items, start=1)}


# ---------------------------------------------------------------- the week in one build
def test_a_daily_cook_gets_the_rest_of_the_week_in_one_build(daily) -> None:  # noqa: F811
    pid, db, *_ = daily
    assert menu_propose.week_run(db, pid, menu.Segment(WED, WED)) == menu.Segment(WED, FRI)
    # Monday has its menu: nothing to widen, and Monday is never planned again
    assert menu_propose.week_run(db, pid, menu.Segment(MON, MON)) == menu.Segment(MON, MON)
    # the weekend is the joint shop's, and None stays None
    assert menu_propose.week_run(db, pid, menu.Segment(SAT, SAT)) == menu.Segment(SAT, SAT)
    assert menu_propose.week_run(db, pid, None) is None


def test_the_scheduled_build_plans_wednesday_to_friday(daily) -> None:  # noqa: F811
    pid, db, _tg, gateways, run_job, clock = daily
    gateways.append(PackModel())  # type: ignore[arg-type]
    clock.set_local(dt.datetime(2026, 9, 14, 12, 0, tzinfo=TZ))
    assert run_job(menu_propose.JOB, MON) in ("ok", "degraded")
    winner = menu.accepted_menu_id(db, pid, WED)
    row = db.one("SELECT segment_start, segment_end FROM menus WHERE id = ?", (winner,))
    assert (row["segment_start"], row["segment_end"]) == (isoday(WED), isoday(FRI))
    assert menu.accepted_menu_id(db, pid, FRI) == winner
    # built, not last week's repeated
    detail = str(db.scalar("SELECT detail FROM run_ledger ORDER BY rowid DESC LIMIT 1"))
    assert "proposed" in detail and "repeated" not in detail, detail


def test_a_change_booked_from_thursday_stops_the_run_there(daily) -> None:  # noqa: F811
    pid, db, *_ = daily
    _profile(db, pid, isoday(THU), planning="off")
    assert menu_propose.week_run(db, pid, menu.Segment(WED, WED)) == menu.Segment(WED, WED)


def test_someone_who_chooses_their_week_is_not_widened(daily) -> None:  # noqa: F811
    pid, db, *_ = daily
    _profile(db, pid, "2026-09-01", planning="choose", choices=3)
    assert menu_propose.week_run(db, pid, menu.Segment(WED, WED)) == menu.Segment(WED, WED)


# ---------------------------------------------------------------- one list for the week
def _week_menu(db, pid) -> int:
    """Wednesday to Friday built in one, chicken every lunch and dinner, in force."""
    n = _numbers(db, pid)
    t = db.one("SELECT * FROM targets WHERE person_id = ?", (pid,))
    plan = menu.build_plan(
        db,
        pid,
        menu.Segment(WED, FRI),
        {"days": [_day(d, n, "chicken", isoday(d)) for d in (WED, THU, FRI)]},
        targets=targets.macros_of(t),
        profile=db.one("SELECT * FROM profiles WHERE person_id = ? ORDER BY id DESC", (pid,)),
        items=catalogue.approved(db, pid),
    )
    mid = menu.store_plan(db, plan, status="proposed")
    menu.decide(db, mid, "accepted")
    return mid


def test_the_week_is_bought_on_one_list_and_no_day_buys_it_again(daily) -> None:  # noqa: F811
    pid, db, tg, _gateways, run_job, _clock = daily
    mid = _week_menu(db, pid)
    tg.sent.clear()
    assert run_job(shopping_list.JOB, MON) == "ok"  # two days before Wednesday
    assert tg.sent[-1]["text"].startswith("<b>Shopping for Wed 16 Sep to Fri 18 Sep</b>")
    lists = db.scalar("SELECT COUNT(*) FROM shopping_lists WHERE menu_id = ?", (mid,))
    assert lists == 1
    tg.sent.clear()
    # Tuesday's run is for Thursday, which Wednesday's list already bought
    assert run_job(shopping_list.JOB, WED - dt.timedelta(days=1)) == "skipped"
    assert tg.sent == []
    assert db.scalar("SELECT COUNT(*) FROM shopping_lists WHERE menu_id = ?", (mid,)) == lists
    detail = db.scalar("SELECT detail FROM run_ledger ORDER BY rowid DESC LIMIT 1")
    assert "bought with the list for Wed 16 Sep" in str(detail)


# ---------------------------------------------------------------- packs used, not opened and left
def _foods() -> dict[str, tuple[str, str]]:
    return {"chicken": CHICKEN, "mince": ("Beef mince 5% fat, raw", "meat")}


#: one shop window, Monday to Friday, as its iso days
WEEK = tuple(isoday(MON + dt.timedelta(days=i)) for i in range(5))


def _on(day: int, grams: float) -> dict[str, float]:
    return {WEEK[day]: grams}


def test_a_kilo_bought_for_one_dinner_is_re_asked_by_number() -> None:
    steer = packs.Steer(sizes={"chicken": 1000.0}, windows=(WEEK,))
    assert steer.wasted({"chicken": _on(2, 200.0)}, _foods(), {"chicken": 7}) == [
        "Chicken breast, raw (number 7) is bought as 1 kg for Mon to Fri and those days cook only "
        "200 g of it, 800 g never eaten: put it in more of those days' meals, or use a food those "
        "days already cook"
    ]
    # ...and when the last attempt keeps it, the person is told in their words
    assert steer.wasted({"chicken": _on(2, 200.0)}, _foods(), {}, person=True) == [
        "Chicken breast uses 200 g of the 1 kg bought for Mon to Fri: no menu using more of it "
        "met your targets"
    ]


def test_rounding_a_little_over_whole_packs_is_kept() -> None:
    steer = packs.Steer(sizes={"mince": 500.0}, windows=(WEEK,))
    assert steer.wasted({"mince": _on(0, 682.0)}, _foods(), {}) == []
    # under a meal's worth over is no waste worth a re-ask, whatever the share
    small = packs.Steer(sizes={"chicken": 300.0}, windows=(WEEK,))
    assert small.wasted({"chicken": _on(0, 120.0)}, _foods(), {}) == []


def test_the_windows_other_days_count_towards_the_pack() -> None:
    # Monday plated 400 g of the kilo: Wednesday's 200 g makes 600 g, more than half
    steer = packs.Steer(sizes={"chicken": 1000.0}, used={"chicken": _on(0, 400.0)}, windows=(WEEK,))
    assert steer.wasted({"chicken": _on(2, 200.0)}, _foods(), {}) == []


def test_each_shop_window_is_weighed_on_its_own() -> None:
    # the partner's two shops: chicken on Monday (400 g of the kilo) and Thursday (400 g) is two
    # half-used kilos, one a shop - not one kilo used 80%
    first, second = WEEK[:3], WEEK[3:]
    steer = packs.Steer(sizes={"chicken": 1000.0}, windows=(first, second))
    got = steer.wasted({"chicken": {WEEK[0]: 400.0, WEEK[3]: 400.0}}, _foods(), {})
    assert len(got) == 2 and "for Mon to Wed" in got[0] and "for Thu to Fri" in got[1], got


def test_the_pack_that_leaves_least_over_is_the_one_weighed() -> None:
    # 300 g of mince: a 500 g pack leaves 200 g, a 250 g pack twice leaves 200 g, two 300 g...
    # with 250 g and 750 g on the shelf, 300 g is best bought as... 2 x 250 g (200 over) rather
    # than 750 g (450 over); with a 300 g pack too, it is that, and nothing is over
    shelf = packs.Steer(options={"mince": (250.0, 300.0, 750.0)}, windows=(WEEK,))
    assert shelf.least_left("mince", 300.0) == (1, 300.0, 0.0)
    near = packs.Steer(options={"mince": (250.0, 750.0)}, windows=(WEEK,))
    assert near.least_left("mince", 300.0) == (2, 250.0, 200.0)


def test_vegetables_are_weighed_only_for_someone_who_asked() -> None:
    spinach = {"spinach": ("Spinach, raw", "produce")}
    grams = {"spinach": _on(1, 40.0)}
    house = packs.Steer(sizes={"spinach": 240.0}, windows=(WEEK,))
    assert house.wasted(grams, spinach, {}) == []
    theirs = packs.Steer(sizes={"spinach": 240.0}, windows=(WEEK,), fresh=("produce",))
    assert (
        theirs.wasted(grams, spinach, {}) and "Spinach, raw" in theirs.wasted(grams, spinach, {})[0]
    )


def test_what_is_not_refused() -> None:
    grams = {"chicken": _on(2, 200.0)}
    # days of the window still to plan may use the rest: no window closed
    assert packs.Steer(sizes={"chicken": 1000.0}).wasted(grams, _foods(), {}) == []
    # "no limit" is their own answer, and refuses nothing
    assert (
        packs.Steer(sizes={"chicken": 1000.0}, windows=(WEEK,), budget=0).wasted(
            grams, _foods(), {}
        )
        == []
    )
    # a food their note asks for by name is theirs to have
    asked = packs.Steer(sizes={"chicken": 1000.0}, windows=(WEEK,), note="chicken on Friday please")
    assert asked.wasted(grams, _foods(), {}) == []
    # a food no pack size is known for is not guessed at
    assert packs.Steer(windows=(WEEK,)).wasted(grams, _foods(), {}) == []


def test_the_steer_knows_the_packs_and_whether_the_build_closes_the_week(daily) -> None:  # noqa: F811
    pid, db, *_ = daily
    items = catalogue.approved(db, pid)
    allowed = {i.code for i in items}
    week = packs.steer_for(db, pid, [WED, THU, FRI], allowed=allowed, items=items)
    assert week.closes
    assert week.sizes["chicken"] == 1000.0 and week.sizes["turkey"] == 500.0
    assert sum(week.used["chicken"].values()) > 0  # Monday's lunch and dinner
    # Wednesday alone leaves Thursday and Friday to plan; the weekend is the joint list's
    assert not packs.steer_for(db, pid, [WED], allowed=allowed, items=items).closes
    assert not packs.steer_for(db, pid, [SAT, SUN], allowed=allowed, items=items).closes
    # the prompt names each pack size by number, only when the build closes the week
    n = {i.code: k for k, i in enumerate(items, start=1)}
    _open, line = week.prompt(n)
    assert f"number {n['chicken']}: 1000 g" in line and "use most of its pack" in line
    alone = packs.steer_for(db, pid, [WED], allowed=allowed, items=items)
    assert "use most of its pack" not in alone.prompt(n)[1]


def _turkey_lunch_week(db, pid) -> dict:
    """Wednesday to Friday on chicken, but for 100 g of turkey in Wednesday's lunch: a 500 g pack
    opened for one dish."""
    n = _numbers(db, pid)
    days = [_day(d, n, "chicken", isoday(d)) for d in (WED, THU, FRI)]
    lunch = days[0]["slots"][1]["recipe"]
    lunch["ingredients"][0] = {"i": n["turkey"], "g": 100}
    lunch["name"] = "Turkey rice bowl"
    return {"days": days}


def test_a_build_that_leaves_a_pack_mostly_unused_is_refused_then_kept_and_said(daily) -> None:  # noqa: F811
    pid, db, *_ = daily
    items = catalogue.approved(db, pid)
    steer = packs.steer_for(db, pid, [WED, THU, FRI], allowed={i.code for i in items}, items=items)
    # no open pack to cook from first: only the waste is in question
    steer = dataclasses.replace(steer, open=())
    t = db.one("SELECT * FROM targets WHERE person_id = ?", (pid,))
    kwargs = {
        "targets": targets.macros_of(t),
        "profile": db.one("SELECT * FROM profiles WHERE person_id = ? ORDER BY id DESC", (pid,)),
        "items": items,
    }
    seg = menu.Segment(WED, FRI)
    with pytest.raises(ModelInvalid, match=r"Turkey breast, raw \(number \d+\) is bought as 500 g"):
        menu.build_plan(db, pid, seg, _turkey_lunch_week(db, pid), steer=steer, **kwargs)
    kept = steer.relaxed()
    plan = menu.build_plan(db, pid, seg, _turkey_lunch_week(db, pid), steer=kept, **kwargs)
    # said in the menu message, as a new pack is (menu_propose._propose_for)
    assert any(n.startswith("Turkey breast uses ") for n in plan.notes), plan.notes
