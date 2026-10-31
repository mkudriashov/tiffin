from __future__ import annotations

import time
from pathlib import Path

from tiffin.app import App
from tiffin.clock import FrozenClock
from tiffin.db import Db
from tiffin.persons import PersonRegistry
from tiffin.runtime import Runtime
from tiffin.secrets import Secrets
from tiffin.telegram.api import TelegramApi
from tiffin.telegram.handlers import Handlers
from tiffin.telegram.outbox import Outbox
from tiffin.telegram.poller import Poller

from .fakes import FakeTelegram, RecordingSleeper

OWNER_CHAT = 1000001


def make(db: Db, clock: FrozenClock, config, tmp_path: Path):
    tg = FakeTelegram()
    api = TelegramApi("1:x", transport=tg, sleeper=RecordingSleeper())
    reg = PersonRegistry(db, config, clock=clock)
    ob = Outbox(
        api, db, env="dev", admin_chat_id=OWNER_CHAT, state_dir=tmp_path / "state", clock=clock
    )
    h = Handlers(db, ob, reg, clock=clock, env="dev", version="t")
    poller = Poller(api, db, reg, h.handle, clock=clock, sleeper=RecordingSleeper())
    return h, poller, tg


def test_ping_replies(fixture_db, two_persons, frozen_clock, config, tmp_path):
    _h, poller, tg = make(fixture_db, frozen_clock, config, tmp_path)
    tg.queue_update(tg.text_update(1, OWNER_CHAT, "/ping"))
    poller.run_once()
    assert len(tg.sent) == 1 and tg.sent[0]["text"].startswith(
        "pong <code>2026-09-07 19:30:00</code>"
    )
    assert tg.sent[0]["chat_id"] == OWNER_CHAT


def test_unknown_command_and_unwired_button(
    fixture_db, two_persons, frozen_clock, config, tmp_path
):
    _h, poller, tg = make(fixture_db, frozen_clock, config, tmp_path)
    tg.queue_update(tg.text_update(1, OWNER_CHAT, "/nope"))
    tg.queue_update(tg.callback_update(2, OWNER_CHAT, "k:zzz:1", query_id="q7"))
    tg.queue_update(tg.text_update(3, OWNER_CHAT, "free text"))
    poller.run_once()
    texts = tg.sent_texts()
    assert "Unknown command /nope" in texts[0]
    assert "not wired" in texts[1] and tg.answered[0]["callback_query_id"] == "q7"
    assert "Noted" in texts[2]


def test_registered_callback_receives_parts(
    fixture_db, two_persons, frozen_clock, config, tmp_path
):
    h, poller, tg = make(fixture_db, frozen_clock, config, tmp_path)
    seen: list[list[str]] = []

    def on_cfm(hh: Handlers, inbound, parts: list[str]) -> str:
        seen.append(parts)
        hh.clear_buttons(inbound)
        hh.outbox.reply(inbound.person, "recorded")
        return "handled"

    h.register_callback("cfm", on_cfm)
    tg.queue_update(tg.callback_update(1, OWNER_CHAT, "k:cfm:12:extra", message_id=55))
    poller.run_once()
    assert seen == [["12", "extra"]]
    assert tg.edited[0]["message_id"] == 55 and tg.sent_texts() == ["recorded"]


def test_help_says_what_each_command_is_for(
    fixture_db, two_persons, frozen_clock, config, tmp_path
):
    """Twenty bare slash commands in alphabetical order is a list, not an answer."""
    h, poller, tg = make(fixture_db, frozen_clock, config, tmp_path)
    h.register_command("today", lambda hh, i, a: "handled")
    h.register_command("wibble", lambda hh, i, a: "handled")  # registered, never described
    tg.queue_update(tg.text_update(1, OWNER_CHAT, "/help@fake_bot"))
    poller.run_once()
    short = tg.sent_texts()[0]
    # the everyday list, short
    assert "/today - where the day stands" in short and "/help all" in short
    assert len(short.splitlines()) <= 10
    tg.queue_update(tg.text_update(2, OWNER_CHAT, "/help all"))
    poller.run_once()
    text = tg.sent_texts()[1]
    assert "/today - where the day stands" in text
    assert "/wibble" in text, "an undescribed command still appears rather than going missing"
    assert "/ping" not in text, "the plumbing commands are not offered to a person"
    assert "/help" not in text.split("<b>Also</b>")[-1], "/help does not list itself"


def test_runtime_end_to_end(tmp_path: Path, frozen_clock: FrozenClock, config) -> None:
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    (secrets_dir / "telegram-token-dev.txt").write_text("1:devtoken\n", encoding="utf-8")
    tg = FakeTelegram()
    app = App(config, Secrets(secrets_dir), clock=frozen_clock, transport=tg)
    app.bootstrap().close()
    tg.queue_update(tg.text_update(1, OWNER_CHAT, "/ping"))
    rt = Runtime(app, heartbeat_s=1, serve_web=False)
    rt.start()
    deadline = time.time() + 5
    while time.time() < deadline and not tg.sent:
        time.sleep(0.02)
    rt.stop()
    assert tg.sent and tg.sent[0]["text"].startswith("pong")
    assert (config.paths.state / "heartbeat").is_file()
    assert not (config.paths.state / "tiffin.pid").exists()
    db = app.open_db()
    try:
        assert db.scalar("SELECT outcome FROM inbound_outcomes") == "handled"
        assert db.scalar("SELECT COUNT(*) FROM messages_out WHERE status = 'sent'") == 1
        assert db.scalar("SELECT chat_id FROM telegram_identities") == OWNER_CHAT
    finally:
        db.close()
    assert rt.exit_code == 0
