"""The browser gate: exactly the reading surface the owner agreed to, and nothing else.

A background Chrome job acts in the owner's real, logged-in browser with nobody watching, so the
approver it asks has to be narrow on purpose. These pin the promise made on 2026-09-14: read
anywhere the job has navigated, navigate only over https to the agreed shops,
and deny typing, clicking, forms, JavaScript, uploads, batched actions and every non-browser
tool.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from tiffin.config import Budget
from tiffin.integrations import chrome_gate as gate
from tiffin.integrations.chrome_job import ALLOWED_DOMAINS, GATE_TOOL, ChromeJob

DOMAINS = ("sainsburys.co.uk", "ocado.com", "tesco.com")
C = gate.PREFIX


@pytest.mark.parametrize(
    "tool",
    [
        "tabs_context_mcp",
        "tabs_create_mcp",
        "tabs_close_mcp",
        "get_page_text",
        "read_page",
        "find",
        "list_connected_browsers",
    ],
)
def test_reading_and_tab_housekeeping_are_allowed(tool: str) -> None:
    assert gate.decide(C + tool, {}, DOMAINS)[0]


@pytest.mark.parametrize(
    "url",
    [
        "https://www.sainsburys.co.uk/gol-ui/SearchResults/skyr",
        "https://www.ocado.com/search?entry=M%26S%20skyr",
        "https://www.tesco.com/groceries/en-GB/search?query=skyr",
        "back",
        "forward",
    ],
)
def test_navigation_to_the_agreed_sites_is_allowed(url: str) -> None:
    assert gate.decide(C + "navigate", {"url": url}, DOMAINS)[0]


@pytest.mark.parametrize(
    "url",
    [
        "https://www.amazon.co.uk/s?k=whole+foods",  # a configured search URL, but not agreed
        "https://tesco.com.evil.example/",  # a suffix that is not a sub-domain
        "https://eviltesco.com/",
        "http://www.tesco.com/",  # not https
        "javascript:alert(1)",
        "file:///C:/example/secrets/tiffin",
        "chrome://settings",
        "",
    ],
)
def test_navigation_anywhere_else_is_denied(url: str) -> None:
    allowed, reason = gate.decide(C + "navigate", {"url": url}, DOMAINS)
    assert not allowed, url
    assert "?" not in reason and "=" not in reason, "a refusal never quotes a query string"


def test_a_new_tab_opened_straight_at_a_url_gets_the_navigation_check() -> None:
    assert not gate.decide(C + "tabs_create_mcp", {"url": "https://example.com"}, DOMAINS)[0]
    assert gate.decide(C + "tabs_create_mcp", {"url": "https://www.tesco.com/"}, DOMAINS)[0]


@pytest.mark.parametrize("action", ["screenshot", "scroll", "scroll_to", "zoom", "wait"])
def test_computer_may_look(action: str) -> None:
    assert gate.decide(C + "computer", {"action": action}, DOMAINS)[0]


@pytest.mark.parametrize(
    "action", ["left_click", "right_click", "double_click", "type", "key", "left_click_drag", ""]
)
def test_computer_may_not_touch(action: str) -> None:
    assert not gate.decide(C + "computer", {"action": action}, DOMAINS)[0]


@pytest.mark.parametrize(
    "tool",
    [
        C + "form_input",
        C + "javascript_tool",
        C + "file_upload",
        C + "upload_image",
        C + "browser_batch",  # would carry actions past the per-call check
        C + "shortcuts_execute",
        C + "gif_creator",
        C + "resize_window",
        C + "switch_browser",
        "Bash",
        "Write",
        "mcp__claude_ai_Gmail__send_message",
        f"mcp__{gate.SERVER_NAME}__{gate.TOOL_NAME}",  # the gate never approves itself
    ],
)
def test_everything_else_is_denied(tool: str) -> None:
    assert not gate.decide(tool, {"url": "https://www.tesco.com/"}, DOMAINS)[0]


def test_with_no_domains_configured_nothing_can_be_opened() -> None:
    assert not gate.decide(C + "navigate", {"url": "https://www.tesco.com/"}, ())[0]
    assert gate.decide(C + "get_page_text", {}, ())[0], "reading still works"


def _rpc(lines: list[dict], domains=DOMAINS, log: str = "") -> list[dict]:
    out = io.StringIO()
    gate.serve(io.StringIO("\n".join(json.dumps(m) for m in lines) + "\n"), out, domains, log)
    return [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]


def _call(tool: str, tool_input: dict) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {"name": "approve", "arguments": {"tool_name": tool, "input": tool_input}},
    }


def test_it_speaks_mcp() -> None:
    replies = _rpc(
        [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "X"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        ]
    )
    assert [r["id"] for r in replies] == [1, 2], "a notification gets no reply"
    assert replies[0]["result"]["protocolVersion"] == "X"
    assert [t["name"] for t in replies[1]["result"]["tools"]] == ["approve"]


def test_the_verdict_is_the_shape_claude_code_expects() -> None:
    ok = _rpc([_call(C + "get_page_text", {"tabId": 7})])[0]
    verdict = json.loads(ok["result"]["content"][0]["text"])
    assert verdict == {"behavior": "allow", "updatedInput": {"tabId": 7}}

    no = _rpc([_call(C + "form_input", {"ref": "x", "value": "y"})])[0]
    verdict = json.loads(no["result"]["content"][0]["text"])
    assert verdict["behavior"] == "deny" and "form_input" in verdict["message"]


def test_every_decision_is_logged_by_host_only(tmp_path: Path) -> None:
    log = tmp_path / "gate.jsonl"
    _rpc(
        [
            _call(C + "navigate", {"url": "https://www.tesco.com/search?query=someone"}),
            _call(C + "form_input", {"ref": "x", "value": "secret"}),
        ],
        log=str(log),
    )
    raw = log.read_text(encoding="utf-8")
    assert "someone" not in raw and "secret" not in raw, "no username, no typed value"
    entries = gate.read_log(log)
    assert [e["allowed"] for e in entries] == [True, False]
    assert gate.summarise(entries).startswith("gate: 1 allowed, 1 denied (form_input")


def test_the_job_runs_through_the_gate_and_not_around_it(tmp_path: Path) -> None:
    job = ChromeJob(tmp_path, Budget(60, 5, 0.5), exe="claude", prober=lambda _p: True)
    config, log = job.write_gate_config(tmp_path)
    argv = job.argv(config)
    assert argv[argv.index("--permission-prompt-tool") + 1] == GATE_TOOL
    assert argv[argv.index("--mcp-config") + 1] == str(config)
    assert "none" not in argv, "prompts go to the gate, not to nobody"

    server = json.loads(config.read_text(encoding="utf-8"))["mcpServers"][gate.SERVER_NAME]
    assert Path(server["args"][0]).name == "chrome_gate.py"
    assert server["env"][gate.ENV_DOMAINS] == ",".join(ALLOWED_DOMAINS)
    assert server["env"][gate.ENV_LOG] == str(log)
    assert set(ALLOWED_DOMAINS) == set(DOMAINS), "exactly the agreed sites, no more"


def test_every_chrome_job_closes_the_tab_it_opened() -> None:
    from pathlib import Path

    from tiffin.integrations import chrome_gate

    jobs = Path(__file__).resolve().parents[1] / "claude-jobs"
    for job in ("shop-reader",):
        assert "tabs_close_mcp" in (jobs / job / "CLAUDE.md").read_text("utf-8"), job
    allowed, why = chrome_gate.decide(
        "mcp__claude-in-chrome__tabs_close_mcp", {"tabId": 7}, ALLOWED_DOMAINS
    )
    assert allowed, f"the gate must let a job close its own tab: {why}"
