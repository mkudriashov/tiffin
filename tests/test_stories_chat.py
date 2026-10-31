"""User stories aimed at the conversational surface.

Each one is a single thing a person types or taps that is slightly off the happy path: a
command sent half way through a question, a voice note or file that needs a typed reply, and a
button from a message three weeks old. The rules under test are the house ones -
one conversation at a time, degrade to manual never to silence, no emoji, escaped not
rendered, and nothing he is sent ever mentions her.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest

from tiffin.app import App
from tiffin.clock import TZ, FrozenClock, isoday
from tiffin.db import Db
from tiffin.food import menu as menu_mod
from tiffin.jobs import morning_targets
from tiffin.secrets import Secrets
from tiffin.telegram.poller import Poller
from tiffin.telegram.render import check_text

from .fakes import FakeTelegram, err
from .test_chat import FakeGateway as ChatGateway
from .test_edge_cases import a_menu, answers, quiet_tap
from .test_menu import MONDAY, seed_catalogue, world  # noqa: F401

OWNER_CHAT = 1000001
PARTNER_CHAT = 1000002
SATURDAY = dt.date(2026, 9, 12)  # the day the `world` clock is frozen on, 18:00
SATURDAY_KEY = "20260912"


# ---------------------------------------------------------------- a bot that can be spoken to
@pytest.fixture
def bot(fixture_db: Db, two_persons, frozen_clock: FrozenClock, config, tmp_path: Path):
    """The `world` wiring plus a way to send a voice note and a file.

    `world` only exposes say() and tap(), and both drive the poller from a counter this file
    cannot see - so an update queued from outside it either collides or pushes the offset past
    every later message. Voice and documents need their own door.
    """
    pid = two_persons["owner"]
    frozen_clock.set_local(dt.datetime(2026, 9, 12, 18, 0, tzinfo=TZ))
    fixture_db.insert(
        "profiles",
        person_id=pid,
        effective_from="2026-09-01",
        sex="m",
        birth_date="1990-03-15",
        height_cm=183.0,
        activity_factor=1.55,
        kcal_floor=1800,
        meals_per_day=4,
    )
    fixture_db.insert(
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
    seed_catalogue(fixture_db)
    secrets_dir = tmp_path / "s"
    secrets_dir.mkdir()
    (secrets_dir / "telegram-token-dev.txt").write_text("1:t", encoding="utf-8")
    tg = FakeTelegram()
    gateways: list[Any] = []
    app = App(
        config,
        Secrets(secrets_dir),
        clock=frozen_clock,
        transport=tg,
        gateway_factory=lambda _app, _db: gateways[0] if gateways else None,
    )
    poller = Poller(
        app.api(),
        fixture_db,
        app.registry(fixture_db),
        app.handlers(fixture_db).handle,
        clock=frozen_clock,
        sleeper=lambda s: None,
    )
    n = {"i": 0}

    def last() -> str:
        return tg.sent[-1]["text"] if tg.sent else ""

    def say(text: str) -> str:
        n["i"] += 1
        tg.queue_update(tg.text_update(n["i"], OWNER_CHAT, text))
        poller.run_once()
        return last()

    def tap(data: str) -> str:
        n["i"] += 1
        tg.queue_update(tg.callback_update(n["i"], OWNER_CHAT, data, message_id=n["i"]))
        poller.run_once()
        return last()

    def voice(file_id: str = "v1", *, audio: bytes = b"ogg", duration: int = 4) -> str:
        n["i"] += 1
        tg.queue_update(tg.voice_update(n["i"], OWNER_CHAT, file_id, duration))
        poller.run_once()
        return last()

    def document(blob: bytes, name: str, *, file_id: str = "d1", mime: str = "application/pdf"):
        n["i"] += 1
        tg.queue_update(
            {
                "update_id": n["i"],
                "message": {
                    "message_id": n["i"],
                    "chat": {"id": OWNER_CHAT, "type": "private"},
                    "document": {"file_id": file_id, "file_name": name, "mime_type": mime},
                },
            }
        )
        poller.run_once()
        return last()

    return pid, fixture_db, tg, gateways, say, tap, voice, document


def state_of(db: Db, person_id: int) -> dict:
    row = db.one("SELECT state_json FROM conversation_state WHERE person_id = ?", (person_id,))
    return {} if row is None else json.loads(row["state_json"])


def partner_dinner(db: Db, person_id: int, day: dt.date, name: str) -> int:
    """One accepted menu of hers, covering `day`, with a dish nothing of his should ever name."""
    mid = db.insert(
        "menus",
        person_id=person_id,
        segment_start=isoday(day),
        segment_end=isoday(day),
        status="proposed",
    )
    md = db.insert(
        "menu_days",
        menu_id=mid,
        day=isoday(day),
        kcal=1800,
        protein_g=140,
        fat_g=60,
        carb_g=180,
        fibre_g=28,
    )
    rid = db.insert("recipes", name=name, prep_minutes=15, method_json='["cook"]', source="model")
    db.insert(
        "meal_slots",
        menu_day_id=md,
        slot="dinner",
        recipe_id=rid,
        portion_multiplier=1.0,
        portion_g=320,
        kcal=600,
        protein_g=45,
        fat_g=20,
        carb_g=55,
        fibre_g=9,
    )
    menu_mod.decide(db, mid, "accepted")
    return mid


# -- one conversation at a time -------------------------------------------------------------


def test_01_a_command_sent_half_way_through_measure_still_runs(world) -> None:  # noqa: F811
    """A command is not an answer. Reading it as one would record `/today` as a waist."""
    _pid, db, _tg, _gw, say, _tap, _run = world
    say("/measure")
    out = say("/today")
    assert answers(out) and "Sat 12 Sep" in out, "the command ran"
    assert db.scalar("SELECT COUNT(*) FROM tape_measures") == 0
    say("84 102")
    assert db.scalar("SELECT COUNT(*) FROM tape_measures") == 2, "the tape question survived it"


def test_02_measure_started_while_a_menu_change_is_open_says_so(world) -> None:  # noqa: F811
    pid, db, _tg, _gw, say, tap, _run = world
    mid = a_menu(db, pid, MONDAY, dt.date(2026, 9, 18), status="proposed")
    tap(f"k:menux:{mid}")
    out = say("/measure")
    assert "Answer the question above first" in out
    assert state_of(db, pid).get("flow") == "menu_change", "the open flow is not overwritten"


def test_03_not_quite_tapped_mid_onboarding_keeps_the_interview(world) -> None:  # noqa: F811
    """Conversation_state holds one flow per person and the interview's answers live in it.

    Telegram buttons never expire, so an evening card from last week is one tap away all the
    time. If that tap armed the off-plan flow it wrote straight over twenty-odd typed answers
    with nothing saying so, and the only way back was the whole interview.
    """
    pid, db, _tg, _gw, say, tap, _run = world
    say("/onboard restart")
    tap("k:ob:cons:0")
    tap("k:ob:sex:m")
    out = tap(f"k:cfmx:{SATURDAY_KEY}")
    assert "Answer the question above first" in out
    st = state_of(db, pid)
    assert st.get("flow") == "onboarding" and st.get("answers") == {"cons": "0", "sex": "m"}


def test_04_onboarding_restarted_half_way_keeps_the_abandoned_answers_in_the_ledger(
    world,  # noqa: F811
) -> None:
    pid, db, _tg, _gw, say, tap, _run = world
    say("/onboard restart")
    tap("k:ob:cons:0")
    tap("k:ob:sex:m")
    say("1990-03-15")
    out = say("/onboard restart")
    assert out.startswith("<b>1/"), "it asks the first question again"
    assert state_of(db, pid).get("answers") == {}, "nothing carried over into the new interview"
    kept = db.all("SELECT step FROM onboarding_answers WHERE person_id = ? ORDER BY id", (pid,))
    assert [r["step"] for r in kept] == ["cons", "sex", "dob"], (
        "the abandoned answers are still on record"
    )


def test_05_the_tape_question_lets_go_once_it_is_answered(world) -> None:  # noqa: F811
    pid, db, _tg, gateways, say, _tap, _run = world
    gateways.append(ChatGateway(answer="You are 300 kcal under today."))
    say("/measure 84 102")
    assert state_of(db, pid) == {}, "a flow that has ended leaves no row behind"
    assert "300 kcal" in say("how am I doing?"), "and the next message reaches the assistant"


# -- the outbox -------------------------------------------------------------------------------


def test_07_in_dev_partner_message_is_recorded_and_never_reaches_partner_phone(
    world,  # noqa: F811
    two_persons,
) -> None:
    """Development never messages her. The ledger still says what would have been sent."""
    _pid, db, tg, _gw, say, _tap, _run = world
    tg.queue_update(tg.text_update(9000, PARTNER_CHAT, "/ping"))
    say("/ping")  # the owner's own message is what drives the poll
    partners = db.one(
        "SELECT status FROM messages_out WHERE person_id = ? ORDER BY id DESC LIMIT 1",
        (two_persons["partner"],),
    )
    assert partners is not None and partners["status"] == "dropped_dev"
    assert tg.sent_to(PARTNER_CHAT) == []


def test_08_a_reply_telegram_rejects_is_a_failed_row_not_a_crash(world) -> None:  # noqa: F811
    pid, db, tg, _gw, say, _tap, _run = world
    say("/ping")
    tg.fail("sendMessage", err(400, "Bad Request: chat not found"))
    say("/ping")
    row = db.one(
        "SELECT status, error FROM messages_out WHERE person_id = ? ORDER BY id DESC LIMIT 1",
        (pid,),
    )
    assert row is not None and row["status"] == "failed"
    assert "chat not found" in str(row["error"])
    assert answers(say("/ping")), "and the next message goes out normally"


def test_09_after_a_pause_the_morning_message_is_a_dropped_row(world) -> None:  # noqa: F811
    pid, db, tg, _gw, say, _tap, run_job = world
    say("/pause 3d")
    before = len(tg.sent)
    assert run_job(morning_targets.JOB, SATURDAY) == "skipped"
    assert len(tg.sent) == before, "a paused person is not messaged"
    row = db.one(
        "SELECT status FROM messages_out WHERE person_id = ? AND kind = 'morning_targets'",
        (pid,),
    )
    assert row is not None and row["status"] == "dropped_paused", "but the ledger says why"


# -- what actually reaches the phone ------------------------------------------------------------


def test_11_a_model_answer_over_the_house_limit_is_cut_not_dropped(world) -> None:  # noqa: F811
    _pid, _db, _tg, gateways, say, _tap, _run = world
    gateways.append(ChatGateway(answer="\n".join(f"point {i}" for i in range(20))))
    out = say("tell me everything")
    assert answers(out)
    assert len(out.splitlines()) <= 8, "the reply cap, not a RenderError and a silent turn"


def test_12_an_emoji_from_the_model_never_reaches_the_phone(world) -> None:  # noqa: F811
    _pid, _db, _tg, gateways, say, _tap, _run = world
    gateways.append(ChatGateway(answer="Great work today \U0001f44d keep it up \U0001f525"))
    out = say("how did I do?")
    check_text(out, max_lines=99)
    assert "\U0001f44d" not in out and "Great work today" in out


def test_13_a_tag_the_model_invents_arrives_as_text_not_markup(world) -> None:  # noqa: F811
    """Telegram rejects an unknown tag with a 400, so an un-escaped reply is a lost turn."""
    _pid, _db, _tg, gateways, say, _tap, _run = world
    gateways.append(ChatGateway(answer="<script>alert(1)</script> you are on track"))
    out = say("anything else?")
    assert "<script>" not in out and "&lt;script&gt;" in out
    check_text(out, max_lines=99)


def test_14_an_ampersand_in_what_someone_ate_is_escaped_on_the_card(world) -> None:  # noqa: F811
    """A bare `&` makes Telegram reject the whole message with a 400, so the card a person is
    waiting to tap never arrives - and "fish & chips" is how people write it."""
    _pid, _db, _tg, gateways, say, _tap, _run = world
    gw = ChatGateway(data={"items": [{"name": "fish and chips", "kcal": 900}], "kcal": 900})
    gw.tool_calls = [("log_offplan", {"day": "2026-09-12", "description": "fish & chips"})]
    gateways.append(gw)
    out = say("I had fish & chips")
    assert "&amp; chips" in out and "fish & chips" not in out


def test_15_a_name_with_an_ampersand_in_it_is_refused_in_words(world) -> None:  # noqa: F811
    _pid, db, _tg, _gw, say, _tap, _run = world
    out = say("/name Alex & Co")
    assert answers(out) and "cannot contain" in out
    assert db.scalar("SELECT COUNT(*) FROM person_names") == 0


def test_16_an_emoji_a_person_types_never_comes_back_at_them(world) -> None:  # noqa: F811
    pid, db, tg, gateways, say, tap, _run = world
    gateways.append(ChatGateway(data={"items": [{"name": "kebab", "kcal": 900}], "kcal": 900}))
    tap(f"k:cfmx:{SATURDAY_KEY}")
    out = say("kebab \U0001f355 and chips")
    assert "kebab" in out and "\U0001f355" not in out
    for sent in tg.sent:
        check_text(sent["text"], max_lines=99)
    payload = db.scalar(
        "SELECT payload_json FROM intents WHERE person_id = ? ORDER BY id DESC LIMIT 1", (pid,)
    )
    assert "\U0001f355" not in str(payload), "scrubbed on the way in, not on the way out"


def test_a_voice_note_gets_one_manual_reply(bot) -> None:
    _pid, db, tg, _gateways, _say, _tap, voice, _doc = bot
    before = len(tg.sent)
    voice()
    replies = tg.sent[before:]
    assert len(replies) == 1
    assert replies[0]["text"] == "Voice notes are not read. Please type it."
    assert db.scalar("SELECT COUNT(*) FROM voice_transcripts") == 0


# -- a plan arriving as a file ------------------------------------------------------------------


def test_a_document_gets_one_manual_reply(bot) -> None:
    _pid, db, tg, _gw, _say, _tap, _voice, document = bot
    before = len(tg.sent)
    document(b"a plan", "plan.pdf")
    replies = tg.sent[before:]
    assert len(replies) == 1
    assert replies[0]["text"] == "Files are not read. Please type what matters."
    assert db.scalar("SELECT COUNT(*) FROM documents") == 0


# -- tone --------------------------------------------------------------------------------------


def test_23_changing_tone_changes_the_wording_and_none_of_the_numbers(world) -> None:  # noqa: F811
    pid, db, _tg, gateways, say, _tap, _run = world
    gw = ChatGateway(answer="2250 kcal today.")
    gateways.append(gw)
    targets_before = db.all("SELECT kcal, protein_g FROM targets WHERE person_id = ?", (pid,))
    say("/set tone blunt")
    say("how many calories today?")
    say("/set tone warm")
    say("how many calories today?")
    first, second = gw.conversations[0][0]["content"], gw.conversations[1][0]["content"]
    assert "blunt" in first.lower() and "warm" in second.lower()
    assert first != second, "tone reaches the model as wording"
    assert db.all("SELECT kcal, protein_g FROM targets WHERE person_id = ?", (pid,)) == (
        targets_before
    ), "and changes no number the person is held to"


def test_24_a_tone_the_tiffin_does_not_have_is_refused(world) -> None:  # noqa: F811
    pid, db, _tg, _gw, say, _tap, _run = world
    before = db.scalar("SELECT COUNT(*) FROM profiles WHERE person_id = ?", (pid,))
    out = say("/set tone shouty")
    assert answers(out) and "buttons" in out.lower()
    assert db.scalar("SELECT COUNT(*) FROM profiles WHERE person_id = ?", (pid,)) == before


# -- the wall between the two of them -----------------------------------------------------------


def test_26_nothing_owner_sees_names_a_dish_from_partner_menu(world, two_persons) -> None:  # noqa: F811
    _pid, db, tg, gateways, say, _tap, _run = world
    gateways.append(ChatGateway(answer="You are on track."))
    partner_dinner(db, two_persons["partner"], SATURDAY, "Kedgeree Of Hers")
    for cmd in ("/menu", "/today", "/recipe", "/shopping", "/rate", "/week"):
        assert answers(say(cmd)), cmd
    say("what is for dinner?")
    assert not any("Kedgeree" in t for t in tg.sent_texts())


# -- buttons that outlive their message ----------------------------------------------------------


def test_27_a_confirm_button_from_six_weeks_ago_refuses_and_says_why(world) -> None:  # noqa: F811
    """The day travels in the callback payload and buttons never expire. A confirmed day is a
    usable day, so writing one for a date long closed moves the measured TDEE and the targets."""
    pid, db, _tg, _gw, _say, tap, _run = world
    out = tap("k:cfm:20260801")
    assert "too far back" in out
    assert db.scalar("SELECT COUNT(*) FROM day_confirmations WHERE person_id = ?", (pid,)) == 0


def test_28_a_callback_id_that_is_not_a_number_gets_a_toast(world) -> None:  # noqa: F811
    _pid, _db, tg, _gw, say, tap, _run = world
    say("/ping")
    quiet_tap(tg, tap, "k:menu:not-a-number")
    assert tg.answered, "a tap that lands nowhere still answers the query"
    assert "no longer works" in str(tg.answered[-1].get("text"))


def test_30_a_meal_button_for_a_slot_that_is_gone_still_answers(world) -> None:  # noqa: F811
    pid, db, tg, _gw, say, tap, _run = world
    say("/ping")
    mid = a_menu(db, pid, MONDAY, dt.date(2026, 9, 18), status="proposed")
    quiet_tap(tg, tap, f"k:meal:{mid}:supper")
    assert "no longer works" in str(tg.answered[-1].get("text"))
