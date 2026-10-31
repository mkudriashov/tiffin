"""Stdlib HTTP: one injectable transport shared by Telegram and intervals.icu.

A transport is `(url, method, data, headers, timeout_s) -> Response`. Tests pass a scripted
fake; production uses urllib. HTTP error statuses come back as a Response (the caller
decides); only a failure to reach the host raises TransportError.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .errors import TiffinError


class TransportError(TiffinError):
    """Could not reach the host (DNS, refused, timeout)."""


@dataclass
class Response:
    status: int
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


Transport = Callable[[str, str, bytes | None, Mapping[str, str], float], Response]


def urllib_transport(
    url: str, method: str, data: bytes | None, headers: Mapping[str, str], timeout_s: float
) -> Response:
    req = urllib.request.Request(url, data=data, method=method, headers=dict(headers))
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return Response(
                resp.status, resp.read(), {k.lower(): v for k, v in resp.headers.items()}
            )
    except urllib.error.HTTPError as e:
        body = e.read() if e.fp else b""
        return Response(e.code, body, {k.lower(): v for k, v in e.headers.items()})
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        # urllib's error text can contain the full request URL, including Telegram's bot token.
        reason = getattr(e, "reason", None)
        timed_out = (
            isinstance(e, TimeoutError)
            or isinstance(reason, TimeoutError)
            or (isinstance(reason, str) and "timed out" in reason.lower())
        )
        cause = "timed out" if timed_out else type(e).__name__
        raise TransportError(f"{method} {_host_of(url)}: {cause}") from None


def _host_of(url: str) -> str:
    return url.split("//", 1)[-1].split("/", 1)[0]


def post_json(
    transport: Transport,
    url: str,
    payload: Any,
    *,
    headers: Mapping[str, str] | None = None,
    timeout_s: float = 30.0,
) -> Response:
    h = {"Content-Type": "application/json", "Accept": "application/json"}
    if headers:
        h.update(headers)
    return transport(url, "POST", json.dumps(payload).encode("utf-8"), h, timeout_s)


def get(
    transport: Transport,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    timeout_s: float = 30.0,
) -> Response:
    return transport(url, "GET", None, dict(headers or {}), timeout_s)
