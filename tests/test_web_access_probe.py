"""The public deployment probe detects an exposed identity boundary."""

from __future__ import annotations

from collections.abc import Mapping

from tiffin.http import Response
from tools.check_web_access import check


def test_web_access_probe_checks_public_route_and_identity() -> None:
    seen: list[str] = []

    def transport(
        url: str, method: str, data: bytes | None, headers: Mapping[str, str], timeout_s: float
    ) -> Response:
        seen.append(url)
        assert method == "GET" and data is None and timeout_s == 10
        if url.endswith("/api/health"):
            return Response(200, b"{}")
        if url.endswith("/api/me"):
            return Response(403 if not headers or "Tailscale-User-Login" in headers else 500, b"")
        return Response(200, b"page")

    assert len(check("https://tiffin.example.com", "/tiffin", transport)) == 3
    assert seen == [
        "https://tiffin.example.com/tiffin/api/health",
        "https://tiffin.example.com/tiffin/",
        "https://tiffin.example.com/tiffin/api/me",
        "https://tiffin.example.com/tiffin/api/me",
    ]


def test_web_access_probe_rejects_header_identity_from_client() -> None:
    def transport(
        url: str, method: str, data: bytes | None, headers: Mapping[str, str], timeout_s: float
    ) -> Response:
        return Response(200 if headers or url.endswith("/api/health") else 403, b"")

    try:
        check("https://tiffin.example.com", "", transport)
    except ValueError as exc:
        assert "client-supplied identity" in str(exc)
    else:
        raise AssertionError("A forwarded identity header must fail the deployment check")
