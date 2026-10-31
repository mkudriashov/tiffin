from __future__ import annotations

from tiffin.clock import FrozenClock
from tiffin.db import Db
from tiffin.persons import PersonRegistry
from tiffin.telegram.api import TelegramApi
from tiffin.telegram.poller import Inbound, Poller, classify

from .fakes import FakeTelegram, RecordingSleeper, err

OWNER_CHAT = 1000001
PARTNER_CHAT = 1000002


class Collector:
    def __init__(self) -> None:
        self.items: list[Inbound] = []

    def __call__(self, inbound: Inbound) -> None:
        self.items.append(inbound)


def make_poller(db: Db, clock: FrozenClock, config, **kw) -> tuple[Poller, FakeTelegram, Collector]:
    tg = FakeTelegram()
    api = TelegramApi("1:x", transport=tg, sleeper=RecordingSleeper())
    registry = PersonRegistry(db, config, clock=clock)
    col = Collector()
    return Poller(api, db, registry, col, clock=clock, sleeper=RecordingSleeper(), **kw), tg, col


def test_known_sender_is_stored_dispatched_and_offset_advances(
    fixture_db, two_persons, frozen_clock, config
):
    poller, tg, col = make_poller(fixture_db, frozen_clock, config)
    tg.queue_update(tg.text_update(10, OWNER_CHAT, "hello"))
    assert poller.run_once() == 1
    assert poller.offset() == 11
    row = fixture_db.latest("messages_in")
    assert row is not None and row["kind"] == "text" and row["person_id"] == two_persons["owner"]
    assert '"hello"' in row["raw_json"]
    assert (
        len(col.items) == 1 and col.items[0].text == "hello" and col.items[0].person.slug == "owner"
    )
    assert tg.sent == []


def test_stranger_gets_nothing(fixture_db, two_persons, frozen_clock, config):
    poller, tg, col = make_poller(fixture_db, frozen_clock, config)
    tg.queue_update(tg.text_update(1, 999, "hi there"))
    poller.run_once()
    row = fixture_db.latest("messages_in")
    assert row is not None and row["kind"] == "stranger" and row["raw_json"] is None
    assert row["person_id"] is None and row["chat_id"] == 999
    assert col.items == [] and tg.sent == [] and poller.offset() == 2


def test_dev_allowlist_blocks_partner_even_when_known(
    fixture_db, two_persons, frozen_clock, config
):
    poller, tg, col = make_poller(
        fixture_db,
        frozen_clock,
        config,
        allowed_chat_ids=frozenset({OWNER_CHAT}),
        pairing_enabled=False,
    )
    tg.queue_update(tg.text_update(1, PARTNER_CHAT, "hello"))
    tg.queue_update(tg.text_update(2, OWNER_CHAT, "hello"))
    poller.run_once()
    kinds = [r["kind"] for r in fixture_db.all("SELECT kind FROM messages_in ORDER BY id")]
    assert kinds == ["stranger", "text"]
    assert [i.person.slug for i in col.items] == ["owner"]


def test_allowlist_blocks_pairing_even_with_a_valid_code(fixture_db, frozen_clock, config):
    registry = PersonRegistry(fixture_db, config, clock=frozen_clock)
    registry.ensure_from_config()
    code = registry.create_pairing_code("partner")
    poller, tg, col = make_poller(
        fixture_db,
        frozen_clock,
        config,
        allowed_chat_ids=frozenset({OWNER_CHAT}),
        pairing_enabled=True,
    )
    tg.queue_update(tg.text_update(1, PARTNER_CHAT, code))
    poller.run_once()
    assert registry.by_chat_id(PARTNER_CHAT) is None
    assert fixture_db.scalar("SELECT COUNT(*) FROM pairing_uses") == 0
    assert fixture_db.latest("messages_in")["kind"] == "stranger"
    assert col.items == []


def test_duplicate_update_is_recorded_once(fixture_db, two_persons, frozen_clock, config):
    poller, tg, col = make_poller(fixture_db, frozen_clock, config)
    tg.queue_update(tg.text_update(5, OWNER_CHAT, "a"))
    poller.run_once()
    fixture_db.state_set("update_offset", "5")  # simulate a lost offset write
    poller.run_once()
    assert fixture_db.scalar("SELECT COUNT(*) FROM messages_in") == 1
    assert (
        fixture_db.scalar("SELECT COUNT(*) FROM inbound_outcomes WHERE outcome = 'duplicate'") == 1
    )
    assert len(col.items) == 1 and poller.offset() == 6


def test_pairing_code_binds_partner_chat_id(fixture_db, frozen_clock, config):
    registry = PersonRegistry(fixture_db, config, clock=frozen_clock)
    registry.ensure_from_config()
    code = registry.create_pairing_code("partner")
    assert fixture_db.scalar("SELECT COUNT(*) FROM telegram_identities") == 1
    poller, tg, col = make_poller(fixture_db, frozen_clock, config)
    tg.queue_update(tg.text_update(1, PARTNER_CHAT, "nonsense"))
    tg.queue_update(tg.text_update(2, PARTNER_CHAT, code.lower()))
    tg.queue_update(
        tg.text_update(3, PARTNER_CHAT, code)
    )  # reuse must fail: already paired -> known
    poller.run_once()
    kinds = [r["kind"] for r in fixture_db.all("SELECT kind FROM messages_in ORDER BY id")]
    assert kinds == ["stranger", "text", "text"]
    assert [i.just_paired for i in col.items] == [True, False]
    paired = registry.by_chat_id(PARTNER_CHAT)
    assert paired is not None and paired.slug == "partner"
    assert fixture_db.scalar("SELECT COUNT(*) FROM pairing_uses") == 1
    assert fixture_db.scalar("SELECT COUNT(*) FROM inbound_outcomes WHERE outcome = 'paired'") == 1


def test_expired_or_used_code_does_not_pair(fixture_db, frozen_clock, config):
    registry = PersonRegistry(fixture_db, config, clock=frozen_clock)
    registry.ensure_from_config()
    code = registry.create_pairing_code("partner", ttl_hours=1)
    frozen_clock.advance(hours=2)
    poller, tg, col = make_poller(fixture_db, frozen_clock, config)
    tg.queue_update(tg.text_update(1, PARTNER_CHAT, code))
    poller.run_once()
    assert col.items == [] and registry.by_chat_id(PARTNER_CHAT) is None
    assert fixture_db.latest("messages_in")["kind"] == "stranger"


def test_poll_error_backs_off_and_continues(fixture_db, two_persons, frozen_clock, config):
    poller, tg, _col = make_poller(fixture_db, frozen_clock, config)
    tg.fail("getUpdates", err(400, "boom"))
    assert poller.run_once() == 0
    assert poller.error_streak == 1
    tg.queue_update(tg.text_update(1, OWNER_CHAT, "a"))
    assert poller.run_once() == 1 and poller.error_streak == 0


def test_classify_shapes() -> None:
    tg = FakeTelegram()
    assert classify(tg.text_update(1, 5, "/ping"))[1] == "command"
    assert classify(tg.text_update(1, 5, "hi"))[1] == "text"
    chat, kind, parts = classify(tg.callback_update(1, 5, "k:cfm:3", query_id="q9"))
    assert (chat, kind, parts["callback_data"], parts["callback_query_id"]) == (
        5,
        "callback",
        "k:cfm:3",
        "q9",
    )
    chat, kind, parts = classify(tg.voice_update(1, 5, "f9"))
    assert kind == "voice" and parts["voice"]["file_id"] == "f9"
    group = {
        "update_id": 1,
        "message": {"message_id": 1, "text": "x", "chat": {"id": -1, "type": "group"}},
    }
    assert classify(group)[1] == "other"
    assert classify({"update_id": 2})[0] is None
