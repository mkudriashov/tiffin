"""Every operator command, enumerated from the parser rather than listed by hand.

This is the surface the owner uses when something has gone wrong: an empty database, a person who
has not paired, a bot token that is not there. A command that raises a traceback at that
moment is worse than one that refuses, because a traceback tells you nothing about what to do
next and looks like Tiffin itself is broken.

So: every subcommand `build_parser` declares, driven with no arguments and with junk, on a
root that has just been created. The bar is the CLI's own contract - `main` returns 0, 1 or 2,
argparse exits 2 on a usage error, and neither one is allowed to be an unhandled exception.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from tiffin import cli
from tiffin.app import App
from tiffin.clock import FrozenClock
from tiffin.db import Db

from .test_cli import _own_secrets

#: `run` and `demo` serve until stopped; `doctor --offline` still reaches the model gateway
#: and the disk. Their own tests cover them, so they cannot be driven blind here.
NOT_DRIVEN = {"run", "demo", "doctor"}
#: what a person fat-fingers into a terminal
JUNK = ["", "--nope", "owner --days x", "'; DROP TABLE persons; --"]


def subcommands() -> list[str]:
    """Read them off the parser, so a new one is audited the day it is added."""
    parser = cli.build_parser()
    names: list[str] = []
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            names += sorted(action.choices)
    return names


@pytest.fixture
def root(tmp_path: Path, frozen_clock: FrozenClock, monkeypatch: pytest.MonkeyPatch):
    """A dev root with both people and nothing else - the state a new box is in."""
    # dev bot's token on every run (test_cli._own_secrets)
    _own_secrets(tmp_path / "root", tmp_path)
    monkeypatch.setenv("TIFFIN_HOME", str(tmp_path / "root"))
    monkeypatch.setenv("TIFFIN_ENV", "dev")
    app = App.from_env(clock=frozen_clock)
    app.config.paths.ensure()
    db = Db.open(app.config.paths.db, clock=frozen_clock)
    db.insert("persons", slug="owner", display_name="Alex", tone="blunt")
    db.insert("persons", slug="partner", display_name="Sam", tone="warm")
    db.close()
    app.config.paths.secrets.mkdir(parents=True, exist_ok=True)
    (app.config.paths.secrets / "telegram-token-dev.txt").write_text("1:t", encoding="utf-8")
    return app.config


def run(argv: list[str]) -> int:
    """`main`, or the code argparse exited with. Anything else is the failure being tested."""
    try:
        return cli.main(argv)
    except SystemExit as e:  # argparse's own usage exit
        return int(e.code or 0)


def test_the_sweep_sees_every_subcommand() -> None:
    found = subcommands()
    assert {"run", "status", "doctor", "pair", "dry-run", "version"} <= set(found), found
    assert len(found) >= 9, found


def test_every_subcommand_has_a_branch_in_main() -> None:
    """A parser entry with no `if args.cmd ==` falls off the end of `main` and returns 0
    having done nothing at all, which reads exactly like success."""
    source = Path(cli.__file__).read_text(encoding="utf-8")
    missing = [name for name in subcommands() if f'args.cmd == "{name}"' not in source]
    assert not missing, f"parsed but never dispatched: {missing}"


@pytest.mark.parametrize("name", [n for n in subcommands() if n not in NOT_DRIVEN])
def test_every_subcommand_answers_its_own_help(name: str) -> None:
    """`--help` is what somebody types when they are stuck. It has to work for all of them."""
    assert run([name, "--help"]) == 0


@pytest.mark.parametrize("name", [n for n in subcommands() if n not in NOT_DRIVEN])
def test_no_subcommand_raises_on_a_fresh_root(root, name: str) -> None:
    """Missing arguments, an empty database, nothing set up. Refusing is fine; raising is not."""
    assert run([name]) in (0, 1, 2)


@pytest.mark.parametrize("name", [n for n in subcommands() if n not in NOT_DRIVEN])
@pytest.mark.parametrize("junk", JUNK)
def test_no_subcommand_raises_on_junk(root, name: str, junk: str) -> None:
    assert run([name, *junk.split()]) in (0, 1, 2)


def test_an_unknown_subcommand_is_a_usage_error_not_a_crash() -> None:
    assert run(["does-not-exist"]) == 2


def test_version_prints_something(capsys) -> None:
    assert run(["version"]) == 0
    assert capsys.readouterr().out.strip()


def test_status_reads_a_root_that_has_never_run(root, capsys) -> None:
    """The first thing anybody types when they think it is down."""
    import json

    assert run(["status"]) in (0, 1)
    out = capsys.readouterr().out
    assert json.loads(out), "status is JSON, and it says something even when nothing has run"
