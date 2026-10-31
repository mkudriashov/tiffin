"""Doctor checks the installed bot and training connection with a fake transport."""

from __future__ import annotations

from dataclasses import replace

from tiffin.app import App
from tiffin.doctor import run_checks
from tiffin.http import Response
from tiffin.secrets import Secrets


def test_doctor_checks_connections_and_pairing(
    config, fixture_db, two_persons, frozen_clock
) -> None:
    config = replace(config, paths=replace(config.paths, db=fixture_db.path))
    config.paths.root.mkdir(parents=True)
    config.data["model"]["provider"] = "none"
    config.data["web"]["enabled"] = False
    secrets = Secrets(config.paths.secrets, {})
    secrets.write("telegram_token_dev", "example-token")
    secrets.write("icu_key_owner", "example-key")
    urls = []

    def transport(url, method, data, headers, timeout):
        urls.append(url)
        if url.endswith("/getMe"):
            return Response(200, b'{"ok":true,"result":{"username":"example_bot"}}')
        if "/wellness?" in url:
            return Response(200, b"[]")
        raise AssertionError(url)

    app = App(config, secrets, clock=frozen_clock, transport=transport)
    checks = {check.name: check for check in run_checks(app)}
    assert checks["database"].status == "ok"
    assert checks["Telegram API"].status == "ok"
    assert checks["chats"].status == "ok"
    assert checks["intervals owner"].status == "ok"
    assert checks["web"].detail == "off by choice"
    assert len(urls) == 2
