"""`tiffin-mcp`: a stdio JSON-RPC 2.0 MCP server exposing the tools registry for ONE person.

The person is fixed by TIFFIN_PERSON_ID in the environment (written into the mcp-config by
ClaudeBackend.converse); every tool ignores any person argument. Writes only create intents.
Hand-rolled: initialize, notifications/initialized, ping, tools/list, tools/call.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, TextIO

from . import __version__, log
from .app import App
from .db import Db
from .model import tools

PROTOCOL_VERSION = "2025-06-18"
logger = log.get("tiffin.mcp")


def _result(req_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _error(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


class McpServer:
    def __init__(
        self,
        db: Db,
        person_id: int,
        *,
        model_used: str | None = None,
        retailers: tuple[str, ...] = (),
    ):
        self.bound = tools.BoundTools(db, person_id, model_used=model_used, retailers=retailers)
        self.person_id = person_id

    def handle(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        method = msg.get("method")
        req_id = msg.get("id")
        params = msg.get("params") or {}
        if method == "initialize":
            return _result(
                req_id,
                {
                    "protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "tiffin", "version": __version__},
                },
            )
        if method == "notifications/initialized" or (method or "").startswith("notifications/"):
            return None
        if method == "ping":
            return _result(req_id, {})
        if method == "tools/list":
            return _result(req_id, {"tools": self.bound.to_mcp()})
        if method == "tools/call":
            name = str(params.get("name") or "")
            args = params.get("arguments") or {}
            try:
                out = self.bound.call(name, args)
                return _result(
                    req_id,
                    {
                        "content": [
                            {
                                "type": "text",
                                "text": json.dumps(out, ensure_ascii=False, default=str),
                            }
                        ],
                        "isError": False,
                    },
                )
            except tools.ToolError as e:
                return _result(
                    req_id, {"content": [{"type": "text", "text": str(e)}], "isError": True}
                )
            except Exception as e:  # a tool crash must not kill the server
                logger.exception(
                    "tool failed type=%s", type(e).__name__, extra={"event": "mcp_tool_error"}
                )
                return _result(
                    req_id,
                    {
                        "content": [{"type": "text", "text": f"{type(e).__name__}: tool failed"}],
                        "isError": True,
                    },
                )
        if req_id is None:
            return None
        return _error(req_id, -32601, f"method not found: {method}")


def serve(server: McpServer, stdin: TextIO, stdout: TextIO) -> None:
    """Newline-delimited JSON-RPC (the stdio transport Claude Code speaks)."""
    for line in stdin:
        line = line.strip()  # noqa: PLW2901
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            stdout.write(json.dumps(_error(None, -32700, "parse error")) + "\n")
            stdout.flush()
            continue
        if isinstance(msg, list):
            responses = [r for r in (server.handle(m) for m in msg) if r is not None]
            if responses:
                stdout.write(json.dumps(responses) + "\n")
                stdout.flush()
            continue
        response = server.handle(msg)
        if response is not None:
            stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            stdout.flush()


def main() -> int:
    person_id = os.environ.get("TIFFIN_PERSON_ID")
    if not person_id or not person_id.isdigit():
        print("TIFFIN_PERSON_ID must be set to the person's integer id", file=sys.stderr)
        return 2
    app = App.from_env()
    app.setup_logging(console=False)
    db = app.open_db()
    try:
        from .jobs import shop_refresh  # noqa: PLC0415 (jobs import the scheduler)

        server = McpServer(
            db,
            int(person_id),
            model_used=os.environ.get("TIFFIN_MODEL_USED"),
            retailers=tuple(shop_refresh.retailer_order(app.config)),
        )
        serve(server, sys.stdin, sys.stdout)
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
