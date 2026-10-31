"""HTTP model adapters. Responses are parsed without retaining provider payloads in logs."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from typing import Any

from ..config import Budget
from ..errors import ModelError, ModelInvalid, ModelToolUnsupported, QuotaExhausted
from ..http import Transport, TransportError, post_json
from . import schemas
from .gateway import Messages, ModelRepairExhausted, ModelResult, ModelTimeout
from .request_limit import RequestLimit
from .tools import BoundTools, ToolError


class ApiBackend:
    """Anthropic Messages, OpenAI Responses, or Chat Completions compatible servers."""

    def __init__(
        self,
        provider: str,
        model: str,
        base_url: str,
        api_key: str | None,
        transport: Transport,
        *,
        task_models: Mapping[str, str] | None = None,
        auth_style: str = "bearer",
        sleeper: Callable[[float], None] = time.sleep,
        request_limit: RequestLimit | None = None,
    ) -> None:
        self.name = provider
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.transport = transport
        self.task_models = dict(task_models or {})
        self.auth_style = auth_style
        self.sleeper = sleeper
        self.request_limit = request_limit

    def _model(self, task: str) -> str:
        return self.task_models.get(task, self.model)

    @staticmethod
    def _output_tokens(task: str) -> int:
        if task == "doctor":
            return 128
        if task.startswith("menu_"):
            return 16_384
        if task in ("intent", "plan_parse"):
            return 2_048
        return 8_192

    def _url(self) -> str:
        if self.name == "anthropic":
            return f"{self.base_url}/v1/messages"
        if self.name == "openai":
            return f"{self.base_url}/v1/responses"
        return (
            f"{self.base_url}/chat/completions"
            if self.base_url.endswith("/v1")
            else f"{self.base_url}/v1/chat/completions"
        )

    def _headers(self) -> dict[str, str]:
        if self.name == "anthropic":
            return {"x-api-key": self.api_key or "", "anthropic-version": "2023-06-01"}
        if self.name == "openai-compatible" and self.auth_style == "api-key":
            return {"api-key": self.api_key} if self.api_key else {}
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    def _post(self, payload: dict[str, Any], budget: Budget) -> dict[str, Any]:
        if budget.usd is not None:
            raise ModelError(
                "USD caps cannot be enforced for this API provider; "
                "set models.cap_usd = false after reviewing provider billing"
            )
        for attempt in range(3):
            if self.request_limit is not None:
                self.request_limit.reserve()
            try:
                response = post_json(
                    self.transport,
                    self._url(),
                    payload,
                    headers=self._headers(),
                    timeout_s=float(budget.timeout_s),
                )
            except TransportError as exc:
                if "timed out" in str(exc).lower():
                    raise ModelTimeout(f"{self.name}: request timed out") from None
                raise ModelError(f"{self.name}: connection failed") from None
            if (response.status == 429 or 500 <= response.status < 600) and attempt < 2:
                self.sleeper(0.5 * 2**attempt)
                continue
            if response.status == 429:
                raise QuotaExhausted(f"{self.name}: HTTP 429")
            if not 200 <= response.status < 300:
                raise ModelError(f"{self.name}: HTTP {response.status}")
            try:
                body = response.json()
            except ValueError, UnicodeError:
                raise ModelInvalid(f"{self.name}: invalid JSON response") from None
            if not isinstance(body, dict):
                raise ModelInvalid(f"{self.name}: response is not an object")
            return body
        raise ModelError(f"{self.name}: request failed")

    def _request(
        self,
        task: str,
        messages: Messages,
        *,
        schema: dict[str, Any] | None = None,
        tools: BoundTools | None = None,
    ) -> dict[str, Any]:
        model = self._model(task)
        if self.name == "anthropic":
            system = "\n\n".join(str(m["content"]) for m in messages if m["role"] == "system")
            payload: dict[str, Any] = {
                "model": model,
                "max_tokens": self._output_tokens(task),
                "messages": [m for m in messages if m["role"] != "system"],
            }
            if system:
                payload["system"] = system
            if schema is not None:
                payload["output_config"] = {
                    "format": {"type": "json_schema", "schema": schemas.for_model(schema)}
                }
            if tools is not None:
                payload["tools"] = [
                    {
                        "name": t["name"],
                        "description": t["description"],
                        "input_schema": t["inputSchema"],
                    }
                    for t in tools.to_mcp()
                ]
            return payload
        if self.name == "openai":
            payload = {
                "model": model,
                "input": messages,
                "max_output_tokens": self._output_tokens(task),
            }
            if schema is not None:
                payload["text"] = {
                    "format": {
                        "type": "json_schema",
                        "name": task,
                        "schema": schemas.for_model(schema),
                        "strict": False,
                    }
                }
            if tools is not None:
                payload["tools"] = [
                    {
                        "type": "function",
                        "name": t["name"],
                        "description": t["description"],
                        "parameters": t["inputSchema"],
                        "strict": False,
                    }
                    for t in tools.to_mcp()
                ]
            return payload
        payload = {"model": model, "messages": messages}
        if schema is not None:
            payload["messages"] = [
                {
                    "role": "system",
                    "content": "Return only a JSON object matching this schema: "
                    + json.dumps(schemas.for_model(schema), separators=(",", ":")),
                },
                *messages,
            ]
        if tools is not None:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t["description"],
                        "parameters": t["inputSchema"],
                    },
                }
                for t in tools.to_mcp()
            ]
        return payload

    def _text(self, body: dict[str, Any]) -> str:
        if self.name == "anthropic":
            blocks = body.get("content", [])
            return "\n".join(
                str(b.get("text", "")) for b in blocks if b.get("type") == "text"
            ).strip()
        if self.name == "openai":
            return "\n".join(
                str(c.get("text", ""))
                for item in body.get("output", [])
                if item.get("type") == "message"
                for c in item.get("content", [])
                if c.get("type") == "output_text"
            ).strip()
        choices = body.get("choices", [])
        return str(choices[0].get("message", {}).get("content") or "").strip() if choices else ""

    def _result(self, task: str, body: dict[str, Any], text: str, data: Any = None) -> ModelResult:
        return ModelResult(
            text=text,
            data=data,
            model_used=str(body.get("model") or self._model(task)),
            backend=self.name,
        )

    def complete_json(
        self, task: str, prompt: str, schema: dict[str, Any], budget: Budget
    ) -> ModelResult:
        messages: Messages = [{"role": "user", "content": prompt}]
        native = self.name != "openai-compatible"
        for attempt in range(2):
            try:
                format_schema = schema if native or self.name == "openai-compatible" else None
                body = self._post(self._request(task, messages, schema=format_schema), budget)
            except ModelError as exc:
                if not native or "HTTP 400" not in str(exc):
                    raise
                native = False
                messages.insert(
                    0,
                    {
                        "role": "system",
                        "content": "Return only a JSON object matching this schema: "
                        + json.dumps(schemas.for_model(schema), separators=(",", ":")),
                    },
                )
                body = self._post(self._request(task, messages), budget)
            answer = self._text(body)
            try:
                data = json.loads(answer)
                schemas.validate_data(schema, data, name=task)
            except ValueError, ModelInvalid:
                if attempt:
                    raise ModelRepairExhausted(f"{self.name}: invalid structured answer") from None
                messages = [
                    *messages,
                    {"role": "assistant", "content": answer},
                    {
                        "role": "user",
                        "content": "Repair the answer to match the schema. Return only JSON.",
                    },
                ]
                continue
            return self._result(task, body, answer, data)
        raise ModelRepairExhausted(f"{self.name}: invalid structured answer")

    def converse(
        self,
        task: str,
        messages: Messages,
        tools: BoundTools | None,
        person_id: int,
        budget: Budget,
    ) -> ModelResult:
        del person_id  # BoundTools already fixes identity; model arguments cannot change it.
        turns: Messages = list(messages)
        calls: list[dict[str, Any]] = []
        for _ in range(max(1, budget.turns)):
            try:
                body = self._post(self._request(task, turns, tools=tools), budget)
            except ModelError as exc:
                if tools is not None and "HTTP 400" in str(exc):
                    raise ModelToolUnsupported(
                        f"{self.name}: chat request with tools rejected (HTTP 400); "
                        "check model tool support"
                    ) from None
                raise
            if self.name == "anthropic":
                requested = [b for b in body.get("content", []) if b.get("type") == "tool_use"]
                if requested and tools is not None:
                    turns.append({"role": "assistant", "content": body["content"]})
                    results = []
                    for item in requested:
                        result = self._call_tool(
                            tools, str(item["name"]), item.get("input", {}), calls
                        )
                        results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": item["id"],
                                "content": json.dumps(result),
                            }
                        )
                    turns.append({"role": "user", "content": results})
                    continue
            elif self.name == "openai":
                requested = [x for x in body.get("output", []) if x.get("type") == "function_call"]
                if requested and tools is not None:
                    turns.extend(body["output"])
                    for item in requested:
                        result = self._call_tool(
                            tools, str(item["name"]), item.get("arguments"), calls
                        )
                        turns.append(
                            {
                                "type": "function_call_output",
                                "call_id": item["call_id"],
                                "output": json.dumps(result),
                            }
                        )
                    continue
            else:
                choices = body.get("choices", [])
                message = choices[0].get("message", {}) if choices else {}
                requested = message.get("tool_calls", [])
                if requested and tools is not None:
                    turns.append(
                        {
                            "role": "assistant",
                            "content": message.get("content"),
                            "tool_calls": requested,
                        }
                    )
                    for item in requested:
                        fn = item["function"]
                        result = self._call_tool(tools, str(fn["name"]), fn.get("arguments"), calls)
                        turns.append(
                            {
                                "role": "tool",
                                "tool_call_id": item["id"],
                                "content": json.dumps(result),
                            }
                        )
                    continue
            answer = self._text(body)
            if answer:
                result = self._result(task, body, answer)
                result.tool_calls = calls
                return result
            error = f"{self.name}: empty reply"
            if calls:
                raise ModelRepairExhausted(error)
            raise ModelInvalid(error)
        raise ModelRepairExhausted(f"{self.name}: tool turn limit reached")

    @staticmethod
    def _arguments(raw: Any) -> dict[str, Any]:
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError:
            raise ModelInvalid("invalid tool arguments") from None
        if not isinstance(parsed, dict):
            raise ModelInvalid("tool arguments must be an object")
        return parsed

    @staticmethod
    def _call_tool(
        tools: BoundTools, name: str, args: Any, calls: list[dict[str, Any]]
    ) -> dict[str, Any]:
        try:
            result = tools.call(name, ApiBackend._arguments(args))
        except (ToolError, ModelInvalid) as exc:
            result = {"error": str(exc)}
        calls.append({"name": name})
        return result
