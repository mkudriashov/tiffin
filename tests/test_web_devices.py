"""Telegram web links become one-use, person-scoped device sessions."""

from __future__ import annotations

from dataclasses import replace

from fastapi.testclient import TestClient

from tiffin.app import App
from tiffin.db import Db
from tiffin.secrets import Secrets
from tiffin.web.app import create_app
from tiffin.web.identity import consume_login_link, device_for, issue_login_link

from .signin import sign_in


def _app(config, fixture_db: Db, frozen_clock):
    config = replace(config, paths=replace(config.paths, db=fixture_db.path))
    config.data["web"].update(
        {
            "identity_mode": "telegram",
            "root_path": "",
            "origin": "https://tiffin.example.com",
        }
    )
    return App(config, Secrets(config.paths.secrets, {}), clock=frozen_clock)


def test_link_expires_and_is_single_use(config, fixture_db, two_persons, frozen_clock) -> None:
    app = _app(config, fixture_db, frozen_clock)
    owner = app.registry(fixture_db).require("owner")
    expired = issue_login_link(app, fixture_db, owner).split("k=", 1)[1]
    frozen_clock.advance(minutes=16)
    assert consume_login_link(app, expired) is None
    live = issue_login_link(app, fixture_db, owner).split("k=", 1)[1]
    person, session = consume_login_link(app, live) or (None, "")
    assert person is not None and person.id == owner.id
    assert consume_login_link(app, live) is None
    assert device_for(app, session) == (owner, 1)


def test_cookie_flags_and_cross_person_device_refusal(
    config, fixture_db, two_persons, frozen_clock
) -> None:
    app = _app(config, fixture_db, frozen_clock)
    owner = app.registry(fixture_db).require("owner")
    partner = app.registry(fixture_db).require("partner")
    owner_link = issue_login_link(app, fixture_db, owner)
    partner_link = issue_login_link(app, fixture_db, partner)
    owner_client = TestClient(create_app(app), base_url="https://tiffin.example.com")
    partner_client = TestClient(create_app(app), base_url="https://tiffin.example.com")
    response = sign_in(owner_client, owner_link)
    assert response.status_code == 303
    cookie = response.headers["set-cookie"]
    assert "httponly" in cookie.lower()
    assert "samesite=lax" in cookie.lower()
    assert "secure" in cookie.lower()
    assert owner_client.get("/api/whoami").json()["person_id"] == owner.id
    assert sign_in(owner_client, owner_link).status_code == 403
    sign_in(partner_client, partner_link)
    another_owner_link = issue_login_link(app, fixture_db, owner)
    assert sign_in(partner_client, another_owner_link).status_code == 403
    assert sign_in(owner_client, another_owner_link).status_code == 303
    me = partner_client.get("/api/me")
    csrf = me.json()["csrf"]
    owner_device = owner_client.get("/api/devices").json()["devices"][0]["id"]
    denied = partner_client.post(
        "/api/devices/sign-out",
        json={"device_id": owner_device},
        headers={"X-CSRF-Token": csrf},
    )
    assert denied.status_code == 404
    assert owner_client.get("/api/whoami").json()["person_id"] == owner.id
    partner_device = partner_client.get("/api/devices").json()["devices"][0]["id"]
    signed_out = partner_client.post(
        "/api/devices/sign-out",
        json={"device_id": partner_device},
        headers={"X-CSRF-Token": csrf},
    )
    assert signed_out.status_code == 200 and signed_out.json() == {"ok": True}
    assert partner_client.get("/api/whoami").status_code == 403


def test_local_http_cookie_is_not_secure(config, fixture_db, two_persons, frozen_clock) -> None:
    app = _app(config, fixture_db, frozen_clock)
    app.config.data["web"]["origin"] = "http://127.0.0.1:7788"
    owner = app.registry(fixture_db).require("owner")
    link = issue_login_link(app, fixture_db, owner)
    client = TestClient(create_app(app), base_url="http://127.0.0.1:7788")
    response = sign_in(client, link)
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie
    assert "secure" not in cookie


def test_https_proxy_flow_requires_session_and_rejects_forged_identity(
    config, fixture_db, two_persons, frozen_clock
) -> None:
    app = _app(config, fixture_db, frozen_clock)
    owner = app.registry(fixture_db).require("owner")
    partner = app.registry(fixture_db).require("partner")
    client = TestClient(create_app(app), base_url="https://tiffin.example.com")
    forged = {"Tailscale-User-Login": "sam@example.com", "X-Forwarded-Proto": "https"}
    assert client.get("/api/whoami", headers=forged).status_code == 403
    link = issue_login_link(app, fixture_db, owner)
    assert link.startswith("https://tiffin.example.com/")
    response = sign_in(client, link)
    assert response.status_code == 303
    assert "secure" in response.headers["set-cookie"].lower()
    assert client.get("/api/whoami", headers=forged).json()["person_id"] == owner.id
    csrf = client.get("/api/me").json()["csrf"]
    own_device = client.get("/api/devices").json()["devices"][0]["id"]
    wrong_origin = client.post(
        "/api/devices/sign-out",
        json={"device_id": own_device},
        headers={"X-CSRF-Token": csrf, "Origin": "https://other.example.com"},
    )
    assert wrong_origin.status_code == 403
    assert client.get("/api/whoami").json()["person_id"] == owner.id
    assert (
        client.post(
            "/api/devices/sign-out",
            json={"device_id": own_device},
            headers={"X-CSRF-Token": csrf, "Origin": "https://tiffin.example.com"},
        ).status_code
        == 200
    )
    assert client.get("/api/whoami").status_code == 403
    assert partner.id != owner.id
