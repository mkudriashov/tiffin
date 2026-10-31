"""Every ledger table rejects UPDATE and DELETE; state tables do not; the two-id cap holds."""

from __future__ import annotations

import sqlite3

import pytest

from tiffin import db as dbmod
from tiffin.db import Db


def test_every_table_is_classified(fixture_db: Db) -> None:
    all_tables = set(dbmod.tables(fixture_db.con))
    ledgers = set(dbmod.ledger_tables(fixture_db.con))
    assert all_tables >= dbmod.PROJECTION_TABLES
    assert all_tables >= dbmod.STATE_TABLES
    assert ledgers.isdisjoint(dbmod.PROJECTION_TABLES | dbmod.STATE_TABLES)
    assert ledgers | dbmod.PROJECTION_TABLES | dbmod.STATE_TABLES == all_tables
    assert len(ledgers) > 50


def test_every_ledger_has_both_triggers(fixture_db: Db) -> None:
    triggers = {
        r[0] for r in fixture_db.all("SELECT name FROM sqlite_master WHERE type = 'trigger'")
    }
    for t in dbmod.ledger_tables(fixture_db.con):
        assert f"trg_{t}_noupd" in triggers, t
        assert f"trg_{t}_nodel" in triggers, t
    for t in dbmod.PROJECTION_TABLES | dbmod.STATE_TABLES:
        assert f"trg_{t}_noupd" not in triggers, t
        assert f"trg_{t}_nodel" not in triggers, t


def test_migrate_is_idempotent(fixture_db: Db) -> None:
    v1 = dbmod.migrate(fixture_db.con, fixture_db.clock)
    v2 = dbmod.migrate(fixture_db.con, fixture_db.clock)
    assert v1 == v2 == dbmod.SCHEMA_VERSION
    assert fixture_db.scalar("SELECT COUNT(*) FROM schema_version") == 1


def test_shopping_migration_adds_manual_products_to_existing_database() -> None:
    con = sqlite3.connect(":memory:")
    try:
        con.executescript(
            "CREATE TABLE schema_version (version INTEGER, applied_at TEXT);"
            "INSERT INTO schema_version VALUES (3, '2026-09-01 00:00:00');"
            "CREATE TABLE ingredients (id INTEGER PRIMARY KEY, code TEXT);"
            "CREATE TABLE shopping_items (id INTEGER PRIMARY KEY, list_id INTEGER);"
            "CREATE TABLE persons (id INTEGER PRIMARY KEY);"
        )
        assert dbmod.migrate(con) == dbmod.SCHEMA_VERSION
        assert "pack_sizes_g" in dbmod.columns(con, "ingredients")
        assert {"product_url", "currency"} <= set(dbmod.columns(con, "shopping_items"))
        assert "manual_products" in dbmod.tables(con)
        assert "trg_manual_products_noupd" in {
            str(r[0]) for r in con.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
        }
    finally:
        con.close()


def test_empty_migration_matches_schema_sql() -> None:
    expected = sqlite3.connect(":memory:")
    actual = sqlite3.connect(":memory:")
    try:
        expected.executescript(dbmod.schema_sql())
        expected.executescript("".join(dbmod.trigger_sql(t) for t in dbmod.ledger_tables(expected)))
        dbmod.migrate(actual)
        query = (
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        )
        assert actual.execute(query).fetchall() == expected.execute(query).fetchall()
    finally:
        expected.close()
        actual.close()


@pytest.mark.parametrize("table", ["persons", "weigh_ins", "messages_out", "day_confirmations"])
def test_ledger_rows_are_immutable(fixture_db: Db, two_persons: dict[str, int], table: str) -> None:
    pid = two_persons["owner"]
    rows = {
        "persons": {"slug": "owner"},
        "weigh_ins": {
            "person_id": pid,
            "day": "2026-09-07",
            "weight_kg": 100.4,
            "source": "manual",
        },
        "messages_out": {
            "person_id": pid,
            "chat_id": 1,
            "kind": "reply",
            "due_key": "x",
            "text_html": "hi",
            "status": "sent",
            "sent_at": "2026-09-07 19:30:00",
        },
        "day_confirmations": {
            "person_id": pid,
            "day": "2026-09-07",
            "status": "confirmed",
            "basis": "menu",
        },
    }
    rid = fixture_db.insert(table, **rows[table]) if table != "persons" else pid
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        fixture_db.execute(
            f"UPDATE {table} SET created_at = '2000-01-01 00:00:00' WHERE id = ?", (rid,)
        )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        fixture_db.execute(f"DELETE FROM {table} WHERE id = ?", (rid,))
    assert fixture_db.scalar(f"SELECT COUNT(*) FROM {table} WHERE id = ?", (rid,)) == 1


def test_state_tables_are_mutable(fixture_db: Db) -> None:
    fixture_db.state_set("update_offset", "10")
    fixture_db.state_set("update_offset", "11")
    assert fixture_db.state_get("update_offset") == "11"
    fixture_db.execute("DELETE FROM bot_state WHERE k = 'update_offset'")
    assert fixture_db.state_get("update_offset") is None


def test_two_people_hard_cap(fixture_db: Db, two_persons: dict[str, int]) -> None:
    assert fixture_db.insert(
        "telegram_identities", person_id=two_persons["owner"], chat_id=999, method="pairing_code"
    ), "a new phone for somebody already paired is bound, not refused"
    assert fixture_db.scalar("SELECT COUNT(*) FROM telegram_identities") == 3
    assert fixture_db.scalar("SELECT COUNT(DISTINCT person_id) FROM telegram_identities") == 2

    # and the cap itself still counts people. A third person cannot exist to test it with -
    # `persons.slug` is CHECKed to max/her - so the condition is what there is to assert.
    sql = str(
        fixture_db.scalar(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = 'trg_ident_cap'"
        )
    )
    assert "COUNT(DISTINCT person_id)" in sql and "NEW.person_id" in sql


def test_person_slug_is_constrained(fixture_db: Db) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        fixture_db.insert("persons", slug="someone", display_name="x")


def test_sent_messages_are_unique_per_slot(fixture_db: Db, two_persons: dict[str, int]) -> None:
    pid = two_persons["owner"]
    base = {
        "person_id": pid,
        "chat_id": 1,
        "kind": "morning_targets",
        "due_key": "2026-09-08",
        "text_html": "t",
    }
    fixture_db.insert("messages_out", status="failed", error="boom", **base)
    fixture_db.insert("messages_out", status="sent", sent_at="2026-09-08 06:30:00", **base)
    with pytest.raises(sqlite3.IntegrityError):
        fixture_db.insert("messages_out", status="sent", sent_at="2026-09-08 06:31:00", **base)


def test_foreign_keys_enforced(fixture_db: Db) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        fixture_db.insert(
            "weigh_ins", person_id=42, day="2026-09-07", weight_kg=80.0, source="manual"
        )
