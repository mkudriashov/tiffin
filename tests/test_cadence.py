"""Fewer messages for the same answers: the Sunday message is the week's fill-the-gaps and nothing
else for someone the page lets in, the review is kept for the page every week, and the cycle
question goes only to a person who opted into it.
"""

from __future__ import annotations

import datetime as dt

from tiffin import days
from tiffin.clock import isoday
from tiffin.jobs import body_check, evening_confirm, sunday_summary

from .test_journey_owner_week import MON14, SUN20, taps, text_of, week  # noqa: F401
from .test_streamline import _new_profile

ORIGIN = "https://tiffin.example.ts.net"


def test_sunday_names_the_days_still_open_and_one_tap_records_them(week) -> None:  # noqa: F811
    week.app.config.data["web"]["origin"] = ORIGIN
    msg = week.run(sunday_summary.JOB, SUN20)
    text = text_of(msg)
    assert "Still open from the week: Mon, Tue, Wed, Thu and Fri." in text, text
    # keyed on the Sunday it asked from, so the tap records the days before it and nothing
    # earlier
    assert "k:cfmall:20260920:week" in taps(msg), taps(msg)
    assert len(text.splitlines()) <= 10
    assert "tab=summary" in text, "the review is on the page"
    assert "Next: /menu" not in text, "the headline and the question, not the whole review"
    done = week.tap("k:cfmall:20260920:week")
    assert "Recorded as planned: Mon, Tue, Wed, Thu, Fri" in text_of(done)
    assert days.unanswered_days(week.db, week.his, SUN20) == []


def test_the_evening_does_not_ask_again_what_the_summary_asked(week) -> None:  # noqa: F811
    week.run(sunday_summary.JOB, SUN20)
    assert week.db.state_get(f"catchup_asked:{week.his}") == isoday(SUN20)
    evening = week.run(evening_confirm.JOB, SUN20)
    assert evening is None or "still open" not in text_of(evening), "one ask a day"


def test_a_week_with_nothing_open_sends_nothing_and_keeps_the_review(week) -> None:  # noqa: F811
    week.app.config.data["web"]["origin"] = ORIGIN
    for i in range(5):
        d = MON14 + dt.timedelta(days=i)
        days.confirm_day(
            week.db, week.his, d, basis="menu", macros=days.planned_macros(week.db, week.his, d)
        )
    assert week.run(sunday_summary.JOB, SUN20) is None, "nothing to answer: the page has it"
    kept = week.db.one("SELECT full_html FROM weekly_reviews WHERE person_id = ?", (week.his,))
    assert kept is not None and "Week to 20 Sep" in str(kept["full_html"])
    shown = week.page("/api/summary/weekly?week=2026-09-14")
    assert shown["text"] is not None and "Week to 20 Sep" in shown["text"]["text_html"]


def test_without_the_page_the_whole_review_still_comes(week) -> None:  # noqa: F811
    week.app.config.data["web"]["origin"] = ""  # no page to send anyone to
    msg = week.run(sunday_summary.JOB, SUN20)
    text = text_of(msg)
    assert "Still open from the week" in text and text.rstrip().endswith(
        "Next: /menu for the week ahead."
    )
    assert len(text.splitlines()) <= 10


def test_a_weekly_rhythm_answers_the_week_in_its_own_check_in(week) -> None:  # noqa: F811
    _new_profile(week.db, week.his, rhythm="weekly")
    week.run(sunday_summary.JOB, SUN20)
    assert week.db.state_get(f"catchup_asked:{week.his}") is None
    evening = week.run(evening_confirm.JOB, SUN20)
    assert evening is not None and "still open" in text_of(evening).lower()


# ---------------------------------------------------------------- the weekly cycle question


def test_no_tape_question_for_a_person_without_cycle_tracking(week) -> None:  # noqa: F811
    assert week.run(body_check.JOB, SUN20) is None
    assert week.db.scalar("SELECT COUNT(*) FROM tape_measures") == 0
    assert week.db.scalar("SELECT COUNT(*) FROM conversation_state") == 0


def test_the_cycle_is_still_asked_weekly_without_the_tape(week) -> None:  # noqa: F811
    _new_profile(week.db, week.his, cycle_tracking=1)
    msg = week.run(body_check.JOB, SUN20)
    assert text_of(msg) == "Where are you in your cycle?"
    assert any(t.startswith("k:cyc:") for t in taps(msg))
    assert week.db.scalar("SELECT COUNT(*) FROM tape_measures") == 0
    assert week.db.scalar("SELECT COUNT(*) FROM conversation_state") == 0
