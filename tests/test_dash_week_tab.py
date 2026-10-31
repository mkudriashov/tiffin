"""Tests for the weekly view within Progress."""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
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

WEEK_A = dt.date(2026, 9, 7)  # Monday, the week already behind him
WEEK_B = dt.date(2026, 9, 14)  # Monday, the week being lived
STEPS_FLOOR = 9000


def at(day: dt.date, hh: int, mm: int) -> dt.datetime:
    return dt.datetime(day.year, day.month, day.day, hh, mm, tzinfo=TZ)


def days_of(monday: dt.date) -> list[dt.date]:
    return [monday + dt.timedelta(days=i) for i in range(7)]


@dataclasses.dataclass
class Box:
    """The page at one moment, with the database behind it."""

    client: TestClient
    db: Db
    clock: FrozenClock
    owner_id: int
    partner_id: int

    # -- what the person does ------------------------------------------------------------
    def as_owner(self, path: str, **kw: Any):
        return self.client.get(path, headers={HEADER: OWNER_LOGIN}, **kw)

    def as_partner(self, path: str, **kw: Any):
        return self.client.get(path, headers={HEADER: PARTNER_LOGIN}, **kw)

    def week(self, **params: str) -> dict[str, Any]:
        """Exactly the GET `renderWeek()` makes, plus anything the address bar could add."""
        query = "&".join(f"{k}={v}" for k, v in params.items())
        response = self.as_owner("api/week" + (f"?{query}" if query else ""))
        assert response.status_code == 200, response.text
        return response.json()

    def app_js(self) -> str:
        """The script the shell actually loads, by the hashed name written into it."""
        return app_source()

    def shell(self) -> str:
        return self.as_owner("").text

    def progress(self, **params: str) -> dict[str, Any]:
        """Fetch the Progress page for the fixture week."""
        query = "&".join(f"{k}={v}" for k, v in params.items())
        response = self.as_owner("api/progress" + (f"?{query}" if query else ""))
        assert response.status_code == 200, response.text
        return response.json()

    def progress_js(self) -> str:
        """Read the Progress module reached from the page entry point."""
        assert 'src="static/app.js"' in self.shell()
        assert 'from "./progress.js"' in self.as_owner("static/app.js").text
        response = self.as_owner("static/progress.js")
        assert response.status_code == 200
        return response.text

    def progress_section(self) -> str:
        shell = self.shell()
        return shell[shell.index('id="progress"') : shell.index('id="more"')]

    # -- what the box records ------------------------------------------------------------
    def confirm(
        self,
        person_id: int,
        day: dt.date,
        *,
        status: str = "confirmed",
        kcal: float | None = 2150.0,
        protein: float | None = 190.0,
        basis: str = "menu",
    ) -> None:
        self.db.insert(
            "day_confirmations",
            person_id=person_id,
            day=day.isoformat(),
            status=status,
            basis=basis,
            kcal=kcal,
            protein_g=protein,
            fat_g=68.0,
            carb_g=190.0,
            fibre_g=28.0,
        )

    def steps(self, person_id: int, day: dt.date, count: int) -> None:
        self.db.insert(
            "icu_wellness", person_id=person_id, day=day.isoformat(), steps=count, icu_raw_id=0
        )

    def trained(self, person_id: int, day: dt.date, *, status: str, minutes: int = 75) -> None:
        icu_id = f"i{person_id}{day.isoformat()}"
        self.db.insert(
            "icu_activity",
            person_id=person_id,
            icu_id=icu_id,
            day=day.isoformat(),
            kind="climb",
            name="Evening climb",
            moving_s=minutes * 60,
            elapsed_s=minutes * 60,
            icu_raw_id=0,
        )
        self.db.insert(
            "session_matches",
            person_id=person_id,
            day=day.isoformat(),
            icu_id=icu_id,
            status=status,
            minutes=minutes,
        )

    def paused(self, person_id: int, first: dt.date, last: dt.date) -> None:
        self.db.insert(
            "pause_periods",
            person_id=person_id,
            start_day=first.isoformat(),
            end_day=last.isoformat(),
            reason="away",
        )

    def summary_sent(self, person_id: int, sunday: dt.date, html: str) -> None:
        self.db.insert(
            "messages_out",
            person_id=person_id,
            chat_id=1,
            kind="sunday_summary",
            due_key=sunday.isoformat(),
            text_html=html,
            status="sent",
            sent_at=f"{sunday.isoformat()} 18:00:00",
        )


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
        for pid, floor, kcal_floor in ((owner_id, STEPS_FLOOR, 1800), (partner_id, 8000, 1500)):
            db.insert(
                "profiles",
                person_id=pid,
                effective_from="2026-09-01",
                sex="m" if pid == owner_id else "f",
                birth_date="1990-03-15",
                height_cm=183.0 if pid == owner_id else 168.0,
                activity_factor=1.55,
                steps_floor=floor,
                kcal_floor=kcal_floor,
                goal_weight_kg=87.0 if pid == owner_id else 60.0,
            )
            for monday, kcal in ((WEEK_A, 2250), (WEEK_B, 2250)):
                db.insert(
                    "targets",
                    person_id=pid,
                    week_start=monday.isoformat(),
                    kcal=kcal if pid == owner_id else 1600,
                    protein_g=200 if pid == owner_id else 132,
                    fat_g=70 if pid == owner_id else 47,
                    carb_g=200 if pid == owner_id else 140,
                    fibre_g=30 if pid == owner_id else 25,
                    source="formula",
                )
        # three real sessions a week, so a week can be judged against a plan
        plan = db.insert("training_plans", person_id=owner_id, effective_from="2026-09-01")
        for weekday in (0, 2, 4):
            db.insert(
                "planned_sessions",
                plan_id=plan,
                person_id=owner_id,
                weekday=weekday,
                kind="climb",
                target_minutes=90,
            )
        secrets_dir = tmp_path / "s"
        secrets_dir.mkdir(exist_ok=True)
        (secrets_dir / "telegram-token-dev.txt").write_text("1:t", encoding="utf-8")
        config.data["web"]["secure_cookies"] = False
        config.data["web"]["root_path"] = ""
        app = App(config, Secrets(secrets_dir), clock=clock, transport=FakeTelegram())
        client = TestClient(create_app(app), base_url="http://testserver")
        return Box(client, db, clock, owner_id, partner_id)

    yield make
    for db in opened:
        db.close()


def full_week(box: Box, monday: dt.date, *, kcal: float = 2150.0) -> None:
    """Seven closed days, the way `day_close` leaves a week that has been lived through."""
    for day in days_of(monday):
        box.confirm(box.owner_id, day, kcal=kcal)
        box.steps(box.owner_id, day, 11000)


# -- the week that just ended ------------------------------------------------------------------


def test_01_monday_morning_owner_looks_back_at_the_week_that_just_ended(open_box) -> None:
    """Monday 21 Sep, 08:10, coffee in hand. Last week is over and he wants to see it whole -
    seven days, what he averaged, whether the training happened - before this week buries it.
    The Week tab is the only place the page shows a week."""
    box = open_box(at(dt.date(2026, 9, 21), 8, 10))
    full_week(box, WEEK_B, kcal=2100.0)
    box.confirm(box.owner_id, dt.date(2026, 9, 21), kcal=800.0)

    # the API can answer it: the week is addressable
    last = box.week(start=WEEK_B.isoformat())
    assert last["week_start"] == WEEK_B.isoformat(), "the API can be asked for a named week"
    assert len(last["days"]) == 7

    # ...and the page asks for it: Progress opens on this week, with an arrow back to the last
    opened = box.progress()
    assert opened["week"]["start"] == "2026-09-21", "Progress lands on the week of today"
    assert opened["week"]["prev"] == WEEK_B.isoformat() and opened["week"]["next"] is None
    shown = box.progress(week=WEEK_B.isoformat())
    assert shown["week"]["start"] == WEEK_B.isoformat() and len(shown["days"]) == 7
    assert shown["cards"]["recorded"]["days"] == 7, "the whole week, recorded"
    source = box.progress_js()
    assert "progressWeek = data.week.prev" in source, (
        "Progress fetches only the week of today and offers nothing to step back with, so the "
        "week that just ended - the one he is asking about - cannot be reached from the page"
    )


# -- seven days, gaps included -----------------------------------------------------------------


def test_02_thursday_owner_asks_which_days_of_this_week_are_still_open(open_box) -> None:
    """Thursday 17 Sep, 12:05. The box was down on Wednesday night, so day_close never wrote
    Wednesday. He opens Week to see where the week stands and has to be able to tell a day he
    has not confirmed from a day the system never recorded."""
    box = open_box(at(dt.date(2026, 9, 17), 12, 5))
    box.confirm(box.owner_id, WEEK_B, kcal=2200.0)
    box.confirm(box.owner_id, WEEK_B + dt.timedelta(days=1), status="unconfirmed", basis="none")
    # Wednesday: nothing at all
    box.confirm(box.owner_id, WEEK_B + dt.timedelta(days=3), status="unconfirmed", basis="none")

    data = box.week()
    named = {d["day"] for d in data["days"]}
    elapsed = {d.isoformat() for d in days_of(WEEK_B)[:4]}
    assert named >= elapsed, (
        "Wednesday is missing from the week entirely - not a row saying 'no record', just "
        f"absent - so the table reads Mon, Tue, Thu and he cannot see the gap. Days: {named}"
    )


def test_03_sunday_evening_owner_wants_the_weeks_average_against_the_target(open_box) -> None:
    """Sunday 20 Sep, 19:40, after the summary lands. The one number he judges a week by is
    what he averaged against the target he was set. The Week tab is where a week's numbers
    live."""
    box = open_box(at(dt.date(2026, 9, 20), 19, 40))
    for day, kcal in zip(days_of(WEEK_B), [2100, 2300, 2050, 2400, 2200, 2600, 1900], strict=True):
        box.confirm(box.owner_id, day, kcal=float(kcal))
        box.steps(box.owner_id, day, 10500)

    recorded = box.progress()["cards"]["recorded"]
    assert recorded["days"] == 7, "the week has seven confirmed days behind it"
    assert recorded["avg_kcal"] == 2221, (
        "nothing on Progress averages the week's record: the one number he judges a week by "
        f"(2221 kcal) has to be worked out on paper from seven rows. Recorded: {recorded}"
    )
    assert "recorded.avg_kcal" in box.progress_js(), "and the page says it"


# -- a week he paused --------------------------------------------------------------------------


def test_04_the_week_owner_was_away_reads_as_paused_not_as_four_days_missed(open_box) -> None:
    """Sunday 20 Sep, 20:00. He was away Tue-Fri and told the bot `/pause 4d`, so those days
    were closed as paused, not missed. The Week tab must not read that back to him as a week
    he failed."""
    box = open_box(at(dt.date(2026, 9, 20), 20, 0))
    box.paused(box.owner_id, WEEK_B + dt.timedelta(days=1), WEEK_B + dt.timedelta(days=4))
    for i, day in enumerate(days_of(WEEK_B)):
        paused = 1 <= i <= 4
        box.confirm(
            box.owner_id,
            day,
            status="paused" if paused else "confirmed",
            kcal=None if paused else 2150.0,
            protein=None if paused else 190.0,
            basis="none" if paused else "menu",
        )

    data = box.week()
    assert data["summary"]["paused_days"] == 4, "the API counts the paused days"
    assert data["summary"]["confirmed_days"] == 3
    days = box.progress()["days"]
    assert [d["recorded"] for d in days] == ["recorded"] + ["paused"] * 4 + ["recorded"] * 2, (
        "Progress does not say which days were paused, so a week he was allowed to be away for "
        "reads as four days missed"
    )
    assert "d.recorded" in box.progress_js(), "and the day table prints each day's own state"


# -- training ----------------------------------------------------------------------------------


def test_05_the_week_counts_the_three_sessions_owner_actually_did(open_box) -> None:
    """Sunday 20 Sep, 19:45. He trained three times. intervals.icu could not line any of them
    up with the plan rows - he climbed on the wrong days - so they came in as 'extra', which is
    what most real training arrives as. Today's tab says he trained; the Week card has to
    agree."""
    box = open_box(at(dt.date(2026, 9, 20), 19, 45))
    full_week(box, WEEK_B)
    for offset in (1, 3, 5):  # Tue, Thu, Sat - real sessions, none of them matched
        box.trained(box.owner_id, WEEK_B + dt.timedelta(days=offset), status="extra")

    saturday = (WEEK_B + dt.timedelta(days=5)).isoformat()
    today = box.as_owner(f"api/today?date={saturday}").json()
    assert [s["status"] for s in today["sessions"]] == ["extra"], "Today shows the session"

    data = box.week()
    assert data["summary"]["sessions_done"] == 3, (
        "the Week card reads 'Sessions "
        f"{data['summary']['sessions_done']}/{data['summary']['sessions_planned']}' after a week "
        "of three real sessions, because v_week_summary counts only matched/confirmed/manual "
        "and intervals.icu writes 'extra' for anything it cannot pair with a plan row - the "
        "same defect the Today tab was fixed for"
    )


# -- steps -------------------------------------------------------------------------------------


def test_06_owner_asks_which_days_this_week_owner_walked_enough(open_box) -> None:
    """Friday 18 Sep, 21:10. His floor is 9,000 steps and he wants to know which days of this
    week cleared it - a per-day question about this week, which is the Week tab's job; Trends
    answers a different one (ninety days of dots, no day named)."""
    box = open_box(at(dt.date(2026, 9, 18), 21, 10))
    counts = [12400, 7300, 9800, 6100, 10200]
    for day, count in zip(days_of(WEEK_B)[:5], counts, strict=True):
        box.confirm(box.owner_id, day, kcal=2150.0)
        box.steps(box.owner_id, day, count)

    # the numbers are on the box: Trends reads them, with the floor
    trend = box.as_owner("api/trends?metric=steps&days=30").json()
    assert trend["raw"] == [float(c) for c in counts]
    assert trend["target"][0] == STEPS_FLOOR

    data = box.week()
    payload = json.dumps(data)
    assert "steps" in payload, (
        "the Week tab's seven rows carry day, status and kcal only - no steps and no floor - "
        "so the one place that lists this week day by day cannot answer 'which days did I "
        f"walk enough'. Row keys: {sorted(data['days'][0])}"
    )


# -- the Sunday summary ------------------------------------------------------------------------


def test_07_from_the_week_owner_taps_through_to_that_weeks_sunday_write_up(open_box) -> None:
    """Sunday 20 Sep, 19:35. The summary message has just arrived in Telegram and he wants to
    read it properly on the page. He is standing on Week looking at the seven days it is about;
    the write-up for that week should be one tap away, not a hunt."""
    box = open_box(at(dt.date(2026, 9, 20), 19, 35))
    full_week(box, WEEK_B)
    box.summary_sent(box.owner_id, dt.date(2026, 9, 20), "<b>Week</b> seven confirmed days.")

    written = box.as_owner("api/summary/weekly").json()
    assert written["text"]["current"] is True, "the summary for this week is on the box"

    section = box.progress_section()
    assert 'id="progress-body"' in section and 'id="progress-sunday"' in section, (
        "the week he is looking at and the prose written about that exact week are on two "
        f"unconnected screens. Section markup: {section.strip()[:400]!r}"
    )
    assert section.index('id="progress-body"') < section.index('id="summary-body"')


# -- the guards --------------------------------------------------------------------------------


def test_08_every_day_of_the_week_says_where_it_stands(open_box) -> None:
    """Wednesday 16 Sep, 07:20, before the morning message. He opens Week to see whether
    yesterday closed unconfirmed. Each day that has been recorded has to carry its own status,
    and the table has to print it."""
    box = open_box(at(dt.date(2026, 9, 16), 7, 20))
    box.confirm(box.owner_id, WEEK_B, kcal=2150.0)
    box.confirm(box.owner_id, WEEK_B + dt.timedelta(days=1), status="unconfirmed", basis="none")

    data = box.week()
    by_day = {d["day"]: d for d in data["days"]}
    assert by_day["2026-09-14"]["status"] == "confirmed"
    assert by_day["2026-09-15"]["status"] == "unconfirmed"
    assert by_day["2026-09-14"]["kcal"] == 2150.0
    assert "week_start" in data and data["week_end"] == "2026-09-20"
    shown = {d["day"]: d for d in box.progress()["days"]}
    assert shown["2026-09-14"]["recorded"] == "recorded"
    assert shown["2026-09-15"]["recorded"] == "not answered", "unanswered, never 'as planned'"
    assert shown["2026-09-15"]["recorded_kcal"] is None, "the plan's number is not a record"
    assert "d.recorded" in box.progress_js(), "the table prints each day's own state"


def test_09_a_date_typed_into_the_address_bar_lands_on_that_whole_week(open_box) -> None:
    """Tuesday 15 Sep, 22:40, in bed, he edits the URL to a Thursday out of curiosity. A day
    in the middle of a week has to bring back that whole Monday-to-Sunday week, his own, and a
    date that is not a date has to be refused rather than break the page."""
    box = open_box(at(dt.date(2026, 9, 15), 22, 40))
    full_week(box, WEEK_B)
    box.confirm(box.partner_id, WEEK_B, kcal=1550.0, protein=130.0)

    mid = box.week(start="2026-09-17")
    assert mid["week_start"] == "2026-09-14" and mid["week_end"] == "2026-09-20"
    assert len(mid["days"]) == 7
    assert box.as_owner("api/week?start=notaday").status_code == 400

    partners = box.as_partner("api/week").json()
    assert [d["kcal"] for d in partners["days"]] == [1550.0], "her week is hers"
    assert partners["summary"]["confirmed_days"] == 1
    assert 2150.0 not in [d["kcal"] for d in partners["days"]], "his days are not on her page"


def test_10_a_week_with_nothing_in_it_says_so_instead_of_an_empty_table(open_box) -> None:
    """Monday 14 Sep, 09:00, his first Monday on the page: nothing has been closed yet this
    week. An empty table under no heading reads as broken, so the tab has to say why it is
    empty and what makes a row appear."""
    box = open_box(at(WEEK_B, 9, 0))

    data = box.week()
    assert data["summary"] is None and data["days"] == []
    # Progress never draws an empty table: every day of the week has its row, the plan in it
    # where there is one, and the record count is said in words
    shown = box.progress()
    assert len(shown["days"]) == 7 and shown["cards"]["recorded"] == {
        "days": 0,
        "of": 1,
        "avg_kcal": None,
        "target_kcal": shown["cards"]["recorded"]["target_kcal"],
    }
    source = box.progress_js()
    assert "Intake recorded on" in source and "so far this week" in source
    assert "Nothing to show yet" in source, "and a person with nothing at all is told so"


def test_11_the_week_reads_as_a_week_and_not_as_a_fifth_broken_card(open_box) -> None:
    """Sunday 20 Sep, 21:00, on his phone. He opens Week to read the week just gone.

    Two things had gone wrong at the top of this tab, and neither was visible from the JSON.
    The three navigation buttons were appended into `#week-summary`, which is the CARDS grid
    (`repeat(auto-fit, minmax(150px, 1fr))`), so all three became one grid item and stacked down
    a 150 px column beside the numbers, reading as a fifth, broken card. And the seven-day table
    printed four columns with no header row at all, so "1915" and "5885" sat side by side with
    nothing saying which was the kcal and which the steps.
    """
    box = open_box(at(dt.date(2026, 9, 20), 21, 0))
    full_week(box, WEEK_B)

    # Progress builds the week's navigation as a row of its own, above the cards it navigates,
    # and the day table with a header row naming every column
    source = box.progress_js()
    assert source.index('class: "progress-week-nav"') < source.index(
        'class: "cards progress-cards"'
    ), "the navigation renders below the numbers it navigates"
    assert 'el("thead"' in source and 'scope: "col"' in source, "columns of numbers and no header"
    for column in ("Day", "Plan", "Record", "Steps", "Train", "kg"):
        assert f'"{column}"' in source, f"the header row does not name {column}"
