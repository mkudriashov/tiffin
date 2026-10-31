"""JSON-lines logging under TIFFIN_HOME/logs.

Fields: ts (UTC space-form), level, logger, event, run_id, person_id, msg, exc.
person_id is an integer only; never a login, name or chat id. Message bodies and prompts are
never logged (lengths and hashes only). Retention 90 days (prune()).
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import sys
import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from types import TracebackType
from typing import Any

from .clock import DEFAULT_CLOCK, STAMP_FMT, Clock

_run_id: ContextVar[str | None] = ContextVar("tiffin_run_id", default=None)
_person_id: ContextVar[int | None] = ContextVar("tiffin_person_id", default=None)
ExcInfo = tuple[type[BaseException] | None, BaseException | None, TracebackType | None]


def _exception_summary(exc_info: ExcInfo) -> str:
    error_type, _, tb = exc_info
    if error_type is None:
        return "unknown exception"
    frames = traceback.extract_tb(tb)
    locations = " > ".join(
        f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}" for frame in frames[-8:]
    )
    return f"{error_type.__name__}: {locations}"


class SafeConsoleFormatter(logging.Formatter):
    def formatException(self, ei: ExcInfo) -> str:
        return _exception_summary(ei)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ts = dt.datetime.fromtimestamp(record.created, tz=dt.UTC).strftime(STAMP_FMT)
        payload: dict[str, Any] = {
            "ts": ts,
            "level": record.levelname,
            "logger": record.name,
            "event": getattr(record, "event", None),
            "run_id": getattr(record, "run_id", None) or _run_id.get(),
            "person_id": getattr(record, "person_id", None) or _person_id.get(),
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = _exception_summary(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


class DailyJsonHandler(logging.Handler):
    """Appends to tiffin-<UTC date>.jsonl, reopening when the UTC date changes."""

    def __init__(self, log_dir: Path, clock: Clock = DEFAULT_CLOCK):
        super().__init__()
        self.log_dir = log_dir
        self.clock = clock
        self._day: str | None = None
        self._fh: Any = None
        self.setFormatter(JsonFormatter())

    def _path_for(self, day: str) -> Path:
        return self.log_dir / f"tiffin-{day}.jsonl"

    def emit(self, record: logging.LogRecord) -> None:
        try:
            day = self.clock.now_utc().strftime("%Y-%m-%d")
            # reopened on a new day AND whenever the file is not open: something else closing
            # the handler (uvicorn's dictConfig shuts down every handler it finds) left it
            # writing into a closed file, every line lost to handleError until UTC midnight -
            # after each of the weekend's twenty restarts the log said "start" and nothing else
            if day != self._day or self._fh is None or self._fh.closed:
                if self._fh is not None and not self._fh.closed:
                    self._fh.close()
                self.log_dir.mkdir(parents=True, exist_ok=True)
                self._fh = self._path_for(day).open("a", encoding="utf-8")
                self._day = day
            self._fh.write(self.format(record) + "\n")
            self._fh.flush()
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None
        self._day = None
        super().close()


def setup(
    log_dir: Path | None, *, level: str = "INFO", console: bool = True, clock: Clock = DEFAULT_CLOCK
) -> None:
    """Install the file handler (and a console handler) on the root logger. Idempotent."""
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)
    if console:
        ch = logging.StreamHandler(sys.stderr)
        ch.setFormatter(SafeConsoleFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(ch)
    if log_dir is not None:
        root.addHandler(DailyJsonHandler(log_dir, clock))


def get(name: str) -> logging.Logger:
    return logging.getLogger(name)


@contextmanager
def context(run_id: str | None = None, person_id: int | None = None) -> Iterator[None]:
    t1 = _run_id.set(run_id) if run_id is not None else None
    t2 = _person_id.set(person_id) if person_id is not None else None
    try:
        yield
    finally:
        if t1 is not None:
            _run_id.reset(t1)
        if t2 is not None:
            _person_id.reset(t2)


def prune(log_dir: Path, keep_days: int = 90, clock: Clock = DEFAULT_CLOCK) -> int:
    """Delete tiffin-*.jsonl older than keep_days. Returns the number removed."""
    cutoff = (clock.now_utc() - dt.timedelta(days=keep_days)).date()
    removed = 0
    for p in log_dir.glob("tiffin-????-??-??.jsonl"):
        raw_day = p.name[7:17]
        try:
            day = dt.date.fromisoformat(raw_day)
        except ValueError:
            continue
        if day.isoformat() == raw_day and day < cutoff:
            p.unlink(missing_ok=True)
            removed += 1
    return removed
