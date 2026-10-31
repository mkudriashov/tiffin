"""Long-poll loop: getUpdates -> messages_in row + offset in one transaction -> dispatch.

Sender gate: a known chat id maps to its person; an unknown chat id whose text is a
valid unused pairing code gets paired; every other sender is stored as kind='stranger' with
raw_json NULL and receives nothing. Duplicate update_ids get an inbound_outcomes row.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .. import log
from ..clock import DEFAULT_CLOCK, Clock
from ..db import Db
from ..errors import TelegramConflict, TelegramError
from ..persons import Person, PersonRegistry
from .api import TelegramApi

logger = log.get("tiffin.poller")
OFFSET_KEY = "update_offset"


@dataclass(frozen=True)
class Inbound:
    message_in_id: int
    person: Person
    kind: str  # text | voice | document | callback | command | other
    chat_id: int
    update: dict[str, Any]
    text: str | None = None
    callback_data: str | None = None
    callback_query_id: str | None = None
    message_id: int | None = None
    voice: dict[str, Any] | None = None
    document: dict[str, Any] | None = None
    just_paired: bool = False


def classify(update: dict[str, Any]) -> tuple[int | None, str, dict[str, Any]]:
    """(chat_id, kind, parts) for an update; chat_id None when it carries no chat."""
    if "callback_query" in update:
        cq = update["callback_query"]
        msg = cq.get("message") or {}
        chat = msg.get("chat") or {}
        return (
            chat.get("id"),
            "callback",
            {
                "callback_data": cq.get("data"),
                "callback_query_id": cq.get("id"),
                "message_id": msg.get("message_id"),
            },
        )
    msg = update.get("message") or {}
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    if chat.get("type") not in (None, "private"):
        return chat_id, "other", {"message_id": msg.get("message_id")}
    if "voice" in msg or "audio" in msg:
        return (
            chat_id,
            "voice",
            {"voice": msg.get("voice") or msg.get("audio"), "message_id": msg.get("message_id")},
        )
    if "document" in msg:
        # a plan arrives as a file: both people already have theirs written down
        return (
            chat_id,
            "document",
            {"document": msg.get("document"), "message_id": msg.get("message_id")},
        )
    text = msg.get("text")
    if isinstance(text, str):
        kind = "command" if text.startswith("/") else "text"
        return chat_id, kind, {"text": text, "message_id": msg.get("message_id")}
    return chat_id, "other", {"message_id": msg.get("message_id")}


class Poller:
    def __init__(
        self,
        api: TelegramApi,
        db: Db,
        registry: PersonRegistry,
        dispatch: Callable[[Inbound], object],
        *,
        clock: Clock = DEFAULT_CLOCK,
        sleeper: Callable[[float], None] = time.sleep,
        allowed_chat_ids: frozenset[int] | None = None,
        pairing_enabled: bool = True,
    ):
        self.api = api
        self.db = db
        self.registry = registry
        self.dispatch = dispatch
        self.clock = clock
        self.sleeper = sleeper
        self.allowed_chat_ids = allowed_chat_ids  # dev: owner only; None permits pairing
        self.pairing_enabled = pairing_enabled
        self.last_poll_utc: str | None = None
        self.error_streak = 0

    # -- loop ----------------------------------------------------------------------
    def offset(self) -> int:
        return int(self.db.state_get(OFFSET_KEY, "0") or 0)

    def run_once(self) -> int:
        try:
            updates = self.api.get_updates(self.offset())
        except TelegramConflict:
            raise
        except TelegramError as e:
            self.error_streak += 1
            logger.warning(
                "getUpdates failed type=%s", type(e).__name__, extra={"event": "poll_error"}
            )
            self.sleeper(min(2.0**self.error_streak, 60.0))
            return 0
        self.error_streak = 0
        self.last_poll_utc = self.clock.utc_stamp()
        for update in updates:
            try:
                inbound = self.ingest(update)
            except Exception:
                # `ingest` writes the messages_in row and advances the offset in ONE
                # transaction, so anything raising here rolled both back: Telegram redelivered
                # the same update on the next poll, and because nothing wrapped this call the
                # exception reached `runtime._guarded`, which stopped the whole process. One
                # message the schema did not expect - a file, before kind='document' existed -
                # therefore wedged the bot until the update aged out of Telegram's queue about
                # a day later, restarting into the same crash every time. Losing one message
                # loudly is the lesser failure; the offset moves on either way.
                logger.exception("ingest failed", extra={"event": "ingest_error"})
                self._skip(update)
                continue
            if inbound is not None:
                try:
                    self.dispatch(inbound)
                except Exception:
                    logger.exception(
                        "dispatch failed",
                        extra={"event": "dispatch_error", "person_id": inbound.person.id},
                    )
        return len(updates)

    def run_forever(self, stop: threading.Event) -> None:
        while not stop.is_set():
            self.run_once()

    # -- one update --------------------------------------------------------------
    def _allowed(self, chat_id: int) -> bool:
        return self.allowed_chat_ids is None or chat_id in self.allowed_chat_ids

    def ingest(self, update: dict[str, Any]) -> Inbound | None:
        update_id = int(update["update_id"])
        chat_id, kind, parts = classify(update)
        with self.db.tx():
            existing = self.db.one("SELECT id FROM messages_in WHERE update_id = ?", (update_id,))
            if existing is not None:
                self.db.insert(
                    "inbound_outcomes", message_in_id=existing["id"], outcome="duplicate"
                )
                self._advance(update_id)
                return None
            if chat_id is None:
                self._advance(update_id)
                return None
            # An allowlist applies to pairing as well as established identities.
            allowed = self._allowed(chat_id)
            person = self.registry.by_chat_id(chat_id) if allowed else None
            just_paired = False
            if allowed and person is None and self.pairing_enabled and kind == "text":
                person = self.registry.try_pair(chat_id, parts.get("text") or "")
                just_paired = person is not None
            if person is None:
                self.db.insert(
                    "messages_in",
                    update_id=update_id,
                    chat_id=chat_id,
                    kind="stranger",
                    raw_json=None,
                    received_at=self.clock.utc_stamp(),
                )
                self._advance(update_id)
                logger.info("stranger ignored", extra={"event": "stranger"})
                return None
            mid = self.db.insert(
                "messages_in",
                update_id=update_id,
                person_id=person.id,
                chat_id=chat_id,
                kind=kind,
                raw_json=json.dumps(update, ensure_ascii=False),
                received_at=self.clock.utc_stamp(),
            )
            if just_paired:
                self.db.insert("inbound_outcomes", message_in_id=mid, outcome="paired")
            self._advance(update_id)
        return Inbound(
            message_in_id=mid,
            person=person,
            kind=kind,
            chat_id=chat_id,
            update=update,
            text=parts.get("text"),
            callback_data=parts.get("callback_data"),
            callback_query_id=parts.get("callback_query_id"),
            message_id=parts.get("message_id"),
            voice=parts.get("voice"),
            document=parts.get("document"),
            just_paired=just_paired,
        )

    def _advance(self, update_id: int) -> None:
        if update_id + 1 > self.offset():
            self.db.state_set(OFFSET_KEY, str(update_id + 1))

    def _skip(self, update: dict[str, Any]) -> None:
        """Step over an update that could not be recorded at all, so one cannot wedge the queue."""
        try:
            self._advance(int(update["update_id"]))
        except Exception:
            logger.exception("could not step over an update", extra={"event": "ingest_error"})
