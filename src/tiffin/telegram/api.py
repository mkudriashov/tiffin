"""Telegram Bot API over the shared transport. HTML messages, inline keyboards, long-poll.

Retry policy: 429 -> sleep parameters.retry_after + 1 s, one retry; 5xx or unreachable ->
backoff 1..60 s, six attempts; 409 -> TelegramConflict (another poller holds this token;
dev must use the dev token). The token never appears in an error message or a log line.
"""

from __future__ import annotations

import contextlib
import html
import json
import time
from collections.abc import Callable
from html.parser import HTMLParser
from typing import Any

from ..clock import DEFAULT_CLOCK, Clock
from ..errors import TelegramConflict, TelegramError
from ..http import Response, Transport, TransportError, urllib_transport

Keyboard = dict[str, Any]
ALLOWED_UPDATES = ["message", "callback_query"]
MAX_CALLBACK_BYTES = 64


class _PlainText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _fit_message(markup: str, max_chars: int) -> str:
    if len(markup) <= max_chars:
        return markup
    parser = _PlainText()
    parser.feed(markup)
    budget = max_chars - 1
    fitted: list[str] = []
    length = 0
    for char in "".join(parser.parts):
        escaped = html.escape(char, quote=False)
        if length + len(escaped) > budget:
            break
        fitted.append(escaped)
        length += len(escaped)
    return "".join(fitted) + "…"


#: characters across a button row on a ~400 px phone at the default size - the same width the
#: message text itself wraps to (phone_reading measures both against this number)
PHONE_COLS = 36
#: never a row this wide - five across is already a squeeze on a phone's home-screen width
MAX_ROW = 5


def button_rows(buttons: list[tuple[str, str]], *, max_row: int = 3) -> list[list[tuple[str, str]]]:
    """Pack buttons into rows that fit a phone: as many per row as `max_row` allows, but never a
    row whose own share of the width (`PHONE_COLS` split evenly, two characters of padding a
    button) is too narrow for the longest label going into it.

    A fixed "three across" was the whole of it before, and a label that grows with the day
    ("Breakfast Mon", "Lunch Mon: 2 boxes") stopped fitting the moment it was more than just the
    slot name - phone_reading/12: short by a few characters is still unreadable on a real phone,
    not merely untidy.
    """
    rows: list[list[tuple[str, str]]] = []
    i, n = 0, len(buttons)
    cap = min(max_row, MAX_ROW)
    while i < n:
        size = min(cap, n - i)
        while size > 1:
            room = (PHONE_COLS - 2 * size) // size
            if all(len(label) <= room for label, _ in buttons[i : i + size]):
                break
            size -= 1
        rows.append(buttons[i : i + size])
        i += size
    return rows


def inline_keyboard(rows: list[list[tuple[str, str]]]) -> Keyboard:
    """[[("As planned", "k:cfm:12"), ("Not quite", "k:cfmx:12")]] -> reply_markup."""
    out = []
    for row in rows:
        buttons = []
        for label, data in row:
            if not data.isascii() or len(data.encode()) > MAX_CALLBACK_BYTES:
                raise ValueError(f"callback data must be ASCII and <= 64 bytes: {data!r}")
            buttons.append({"text": label, "callback_data": data})
        out.append(buttons)
    return {"inline_keyboard": out}


class TelegramApi:
    def __init__(
        self,
        token: str,
        *,
        transport: Transport = urllib_transport,
        clock: Clock = DEFAULT_CLOCK,
        sleeper: Callable[[float], None] = time.sleep,
        base: str = "https://api.telegram.org",
        poll_timeout_s: int = 50,
        http_timeout_extra_s: int = 15,
        max_chars: int = 4000,
    ):
        self._token = token
        self.transport = transport
        self.clock = clock
        self.sleeper = sleeper
        self.base = base.rstrip("/")
        self.poll_timeout_s = poll_timeout_s
        self.http_timeout_extra_s = http_timeout_extra_s
        self.max_chars = max_chars

    def __repr__(self) -> str:
        return "TelegramApi(token=***)"

    # -- transport ------------------------------------------------------------------
    def _call(
        self, method: str, payload: dict[str, Any] | None = None, *, timeout_s: float = 30.0
    ) -> Any:
        url = f"{self.base}/bot{self._token}/{method}"
        data = json.dumps(payload or {}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        delay = 1.0
        retried_429 = False
        last = "no attempt"
        for _ in range(6):
            try:
                resp = self.transport(url, "POST", data, headers, timeout_s)
            except TransportError as e:
                last = f"unreachable: {e}"
                self.sleeper(delay)
                delay = min(delay * 2, 60.0)
                continue
            if resp.status == 409:
                raise TelegramConflict(f"{method}: 409 {_description(resp)}")
            if resp.status == 429:
                if retried_429:
                    raise TelegramError(f"{method}: 429 twice")
                retried_429 = True
                self.sleeper(_retry_after(resp) + 1.0)
                continue
            if resp.status >= 500:
                last = f"{resp.status}"
                self.sleeper(delay)
                delay = min(delay * 2, 60.0)
                continue
            try:
                body = resp.json()
            except ValueError:
                raise TelegramError(f"{method}: {resp.status} non-JSON body") from None
            if resp.status != 200 or not body.get("ok"):
                raise TelegramError(f"{method}: {resp.status} {body.get('description', '?')}")
            return body.get("result")
        raise TelegramError(f"{method}: gave up ({last})")

    # -- methods --------------------------------------------------------------------
    def get_me(self) -> dict[str, Any]:
        return self._call("getMe")

    def set_my_commands(self, commands: list[dict[str, str]]) -> None:
        """The bot's "/" menu in Telegram, replacing whatever was set before."""
        self._call("setMyCommands", {"commands": commands})

    def get_updates(self, offset: int, timeout_s: int | None = None) -> list[dict[str, Any]]:
        poll = self.poll_timeout_s if timeout_s is None else timeout_s
        payload = {"offset": offset, "timeout": poll, "allowed_updates": ALLOWED_UPDATES}
        result = self._call("getUpdates", payload, timeout_s=poll + self.http_timeout_extra_s)
        return list(result or [])

    def send_message(
        self,
        chat_id: int,
        html: str,
        keyboard: Keyboard | None = None,
        *,
        disable_preview: bool = True,
    ) -> dict[str, Any]:
        text = _fit_message(html, self.max_chars)
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": disable_preview},
        }
        if keyboard:
            payload["reply_markup"] = keyboard
        return self._call("sendMessage", payload)

    def answer_callback(self, callback_query_id: str, text: str | None = None) -> None:
        payload: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text[:200]
        with contextlib.suppress(TelegramError):  # a stale query id is not worth a failed handler
            self._call("answerCallbackQuery", payload)

    def edit_reply_markup(
        self, chat_id: int, message_id: int, keyboard: Keyboard | None = None
    ) -> None:
        payload: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id}
        payload["reply_markup"] = keyboard or {"inline_keyboard": []}
        try:
            self._call("editMessageReplyMarkup", payload)
        except TelegramError as e:
            if "not modified" not in str(e):
                raise


def _description(resp: Response) -> str:
    try:
        return str(resp.json().get("description", ""))
    except ValueError:
        return ""


def _retry_after(resp: Response) -> float:
    try:
        return float(resp.json().get("parameters", {}).get("retry_after", 1))
    except ValueError, AttributeError, TypeError:
        return 1.0
