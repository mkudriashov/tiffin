"""Create a verified SQLite backup and prune old local copies."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import shutil
import sqlite3
import tempfile
from pathlib import Path

from .clock import DEFAULT_CLOCK, Clock

KEEP_DAYS = 30


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def table_counts(con: sqlite3.Connection) -> dict[str, int]:
    names = [
        r[0]
        for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        )
    ]
    counts: dict[str, int] = {}
    for name in names:
        quoted = '"' + name.replace('"', '""') + '"'
        counts[name] = int(con.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0])
    return counts


def run_backup(
    db_path: Path,
    backups_dir: Path,
    day: str,
    *,
    keep_days: int = KEEP_DAYS,
    clock: Clock = DEFAULT_CLOCK,
) -> dict:
    if keep_days < 0:
        raise ValueError("backup retention cannot be negative")
    on_day = dt.date.fromisoformat(day)
    if on_day.isoformat() != day:
        raise ValueError("backup day must be YYYY-MM-DD")
    backups_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".backup-", dir=backups_dir) as temporary:
        stage = Path(temporary)
        copy_path = stage / "tiffin.db"
        sql_path = stage / "tiffin.sql"
        src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            live_check = src.execute("PRAGMA integrity_check").fetchone()[0]
            if live_check != "ok":
                raise RuntimeError(f"live integrity_check: {live_check}")
            dst = sqlite3.connect(str(copy_path))
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        copy = sqlite3.connect(str(copy_path))
        try:
            with sql_path.open("w", encoding="utf-8") as fh:
                for line in copy.iterdump():
                    fh.write(line + "\n")
            copy_check = copy.execute("PRAGMA integrity_check").fetchone()[0]
            if copy_check != "ok":
                raise RuntimeError(f"copy integrity_check: {copy_check}")
            counts = table_counts(copy)
        finally:
            copy.close()
        manifest = {
            "day": day,
            "source": str(db_path),
            "db_bytes": copy_path.stat().st_size,
            "db_sha256": sha256_of(copy_path),
            "sql_bytes": sql_path.stat().st_size,
            "sql_sha256": sha256_of(sql_path),
            "tables": counts,
            "made_at": clock.utc_stamp(),
        }
        (stage / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
        out_dir = backups_dir / day
        out_dir.mkdir(parents=True, exist_ok=True)
        for name in ("tiffin.db", "tiffin.sql", "manifest.json"):
            (stage / name).replace(out_dir / name)
    cutoff = on_day - dt.timedelta(days=keep_days)
    pruned = []
    for d in backups_dir.iterdir():
        try:
            old_day = dt.date.fromisoformat(d.name)
        except ValueError:
            continue
        if d.is_dir() and old_day.isoformat() == d.name and old_day < cutoff:
            shutil.rmtree(d)
            pruned.append(d.name)
    manifest["pruned"] = pruned
    return manifest


def write_last(state_dir: Path, payload: dict) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    tmp = state_dir / "backup_last.json.tmp"
    tmp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    tmp.replace(state_dir / "backup_last.json")
