"""Every string Tiffin can say, read out of the source rather than reached by a test.

The dynamic sweeps drive the paths a test can arrange. Most user-facing strings live on
branches that need a particular failure to reach - a model that is down, a solver that cannot
close, a file that will not parse - and those are exactly the messages a person meets on a bad
day, when they can least afford jargon or a dead end.

So this reads them straight out of the AST: every piece of prose in Tiffin's own source,
plus every message template under config/prompts/messages. Then it asserts the house rules on
all of them at once.

Two collections, because Tiffin speaks to two audiences. `ALL` is everything, and the
rules about jargon, emoji, dead commands and moralising hold everywhere. `PHONE` is the subset
written in the modules that render to Telegram, and the HTML rules apply only there - an
argparse placeholder like `TIFFIN_HOME` is correct in a terminal and would be nonsense on a
phone. A guard test keeps that split honest.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "tiffin"
TEMPLATES = Path(__file__).resolve().parents[1] / "config" / "prompts" / "messages"

#: where text that reaches a phone is written. Everything else talks to an operator in a
#: terminal. `test_nothing_outside_the_phone_surface_speaks_to_a_person` stops this rotting.
PHONE_DIRS = ("telegram", "jobs")
PHONE_FILES = (
    "onboarding.py",
    "runtime.py",
    "scheduler.py",
    # domain modules that raise messages a handler puts on a phone verbatim
    "body.py",
    "persons.py",
    "documents.py",
    # what a swap or a change to a food list says back, word for word, on a phone and the page
    "swap.py",
    "prefs.py",
    # what a build in flight, a build that broke and one a restart killed say, bot and page
    "building.py",
)
#: the wire, not the words: `telegram/api.py` speaks HTTP to Telegram and builds no prose.
#: Its only long strings are protocol and `TelegramError` text, and the echoed-exception guard
#: below is what keeps that true - the day a handler replies with one, this stops being safe.
PHONE_EXCLUDE = ("api.py",)
#: exception classes whose message some handler shows a person as-is, and where the words
#: are written. A new one has to be declared here, which is what makes the split hold.
ECHOED_TO_A_PERSON = {
    "Invalid": ("onboarding.py",),
    "ValueError": ("body.py", "persons.py", "swap.py"),
    "NothingToBuild": ("menuflow.py", "swap.py"),
    "Infeasible": ("swap.py",),
}

#: words the code uses for its own bookkeeping. A person has never been shown them.
JARGON = (
    "run_ledger",
    "conversation_state",
    "person_id",
    "due_key",
    "menu_id",
    "document_id",
    "sqlite",
    "traceback",
    "json",
    "nonetype",
    "kwargs",
    "callback",
    "catch_up",
)
#: HTML the renderer allows. Anything else arrives as literal angle brackets on the phone.
ALLOWED_TAGS = {"b", "i", "code", "pre", "a"}

#: SQL written the way this codebase writes it - keywords in capitals. Prose never shouts
#: FROM or WHERE, so this separates a query fragment from a sentence without guessing.
_SQL_START = re.compile(r"^\s*(select|insert|update|delete|create|drop|alter|pragma|with)\b", re.I)
_SQL_LOUD = re.compile(
    r"\b(SELECT|INSERT INTO|DELETE FROM|CREATE (?:TABLE|VIEW|INDEX|TRIGGER)"
    r"|DROP (?:TABLE|VIEW|INDEX|TRIGGER)|ALTER TABLE|PRAGMA|ORDER BY|GROUP BY)\b"
)


def _is_sql(text: str) -> bool:
    return bool(_SQL_START.match(text) or _SQL_LOUD.search(text))


def _flat(node: ast.AST) -> str:
    """One string for a literal or an f-string, with the substitutions as a placeholder.

    Walking an f-string's parts separately splits "<code>...{x}...</code>" into two literals
    and makes a balanced tag look unbalanced, which is how a sweep gets ignored.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            part.value if isinstance(part, ast.Constant) and isinstance(part.value, str) else "{}"
            for part in node.values
        )
    return ""


#: calls whose arguments only ever reach a log line, a ledger row or a dict lookup
QUIET_CALLS = ("get", "warning", "info", "error", "exception", "debug", "note", "degrade", "skip")


def _prose_in(path: Path) -> list[tuple[str, int, str]]:
    """(file, line, text) for every piece of prose in one module.

    Deliberately wide: most user-facing text is built in a helper and returned, not handed
    straight to `reply`, so following only the speaking calls misses the messages that live on
    the failure branches - which are exactly the ones a person meets on a bad day.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    skip: set[int] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
        ):
            skip.add(id(node.body[0].value))
        if isinstance(node, ast.JoinedStr):
            # the parts belong to the whole; reading them again splits balanced markup
            skip.update(id(part) for part in node.values)
        if isinstance(node, ast.Call):
            name = (
                node.func.attr
                if isinstance(node.func, ast.Attribute)
                else getattr(node.func, "id", "")
            )
            if name in QUIET_CALLS:
                skip.update(id(arg) for arg in ast.walk(node))
    out: list[tuple[str, int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant | ast.JoinedStr) or id(node) in skip:
            continue
        text = _flat(node)
        if len(text.strip()) < 20 or " " not in text or _is_sql(text):
            continue
        if not re.search(r"[a-z]{3}", text):  # identifiers and format specs, not sentences
            continue
        out.append((path.name, getattr(node, "lineno", 0), text))
    return out


def _speaks_to_a_phone(path: Path) -> bool:
    if path.name in PHONE_EXCLUDE:
        return False
    return path.parent.name in PHONE_DIRS or path.name in PHONE_FILES


def spoken_strings() -> tuple[list[tuple[str, int, str]], list[tuple[str, int, str]]]:
    everything: list[tuple[str, int, str]] = []
    phone: list[tuple[str, int, str]] = []
    for path in sorted(SRC.rglob("*.py")):
        found = _prose_in(path)
        everything += found
        if _speaks_to_a_phone(path):
            phone += found
    return everything, phone


def template_strings() -> list[tuple[str, int, str]]:
    out: list[tuple[str, int, str]] = []
    if not TEMPLATES.is_dir():
        return out
    for path in sorted(TEMPLATES.rglob("*")):
        if path.is_file():
            for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if line.strip():
                    out.append((path.name, i, line))
    return out


_EVERYTHING, _PHONE = spoken_strings()
TEMPLATE = template_strings()
ALL = _EVERYTHING + TEMPLATE
PHONE = _PHONE + TEMPLATE


def test_the_sweep_actually_found_the_strings() -> None:
    """If this collapses to nothing the assertions below are all vacuously true."""
    assert len(ALL) > 150, len(ALL)
    assert len(PHONE) > 100, len(PHONE)
    joined = " ".join(t for _f, _l, t in PHONE)
    assert "Where are you in your cycle?" in joined, "a known message must be in the sweep"
    assert "<b>" in joined, "the markup rules would be vacuous without any markup"


def test_nothing_outside_the_phone_surface_speaks_to_a_person() -> None:
    """The HTML rules only run over PHONE. A new module that sends messages without being
    listed there would be exempt from them silently - so make adding one fail here."""
    speaking = {
        p.name
        for p in SRC.rglob("*.py")
        if not _speaks_to_a_phone(p)
        and re.search(r"outbox\.(reply|send|alert)\(", p.read_text(encoding="utf-8"))
    }
    assert not speaking, f"sends messages but is outside PHONE_DIRS/PHONE_FILES: {sorted(speaking)}"


def test_every_exception_a_person_is_shown_is_declared() -> None:
    """`reply(esc(str(e)))` puts an exception's own words on a phone. The module that writes
    those words has to be swept too, or its wording is exempt from every rule in this file."""
    import ast as _ast

    caught: set[str] = set()
    for path in SRC.rglob("*.py"):
        if not _speaks_to_a_phone(path):
            continue
        source = path.read_text(encoding="utf-8")
        tree = _ast.parse(source)
        for node in _ast.walk(tree):
            if not isinstance(node, _ast.ExceptHandler) or node.type is None:
                continue
            body = _ast.get_source_segment(source, node) or ""
            if "str(e)" in body and re.search(r"\b(reply|send)\(", body):
                caught |= {n.id for n in _ast.walk(node.type) if isinstance(n, _ast.Name)}
    assert caught == set(ECHOED_TO_A_PERSON), (
        f"undeclared exception shown to a person: {sorted(caught - set(ECHOED_TO_A_PERSON))}; "
        f"declared but no longer shown: {sorted(set(ECHOED_TO_A_PERSON) - caught)}"
    )
    # the point is that the module is swept, and a module inside a phone directory already is:
    # asking for PHONE_FILES membership held only while every declared file sat outside them
    swept = {p.name for p in SRC.rglob("*.py") if _speaks_to_a_phone(p)}
    for names in ECHOED_TO_A_PERSON.values():
        for name in names:
            assert name in swept, f"{name} writes messages but is not swept"


def test_no_message_leaks_internal_vocabulary() -> None:
    """PHONE, not ALL: an operator reading a stack trace in a terminal wants to be told the
    reply was not JSON. A person confirming dinner has never heard the word."""
    problems = [
        f"{f}:{line}: {word!r} in {text[:70]!r}"
        for f, line, text in PHONE
        for word in JARGON
        if re.search(rf"\b{re.escape(word)}\b", text.lower())
    ]
    assert not problems, "\n".join(problems)


def test_every_command_a_message_names_actually_exists() -> None:
    """A message that sends someone to a command that does not exist is a dead end - the
    shape of a command replaced by an invented settings screen."""
    from tiffin.telegram import handlers as h

    known = {n for _t, items in h.HELP_GROUPS for n, _w in items} | {
        "start",
        "ping",
        "help",
        "onboard",
        "confirm",
        "today",
        "measure",
        "rate",
        "recipe",
        "menu",
        "shopping",
        "shop",
        "cook",
        "kit",
        "plan",
        "week",
        "proposals",
        "pause",
        "resume",
        "name",
        "set",
        "consent",
    }
    problems = []
    for f, line, text in PHONE:
        # a slash after a word character, an angle bracket or another slash is a path or a
        # placeholder (TIFFIN_HOME/dryrun/<date>.html), never a command
        for named in set(re.findall(r"(?<![<>\w/])/([a-z]{2,12})\b", text)):
            if named not in known:
                problems.append(f"{f}:{line}: names /{named}")
    assert not problems, "\n".join(problems)


def test_no_message_sends_anyone_to_a_place_that_does_not_exist() -> None:
    """There is no app, no website they can edit, no settings screen. Everything is the bot."""
    forbidden = (
        r"\bthe app\b",
        r"\bin the app\b",
        r"\bapp's\b",
        r"\bsettings (?:screen|page|menu|tab)\b",
        r"\bthe dashboard\b",
        r"\bthe website\b",
        r"\bthe web ?(?:app|form)\b",
    )
    problems = [
        f"{f}:{line}: {text[:70]!r}"
        for f, line, text in ALL
        for pattern in forbidden
        if re.search(pattern, text.lower())
    ]
    assert not problems, "\n".join(problems)


def test_every_html_tag_is_one_the_renderer_allows() -> None:
    """Anything else arrives on the phone as literal angle brackets, or is rejected outright
    by Telegram's parser - which drops the whole message."""
    problems = []
    for f, line, text in PHONE:
        for tag in re.findall(r"</?([a-zA-Z][a-zA-Z0-9]*)[ >]", text):
            if tag.lower() not in ALLOWED_TAGS:
                problems.append(f"{f}:{line}: <{tag}> in {text[:60]!r}")
    assert not problems, "\n".join(problems)


def test_every_opened_tag_is_closed() -> None:
    """Telegram rejects unbalanced HTML with 400 and the message is never delivered."""
    problems = []
    for f, line, text in PHONE:
        for tag in ALLOWED_TAGS:
            opened = len(re.findall(rf"<{tag}[ >]", text))
            closed = len(re.findall(rf"</{tag}>", text))
            if opened != closed:
                problems.append(f"{f}:{line}: {opened} <{tag}> vs {closed} </{tag}>")
    assert not problems, "\n".join(problems)


def test_no_message_uses_an_emoji() -> None:
    """The house rule, and the renderer strips them - but a stripped emoji leaves a gap in a
    sentence that was written around it."""
    problems = [
        f"{f}:{line}: {text[:60]!r}"
        for f, line, text in ALL
        if any(ord(ch) > 0x2100 for ch in text)
    ]
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize(
    "word", ["good food", "bad food", "cheat", "guilty", "naughty", "treat yourself"]
)
def test_no_message_moralises_about_food(word: str) -> None:
    """Food is on plan or off plan, never good or bad."""
    problems = [f"{f}:{line}: {text[:70]!r}" for f, line, text in ALL if word in text.lower()]
    assert not problems, "\n".join(problems)


def test_the_rules_would_catch_a_violation() -> None:
    """A rule whose pattern quietly stops matching passes forever and proves nothing. These
    are the exact shapes this file exists to stop, checked against the rules themselves."""
    assert re.search(r"\bperson_id\b", "no row for person_id 3".lower())
    assert re.findall(r"(?<![<>\w/])/([a-z]{2,12})\b", "Send /nosuchcmd to fix it") == ["nosuchcmd"]
    assert re.search(r"\bapp's\b", "change it in the app's settings")
    assert re.search(r"\bsettings (?:screen|page|menu|tab)\b", "open the settings screen")
    assert [t for t in re.findall(r"</?([a-zA-Z][a-zA-Z0-9]*)[ >]", "<h1>hi</h1>")] == ["h1", "h1"]
    assert len(re.findall(r"<b[ >]", "<b>hi")) != len(re.findall(r"</b>", "<b>hi"))
    assert any(ord(ch) > 0x2100 for ch in "nice work \U0001f44d")
    # and the collector still reads an f-string as one balanced string
    stmt = ast.parse('f"<code>/cook {x}</code>"').body[0]
    assert isinstance(stmt, ast.Expr)
    assert _flat(stmt.value) == "<code>/cook {}</code>"
    # while the machinery it must ignore stays out
    assert _is_sql("pairing_code_id IN (SELECT id FROM pairing_codes WHERE person_id = ?)")
    assert not _is_sql("Send /supp add creatine 5 g am to put it back on the stack.")
