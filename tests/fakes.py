"""Fakes: a scripted transport and a stateful fake Telegram server behind the transport."""

from __future__ import annotations

import json
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any

from tiffin.http import Response, TransportError


def ok(result: Any) -> Response:
    return Response(200, json.dumps({"ok": True, "result": result}).encode())


def err(status: int, description: str, **params: Any) -> Response:
    body: dict[str, Any] = {"ok": False, "error_code": status, "description": description}
    if params:
        body["parameters"] = params
    return Response(status, json.dumps(body).encode())


class ScriptedTransport:
    """Returns the scripted responses in order; an Exception instance is raised instead."""

    def __init__(self, script: list[Response | Exception]):
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        url: str,
        method: str,
        data: bytes | None,
        headers: Mapping[str, str],
        timeout_s: float,
    ) -> Response:
        self.calls.append({"url": url, "method": method, "data": data, "timeout": timeout_s})
        if not self.script:
            raise AssertionError("scripted transport exhausted")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeTelegram:
    """Behaves like api.telegram.org for the handful of methods Tiffin uses."""

    def __init__(self, *, idle_sleep: float = 0.01):
        self.updates: list[dict[str, Any]] = []
        self.sent: list[dict[str, Any]] = []
        self.answered: list[dict[str, Any]] = []
        self.edited: list[dict[str, Any]] = []
        self.calls: list[str] = []
        self.fail_next: dict[str, list[Response | Exception]] = {}
        self.next_message_id = 100
        self.idle_sleep = idle_sleep
        self._lock = threading.Lock()

    # -- test helpers ----------------------------------------------------------------
    def queue_update(self, update: dict[str, Any]) -> None:
        with self._lock:
            self.updates.append(update)

    def text_update(
        self, update_id: int, chat_id: int, text: str, *, message_id: int = 1
    ) -> dict[str, Any]:
        return {
            "update_id": update_id,
            "message": {
                "message_id": message_id,
                "text": text,
                "chat": {"id": chat_id, "type": "private"},
                "from": {"id": chat_id},
            },
        }

    def callback_update(
        self, update_id: int, chat_id: int, data: str, *, message_id: int = 7, query_id: str = "q1"
    ) -> dict[str, Any]:
        return {
            "update_id": update_id,
            "callback_query": {
                "id": query_id,
                "data": data,
                "from": {"id": chat_id},
                "message": {"message_id": message_id, "chat": {"id": chat_id, "type": "private"}},
            },
        }

    def voice_update(
        self, update_id: int, chat_id: int, file_id: str = "f1", duration: int = 3
    ) -> dict[str, Any]:
        return {
            "update_id": update_id,
            "message": {
                "message_id": 3,
                "chat": {"id": chat_id, "type": "private"},
                "voice": {"file_id": file_id, "duration": duration, "mime_type": "audio/ogg"},
            },
        }

    def fail(self, method: str, *responses: Response | Exception) -> None:
        self.fail_next.setdefault(method, []).extend(responses)

    # -- transport --------------------------------------------------------------------
    def __call__(
        self,
        url: str,
        method: str,
        data: bytes | None,
        headers: Mapping[str, str],
        timeout_s: float,
    ) -> Response:
        api_method = url.rsplit("/", 1)[-1]
        self.calls.append(api_method)
        queued = self.fail_next.get(api_method)
        if queued:
            item = queued.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        payload = json.loads(data or b"{}")
        handler = getattr(self, f"_m_{api_method}", None)
        if handler is None:
            return err(404, f"method {api_method} not faked")
        return handler(payload)

    def _m_getMe(self, payload: dict[str, Any]) -> Response:
        return ok({"id": 1, "is_bot": True, "username": "fake_bot"})

    def _m_getUpdates(self, payload: dict[str, Any]) -> Response:
        offset = int(payload.get("offset", 0))
        with self._lock:
            pending = [u for u in self.updates if u["update_id"] >= offset]
        if not pending:
            time.sleep(self.idle_sleep)
        return ok(pending)

    def _m_sendMessage(self, payload: dict[str, Any]) -> Response:
        with self._lock:
            mid = self.next_message_id
            self.next_message_id += 1
            self.sent.append(dict(payload, message_id=mid))
        return ok({"message_id": mid, "chat": {"id": payload["chat_id"]}, "text": payload["text"]})

    def _m_answerCallbackQuery(self, payload: dict[str, Any]) -> Response:
        self.answered.append(payload)
        return ok(True)

    def _m_editMessageReplyMarkup(self, payload: dict[str, Any]) -> Response:
        self.edited.append(payload)
        return ok(True)

    # -- inspection --------------------------------------------------------------------
    def sent_texts(self) -> list[str]:
        return [m["text"] for m in self.sent]

    def sent_to(self, chat_id: int) -> list[dict[str, Any]]:
        return [m for m in self.sent if m["chat_id"] == chat_id]


def unreachable() -> TransportError:
    return TransportError("POST api.telegram.org: connection refused")


Sleeper = Callable[[float], None]


class RecordingSleeper:
    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


# ---------------------------------------------------------------- model gateway fakes
def claude_envelope(
    *,
    structured: Any = None,
    result: str = "ok",
    subtype: str = "success",
    is_error: bool = False,
    cost: float = 0.0123,
    model: str = "claude-opus-4-1-20250805",
    num_turns: int = 1,
    **extra: Any,
) -> dict[str, Any]:
    """The `claude -p --output-format json` envelope, as pinned by spike S3."""
    env: dict[str, Any] = {
        "type": "result",
        "subtype": subtype,
        "is_error": is_error,
        "duration_ms": 1234,
        "duration_api_ms": 1000,
        "num_turns": num_turns,
        "result": result,
        "session_id": "00000000-0000-0000-0000-000000000000",
        "total_cost_usd": cost,
        "usage": {"input_tokens": 10, "output_tokens": 5},
        "modelUsage": {model: {"inputTokens": 10, "outputTokens": 5, "costUSD": cost}},
        "permission_denials": [],
    }
    if structured is not None:
        env["structured_output"] = structured
    env.update(extra)
    return env


class FakeClaudeCli:
    """A `subprocess.run` stand-in for ClaudeBackend.

    Script items: a dict (JSON envelope on stdout, exit 0), a str (raw stdout, exit 1), a
    (returncode, stdout, stderr) tuple, or an Exception instance to raise. Records every call.
    """

    def __init__(self, script: list[Any]):
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append({"argv": list(argv), "stdin": kw.get("input"), **kw})
        if not self.script:
            raise AssertionError("FakeClaudeCli script exhausted")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, dict):
            return subprocess.CompletedProcess(argv, 0, json.dumps(item), "")
        if isinstance(item, tuple):
            code, out, err = item
            return subprocess.CompletedProcess(argv, code, out, err)
        return subprocess.CompletedProcess(argv, 1, str(item), "")

    @property
    def last(self) -> dict[str, Any]:
        return self.calls[-1]

    def flags(self, i: int = -1) -> list[str]:
        argv = self.calls[i]["argv"]
        return [a for a in argv[1:] if a.startswith("--")]

    def flag_value(self, name: str, i: int = -1) -> str | None:
        argv = self.calls[i]["argv"]
        return argv[argv.index(name) + 1] if name in argv else None


class ScriptedBackend:
    """A gateway Backend: each call pops a ModelResult, a dict (becomes .data), or an Exception."""

    def __init__(self, name: str, script: list[Any], model: str | None = None):
        self.name = name
        self.script = list(script)
        self.model = model or f"{name}-model"
        self.calls: list[dict[str, Any]] = []

    def _next(self, kind: str, task: str, **kw: Any) -> Any:
        self.calls.append({"kind": kind, "task": task, **kw})
        if not self.script:
            raise AssertionError(f"ScriptedBackend {self.name} exhausted")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        from tiffin.model.gateway import ModelResult

        if isinstance(item, ModelResult):
            return item
        if isinstance(item, dict):
            return ModelResult(text="", data=item, model_used=self.model, backend=self.name)
        return ModelResult(text=str(item), data=None, model_used=self.model, backend=self.name)

    def complete_json(self, task: str, prompt: str, schema: dict, budget: Any) -> Any:
        return self._next("json", task, prompt=prompt, budget=budget)

    def converse(self, task: str, messages: list, tools: Any, person_id: int, budget: Any) -> Any:
        return self._next(
            "converse", task, messages=messages, tools=tools, person_id=person_id, budget=budget
        )


def read_notices(state_dir: Any) -> list[dict[str, Any]]:
    """Return all stored operational notices in chronological order."""
    import json as _json
    from pathlib import Path as _Path

    p = _Path(state_dir) / "notices.jsonl"
    if not p.exists():
        return []
    return [_json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line]
