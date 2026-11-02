"""Linking through the bot: /link, its buttons, and what a lone person is (not) offered.

Attacks tried: a guessed code, the maker entering their own code, a second code while linked,
and a stale button from a screen long gone.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tiffin import links
from tiffin.db import Db
from tiffin.persons import PersonRegistry
from tiffin.telegram import linkflow
from tiffin.telegram.api import TelegramApi
from tiffin.telegram.handlers import Handlers
from tiffin.telegram.outbox import Outbox
from tiffin.telegram.poller import Poller

from .fakes import FakeTelegram, RecordingSleeper

OWNER_CHAT, PARTNER_CHAT = 1000001, 1000002


# -- the bot ---------------------------------------------------------------------------------


@pytest.fixture
def bot(fixture_db: Db, two_persons, frozen_clock, config, tmp_path: Path):
    tg = FakeTelegram()
    api = TelegramApi("1:x", transport=tg, sleeper=RecordingSleeper())
    reg = PersonRegistry(fixture_db, config, clock=frozen_clock)
    ob = Outbox(
        api, fixture_db, env="prod", admin_chat_id=OWNER_CHAT,
        state_dir=tmp_path / "state", clock=frozen_clock,
    )  # fmt: skip
    h = Handlers(fixture_db, ob, reg, clock=frozen_clock, env="prod", version="t")
    linkflow.install(h)
    poller = Poller(api, fixture_db, reg, h.handle, clock=frozen_clock, sleeper=RecordingSleeper())
    counter = iter(range(1, 1000))

    def say(chat: int, text: str) -> list[str]:
        before = len(tg.sent)
        tg.queue_update(tg.text_update(next(counter), chat, text))
        poller.run_once()
        return [m["text"] for m in tg.sent[before:]]

    def tap(chat: int, data: str) -> list[str]:
        before = len(tg.sent)
        tg.queue_update(tg.callback_update(next(counter), chat, data))
        poller.run_once()
        return [m["text"] for m in tg.sent[before:]]

    return fixture_db, tg, say, tap


def code_in(texts: list[str]) -> str:
    import re

    found = re.search(r"<code>([A-Z0-9]{4}-[A-Z0-9]{4})</code>", "\n".join(texts))
    assert found, texts
    return found.group(1)


def test_link_with_no_arguments_says_where_you_stand(bot) -> None:
    _db, _tg, say, _tap = bot
    said = say(OWNER_CHAT, "/link")
    assert said and "not linked" in said[0] and "Weekend menus are your own" in said[0]
    assert "/link new" in said[0]


def test_a_code_made_in_the_bot_links_them_when_the_other_enters_it(bot) -> None:
    db, tg, say, _tap = bot
    made = say(OWNER_CHAT, "/link new")
    code = code_in(made)
    assert "works once" in made[0] and "48 hours" in made[0]
    got = say(PARTNER_CHAT, f"/link {code.lower()}")
    assert links.is_linked(db)
    assert any("You are linked with" in t for t in got)
    # the one who made the code is told too, in their own chat
    assert any("You are linked with" in m["text"] for m in tg.sent_to(OWNER_CHAT))
    # and told what is still missing, without anything about the other's data
    assert any("/consent" in t for t in got)


def test_a_wrong_code_in_the_bot_is_refused_and_five_stop_the_tries(bot) -> None:
    db, _tg, say, _tap = bot
    code = code_in(say(OWNER_CHAT, "/link new"))
    for _ in range(5):
        assert "not right" in say(PARTNER_CHAT, "/link ZZZZ-9999")[0]
    assert "Too many codes" in say(PARTNER_CHAT, f"/link {code}")[0]
    assert not links.is_linked(db)


def test_the_maker_cannot_enter_their_own_code(bot) -> None:
    db, _tg, say, _tap = bot
    code = code_in(say(OWNER_CHAT, "/link new"))
    assert "code you made" in say(OWNER_CHAT, f"/link {code}")[0]
    assert not links.is_linked(db)


def test_linked_people_cannot_make_another_code(bot) -> None:
    db, _tg, say, _tap = bot
    say(PARTNER_CHAT, f"/link {code_in(say(OWNER_CHAT, '/link new'))}")
    assert "linked already" in say(OWNER_CHAT, "/link new")[0]
    assert db.scalar("SELECT COUNT(*) FROM links") == 1


def test_unlink_by_button_asks_first_then_tells_the_other_neutrally(bot) -> None:
    db, tg, say, tap = bot
    say(PARTNER_CHAT, f"/link {code_in(say(OWNER_CHAT, '/link new'))}")
    status = say(OWNER_CHAT, "/link")[0]
    assert "You are linked with" in status
    asked = tap(OWNER_CHAT, "k:link:ask")
    assert "Unlink now?" in asked[0] and links.is_linked(db), "asking changes nothing"
    assert "Still linked" in tap(OWNER_CHAT, "k:link:keep")[0] and links.is_linked(db)
    done = tap(OWNER_CHAT, "k:link:unlink")
    assert not links.is_linked(db)
    assert "not taken back" in done[0] and "stays as it is" in done[0]
    told = tg.sent_to(PARTNER_CHAT)[-1]["text"]
    assert "has unlinked" in told and "not taken back" in told
    # neutral: it says nothing about why, and nothing about anything they did or missed
    for word in ("miss", "over", "fail", "because", "weight", "target", "kcal"):
        assert word not in told.lower()
    assert db.scalar("SELECT COUNT(*) FROM link_events WHERE event = 'unlinked'") == 1


def test_either_person_can_unlink_in_one_command(bot) -> None:
    db, tg, say, _tap = bot
    say(PARTNER_CHAT, f"/link {code_in(say(OWNER_CHAT, '/link new'))}")
    say(PARTNER_CHAT, "/link unlink")
    assert not links.is_linked(db)
    assert "has unlinked" in tg.sent_to(OWNER_CHAT)[-1]["text"]
    assert "not linked to anyone" in say(PARTNER_CHAT, "/link unlink")[0]


def test_the_link_command_is_in_help(bot) -> None:
    _db, _tg, _say, _tap = bot
    from tiffin.telegram.handlers import HELP_GROUPS

    assert any(n == "link" for _t, items in HELP_GROUPS for n, _d in items)


def test_a_stale_share_button_for_a_lone_person_says_weekends_are_their_own(
    fixture_db: Db, two_persons, frozen_clock, config, tmp_path: Path
) -> None:
    from tiffin import onboarding as onboarding_mod

    tg = FakeTelegram()
    api = TelegramApi("1:x", transport=tg, sleeper=RecordingSleeper())
    reg = PersonRegistry(fixture_db, config, clock=frozen_clock)
    ob = Outbox(
        api, fixture_db, env="prod", admin_chat_id=OWNER_CHAT,
        state_dir=tmp_path / "state", clock=frozen_clock,
    )  # fmt: skip
    h = Handlers(fixture_db, ob, reg, clock=frozen_clock, env="prod", version="t")
    onboarding_mod.install(
        h, onboarding_mod.Onboarding(fixture_db, ob, reg, config, clock=frozen_clock)
    )
    poller = Poller(api, fixture_db, reg, h.handle, clock=frozen_clock, sleeper=RecordingSleeper())
    tg.queue_update(tg.text_update(1, OWNER_CHAT, "/consent"))
    poller.run_once()
    shown = json.dumps(tg.sent[-1])
    assert "Weekend menus are your own" in shown
    assert "Share weekend menus" not in shown, "no dead button for someone who is not linked"
    tg.queue_update(tg.callback_update(2, OWNER_CHAT, "k:consent:share:1"))
    poller.run_once()
    assert "until you are linked" in tg.sent[-1]["text"]
    # linked, the same screen offers the button
    links.record_migrated(fixture_db, two_persons["owner"], two_persons["partner"])
    tg.queue_update(tg.text_update(3, OWNER_CHAT, "/consent"))
    poller.run_once()
    assert "sharing weekends" in json.dumps(tg.sent[-1]), "linked, the share switch is there"
