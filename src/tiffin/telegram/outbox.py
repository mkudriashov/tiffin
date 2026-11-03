"""Every outbound message is a messages_out row: sent, failed, or dropped (quiet/paused/dev).

ux_messages_out_sent (person_id, kind, due_key) WHERE status='sent' makes scheduled sends
idempotent. In dev, only the owner's chat id is ever sent to; everything else is dropped_dev.
Ops notices never go to a chat: `alert` records them for the page's status strip.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import log
from ..clock import DEFAULT_CLOCK, Clock
from ..db import Db
from ..errors import TelegramError
from ..persons import Person
from .api import Keyboard, TelegramApi

logger = log.get("tiffin.outbox")
DROPS = ("dropped_quiet", "dropped_paused", "dropped_dev")


@dataclass(frozen=True)
class SendResult:
    id: int
    status: str
    telegram_message_id: int | None = None
    error: str | None = None

    @property
    def sent(self) -> bool:
        return self.status == "sent"


class Outbox:
    def __init__(
        self,
        api: TelegramApi,
        db: Db,
        *,
        env: str,
        admin_chat_id: int | None,
        state_dir: Path | None = None,
        clock: Clock = DEFAULT_CLOCK,
        alert_cooldown_min: int = 180,
        link_signer: Callable[[Person, str], str] | None = None,
    ):
        self.api = api
        self.db = db
        self.env = env
        self.admin_chat_id = admin_chat_id
        self.state_dir = state_dir
        self.clock = clock
        self.alert_cooldown_min = alert_cooldown_min
        #: turns links to the page into ones that sign the recipient's device in on the way;
        #: applied to the text sent only, so the ledger never holds a sign-in token
        self.link_signer = link_signer
        #: how many messages this Outbox has attempted, so a caller can tell whether a handler
        #: said anything at all. A button that answers with silence is the failure the house
        #: rule exists to prevent, and Telegram buttons never expire.
        self.attempts = 0

    # -- core ------------------------------------------------------------------------
    def already_sent(self, person_id: int | None, kind: str, due_key: str) -> SendResult | None:
        row = self.db.one(
            "SELECT id, telegram_message_id FROM messages_out WHERE person_id IS ? AND kind = ? "
            "AND due_key = ? AND status = 'sent'",
            (person_id, kind, due_key),
        )
        if row is None:
            return None
        return SendResult(int(row["id"]), "sent", row["telegram_message_id"])

    def send(
        self,
        person: Person,
        kind: str,
        due_key: str,
        html: str,
        keyboard: Keyboard | None = None,
        *,
        payload: dict[str, Any] | None = None,
        model_used: str | None = None,
        drop: str | None = None,
        sensitive: bool = False,
    ) -> SendResult:
        """Send one message to `person` for (kind, due_key). Idempotent on a prior 'sent'."""
        self.attempts += 1
        prior = self.already_sent(person.id, kind, due_key)
        if prior is not None:
            return prior
        base = {
            "person_id": person.id,
            "chat_id": person.chat_id or 0,
            "kind": kind,
            "due_key": due_key,
            "text_html": "[one-time sign-in link]" if sensitive else html,
            "payload_json": json.dumps(payload or {}, ensure_ascii=False),
            "model_used": model_used,
        }
        if drop is not None:
            if drop not in DROPS:
                raise ValueError(f"unknown drop status {drop}")
            return SendResult(self.db.insert("messages_out", status=drop, **base), drop)
        if person.chat_id is None:
            rid = self.db.insert("messages_out", status="failed", error="no chat id", **base)
            return SendResult(rid, "failed", error="no chat id")
        if self.env != "prod" and person.chat_id != self.admin_chat_id:
            return SendResult(
                self.db.insert("messages_out", status="dropped_dev", **base), "dropped_dev"
            )
        outgoing = html
        if self.link_signer is not None:
            try:
                outgoing = self.link_signer(person, html)
            except Exception:  # a link that does not sign must not cost the message
                logger.exception("page links not signed", extra={"event": "link_sign_error"})
                outgoing = html
        try:
            msg = self.api.send_message(person.chat_id, outgoing, keyboard)
        except TelegramError as e:
            err = str(e)[:300]
            rid = self.db.insert("messages_out", status="failed", error=err, **base)
            logger.warning(
                "send failed kind=%s type=%s",
                kind,
                type(e).__name__,
                extra={"event": "send_failed", "person_id": person.id},
            )
            return SendResult(rid, "failed", error=err)
        tg_id = msg.get("message_id") if isinstance(msg, dict) else None
        stamp = self.clock.utc_stamp()
        try:
            rid = self.db.insert(
                "messages_out", status="sent", telegram_message_id=tg_id, sent_at=stamp, **base
            )
        except sqlite3.IntegrityError:
            prior = self.already_sent(person.id, kind, due_key)
            return prior or SendResult(0, "sent", tg_id)
        return SendResult(rid, "sent", tg_id)

    def reply(
        self,
        person: Person,
        html: str,
        keyboard: Keyboard | None = None,
        *,
        payload: dict[str, Any] | None = None,
        model_used: str | None = None,
        sensitive: bool = False,
    ) -> SendResult:
        due_key = f"{self.clock.utc_stamp()}-{uuid.uuid4().hex[:6]}"
        return self.send(
            person,
            "reply",
            due_key,
            html,
            keyboard,
            payload=payload,
            model_used=model_used,
            sensitive=sensitive,
        )

    # -- ops notices -------------------------------------------------------------------
    def alert(self, admin: Person, event_kind: str, text: str) -> SendResult | None:
        """Record an internal failure for the page's status strip. Never a Telegram message.

        It was a DM to the owner's own chat at any hour, including inside his quiet window - the
        chat that is meant to be about food carried the box's plumbing.
        The page shows these
        to the admin; the status strip shows it.
        """
        del admin  # kept in the signature: every caller names who the notice is for
        from .. import notices  # noqa: PLC0415 (notices is tiny and imports nothing of ours)

        try:
            notices.record(self.state_dir, event_kind, text, clock=self.clock)
        except OSError:
            logger.exception("notice not written: %s", event_kind, extra={"event": "notice_error"})
        logger.warning("ops notice %s", event_kind, extra={"event": "ops_notice"})
        return None
