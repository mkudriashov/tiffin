from __future__ import annotations

from pathlib import Path

from tiffin.clock import FrozenClock
from tiffin.db import Db
from tiffin.persons import PersonRegistry
from tiffin.telegram.api import TelegramApi
from tiffin.telegram.outbox import Outbox

from .fakes import FakeTelegram, RecordingSleeper, err, read_notices

OWNER_CHAT = 1000001
PARTNER_CHAT = 1000002


def make(
    db: Db, clock: FrozenClock, config, tmp_path: Path, env: str = "dev"
) -> tuple[Outbox, FakeTelegram, PersonRegistry]:
    tg = FakeTelegram()
    api = TelegramApi("1:x", transport=tg, sleeper=RecordingSleeper())
    ob = Outbox(
        api, db, env=env, admin_chat_id=OWNER_CHAT, state_dir=tmp_path / "state", clock=clock
    )
    return ob, tg, PersonRegistry(db, config, clock=clock)


def test_sent_row_is_idempotent(fixture_db, two_persons, frozen_clock, config, tmp_path):
    ob, tg, reg = make(fixture_db, frozen_clock, config, tmp_path)
    owner_ = reg.require("owner")
    r = ob.send(owner_, "morning_targets", "2026-09-08", "<b>t</b>")
    assert r.sent and r.telegram_message_id == 100
    row = fixture_db.latest("messages_out")
    assert row["status"] == "sent" and row["sent_at"] == "2026-09-07 19:30:00"
    again = ob.send(owner_, "morning_targets", "2026-09-08", "<b>t</b>")
    assert again.id == r.id and len(tg.sent) == 1


def test_dev_never_sends_to_partner(fixture_db, two_persons, frozen_clock, config, tmp_path):
    ob, tg, reg = make(fixture_db, frozen_clock, config, tmp_path, env="dev")
    r = ob.send(reg.require("partner"), "morning_targets", "2026-09-08", "t")
    assert r.status == "dropped_dev" and tg.sent == []
    ob_prod, tg_prod, reg2 = make(fixture_db, frozen_clock, config, tmp_path, env="prod")
    r2 = ob_prod.send(reg2.require("partner"), "morning_targets", "2026-09-08", "t")
    assert r2.sent and tg_prod.sent[0]["chat_id"] == PARTNER_CHAT


def test_failure_is_a_failed_row_not_an_exception(
    fixture_db, two_persons, frozen_clock, config, tmp_path
):
    ob, tg, reg = make(fixture_db, frozen_clock, config, tmp_path)
    tg.fail("sendMessage", err(400, "Bad Request: chat not found"))
    r = ob.send(reg.require("owner"), "evening_confirm", "2026-09-07", "t")
    assert r.status == "failed" and "chat not found" in (r.error or "")
    r2 = ob.send(reg.require("owner"), "evening_confirm", "2026-09-07", "t")
    assert r2.sent  # the failed row does not block the retry


def test_explicit_drops(fixture_db, two_persons, frozen_clock, config, tmp_path):
    ob, tg, reg = make(fixture_db, frozen_clock, config, tmp_path)
    r = ob.send(reg.require("owner"), "evening_confirm", "2026-09-07", "t", drop="dropped_quiet")
    assert r.status == "dropped_quiet" and tg.sent == []


def test_no_chat_id_is_failed(fixture_db, frozen_clock, config, tmp_path):
    ob, _tg, reg = make(fixture_db, frozen_clock, config, tmp_path)
    reg.ensure_from_config()
    r = ob.send(reg.require("partner"), "morning_targets", "x", "t")
    assert r.status == "failed" and r.error == "no chat id"


def test_reply_has_unique_keys(fixture_db, two_persons, frozen_clock, config, tmp_path):
    ob, tg, reg = make(fixture_db, frozen_clock, config, tmp_path)
    m = reg.require("owner")
    assert ob.reply(m, "a").sent and ob.reply(m, "b").sent
    assert tg.sent_texts() == ["a", "b"]


def test_an_alert_is_a_notice_for_the_page_never_a_message(
    fixture_db, two_persons, frozen_clock, config, tmp_path
):
    """Operational problems appear on the status strip without a food-chat message."""
    ob, tg, reg = make(fixture_db, frozen_clock, config, tmp_path)
    admin = reg.admin()
    ob.alert(admin, "icu_sync", "intervals.icu sync failed")
    ob.alert(admin, "backup", "backup failed")
    assert tg.sent == [], "nothing reaches a chat"
    got = [(n["event"], n["text"]) for n in read_notices(ob.state_dir)]
    assert got == [("icu_sync", "intervals.icu sync failed"), ("backup", "backup failed")]
