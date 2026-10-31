"""Moments where a correct rule arrives at the wrong time.

Nothing here is a validation gap. Every story stands inside one minute of a real day - just
after midnight, 22:30 on an ordinary Monday, 19:00 on a Sunday that is not over, the six
minutes a menu build holds the worker - and asks what the person actually sees. The shape
being hunted is a system that is right for the moment it was designed for and useless in the
moment the person is in.
"""

from __future__ import annotations

import datetime as dt
import threading
import time
from pathlib import Path

import pytest

from tiffin import body, days
from tiffin import scheduler as sc
from tiffin.app import App
from tiffin.clock import FrozenClock, isoday
from tiffin.db import Db
from tiffin.energy import Macros
from tiffin.food import menu as menu_mod
from tiffin.jobs import (
    day_close,
    evening_confirm,
    menu_propose,
    shopping_list,
    sunday_summary,
    weekly_calibration,
)
from tiffin.runtime import Runtime
from tiffin.secrets import Secrets
from tiffin.telegram.poller import Poller

from .fakes import FakeTelegram
from .test_menu import MONDAY as COOK_MONDAY
from .test_menu import FakeGateway, proposal_for
from .test_menu import world as cook_world  # noqa: F401 (a fixture, registered by this name)

OWNER_CHAT = 1000001
MONDAY = dt.date(2026, 9, 7)
TUESDAY = dt.date(2026, 9, 8)
SUNDAY = dt.date(2026, 9, 13)


def wait_for(predicate, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def make_app(config, frozen_clock: FrozenClock, tmp_path: Path, tg: FakeTelegram) -> App:
    secrets_dir = tmp_path / "s"
    secrets_dir.mkdir(exist_ok=True)
    (secrets_dir / "telegram-token-dev.txt").write_text("1:t", encoding="utf-8")
    return App(config, Secrets(secrets_dir), clock=frozen_clock, transport=tg)


@pytest.fixture
def onboarded(fixture_db: Db, two_persons, frozen_clock: FrozenClock, config, tmp_path: Path):
    """The owner, on the schema's own default times, with a Monday session and this week's targets.

    confirm_time, quiet_start and quiet_end are deliberately NOT given: the point of several
    stories below is what the defaults do to each other, so the profile takes them.
    """
    pid = two_persons["owner"]
    fixture_db.insert(
        "profiles",
        person_id=pid,
        effective_from="2026-09-01",
        sex="m",
        birth_date="1990-03-15",
        height_cm=183.0,
        activity_factor=1.55,
        kcal_floor=1800,
    )
    body.record_weigh_in(fixture_db, pid, dt.date(2026, 9, 6), 100.9, "manual")
    fixture_db.insert(
        "targets",
        person_id=pid,
        week_start="2026-09-07",
        kcal=2250,
        protein_g=202,
        fat_g=71,
        carb_g=200,
        fibre_g=30,
        source="formula",
    )
    plan = fixture_db.insert("training_plans", person_id=pid, effective_from="2026-09-01")
    fixture_db.insert(
        "planned_sessions", plan_id=plan, person_id=pid, weekday=0, kind="climb", target_minutes=90
    )
    tg = FakeTelegram()
    app = make_app(config, frozen_clock, tmp_path, tg)
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
        tg.queue_update(tg.text_update(n["i"], OWNER_CHAT, text))
        poller.run_once()
        return tg.sent[-1]["text"]

    def tap(data: str) -> str:
        n["i"] += 1
        tg.queue_update(tg.callback_update(n["i"], OWNER_CHAT, data, message_id=n["i"]))
        poller.run_once()
        return tg.sent[-1]["text"]

    def run_job(job: sc.Job, day: dt.date, *, index: int = 0) -> str:
        s = sc.Scheduler(app, [job], lambda p, fn: None)
        target = pid if job.scope == "each" else 0
        prof = fixture_db.one(
            "SELECT * FROM profiles WHERE person_id = ? ORDER BY id DESC LIMIT 1", (pid,)
        )
        slots = sc.slots_for(job, target, prof, day, fixture_db)
        assert slots, f"no slot for {job.name} on {day}"
        return s.execute(fixture_db, slots[index])

    return pid, app, tg, say, tap, run_job


# ---------------------------------------------------------------- 1. the worker queue
def test_01_a_long_menu_build_makes_a_bare_status_read_wait_behind_it(
    config, frozen_clock: FrozenClock, tmp_path: Path
) -> None:
    """A model call is holding the owner's worker. He sends /today, which is one SELECT and a
    render.

    One queue per person and no fast lane, so every cheap read sits behind six minutes of model
    calls. From the phone a working tiffin and a dead one look identical, which is the reason
    /cook answers "this takes a minute" at all - but nothing else can get a word in while it does.
    """
    tg = FakeTelegram()
    app = make_app(config, frozen_clock, tmp_path, tg)
    app.bootstrap().close()
    db = app.open_db()
    try:
        pid = int(
            db.scalar("SELECT person_id FROM telegram_identities WHERE chat_id = ?", (OWNER_CHAT,))
        )
    finally:
        db.close()
    runtime = Runtime(app, heartbeat_s=60, tick_s=3600, jobs=[], serve_web=False)
    started, release = threading.Event(), threading.Event()

    def six_minutes_of_model_calls(_db: Db) -> None:
        started.set()
        release.wait(5.0)

    def today_answered() -> bool:
        return any("Day:" in m["text"] for m in tg.sent)

    runtime.start()
    try:
        runtime.dispatch_job(pid, six_minutes_of_model_calls)
        assert started.wait(3.0), "the build is on Alex's worker"
        tg.queue_update(tg.text_update(1, OWNER_CHAT, "/today"))
        while_busy = wait_for(today_answered, 2.0)
        release.set()
        once_free = wait_for(today_answered, 3.0)
    finally:
        release.set()
        runtime.stop()
    assert once_free, "the control: the same /today is answered the moment the build lets go"
    assert while_busy, "/today is a database read; it must not wait for a model call to finish"


# ---------------------------------------------------------------- 2. just after midnight
def test_02_confirm_sent_at_half_past_midnight_records_the_day_that_just_ended(
    onboarded, fixture_db: Db, frozen_clock: FrozenClock
) -> None:
    """He gets home from the pub at midnight and answers the evening check-in with /confirm.

    "Today" at 00:30 is thirty minutes old and has had no food in it. The day he actually
    lived is the one the check-in asked about, and day_close has already written it off as
    unconfirmed. /confirm is one of the three documented ways to close a day.
    """
    pid, _app, _tg, say, _tap, run_job = onboarded
    run_job(evening_confirm.JOB, MONDAY)  # 20:30, unanswered
    frozen_clock.set_local(dt.datetime(2026, 9, 8, 0, 5))
    run_job(day_close.JOB, TUESDAY)  # 00:05: Monday becomes unconfirmed
    frozen_clock.set_local(dt.datetime(2026, 9, 8, 0, 30))

    out = say("/confirm")
    monday = days.latest_confirmation(fixture_db, pid, MONDAY)
    tuesday = days.latest_confirmation(fixture_db, pid, TUESDAY)
    # The harm this story was written for: Tuesday, thirty minutes old and with no food in it,
    # was written confirmed at the full target - a fabricated 2250 kcal into the measured TDEE -
    # and `evening_confirm` then skipped Tuesday for ever as "already confirmed".
    assert tuesday is None or str(tuesday["status"]) != "confirmed", (
        f"/confirm at 00:30 recorded {TUESDAY} as eaten to target: {out!r}"
    )
    # and the remedy went further than this test first asked for: before dawn a bare /confirm is
    # about the day that just ended, which is the day the check-in asked about and the day he
    # actually lived. So Monday is now recorded rather than merely left alone.
    assert monday is not None and str(monday["status"]) == "confirmed", "the day he lived"
    assert "Mon 07 Sep" in out, f"and the reply says which day it took: {out!r}"


# ---------------------------------------------------------------- 3. the week is not over
def test_03_the_sunday_summary_counts_the_day_it_is_sent_on_as_a_miss(
    onboarded, fixture_db: Db, frozen_clock: FrozenClock
) -> None:
    """19:00 on Sunday, after a week in which he answered every single evening check-in.

    Sunday's own check-in is at 20:30 and day_close is at 00:05 on Monday, so at 19:00 Sunday
    cannot possibly be confirmed yet. The summary reads the whole Mon-Sun week and treats a
    day with no row as unconfirmed, so a perfect week is reported with a miss in it - every
    week, for ever.
    """
    pid, _app, tg, _say, _tap, run_job = onboarded
    for i in range(6):  # Mon to Sat, every one answered
        day = MONDAY + dt.timedelta(days=i)
        body.record_weigh_in(fixture_db, pid, day, 100.9 - i * 0.05, "manual")
        days.confirm_day(fixture_db, pid, day, basis="menu", macros=Macros(2250, 202, 71, 200, 30))
    frozen_clock.set_local(dt.datetime(2026, 9, 13, 19, 0))
    assert run_job(sunday_summary.JOB, SUNDAY) == "ok"

    text = tg.sent[-1]["text"]
    line = next(x for x in text.splitlines() if x.startswith("Days recorded:"))
    assert "not answered" not in line or "0 not answered" in line, (
        f"the day the summary is sent on is not a miss: {line!r}"
    )


# ---------------------------------------------------------------- 4. the list already went out
def test_04_a_menu_replaced_after_the_shopping_list_went_out_is_not_shopped_from_stale(
    cook_world,  # noqa: F811 (the imported fixture, requested by name)
    frozen_clock: FrozenClock,
) -> None:
    _pid, db, _tg, gateways, say, _tap, run_job = cook_world
    segment = menu_mod.Segment(COOK_MONDAY, dt.date(2026, 9, 18))
    # the replacement has to actually buy something different, or there is nothing for a
    # change note to report - yoghurt (used only in breakfast and the snack) drops out of the
    # whole week once both slots stop naming it; milk (already needed for the snack) covers it
    replacement = proposal_for(segment)
    for day in replacement["days"]:
        for slot in day["slots"]:
            if slot["slot"] == "breakfast":
                slot["recipe"]["name"] = "Oats with milk"
                slot["recipe"]["ingredients"] = [{"i": 5, "g": 80}, {"i": 4, "g": 320}]
            elif slot["slot"] == "snack":
                slot["recipe"]["name"] = "Milk and whey"
                slot["recipe"]["ingredients"] = [{"i": 3, "g": 200}, {"i": 7, "g": 20}]
    # two answers a build: the fixture's dishes carry 70% of the day's carbohydrate, under the
    # floor, so each build asks once more before keeping what it has
    gateways.append(
        FakeGateway([proposal_for(segment), proposal_for(segment), replacement, replacement])
    )
    assert run_job(menu_propose.JOB, dt.date(2026, 9, 12)) == "ok"
    first = int(db.scalar("SELECT MAX(id) FROM menus"))
    assert menu_mod.decision_of(db, first) == "accepted", "in force the moment it was sent"

    frozen_clock.set_local(dt.datetime(2026, 9, 13, 18, 0))
    assert run_job(shopping_list.JOB, dt.date(2026, 9, 13)) == "ok"
    assert db.one("SELECT id FROM messages_out WHERE kind = 'shopping_list' AND status = 'sent'")

    frozen_clock.set_local(dt.datetime(2026, 9, 13, 20, 0))
    say("/cook replace")
    second = int(db.scalar("SELECT MAX(id) FROM menus"))
    assert second != first, "a replacement menu was built, and it too is already in force"
    assert menu_mod.decision_of(db, second) == "accepted"
    assert db.one(
        "SELECT id FROM messages_out WHERE kind = 'shopping_list' AND due_key = ? "
        "AND status = 'sent'",
        (isoday(COOK_MONDAY),),
    ), "the key a corrected list would need is already spent"

    listed = say("/shopping")
    assert "replaces the one already sent" in listed, (
        f"a stale list re-read after a replacement must say so: {listed!r}"
    )


# ---------------------------------------------------------------- 5. the outage over a Sunday
def test_05_a_sunday_lost_to_a_two_day_outage_is_never_caught_up_and_nobody_is_told(
    onboarded, fixture_db: Db, frozen_clock: FrozenClock
) -> None:
    """The box is down from Saturday night to Tuesday morning. Sunday's calibration never ran.

    weekly_calibration declares catch_up="always", which the scheduler documents as "no
    deadline: always run once due". due_slots only ever looks one day back, so a slot missed
    by two days is silently outside the window: no run_ledger row, no alert, no tdee row and
    no proposals. The week's targets then come from the formula as though nobody had ever
    weighed anything, and nothing anywhere says the measurement was skipped.
    """
    pid, app, tg, _say, _tap, _run_job = onboarded
    sched = sc.Scheduler(app, [weekly_calibration.JOB], lambda p, fn: None)
    prof = fixture_db.one("SELECT * FROM profiles ORDER BY id DESC LIMIT 1")
    sunday_slot = sc.slots_for(weekly_calibration.JOB, pid, prof, SUNDAY)[0]
    assert sunday_slot.deadline is None, "catch_up='always' means no deadline at all"

    frozen_clock.set_local(dt.datetime(2026, 9, 15, 10, 0))  # Tuesday, the box is back
    persons = sched.persons_with_profiles(fixture_db, dt.date(2026, 9, 15))
    due = sc.due_slots([weekly_calibration.JOB], persons, frozen_clock.now_local(), db=fixture_db)
    assert fixture_db.all("SELECT * FROM run_ledger WHERE job = 'weekly_calibration'") == []
    assert tg.sent == [], "and nothing told anyone the week was lost"
    assert [s.key for s in due] == [f"{isoday(SUNDAY)}T1730"], (
        "a slot with no deadline is still due when the box comes back"
    )


# ---------------------------------------------------------------- 7. the same second
def test_07_two_cook_messages_in_one_second_promise_a_menu_and_then_say_nothing(
    cook_world,  # noqa: F811 (the imported fixture, requested by name)
) -> None:
    """He sends "/cook more fish" and, a finger-twitch later, sends it again.

    The five-minute cooldown deliberately does not apply to a request carrying words, and the
    on-demand run claims its ledger slot as manual-<utc stamp to the second> - so the second
    message answers "This takes a minute", collides with the first slot, and dies with
    AlreadyDone escaping cmd_cook. Nothing follows the promise.
    """
    pid, db, tg, gateways, say, _tap, _run_job = cook_world
    tomorrow = dt.date(2026, 9, 13)  # Sunday: the weekend segment, and it starts tomorrow
    db.insert(
        "targets",
        person_id=pid,
        week_start="2026-09-07",
        kcal=2250,
        protein_g=200,
        fat_g=70,
        carb_g=200,
        fibre_g=30,
        source="formula",
    )
    one_day = menu_mod.Segment(tomorrow, tomorrow)
    gateways.append(FakeGateway([proposal_for(one_day), proposal_for(one_day)]))
    assert "Menu" in say("/cook more fish"), "the first one builds a menu"
    before = len(tg.sent)

    say("/cook more fish")
    new = [m["text"] for m in tg.sent[before:]]
    assert new, "the second message is answered at all"
    assert new[0].startswith("Working out what to cook"), new[0]
    assert not any("went wrong" in t for t in new), new
    assert len(new) > 1, "it promised a menu in a minute and then said nothing at all"


# ---------------------------------------------------------------- 8. the six-week-old card
def test_08_a_session_button_on_a_six_week_old_card_records_that_day_anyway(
    onboarded, fixture_db: Db
) -> None:
    """He scrolls a long way back and taps a button on an evening card from six weeks ago.

    Telegram buttons never expire. The food buttons on that card refuse ("too far back to
    change now") and so does the manual session pair, because both go through the same
    seven-day guard. The matched-session pair on the very same keyboard has no guard at all,
    so it writes a confirmed session against a day inside a week that has been summarised,
    calibrated and closed.
    """
    pid, _app, _tg, _say, tap, _run_job = onboarded
    old = MONDAY - dt.timedelta(days=42)
    key = old.strftime("%Y%m%d")
    match_id = fixture_db.insert(
        "session_matches",
        person_id=pid,
        day=isoday(old),
        status="matched",
        minutes=85,
        icu_id="i-old",
    )
    assert "too far back" in tap(f"k:cfm:{key}"), "the food button on that card refuses"
    assert "too far back" in tap(f"k:sessm:{key}:done"), "so does the manual session button"

    tap(f"k:sess:{match_id}")
    latest = fixture_db.latest("session_matches", person_id=pid)
    assert latest is not None and str(latest["status"]) == "matched", (
        "a tap on a six-week-old card must not record a session for that day"
    )


# ---------------------------------------------------------------- 9. Sunday's tape question


# ---------------------------------------------------------------- 10. 22:30 every night
