"""Probe a deployed Telegram sign-in page through its public HTTPS origin."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Mapping
from urllib.parse import urlsplit

from tiffin.errors import TiffinError
from tiffin.http import Response, get, urllib_transport

Transport = Callable[[str, str, bytes | None, Mapping[str, str], float], Response]


def check(origin: str, root_path: str, transport: Transport = urllib_transport) -> list[str]:
    parsed = urlsplit(origin)
    if parsed.scheme != "https" or not parsed.hostname or parsed.path not in ("", "/"):
        raise ValueError("Give an HTTPS origin without a path, such as https://tiffin.example.com")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("The origin cannot contain credentials, a query or a fragment")
    if root_path and (not root_path.startswith("/") or root_path.endswith("/")):
        raise ValueError("The mount path must start with / and have no trailing /")
    base = origin.rstrip("/") + root_path
    health = get(transport, f"{base}/api/health", timeout_s=10)
    if health.status != 200:
        raise ValueError(
            f"Public health route returned HTTP {health.status}; check the proxy route"
        )
    page = get(transport, f"{base}/", timeout_s=10)
    if page.status not in (200, 403):
        raise ValueError(f"Page returned HTTP {page.status}; check the mount path")
    unauthenticated = get(transport, f"{base}/api/me", timeout_s=10)
    if unauthenticated.status != 403:
        raise ValueError("The public API did not reject an unsigned visitor")
    forged = get(
        transport,
        f"{base}/api/me",
        headers={"Tailscale-User-Login": "alex@example.com"},
        timeout_s=10,
    )
    if forged.status != 403:
        raise ValueError("A client-supplied identity header was accepted")
    return ["public route", "unauthenticated API", "client-supplied identity header"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("origin", help="public HTTPS origin from web.origin")
    parser.add_argument("--root-path", default="", help="web.root_path, if set")
    args = parser.parse_args(argv)
    try:
        checks = check(args.origin, args.root_path)
    except (ValueError, OSError, TiffinError) as exc:
        print(f"Web access check failed: {exc}", file=sys.stderr)
        return 1
    print("Web access checks passed: " + ", ".join(checks) + ".")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
