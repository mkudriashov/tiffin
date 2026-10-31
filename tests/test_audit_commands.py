"""Audits over the command surface, so a whole class of bug cannot ship twice.

Four bugs found by hand this week were the same bug: `/rate 4` dropped the four, `/recipe
Porridge` answered with the chilli, `/menu tomorrow` silently showed today, `/set training week`
answered "I do not know a setting called training". Each is a command that ACCEPTS an argument in
its signature, never reads it, and answers as though nothing had been typed - which is invisible
from the outside, because the answer looks like a perfectly good answer.

Reading source is how you find that. A test that calls `/rate` and asserts the buttons come back
passes just as happily on the broken version, which is why 1229 green tests did not see any of
these. These audits walk the AST instead and ask structural questions no behaviour test can.

The rule they follow: **be precise or be deleted.** An audit that cries wolf gets an ignore-list
bolted on within a month and then means nothing. Each one here narrows until every hit is a real
defect, and the narrowing is written down so the next person can judge whether it still holds.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "tiffin"
TELEGRAM = SRC / "telegram"


def _module(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _functions(path: Path) -> list[ast.FunctionDef]:
    return [n for n in ast.walk(_module(path)) if isinstance(n, ast.FunctionDef)]


def _text_of(node: ast.AST) -> str:
    """A string literal or f-string as one string, with `{...}` kept as a placeholder.

    Flattening an f-string to its literal halves is how an audit convinces itself that
    "No food called {name}. /shop lists them." names no command: the "/shop" survives, but the
    sentence has been cut in two and the halves are checked separately.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        out = []
        for part in node.values:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                out.append(part.value)
            else:
                out.append("{}")
        return "".join(out)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _text_of(node.left) + _text_of(node.right)
    return ""


def _spoken(fn: ast.FunctionDef) -> list[str]:
    """Every string this function actually puts in front of a person.

    Only what is passed to `reply`, `send` or `alert` - not every literal in the body. The wide
    version flagged exception text, template names and SQL, which is exactly the noise that gets
    an audit switched off.
    """
    out: list[str] = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in ("reply", "send", "alert"):
            continue
        for arg in [*node.args, *(kw.value for kw in node.keywords)]:
            said = _text_of(arg)
            if said:
                out.append(said)
    return out


# ---------------------------------------------------------------- 1. arguments that go nowhere
#: handlers that genuinely take no argument. Each entry is a decision that somebody made on
#: purpose, and the list has to stay short enough to read.
ARGLESS_ON_PURPOSE = {
    "cmd_today": "the day it is; there is nothing to qualify",
    "cmd_week": "this week, and a past week cannot be changed",
    "cmd_consent": "a yes or no held in buttons, never typed",
    "cmd_proposals": "everything open, which is the whole point of it",
    "cmd_ping": "a liveness check",
    "cmd_start": "the first message anybody sends",
}


def test_a_command_that_takes_an_argument_reads_it() -> None:
    """`/rate 4`, `/recipe chilli`, `/menu tomorrow`: typed, accepted, and thrown away.

    The signature is the promise. A handler declaring `arg` and never mentioning it answers the
    bare form to somebody who typed something - and the reply looks right, so nobody notices
    until they wonder why the number they sent did nothing. Four of these shipped at once.
    """
    deaf = []
    for path in sorted(TELEGRAM.rglob("*.py")):
        for fn in _functions(path):
            if not fn.name.startswith("cmd_") or fn.name in ARGLESS_ON_PURPOSE:
                continue
            if "arg" not in [a.arg for a in fn.args.args]:
                continue
            body = ast.Module(body=fn.body, type_ignores=[])
            if "arg" not in {n.id for n in ast.walk(body) if isinstance(n, ast.Name)}:
                deaf.append(
                    f"{path.name}:{fn.lineno} {fn.name} takes `arg` and never reads it. "
                    "Read it, or add it to ARGLESS_ON_PURPOSE with the reason."
                )
    assert not deaf, "\n".join(deaf)


# ---------------------------------------------------------------- 2. a tap with nothing to tap
#: "Tap a meal for its recipe" on a message with no keyboard was a `/menu` and `/batch`
#: bug. But "tap the other button on that message if that was
#: wrong" is CORRECT and points at a different message, so the pattern has to mean *this* one.
_TAP_HERE = re.compile(r"\btap (?:a|an|one|the button|below|it)\b|\bbuttons? below\b", re.I)
_ELSEWHERE = re.compile(r"\bthat message\b|\babove\b|\bearlier\b|\bon that\b", re.I)


def test_a_message_that_names_a_tap_ships_a_keyboard() -> None:
    ships = ("keyboard", "inline_keyboard", "reply_markup", "meal_buttons", "rows")
    lying = []
    for path in sorted(TELEGRAM.rglob("*.py")):
        for fn in _functions(path):
            said = [s for s in _spoken(fn) if _TAP_HERE.search(s) and not _ELSEWHERE.search(s)]
            if not said:
                continue
            source = ast.unparse(fn)
            if any(token in source for token in ships):
                continue
            lying.append(f"{path.name}:{fn.lineno} {fn.name}: {said[0][:60]!r} with no keyboard")
    assert not lying, "\n".join(lying)


# ---------------------------------------------------------------- 3. buttons nothing answers
def test_every_button_a_message_builds_has_a_handler() -> None:
    """A callback prefix nothing routes is a button that does nothing when tapped.

    The router answers an unknown prefix with "That button is not wired yet", which is honest
    and is still a person tapping a thing that cannot work. `register_callback` is the register;
    a `k:<prefix>` built anywhere and missing from it is a dead button.
    """
    registered = set()
    built: dict[str, str] = {}
    for path in sorted(SRC.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        registered |= set(re.findall(r'register_callback\(\s*"([a-z]+)"', text))
        for m in re.finditer(r"k:([a-z]+):", text):
            built.setdefault(m.group(1), path.name)
    missing = sorted(
        f"k:{p}: built in {where}" for p, where in built.items() if p not in registered
    )
    assert not missing, "buttons with no handler:\n" + "\n".join(missing)
    assert len(registered) > 15, f"the register looks unread: {sorted(registered)}"


# ---------------------------------------------------------------- 4. a refusal that names nothing
#: a short "no" with no next step. "Nothing to rate today." and "No menu to cook from yet." were
#: both true and both terminal, and the person hearing them is exactly the one who needs the
#: next step. Long answers are allowed to explain instead: the rule is about dead ends.
_REFUSAL = re.compile(r"^(no |nothing |i do not know|i did not|there is no)", re.I)
#: a command, a tap, or a list of what DOES exist ("You take: {}") - the last one is a real way
#: forward, because it names the words that would have worked instead.
_A_WAY_ON = re.compile(r"/[a-z]{3,12}\b|\btap\b|<code>|:\s*\{\}")


def test_a_refusal_offers_something() -> None:
    silent = []
    for path in sorted(TELEGRAM.rglob("*.py")):
        for fn in _functions(path):
            for said in _spoken(fn):
                text = said.strip()
                if not _REFUSAL.match(text) or len(text) > 200 or _A_WAY_ON.search(text):
                    continue
                silent.append(f"{path.name}:{fn.lineno} {fn.name}: {text[:70]!r}")
    assert not silent, "a refusal with no way forward:\n" + "\n".join(sorted(set(silent)))
