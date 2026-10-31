"""How a person's week is planned, in their own word (food/weekplan.py; the partner's /questions,
18 Oct
2026): built on Sunday in one go, bought in two shops, packs said on each meal, lunch from the
night before, a no-cook day, breakfasts in turn, a training day's extra food, one day rebuilt
alone, messages only when needed, the weekly check, and what she records on the page.

The worlds are tests/test_menu.py's and tests/test_packs_build.py's: the frozen clock on Saturday
"""

from __future__ import annotations

import datetime as dt
import json
import re

import pytest

from tiffin import body, logbook, targets
from tiffin.clock import TZ, isoday
from tiffin.errors import ModelInvalid
from tiffin.food import catalogue, menu, packs, shopping, weekplan
from tiffin.jobs import evening_confirm, menu_propose, shopping_list, weekly_check

from .test_menu import world  # noqa: F401
from .test_packs_build import MON, WED, PackModel, _day, daily  # noqa: F401

TUE, THU, FRI = MON + dt.timedelta(days=1), MON + dt.timedelta(days=3), MON + dt.timedelta(days=4)
SAT, SUN = MON - dt.timedelta(days=2), MON - dt.timedelta(days=1)
DAILY = json.dumps([[0], [1], [2], [3], [4]])
#: the partner's answers, as `tiffin weekplan partner --set ...` writes them
PARTNERS = {
    "plan_day": 6,
    "shops": [[0, 1, 2], [3, 4]],
    "topup_list_day": 1,
    "freeze_packs": False,
    "nocook_day": 2,
    "lunch_from_dinner": True,
    "breakfasts": 3,
    "dinner_minutes": 45,
    "full_steps": True,
    "tinned_fish": 2,
    "check_day": 0,
    "training_kcal": 150,
    "quiet": True,
    "least_waste": True,
}


def _daily_profile(db, pid, **changed) -> None:
    row = dict(db.one("SELECT * FROM profiles WHERE person_id = ? ORDER BY id DESC", (pid,)))
    for key in ("id", "created_at"):
        row.pop(key, None)
    db.insert(
        "profiles",
        **{
            **row,
            "effective_from": "2026-09-01",
            "segment_days": DAILY,
            "variety_json": json.dumps({"all": "rotate"}),
            **changed,
        },
    )


def _add_meats(db) -> None:
    """The chicken and turkey tests/test_packs_build.py's fake model cooks."""
    for code, name, pack in (
        ("chicken", "Chicken breast, raw", 1000.0),
        ("turkey", "Turkey breast, raw", 500.0),
    ):
        db.insert(
            "ingredients",
            code=code,
            version=2 if code == "chicken" else 1,
            name=name,
            kcal_100=165,
            protein_100=31.0,
            fat_100=3.6,
            carb_100=0.0,
            fibre_100=0.0,
            pack_g=pack,
            pack_label=f"{pack:g} g pack",
            aisle="meat",
            pantry=0,
            use_whole_pack=0,
            approved=1,
            person_scope="both",
        )


@pytest.fixture
def week(world):  # noqa: F811
    """The owner cooking each day, no menu yet, the meats the fake model cooks in the catalogue."""
    pid, db, *_ = world
    _daily_profile(db, pid)
    _add_meats(db)
    return world


def _person(db, config):
    from tiffin.persons import PersonRegistry

    return PersonRegistry(db, config).require("owner")


# ---------------------------------------------------------------- the ledger
def test_nobody_has_a_week_plan_until_they_say_one(world) -> None:  # noqa: F811
    pid, db, *_ = world
    assert weekplan.of(db, pid) == weekplan.HOUSE
    plan = weekplan.change(db, pid, source="operator", **PARTNERS)
    assert plan.shops == ((0, 1, 2), (3, 4)) and plan.nocook_day == 2 and plan.quiet
    assert weekplan.of(db, pid) == plan
    # a change is a new row: what they had, with the change
    weekplan.change(db, pid, source="page", breakfasts=2)
    assert weekplan.of(db, pid).breakfasts == 2 and weekplan.of(db, pid).nocook_day == 2
    assert db.scalar("SELECT COUNT(*) FROM week_prefs WHERE person_id = ?", (pid,)) == 2
    for bad in ({"plan_day": 9}, {"shops": [[0, 1], [1, 2]]}, {"breakfasts": 9}):
        with pytest.raises(ValueError):
            weekplan.change(db, pid, source="operator", **bad)
    with pytest.raises(ValueError):
        weekplan.change(db, pid, source="someone", quiet=True)


def test_lunch_follows_dinner_within_a_shop_and_never_after_the_no_cook_day() -> None:
    plan = weekplan.WeekPlan(lunch_from_dinner=True, nocook_day=2, shops=((0, 1, 2), (3, 4)))
    assert [weekplan.follows(plan, MON + dt.timedelta(days=i)) for i in range(5)] == [
        False,  # Monday's lunch is cooked that morning
        True,
        True,
        False,  # Wednesday was the no-cook day, and Thursday is the other shop's
        True,
    ]
    assert not weekplan.follows(weekplan.HOUSE, TUE)
    said = weekplan.day_notes(plan, WED, training=True)
    assert "no-cook" in said and "lunch is yesterday's dinner" in said and "training day" in said


# ---------------------------------------------------------------- two shops
def test_the_week_is_bought_in_two_shops(world) -> None:  # noqa: F811
    pid, db, *_ = world
    _daily_profile(db, pid)
    weekplan.change(db, pid, source="operator", **PARTNERS)
    assert packs.window_of(db, pid, TUE) == [MON, TUE, WED]
    assert packs.window_of(db, pid, FRI) == [THU, FRI]
    assert packs.window_of(db, pid, SAT) == [SAT, SUN]
    # Sunday shops for Monday to Wednesday; from the top-up list's evening (Tuesday), the next
    assert shopping.shop_days(db, pid, SUN) == [MON, TUE, WED]
    assert shopping.shop_days(db, pid, MON) == [MON, TUE, WED]
    assert shopping.shop_days(db, pid, TUE) == [THU, FRI]
    assert shopping.shop_days(db, pid, SAT) == []


# ---------------------------------------------------------------- the week on Sunday
def test_the_week_is_planned_on_its_day_and_not_a_day_at_a_time(world) -> None:  # noqa: F811
    pid, db, *_ = world
    _daily_profile(db, pid)
    weekplan.change(db, pid, source="operator", plan_day=6)
    # Saturday: Monday is two days off, but its week is planned tomorrow
    ahead, said = menu_propose.planned_ahead(db, pid, SAT, menu.Segment(MON, MON))
    assert ahead is None and "Sunday" in said
    # Sunday: next Monday, and with it the whole week
    ahead, said = menu_propose.planned_ahead(db, pid, SUN, menu.Segment(TUE, TUE))
    assert ahead == menu.Segment(MON, MON) and not said
    assert menu_propose.week_run(db, pid, ahead) == menu.Segment(MON, FRI)
    # Monday, after a Sunday that built nothing: the days ahead are still built
    ahead, _said = menu_propose.planned_ahead(db, pid, MON, menu.Segment(WED, WED))
    assert ahead == menu.Segment(WED, WED)
    assert menu_propose.plan_date(MON, 6) == SUN and menu_propose.plan_date(MON, 5) == SAT


def test_sundays_build_is_monday_to_friday_with_the_first_shops_list(week) -> None:
    pid, db, tg, gateways, _say, _tap, run_job = week
    weekplan.change(
        db, pid, source="operator", plan_day=6, shops=[[0, 1, 2], [3, 4]], topup_list_day=1
    )
    gateways.append(PackModel())  # type: ignore[arg-type]
    db.clock.set_local(dt.datetime(2026, 9, 13, 12, 0, tzinfo=TZ))  # type: ignore[attr-defined]
    tg.sent.clear()
    assert run_job(menu_propose.JOB, SUN) in ("ok", "degraded")
    winner = menu.accepted_menu_id(db, pid, MON)
    row = db.one("SELECT segment_start, segment_end FROM menus WHERE id = ?", (winner,))
    assert (row["segment_start"], row["segment_end"]) == (isoday(MON), isoday(FRI))
    text = tg.sent[-1]["text"]
    assert "Shopping for Mon, Tue, Wed" in text and "top-up list of its own" in text, text
    made = db.one("SELECT * FROM shopping_lists WHERE person_id = ? AND kind = 'range'", (pid,))
    assert (made["from_day"], made["to_day"]) == (isoday(MON), isoday(WED))


def test_lists_go_out_on_the_top_up_evening_and_no_other(week) -> None:
    pid, db, tg, gateways, _say, _tap, run_job = week
    weekplan.change(
        db, pid, source="operator", plan_day=6, shops=[[0, 1, 2], [3, 4]], topup_list_day=1
    )
    # Saturday: no "no menu for Monday yet" - the week is planned tomorrow
    tg.sent.clear()
    assert run_job(shopping_list.JOB, SAT) == "skipped"
    assert tg.sent == []
    gateways.append(PackModel())  # type: ignore[arg-type]
    db.clock.set_local(dt.datetime(2026, 9, 13, 12, 0, tzinfo=TZ))  # type: ignore[attr-defined]
    run_job(menu_propose.JOB, SUN)
    tg.sent.clear()
    assert run_job(shopping_list.JOB, MON) == "skipped"
    assert tg.sent == []
    assert run_job(shopping_list.JOB, TUE) == "ok"
    assert tg.sent[-1]["text"].startswith("<b>Top-up shop for Thu, Fri</b>"), tg.sent[-1]["text"]


# ---------------------------------------------------------------- the week's shape
def _payload(db, pid, days, main="chicken") -> dict:
    items = catalogue.approved(db, pid)
    n = {re.findall(r"[a-z]+", i.name.lower())[0]: k for k, i in enumerate(items, start=1)}
    return {"days": [_day(d, n, main, isoday(d)) for d in days]}


def _build(db, pid, days, payload=None, **kw):
    t = db.one("SELECT * FROM targets WHERE person_id = ?", (pid,))
    return menu.build_plan(
        db,
        pid,
        menu.Segment(days[0], days[-1]),
        payload or _payload(db, pid, days),
        targets=targets.macros_of(t),
        profile=db.one("SELECT * FROM profiles WHERE person_id = ? ORDER BY id DESC", (pid,)),
        items=catalogue.approved(db, pid),
        **kw,
    )


def _dish(db, plan, day, slot) -> str:
    rid = plan.recipe_ids[menu.slot_key(day, slot)]
    return str(db.scalar("SELECT name FROM recipes WHERE id = ?", (rid,)))


def test_lunch_is_the_night_befores_dinner(daily) -> None:  # noqa: F811
    pid, db, *_ = daily
    weekplan.change(db, pid, source="operator", lunch_from_dinner=True)
    plan = _build(db, pid, [TUE, WED, THU])
    # the model wrote its own lunches; the plan holds the dinner before in each
    assert _dish(db, plan, WED, "lunch") == _dish(db, plan, TUE, "dinner")
    assert _dish(db, plan, THU, "lunch") == _dish(db, plan, WED, "dinner")
    # the first day's lunch has no dinner in the build to come from: the model's own
    assert _dish(db, plan, TUE, "lunch") != _dish(db, plan, TUE, "dinner")


def test_a_no_cook_day_refuses_the_hob_and_the_oven(daily) -> None:  # noqa: F811
    pid, db, *_ = daily
    weekplan.change(db, pid, source="operator", nocook_day=2)
    # the fixture's dishes grill, boil and steam: nothing a no-cook Wednesday may hold
    with pytest.raises(ModelInvalid, match=r"2026-09-16 is a no-cook day"):
        _build(db, pid, [TUE, WED])
    # Tuesday alone is not the no-cook day
    _build(db, pid, [TUE])


def test_breakfasts_take_turns_as_asked(daily) -> None:  # noqa: F811
    pid, db, *_ = daily
    _daily_profile(db, pid)  # breakfasts that rotate, as hers do
    weekplan.change(db, pid, source="operator", breakfasts=2)
    # five days, five breakfasts written: two were asked for
    with pytest.raises(ModelInvalid, match=r"breakfasts: 2 different ones taking turns, not 3"):
        _build(db, pid, [TUE, WED, THU])
    payload = _payload(db, pid, [TUE, WED, THU])
    first = payload["days"][0]["slots"][0]["recipe"]
    second = payload["days"][1]["slots"][0]["recipe"]
    payload["days"][2]["slots"][0]["recipe"] = first
    _build(db, pid, [TUE, WED, THU], payload)
    assert first["name"] != second["name"]


def test_a_dinner_may_take_as_long_as_they_said(daily) -> None:  # noqa: F811
    pid, db, *_ = daily
    payload = _payload(db, pid, [TUE])
    payload["days"][0]["slots"][2]["recipe"]["prep_minutes"] = 40
    with pytest.raises(ModelInvalid, match=r"40 minutes by its own method > 30"):
        _build(db, pid, [TUE], payload)
    weekplan.change(db, pid, source="operator", dinner_minutes=45)
    _build(db, pid, [TUE], payload)


def test_a_training_day_has_a_little_more_food(daily) -> None:  # noqa: F811
    pid, db, *_ = daily
    plan_id = db.insert("training_plans", person_id=pid, effective_from="2026-09-01")
    db.insert(
        "planned_sessions", plan_id=plan_id, person_id=pid, weekday=1, kind="run", target_minutes=40
    )
    macros = targets.macros_of(db.one("SELECT * FROM targets WHERE person_id = ?", (pid,)))
    seg = menu.Segment(TUE, WED)
    assert menu_propose.training_targets(db, pid, seg, macros) == {}
    weekplan.change(db, pid, source="operator", training_kcal=150)
    got = menu_propose.training_targets(db, pid, seg, macros)
    assert list(got) == [TUE] and got[TUE].kcal == macros.kcal + 150
    assert got[TUE].protein_g == macros.protein_g and got[TUE].fat_g == macros.fat_g


# ---------------------------------------------------------------- packs on each meal
def test_each_meal_says_where_its_meat_comes_from(daily) -> None:  # noqa: F811
    pid, db, *_ = daily
    lines = packs.meal_lines(db, pid, [MON])
    lunch, dinner = lines[(isoday(MON), "lunch")], lines[(isoday(MON), "dinner")]
    # Monday's chicken: a 1 kg pack bought on Sunday, lunch then dinner from it
    assert re.match(
        r"Chicken breast: \d+ g from Sunday's 1000 g pack, \d+ g left for dinner", lunch[0]
    )
    assert re.match(r"Chicken breast: \d+ g from Sunday's 1000 g pack, \d+ g over", dinner[0])


# ---------------------------------------------------------------- one day rebuilt
def test_a_change_rebuilds_only_the_day_asked_and_the_next_lunch_follows(
    daily,  # noqa: F811
    config,
    tmp_path,
) -> None:
    from tiffin.app import App
    from tiffin.persons import PersonRegistry
    from tiffin.secrets import Secrets

    pid, db, tg, gateways, run_job, clock = daily
    weekplan.change(db, pid, source="operator", lunch_from_dinner=True)
    gateways.append(PackModel())  # type: ignore[arg-type]
    clock.set_local(dt.datetime(2026, 9, 14, 12, 0, tzinfo=TZ))
    run_job(menu_propose.JOB, MON)  # Wednesday to Friday, in one
    week = menu.accepted_menu_id(db, pid, WED)
    assert week is not None and menu.accepted_menu_id(db, pid, FRI) == week
    # the page's "Only Thu": the bot and the page build through the same App
    model = PackModel()
    app = App(
        config,
        Secrets(tmp_path / "s"),
        clock=clock,
        transport=tg,
        gateway_factory=lambda _a, _d: model,  # type: ignore[arg-type,return-value]
    )
    person = PersonRegistry(db, config).require("owner")
    menu_propose.propose_now(
        app,
        db,
        person,
        targets.profile_on(db, person.id, MON),
        app.outbox(db),
        clock=clock,
        replace=True,
        for_menu=week,
        only_day=THU,
    )
    # the rebuild was Thursday alone
    rebuilt = int(
        db.scalar(
            "SELECT id FROM menus WHERE person_id = ? AND segment_start = ? AND segment_end = ?",
            (pid, isoday(THU), isoday(THU)),
        )
    )

    def in_force(day: dt.date, slot: str) -> str:
        return str(
            db.scalar(
                "SELECT rc.name FROM v_menu_days_current md "
                "JOIN meal_slots ms ON ms.menu_day_id = md.id "
                "JOIN recipes rc ON rc.id = ms.recipe_id "
                "WHERE md.person_id = ? AND md.day = ? AND ms.slot = ?",
                (pid, isoday(day), slot),
            )
        )

    def of_menu(menu_id: int, day: dt.date, slot: str) -> str:
        return str(
            db.scalar(
                "SELECT rc.name FROM menu_days md JOIN meal_slots ms ON ms.menu_day_id = md.id "
                "JOIN recipes rc ON rc.id = ms.recipe_id "
                "WHERE md.menu_id = ? AND md.day = ? AND ms.slot = ?",
                (menu_id, isoday(day), slot),
            )
        )

    # Thursday eats the rebuilt dinner; Wednesday is the week's, untouched
    assert in_force(THU, "dinner") == of_menu(rebuilt, THU, "dinner")
    assert in_force(WED, "dinner") == of_menu(week, WED, "dinner")
    # Friday's lunch is Thursday's new dinner, reheated
    assert in_force(FRI, "lunch") == of_menu(rebuilt, THU, "dinner")


# ---------------------------------------------------------------- quiet, and the weekly check
def test_quiet_means_no_evening_check_in(world) -> None:  # noqa: F811
    pid, db, tg, _gw, _say, _tap, run_job = world
    weekplan.change(db, pid, source="operator", quiet=True)
    tg.sent.clear()
    run_job(evening_confirm.JOB, SAT)
    assert tg.sent == []
    detail = str(db.scalar("SELECT detail FROM run_ledger ORDER BY rowid DESC LIMIT 1"))
    assert "messages only when needed" in detail


def test_the_weekly_check_comes_on_its_day_with_the_period_buttons(world) -> None:  # noqa: F811
    pid, db, tg, _gw, _say, tap, run_job = world
    _daily_profile(db, pid, cycle_tracking=1)
    weekplan.change(db, pid, source="operator", check_day=0)
    tg.sent.clear()
    assert run_job(weekly_check.JOB, SAT) == "skipped"
    assert run_job(weekly_check.JOB, MON) == "ok"
    text = tg.sent[-1]["text"]
    assert "Step on the scale" in text and "84 102" in text and "period" in text
    assert "k:prd:no" in json.dumps(tg.sent[-1].get("reply_markup"))
    # one tap: the first day of a period, and where today falls from it
    said = tap(f"k:prd:{isoday(MON - dt.timedelta(days=2))}")
    assert "your period started on Sat 12 Sep" in said
    assert (
        db.scalar(
            "SELECT phase FROM cycle_phases WHERE person_id = ? ORDER BY id DESC LIMIT 1", (pid,)
        )
        == "menstrual"
    )


def test_the_cycle_is_worked_out_from_period_starts(world) -> None:  # noqa: F811
    pid, db, *_ = world
    start = dt.date(2026, 8, 20)
    assert body.cycle_on(db, pid, start) is None
    body.record_period_start(db, pid, start)

    def phase(days: int) -> str:
        got = body.cycle_on(db, pid, start + dt.timedelta(days=days))
        assert got is not None
        return got.phase

    assert [phase(2), phase(8), phase(13), phase(20)] == [
        "menstrual",
        "follicular",
        "ovulation",
        "luteal",
    ]
    # a second start 30 days on makes her cycle 30 days, not 28
    body.record_period_start(db, pid, start + dt.timedelta(days=30))
    got = body.cycle_on(db, pid, start + dt.timedelta(days=32))
    assert got is not None and got.length == 30 and got.day == 3
    # six weeks with no start entered says nothing
    assert body.cycle_on(db, pid, start + dt.timedelta(days=80)) is None


# ---------------------------------------------------------------- what she records on the page
class _Estimator:
    def __init__(self, kcal: float) -> None:
        self.kcal = kcal

    def json_task(self, task, prompt, schema_name, *, person_id):
        from tiffin.model.gateway import ModelResult

        assert task == "offplan_estimate"
        items = [{"name": "chicken wrap", "kcal": self.kcal}] if self.kcal else []
        return ModelResult(
            text="",
            data={"kcal": self.kcal, "items": items, "protein_g": 30, "fat_g": 15, "carb_g": 50},
            model_used="fake",
            backend="fake",
        )


class _App:
    def __init__(self, gateway) -> None:
        self._gateway = gateway

    def gateway(self, db):
        return self._gateway


def test_a_meal_eaten_differently_is_counted_in_its_place(world, config) -> None:  # noqa: F811
    pid, db, *_ = world
    person = _person(db, config)
    day = SAT
    got = logbook.record_different(_App(_Estimator(520)), db, person, day, "lunch", "chicken wrap")
    assert got["ok"] and got["kcal"] == 520 and "in place of the planned lunch" in got["said"]
    row = db.one("SELECT * FROM estimates WHERE person_id = ? ORDER BY id DESC LIMIT 1", (pid,))
    assert row["status"] == "accepted" and json.loads(row["replaced_slots_json"]) == ["lunch"]
    # not food: nothing written; no model: nothing written, and said
    n = db.scalar("SELECT COUNT(*) FROM estimates")
    assert not logbook.record_different(_App(_Estimator(0)), db, person, day, "lunch", "hello")[
        "ok"
    ]
    assert not logbook.record_different(_App(None), db, person, day, "lunch", "a wrap")["ok"]
    assert db.scalar("SELECT COUNT(*) FROM estimates") == n


def test_a_session_is_recorded_from_the_page(world, config) -> None:  # noqa: F811
    pid, db, *_ = world
    person = _person(db, config)
    said = logbook.record_session(db, person, SAT, minutes=45, done=True)
    assert "45 minutes" in said
    row = db.one("SELECT * FROM session_matches WHERE person_id = ?", (pid,))
    assert row["status"] == "manual" and row["minutes"] == 45
    assert "nothing to skip" in logbook.record_session(db, person, SAT, minutes=None, done=False)


# ---------------------------------------------------------------- the rest of her answers
def test_tinned_fish_is_held_to_a_few_meals_a_week(week) -> None:
    pid, db, *_ = week
    db.insert(
        "ingredients",
        code="tuna",
        version=1,
        name="Tuna in brine, drained",
        kcal_100=109,
        protein_100=25.0,
        fat_100=1.0,
        carb_100=0.0,
        fibre_100=0.0,
        pack_g=102,
        pack_label="145 g tin",
        aisle="tins",
        pantry=0,
        use_whole_pack=0,
        approved=1,
        person_scope="both",
    )
    weekplan.change(db, pid, source="operator", tinned_fish=2)
    days = [MON, TUE, WED]
    # tuna in every lunch and dinner of three days: six meals, two allowed
    with pytest.raises(ModelInvalid, match=r"tinned fish is in 6 meals this week: at most 2"):
        _build(db, pid, days, _payload(db, pid, days, main="tuna"))


def test_the_pack_bought_is_the_one_that_leaves_least_over(world) -> None:  # noqa: F811
    from tiffin.food import products

    pid, db, *_ = world
    for sku, grams in (("a", 250.0), ("b", 500.0), ("c", 750.0)):
        db.insert(
            "products",
            ingredient_code="mince",
            retailer="sainsburys",
            retailer_sku=sku,
            name=f"Beef mince {grams:g}g",
            pack_g=grams,
            seen_at="2026-09-10 10:00:00",
            source="manual",
        )
    shops = ("sainsburys",)
    # the house way: the pack closest to the catalogue's own (500 g)
    assert products.for_shopping(db, pid, {"mince": 500.0}, shops)["mince"].pack_g == 500.0
    # least waste: 700 g needs 750 g (50 over), not 2 x 500 g (300 over) or 3 x 250 g (50 over,
    # but the larger single pack wins only on ties of what is over... smaller first)
    got = products.for_shopping(db, pid, {"mince": 500.0}, shops, need={"mince": 700.0})
    assert got["mince"].pack_g == 250.0  # 3 x 250 g leaves 50 g, as 750 g does: smaller wins
    assert products.pack_sizes(db, pid, ["mince"], shops) == {"mince": (250.0, 500.0, 750.0)}


def test_partner_sunday_cycle_question_stands_down_for_the_weekly_check(world) -> None:  # noqa: F811
    from tiffin.jobs import body_check

    pid, db, tg, _gw, _say, _tap, run_job = world
    _daily_profile(db, pid, cycle_tracking=1)
    weekplan.change(db, pid, source="operator", check_day=0)
    tg.sent.clear()
    run_job(body_check.JOB, SUN)
    assert tg.sent == []
    detail = str(db.scalar("SELECT detail FROM run_ledger ORDER BY rowid DESC LIMIT 1"))
    assert "weekly check" in detail


@pytest.fixture
def page(frozen_clock, config, tmp_path):
    """Tests/test_web.py's page, with a stand-in for the estimate."""
    from fastapi.testclient import TestClient

    from tiffin.app import App
    from tiffin.db import Db
    from tiffin.secrets import Secrets
    from tiffin.web.app import create_app

    from .fakes import FakeTelegram
    from .test_web import OWNER_LOGIN, seed

    config.paths.ensure()
    db = Db.open(config.paths.db, clock=frozen_clock)
    owner_id = db.insert("persons", slug="owner", display_name="Alex", tone="blunt")
    db.insert("tailscale_identities", person_id=owner_id, login=OWNER_LOGIN)
    db.insert("telegram_identities", person_id=owner_id, chat_id=1000001, method="config")
    seed(db, owner_id, kcal=2250, weight=100.9, steps=11000, dish="His chilli")
    secrets_dir = tmp_path / "s"
    secrets_dir.mkdir()
    (secrets_dir / "telegram-token-dev.txt").write_text("1:t", encoding="utf-8")
    config.data["web"]["secure_cookies"] = False
    config.data["web"]["root_path"] = ""
    estimator = _Estimator(480)
    app = App(
        config,
        Secrets(secrets_dir),
        clock=frozen_clock,
        transport=FakeTelegram(),
        gateway_factory=lambda _a, _d: estimator,  # type: ignore[arg-type,return-value]
    )
    client = TestClient(create_app(app), base_url="http://testserver")
    me = client.get("api/me", headers={"Tailscale-User-Login": OWNER_LOGIN})
    headers = {"Tailscale-User-Login": OWNER_LOGIN, "X-CSRF-Token": me.json()["csrf"]}
    try:
        yield client, headers, owner_id, db
    finally:
        db.close()


def test_the_page_records_a_meal_a_session_and_a_period(page) -> None:
    c, headers, owner_id, db = page
    day = "2026-09-07"  # the seeded day: its dinner, already recorded
    got = c.post(
        "api/today/different",
        json={"day": day, "slot": "dinner", "text": "a chicken wrap"},
        headers=headers,
    )
    assert got.status_code == 200 and got.json()["ok"], got.text
    # the recorded day is recorded again with it: the dinner off, the wrap on
    row = db.one(
        "SELECT * FROM day_confirmations WHERE person_id = ? AND day = ? ORDER BY id DESC",
        (owner_id, day),
    )
    assert row["basis"] in ("menu", "mixed") and round(row["kcal"]) == 2250 - 350 + 480
    # a meal that is not on that day, a future day, nothing said: refused, nothing written
    for bad in (
        {"day": day, "slot": "breakfast", "text": "toast"},
        {"day": "2026-09-30", "slot": "dinner", "text": "toast"},
        {"day": day, "slot": "dinner", "text": "  "},
    ):
        assert c.post("api/today/different", json=bad, headers=headers).status_code == 400
    trained = c.post(
        "api/today/session", json={"day": day, "minutes": 40, "done": True}, headers=headers
    )
    assert trained.status_code == 200 and "40 minutes" in trained.json()["said"]
    assert c.post("api/today/session", json={}, headers=headers).status_code == 400
    started = c.post("api/cycle/start", json={"day": "2026-09-05"}, headers=headers)
    assert started.status_code == 200
    assert db.scalar("SELECT COUNT(*) FROM cycle_phases WHERE person_id = ?", (owner_id,)) == 1


def test_the_bots_change_offers_one_day_of_the_week(world) -> None:  # noqa: F811
    pid, db, tg, gateways, _say, tap, run_job = world
    _daily_profile(db, pid)
    _add_meats(db)
    weekplan.change(db, pid, source="operator", plan_day=6)
    gateways.append(PackModel())  # type: ignore[arg-type]
    db.clock.set_local(dt.datetime(2026, 9, 13, 12, 0, tzinfo=TZ))  # type: ignore[attr-defined]
    run_job(menu_propose.JOB, SUN)
    week_id = menu.accepted_menu_id(db, pid, MON)
    tap(f"k:menux:{week_id}")
    keyboard = json.dumps(tg.sent[-1].get("reply_markup"))
    assert f"k:rebuild:{week_id}:{isoday(WED)}" in keyboard and "Only Wed" in keyboard
    assert f"k:rebuild:{week_id}" in keyboard  # the whole week is still one tap


def test_the_last_attempt_keeps_a_plan_that_misses_the_weeks_shape_and_says(daily) -> None:  # noqa: F811
    pid, db, *_ = daily
    weekplan.change(db, pid, source="operator", nocook_day=2)
    items = catalogue.approved(db, pid)
    steer = packs.steer_for(db, pid, [TUE, WED], allowed={i.code for i in items}, items=items)
    with pytest.raises(ModelInvalid, match="no-cook day"):
        _build(db, pid, [TUE, WED], steer=steer)
    plan = _build(db, pid, [TUE, WED], steer=steer.relaxed())
    assert any(n.startswith("Not quite as asked: 2026-09-16 is a no-cook day") for n in plan.notes)


def test_a_kettles_boiling_water_is_not_cooking() -> None:
    from tiffin.food import recipes

    soak = recipes.RecipeDraft(
        name="Couscous with tuna",
        ingredients=(),
        method=("Pour boiling water from the kettle over the couscous and leave for 5 minutes.",),
        prep_minutes=5,
    )
    fry = recipes.RecipeDraft(
        name="Fried egg", ingredients=(), method=("Fry the egg in a pan.",), prep_minutes=5
    )
    assert recipes.cooking_without_kettle(soak) == []
    assert "hob" in recipes.cooking_without_kettle(fry)


def test_lunch_follows_a_dinner_planned_before_the_build(daily) -> None:  # noqa: F811
    pid, db, *_ = daily
    weekplan.change(db, pid, source="operator", lunch_from_dinner=True)
    # Monday was planned on its own; Tuesday's build eats Monday's dinner for lunch
    monday_dinner = db.scalar(
        "SELECT rc.name FROM v_menu_days_current md JOIN meal_slots ms ON ms.menu_day_id = md.id "
        "JOIN recipes rc ON rc.id = ms.recipe_id WHERE md.person_id = ? AND md.day = ? "
        "AND ms.slot = 'dinner'",
        (pid, isoday(MON)),
    )
    plan = _build(db, pid, [TUE])
    assert _dish(db, plan, TUE, "lunch") == monday_dinner


def test_a_week_built_part_way_through_lists_only_its_own_days(week) -> None:
    """This week of hers: Monday and Tuesday planned (and bought) a day at a time, then Monday's
    run builds Wednesday to Friday - its list is Wednesday's food, never Monday's and Tuesday's
    again; Thursday and Friday wait for the top-up list."""
    pid, db, tg, gateways, _say, _tap, run_job = week
    weekplan.change(
        db, pid, source="operator", plan_day=6, shops=[[0, 1, 2], [3, 4]], topup_list_day=1
    )
    for day in (MON, TUE):
        plan = _build(db, pid, [day], _payload(db, pid, [day], main="turkey"))
        menu.decide(db, menu.store_plan(db, plan, status="proposed"), "accepted")
    gateways.append(PackModel())  # type: ignore[arg-type]
    db.clock.set_local(dt.datetime(2026, 9, 14, 12, 0, tzinfo=TZ))  # type: ignore[attr-defined]
    tg.sent.clear()
    assert run_job(menu_propose.JOB, MON) in ("ok", "degraded")
    assert menu.accepted_menu_id(db, pid, FRI) == menu.accepted_menu_id(db, pid, WED)
    text = tg.sent[-1]["text"]
    assert "Shopping for Wed:" in text and "top-up list of its own" in text, text
    made = db.one(
        "SELECT * FROM shopping_lists WHERE person_id = ? AND kind = 'range' ORDER BY id DESC",
        (pid,),
    )
    assert (made["from_day"], made["to_day"]) == (isoday(WED), isoday(WED))


# ---------------------------------------------------------------- the owner's week of 19 Oct
def _open(code: str, name: str) -> packs.Pack:
    p = packs.Pack(code, name, "meat", 500.0, "500 g pack")
    p.by_day = {"2026-09-13": 100.0}  # 400 g left
    p.slots = {"dinner"}
    return p


def test_two_open_packs_never_refuse_each_other() -> None:
    """Saturday 17 Oct: the weekend's chicken and beef were both open, and every plan was refused
    - chicken because the beef was open, beef because the chicken was. A plan that cooks from an
    open pack is using it up; opening another beside it is the budget's to judge."""
    steer = packs.Steer(
        open=(_open("chicken", "Chicken breast, raw"), _open("beef", "Beef mince, raw")),
        budget=2,
    )
    both = {
        "2026-09-14|lunch": {("chicken", "Chicken breast, raw", "meat", "Chicken bowl")},
        "2026-09-14|dinner": {("beef", "Beef mince, raw", "meat", "Beef ragu")},
    }
    assert steer.problems(both, {}) == []
    # a third, new meat while the beef sits unused is still refused
    other = {
        "2026-09-14|lunch": {("chicken", "Chicken breast, raw", "meat", "Chicken bowl")},
        "2026-09-14|dinner": {("turkey", "Turkey breast, raw", "meat", "Turkey tray")},
    }
    assert any("Beef mince" in p for p in steer.problems(other, {}))


def test_the_weekends_packs_are_not_carried_into_a_weekday_cook(daily) -> None:  # noqa: F811
    pid, db, *_ = daily
    assert packs.carried(db, pid, MON) == []


def test_a_failed_weekday_build_repeats_a_weekday_menu_not_the_weekends_treats(daily) -> None:  # noqa: F811
    pid, db, *_ = daily
    monday = menu.accepted_menu_id(db, pid, MON)
    sat2, sun2 = MON + dt.timedelta(days=5), MON + dt.timedelta(days=6)
    plan = _build(db, pid, [sat2, sun2], _payload(db, pid, [sat2, sun2]), weekend=True)
    weekend = menu.store_plan(db, plan, status="proposed")
    menu.decide(db, weekend, "accepted")
    week = [MON + dt.timedelta(days=7 + i) for i in range(5)]
    repeated = menu.repeat_last(db, pid, menu.Segment(week[0], week[-1]))
    row = db.one("SELECT source_menu_id FROM menus WHERE id = ?", (repeated,))
    assert row["source_menu_id"] == monday != weekend


def test_today_is_planned_afresh_only_on_the_persons_own_word(daily, config, tmp_path) -> None:  # noqa: F811
    """
    Monday to Friday ... I'll cook everything today". The started-day rule protects food already
    shopped and cooked for a menu; his own word takes today over."""
    from tiffin.app import App
    from tiffin.persons import PersonRegistry
    from tiffin.secrets import Secrets

    pid, db, tg, _gateways, _run_job, clock = daily
    monday_before = menu.accepted_menu_id(db, pid, MON)
    clock.set_local(dt.datetime(2026, 9, 14, 9, 45, tzinfo=TZ))
    model = PackModel()
    app = App(
        config,
        Secrets(tmp_path / "s"),
        clock=clock,
        transport=tg,
        gateway_factory=lambda _a, _d: model,  # type: ignore[arg-type,return-value]
    )
    person = PersonRegistry(db, config).require("owner")

    def cook(take: bool) -> str:
        return menu_propose.propose_now(
            app,
            db,
            person,
            targets.profile_on(db, person.id, MON),
            app.outbox(db),
            clock=clock,
            replace=True,
            day=MON,
            quiet_failure=True,
            take_today=take,
        )

    said = cook(False)
    assert "already under way" in said and menu.accepted_menu_id(db, pid, MON) == monday_before
    clock.advance(minutes=1)  # a manual build's ledger slot is to the second
    cook(True)
    assert menu.accepted_menu_id(db, pid, MON) != monday_before


def test_a_batch_cooks_repeat_is_one_day_all_week(world) -> None:  # noqa: F811
    """
    cooking" - never two different days in turn."""
    pid, db, *_ = (
        world  # tests/test_menu.py's the owner: one cook Monday to Friday, everything "same"
    )
    weekend = db.insert(
        "menus",
        person_id=pid,
        segment_start=isoday(SAT),
        segment_end=isoday(SUN),
        status="accepted",
    )
    for n, day in enumerate((SAT, SUN)):
        dish = db.insert(
            "recipes",
            name=f"Treat {n}",
            servings=1,
            prep_minutes=20,
            method_json='["Cook it."]',
            source="model",
        )
        md = db.insert(
            "menu_days",
            menu_id=weekend,
            day=isoday(day),
            kcal=2000,
            protein_g=180,
            fat_g=70,
            carb_g=200,
            fibre_g=30,
        )
        db.insert(
            "meal_slots",
            menu_day_id=md,
            slot="dinner",
            recipe_id=dish,
            portion_multiplier=1.0,
            portion_g=300,
            kcal=700,
            protein_g=60,
            fat_g=20,
            carb_g=60,
            fibre_g=8,
        )
    repeated = menu.repeat_last(db, pid, menu.Segment(MON, FRI))
    names = [
        str(r["name"])
        for r in db.all(
            "SELECT rc.name FROM menu_days md JOIN meal_slots ms ON ms.menu_day_id = md.id "
            "JOIN recipes rc ON rc.id = ms.recipe_id WHERE md.menu_id = ? ORDER BY md.day",
            (repeated,),
        )
    ]
    assert names == ["Treat 0"] * 5, names


def test_setting_time_is_not_cooking_time() -> None:
    """Her chia pudding was refused as 126 minutes of cooking: the fridge does that
    part. A stretch that also cooks still counts."""
    from tiffin.food.recipes import method_minutes

    assert method_minutes(["Stir the chia into the skyr and milk, then chill for 2 hours."]) == 0
    assert method_minutes(["Mix and leave to set in the fridge overnight, 8 hours."]) == 0
    assert method_minutes(["Marinate the chicken for 30 minutes.", "Grill 12 minutes."]) == 12
    assert method_minutes(["Bake 20 minutes, then leave to cool for 10 minutes."]) == 20
    assert method_minutes(["Air fry 200C 18 minutes, shaking halfway."]) == 18
    assert method_minutes(["Fry 6 to 8 minutes until browned."]) == 8


def test_a_rebuild_from_a_day_takes_partner_week_to_friday(daily) -> None:  # noqa: F811
    pid, db, *_ = daily
    weekplan.change(db, pid, source="operator", plan_day=6)
    # Wednesday already has a menu (the repeat she got): a rebuild asked for still runs to Friday
    plan = _build(db, pid, [WED])
    menu.decide(db, menu.store_plan(db, plan, status="proposed"), "accepted")
    assert menu_propose.week_run(db, pid, menu.Segment(WED, WED)) == menu.Segment(WED, WED)
    assert menu_propose.week_run(db, pid, menu.Segment(WED, WED), replace=True) == menu.Segment(
        WED, FRI
    )


BLOCKS = {"same_food": [[0, 1], [3, 4]], "rotate_mains": True}


def _add_food(db, code: str, name: str, aisle: str, *, kcal=200, protein=25.0, fat=10.0) -> None:
    db.insert(
        "ingredients",
        code=code,
        version=1,
        name=name,
        kcal_100=kcal,
        protein_100=protein,
        fat_100=fat,
        carb_100=0.0,
        fibre_100=0.0,
        pack_g=300.0,
        pack_label="300 g pack",
        aisle=aisle,
        pantry=0,
        use_whole_pack=0,
        approved=1,
        person_scope="both",
    )


def test_a_block_eats_the_same_lunch_and_dinner(daily) -> None:  # noqa: F811
    pid, db, *_ = daily
    _daily_profile(db, pid)  # rotating, as hers
    weekplan.change(db, pid, source="operator", **BLOCKS)
    plan = _build(db, pid, [THU, FRI], _payload(db, pid, [THU, FRI], main="turkey"))
    assert _dish(db, plan, FRI, "lunch") == _dish(db, plan, THU, "lunch")
    assert _dish(db, plan, FRI, "dinner") == _dish(db, plan, THU, "dinner")
    # breakfasts are not part of it
    assert _dish(db, plan, FRI, "breakfast") != _dish(db, plan, THU, "breakfast")
    # Tuesday built alone follows Monday, already planned
    monday_lunch = db.scalar(
        "SELECT rc.name FROM v_menu_days_current md JOIN meal_slots ms ON ms.menu_day_id = md.id "
        "JOIN recipes rc ON rc.id = ms.recipe_id WHERE md.person_id = ? AND md.day = ? "
        "AND ms.slot = 'lunch'",
        (pid, isoday(MON)),
    )
    tuesday = _build(db, pid, [TUE], _payload(db, pid, [TUE], main="turkey"))
    assert _dish(db, tuesday, TUE, "lunch") == monday_lunch


def test_each_block_cooks_a_different_meat(daily) -> None:  # noqa: F811
    pid, db, *_ = daily  # Monday's lunch and dinner are chicken
    _daily_profile(db, pid)
    weekplan.change(db, pid, source="operator", **BLOCKS)
    tuesday = _build(db, pid, [TUE], _payload(db, pid, [TUE], main="chicken"))
    menu.decide(db, menu.store_plan(db, tuesday, status="proposed"), "accepted")
    with pytest.raises(ModelInvalid, match="a different meat or fish for each stretch"):
        _build(db, pid, [THU, FRI], _payload(db, pid, [THU, FRI], main="chicken"))
    _build(db, pid, [THU, FRI], _payload(db, pid, [THU, FRI], main="turkey"))


def test_the_steak_day_is_a_steak_dinner_and_no_cook_fish_meals(daily) -> None:  # noqa: F811
    pid, db, *_ = daily
    _daily_profile(db, pid)
    _add_food(db, "sirloin_steak", "Sirloin steak, raw", "meat", kcal=201, protein=21.6, fat=12.7)
    _add_food(db, "sardines", "Sardines in olive oil, drained", "tins", kcal=220, protein=23.3)
    weekplan.change(db, pid, source="operator", nocook_day=2, steak_day=2)
    items = catalogue.approved(db, pid)
    n = {i.code: k for k, i in enumerate(items, start=1)}

    def dish(name, parts, method):
        return {"name": name, "ingredients": parts, "method": method, "prep_minutes": 5}

    good = {
        "days": [
            {
                "day": isoday(WED),
                "slots": [
                    {
                        "slot": "breakfast",
                        "recipe": dish(
                            "Sardines on toast",
                            [{"i": n["sardines"], "g": 90}],
                            ["Mash the sardines and spread on the bread."],
                        ),
                    },
                    {
                        "slot": "lunch",
                        "recipe": dish(
                            "Sardine plate",
                            [{"i": n["sardines"], "g": 90}],
                            ["Arrange the sardines on a plate."],
                        ),
                    },
                    {
                        "slot": "dinner",
                        "recipe": dish(
                            "Pan-fried sirloin",
                            [{"i": n["sirloin_steak"], "g": 200}],
                            ["Fry the steak 3 minutes a side in a hot pan, then rest it."],
                        ),
                    },
                    {
                        "slot": "snack",
                        "recipe": dish(
                            "Sardine crackers",
                            [{"i": n["sardines"], "g": 45}],
                            ["Spoon the sardines onto crackers."],
                        ),
                    },
                ],
            }
        ]
    }
    drafts = menu._drafts_from(good)
    wp = weekplan.of(db, pid)
    slots = ["breakfast", "lunch", "dinner", "snack"]
    seg = menu.Segment(WED, WED)
    assert menu.week_errors(db, pid, drafts, seg, slots, items, wp) == []
    # a dinner that is not a steak, and no fish in the other meals: said
    good["days"][0]["slots"][2]["recipe"]["ingredients"] = [{"i": n["chicken"], "g": 200}]
    for k in (0, 1, 3):
        good["days"][0]["slots"][k]["recipe"]["ingredients"] = [{"i": n["turkey"], "g": 150}]
    said = " ".join(menu.week_errors(db, pid, menu._drafts_from(good), seg, slots, items, wp))
    assert "dinner is a steak" in said and "tinned tuna, sardines or smoked salmon" in said


def test_the_steak_day_counts_to_no_meat_budget() -> None:
    steer = packs.Steer(budget=2, free=frozenset({isoday(WED)}))
    uses = {
        f"{isoday(MON)}|dinner": {("beef", "Beef mince, raw", "meat", "Beef ragu")},
        f"{isoday(WED)}|dinner": {("sirloin_steak", "Sirloin steak, raw", "meat", "Steak")},
        f"{isoday(THU)}|dinner": {("chicken", "Chicken breast, raw", "meat", "Chicken tray")},
    }
    assert steer.problems(uses, {}) == []
