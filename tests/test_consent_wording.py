"""The consent text describes the machine really running tiffin; old grants stay honoured."""

from __future__ import annotations

from pathlib import Path

import pytest

from tiffin import config as cfg
from tiffin import onboarding as ob
from tiffin.db import Db
from tiffin.errors import ConfigError
from tiffin.model.gateway import consent_granted

from .test_onboarding import OWNER_CHAT, bot  # noqa: F401  (the fixture)


def _config(tmp_path: Path, toml: str = "") -> cfg.Config:
    (tmp_path / "tiffin.toml").write_text(toml, encoding="utf-8")
    return cfg.load("dev", root=tmp_path, environ={})


# -- the words -------------------------------------------------------------------------------------
def test_the_default_text_is_true_of_a_home_computer(tmp_path: Path) -> None:
    text = ob.consent_text(_config(tmp_path))
    assert "held on this computer" in text
    assert "Telegram carries your chats" in text
    assert "Anthropic's Claude CLI" in text
    assert "Nothing else leaves" in text
    assert "Cloudflare" not in text and "stay on this computer" not in text


def test_the_text_names_a_rented_server_and_what_fronts_the_page(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        '[hosting]\nrecords = "a rented server in the EU, run by Sam"\nweb_front = "Cloudflare"\n',
    )
    text = ob.consent_text(config)
    assert "held on a rented server in the EU, run by Sam" in text
    assert "The web page passes through Cloudflare" in text
    assert "Telegram carries your chats" in text
    assert "this computer" not in text


def test_a_front_is_not_named_when_there_is_no_web_page(tmp_path: Path) -> None:
    config = _config(tmp_path, '[web]\nenabled = false\n[hosting]\nweb_front = "Cloudflare"\n')
    assert "Cloudflare" not in ob.consent_text(config)


@pytest.mark.parametrize(
    ("provider", "extra", "says"),
    [
        ("anthropic", "", "Anthropic's Messages API"),
        ("openai", "", "OpenAI's API"),
        (
            "openai-compatible",
            'model = "m"\nbase_url = "http://llm.example.org/v1"\n',
            "llm.example",
        ),
    ],
)
def test_the_text_names_where_model_requests_go(
    tmp_path: Path, provider: str, extra: str, says: str
) -> None:
    text = ob.consent_text(_config(tmp_path, f'[model]\nprovider = "{provider}"\n{extra}'))
    assert says in text


def test_with_no_model_the_text_says_so_and_asks_nothing(tmp_path: Path) -> None:
    text = ob.consent_text(_config(tmp_path, '[model]\nprovider = "none"\n'))
    assert "No model is configured" in text and "Agree?" not in text


def test_no_hosting_text_can_carry_markup_or_run_long(tmp_path: Path) -> None:
    for bad in ('"a <b>x</b>"', '"x & y"', '"' + "x" * 121 + '"', '""'):
        with pytest.raises(ConfigError, match=r"hosting.records"):
            _config(tmp_path, f"[hosting]\nrecords = {bad}\n")
    with pytest.raises(ConfigError, match=r"hosting.web_front"):
        _config(tmp_path, '[hosting]\nweb_front = "<i>"\n')


def test_the_text_is_phone_sized_and_plain(tmp_path: Path) -> None:
    config = _config(
        tmp_path, '[hosting]\nrecords = "a rented server in the EU"\nweb_front = "Cloudflare"\n'
    )
    text = ob.consent_text(config)
    assert len(text) < 650 and "\n" not in text
    assert not any(word in text.lower() for word in ("ai ", "llm", "gdpr", "tls"))


# -- the version and the people already answered --------------------------------------------------
def test_the_wording_version_moved_and_v3_still_counts() -> None:
    assert ob.PROCESSING_CONSENT_VERSION == "v4"


def _grant(db: Db, person_id: int, wording: str, kind: str = "model_processing") -> None:
    db.insert("consents", person_id=person_id, kind=kind, granted=1, wording_version=wording)


@pytest.mark.parametrize(
    ("wording", "kind"),
    [
        ("v3:claude-cli", "model_processing"),
        ("v4:claude-cli", "model_processing"),
        ("v1", "anthropic_processing"),
        ("v2", "model_processing"),
    ],
)
def test_an_existing_grant_for_the_claude_cli_is_honoured(
    fixture_db: Db, two_persons, wording: str, kind: str
) -> None:
    _grant(fixture_db, two_persons["owner"], wording, kind)
    assert consent_granted(fixture_db, two_persons["owner"], provider="claude-cli")


def test_a_grant_for_one_provider_is_not_one_for_another(fixture_db: Db, two_persons) -> None:
    _grant(fixture_db, two_persons["owner"], "v4:anthropic")
    assert not consent_granted(fixture_db, two_persons["owner"], provider="claude-cli")
    assert not consent_granted(fixture_db, two_persons["owner"], provider="openai")
    assert consent_granted(fixture_db, two_persons["owner"], provider="anthropic")
    _grant(fixture_db, two_persons["owner"], "v5:claude-cli")
    assert not consent_granted(fixture_db, two_persons["owner"], provider="claude-cli")


def test_an_old_grant_is_not_asked_again_and_no_row_is_written(bot, fixture_db: Db) -> None:  # noqa: F811
    _app, tg, say, _tap = bot
    _grant(fixture_db, 1, "v3:claude-cli")
    before = fixture_db.scalar("SELECT COUNT(*) FROM consents")
    sent = len(tg.sent)
    for command in ("/today", "/help", "/web"):
        say(command)
    for message in tg.sent[sent:]:
        assert "Agree?" not in message["text"] and "model provider" not in message["text"]
        assert "k:consent" not in str(message.get("reply_markup", ""))
    assert fixture_db.scalar("SELECT COUNT(*) FROM consents") == before
    assert consent_granted(fixture_db, 1, provider="claude-cli")


def test_a_legacy_tracker_style_row_is_honoured_without_a_prompt(bot, fixture_db: Db) -> None:  # noqa: F811
    _app, tg, say, _tap = bot
    _grant(fixture_db, 1, "v1", kind="anthropic_processing")
    before = fixture_db.scalar("SELECT COUNT(*) FROM consents")
    say("/today")
    assert fixture_db.scalar("SELECT COUNT(*) FROM consents") == before
    assert all("Agree?" not in m["text"] for m in tg.sent)


def test_a_new_grant_is_written_under_the_new_wording(bot, fixture_db: Db) -> None:  # noqa: F811
    _app, tg, _say, _tap = bot
    tg.queue_update(tg.callback_update(900, OWNER_CHAT, "k:consent:1", message_id=900))
    from tiffin.telegram.poller import Poller

    poller = Poller(
        _app.api(),
        fixture_db,
        _app.registry(fixture_db),
        _app.handlers(fixture_db).handle,
        clock=_app.clock,
        sleeper=lambda s: None,
    )
    poller.run_once()
    latest = fixture_db.one("SELECT wording_version, granted FROM consents ORDER BY id DESC")
    assert latest is not None
    assert latest["wording_version"] == "v4:claude-cli" and latest["granted"] == 1
    assert consent_granted(fixture_db, 1, provider="claude-cli")
