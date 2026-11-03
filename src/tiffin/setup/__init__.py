"""`tiffin setup`: a guided set-up that is safe to run again, and the same steps for scripts.

Seven parts, then a review: home and time zone, people, Telegram, AI model, training data, web
page and shopping. Each part says in a sentence or two what it is for, offers the current value
(or a sensible default) in [brackets] and checks its answers as it goes: the bot token with
Telegram, the intervals.icu key with intervals.icu, and the model with one tiny request if the
person agrees after being told what that may cost. A failed check says what went wrong and how to
put it right, and the person can try again, skip an optional part, or go back a step. Secrets are
read without being shown and are only ever printed masked.

Nothing is written until the review is accepted, apart from pairing a Telegram chat, which is
kept the moment it happens. Run again, setup shows what is set and changes one part:
tiffin.toml keeps every other line and its comments, a copy of the previous file goes to
`state/setup/`, and a saved secret is replaced only when a new one is typed or given.

With --non-interactive the answers come from flags and environment variables (secrets from
TIFFIN_TELEGRAM_TOKEN, TIFFIN_MODEL_API_KEY, TIFFIN_ICU_KEY_OWNER and TIFFIN_ICU_KEY_PARTNER, or
their _FILE forms). A missing or wrong answer stops it with exit code 2 before anything is
written, and on an existing install only the parts its flags name are changed. `tiffin init` and
`tiffin connect <part>` run this same set-up.
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Mapping
from pathlib import Path

from ..config import default_home
from ..errors import ConfigError
from ..term import table
from .ask import Invalid, Prompter, Quit, check_timezone
from .finish import _finish
from .flow import ToMenu, _flags_given, _menu, _save, _walk
from .state import Deps, State
from .steps import (
    COUNTRY_CURRENCY,
    PROVIDERS,
    SECTION_LIST,
    SECTION_NAMES,
    SECTIONS,
    ZONE_COUNTRY,
    MoveHome,
)

__all__ = [
    "COUNTRY_CURRENCY",
    "SECTIONS",
    "ZONE_COUNTRY",
    "Deps",
    "Invalid",
    "add_arguments",
    "check_timezone",
    "default_deps",
    "run",
]


def default_deps() -> Deps:
    return Deps()


def _section_of(args: argparse.Namespace) -> str | None:
    section = getattr(args, "section", None) or getattr(args, "target", None)
    return {"intervals": "training"}.get(section, section) if section else None


def _resolve_home(args: argparse.Namespace, environ: Mapping[str, str]) -> Path:
    home = Path(args.home or environ.get("TIFFIN_HOME") or default_home(environ)).expanduser()
    if not home.is_absolute():
        raise ConfigError("the home must be an absolute path")
    return home


def run(args: argparse.Namespace, deps: Deps | None = None) -> int:
    deps = deps or default_deps()
    environ = os.environ if deps.environ is None else deps.environ
    p = Prompter(deps, interactive=not args.non_interactive)
    home = _resolve_home(args, environ)
    section = _section_of(args)
    if p.interactive:
        p.say(p.style.heading("Tiffin setup"))
        p.explain(
            "Press Enter to take the answer in [brackets]. Type back to go to the previous "
            "step, skip to leave an optional step as it is, or quit to stop. Nothing is saved "
            "until you say so at the end."
        )
    s: State | None = None
    saved = False
    try:
        while True:
            s = State(home, args, deps, p.style)
            if section:
                plan = [section]
            elif s.first_run or not p.interactive:
                plan = (
                    list(SECTION_NAMES)
                    if s.first_run
                    else [x.key for x in SECTION_LIST if _flags_given(args, x)]
                )
            else:
                try:
                    plan = _menu(s, p)
                except Quit:
                    p.result("info", "Nothing changed.")
                    return 0
            s.full = len(plan) == len(SECTION_NAMES)
            try:
                _walk(s, p, plan, menu=not section and not s.first_run and p.interactive)
            except MoveHome as move:
                home = move.home
                continue
            except ToMenu:
                continue
            break
        if not plan:
            if not p.interactive:
                p.result("ok", "Nothing to change. Current settings:")
                for line in table([[x.title, x.show(s, False)] for x in SECTION_LIST]):
                    p.say(line)
            return _finish(s, p, saved=False)
        saved = _save(s, p, len(plan) + 1)
        if not saved:
            return 0
        return _finish(s, p, saved=True)
    except KeyboardInterrupt:
        p.say("")
        p.result("warn", _stopped(s, saved))
        return 130
    except Quit:
        p.say("")
        p.result("info", _stopped(s, saved))
        return 1


def _stopped(s: State | None, saved: bool) -> str:
    if saved:
        return "Setup stopped after saving your settings; run tiffin doctor to check them."
    paired = s is not None and bool(s.paired_now)
    return "Setup stopped. Nothing was saved" + (
        " apart from pairing, which is kept." if paired else "."
    )


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--home", help="absolute data folder (default: TIFFIN_HOME, or your user data folder)"
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="ask nothing: take every answer from the flags below and environment variables, "
        "and stop with exit code 2 if one is missing or wrong",
    )
    answers = parser.add_argument_group(
        "answers",
        "Each flag answers one question; asked interactively, it is the suggested answer. Pass "
        "secrets as environment variables rather than flags (TIFFIN_TELEGRAM_TOKEN, "
        "TIFFIN_MODEL_API_KEY, TIFFIN_ICU_KEY_OWNER, TIFFIN_ICU_KEY_PARTNER, or the same names "
        "ending _FILE that name a file), so they stay out of your shell history.",
    )
    answers.add_argument("--timezone", metavar="ZONE", help="for example Europe/London")
    answers.add_argument("--country", metavar="CC", help="two letters, for example GB")
    answers.add_argument("--currency", metavar="CODE", help="three letters, for example GBP")
    answers.add_argument("--weight-unit", choices=["kg", "lb"])
    answers.add_argument("--height-unit", choices=["cm", "in"])
    answers.add_argument("--owner-name", metavar="NAME", help="what Tiffin calls you")
    answers.add_argument("--partner", choices=["yes", "no"], help="yes when two people use it")
    answers.add_argument("--partner-name", metavar="NAME")
    answers.add_argument(
        "--telegram-token", metavar="TOKEN", help="your bot's token from @BotFather"
    )
    answers.add_argument(
        "--owner-chat-id", type=int, metavar="ID", help="pair your chat by its number, not a code"
    )
    answers.add_argument(
        "--pair",
        choices=["wait", "later"],
        help="wait now for your pairing code to reach the bot (default), or print a code to send "
        "once Tiffin is running",
    )
    answers.add_argument("--model-provider", choices=list(PROVIDERS))
    answers.add_argument("--model-name", metavar="NAME", help="only to override Tiffin's choice")
    answers.add_argument("--model-key", metavar="KEY")
    answers.add_argument("--model-base-url", metavar="URL", help="for openai-compatible")
    answers.add_argument(
        "--model-test",
        choices=["yes", "no"],
        help="send the model one tiny request to check it (default no; it may cost a fraction "
        "of a penny)",
    )
    answers.add_argument("--icu-key", metavar="KEY", help="an intervals.icu API key")
    answers.add_argument(
        "--person", choices=["owner", "partner"], help="whose intervals.icu key --icu-key is"
    )
    answers.add_argument("--icu-athlete", help=argparse.SUPPRESS)  # not needed: the key says
    answers.add_argument("--web-mode", choices=["off", "local", "proxy", "tailscale"])
    answers.add_argument("--web-origin", metavar="URL", help="the address people open")
    answers.add_argument("--web-root-path", metavar="PATH", help="for example /tiffin")
    answers.add_argument(
        "--web-listen",
        choices=["loopback", "network"],
        help="network only when your web server is another container on a private network",
    )
    answers.add_argument("--hosting-records", metavar="WORDS", help="where the records are held")
    answers.add_argument("--hosting-web-front", metavar="WORDS", help="for example Cloudflare")
    answers.add_argument("--product-links", choices=["yes", "no"])
    answers.add_argument("--retailers", metavar="LIST", help="for example sainsburys,tesco")
    answers.add_argument("--run-mode", choices=["foreground", "service"])
    answers.add_argument(
        "--no-catalogue", action="store_true", help="do not load the food list when it is empty"
    )
