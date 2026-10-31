"""Optional Claude-in-Chrome reader with a restricted browser tool surface.

The permission gate allows navigation and reading on configured retailer domains.
A missing browser degrades the job. Prompts, replies and page contents are not logged.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import Budget
from ..errors import TiffinError
from ..model.claude_cli import child_env, extract_json, parse_envelope, resolve_exe
from . import chrome_gate

BROWSER_TOOLS = "mcp__claude-in-chrome__*"
DEFAULT_PROFILE = "Default"
#: the only sites a background browser job may open: Sainsbury's, Ocado and Tesco.
#: M&S is read through Ocado. The
#: Whole Foods search URL in config goes through Amazon and is deliberately NOT here.
ALLOWED_DOMAINS = ("sainsburys.co.uk", "ocado.com", "tesco.com")
GATE_TOOL = f"mcp__{chrome_gate.SERVER_NAME}__{chrome_gate.TOOL_NAME}"
GATE_SCRIPT = Path(chrome_gate.__file__)

Runner = Callable[..., "subprocess.CompletedProcess[str]"]
Prober = Callable[[str], bool]


class ChromeUnavailable(TiffinError):
    """Chrome is not running on the profile the job needs."""


@dataclass
class ChromeRun:
    """What the browser session reported, plus what it cost."""

    written: list[dict[str, Any]] = field(default_factory=list)
    failed: list[dict[str, Any]] = field(default_factory=list)
    num_turns: int = 0
    cost_usd: float = 0.0
    elapsed_ms: int = 0
    #: what the gate allowed and refused, for the run ledger
    gate: str = ""

    @property
    def summary(self) -> str:
        line = (
            f"{len(self.written)} written, {len(self.failed)} failed, "
            f"{self.num_turns} turns, {self.cost_usd:.2f} USD"
        )
        return f"{line}; {self.gate}" if self.gate else line


def local_state_profiles(path: Path | None = None) -> list[str]:
    """The profiles Chrome itself records as open, from its `Local State` file.

    Chrome keeps `profile.last_active_profiles` current as profile windows open and close (it
    is what a restart restores). Empty when the file is missing or unreadable.
    """
    if path is None:
        root = os.environ.get("LOCALAPPDATA", "")
        if not root:
            return []
        path = Path(root) / "Google" / "Chrome" / "User Data" / "Local State"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return []
    profile = data.get("profile") if isinstance(data, dict) else None
    active = profile.get("last_active_profiles") if isinstance(profile, dict) else None
    return [str(p) for p in active] if isinstance(active, list) else []


def powershell_chrome_probe(
    profile: str,
    *,
    runner: Runner = subprocess.run,
    open_profiles: Callable[[], Sequence[str]] = local_state_profiles,
) -> bool:
    """True when Chrome is running with this profile open.

    A command line or Chrome's Local State can identify the open profile. Windows exposes
    command lines through CIM. Where the probe cannot run, the browser session checks itself.
    """
    script = (
        "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" "
        "| Select-Object -ExpandProperty CommandLine"
    )
    try:
        proc = runner(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
    except OSError, subprocess.SubprocessError:
        return True
    if proc.returncode != 0:
        return True
    lines = proc.stdout or ""
    if f"--profile-directory={profile}" in lines or f'--profile-directory="{profile}"' in lines:
        return True
    # Chrome is running (some chrome.exe has a command line) and says the profile is open
    return bool(lines.strip()) and profile in open_profiles()


def result_object(envelope: Mapping[str, Any]) -> dict[str, Any]:
    """The JSON object a session ends with.

    The model writes prose plus a JSON block, so this reads the last JSON object in the text
    rather than requiring the whole reply to be JSON. A reply with no object at all raises:
    prose that sounds successful is not a result.
    """
    text = str(envelope.get("result") or "")
    data = envelope.get("structured_output")
    if not isinstance(data, dict):
        try:
            data = extract_json(text)
        except ValueError, json.JSONDecodeError:
            data = None
    if not isinstance(data, dict):
        gate = str(envelope.get("gate") or "")
        if refused_the_browser(text):
            raise ChromeUnavailable(
                "the session was not allowed to drive Chrome"
                + (f" ({gate})" if gate else "")
                + ". Check that the Claude in Chrome extension is enabled on the profile."
            )
        raise TiffinError("the browser session returned no result object")
    return data


#: a session that declined to act reads nothing like one that acted and found nothing, and the
#: two need different answers from a human: one is a permission to fix, the other is a shop
#: that did not stock the food
_REFUSED = (
    "i need your permission",
    "grant permission",
    "requires browser automation",
    "don't have permission",
    "do not have permission",
    "permission to use claude in chrome",
    "need your approval",
    "approve claude in chrome",
    "approval to use claude in chrome",
    "refused by Tiffin's browser gate",
)


def refused_the_browser(text: str) -> bool:
    """True when the reply is the session asking to be allowed, rather than a failed read."""
    low = text.casefold()
    return any(marker in low for marker in _REFUSED)


def rows_of(data: Mapping[str, Any], key: str) -> list[dict[str, Any]]:
    value = data.get(key)
    return [r for r in value if isinstance(r, dict)] if isinstance(value, list) else []


def parse_result(envelope: Mapping[str, Any]) -> tuple[list[dict], list[dict]]:
    """`{written: [...], failed: [...]}`, the recipe writer's shape."""
    data = result_object(envelope)
    return rows_of(data, "written"), rows_of(data, "failed")


class ChromeJob:
    """One `claude -p --chrome` session in a fixed working directory."""

    def __init__(
        self,
        cwd: Path,
        budget: Budget,
        *,
        exe: str | None = None,
        model: str | None = None,
        profile: str = DEFAULT_PROFILE,
        runner: Runner = subprocess.run,
        prober: Prober | None = None,
        env: Mapping[str, str] | None = None,
        allowed_domains: Sequence[str] = ALLOWED_DOMAINS,
        gate_python: str | None = None,
        state_dir: Path | None = None,
        retry_hint: str = "next week",
    ):
        self.cwd = Path(cwd)
        self.budget = budget
        self.exe = exe or resolve_exe()
        self.model = model
        self.profile = profile
        self.runner = runner
        self.prober = prober if prober is not None else powershell_chrome_probe
        self.env = child_env(env)
        self.allowed_domains = tuple(allowed_domains)
        # said in `check_ready`'s message when Chrome is shut.
        self.retry_hint = retry_hint
        # the gate is stdlib-only, so whichever interpreter Tiffin runs under can run it
        self.gate_python = gate_python or sys.executable
        self.state_dir = state_dir or self.cwd / ".state"

    def argv(self, gate_config: Path | None = None) -> list[str]:
        argv = [
            self.exe,
            "-p",
            "--chrome",
            "--output-format",
            "json",
            "--tools",
            "",
            "--allowedTools",
            BROWSER_TOOLS,
        ]
        if gate_config is not None:
            argv += ["--mcp-config", str(gate_config), "--permission-prompt-tool", GATE_TOOL]
        else:
            # no gate means nobody may answer, which is the safe failure: every browser action
            # that needs approval is refused and the job degrades saying so
            argv += ["--permission-prompts", "none"]
        argv += [
            "--setting-sources",
            "project",
            "--no-session-persistence",
            "--max-turns",
            str(max(1, self.budget.turns)),
        ]
        if self.budget.usd is not None:  # no cap at all when `models.cap_usd` is off
            argv += ["--max-budget-usd", f"{self.budget.usd:.3f}"]
        if self.model:
            argv += ["--model", self.model]
        return argv

    def write_gate_config(self, workdir: Path) -> tuple[Path, Path]:
        """(mcp-config, decision log) for one session, both inside `workdir`."""
        workdir.mkdir(parents=True, exist_ok=True)
        log = workdir / "gate.jsonl"
        config = workdir / "gate-mcp.json"
        payload = {
            "mcpServers": {
                chrome_gate.SERVER_NAME: {
                    "type": "stdio",
                    "command": self.gate_python,
                    "args": [str(GATE_SCRIPT)],
                    "env": {
                        chrome_gate.ENV_DOMAINS: ",".join(self.allowed_domains),
                        chrome_gate.ENV_LOG: str(log),
                    },
                }
            }
        }
        config.write_text(json.dumps(payload, indent=1), encoding="utf-8")
        return config, log

    def check_ready(self) -> None:
        if not self.cwd.is_dir():
            raise ChromeUnavailable(f"the job directory is missing: {self.cwd}")
        if not self.prober(self.profile):
            raise ChromeUnavailable(
                f'Chrome is not running on --profile-directory="{self.profile}"; '
                f"start Chrome with that profile and retry {self.retry_hint}"
            )

    def run_json(self, prompt: str) -> tuple[dict[str, Any], Mapping[str, Any]]:
        """One session; returns (the object it ended with, the CLI envelope)."""
        envelope = self._invoke(prompt)
        return result_object(envelope), envelope

    def _invoke(self, prompt: str) -> Mapping[str, Any]:
        self.check_ready()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        workdir = Path(tempfile.mkdtemp(prefix="tiffin-chrome-gate-", dir=self.state_dir))
        try:
            config, log = self.write_gate_config(workdir)
            t0 = time.monotonic()
            try:
                proc = self.runner(
                    self.argv(config),
                    input=prompt,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=float(self.budget.timeout_s),
                    cwd=str(self.cwd),
                    env=self.env,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                raise ChromeUnavailable(
                    f"the browser session exceeded {self.budget.timeout_s:.0f}s"
                ) from None
            except OSError as e:
                raise ChromeUnavailable(f"claude -p --chrome could not start: {e}") from None
            elapsed = int((time.monotonic() - t0) * 1000)
            envelope = dict(
                parse_envelope(proc.stdout or "", proc.stderr or "", int(proc.returncode))
            )
            envelope["elapsed_ms"] = elapsed
            envelope["gate"] = chrome_gate.summarise(chrome_gate.read_log(log))
            return envelope
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    def run(self, prompt: str) -> ChromeRun:
        """The recipe writer's shape: what was written, what failed, what it cost."""
        envelope = self._invoke(prompt)
        written, failed = parse_result(envelope)
        return ChromeRun(
            written=written,
            failed=failed,
            num_turns=int(envelope.get("num_turns") or 0),
            cost_usd=float(envelope.get("total_cost_usd") or 0.0),
            elapsed_ms=int(envelope.get("elapsed_ms") or 0),
            gate=str(envelope.get("gate") or ""),
        )


__all__ = [
    "ALLOWED_DOMAINS",
    "BROWSER_TOOLS",
    "GATE_TOOL",
    "ChromeJob",
    "ChromeRun",
    "ChromeUnavailable",
    "local_state_profiles",
    "parse_result",
    "powershell_chrome_probe",
]
