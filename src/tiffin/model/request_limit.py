"""Persistent, atomic limit on API requests, including retries and failed responses."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from ..clock import Clock
from ..errors import ModelError


class RequestLimit:
    def __init__(self, state_dir: Path, per_day: int, clock: Clock) -> None:
        self.path = state_dir / "model_requests.sqlite3"
        self.per_day = per_day
        self.clock = clock

    def reserve(self) -> None:
        """Count the attempt before sending it; SQLite serialises concurrent processes."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self.path, timeout=10) as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS requests (day TEXT NOT NULL, at_utc TEXT NOT NULL)"
                )
                day = self.clock.today_local().isoformat()
                used = connection.execute(
                    "SELECT count(*) FROM requests WHERE day = ?", (day,)
                ).fetchone()[0]
                if used >= self.per_day:
                    raise ModelError(
                        f"API request limit reached ({self.per_day} per local day); "
                        "model tasks are manual until tomorrow"
                    )
                connection.execute(
                    "INSERT INTO requests (day, at_utc) VALUES (?, ?)",
                    (day, self.clock.utc_stamp()),
                )
        except (OSError, sqlite3.Error) as exc:
            raise ModelError("API request limit unavailable; model tasks are manual") from exc
