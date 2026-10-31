"""Provider wire contracts, using representative responses and no network."""

from __future__ import annotations

import json
from typing import Any, cast
from unittest.mock import patch

import pytest

from tiffin.config import Budget, load
from tiffin.errors import ModelError, ModelInvalid
from tiffin.http import Response
from tiffin.model.api import ApiBackend
from tiffin.model.provider import configured_provider
from tiffin.model.tools import BoundTools
from tiffin.secrets import Secrets

BUDGET = Budget(12, 3, None)
SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}


class Wire:
    def __init__(self, *responses: dict[str, Any] | int):
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, Any], dict[str, str], float]] = []

    def __call__(self, url, method, data, headers, timeout):
        self.calls.append((url, json.loads(data), dict(headers), timeout))
        item = self.responses.pop(0)
        return Response(
            item if isinstance(item, int) else 200,
            b"" if isinstance(item, int) else json.dumps(item).encode(),
        )


@pytest.mark.parametrize(
    ("provider", "url", "reply", "request_key"),
    [
        (
            "anthropic",
            "https://api.anthropic.com/v1/messages",
            {"model": "claude-sonnet-4-5", "content": [{"type": "text", "text": '{"ok":true}'}]},
            "output_config",
        ),
        (
            "openai",
            "https://api.openai.com/v1/responses",
            {
                "model": "gpt-4.1-mini",
                "output": [
                    {"type": "message", "content": [{"type": "output_text", "text": '{"ok":true}'}]}
                ],
            },
            "text",
        ),
        (
            "openai-compatible",
            "http://localhost:11434/v1/chat/completions",
            {"model": "local-example", "choices": [{"message": {"content": '{"ok":true}'}}]},
            "messages",
        ),
    ],
)
def test_structured_wire_contract(provider, url, reply, request_key) -> None:
    wire = Wire(reply)
    base = url.rsplit("/v1/", 1)[0] + ("/v1" if provider == "openai-compatible" else "")
    backend = ApiBackend(provider, reply["model"], base, "test-key", wire)
    with patch("tiffin.model.api.schemas.validate_data", side_effect=lambda s, d, name: d):
        result = backend.complete_json("doctor", "Return JSON", SCHEMA, BUDGET)
    assert result.data == {"ok": True} and result.model_used == reply["model"]
    sent_url, payload, headers, timeout = wire.calls[0]
    assert sent_url == url and request_key in payload and timeout == 12
    assert payload["model"] == reply["model"]
    if provider == "anthropic":
        assert payload["max_tokens"] == 128
    if provider == "openai":
        assert payload["max_output_tokens"] == 128
    assert ("x-api-key" in headers) == (provider == "anthropic")


class FakeTools:
    def __init__(self):
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def to_mcp(self):
        return [
            {"name": "get_targets", "description": "Targets", "inputSchema": {"type": "object"}}
        ]

    def call(self, name, args):
        self.calls.append((name, args))
        return {"kcal": 2000}


@pytest.mark.parametrize(
    ("provider", "first", "second"),
    [
        (
            "anthropic",
            {"content": [{"type": "tool_use", "id": "t1", "name": "get_targets", "input": {}}]},
            {"content": [{"type": "text", "text": "2000 kcal"}]},
        ),
        (
            "openai",
            {
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "c1",
                        "name": "get_targets",
                        "arguments": "{}",
                    }
                ]
            },
            {
                "output": [
                    {"type": "message", "content": [{"type": "output_text", "text": "2000 kcal"}]}
                ]
            },
        ),
        (
            "openai-compatible",
            {
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {"id": "t1", "function": {"name": "get_targets", "arguments": "{}"}}
                            ]
                        }
                    }
                ]
            },
            {"choices": [{"message": {"content": "2000 kcal"}}]},
        ),
    ],
)
def test_chat_tool_round_trip(provider, first, second) -> None:
    wire = Wire(first, second)
    base = (
        "http://localhost:11434/v1"
        if provider == "openai-compatible"
        else "https://api.example.com"
    )
    backend = ApiBackend(provider, "example-model", base, "test-key", wire)
    tools = FakeTools()
    result = backend.converse(
        "chat", [{"role": "user", "content": "targets?"}], cast(BoundTools, tools), 7, BUDGET
    )
    assert result.text == "2000 kcal" and result.tool_calls == [{"name": "get_targets"}]
    assert tools.calls == [("get_targets", {})]
    assert len(wire.calls) == 2


def test_retry_on_rate_limit_then_success() -> None:
    wire = Wire(
        429, {"output": [{"type": "message", "content": [{"type": "output_text", "text": "OK"}]}]}
    )
    delays: list[float] = []
    backend = ApiBackend(
        "openai", "example-model", "https://api.openai.com", "test-key", wire, sleeper=delays.append
    )
    result = backend.converse("chat", [{"role": "user", "content": "ping"}], None, 1, BUDGET)
    assert result.text == "OK" and delays == [0.5] and len(wire.calls) == 2


def test_provider_selection_and_none(tmp_path) -> None:
    config_file = tmp_path / "tiffin.toml"
    config_file.write_text('[model]\nprovider = "none"\n', encoding="utf-8")
    config = load(root=tmp_path, environ={})
    assert configured_provider(config, Secrets(tmp_path / "secrets", {}), Wire()) is None
    config_file.write_text('[model]\nprovider = "openai"\n', encoding="utf-8")
    config = load(root=tmp_path, environ={})
    selected = configured_provider(
        config, Secrets(tmp_path / "secrets", {"TIFFIN_MODEL_API_KEY": "example-key"}), Wire()
    )
    assert isinstance(selected, ApiBackend)
    assert selected.name == "openai" and selected.model == "gpt-4.1-mini"


def test_compatible_api_key_header() -> None:
    wire = Wire({"choices": [{"message": {"content": "OK"}}]})
    backend = ApiBackend(
        "openai-compatible",
        "azure-example",
        "https://example.com/openai/v1",
        "key",
        wire,
        auth_style="api-key",
    )
    backend.converse("chat", [{"role": "user", "content": "ping"}], None, 1, BUDGET)
    assert wire.calls[0][2]["api-key"] == "key"
    assert "Authorization" not in wire.calls[0][2]


def test_chat_without_tool_support_has_a_clear_error() -> None:
    wire = Wire(400)
    backend = ApiBackend(
        "openai-compatible", "local-example", "http://localhost:11434/v1", None, wire
    )
    with pytest.raises(ModelError, match="chat request with tools rejected"):
        backend.converse(
            "chat",
            [{"role": "user", "content": "targets?"}],
            cast(BoundTools, FakeTools()),
            1,
            BUDGET,
        )


def test_native_schema_rejection_falls_back_to_prompt_json() -> None:
    wire = Wire(
        400,
        {"content": [{"type": "text", "text": '{"ok":true}'}]},
    )
    backend = ApiBackend("anthropic", "example-model", "https://api.anthropic.com", "key", wire)
    with patch("tiffin.model.api.schemas.validate_data", side_effect=lambda s, d, name: d):
        result = backend.complete_json("doctor", "Return JSON", SCHEMA, BUDGET)
    assert result.data == {"ok": True}
    assert "output_config" in wire.calls[0][1]
    assert "output_config" not in wire.calls[1][1]
    assert "JSON object matching" in wire.calls[1][1]["system"]


def test_invalid_structured_answer_gets_one_repair_retry() -> None:
    wire = Wire(
        {"output": [{"type": "message", "content": [{"type": "output_text", "text": "{}"}]}]},
        {
            "output": [
                {"type": "message", "content": [{"type": "output_text", "text": '{"ok":true}'}]}
            ]
        },
    )
    backend = ApiBackend("openai", "example-model", "https://api.openai.com", "key", wire)
    validator = [ModelInvalid("missing ok"), {"ok": True}]

    def validate_once(_schema, data, *, name):
        del name
        next_result = validator.pop(0)
        if isinstance(next_result, Exception):
            raise next_result
        return data

    with patch("tiffin.model.api.schemas.validate_data", side_effect=validate_once):
        result = backend.complete_json("doctor", "Return JSON", SCHEMA, BUDGET)
    assert result.data == {"ok": True} and len(wire.calls) == 2
    assert "Repair" in wire.calls[1][1]["input"][-1]["content"]


def test_api_does_not_ignore_a_configured_usd_cap() -> None:
    wire = Wire()
    backend = ApiBackend("openai", "example-model", "https://api.openai.com", "key", wire)
    with pytest.raises(ModelError, match="USD caps cannot be enforced"):
        backend.converse(
            "chat", [{"role": "user", "content": "ping"}], None, 1, Budget(12, 1, 0.10)
        )
    assert wire.calls == []
