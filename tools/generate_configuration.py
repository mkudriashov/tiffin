"""Render the configuration reference directly from packaged defaults."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULTS = ROOT / "config" / "defaults.toml"
OUTPUT = ROOT / "docs" / "configuration.md"

SECTIONS = {
    "env": "Runtime mode, country and local time zone. Days use this zone; timestamps stay UTC.",
    "persons": (
        "Initial names and optional chat IDs. A chat ID should normally be paired by the wizard."
    ),
    "telegram": "Polling timeouts, message limits and owner alert delivery.",
    "web": "Loopback web listener, URL and identity mode. See [web access](web-access.md).",
    "units": "Display units for body measurements and currency for manual product prices.",
    "household": "Whether a second person is expected during onboarding.",
    "chrome": "Optional Claude CLI browser profile and shop-reader switches.",
    "shop": (
        "No retailer is selected by default. Catalogue packs work on their own. "
        "Retailer links are optional; `{q}` is the encoded search term."
    ),
    "models": (
        "Claude CLI executable/model settings and spending controls. "
        "The USD cap applies only to Claude CLI."
    ),
    "model": (
        "Chosen provider and endpoint. API keys belong in the secrets directory; "
        "see [models](models.md)."
    ),
    "budgets": (
        "Each task has `[timeout seconds, maximum turns, maximum USD]`. "
        "USD enforcement is provider-specific."
    ),
    "schedule": (
        "Scheduler times use the household time zone; intervals are seconds or minutes as named."
    ),
    "guardrails": (
        "Limits for targets, calibration and menu sizing. "
        "Change only with an understanding of the solver."
    ),
    "icu": "intervals.icu API URL, sync lookback windows and rate budget.",
    "ops": "Heartbeat, log and backup retention settings.",
}

SPECIAL = {
    "web.identity_mode": (
        "`telegram` (default) uses a one-use bot link; `header` trusts Tailscale Serve and needs "
        "`trusted_header_proxy`; "
        "`signed_link` is retained for existing installations."
    ),
    "web.bind_trusted_network": (
        "`true` lets the page bind an address other than loopback, for a container on a network "
        "only your reverse proxy can reach. Never publish that port directly."
    ),
    "web.trusted_header_proxy": (
        'Must be `true` for `identity_mode = "header"`: it says a proxy you control sets the '
        "login header and removes any copy a client sends."
    ),
    "web.signed_out_redirect": (
        "Where a signed-out visitor to the page root is sent: a path beginning with `/` or an "
        "`https://` URL. Empty shows the sign-in instructions."
    ),
    "web.root_path": (
        "URL mount path with ordinary segments; use an empty string when served at `/`."
    ),
    "web.origin": (
        "Browser-facing HTTP(S) origin, with no path or credentials. "
        "HTTPS is required away from loopback for Telegram sign-in."
    ),
    "web.secure_cookies": (
        "Secure cookies for development HTTPS; production HTTPS always uses Secure cookies."
    ),
    "model.provider": "`claude-cli`, `anthropic`, `openai`, `openai-compatible` or `none`.",
    "model.auth_style": (
        "`bearer`, or `api-key` for a compatible endpoint that requires that header."
    ),
    "model.base_url": "API root for `openai-compatible`, without credentials.",
    "model.api_requests_per_day": (
        "Maximum API HTTP attempts per household local day, including retries and doctor "
        "probes. This is a request count, not a USD cap."
    ),
    "models.cap_usd": (
        "When true, API providers degrade to manual because their spending cap "
        "cannot be enforced here."
    ),
    "persons.owner.chat_id": (
        "Optional owner chat ID for scripted setup; normally paired by a code."
    ),
    "persons.partner.chat_id": (
        "Optional partner chat ID for scripted setup; normally paired by a code."
    ),
    "chrome.shop_refresh_enabled": (
        "Enable optional retailer product refresh through Claude CLI browser tools."
    ),
    "chrome.shop_fill_enabled": "Allow the optional shop reader to fill missing products.",
    "env.country": (
        "Two-letter country code; setup pre-fills it from the time zone. "
        "GB is the supported country for the bundled food catalogue."
    ),
    "units.currency": "ISO currency code for prices entered by hand.",
    "shop.retailers": "Enabled retailer adapters in preferred order; empty means catalogue only.",
    "shop.retailer": "Legacy single-retailer setting; still read when retailers is empty.",
    "schedule.tick_s": "Seconds between scheduler checks.",
    "schedule.icu_sync_minute": "Minute of each hour for intervals.icu sync.",
    "schedule.day_close": "Daily close time.",
    "schedule.menu_propose_days_before": "How far ahead to build a menu.",
    "schedule.menu_propose_time": "Main menu build time; the 12:00 retry remains fixed.",
    "schedule.shopping_list_time": "Personal shopping list time.",
    "schedule.shopping_joint_time": "Shared shopping list time.",
    "schedule.weekly_calibration": "Weekly calibration time.",
    "schedule.sunday_summary": "Sunday summary time.",
    "schedule.backup": "Daily backup and log pruning time.",
    "ops.heartbeat_s": "Seconds between process heartbeats.",
    "ops.heartbeat_stale_s": "Age in seconds at which health checks mark a heartbeat stale.",
    "ops.log_keep_days": "Days to keep tiffin log files; old files are removed by the backup job.",
    "ops.backup_keep_days": "Days to keep dated database backups.",
}


def _flatten(node: dict[str, Any], prefix: str = "") -> list[tuple[str, Any]]:
    result: list[tuple[str, Any]] = []
    for key, value in node.items():
        dotted = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            result.extend(_flatten(value, dotted))
        else:
            result.append((dotted, value))
    return result


def _default(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list):
        return "[" + ", ".join(_default(item) for item in value) + "]"
    return str(value)


def render() -> str:
    with DEFAULTS.open("rb") as stream:
        values = tomllib.load(stream)
    lines = [
        "# Configuration reference",
        "",
        "`tiffin init` writes only local overrides to `TIFFIN_HOME/tiffin.toml`. "
        "Unspecified keys use the packaged defaults below. Never put a token or API key in TOML; "
        "use `TIFFIN_HOME/secrets/` or the supported environment variable. "
        "This table is generated from `config/defaults.toml` and checked by a test.",
        "",
    ]
    for section, summary in SECTIONS.items():
        lines += [
            f"## {section}",
            "",
            summary,
            "",
            "| Key | Default | Meaning |",
            "| --- | --- | --- |",
        ]
        for key, value in _flatten({section: values[section]}):
            default = _default(value).replace("|", "\\|")
            explanation = SPECIAL.get(
                key, key.rsplit(".", 1)[-1].replace("_", " ").capitalize() + "."
            )
            lines.append(f"| `{key}` | `{default}` | {explanation} |")
        lines.append("")
    lines += [
        "## Local overrides",
        "",
        "Place only changed values in `tiffin.toml`, for example:",
        "",
        "```toml",
        "[env]",
        'tz = "Europe/London"',
        "[web]",
        'identity_mode = "telegram"',
        'origin = "http://127.0.0.1:7788"',
        "```",
        "",
        "Optional `[model.tasks]` entries name a model for a task whose key appears under "
        "`[budgets]`; no task overrides are set by default. `TIFFIN_HOME` chooses the data "
        "directory and `TIFFIN_ENV` chooses `dev` or `prod`.",
        "",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(render(), encoding="utf-8")
