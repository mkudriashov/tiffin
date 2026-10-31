"""API request limits count attempts before they reach the transport."""

from __future__ import annotations

import datetime as dt
from concurrent.futures import ThreadPoolExecutor

import pytest

from tiffin.clock import TZ, FrozenClock
from tiffin.config import Budget, load
from tiffin.errors import ModelError
from tiffin.http import Response
from tiffin.model.api import ApiBackend
from tiffin.model.provider import configured_provider
from tiffin.model.request_limit import RequestLimit
from tiffin.secrets import Secrets


def test_limit_is_atomic_and_persists_across_instances(tmp_path) -> None:
    clock = FrozenClock(dt.datetime(2026, 9, 7, 20, 30, tzinfo=TZ))
    first = RequestLimit(tmp_path, 3, clock)
    second = RequestLimit(tmp_path, 3, clock)
    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(_reserve, [first, second] * 2))
    assert outcomes.count("allowed") == 3
    assert outcomes.count("blocked") == 1
    with pytest.raises(ModelError, match="request limit reached"):
        RequestLimit(tmp_path, 3, clock).reserve()
    clock.advance(days=1)
    RequestLimit(tmp_path, 3, clock).reserve()


def _reserve(limiter: RequestLimit) -> str:
    try:
        limiter.reserve()
    except ModelError:
        return "blocked"
    return "allowed"


def test_configured_api_counts_retries_before_transport(tmp_path) -> None:
    (tmp_path / "tiffin.toml").write_text(
        '[model]\nprovider = "openai"\napi_requests_per_day = 2\n', encoding="utf-8"
    )
    config = load(root=tmp_path, environ={})
    clock = FrozenClock(dt.datetime(2026, 9, 7, 20, 30, tzinfo=TZ))
    calls: list[int] = []

    def transport(url, method, data, headers, timeout):
        del url, method, data, headers, timeout
        calls.append(1)
        return Response(429, b"")

    provider = configured_provider(
        config,
        Secrets(tmp_path / "secrets", {"TIFFIN_MODEL_API_KEY": "example-key"}),
        transport,
        clock=clock,
    )
    assert isinstance(provider, ApiBackend)
    provider.sleeper = lambda _: None
    with pytest.raises(ModelError, match="request limit reached"):
        provider.converse(
            "chat", [{"role": "user", "content": "ping"}], None, 1, Budget(12, 1, None)
        )
    assert len(calls) == 2


@pytest.mark.parametrize("value", [0, -1, True, "5"])
def test_invalid_limit_is_rejected(tmp_path, value) -> None:
    (tmp_path / "tiffin.toml").write_text(
        "[model]\napi_requests_per_day = "
        + (str(value).lower() if isinstance(value, bool) else repr(value))
        + "\n",
        encoding="utf-8",
    )
    from tiffin.errors import ConfigError

    with pytest.raises(ConfigError, match=r"model\.api_requests_per_day"):
        load(root=tmp_path, environ={})
