"""Weekly choices: a few dishes for every weekday meal, picked on the page, made the week's menu.

options for each meal ... and she preselects them on Sunday for the week? So then she can buy
things Sunday for the week. No need to enable that for me, so we can make it a toggle."

The world is the swap tests' week (tests/test_swap.py): her Monday-to-Friday cook, two weeks of
history (a salmon traybake she rated 5, a chicken stir-fry she rated 1), and next week's menu
already in force. The clock is Sunday 13 Sep, 08:00, the morning the week's choices are built.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any

import pytest

from tiffin import building
from tiffin.clock import TZ, FrozenClock, isoday
from tiffin.db import Db
from tiffin.errors import NothingToBuild
from tiffin.food import choices, menu, shopping
from tiffin.model.gateway import ModelResult
from tiffin.telegram import settings

from .test_food_shopping import seed_recipe
from .test_swap import (  # noqa: F401
    FRI,
    MON,
    OWNER_LOGIN,
    PARTNER_LOGIN,
    _headers,
    _menu,
    _profile,
    page,
    week,
)

SUNDAY = dt.datetime(2026, 9, 13, 8, 0, tzinfo=TZ)
DAILY = json.dumps([[0], [1], [2], [3], [4]])


def _index(prompt: str, name: str) -> int:
    m = re.search(rf"^(\d+)\. {re.escape(name)}", prompt, re.M)
    assert m, f"{name} is not on the list"
    return int(m.group(1))


class ChoicesModel:
    """Answers menu_choices with as many dishes as asked for, each simple enough for its meal and
    built from foods on the numbered list, by index."""

    def __init__(self) -> None:
        self.tasks: list[str] = []
        self.prompts: list[str] = []

    def json_task(self, task: str, prompt: str, schema_name: str, *, person_id: int) -> ModelResult:
        self.tasks.append(task)
        self.prompts.append(prompt)
        assert schema_name == "dish_options"
        count = int(re.search(r"write (\d+) different dishes", prompt).group(1))  # type: ignore[union-attr]
        meal = re.search(r"for ONE meal, (\w[\w-]*)", prompt).group(1)  # type: ignore[union-attr]
        if meal == "breakfast":
            parts = [(_index(prompt, "Porridge oats"), 55), (_index(prompt, "Greek yoghurt"), 160)]
            steps, minutes = ["Stir the oats into the yoghurt.", "Leave for five minutes."], 5
        else:
            parts = [
                (_index(prompt, "Chicken breast"), 150),
                (_index(prompt, "Basmati rice"), 60),
                (_index(prompt, "Red pepper"), 80),
            ]
            steps, minutes = ["Cook the rice.", "Fry the chicken and pepper for 10 minutes."], 20
        options = [
            {
                "name": f"{meal.capitalize()} dish {n}",
                "ingredients": [{"i": i, "g": g + n} for i, g in parts],
                "method": steps,
                "prep_minutes": minutes,
            }
            for n in range(1, count + 1)
        ]
        return ModelResult(
            text="", data={"options": options}, model_used="fake-opus", backend="fake"
        )


class _App:
    """Enough of App for the week's choices: a gateway, the config, and nothing on a lane."""

    def __init__(self, model: Any, config: Any) -> None:
        self.model = model
        self.config = config

    def gateway(self, _db: Db) -> Any:
        return self.model


def _choosing(db: Db, pid: int, *, per_slot: int = 3, segments: str | None = None) -> None:
    """Her profile again from today with weekly choices on, cooking each day as she does (or the
    cadence given): a batch cook's choices are tests/test_planning.py's."""
    _profile(db, pid, effective_from="2026-09-13", choices=per_slot, segment_days=segments or DAILY)


def _consent(db: Db, pid: int) -> None:
    db.insert("consents", person_id=pid, granted=1, wording_version="v2")


def _liked_long_ago(db: Db, pid: int) -> int:
    """A prawn rice bowl she had once, five weeks back, and rated 5: liked, and long rested."""
    ids = {
        str(r["code"]): int(r["id"])
        for r in db.all(
            "SELECT id, code FROM ingredients WHERE code IN ('prawns', 'rice', 'pepper')"
        )
    }
    rid = seed_recipe(
        db, "Prawn rice bowl", 1, {ids["prawns"]: 160, ids["rice"]: 60, ids["pepper"]: 80}
    )
    _menu(db, pid, dt.date(2026, 8, 10), 1, {"lunch": rid})
    db.insert("ratings", person_id=pid, recipe_id=rid, score=5, note=None)
    return rid


def _built(week_, config, *, per_slot: int = 3) -> tuple[Db, int, choices.Built, ChoicesModel]:
    db, pid, _base, _r, clock = week_
    clock.set_local(SUNDAY)
    _liked_long_ago(db, pid)
    _choosing(db, pid, per_slot=per_slot)
    _consent(db, pid)
    model = ChoicesModel()
    built = choices.build(_App(model, config), db, pid, clock=clock)
    return db, pid, built, model


# ---------------------------------------------------------------- the days and the shares
def test_a_week_is_next_monday_to_friday_from_friday_on_and_the_rest_of_this_one_before() -> None:
    sunday = dt.date(2026, 9, 13)
    assert choices.week_days(sunday) == [MON + dt.timedelta(days=i) for i in range(5)]
    assert choices.week_days(dt.date(2026, 9, 11)) == [MON + dt.timedelta(days=i) for i in range(5)]
    assert choices.week_days(dt.date(2026, 9, 8)) == [
        dt.date(2026, 9, 9),
        dt.date(2026, 9, 10),
        dt.date(2026, 9, 11),
    ], "on a Tuesday: Wednesday to Friday, never today"
    assert choices.days_words([MON, FRI]) == "Mon 14 to Fri 18 Sep"


def test_each_meals_share_comes_from_partner_own_menus_and_adds_up_to_the_day(week) -> None:  # noqa: F811
    db, pid, _base, _r, clock = week
    clock.set_local(SUNDAY)
    got = choices.shares(db, pid, ["breakfast", "lunch", "dinner"], dt.date(2026, 9, 13))
    assert abs(sum(got.values()) - 1.0) < 0.001
    assert got != {s: choices.DEFAULT_SHARES[s] / 0.86 for s in ("breakfast", "lunch", "dinner")}, (
        "two weeks of her own weekdays outweigh the defaults"
    )
    none = choices.shares(
        db, pid, ["breakfast", "lunch", "dinner", "pre_bed"], dt.date(2026, 9, 13)
    )
    assert round(sum(none.values()), 3) == 1.0 and none["pre_bed"] > 0, "no such days: defaults"


# ---------------------------------------------------------------- building a week
def test_a_week_of_choices_is_written_sized_and_stored(week, config) -> None:  # noqa: F811
    db, _pid, built, model = _built(week, config)
    assert built.set_id is not None and not built.failed, built.refused
    assert model.tasks == ["menu_choices"] * 3, "one call a meal: breakfast, lunch, dinner"
    assert "Already offered, from the dishes they know" in model.prompts[1]
    assert "Prawn rice bowl" in model.prompts[1], "the dish she rated 5 is named as offered"
    options = choices.options_of(db, built.set_id)
    by_meal: dict[tuple[str, str], list[Any]] = {}
    for o in options:
        by_meal.setdefault((str(o["day"]), str(o["slot"])), []).append(o)
    assert len(by_meal) == 15, "five days, three meals"
    assert all(len(v) == 3 for v in by_meal.values()), "three dishes a meal"
    assert all(len({o["name"] for o in v}) == 3 for v in by_meal.values()), "never one twice a day"
    names = {str(o["name"]) for o in options}
    assert "Chicken stir-fry" not in names, "rated 1: never offered"
    assert "Salmon traybake" not in names, "a whole batch week of it two weeks ago: resting"
    prawns = [o for o in options if o["name"] == "Prawn rice bowl"]
    assert prawns and all(o["origin"] == "known" for o in prawns)
    assert "rated 5" in str(prawns[0]["why"])
    assert len({str(o["day"]) for o in prawns}) <= choices.KNOWN_REPEAT, (
        "a known dish twice at most"
    )
    # each sized to its meal's share of the day: whichever is picked, the day comes out near it
    row = db.one("SELECT * FROM choice_sets WHERE id = ?", (built.set_id,))
    assert row is not None
    split = json.loads(str(row["shares_json"]))
    for o in options:
        share = round(1300 * split[str(o["slot"])])
        assert abs(int(o["kcal"]) - share) <= share * choices.KCAL_REACH + 1, o["name"]
    new = [o for o in options if o["origin"] == "new"]
    assert new and all(o["why"] == "new" for o in new)
    # lunch and dinner draw on the same dishes: never the same one twice in a day
    for day in {str(o["day"]) for o in options}:
        names_today = [str(o["name"]) for o in options if str(o["day"]) == day]
        assert len(names_today) == len(set(names_today)), day


def test_without_consent_nothing_leaves_the_box_and_too_few_dishes_partner_knows_fail_loudly(
    week,  # noqa: F811
    config,
) -> None:
    db, pid, _base, _r, clock = week
    clock.set_local(SUNDAY)
    _choosing(db, pid)
    model = ChoicesModel()
    built = choices.build(_App(model, config), db, pid, clock=clock)
    assert model.tasks == [], "no consent, no call"
    assert built.failed and "too few breakfast dishes" in built.failed, built.failed
    assert "built as before" in built.failed, "and says what happens instead"
    assert choices.failed_on(db, pid, MON), "the failure is recorded for the ordinary build"


def test_off_means_nothing_is_built(week, config) -> None:  # noqa: F811
    db, pid, _base, _r, clock = week
    clock.set_local(SUNDAY)
    with pytest.raises(NothingToBuild, match="off"):
        choices.build(_App(ChoicesModel(), config), db, pid, clock=clock)


# ---------------------------------------------------------------- picking and setting the week
def test_picks_become_the_weeks_menu_and_the_rest_stay_on_the_swap_list(week, config) -> None:  # noqa: F811
    db, pid, built, _model = _built(week, config)
    clock = db.clock
    assert built.set_id is not None
    options = choices.options_of(db, built.set_id)
    tuesday_lunch = [o for o in options if o["day"] == isoday(MON + dt.timedelta(1))]
    tuesday_lunch = [o for o in tuesday_lunch if o["slot"] == "lunch"]
    second = tuesday_lunch[1]
    choices.pick(db, pid, built.set_id, int(second["id"]), clock=clock)
    settled = choices.settle(_App(None, config), db, pid, built.set_id, how="chosen", clock=clock)
    assert settled.chosen == 1 and settled.kept == 14
    assert "1 meals you picked and 14 first choices" in settled.said()
    tue = MON + dt.timedelta(days=1)
    plates = {
        p.slot: p for p in __import__("tiffin.food.swap", fromlist=["x"]).plates_on(db, pid, tue)
    }
    assert plates["lunch"].name == second["name"], "her pick is Tuesday's lunch"
    assert menu.accepted_menu_id(db, pid, tue) in settled.menu_ids, "decided at once"
    spare = {
        int(r["recipe_id"])
        for r in db.all("SELECT recipe_id FROM built_dishes WHERE person_id = ?", (pid,))
    }
    assert int(tuesday_lunch[0]["recipe_id"]) in spare, "the dish not picked is one tap away"
    # each day solved again from the picks, as every menu is
    day = db.one(
        "SELECT kcal FROM v_menu_days_current WHERE person_id = ? AND day = ?", (pid, isoday(tue))
    )
    assert day is not None and abs(int(day["kcal"]) - 1300) <= 1300 * 0.05 + 1
    with pytest.raises(NothingToBuild, match="already set"):
        choices.settle(_App(None, config), db, pid, built.set_id, how="chosen", clock=clock)
    with pytest.raises(NothingToBuild, match="already set"):
        choices.pick(db, pid, built.set_id, int(tuesday_lunch[2]["id"]), clock=clock)


def test_a_pick_is_partners_alone(week, config, two_persons) -> None:  # noqa: F811
    db, _pid, built, _model = _built(week, config)
    assert built.set_id is not None
    one = choices.options_of(db, built.set_id)[0]
    with pytest.raises(LookupError):
        choices.pick(db, two_persons["owner"], built.set_id, int(one["id"]), clock=db.clock)


# ---------------------------------------------------------------- the ordinary build stands down
def test_the_ordinary_build_leaves_a_chosen_week_alone_but_not_one_that_failed(
    week,  # noqa: F811
    config,
) -> None:
    db, pid, _base, _r, clock = week
    clock.set_local(SUNDAY)
    _choosing(db, pid)
    profile = db.one("SELECT * FROM profiles WHERE person_id = ? ORDER BY id DESC LIMIT 1", (pid,))
    today = dt.date(2026, 9, 13)
    nextweek = [MON + dt.timedelta(days=i) for i in range(5)]
    assert choices.stands_down(db, pid, profile, nextweek[:1], today), "Sunday's build writes it"
    assert not choices.stands_down(db, pid, profile, [dt.date(2026, 9, 19)], today), "weekend"
    # the week's build failed: the menu is built as before
    model = ChoicesModel()
    choices.build(_App(model, config), db, pid, clock=clock)  # no consent: fails
    assert not choices.stands_down(db, pid, profile, nextweek[:1], today)


# ---------------------------------------------------------------- the evening before
def test_a_week_not_set_by_the_evening_before_is_set_from_the_picks_so_far(week, config) -> None:  # noqa: F811
    from tiffin.jobs import menu_choices

    db, pid, built, _model = _built(week, config)
    assert built.set_id is not None
    clock = db.clock
    assert isinstance(clock, FrozenClock)
    clock.set_local(dt.datetime(2026, 9, 13, 20, 0, tzinfo=TZ))
    sent: list[tuple[str, str]] = []

    class Ctx:
        app = _App(None, config)
        person = type("P", (), {"id": pid})()
        day = dt.date(2026, 9, 13)

        def __init__(self) -> None:
            self.db = db
            self.clock = clock
            self.handle = type(
                "H", (), {"skip": lambda s, w: sent.append(("skip", w)), "note": lambda s, w: None}
            )()
            self.outbox = type(
                "O", (), {"send": lambda s, p, kind, key, html, *a, **k: sent.append((kind, html))}
            )()

    menu_choices.run_close(Ctx())  # type: ignore[arg-type]
    assert [k for k, _h in sent] == ["choices_set"], sent
    done = choices.outcome(db, built.set_id)
    assert done is not None and done["how"] == "closed"
    assert "first choices for every meal" in sent[0][1]


# ---------------------------------------------------------------- Sunday's shop for the week
def test_a_daily_cooks_sunday_list_buys_the_chosen_week(week, config) -> None:  # noqa: F811
    db, pid, _base, _r, clock = week
    clock.set_local(SUNDAY)
    _choosing(db, pid, segments=DAILY)
    _consent(db, pid)
    built = choices.build(_App(ChoicesModel(), config), db, pid, clock=clock)
    assert built.set_id is not None and not built.failed, built.refused
    settled = choices.settle(_App(None, config), db, pid, built.set_id, how="kept", clock=clock)
    assert len(settled.menu_ids) == 5, "a menu a day, as her cook is daily"
    shown = shopping.list_to_show(db, pid, dt.date(2026, 9, 13), clock=clock)
    assert shown is not None and shown["kind"] == "range"
    assert (shown["from_day"], shown["to_day"]) == (isoday(MON), isoday(FRI)), "Monday to Friday"
    assert settled.list_id == int(shown["id"])
    # and the day lists that would have bought it again, one a day, stand down
    assert all(choices.settled_on(db, pid, MON + dt.timedelta(days=i)) for i in range(5))


# ---------------------------------------------------------------- the page
def test_the_page_reads_the_week_picks_and_sets_it(page, config) -> None:  # noqa: F811
    from tiffin.food import choices as choices_mod
    from tiffin.web.app import create_app  # noqa: F401

    client, db, partners, _his, _r = page
    db.clock.set_local(SUNDAY)
    # she cooks each day, as she does: every meal is picked day by day (a batch cook's are
    # tests/test_planning.py's)
    _profile(db, partners, effective_from="2026-09-13", segment_days=DAILY)
    h = _headers(client)
    off = client.get("/api/choices", headers={"Tailscale-User-Login": PARTNER_LOGIN}).json()
    assert off["enabled"] is False and off["set"] is None
    on = client.post("/api/choices/setting", json={"per_slot": 3}, headers=h)
    assert on.status_code == 200, on.text
    assert "3 dishes for every weekday meal" in on.json()["said"]
    _consent(db, partners)
    built = choices_mod.build(_App(ChoicesModel(), config), db, partners, clock=db.clock)
    assert built.set_id is not None and not built.failed, built.refused
    got = client.get("/api/choices", headers={"Tailscale-User-Login": PARTNER_LOGIN}).json()
    week_ = got["set"]
    assert week_["status"] == "open" and week_["of"] == 15 and week_["picked"] == 0
    assert week_["deadline"] == "Sun 13 Sep, 20:00"
    monday = week_["days"][0]
    lunch = next(s for s in monday["slots"] if s["slot"] == "lunch")
    assert lunch["picked"] == lunch["options"][0]["id"] and lunch["chosen"] is False
    assert lunch["options"][0]["ingredients"] and lunch["options"][0]["method"]
    other = lunch["options"][2]
    picked = client.post(
        "/api/choices/pick",
        json={
            "set_id": week_["id"],
            "day": monday["day"],
            "slot": "lunch",
            "option_id": other["id"],
        },
        headers=h,
    )
    assert picked.status_code == 200, picked.text
    assert picked.json()["picked"] == 1
    day = picked.json()["day"]
    assert next(s for s in day["slots"] if s["slot"] == "lunch")["chosen"] is True
    his_h = _headers(client, OWNER_LOGIN)
    refused = client.post(
        "/api/choices/pick",
        json={
            "set_id": week_["id"],
            "day": monday["day"],
            "slot": "lunch",
            "option_id": other["id"],
        },
        headers=his_h,
    )
    assert refused.status_code == 404
    done = client.post("/api/choices/confirm", json={"set_id": week_["id"]}, headers=h)
    assert done.status_code == 200, done.text
    assert "is set" in done.json()["said"]
    after = client.get("/api/choices", headers={"Tailscale-User-Login": PARTNER_LOGIN}).json()
    assert after["set"]["status"] == "chosen" and after["set"]["chosen_on"]
    assert all(len(s["options"]) == 1 for d in after["set"]["days"] for s in d["slots"])


def test_a_build_from_the_page_is_marked_as_a_week_of_choices(page) -> None:  # noqa: F811
    client, db, _partners, _his, _r = page
    db.clock.set_local(SUNDAY)
    h = _headers(client)
    assert client.post("/api/choices/build", json={}, headers=h).status_code == 409, "off"
    client.post("/api/choices/setting", json={"per_slot": 3}, headers=h)
    started = client.post("/api/choices/build", json={}, headers=h)
    assert started.status_code == 200, started.text
    assert "Mon 14 to Fri 18 Sep" in started.json()["said"]
    # a week of choices outlasts a menu's fifteen minutes before its mark is thought stale
    mark = {"kind": building.CHOICES, "since": db.clock.utc_stamp(), "id": "x", "what": "w"}
    db.state_set("building:999", json.dumps(mark))
    db.clock.set_local(SUNDAY + dt.timedelta(minutes=30))
    assert building.current(db, 999, clock=db.clock) is not None


# ---------------------------------------------------------------- the setting
def test_choices_is_a_setting_of_its_own_never_asked_in_the_interview() -> None:
    from tiffin.onboarding import FIRST_STEPS, FOLLOW_UP_STEPS, STEP_BY_ID

    field = settings.BY_NAME["choices"]
    assert field.column == "choices"
    step = STEP_BY_ID[field.step]
    assert step.parse("off", {}) == 0 and step.parse("3", {}) == 3 and step.parse("4", {}) == 4
    assert step.id not in FIRST_STEPS and step.id in FOLLOW_UP_STEPS


def test_initial_schema_has_choices_tables_and_setting(fixture_db, two_persons) -> None:
    from tiffin import db as dbmod

    con = fixture_db.con
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"choice_sets", "choice_options", "choice_picks", "choice_outcomes"} <= tables
    assert "choices" in {r[1] for r in con.execute("PRAGMA table_info(profiles)")}
    assert dbmod.current_version(con) == dbmod.SCHEMA_VERSION
    triggers = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
    assert {"trg_choice_picks_noupd", "trg_choice_picks_nodel"} <= triggers, "a ledger"
