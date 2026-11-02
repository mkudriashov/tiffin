"""GET /tiffin/api/version: release and commit for the container's own check and the owner only."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tiffin import release
from tiffin.app import App
from tiffin.secrets import Secrets
from tiffin.web.app import create_app

OWNER = "owner@example.com"
OTHER = "partner@example.com"


def test_release_and_commit_are_read_and_validated() -> None:
    env = {"TIFFIN_RELEASE": "2026.11.02-3", "TIFFIN_COMMIT": "a" * 40}
    assert release.info(env) == {
        "component": "tiffin",
        "release": "2026.11.02-3",
        "commit": "a" * 40,
    }
    junk = {"TIFFIN_RELEASE": "x; rm -rf /", "TIFFIN_COMMIT": "not a commit"}
    got = release.info(junk)
    assert got["release"] == "unknown"
    assert got["commit"] in ("unknown", release._stamp.COMMIT.strip())


def test_an_unexpanded_stamp_reads_unknown() -> None:
    assert release.commit({}) == "unknown" or len(release.commit({})) >= 7
    assert release._COMMIT.fullmatch("$Format:%H$") is None


@pytest.fixture
def client(config, tmp_path, frozen_clock, two_persons, monkeypatch) -> TestClient:
    for name in ("TIFFIN_RELEASE", "TIFFIN_COMMIT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TIFFIN_RELEASE", "2026.11.02-9")
    monkeypatch.setenv("TIFFIN_COMMIT", "b" * 40)
    d = tmp_path / "s"
    d.mkdir()
    (d / "telegram-token-dev.txt").write_text("1:t", encoding="utf-8")
    app = App(config, Secrets(d), clock=frozen_clock, transport=lambda *a, **k: None)  # type: ignore[arg-type]
    app.bootstrap().close()
    db = app.open_db()
    try:
        db.insert("tailscale_identities", person_id=two_persons["owner"], login=OWNER)
        db.insert("tailscale_identities", person_id=two_persons["partner"], login=OTHER)
    finally:
        db.close()
    return TestClient(create_app(app), base_url="http://testserver")


def test_the_container_itself_gets_the_reply(client: TestClient) -> None:
    # TestClient's peer is not loopback; the loopback rule is exercised in the identity tests,
    # so here the owner path and the refusals are what matter
    r = client.get("/api/version", headers={"Tailscale-User-Login": OWNER})
    assert r.status_code == 200
    assert r.json() == {"component": "tiffin", "release": "2026.11.02-9", "commit": "b" * 40}
    assert client.get("/version", headers={"Tailscale-User-Login": OWNER}).json() == r.json()


def test_a_stranger_and_the_partner_get_a_plain_404(client: TestClient) -> None:
    for headers in ({}, {"Tailscale-User-Login": OTHER}, {"Tailscale-User-Login": "x@example.com"}):
        for path in ("/api/version", "/version"):
            r = client.get(path, headers=headers)
            assert r.status_code == 404, (path, headers)
            assert "b" * 40 not in r.text and "2026.11" not in r.text


def test_the_reply_has_nothing_personal(client: TestClient) -> None:
    r = client.get("/api/version", headers={"Tailscale-User-Login": OWNER})
    assert set(r.json()) == {"component", "release", "commit"}


def test_loopback_without_proxy_headers_is_the_containers_own_check(
    client: TestClient,
) -> None:
    inside = TestClient(client.app, client=("127.0.0.1", 50000))
    assert inside.get("/api/version").status_code == 200
    # a request that came through a proxy carries one of these headers: not the container's own
    for header in ("X-Forwarded-For", "Forwarded", "X-Real-IP"):
        assert inside.get("/api/version", headers={header: "203.0.113.9"}).status_code == 404
