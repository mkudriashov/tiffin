"""Process liveness files under TIFFIN_HOME/state, written atomically."""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

from .clock import DEFAULT_CLOCK, Clock, parse_stamp


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


class Heartbeat:
    def __init__(self, state_dir: Path, clock: Clock = DEFAULT_CLOCK):
        self.state_dir = Path(state_dir)
        self.path = self.state_dir / "heartbeat"
        self.clock = clock

    def beat(self, pid: int | None = None) -> None:
        atomic_write(self.path, f"{self.clock.utc_stamp()} {pid or os.getpid()}\n")

    def read(self) -> tuple[dt.datetime, int] | None:
        try:
            stamp, pid = self.path.read_text(encoding="utf-8").strip().rsplit(" ", 1)
            return parse_stamp(stamp), int(pid)
        except OSError, ValueError:
            return None

    def age_s(self) -> float | None:
        got = self.read()
        if got is None:
            return None
        return (self.clock.now_utc() - got[0]).total_seconds()


class PidFile:
    def __init__(self, state_dir: Path, clock: Clock = DEFAULT_CLOCK):
        self.path = Path(state_dir) / "tiffin.pid"
        self.clock = clock

    def write(self, exe: str) -> None:
        atomic_write(
            self.path,
            json.dumps({"pid": os.getpid(), "start_utc": self.clock.utc_stamp(), "exe": exe}),
        )

    def read(self) -> dict | None:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except OSError, ValueError:
            return None

    def remove(self) -> None:
        self.path.unlink(missing_ok=True)
