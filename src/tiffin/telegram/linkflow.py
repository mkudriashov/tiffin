"""`/link`: connect to a partner with a code, and disconnect again.

    /link                where you stand
    /link new            make a code for your partner (valid 48 hours, works once)
    /link ABCD-EFGH      enter the code your partner made
    /link cancel         withdraw a code you made
    /link unlink         end the link, now

The rules live in `links.py`; this only says them in words. Linking shares nothing by itself:
weekends are cooked together once you are linked and have both said yes to sharing weekend
menus (/consent).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .. import links
from ..food import shared
from ..links import LinkError
from .api import inline_keyboard
from .render import esc

if TYPE_CHECKING:
    from .handlers import Handlers
    from .poller import Inbound

NEW_WORDS = {"new", "make", "code"}

#: said to the other person when a link ends: neutral, and never about what either of them did
UNLINKED_TO_OTHER = (
    "{name} has unlinked. Weekend menus are your own from now on. A weekend already built stays "
    "as it is, and what you have already seen is not taken back. /link connects you again."
)
UNLINKED_TO_YOU = (
    "You are unlinked. Weekend menus are your own from now on, and a weekend already built stays "
    "as it is. What your partner has already seen is not taken back. Your shared weekend "
    "shopping list is now a list of your own for each of you."
)


def linked_message(name: str, *, both_agreed: bool) -> str:
    if both_agreed:
        return (
            f"You are linked with {name}. Weekends are cooked together, each with your own "
            "portion. /link unlink ends it whenever you like."
        )
    return (
        f"You are linked with {name}. Weekends are cooked together once you have both said "
        "yes to sharing weekend menus: each of you sends /consent and taps Share weekend menus. "
        "Until then your weekends are your own. /link unlink ends it whenever you like."
    )


def status_text(h: Handlers, person_id: int) -> str:
    st = links.state(h.db, person_id)
    if st.kind == "linked" and st.partner_id is not None:
        both = shared.linked_and_both_consented(h.db)
        return linked_message(shared.name_of(h.db, st.partner_id), both_agreed=both)
    if st.kind == "offered" and st.expires_at:
        return (
            "You have a code out, waiting for your partner to enter it. It runs out "
            f"{links.expires_label(st.expires_at)} UTC. /link cancel withdraws it; "
            "/link new makes a fresh one. Until you are linked, weekend menus are your own."
        )
    lead = "You are not linked. Weekend menus are your own."
    if not st.partner_joined:
        return (
            f"{lead} Your partner has not joined yet; whoever runs this install gives them a "
            "join code first (tiffin pair partner). After that /link new makes a code for them."
        )
    return (
        f"{lead} /link new makes a code to give your partner. If they gave you one, "
        "send /link and the code."
    )


def keyboard_for(h: Handlers, person_id: int):
    st = links.state(h.db, person_id)
    if st.kind == "linked":
        return inline_keyboard([[("Unlink", "k:link:ask")]])
    if st.kind == "offered":
        return inline_keyboard([[("New code", "k:link:new"), ("Withdraw it", "k:link:cancel")]])
    if st.partner_joined:
        return inline_keyboard([[("Make a code", "k:link:new")]])
    return None


def _tell_both_linked(h: Handlers, inbound: Inbound, link_id: int) -> None:
    both = shared.linked_and_both_consented(h.db)
    me = inbound.person
    partner_id = links.partner_of(h.db, me.id)
    if partner_id is None:
        return
    other = h.registry.by_id(partner_id)
    h.outbox.reply(me, esc(linked_message(shared.name_of(h.db, partner_id), both_agreed=both)))
    if other is not None:
        h.outbox.send(
            other,
            "link_accepted",
            f"link_accepted:{link_id}",
            esc(linked_message(shared.name_of(h.db, me.id), both_agreed=both)),
        )


class LinkFlow:
    def cmd_link(self, h: Handlers, inbound: Inbound, arg: str) -> str:
        person = inbound.person
        word = arg.strip()
        lowered = word.lower()
        if not word:
            h.outbox.reply(person, esc(status_text(h, person.id)), keyboard_for(h, person.id))
            return "handled"
        if lowered in NEW_WORDS:
            return self._new(h, inbound)
        if lowered in ("cancel", "withdraw", "decline"):
            return self._cancel(h, inbound)
        if lowered == "unlink":
            return self._unlink(h, inbound)
        return self._enter(h, inbound, word)

    def _new(self, h: Handlers, inbound: Inbound) -> str:
        try:
            code = links.mint(h.db, inbound.person.id, "telegram")
        except LinkError as e:
            h.outbox.reply(inbound.person, esc(str(e)))
            return "handled"
        h.outbox.reply(
            inbound.person,
            f"Your code is <code>{code}</code>. Give it to your partner: they send "
            f"/link {code} to this bot, or enter it on the page (More). It works once and runs "
            "out in 48 hours. /link cancel withdraws it.",
            sensitive=True,
        )
        return "handled"

    def _cancel(self, h: Handlers, inbound: Inbound) -> str:
        done = links.decline(h.db, inbound.person.id, "telegram")
        h.outbox.reply(
            inbound.person,
            "The code is withdrawn. It will not work now." if done else "No code is waiting.",
        )
        return "handled"

    def _enter(self, h: Handlers, inbound: Inbound, text: str) -> str:
        try:
            link_id = links.accept(h.db, inbound.person.id, text, "telegram")
        except LinkError as e:
            h.outbox.reply(inbound.person, esc(str(e)))
            return "handled"
        _tell_both_linked(h, inbound, link_id)
        return "handled"

    def _unlink(self, h: Handlers, inbound: Inbound) -> str:
        try:
            done = links.unlink(h.db, inbound.person.id, "telegram")
        except LinkError as e:
            h.outbox.reply(inbound.person, esc(str(e)))
            return "handled"
        h.outbox.reply(inbound.person, esc(UNLINKED_TO_YOU))
        other = h.registry.by_id(done.other_id)
        if other is not None:
            h.outbox.send(
                other,
                "link_ended",
                f"link_ended:{done.link_id}",
                esc(
                    UNLINKED_TO_OTHER.format(
                        name=shared.name_of(h.db, inbound.person.id) or "Your partner"
                    )
                ),
            )
        return "handled"

    def tap(self, h: Handlers, inbound: Inbound, parts: list[str]) -> str:
        """k:link:<new|cancel|ask|unlink>"""
        action = parts[0] if parts else ""
        h.clear_buttons(inbound)
        if action == "new":
            return self._new(h, inbound)
        if action == "cancel":
            return self._cancel(h, inbound)
        if action == "ask":
            h.outbox.reply(
                inbound.person,
                "Unlink now? It takes effect at once and your partner is told. Weekends already "
                "built stay as they are.",
                inline_keyboard([[("Yes, unlink", "k:link:unlink"), ("Keep it", "k:link:keep")]]),
            )
            return "handled"
        if action == "unlink":
            return self._unlink(h, inbound)
        if action == "keep":
            h.outbox.reply(inbound.person, "Still linked.")
            return "handled"
        h.outbox.reply(inbound.person, "That button is old. /link shows where you stand now.")
        return "handled"


def install(handlers: Handlers) -> LinkFlow:
    flow = LinkFlow()
    handlers.register_command("link", flow.cmd_link)
    handlers.register_callback("link", flow.tap)
    return flow
