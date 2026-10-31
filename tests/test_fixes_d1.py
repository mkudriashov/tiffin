"""Regression tests for weekly summaries and their actions."""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tiffin import body, days, reset
from tiffin import scheduler as sc
from tiffin import targets as targets_mod
from tiffin.clock import TZ, isoday
from tiffin.errors import ModelUnavailable
from tiffin.jobs import evening_confirm, sunday_summary
from tiffin.model.gateway import ModelResult
from tiffin.telegram import convstate
from tiffin.telegram.poller import Poller
from tiffin.web.app import create_app

from .fakes import err
from .test_journey_owner_week import (  # noqa: F401 (week is a pytest fixture)
    FRI18,
    MON14,
    MON21,
    OWNER_LOGIN,
    PARTNER_CHAT,
    SAT19,
    SUN13,
    SUN20,
    THU17,
    TUE15,
    WED16,
    _Gateway,
    _seed_week,
    taps,
    text_of,
    week,
)
from .test_streamline import _new_profile

ORIGIN = "https://tiffin.example.ts.net"
WED09 = dt.date(2026, 9, 9)
TUE22 = dt.date(2026, 9, 22)


def _confirmed(db, pid: int, day: dt.date) -> bool:
    row = days.latest_confirmation(db, pid, day)
    return row is not None and str(row["status"]) == "confirmed"


def _partner(w) -> int:
    return int(w.db.scalar("SELECT id FROM persons WHERE slug = 'partner'"))


def _to_partner(w) -> list[dict[str, Any]]:
    return [m for m in w.tg.sent if int(m["chat_id"]) == PARTNER_CHAT]


def _labels(msg: dict[str, Any] | None) -> list[str]:
    kb = (msg or {}).get("reply_markup") or {}
    if isinstance(kb, str):
        kb = json.loads(kb)
    return [str(b.get("text", "")) for row in kb.get("inline_keyboard", []) for b in row]


def _week_button(msg: dict[str, Any] | None) -> str:
    found = [t for t in taps(msg or {}) if t.startswith("k:cfmall:")]
    assert len(found) == 1, f"one button for the week's open days: {taps(msg or {})}"
    return found[0]


def _last_edit(w) -> list[str]:
    edit = w.tg.edited[-1]
    kb = edit.get("reply_markup") or {}
    return [str(b.get("callback_data", "")) for row in kb.get("inline_keyboard", []) for b in row]


def _confirm_all_before(w, day: dt.date) -> None:
    for d in (MON14, TUE15, WED16, THU17, FRI18):
        if d < day:
            days.confirm_day(
                w.db, w.his, d, basis="menu", macros=days.planned_macros(w.db, w.his, d)
            )


def _client(w) -> TestClient:
    w.app.config.data["web"]["root_path"] = ""  # the CSRF cookie's path is the page's root
    return TestClient(create_app(w.app), base_url="http://testserver")


def _post_headers(client: TestClient) -> dict[str, str]:
    csrf = client.get("/api/me", headers={"Tailscale-User-Login": OWNER_LOGIN}).json()["csrf"]
    return {"Tailscale-User-Login": OWNER_LOGIN, "X-CSRF-Token": csrf}


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


def _model_logs(monkeypatch: pytest.MonkeyPatch, day: dt.date) -> list[str]:
    """The chat model, hearing food on a day the person named: one log_offplan for that day.
    Returns what reached it, so a story can say the message got there at all."""
    heard: list[str] = []

    def converse(_self, _task, messages, *, person_id, tools, **_kw) -> ModelResult:
        said = str(messages[-1]["content"])
        heard.append(said)
        tools.call("log_offplan", {"day": isoday(day), "description": said})
        return ModelResult(text="", data=None, model_used="fake", backend="fake")

    monkeypatch.setattr(_Gateway, "converse", converse, raising=False)
    return heard


def _targets(w, week_start: dt.date) -> None:
    w.db.insert(
        "targets",
        person_id=w.his,
        week_start=isoday(week_start),
        kcal=2200,
        protein_g=200,
        fat_g=70,
        carb_g=190,
        fibre_g=30,
        source="formula",
    )


def _accept(w, card: dict[str, Any]) -> str:
    accept = next((t for t in taps(card) if t.startswith("k:intent:")), "")
    assert accept, f"no card to accept: {text_of(card)}"
    return text_of(w.tap(accept))


# ---------------------------------------------------------------- #11
def test_the_sunday_tap_records_only_the_days_the_message_named(week) -> None:  # noqa: F811
    """The Sunday button records the week's open days and never last Sunday,
    which the message did not name."""
    _seed_week(week.db, week.his, week.ing, WED09, status="repeated", decision="accepted")
    for d in (WED09, dt.date(2026, 9, 10), dt.date(2026, 9, 11), dt.date(2026, 9, 12)):
        days.confirm_day(
            week.db, week.his, d, basis="menu", macros=days.planned_macros(week.db, week.his, d)
        )
    assert days.unanswered_days(week.db, week.his, dt.date(2026, 9, 18))[0] == SUN13, "setup"
    week.app.config.data["web"]["origin"] = ORIGIN
    msg = week.run(sunday_summary.JOB, SUN20)
    assert "Still open from the week: Mon, Tue, Wed, Thu and Fri." in text_of(msg)
    done = week.tap(_week_button(msg))
    assert "Recorded as planned: Mon, Tue, Wed, Thu, Fri." in text_of(done), text_of(done)
    assert not _confirmed(week.db, week.his, SUN13), "last Sunday was never asked about"
    for d in (MON14, TUE15, WED16, THU17, FRI18):
        assert _confirmed(week.db, week.his, d), d


def test_the_sunday_tap_leaves_the_sunday_itself_to_its_own_check_in(week) -> None:  # noqa: F811
    """A Sunday with a menu is not in the week's button; tonight's check-in
    asks about it."""
    _seed_week(week.db, week.his, week.ing, SUN20, status="accepted", decision="accepted")
    assert days.planned_macros(week.db, week.his, SUN20) is not None, "setup: Sunday has a menu"
    msg = week.run(sunday_summary.JOB, SUN20)
    week.tap(_week_button(msg))
    assert not _confirmed(week.db, week.his, SUN20)


# ---------------------------------------------------------------- #12, #131
def test_the_gap_line_sends_an_off_plan_day_to_a_route_that_asks_what_changed(week) -> None:  # noqa: F811
    """The Sunday message never sends a day that went off plan to /confirm
    or the page, which only record it as planned; it asks to be told what changed."""
    week.app.config.data["web"]["origin"] = ORIGIN
    text = text_of(week.run(sunday_summary.JOB, SUN20))
    assert "Still open from the week" in text, text
    assert "/confirm" not in text and "or the page" not in text, text
    assert "tell me the day and what changed (e.g. 'Monday: " in text, text


def test_one_open_day_is_asked_about_with_its_name_in_the_answer(week) -> None:  # noqa: F811
    _confirm_all_before(week, FRI18)
    text = text_of(week.run(sunday_summary.JOB, SUN20))
    assert "Still open from the week: Fri." in text, text
    assert "tell me the day and what changed (e.g. 'Friday: " in text, text


def test_an_answer_naming_another_day_after_the_check_in_is_that_days(
    week,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    heard = _model_logs(monkeypatch, WED16)
    week.run(sunday_summary.JOB, SUN20)
    week.run(evening_confirm.JOB, SUN20)
    week.at(SUN20, "20:30")
    asked = week.say("Wednesday: kebab instead of dinner")
    # an open day named in the answer is read by the check-in's own flow, with no model
    #
    assert heard == [], text_of(asked)
    assert "skipping dinner" in text_of(asked), text_of(asked)
    card = week.tap("k:mslotok:20260916")
    assert "Log Wed 16 Sep" in text_of(card), text_of(card)
    said = _accept(week, card)
    assert "Wed 16 Sep is now" in said, said
    assert _confirmed(week.db, week.his, WED16)
    assert not _confirmed(week.db, week.his, SUN20), "Sunday's own question is still open"


def test_an_answer_about_tonight_still_answers_the_check_in(week, monkeypatch) -> None:  # noqa: F811
    heard = _model_logs(monkeypatch, WED16)
    week.run(evening_confirm.JOB, SUN20)
    week.at(SUN20, "20:30")
    card = week.say("Sunday: pizza instead of dinner")
    assert heard == [], "the check-in's own answer never goes to chat"
    assert "Log Sun 20 Sep" in text_of(card), text_of(card)


@pytest.mark.parametrize(
    ("said", "day", "first_open"),
    [
        ("Fri: pizza instead of dinner", FRI18, FRI18),
        ("Weds: kebab instead of dinner", WED16, MON14),
    ],
)
def test_an_answer_naming_its_day_as_the_message_does_is_that_days(
    week,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    said: str,
    day: dt.date,
    first_open: dt.date,
) -> None:
    heard = _model_logs(monkeypatch, day)
    _confirm_all_before(week, first_open)
    summary = text_of(week.run(sunday_summary.JOB, SUN20))
    assert f"Still open from the week: {first_open:%a}" in summary, summary
    week.run(evening_confirm.JOB, SUN20)
    week.at(SUN20, "20:30")
    asked = week.say(said)
    assert heard == [], text_of(asked)  # read without a model
    card = week.tap(f"k:mslotok:{day:%Y%m%d}")
    assert f"Log {day:%a %d %b}" in text_of(card), text_of(card)
    _accept(week, card)
    assert _confirmed(week.db, week.his, day)
    assert not _confirmed(week.db, week.his, SUN20), "Sunday's own question is still open"


def test_sat_and_sun_name_a_day_only_where_the_answer_heads_with_them(
    week,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    heard = _model_logs(monkeypatch, WED16)
    week.run(evening_confirm.JOB, SUN20)
    week.at(SUN20, "20:30")
    card = week.say("sat on the sofa with a pizza instead of dinner")
    assert heard == [], "the check-in's own answer never goes to chat"
    assert "Log Sun 20 Sep" in text_of(card), text_of(card)


def _partner_phone(w):
    """Her side of the chat: what she types and taps, and the last thing she was sent."""
    handlers = w.app.handlers(w.db)
    poller = Poller(
        w.app.api(),
        w.db,
        w.app.registry(w.db),
        handlers.handle,
        clock=w.clock,
        sleeper=lambda s: None,
    )
    n = {"i": 9000}  # past every update the fixture's own poller has seen

    def say(text: str) -> dict[str, Any]:
        n["i"] += 1
        w.tg.queue_update(w.tg.text_update(n["i"], PARTNER_CHAT, text))
        poller.run_once()
        return _to_partner(w)[-1]

    def tap(data: str) -> dict[str, Any]:
        n["i"] += 1
        w.tg.queue_update(w.tg.callback_update(n["i"], PARTNER_CHAT, data, message_id=n["i"]))
        poller.run_once()
        return _to_partner(w)[-1]

    return say, tap


def test_without_a_model_the_day_named_in_words_still_gets_recorded(
    week,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    def unavailable(*_a, **_kw) -> ModelResult:
        raise ModelUnavailable("chat: claude unavailable (no_consent)")

    monkeypatch.setattr(_Gateway, "converse", unavailable, raising=False)
    monkeypatch.setattr(_Gateway, "json_task", unavailable)
    partner = _partner(week)
    _seed_week(week.db, partner, week.ing, MON14, status="accepted", decision="accepted")
    week.run(sunday_summary.JOB, SUN20)
    assert "tell me the day and what changed" in text_of(_to_partner(week)[-1]), "setup"
    say, tap = _partner_phone(week)
    week.at(SUN20, "19:30")
    asked = say("Wednesday: kebab instead of dinner")
    assert "could not reach a model" not in text_of(asked), text_of(asked)
    assert "skipping dinner" in text_of(asked), text_of(asked)
    done = tap("k:mslotok:20260916")
    assert "Noted for Wed 16 Sep" in text_of(done), text_of(done)
    assert _confirmed(week.db, partner, WED16)


def test_without_a_model_a_question_naming_a_day_is_not_food(
    week,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    def unavailable(*_a, **_kw) -> ModelResult:
        raise ModelUnavailable("chat: claude unavailable (no_consent)")

    monkeypatch.setattr(_Gateway, "converse", unavailable, raising=False)
    week.at(SUN20, "19:30")
    said = week.say("what did I have on Wednesday?")
    assert "could not reach a model" in text_of(said), text_of(said)
    assert not _confirmed(week.db, week.his, WED16)


def test_partner_sunday_message_never_points_partner_at_a_page_partner_cannot_open(week) -> None:  # noqa: F811
    """Without a page login, the Sunday message does not tell her to use
    the page."""
    week.app.config.data["web"]["origin"] = ORIGIN
    partner = _partner(week)
    _seed_week(week.db, partner, week.ing, MON14, status="accepted", decision="accepted")
    week.run(sunday_summary.JOB, SUN20)
    mine = _to_partner(week)
    assert mine, "nothing reached her"
    text = text_of(mine[-1])
    assert "Still open from the week: Mon, Tue, Wed, Thu and Fri." in text, text
    assert "the page" not in text, text


# ---------------------------------------------------------------- #62
def test_without_the_page_the_nudges_go_before_the_partner_line(week) -> None:  # noqa: F811
    """With open days and no page, the gap line's room comes out of the
    optional nudges, never out of the partner's line."""
    week.app.config.data["web"]["origin"] = ""
    partner = _partner(week)
    for i, kg in enumerate((70.4, 70.2, 70.0)):
        body.record_weigh_in(week.db, partner, MON14 + dt.timedelta(days=2 * i), kg, "manual")
    week.db.state_set(
        f"sunday_notes:{week.his}",
        json.dumps({"week_start": isoday(MON14), "lines": ["Nudge one.", "Nudge two."]}),
    )
    week.db.state_set(
        f"sunday_skip:{week.his}",
        json.dumps({"week_start": isoday(MON14), "reason": "usable_days=0 < 5"}),
    )
    week.db.insert(
        "targets",
        person_id=week.his,
        week_start=isoday(MON21),
        kcal=2200,
        protein_g=200,
        fat_g=70,
        carb_g=190,
        fibre_g=30,
        source="formula",
    )
    text = text_of(week.run(sunday_summary.JOB, SUN20))
    assert "Next week:" in text and "No recalibration this week" in text, "setup: five core lines"
    lines = text.splitlines()
    assert len(lines) <= 10, text
    assert any(ln.startswith("Sam: trend") for ln in lines), text
    assert "Still open from the week" in text and lines[-1] == sunday_summary.ROUTE_LINE, text
    assert "Nudge two." not in text, "a nudge is what made the room"


# ---------------------------------------------------------------- #63
def test_a_summary_that_failed_to_send_leaves_the_evening_to_ask(week) -> None:  # noqa: F811
    """When the Sunday message did not go out, the evening check-in still
    offers the open days."""
    week.tg.fail("sendMessage", err(400, "Bad Request: chat not found"), err(400, "Bad Request"))
    assert week.run(sunday_summary.JOB, SUN20) is None
    failed = week.db.scalar(
        "SELECT status FROM messages_out WHERE kind = 'sunday_summary' AND person_id = ?",
        (week.his,),
    )
    assert failed == "failed", "setup: the summary did not go out"
    assert week.db.state_get(f"catchup_asked:{week.his}") is None
    evening = week.run(evening_confirm.JOB, SUN20)
    assert "Also still open: Mon, Tue, Wed, Thu, Fri" in text_of(evening), text_of(evening)
    assert any(t.startswith("k:cfmall:") for t in taps(evening or {}))


# ---------------------------------------------------------------- #83
def test_an_early_check_in_that_asked_keeps_the_summary_from_asking_again(week) -> None:  # noqa: F811
    """With a check-in at 18:30 that offered the open days, the 19:00 summary
    does not offer them a second time."""
    week.app.config.data["web"]["origin"] = ORIGIN
    _new_profile(week.db, week.his, confirm_time="18:30")
    evening = week.run(evening_confirm.JOB, SUN20)
    assert "Also still open: Mon, Tue, Wed, Thu, Fri" in text_of(evening), text_of(evening)
    summary = week.run(sunday_summary.JOB, SUN20)
    assert summary is None or "Still open" not in text_of(summary), text_of(summary)
    assert summary is None or not any(t.startswith("k:cfmall:") for t in taps(summary))
    kept = week.db.one(
        "SELECT full_html FROM weekly_reviews WHERE person_id = ? ORDER BY id DESC", (week.his,)
    )
    assert kept is not None and "Week to 20 Sep" in str(kept["full_html"]), "the review is kept"


def test_without_the_page_an_early_ask_still_sends_the_review_without_the_question(week) -> None:  # noqa: F811
    """Without the page, the review still comes after an early check-in,
    with no second question in it."""
    week.app.config.data["web"]["origin"] = ""
    _new_profile(week.db, week.his, confirm_time="18:30")
    week.run(evening_confirm.JOB, SUN20)
    text = text_of(week.run(sunday_summary.JOB, SUN20))
    assert "Week to 20 Sep" in text and "Still open" not in text, text


# ---------------------------------------------------------------- #130
def test_a_summary_caught_up_on_monday_counts_as_mondays_ask(week) -> None:  # noqa: F811
    """A Sunday summary that goes out on Monday morning stops Monday's
    check-in asking about the same days again."""
    week.app.config.data["web"]["origin"] = ORIGIN
    prof = targets_mod.profile_on(week.db, week.his, SUN20)
    slots = sc.slots_for(sunday_summary.JOB, week.his, prof, SUN20, week.db)
    week.clock.set_local(dt.datetime(2026, 9, 21, 9, 0, tzinfo=TZ))
    sc.Scheduler(week.app, [sunday_summary.JOB], lambda _p, _fn: None).execute(week.db, slots[0])
    late = [m for m in week.tg.sent if "Still open from the week" in str(m["text"])]
    assert late, "setup: the summary went out on Monday morning"
    assert week.db.state_get(f"catchup_asked:{week.his}") == isoday(MON21)
    evening = week.run(evening_confirm.JOB, MON21)
    assert "still open" not in text_of(evening).lower(), text_of(evening)


def test_a_summary_caught_up_on_monday_asks_about_the_sunday_too(week) -> None:  # noqa: F811
    week.app.config.data["web"]["origin"] = ""
    _seed_week(week.db, week.his, week.ing, SAT19, status="accepted", decision="accepted")
    days.close_day(week.db, week.his, SUN20)
    prof = targets_mod.profile_on(week.db, week.his, SUN20)
    slots = sc.slots_for(sunday_summary.JOB, week.his, prof, SUN20, week.db)
    week.clock.set_local(dt.datetime(2026, 9, 21, 9, 0, tzinfo=TZ))
    sc.Scheduler(week.app, [sunday_summary.JOB], lambda _p, _fn: None).execute(week.db, slots[0])
    late = [m for m in week.tg.sent if "Still open from the week" in str(m["text"])][-1]
    assert "7 not answered" in text_of(late), text_of(late)
    assert "Still open from the week: Mon, Tue, Wed, Thu, Fri, Sat and Sun." in text_of(late)
    assert "All 7 as planned" in _labels(late), _labels(late)
    evening = week.run(evening_confirm.JOB, MON21)
    assert "still open" not in text_of(evening).lower(), text_of(evening)
    done = week.tap(_week_button(late))
    assert "Recorded as planned: Mon, Tue, Wed, Thu, Fri, Sat, Sun." in text_of(done)
    assert _confirmed(week.db, week.his, SUN20)


def test_a_summary_caught_up_before_day_close_counts_the_sunday_it_names(week) -> None:  # noqa: F811
    week.app.config.data["web"]["origin"] = ""
    _seed_week(week.db, week.his, week.ing, SAT19, status="accepted", decision="accepted")
    assert days.latest_confirmation(week.db, week.his, SUN20) is None, "setup: no row for Sunday"
    prof = targets_mod.profile_on(week.db, week.his, SUN20)
    slots = sc.slots_for(sunday_summary.JOB, week.his, prof, SUN20, week.db)
    week.clock.set_local(dt.datetime(2026, 9, 21, 9, 0, tzinfo=TZ))
    sc.Scheduler(week.app, [sunday_summary.JOB], lambda _p, _fn: None).execute(week.db, slots[0])
    late = [m for m in week.tg.sent if "Still open from the week" in str(m["text"])][-1]
    assert "Still open from the week: Mon, Tue, Wed, Thu, Fri, Sat and Sun." in text_of(late)
    assert "All 7 as planned" in _labels(late), _labels(late)
    assert "7 not answered" in text_of(late), text_of(late)


# ---------------------------------------------------------------- #132
def test_one_sunday_message_gives_one_count_of_the_open_days(week) -> None:  # noqa: F811
    """When the review's count of days not answered includes a day with
    no menu, the gap line names that day too."""
    week.app.config.data["web"]["origin"] = ORIGIN
    partner = _partner(week)
    _seed_week(week.db, partner, week.ing, MON14, status="accepted", decision="accepted")
    week.run(sunday_summary.JOB, SUN20)
    text = text_of(_to_partner(week)[-1])
    assert "6 not answered" in text, text
    assert "Still open from the week: Mon, Tue, Wed, Thu and Fri." in text, text
    assert "Sat had no menu" in text, "the sixth day is named, so the two counts agree"
    # and never sent to be recorded: with no menu, what she ate went in on top of the target
    #
    assert "Sat had no menu, so the tap leaves it out and it is not counted this week." in text
    assert "what you ate" not in text, text


# ---------------------------------------------------------------- #138
def test_one_open_day_reads_as_one_day(week) -> None:  # noqa: F811
    """With one open day the Sunday button names it, and the evening asks
    about it in the singular."""
    evening = week.run(evening_confirm.JOB, TUE15)
    assert "Also still open: Mon. Was it as planned too?" in text_of(evening), text_of(evening)
    _confirm_all_before(week, FRI18)
    msg = week.run(sunday_summary.JOB, SUN20)
    assert "Still open from the week: Fri." in text_of(msg), text_of(msg)
    assert "Fri as planned" in _labels(msg), _labels(msg)
    assert "All 1 as planned" not in _labels(msg)


# ---------------------------------------------------------------- #59
def test_answering_today_keeps_the_button_for_the_earlier_days(week) -> None:  # noqa: F811
    """After today's Not quite, the check-in keeps a button that records the
    earlier open days, and it leaves today and its question alone."""
    evening = week.run(evening_confirm.JOB, WED16)
    assert "Also still open: Mon, Tue" in text_of(evening), "setup"
    _tap_on(week, evening, "k:cfmx:20260916")
    kept = [t for t in _last_edit(week) if t.startswith("k:cfmall:")]
    assert len(kept) == 1, f"the earlier days' button went: {_last_edit(week)}"
    assert "k:cfm:20260916" in _last_edit(week) and "k:cfmx:20260916" in _last_edit(week)
    done = week.tap(kept[0])
    assert "Recorded as planned: Mon, Tue." in text_of(done), text_of(done)
    assert _confirmed(week.db, week.his, MON14) and _confirmed(week.db, week.his, TUE15)
    assert not _confirmed(week.db, week.his, WED16), "today was Not quite"
    state = week.db.one(
        "SELECT state_json FROM conversation_state WHERE person_id = ?", (week.his,)
    )
    assert state is not None and json.loads(state["state_json"])["day"] == isoday(WED16), (
        "'What changed?' about today is still waiting for its answer"
    )


def test_as_planned_today_keeps_the_earlier_days_button_named(week) -> None:  # noqa: F811
    """After today's As planned, one earlier open day keeps a button that
    names it."""
    _confirm_all_before(week, TUE15)
    evening = week.run(evening_confirm.JOB, WED16)
    _tap_on(week, evening, "k:cfm:20260916")
    edit = week.tg.edited[-1]["reply_markup"]["inline_keyboard"]
    labels = [b["text"] for row in edit for b in row]
    assert "Tue as planned" in labels, labels


def test_a_check_in_that_never_asked_gets_no_button_for_the_earlier_days(week) -> None:  # noqa: F811
    """When the Sunday summary asked about the open days first, answering
    the evening check-in (which left them out) does not add a button for them."""
    week.run(sunday_summary.JOB, SUN20)
    evening = week.run(evening_confirm.JOB, SUN20)
    assert "still open" not in text_of(evening).lower(), "setup: the summary asked"
    _tap_on(week, evening, "k:cfm:20260920")
    assert not [t for t in _last_edit(week) if t.startswith("k:cfmall:")], _last_edit(week)


# ---------------------------------------------------------------- #60
def test_the_sunday_tap_takes_its_button_off_and_puts_no_weekday_buttons_on(week) -> None:  # noqa: F811
    """Tapping the Sunday button clears the summary's keyboard rather than
    leaving unlabelled As planned / Not quite buttons for one weekday."""
    msg = week.run(sunday_summary.JOB, SUN20)
    week.tap(_week_button(msg))
    assert _last_edit(week) == [], f"the summary now carries {_last_edit(week)}"


# ---------------------------------------------------------------- #116
def test_the_sunday_taps_reply_carries_mondays_meals(week) -> None:  # noqa: F811
    """The reply to the Sunday tap offers tomorrow's meals, not those of the
    day after the last open day."""
    _seed_week(week.db, week.his, week.ing, MON21, status="accepted", decision="accepted")
    msg = week.run(sunday_summary.JOB, SUN20)
    done = week.tap(_week_button(msg))
    meals = [t for t in taps(done) if t.startswith("k:meal:")]
    assert meals, f"no meal buttons on the reply: {taps(done)}"
    assert all(t.endswith(":20260921") for t in meals), meals


def test_an_old_cards_tap_carries_no_meals_for_a_day_long_past(week) -> None:  # noqa: F811
    """A catch-up tapped days after its evening offers no stale meals."""
    week.run(evening_confirm.JOB, WED16)
    week.at(dt.date(2026, 9, 19), "10:00")
    done = week.tap("k:cfmall:20260916")
    assert "Recorded as planned" in text_of(done)
    assert not [t for t in taps(done) if t.startswith("k:meal:")], taps(done)


# ---------------------------------------------------------------- #117
def _onboarding(w) -> None:
    w.db.execute(
        "INSERT INTO conversation_state(person_id, state_json, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(person_id) DO UPDATE SET state_json = excluded.state_json, "
        "updated_at = excluded.updated_at",
        (w.his, json.dumps({"flow": "onboarding", "step": "sex"}), w.clock.utc_stamp()),
    )


def test_the_week_button_never_deletes_an_interview_in_progress(week) -> None:  # noqa: F811
    """'All N days as planned' leaves a restarted onboarding interview where
    it was."""
    week.run(evening_confirm.JOB, WED16)
    _onboarding(week)
    week.tap("k:cfmall:20260916")
    assert _confirmed(week.db, week.his, MON14), "setup: the tap recorded the days"
    assert convstate.open_flow(week.db, week.his, clock=week.clock) == "onboarding"


def test_as_planned_never_deletes_an_interview_in_progress(week) -> None:  # noqa: F811
    """The single As planned leaves the interview's answers alone too."""
    week.run(evening_confirm.JOB, WED16)
    _onboarding(week)
    week.tap("k:cfm:20260916")
    assert _confirmed(week.db, week.his, WED16)
    assert convstate.open_flow(week.db, week.his, clock=week.clock) == "onboarding"


# ---------------------------------------------------------------- #77
def _summary_text(w) -> str:
    shown = w.page("/api/summary/weekly?week=2026-09-14")
    assert shown["text"] is not None
    return str(shown["text"]["text_html"])


def test_the_kept_review_catches_up_with_the_sunday_tap(week) -> None:  # noqa: F811
    """Once the Sunday button has recorded the open days, the review the
    message links to says so."""
    week.app.config.data["web"]["origin"] = ORIGIN
    msg = week.run(sunday_summary.JOB, SUN20)
    assert "Days recorded: 0, 6 not answered" in _summary_text(week), "setup"
    week.tap(_week_button(msg))
    assert "Days recorded: 5, 1 not answered" in _summary_text(week), _summary_text(week)


def test_the_kept_review_catches_up_with_the_pages_tap(week) -> None:  # noqa: F811
    """The page's one tap for the open days refreshes the kept review too."""
    week.app.config.data["web"]["origin"] = ORIGIN
    week.run(sunday_summary.JOB, SUN20)
    week.at(SUN20, "21:00")
    client = _client(week)
    got = client.post(
        "/api/today/confirm",
        json={"as_planned": True, "day": isoday(FRI18), "with_open": True},
        headers=_post_headers(client),
    )
    assert got.status_code == 200, got.text
    assert "Days recorded: 5, 1 not answered" in _summary_text(week), _summary_text(week)


def test_the_kept_review_catches_up_with_an_off_plan_answer(week) -> None:  # noqa: F811
    week.app.config.data["web"]["origin"] = ORIGIN
    _targets(week, MON14)
    week.run(sunday_summary.JOB, SUN20)
    assert "Days recorded: 0, 6 not answered" in _summary_text(week), "setup"
    week.run(evening_confirm.JOB, SUN20)
    week.tap("k:cfmx:20260920")
    said = _accept(week, week.say("pizza instead of dinner"))
    assert "Sun 20 Sep is now 2850 kcal" in said, said
    assert "Days recorded: 1, 6 not answered" in _summary_text(week), _summary_text(week)


def test_the_kept_review_catches_up_with_a_day_named_in_chat(
    week,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _model_logs(monkeypatch, WED16)
    week.app.config.data["web"]["origin"] = ORIGIN
    week.run(sunday_summary.JOB, SUN20)
    week.at(SUN20, "19:30")
    week.say("Wednesday: kebab instead of dinner")
    said = _accept(week, week.tap("k:mslotok:20260916"))
    assert "Wed 16 Sep is now" in said, said
    assert "Days recorded: 1, 5 not answered" in _summary_text(week), _summary_text(week)


# ---------------------------------------------------------------- #79
def test_the_page_offers_only_the_open_days_it_can_still_record(week) -> None:  # noqa: F811
    """Looking back at a day, the page's open days are only those a tap can
    still record, so the button's count is what gets recorded."""
    week.at(TUE22, "09:00")
    shown = week.page("/api/today?date=2026-09-18")
    assert shown["open_days"] == ["2026-09-15", "2026-09-16", "2026-09-17"], shown["open_days"]
    client = _client(week)
    got = client.post(
        "/api/today/confirm",
        json={"as_planned": True, "day": isoday(FRI18), "with_open": True},
        headers=_post_headers(client),
    )
    assert got.status_code == 200, got.text
    assert got.json()["also"] == shown["open_days"]
    assert not _confirmed(week.db, week.his, MON14)
    for d in (TUE15, WED16, THU17, FRI18):
        assert _confirmed(week.db, week.his, d), d


# ---------------------------------------------------------------- #143
def test_a_reset_keeps_the_weekly_reviews_like_the_messages_that_carried_them(week) -> None:  # noqa: F811
    """Reset-onboarding keeps the weekly reviews, as it keeps the Sunday
    messages, so the Summary tab never goes back to an older week."""
    assert "weekly_reviews" in reset.KEPT and "weekly_reviews" not in reset.TOLD
    week.app.config.data["web"]["origin"] = ORIGIN
    _confirm_all_before(week, SAT19)
    assert week.run(sunday_summary.JOB, SUN20) is None, "setup: nothing open, nothing sent"
    reset.reset_onboarding(week.db, "owner", clock=week.clock)
    left = week.db.scalar("SELECT COUNT(*) FROM weekly_reviews WHERE person_id = ?", (week.his,))
    assert left == 1
    assert "Week to 20 Sep" in _summary_text(week)
