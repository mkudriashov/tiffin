"""Inbound routing: commands, callbacks (k:<kind>:<id>[:<extra>]), text and files.

Later steps register real handlers via `register_command` / `register_callback` and by
prepending to `text_handlers` (a chain; the first that returns an outcome wins). Every
handler is idempotent (an existing decision answers "already recorded").
"""

from __future__ import annotations

import html
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from .. import log
from ..clock import DEFAULT_CLOCK, Clock
from ..db import Db
from ..model.gateway import consent_granted
from ..persons import Person, PersonRegistry
from .outbox import Outbox
from .poller import Inbound
from .render import check_text

logger = log.get("tiffin.handlers")

if TYPE_CHECKING:
    from ..app import App

CommandFn = Callable[["Handlers", Inbound, str], str]
CallbackFn = Callable[["Handlers", Inbound, list[str]], str]
TextFn = Callable[["Handlers", Inbound, str], "str | None"]


def esc(text: str) -> str:
    return html.escape(text, quote=False)


def _greeting(person) -> str:
    """Greet the person by their supplied name when one exists."""
    name = (person.display_name or "").strip()
    return f" Hello {esc(name)}." if name else " Hello."


class Handlers:
    def __init__(
        self,
        db: Db,
        outbox: Outbox,
        registry: PersonRegistry,
        *,
        clock: Clock = DEFAULT_CLOCK,
        env: str = "dev",
        version: str = "0",
    ):
        self.db = db
        self.outbox = outbox
        self.registry = registry
        self.clock = clock
        self.env = env
        self.version = version
        self.commands: dict[str, CommandFn] = {}
        self.callbacks: dict[str, CallbackFn] = {}
        self.text_handlers: list[TextFn] = []
        self._answered = False  # one answerCallbackQuery per query id; see answer()
        self.model_provider: str | None = None
        self.register_command("ping", _cmd_ping)
        self.register_command("start", _cmd_start)
        self.register_command("help", _cmd_help)
        self.register_command("name", _cmd_name)

    # -- registration ---------------------------------------------------------------
    def register_command(self, name: str, fn: CommandFn) -> None:
        self.commands[name.lower()] = fn

    def register_callback(self, kind: str, fn: CallbackFn) -> None:
        self.callbacks[kind] = fn

    # -- entry -------------------------------------------------------------------------
    def handle(self, inbound: Inbound) -> str:
        """Returns the inbound_outcomes value: handled | ignored."""
        if inbound.just_paired:
            self.outbox.send(
                inbound.person,
                "pairing_welcome",
                f"pair:{inbound.person.id}",
                f"Paired.{_greeting(inbound.person)} Send /onboard to set up, "
                "or /help to see what I do.",
            )
            return "handled"
        self._provider_notice(inbound)
        if inbound.kind == "command":
            name, _, arg = (inbound.text or "/").lstrip("/").partition(" ")
            name = name.split("@", 1)[0].lower()
            fn = self.commands.get(name)
            if fn is None:
                self.outbox.reply(inbound.person, f"Unknown command /{esc(name)}. Try /help.")
                return "ignored"
            return fn(self, inbound, arg.strip())
        if inbound.kind == "callback":
            return self._callback(inbound)
        if inbound.kind == "document":
            self.outbox.reply(inbound.person, "Files are not read. Please type what matters.")
            return "handled"
        if inbound.kind == "voice":
            self.outbox.reply(inbound.person, "Voice notes are not read. Please type it.")
            return "handled"
        if inbound.kind == "text":
            for tfn in self.text_handlers:
                outcome = tfn(self, inbound, inbound.text or "")
                if outcome is not None:
                    return outcome
            self.outbox.reply(
                inbound.person, "Noted. Free text is not wired yet; /help lists commands."
            )
            return "ignored"
        return "ignored"

    def _provider_notice(self, inbound: Inbound) -> None:
        provider = self.model_provider
        if provider is None:
            return
        if inbound.kind == "command":
            command = (inbound.text or "").lstrip("/").split(" ", 1)[0].split("@", 1)[0].lower()
            if command == "consent":
                return
        row = self.db.one(
            "SELECT granted FROM consents WHERE person_id = ? "
            "AND kind IN ('model_processing', 'anthropic_processing') "
            "ORDER BY id DESC LIMIT 1",
            (inbound.person.id,),
        )
        if row is None or not int(row["granted"]):
            return
        if consent_granted(self.db, inbound.person.id, provider=provider):
            return
        key = f"provider_notice_{inbound.person.id}"
        if self.db.state_get(key) == provider:
            return
        self.db.state_set(key, provider)
        self.outbox.reply(
            inbound.person,
            f"The model provider is now {esc(provider.split('@', 1)[0])}. "
            "Model tasks are paused for you. "
            "Send /consent to review where your data would go and choose again.",
        )

    def answer(self, inbound: Inbound, text: str | None = None) -> None:
        """Answer the callback query once, with a toast if the handler has words for it.

        Telegram accepts exactly one answer per query id. Answering unconditionally here,
        before dispatch and with no text, meant a handler's own toast was a SECOND answer to
        the same id - rejected by Telegram and swallowed by the suppress in answer_callback.
        So ticking a shopping item in the aisle, the one interaction that happens standing in
        a shop, gave no feedback at all: no toast, and the message text does not change.
        """
        if inbound.callback_query_id and not self._answered:
            self._answered = True
            self.outbox.api.answer_callback(inbound.callback_query_id, text)

    def _callback(self, inbound: Inbound) -> str:
        data = inbound.callback_data or ""
        parts = data.split(":")
        self._answered = False
        spoke = self.outbox.attempts
        outcome = "ignored"
        try:
            if len(parts) < 2 or parts[0] != "k":
                return outcome
            fn = self.callbacks.get(parts[1])
            if fn is None:
                self.outbox.reply(inbound.person, "That button is not wired yet.")
                return outcome
            outcome = fn(self, inbound, parts[2:])
            return outcome
        finally:
            # A tap that reaches a handler which cannot place it - a menu from three weeks
            # ago, a shopping list that has been superseded, a card from before a restart -
            # returned "ignored" and said NOTHING. Telegram buttons never expire, so sixteen
            # of the twenty-three callbacks answered an old tap with silence, and the person
            # cannot tell that from a bot that has stopped working. The toast costs nothing
            # and is the difference between "that is old" and "this is broken".
            said = self.outbox.attempts > spoke
            if outcome == "ignored" and not said and not self._answered:
                self.answer(inbound, "That button is from an older message and no longer works.")
            self.answer(inbound)  # a no-op if anything above already answered

    # -- helpers for handlers ------------------------------------------------------------
    def clear_buttons(self, inbound: Inbound) -> None:
        if inbound.message_id is not None:
            self.outbox.api.edit_reply_markup(inbound.chat_id, inbound.message_id, None)

    def person_arg(self, inbound: Inbound) -> Person:
        return inbound.person


def _cmd_ping(h: Handlers, inbound: Inbound, arg: str) -> str:
    h.outbox.reply(
        inbound.person, f"pong <code>{h.clock.utc_stamp()}</code> env={h.env} v{h.version}"
    )
    return "handled"


def _cmd_start(h: Handlers, inbound: Inbound, arg: str) -> str:
    h.outbox.reply(
        inbound.person,
        f"{_greeting(inbound.person).strip() or 'Hello.'} This is Tiffin. "
        "/onboard sets you up; /help lists what I do.",
    )
    return "handled"


def _cmd_name(h: Handlers, inbound: Inbound, arg: str) -> str:
    """`/name Sam`. Nothing else could ever set it: persons is written once, at bootstrap."""
    person = inbound.person
    if not arg.strip():
        current = (person.display_name or "").strip()
        h.outbox.reply(
            person,
            (f"I call you {esc(current)}. " if current else "I do not have a name for you. ")
            + "Send <code>/name Sam</code> to change it.",
        )
        return "handled"
    try:
        cleaned = h.registry.set_display_name(person.slug, arg, set_by="self")
    except ValueError as e:
        h.outbox.reply(person, esc(str(e)) + ".")
        return "handled"
    h.outbox.reply(person, f"Noted. I will call you {esc(cleaned)}.")
    return "handled"


HELP_GROUPS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    (
        "Every day",
        (
            ("today", "where the day stands"),
            ("confirm", "record the day as planned"),
            ("measure", "waist and hip, e.g. /measure 84 102"),
        ),
    ),
    (
        "Food",
        (
            ("cook", "a menu now: /cook, or /cook more fish"),
            ("menu", "the dishes planned for the days ahead"),
            ("plan", "make a recipe, menu and shopping list without a model"),
            ("batch", "the cook day: totals, boxes and what to freeze"),
            ("shopping", "the open shopping list"),
            ("shop", "which product a food is: /shop chicken. Never buys anything"),
            ("recipe", "one dish in full: /recipe thursday lunch"),
            ("swap", "swap a dish for one you have had: /swap thu lunch; bare, what you avoid"),
            ("avoid", "what is kept off your menus, to have back: /avoid, or /avoid mushrooms"),
            ("notes", "what every menu is asked for: /notes, /notes + less rice, /notes - 2"),
            ("packs", "how many meats or fish a week your menus keep to"),
            ("page", "the recipe book on the web page: /page, or /page thu"),
            ("web", "a sign-in link for the web page"),
            ("rate", "rate a dish 1-5"),
            ("kit", "what you can cook with"),
        ),
    ),
    (
        "Now and then",
        (
            ("week", "your training week: /week, or /week Mon climb 90, Wed ride 60"),
            ("pause", "/pause 3d - no messages, days count as paused"),
            ("resume", "start again after a pause"),
        ),
    ),
    (
        "You",
        (
            ("name", "what I call you: /name Sam"),
            ("link", "connect to a partner: /link new makes a code, /link CODE enters theirs"),
            ("set", "change any onboarding answer: /set, or /set evening 21:00"),
            ("consent", "whether the configured model may process your data"),
            ("questions", "what you want from Tiffin, a question at a time"),
            ("onboard", "the setup interview; /onboard restart redoes it"),
            ("followup", "answer optional setup questions later"),
        ),
    ),
)


#: the everyday commands, in the order a day uses them. `/help` was 28 lines and listed itself
#: under "Also"; the rest is one `/help all` away and on the page.
EVERYDAY: tuple[tuple[str, str], ...] = (
    ("today", "where the day stands"),
    ("menu", "what is planned"),
    ("recipe", "one dish in full: /recipe thursday lunch"),
    ("shopping", "the open shopping list"),
    ("cook", "a menu now, or /cook more fish"),
    ("swap", "swap a dish for one you have had, at once"),
    ("confirm", "record the day as planned"),
    ("page", "everything on the web page"),
    ("web", "sign in to the web page"),
)


def _cmd_help(h: Handlers, inbound: Inbound, arg: str) -> str:
    if arg.strip().lower() != "all":
        lines = [f"/{n} - {d}" for n, d in EVERYDAY if n in h.commands]
        lines.append("Or just write: what you ate, what you want changed. /help all for the rest.")
        h.outbox.reply(inbound.person, check_text("\n".join(lines), kind="help", max_lines=12))
        return "handled"
    described = {name for _, items in HELP_GROUPS for name, _ in items}
    lines: list[str] = []
    for title, items in HELP_GROUPS:
        shown = [f"/{n} - {d}" for n, d in items if n in h.commands]
        if shown:
            lines += [f"<b>{title}</b>", *shown]
    rest = sorted(
        n for n in h.commands if n not in described and n not in ("start", "ping", "help")
    )
    if rest:
        lines += ["<b>Also</b>", " ".join(f"/{n}" for n in rest)]
    h.outbox.reply(inbound.person, check_text("\n".join(lines), kind="help", max_lines=40))
    return "handled"


def bot_commands(h: Handlers) -> list[dict[str, str]]:
    """What Telegram's "/" menu lists: the everyday commands, from the same table /help uses.

    Nothing ever called setMyCommands, so the menu was empty unless someone had filled it in by
    hand in BotFather.
    """
    return [{"command": n, "description": d[:60]} for n, d in EVERYDAY if n in h.commands] + [
        {"command": "help", "description": "the everyday commands; /help all for the rest"}
    ]


def install_web_command(h: Handlers, app: App) -> None:
    from ..web.identity import issue_login_link  # noqa: PLC0415

    def web_link(handler: Handlers, inbound: Inbound, arg: str) -> str:
        if not app.config.get("web.enabled", True):
            handler.outbox.reply(inbound.person, "The web page is off. Telegram still works here.")
            return "handled"
        if app.config.get("web.identity_mode", "header") == "header":
            handler.outbox.reply(inbound.person, "This page uses Tailscale sign-in.")
            return "handled"
        url = issue_login_link(app, handler.db, inbound.person)
        handler.outbox.reply(
            inbound.person,
            f'Open this five-minute sign-in link: <a href="{url}">Open your page</a>. '
            "It works once and signs in this device for 90 days.",
            sensitive=True,
        )
        return "handled"

    h.register_command("web", web_link)
    h.register_callback("web", lambda handler, inbound, _parts: web_link(handler, inbound, ""))


def payload_of(inbound: Inbound) -> dict[str, Any]:
    return inbound.update
