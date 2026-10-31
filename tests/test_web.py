"""The page: identity before routing, every read scoped to the asker, the write fence, the
security headers, and the pinned Plotly bundle."""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tiffin import body as body_mod
from tiffin.app import App
from tiffin.clock import FrozenClock
from tiffin.db import Db
from tiffin.secrets import Secrets
from tiffin.web import queries, vendor
from tiffin.web.app import create_app

from .fakes import FakeTelegram

OWNER_LOGIN = "alex@example.com"
PARTNER_LOGIN = "sam@example.com"
DAY = dt.date(2026, 9, 7)


def seed(db: Db, pid: int, *, kcal: int, weight: float, steps: int, dish: str) -> int:
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
        goal_weight_kg=87.0,
        goal_date="2027-01-24",
    )
    db.insert(
        "targets",
        person_id=pid,
        week_start="2026-09-07",
        kcal=kcal,
        protein_g=200,
        fat_g=70,
        carb_g=200,
        fibre_g=30,
        source="formula",
    )
    body_mod.record_weigh_in(db, pid, DAY, weight, "manual")
    db.insert("icu_wellness", person_id=pid, day="2026-09-07", steps=steps, icu_raw_id=0)
    plan = db.insert("training_plans", person_id=pid, effective_from="2026-09-01")
    db.insert(
        "planned_sessions", plan_id=plan, person_id=pid, weekday=0, kind="climb", target_minutes=90
    )
    ing = db.insert(
        "ingredients",
        code=f"rice{pid}",
        version=1,
        name="Rice",
        kcal_100=350,
        protein_100=8,
        fat_100=1,
        carb_100=78,
        fibre_100=1.4,
        pack_g=1000,
        pack_label="1 kg bag",
        aisle="grains",
        pantry=0,
        use_whole_pack=0,
        approved=1,
        person_scope="both",
    )
    recipe = db.insert(
        "recipes",
        name=dish,
        servings=1,
        prep_minutes=20,
        method_json='["Cook it."]',
        source="model",
    )
    db.insert("recipe_ingredients", recipe_id=recipe, ingredient_id=ing, grams=100)
    menu = db.insert(
        "menus",
        person_id=pid,
        segment_start="2026-09-07",
        segment_end="2026-09-11",
        status="accepted",
    )
    menu_day = db.insert(
        "menu_days",
        menu_id=menu,
        day="2026-09-07",
        kcal=kcal,
        protein_g=200,
        fat_g=70,
        carb_g=200,
        fibre_g=30,
    )
    db.insert(
        "meal_slots",
        menu_day_id=menu_day,
        slot="dinner",
        recipe_id=recipe,
        portion_multiplier=1.0,
        portion_g=100,
        kcal=350,
        protein_g=8,
        fat_g=1,
        carb_g=78,
        fibre_g=1.4,
    )
    # confirmed AFTER the menu exists, the order it happens in: a menu can never write a
    # day that is already confirmed by the append-only trigger
    db.insert(
        "day_confirmations",
        person_id=pid,
        day="2026-09-07",
        status="confirmed",
        basis="menu",
        kcal=kcal - 100,
        protein_g=190,
        fat_g=68,
        carb_g=190,
        fibre_g=28,
    )
    slist = db.insert("shopping_lists", menu_id=menu, kind="segment", person_id=pid)
    db.insert(
        "shopping_items",
        list_id=slist,
        ingredient_id=ing,
        grams_needed=500,
        packs=1,
        leftover_g=500,
        aisle="grains",
    )
    db.insert(
        "messages_out",
        person_id=pid,
        chat_id=1,
        kind="sunday_summary",
        due_key="2026-09-13",
        text_html=f"<b>Week</b> {dish}",
        status="sent",
        sent_at="2026-09-13 18:00:00",
    )
    return recipe


@pytest.fixture
def client(frozen_clock: FrozenClock, config, tmp_path):
    """The page opens its own connections at `config.paths.db`, so seed exactly that file."""
    config.paths.ensure()
    db = Db.open(config.paths.db, clock=frozen_clock)
    owner_id = db.insert("persons", slug="owner", display_name="Alex", tone="blunt")
    partner_id = db.insert("persons", slug="partner", display_name="Sam", tone="warm")
    db.insert("telegram_identities", person_id=owner_id, chat_id=1000001, method="config")
    db.insert("tailscale_identities", person_id=owner_id, login=OWNER_LOGIN)
    db.insert("tailscale_identities", person_id=partner_id, login=PARTNER_LOGIN)
    seed(db, owner_id, kcal=2250, weight=100.9, steps=11000, dish="Alex's chilli")
    seed(db, partner_id, kcal=1700, weight=67.4, steps=7000, dish="Sam's salad")
    secrets_dir = tmp_path / "s"
    secrets_dir.mkdir()
    (secrets_dir / "telegram-token-dev.txt").write_text("1:t", encoding="utf-8")
    config.data["web"]["secure_cookies"] = False
    # served at the root in tests; Tailscale Serve supplies the /tiffin prefix in
    # production and route_path() collapses it (covered by test_mount_prefix_collapses)
    config.data["web"]["root_path"] = ""
    app = App(config, Secrets(secrets_dir), clock=frozen_clock, transport=FakeTelegram())
    client = TestClient(create_app(app), base_url="http://testserver")
    try:
        yield client, owner_id, partner_id, db
    finally:
        db.close()


def as_owner(c: TestClient, path: str, **kw):
    return c.get(path, headers={"Tailscale-User-Login": OWNER_LOGIN}, **kw)


def test_identity_is_required_and_resolved(client) -> None:
    c, owner_id, _partner, _db = client
    anon = c.get("api/today")
    assert anon.status_code == 403 and anon.json()["error"] == "no identity"
    assert anon.headers["content-security-policy"]
    assert anon.headers["cache-control"] == "private, no-store"
    unknown = c.get("api/today", headers={"Tailscale-User-Login": "stranger@example.com"})
    assert unknown.status_code == 403
    ok = as_owner(c, "api/whoami")
    assert ok.status_code == 200 and ok.json() == {"slug": "owner", "person_id": owner_id}
    # health needs no identity and carries nothing personal
    health = c.get("api/health")
    assert health.status_code == 200
    payload = health.json()
    assert payload["status"] in ("ok", "degraded") and "person" not in payload
    assert set(payload) >= {"env", "version", "db", "heartbeat_age_s", "last_backup"}


def test_owner_status_includes_intervals_failures(client) -> None:
    c, owner_id, _partner, db = client
    db.insert(
        "run_ledger",
        run_id="icu-example",
        job="icu_sync",
        slot="2026-09-07T2010",
        person_id=owner_id,
        started_at=db.clock.utc_stamp(),
        ended_at=db.clock.utc_stamp(),
        status="degraded",
        detail="rate limit; next hour retries",
    )

    items = as_owner(c, "api/status").json()["items"]
    assert any("icu sync" in item["what"] and "rate limit" in item["why"] for item in items)


def test_reads_only_ever_show_the_askers_rows(client) -> None:
    c, _owner, _partner, _db = client
    today = as_owner(c, "api/today").json()
    assert today["targets"]["kcal"] == 2250
    assert today["meals"][0]["name"] == "Alex's chilli"
    assert "Sam's salad" not in str(today)
    assert today["trend"]["trend_kg"] == pytest.approx(100.9)
    assert today["wellness"]["steps"] == 11000
    partners = c.get("api/today", headers={"Tailscale-User-Login": PARTNER_LOGIN}).json()
    assert partners["targets"]["kcal"] == 1700 and partners["meals"][0]["name"] == "Sam's salad"
    assert "Alex's chilli" not in str(partners)
    assert as_owner(c, "api/week").json()["summary"] is not None
    for path in ("api/week", "api/menu", "api/shopping", "api/dishes", "api/summary/weekly"):
        mine = str(as_owner(c, path).json())
        assert "Sam's salad" not in mine, path
    trends = as_owner(c, "api/trends?metric=weight&days=90").json()
    assert trends["raw"] == [pytest.approx(100.9)] and trends["goal"] == 87.0
    intake = as_owner(c, "api/trends?metric=intake&days=30").json()
    assert intake["raw"] == [2150.0] and intake["target"] == [2250]
    assert as_owner(c, "api/trends?metric=nope").status_code == 400


def test_today_recipe_queries_do_not_grow_with_meal_count(client) -> None:
    _client, owner_id, _partner, db = client

    def read_count() -> int:
        statements: list[str] = []
        db.con.set_trace_callback(statements.append)
        try:
            queries.meals(db, owner_id, DAY)
        finally:
            db.con.set_trace_callback(None)
        return len(statements)

    one_meal = read_count()
    menu_day_id = int(
        db.scalar(
            "SELECT md.id FROM menu_days md JOIN menus m ON m.id = md.menu_id "
            "WHERE m.person_id = ? AND md.day = ?",
            (owner_id, DAY.isoformat()),
        )
    )
    recipe_id = int(
        db.scalar("SELECT recipe_id FROM meal_slots WHERE menu_day_id = ?", (menu_day_id,))
    )
    for slot in ("breakfast", "lunch", "snack", "pre_bed"):
        db.insert(
            "meal_slots",
            menu_day_id=menu_day_id,
            slot=slot,
            recipe_id=recipe_id,
            portion_multiplier=1.0,
            portion_g=100,
            kcal=350,
            protein_g=8,
            fat_g=1,
            carb_g=78,
            fibre_g=1.4,
        )
    assert len(queries.meals(db, owner_id, DAY)) == 5
    assert read_count() == one_meal


def test_write_fence(client) -> None:
    c, owner_id, _partner, db = client
    me = as_owner(c, "api/me")
    csrf = me.json()["csrf"]
    assert me.cookies.get("tiffin_csrf") == csrf
    item_id = int(
        db.scalar(
            "SELECT si.id FROM shopping_items si JOIN shopping_lists sl ON sl.id = si.list_id "
            "WHERE sl.person_id = ?",
            (owner_id,),
        )
    )
    headers = {"Tailscale-User-Login": OWNER_LOGIN, "X-CSRF-Token": csrf}
    # no token
    bad = c.post(
        "api/shopping/tick",
        json={"item_id": item_id, "ticked": True},
        headers={"Tailscale-User-Login": OWNER_LOGIN},
    )
    assert bad.status_code == 403
    # unknown field, before any row is written
    unknown = c.post(
        "api/shopping/tick", json={"item_id": item_id, "ticked": True, "x": 1}, headers=headers
    )
    assert unknown.status_code == 400 and "unknown fields" in unknown.json()["detail"]
    assert db.scalar("SELECT COUNT(*) FROM shopping_ticks") == 0
    # foreign origin
    foreign = c.post(
        "api/shopping/tick",
        json={"item_id": item_id, "ticked": True},
        headers={**headers, "Origin": "https://evil.example"},
    )
    assert foreign.status_code == 403
    foreign_with_fetch_header = c.post(
        "api/shopping/tick",
        json={"item_id": item_id, "ticked": True},
        headers={
            **headers,
            "Origin": "https://evil.example",
            "Sec-Fetch-Site": "same-origin",
        },
    )
    assert foreign_with_fetch_header.status_code == 403
    good = c.post("api/shopping/tick", json={"item_id": item_id, "ticked": True}, headers=headers)
    assert good.status_code == 200 and good.json()["ticked"] is True
    assert db.scalar("SELECT ticked FROM shopping_ticks WHERE item_id = ?", (item_id,)) == 1
    assert (
        db.scalar("SELECT kind FROM web_events WHERE person_id = ?", (owner_id,)) == "shopping_tick"
    )
    # her item is invisible to him
    partner_item = int(
        db.scalar(
            "SELECT si.id FROM shopping_items si JOIN shopping_lists sl ON sl.id = si.list_id "
            "WHERE sl.person_id <> ?",
            (owner_id,),
        )
    )
    denied = c.post(
        "api/shopping/tick", json={"item_id": partner_item, "ticked": True}, headers=headers
    )
    assert denied.status_code == 404


def test_rating_writes_one_row_each(client) -> None:
    c, owner_id, _partner, db = client
    csrf = as_owner(c, "api/me").json()["csrf"]
    headers = {"Tailscale-User-Login": OWNER_LOGIN, "X-CSRF-Token": csrf}
    recipe_id = int(
        db.scalar(
            "SELECT rc.id FROM recipes rc JOIN meal_slots ms ON ms.recipe_id = rc.id "
            "JOIN menu_days md ON md.id = ms.menu_day_id JOIN menus m ON m.id = md.menu_id "
            "WHERE m.person_id = ?",
            (owner_id,),
        )
    )
    assert (
        c.post(
            "api/dishes/rate", json={"recipe_id": recipe_id, "score": 9}, headers=headers
        ).status_code
        == 400
    )
    ok = c.post("api/dishes/rate", json={"recipe_id": recipe_id, "score": 5}, headers=headers)
    assert ok.status_code == 200
    assert db.scalar("SELECT score FROM ratings WHERE person_id = ?", (owner_id,)) == 5
    assert db.scalar("SELECT COUNT(*) FROM web_events WHERE kind = 'dish_rating'") == 1
    partner_recipe = int(
        db.scalar(
            "SELECT rc.id FROM recipes rc JOIN meal_slots ms ON ms.recipe_id = rc.id "
            "JOIN menu_days md ON md.id = ms.menu_day_id JOIN menus m ON m.id = md.menu_id "
            "WHERE m.person_id <> ?",
            (owner_id,),
        )
    )
    assert (
        c.post(
            "api/dishes/rate", json={"recipe_id": partner_recipe, "score": 5}, headers=headers
        ).status_code
        == 404
    )


def test_page_and_static_and_headers(client) -> None:
    c, _owner, _partner, _db = client
    page = c.get("/", headers={"Tailscale-User-Login": OWNER_LOGIN})
    assert page.status_code == 200 and b"<base href=" in page.content
    assert page.headers["content-security-policy"].startswith("default-src 'self'")
    assert "connect-src 'self'" in page.headers["content-security-policy"]
    assert page.headers["referrer-policy"] == "no-referrer"
    assert page.headers["x-content-type-options"] == "nosniff"
    assert page.headers["cache-control"] == "private, no-store"
    # stylesheets are hashed and cached for a year; the ES modules import each other by name,
    # so they are served by name and never marked immutable
    found = re.search(rb'href="static/(app\.[0-9a-f]+\.css)"', page.content)
    assert found is not None
    name = found.group(1).decode()
    asset = c.get(f"static/{name}", headers={"Tailscale-User-Login": OWNER_LOGIN})
    assert (
        asset.status_code == 200
        and asset.headers["cache-control"] == "public, max-age=31536000, immutable"
    )
    assert b'<script type="module" src="static/app.js">' in page.content
    entry = c.get("static/app.js", headers={"Tailscale-User-Login": OWNER_LOGIN})
    assert entry.status_code == 200
    assert entry.headers["cache-control"] == "private, no-cache"
    assert entry.headers["etag"]
    assert (
        c.get(
            "static/app.js",
            headers={"Tailscale-User-Login": OWNER_LOGIN, "If-None-Match": entry.headers["etag"]},
        ).status_code
        == 304
    )
    modules = c.app.state.assets.module_names
    for module in modules:
        response = c.get(f"static/{module}", headers={"Tailscale-User-Login": OWNER_LOGIN})
        assert response.status_code == 200, module
        assert response.headers["cache-control"] == "private, no-cache"
        for target in re.findall(rb'from "\./([^"]+)"', response.content):
            assert target.decode() in modules, f"{module} imports {target!r}, which is not served"
    assert c.get("static/nope.js", headers={"Tailscale-User-Login": OWNER_LOGIN}).status_code == 404
    # the page pulls nothing from outside the box
    assert b"http://" not in page.content and b"https://" not in page.content


def test_plotly_bundle_is_pinned() -> None:
    ok, detail = vendor.check_plotly()
    assert ok, detail
    assert vendor.plotly_path().exists()


def test_every_query_is_person_scoped() -> None:
    """A read never takes a person from the request, and every SQL query is scoped."""
    import ast

    checked = 0
    for module in ("queries.py", "query_shopping.py", "query_actions.py"):
        tree = ast.parse(Path(queries.__file__).with_name(module).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef) or node.name.startswith("_"):
                continue
            text = " ".join(
                n.value
                for n in ast.walk(node)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)
            )
            upper = text.upper()
            if "SELECT" not in upper or "FROM" not in upper:
                continue
            checked += 1
            assert "PERSON_ID = ?" in upper or "PERSON_ID IS NULL" in upper, node.name
            assert "person_id" in {a.arg for a in node.args.args}, node.name
    assert checked >= 10, checked


def test_mount_prefix_collapses() -> None:
    """Serve may or may not strip the mount point; routing must work either way."""
    from tiffin.web.identity import collapse_prefix, route_path

    stripped = {"path": "/api/today", "root_path": "/tiffin"}
    doubled = {"path": "/tiffin/tiffin/api/today", "root_path": "/tiffin"}
    single = {"path": "/tiffin/api/today", "root_path": "/tiffin"}
    assert route_path(stripped) == "/api/today"
    assert route_path(single) == "/api/today"
    assert route_path(doubled) == "/api/today"
    assert route_path({"path": "/tiffin", "root_path": "/tiffin"}) == "/"
    for scope in (stripped, doubled, single):
        collapse_prefix(scope)
        assert scope["path"] == "/tiffin/api/today"
