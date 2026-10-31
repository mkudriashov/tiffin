"""Weekly choices in the chat: "Keep the first choices" under Sunday's message.

Choosing is done on the page (Menu, then Choose), where every meal's dishes can be read side by
side; the chat carries the one tap that needs no reading - the first choices, as they stand - and
says what came of it, with the week's shopping list (food/choices.py).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..clock import DEFAULT_CLOCK, Clock
from ..errors import NothingToBuild
from ..food import choices
from .render import esc

if TYPE_CHECKING:
    from .handlers import Handlers
    from .poller import Inbound


class ChoiceFlow:
    def __init__(self, app: Any, *, clock: Clock = DEFAULT_CLOCK):
        self.app = app
        self.clock = clock

    def tap(self, h: Handlers, inbound: Inbound, parts: list[str]) -> str:
        """k:chs:keep:<set id> - the week set from the first choices, and the list built."""
        from ..jobs import menu_choices, shop_refresh  # noqa: PLC0415 (jobs import the scheduler)

        if len(parts) < 2 or parts[0] != "keep":
            return "ignored"
        try:
            set_id = int(parts[1])
        except ValueError:
            return "ignored"
        person = inbound.person
        try:
            settled = choices.settle(
                self.app,
                h.db,
                person.id,
                set_id,
                how="kept",
                clock=self.clock,
                retailers=tuple(shop_refresh.retailer_order(self.app.config)),
            )
        except LookupError:
            h.outbox.reply(person, "That week is not one of yours.")
            return "handled"
        except NothingToBuild as e:
            h.outbox.reply(person, esc(str(e)))
            return "handled"
        h.outbox.reply(person, menu_choices.settled_text(self.app, settled))
        return "handled"


def install(handlers: Handlers, app: Any, *, clock: Clock = DEFAULT_CLOCK) -> ChoiceFlow:
    flow = ChoiceFlow(app, clock=clock)
    handlers.register_callback("chs", flow.tap)
    return flow


__all__ = ["ChoiceFlow", "install"]
