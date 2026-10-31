"""Show or amend the training week through Telegram's `/week` command."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from ..clock import DEFAULT_CLOCK, Clock, isoday
from .render import check_text, esc

if TYPE_CHECKING:
    from .handlers import Handlers
    from .poller import Inbound


class WeekFlow:
    def __init__(self, *, clock: Clock = DEFAULT_CLOCK):
        self.clock = clock

    def cmd_week(self, h: Handlers, inbound: Inbound, arg: str) -> str:
        """`/week` shows the training week; `/week Mon climb 90, Wed ride 60` rewrites it.

        The week could only be changed by redoing all the onboarding questions, so dropping one
        session - the commonest change there is, when something moves or stops - meant that. It
        is a handful of items, so retyping it is the honest primitive; the parser is the
        interview's own.
        """
        from ..onboarding import Invalid, parse_plan, plan_lines  # noqa: PLC0415

        person = inbound.person
        today = self.clock.today_local()
        if not arg.strip():
            rows = h.db.all(
                "SELECT weekday, kind, target_minutes, start_time FROM planned_sessions "
                "WHERE person_id = ? AND plan_id = (SELECT id FROM training_plans "
                "WHERE person_id = ? AND effective_from <= ? ORDER BY effective_from DESC, "
                "id DESC LIMIT 1) ORDER BY weekday, id",
                (person.id, person.id, isoday(today)),
            )
            if not rows:
                h.outbox.reply(
                    person,
                    "No training week yet. Send <code>/week Mon climb 90, Wed ride 60</code>.",
                )
                return "handled"
            lines = ["<b>Your week</b>", *plan_lines([dict(r) for r in rows])]
            lines.append("Send <code>/week Mon climb 90, Wed ride 60</code> to rewrite it.")
            h.outbox.reply(person, check_text("\n".join(lines), kind="week", max_lines=20))
            return "handled"
        try:
            week = parse_plan(arg, {})
        except Invalid as e:
            h.outbox.reply(person, esc(str(e)))
            return "handled"
        # a ledger, like every other plan: the old week still says what it was
        plan_id = h.db.insert(
            "training_plans",
            person_id=person.id,
            effective_from=isoday(today),
            note="typed",
            source_text=json.dumps(week, ensure_ascii=False),
        )
        for s in week:
            h.db.insert(
                "planned_sessions",
                plan_id=plan_id,
                person_id=person.id,
                weekday=int(s["weekday"]),
                kind=str(s["kind"]),
                target_minutes=int(s["target_minutes"]),
                start_time=s.get("start_time"),
            )
        h.db.insert(
            "onboarding_answers",
            person_id=person.id,
            step="plan",
            answer_json=json.dumps(week),
            message_in_id=inbound.message_in_id,
        )
        sessions = [s for s in week if s["kind"] != "rest"]
        h.outbox.reply(
            person,
            check_text(
                "\n".join(["<b>Your week</b>", *plan_lines(week)]) + f"\n{len(sessions)} sessions.",
                kind="week",
                max_lines=20,
            ),
        )
        return "handled"


def install(handlers: Handlers, *, clock: Clock = DEFAULT_CLOCK) -> WeekFlow:
    flow = WeekFlow(clock=clock)
    handlers.register_command("week", flow.cmd_week)
    return flow


__all__ = ["WeekFlow", "install"]
