"""A person can plan and shop from their own recipes with no model."""

from __future__ import annotations

import datetime as dt
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from tiffin.app import App
from tiffin.db import Db
from tiffin.food import catalogue, manual, menu
from tiffin.secrets import Secrets
from tiffin.web.app import create_app
from tiffin.web.identity import issue_login_link

from .signin import sign_in
from .test_food_catalogue import BASE_YAML, write_csv, write_yaml


def test_manual_recipe_menu_and_shopping_are_person_scoped(
    fixture_db: Db, two_persons: dict[str, int], frozen_clock, tmp_path
) -> None:
    db = fixture_db
    owner, partner = two_persons["owner"], two_persons["partner"]
    catalogue.build(db, write_yaml(tmp_path, BASE_YAML), write_csv(tmp_path))
    foods = manual.library(db, owner)["foods"]
    assert "olive_oil" not in {f["code"] for f in foods}
    body = {
        "name": "Chicken and rice",
        "ingredients": [{"code": "chicken_breast", "g": 200}, {"code": "rice_basmati", "g": 100}],
        "method": ["Cook the rice.", "Cook the chicken until done."],
        "prep_minutes": 25,
        "servings": 1,
    }
    recipe_id = manual.add_recipe(db, owner, body, clock=frozen_clock)
    assert recipe_id in {r["id"] for r in manual.library(db, owner)["recipes"]}
    assert recipe_id not in {r["id"] for r in manual.library(db, partner)["recipes"]}
    first = frozen_clock.today_local() + dt.timedelta(days=1)
    menu_id, list_id = manual.save_menu(
        db,
        owner,
        first,
        3,
        [{"slot": "dinner", "recipe_id": recipe_id, "portion": 1.0}],
        clock=frozen_clock,
    )
    assert list_id is not None
    assert menu.accepted_menu_id(db, owner, first) == menu_id
    assert menu.accepted_menu_id(db, owner, first + dt.timedelta(days=2)) == menu_id
    assert menu.accepted_menu_id(db, partner, first) is None
    bought = db.all("SELECT packs FROM shopping_items WHERE list_id = ?", (list_id,))
    assert len(bought) == 2 and all(int(row["packs"]) > 0 for row in bought)
    with pytest.raises(ValueError, match="available dishes"):
        manual.save_menu(
            db,
            partner,
            first,
            1,
            [{"slot": "dinner", "recipe_id": recipe_id, "portion": 1.0}],
            clock=frozen_clock,
        )


def test_manual_input_rejects_unavailable_food_and_duplicate_slots(
    fixture_db: Db, two_persons: dict[str, int], frozen_clock, tmp_path
) -> None:
    db = fixture_db
    owner = two_persons["owner"]
    catalogue.build(db, write_yaml(tmp_path, BASE_YAML), write_csv(tmp_path))
    with pytest.raises(ValueError, match="available catalogue"):
        manual.add_recipe(
            db,
            owner,
            {
                "name": "Unavailable",
                "ingredients": [{"code": "olive_oil", "g": 20}],
                "method": ["Mix."],
                "prep_minutes": 5,
                "servings": 1,
            },
            clock=frozen_clock,
        )
    assert db.scalar("SELECT COUNT(*) FROM recipes") == 0
    recipe_id = manual.add_recipe(
        db,
        owner,
        {
            "name": "Chicken",
            "ingredients": [{"code": "chicken_breast", "g": 200}],
            "method": ["Cook thoroughly."],
            "prep_minutes": 15,
            "servings": 1,
        },
        clock=frozen_clock,
    )
    meal = {"slot": "dinner", "recipe_id": recipe_id, "portion": 1.0}
    with pytest.raises(ValueError, match="slot once"):
        manual.save_menu(db, owner, frozen_clock.today_local(), 1, [meal, meal], clock=frozen_clock)
    assert db.scalar("SELECT COUNT(*) FROM menus") == 0


def test_manual_web_flow_uses_session_identity_and_csrf(
    config, fixture_db: Db, two_persons: dict[str, int], frozen_clock, tmp_path
) -> None:
    catalogue.build(fixture_db, write_yaml(tmp_path, BASE_YAML), write_csv(tmp_path))
    config = replace(config, paths=replace(config.paths, db=fixture_db.path))
    config.data["web"].update(
        {"identity_mode": "telegram", "root_path": "", "origin": "https://tiffin.example.com"}
    )
    tiffin = App(config, Secrets(config.paths.secrets, {}), clock=frozen_clock)
    owner = tiffin.registry(fixture_db).require("owner")
    partner = tiffin.registry(fixture_db).require("partner")
    client = TestClient(create_app(tiffin), base_url="https://tiffin.example.com")
    sign_in(client, issue_login_link(tiffin, fixture_db, owner))
    csrf = client.get("/api/me").json()["csrf"]
    headers = {"X-CSRF-Token": csrf, "Origin": "https://tiffin.example.com"}
    assert client.get("/api/manual").status_code == 200
    recipe = {
        "name": "Chicken and rice",
        "ingredients": [{"code": "chicken_breast", "g": 200}],
        "method": ["Cook the chicken."],
        "prep_minutes": 15,
        "servings": 1,
    }
    assert client.post("/api/manual/recipes", json=recipe).status_code == 403
    saved = client.post("/api/manual/recipes", json=recipe, headers=headers)
    assert saved.status_code == 200, saved.text
    recipe_id = saved.json()["recipe_id"]
    body = {
        "start": "2026-09-08",
        "days": 2,
        "meals": [{"slot": "dinner", "recipe_id": recipe_id, "portion": 1.0}],
    }
    assert (
        client.post(
            "/api/manual/menu",
            json=body,
            headers={**headers, "Origin": "https://other.example.com"},
        ).status_code
        == 403
    )
    saved_menu = client.post("/api/manual/menu", json=body, headers=headers)
    assert saved_menu.status_code == 200, saved_menu.text
    assert saved_menu.json()["list_id"] is not None
    assert menu.accepted_menu_id(fixture_db, owner.id, dt.date(2026, 9, 8)) is not None
    assert menu.accepted_menu_id(fixture_db, partner.id, dt.date(2026, 9, 8)) is None
