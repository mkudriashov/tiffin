"""Build the configured provider in one place."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from ..clock import DEFAULT_CLOCK, Clock
from ..config import Config
from ..errors import ConfigError
from ..http import Transport
from ..secrets import Secrets
from .api import ApiBackend
from .claude_cli import ClaudeBackend, resolve_exe
from .gateway import Backend
from .request_limit import RequestLimit

DEFAULT_MODELS = {
    "claude-cli": "opus",
    "anthropic": "claude-sonnet-4-5",
    "openai": "gpt-4.1-mini",
}


def configured_provider(
    config: Config, secrets: Secrets, transport: Transport, *, clock: Clock = DEFAULT_CLOCK
) -> Backend | None:
    model_config = config.section("model")
    provider = str(model_config["provider"])
    if provider == "none":
        return None
    if provider == "claude-cli":
        legacy = config.section("models")
        scripts = Path(sys.executable).parent
        mcp_exe = scripts / ("tiffin-mcp.exe" if os.name == "nt" else "tiffin-mcp")
        child = dict(os.environ)
        token = secrets.environ.get("TIFFIN_CLAUDE_OAUTH_TOKEN", "").strip()
        if token and "CLAUDE_CODE_OAUTH_TOKEN" not in child:
            # the subscription token of a machine with no interactive login; child_env passes
            # this one variable through and nothing else of Claude Code's
            child["CLAUDE_CODE_OAUTH_TOKEN"] = token
        return ClaudeBackend(
            resolve_exe(str(legacy.get("claude_exe", "claude"))),
            str(model_config.get("model") or legacy.get("claude_model", "opus")),
            config.paths.claude_cwd,
            state_dir=config.paths.state,
            mcp_exe=str(mcp_exe) if mcp_exe.exists() else None,
            mcp_env={"TIFFIN_ENV": config.env, "TIFFIN_HOME": str(config.paths.root)},
            task_models=model_config.get("tasks", {}),
            env=child,
        )
    key = secrets.optional("model_api_key")
    if (
        provider in ("anthropic", "openai") or model_config.get("auth_style") == "api-key"
    ) and not key:
        raise ConfigError(
            "model API key missing: set TIFFIN_MODEL_API_KEY or create model-api-key.txt in secrets"
        )
    base_url = str(
        model_config.get("base_url")
        or {
            "anthropic": "https://api.anthropic.com",
            "openai": "https://api.openai.com",
        }.get(provider, "")
    )
    return ApiBackend(
        provider,
        str(model_config.get("model") or DEFAULT_MODELS.get(provider, "")),
        base_url,
        key,
        transport,
        task_models=model_config.get("tasks", {}),
        auth_style=str(model_config.get("auth_style", "bearer")),
        request_limit=RequestLimit(
            config.paths.state, int(model_config["api_requests_per_day"]), clock
        ),
    )
