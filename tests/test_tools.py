"""Tool registry: MCP/OpenAI shapes, person scoping in every SQL string, writes = intents only."""

from __future__ import annotations

import ast
import inspect
import json

import pytest

from tiffin.db import Db
from tiffin.food import menu as menu_mod
from tiffin.model import tools
from tiffin.model.tools import BoundTools, ToolError

READS = (
    "get_targets",
    "get_day",
    "get_week",
    "get_trend",
    "get_sessions",
    "get_menu",
    "get_shopping",
    "get_kitchen",
)
WRITES = ("log_offplan", "confirm_day", "rate_dish", "set_pause", "set_food_prefs")
LEDGERS_WRITES_MUST_NOT_TOUCH = (
    "profiles",
    "estimates",
    "day_confirmations",
    "ratings",
    "pause_periods",
)


def test_registry_names_and_rendered_shapes() -> None:
    assert tools.READ_TOOLS == READS and tools.WRITE_TOOLS == WRITES
    mcp = tools.to_mcp()
    assert [t["name"] for t in mcp] == list(READS + WRITES)
    for t in mcp:
        assert set(t) == {"name", "description", "inputSchema"}
        assert t["inputSchema"]["type"] == "object"
        assert t["inputSchema"]["additionalProperties"] is False
        assert "person" not in t["inputSchema"]["properties"]
        assert t["description"] and len(t["description"]) < 200
    json.dumps(mcp)  # serialisable as sent to claude -p


def test_every_sql_string_in_the_module_is_person_scoped() -> None:
    tree = ast.parse(inspect.getsource(tools))
    sql: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if "SELECT" in node.value and "FROM" in node.value:
                sql.append(node.value)
        elif isinstance(node, ast.JoinedStr):
            joined = "".join(
                v.value
                for v in node.values
                if isinstance(v, ast.Constant) and isinstance(v.value, str)
            )
            if "SELECT" in joined and "FROM" in joined:
                sql.append(joined)
    assert len(sql) >= 12, "expected one SQL string per read and per scoped write check"
    for s in sql:
        assert "person_id = ?" in s, s


def seed(db: Db, pid: int, *, day: str = "2026-09-07", kcal: int = 2100) -> dict[str, int]:
    """One row per table a read tool touches, for one person; ids returned for assertions."""
    ids: dict[str, int] = {}
    db.insert(
        "targets",
        person_id=pid,
        week_start=day,
        kcal=kcal,
        protein_g=200,
        fat_g=70,
        carb_g=170,
        fibre_g=30,
        source="formula",
    )
    w1 = db.insert("weigh_ins", person_id=pid, day="2026-09-01", weight_kg=100.0, source="manual")
    w2 = db.insert("weigh_ins", person_id=pid, day=day, weight_kg=99.4, source="manual")
    db.insert(
        "trend_points",
        person_id=pid,
        day="2026-09-01",
        trend_kg=100.0,
        alpha=0.1,
        n=1,
        weigh_in_id=w1,
    )
    db.insert("trend_points", person_id=pid, day=day, trend_kg=99.7, alpha=0.1, n=2, weigh_in_id=w2)
    plan = db.insert("training_plans", person_id=pid, effective_from="2026-09-01")
    ids["session"] = db.insert(
        "planned_sessions", plan_id=plan, person_id=pid, weekday=0, kind="climb", target_minutes=90
    )
    db.insert(
        "planned_sessions", plan_id=plan, person_id=pid, weekday=1, kind="rest", target_minutes=0
    )
    db.insert(
        "session_matches",
        person_id=pid,
        day=day,
        planned_session_id=ids["session"],
        icu_id=f"i{pid}",
        status="matched",
        minutes=85,
    )
    db.insert(
        "session_matches",
        person_id=pid,
        day=day,
        planned_session_id=ids["session"],
        icu_id=f"i{pid}",
        status="confirmed",
        minutes=85,
    )
    db.execute(
        "INSERT INTO icu_activity(person_id, icu_id, day, start_utc, kind, name, moving_s, "
        "calories, avg_hr, icu_raw_id) "
        "VALUES (?, ?, ?, ?, 'climb', 'Bouldering', 5100, 600, 130, 1)",
        (pid, f"i{pid}", day, f"{day} 17:00:00"),
    )
    ing = db.insert(
        "ingredients",
        code="chicken",
        version=pid,
        name="Chicken breast",
        kcal_100=165,
        protein_100=31,
        fat_100=3.6,
        carb_100=0,
        fibre_100=0,
        pack_g=500,
        pack_label="500 g",
        aisle="meat",
    )
    ids["recipe"] = db.insert(
        "recipes", name=f"Chicken bowl {pid}", prep_minutes=20, method_json="[]", source="model"
    )
    db.insert("recipe_ingredients", recipe_id=ids["recipe"], ingredient_id=ing, grams=200)
    menu = db.insert(
        "menus",
        person_id=pid,
        segment_start="2026-09-07",
        segment_end="2026-09-11",
        status="accepted",
    )
    md = db.insert(
        "menu_days",
        menu_id=menu,
        day=day,
        kcal=kcal,
        protein_g=200,
        fat_g=70,
        carb_g=170,
        fibre_g=30,
    )
    db.insert(
        "meal_slots",
        menu_day_id=md,
        slot="lunch",
        recipe_id=ids["recipe"],
        portion_multiplier=1.0,
        portion_g=350,
        kcal=700,
        protein_g=60,
        fat_g=20,
        carb_g=60,
        fibre_g=8,
    )
    lst = db.insert("shopping_lists", menu_id=menu, kind="segment", person_id=pid)
    ids["item"] = db.insert(
        "shopping_items", list_id=lst, ingredient_id=ing, grams_needed=1000, packs=2, aisle="meat"
    )
    db.insert("shopping_ticks", item_id=ids["item"], person_id=pid, ticked=1)
    db.insert(
        "day_confirmations", person_id=pid, day=day, status="confirmed", basis="menu", kcal=kcal
    )
    est = db.insert(
        "estimates", person_id=pid, day=day, description=f"beer {pid}", kcal=200, status="pending"
    )
    db.insert(
        "estimates",
        person_id=pid,
        day=day,
        description=f"beer {pid}",
        kcal=200,
        status="accepted",
        source_estimate_id=est,
    )
    return ids


@pytest.fixture
def seeded(fixture_db: Db, two_persons) -> tuple[Db, int, int, dict[str, int], dict[str, int]]:
    owner_id, partner = two_persons["owner"], two_persons["partner"]
    a = seed(fixture_db, owner_id, kcal=2100)
    b = seed(fixture_db, partner, kcal=1600)
    return fixture_db, owner_id, partner, a, b


def test_reads_show_only_the_askers_rows(seeded) -> None:
    db, owner_id, partner, _, _ = seeded
    for pid, kcal in ((owner_id, 2100), (partner, 1600)):
        t = tools.call(db, pid, "get_targets", {})
        assert t["targets"]["kcal"] == kcal and "person_id" not in t["targets"]
        day = tools.call(db, pid, "get_day", {"day": "2026-09-07"})
        assert day["confirmation"]["kcal"] == kcal
        assert day["meals"] == [f"Chicken bowl {pid}"]
        assert [e["description"] for e in day["estimates"]] == [f"beer {pid}"]
        assert day["estimates"][0]["status"] == "accepted"
        week = tools.call(db, pid, "get_week", {"week": "2026-09-09"})
        assert week["week_start"] == "2026-09-07" and week["summary"]["confirmed_days"] == 1
        assert week["summary"]["sessions_done"] == 1 and week["summary"]["sessions_planned"] == 1
        assert week["targets"]["kcal"] == kcal and [d["kcal"] for d in week["days"]] == [kcal]
        trend = tools.call(db, pid, "get_trend", {"days": 14})
        assert trend["latest"]["trend_kg"] == 99.7 and trend["delta_kg"] == -0.3
        sessions = tools.call(db, pid, "get_sessions", {})
        assert [s["kind"] for s in sessions["planned"]] == ["climb", "rest"]
        assert [m["status"] for m in sessions["matches"]] == ["confirmed"]
        assert sessions["activities"][0]["icu_id"] == f"i{pid}"
        menu = tools.call(db, pid, "get_menu", {})
        assert (
            menu["meals"][0]["name"] == f"Chicken bowl {pid}" and menu["day_totals"]["kcal"] == kcal
        )
        assert menu["status"] == "accepted" and menu["segment"] == ["2026-09-07", "2026-09-11"]
        shop = tools.call(db, pid, "get_shopping", {})
        assert shop["items"][0]["name"] == "Chicken breast" and shop["items"][0]["ticked"] == 1
        assert shop["list"]["kind"] == "segment"
    assert (
        tools.call(db, partner, "get_shopping", {})["list"]["id"]
        != tools.call(db, owner_id, "get_shopping", {})["list"]["id"]
    )


def test_get_menu_reports_the_decision_not_the_frozen_column(fixture_db: Db, two_persons) -> None:
    """`menus` is append-only, so an accepted menu keeps status 'proposed' on the row for ever.
    Handing the model the column made it tell the person their accepted menu was still only
    proposed and send them to /proposals before shopping."""
    pid = two_persons["owner"]
    menu_id = fixture_db.insert(
        "menus",
        person_id=pid,
        segment_start="2026-09-07",
        segment_end="2026-09-11",
        status="proposed",
    )
    fixture_db.insert(
        "menu_days",
        menu_id=menu_id,
        day="2026-09-07",
        kcal=1900,
        protein_g=200,
        fat_g=70,
        carb_g=170,
        fibre_g=30,
    )
    ing = fixture_db.insert(
        "ingredients",
        code="chicken_breast",
        version=1,
        name="Chicken breast, raw",
        kcal_100=165,
        protein_100=31,
        fat_100=3.6,
        carb_100=0,
        fibre_100=0,
        pack_g=500,
        pack_label="500 g",
        aisle="meat",
    )
    recipe = fixture_db.insert(
        "recipes", name="Chicken bowl", prep_minutes=20, method_json="[]", source="model"
    )
    fixture_db.insert("recipe_ingredients", recipe_id=recipe, ingredient_id=ing, grams=200)
    md = fixture_db.one("SELECT id FROM menu_days WHERE menu_id = ?", (menu_id,))
    assert md is not None
    fixture_db.insert(
        "meal_slots",
        menu_day_id=md["id"],
        slot="lunch",
        recipe_id=recipe,
        portion_multiplier=1.0,
        portion_g=350,
        kcal=700,
        protein_g=60,
        fat_g=20,
        carb_g=60,
        fibre_g=8,
    )
    # not yet decided: nothing to correct, so the column stands
    menu_mod.decide(fixture_db, menu_id, "accepted")
    out = tools.call(fixture_db, pid, "get_menu", {"day": "2026-09-07"})
    assert out["status"] == "accepted"
    assert (
        fixture_db.scalar("SELECT status FROM menus WHERE id = ?", (menu_id,)) == "proposed"
    )  # the row never changes


def test_reads_on_an_empty_person_are_calm(fixture_db: Db, two_persons) -> None:
    pid = two_persons["partner"]
    assert tools.call(fixture_db, pid, "get_targets", {})["targets"] is None
    day = tools.call(fixture_db, pid, "get_day", {})
    assert day == {"day": "2026-09-07", "confirmation": None, "meals": [], "estimates": []}
    assert tools.call(fixture_db, pid, "get_trend", {})["latest"] is None
    assert tools.call(fixture_db, pid, "get_menu", {})["meals"] == []
    assert tools.call(fixture_db, pid, "get_shopping", {}) == {"list": None, "items": []}
    assert tools.call(fixture_db, pid, "get_week", {})["summary"] is None


def test_writes_create_intents_only(seeded) -> None:
    db, owner_id, _partner, ids, _ = seeded
    before = {t: db.scalar(f"SELECT COUNT(*) FROM {t}") for t in LEDGERS_WRITES_MUST_NOT_TOUCH}
    bound = BoundTools(db, owner_id, model_used="claude-opus-5")
    r1 = bound.call("log_offplan", {"description": "two beers"})
    r2 = bound.call("confirm_day", {"as_planned": False, "note": "skipped lunch"})
    r3 = bound.call("rate_dish", {"recipe_id": ids["recipe"], "score": 4})
    r4 = bound.call(
        "set_pause", {"start_day": "2026-09-10", "end_day": "2026-09-12", "reason": "trip"}
    )
    r5 = bound.call("set_food_prefs", {"list_name": "avoid", "add": ["chickpeas"]})
    for r in (r1, r2, r3, r4, r5):
        assert r["needs_tap"] is True and r["status"] == "pending"
    rows = db.all("SELECT * FROM intents WHERE person_id = ? ORDER BY id", (owner_id,))
    assert [r["kind"] for r in rows] == list(WRITES)
    assert [r["id"] for r in rows] == [
        r1["intent_id"],
        r2["intent_id"],
        r3["intent_id"],
        r4["intent_id"],
        r5["intent_id"],
    ]
    assert all(r["model_used"] == "claude-opus-5" for r in rows)
    assert json.loads(rows[0]["payload_json"]) == {"day": "2026-09-07", "description": "two beers"}
    assert json.loads(rows[1]["payload_json"]) == {
        "as_planned": False,
        "day": "2026-09-07",
        "note": "skipped lunch",
    }
    assert json.loads(rows[2]["payload_json"])["name"] == f"Chicken bowl {owner_id}"
    after = {t: db.scalar(f"SELECT COUNT(*) FROM {t}") for t in LEDGERS_WRITES_MUST_NOT_TOUCH}
    assert after == before


def test_writes_refuse_what_is_not_the_persons(seeded) -> None:
    db, owner_id, partner, _a, b = seeded
    with pytest.raises(ToolError, match="not on any of your menus"):
        tools.call(db, owner_id, "rate_dish", {"recipe_id": b["recipe"], "score": 5})
    with pytest.raises(ToolError, match="before start_day"):
        tools.call(db, partner, "set_pause", {"start_day": "2026-09-12", "end_day": "2026-09-10"})
    with pytest.raises(ToolError, match="YYYY-MM-DD"):
        tools.call(db, partner, "get_day", {"day": "2026-13-45"})
    with pytest.raises(ToolError, match="must not be empty"):
        tools.call(db, partner, "log_offplan", {"description": "   "})
    assert db.scalar("SELECT COUNT(*) FROM intents") == 0


def test_person_and_model_arguments_from_the_model_are_ignored(seeded) -> None:
    db, owner_id, partner, _, _ = seeded
    r = tools.call(db, owner_id, "get_targets", {"person_id": partner})
    assert r["targets"]["kcal"] == 2100
    r = tools.call(
        db,
        owner_id,
        "log_offplan",
        {"description": "x", "person_id": partner, "model_used": "evil"},
    )
    row = db.one("SELECT person_id, model_used FROM intents WHERE id = ?", (r["intent_id"],))
    assert row is not None and row["person_id"] == owner_id and row["model_used"] is None


def test_arguments_are_validated_before_the_handler_runs(fixture_db: Db, two_persons) -> None:
    pid = two_persons["owner"]
    with pytest.raises(ToolError, match="unknown tool"):
        tools.call(fixture_db, pid, "drop_tables", {})
    with pytest.raises(ToolError, match="rate_dish"):
        tools.call(fixture_db, pid, "rate_dish", {"recipe_id": 1, "score": 9})
    with pytest.raises(ToolError, match="get_trend"):
        tools.call(fixture_db, pid, "get_trend", {"days": "many"})
    with pytest.raises(ToolError, match="confirm_day"):
        tools.call(fixture_db, pid, "confirm_day", {"as_planned": True, "mood": "fine"})
    assert fixture_db.scalar("SELECT COUNT(*) FROM intents") == 0


def test_bound_tools_expose_the_registry(fixture_db: Db, two_persons) -> None:
    bound = BoundTools(fixture_db, two_persons["owner"])
    assert bound.names() == list(READS + WRITES)
    assert bound.to_mcp() == tools.to_mcp()
