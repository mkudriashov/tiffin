"""`claude -p` as a backend: prompt on stdin, one JSON envelope on stdout, an empty cwd, no tools.

JSON tasks: `--json-schema <schema>` returns `structured_output`. Conversations: the only tools
are Tiffin's own MCP server, named in a per-person mcp-config whose env fixes
TIFFIN_PERSON_ID, so the person is set by the environment and never by an argument.

Envelope mapping (plan B.5): `subtype == "success"` -> ok; `error_max_turns`,
`error_max_budget_usd`, `error_max_structured_output_retries` -> ModelInvalid; `is_error` whose
text matches the usage-limit pattern -> QuotaExhausted; any other failure -> ModelError; the
subprocess timing out -> ModelTimeout. Nothing here logs the prompt or the reply.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from ..clock import DEFAULT_CLOCK, Clock
from ..config import Budget
from ..errors import ModelError, ModelInvalid, QuotaExhausted
from . import schemas
from .gateway import BudgetExceeded, Messages, ModelResult, ModelTimeout
from .tools import BoundTools

QUOTA_RE = re.compile(r"hit your limit|usage limit|limit (will )?reset|rate limit", re.I)
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
INVALID_SUBTYPES = frozenset(
    {"error_max_turns", "error_max_budget_usd", "error_max_structured_output_retries"}
)
MCP_SERVER_NAME = "tiffin"
MCP_TOOL_PATTERN = "mcp__tiffin__*"
# Tiffin's own session variables must not leak into the child: a nested Claude Code
# reading CLAUDECODE / CLAUDE_CODE_* would think it is a child of an interactive session.
_STRIP_ENV_PREFIXES = (
    "CLAUDECODE",
    "CLAUDE_CODE_",
    "CLAUDE_PID",
    "CLAUDE_EFFORT",
    "CLAUDE_JOB_DIR",
)

# The one CLAUDE_CODE_ variable that must reach the child: the long-lived subscription token a
# server or container sets (from `claude setup-token`) because it has no interactive login.
_KEEP_ENV = frozenset({"CLAUDE_CODE_OAUTH_TOKEN"})

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


def resolve_exe(name: str = "claude") -> str:
    """The native claude.exe behind the npm shim when present (no cmd.exe in the chain, so JSON
    arguments survive), otherwise whatever `which` finds, otherwise the name itself."""
    found = shutil.which(name) or name
    p = Path(found)
    if p.suffix.lower() in (".cmd", ".bat"):
        native = p.parent / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
        if native.is_file():
            return str(native)
    return found


def child_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    src = os.environ if base is None else base
    return {
        k: v
        for k, v in src.items()
        if k.upper() in _KEEP_ENV or not k.upper().startswith(_STRIP_ENV_PREFIXES)
    }


def default_mcp_exe() -> str:
    return str(Path(sys.executable).with_name("tiffin-mcp.exe"))


def extract_json(text: str) -> Any:
    """The largest brace-balanced JSON object in `text` (fenced or bare), or None."""
    if not text:
        return None
    candidates: list[str] = []
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fence:
        candidates.append(fence.group(1))
    for start, ch in enumerate(text):
        if ch != "{":
            continue
        depth = 0
        in_str = False
        escaped = False
        for j in range(start, len(text)):
            c = text[j]
            if in_str:
                if escaped:
                    escaped = False
                elif c == "\\":
                    escaped = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(text[start : j + 1])
                    break
    for cand in sorted(candidates, key=len, reverse=True):
        try:
            parsed = json.loads(cand)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def parse_envelope(stdout: str, stderr: str, returncode: int) -> dict[str, Any]:
    """The result envelope, or the exception the plan maps its failure to."""
    raw = (stdout or "").strip()
    envelope: Any = None
    if raw:
        try:
            envelope = json.loads(raw)
        except json.JSONDecodeError:
            envelope = extract_json(raw)
    if not isinstance(envelope, dict):
        text = _ANSI.sub("", f"{raw[-400:]} {(stderr or '')[-400:]}").strip()
        if QUOTA_RE.search(text):
            raise QuotaExhausted("claude -p quota exhausted")
        raise ModelError(f"claude -p exit {returncode} without a JSON envelope")
    subtype = str(envelope.get("subtype") or "")
    is_error = bool(envelope.get("is_error"))
    text = _ANSI.sub("", str(envelope.get("result") or "") + " " + (stderr or "")[-400:])
    if subtype == "error_max_budget_usd":
        raise BudgetExceeded(
            f"claude -p {subtype}: spent {envelope.get('total_cost_usd')} USD, answer discarded"
        )
    if subtype in INVALID_SUBTYPES:
        raise ModelInvalid(f"claude -p {subtype}")
    if is_error or (subtype and subtype != "success"):
        if QUOTA_RE.search(text):
            raise QuotaExhausted(f"claude -p {subtype or 'error'}: quota exhausted")
        raise ModelError(f"claude -p {subtype or 'error'}")
    if returncode != 0 and not subtype:
        raise ModelError(f"claude -p exit {returncode}")
    return envelope


def model_used_of(envelope: Mapping[str, Any], default: str) -> str:
    usage = envelope.get("modelUsage")
    if isinstance(usage, dict) and usage:
        best = max(usage, key=lambda k: int((usage[k] or {}).get("outputTokens") or 0))
        return str(best)
    return default


def flatten(messages: Messages) -> str:
    """Prior turns as a transcript; `claude -p` takes one prompt, not a message array."""
    parts: list[str] = []
    for m in messages:
        role = str(m.get("role") or "user")
        if role == "system":
            continue
        content = m.get("content")
        if isinstance(content, list):
            content = "\n".join(str(x.get("text", "")) for x in content if isinstance(x, dict))
        parts.append(f"<{role}>\n{'' if content is None else content}\n</{role}>")
    return "\n\n".join(parts) + "\n\nReply to the last user turn."


class ClaudeBackend:
    name = "claude"

    def __init__(
        self,
        exe: str,
        model: str,
        cwd: Path,
        *,
        runner: Runner = subprocess.run,
        clock: Clock = DEFAULT_CLOCK,
        state_dir: Path | None = None,
        mcp_exe: str | None = None,
        mcp_env: Mapping[str, str] | None = None,
        env: Mapping[str, str] | None = None,
        system_prompt: str | None = None,
        task_models: Mapping[str, str] | None = None,
    ):
        self.exe = exe
        self.model = model
        self.task_models = dict(task_models or {})
        self.cwd = Path(cwd)
        self.runner = runner
        self.clock = clock
        # When set, JSON tasks run with `--system-prompt <text>` in place of Claude Code's own
        # (coding) system prompt, which is most of a one-turn call's cost (S3).
        self.system_prompt = system_prompt
        self.state_dir = Path(state_dir) if state_dir is not None else self.cwd.parent
        self.mcp_exe = mcp_exe or default_mcp_exe()
        self.mcp_env = dict(mcp_env or {})
        self.env = child_env(env)

    def __repr__(self) -> str:
        return f"ClaudeBackend(model={self.model!r}, cwd={str(self.cwd)!r})"

    # -- argv ------------------------------------------------------------------------
    def base_argv(self, budget: Budget, task: str | None = None) -> list[str]:
        argv = [
            self.exe,
            "-p",
            "--output-format",
            "json",
            "--setting-sources",
            "",
            "--no-session-persistence",
            "--permission-prompts",
            "none",
            "--model",
            self.task_models.get(task, self.model) if task is not None else self.model,
        ]
        if budget.usd is not None:  # no cap at all when `models.cap_usd` is off
            argv += ["--max-budget-usd", f"{budget.usd:.3f}"]
        return [*argv, "--max-turns", str(max(1, budget.turns))]

    def json_argv(
        self, schema: Mapping[str, Any], budget: Budget, task: str | None = None
    ) -> list[str]:
        argv = [
            *self.base_argv(budget, task),
            "--tools",
            "",
            "--json-schema",
            json.dumps(schemas.for_model(dict(schema)), separators=(",", ":")),
        ]
        if self.system_prompt:
            argv += ["--system-prompt", self.system_prompt]
        return argv

    def converse_argv(
        self, mcp_config: Path, system: str, budget: Budget, task: str | None = None
    ) -> list[str]:
        argv = [
            *self.base_argv(budget, task),
            "--mcp-config",
            str(mcp_config),
            "--strict-mcp-config",
            "--tools",
            "",
            "--allowedTools",
            MCP_TOOL_PATTERN,
        ]
        if system:
            argv += ["--append-system-prompt", system]
        return argv

    def write_mcp_config(self, person_id: int) -> Path:
        """state\\mcp-<person>.json naming tiffin-mcp with TIFFIN_PERSON_ID in its env."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        path = self.state_dir / f"mcp-{int(person_id)}.json"
        env = dict(self.mcp_env)
        env["TIFFIN_PERSON_ID"] = str(int(person_id))
        payload = {
            "mcpServers": {
                MCP_SERVER_NAME: {
                    "type": "stdio",
                    "command": self.mcp_exe,
                    "args": [],
                    "env": env,
                }
            }
        }
        path.write_text(json.dumps(payload, indent=1), encoding="utf-8")
        return path

    # -- running -----------------------------------------------------------------------
    def invoke_raw(self, argv: list[str], prompt: str, timeout_s: float) -> dict[str, Any]:
        """Run the CLI and return {returncode, stdout, stderr, elapsed_ms}; no mapping."""
        self.cwd.mkdir(parents=True, exist_ok=True)
        t0 = time.monotonic()
        try:
            proc = self.runner(
                argv,
                input=prompt,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_s,
                cwd=str(self.cwd),
                env=self.env,
            )
        except subprocess.TimeoutExpired:
            raise ModelTimeout(f"claude -p exceeded {timeout_s:.0f}s") from None
        except FileNotFoundError:
            raise ModelError(
                "Claude CLI was not found. Install Claude Code and ensure 'claude' is on PATH, "
                "or choose another model provider."
            ) from None
        except OSError as e:
            raise ModelError(f"claude -p could not start: {e}") from None
        return {
            "returncode": int(proc.returncode),
            "stdout": proc.stdout or "",
            "stderr": proc.stderr or "",
            "elapsed_ms": int((time.monotonic() - t0) * 1000),
        }

    def _invoke(self, argv: list[str], prompt: str, budget: Budget) -> tuple[dict[str, Any], int]:
        run = self.invoke_raw(argv, prompt, float(budget.timeout_s))
        envelope = parse_envelope(run["stdout"], run["stderr"], run["returncode"])
        return envelope, int(run["elapsed_ms"])

    def _result(
        self, envelope: Mapping[str, Any], elapsed_ms: int, data: Any, task: str | None = None
    ) -> ModelResult:
        return ModelResult(
            text=str(envelope.get("result") or ""),
            data=data,
            model_used=model_used_of(
                envelope, self.task_models.get(task, self.model) if task is not None else self.model
            ),
            backend=self.name,
            cost_usd=float(envelope.get("total_cost_usd") or 0.0),
            duration_ms=int(envelope.get("duration_ms") or elapsed_ms),
            raw=dict(envelope),
        )

    def complete_json(
        self, task: str, prompt: str, schema: dict[str, Any], budget: Budget
    ) -> ModelResult:
        envelope, elapsed = self._invoke(self.json_argv(schema, budget, task), prompt, budget)
        data = envelope.get("structured_output")
        if data is None:
            data = extract_json(str(envelope.get("result") or ""))
        if data is None:
            raise ModelInvalid(f"claude -p {task}: no structured_output in the envelope")
        schemas.validate_data(schema, data, name=task)
        return self._result(envelope, elapsed, data, task)

    def converse(
        self,
        task: str,
        messages: Messages,
        tools: BoundTools | None,
        person_id: int,
        budget: Budget,
    ) -> ModelResult:
        system = "\n\n".join(
            str(m.get("content") or "") for m in messages if m.get("role") == "system"
        )
        config_path = self.write_mcp_config(person_id)
        argv = self.converse_argv(config_path, system, budget, task)
        envelope, elapsed = self._invoke(argv, flatten(messages), budget)
        text = str(envelope.get("result") or "").strip()
        if not text:
            raise ModelInvalid(f"claude -p {task}: empty reply")
        result = self._result(envelope, elapsed, None, task)
        result.text = text
        return result
