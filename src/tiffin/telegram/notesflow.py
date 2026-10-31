"""Record a person's standing menu notes from Telegram."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..clock import DEFAULT_CLOCK, Clock
from ..food import prefs
from .render import check_text, esc

if TYPE_CHECKING:
    from .handlers import Handlers
    from .poller import Inbound

HALF = {"weekdays": " (weekdays)", "weekend": " (the weekend)", "all": ""}


def noted_said(got: prefs.Noted) -> str:
    """What one change to the notes did, in a sentence: the page says the same."""
    if got.added:
        return f'Kept: "{got.added[0]}". Every menu from the next one is asked for it.'
    if got.dropped:
        return f'Taken off: "{got.dropped[0]}". Menus are no longer asked for it.'
    if got.already:
        return f'"{got.already[0]}" is already one of your notes.'
    if got.refused:
        return "That is too long for a note, or you have as many as a menu can carry."
    return "That is not one of your notes."


class NotesFlow:
    def __init__(self, *, clock: Clock = DEFAULT_CLOCK):
        self.clock = clock

    def cmd_notes(self, h: Handlers, inbound: Inbound, arg: str) -> str:
        """`/notes` lists them; `/notes + less rice` adds one; `/notes - 2` (or its words) takes
        one off; `/notes less rice`, with no sign, adds."""
        person = inbound.person
        said = arg.strip()
        if said:
            sign = said[0] if said[0] in "+-" else "+"
            words = said[1:].strip() if said[0] in "+-" else said
            if not words:
                return self._list(h, inbound)
            current = prefs.standing_notes(h.db, person.id)
            if sign == "-" and words.isdigit() and 1 <= int(words) <= len(current):
                words = current[int(words) - 1]
            with h.db.tx():
                got = prefs.amend_notes(
                    h.db,
                    person.id,
                    add=[words] if sign == "+" else (),
                    drop=[words] if sign == "-" else (),
                    source="chat",
                    clock=self.clock,
                )
            h.outbox.reply(person, esc(noted_said(got)))
            return "handled"
        return self._list(h, inbound)

    def _list(self, h: Handlers, inbound: Inbound) -> str:
        person = inbound.person
        notes = prefs.standing_notes(h.db, person.id)
        if not notes:
            h.outbox.reply(
                person,
                "No notes yet. A note is what every menu is asked for, in your words: "
                "<code>/notes + less rice</code>.",
            )
            return "handled"
        lines = ["<b>What every menu is asked for</b>"]
        lines += [f"{n}. {esc(t)}{HALF[prefs.note_half(t)]}" for n, t in enumerate(notes, 1)][:7]
        lines.append("<code>/notes + less rice</code> adds, <code>/notes - 2</code> takes off.")
        h.outbox.reply(person, check_text("\n".join(lines), kind="notes", max_lines=10))
        return "handled"


def install(handlers: Handlers, *, clock: Clock = DEFAULT_CLOCK) -> NotesFlow:
    flow = NotesFlow(clock=clock)
    handlers.register_command("notes", flow.cmd_notes)
    return flow


__all__ = ["NotesFlow", "install", "noted_said"]
