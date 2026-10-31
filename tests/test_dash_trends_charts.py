"""Ten moments in front of the Trends tab: does the chart answer the question that opened it.

months of closed days, four months of steps from intervals.icu and twelve Sundays of tape
measures. the partner is on the tailnet with nothing recorded at all - the state a second person is
in on their first weekend.

Each story drives the real API the way the tab does (`GET api/trends?metric=&days=`) and, where
the answer is drawn rather than returned, reads the script the shell actually loads and asserts
on the code path it takes - the way `test_journey_web_recipe_book.py` does. A number the API
carries that no chart draws is a finding, and so is a chart that is correct and still leaves the
person unable to answer what they opened it for.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import itertools
import math
import re
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tiffin import body as body_mod
from tiffin.app import App
from tiffin.clock import TZ, FrozenClock, isoday, week_start
from tiffin.config import Config
from tiffin.db import Db
from tiffin.secrets import Secrets
from tiffin.web import queries
from tiffin.web.app import create_app

from .fakes import FakeTelegram
from .web_source import app_source

HEADER = "Tailscale-User-Login"
OWNER_LOGIN = "alex@example.com"
PARTNER_LOGIN = "sam@example.com"

TODAY = dt.date(2026, 9, 13)  # a Sunday: body_check at 10:00, calibration at 17:30
STEPS_FLOOR = 9000
GOAL_KG = 95.0
GOAL_DATE = "2026-12-20"
#: the widest a phone gives the plotting area: 375 px less plotTheme's l:44 and r:12 margins
PHONE_PLOT_PX = 375 - 44 - 12


def at(day: dt.date, hh: int, mm: int) -> dt.datetime:
    return dt.datetime(day.year, day.month, day.day, hh, mm, tzinfo=TZ)


# -- the world ---------------------------------------------------------------------------------


def _profile(db: Db, person_id: int, *, goal: bool) -> None:
    db.insert(
        "profiles",
        person_id=person_id,
        effective_from="2025-07-01",
        sex="m" if goal else "f",
        birth_date="1988-03-11",
        height_cm=183.0 if goal else 166.0,
        activity_factor=1.5,
        kcal_floor=1600,
        steps_floor=STEPS_FLOOR,
        goal_weight_kg=GOAL_KG if goal else None,
        goal_date=GOAL_DATE if goal else None,
    )


def _weights(db: Db, person_id: int) -> list[tuple[dt.date, float]]:
    """Fourteen months of scales: a slow fall with the day-to-day wobble that hides it."""
    kept: list[tuple[dt.date, float]] = []
    for i in range(420):
        day = TODAY - dt.timedelta(days=419 - i)
        if i % 4 == 3:  # the mornings he did not stand on them
            continue
        kg = round(104.4 - 0.0135 * i + 0.62 * math.sin(i * 2.4) + 0.31 * math.sin(i * 0.7), 2)
        db.insert("weigh_ins", person_id=person_id, day=isoday(day), weight_kg=kg, source="manual")
        kept.append((day, kg))
    body_mod.rebuild_trend(db, person_id)
    return kept


def _targets_and_days(db: Db, person_id: int) -> dict[str, Any]:
    """Weekly targets, and 330 closed days - most confirmed, a holiday week never confirmed."""
    monday = week_start(TODAY - dt.timedelta(days=400))
    while monday <= TODAY:
        db.insert(
            "targets",
            person_id=person_id,
            week_start=isoday(monday),
            kcal=2400 if monday < TODAY - dt.timedelta(days=60) else 2350,
            protein_g=185,
            fat_g=70,
            carb_g=240,
            fibre_g=32,
            source="calibration",
        )
        monday += dt.timedelta(days=7)

    unconfirmed: list[str] = []
    for i in range(330):
        day = TODAY - dt.timedelta(days=330 - i)
        holiday = dt.date(2026, 7, 20) <= day <= dt.date(2026, 7, 26)
        if holiday:
            # away: the day was closed on the menu, never confirmed, and the calibration
            # is not allowed to trust it
            db.insert(
                "day_confirmations",
                person_id=person_id,
                day=isoday(day),
                status="unconfirmed",
                basis="menu",
                kcal=2400.0,
                protein_g=170.0,
                fat_g=80.0,
                carb_g=250.0,
                fibre_g=28.0,
            )
            unconfirmed.append(isoday(day))
            continue
        db.insert(
            "day_confirmations",
            person_id=person_id,
            day=isoday(day),
            status="confirmed",
            basis="menu",
            kcal=round(2280 + 220 * math.sin(i * 1.7), 1),
            protein_g=182.0,
            fat_g=68.0,
            carb_g=230.0,
            fibre_g=31.0,
        )
    return {"unconfirmed": unconfirmed}


def _steps(db: Db, person_id: int) -> dict[str, int]:
    """Four months of intervals.icu steps: some days well over the floor, plenty under."""
    over = 0
    total = 0
    for i in range(120):
        day = TODAY - dt.timedelta(days=120 - i)
        steps = int(8200 + 4200 * math.sin(i * 1.1) + 900 * math.sin(i * 0.31))
        db.insert(
            "icu_wellness",
            person_id=person_id,
            day=isoday(day),
            steps=steps,
            sleep_s=25200,
            icu_raw_id=0,
        )
        total += 1
        if steps >= STEPS_FLOOR:
            over += 1
    return {"days": total, "over": over}


def _tape(db: Db, person_id: int) -> dict[str, list[float]]:
    """Twelve Sundays of waist and hip - both measured in the same minute, from one message."""
    waist: list[float] = []
    hip: list[float] = []
    for w in range(12):
        day = TODAY - dt.timedelta(weeks=11 - w)
        cm_w = round(102.0 - 0.32 * w + 0.4 * math.sin(w * 2.0), 1)
        cm_h = round(108.0 - 0.14 * w, 1)
        body_mod.record_tape(db, person_id, day, "waist", cm_w)
        body_mod.record_tape(db, person_id, day, "hip", cm_h)
        waist.append(cm_w)
        hip.append(cm_h)
    return {"waist": waist, "hip": hip}


@dataclasses.dataclass
class Box:
    """The page at one moment, and the numbers the fixture put behind it."""

    client: TestClient
    db: Db
    config: Config
    clock: FrozenClock
    owner_id: int
    partner_id: int
    facts: dict[str, Any]

    def as_owner(self, path: str, **kw: Any):
        return self.client.get(path, headers={HEADER: OWNER_LOGIN}, **kw)

    def as_partner(self, path: str, **kw: Any):
        return self.client.get(path, headers={HEADER: PARTNER_LOGIN}, **kw)

    def trend(self, metric: str, days: int = 90, *, who: str = "owner") -> dict[str, Any]:
        get = self.as_owner if who == "owner" else self.as_partner
        response = get(f"api/trends?metric={metric}&days={days}")
        assert response.status_code == 200, f"{metric}/{days}: {response.status_code}"
        return dict(response.json())

    def app_js(self) -> str:
        """The script the shell actually loads, by the hashed name written into it."""
        return app_source()


def fn_body(js: str, name: str) -> str:
    """One top-level function out of app.js, by its own closing brace at column zero."""
    found = re.search(rf"function {name}\([\s\S]*?\n\}}", js)
    assert found is not None, f"app.js has no {name}()"
    return found.group(0)


@pytest.fixture
def open_box(config: Config, tmp_path: Path) -> Iterator[Callable[..., Box]]:
    opened: list[Db] = []

    def make(when: dt.datetime) -> Box:
        clock = FrozenClock(when)
        config.paths.ensure()
        db = Db.open(config.paths.db, clock=clock)
        opened.append(db)
        owner_id = db.insert("persons", slug="owner", display_name="Alex", tone="blunt")
        partner_id = db.insert("persons", slug="partner", display_name="Sam", tone="warm")
        db.insert("tailscale_identities", person_id=owner_id, login=OWNER_LOGIN)
        db.insert("tailscale_identities", person_id=partner_id, login=PARTNER_LOGIN)
        _profile(db, owner_id, goal=True)
        _profile(db, partner_id, goal=False)
        facts: dict[str, Any] = {"weights": _weights(db, owner_id)}
        facts.update(_targets_and_days(db, owner_id))
        facts["steps"] = _steps(db, owner_id)
        facts["tape"] = _tape(db, owner_id)

        secrets_dir = tmp_path / "s"
        secrets_dir.mkdir(exist_ok=True)
        (secrets_dir / "telegram-token-dev.txt").write_text("1:t", encoding="utf-8")
        config.data["web"]["secure_cookies"] = False
        config.data["web"]["root_path"] = ""
        app = App(config, Secrets(secrets_dir), clock=clock, transport=FakeTelegram())
        client = TestClient(create_app(app), base_url="http://testserver")
        return Box(client, db, config, clock, owner_id, partner_id, facts)

    yield make
    for db in opened:
        db.close()


def mean_step(series: list[float | None]) -> float:
    """Mean absolute day-to-day move: how much of the line is noise."""
    values = [v for v in series if v is not None]
    assert len(values) > 2
    return sum(abs(b - a) for a, b in itertools.pairwise(values)) / (len(values) - 1)


# -- the weight chart --------------------------------------------------------------------------


def test_01_whether_the_scales_are_noise_or_a_trend(open_box) -> None:
    """Sunday 08:40. The scales said 0.7 kg more than Thursday and he wants to know whether
    that is him or the salt in last night's dinner. This is the one question the Trends tab
    exists for: the raw points and the smoothed line on the same axes, so the wobble is
    visibly wobble."""
    box = open_box(at(TODAY, 8, 40))
    data = box.trend("weight", 90)
    assert data["dates"] and data["raw"], "ninety days of weigh-ins came back empty"
    assert data["ewma"] and data["ewma_dates"], "no trend line beside the raw points"
    assert mean_step(data["ewma"]) < mean_step(data["raw"]) / 2, (
        "the trend line moves as much as the scales do, so it smooths nothing"
    )
    js = box.app_js()
    render = fn_body(js, "renderTrends")
    assert 'mode: "markers"' in render and '"measured"' in render, "raw points are drawn as points"
    assert 'name: "trend"' in render and '"lines"' in render, "the trend is drawn as a line"
    # the window selector reaches back a year, and the API honours it
    year = box.trend("weight", 365)
    assert len(year["dates"]) > len(data["dates"]), "365 days returned no more than 90"
    assert min(year["dates"]) >= isoday(TODAY - dt.timedelta(days=365))


def test_02_what_the_numbers_up_the_side_of_the_chart_are(open_box) -> None:
    """Sunday 08:45, still on the weight chart, he screenshots it to send to a coach. The
    same chart draws 99.4 for weight, 101.8 for waist and 9,000 for steps and never once
    says kg, cm or steps: `plotTheme()` sets a grid colour on each axis and no title, and
    `renderTrends()` adds none per metric. A chart whose units are guessed from memory is
    a chart you cannot send to anybody."""
    box = open_box(at(TODAY, 8, 45))
    units = {"weight": "kg", "waist": "cm", "hip": "cm", "steps": "steps", "intake": "kcal"}
    payloads = {m: box.trend(m, 90) for m in units}
    carried = {m: d.get("unit") or d.get("units") for m, d in payloads.items()}
    js = box.app_js()
    drawn = fn_body(js, "plotTheme") + fn_body(js, "renderTrends")
    axis_titled = re.search(r"(x|y)axis[^\n]*title|title:\s*\{", drawn)
    assert any(carried.values()) or axis_titled, (
        "no axis on any metric is labelled and the API names no unit either "
        f"(it returns {sorted(payloads['weight'])}), so weight in kg and waist in cm are the "
        "same nameless numbers up the side of the same chart"
    )


def test_03_which_of_those_bars_owner_actually_confirmed(open_box) -> None:
    """Sunday 17:25, five minutes before the weekly calibration. Intake vs target over 90
    days, and he wants to see which days it is allowed to believe. Seven of them - the week
    he was away - were closed on the menu and never confirmed. The API hands the page a
    `status` for every day; the bar chart draws all of them the same colour."""
    box = open_box(at(TODAY, 17, 25))
    data = box.trend("intake", 90)
    assert data["status"], "the API stopped carrying a status per day"
    pairs = zip(data["dates"], data["status"], strict=True)
    unconfirmed = [d for d, s in pairs if s != "confirmed"]
    assert unconfirmed, "the fixture's holiday week is not in the window"
    render = fn_body(box.app_js(), "renderTrends")
    assert "status" in render, (
        f"{len(unconfirmed)} of {len(data['dates'])} days in this chart were never confirmed "
        f"({unconfirmed[0]} to {unconfirmed[-1]}) and renderTrends() never reads data.status, "
        "so a day the calibration refuses to count looks exactly like a day he logged"
    )


def test_04_how_many_days_owner_actually_cleared_their_step_floor(open_box) -> None:
    """Wednesday 21:00 - "get a view on steps properly". His floor is 9,000. Standing in
    front of a month of dots and one flat line, the question is not what Tuesday was; it is how
    often he clears it and where the average sits. Nothing in the payload or on the page
    counts either, and steps ride the weight branch of renderTrends(), so a daily count
    arrives as translucent markers rather than bars."""
    box = open_box(at(dt.date(2026, 9, 9), 21, 0))
    data = box.trend("steps", 30)
    assert data["raw"], "thirty days of steps came back empty"
    assert set(data["target"]) == {STEPS_FLOOR}, "the floor is the comparison line"
    over = sum(1 for v in data["raw"] if v is not None and v >= STEPS_FLOOR)
    aggregate = {k for k in ("over_target", "days_over", "mean", "average", "summary") if k in data}
    counted = len(data["raw"])
    mean = round(sum(v for v in data["raw"] if v is not None) / counted)
    assert aggregate, (
        f"the payload is {counted} raw counts and the floor repeated {len(data['target'])} times, "
        f"and nothing anywhere says he cleared it on {over} of {counted} days or averaged "
        f"{mean} steps"
    )


def test_05_whether_partner_waist_is_moving_against_partner_hip(open_box) -> None:
    """Sunday 10:20. The tape question came at 10:00 and she answered it with both numbers in
    one message - `/measure 86 104`. Waist and hip are the same measurement to the person who
    took them, and the thing worth watching is one against the other. The tab offers Waist or
    Hip, one at a time: seeing both means changing the select and remembering the first."""
    box = open_box(at(TODAY, 10, 20))
    waist = box.trend("waist", 180)
    hip = box.trend("hip", 180)
    assert waist["dates"] == hip["dates"] and waist["dates"], "both are measured on the same day"
    together = []
    for metric in queries.TREND_METRICS:
        payload = box.trend(metric, 180)
        series = [v for k, v in payload.items() if isinstance(v, list) and k != "dates"]
        if any(s == waist["raw"] for s in series) and any(s == hip["raw"] for s in series):
            together.append(metric)
    assert together, (
        f"no metric in {list(queries.TREND_METRICS)} returns waist and hip together, so the "
        "ratio the two numbers exist to make is on no chart, and comparing them is two taps "
        "and a memory of the chart you just left"
    )


# -- nothing there yet -------------------------------------------------------------------------


def test_06_a_metric_with_nothing_behind_it_says_what_to_do_next(open_box) -> None:
    """Sunday 11:30, the partner's first weekend on the page. She taps Trends and gets Weight, then
    Steps, then Waist, and each says the same eight words and stops. The Menu tab in the same
    file names `/cook`; the house rule is degrade to manual, never to silence, and the audits
    already catch "a short refusal with no next step" in the bot."""
    box = open_box(at(TODAY, 11, 30))
    for metric in ("weight", "steps", "waist"):
        empty = box.trend(metric, 90, who="partner")
        assert not [v for v in empty["raw"] if v is not None], f"{metric} is not empty for her"
    js = box.app_js()
    render = fn_body(js, "renderTrends")
    said = re.search(r'text:\s*"([^"]*[Nn]othing recorded[^"]*)"', render)
    assert said is not None, "renderTrends() has no empty state at all"
    sentence = said.group(1)
    routes = ("/measure", "/today", "Telegram", "bot", "intervals")
    assert any(step in sentence for step in routes), (
        f"the page says only {sentence!r} - no route to recording one, while the same file's "
        "menu empty state names /cook and the bot in Telegram"
    )


def test_07_the_chart_follows_the_phone_into_dark_mode(open_box) -> None:
    """Sunday 21:50, the phone turns the page dark under him while the Trends tab is open.
    A chart painted with light-mode ink on a dark card is unreadable, and re-reading it must
    not mean a reload."""
    box = open_box(at(TODAY, 21, 50))
    js = box.app_js()
    theme = fn_body(js, "plotTheme")
    assert "prefers-color-scheme: dark" in theme, "the chart never asks what scheme the phone is in"
    assert theme.count("dark ?") >= 2, "only one colour changes between the schemes"
    assert 'paper_bgcolor: "rgba(0,0,0,0)"' in theme and 'plot_bgcolor: "rgba(0,0,0,0)"' in theme, (
        "the plot paints its own background, so it will not match the card under it"
    )
    listener = re.search(
        r'matchMedia\("\(prefers-color-scheme: dark\)"\)\.addEventListener\(\s*"change",'
        r"([\s\S]*?)\}\);",
        js,
    )
    assert listener is not None, "nothing listens for the scheme changing"
    assert "renderTrends" in listener.group(1), "the open chart is not repainted when it changes"
    css = box.as_owner(
        re.search(r"static/app\.[0-9a-f]{10}\.css", box.as_owner("").text).group(0)  # type: ignore[union-attr]
    ).text
    assert "@media (prefers-color-scheme: dark)" in css and "--ink" in css


# -- getting to the chart ----------------------------------------------------------------------


def test_08_a_link_that_opens_the_chart_owner_was_told_about(open_box) -> None:
    """Thursday 07:05. The morning message says yesterday came to 4,900 steps, under his
    floor, and he taps the page link to see whether that is a week or a bad day. A link can
    name a tab - and a day and a meal on the Menu tab - but not a metric or a window, so he
    lands on Weight over 90 days and pays two more taps, every morning, for the same chart."""
    box = open_box(at(dt.date(2026, 9, 10), 7, 5))
    js = box.app_js()
    assert 'get("tab")' in js and 'get("day")' in js, "the page does read its own address"
    reads_metric = re.search(r'get\("(metric|days|window)"\)', js)
    remembers = re.search(r'localStorage\.setItem\("tiffin\.(metric|days)"', js)
    assert reads_metric or remembers, (
        "app.js reads tab, day and slot out of the URL and stores only tiffin.tab in "
        "localStorage, so ?tab=trends&metric=steps opens the weight chart and the bot cannot "
        "link to the one chart its own message is about"
    )


# -- what the numbers add up to ----------------------------------------------------------------


def test_09_whether_a_long_run_of_eating_has_been_under_target(open_box) -> None:
    """Sunday 17:30, on the sofa, phone in hand, the weekly calibration has just moved his
    TDEE again. He switches Intake to 365 days to ask one thing: over the long run, have I
    actually been under. The tab answers with a bar per day and a dotted line, and averaging
    three hundred bars against a moving line by eye is exactly the arithmetic a person cannot
    do - on a 375 px phone each bar is a pixel wide."""
    box = open_box(at(TODAY, 17, 30))
    data = box.trend("intake", 365)
    eaten = [v for v in data["raw"] if v is not None]
    target = [v for v in data["target"] if v is not None]
    assert eaten and target, "a year of intake came back empty"
    bars = len(data["dates"])
    assert bars > 60, "the fixture's year of days is not in the window"
    per_bar = PHONE_PLOT_PX / bars
    summary = {k for k in ("mean", "average", "deficit", "total", "summary", "weeks") if k in data}
    render = fn_body(box.app_js(), "renderTrends")
    rolls_up = re.search(r"reduce\(|/\s*\w+\.length", render)
    assert summary or rolls_up, (
        f"{bars} daily bars at {per_bar:.1f} px each and no rollup: nothing states the mean "
        f"intake ({round(sum(eaten) / len(eaten))} kcal), the mean target "
        f"({round(sum(target) / len(target))} kcal) or the gap between them, which is the only "
        "thing a year of eating is opened to find out"
    )


def test_10_when_the_trend_actually_reaches_the_goal(open_box) -> None:
    """Sunday 08:50, back on the weight chart with the dashed goal line across it. He set
    95 kg by 20 December and the box knows both halves - `profiles.goal_weight_kg` and
    `profiles.goal_date`, and /api/today hands the page the pair. The trend payload carries
    only the weight, so the goal is drawn as a flat line from the first weigh-in to the last,
    entirely in the past, and the chart cannot say whether he is ahead of it or behind."""
    box = open_box(at(TODAY, 8, 50))
    profile = box.as_owner("api/today").json()["profile"]
    assert profile["goal_weight_kg"] == GOAL_KG and profile["goal_date"] == GOAL_DATE
    data = box.trend("weight", 180)
    assert data["goal"] == GOAL_KG, "the goal weight does reach the chart"
    render = fn_body(box.app_js(), "renderTrends")
    assert 'name: "goal"' in render, "the goal is drawn"
    goal_line = re.search(r"y:\s*\[data\.goal, data\.goal\]", render)
    assert goal_line is not None, "the goal is no longer a flat line; re-read this story"
    assert "goal_date" in data or "eta" in data or "projection" in data, (
        f"the goal line runs from {data['dates'][0]} to {data['dates'][-1]} - both behind him - "
        f"at {GOAL_KG} kg, and neither the payload ({sorted(data)}) nor the page carries the "
        f"{GOAL_DATE} he set it for or where the trend meets it, so the one line about the "
        "future on this chart is drawn entirely in the past"
    )
