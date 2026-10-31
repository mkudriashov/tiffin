"""The operator commands: pairing codes and the Tailscale binding the web page resolves against.

`main()` builds its own App from the environment, so these drive it the way a shell does — via
TIFFIN_HOME and TIFFIN_ENV — and assert on what a person at a terminal would see.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tiffin import cli
from tiffin.app import App
from tiffin.clock import FrozenClock
from tiffin.db import Db
from tiffin.secrets import Secrets


def _own_secrets(root: Path, tmp_path: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)


@pytest.fixture
def env(tmp_path: Path, frozen_clock: FrozenClock, monkeypatch: pytest.MonkeyPatch):
    """A dev root with both people, wired the way `tiffin <cmd>` finds it."""
    root = tmp_path / "root"
    _own_secrets(root, tmp_path)
    monkeypatch.setenv("TIFFIN_HOME", str(root))
    monkeypatch.setenv("TIFFIN_ENV", "dev")
    app = App.from_env(clock=frozen_clock)
    app.config.paths.ensure()
    db = Db.open(app.config.paths.db, clock=frozen_clock)
    db.insert("persons", slug="owner", display_name="Alex", tone="blunt")
    db.insert("persons", slug="partner", display_name="Sam", tone="warm")
    db.close()
    secrets = Secrets(app.config.paths.secrets)
    app.config.paths.secrets.mkdir(parents=True, exist_ok=True)
    (app.config.paths.secrets / "telegram-token-dev.txt").write_text("1:t", encoding="utf-8")
    return app.config, secrets, frozen_clock


def _persons(config, clock) -> Db:
    return Db.open(config.paths.db, clock=clock)


def test_link_tailscale_binds_a_login_the_page_can_resolve(env, capsys) -> None:
    config, _secrets, clock = env
    assert cli.main(["link-tailscale", "owner", "someone@example.com"]) == 0
    assert "someone@example.com" not in capsys.readouterr().out, "a login is not echoed back"
    db = _persons(config, clock)
    try:
        from tiffin.persons import PersonRegistry

        found = PersonRegistry(db, config).by_tailscale_login("someone@example.com")
        assert found is not None and found.slug == "owner"
        # the resolution is case-insensitive, because Tailscale is not consistent about case
        assert PersonRegistry(db, config).by_tailscale_login("SomeOne@Example.com") is not None
    finally:
        db.close()


def test_link_tailscale_rebinding_is_a_new_row_not_an_edit(env) -> None:
    config, _secrets, clock = env
    assert cli.main(["link-tailscale", "owner", "old@example.com"]) == 0
    assert cli.main(["link-tailscale", "owner", "new@example.com"]) == 0
    db = _persons(config, clock)
    try:
        rows = db.all("SELECT login FROM tailscale_identities ORDER BY id")
        assert [r["login"] for r in rows] == ["old@example.com", "new@example.com"]
        from tiffin.persons import PersonRegistry

        # the newest attachment wins, and the old one still resolves to the same person
        still = PersonRegistry(db, config).by_tailscale_login("old@example.com")
        assert still is not None and still.slug == "owner"
    finally:
        db.close()


def test_link_tailscale_refuses_something_that_is_not_a_login(env, capsys) -> None:
    assert cli.main(["link-tailscale", "partner", "not-an-email"]) == 2
    assert "config error" in capsys.readouterr().err


def test_pair_prints_a_code_once(env, capsys) -> None:
    assert cli.main(["pair", "partner"]) == 0
    out = capsys.readouterr().out
    assert "pairing code for partner" in out
    assert "Shown once" in out
