"""A corrected web login takes effect without changing an earlier identity row."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tiffin import admin
from tiffin.db import SCHEMA_VERSION, Db, migrate
from tiffin.persons import PersonRegistry


def test_latest_tailscale_binding_wins(fixture_db: Db, two_persons: dict[str, int], config) -> None:
    registry = PersonRegistry(fixture_db, config, clock=fixture_db.clock)
    registry.attach_tailscale_login("owner", "alex@example.com")
    first = registry.by_tailscale_login("alex@example.com")
    assert first is not None and first.id == two_persons["owner"]

    registry.attach_tailscale_login("partner", "ALEX@example.com")
    second = registry.by_tailscale_login("alex@example.com")
    owner = registry.by_id(two_persons["owner"])
    partner = registry.by_id(two_persons["partner"])
    assert second is not None and second.id == two_persons["partner"]
    assert owner is not None and owner.tailscale_login is None
    assert partner is not None and partner.tailscale_login == "ALEX@example.com"


def test_v4_tailscale_identity_migrates_without_losing_rows(frozen_clock) -> None:
    con = sqlite3.connect(":memory:", isolation_level=None)
    try:
        con.executescript(
            "CREATE TABLE schema_version(version INTEGER, applied_at TEXT);"
            "INSERT INTO schema_version VALUES (4, '2026-09-07 19:30:00');"
            "CREATE TABLE persons(id INTEGER PRIMARY KEY);"
            "INSERT INTO persons(id) VALUES (1), (2);"
            "CREATE TABLE tailscale_identities("
            "id INTEGER PRIMARY KEY, person_id INTEGER NOT NULL REFERENCES persons(id), "
            "login TEXT NOT NULL UNIQUE COLLATE NOCASE, created_at TEXT NOT NULL);"
            "CREATE INDEX ix_tailscale_person ON tailscale_identities(person_id, id DESC);"
            "INSERT INTO tailscale_identities VALUES (1, 1, 'alex@example.com', "
            "'2026-09-07 19:30:00');"
        )
        assert migrate(con, frozen_clock) == SCHEMA_VERSION
        con.execute(
            "INSERT INTO tailscale_identities(person_id, login, created_at) VALUES (2, ?, ?)",
            ("ALEX@example.com", frozen_clock.utc_stamp()),
        )
        rows = con.execute("SELECT person_id FROM tailscale_identities ORDER BY id").fetchall()
        assert rows == [(1,), (2,)]
        with pytest.raises(sqlite3.IntegrityError):
            con.execute("DELETE FROM tailscale_identities WHERE id = 1")
    finally:
        con.close()


def test_erasing_latest_binding_does_not_restore_older_access(
    fixture_db: Db, two_persons: dict[str, int], config, tmp_path: Path
) -> None:
    registry = PersonRegistry(fixture_db, config, clock=fixture_db.clock)
    registry.attach_tailscale_login("owner", "alex@example.com")
    registry.attach_tailscale_login("partner", "alex@example.com")
    manifest = admin.export_person(
        fixture_db, "partner", tmp_path / "exports", clock=fixture_db.clock
    )
    admin.erase_person(
        fixture_db, "partner", manifest_path=manifest.path / "manifest.json", clock=fixture_db.clock
    )
    assert registry.by_tailscale_login("alex@example.com") is None
    owner = registry.by_id(two_persons["owner"])
    assert owner is not None and owner.tailscale_login is None
