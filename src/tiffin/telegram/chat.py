"""Handle free text with tools scoped to the sender's own data."""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from .. import log
from ..clock import DEFAULT_CLOCK, Clock, isoday, parse_day
from ..errors import ModelError, ModelToolUnsupported, TiffinError
from ..model import prompts, tools
from . import convstate, intents
from .dayflow import open_day_named
from .render import esc, fit, strip_emoji

if TYPE_CHECKING:
    from ..model.gateway import Gateway
    from .handlers import Handlers
    from .poller import Inbound

logger = log.get("tiffin.chat")
GatewayFn = Callable[[], "Gateway | None"]
#: the report is on the page, and `_short` links to it when an answer had to be cut
MAX_REPLY_LINES = 4
HISTORY = 6


def history(
    h: Handlers, person_id: int, limit: int = HISTORY, *, before_message_in_id: int | None = None
) -> list[dict[str, Any]]:
    """The last few turns, oldest first: what they typed and what we answered.

    The message being handled is already in `messages_in`, so the caller passes its id to keep
    it out of the history (it goes in as the final user turn instead). Ties on the stamp put the
    reply after the message it answered.
    """
    rows = h.db.all(
        # a compound SELECT can only be ordered by result columns, so the tie-break is one
        "SELECT 'user' AS role, received_at AS at, id AS mid, 1 AS ord, raw_json AS body "
        "FROM messages_in WHERE person_id = ? AND kind = 'text' AND id < ? "
        "UNION ALL "
        "SELECT 'assistant' AS role, created_at AS at, id AS mid, 0 AS ord, text_html AS body "
        "FROM messages_out WHERE person_id = ? AND kind = 'reply' AND status = 'sent' "
        "ORDER BY at DESC, ord ASC, mid DESC LIMIT ?",
        (
            person_id,
            before_message_in_id if before_message_in_id is not None else 1 << 62,
            person_id,
            limit,
        ),
    )
    out: list[dict[str, Any]] = []
    for row in reversed(rows):
        body = str(row["body"] or "")
        if row["role"] == "user":
            try:
                payload = json.loads(body)
                body = str((payload.get("message") or {}).get("text") or "")
            except ValueError:
                body = ""
        if body.strip():
            out.append({"role": str(row["role"]), "content": body[:1500]})
    return out


def command_list(h: Handlers) -> str:
    """The real commands, with what each is for, built from the same table /help renders.

    Read from `handlers`, not written out here, so a command that exists is one the assistant
    knows about and a command that does not cannot be recommended.
    """
    from .handlers import HELP_GROUPS  # noqa: PLC0415 (avoids a cycle)

    lines: list[str] = []
    for _title, items in HELP_GROUPS:
        lines += [f"/{name} - {what}" for name, what in items if name in h.commands]
    described = {name for _t, items in HELP_GROUPS for name, _w in items}
    rest = sorted(n for n in h.commands if n not in described and n not in ("start", "ping"))
    lines += [f"/{n}" for n in rest]
    return "\n".join(lines).replace("&lt;", "<").replace("&gt;", ">")


class ChatFlow:
    def __init__(self, gateway_fn: GatewayFn, *, clock: Clock = DEFAULT_CLOCK, config: Any = None):
        self.gateway_fn = gateway_fn
        self.clock = clock
        self.config = config

    # -- free text -------------------------------------------------------------------
    def reply(self, h: Handlers, inbound: Inbound, text: str) -> str | None:
        """Answer from the person's own numbers. None hands the message to the next handler."""
        cleaned = text.strip()
        if not cleaned:
            return None
        gateway = self.gateway_fn()
        if gateway is None:
            return None
        person = inbound.person
        from ..jobs import shop_refresh  # noqa: PLC0415 (jobs import the scheduler)

        # the shops a list built from chat prefers, same as /shopping and the page (S11)
        shops = shop_refresh.retailer_order(self.config) if self.config is not None else ()
        bound = tools.BoundTools(h.db, person.id, retailers=shops)
        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": prompts.chat_system(
                    tone=person.tone,
                    max_lines=MAX_REPLY_LINES,
                    commands=command_list(h),
                ),
            },
            *history(h, person.id, before_message_in_id=inbound.message_in_id),
            {"role": "user", "content": cleaned[:2000]},
        ]
        before_id = self._last_intent_id(h, person.id)
        try:
            result = gateway.converse("chat", messages, person_id=person.id, tools=bound)
        except TiffinError as e:
            logger.warning(
                "chat failed type=%s",
                type(e).__name__,
                extra={"event": "chat_failed", "person_id": person.id},
            )
            # the Sunday message asks for an open day that went off plan in words ("Wednesday:
            # kebab instead of dinner"). With no model - her, with no consent - "try again" could
            # never work and the day stayed open: it degrades to manual like the check-in's own
            # answer, the meals it replaced and then the day with its note
            day = open_day_named(h.db, person.id, cleaned, self.clock.today_local())
            if day is not None and not convstate.food_answer_is_something_else(cleaned):
                return self.start_offplan(h, inbound, day, cleaned)
            if isinstance(e, ModelToolUnsupported):
                h.outbox.reply(
                    person,
                    "The configured model rejected chat tools. Ask the owner to choose a model "
                    "that supports tools. The manual commands still work.",
                )
                return "error"
            h.outbox.reply(
                person,
                "I could not reach a model just now. The numbers are still being recorded; "
                "try again in a few minutes. /menu still works.",
            )
            return "error"
        answer = (result.text or "").strip()
        pending = self._pending_intents(h, person.id, after_id=before_id)
        if pending:
            if answer:
                # a write tool firing is not the same as having nothing else to say - "no
                # chickpeas!!" got back both a reason ("Wednesday's menu was built before you
                # asked") and the tap that changes it going forward, and dropping the first to
                # make room for the second was Tiffin choosing silence over her own
                # question.
                h.outbox.reply(person, self._short(h, person, answer), model_used=result.model_used)
            outcome = "handled"
            food = any(str(row["kind"]) == "log_offplan" for row in pending)
            for row in pending:
                if food and str(row["kind"]) == "confirm_day":
                    # the food card's own flow closes the day against the plan minus the meals
                    # it replaced; a second "record the day" card beside it was how Friday got
                    # recorded at the whole plan before the food had a number at all
                    h.db.insert("intent_decisions", intent_id=int(row["id"]), decision="lapsed")
                    continue
                if self._confirm_pending(h, inbound, row, result.model_used) != "handled":
                    outcome = "error"
            return outcome
        if not answer:
            h.outbox.reply(person, "I have nothing useful to add to that.")
            return "handled"
        h.outbox.reply(
            person,
            self._short(h, person, answer),
            model_used=result.model_used,
        )
        return "handled"

    def _short(self, h: Handlers, person, answer: str) -> str:
        """The answer in at most MAX_REPLY_LINES lines, with the page named when it was cut."""
        from .render import page_today_link  # noqa: PLC0415

        lines = [ln for ln in answer.splitlines() if ln.strip()]
        link = (
            page_today_link(self.config, label="The rest is on the page")
            if self.config is not None and person.tailscale_login
            else ""
        )
        if len(lines) <= MAX_REPLY_LINES or not link:
            return fit(esc(answer), max_lines=MAX_REPLY_LINES, kind="reply")
        kept = "\n".join(esc(ln) for ln in lines[: MAX_REPLY_LINES - 1])
        return fit(f"{kept}\n{link}.", max_lines=MAX_REPLY_LINES, kind="reply")

    def _pending_intents(self, h: Handlers, person_id: int, *, after_id: int = 0) -> list[dict]:
        """Every intent THIS turn created, oldest first, not any card still waiting for a tap.

        Without the id floor, one ignored card hijacked every later turn: the query found it
        again, `_confirm_pending` re-sent it under the same due_key, the outbox recognised the
        already-sent row and returned it without calling Telegram, and reply() said "handled".
        The model's real answer was discarded and nothing arrived. Every free-text
        message after that went into a void until the person scrolled back and tapped the old
        card, which nothing told them to do.

        The floor is a MAX(id) snapshot taken before the model runs. `message_in_id` cannot
        serve: `tools._intent` never sets it. Nor can `created_at`: stamps are whole seconds,
        so a card written in the same second as the next turn would be a coin toss.
        """
        rows = h.db.all(
            "SELECT i.* FROM intents i LEFT JOIN intent_decisions d ON d.intent_id = i.id "
            "WHERE i.person_id = ? AND i.id > ? AND d.id IS NULL ORDER BY i.id",
            (person_id, after_id),
        )
        return [dict(r) for r in rows]

    def _last_intent_id(self, h: Handlers, person_id: int) -> int:
        return int(
            h.db.scalar(
                "SELECT COALESCE(MAX(id), 0) FROM intents WHERE person_id = ?", (person_id,)
            )
            or 0
        )

    def _confirm_pending(
        self, h: Handlers, inbound: Inbound, row: dict, model_used: str | None
    ) -> str:
        """A write tool fired: show it for a tap instead of answering in prose.

        `log_offplan` carries only a description, because a tool argument is the model talking
        and numbers may never come from there. The kcal are filled by the schema-checked
        `offplan_estimate` task, and that enriched intent is the one the person taps; the bare
        one lapses (append-only: it is superseded, not edited).
        """
        payload = json.loads(row["payload_json"] or "{}")
        if str(row["kind"]) == "log_offplan" and not payload.get("kcal"):
            day = (
                parse_day(str(payload.get("day")))
                if payload.get("day")
                else self.clock.today_local()
            )
            h.db.insert("intent_decisions", intent_id=int(row["id"]), decision="lapsed")
            return self.start_offplan(h, inbound, day, str(payload.get("description") or ""))
        text = fit(esc(intents.describe(str(row["kind"]), payload)), kind="intent")
        from .api import inline_keyboard  # noqa: PLC0415 (small, avoids a cycle at import time)

        keyboard = inline_keyboard(
            [[("Accept", f"k:intent:{row['id']}"), ("Discard", f"k:intentx:{row['id']}")]]
        )
        # the due_key makes the send idempotent, which is what stops a retried turn posting the
        # card twice; but a dedupe returns the earlier row and reaches Telegram with nothing.
        # Ask first, so a turn can never end having said nothing at all.
        deduped = h.outbox.already_sent(inbound.person.id, "intent_confirm", f"intent:{row['id']}")
        if deduped is not None:
            h.outbox.reply(inbound.person, text, keyboard=keyboard)
            return "handled"
        h.outbox.send(
            inbound.person,
            "intent_confirm",
            f"intent:{row['id']}",
            text,
            keyboard,
            model_used=model_used,
        )
        return "handled"

    # -- off-plan estimates ------------------------------------------------------------
    def start_offplan(self, h: Handlers, inbound: Inbound, day: dt.date, description: str) -> str:
        """Food the chat model heard about: ask which planned meals it replaced, then estimate.

        The same question the evening flow asks, because the same sentence means the same
        thing wherever it is typed. `offplan_asker` is DayFlow's; without it (no menu flow
        installed) the food is estimated as an extra on top of the plan.
        """
        asker = getattr(h, "offplan_asker", None)
        if asker is not None:
            return str(asker(h, inbound, day, description))
        return self.estimate(h, inbound, day, description, replaced_slots=[], finalize=False)

    def estimate(
        self,
        h: Handlers,
        inbound: Inbound,
        day: dt.date,
        description: str,
        *,
        replaced_slots: list[str] | None = None,
        finalize: bool = False,
    ) -> str:
        """Called by the evening flow when the day was 'not quite' as planned.

        `replaced_slot` and `finalize` distinguish that evening call from an ordinary daytime
        "I had an extra biscuit" chat message, which also lands here: the evening
        flow names the planned meal the estimate replaces and wants the day closed once the
        estimate is Accepted, however far off `apply()` finds the day right now; the daytime one
        names neither and, if the day is not already confirmed, leaves closing it to the evening
        flow that will run later.
        """
        gateway = self.gateway_fn()
        person = inbound.person
        if gateway is None:
            return self._estimate_unavailable(h, inbound, day, description)
        prompt = prompts.offplan_estimate(description, hints=f"The day is {isoday(day)}.")
        try:
            result = gateway.json_task(
                "offplan_estimate", prompt, "offplan_estimate", person_id=person.id
            )
        except (ModelError, TiffinError) as e:
            logger.warning(
                "estimate failed type=%s", type(e).__name__, extra={"event": "estimate_failed"}
            )
            return self._estimate_unavailable(h, inbound, day, description)
        data = result.data or {}
        kcal = float(data.get("kcal") or 0)
        if kcal <= 0 or not data.get("items"):
            # nothing edible in it. Recording it anyway is how "I will start on Monday" became a
            # confirmed day, and an Accept on a 0 kcal card is how two chocolate bars became
            # nothing. Say so; the day stays open.
            h.outbox.reply(
                inbound.person,
                f"That did not read as food, so nothing is logged for {day:%a %d %b}. "
                "Say what you ate, like <i>2 Twix fingers</i>, or tap As planned.",
            )
            return "handled"
        payload = {
            "day": isoday(day),
            "description": strip_emoji(description)[:300],
            "items": data.get("items", []),
            "kcal": kcal,
            "protein_g": data.get("protein_g"),
            "fat_g": data.get("fat_g"),
            "carb_g": data.get("carb_g"),
            "fibre_g": data.get("fibre_g"),
            "replaced_slots": list(replaced_slots or []),
            "finalize": finalize,
        }
        iid, _ = intents.propose(
            h,
            person,
            "log_offplan",
            payload,
            model_used=result.model_used,
            message_in_id=inbound.message_in_id,
        )
        logger.info(
            "offplan estimate proposed", extra={"event": "estimate", "person_id": person.id}
        )
        return "handled" if iid else "error"

    def _estimate_unavailable(
        self, h: Handlers, inbound: Inbound, day: dt.date, description: str
    ) -> str:
        """No model: record the note against the day rather than lose it.

        Degrade to manual, never to silence: the day still closes with the planned numbers.
        """
        from .. import days as days_mod  # noqa: PLC0415

        planned = days_mod.planned_macros(h.db, inbound.person.id, day)
        macros = planned or days_mod.target_macros(h.db, inbound.person.id, day)
        days_mod.confirm_day(
            h.db,
            inbound.person.id,
            day,
            basis="mixed" if planned else "none",
            macros=macros,
            note=description[:500],
            message_in_id=inbound.message_in_id,
        )
        refresh = getattr(h, "review_refresher", None)
        if refresh is not None:
            refresh(h, inbound.person, [day])  # the week's review says the day is recorded
        h.outbox.reply(
            inbound.person,
            f"Noted for {day:%a %d %b}: {esc(description[:120])}. No model was reachable, so the "
            "day carries the planned numbers and your note.",
        )
        return "handled"


def install(
    handlers: Handlers, gateway_fn: GatewayFn, *, clock: Clock = DEFAULT_CLOCK, config: Any = None
) -> ChatFlow:
    flow = ChatFlow(gateway_fn, clock=clock, config=config)
    # the day flow asks first (an open "what changed?" wins), then free chat
    handlers.text_handlers.append(flow.reply)
    handlers.offplan_estimator = flow.estimate  # type: ignore[attr-defined]
    return flow
