"""A stand-in world for `tiffin setup`: Telegram, intervals.icu, a model and a person typing.

Pairing codes are made predictable (AAAA-AAAA first, then BBBB-BBBB, ...), so a test can have
"the person" send the code the wizard showed. Every secret here is made up, and none has the
shape of a real one.
"""

from __future__ import annotations

import io
import itertools
import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from tiffin import persons
from tiffin.cli import build_parser
from tiffin.clock import FrozenClock
from tiffin.http import Response, TransportError
from tiffin.setup import Deps, run
from tiffin.term import Style

GOOD_TOKEN = "123456:" + "G" * 35
BAD_TOKEN = "123456:" + "B" * 35
GOOD_ICU = "icu-good-" + "k" * 16
BAD_ICU = "icu-bad-" + "x" * 16
MODEL_KEY = "model-key-" + "m" * 20
SECRETS = (GOOD_TOKEN, BAD_TOKEN, GOOD_ICU, BAD_ICU, MODEL_KEY)
OWNER_CODE = "AAAA-AAAA"
SECOND_CODE = "BBBB-BBBB"
CHAT_ID = 4242


def predictable_codes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pairing codes in order: AAAA-AAAA, BBBB-BBBB, CCCC-CCCC ..."""
    calls = itertools.count()
    monkeypatch.setattr(persons._secrets, "choice", lambda _letters: "ABCDEFGH"[next(calls) // 8])


@dataclass
class World:
    """Telegram answers getMe for GOOD_TOKEN only; getUpdates hands out `sent` messages."""

    sent: list[str] = field(default_factory=list)
    chat_id: int = CHAT_ID
    telegram_down: bool = False
    urls: list[str] = field(default_factory=list)

    def transport(
        self, url: str, method: str, data: bytes | None, headers: Mapping[str, str], timeout: float
    ) -> Response:
        self.urls.append(url)
        if "api.telegram.org" in url:
            if self.telegram_down:
                raise TransportError("POST api.telegram.org: timed out")
            if GOOD_TOKEN not in url:
                return Response(401, b'{"ok":false,"description":"Unauthorized"}')
            if url.endswith("/getMe"):
                return Response(200, b'{"ok":true,"result":{"username":"example_tiffin_bot"}}')
            if url.endswith("/getUpdates"):
                offset = int(json.loads(data or b"{}").get("offset", 0))
                updates = [
                    {
                        "update_id": number,
                        "message": {"chat": {"id": self.chat_id, "type": "private"}, "text": text},
                    }
                    for number, text in enumerate(self.sent, 1)
                    if number >= offset
                ]
                return Response(200, json.dumps({"ok": True, "result": updates}).encode())
        if "/wellness?" in url:
            if headers.get("Authorization") == f"Basic {_basic(GOOD_ICU)}":
                return Response(200, b"[]")
            return Response(403, b'{"error":"forbidden"}')
        raise AssertionError(f"setup reached an address it has no business with: {url}")


def _basic(key: str) -> str:
    import base64

    return base64.b64encode(f"API_KEY:{key}".encode()).decode("ascii")


class Keyboard:
    """Types the answers in order. What it typed shows on the screen, as on a terminal; what
    it typed for a hidden question does not."""

    def __init__(self, answers: Iterable[str]) -> None:
        self.answers = list(answers)
        self.out = io.StringIO()
        self.prompts: list[str] = []

    def _next(self, prompt: str) -> str:
        if not self.answers:
            raise AssertionError(f"an unexpected question: {prompt!r}\n{self.out.getvalue()}")
        self.prompts.append(prompt)
        return self.answers.pop(0)

    def read(self, prompt: str) -> str:
        answer = self._next(prompt)
        self.out.write(prompt + answer + "\n")
        return answer

    def read_secret(self, prompt: str) -> str:
        answer = self._next(prompt)
        self.out.write(prompt + "\n")
        return answer

    @property
    def screen(self) -> str:
        return self.out.getvalue()


class Ticker:
    """A clock for waiting that moves one second each time it is read."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        self.now += 1.0
        return self.now


def deps(
    world: World,
    keyboard: Keyboard,
    clock: FrozenClock,
    *,
    environ: Mapping[str, str] | None = None,
    container: str | None = None,
    probe: Callable[[Any], None] | None = None,
    installed: list[Path] | None = None,
) -> Deps:
    return Deps(
        transport=world.transport,
        environ=dict(environ or {}),
        read=keyboard.read,
        read_secret=keyboard.read_secret,
        out=keyboard.out,
        style=Style(terminal=True, colour=False, unicode=True),
        clock=clock,
        monotonic=Ticker(),
        sleep=lambda _seconds: None,
        which=lambda _name: None,
        container=lambda: container,
        install_service=lambda home, env: (installed if installed is not None else []).append(home),
        probe_model=probe or (lambda app: None),
        pair_wait_s=5,
        width=88,
    )


def setup(argv: list[str], the_deps: Deps) -> int:
    """`tiffin <argv>` for a set-up command, with the stand-in world."""
    return run(build_parser().parse_args(argv), the_deps)


def assert_no_secret_shown(*texts: str) -> None:
    for text in texts:
        for secret in SECRETS:
            assert secret not in text, "a secret was printed"
