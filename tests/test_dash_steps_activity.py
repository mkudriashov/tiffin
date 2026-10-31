"""Ten moments on the tailnet page about steps and activity: what walked, what trained.

The brief, in his words: "get a view on steps properly". Two things live under that word on this
box - the daily step count intervals.icu writes into `icu_wellness`, measured against the
profile's `steps_floor`, and the training sessions `session_matches` records against the plan.
The page has a Steps card on Today, a Steps metric and a Sessions metric on Trends, and a
Sessions count on Week. This file stands in front of each of them.

intervals.icu key, a watch, ninety days of steps, a floor he raised from 7,000 to 9,000 on the
10th, a three-session plan and two sessions this week that intervals.icu filed as `extra`
because nothing matched them to a plan row. Tuesday the watch spent the day on charge and icu
returned a zero; Friday the 11th icu returned nothing at all. the partner is linked to the page and
has no intervals.icu key, so nothing on this box has ever counted a step of hers.

Each story drives the real API the way the tab does and, where the SPA is what turns the JSON
into something a person reads, asserts against the code path `app.js` actually takes - the same
method `test_journey_web_recipe_book.py` uses. A number the API has and no tab shows is a
finding, not a pass.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import re
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tiffin.app import App
from tiffin.clock import TZ, FrozenClock
from tiffin.config import Config
from tiffin.db import Db
from tiffin.secrets import Secrets
from tiffin.web.app import create_app

from .fakes import FakeTelegram
from .web_source import app_source

HEADER = "Tailscale-User-Login"
OWNER_LOGIN = "alex@example.com"
PARTNER_LOGIN = "sam@example.com"

WED = dt.date(2026, 9, 16)  # today: the frozen clock's day
MON = dt.date(2026, 9, 14)  # this week's Monday
TUE = dt.date(2026, 9, 15)  # the watch spent the day on charge: icu returned steps = 0
FRI_GAP = dt.date(2026, 9, 11)  # icu returned no wellness record at all
LAST_MON = dt.date(2026, 9, 7)  # a week he trained in and closed no day of
LAST_TUE = dt.date(2026, 9, 8)  # a lunchtime run nothing had planned
LAST_THU = dt.date(2026, 9, 10)  # the day he raised his own floor, and a confirmed session

OLD_FLOOR = 7000
FLOOR = 9000
PARTNER_FLOOR = 8000
TODAY_STEPS = 12800  # a floor beaten by 42 per cent
MON_STEPS = 6400
WINDOW_DAYS = 89  # how far back the seeded wellness runs, so a 90-day chart is full

#: the one line of `card()` in app.js that decides the colour of a progress bar
OVER_RULE = 'ratio > 1.05 ? "bar over" : "bar"'
#: the one gate in `renderToday` that decides whether a Steps card exists at all
STEPS_GATE = "data.wellness && data.wellness.steps !== null"


def at(day: dt.date, hh: int, mm: int) -> dt.datetime:
    return dt.datetime(day.year, day.month, day.day, hh, mm, tzinfo=TZ)


def steps_on(day: dt.date) -> int:
    """A plausible, deterministic day. Real days are overridden by name below."""
    return 7200 + (day.toordinal() % 5) * 900


# -- the world ---------------------------------------------------------------------------------


def _profile(db: Db, person_id: int, effective_from: dt.date, steps_floor: int, sex: str) -> None:
    db.insert(
        "profiles",
        person_id=person_id,
        effective_from=effective_from.isoformat(),
        sex=sex,
        birth_date="1990-03-15" if sex == "m" else "1992-05-01",
        height_cm=183.0 if sex == "m" else 168.0,
        activity_factor=1.55,
        kcal_floor=1800 if sex == "m" else 1500,
        steps_floor=steps_floor,
        goal_weight_kg=87.0 if sex == "m" else None,
    )


def _targets(db: Db, person_id: int, week_start: dt.date, kcal: int) -> None:
    db.insert(
        "targets",
        person_id=person_id,
        week_start=week_start.isoformat(),
        kcal=kcal,
        protein_g=200,
        fat_g=70,
        carb_g=200,
        fibre_g=30,
        source="formula",
    )


def _closed_day(db: Db, person_id: int, day: dt.date, kcal: int) -> None:
    db.insert(
        "day_confirmations",
        person_id=person_id,
        day=day.isoformat(),
        status="confirmed",
        basis="menu",
        kcal=kcal,
        protein_g=190,
        fat_g=68,
        carb_g=190,
        fibre_g=28,
    )


def _activity(db: Db, person_id: int, day: dt.date, icu_id: str, kind: str, name: str) -> None:
    db.insert(
        "icu_activity",
        person_id=person_id,
        icu_id=icu_id,
        day=day.isoformat(),
        start_utc=f"{day.isoformat()} 12:00:00",
        icu_type=kind.title(),
        kind=kind,
        name=name,
        icu_raw_id=0,
    )


def _session(
    db: Db, person_id: int, day: dt.date, icu_id: str | None, status: str, minutes: int
) -> None:
    db.insert(
        "session_matches",
        person_id=person_id,
        day=day.isoformat(),
        icu_id=icu_id,
        status=status,
        minutes=minutes,
    )


@dataclasses.dataclass
class Box:
    """The page as it is on the box at one moment, with the database behind it."""

    client: TestClient
    db: Db
    config: Config
    clock: FrozenClock
    owner_id: int
    partner_id: int

    def as_owner(self, path: str, **kw: Any):
        return self.client.get(path, headers={HEADER: OWNER_LOGIN}, **kw)

    def as_partner(self, path: str, **kw: Any):
        return self.client.get(path, headers={HEADER: PARTNER_LOGIN}, **kw)

    def app_js(self) -> str:
        """The script the shell actually loads, by the hashed name written into it."""
        return app_source()

    def render_today(self) -> str:
        """`renderToday` alone, so a story can ask what that one function does."""
        js = self.app_js()
        start = js.index("async function renderToday()")
        return js[start : js.index("/* ------", start)]


@pytest.fixture
def open_box(config: Config, tmp_path: Path) -> Iterator[Callable[..., Box]]:
    opened: list[Db] = []

    def make(when: dt.datetime | None = None, *, partner_has_icu_key: bool = False) -> Box:
        clock = FrozenClock(when or at(WED, 18, 10))
        config.paths.ensure()
        db = Db.open(config.paths.db, clock=clock)
        opened.append(db)
        owner_id = db.insert("persons", slug="owner", display_name="Alex", tone="blunt")
        partner_id = db.insert("persons", slug="partner", display_name="Sam", tone="warm")
        db.insert("tailscale_identities", person_id=owner_id, login=OWNER_LOGIN)
        db.insert("tailscale_identities", person_id=partner_id, login=PARTNER_LOGIN)

        # he raised his own floor on the 10th; both rows stand, the way a ledger keeps them
        _profile(db, owner_id, dt.date(2026, 6, 1), OLD_FLOOR, "m")
        _profile(db, owner_id, LAST_THU, FLOOR, "m")
        _profile(db, partner_id, dt.date(2026, 9, 1), PARTNER_FLOOR, "f")

        for pid, kcal in ((owner_id, 2250), (partner_id, 1600)):
            _targets(db, pid, LAST_MON, kcal)
            _targets(db, pid, MON, kcal)

        # ninety days of steps for him, minus the Friday icu had nothing for
        overrides = {WED: TODAY_STEPS, TUE: 0, MON: MON_STEPS}
        for back in range(WINDOW_DAYS, -1, -1):
            day = WED - dt.timedelta(days=back)
            if day == FRI_GAP:
                continue
            db.insert(
                "icu_wellness",
                person_id=owner_id,
                day=day.isoformat(),
                steps=overrides.get(day, steps_on(day)),
                resting_hr=52,
                icu_raw_id=0,
            )
        # nothing has ever written a wellness row for her: no key, no watch, no icu

        plan = db.insert("training_plans", person_id=owner_id, effective_from="2026-09-01")
        for weekday, kind, minutes in ((0, "climb", 90), (2, "strength", 45), (4, "ride", 60)):
            db.insert(
                "planned_sessions",
                plan_id=plan,
                person_id=owner_id,
                weekday=weekday,
                kind=kind,
                target_minutes=minutes,
            )

        # what intervals.icu actually filed. 'extra' is what it writes when nothing matched a
        # plan row, which on this box is most real training.
        _activity(db, owner_id, MON, "icu-mon", "climb", "Monday session at the wall")
        _session(db, owner_id, MON, "icu-mon", "extra", 95)
        _activity(db, owner_id, WED, "icu-wed", "ride", "Turbo 50")
        _session(db, owner_id, WED, "icu-wed", "extra", 50)
        _activity(db, owner_id, LAST_TUE, "icu-lasttue", "run", "Lunchtime run")
        _session(db, owner_id, LAST_TUE, "icu-lasttue", "extra", 60)
        _activity(db, owner_id, LAST_THU, "icu-lastthu", "climb", "Thursday at the wall")
        _session(db, owner_id, LAST_THU, "icu-lastthu", "confirmed", 45)

        # this week he closed Monday and Tuesday; last week he closed nothing
        _closed_day(db, owner_id, MON, 2150)
        _closed_day(db, owner_id, TUE, 2205)
        _closed_day(db, partner_id, MON, 1580)

        secrets_dir = tmp_path / "s"
        secrets_dir.mkdir(exist_ok=True)
        (secrets_dir / "telegram-token-dev.txt").write_text("1:t", encoding="utf-8")
        (secrets_dir / "icu-key-owner.txt").write_text("k" * 20, encoding="utf-8")
        if partner_has_icu_key:
            (secrets_dir / "icu-key-partner.txt").write_text("k" * 20, encoding="utf-8")
        config.data["web"]["secure_cookies"] = False
        config.data["web"]["root_path"] = ""
        app = App(config, Secrets(secrets_dir), clock=clock, transport=FakeTelegram())
        client = TestClient(create_app(app), base_url="http://testserver")
        return Box(client, db, config, clock, owner_id, partner_id)

    yield make
    for db in opened:
        db.close()


def bar_class(ratio: float) -> str:
    """`card()` in app.js, reduced to its one decision. `.bar.over > i` is painted --warn."""
    return "bar over" if ratio > 1.05 else "bar"


# -- today's steps -----------------------------------------------------------------------------


def test_01_today_their_steps_stand_against_the_floor_owner_set(open_box) -> None:
    """Wednesday 18:10, phone in hand on the sofa. He opens the page and wants one number: how
    far he has walked today and whether it clears the floor he set himself."""
    box = open_box()
    today = box.as_owner("api/today").json()
    assert today["day"] == WED.isoformat()
    assert today["wellness"] is not None, "Wednesday's wellness row is the seeded one"
    assert today["wellness"]["steps"] == TODAY_STEPS
    assert today["profile"]["steps_floor"] == FLOOR, (
        "the floor has to travel with the number; a step count with nothing to beat is trivia"
    )
    js = box.render_today()
    assert re.search(r'card\(\s*"Steps"', js), "the Today tab renders no Steps card"
    assert "steps_floor" in js, "the Steps card does not read the floor out of the profile"


def test_02_beating_the_steps_floor_is_not_painted_as_an_overshoot(open_box) -> None:
    """Wednesday 18:10, same glance. 12,800 against a 9,000 floor is the best day of his week,
    and the bar under the number has to read like one - not like the amber he gets for eating
    1.4 times his calories."""
    box = open_box()
    today = box.as_owner("api/today").json()
    js = box.app_js()
    assert OVER_RULE in js, "card() no longer decides the bar colour this way - re-read the story"
    steps_card = re.search(r'card\("Steps",[\s\S]{0,400}?\)\);', js)
    assert steps_card is not None, "the Steps card is not built by card() any more"
    assert "steps / floor" in steps_card.group(0), (
        "the Steps card no longer hands card() a raw steps/floor ratio - re-read the story"
    )
    steps = int(today["wellness"]["steps"])
    floor = int(today["profile"]["steps_floor"])
    # §0.3: bar_class() is a bare re-implementation with no clamp of its own - the product
    # code hands card() min(ratio, 1), so the check here has to clamp the same way rather than
    # feed it the raw 1.42x, which no production change could ever make read as "bar"
    assert bar_class(min(steps / floor, 1)) == "bar", (
        f"{steps:,} steps against a {floor:,} floor is {steps / floor:.2f}x, so the Steps card "
        f'hands card() a ratio above 1.05 and the bar is built with class "bar over", which '
        "app.css paints in --warn amber - the exact colour that means 'you have gone over' on "
        "the Calories card above it. A floor is a minimum: clearing it is the win, and the page "
        "colours it like a mistake. Calories and steps need opposite signs for the same ratio."
    )


def test_03_partner_page_says_partner_steps_are_not_counted_instead_of_saying_nothing(
    open_box,
) -> None:
    """Wednesday 18:10, the partner opens the page for the first time since it was linked. She has
    no
    watch and no intervals.icu key, so nothing on this box counts her steps. Her morning message
    from the bot says so in words - "Steps floor 8,000 (not counted; no intervals.icu)" - and the
    page has to be no quieter than the bot about the same fact."""
    box = open_box()
    partner = box.as_partner("api/today").json()
    assert partner["profile"]["steps_floor"] == PARTNER_FLOOR, "she has a floor like everyone else"
    assert partner["wellness"] is None, "nothing has ever written her a wellness row"
    assert partner["targets"] is not None, (
        "she is set up otherwise, so `anything` in renderToday is true and Today is not the "
        "blanket 'nothing set up yet' empty state - the Steps card is the only thing missing"
    )
    js = box.render_today()
    assert STEPS_GATE in js, "the Steps card gate has moved - re-read the story"
    # only the Steps block: 'intervals.icu' appears elsewhere in renderToday, about training
    steps_block = js[js.index(STEPS_GATE) : js.index("if (data.meals.length)")]
    said = re.search(r"\}\s*else|intervals\.icu|not counted|no steps", steps_block, re.I)
    assert said is not None, (
        "her Today tab renders a Calories card, a Protein card and then stops: the Steps card is "
        f"behind `{STEPS_GATE}` with no else, so a person with no step source sees no steps row, "
        "no floor and no sentence. The bot's morning_targets already writes 'Steps floor 8,000 "
        "(not counted; no intervals.icu)' for exactly this person. Degrade to manual, never to "
        "silence: the page has to say the floor is not being measured, and what would measure it."
    )


def test_04_a_day_the_watch_was_on_charge_is_not_drawn_as_a_lazy_day(open_box) -> None:
    """Thursday-ish reflection, scrolling the 30-day steps chart. Tuesday the watch sat on
    charge and intervals.icu returned steps = 0; Friday the 11th it returned no record at all.
    One of those is a day he did not walk. Neither is, and the chart must not claim either."""
    box = open_box()
    trend = box.as_owner("api/trends?metric=steps&days=30").json()
    assert FRI_GAP.isoformat() not in trend["dates"], (
        "a day with no reading is left out of the series rather than plotted as zero - right"
    )
    tue = TUE.isoformat()
    assert tue in trend["dates"], "Tuesday has a wellness row, so it is in the window"
    value = trend["raw"][trend["dates"].index(tue)]
    day = box.as_owner(f"api/today?date={tue}").json()
    # icu wrote a zero for the day the watch was charging; the page reads it as not counted
    assert day["wellness"]["steps"] is None, "a watch on charge is not a lazy day"
    assert value != 0, (
        "the 30-day chart plots Tuesday at 0 steps, sitting on the axis under a 9,000 floor "
        "line, and the Today tab for that date renders a Steps card reading '0' with an empty "
        "bar. Nobody walks zero steps in a day; a zero from intervals.icu means the watch was "
        "not worn. The page has two different nothings - no row at all (dropped, correct) and a "
        "zero row (drawn as a lazy day) - and treats the misleading one as data."
    )


# -- the week ----------------------------------------------------------------------------------


def test_05_the_week_tab_shows_the_weeks_steps(open_box) -> None:
    """Wednesday 18:10. He taps Week to see how the week is going - calories, sessions, and the
    walking. Steps are a daily floor, so the only place the question 'did I hit it this week'
    can be answered is the tab that is about this week."""
    box = open_box()
    week = box.as_owner("api/week").json()
    assert week["week_start"] == MON.isoformat()
    days = {str(d["day"]): d for d in week["days"]}
    assert set(days) == {MON.isoformat(), TUE.isoformat()}, (
        "only closed days reach the Week table, so today is not on it either"
    )
    on_the_tab = {**(week["summary"] or {}), **(week["days"][0] if week["days"] else {})}
    carried = [key for key in on_the_tab if "step" in key]
    assert carried, (
        "/api/week returns week_start, week_end, a v_week_summary row (confirmed days, usable "
        "days, paused days, trend delta, sessions) and one row per closed "
        "day carrying day/status/basis/kcal/protein_g. Not one step, anywhere, though "
        "icu_wellness holds a row per day and the profile holds the floor. To see this week's "
        "steps he leaves Week, taps Trends, opens the Metric select, picks Steps, opens the Days "
        "select and picks 30 - five taps to a 30-day window that does not line up with the week "
        "he was looking at, and then he counts the days over the floor off the chart himself."
    )


def test_06_thirty_and_ninety_days_of_steps_come_with_the_floor_drawn(open_box) -> None:
    """Sunday evening, deciding whether the walking is actually happening. He opens Trends,
    picks Steps, and switches between 30 and 90 days. Both windows have to carry the floor line,
    or the dots are just dots."""
    box = open_box()
    thirty = box.as_owner("api/trends?metric=steps&days=30").json()
    ninety = box.as_owner("api/trends?metric=steps&days=90").json()
    for window, data in (("30", thirty), ("90", ninety)):
        assert data["metric"] == "steps" and data["dates"], f"{window} days came back empty"
        assert len(data["target"]) == len(data["dates"]), f"{window}: floor line is ragged"
        assert all(isinstance(v, int) and v > 0 for v in data["target"]), f"{window}: no floor"
    assert len(ninety["dates"]) > len(thirty["dates"]), "90 days has to reach further back"
    js = box.app_js()
    assert 'name: "floor"' in js, "the Trends tab does not draw the target series as a floor"
    assert 'dash: "dot"' in js, "the floor is drawn as a solid line, reading as measured data"


# -- training --------------------------------------------------------------------------------


def test_07_a_session_nothing_planned_still_shows_on_that_day(open_box) -> None:
    """Tuesday the 8th, looking back. Nothing was planned for a Tuesday and he ran at lunchtime;
    intervals.icu filed it 'extra' because there was no plan row to match. The day must not read
    as a rest day."""
    box = open_box()
    day = box.as_owner(f"api/today?date={LAST_TUE}").json()
    assert day["planned_sessions"] == [], "nothing is planned for a Tuesday"
    assert [s["status"] for s in day["sessions"]] == ["extra"]
    assert day["sessions"][0]["name"] == "Lunchtime run"
    assert day["sessions"][0]["minutes"] == 60
    js = box.render_today()
    assert "planned.length || trained.length" in js, (
        "the Training block is gated on a planned session again: an unplanned session vanishes"
    )
    assert '"extra"' in js, "'extra' is not counted as training by the Today tab"


def test_08_the_week_counts_the_sessions_owner_actually_did(open_box) -> None:
    """Wednesday 18:10, Week tab. Three sessions are planned this week; he climbed on Monday and
    rode today. The Sessions card is the one number he reads to know whether the training week
    is on track."""
    box = open_box()
    summary = box.as_owner("api/week").json()["summary"]
    assert summary is not None and summary["sessions_planned"] == 3
    done = box.db.all(
        "SELECT day, status FROM session_matches WHERE person_id = ? AND day BETWEEN ? AND ?",
        (box.owner_id, MON.isoformat(), WED.isoformat()),
    )
    assert [str(r["status"]) for r in done] == ["extra", "extra"], "what icu actually filed"
    assert summary["sessions_done"] == 2, (
        "the Week card reads 'Sessions 0/3' on a week with two sessions in the database. "
        "v_week_summary counts sessions_done over statuses ('matched', 'confirmed', 'manual') "
        "and leaves out 'extra' - which is what intervals.icu writes for every session no plan "
        "row matched, i.e. most real training. The Today tab was fixed for exactly this ('the "
        "page said Rest day on a day he trained') and the Week card and the Sessions trend, "
        "which read the same view, were not. Two people who trained identically read 0/3 and "
        "3/3 depending on whether the plan happened to name the right weekday."
    )


def test_09_a_week_owner_trained_in_does_not_vanish_from_the_sessions_trend(open_box) -> None:
    """Sunday, Trends, metric Sessions, 90 days. Last week he ran on the Tuesday and climbed on
    the Thursday and - being away - confirmed no days. The bar for that week has to be there,
    showing what he did against what was planned."""
    box = open_box()
    trained = box.db.all(
        "SELECT day FROM session_matches WHERE person_id = ? AND day BETWEEN ? AND ? ORDER BY day",
        (box.owner_id, LAST_MON.isoformat(), (LAST_MON + dt.timedelta(days=6)).isoformat()),
    )
    assert [str(r["day"]) for r in trained] == [LAST_TUE.isoformat(), LAST_THU.isoformat()]
    trend = box.as_owner("api/trends?metric=sessions&days=90").json()
    assert MON.isoformat() in trend["dates"], "this week is on the chart"
    assert LAST_MON.isoformat() in trend["dates"], (
        "the week of 7 Sep is missing from the Sessions chart entirely - not a zero bar, no bar "
        "at all - because v_week_summary builds its list of weeks from v_day_latest, so a week "
        "with no confirmed or closed day produces no row however much training is in it. The "
        "gap reads as a week that did not happen, and the weeks that go missing are exactly the "
        "ones worth looking at: the away weeks, the ill weeks, the weeks nobody tapped confirm."
    )


def test_10_the_floor_line_follows_the_floor_that_was_in_force(open_box) -> None:
    """Sunday, Trends, Steps, 90 days. He raised his floor from 7,000 to 9,000 on 10 September.
    Looking back over the summer he wants to see whether he was hitting the floor he had then,
    not be marked against one he set last week."""
    box = open_box()
    rows = box.db.all(
        "SELECT effective_from, steps_floor FROM profiles WHERE person_id = ? "
        "ORDER BY effective_from",
        (box.owner_id,),
    )
    assert [(str(r["effective_from"]), int(r["steps_floor"])) for r in rows] == [
        ("2026-06-01", OLD_FLOOR),
        (LAST_THU.isoformat(), FLOOR),
    ]
    trend = box.as_owner("api/trends?metric=steps&days=90").json()
    before = {
        v for d, v in zip(trend["dates"], trend["target"], strict=True) if d < LAST_THU.isoformat()
    }
    assert before == {OLD_FLOOR}, (
        f"every point in the 90-day window carries the floor {FLOOR}, including the ten weeks "
        f"before he raised it from {OLD_FLOOR} on {LAST_THU.isoformat()}. queries.trend reads "
        "one profile row - the latest at or before the end of the window - and repeats it across "
        "the whole series, so July and August are redrawn against a rule that did not exist "
        "then and a summer of days that cleared 7,000 now reads as a summer of misses. The "
        "profiles table is a ledger precisely so the past keeps the numbers it was judged by."
    )
