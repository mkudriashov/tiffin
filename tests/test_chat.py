"""Free-text conversation: answers from the person's own tools, write tools become taps,
off-plan text becomes a structured estimate, and every failure degrades instead of going quiet."""

from __future__ import annotations

import datetime as dt
import json
from typing import Any, cast

import pytest

from tiffin import days
from tiffin.app import App
from tiffin.clock import FrozenClock
from tiffin.db import Db
from tiffin.errors import ModelToolUnsupported, ModelUnavailable
from tiffin.model import tools
from tiffin.model.gateway import ModelResult
from tiffin.secrets import Secrets
from tiffin.telegram.poller import Poller

from .fakes import FakeTelegram

OWNER_CHAT = 1000001


class FakeGateway:
    """Records calls; `converse` may run tools before answering, like a real backend."""

    def __init__(
        self,
        answer: str = "",
        data: dict | None = None,
        raises: Exception | None = None,
        tool_calls: list[tuple[str, dict]] | None = None,
    ):
        self.answer = answer
        self.data = data or {}
        self.raises = raises
        self.tool_calls = tool_calls or []
        self.conversations: list[list[dict]] = []
        self.json_calls: list[tuple[str, str]] = []

    def converse(self, task, messages, *, person_id, tools=None):
        self.conversations.append(list(messages))
        if self.raises is not None:
            raise self.raises
        for name, args in self.tool_calls:
            assert tools is not None
            tools.call(name, args)
        return ModelResult(text=self.answer, data=None, model_used="claude-opus-5", backend="fake")

    def json_task(self, task, prompt, schema_name, *, person_id):
        self.json_calls.append((task, prompt))
        if self.raises is not None:
            raise self.raises
        return ModelResult(text="", data=self.data, model_used="claude-opus-5", backend="fake")


@pytest.fixture
def world(fixture_db: Db, two_persons, frozen_clock: FrozenClock, config, tmp_path):
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
        confirm_time="20:30",
        tone="blunt",
    )
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
    secrets_dir = tmp_path / "s"
    secrets_dir.mkdir()
    (secrets_dir / "telegram-token-dev.txt").write_text("1:t", encoding="utf-8")
    tg = FakeTelegram()
    holder: list[FakeGateway | None] = [FakeGateway("You have 2250 kcal today.")]
    app = App(
        config,
        Secrets(secrets_dir),
        clock=frozen_clock,
        transport=tg,
        gateway_factory=lambda _app, _db: holder[0],  # type: ignore[arg-type]
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

    return pid, fixture_db, tg, holder, say, tap


def test_free_text_answers_from_the_persons_own_data(world) -> None:
    _pid, db, _tg, holder, say, _tap = world
    gw = holder[0]
    assert gw is not None
    gw.tool_calls = [("get_targets", {})]
    assert "2250 kcal today" in say("how many calories today?")
    system = gw.conversations[0][0]
    assert system["role"] == "system" and "blunt" in system["content"].lower()
    assert gw.conversations[0][-1]["content"] == "how many calories today?"
    row = db.latest("messages_out", kind="reply")
    assert row is not None and row["model_used"] == "claude-opus-5"
    # the second turn carries the first as history
    say("and protein?")
    assert [m["role"] for m in gw.conversations[1]] == ["system", "user", "assistant", "user"]
    assert gw.conversations[1][1]["content"] == "how many calories today?"


def test_write_tool_becomes_a_tap_not_a_row(world) -> None:
    pid, db, tg, holder, say, tap = world
    gw = holder[0]
    assert gw is not None
    gw.tool_calls = [("log_offplan", {"day": "2026-09-07", "description": "kebab and chips"})]
    gw.answer = "Logged."
    gw.data = {"items": [{"name": "kebab", "kcal": 900}], "kcal": 900}
    out = say("I had a kebab")
    assert "Log Mon 07 Sep: kebab and chips (900 kcal)?" in out, "a day in words, never ISO"
    kb = [b["callback_data"] for row in tg.sent[-1]["reply_markup"]["inline_keyboard"] for b in row]
    assert kb[0].startswith("k:intent:") and kb[1].startswith("k:intentx:")
    assert db.scalar("SELECT COUNT(*) FROM estimates") == 0  # nothing written before the tap
    assert db.scalar("SELECT COUNT(*) FROM intents") == 2  # the bare one lapsed
    assert "Logged 900 kcal off plan" in tap(kb[0])
    est = db.latest("estimates", person_id=pid)
    assert est is not None and est["status"] == "accepted" and est["kcal"] == 900
    assert est["model_used"] == "claude-opus-5"
    decisions = [r["decision"] for r in db.all("SELECT decision FROM intent_decisions ORDER BY id")]
    assert decisions == ["lapsed", "accepted"]
    assert "Already done" in tap(kb[0]), "a second tap says so in words, not the ledger's"


def test_a_turn_that_writes_two_cards_shows_the_food_one(world) -> None:
    """25 Sep: the turn wrote a food card and a "not quite" card and only the second was shown.

    The food card is now shown, with its estimate; the day card beside it is lapsed, because
    the food card's own flow closes the day against the plan minus what it replaced.
    """
    pid, db, _tg, holder, say, _tap = world
    gw = holder[0]
    assert gw is not None
    gw.tool_calls = [
        ("log_offplan", {"day": "2026-09-07", "description": "2 Kinder sticks and 2 Twix"}),
        ("confirm_day", {"day": "2026-09-07", "as_planned": False}),
    ]
    gw.answer = ""
    gw.data = {"items": [{"name": "Twix", "kcal": 250}], "kcal": 450}
    out = say("didn't have breakfast, had 2 kinder sticks and 2 twix instead")
    assert "450 kcal" in out, out
    kinds = {
        str(r["kind"]): str(r["decision"])
        for r in db.all(
            "SELECT i.kind, d.decision FROM intents i JOIN intent_decisions d "
            "ON d.intent_id = i.id WHERE i.person_id = ?",
            (pid,),
        )
    }
    assert kinds.get("confirm_day") == "lapsed", kinds
    assert db.scalar("SELECT COUNT(*) FROM estimates") == 0, "nothing before the tap"


def test_discarded_intent_writes_nothing(world) -> None:
    _pid, db, tg, holder, say, tap = world
    gw = holder[0]
    assert gw is not None
    gw.tool_calls = [("set_pause", {"start_day": "2026-09-08", "end_day": "2026-09-10"})]
    say("pause me for a few days")
    kb = [b["callback_data"] for row in tg.sent[-1]["reply_markup"]["inline_keyboard"] for b in row]
    assert "Discarded" in tap(kb[1])
    assert db.scalar("SELECT COUNT(*) FROM pause_periods") == 0
    assert (
        db.scalar("SELECT decision FROM intent_decisions ORDER BY id DESC LIMIT 1") == "discarded"
    )


def test_model_down_says_so_and_keeps_recording(world) -> None:
    _pid, _db, _tg, holder, say, _tap = world
    holder[0] = FakeGateway(raises=ModelUnavailable("both backends down"))
    out = say("what should I eat?")
    assert "could not reach a model" in out
    holder[0] = None  # no gateway at all: the old fallback answer, never silence
    assert "not wired yet" in say("hello?")


def test_chat_tool_rejection_explains_the_manual_route(world) -> None:
    _pid, _db, _tg, holder, say, _tap = world
    holder[0] = FakeGateway(raises=ModelToolUnsupported("chat tools rejected"))
    out = say("what should I eat?")
    assert "model rejected chat tools" in out
    assert "manual commands still work" in out


def test_offplan_estimate_from_the_evening_flow(world) -> None:
    pid, db, tg, holder, say, tap = world
    gw = holder[0]
    assert gw is not None
    gw.data = {
        "items": [{"name": "kebab", "kcal": 750}, {"name": "chips", "kcal": 400}],
        "kcal": 1150,
        "protein_g": 45,
        "fat_g": 60,
        "carb_g": 95,
        "fibre_g": 6,
    }
    db.insert(
        "day_confirmations", person_id=pid, day="2026-09-07", status="unconfirmed", basis="none"
    )
    tap("k:cfmx:20260907")  # "not quite" opens the what-changed question
    out = say("kebab and chips after the pub")
    assert "1150 kcal" in out and "kebab and chips after the pub" in out
    assert gw.json_calls and gw.json_calls[0][0] == "offplan_estimate"
    assert "2026-09-07" in gw.json_calls[0][1]
    kb = [b["callback_data"] for row in tg.sent[-1]["reply_markup"]["inline_keyboard"] for b in row]
    tap(kb[0])
    est = db.latest("estimates", person_id=pid)
    assert est is not None and est["kcal"] == 1150 and est["status"] == "accepted"
    assert json.loads(est["items_json"])[0]["name"] == "kebab"
    # the day now counts the extra on top of its basis
    latest = days.latest_confirmation(db, pid, dt.date(2026, 9, 7))
    assert latest is not None


def test_estimate_without_a_model_still_records_the_note(world) -> None:
    pid, db, _tg, holder, say, tap = world
    holder[0] = None
    tap("k:cfmx:20260907")
    out = say("two pints and a curry")
    assert "Noted for Mon 07 Sep" in out
    row = days.latest_confirmation(db, pid, dt.date(2026, 9, 7))
    assert row is not None and "curry" in str(row["note"])


def test_tools_are_scoped_to_the_asker(fixture_db: Db, two_persons, frozen_clock) -> None:
    """The chat gateway hands BoundTools; a read never crosses to the other person."""
    owner_id, partner_id = two_persons["owner"], two_persons["partner"]
    for pid, kcal in ((owner_id, 2250), (partner_id, 1700)):
        fixture_db.insert(
            "targets",
            person_id=pid,
            week_start="2026-09-07",
            kcal=kcal,
            protein_g=100,
            fat_g=50,
            carb_g=200,
            fibre_g=30,
            source="formula",
        )
    mine = tools.BoundTools(fixture_db, owner_id).call("get_targets", {})
    partners = tools.BoundTools(fixture_db, partner_id).call("get_targets", {})
    assert mine["targets"]["kcal"] == 2250 and partners["targets"]["kcal"] == 1700
    intent = tools.BoundTools(fixture_db, owner_id, model_used="m").call(
        "confirm_day", {"day": "2026-09-07", "as_planned": True}
    )
    assert (
        fixture_db.scalar("SELECT person_id FROM intents WHERE id = ?", (intent["intent_id"],))
        == owner_id
    )


def test_an_ignored_card_does_not_silence_every_later_message(world) -> None:
    _pid, _db, tg, holder, say, _tap = world
    gw = holder[0]
    assert gw is not None
    gw.tool_calls = [("set_pause", {"start_day": "2026-09-08", "end_day": "2026-09-10"})]
    card = say("pause me for a few days")
    assert "Pause" in card

    gw.tool_calls = []  # the next turn is an ordinary question, and the card is still untapped
    gw.answer = "You are 300 kcal under today."
    before = len(tg.sent)
    second = say("how many calories today?")
    assert len(tg.sent) > before, "the second turn must produce a message at all"
    assert "300 kcal" in second, "the answer, not the stale card"

    third = say("and yesterday?")
    assert len(tg.sent) > before + 1, "and it keeps working"
    assert "Pause" not in third


def test_the_pending_intent_query_ignores_older_undecided_cards(fixture_db, two_persons) -> None:
    from tiffin.telegram.chat import ChatFlow

    pid = two_persons["owner"]
    old = fixture_db.insert("intents", person_id=pid, kind="set_pause", payload_json="{}")
    flow = ChatFlow(lambda: None)

    class H:  # only .db is touched by the query
        db = fixture_db

    h = cast("Any", H())
    assert flow._pending_intents(h, pid, after_id=0), "no floor: the old card is found"
    assert flow._pending_intents(h, pid, after_id=old) == [], "with the floor it is ignored"
    new = fixture_db.insert("intents", person_id=pid, kind="confirm_day", payload_json="{}")
    newer = fixture_db.insert("intents", person_id=pid, kind="log_offplan", payload_json="{}")
    found = flow._pending_intents(h, pid, after_id=old)
    # BOTH cards this turn wrote, oldest first: showing only the newest is how the food card of
    assert [int(r["id"]) for r in found] == [new, newer], "everything this turn created"
