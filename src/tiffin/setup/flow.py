"""Walking the steps, the list of parts on a re-run, and the review before anything is saved."""

from __future__ import annotations

import argparse
import shutil
from collections.abc import Sequence
from pathlib import Path

from ..clock import Clock
from ..config import edit_override
from ..secrets import Secrets
from ..term import plural
from .ask import Back, Prompter, Quit, Redo, Skip
from .state import State
from .steps import BY_KEY, REQUIRED, SECTION_LIST, SECTION_NAMES, Section


class ToMenu(Exception):
    """Back from the first step of a re-run: show the list of parts again."""


def _flags_given(args: argparse.Namespace, section: Section) -> bool:
    return any(getattr(args, flag, None) not in (None, "", False) for flag in section.flags)


def _walk(s: State, p: Prompter, plan: Sequence[str], *, menu: bool) -> None:
    total = len(plan) + 1
    index = 0
    while index < len(plan):
        section = BY_KEY[plan[index]]
        p.say("")
        p.say(p.style.step(index + 1, total, section.title))
        before = s.snapshot()
        try:
            section.run(s, p)
        except Back:
            s.restore(before)
            if index == 0:
                if menu:
                    raise ToMenu from None
                p.result("info", "This is the first step.")
                continue
            index -= 1
            continue
        except Skip:
            s.restore(before)
            if s.first_run and section.key in REQUIRED:
                p.result("warn", "Tiffin needs this step, so it cannot be skipped.")
                continue
            if s.first_run:
                for key, value in section.on_first_skip.items():
                    s.set(key, value)
            p.result("info", section.skipped if s.first_run else "Skipped: left as it was.")
        except Redo:
            s.restore(before)
            continue
        else:
            s.ran.add(section.key)
        index += 1


def _say_rows(p: Prompter, rows: Sequence[Sequence[str]]) -> None:
    """Aligned rows whose last column wraps under itself."""
    widths = [max(len(r[c]) for r in rows) for c in range(len(rows[0]) - 1)] if rows else []
    lead = 2 + sum(w + 2 for w in widths)
    for row in rows:
        pieces = p.wrap(row[-1], max(30, p.width - lead))
        cells = [cell.ljust(width) for cell, width in zip(row[:-1], widths, strict=False)]
        p.say(("  " + "  ".join([*cells, pieces[0]])).rstrip())
        for piece in pieces[1:]:
            p.say(" " * lead + piece)


def _menu(s: State, p: Prompter) -> list[str]:
    p.say(f"Settings found in {s.home}")
    p.say("")
    rows = [
        [str(n), section.title, section.show(s, False)] for n, section in enumerate(SECTION_LIST, 1)
    ]
    _say_rows(p, rows)
    p.say("")
    while True:
        try:
            answer = p._input(
                f"Choose a part to change (1-{len(rows)}), a for all of them, c to check "
                "everything, or q to quit [q]: "
            ).lower()
        except Quit:
            answer = "q"
        if answer in ("", "q"):
            raise Quit
        if answer == "a":
            return list(SECTION_NAMES)
        if answer == "c":
            return []
        if answer.isdigit() and 1 <= int(answer) <= len(rows):
            return [SECTION_LIST[int(answer) - 1].key]
        if answer in BY_KEY or answer == "intervals":
            return [BY_KEY.get(answer, BY_KEY["training"]).key]
        p.result("fail", f"Type a number from 1 to {len(rows)}, a, c or q.")


def _review(s: State, p: Prompter) -> bool:
    """Show what will be saved; True when there is anything to save."""
    if s.first_run:
        rows = [[section.title, section.show(s, True)] for section in SECTION_LIST]
        p.say("Here is what will be saved:")
    else:
        rows = []
        for section in SECTION_LIST:
            before, after = section.show(s, False), section.show(s, True)
            if before != after:
                rows += [[section.title, after], ["", "was: " + before]]
        new_secrets = [n for n, v in s.secrets.items() if v != s.saved_secret(n)]
        if not rows and not s.names and not new_secrets:
            p.result("ok", "Nothing has changed, so there is nothing to save.")
            return False
        if not rows:
            rows = [["Keys", plural(len(new_secrets), "new secret") + ", same last characters"]]
        p.say("Here is what will change:")
    _say_rows(p, rows)
    p.say("")
    p.explain(f"Settings go in {s.toml_path}.")
    if any(v != s.saved_secret(n) for n, v in s.secrets.items()):
        p.explain(f"Tokens and keys go in {s.home / 'secrets'}, where only you can read them.")
    return True


def _backup(s: State) -> Path | None:
    if not s.toml_path.is_file():
        return None
    folder = s.home / "state" / "setup"
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    clock = s.deps.clock or Clock()
    stamp = clock.now_utc().strftime("%Y%m%d-%H%M%S")
    target = folder / f"tiffin.toml.{stamp}"
    number = 1
    while target.exists():  # two saves in one second keep both copies
        number += 1
        target = folder / f"tiffin.toml.{stamp}-{number}"
    shutil.copy2(s.toml_path, target)
    return target


def _write(s: State) -> Path | None:
    """Write the answers: tiffin.toml line by line, secrets as owner-only files, names as new
    rows. Everything is checked together first, so a bad answer writes nothing."""
    s.config()
    s.home.mkdir(mode=0o700, parents=True, exist_ok=True)
    backup = _backup(s) if s.overlay else None
    if s.overlay or s.first_run:
        edit_override(s.toml_path, s.overlay)
    store = Secrets(s.home / "secrets")
    for name, value in s.secrets.items():
        if value != s.saved_secret(name):
            store.write(name, value)
    app = s.app()
    db = app.bootstrap()
    try:
        registry = app.registry(db)
        for slug, name in s.names.items():
            if registry.require(slug).display_name != name:
                registry.set_display_name(slug, name)
    finally:
        db.close()
    return backup


def _save(s: State, p: Prompter, number: int) -> bool:
    p.say("")
    p.say(p.style.step(number, number, "Save"))
    if not _review(s, p):
        return False
    if not p.confirm("Save these settings", True):
        raise Quit
    backup = _write(s)
    p.result("ok", "Saved.")
    if backup is not None:
        p.result("info", f"The previous tiffin.toml is kept as {backup}.")
    s.first_run = False
    return True
