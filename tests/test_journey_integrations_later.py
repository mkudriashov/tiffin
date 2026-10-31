"""Stories about missing integrations and the manual path they leave open."""

from __future__ import annotations

import datetime as dt
import shutil
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tiffin import body
from tiffin.app import App
from tiffin.clock import TZ, FrozenClock
from tiffin.db import Db
from tiffin.jobs import (
    day_close,
    evening_confirm,
    morning_targets,
    sunday_summary,
    weekly_calibration,
)
from tiffin.secrets import Secrets
from tiffin.telegram.poller import Poller
from tiffin.web.app import create_app

from .fakes import FakeTelegram
from .test_stories_jobs import Box

PARTNER_CHAT = 1000002
MONDAY = dt.date(2026, 9, 14)  # today
TUESDAY = dt.date(2026, 9, 15)
SUNDAY = dt.date(2026, 9, 20)


# ---------------------------------------------------------------- the world
@pytest.fixture
def frozen_clock() -> FrozenClock:
    """Use a fixed Monday evening for the integration journey."""
    return FrozenClock(dt.datetime(2026, 9, 14, 20, 30, tzinfo=TZ))


@pytest.fixture
def world(fixture_db: Db, two_persons, frozen_clock: FrozenClock, config, tmp_path: Path) -> Box:
    """Tiffin wired to fakes, exactly as test_stories_jobs builds it: the owner has an icu key
    file, she has none."""
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    (secrets_dir / "telegram-token-dev.txt").write_text("1:t", encoding="utf-8")
    (secrets_dir / "icu-key-owner.txt").write_text("k-owner", encoding="utf-8")
    config.paths.ensure()
    shutil.copyfile(fixture_db.path, config.paths.db)
    tg = FakeTelegram()
    holder: dict[str, Any] = {"gateway": None}
    app = App(
        config,
        Secrets(secrets_dir),
        clock=frozen_clock,
        transport=tg,
        gateway_factory=lambda _a, _d: holder["gateway"],
    )
    b = Box(app, fixture_db, tg, frozen_clock, holder)
    b.owner, b.partner = two_persons["owner"], two_persons["partner"]
    return b


def partner(b: Box) -> int:
    """The partner as she really is: onboarded, weighed, targets set, a 09:00 strength session on
    Mondays, no icu key and no tailnet identity."""
    b.profile(
        b.partner,
        sex="f",
        birth_date="1992-05-01",
        height_cm=168.0,
        activity_factor=1.45,
        kcal_floor=1400,
        steps_floor=10000,
    )
    body.record_weigh_in(b.db, b.partner, dt.date(2026, 9, 7), 66.4, "manual")
    body.record_weigh_in(b.db, b.partner, MONDAY, 66.0, "manual")
    for week in ("2026-09-14", "2026-09-21"):
        b.db.insert(
            "targets",
            person_id=b.partner,
            week_start=week,
            kcal=1600,
            protein_g=132,
            fat_g=47,
            carb_g=162,
            fibre_g=25,
            source="formula",
        )
    plan = b.db.insert("training_plans", person_id=b.partner, effective_from="2026-09-01")
    b.db.insert(
        "planned_sessions",
        plan_id=plan,
        person_id=b.partner,
        weekday=0,
        kind="strength",
        target_minutes=60,
        start_time="09:00",
    )
    return b.partner


def text_of(b: Box, kind: str, person_id: int) -> str:
    rows = b.outbound(kind, person_id)
    assert rows, f"no {kind} message was composed for person {person_id}"
    return str(rows[-1]["text_html"])


def tap_as(b: Box, chat_id: int, data: str) -> str:
    """One button press from a phone, through the real poller and handlers.

    Returns the reply the person was given. Dev never sends to her (`dropped_dev`), so the reply
    is read from messages_out, where it is recorded whatever the environment.
    """
    person = b.app.registry(b.db).by_chat_id(chat_id)
    assert person is not None
    before = int(b.db.scalar("SELECT COALESCE(MAX(id), 0) FROM messages_out") or 0)
    handlers = b.app.handlers(b.db)
    poller = Poller(
        b.app.api(),
        b.db,
        b.app.registry(b.db),
        handlers.handle,
        clock=b.clock,
        sleeper=lambda _s: None,
    )
    uid = 900 + len(b.tg.updates)
    b.tg.queue_update(b.tg.callback_update(uid, chat_id, data, message_id=uid))
    poller.run_once()
    rows = b.db.all(
        "SELECT text_html FROM messages_out WHERE id > ? AND person_id = ? ORDER BY id",
        (before, person.id),
    )
    return "\n".join(str(r["text_html"]) for r in rows)


# ================================================================ her: no intervals.icu
def test_01_partner_evening_check_in_does_not_wait_for_an_icu_partner_does_not_have(
    world: Box,
) -> None:
    """Monday 20:30, on the sofa after her 09:00 strength session. She has no intervals.icu
    account, so the check-in should simply ask whether she trained - not say 'nothing from
    intervals.icu yet', as if data might still arrive from somewhere."""
    pid = partner(world)
    assert world.run(evening_confirm.JOB, MONDAY, person_id=pid) == "ok"
    text = text_of(world, "evening_confirm", pid)
    assert "strength 60 min" in text, text
    assert "intervals.icu" not in text, "her check-in points at a sync she will never have: " + text


def test_02_partner_did_it_tap_is_the_session_and_sunday_counts_it(world: Box) -> None:
    """Monday 20:31 she taps "Did it" on the training row. With no watch data that tap is the
    only record there will ever be, so Sunday's summary has to count it."""
    pid = partner(world)
    world.run(evening_confirm.JOB, MONDAY, person_id=pid)
    reply = tap_as(world, PARTNER_CHAT, "k:sessm:20260914:done")
    assert "Session recorded" in reply, reply
    for offset in range(1, 7):  # 00:05 every night the day before is closed, as in prod
        world.run(day_close.JOB, MONDAY + dt.timedelta(days=offset), person_id=pid)
    assert world.run(sunday_summary.JOB, SUNDAY) == "ok"
    text = text_of(world, "sunday_summary", pid)
    assert "Sessions 1/1" in text, text


def test_03_partner_sunday_summary_says_why_there_was_no_recalibration_in_words(world: Box) -> None:
    """Sunday 19:00. She answered no check-ins, so there is no
    recalibration. The summary should tell her that in words she can act on - not
    'usable_days=0 &lt; 5', a variable name and an escaped operator (sent for real on 13 Sep)."""
    pid = partner(world)
    assert world.run(weekly_calibration.JOB, SUNDAY, person_id=pid) == "ok"
    assert world.run(sunday_summary.JOB, SUNDAY) == "ok"
    text = text_of(world, "sunday_summary", pid)
    assert "No recalibration this week" in text, text
    assert "usable_days" not in text and "&lt;" not in text, (
        "her summary shows the calibration's internal reason string: " + text
    )


def test_04_partner_evening_taps_alone_are_enough_to_recalibrate(world: Box) -> None:
    """Monday to Friday, 20:30 each evening, she taps "As planned" on the check-in. With no
    diary those taps are her whole food record, and five of them should be enough for Sunday's
    calibration to measure her."""
    pid = partner(world)
    for offset in range(5):
        day = MONDAY + dt.timedelta(days=offset)
        world.clock.set_local(dt.datetime.combine(day, dt.time(20, 30), tzinfo=TZ))
        world.run(evening_confirm.JOB, day, person_id=pid)
        reply = tap_as(world, PARTNER_CHAT, f"k:cfm:{day:%Y%m%d}")
        assert "recorded" in reply, reply
    body.record_weigh_in(world.db, pid, dt.date(2026, 9, 19), 65.7, "manual")
    world.clock.set_local(dt.datetime(2026, 9, 20, 17, 30, tzinfo=TZ))
    assert world.run(weekly_calibration.JOB, SUNDAY, person_id=pid) == "ok"
    row = world.db.one(
        "SELECT usable_days, tdee_measured, skipped_reason FROM tdee_calibrations "
        "WHERE person_id = ? ORDER BY id DESC LIMIT 1",
        (pid,),
    )
    assert row is not None and row["tdee_measured"] is not None, dict(row or {})


def test_05_the_page_link_in_partner_morning_message_opens_for_partner(world: Box) -> None:
    """Monday 07:30. Her morning message ends with a link to "The page". She has no Tailscale
    identity, so when she taps it the page sees a stranger. A link that only refuses her
    should not be in her message at all - or it should open."""
    pid = partner(world)
    world.clock.set_local(dt.datetime(2026, 9, 14, 7, 30, tzinfo=TZ))
    assert world.run(morning_targets.JOB, MONDAY, person_id=pid) == "ok"
    text = text_of(world, "morning_targets", pid)
    linked = "<a href=" in text
    world.config_set("web.root_path", "")
    world.config_set("web.secure_cookies", False)
    client = TestClient(create_app(world.app), base_url="http://testserver")
    # her phone is not on the tailnet as her: Serve adds no Tailscale-User-Login header
    page = client.get("/")
    assert not linked or page.status_code == 200, (
        f"her morning message links the page, and the page answers her {page.status_code} "
        f"{page.text[:80]!r}. Message: {text}"
    )


def test_08_partner_morning_steps_floor_is_not_a_target_nothing_counts(world: Box) -> None:
    """Monday 07:30. Her morning message sets 'Steps floor 10,000'. Nothing reads her steps -
    no watch, no icu - and nothing ever mentions them again. A floor with no counter is
    silence dressed as a target; the message should say her steps are not counted."""
    pid = partner(world)
    world.clock.set_local(dt.datetime(2026, 9, 14, 7, 30, tzinfo=TZ))
    world.run(morning_targets.JOB, MONDAY, person_id=pid)
    text = text_of(world, "morning_targets", pid)
    promised = "Steps floor" in text
    explained = "not counted" in text.lower() or "no step" in text.lower()
    assert not promised or explained, "a steps floor nobody can measure: " + text
