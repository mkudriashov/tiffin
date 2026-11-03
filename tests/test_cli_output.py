"""What the commands print: plain and unchanged for scripts, marked and aligned for a person.

Scripts read three things, and these tests pin each of them:

- `tiffin status` with no terminal (a monitor runs `docker exec <container> tiffin status` and
  reads `heartbeat_age_s`, `persons[].has_chat_id`/`erased`, `messages_out_24h` and
  `missed_slots[].job/slot/person_id/state/due` from the JSON). `--json` prints the same JSON on
  a terminal too.
- `tiffin doctor` with no terminal: one `{pass|warn|fail:<4} {name:<16} {detail}` line per
  check and `doctor: ok|FAIL`, read by splitting each line on whitespace; and its exit codes.
- `tiffin version`: the version and nothing else.

A person at a terminal gets marks, headings and colour, and NO_COLOR turns the colour off.
"""

from __future__ import annotations

import io
import json
import re
from pathlib import Path

import pytest

from tiffin import __version__, cli, doctor, term
from tiffin.app import App
from tiffin.clock import FrozenClock
from tiffin.db import Db
from tiffin.term import Style

STATUS_KEYS = [
    "env",
    "version",
    "db",
    "db_exists",
    "heartbeat_age_s",
    "pid",
    "persons",
    "last_runs",
    "missed_slots",
    "messages_out_24h",
]
PLAIN_LINE = re.compile(r"^(pass|warn|fail) (\S.*?) {1,}(.*)$")
UNICODE = Style(terminal=True, colour=False, unicode=True)


@pytest.fixture
def root(tmp_path: Path, frozen_clock: FrozenClock, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A dev root with both people and a run in the ledger, wired as `tiffin <cmd>` finds it."""
    root = tmp_path / "root"
    monkeypatch.setenv("TIFFIN_HOME", str(root))
    monkeypatch.setenv("TIFFIN_ENV", "dev")
    app = App.from_env(clock=frozen_clock)
    app.config.paths.ensure()
    with Db.open(app.config.paths.db, clock=frozen_clock) as db:
        owner = db.insert("persons", slug="owner", display_name="Alex", tone="blunt")
        db.insert("persons", slug="partner", display_name="Sam", tone="warm")
        db.insert("telegram_identities", person_id=owner, chat_id=1000001, method="config")
        db.insert(
            "run_ledger",
            run_id="run-1",
            job="icu_sync",
            slot="hourly",
            person_id=owner,
            status="degraded",
            detail="intervals.icu not connected",
            started_at="2026-09-07 19:00:00",
        )
    (app.config.paths.secrets / "telegram-token-dev.txt").write_text("1:t", encoding="utf-8")
    return root


def a_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every stream counts as a terminal, as stdout does when a person runs the command."""
    monkeypatch.setattr(term, "is_terminal", lambda stream: True)
    monkeypatch.setattr(term, "_windows_escapes", lambda stream: True)


# -------------------------------------------------------------------------------- status
def test_status_without_a_terminal_is_the_json_monitors_read(root, capsys) -> None:
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert list(data) == STATUS_KEYS
    assert out.startswith('{\n "env": "dev",\n "version": "'), "indent=1, keys in this order"
    assert data["persons"] == [
        {"slug": "owner", "has_chat_id": True, "erased": False},
        {"slug": "partner", "has_chat_id": False, "erased": False},
    ]
    assert data["last_runs"][0] == {
        "job": "icu_sync",
        "slot": "hourly",
        "person_id": 1,
        "status": "degraded",
        "detail": "intervals.icu not connected",
    }
    assert isinstance(data["missed_slots"], list)
    assert data["messages_out_24h"] == {}
    assert data["heartbeat_age_s"] is None and data["pid"] is None


def test_status_json_flag_prints_the_same_json_on_a_terminal(root, capsys, monkeypatch) -> None:
    assert cli.main(["status"]) == 0
    piped = capsys.readouterr().out
    a_terminal(monkeypatch)
    assert cli.main(["status", "--json"]) == 0
    assert capsys.readouterr().out == piped


def test_status_on_a_terminal_is_a_summary_in_words(root, capsys, monkeypatch) -> None:
    a_terminal(monkeypatch)
    monkeypatch.setenv("NO_COLOR", "1")
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert not out.lstrip().startswith("{")
    assert "Tiffin status" in out
    assert "! not running; start it with tiffin run" in out
    assert re.search(r"owner +paired", out) and re.search(r"partner +not paired", out)
    assert re.search(r"!\s+icu_sync\s+owner\s+degraded\s+intervals\.icu not connected", out)
    assert "For scripts: tiffin status --json" in out
    assert "\x1b[" not in out, "NO_COLOR"
    assert "Alex" not in out and "1000001" not in out, "no names and no chats"


def test_status_with_no_database_yet_still_answers(tmp_path, capsys, monkeypatch) -> None:
    monkeypatch.setenv("TIFFIN_HOME", str(tmp_path / "empty"))
    assert cli.main(["status"]) == 0
    assert json.loads(capsys.readouterr().out)["db_exists"] is False
    a_terminal(monkeypatch)
    assert cli.main(["status"]) == 0
    assert "no database yet" in capsys.readouterr().out


# -------------------------------------------------------------------------------- doctor
CHECKS = [
    doctor.Check("config", "ok", "tiffin.toml and packaged defaults are valid"),
    doctor.Check("Telegram token", "red", "set TIFFIN_TELEGRAM_TOKEN"),
    doctor.Check("expected messages", "warn", "1 missed"),
    doctor.Check("backup", "warn", "no backup yet"),
]


def test_doctor_plain_text_is_unchanged() -> None:
    out = io.StringIO()
    assert doctor.report(CHECKS, out=out, style=Style()) == 1
    assert out.getvalue() == (
        "pass config           tiffin.toml and packaged defaults are valid\n"
        "fail Telegram token   set TIFFIN_TELEGRAM_TOKEN\n"
        "warn expected messages 1 missed\n"
        "warn backup           no backup yet\n"
        "doctor: FAIL\n"
    )


@pytest.mark.parametrize(
    ("statuses", "strict", "code"),
    [
        (["ok"], False, 0),
        (["ok", "warn"], False, 0),
        (["ok", "warn"], True, 1),
        (["ok", "red"], False, 1),
    ],
)
def test_doctor_exit_codes_are_the_same_in_every_format(statuses, strict, code) -> None:
    checks = [doctor.Check(f"check {n}", s, "x") for n, s in enumerate(statuses)]
    for style, as_json in ((Style(), False), (UNICODE, False), (Style(), True)):
        out = io.StringIO()
        assert doctor.report(checks, strict=strict, as_json=as_json, out=out, style=style) == code
    plain = io.StringIO()
    doctor.report(checks, strict=strict, out=plain, style=Style())
    assert plain.getvalue().splitlines()[-1] == ("doctor: FAIL" if code else "doctor: ok")


def test_doctor_from_the_command_line_keeps_its_plain_lines(root, capsys) -> None:
    code = cli.main(["doctor", "--offline"])
    lines = capsys.readouterr().out.splitlines()
    failed = any(line.startswith("fail ") for line in lines)
    assert code == int(failed)
    assert lines[-1] == ("doctor: FAIL" if failed else "doctor: ok")
    app = App.from_env()
    expected = {
        f"{doctor.LABELS[c.status]:<4} {c.name:<16} {c.detail}"
        for c in doctor.run_checks(app, online=False)
        if c.name != "disk"
    }
    printed = {line for line in lines[:-1] if line.split()[1] != "disk"}
    assert printed == expected
    for line in lines[:-1]:
        # how the cut-over import tool reads it: status word, then the name
        bits = line.split(None, 2)
        assert bits[0] in {"pass", "warn", "fail"} and PLAIN_LINE.match(line), line


def test_doctor_json_lists_every_check(root, capsys) -> None:
    code = cli.main(["doctor", "--offline", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert code == int(not data["ok"]) and data["strict"] is False
    assert {c["status"] for c in data["checks"]} <= {"pass", "warn", "fail"}
    assert {"config", "home", "Telegram token", "database"} <= {c["name"] for c in data["checks"]}


def test_doctor_with_a_broken_config_still_prints_a_check_line(tmp_path, capsys, monkeypatch):
    home = tmp_path / "broken"
    home.mkdir()
    (home / "tiffin.toml").write_text("[web]\nenabled = 'perhaps'\n", encoding="utf-8")
    monkeypatch.setenv("TIFFIN_HOME", str(home))
    assert cli.main(["doctor"]) == 1
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("fail config           web.enabled must be true or false")
    assert lines[0].endswith("; fix tiffin.toml or run tiffin setup")
    assert lines[-1] == "doctor: FAIL"
    assert cli.main(["doctor", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["checks"][0]["status"] == "fail"


def test_doctor_on_a_terminal_is_grouped_marked_and_aligned() -> None:
    checks = [
        *CHECKS,
        doctor.Check("menu_propose", "warn", doctor.NEVER_RUN),
        doctor.Check("icu_sync", "warn", doctor.NEVER_RUN),
    ]
    lines = doctor.terminal_lines(checks, UNICODE, context="home · production", width=60)
    text = "\n".join(lines)
    assert lines[:2] == ["Tiffin health check", "home · production"]
    assert {"Set-up", "Running", "Scheduled jobs"} <= set(lines)
    rows = [line for line in lines if line.startswith("  ")]
    column = rows[0].index("tiffin.toml")
    assert all(len(row) <= 60 for row in rows)
    assert rows[1].strip() == "are valid" and rows[1].index("are") == column, "wrapped under"
    assert rows[2].index("set TIFFIN_TELEGRAM_TOKEN") == column, "one detail column"
    assert [row[2] for row in rows if row[2] != " "] == ["✓", "✗", "!", "!", "!"]
    assert "2 scheduled jobs; they start with tiffin run" in " ".join(text.split())
    assert "menu_propose" not in text, "jobs that have never run are one line"
    assert lines[-1] == "✗ 1 check failed, 4 to look at, 1 fine."
    assert "\x1b[" not in text


def test_doctor_on_a_terminal_never_breaks_a_path_in_two() -> None:
    path = "/srv/a-long-folder/with-hyphens/tiffin-home/secrets/telegram-token.txt"
    check = doctor.Check("Telegram token", "red", f"set TIFFIN_TELEGRAM_TOKEN or create {path}")
    lines = doctor.check_lines([check], UNICODE, width=50)
    assert any(line.strip() == path for line in lines), lines


def test_doctor_verdicts_say_how_it_went() -> None:
    fine = [doctor.Check("config", "ok", "x"), doctor.Check("disk", "ok", "y")]
    assert doctor.terminal_lines(fine, UNICODE)[-1] == "✓ All 2 checks fine."
    look = [*fine, doctor.Check("backup", "warn", "z")]
    assert doctor.terminal_lines(look, UNICODE)[-1] == "! 2 fine, 1 to look at."
    strict = doctor.terminal_lines(look, UNICODE, strict=True)[-1]
    assert strict.startswith("✗ 1 check failed, 2 fine.")


# -------------------------------------------------------------------------------- terminals
class FakeStream(io.StringIO):
    def __init__(self, tty: bool, encoding: str = "utf-8") -> None:
        super().__init__()
        self._tty = tty
        self._encoding = encoding

    def isatty(self) -> bool:
        return self._tty

    @property
    def encoding(self) -> str:  # type: ignore[override]
        return self._encoding


@pytest.fixture
def escapes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(term, "_windows_escapes", lambda stream: True)


def test_colour_and_marks_only_on_a_terminal(escapes) -> None:
    piped = Style.detect(FakeStream(tty=False), {})
    assert (piped.terminal, piped.colour, piped.unicode) == (False, False, False)
    assert piped.mark("ok") == "+" and piped.paint("x", "red") == "x"
    tty = Style.detect(FakeStream(tty=True), {})
    assert (tty.terminal, tty.colour, tty.unicode) == (True, True, True)
    assert tty.mark("ok") == "\x1b[32m✓\x1b[0m"


def test_no_color_turns_colour_off_but_keeps_the_marks(escapes) -> None:
    style = Style.detect(FakeStream(tty=True), {"NO_COLOR": "1"})
    assert style.colour is False and style.unicode is True
    assert style.mark("fail") == "✗"
    assert Style.detect(FakeStream(tty=True), {"NO_COLOR": ""}).colour is True, "empty: unset"


def test_a_dumb_or_ascii_terminal_gets_plain_marks(escapes) -> None:
    dumb = Style.detect(FakeStream(tty=True), {"TERM": "dumb"})
    assert dumb.colour is False and dumb.unicode is False
    ascii_only = Style.detect(FakeStream(tty=True, encoding="ascii"), {})
    assert ascii_only.unicode is False
    assert [ascii_only.mark(s) for s in ("ok", "warn", "fail", "info")] == [
        "\x1b[32m+\x1b[0m",
        "\x1b[33m!\x1b[0m",
        "\x1b[31mx\x1b[0m",
        "\x1b[2m-\x1b[0m",
    ]


def test_masks_never_show_more_than_the_last_four_characters() -> None:
    assert UNICODE.mask("123456:" + "A" * 31 + "wxyz") == "••••wxyz"
    assert Style().mask("short-one") == "****", "a short secret shows nothing at all"


def test_version_prints_the_version_and_nothing_else(capsys) -> None:
    assert cli.main(["version"]) == 0
    assert capsys.readouterr().out == __version__ + "\n"


def test_errors_keep_their_words_for_scripts(tmp_path, capsys, monkeypatch) -> None:
    monkeypatch.setenv("TIFFIN_HOME", str(tmp_path))
    (tmp_path / "tiffin.toml").write_text("[env]\ntz = 'Nowhere/Atlantis'\n", encoding="utf-8")
    assert cli.main(["status"]) == 2
    assert capsys.readouterr().err.startswith("config error: env.tz must be a known IANA")
