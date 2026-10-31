"""`/packs` and the one question under a menu: how many meats or fish a week's shop keeps to.

answer is the person's own (food/packs.py `pack_prefs`), asked under their next menu until they
give one, and theirs to change with /packs whenever they like.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..food import packs
from .api import inline_keyboard
from .render import esc

if TYPE_CHECKING:
    from .handlers import Handlers
    from .poller import Inbound


def buttons() -> list[list[tuple[str, str]]]:
    return [[(label, f"k:mains:{n}") for label, n in packs.MAINS_BUTTONS]]


def now_said(mains: int | None) -> str:
    if mains is None:
        return f"{packs.MAINS_QUESTION} You have not said yet, so it is two."
    if mains == 0:
        return "Your menus have no limit on meats or fish a week."
    return f"Your menus keep to {mains} meats or fish a week, each pack used up before the next."


class PackFlow:
    def cmd_packs(self, h: Handlers, inbound: Inbound, _arg: str) -> str:
        person = inbound.person
        h.outbox.reply(
            person,
            esc(now_said(packs.mains_answer(h.db, person.id))),
            inline_keyboard(buttons()),
        )
        return "handled"

    def tap(self, h: Handlers, inbound: Inbound, parts: list[str]) -> str:
        """k:mains:<2|3|0> - their answer, kept and said back."""
        try:
            mains = int(parts[0])
        except IndexError, ValueError:
            return "ignored"
        if mains not in packs.MAINS_ANSWERS:
            return "ignored"
        with h.db.tx():
            said = packs.set_mains(h.db, inbound.person.id, mains, source="chat")
        h.outbox.reply(inbound.person, esc(said + " /packs changes it."))
        return "handled"


def install(handlers: Handlers) -> PackFlow:
    flow = PackFlow()
    handlers.register_command("packs", flow.cmd_packs)
    handlers.register_callback("mains", flow.tap)
    return flow


__all__ = ["PackFlow", "buttons", "install", "now_said"]
