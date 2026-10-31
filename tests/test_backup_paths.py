"""Backup retention touches only dated backup directories."""

from __future__ import annotations

from pathlib import Path

import pytest

from tiffin import backup
from tiffin.backup import run_backup
from tiffin.db import Db


def test_backup_rejects_a_day_that_is_a_path(fixture_db: Db, tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        run_backup(Path(fixture_db.path), tmp_path / "backups", "../outside")
    assert not (tmp_path / "backups").exists()


def test_backup_prunes_only_valid_old_day_directories(fixture_db: Db, tmp_path: Path) -> None:
    root = tmp_path / "backups"
    old = root / "2026-07-01"
    unrelated = root / "0000000000"
    old.mkdir(parents=True)
    unrelated.mkdir()
    fixture_db.execute('CREATE TABLE "odd""name" (id INTEGER)')

    result = run_backup(Path(fixture_db.path), root, "2026-09-07")

    assert result["pruned"] == ["2026-07-01"]
    assert not old.exists() and unrelated.is_dir()
    assert result["tables"]['odd"name'] == 0


def test_failed_refresh_keeps_previous_verified_backup(
    fixture_db: Db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "backups"
    run_backup(Path(fixture_db.path), root, "2026-09-07")
    previous = {p.name: p.read_bytes() for p in (root / "2026-09-07").iterdir()}

    def fail_hash(path: Path) -> str:
        raise OSError("simulated disk failure")

    monkeypatch.setattr(backup, "sha256_of", fail_hash)
    with pytest.raises(OSError, match="simulated disk failure"):
        run_backup(Path(fixture_db.path), root, "2026-09-07")

    assert {p.name: p.read_bytes() for p in (root / "2026-09-07").iterdir()} == previous
    assert not list(root.glob(".backup-*"))
