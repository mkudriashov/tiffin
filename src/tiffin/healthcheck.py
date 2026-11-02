"""Container health check: `python -m tiffin.healthcheck` exits 0 when tiffin is well.

With the web page on, it asks the page's own `<root_path>/api/health` over loopback and wants
`"status": "ok"`. With the page off it falls back to the heartbeat file. It uses only the
standard library and reads the same configuration the running process does.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any

from . import config as cfg
from .clock import DEFAULT_CLOCK, Clock
from .heartbeat import Heartbeat

Opener = Callable[..., Any]


def health_url(config: cfg.Config) -> str:
    root = str(config.get("web.root_path", "")).rstrip("/")
    return f"http://127.0.0.1:{config.web_port}{root}/api/health"


def check(
    config: cfg.Config, *, opener: Opener = urllib.request.urlopen, clock: Clock = DEFAULT_CLOCK
) -> tuple[bool, str]:
    if not config.get("web.enabled", True):
        age = Heartbeat(config.paths.state, clock).age_s()
        stale = int(config.get("ops.heartbeat_stale_s", 180))
        if age is None or age >= stale:
            return False, "no recent heartbeat"
        return True, f"heartbeat {age:.0f} s ago"
    try:
        with opener(health_url(config), timeout=5) as response:
            body = json.loads(response.read(65536).decode("utf-8"))
    except (OSError, ValueError) as exc:
        return False, f"health page unreachable: {type(exc).__name__}"
    status = body.get("status") if isinstance(body, dict) else None
    return status == "ok", f"health page says {status!r}"


def main(environ: Mapping[str, str] | None = None) -> int:
    try:
        config = cfg.load(environ=environ)
    except Exception as exc:  # a broken config is an unhealthy container, said once
        print(f"config: {exc}", file=sys.stderr)
        return 1
    ok, detail = check(config)
    print(detail, file=sys.stderr if not ok else sys.stdout)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
