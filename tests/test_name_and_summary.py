"""Tests for display names and weekly summary wording."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from tiffin.app import App
from tiffin.clock import FrozenClock
from tiffin.db import Db
from tiffin.persons import PersonRegistry
from tiffin.secrets import Secrets
from tiffin.telegram.poller import Poller
from tiffin.web import queries

from .fakes import FakeTelegram

MONDAY = dt.date(2026, 9, 7)


# ---------------------------------------------------------------- the name
def test_a_name_can_be_set_after_the_row_exists(fixture_db: Db, two_persons, config) -> None:
    reg = PersonRegistry(fixture_db, config)
    with pytest.raises(Exception, match="append-only"):
        fixture_db.execute("UPDATE persons SET display_name = 'Sam' WHERE slug = 'partner'")
    assert reg.set_display_name("partner", "  Sam  ") == "Sam", "whitespace is tidied"
    named = reg.by_slug("partner")
    assert named is not None and named.display_name == "Sam"


def test_a_correction_is_another_row_and_the_newest_wins(
    fixture_db: Db, two_persons, config
) -> None:
    reg = PersonRegistry(fixture_db, config)
    reg.set_display_name("partner", "Ana")
    reg.set_display_name("partner", "Sam")
    assert fixture_db.scalar("SELECT COUNT(*) FROM person_names") == 2
    person = reg.by_slug("partner")
    assert person is not None and person.display_name == "Sam"


@pytest.mark.parametrize("bad", ["", "   ", "<b>x</b>"])
def test_a_name_that_would_break_a_message_is_refused(
    fixture_db: Db, two_persons, config, bad
) -> None:
    reg = PersonRegistry(fixture_db, config)
    with pytest.raises(ValueError):
        reg.set_display_name("partner", bad)


def test_the_name_command_sets_it_and_reports_it(
    fixture_db: Db, two_persons, frozen_clock: FrozenClock, config, tmp_path: Path
) -> None:
    secrets_dir = tmp_path / "s"
    secrets_dir.mkdir()
    (secrets_dir / "telegram-token-dev.txt").write_text("1:t", encoding="utf-8")
    tg = FakeTelegram()
    app = App(config, Secrets(secrets_dir), clock=frozen_clock, transport=tg)
    poller = Poller(
        app.api(),
        fixture_db,
        app.registry(fixture_db),
        app.handlers(fixture_db).handle,
        clock=frozen_clock,
        sleeper=lambda s: None,
    )
    n = {"i": 0}

    def say(text: str) -> str:
        n["i"] += 1
        tg.queue_update(tg.text_update(n["i"], 1000001, text))
        poller.run_once()
        return tg.sent[-1]["text"]

    assert "Alex" in say("/name")
    assert "Maxim" in say("/name Maxim")
    assert "Maxim" in say("/name"), "and it sticks"
    assert "cannot contain" in say("/name <b>")


# ---------------------------------------------------------------- the summary tab
def _summary(db: Db, person_id: int, sunday: dt.date, text: str) -> None:
    db.insert(
        "messages_out",
        person_id=person_id,
        kind="sunday_summary",
        chat_id=1000001,
        due_key=sunday.isoformat(),
        text_html=text,
        status="sent",
        sent_at=f"{sunday.isoformat()} 17:30:00",
    )


def test_the_tab_shows_the_last_summary_rather_than_nothing(fixture_db: Db, two_persons) -> None:
    pid = two_persons["owner"]
    _summary(fixture_db, pid, dt.date(2026, 9, 6), "<b>Last week</b>")
    # asking on the Monday after: this week's is not written until Sunday evening
    out = queries.weekly_summary_text(fixture_db, pid, MONDAY)
    assert out["text"] is not None
    assert out["text"]["text_html"] == "<b>Last week</b>"
    assert out["text"]["current"] is False, "and the page can say it is last week's"


def test_this_weeks_summary_is_marked_current(fixture_db: Db, two_persons) -> None:
    pid = two_persons["owner"]
    _summary(fixture_db, pid, dt.date(2026, 9, 6), "<b>Older</b>")
    _summary(fixture_db, pid, dt.date(2026, 9, 13), "<b>This week</b>")
    out = queries.weekly_summary_text(fixture_db, pid, MONDAY)
    assert out["text"] is not None
    assert out["text"]["text_html"] == "<b>This week</b>" and out["text"]["current"] is True


def test_a_summary_from_the_future_is_not_shown(fixture_db: Db, two_persons) -> None:
    pid = two_persons["owner"]
    _summary(fixture_db, pid, dt.date(2026, 9, 20), "<b>Next week</b>")
    assert queries.weekly_summary_text(fixture_db, pid, MONDAY)["text"] is None


def test_it_is_still_only_ever_this_persons_summary(fixture_db: Db, two_persons) -> None:
    _summary(fixture_db, two_persons["owner"], dt.date(2026, 9, 6), "<b>His</b>")
    out = queries.weekly_summary_text(fixture_db, two_persons["partner"], MONDAY)
    assert out["text"] is None, "never one composed across people"
