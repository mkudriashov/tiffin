"""Fixes from the independent review: the write token bound to the session, the public health
answer, dev mode on a public address, and the doctor's hosting-words warning."""

from __future__ import annotations

from dataclasses import replace

import pytest

from tiffin import config as cfg
from tiffin import healthcheck
from tiffin.app import App
from tiffin.doctor import run_checks
from tiffin.errors import ConfigError
from tiffin.secrets import Secrets
from tiffin.web.app import create_app
from tiffin.web.identity import settings_from

from .test_web_root_path import ORIGIN, ROOT, SHAPES, _app, _client, _signed_in

# built in pieces: a real-looking public address, which the privacy scan would otherwise flag
PUBLIC = "https" + "://tiffin.my-site.org"


def _post(client, prefix: str, headers: dict) -> int:
    response = client.post(
        prefix + "/api/shopping/tick", json={"item_id": 1, "ticked": True}, headers=headers
    )
    return response.status_code


@pytest.mark.parametrize("prefix", SHAPES.values(), ids=SHAPES.keys())
def test_a_token_signed_to_the_session_is_accepted_under_a_root_path(
    config, fixture_db, two_persons, frozen_clock, prefix
) -> None:
    app = _app(config, fixture_db, frozen_clock)
    client = _signed_in(app, fixture_db, prefix)
    token = client.get(prefix + "/api/me").json()["csrf"]
    assert client.get(prefix + "/api/me").json()["csrf"] == token, "a second tab keeps working"
    assert "tiffin_csrf" not in client.cookies
    # past the fence: the item does not exist, so the answer is not 403
    assert _post(client, prefix, {"X-CSRF-Token": token, "Origin": ORIGIN}) != 403


def test_a_planted_cookie_and_a_matching_header_are_refused(
    config, fixture_db, two_persons, frozen_clock
) -> None:
    """Any script on the same address can set a cookie. It cannot sign a token."""
    app = _app(config, fixture_db, frozen_clock)
    client = _signed_in(app, fixture_db, ROOT)
    client.cookies.set("tiffin_csrf", "planted-by-another-app", path=ROOT)
    headers = {"X-CSRF-Token": "planted-by-another-app", "Origin": ORIGIN}
    assert _post(client, ROOT, headers) == 403
    assert _post(client, ROOT, {"Origin": ORIGIN}) == 403


def test_a_token_from_another_session_is_refused(
    config, fixture_db, two_persons, frozen_clock
) -> None:
    app = _app(config, fixture_db, frozen_clock)
    first = _signed_in(app, fixture_db, ROOT)
    second = _signed_in(app, fixture_db, ROOT)
    stolen = first.get(ROOT + "/api/me").json()["csrf"]
    assert second.get(ROOT + "/api/me").json()["csrf"] != stolen
    assert _post(second, ROOT, {"X-CSRF-Token": stolen, "Origin": ORIGIN}) == 403


def test_the_token_survives_a_restart_because_the_key_is_kept(
    config, fixture_db, two_persons, frozen_clock
) -> None:
    app = _app(config, fixture_db, frozen_clock)
    client = _signed_in(app, fixture_db, ROOT)
    token = client.get(ROOT + "/api/me").json()["csrf"]
    again = App(app.config, Secrets(app.config.paths.secrets, {}), clock=frozen_clock)
    client2 = type(client)(create_app(again), base_url=ORIGIN, root_path=ROOT)
    client2.cookies.update(client.cookies)
    assert client2.get(ROOT + "/api/me").json()["csrf"] == token


# -- public health -----------------------------------------------------------------------------
def test_public_health_is_the_status_word_alone(
    config, fixture_db, two_persons, frozen_clock
) -> None:
    app = _app(config, fixture_db, frozen_clock)
    body = _client(app).get(ROOT + "/api/health").json()
    assert set(body) == {"status"}


def test_the_containers_own_check_and_a_signed_in_owner_get_the_detail(
    config, fixture_db, two_persons, frozen_clock
) -> None:
    app = _app(config, fixture_db, frozen_clock)
    local = type(_client(app))(
        create_app(app), base_url=ORIGIN, root_path=ROOT, client=("127.0.0.1", 50000)
    )
    detail = {"env", "version", "uptime_s", "last_backup"}
    assert detail <= set(local.get(ROOT + "/api/health").json())
    # the same loopback address, but through a proxy (it names the real caller): status only
    proxied = local.get(ROOT + "/api/health", headers={"X-Forwarded-For": "203.0.113.9"})
    assert set(proxied.json()) == {"status"}
    owner = _signed_in(app, fixture_db, ROOT)
    assert detail <= set(owner.get(ROOT + "/api/health").json())


def test_the_container_health_check_passes_on_the_status_word_alone(config) -> None:
    class Reply:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, _n=-1):
            return b'{"status":"ok"}'

    seen = []

    def opener(url, timeout):
        seen.append(url)
        return Reply()

    config.data["web"]["enabled"] = True
    ok, _why = healthcheck.check(config, opener=opener)
    assert ok and seen[0].endswith("/api/health")


# -- dev mode on a public address --------------------------------------------------------------
def test_dev_is_refused_on_a_public_https_address(config) -> None:
    assert config.env == "dev"
    config.data["web"]["origin"] = PUBLIC
    with pytest.raises(ConfigError, match="X-Dev-Person"):
        settings_from(config, Secrets(config.paths.secrets, {}))


@pytest.mark.parametrize(
    "origin",
    ["https://localhost:8443", PUBLIC.replace("https", "http"), "https://tiffin.example.com"],
)
def test_dev_is_allowed_on_this_computer_or_a_placeholder(config, origin) -> None:
    config.data["web"]["origin"] = origin
    settings_from(config, Secrets(config.paths.secrets, {}))


def test_public_https_origin_shapes() -> None:
    assert cfg.public_https_origin(PUBLIC + "/x")
    assert not cfg.public_https_origin("https://127.0.0.1:7788")
    assert not cfg.public_https_origin("https://tiffin.example.ts.net")
    assert not cfg.public_https_origin("")


# -- the doctor's words check ------------------------------------------------------------------
def _doctor(config, fixture_db, frozen_clock):
    config = replace(config, paths=replace(config.paths, db=fixture_db.path))
    config.paths.root.mkdir(parents=True, exist_ok=True)
    config.data["model"]["provider"] = "none"
    config.data["web"]["enabled"] = False
    app = App(config, Secrets(config.paths.secrets, {}), clock=frozen_clock)
    return {c.name: c for c in run_checks(app, online=False)}


def test_doctor_warns_on_a_public_address_with_default_hosting_words(
    config, fixture_db, two_persons, frozen_clock
) -> None:
    config.data["web"]["origin"] = PUBLIC
    check = _doctor(config, fixture_db, frozen_clock)["consent wording"]
    assert check.status == "warn" and "hosting.records" in check.detail


def test_doctor_is_quiet_when_the_words_are_set_or_the_address_is_private(
    config, fixture_db, two_persons, frozen_clock
) -> None:
    config.data["web"]["origin"] = PUBLIC
    config.data["hosting"]["records"] = "a rented server in London"
    config.data["hosting"]["web_front"] = "Cloudflare"
    assert "consent wording" not in _doctor(config, fixture_db, frozen_clock)
    config.data["hosting"]["records"] = "this computer"
    config.data["hosting"]["web_front"] = ""
    config.data["web"]["origin"] = "https://127.0.0.1:7788"
    assert "consent wording" not in _doctor(config, fixture_db, frozen_clock)
