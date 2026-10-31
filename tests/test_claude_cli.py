"""ClaudeBackend: argv per the plan, envelope -> exception mapping, mcp-config; no subprocess."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tiffin.config import Budget
from tiffin.errors import ModelError, ModelInvalid, QuotaExhausted
from tiffin.model import schemas
from tiffin.model.claude_cli import (
    ClaudeBackend,
    child_env,
    extract_json,
    flatten,
    parse_envelope,
    resolve_exe,
)
from tiffin.model.gateway import BudgetExceeded, ModelTimeout

from .fakes import FakeClaudeCli, claude_envelope

BUDGET = Budget(90, 1, 0.10)
LINES = {"lines": ["Calories go up 100 kcal."]}


def make(tmp_path: Path, script: list) -> tuple[ClaudeBackend, FakeClaudeCli]:
    cli = FakeClaudeCli(script)
    backend = ClaudeBackend(
        "claude.exe",
        "opus",
        tmp_path / "state" / "claude-cwd",
        runner=cli,
        state_dir=tmp_path / "state",
        mcp_exe=r"C:\x\tiffin-mcp.exe",
        mcp_env={"TIFFIN_ENV": "dev"},
        env={"PATH": "p", "CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "s", "HOME": "h"},
    )
    return backend, cli


def test_task_model_override_selects_only_that_task(tmp_path: Path) -> None:
    backend = ClaudeBackend("claude.exe", "opus", tmp_path, task_models={"intent": "haiku"})
    intent = backend.base_argv(BUDGET, "intent")
    chat = backend.base_argv(BUDGET, "chat")
    assert intent[intent.index("--model") + 1] == "haiku"
    assert chat[chat.index("--model") + 1] == "opus"


def test_json_task_argv_matches_the_plan_and_prompt_goes_on_stdin(tmp_path: Path) -> None:
    backend, cli = make(tmp_path, [claude_envelope(structured=LINES)])
    schema = schemas.load("sunday_prose")
    r = backend.complete_json("sunday_prose", "the prompt", schema, BUDGET)
    assert r.data == LINES and r.backend == "claude"
    assert r.model_used == "claude-opus-4-1-20250805" and r.cost_usd == 0.0123
    argv = cli.last["argv"]
    assert argv[:3] == ["claude.exe", "-p", "--output-format"]
    for flag, value in [
        ("--output-format", "json"),
        ("--setting-sources", ""),
        ("--permission-prompts", "none"),
        ("--model", "opus"),
        ("--max-budget-usd", "0.100"),
        ("--max-turns", "1"),
        ("--tools", ""),
    ]:
        assert cli.flag_value(flag) == value, flag
    assert "--no-session-persistence" in argv
    sent = json.loads(cli.flag_value("--json-schema") or "")
    assert "$schema" not in sent and sent["properties"] == schema["properties"]
    assert "--mcp-config" not in argv and "--system-prompt" not in argv
    assert cli.last["stdin"] == "the prompt"
    assert cli.last["cwd"] == str(tmp_path / "state" / "claude-cwd")
    assert (tmp_path / "state" / "claude-cwd").is_dir()
    assert cli.last["timeout"] == 90.0


def test_child_env_drops_the_session_variables(tmp_path: Path) -> None:
    backend, cli = make(tmp_path, [claude_envelope(structured=LINES)])
    backend.complete_json("sunday_prose", "p", schemas.load("sunday_prose"), BUDGET)
    assert cli.last["env"] == {"PATH": "p", "HOME": "h"}
    assert "CLAUDECODE" not in child_env({"CLAUDECODE": "1", "X": "y"})


def test_optional_system_prompt_replaces_the_default(tmp_path: Path) -> None:
    backend, cli = make(tmp_path, [claude_envelope(structured=LINES)])
    backend.system_prompt = "persona"
    backend.complete_json("sunday_prose", "p", schemas.load("sunday_prose"), BUDGET)
    assert cli.flag_value("--system-prompt") == "persona"


@pytest.mark.parametrize(
    ("envelope", "exc"),
    [
        (claude_envelope(subtype="error_max_turns", is_error=True), ModelInvalid),
        (claude_envelope(subtype="error_max_budget_usd", is_error=True), BudgetExceeded),
        (
            claude_envelope(subtype="error_max_structured_output_retries", is_error=True),
            ModelInvalid,
        ),
        (
            claude_envelope(result="You've hit your usage limit until 5pm", is_error=True),
            QuotaExhausted,
        ),
        (
            claude_envelope(
                result="Rate limit reached", subtype="error_during_execution", is_error=True
            ),
            QuotaExhausted,
        ),
        (claude_envelope(result="Your limit will reset at 3pm", is_error=True), QuotaExhausted),
        (claude_envelope(result="OAuth session expired", is_error=True), ModelError),
        (claude_envelope(subtype="error_during_execution", is_error=True, result=""), ModelError),
    ],
)
def test_envelope_error_mapping(tmp_path: Path, envelope: dict, exc: type) -> None:
    backend, _ = make(tmp_path, [envelope])
    with pytest.raises(exc):
        backend.complete_json("sunday_prose", "p", schemas.load("sunday_prose"), BUDGET)


def test_budget_exceeded_is_an_invalid_answer_but_not_retried() -> None:
    assert issubclass(BudgetExceeded, ModelInvalid)
    with pytest.raises(BudgetExceeded, match=r"0\.05"):
        parse_envelope(
            json.dumps(claude_envelope(subtype="error_max_budget_usd", is_error=True, cost=0.05)),
            "",
            1,
        )


def test_non_json_output_maps_by_text(tmp_path: Path) -> None:
    backend, _ = make(tmp_path, ["\x1b[31mError: usage limit reached\x1b[0m"])
    with pytest.raises(QuotaExhausted, match="quota exhausted"):
        backend.complete_json("sunday_prose", "p", schemas.load("sunday_prose"), BUDGET)
    backend, _ = make(tmp_path, [(1, "", "Error: --json-schema is not a valid JSON Schema")])
    with pytest.raises(ModelError, match="without a JSON envelope") as exc:
        backend.complete_json("sunday_prose", "p", schemas.load("sunday_prose"), BUDGET)
    assert "valid JSON Schema" not in str(exc.value)


def test_timeout_and_missing_exe(tmp_path: Path) -> None:
    backend, _ = make(tmp_path, [subprocess.TimeoutExpired("claude", 90)])
    with pytest.raises(ModelTimeout, match="90s"):
        backend.complete_json("sunday_prose", "p", schemas.load("sunday_prose"), BUDGET)
    backend, _ = make(tmp_path, [FileNotFoundError("claude.exe")])
    with pytest.raises(ModelError, match="Install Claude Code"):
        backend.complete_json("sunday_prose", "p", schemas.load("sunday_prose"), BUDGET)


def test_structured_output_missing_falls_back_to_the_result_text(tmp_path: Path) -> None:
    backend, _ = make(tmp_path, [claude_envelope(result='Here: {"lines": ["ok"]}')])
    r = backend.complete_json("sunday_prose", "p", schemas.load("sunday_prose"), BUDGET)
    assert r.data == {"lines": ["ok"]}
    backend, _ = make(tmp_path, [claude_envelope(result="no json here")])
    with pytest.raises(ModelInvalid, match="no structured_output"):
        backend.complete_json("sunday_prose", "p", schemas.load("sunday_prose"), BUDGET)
    backend, _ = make(tmp_path, [claude_envelope(structured={"lines": []})])
    with pytest.raises(ModelInvalid, match="schema validation"):
        backend.complete_json("sunday_prose", "p", schemas.load("sunday_prose"), BUDGET)


def test_model_used_prefers_the_model_with_most_output(tmp_path: Path) -> None:
    env = claude_envelope(structured=LINES)
    env["modelUsage"] = {
        "claude-haiku-4-5-20251001": {"outputTokens": 3},
        "claude-opus-5": {"outputTokens": 40},
    }
    backend, _ = make(tmp_path, [env])
    r = backend.complete_json("sunday_prose", "p", schemas.load("sunday_prose"), BUDGET)
    assert r.model_used == "claude-opus-5"
    env = claude_envelope(structured=LINES)
    env["modelUsage"] = {}
    backend, _ = make(tmp_path, [env])
    assert (
        backend.complete_json("x", "p", schemas.load("sunday_prose"), BUDGET).model_used == "opus"
    )


def test_converse_writes_the_mcp_config_and_uses_the_plan_flags(tmp_path: Path) -> None:
    backend, cli = make(tmp_path, [claude_envelope(result="2100 kcal today.", num_turns=3)])
    messages = [
        {"role": "system", "content": "persona and rules"},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
        {"role": "user", "content": "targets?"},
    ]
    r = backend.converse("chat", messages, None, 7, Budget(180, 8, 0.50))
    assert r.text == "2100 kcal today." and r.tool_calls == []
    cfg_path = tmp_path / "state" / "mcp-7.json"
    assert cli.flag_value("--mcp-config") == str(cfg_path)
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    server = cfg["mcpServers"]["tiffin"]
    assert server["command"] == r"C:\x\tiffin-mcp.exe" and server["type"] == "stdio"
    assert server["env"] == {"TIFFIN_ENV": "dev", "TIFFIN_PERSON_ID": "7"}
    argv = cli.last["argv"]
    assert "--strict-mcp-config" in argv and "--no-session-persistence" in argv
    assert cli.flag_value("--allowedTools") == "mcp__tiffin__*"
    assert cli.flag_value("--tools") == "" and cli.flag_value("--max-turns") == "8"
    assert cli.flag_value("--max-budget-usd") == "0.500"
    assert cli.flag_value("--append-system-prompt") == "persona and rules"
    assert "--json-schema" not in argv
    stdin = cli.last["stdin"]
    assert "<user>\nhello\n</user>" in stdin and "<assistant>\nhi\n</assistant>" in stdin
    assert "persona and rules" not in stdin and stdin.endswith("Reply to the last user turn.")


def test_converse_empty_reply_is_invalid(tmp_path: Path) -> None:
    backend, _ = make(tmp_path, [claude_envelope(result="")])
    with pytest.raises(ModelInvalid, match="empty reply"):
        backend.converse("chat", [{"role": "user", "content": "x"}], None, 1, Budget(10, 2, 0.1))


def test_extract_json_is_brace_balanced_and_string_aware() -> None:
    assert extract_json('text {"a": {"b": "}"}} more') == {"a": {"b": "}"}}
    assert extract_json('```json\n{"lines": ["x"]}\n```') == {"lines": ["x"]}
    assert extract_json('{"a": 1} and then {"b": {"c": 2}}') == {"b": {"c": 2}}
    assert extract_json("no braces") is None and extract_json("") is None
    assert extract_json("{not json}") is None
    assert extract_json('{"esc": "a \\" b"}') == {"esc": 'a " b'}


def test_flatten_skips_system_and_handles_block_content() -> None:
    text = flatten(
        [
            {"role": "system", "content": "s"},
            {"role": "user", "content": [{"type": "text", "text": "part"}]},
        ]
    )
    assert text.startswith("<user>\npart\n</user>") and "s\n" not in text


def test_resolve_exe_prefers_the_native_exe_behind_the_npm_shim(
    tmp_path: Path, monkeypatch
) -> None:
    shim = tmp_path / "claude.CMD"
    shim.write_text("@echo off", encoding="utf-8")
    native = tmp_path / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
    monkeypatch.setattr("tiffin.model.claude_cli.shutil.which", lambda name: str(shim))
    assert resolve_exe("claude") == str(shim)
    native.parent.mkdir(parents=True)
    native.write_bytes(b"MZ")
    assert resolve_exe("claude") == str(native)
    monkeypatch.setattr("tiffin.model.claude_cli.shutil.which", lambda name: None)
    assert resolve_exe("claude") == "claude"
