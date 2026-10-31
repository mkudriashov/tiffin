"""Tests for quiet notifications and page-led summaries."""

from __future__ import annotations

import datetime as dt
import json
import re

from tiffin.jobs import all_jobs, evening_confirm, morning_targets, sunday_summary
from tiffin.telegram import handlers as handlers_mod

from .test_journey_owner_week import (  # noqa: F401 (week is a pytest fixture)
    FRI18,
    MON14,
    OATS,
    SUN20,
    TUE15,
    taps,
    text_of,
    week,
)
from .web_source import app_source

#: One prompt a person per day at most; the evening check-in answers the open week.
ROUTINE_MESSAGE_JOBS = {"morning_targets", "evening_confirm"}


def test_only_the_morning_and_the_evening_are_daily_messages() -> None:
    daily = {j.name for j in all_jobs() if j.kind == "message" and j.days is None}
    checks = {
        "menu_propose",
        "shopping_list",
        "menu_choices_close",
        "weekly_check",
        "profile_followup",
    }
    assert daily - checks == ROUTINE_MESSAGE_JOBS, daily


def test_the_morning_is_two_lines_and_a_link_for_a_linked_person(week) -> None:  # noqa: F811
    msg = week.run(morning_targets.JOB, MON14)
    text = text_of(msg)
    lines = [ln for ln in text.splitlines() if ln.strip()]
    assert len(lines) == 2, text
    assert re.match(r"<b>Mon 14 Sep</b> · [\d,]+ kcal · protein at least \d+ g$", lines[0])
    assert f"First up: {OATS}." in lines[1] and 'href="' in lines[1] and "?tab=today" in lines[1]
    assert "&date=" not in lines[1], "a link with today's date would pin the page to it"
    assert not taps(msg), "the page carries the meals; no keyboard for someone it lets in"


def test_the_morning_keeps_meal_buttons_for_someone_the_page_cannot_let_in(
    week,  # noqa: F811
    monkeypatch,
) -> None:
    from tiffin.telegram import render

    # no link can be built (no page origin), so this person has no page to be sent to
    monkeypatch.setattr(render, "page_today_link", lambda *a, **k: "")
    msg = week.run(morning_targets.JOB, MON14)
    text = text_of(msg)
    assert "href" not in text and "Tap a meal for its recipe." in text, text
    assert any(d.startswith("k:meal:") for d in taps(msg)), "buttons are the only way in"


def test_the_evening_names_planned_and_target_apart(week) -> None:  # noqa: F811
    week.db.insert(
        "targets",
        person_id=week.his,
        week_start="2026-09-14",
        kcal=1914,
        protein_g=201,
        fat_g=71,
        carb_g=118,
        fibre_g=27,
        source="formula",
    )
    msg = week.run(evening_confirm.JOB, TUE15)
    first = text_of(msg).splitlines()[0]
    # the 18 Sep screenshot: "Target 1914" at 06:30 and "Planned: 1893" at 20:00, unexplained
    assert "The menu planned 1,915 kcal (target 1,914)." in first, first
    assert "Rest day" not in text_of(msg), "nothing planned is nothing to ask"
    assert "weigh-in" not in text_of(msg).lower(), "the scale reminder is on the page now"


def test_help_is_short_and_the_same_list_feeds_telegrams_menu(week) -> None:  # noqa: F811
    short = week.say("/help")["text"]
    assert len(short.splitlines()) <= 10 and "/help all" in short, short
    listed = handlers_mod.bot_commands(week.app.handlers(week.db))
    names = [c["command"] for c in listed]
    assert names[0] == "today" and names[-1] == "help" and len(names) <= 10, names
    assert all(len(c["description"]) <= 60 for c in listed)


def test_the_status_strip_is_for_owner_and_never_a_message(week) -> None:  # noqa: F811
    week.at(TUE15, "09:00")
    outbox = week.app.outbox(week.db)
    outbox.alert(week.app.registry(week.db).admin(), "shop_refresh", "Shop browser closed")
    got = week.page("/api/status")
    assert any("shop" in i["what"] and "browser closed" in i["why"] for i in got["items"]), got
    sent = week.db.scalar("SELECT COUNT(*) FROM messages_out WHERE kind = 'admin_alert'")
    assert sent == 0


def test_the_status_strip_counts_everything_it_holds_in_one_day(week) -> None:  # noqa: F811
    """ "5 problems in the last day" when the box had 8, read from a notices window of two
    days: the count is of everything, and the window is the same day for both."""
    week.at(TUE15, "09:00")
    outbox = week.app.outbox(week.db)
    admin = week.app.registry(week.db).admin()
    for n in range(9):
        outbox.alert(admin, f"job_{n}", f"problem {n}")
    got = week.page("/api/status")
    assert got["total"] == 9 and len(got["items"]) == 8, got
    week.at(dt.date(2026, 9, 16), "10:00")  # a day and an hour later
    assert week.page("/api/status")["total"] == 0, "yesterday's notices are not the last day's"
    js = app_source()
    assert 'plural(total, "problem")' in js and "data.total" in js


def test_the_sunday_review_is_short_and_the_page_keeps_all_of_it(week) -> None:  # noqa: F811
    week.at(SUN20, "19:00")
    week.run(sunday_summary.JOB, SUN20)
    row = week.db.one(
        "SELECT text_html, payload_json FROM messages_out WHERE kind = 'sunday_summary' "
        "AND person_id = ?",
        (week.his,),
    )
    assert row is not None
    short = str(row["text_html"])
    full = json.loads(str(row["payload_json"]))["full_html"]
    assert len(short.splitlines()) <= 4 and "?tab=summary" in short, short
    assert len(full.splitlines()) > len(short.splitlines()), "the page shows the whole review"
    page = week.page("/api/summary/weekly?week=2026-09-14")
    assert page["text"]["text_html"] == full


def test_no_raw_slot_keys_or_utc_stamps_on_the_page() -> None:

    js = app_source()
    assert "`${s.slot}: ${s.name}" not in js and "`${m.slot}: ${m.name}" not in js
    assert "`sent ${data.text.sent_at}`" not in js and "fmtStamp(data.text.sent_at)" in js
    assert 'data.list.kind.replace("_", " ")' not in js, "no 'segment' in the Shopping heading"


def test_friday_check_in_after_a_menu_week_is_one_question(week) -> None:  # noqa: F811
    msg = week.run(evening_confirm.JOB, FRI18)
    text = text_of(msg)
    assert len([ln for ln in text.splitlines() if ln.strip()]) <= 4, text
    assert dt.date(2026, 9, 18).strftime("%a") in text
