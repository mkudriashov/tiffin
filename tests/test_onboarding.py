"""The onboarding interview end to end through the poller, with buttons and text."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from tiffin import onboarding as ob
from tiffin.app import App
from tiffin.clock import FrozenClock
from tiffin.db import Db
from tiffin.scheduler import Scheduler, slots_for
from tiffin.secrets import Secrets
from tiffin.telegram.poller import Poller

from .fakes import FakeTelegram

OWNER_CHAT = 1000001


@pytest.fixture
def bot(fixture_db: Db, two_persons, frozen_clock: FrozenClock, config, tmp_path: Path):
    secrets_dir = tmp_path / "s"
    secrets_dir.mkdir()
    (secrets_dir / "telegram-token-dev.txt").write_text("1:t", encoding="utf-8")
    tg = FakeTelegram()
    app = App(
        config,
        Secrets(secrets_dir),
        clock=frozen_clock,
        transport=tg,
        gateway_factory=lambda _app, _db: None,  # no model in the onboarding tests
    )
    handlers = app.handlers(fixture_db)
    poller = Poller(
        app.api(),
        fixture_db,
        app.registry(fixture_db),
        handlers.handle,
        clock=frozen_clock,
        sleeper=lambda s: None,
    )
    state = {"n": 0}

    def say(text: str) -> str:
        state["n"] += 1
        tg.queue_update(tg.text_update(state["n"], OWNER_CHAT, text))
        poller.run_once()
        return tg.sent[-1]["text"] if tg.sent else ""

    def tap(step: str, value: str) -> str:
        state["n"] += 1
        tg.queue_update(
            tg.callback_update(
                state["n"], OWNER_CHAT, f"k:ob:{step}:{value}", message_id=state["n"]
            )
        )
        poller.run_once()
        return tg.sent[-1]["text"] if tg.sent else ""

    return app, tg, say, tap


def last_keyboard(tg: FakeTelegram) -> list[str]:
    kb = tg.sent[-1].get("reply_markup", {}).get("inline_keyboard", [])
    return [b["callback_data"] for row in kb for b in row]


def test_household_units_are_accepted_in_the_opening_questions(bot, fixture_db: Db) -> None:
    app, _tg, say, tap = bot
    app.config.data["units"] = {"weight": "lb", "height": "in"}
    say("/onboard")
    tap("cons", "0")
    tap("sex", "m")
    height = say("1990-03-15")
    assert "inches" in height
    weight = say("72")
    assert "lb" in weight
    say("180")
    answers = {
        row["step"]: json.loads(row["answer_json"])
        for row in fixture_db.all("SELECT step, answer_json FROM onboarding_answers")
    }
    assert answers["hcm"] == pytest.approx(182.88)
    assert answers["wkg"] == pytest.approx(81.6466266)


def test_no_model_starts_with_target_facts(bot) -> None:
    app, _tg, say, _tap = bot
    app.config.data["model"]["provider"] = "none"
    assert "Sex" in say("/start")


def test_provider_change_during_interview_requires_new_consent(bot) -> None:
    app, _tg, say, tap = bot
    say("/start")
    tap("cons", "1")
    tap("sex", "m")
    app.config.data["model"]["provider"] = "openai"
    assert "OpenAI" in say("1990-03-15")
    assert "Height" in tap("cons", "0")


def test_first_targets_then_resumable_followups(bot, fixture_db: Db, two_persons) -> None:
    app, _tg, say, tap = bot
    pid = two_persons["owner"]
    first = say("/onboard")
    assert first.startswith("<b>1/7") and "Anthropic" in first
    assert "Sex" in tap("cons", "0")
    assert "Date of birth" in tap("sex", "m")
    assert "YYYY-MM-DD" in say("15 march")  # invalid -> re-ask stays on dob
    assert "Height" in say("1990-03-15")
    assert "weight" in say("183").lower()
    assert "Activity" in say("100.9")
    assert "Rate of loss" in tap("act", "1.55")
    done = tap("rate", "0.7")
    assert done.startswith("<b>Profile saved.</b>") and "This week:" in done
    assert "P 202 g" in done and "/followup" in done

    prof = fixture_db.latest("profiles", person_id=pid)
    assert prof is not None
    assert prof["sex"] == "m" and prof["height_cm"] == 183.0
    assert prof["morning_time"] == "07:30" and prof["confirm_time"] == "20:30"
    assert json.loads(prof["refused_foods"]) == []
    assert prof["goal_weight_kg"] is None
    assert (
        fixture_db.scalar("SELECT COUNT(*) FROM planned_sessions WHERE person_id = ?", (pid,)) == 0
    )
    assert fixture_db.scalar("SELECT granted FROM consents WHERE person_id = ?", (pid,)) == 0
    assert fixture_db.scalar("SELECT weight_kg FROM weigh_ins WHERE person_id = ?", (pid,)) == 100.9
    assert fixture_db.scalar("SELECT source FROM targets WHERE person_id = ?", (pid,)) == "formula"
    assert (
        fixture_db.scalar("SELECT COUNT(*) FROM onboarding_answers WHERE person_id = ?", (pid,))
        == 7
    )
    assert fixture_db.scalar("SELECT COUNT(*) FROM conversation_state") == 0
    assert app.registry(fixture_db).require("owner").tone == "neutral"
    assert "onboarded" in say("/onboard")
    assert "How do you want to plan" in say("/followup")
    assert "Saved" in say("/followup pln auto")
    assert "message" in say("/followup")


def test_followups_arrive_on_later_days_and_skips_can_be_answered(bot, fixture_db, frozen_clock):
    app, tg, say, tap = bot
    say("/start")
    tap("cons", "0")
    tap("sex", "f")
    say("1990-03-15")
    say("168")
    say("70")
    tap("act", "1.375")
    tap("rate", "0.5")

    from tiffin.jobs import profile_followup
    from tiffin.targets import profile_on

    person = app.registry(fixture_db).require("owner")
    scheduler = Scheduler(app, [profile_followup.JOB], lambda _pid, _task: None)
    today = frozen_clock.today_local()

    def run(day: dt.date) -> str:
        frozen_clock.set_local(dt.datetime.combine(day, dt.time(11)))
        profile = profile_on(fixture_db, person.id, day)
        slot = slots_for(profile_followup.JOB, person.id, profile, day, fixture_db)[0]
        return scheduler.execute(fixture_db, slot)

    assert run(today) == "skipped"
    assert run(today + dt.timedelta(days=1)) == "ok"
    assert "How do you want to plan" in tg.sent[-1]["text"]
    assert "/followup avoid" in tg.sent[-1]["text"]
    assert len(tg.sent[-1]["text"].splitlines()) <= 10
    assert fixture_db.scalar("SELECT COUNT(*) FROM messages_out WHERE kind = 'followup'") == 1
    assert "Saved" in say("/followup avoid celery")
    assert "Skipped" in say("/followup pln skip")
    assert "pln" in ob.answered_followups(fixture_db, person.id)[0]
    assert "pln" not in ob.provided_followups(fixture_db, person.id)
    assert run(today + dt.timedelta(days=2)) == "ok"
    assert "choose next week" in tg.sent[-1]["text"]
    assert "Saved" in say("/followup pln off")
    assert fixture_db.latest("profiles", person_id=person.id)["planning"] == "off"

    from tiffin.jobs import morning_targets

    next_day = today + dt.timedelta(days=3)
    frozen_clock.set_local(dt.datetime.combine(next_day, dt.time(8)))
    morning = Scheduler(app, [morning_targets.JOB], lambda _pid, _task: None)
    profile = profile_on(fixture_db, person.id, next_day)
    slot = slots_for(morning_targets.JOB, person.id, profile, next_day, fixture_db)[0]
    assert morning.execute(fixture_db, slot) == "degraded"
    notices = fixture_db.all("SELECT text_html FROM messages_out WHERE kind = 'profile_default'")
    assert len(notices) == 1 and "weekly message rhythm" in notices[0]["text_html"]
    day_after = next_day + dt.timedelta(days=1)
    frozen_clock.set_local(dt.datetime.combine(day_after, dt.time(8)))
    profile = profile_on(fixture_db, person.id, day_after)
    slot = slots_for(morning_targets.JOB, person.id, profile, day_after, fixture_db)[0]
    assert morning.execute(fixture_db, slot) == "degraded"
    assert (
        fixture_db.scalar("SELECT COUNT(*) FROM messages_out WHERE kind = 'profile_default'") == 1
    )


def test_paused_old_interview_keeps_answers_and_finishes_short_flow(bot, fixture_db):
    app, _tg, say, _tap = bot
    person = app.registry(fixture_db).require("owner")
    state = {
        "flow": "onboarding",
        "paused": True,
        "step": "kit",
        "answers": {
            "cons": "0",
            "sex": "m",
            "dob": "1990-03-15",
            "hcm": 183.0,
            "wkg": 100.0,
            "act": 1.55,
            "rate": 0.7,
            "refu": ["celery"],
        },
    }
    fixture_db.state_set(f"onboarding_parked:{person.id}", json.dumps(state))
    assert "Anthropic" in say("/onboard")
    assert "Profile saved" in _tap("cons", "0")
    profile = fixture_db.latest("profiles", person_id=person.id)
    assert json.loads(profile["refused_foods"]) == ["celery"]
    assert fixture_db.scalar("SELECT COUNT(*) FROM targets WHERE person_id = ?", (person.id,)) == 1


def test_abandoned_answers_do_not_hide_followups(bot, fixture_db):
    app, _tg, say, tap = bot
    person = app.registry(fixture_db).require("owner")
    fixture_db.insert(
        "onboarding_answers",
        person_id=person.id,
        step="refu",
        answer_json=json.dumps(["celery"]),
    )
    say("/onboard")
    tap("cons", "0")
    tap("sex", "m")
    say("1990-03-15")
    say("183")
    say("100")
    tap("act", "1.55")
    tap("rate", "0.7")
    answered, _ = ob.answered_followups(fixture_db, person.id)
    assert "refu" not in answered
    assert fixture_db.latest("profiles", person_id=person.id)["refused_foods"] == "[]"


def test_restart_keeps_optional_settings_that_are_not_asked_again(bot, fixture_db):
    app, _tg, say, tap = bot
    say("/onboard")
    tap("cons", "0")
    tap("sex", "m")
    say("1990-03-15")
    say("183")
    say("100")
    tap("act", "1.55")
    tap("rate", "0.7")
    say("/followup refu celery")
    say("/followup kit hob")
    say("/set morning 08:00")
    say("/onboard restart")
    tap("cons", "0")
    tap("sex", "m")
    say("1990-03-15")
    say("183")
    say("100")
    tap("act", "1.55")
    assert "Profile saved" in tap("rate", "0.7")
    profile = fixture_db.latest("profiles", person_id=app.registry(fixture_db).require("owner").id)
    assert json.loads(profile["refused_foods"]) == ["celery"]
    assert json.loads(profile["equipment_json"]) == ["hob"]
    assert profile["morning_time"] == "08:00"


def test_week_shape_and_measurements_can_be_set_after_first_targets(bot, fixture_db):
    app, _tg, say, tap = bot
    say("/start")
    tap("cons", "0")
    tap("sex", "m")
    say("1990-03-15")
    say("183")
    say("100")
    tap("act", "1.55")
    tap("rate", "0.7")
    assert "Saved" in say("/followup shopping-days twice")
    assert "Saved" in say("/followup measurements mon")
    from tiffin.food import weekplan

    plan = weekplan.of(fixture_db, app.registry(fixture_db).require("owner").id)
    assert plan.shops == ((0, 1, 2), (3, 4)) and plan.check_day == 0


def test_later_pauses_and_onboard_resumes(bot, fixture_db: Db) -> None:
    _app, _tg, say, tap = bot
    say("/onboard")
    tap("cons", "0")
    tap("sex", "m")
    paused = say("later")
    assert "Paused" in paused
    assert "Noted" in say("hello?")  # free text falls through while paused
    resumed = say("/onboard")
    assert "Date of birth" in resumed and resumed.startswith("<b>3/")
    assert fixture_db.scalar("SELECT COUNT(*) FROM conversation_state") == 1


def test_start_opens_the_interview_rather_than_naming_a_command(bot) -> None:
    """Pressing Start is the first thing anyone does with a new bot; it must go somewhere."""
    _app, _tg, say, _tap = bot
    first = say("/start")
    assert first.startswith("<b>1/") and "Anthropic" in first


def test_start_resumes_an_interview_already_under_way(bot) -> None:
    _app, _tg, say, tap = bot
    say("/onboard")
    tap("cons", "0")
    tap("sex", "m")
    assert "Date of birth" in say("/start")


def test_restart_and_stale_button(bot, fixture_db: Db) -> None:
    _app, _tg, say, tap = bot
    say("/onboard")
    tap("cons", "0")
    tap("sex", "f")
    stale = tap("sex", "m")
    assert "earlier question" in stale
    fresh = say("restart")
    assert fresh.startswith("<b>1/") and "Anthropic" in fresh


def test_parsers() -> None:
    plan = ob.parse_plan("mon climb 90 at 18:30; thursday Strength; sun rest", {})
    assert plan[0] == {"weekday": 0, "kind": "climb", "target_minutes": 90, "start_time": "18:30"}
    assert plan[3]["kind"] == "strength" and plan[3]["target_minutes"] == 60
    assert plan[6]["kind"] == "rest" and plan[1]["kind"] == "rest"
    with pytest.raises(ob.Invalid):
        ob.parse_plan("mon climb 999", {})
    assert ob._list("a, b;c\nd", {}) == ["a", "b", "c", "d"]
    with pytest.raises(ob.Invalid):
        ob._birth_date("2020-01-01", {})
    assert ob._time("7.05", {}) == "07:05"


def test_the_kitchen_question_reads_what_people_actually_type() -> None:
    """A menu that says "roast 40 minutes" is no use to someone with an air fryer."""
    from tiffin.onboarding import parse_kit

    assert parse_kit("oven, hob, air fryer", {}) == ["air_fryer", "hob", "oven"]
    assert parse_kit("Oven and a microwave", {}) == ["microwave", "oven"]
    assert parse_kit("none", {}) == []
    # something not on the list is kept as written: they know their own kitchen
    got = parse_kit("hob, tagine, rice cooker", {})
    assert "hob" in got and "tagine" in got and "rice cooker" in got


def test_the_kitchen_reaches_the_menu_prompt() -> None:
    import json

    from tiffin.food.menu import equipment_of
    from tiffin.model import prompts

    profile = {"equipment_json": json.dumps(["air_fryer", "hob"])}
    assert equipment_of(profile) == ["air fryer", "hob"]  # type: ignore[arg-type]
    assert equipment_of(None) == [], "unstated is empty, not a guess"
    assert equipment_of({"equipment_json": "not json"}) == []  # type: ignore[arg-type]

    text = prompts.menu_propose(
        ingredients=["1. Chicken"], days=[], variety={}, equipment=["air fryer", "hob"]
    )
    assert "air fryer" in text
    assert "An air fryer is not an oven" in text, "the rule travels with the fact"


def test_no_name_is_invented_for_someone_config_does_not_name(fixture_db: Db, config) -> None:
    """Her very first message read "Hello Her", and persons is append-only so it stuck."""
    from tiffin.persons import PersonRegistry

    fixture_db.execute("DELETE FROM telegram_identities")
    fixture_db.execute("DELETE FROM persons")
    PersonRegistry(fixture_db, config).ensure_from_config()
    partner = fixture_db.one("SELECT display_name FROM persons WHERE slug = 'partner'")
    assert partner is not None and partner["display_name"] == "", "blank is honest; 'Her' is wrong"


def test_a_greeting_copes_with_a_blank_name() -> None:
    from tiffin.persons import Person
    from tiffin.telegram.handlers import _greeting

    def person(name: str) -> Person:
        return Person(
            id=1,
            slug="partner",
            display_name=name,
            tone="warm",
            chat_id=None,
            tailscale_login=None,
            erased=False,
        )

    named, blank = person("Sam"), person("")
    assert _greeting(named) == " Hello Sam."
    assert _greeting(blank) == " Hello."


def test_consent_can_be_changed_without_redoing_the_interview(bot, fixture_db: Db) -> None:
    _app, tg, say, _tap = bot
    fixture_db.insert(
        "consents",
        person_id=1000001 and 1,
        kind="anthropic_processing",
        granted=1,
        wording_version="v1",
    )
    shown = say("/consent")
    assert "Claude processing is ON" in shown
    buttons = [
        b["callback_data"] for row in tg.sent[-1]["reply_markup"]["inline_keyboard"] for b in row
    ]
    # one button per switch, each the change that applies: Claude processing (on, so "off"), and
    # since 26 Sep the shared weekend (off, so "on")
    assert buttons == ["k:consent:0", "k:consent:share:1"], "the change that applies, per switch"
    assert "Shared weekend menu is OFF" in shown
