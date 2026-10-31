"""Regression tests for evening check-ins and notification rhythms."""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import pytest

from tiffin import body, days
from tiffin import scheduler as sc
from tiffin import targets as targets_mod
from tiffin.clock import TZ, isoday
from tiffin.jobs import (
    all_jobs,
    evening_confirm,
    morning_targets,
    sunday_summary,
)
from tiffin.model.gateway import ModelResult

from .test_journey_new_person import _interview, newcomer  # noqa: F401 (a pytest fixture)
from .test_journey_owner_week import (  # noqa: F401 (week is a pytest fixture)
    FRI18,
    MON14,
    MON21,
    SAT19,
    SUN20,
    THU17,
    TUE15,
    WED16,
    _Gateway,
    taps,
    text_of,
    week,
)
from .test_streamline import _new_profile


def _confirmed(db, pid: int, day: dt.date) -> bool:
    row = days.latest_confirmation(db, pid, day)
    return row is not None and str(row["status"]) == "confirmed"


def _weekly(w) -> None:
    _new_profile(w.db, w.his, rhythm="weekly")


def _targets(w, week_start: dt.date, kcal: int = 2200) -> None:
    w.db.insert(
        "targets",
        person_id=w.his,
        week_start=isoday(week_start),
        kcal=kcal,
        protein_g=200,
        fat_g=70,
        carb_g=190,
        fibre_g=30,
        source="formula",
    )


def _weigh_ins(w) -> None:
    """A fortnight of mornings on the scale, losing a little: a trend the calibration can read."""
    for i in range(15):
        body.record_weigh_in(
            w.db, w.his, dt.date(2026, 9, 6) + dt.timedelta(days=i), 101.0 - 0.1 * i, "manual"
        )


def _tap_on(w, msg: dict[str, Any] | None, data: str) -> dict[str, Any]:
    """A tap on one of `msg`'s own buttons: Telegram sends the id of the message it is on."""
    assert msg is not None
    real = w.tg.callback_update
    with pytest.MonkeyPatch.context() as m:
        m.setattr(
            w.tg,
            "callback_update",
            lambda uid, chat, d, **_kw: real(uid, chat, d, message_id=int(msg["message_id"])),
        )
        return w.tap(data)


def _last_edit(w) -> list[str]:
    edit = w.tg.edited[-1]
    kb = edit.get("reply_markup") or {}
    return [str(b.get("callback_data", "")) for row in kb.get("inline_keyboard", []) for b in row]


def _tick(w, jobs: list[sc.Job], when: dt.datetime) -> None:
    """The real scheduler at `when`, running every slot it finds due, one after another."""
    w.clock.set_local(when)

    def run_now(_pid: int, fn) -> None:
        fn(w.db)

    sc.Scheduler(w.app, jobs, run_now).tick(w.db)


# ---------------------------------------------------------------- #13
def _calibration_jobs() -> list[sc.Job]:
    """Every scheduled job that writes a week's calibration, and the night's day_close."""
    return [j for j in all_jobs() if "calibration" in j.name or j.name == "day_close"]


def test_a_weekly_rhythms_sunday_answers_reach_the_weeks_calibration(week) -> None:  # noqa: F811
    """A weekly-rhythm person who answers the week in the Sunday check-in has
    that week measured, though the check-in comes after the 17:30 calibration."""
    _weekly(week)
    _weigh_ins(week)
    _targets(week, MON14)
    _tick(week, _calibration_jobs(), dt.datetime(2026, 9, 20, 17, 35, tzinfo=TZ))
    sunday = week.db.one(
        "SELECT * FROM tdee_calibrations WHERE person_id = ? AND week_start = ? "
        "ORDER BY id DESC LIMIT 1",
        (week.his, isoday(MON14)),
    )
    assert sunday is not None and sunday["tdee_measured"] is None, "setup: 17:30 had nothing"
    checkin = week.run(evening_confirm.JOB, SUN20)
    all_days = next(t for t in taps(checkin or {}) if t.startswith("k:cfmall:"))
    week.tap(all_days)
    for d in (MON14, TUE15, WED16, THU17, FRI18, SUN20):
        assert _confirmed(week.db, week.his, d), d
    _tick(week, _calibration_jobs(), dt.datetime(2026, 9, 21, 0, 15, tzinfo=TZ))
    measured = week.db.one(
        "SELECT * FROM tdee_calibrations WHERE person_id = ? AND week_start = ? "
        "ORDER BY id DESC LIMIT 1",
        (week.his, isoday(MON14)),
    )
    assert measured is not None and measured["tdee_measured"] is not None, dict(measured or {})
    assert int(measured["usable_days"]) == 6, dict(measured)
    assert not week.db.state_get(f"sunday_skip:{week.his}"), "the week did recalibrate"


def test_a_light_weeks_sunday_gaps_answered_tonight_reach_the_calibration(week) -> None:  # noqa: F811
    """Days the Sunday message's one tap records after 17:30 are measured
    that night, not left out of every calibration."""
    _weigh_ins(week)
    _targets(week, MON14, kcal=2000)  # near enough to the measure to move without a tap
    _tick(week, _calibration_jobs(), dt.datetime(2026, 9, 20, 17, 35, tzinfo=TZ))
    summary = week.run(sunday_summary.JOB, SUN20)
    gaps = next(t for t in taps(summary or {}) if t.startswith("k:cfmall:"))
    week.tap(gaps)
    week.run(evening_confirm.JOB, SUN20)
    week.tap("k:cfm:20260920")
    _tick(week, _calibration_jobs(), dt.datetime(2026, 9, 21, 0, 15, tzinfo=TZ))
    measured = week.db.one(
        "SELECT * FROM tdee_calibrations WHERE person_id = ? AND week_start = ? "
        "ORDER BY id DESC LIMIT 1",
        (week.his, isoday(MON14)),
    )
    assert measured is not None and measured["tdee_measured"] is not None, dict(measured or {})
    nxt = week.db.one(
        "SELECT * FROM v_targets_latest WHERE person_id = ? AND week_start = ?",
        (week.his, isoday(MON21)),
    )
    assert nxt is not None, "next week has targets"
    basis = json.loads(str(nxt["basis_json"] or "{}"))
    assert basis.get("calibration", {}).get("usable_days") == 6, basis


def test_a_second_look_never_overwrites_targets_chosen_by_a_tap(week) -> None:  # noqa: F811
    """The overnight calibration leaves next week alone when the person has
    already accepted next week's targets themselves."""
    _weekly(week)
    _weigh_ins(week)
    _targets(week, MON14, kcal=2000)  # a measure the second look could apply without a tap
    _tick(week, _calibration_jobs(), dt.datetime(2026, 9, 20, 17, 35, tzinfo=TZ))
    pid = week.db.insert(
        "proposals",
        person_id=week.his,
        kind="targets",
        payload_json="{}",
        reason_line="r",
        expires_at="2026-09-27 17:55:00",
    )
    week.db.insert(
        "targets",
        person_id=week.his,
        week_start=isoday(MON21),
        kcal=2050,
        protein_g=200,
        fat_g=70,
        carb_g=150,
        fibre_g=30,
        source="proposal",
        proposal_id=pid,
    )
    checkin = week.run(evening_confirm.JOB, SUN20)
    week.tap(next(t for t in taps(checkin or {}) if t.startswith("k:cfmall:")))
    _tick(week, _calibration_jobs(), dt.datetime(2026, 9, 21, 0, 15, tzinfo=TZ))
    nxt = week.db.one(
        "SELECT * FROM v_targets_latest WHERE person_id = ? AND week_start = ?",
        (week.his, isoday(MON21)),
    )
    assert nxt is not None and str(nxt["source"]) == "proposal" and int(nxt["kcal"]) == 2050


def test_a_weekly_review_before_its_check_in_never_counts_the_week_as_unanswered(week) -> None:  # noqa: F811
    """A weekly person's Sunday review, read before their own check-in, says
    the check-in asks about the open days and that the week is measured overnight - not
    'N not answered' and 'No recalibration this week'."""
    week.app.config.data["web"]["origin"] = ""  # no page: the whole review comes
    _weekly(week)
    _weigh_ins(week)
    _targets(week, MON14)
    _tick(week, _calibration_jobs(), dt.datetime(2026, 9, 20, 17, 35, tzinfo=TZ))
    text = text_of(week.run(sunday_summary.JOB, SUN20))
    assert "Week to 20 Sep" in text, text
    assert "not answered" not in text and "No recalibration this week" not in text, text
    # Mon-Fri with their menu, and Saturday without one
    assert "Days recorded: 0; tonight's check-in asks about the other 6" in text, text
    assert "measured again overnight" in text, text
    assert len(text.splitlines()) <= 10, text


def test_a_paused_weekly_sunday_review_never_promises_a_check_in(week) -> None:  # noqa: F811
    week.app.config.data["web"]["origin"] = ""  # no page: the whole review comes
    _weekly(week)
    _weigh_ins(week)
    _targets(week, MON14)
    week.db.insert(
        "pause_periods", person_id=week.his, start_day="2026-09-20", end_day="2026-09-20"
    )
    _tick(week, _calibration_jobs(), dt.datetime(2026, 9, 20, 17, 35, tzinfo=TZ))
    msg = week.run(sunday_summary.JOB, SUN20)
    text = text_of(msg)
    assert "Week to 20 Sep" in text, text
    assert "tonight's check-in" not in text.lower(), text
    assert "measured again overnight" not in text, text
    assert "not answered" in text, text
    assert any(t.endswith(":week") for t in taps(msg or {})), "the week is asked here, once"
    assert week.run(evening_confirm.JOB, SUN20) is None, "setup: no check-in while paused"


# ---------------------------------------------------------------- #82
def test_a_weekly_sunday_answered_early_still_gets_the_week_asked(week) -> None:  # noqa: F811
    """A weekly person who recorded Sunday before the check-in is still
    offered the week's open days that evening, and one tap records them."""
    _weekly(week)
    week.at(SUN20, "13:00")
    assert "Sun 20 Sep recorded" in text_of(week.say("/confirm")), "setup"
    msg = week.run(evening_confirm.JOB, SUN20)
    text = text_of(msg)
    assert "Mon, Tue, Wed, Thu, Fri" in text, text
    assert "k:cfmall:20260920:before" in taps(msg or {}), taps(msg or {})
    assert "k:cfm:20260920" not in taps(msg or {}), "today is already answered"
    done = _tap_on(week, msg, "k:cfmall:20260920:before")
    assert "Recorded as planned: Mon, Tue, Wed, Thu, Fri." in text_of(done), text_of(done)
    assert days.unanswered_days(week.db, week.his, SUN20) == []


def test_a_day_answered_early_still_hears_about_the_days_before_it(week) -> None:  # noqa: F811
    """On any rhythm, a day recorded before its check-in does not silence
    the check-in's offer of the earlier open days."""
    week.at(WED16, "13:00")
    week.say("/confirm")
    msg = week.run(evening_confirm.JOB, WED16)
    assert "Mon, Tue" in text_of(msg), text_of(msg)
    assert "k:cfmall:20260916:before" in taps(msg or {}), taps(msg or {})


# ---------------------------------------------------------------- #84
def _plan(w, *sessions: tuple[int, str, int]) -> None:
    plan = w.db.insert(
        "training_plans", person_id=w.his, effective_from="2026-09-01", source_text="[]"
    )
    for weekday, kind, minutes in sessions:
        w.db.insert(
            "planned_sessions",
            plan_id=plan,
            person_id=w.his,
            weekday=weekday,
            kind=kind,
            target_minutes=minutes,
        )


def test_a_weekly_check_in_asks_about_the_weeks_sessions(week) -> None:  # noqa: F811
    """The weekly Sunday check-in asks, day by day, about the planned sessions
    of the week nobody recorded, and a tap records each one."""
    _weekly(week)
    _plan(week, (1, "strength", 30), (3, "run", 30))
    msg = week.run(evening_confirm.JOB, SUN20)
    text = text_of(msg)
    assert "Tue strength 30 min" in text and "Thu run 30 min" in text, text
    for key in ("20260915", "20260917"):
        assert f"k:sessm:{key}:done" in taps(msg or {}) and f"k:sessm:{key}:skip" in taps(
            msg or {}
        ), taps(msg or {})
    said = _tap_on(week, msg, "k:sessm:20260915:done")
    assert "Tue 15 Sep" in text_of(said), text_of(said)
    decided = days.session_decided(week.db, week.his, TUE15)
    assert decided is not None and str(decided["status"]) == "manual"
    _tap_on(week, msg, "k:cfm:20260920")
    kept = _last_edit(week)
    assert "k:sessm:20260917:done" in kept, "Thursday is still to answer after today's tap"
    assert "k:sessm:20260915:done" not in kept, "Tuesday is answered"


def test_a_weekly_sunday_answered_early_still_asks_about_its_own_session(week) -> None:  # noqa: F811
    _weekly(week)
    _plan(week, (1, "strength", 30), (6, "ride", 90))
    week.at(SUN20, "13:00")
    assert "Sun 20 Sep recorded" in text_of(week.say("/confirm")), "setup"
    msg = week.run(evening_confirm.JOB, SUN20)
    text = text_of(msg)
    assert "strength 30 min" in text and "ride 90 min" in text, text
    for key in ("20260915", "20260920"):
        assert f"k:sessm:{key}:done" in taps(msg or {}), taps(msg or {})
    _tap_on(week, msg, "k:sessm:20260920:done")
    decided = days.session_decided(week.db, week.his, SUN20)
    assert decided is not None and str(decided["status"]) == "manual"


def test_a_sunday_answered_early_with_only_its_session_open_is_still_asked(week) -> None:  # noqa: F811
    _weekly(week)
    _plan(week, (6, "ride", 90))
    for d in (MON14, TUE15, WED16, THU17, FRI18):
        days.confirm_day(
            week.db, week.his, d, basis="menu", macros=days.planned_macros(week.db, week.his, d)
        )
    week.at(SUN20, "13:00")
    week.say("/confirm")
    msg = week.run(evening_confirm.JOB, SUN20)
    assert "ride 90 min" in text_of(msg), text_of(msg)
    assert "k:sessm:20260920:done" in taps(msg or {}), taps(msg or {})


# ---------------------------------------------------------------- #101
def test_a_typed_answer_naming_an_open_day_is_that_days_food(
    week,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On Wednesday's check-in, 'Tuesday no, I had a pizza instead of
    dinner' asks about Tuesday's meals, by name, and is recorded against Tuesday."""
    heard: list[str] = []

    def converse(_self, _task, messages, *, person_id, tools, **_kw) -> ModelResult:
        said = str(messages[-1]["content"])
        heard.append(said)  # a model that takes "today is the default day" at its word
        tools.call("log_offplan", {"description": said})
        return ModelResult(text="", data=None, model_used="fake", backend="fake")

    monkeypatch.setattr(_Gateway, "converse", converse, raising=False)
    msg = week.run(evening_confirm.JOB, WED16)
    assert "Also still open: Mon, Tue" in text_of(msg), "setup"
    week.at(WED16, "20:10")
    asked = week.say("Tuesday no, I had a pizza instead of dinner")
    assert "Tue 15 Sep" in text_of(asked), text_of(asked)
    assert "k:mslotok:20260915" in taps(asked), taps(asked)
    card = week.tap("k:mslotok:20260915")
    assert "Log Tue 15 Sep" in text_of(card), text_of(card)
    accept = next(t for t in taps(card) if t.startswith("k:intent:"))
    week.tap(accept)
    assert _confirmed(week.db, week.his, TUE15)
    assert not _confirmed(week.db, week.his, WED16), "Wednesday's own question is still open"


def _chat_hears(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    heard: list[str] = []

    def converse(_self, _task, messages, *, person_id, tools, **_kw) -> ModelResult:
        heard.append(str(messages[-1]["content"]))
        return ModelResult(text="Noted.", data=None, model_used="fake", backend="fake")

    monkeypatch.setattr(_Gateway, "converse", converse, raising=False)
    return heard


@pytest.mark.parametrize("said", ["Tuesday was as planned", "yesterday was fine"])
def test_an_open_day_named_as_planned_is_recorded_as_planned(
    week,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    said: str,
) -> None:
    _chat_hears(monkeypatch)
    week.run(evening_confirm.JOB, WED16)
    week.at(WED16, "20:10")
    got = week.say(said)
    assert not any(t.startswith("k:mslot") for t in taps(got)), text_of(got)
    assert "Tue 15 Sep" in text_of(got), text_of(got)
    row = days.latest_confirmation(week.db, week.his, TUE15)
    assert row is not None and str(row["status"]) == "confirmed" and str(row["basis"]) == "menu"
    assert not _confirmed(week.db, week.his, WED16), "Wednesday's own question is still open"
    asked = week.say("kebab instead of dinner")
    assert "k:mslotok:20260916" in taps(asked), "and it is still the one the flow answers"


def test_an_open_day_named_without_food_is_left_to_chat(
    week,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    heard = _chat_hears(monkeypatch)
    week.run(evening_confirm.JOB, WED16)
    week.at(WED16, "20:10")
    got = week.say("Tuesday I was at my mum's")
    assert not any(t.startswith("k:mslot") for t in taps(got)), text_of(got)
    assert heard == ["Tuesday I was at my mum's"], heard
    assert not _confirmed(week.db, week.his, TUE15)


def test_a_typed_answer_naming_a_day_with_no_menu_is_that_days_food(
    week,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    def converse(_self, _task, messages, *, person_id, tools, **_kw) -> ModelResult:
        tools.call("log_offplan", {"description": str(messages[-1]["content"])})
        return ModelResult(text="", data=None, model_used="fake", backend="fake")

    monkeypatch.setattr(_Gateway, "converse", converse, raising=False)
    _weekly(week)
    _targets(week, MON14)
    msg = week.run(evening_confirm.JOB, SUN20)
    assert "Sat had no menu" in text_of(msg), "setup"
    week.at(SUN20, "20:40")
    card = week.say("Saturday: pizza and two beers")
    assert "Log Sat 19 Sep" in text_of(card), text_of(card)
    week.tap(next(t for t in taps(card) if t.startswith("k:intent:")))
    assert _confirmed(week.db, week.his, SAT19)
    assert not _confirmed(week.db, week.his, SUN20), "Sunday's own question is still open"


def test_the_meal_question_names_its_day(week) -> None:  # noqa: F811
    """The question about which meals were replaced names the day it will
    be recorded against."""
    week.run(evening_confirm.JOB, WED16)
    week.at(WED16, "20:10")
    week.tap("k:cfmx:20260916")
    asked = week.say("skipped dinner, had a burrito")
    assert "skipping dinner on Wed 16 Sep" in text_of(asked), text_of(asked)


# ---------------------------------------------------------------- #102
def test_a_weekly_evening_still_says_what_to_move_from_the_freezer(week) -> None:  # noqa: F811
    """On a weekly rhythm's weekday, the evening still says which boxes to
    move from the freezer for tomorrow - only that, with no question."""
    _weekly(week)
    profile = targets_mod.profile_on(week.db, week.his, WED16)
    freezer = evening_confirm.freezer_line(week.db, week.his, WED16, profile)
    assert "from the freezer to the fridge tonight" in freezer, "setup: Thursday's boxes"
    week.at(WED16, "12:00")
    week.say("/today")  # not silent: the reminder would chase this person
    msg = week.run(evening_confirm.JOB, WED16)
    text = text_of(msg)
    assert "from the freezer to the fridge tonight" in text, text
    assert "As planned" not in text and not taps(msg or {}), "not a check-in"


# ---------------------------------------------------------------- #61
def test_a_weekly_check_in_asks_about_a_day_with_no_menu(week) -> None:  # noqa: F811
    """The weekly Sunday check-in names a day with no menu and records it at
    the target with one tap."""
    _weekly(week)
    _targets(week, MON14)
    msg = week.run(evening_confirm.JOB, SUN20)
    text = text_of(msg)
    assert "Sat had no menu" in text and "2,200 kcal" in text, text
    tap = next((t for t in taps(msg or {}) if t.endswith(":target")), "")
    assert tap, taps(msg or {})
    done = _tap_on(week, msg, tap)
    assert "Sat" in text_of(done), text_of(done)
    row = days.latest_confirmation(week.db, week.his, SAT19)
    assert row is not None and str(row["status"]) == "confirmed", row
    assert int(row["kcal"]) == 2200 and row["note"] == "assumed target"
    assert not _confirmed(week.db, week.his, SUN20), "the Sunday is its own question"
    assert not _confirmed(week.db, week.his, MON14), "and the menu days are the other button's"


# ---------------------------------------------------------------- #103
def test_a_paused_first_morning_does_not_silence_the_rest_of_the_food(week) -> None:  # noqa: F811
    """On the light rhythm, when Monday's morning message was never sent,
    Tuesday's is - the same food as yesterday, but never yet said."""
    week.db.insert(
        "pause_periods", person_id=week.his, start_day="2026-09-14", end_day="2026-09-14"
    )
    assert week.run(morning_targets.JOB, MON14) is None, "setup: paused"
    tuesday = week.run(morning_targets.JOB, TUE15)
    assert "Tue 15 Sep" in text_of(tuesday) and "First up" in text_of(tuesday), text_of(tuesday)
    assert week.run(morning_targets.JOB, WED16) is None, "Tuesday's message said it"


def test_a_morning_lost_to_an_outage_does_not_silence_the_next(week) -> None:  # noqa: F811
    """With no Monday run at all, Tuesday's morning message still comes."""
    assert week.run(morning_targets.JOB, TUE15) is not None


# ---------------------------------------------------------------- #76
def test_redoing_the_interview_keeps_the_rhythm_chosen(newcomer) -> None:  # noqa: F811
    """/onboard restart keeps the rhythm set with /set rhythm, since the
    interview never asks it."""
    _interview(newcomer)
    assert "weekly" in newcomer["say"]("/set rhythm weekly")
    say = newcomer["say"]
    redo = dict(newcomer, say=lambda t: say("/onboard restart" if t == "/onboard" else t))
    _interview(redo)
    db, pid = newcomer["db"], newcomer["pid"]
    profiles = db.all("SELECT rhythm FROM profiles WHERE person_id = ? ORDER BY id", (pid,))
    assert len(profiles) >= 3, "setup: two interviews and the setting"
    assert str(profiles[-1]["rhythm"]) == "weekly", [str(p["rhythm"]) for p in profiles]


# ---------------------------------------------------------------- #139
def test_the_interview_says_how_often_the_messages_come(newcomer) -> None:  # noqa: F811
    """The end of the interview says that on the light rhythm the morning
    message comes only when the food changes, and how to change that."""
    _interview(newcomer)
    done = next(m for m in newcomer["tg"].sent if "Profile saved" in str(m["text"]))
    text = str(done["text"])
    assert "only when the day's food" in text and "/set rhythm" in text, text
    assert len(text.splitlines()) <= 10, text
