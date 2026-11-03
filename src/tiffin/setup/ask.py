"""Asking a person, or reading a script's answers, and checking each answer as it comes.

Asked interactively, a question shows its suggested answer in [brackets] and Enter takes it;
`back`, `skip` and `quit` work at any question, and a secret is read without being shown.
Unattended, the suggestion is the answer, and a question with none stops setup naming the flag.
"""

from __future__ import annotations

import contextlib
import getpass
import re
import shutil
import sys
import textwrap
import zoneinfo
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from ..errors import ConfigError
from ..term import Style

if TYPE_CHECKING:
    from .state import Deps


RETAILERS = {
    "sainsburys": "Sainsbury's",
    "tesco": "Tesco",
    "ocado": "Ocado",
    "marks_spencer": "M&S",
    "wholefoods": "Whole Foods",
}


class Back(Exception):
    """Go back to the previous step."""


class Skip(Exception):
    """Leave this step as it is."""


class Quit(Exception):
    """Stop without saving."""


class Redo(Exception):
    """Ask this step's questions again."""


class Invalid(ValueError):
    """An answer that cannot be used; the message says how to put it right."""


CONTROL_WORDS: dict[str, type[Exception]] = {"back": Back, "skip": Skip, "quit": Quit}


def _read_hidden(prompt: str) -> str:
    """A secret from the keyboard with a * for each character, or a plain line from a pipe
    (nothing typed into a pipe is echoed)."""
    if sys.stdin is not None and sys.stdin.isatty():
        return getpass.getpass(prompt, echo_char="*")
    return input(prompt)


class Prompter:
    """Questions for a person at a terminal, or the given answers for a script.

    Asked interactively, each question shows its suggested answer in [brackets] and Enter takes
    it; `back`, `skip` and `quit` work at any question. Without a person, the suggestion is the
    answer, and a question with none stops setup with the flag that would supply it.
    """

    def __init__(self, deps: Deps, *, interactive: bool) -> None:
        self.out = deps.out or sys.stdout
        self.style = deps.style or Style.detect(self.out, deps.environ)
        self.interactive = interactive
        self._read = deps.read
        self._read_secret = deps.read_secret
        columns = deps.width or shutil.get_terminal_size((88, 24)).columns
        self.width = max(40, min(columns, 88) - 2)

    # -- saying ----------------------------------------------------------------------------
    def say(self, text: str = "") -> None:
        print(text, file=self.out, flush=True)

    def wrap(self, text: str, width: int) -> list[str]:
        """Lines of at most `width`, never breaking a path or an address in two."""
        return textwrap.wrap(text, width, break_on_hyphens=False, break_long_words=False) or [""]

    def explain(self, text: str, *, indent: str = "") -> None:
        for line in self.wrap(text, self.width - len(indent)):
            self.say(indent + line)

    def result(self, status: str, text: str) -> None:
        mark = self.style.mark(status)
        lines = self.wrap(text, self.width - 2)
        self.say(f"{mark} {lines[0]}")
        for line in lines[1:]:
            self.say("  " + line)

    # -- asking ----------------------------------------------------------------------------
    def _input(self, prompt: str, *, secret: bool = False) -> str:
        try:
            answer = (self._read_secret if secret else self._read)(prompt)
        except EOFError, OSError:
            raise ConfigError(
                "no answers can be read here (there is no terminal); run tiffin setup "
                "--non-interactive with flags instead"
            ) from None
        word = answer.strip()
        control = CONTROL_WORDS.get(word.lower())
        if control is not None:
            raise control()
        return word

    def _needed(self, flag: str, what: str) -> ConfigError:
        return ConfigError(f"{flag} is needed with --non-interactive ({what})")

    def ask(
        self,
        label: str,
        default: str = "",
        *,
        flag: str = "",
        required: bool = True,
        check: Callable[[str], str] | None = None,
        empty: str = "",
        shown: str = "",
    ) -> str:
        """One answer. `empty` says what Enter means when there is no suggestion; `shown`
        replaces the suggestion in the brackets (for a value that is not shown on screen)."""
        if not self.interactive:
            if not default:
                if required:
                    raise self._needed(flag, label)
                return ""
            if check is None:
                return default
            try:
                return check(default)
            except Invalid as e:
                raise ConfigError(f"{flag or label}: {e}") from None
        hint = shown or default or empty
        suffix = f" [{hint}]" if hint else ""
        while True:
            answer = self._input(f"{label}{suffix}: ") or default
            if not answer:
                if not required:
                    return ""
                self.result("fail", "This one needs an answer.")
                continue
            if check is None:
                return answer
            try:
                cleaned = check(answer)
            except Invalid as e:
                self.result("fail", str(e))
                continue
            if cleaned and cleaned.lower() != answer.lower().strip():
                self.say(f"  Using {cleaned}.")
            return cleaned

    def ask_secret(
        self,
        label: str,
        *,
        saved: str | None = None,
        given: str = "",
        flag: str = "",
        required: bool = True,
    ) -> str | None:
        """A secret, never shown. The new value, or None to keep the saved one (`saved` is its
        mask) or, when not required, to leave it unset."""
        if not self.interactive:
            if given:
                return given
            if saved or not required:
                return None
            raise self._needed(flag, label)
        if given:
            hint = " [given on the command line; Enter uses it]"
        elif saved:
            hint = f" [saved, {saved}; Enter keeps it]"
        elif not required:
            hint = " [Enter to skip]"
        else:
            hint = ""
        while True:
            answer = self._input(f"{label}{hint}: ", secret=True)
            if answer:
                return answer
            if given:
                return given
            if saved or not required:
                return None
            self.result("fail", "This one needs an answer.")

    def choose(
        self,
        label: str,
        options: Sequence[tuple[str, str]],
        default: str,
        *,
        flag: str = "",
        aliases: Mapping[str, str] | None = None,
    ) -> str:
        """One of `options` (key, words); the person may type its number or its key."""
        keys = [key for key, _ in options]
        names = {k.lower(): v for k, v in (aliases or {}).items()}

        def resolve(answer: str) -> str | None:
            word = answer.strip().lower()
            if word.isdigit() and 1 <= int(word) <= len(keys):
                return keys[int(word) - 1]
            word = names.get(word, word)
            return word if word in keys else None

        chosen = resolve(default) if default else None
        if not self.interactive:
            if chosen is None:
                raise ConfigError(f"{flag or label}: choose one of {', '.join(keys)}")
            return chosen
        for number, (_, words) in enumerate(options, 1):
            self.say(f"  {number}  {words}")
        suffix = f" [{keys.index(chosen) + 1}]" if chosen else ""
        while True:
            answer = self._input(f"{label}{suffix}: ")
            if not answer and chosen:
                return chosen
            picked = resolve(answer) if answer else None
            if picked:
                return picked
            self.result("fail", f"Type a number from 1 to {len(options)}.")

    def confirm(self, label: str, default: bool) -> bool:
        if not self.interactive:
            return default
        suffix = " [Y/n]" if default else " [y/N]"
        while True:
            answer = self._input(f"{label}{suffix}: ").lower()
            if not answer:
                return default
            if answer in ("y", "yes"):
                return True
            if answer in ("n", "no"):
                return False
            self.result("fail", "Type y or n.")


def check_timezone(text: str) -> str:
    name = text.strip().replace(" ", "_")
    try:
        # a system zone folder can hold copies under posix/ and right/; the plain names will do
        zones = {
            z for z in zoneinfo.available_timezones() if not z.startswith(("posix/", "right/"))
        }
    except OSError:
        zones = set()
    if name in zones or name == "Europe/London":
        return name
    by_lower = {zone.lower(): zone for zone in zones}
    if name.lower() in by_lower:
        return by_lower[name.lower()]
    tail = sorted(z for z in zones if z.lower().endswith("/" + name.lower()))
    if len(tail) == 1:
        return tail[0]
    if tail:
        raise Invalid(f"{text} could be {', '.join(tail[:4])}: type the whole name.")
    with contextlib.suppress(zoneinfo.ZoneInfoNotFoundError, ValueError, OSError):
        zoneinfo.ZoneInfo(name)
        return name
    raise Invalid(f"{text} is not a time zone. Type one like Europe/London or America/New_York.")


def check_country(text: str) -> str:
    code = text.strip().upper()
    if len(code) != 2 or not code.isalpha():
        raise Invalid("Type the country's two-letter code, for example GB, IE or US.")
    return code


def check_currency(text: str) -> str:
    code = text.strip().upper()
    if len(code) != 3 or not code.isalpha():
        raise Invalid("Type the currency's three-letter code, for example GBP, EUR or USD.")
    return code


def check_name(text: str) -> str:
    cleaned = " ".join(text.split())
    if not cleaned or len(cleaned) > 60 or any(ch in cleaned for ch in "<>&"):
        raise Invalid("A name is 1 to 60 characters, without <, > or &.")
    return cleaned


def one_of(*words: str) -> Callable[[str], str]:
    def check(text: str) -> str:
        word = text.strip().lower()
        if word not in words:
            raise Invalid(f"Type {' or '.join(words)}.")
        return word

    return check


def check_token(text: str) -> str:
    token = text.strip()
    if not re.fullmatch(r"\d{5,}:[A-Za-z0-9_-]{30,}", token):
        raise Invalid(
            "That does not look like a bot token: it is a number, a colon, then a long run of "
            "letters and digits. Copy it again from @BotFather."
        )
    return token


def check_https_address(text: str) -> str:
    """An https address with a host, and maybe a path; never a user name, query or #part."""
    raw = text.strip()
    if "://" not in raw:
        raw = "https://" + raw
    try:
        parts = urlsplit(raw)
        _ = parts.port
    except ValueError:
        raise Invalid("That address has a port that is not a number.") from None
    if parts.scheme != "https":
        raise Invalid("The address must start with https:// (Telegram sign-in needs it).")
    if not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise Invalid("Type just the address, like https://tiffin.example.com, with no ? or #.")
    return f"https://{parts.netloc}{parts.path.rstrip('/')}"


def check_root_path(text: str) -> str:
    path = "/" + text.strip().strip("/") if text.strip().strip("/") else ""
    segments = path.removeprefix("/").split("/") if path else []
    if any(
        segment in ("", ".", "..") or any(c in segment for c in "\\?#% ") for segment in segments
    ):
        raise Invalid("A path is plain words separated by /, for example /tiffin.")
    return path


def check_base_url(text: str) -> str:
    raw = text.strip().rstrip("/")
    try:
        parts = urlsplit(raw)
        _ = parts.port
    except ValueError:
        raise Invalid("That address has a port that is not a number.") from None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise Invalid("Type the full address, like http://127.0.0.1:11434/v1.")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise Invalid("Leave any key out of the address: Tiffin asks for it next.")
    return raw


def check_words(required: bool) -> Callable[[str], str]:
    def check(text: str) -> str:
        cleaned = " ".join(text.split())
        if (required and not cleaned) or len(cleaned) > 120 or any(c in cleaned for c in "<>&"):
            raise Invalid("Plain words, at most 120 characters, without <, > or &.")
        return cleaned

    return check


def check_retailers(text: str) -> str:
    chosen = [w.strip().lower() for w in text.replace(";", ",").split(",") if w.strip()]
    unknown = [w for w in chosen if w not in RETAILERS]
    if not chosen or unknown:
        raise Invalid("Choose from: " + ", ".join(RETAILERS) + " (separate them with commas).")
    return ", ".join(dict.fromkeys(chosen))


def check_folder(text: str) -> str:
    path = Path(text.strip().strip('"')).expanduser()
    if not path.is_absolute():
        raise Invalid("Type the whole path, from the drive or / at the start.")
    if path.exists() and not path.is_dir():
        raise Invalid("That is a file, not a folder.")
    return str(path)
