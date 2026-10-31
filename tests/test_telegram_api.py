from __future__ import annotations

import json
import urllib.error

import pytest

from tiffin.errors import TelegramConflict, TelegramError
from tiffin.http import TransportError, urllib_transport
from tiffin.telegram.api import TelegramApi, inline_keyboard

from .fakes import RecordingSleeper, ScriptedTransport, err, ok, unreachable


def make(script: list) -> tuple[TelegramApi, ScriptedTransport, RecordingSleeper]:
    t = ScriptedTransport(script)
    s = RecordingSleeper()
    return TelegramApi("123:SECRET", transport=t, sleeper=s), t, s


def test_send_message_posts_html_and_keyboard() -> None:
    api, t, _ = make([ok({"message_id": 5})])
    kb = inline_keyboard([[("Yes", "k:cfm:1"), ("No", "k:cfmx:1")]])
    got = api.send_message(42, "<b>hi</b>", kb)
    assert got["message_id"] == 5
    call = t.calls[0]
    assert call["url"].endswith("/bot123:SECRET/sendMessage")
    body = json.loads(call["data"])
    assert body["parse_mode"] == "HTML" and body["chat_id"] == 42
    assert body["reply_markup"]["inline_keyboard"][0][1]["callback_data"] == "k:cfmx:1"
    assert body["link_preview_options"] == {"is_disabled": True}


def test_long_message_is_truncated_to_cap() -> None:
    api, t, _ = make([ok({"message_id": 1})])
    api.max_chars = 50
    api.send_message(1, "x" * 80)
    assert len(json.loads(t.calls[0]["data"])["text"]) == 50


def test_truncation_keeps_html_valid() -> None:
    api, transport, _ = make([ok({"message_id": 1})])
    api.max_chars = 12
    api.send_message(1, "<b>A &amp; B with a long ending</b>")
    assert json.loads(transport.calls[0]["data"])["text"] == "A &amp; B w…"


def test_429_sleeps_retry_after_then_retries_once() -> None:
    api, t, s = make([err(429, "Too Many Requests", retry_after=3), ok({"message_id": 9})])
    assert api.send_message(1, "a")["message_id"] == 9
    assert s.calls == [4.0] and len(t.calls) == 2


def test_double_429_gives_up() -> None:
    api, _, _ = make([err(429, "x", retry_after=1), err(429, "x", retry_after=1)])
    with pytest.raises(TelegramError, match="429"):
        api.send_message(1, "a")


def test_409_is_a_conflict() -> None:
    api, _, _ = make([err(409, "Conflict: terminated by other getUpdates request")])
    with pytest.raises(TelegramConflict):
        api.get_updates(0)


def test_5xx_and_unreachable_back_off_then_succeed() -> None:
    api, t, s = make([err(502, "bad gateway"), unreachable(), ok([])])
    assert api.get_updates(0) == []
    assert s.calls == [1.0, 2.0] and len(t.calls) == 3


def test_gives_up_after_six_attempts() -> None:
    api, _, s = make([unreachable()] * 6)
    with pytest.raises(TelegramError, match="gave up"):
        api.get_me()
    assert len(s.calls) == 6 and max(s.calls) <= 60


def test_client_error_raises_with_description() -> None:
    api, _, _ = make([err(400, "Bad Request: chat not found")])
    with pytest.raises(TelegramError, match="chat not found"):
        api.send_message(1, "a")


def test_get_updates_uses_long_poll_timeout() -> None:
    api, t, _ = make([ok([{"update_id": 1}])])
    api.get_updates(7)
    body = json.loads(t.calls[0]["data"])
    assert body == {"offset": 7, "timeout": 50, "allowed_updates": ["message", "callback_query"]}
    assert t.calls[0]["timeout"] == 65


def test_transport_error_does_not_expose_bot_token(monkeypatch: pytest.MonkeyPatch) -> None:
    url = "https://api.telegram.org/bot123:SECRET/getUpdates"

    def fail(*args: object, **kwargs: object) -> None:
        raise urllib.error.URLError(f"could not open {url}")

    monkeypatch.setattr("urllib.request.urlopen", fail)
    with pytest.raises(TransportError) as caught:
        urllib_transport(url, "POST", b"{}", {}, 1)
    assert "SECRET" not in str(caught.value)
    assert "api.telegram.org" in str(caught.value)


def test_keyboard_rejects_long_or_non_ascii_data() -> None:
    with pytest.raises(ValueError):
        inline_keyboard([[("x", "k:" + "a" * 70)]])
    with pytest.raises(ValueError):
        inline_keyboard([[("x", "k:é")]])


def test_repr_hides_token() -> None:
    api, _, _ = make([])
    assert "SECRET" not in repr(api)
