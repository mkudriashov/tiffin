"""Every route on the page, enumerated from the app rather than listed by hand.

The page is the second surface. It is read on a phone on the sofa, by whoever the tailnet
says is asking, and it is the one place either of them can see a week at a time. The existing
tests cover what it is for; this covers what happens when it is asked something it did not
expect - no identity, somebody else's identity, a person with no data at all, a query string
full of nonsense, a write with the wrong shape.

The rule is the same one the jobs obey: degrade, never break. A route may refuse. It may not
raise, and it may not answer a stranger.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tiffin.app import App
from tiffin.clock import FrozenClock
from tiffin.db import Db
from tiffin.secrets import Secrets
from tiffin.web.app import create_app

from .fakes import FakeTelegram

OWNER_LOGIN = "alex@example.com"
STRANGER = "nobody@example.com"

#: routes that answer without an identity, on purpose. Everything else must refuse one.
PUBLIC = {"/api/health", "/manifest.webmanifest", "/static/{name}"}
#: junk in a query string. Every one of these has to end in a page or a 400, never a 500.
JUNK = ("x", "-1", "9" * 40, "2026-13-45", "'; DROP TABLE persons; --", "☃", "")


def _app(config, frozen_clock: FrozenClock, tmp_path, *, with_data: bool) -> tuple[Any, Db]:
    config.paths.ensure()
    db = Db.open(config.paths.db, clock=frozen_clock)
    pid = db.insert("persons", slug="owner", display_name="Alex", tone="blunt")
    db.insert("persons", slug="partner", display_name="Sam", tone="warm")
    db.insert("tailscale_identities", person_id=pid, login=OWNER_LOGIN)
    if with_data:
        db.insert(
            "profiles",
            person_id=pid,
            effective_from="2026-09-01",
            sex="m",
            birth_date="1990-03-15",
            height_cm=183.0,
            activity_factor=1.55,
            kcal_floor=1800,
            steps_floor=9000,
        )
    secrets_dir = tmp_path / "s"
    secrets_dir.mkdir(exist_ok=True)
    (secrets_dir / "telegram-token-dev.txt").write_text("1:t", encoding="utf-8")
    config.data["web"]["secure_cookies"] = False
    config.data["web"]["root_path"] = ""
    app = App(config, Secrets(secrets_dir), clock=frozen_clock, transport=FakeTelegram())
    return app, db


@pytest.fixture
def bare(config, frozen_clock: FrozenClock, tmp_path):
    """Paired, and not one row of anything else. The state she is in today."""
    app, db = _app(config, frozen_clock, tmp_path, with_data=False)
    client = TestClient(create_app(app), base_url="http://testserver")
    try:
        yield client, db
    finally:
        db.close()


@pytest.fixture
def onboarded(config, frozen_clock: FrozenClock, tmp_path):
    app, db = _app(config, frozen_clock, tmp_path, with_data=True)
    client = TestClient(create_app(app), base_url="http://testserver")
    try:
        yield client, db
    finally:
        db.close()


def _walk(items: Any, prefix: str = "") -> list[tuple[str, str]]:
    """Descend into included routers.

    `include_router` does not flatten into `app.routes` any more - it leaves one wrapper
    object holding the router. Reading only the top level found three routes out of fifteen
    and would have called the page audited.
    """
    out: list[tuple[str, str]] = []
    for route in items or ():
        inner = getattr(route, "original_router", None)
        if inner is not None:
            context = getattr(route, "include_context", None)
            out += _walk(inner.routes, prefix + str(getattr(context, "prefix", "") or ""))
            continue
        nested = getattr(route, "routes", None)
        if nested:
            out += _walk(nested, prefix + str(getattr(route, "prefix", "") or ""))
            continue
        path = prefix + str(getattr(route, "path", ""))
        for method in sorted(getattr(route, "methods", set()) or set()):
            if method in ("GET", "POST"):
                out.append((method, path))
    return out


def routes(client: TestClient) -> list[tuple[str, str]]:
    """(method, path) for every route the app actually serves."""
    return sorted(set(_walk(getattr(client.app, "routes", ()))))


def gets(client: TestClient) -> list[str]:
    return [p for m, p in routes(client) if m == "GET" and "{" not in p]


def posts(client: TestClient) -> list[str]:
    return [p for m, p in routes(client) if m == "POST"]


def test_the_sweep_sees_the_whole_page(bare) -> None:
    """A route that stops being enumerated stops being audited, silently."""
    client, _db = bare
    found = {p for _m, p in routes(client)}
    assert {"/", "/api/health", "/api/today", "/api/week", "/api/shopping/tick"} <= found, found
    assert len(gets(client)) >= 10, gets(client)
    assert posts(client), "the write routes must be in the sweep too"


def test_no_route_answers_a_stranger(bare) -> None:
    """Identity comes from the header Tailscale sets and from nowhere else. A login the box
    has never seen is not an error to apologise for - it is a refusal."""
    client, _db = bare
    problems = []
    for path in gets(client) + posts(client):
        if path in PUBLIC:
            continue
        for headers in ({}, {"Tailscale-User-Login": STRANGER}):
            method = client.post if path in posts(client) else client.get
            resp = method(path, headers=headers)  # type: ignore[operator]
            if resp.status_code not in (401, 403):
                problems.append(f"{path} {headers or 'anonymous'}: {resp.status_code}")
    assert not problems, "\n".join(problems)


def test_no_route_breaks_for_a_person_with_no_data(bare) -> None:
    """Her, right now: paired, no profile, no targets, no menu, no weigh-in. The page has to
    open anyway - a 500 here is somebody staring at a broken tab with nothing to do about it."""
    client, _db = bare
    problems = []
    for path in gets(client):
        resp = client.get(path, headers={"Tailscale-User-Login": OWNER_LOGIN})
        if resp.status_code >= 500:
            problems.append(f"{path}: {resp.status_code} {resp.text[:120]}")
    assert not problems, "\n".join(problems)


def test_no_route_breaks_with_data_either(onboarded) -> None:
    client, _db = onboarded
    problems = [
        f"{p}: {r.status_code}"
        for p in gets(client)
        if (r := client.get(p, headers={"Tailscale-User-Login": OWNER_LOGIN})).status_code >= 500
    ]
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize("junk", JUNK)
def test_no_query_string_can_break_a_route(onboarded, junk: str) -> None:
    """Every parameter the page reads is a string somebody could have typed."""
    client, _db = onboarded
    params = {k: junk for k in ("day", "week", "list", "recent", "from", "to")}
    problems = [
        f"{p}?{junk!r}: {r.status_code} {r.text[:100]}"
        for p in gets(client)
        if (
            r := client.get(p, params=params, headers={"Tailscale-User-Login": OWNER_LOGIN})
        ).status_code
        >= 500
    ]
    assert not problems, "\n".join(problems)


def test_no_write_route_writes_on_a_malformed_body(onboarded) -> None:
    """The fence has to hold before anything reaches the database, not after."""
    client, db = onboarded
    csrf = client.get("/api/me", headers={"Tailscale-User-Login": OWNER_LOGIN}).json()["csrf"]
    headers = {"Tailscale-User-Login": OWNER_LOGIN, "X-CSRF-Token": csrf}
    bodies: list[Any] = [
        {},
        {"item_id": "x", "ticked": "maybe"},
        {"recipe_id": None, "score": 99},
        {"item_id": 1, "ticked": True, "surprise": 1},
        {"item_id": 9**40, "ticked": True},
        {"recipe_id": 9**40, "score": 3},
        [],
        "not json at all",
        {"score": 3, "recipe_id": 1, "note": {"nested": True}},
    ]
    before = {
        t: db.scalar(f"SELECT COUNT(*) FROM {t}")
        for t in ("shopping_ticks", "ratings", "web_events")
    }
    problems = []
    for path in posts(client):
        for body in bodies:
            resp = (
                client.post(path, content=body, headers=headers)
                if isinstance(body, str)
                else client.post(path, json=body, headers=headers)
            )
            if resp.status_code >= 500:
                problems.append(f"{path} {body!r}: {resp.status_code} {resp.text[:100]}")
    after = {t: db.scalar(f"SELECT COUNT(*) FROM {t}") for t in before}
    assert not problems, "\n".join(problems)
    assert after == before, f"a malformed write got through: {before} -> {after}"


def test_no_write_route_accepts_a_missing_or_wrong_csrf_token(onboarded) -> None:
    client, _db = onboarded
    problems = []
    for path in posts(client):
        for headers in (
            {"Tailscale-User-Login": OWNER_LOGIN},
            {"Tailscale-User-Login": OWNER_LOGIN, "X-CSRF-Token": "wrong"},
        ):
            resp = client.post(path, json={}, headers=headers)
            if resp.status_code != 403:
                problems.append(f"{path} {headers}: {resp.status_code}")
    assert not problems, "\n".join(problems)


def test_health_says_what_is_wrong_without_saying_who_anyone_is(bare) -> None:
    """The health endpoint is unauthenticated, so it must carry no personal data."""
    body = bare[0].get("/api/health").json()
    assert body["status"] in ("ok", "degraded")
    flat = str(body).lower()
    for private in ("owner", "sam", OWNER_LOGIN, "1000001", "weight", "kcal"):
        assert private.lower() not in flat, f"health leaked {private!r}: {flat[:200]}"


def test_the_page_itself_opens_for_someone_with_nothing(bare) -> None:
    """A first visit, before onboarding, is the most likely first visit there is."""
    resp = bare[0].get("/", headers={"Tailscale-User-Login": OWNER_LOGIN})
    assert resp.status_code == 200
    assert "<html" in resp.text.lower()


def test_a_day_far_outside_any_data_is_answered_not_refused(onboarded) -> None:
    """Scrolling back through an empty history must not look like a broken page."""
    client, _db = onboarded
    for day in (dt.date(2019, 1, 1), dt.date(2039, 12, 31)):
        resp = client.get(
            "/api/today",
            params={"day": day.isoformat()},
            headers={"Tailscale-User-Login": OWNER_LOGIN},
        )
        assert resp.status_code < 500, f"{day}: {resp.status_code} {resp.text[:120]}"
