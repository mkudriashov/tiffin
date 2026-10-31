"""Offline install smoke check for Windows, macOS and Linux after ``uv sync``."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from tiffin import backup, demo
from tiffin.db import Db
from tiffin.web.app import create_app


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    entrypoint = Path(sys.executable).parent / (
        "tiffin.exe" if sys.platform == "win32" else "tiffin"
    )
    subprocess.run([str(entrypoint), "version"], check=True, cwd=root)
    with tempfile.TemporaryDirectory(prefix="install-smoke-", dir=root) as temporary:
        home = Path(temporary)
        app = demo.make_app(home)
        demo.seed(app)
        with app.open_db() as db:
            person = app.registry(db).require("owner")
            assert db.scalar("PRAGMA integrity_check") == "ok"
            assert db.scalar("SELECT COUNT(*) FROM menus WHERE person_id = ?", (person.id,)) > 0
        with TestClient(create_app(app, demo_person=person)) as client:
            assert client.get("/api/health").status_code == 200
            assert client.get("/").status_code == 200
            assert client.get("/api/menu").status_code == 200
            assert client.get("/api/manual").status_code == 200
        result = backup.run_backup(
            app.config.paths.db,
            home / "backups",
            app.clock.today_local().isoformat(),
            clock=app.clock,
        )
        restored = home / "restored.db"
        restored.write_bytes((home / "backups" / result["day"] / "tiffin.db").read_bytes())
        with Db.open(restored, clock=app.clock) as db:
            assert db.scalar("PRAGMA integrity_check") == "ok"
            assert db.scalar("SELECT COUNT(*) FROM persons") == 2
    print("Install smoke passed: CLI, no-model demo, web, backup and restore.")


if __name__ == "__main__":
    main()
