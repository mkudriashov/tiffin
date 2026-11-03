"""Links in the bot's messages open the page in one tap (3 Nov 2026).

A menu link sent the partner to a sign-in page instead of the menu: a chat app opens links in
its own browser, with its own cookies, and a session ends after 14 idle days. Each message's links now
carry one sign-in token for the recipient, good for a week and one use; a signed-in device never
spends it, and the ledger never holds it.
"""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Any

from fastapi.testclient import TestClient

from tiffin.app import App
from tiffin.db import Db
from tiffin.http import DEFAULT_USER_AGENT, urllib_transport
from tiffin.secrets import Secrets
from tiffin.telegram import render
from tiffin.web.app import create_app
from tiffin.web.identity import (
    MESSAGE_LINK_DAYS,
    SIGNIN_SCRIPT,
    link_is_live,
    safe_next,
    sign_page_links,
)

from .signin import sign_in

ORIGIN = "https://tiffin.example.com"


def _app(config, fixture_db: Db, frozen_clock, **web: Any) -> App:
    config = replace(config, paths=replace(config.paths, db=fixture_db.path))
    config.data["web"].update(
        {"identity_mode": "telegram", "root_path": "", "origin": ORIGIN, **web}
    )
    return App(config, Secrets(config.paths.secrets, {}), clock=frozen_clock)


def _client(app: App) -> TestClient:
    return TestClient(create_app(app), base_url=ORIGIN)


def _hrefs(text: str) -> list[str]:
    return [h.replace("&amp;", "&") for h in re.findall(r'href="([^"]+)"', text)]


def test_every_page_link_in_a_message_carries_one_token_for_its_recipient(
    config, fixture_db, two_persons, frozen_clock
) -> None:
    app = _app(config, fixture_db, frozen_clock)
    partner = app.registry(fixture_db).require("partner")
    text = (
        "Tonight: "
        + render.menu_link(app.config, day="2026-11-03", slot="dinner")
        + " and "
        + render.page_link(app.config)
        + " and "
        + '<a href="https://example.com/x">elsewhere</a>'
    )
    signed = sign_page_links(app, fixture_db, partner, text)
    menu, page, other = _hrefs(signed)
    assert other == "https://example.com/x"
    assert menu.startswith(ORIGIN + "/?tab=menu&day=2026-11-03&slot=dinner&k=")
    assert page.startswith(ORIGIN + "/?k=")
    assert menu.split("k=")[1] == page.split("k=")[1]  # one token per message
    assert link_is_live(app, page.split("k=")[1])
    rows = fixture_db.all("SELECT person_id, expires_at FROM web_login_links")
    assert len(rows) == 1 and rows[0]["person_id"] == partner.id


def test_a_message_without_page_links_issues_nothing(
    config, fixture_db, two_persons, frozen_clock
) -> None:
    app = _app(config, fixture_db, frozen_clock)
    owner = app.registry(fixture_db).require("owner")
    assert sign_page_links(app, fixture_db, owner, "<b>Lunch</b> at one") == "<b>Lunch</b> at one"
    assert fixture_db.all("SELECT id FROM web_login_links") == []


def test_tapping_a_message_link_signs_in_and_opens_the_tab_it_named(
    config, fixture_db, two_persons, frozen_clock
) -> None:
    app = _app(config, fixture_db, frozen_clock)
    partner = app.registry(fixture_db).require("partner")
    link = _hrefs(
        sign_page_links(app, fixture_db, partner, render.menu_link(app.config, day="2026-11-04"))
    )[0]
    browser = _client(app)
    page = browser.get(link, follow_redirects=False)
    assert page.status_code == 200 and 'id="go"' in page.text
    assert 'name="next" value="tab=menu&amp;day=2026-11-04"' in page.text
    done = sign_in(browser, link)
    assert done.status_code == 303
    assert done.headers["location"] == "/?tab=menu&day=2026-11-04"
    assert browser.get("/api/whoami").json()["person_id"] == partner.id


def test_a_signed_in_device_goes_straight_to_the_page_and_spends_nothing(
    config, fixture_db, two_persons, frozen_clock
) -> None:
    app = _app(config, fixture_db, frozen_clock)
    partner = app.registry(fixture_db).require("partner")
    browser = _client(app)
    first = _hrefs(sign_page_links(app, fixture_db, partner, render.page_link(app.config)))[0]
    assert sign_in(browser, first).status_code == 303
    later = _hrefs(
        sign_page_links(app, fixture_db, partner, render.shopping_link(app.config, list_id=7))
    )[0]
    opened = browser.get(later, follow_redirects=False)
    assert opened.status_code == 303 and opened.headers["location"] == "/?tab=shopping&list=7"
    assert link_is_live(app, later.split("k=")[1])
    # and a link already used once still opens the page on the device it signed in
    again = browser.get(first, follow_redirects=False)
    assert again.status_code == 303 and again.headers["location"] == "/"


def test_message_links_last_a_week_then_say_so(
    config, fixture_db, two_persons, frozen_clock
) -> None:
    app = _app(config, fixture_db, frozen_clock)
    owner = app.registry(fixture_db).require("owner")
    link = _hrefs(sign_page_links(app, fixture_db, owner, render.page_link(app.config)))[0]
    frozen_clock.advance(minutes=MESSAGE_LINK_DAYS * 24 * 60 - 5)
    assert _client(app).get(link).status_code == 200
    frozen_clock.advance(minutes=10)
    gone = _client(app).get(link)
    assert gone.status_code == 403 and "expired or was used already" in gone.text


def test_the_ledger_keeps_the_message_without_its_token(
    config, fixture_db, two_persons, frozen_clock
) -> None:
    app = _app(config, fixture_db, frozen_clock)
    owner = app.registry(fixture_db).require("owner")
    sent: list[str] = []

    class FakeApi:
        def send_message(self, chat_id, html, keyboard=None):
            sent.append(html)
            return {"message_id": 1}

    app._api = FakeApi()  # type: ignore[assignment]
    owner = replace(owner, chat_id=owner.chat_id or 111)
    outbox = app.outbox(fixture_db)
    outbox.env = "prod"
    result = outbox.send(owner, "test", "k1", "See " + render.page_link(app.config))
    assert result.sent
    assert "k=" in sent[0]
    stored = fixture_db.one("SELECT text_html FROM messages_out WHERE id = ?", (result.id,))
    assert "k=" not in stored["text_html"]


def test_next_keeps_only_what_a_tab_reads() -> None:
    assert safe_next("tab=menu&day=2026-11-04&k=abc") == "tab=menu&day=2026-11-04"
    assert safe_next("tab=menu&next=//evil.example&x=1") == "tab=menu"
    assert safe_next("tab=<script>") == ""
    assert safe_next("metric=weight&list=12") == "metric=weight&list=12"


def test_the_sign_in_script_is_served_signed_out_and_holds_no_secret(
    config, fixture_db, two_persons, frozen_clock
) -> None:
    app = _app(config, fixture_db, frozen_clock)
    response = _client(app).get("/signin.js")
    assert response.status_code == 200
    assert response.text == SIGNIN_SCRIPT
    assert response.headers["content-type"].startswith("text/javascript")


def test_requests_without_a_user_agent_get_tiffins_own(monkeypatch) -> None:
    seen: dict[str, str] = {}

    class Resp:
        status = 200
        headers: dict[str, str] = {}

        def read(self) -> bytes:
            return b""

        def __enter__(self):
            return self

        def __exit__(self, *a: object) -> None:
            return None

    def fake_urlopen(req, timeout):
        seen.update({k.lower(): v for k, v in req.header_items()})
        return Resp()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    urllib_transport("https://example.com/", "GET", None, {}, 5)
    assert seen["user-agent"] == DEFAULT_USER_AGENT
    urllib_transport("https://example.com/", "GET", None, {"User-Agent": "mine"}, 5)
    assert seen["user-agent"] == "mine"
