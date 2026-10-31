"""A new laptop can pair its chat without asking for a numeric Telegram ID."""

from __future__ import annotations

import argparse
from pathlib import Path

from tiffin import persons, setup
from tiffin.app import App
from tiffin.cli import main
from tiffin.config import load
from tiffin.db import Db
from tiffin.secrets import Secrets


def test_country_sets_units_and_catalogue_only_shop(tmp_path: Path, capsys) -> None:
    args = argparse.Namespace(
        owner_name="Alex",
        weight_unit=None,
        height_unit=None,
        timezone="Europe/London",
        country="FR",
        currency=None,
        product_links=None,
        retailers=None,
        partner="no",
        non_interactive=True,
        env="dev",
    )
    setup._household(tmp_path, args)
    assert "UK CoFID data" in capsys.readouterr().out
    assert setup.ZONE_COUNTRY["Europe/Paris"] == "FR"
    conf = load("dev", root=tmp_path, environ={})
    assert conf.get("env.country") == "FR"
    assert conf.get("units.weight") == "kg" and conf.get("units.currency") == "EUR"
    assert conf.get("shop.retailers") == [] and conf.get("chrome.shop_refresh_enabled") is False


def test_proxy_setup_uses_telegram_identity_and_public_origin(tmp_path: Path) -> None:
    args = argparse.Namespace(
        web_mode="proxy",
        web_origin="https://tiffin.example.com",
        non_interactive=True,
        env="dev",
    )
    setup._web(tmp_path, args)
    config = load("dev", root=tmp_path, environ={})
    assert config.get("web.identity_mode") == "telegram"
    assert config.get("web.origin") == "https://tiffin.example.com"
    assert config.get("web.root_path") == ""
    assert config.get("web.host") == "127.0.0.1"


class Bot:
    def __init__(self, token: str, **kwargs):
        assert token == "example-token"

    def get_me(self):
        return {"username": "example_tiffin_bot"}

    def get_updates(self, offset: int, timeout_s: int):
        return (
            [
                {
                    "update_id": 1,
                    "message": {"chat": {"id": 123456, "type": "private"}, "text": "AAAA-AAAA"},
                }
            ]
            if offset <= 1
            else []
        )


def test_noninteractive_init_pairs_owner_and_keeps_token_out_of_config(
    tmp_path: Path, monkeypatch, frozen_clock
) -> None:
    monkeypatch.setattr(setup, "TelegramApi", Bot)
    monkeypatch.setattr(
        setup,
        "_app",
        lambda home, env=None: App(
            load(env, root=home, environ={}), Secrets(home / "secrets"), clock=frozen_clock
        ),
    )
    monkeypatch.setattr(persons._secrets, "choice", lambda _letters: "A")
    home = tmp_path / "home"
    assert (
        main(
            [
                "init",
                "--home",
                str(home),
                "--non-interactive",
                "--telegram-token",
                "example-token",
                "--owner-name",
                "Alex",
                "--model-provider",
                "none",
                "--web-mode",
                "local",
            ]
        )
        == 0
    )
    config = load(root=home, environ={})
    assert config.get("web.identity_mode") == "telegram"
    assert config.get("web.origin") == "http://127.0.0.1:7788"
    assert "example-token" not in (home / "tiffin.toml").read_text(encoding="utf-8")
    assert (home / "secrets" / "telegram-token.txt").read_text(
        encoding="utf-8"
    ).strip() == "example-token"
    with Db.open(home / "tiffin.db", clock=frozen_clock) as db:
        assert persons.PersonRegistry(db, config).admin().chat_id == 123456
        assert db.state_get("update_offset") == "2"
