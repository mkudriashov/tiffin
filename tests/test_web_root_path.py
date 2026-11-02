"""`web.root_path = /tiffin` behind a reverse proxy, whether it strips the prefix or keeps it.

uvicorn prepends the root path to every path it receives, so a proxy that strips the prefix
reaches the app as `/tiffin/x` and one that keeps it as `/tiffin/tiffin/x`. Both must serve
pages, assets, the API and the manifest, and every address the app writes must carry the prefix.
"""

from __future__ import annotations

import re
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from tiffin.app import App
from tiffin.db import Db
from tiffin.secrets import Secrets
from tiffin.web.app import create_app
from tiffin.web.identity import issue_login_link

ORIGIN = "https://tiffin.example.com"
ROOT = "/tiffin"
SHAPES = {"stripping": ROOT, "keeping": ROOT + ROOT}


def _app(config, fixture_db: Db, frozen_clock, **web) -> App:
    config = replace(config, paths=replace(config.paths, db=fixture_db.path))
    config.data["web"].update(
        {"identity_mode": "telegram", "root_path": ROOT, "origin": ORIGIN, **web}
    )
    return App(config, Secrets(config.paths.secrets, {}), clock=frozen_clock)


def _client(app: App) -> TestClient:
    return TestClient(create_app(app), base_url=ORIGIN, root_path=ROOT)


def _signed_in(app: App, db: Db, prefix: str) -> TestClient:
    client = _client(app)
    link = issue_login_link(app, db, app.registry(db).require("owner"))
    assert link.startswith(f"{ORIGIN}{ROOT}/?k=")
    page = client.get(prefix + "/?k=" + link.split("k=", 1)[1])
    assert page.status_code == 200
    token = re.search(r'name="k" value="([^"]+)"', page.text)
    action = re.search(r'<form method="post" action="([^"]*)"', page.text)
    assert token and action and action.group(1) == ROOT + "/"
    done = client.post(prefix + "/", data={"k": token.group(1)}, follow_redirects=False)
    assert done.status_code == 303 and done.headers["location"] == ROOT + "/"
    cookie = done.headers["set-cookie"]
    assert f"Path={ROOT};" in cookie or f"path={ROOT};" in cookie.lower()
    return client


@pytest.mark.parametrize("prefix", SHAPES.values(), ids=SHAPES.keys())
def test_page_api_assets_and_manifest_under_the_prefix(
    config, fixture_db, two_persons, frozen_clock, prefix
) -> None:
    app = _app(config, fixture_db, frozen_clock)
    client = _signed_in(app, fixture_db, prefix)
    owner = app.registry(fixture_db).require("owner")

    page = client.get(prefix + "/")
    assert page.status_code == 200 and f'<base href="{ROOT}/">' in page.text
    assert client.get(prefix + "/api/whoami").json()["person_id"] == owner.id
    assert client.get(prefix + "/api/me").status_code == 200

    # every asset the page names resolves against <base> to the prefixed address and loads
    refs = re.findall(r'(?:href|src)="(static/[^"]+)"', page.text)
    assert refs, "the page names no assets"
    for ref in refs:
        response = client.get(prefix + "/" + ref)
        assert response.status_code == 200, ref
        assert response.headers["content-type"].split(";")[0] != "application/json", ref

    manifest = client.get(prefix + "/manifest.webmanifest").json()
    assert manifest["start_url"] == ROOT + "/" and manifest["scope"] == ROOT + "/"


@pytest.mark.parametrize("prefix", SHAPES.values(), ids=SHAPES.keys())
def test_the_manifest_is_fetched_with_the_cookie_or_it_is_a_refusal(
    config, fixture_db, two_persons, frozen_clock, prefix
) -> None:
    """Browsers fetch a manifest without cookies unless the link says `use-credentials`; the
    manifest sits behind sign-in, so without it an installed page would never get one."""
    app = _app(config, fixture_db, frozen_clock)
    signed_out = _client(app).get(prefix + "/manifest.webmanifest")
    assert signed_out.status_code == 403
    page = _signed_in(app, fixture_db, prefix).get(prefix + "/").text
    assert re.search(r'<link rel="manifest" href="manifest.webmanifest"[^>]*use-credentials', page)


def test_the_page_carries_no_absolute_address_outside_the_prefix(
    config, fixture_db, two_persons, frozen_clock
) -> None:
    app = _app(config, fixture_db, frozen_clock)
    page = _signed_in(app, fixture_db, ROOT).get(ROOT + "/").text
    for ref in re.findall(r'(?:href|src|action)="(/[^"]*)"', page):
        assert ref.startswith(ROOT + "/"), ref
    assert "http://" not in page.replace("http://www.w3.org", "")


@pytest.mark.parametrize("prefix", SHAPES.values(), ids=SHAPES.keys())
def test_health_is_open_and_the_rest_refuses_under_either_shape(
    config, fixture_db, two_persons, frozen_clock, prefix
) -> None:
    client = _client(_app(config, fixture_db, frozen_clock))
    assert client.get(prefix + "/api/health").status_code in (200, 503)
    assert client.get(prefix + "/api/whoami").status_code == 403
    assert client.get(prefix + "/api/me").status_code == 403
    assert client.get(prefix + "/").status_code == 403


@pytest.mark.parametrize("prefix", SHAPES.values(), ids=SHAPES.keys())
def test_a_signed_out_visit_to_the_root_goes_to_the_landing(
    config, fixture_db, two_persons, frozen_clock, prefix
) -> None:
    app = _app(config, fixture_db, frozen_clock, signed_out_redirect=ROOT + "/overview/")
    response = _client(app).get(prefix + "/", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == ROOT + "/overview/"


@pytest.mark.parametrize("prefix", SHAPES.values(), ids=SHAPES.keys())
def test_writes_work_under_the_prefix_with_the_one_origin(
    config, fixture_db, two_persons, frozen_clock, prefix
) -> None:
    app = _app(config, fixture_db, frozen_clock)
    client = _signed_in(app, fixture_db, prefix)
    csrf = client.get(prefix + "/api/me").json()["csrf"]
    device = client.get(prefix + "/api/devices").json()["devices"][0]["id"]
    wrong = client.post(
        prefix + "/api/devices/sign-out",
        json={"device_id": device},
        headers={"X-CSRF-Token": csrf, "Origin": "https://other.example.com"},
    )
    assert wrong.status_code == 403
    right = client.post(
        prefix + "/api/devices/sign-out",
        json={"device_id": device},
        headers={"X-CSRF-Token": csrf, "Origin": ORIGIN},
    )
    assert right.status_code == 200
    assert client.get(prefix + "/api/whoami").status_code == 403
