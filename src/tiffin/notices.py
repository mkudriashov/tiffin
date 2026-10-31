"""Recent process notices for the owner's status strip."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

from .clock import DEFAULT_CLOCK, Clock, parse_stamp

FILE = "notices.jsonl"
#: how many lines the file keeps; older ones are dropped when it grows past this
KEEP = 400


def path(state_dir: Path) -> Path:
    return state_dir / FILE


def record(state_dir: Path | None, event: str, text: str, *, clock: Clock = DEFAULT_CLOCK) -> None:
    """Append one notice. Never raises: a notice that cannot be written is logged by the caller."""
    if state_dir is None:
        return
    p = path(state_dir)
    line = json.dumps(
        {"at": clock.utc_stamp(), "event": event, "text": text[:500], "source": "tiffin"}
    )
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    _trim(p)


def _trim(p: Path) -> None:
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    if len(lines) > KEEP * 2:
        p.write_text("\n".join(lines[-KEEP:]) + "\n", encoding="utf-8")


def recent(
    state_dir: Path | None, *, hours: int = 48, clock: Clock = DEFAULT_CLOCK
) -> list[dict[str, Any]]:
    """Notices from the last `hours`, newest first, one per event kind (the newest)."""
    if state_dir is None:
        return []
    try:
        lines = path(state_dir).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    since = clock.now_utc() - dt.timedelta(hours=hours)
    seen: dict[str, dict[str, Any]] = {}
    for raw in lines:
        try:
            item = json.loads(raw)
            at = parse_stamp(str(item["at"]))
        except ValueError, KeyError, TypeError:
            continue
        if at < since:
            continue
        seen[str(item.get("event") or "")] = {
            "at": str(item["at"]),
            "event": str(item.get("event") or ""),
            "text": str(item.get("text") or ""),
            "source": str(item.get("source") or ""),
        }
    return sorted(seen.values(), key=lambda n: n["at"], reverse=True)


__all__ = ["FILE", "path", "recent", "record"]
