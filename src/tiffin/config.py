"""Packaged defaults with one optional overlay at TIFFIN_HOME/tiffin.toml."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import sys
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .errors import ConfigError

ENVS = ("dev", "prod")
_MISSING = object()
_SOURCE_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = (
    _SOURCE_ROOT
    if (_SOURCE_ROOT / "config" / "defaults.toml").is_file()
    else Path(__file__).resolve().parent
)


@dataclass(frozen=True)
class Budget:
    timeout_s: int
    turns: int
    # None disables the cost cap while keeping the time and turn limits.
    usd: float | None


@dataclass(frozen=True)
class Paths:
    root: Path
    db: Path
    state: Path
    logs: Path
    backups: Path
    exports: Path
    dryrun: Path
    secrets: Path
    config_dir: Path
    claude_cwd: Path
    icu_streams: Path

    def ensure(self) -> None:
        for p in (
            self.root,
            self.state,
            self.logs,
            self.backups,
            self.exports,
            self.dryrun,
            self.secrets,
            self.icu_streams,
            self.claude_cwd,
        ):
            p.mkdir(mode=0o700, parents=True, exist_ok=True)


@dataclass(frozen=True)
class Config:
    env: str
    data: dict[str, Any]
    paths: Paths

    @property
    def is_prod(self) -> bool:
        return self.env == "prod"

    def get(self, dotted: str, default: Any = _MISSING) -> Any:
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, Mapping) or part not in node:
                if default is _MISSING:
                    raise ConfigError(f"missing config key: {dotted}")
                return default
            node = node[part]
        return node

    def section(self, name: str) -> dict[str, Any]:
        value = self.get(name, {})
        if not isinstance(value, Mapping):
            raise ConfigError(f"config section {name} is not a table")
        return dict(value)

    def budget(self, task: str) -> Budget:
        raw = self.get(f"budgets.{task}")
        if not isinstance(raw, list) or len(raw) != 3:
            raise ConfigError(f"budgets.{task} must be [timeout_s, turns, usd]")
        capped = bool(self.get("models.cap_usd", True))
        return Budget(int(raw[0]), int(raw[1]), float(raw[2]) if capped else None)

    def model_scope(self) -> str:
        """Consent identity; a compatible endpoint change is a new processor."""
        provider = str(self.get("model.provider"))
        if provider != "openai-compatible":
            return provider
        address = str(self.get("model.base_url")).rstrip("/")
        digest = hashlib.sha256(address.encode("utf-8")).hexdigest()[:16]
        return f"{provider}@{digest}"

    def person_chat_id(self, slug: str) -> int | None:
        value = self.get(f"persons.{slug}.chat_id", None)
        return None if value in (None, 0) else int(value)

    @property
    def web_port(self) -> int:
        return int(self.get("web.port_prod" if self.is_prod else "web.port_dev"))


def deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = dict(base)
    for k, v in override.items():
        if isinstance(v, Mapping) and isinstance(out.get(k), Mapping):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except FileNotFoundError:
        raise ConfigError(f"config file missing: {path}") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"config file invalid: {path}: {e}") from None


def public_https_origin(origin: str) -> bool:
    """True for an https address that is not this computer: the shape of a public site. The
    reserved example names (the shipped placeholder, `example.com`, `.test`) are not public."""
    parts = urlsplit(str(origin).strip())
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host or host == "localhost":
        return False
    labels = host.split(".")
    if (
        "example" in labels
        or labels[-1] in ("test", "invalid", "localhost")
        or host.endswith((".example.com", ".example.net", ".example.org"))
        or host in ("example.com", "example.net", "example.org")
    ):
        return False
    try:
        return not ipaddress.ip_address(host).is_loopback
    except ValueError:
        return True


def default_home(environ: Mapping[str, str]) -> Path:
    if sys.platform == "win32":
        return Path(environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "tiffin"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "tiffin"
    return Path(environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "tiffin"


def build_paths(root: Path, config_dir: Path) -> Paths:
    return Paths(
        root=root,
        db=root / "tiffin.db",
        state=root / "state",
        logs=root / "logs",
        backups=root / "backups",
        exports=root / "exports",
        dryrun=root / "dryrun",
        secrets=root / "secrets",
        config_dir=config_dir,
        claude_cwd=root / "state" / "claude-cwd",
        icu_streams=root / "archives" / "icu" / "streams",
    )


def _validate(data: Mapping[str, Any]) -> None:
    if "paths" in data:
        raise ConfigError(
            "remove [paths] from tiffin.toml; set TIFFIN_HOME to choose the data directory"
        )
    env = data.get("env", {})
    if not isinstance(env, Mapping) or not isinstance(env.get("tz"), str):
        raise ConfigError("env.tz must be an IANA time zone")
    if env["tz"] != "Europe/London":
        try:
            ZoneInfo(env["tz"])
        except ZoneInfoNotFoundError:
            raise ConfigError("env.tz must be a known IANA time zone") from None
    country = env.get("country")
    if not isinstance(country, str) or len(country) != 2 or not country.isalpha():
        raise ConfigError("env.country must be a two-letter country code")
    persons = data.get("persons", {})
    if not isinstance(persons, Mapping):
        raise ConfigError("[persons] must be a table in tiffin.toml")
    for slug in ("owner", "partner"):
        person = persons.get(slug, {})
        if not isinstance(person, Mapping):
            raise ConfigError(f"[persons.{slug}] must be a table in tiffin.toml")
        chat_id = person.get("chat_id", 0)
        if not isinstance(chat_id, int) or isinstance(chat_id, bool) or chat_id < 0:
            raise ConfigError(f"persons.{slug}.chat_id must be a non-negative integer")
    web = data.get("web", {})
    if not isinstance(web, Mapping):
        raise ConfigError("[web] must be a table in tiffin.toml")
    if not isinstance(web.get("enabled"), bool):
        raise ConfigError("web.enabled must be true or false")
    if not isinstance(web.get("bind_trusted_network"), bool):
        raise ConfigError("web.bind_trusted_network must be true or false")
    if web.get("identity_mode") not in ("header", "signed_link", "telegram"):
        raise ConfigError("web.identity_mode must be header, telegram or signed_link")
    if not isinstance(web.get("trusted_header_proxy"), bool):
        raise ConfigError("web.trusted_header_proxy must be true or false")
    if web.get("identity_mode") == "header" and web["trusted_header_proxy"] is not True:
        raise ConfigError(
            "web.identity_mode = header needs web.trusted_header_proxy = true, "
            "and a proxy that sets the login header and strips the client's copy"
        )
    redirect = web.get("signed_out_redirect")
    if not isinstance(redirect, str) or (
        redirect
        and not (
            (redirect.startswith("/") and not redirect.startswith("//") and "\\" not in redirect)
            or redirect.startswith("https://")
        )
    ):
        raise ConfigError(
            "web.signed_out_redirect must be empty, a path beginning with one /, or an https URL"
        )
    try:
        origin = urlsplit(str(web.get("origin", "")))
        origin_host, _ = origin.hostname, origin.port
    except ValueError:
        raise ConfigError("web.origin must be a valid HTTP(S) origin") from None
    if web.get("enabled") and (
        origin.scheme not in ("http", "https")
        or not origin_host
        or origin.username
        or origin.password
        or origin.path not in ("", "/")
        or origin.query
        or origin.fragment
    ):
        raise ConfigError("web.origin must be an HTTP(S) origin without a path or credentials")
    if (
        web.get("identity_mode") == "telegram"
        and origin.scheme == "http"
        and origin_host not in ("127.0.0.1", "localhost", "::1")
    ):
        raise ConfigError("Telegram web sign-in requires HTTPS away from this computer")
    root_path = web.get("root_path")
    if not isinstance(root_path, str) or (root_path and not root_path.startswith("/")):
        raise ConfigError("web.root_path must be empty or begin with /")
    if root_path not in ("", "/"):
        segments = root_path.removeprefix("/").rstrip("/").split("/")
        if any(
            segment in ("", ".", "..")
            or any(char in segment for char in "\\?#%")
            or any(ord(char) < 32 for char in segment)
            for segment in segments
        ):
            raise ConfigError("web.root_path must contain only ordinary path segments")
    for key in ("port_prod", "port_dev"):
        port = web.get(key)
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            raise ConfigError(f"web.{key} must be an integer from 1 to 65535")
    schedule = data.get("schedule", {})
    if not isinstance(schedule, Mapping):
        raise ConfigError("[schedule] must be a table in tiffin.toml")
    for key in (
        "day_close",
        "menu_propose_time",
        "shopping_list_time",
        "shopping_joint_time",
        "weekly_calibration",
        "sunday_summary",
        "backup",
    ):
        value = schedule.get(key)
        if not isinstance(value, str) or re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value) is None:
            raise ConfigError(f"schedule.{key} must be HH:MM in local time")
    for key, low, high in (
        ("tick_s", 1, 3600),
        ("icu_sync_minute", 0, 59),
        ("menu_propose_days_before", 0, 7),
    ):
        value = schedule.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
            raise ConfigError(f"schedule.{key} must be an integer from {low} to {high}")
    ops = data.get("ops", {})
    if not isinstance(ops, Mapping):
        raise ConfigError("[ops] must be a table in tiffin.toml")
    for key in ("heartbeat_s", "heartbeat_stale_s", "log_keep_days", "backup_keep_days"):
        value = ops.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ConfigError(f"ops.{key} must be a positive integer")
    hosting = data.get("hosting", {})
    if not isinstance(hosting, Mapping):
        raise ConfigError("[hosting] must be a table in tiffin.toml")
    for key, required in (("records", True), ("web_front", False)):
        value = hosting.get(key)
        if (
            not isinstance(value, str)
            or (required and not value.strip())
            or len(value) > 120
            or any(c in value for c in "<>&\n\r")
        ):
            raise ConfigError(
                f"hosting.{key} must be plain text of at most 120 characters"
                + (" and not empty" if required else "")
            )
    units = data.get("units", {})
    if (
        not isinstance(units, Mapping)
        or units.get("weight") not in ("kg", "lb")
        or units.get("height") not in ("cm", "in")
        or not isinstance(units.get("currency"), str)
        or len(units["currency"]) != 3
        or not units["currency"].isalpha()
    ):
        raise ConfigError("units must choose kg/lb, cm/in and a three-letter currency")
    shopping = data.get("shopping", {})
    if not isinstance(shopping, Mapping) or not isinstance(
        shopping.get("count_on_home", False), bool
    ):
        raise ConfigError("shopping.count_on_home must be true or false")
    chrome = data.get("chrome", {})
    if not isinstance(chrome, Mapping):
        raise ConfigError("[chrome] must be a table in tiffin.toml")
    for key in ("shop_refresh_enabled", "shop_fill_enabled"):
        if not isinstance(chrome.get(key), bool):
            raise ConfigError(f"chrome.{key} must be true or false")
    model = data.get("model", {})
    if isinstance(model, Mapping) and "api_key" in model:
        raise ConfigError("model.api_key must be in TIFFIN_MODEL_API_KEY or the secrets directory")
    if not isinstance(model, Mapping) or model.get("provider") not in (
        "claude-cli",
        "anthropic",
        "openai",
        "openai-compatible",
        "none",
    ):
        raise ConfigError(
            "model.provider must be claude-cli, anthropic, openai, openai-compatible or none"
        )
    for key in ("model", "base_url"):
        if not isinstance(model.get(key), str):
            raise ConfigError(f"model.{key} must be a string")
    if model.get("auth_style") not in ("bearer", "api-key"):
        raise ConfigError("model.auth_style must be bearer or api-key")
    request_limit = model.get("api_requests_per_day")
    if not isinstance(request_limit, int) or isinstance(request_limit, bool) or request_limit < 1:
        raise ConfigError("model.api_requests_per_day must be a positive integer")
    if model["provider"] != "openai-compatible" and model["auth_style"] != "bearer":
        raise ConfigError("model.auth_style = api-key is only for openai-compatible")
    if model["provider"] == "openai-compatible" and not (model["model"] and model["base_url"]):
        raise ConfigError("openai-compatible requires model.model and model.base_url")
    if model["base_url"]:
        try:
            url = urlsplit(model["base_url"])
            url_host, _ = url.hostname, url.port
        except ValueError:
            raise ConfigError("model.base_url must be a valid HTTP(S) URL") from None
        if (
            url.scheme not in ("http", "https")
            or not url_host
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ConfigError("model.base_url must be an HTTP(S) URL without credentials")
    if model["provider"] in ("openai", "anthropic") and model["base_url"]:
        raise ConfigError("model.base_url is only supported for openai-compatible")
    tasks = model.get("tasks", {})
    if not isinstance(tasks, Mapping) or any(
        not isinstance(v, str) or not v.strip() or k not in data.get("budgets", {})
        for k, v in tasks.items()
    ):
        raise ConfigError("model.tasks must map task names to model names")


def load(
    env: str | None = None,
    *,
    root: Path | str | None = None,
    override: Path | str | None = None,
    environ: Mapping[str, str] | None = None,
) -> Config:
    environ = os.environ if environ is None else environ
    config_dir = REPO_ROOT / "config"
    data = _read_toml(config_dir / "defaults.toml")
    root_path = Path(root or environ.get("TIFFIN_HOME") or default_home(environ)).expanduser()
    if not root_path.is_absolute():
        raise ConfigError("TIFFIN_HOME must be an absolute path; set it to a directory you own")
    override_path = Path(override) if override else root_path / "tiffin.toml"
    if override_path.exists():
        data = deep_merge(data, _read_toml(override_path))
    _validate(data)
    env = env or environ.get("TIFFIN_ENV") or str(data["env"].get("default", "prod"))
    if env not in ENVS:
        raise ConfigError(f"TIFFIN_ENV must be one of {ENVS}, got {env!r}")
    paths = build_paths(root_path, config_dir)
    return Config(env=env, data=data, paths=paths)


# ---------------------------------------------------------------- writing the local overlay
def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    raise ConfigError(f"cannot write {type(value).__name__} to tiffin.toml")


def dump_toml(data: Mapping[str, Any]) -> str:
    """Just enough TOML to round-trip an overlay: scalars, flat lists, nested tables.

    Deliberately not a general writer. Anything it cannot represent raises rather than being
    silently dropped, because a config key that vanishes on rewrite is the kind of bug nobody
    finds until a job stops running.
    """
    lines: list[str] = []
    tables: list[tuple[str, Mapping[str, Any]]] = []

    def walk(prefix: str, node: Mapping[str, Any]) -> None:
        scalars = {k: v for k, v in node.items() if not isinstance(v, Mapping)}
        if scalars or not node:
            tables.append((prefix, scalars))
        for k, v in node.items():
            if isinstance(v, Mapping):
                walk(f"{prefix}.{k}" if prefix else k, v)

    walk("", data)
    for name, scalars in tables:
        if name:
            lines.append(f"[{name}]")
        for k, v in scalars.items():
            lines.append(f"{k} = {_toml_value(v)}")
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def update_override(path: Path, updates: Mapping[str, Any]) -> dict[str, Any]:
    """Deep-merge `updates` into the overlay at `path` and write it back.

    The overlay holds only what differs from the repo's defaults, so it stays small enough to
    read at a glance - which is the point of keeping it separate from defaults.toml.
    """
    current = _read_toml(path) if path.exists() else {}
    merged = deep_merge(current, updates)
    path.parent.mkdir(parents=True, exist_ok=True)
    header = "# Local overrides for tiffin; packaged defaults hold the rest.\n"
    path.write_text(header + dump_toml(merged), encoding="utf-8")
    return merged
