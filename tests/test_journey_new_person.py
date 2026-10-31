"""A new person, from the first message to a week of food, and what moves the calories.

with a test user - how they add trainings, how calories get calculated, how menu is made, how
user can add new ingredients. additionally check if there is a specific setup where trainings and
calorie spend per day then adjust the menu / calories that are needed to keep the same deficit?"

Every other journey here starts from a fixture: a profile already written, targets already in the
table, a menu already built. This one starts from nothing and walks the joins between the parts,
because that is where a system of working parts stops working - the interview writes a profile
the targets job cannot read, a session lands on a day the plan does not have, a food is added to
a catalogue the menu prompt is numbered from somewhere else.

The answer to the training question is in story 05, and it is not the one the question expects.
There is no per-day eat-back: a hard Tuesday does not raise Tuesday's menu. Training reaches the
calories through the SUNDAY CALIBRATION, which measures TDEE from what was actually eaten against
what the trend actually did (energy.calibrate_tdee). Somebody who trains more loses faster at the
same intake, the trend says so, and the measured TDEE rises - so the deficit is held by
measurement rather than by adding up watts. Story 05 walks exactly that and states what it means.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from tiffin import energy
from tiffin import targets as targets_mod
from tiffin.app import App
from tiffin.clock import TZ, FrozenClock
from tiffin.db import Db
from tiffin.food import catalogue
from tiffin.secrets import Secrets
from tiffin.telegram.poller import Poller

from .fakes import FakeTelegram

CHAT = 1000001
MONDAY = dt.date(2026, 9, 14)
SUNDAY_BEFORE = dt.date(2026, 9, 13)


def at(day: dt.date, hh: int, mm: int = 0) -> dt.datetime:
    return dt.datetime(day.year, day.month, day.day, hh, mm, tzinfo=TZ)


@pytest.fixture
def newcomer(fixture_db: Db, two_persons, frozen_clock: FrozenClock, config, tmp_path: Path):
    """The bot, the poller and a person who has said nothing to it yet."""
    frozen_clock.set_local(at(SUNDAY_BEFORE, 10, 0))
    secrets_dir = tmp_path / "s"
    secrets_dir.mkdir()
    (secrets_dir / "telegram-token-dev.txt").write_text("1:t", encoding="utf-8")
    tg = FakeTelegram()
    app = App(
        config,
        Secrets(secrets_dir),
        clock=frozen_clock,
        transport=tg,
        gateway_factory=lambda _app, _db: None,
    )
    handlers = app.handlers(fixture_db)
    poller = Poller(
        app.api(),
        fixture_db,
        app.registry(fixture_db),
        handlers.handle,
        clock=frozen_clock,
        sleeper=lambda s: None,
    )
    n = {"i": 0}

    def say(text: str) -> str:
        n["i"] += 1
        tg.queue_update(tg.text_update(n["i"], CHAT, text))
        poller.run_once()
        return tg.sent[-1]["text"] if tg.sent else ""

    def tap(step: str, value: str) -> str:
        n["i"] += 1
        tg.queue_update(tg.callback_update(n["i"], CHAT, f"k:ob:{step}:{value}", message_id=n["i"]))
        poller.run_once()
        return tg.sent[-1]["text"] if tg.sent else ""

    return {
        "app": app,
        "db": fixture_db,
        "tg": tg,
        "say": say,
        "tap": tap,
        "clock": frozen_clock,
        "config": config,
        "pid": two_persons["owner"],
    }


def _interview(w, *, rate: str = "0.7", max_loss: str = "1.0") -> None:
    """Finish the short interview, then answer the optional questions when ready."""
    say, tap = w["say"], w["tap"]
    say("/onboard")
    tap("cons", "1")
    tap("sex", "m")
    say("1990-03-15")
    say("183")
    say("100.9")
    tap("act", "1.55")
    tap("rate", rate)
    say(f"/followup mloss {max_loss}")
    say("/followup floor 1800")
    say("/followup morn 07:00")
    say("/followup conf 20:45")
    say("/followup steps 10000")
    say("/followup meals 4")
    say("/followup pln auto")
    say("/followup segs mf")
    say("/followup frz 1")
    say("/followup vary same")
    say("/followup refu liver, celery")
    say("/followup pant rice, olive oil, salt")
    say("/followup kit oven, hob, air fryer")
    say("/followup plan Mon climb 90 at 18:30, Wed ride 60, Sat ropes 120")
    say("/followup tone blunt")


# -- 01. the interview writes a person the rest of the system can read ---------------------------


def test_01_the_interview_ends_with_a_profile_and_a_week_of_targets(newcomer) -> None:
    """Sunday 10:00. He sends /onboard and answers. By the last tap Tiffin has to hold a
    person: a profile, a first weight, a training week, and this week's numbers - not a pile of
    answers waiting for a job to turn them into something."""
    w = newcomer
    _interview(w)
    db, pid = w["db"], w["pid"]

    prof = db.latest("profiles", person_id=pid)
    assert prof is not None, "the interview finished and wrote no profile"
    assert prof["sex"] == "m" and prof["height_cm"] == 183.0
    assert prof["rate_pct_per_week"] == 0.7 and prof["kcal_floor"] == 1800

    weight = targets_mod.latest_weight(db, pid, SUNDAY_BEFORE)
    assert weight is not None and weight[1] == 100.9, "the weight he typed is his first weigh-in"

    # the plan holds all seven days - a rest day is a fact about the week, and the evening
    # check-in asks differently on one - so the three he described are the ones with minutes
    sessions = db.all(
        "SELECT weekday, kind, target_minutes FROM planned_sessions "
        "WHERE target_minutes > 0 ORDER BY weekday"
    )
    described = [(int(r["weekday"]), str(r["kind"])) for r in sessions]
    assert len(sessions) == 3, f"three sessions were described; stored {described}"
    assert [int(s["weekday"]) for s in sessions] == [0, 2, 5], "Mon, Wed and Sat"

    row = targets_mod.latest_targets(db, pid, SUNDAY_BEFORE)
    assert row is not None, "no targets, so the morning message has nothing to say"
    assert row["kcal"] > 0 and row["protein_g"] > 0


# -- 02. and the calories can be explained -------------------------------------------------------


def test_02_the_calories_are_a_chain_anyone_can_check(newcomer) -> None:
    """The one number everything else hangs off. It is not a guess: Mifflin from his own sex,
    height, age and weight, multiplied by the activity he chose, less the deficit his rate asks
    for, held above his floor. The basis is stored with the row so the chain can be read back."""
    w = newcomer
    _interview(w)
    db, pid = w["db"], w["pid"]
    row = targets_mod.latest_targets(db, pid, SUNDAY_BEFORE)
    assert row is not None
    basis = json.loads(str(row["basis_json"]))

    age = energy.age_on(dt.date(1990, 3, 15), SUNDAY_BEFORE)
    bmr = energy.bmr_mifflin("m", 100.9, 183.0, age)
    tdee = energy.tdee_formula(bmr, 1.55)
    assert basis["bmr"] == pytest.approx(bmr, abs=1), basis
    assert basis["tdee"] == pytest.approx(tdee, abs=1), basis

    # 0.7 % of 100.9 kg is 0.706 kg a week, under the ceiling, so the rate is what bites
    deficit = energy.deficit_for_rate(100.9, 0.7, 0.8)
    assert basis["deficit_kcal"] == pytest.approx(deficit, abs=1)
    initial_floor = max(1200, round(bmr / 50) * 50)
    assert int(row["kcal"]) == pytest.approx(max(initial_floor, tdee - deficit), abs=2), (
        f"the target is not TDEE minus the deficit: {dict(row)}"
    )

    # protein and fat are floors, carbs fill what is left, and none of it is the model's opinion
    assert int(row["protein_g"]) == pytest.approx(100.9 * 2.0, abs=1)
    assert 4 * int(row["protein_g"]) + 9 * int(row["fat_g"]) <= int(row["kcal"]) + 1


# -- 03. a person who wants a kilogram a week can have one ---------------------------------------


def test_03_a_kilo_a_week_is_reachable_and_the_floor_still_holds(newcomer) -> None:
    """
    if needed." The rate is a share of bodyweight and the ceiling is in kilograms; both have to
    allow it, and the floor still has the last word."""
    w = newcomer
    _interview(w, rate="1.0", max_loss="1.0")
    db, pid = w["db"], w["pid"]
    prof = db.latest("profiles", person_id=pid)
    assert prof is not None, "the interview finished and wrote no profile"
    assert float(prof["max_loss_kg_week"]) == 1.0, (
        f"the ceiling he chose: {prof['max_loss_kg_week']}"
    )

    row = targets_mod.ensure_targets(db, pid, MONDAY, w["config"], clock=w["clock"])
    assert row is not None
    basis = json.loads(str(row["basis_json"]))
    # 1 % of 100.9 kg is 1.009 kg a week, so the ceiling he chose is what binds - at 0.8 it was
    # holding his fastest choice back with no way for him to raise it
    assert basis["deficit_kcal"] == pytest.approx(1.0 * 7700 / 7, abs=1), basis
    assert int(row["kcal"]) >= 1800, "the floor is the backstop, whatever the ceiling says"


# -- 04. the training week he described is a week Tiffin can match against ------------------


def test_04_the_training_owner_described_is_a_week_with_days_in_it(newcomer) -> None:
    """He typed one line of prose. What has to come out of it is a plan the rest of Tiffin
    can work against: a session on a weekday, with minutes, that an intervals.icu activity can
    later be matched to and the evening check-in can ask about."""
    w = newcomer
    _interview(w)
    db = w["db"]
    rows = db.all(
        "SELECT weekday, kind, target_minutes, start_time FROM planned_sessions "
        "WHERE target_minutes > 0 ORDER BY weekday"
    )
    assert [int(r["weekday"]) for r in rows] == [0, 2, 5]
    monday = rows[0]
    assert int(monday["target_minutes"]) == 90, dict(monday)
    assert str(monday["start_time"]).startswith("18:"), "the time he gave survives the parse"
    assert all(str(r["kind"]) for r in rows), "a session with no kind cannot be matched"


# -- 05. what actually moves the calories --------------------------------------------------------


def test_05_training_reaches_the_calories_through_measurement_not_arithmetic(newcomer) -> None:
    """The question this file was written for.

    There is NO per-day eat-back: a hard Tuesday does not raise Tuesday's menu, and nothing in
    Tiffin adds an activity's calories to a day's target. That is deliberate. The TDEE these
    targets are built on is not a formula after the first week - it is MEASURED, from what was
    actually eaten against what the trend actually did (`energy.calibrate_tdee`). Somebody who
    trains more loses faster at the same intake; the trend says so; the measured TDEE rises; and
    the next week's target rises with it, holding the same deficit. Adding the watts on top would
    count the same training twice, because the measurement already contains it.

    This walks the arithmetic of that claim rather than asserting the sentence.
    """
    # a fortnight at 2200 kcal, and the trend fell 0.5 kg over the week
    days = [
        energy.DayIntake(day=MONDAY + dt.timedelta(days=i), kcal=2200, status="confirmed")
        for i in range(7)
    ]
    quiet = energy.calibrate_tdee(days, delta_kg=-0.5, prior_tdee=2700.0, prior_count=4)
    assert isinstance(quiet, energy.Calibration), quiet
    # 2200 eaten while losing 0.5 kg means about 2200 + 550 = 2750 burned
    assert quiet.tdee_measured == pytest.approx(2200 + 0.5 * 7700 / 7, abs=1)

    # the same intake, a heavier training week, and the trend falls further: the measurement
    # moves on its own, and so does the target built from it
    harder = energy.calibrate_tdee(days, delta_kg=-0.9, prior_tdee=2700.0, prior_count=4)
    assert isinstance(harder, energy.Calibration), harder
    assert harder.tdee_measured > quiet.tdee_measured, (
        "training that shows up in the trend has to show up in the measured burn"
    )
    assert harder.tdee_blended > quiet.tdee_blended

    same_deficit = energy.weekly_targets(
        weight_kg=100.9, tdee=harder.tdee_blended, rate_pct=0.7, max_loss_kg_week=0.8,
        kcal_floor=1800,
    )  # fmt: skip
    quieter = energy.weekly_targets(
        weight_kg=100.9, tdee=quiet.tdee_blended, rate_pct=0.7, max_loss_kg_week=0.8,
        kcal_floor=1800,
    )  # fmt: skip
    assert same_deficit.kcal > quieter.kcal, (
        "a harder week has to come back as more food at the same deficit, or the deficit grows "
        "every time he trains and nobody ever told him"
    )
    # and the deficit itself is unchanged: it is the burn that moved, not the ambition
    assert (harder.tdee_blended - same_deficit.kcal) == pytest.approx(
        quiet.tdee_blended - quieter.kcal, abs=1
    )


# -- 06. a food he bought, and the menu that can then use it -------------------------------------


def test_06_a_food_owner_adds_is_in_the_list_the_menu_is_built_from(newcomer) -> None:
    """The last join, and the one with no job in between: `catalogue.approved()` IS the roster
    the menu prompt is numbered from, so a food typed off a label is in the next menu with
    nothing else to do. The interview's own kitchen answers are in there too."""
    w = newcomer
    _interview(w)
    db, pid = w["db"], w["pid"]

    before = {i.code for i in catalogue.approved(db, pid)}
    got = catalogue.add_food(
        db,
        name="Arla Skyr Natural",
        kcal_100=60,
        protein_100=10.0,
        fat_100=0.2,
        carb_100=4.0,
        fibre_100=0.0,
        pack_g=450,
        pack_label="450 g tub",
        aisle="dairy",
    )
    assert got["version"] == 1 and not got["warning"], got

    after = catalogue.approved(db, pid)
    codes = {i.code for i in after}
    assert codes - before == {"arla_skyr_natural"}, "the food did not reach the roster"

    # the prompt is numbered from that same list, so the model can actually pick it
    lines = catalogue.prompt_lines(after)
    assert any("Arla Skyr Natural" in ln for ln in lines), lines[:3]
    assert len(lines) == len(after), "one numbered line per food, or the indexes lie"


def test_07_a_refused_food_never_reaches_the_roster_however_it_got_in(newcomer) -> None:
    """He typed "liver, celery" at the refuse question. A food he adds by hand later must not
    walk around that: the roster is one function, and it filters what it is given."""
    w = newcomer
    _interview(w)
    db, pid = w["db"], w["pid"]
    catalogue.add_food(
        db,
        name="Liver, calves",
        kcal_100=137,
        protein_100=20.0,
        fat_100=6.0,
        carb_100=1.0,
        fibre_100=0.0,
        pack_g=400,
        pack_label="400 g",
        aisle="meat",
    )
    codes = {i.code for i in catalogue.approved(db, pid)}
    assert "liver_calves" not in codes, (
        "a food he refused in the interview came back because he typed it into the form"
    )
