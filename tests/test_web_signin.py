"""Web sign-in: the safe default, the confirm page, the cookie, the idle cut-off, one origin."""

from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tiffin import cli
from tiffin import config as cfg
from tiffin.app import App
from tiffin.db import Db
from tiffin.errors import ConfigError
from tiffin.secrets import Secrets
from tiffin.web.app import create_app
from tiffin.web.identity import (
    IDLE_DAYS,
    consume_login_link,
    device_for,
    issue_login_link,
    link_is_live,
)

from .signin import sign_in

ORIGIN = "https://tiffin.example.com"


def _app(config, fixture_db: Db, frozen_clock, **web) -> App:
    config = replace(config, paths=replace(config.paths, db=fixture_db.path))
    config.data["web"].update(
        {"identity_mode": "telegram", "root_path": "", "origin": ORIGIN, **web}
    )
    return App(config, Secrets(config.paths.secrets, {}), clock=frozen_clock)


def _client(app: App, **kw) -> TestClient:
    return TestClient(create_app(app), base_url=ORIGIN, **kw)


def _token(link: str) -> str:
    return link.split("k=", 1)[1]


def _overlay(tmp_path: Path, text: str) -> cfg.Config:
    (tmp_path / "tiffin.toml").write_text(text, encoding="utf-8")
    return cfg.load("dev", root=tmp_path, environ={})


# -- the default and the header refusal ---------------------------------------------------------
def test_the_packaged_default_is_telegram_sign_in(tmp_path: Path) -> None:
    config = cfg.load("dev", root=tmp_path, environ={})
    assert config.get("web.identity_mode") == "telegram"
    assert config.get("web.trusted_header_proxy") is False


def test_header_mode_is_refused_unless_a_trusted_proxy_is_declared(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="trusted_header_proxy"):
        _overlay(tmp_path, '[web]\nidentity_mode = "header"\n')
    config = _overlay(tmp_path, '[web]\nidentity_mode = "header"\ntrusted_header_proxy = true\n')
    assert config.get("web.identity_mode") == "header"


def test_the_page_itself_refuses_header_mode_set_around_the_loader(
    config, fixture_db, frozen_clock
) -> None:
    app = _app(config, fixture_db, frozen_clock, identity_mode="header", trusted_header_proxy=False)
    with pytest.raises(ConfigError, match="trusted_header_proxy"):
        create_app(app)


@pytest.mark.parametrize("value", ["true", 1, None])
def test_only_a_real_true_declares_the_proxy(config, fixture_db, frozen_clock, value) -> None:
    app = _app(config, fixture_db, frozen_clock, identity_mode="header", trusted_header_proxy=value)
    with pytest.raises(ConfigError):
        create_app(app)


def test_a_forged_login_header_gets_nothing_in_the_default_mode(
    config, fixture_db, two_persons, frozen_clock
) -> None:
    app = _app(config, fixture_db, frozen_clock)
    owner = app.registry(fixture_db).require("owner")
    fixture_db.insert("tailscale_identities", person_id=owner.id, login="owner@example.com")
    response = _client(app).get(
        "/api/whoami", headers={"Tailscale-User-Login": "owner@example.com"}
    )
    assert response.status_code == 403


def test_tailscale_setup_declares_its_proxy(tmp_path: Path, frozen_clock) -> None:
    """Asked for Tailscale, setup writes header sign-in together with the proxy that the header
    mode needs, so the configuration loads. (This read the setup source for a string until the
    set-up became a wizard; now it runs it.)"""
    from tiffin.config import load

    from .wizard import Keyboard, World, deps
    from .wizard import setup as run_setup

    keys = Keyboard([])
    code = run_setup(
        [
            "setup",
            "web",
            "--non-interactive",
            "--web-mode",
            "tailscale",
            "--web-origin",
            "https://tiffin.example.ts.net",
        ],
        deps(World(), keys, frozen_clock, environ={"TIFFIN_HOME": str(tmp_path)}),
    )
    assert code == 0, keys.screen
    config = load(root=tmp_path, environ={})
    assert config.get("web.identity_mode") == "header"
    assert config.get("web.trusted_header_proxy") is True


# -- the confirm page ----------------------------------------------------------------------------
def test_opening_the_link_spends_nothing_so_a_preview_cannot_burn_it(
    config, fixture_db, two_persons, frozen_clock
) -> None:
    app = _app(config, fixture_db, frozen_clock)
    owner = app.registry(fixture_db).require("owner")
    link = issue_login_link(app, fixture_db, owner)
    previewer = _client(app)
    for _ in range(3):
        page = previewer.get(link, follow_redirects=False)
        assert page.status_code == 200
        assert "set-cookie" not in page.headers
        assert 'method="post"' in page.text and _token(link) in page.text
    assert link_is_live(app, _token(link))
    person = _client(app)
    assert sign_in(person, link).status_code == 303
    assert person.get("/api/whoami").json()["person_id"] == owner.id
    assert not link_is_live(app, _token(link))


def test_the_confirm_page_sends_no_referrer_to_other_sites_and_is_not_cached_or_indexed(
    config, fixture_db, two_persons, frozen_clock
) -> None:
    app = _app(config, fixture_db, frozen_clock)
    owner = app.registry(fixture_db).require("owner")
    page = _client(app).get(issue_login_link(app, fixture_db, owner))
    assert page.headers["referrer-policy"] == "same-origin"
    assert "no-store" in page.headers["cache-control"]
    assert page.headers["x-robots-tag"] == "noindex"
    assert 'name="referrer" content="same-origin"' in page.text
    # one script, the page's own file that presses the button; nothing inline, nothing foreign
    assert (
        page.text.count("<script") == 1 and '<script src="/signin.js" defer></script>' in page.text
    )


def test_a_bad_or_spent_link_shows_the_refusal_and_echoes_nothing(
    config, fixture_db, two_persons, frozen_clock
) -> None:
    app = _app(config, fixture_db, frozen_clock)
    client = _client(app)
    hostile = '"><script>alert(1)</script>'
    for token in (hostile, "x" * 500, "", "not-a-token"):
        response = client.get("/", params={"k": token}, follow_redirects=False)
        assert response.status_code == 403
        assert "<script>alert" not in response.text and "not-a-token" not in response.text


def test_an_expired_link_is_not_offered(config, fixture_db, two_persons, frozen_clock) -> None:
    app = _app(config, fixture_db, frozen_clock)
    owner = app.registry(fixture_db).require("owner")
    link = issue_login_link(app, fixture_db, owner)
    frozen_clock.advance(minutes=14)
    assert (
        _client(app).get(link).status_code == 200
    )  # 15 minutes: room for a first sign-in at a gate
    frozen_clock.advance(minutes=2)
    expired = _client(app).get(link)
    assert expired.status_code == 403
    assert "expired or was used already" in expired.text
    assert sign_in(_client(app), link).status_code == 403


def test_safari_null_origin_signs_in_only_when_the_browser_says_same_origin(
    config, fixture_db, two_persons, frozen_clock
) -> None:
    """Safari, from a page with a no-referrer policy, sends Origin: null.

    Seen on a real iPhone on 3 Nov."""
    app = _app(config, fixture_db, frozen_clock)
    owner = app.registry(fixture_db).require("owner")
    client = _client(app)
    token = _token(issue_login_link(app, fixture_db, owner))
    for headers in (
        {"Origin": "null"},
        {"Origin": "null", "Sec-Fetch-Site": "cross-site"},
        {"Origin": "null", "Sec-Fetch-Site": "same-site"},
    ):
        assert (
            client.post("/", data={"k": token}, headers=headers, follow_redirects=False).status_code
            == 403
        ), headers
    assert link_is_live(app, token)
    ok = client.post(
        "/",
        data={"k": token},
        headers={"Origin": "null", "Sec-Fetch-Site": "same-origin"},
        follow_redirects=False,
    )
    assert ok.status_code == 303


def test_the_confirm_page_does_not_tell_the_browser_to_drop_its_origin(
    config, fixture_db, two_persons, frozen_clock
) -> None:
    app = _app(config, fixture_db, frozen_clock)
    page = _client(app).get(
        issue_login_link(app, fixture_db, app.registry(fixture_db).require("owner"))
    )
    assert 'content="same-origin"' in page.text and "no-referrer" not in page.text
    assert page.headers.get("referrer-policy") == "same-origin"


def test_posting_without_a_token_or_with_a_huge_body_signs_nobody_in(
    config, fixture_db, two_persons, frozen_clock
) -> None:
    app = _app(config, fixture_db, frozen_clock)
    client = _client(app)
    assert client.post("/", data={}, follow_redirects=False).status_code == 403
    assert client.post("/", data={"k": "a" * 100000}, follow_redirects=False).status_code == 403
    assert client.post("/", content=b"k=", follow_redirects=False).status_code == 403
    assert client.get("/api/whoami").status_code == 403


def test_a_post_from_another_site_cannot_sign_in_and_does_not_spend_the_link(
    config, fixture_db, two_persons, frozen_clock
) -> None:
    app = _app(config, fixture_db, frozen_clock)
    owner = app.registry(fixture_db).require("owner")
    token = _token(issue_login_link(app, fixture_db, owner))
    client = _client(app)
    for headers in (
        {"Origin": "https://evil.example.org"},
        {"Origin": "null"},
        {"Origin": ORIGIN.replace("https", "http")},
        {"Sec-Fetch-Site": "cross-site"},
    ):
        response = client.post("/", data={"k": token}, headers=headers, follow_redirects=False)
        assert response.status_code == 403, headers
    assert link_is_live(app, token)
    same = client.post("/", data={"k": token}, headers={"Origin": ORIGIN}, follow_redirects=False)
    assert same.status_code == 303


def test_a_spent_link_cannot_be_replayed_and_a_second_person_cannot_borrow_a_session(
    config, fixture_db, two_persons, frozen_clock
) -> None:
    app = _app(config, fixture_db, frozen_clock)
    owner = app.registry(fixture_db).require("owner")
    partner = app.registry(fixture_db).require("partner")
    link = issue_login_link(app, fixture_db, owner)
    assert sign_in(_client(app), link).status_code == 303
    assert sign_in(_client(app), link).status_code == 403
    signed = _client(app)
    sign_in(signed, issue_login_link(app, fixture_db, owner))
    other = issue_login_link(app, fixture_db, partner)
    assert sign_in(signed, other).status_code == 403


def test_query_tokens_are_ignored_off_the_root_page(
    config, fixture_db, two_persons, frozen_clock
) -> None:
    app = _app(config, fixture_db, frozen_clock)
    owner = app.registry(fixture_db).require("owner")
    token = _token(issue_login_link(app, fixture_db, owner))
    client = _client(app)
    assert client.get("/api/whoami", params={"k": token}).status_code == 403
    assert link_is_live(app, token)


# -- the cookie ----------------------------------------------------------------------------------
def test_the_cookie_works_on_the_redirect_and_is_lax_httponly_secure(
    config, fixture_db, two_persons, frozen_clock
) -> None:
    app = _app(config, fixture_db, frozen_clock)
    owner = app.registry(fixture_db).require("owner")
    client = _client(app)
    response = sign_in(client, issue_login_link(app, fixture_db, owner))
    cookie = response.headers["set-cookie"].lower()
    assert "samesite=lax" in cookie and "httponly" in cookie and "secure" in cookie
    assert f"max-age={90 * 86400}" in cookie
    assert response.headers["location"] == "/"
    followed = client.get(response.headers["location"])
    assert followed.status_code == 200 and "text/html" in followed.headers["content-type"]


# -- idle cut-off --------------------------------------------------------------------------------
def _signed_in(app: App, db: Db, slug: str = "owner") -> tuple[TestClient, str]:
    person = app.registry(db).require(slug)
    client = _client(app)
    sign_in(client, issue_login_link(app, db, person))
    return client, client.cookies.get("tiffin_session") or ""


def test_a_device_idle_for_fourteen_days_is_signed_out(
    config, fixture_db, two_persons, frozen_clock
) -> None:
    app = _app(config, fixture_db, frozen_clock)
    client, token = _signed_in(app, fixture_db)
    frozen_clock.advance(days=IDLE_DAYS - 1)
    assert client.get("/api/whoami").status_code == 200
    frozen_clock.advance(days=IDLE_DAYS + 1)
    assert client.get("/api/whoami").status_code == 403
    assert device_for(app, token) is None
    assert client.get("/api/devices").status_code == 403


def test_using_the_page_keeps_a_device_in_until_the_ninety_days_end(
    config, fixture_db, two_persons, frozen_clock
) -> None:
    app = _app(config, fixture_db, frozen_clock)
    client, _ = _signed_in(app, fixture_db)
    for _ in range(8):  # a visit every ten days: 80 days
        frozen_clock.advance(days=10)
        assert client.get("/api/whoami").status_code == 200
    frozen_clock.advance(days=11)  # 91 days from sign-in, however active
    assert client.get("/api/whoami").status_code == 403


def test_the_seen_ledger_grows_at_most_twice_a_day_and_is_append_only(
    config, fixture_db, two_persons, frozen_clock
) -> None:
    app = _app(config, fixture_db, frozen_clock)
    client, _ = _signed_in(app, fixture_db)
    count = lambda: fixture_db.scalar("SELECT COUNT(*) FROM web_device_seen")  # noqa: E731
    for _ in range(20):
        client.get("/api/whoami")
    assert count() == 0
    frozen_clock.advance(hours=13)
    for _ in range(20):
        client.get("/api/whoami")
    assert count() == 1
    with pytest.raises(Exception, match="append-only"):
        fixture_db.execute("UPDATE web_device_seen SET device_id = 1")
    with pytest.raises(Exception, match="append-only"):
        fixture_db.execute("DELETE FROM web_device_seen")


def test_the_device_list_hides_an_idle_device(
    config, fixture_db, two_persons, frozen_clock
) -> None:
    app = _app(config, fixture_db, frozen_clock)
    old, _ = _signed_in(app, fixture_db)
    frozen_clock.advance(days=IDLE_DAYS + 1)
    fresh, _ = _signed_in(app, fixture_db)
    devices = fresh.get("/api/devices").json()["devices"]
    assert len(devices) == 1 and devices[0]["current"] is True
    assert old.get("/api/whoami").status_code == 403


def test_signing_out_ends_a_device_whatever_its_activity(
    config, fixture_db, two_persons, frozen_clock
) -> None:
    app = _app(config, fixture_db, frozen_clock)
    client, _ = _signed_in(app, fixture_db)
    csrf = client.get("/api/me").json()["csrf"]
    device = client.get("/api/devices").json()["devices"][0]["id"]
    out = client.post(
        "/api/devices/sign-out",
        json={"device_id": device},
        headers={"X-CSRF-Token": csrf, "Origin": ORIGIN},
    )
    assert out.status_code == 200
    assert client.get("/api/whoami").status_code == 403


# -- one configured origin -----------------------------------------------------------------------
def _write(client: TestClient, origin: str | None):
    csrf = client.get("/api/me").json()["csrf"]
    headers = {"X-CSRF-Token": csrf}
    if origin is not None:
        headers["Origin"] = origin
    return client.post("/api/devices/sign-out", json={"device_id": 999999}, headers=headers)


def test_only_the_configured_origin_may_write(
    config, fixture_db, two_persons, frozen_clock
) -> None:
    app = _app(config, fixture_db, frozen_clock)
    client, _ = _signed_in(app, fixture_db)
    assert _write(client, ORIGIN).status_code == 404  # past the fence: no such device
    assert _write(client, ORIGIN + "/").status_code == 404
    for origin in (
        "https://evil.example.org",
        "https://www.tiffin.example.com",
        "https://tiffin.example.com:8443",
        "http://tiffin.example.com",
        "https://tiffin.example.com.evil.example.org",
        "null",
    ):
        assert _write(client, origin).status_code == 403, origin


def test_changing_the_configured_origin_moves_the_fence_with_it(
    config, fixture_db, two_persons, frozen_clock
) -> None:
    app = _app(config, fixture_db, frozen_clock)
    client, _ = _signed_in(app, fixture_db)
    app.config.data["web"]["origin"] = "https://new.example.org"
    assert _write(client, ORIGIN).status_code == 403
    assert _write(client, "https://new.example.org").status_code == 404


def test_a_cross_site_fetch_marker_is_refused_on_writes(
    config, fixture_db, two_persons, frozen_clock
) -> None:
    app = _app(config, fixture_db, frozen_clock)
    client, _ = _signed_in(app, fixture_db)
    csrf = client.get("/api/me").json()["csrf"]
    response = client.post(
        "/api/devices/sign-out",
        json={"device_id": 999999},
        headers={"X-CSRF-Token": csrf, "Sec-Fetch-Site": "cross-site"},
    )
    assert response.status_code == 403


# -- the signed-out landing ----------------------------------------------------------------------
def test_a_signed_out_visitor_to_the_root_is_sent_to_the_configured_landing(
    config, fixture_db, two_persons, frozen_clock
) -> None:
    app = _app(config, fixture_db, frozen_clock, signed_out_redirect="/tiffin/overview/")
    client = _client(app)
    response = client.get("/", follow_redirects=False)
    # a page whose script goes on to the landing (a message link's "#k=" must reach it first)
    assert response.status_code == 200 and 'data-fallback="/tiffin/overview/"' in response.text
    assert '<a href="/tiffin/overview/">' in response.text
    # the API and every other page still refuse, in words, rather than redirecting
    assert client.get("/api/whoami", follow_redirects=False).status_code == 403
    assert client.get("/static/x", follow_redirects=False).status_code == 403
    # health stays open
    assert client.get("/api/health").status_code in (200, 503)


def test_a_signed_in_person_is_not_redirected(
    config, fixture_db, two_persons, frozen_clock
) -> None:
    app = _app(config, fixture_db, frozen_clock, signed_out_redirect="/tiffin/overview/")
    client, _ = _signed_in(app, fixture_db)
    assert client.get("/", follow_redirects=False).status_code == 200


def test_without_a_landing_the_root_explains_how_to_sign_in(
    config, fixture_db, two_persons, frozen_clock
) -> None:
    response = _client(_app(config, fixture_db, frozen_clock)).get("/", follow_redirects=False)
    assert response.status_code == 403 and "/web" in response.text


@pytest.mark.parametrize(
    "bad",
    ["//evil.example.org", "http://insecure.example.org", "javascript:alert(1)", "x", "/\\\\x"],
)
def test_a_landing_that_could_leave_the_site_unsafely_is_refused(tmp_path: Path, bad: str) -> None:
    with pytest.raises(ConfigError, match="signed_out_redirect"):
        _overlay(tmp_path, f"[web]\nsigned_out_redirect = {bad!r}\n".replace("'", '"'))


@pytest.mark.parametrize("good", ["", "/tiffin/overview/", "https://landing.example.org/gallery/"])
def test_a_path_or_an_https_landing_is_accepted(tmp_path: Path, good: str) -> None:
    assert _overlay(tmp_path, f'[web]\nsigned_out_redirect = "{good}"\n')


# -- link consumption itself ---------------------------------------------------------------------
def test_consuming_twice_in_a_row_succeeds_once(
    config, fixture_db, two_persons, frozen_clock
) -> None:
    app = _app(config, fixture_db, frozen_clock)
    owner = app.registry(fixture_db).require("owner")
    token = _token(issue_login_link(app, fixture_db, owner))
    assert consume_login_link(app, token) is not None
    assert consume_login_link(app, token) is None


# -- one poller ----------------------------------------------------------------------------------
def test_a_second_process_cannot_take_the_lock(tmp_path: Path) -> None:
    lock = tmp_path / "state" / "tiffin.lock"
    code = (
        "import sys, time; from pathlib import Path; from tiffin.cli import acquire_lock; "
        "ok = acquire_lock(Path(sys.argv[1])); print('held' if ok else 'no', flush=True); "
        "time.sleep(60)"
    )
    holder = subprocess.Popen(
        [sys.executable, "-c", code, str(lock)], stdout=subprocess.PIPE, text=True
    )
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "held"
        assert cli.acquire_lock(lock) is False
    finally:
        holder.kill()
        holder.wait()


def test_run_says_so_and_exits_3_when_another_tiffin_holds_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("TIFFIN_HOME", str(home))
    monkeypatch.setenv("TIFFIN_TELEGRAM_TOKEN_DEV", "0:fake")
    monkeypatch.setattr(cli, "acquire_lock", lambda _path: False)
    assert cli.cmd_run(argparse.Namespace(env="dev")) == 3
    assert "another tiffin holds the lock" in capsys.readouterr().err
    assert (home / "state").is_dir()
