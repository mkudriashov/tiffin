"""What `tiffin init` always did keeps working, now that it runs `tiffin setup`.

A new laptop pairs its chat without asking for a numeric Telegram ID; the country sets the units
and a catalogue-only shop; a proxy set-up uses Telegram sign-in at its public address. These are
the scripted forms the README and the Docker notes have printed since the first release.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tiffin import doctor, persons, setup
from tiffin.config import load
from tiffin.db import Db

from .wizard import CHAT_ID, GOOD_TOKEN, OWNER_CODE, Keyboard, World, deps, predictable_codes
from .wizard import setup as run_setup


@pytest.fixture(autouse=True)
def stand_ins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "_listening", lambda port: False)
    predictable_codes(monkeypatch)


def test_country_sets_units_and_catalogue_only_shop(tmp_path: Path, frozen_clock) -> None:
    keys = Keyboard([])
    code = run_setup(
        [
            "--env",
            "dev",
            "init",
            "--non-interactive",
            "--owner-name",
            "Alex",
            "--timezone",
            "Europe/London",
            "--country",
            "FR",
            "--partner",
            "no",
            "--web-mode",
            "off",
            "--owner-chat-id",
            "1000001",
            "--no-catalogue",
        ],
        deps(
            World(),
            keys,
            frozen_clock,
            environ={"TIFFIN_HOME": str(tmp_path), "TIFFIN_TELEGRAM_TOKEN_DEV": GOOD_TOKEN},
        ),
    )
    assert code == 0, keys.screen
    assert "UK CoFID data" in keys.screen
    assert setup.ZONE_COUNTRY["Europe/Paris"] == "FR"
    conf = load("dev", root=tmp_path, environ={})
    assert conf.get("env.country") == "FR"
    assert conf.get("units.weight") == "kg" and conf.get("units.currency") == "EUR"
    assert conf.get("shop.retailers") == [] and conf.get("chrome.shop_refresh_enabled") is False


def test_proxy_setup_uses_telegram_identity_and_public_origin(tmp_path: Path, frozen_clock) -> None:
    keys = Keyboard([])
    code = run_setup(
        [
            "--env",
            "dev",
            "connect",
            "web",
            "--non-interactive",
            "--web-mode",
            "proxy",
            "--web-origin",
            "https://tiffin.example.com",
        ],
        deps(World(), keys, frozen_clock, environ={"TIFFIN_HOME": str(tmp_path)}),
    )
    assert code == 0, keys.screen
    config = load("dev", root=tmp_path, environ={})
    assert config.get("web.identity_mode") == "telegram"
    assert config.get("web.origin") == "https://tiffin.example.com"
    assert config.get("web.root_path") == ""
    assert config.get("web.host") == "127.0.0.1"


def test_noninteractive_init_pairs_owner_and_keeps_token_out_of_config(
    tmp_path: Path, frozen_clock
) -> None:
    home = tmp_path / "home"
    keys = Keyboard([])
    code = run_setup(
        [
            "init",
            "--home",
            str(home),
            "--non-interactive",
            "--telegram-token",
            GOOD_TOKEN,
            "--owner-name",
            "Alex",
            "--model-provider",
            "none",
            "--web-mode",
            "local",
            "--no-catalogue",
        ],
        deps(World(sent=[OWNER_CODE]), keys, frozen_clock),
    )
    assert code == 0, keys.screen
    config = load(root=home, environ={})
    assert config.get("web.identity_mode") == "telegram"
    assert config.get("web.origin") == "http://127.0.0.1:7788"
    assert GOOD_TOKEN not in (home / "tiffin.toml").read_text(encoding="utf-8")
    assert (home / "secrets" / "telegram-token.txt").read_text(
        encoding="utf-8"
    ).strip() == GOOD_TOKEN
    with Db.open(home / "tiffin.db", clock=frozen_clock) as db:
        assert persons.PersonRegistry(db, config).admin().chat_id == CHAT_ID
        assert db.state_get("update_offset") == "2"
