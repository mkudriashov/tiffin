"""The approver a background Chrome job asks instead of a person.

Claude in Chrome will not open a tab or navigate until somebody approves it, and no permission
mode counts as that approval - not `auto`, not `bypassPermissions`. A background `claude -p` job
has nobody to ask, so every browser job died at its first step ("I need your approval to use
Claude in Chrome") before it ever reached a website. The shop reader ran exactly once.

Claude Code's answer for unattended sessions is `--permission-prompt-tool`: an MCP tool the
session asks instead of a person. This is that tool, and it is deliberately narrow, because
whatever it allows happens in the user's logged-in Chrome with nobody watching:

- ALLOWED: reading and tab housekeeping (tab list, new tab, close tab, page text, read page,
  find), looking actions of `computer` (screenshot, scroll, zoom, wait), and `navigate` - but
  only over https, only to the configured domains and their sub-domains.
- DENIED: everything else. Typing, clicking, form input, JavaScript, file upload, batched
  actions, shortcuts, and every tool that is not a Claude in Chrome tool - including this one.


Every decision is appended to a JSON-lines log the job runner reads back and summarises into
the run ledger. A navigation is logged by HOST only: a URL may carry private details, and
nothing about either person goes into a log.

Stdlib only and no package-relative imports, so the session can run it by path with whichever
interpreter Tiffin itself is running under.
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, TextIO
from urllib.parse import urlsplit

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "chrome_gate"
TOOL_NAME = "approve"
PREFIX = "mcp__claude-in-chrome__"
ENV_DOMAINS = "CHROME_GATE_DOMAINS"
ENV_LOG = "CHROME_GATE_LOG"

#: nothing here changes a page or anything on a site
READ_TOOLS = frozenset(
    {
        "tabs_context_mcp",
        "tabs_create_mcp",
        "tabs_close_mcp",
        "get_page_text",
        "read_page",
        "find",
        "list_connected_browsers",
    }
)
#: the `computer` actions that only look
LOOK_ACTIONS = frozenset({"screenshot", "scroll", "scroll_to", "zoom", "wait"})


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().rstrip(".")


def allowed_url(url: str, domains: Sequence[str]) -> bool:
    """https to a configured domain or one of its sub-domains. Nothing else, ever."""
    if url in ("back", "forward"):
        return True  # history inside a tab this job opened and only ever navigated here
    parts = urlsplit(url)
    if parts.scheme != "https":
        return False
    host = host_of(url)
    return bool(host) and any(host == d or host.endswith("." + d) for d in domains)


def decide(
    tool_name: str, tool_input: Mapping[str, Any], domains: Sequence[str]
) -> tuple[bool, str]:
    """(allowed, reason). The reason never contains more of a URL than its host."""
    if not tool_name.startswith(PREFIX):
        return False, f"{tool_name} is not a browser tool"
    short = tool_name[len(PREFIX) :]
    if short in READ_TOOLS:
        # a new tab opened straight at a URL is a navigation, and gets the same check
        url = str(tool_input.get("url") or "")
        if url and not allowed_url(url, domains):
            return False, f"{short} at {host_of(url) or 'a non-https page'} is off the list"
        return True, short
    if short == "navigate":
        url = str(tool_input.get("url") or "")
        if allowed_url(url, domains):
            return True, f"navigate {host_of(url) or url}"
        return False, f"navigate to {host_of(url) or 'a non-https page'} is off the list"
    if short == "computer":
        action = str(tool_input.get("action") or "")
        if action in LOOK_ACTIONS:
            return True, f"computer {action}"
        return False, f"computer {action or '?'} changes the page"
    return False, f"{short} is not a reading tool"


def _log(path: str, tool_name: str, allowed: bool, reason: str) -> None:
    if not path:
        return
    entry = {"at": round(time.time(), 3), "tool": tool_name, "allowed": allowed, "reason": reason}
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except OSError:
        pass  # a log that cannot be written must not become a reason to allow or deny


def read_log(path: str | os.PathLike[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        with open(path, encoding="utf-8") as fh:
            for raw in fh:
                text = raw.strip()
                if text:
                    try:
                        out.append(json.loads(text))
                    except ValueError:
                        continue
    except OSError:
        return []
    return out


def summarise(entries: Iterable[Mapping[str, Any]]) -> str:
    """One line for the run ledger: how much was allowed, and every distinct refusal."""
    entries = list(entries)
    if not entries:
        return "gate: nothing asked"
    allowed = sum(1 for e in entries if e.get("allowed"))
    denied = [str(e.get("reason") or "") for e in entries if not e.get("allowed")]
    line = f"gate: {allowed} allowed, {len(denied)} denied"
    if denied:
        line += " (" + "; ".join(dict.fromkeys(denied)) + ")"
    return line[:400]


def _result(req_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _error(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


TOOL_SPEC = {
    "name": TOOL_NAME,
    "description": "Approve or deny one Claude in Chrome tool call for an unattended job.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "tool_name": {"type": "string"},
            "input": {"type": "object"},
            "tool_use_id": {"type": "string"},
        },
        "required": ["tool_name", "input"],
    },
}


def handle(msg: Mapping[str, Any], domains: Sequence[str], log_path: str) -> dict[str, Any] | None:
    method = msg.get("method")
    req_id = msg.get("id")
    params = msg.get("params") or {}
    if method == "initialize":
        return _result(
            req_id,
            {
                "protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": "1"},
            },
        )
    if str(method or "").startswith("notifications/"):
        return None
    if method == "ping":
        return _result(req_id, {})
    if method == "tools/list":
        return _result(req_id, {"tools": [TOOL_SPEC]})
    if method == "tools/call":
        if params.get("name") != TOOL_NAME:
            return _error(req_id, -32602, f"unknown tool {params.get('name')}")
        args = params.get("arguments") or {}
        tool_name = str(args.get("tool_name") or "")
        raw = args.get("input")
        tool_input: dict[str, Any] = raw if isinstance(raw, dict) else {}
        allowed, reason = decide(tool_name, tool_input, domains)
        _log(log_path, tool_name, allowed, reason)
        verdict = (
            {"behavior": "allow", "updatedInput": tool_input}
            if allowed
            else {
                "behavior": "deny",
                "message": f"Refused by Tiffin's browser gate: {reason}.",
            }
        )
        return _result(req_id, {"content": [{"type": "text", "text": json.dumps(verdict)}]})
    if req_id is None:
        return None
    return _error(req_id, -32601, f"method not found: {method}")


def serve(stdin: TextIO, stdout: TextIO, domains: Sequence[str], log_path: str) -> None:
    for raw in stdin:
        text = raw.strip()
        if not text:
            continue
        try:
            msg = json.loads(text)
        except ValueError:
            stdout.write(json.dumps(_error(None, -32700, "parse error")) + "\n")
            stdout.flush()
            continue
        batch = msg if isinstance(msg, list) else [msg]
        replies = [
            r for r in (handle(m, domains, log_path) for m in batch if isinstance(m, dict)) if r
        ]
        if not replies:
            continue
        out = replies if isinstance(msg, list) else replies[0]
        stdout.write(json.dumps(out) + "\n")
        stdout.flush()


def domains_from_env(value: str | None) -> tuple[str, ...]:
    """Comma-separated, lower-cased, no scheme. Empty means no navigation at all."""
    return tuple(d.strip().lower().strip(".") for d in (value or "").split(",") if d.strip())


def main() -> int:
    serve(
        sys.stdin,
        sys.stdout,
        domains_from_env(os.environ.get(ENV_DOMAINS)),
        os.environ.get(ENV_LOG, ""),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
