"""The example household is local, disposable and isolated from normal web sign-in."""

from __future__ import annotations

from dataclasses import replace

from fastapi.testclient import TestClient

from tiffin.app import App
from tiffin.demo import make_app, seed, write_transcript
from tiffin.errors import ConfigError
from tiffin.web.app import create_app


def test_demo_pages_and_identity_boundary(tmp_path) -> None:
    demo = make_app(tmp_path)
    seed(demo)
    with demo.open_db() as db:
        owner = demo.registry(db).require("owner")
        partner = demo.registry(db).require("partner")
        assert owner.display_name == "Alex"
        assert partner.display_name == "Sam"
        assert db.scalar("SELECT COUNT(*) FROM day_confirmations") >= 30
        assert db.scalar("SELECT COUNT(*) FROM choice_options") >= 20
        assert db.scalar("SELECT COUNT(*) FROM icu_activity") >= 10
        owner_portion = db.scalar(
            "SELECT portion_g FROM meal_slots WHERE menu_day_id IN "
            "(SELECT d.id FROM menu_days d JOIN menus m ON m.id = d.menu_id "
            "WHERE m.person_id = ?) ORDER BY id LIMIT 1",
            (owner.id,),
        )
        partner_portion = db.scalar(
            "SELECT portion_g FROM meal_slots WHERE menu_day_id IN "
            "(SELECT d.id FROM menu_days d JOIN menus m ON m.id = d.menu_id "
            "WHERE m.person_id = ?) ORDER BY id LIMIT 1",
            (partner.id,),
        )
        assert owner_portion > partner_portion

    with TestClient(create_app(demo, demo_person=owner)) as client:
        assert client.get("/").status_code == 200
        for path in (
            "/api/me",
            "/api/today",
            "/api/menu",
            "/api/shopping",
            "/api/progress",
            "/api/choices",
            "/api/batch",
            "/api/dishes",
            "/api/measurements",
        ):
            response = client.get(path)
            assert response.status_code == 200, (path, response.text)
        assert client.get("/api/menu").json()["days"]
        assert client.get("/api/shopping").json()["items"]
        assert client.get("/api/choices").json()["set"]
        response = client.get("/api/me", headers={"x-dev-person": "partner"})
        assert response.json()["person"]["slug"] == "owner"

    normal = App(replace(demo.config, env="prod"), demo.secrets, clock=demo.clock)
    try:
        create_app(normal, demo_person=owner)
    except ConfigError as exc:
        assert "demo identity" in str(exc)
    else:
        raise AssertionError("normal app accepted demo identity")
    with TestClient(create_app(normal)) as client:
        assert client.get("/api/me").status_code == 403


def test_transcript_is_standalone(tmp_path) -> None:
    path = tmp_path / "example.html"
    write_transcript(path)
    content = path.read_text(encoding="utf-8")
    assert "<style>" in content
    assert "Onboarding" in content
    assert "Weekly check" in content
    assert "<script" not in content
