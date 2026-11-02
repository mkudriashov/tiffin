"""Small terminal setup flows. Every step can be run again without rebuilding the database."""

from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
import plistlib
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote, urlsplit

from .app import App
from .clock import Clock, detected_timezone
from .config import Budget, default_home, load, update_override
from .envfile import with_file_values
from .errors import ConfigError, TelegramConflict
from .http import get
from .model.provider import configured_provider
from .secrets import Secrets
from .telegram.api import TelegramApi

COUNTRY_CURRENCY = {
    "GB": "GBP",
    "IE": "EUR",
    "FR": "EUR",
    "DE": "EUR",
    "NL": "EUR",
    "BE": "EUR",
    "ES": "EUR",
    "IT": "EUR",
    "PT": "EUR",
    "AT": "EUR",
    "FI": "EUR",
    "GR": "EUR",
    "US": "USD",
    "CA": "CAD",
    "AU": "AUD",
    "NZ": "NZD",
    "CH": "CHF",
    "SE": "SEK",
    "NO": "NOK",
    "DK": "DKK",
}
ZONE_COUNTRY = {
    "Europe/London": "GB",
    "Europe/Dublin": "IE",
    "Europe/Paris": "FR",
    "Europe/Berlin": "DE",
    "Europe/Amsterdam": "NL",
    "Europe/Brussels": "BE",
    "Europe/Madrid": "ES",
    "Europe/Rome": "IT",
    "Europe/Lisbon": "PT",
    "Europe/Vienna": "AT",
    "Europe/Helsinki": "FI",
    "Europe/Athens": "GR",
    "Europe/Zurich": "CH",
    "Europe/Stockholm": "SE",
    "Europe/Oslo": "NO",
    "Europe/Copenhagen": "DK",
    "Australia/Sydney": "AU",
    "Australia/Melbourne": "AU",
    "Pacific/Auckland": "NZ",
    "America/Toronto": "CA",
    "America/Vancouver": "CA",
    "America/New_York": "US",
    "America/Chicago": "US",
    "America/Denver": "US",
    "America/Los_Angeles": "US",
}


def _ask(label: str, default: str, *, quiet: bool = False, non_interactive: bool = False) -> str:
    if non_interactive:
        return default
    prompt = (
        f"{label} [already supplied]: "
        if quiet and default
        else f"{label} [{default}]: "
        if default
        else f"{label}: "
    )
    try:
        answer = getpass.getpass(prompt) if quiet and sys.stdin.isatty() else input(prompt)
    except (EOFError, OSError) as exc:
        raise ConfigError(
            "interactive input is unavailable; use --non-interactive and flags"
        ) from exc
    return answer.strip() or default


def _choice(label: str, value: str, choices: tuple[str, ...], *, non_interactive: bool) -> str:
    while True:
        answer = _ask(f"{label} ({'/'.join(choices)})", value, non_interactive=non_interactive)
        if answer in choices:
            return answer
        if non_interactive:
            raise ConfigError(f"{label}: choose one of {', '.join(choices)}")
        print(f"Choose one of: {', '.join(choices)}")


def _app(home: Path, env: str | None = None) -> App:
    config = load(env, root=home)
    return App(
        config,
        Secrets(config.paths.secrets, with_file_values()),
        clock=Clock(str(config.get("env.tz"))),
    )


def _telegram(home: Path, args: argparse.Namespace) -> None:
    print("\nTelegram bot")
    print("1. Open @BotFather in Telegram and send /newbot.")
    print("2. Give the bot a name, then a username ending in bot.")
    print("3. Copy the token BotFather gives you and paste it here.")
    secrets = Secrets(home / "secrets")
    app = _app(home, args.env)
    token_name = "telegram_token" if app.config.is_prod else "telegram_token_dev"
    token = args.telegram_token or os.environ.get(f"TIFFIN_{token_name.upper()}", "")
    token = _ask("Bot token", token, quiet=True, non_interactive=args.non_interactive)
    if not token:
        raise ConfigError("a Telegram bot token is required")
    api = TelegramApi(token, transport=app.transport, clock=app.clock, poll_timeout_s=10)
    bot = api.get_me()
    print(f"Connected to @{bot['username']}.")
    secrets.write(token_name, token)
    db = app.bootstrap()
    try:
        registry = app.registry(db)
        if registry.admin().chat_id is not None:
            print("The owner chat is already linked.")
            return
        if args.owner_chat_id:
            paired = registry.try_pair(
                int(args.owner_chat_id), registry.create_pairing_code("owner")
            )
            if paired is None:
                raise ConfigError("the supplied owner chat ID could not be linked")
            print("Owner chat linked from the supplied chat ID.")
            return
        code = registry.create_pairing_code("owner", ttl_hours=1)
        print(f"Send this one-time code to @{bot['username']}: {code}")
        print("Waiting for your message (up to five minutes)...")
        deadline = time.monotonic() + 300
        offset = 0
        while time.monotonic() < deadline:
            try:
                updates = api.get_updates(offset, timeout_s=10)
            except TelegramConflict as exc:
                raise ConfigError("another poller is using this bot; stop it, then retry") from exc
            for update in updates:
                offset = max(offset, int(update["update_id"]) + 1)
                msg = update.get("message") or {}
                chat = msg.get("chat") or {}
                if chat.get("type") != "private":
                    continue
                if registry.try_pair(int(chat["id"]), str(msg.get("text") or "")) is not None:
                    db.state_set("update_offset", str(offset))
                    print("Owner chat linked.")
                    return
        raise ConfigError("no pairing message arrived; run tiffin connect telegram to try again")
    finally:
        db.close()


def _household(home: Path, args: argparse.Namespace) -> None:
    print("\nHousehold")
    name = _ask(
        "What should the bot call you?",
        args.owner_name or "",
        non_interactive=args.non_interactive,
    )
    if not name or any(ch in name for ch in "<>&\r\n") or len(name) > 60:
        raise ConfigError("name must be 1-60 characters without <, > or &")
    timezone = _ask(
        "Time zone", args.timezone or detected_timezone(), non_interactive=args.non_interactive
    )
    guessed = ZONE_COUNTRY.get(timezone, "GB")
    country = _ask(
        "Country (two-letter code)", args.country or guessed, non_interactive=args.non_interactive
    ).upper()
    if len(country) != 2 or not country.isalpha():
        raise ConfigError("country must be a two-letter code")
    if country != "GB":
        print(
            "The bundled food catalogue uses UK CoFID data. Food names, packs and retailer "
            "links have not been validated for your country; review them before planning."
        )
    currency = (
        args.currency
        or COUNTRY_CURRENCY.get(country)
        or _ask("Currency (three-letter code)", "", non_interactive=args.non_interactive)
    )
    currency = currency.upper()
    if len(currency) != 3 or not currency.isalpha():
        raise ConfigError("currency must be a three-letter code; pass --currency if unattended")
    weight = _choice(
        "Weight unit",
        args.weight_unit or ("lb" if country == "US" else "kg"),
        ("kg", "lb"),
        non_interactive=args.non_interactive,
    )
    height = _choice(
        "Height unit",
        args.height_unit or ("in" if country == "US" else "cm"),
        ("cm", "in"),
        non_interactive=args.non_interactive,
    )
    links = _choice(
        "Would you like product links?",
        args.product_links or "no",
        ("yes", "no"),
        non_interactive=args.non_interactive,
    )
    retailers: list[str] = []
    if links == "yes":
        print("UK readers need Claude CLI, logged-in Chrome and its Claude extension here.")
        selected = _ask(
            "Retailers (comma-separated: sainsburys, marks_spencer, tesco, ocado, wholefoods)",
            args.retailers or "sainsburys",
            non_interactive=args.non_interactive,
        )
        retailers = [value.strip() for value in selected.split(",") if value.strip()]
        supported = {"sainsburys", "marks_spencer", "tesco", "ocado", "wholefoods"}
        if not retailers or any(value not in supported for value in retailers):
            raise ConfigError("choose supported retailer adapters: " + ", ".join(sorted(supported)))
    partner = _choice(
        "Will a partner join later?",
        args.partner or "no",
        ("yes", "no"),
        non_interactive=args.non_interactive,
    )
    update_override(
        home / "tiffin.toml",
        {
            "env": {"tz": timezone, "country": country},
            "persons": {"owner": {"display_name": name}},
            "units": {"weight": weight, "height": height, "currency": currency},
            "household": {"partner_expected": partner == "yes"},
            "shop": {"retailers": retailers, "retailer": retailers[0] if retailers else ""},
            "chrome": {"shop_refresh_enabled": bool(retailers)},
        },
    )
    app = _app(home, args.env)
    db = app.bootstrap()
    try:
        if app.registry(db).admin().display_name != name:
            app.registry(db).set_display_name("owner", name)
    finally:
        db.close()
    if partner == "yes":
        print("Your partner can join later with tiffin pair partner.")


def _model(home: Path, args: argparse.Namespace) -> None:
    print("\nAI model (optional; without one, model tasks stay manual)")
    provider = _choice(
        "Provider",
        args.model_provider or "none",
        ("none", "anthropic", "openai", "openai-compatible", "claude-cli"),
        non_interactive=args.non_interactive,
    )
    model = args.model_name or ""
    base = args.model_base_url or ""
    if provider == "openai-compatible":
        base = _ask("Base URL", base, non_interactive=args.non_interactive)
    if provider not in ("none", "claude-cli"):
        model = _ask("Model name", model, non_interactive=args.non_interactive)
    if provider in ("anthropic", "openai", "openai-compatible"):
        key = _ask(
            "API key (blank if your local server needs none)",
            args.model_key or os.environ.get("TIFFIN_MODEL_API_KEY", ""),
            quiet=True,
            non_interactive=args.non_interactive,
        )
        if key:
            Secrets(home / "secrets").write("model_api_key", key)
    update_override(
        home / "tiffin.toml",
        {
            "model": {
                "provider": provider,
                "model": model,
                "base_url": base,
            }
        },
    )
    if provider == "none":
        print("Model tasks will be offered manually.")
        return
    app = _app(home, args.env)
    backend = configured_provider(app.config, app.secrets, app.transport, clock=app.clock)
    if backend is None:
        raise ConfigError("the selected model could not be configured")
    answer = backend.complete_json(
        "doctor",
        'Return {"ok": true}.',
        {
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
            "additionalProperties": False,
        },
        Budget(20, 1, None),
    )
    if answer.data != {"ok": True}:
        raise ConfigError("the model did not answer the test request")
    print("Model test passed. Each person chooses their own data consent in Telegram.")


def _intervals(home: Path, args: argparse.Namespace) -> None:
    print("\nTraining data (optional)")
    slug = args.person or "owner"
    print(
        "Create a free intervals.icu account. In Settings -> Connections, connect Garmin Connect."
    )
    print("Find your athlete ID and make an API key in Settings -> Developer Settings.")
    athlete = _ask(
        "Athlete ID (blank to skip)", args.icu_athlete or "", non_interactive=args.non_interactive
    )
    if not athlete:
        print("Skipped. Add it later with tiffin connect intervals.")
        return
    if not athlete.isalnum():
        raise ConfigError("athlete ID must contain only letters and numbers")
    key = _ask(
        "intervals.icu API key",
        args.icu_key or os.environ.get(f"TIFFIN_ICU_KEY_{slug.upper()}", ""),
        quiet=True,
        non_interactive=args.non_interactive,
    )
    if not key:
        raise ConfigError("an intervals.icu API key is required with the athlete ID")
    app = _app(home, args.env)
    basic = base64.b64encode(f"API_KEY:{key}".encode()).decode("ascii")
    day = app.clock.today_local().isoformat()
    url = (
        f"{app.config.get('icu.base_url')}/athlete/{quote(athlete)}/wellness"
        f"?oldest={day}&newest={day}"
    )
    response = get(app.transport, url, headers={"Authorization": f"Basic {basic}"}, timeout_s=15)
    if response.status != 200:
        raise ConfigError(f"intervals.icu rejected the details (HTTP {response.status})")
    Secrets(home / "secrets").write(f"icu_key_{slug}", key)
    update_override(home / "tiffin.toml", {"icu": {f"athlete_id_{slug}": athlete}})
    print("Training connection passed.")


def _web(home: Path, args: argparse.Namespace) -> None:
    print("\nWeb page (optional)")
    print("off: use Telegram only. local: browser on this computer at 127.0.0.1.")
    print(
        "tailscale: share securely with Tailscale Serve. proxy: use your own HTTPS reverse proxy."
    )
    mode = _choice(
        "Web mode",
        args.web_mode or "local",
        ("off", "local", "tailscale", "proxy"),
        non_interactive=args.non_interactive,
    )
    origin = args.web_origin or ("http://127.0.0.1:7788" if mode == "local" else "")
    if mode in ("tailscale", "proxy"):
        origin = _ask("Public HTTPS URL", origin, non_interactive=args.non_interactive)
        if not origin.startswith("https://"):
            raise ConfigError("a public web URL must begin with https://")
    parsed = urlsplit(origin)
    if mode != "off" and (not parsed.hostname or parsed.query or parsed.fragment):
        raise ConfigError("web URL needs a host and no query or fragment")
    root_path = parsed.path.rstrip("/") if mode != "off" else ""
    if mode == "tailscale" and not root_path:
        root_path = "/tiffin"
    web: dict[str, object] = {
        "enabled": mode != "off",
        "identity_mode": "header" if mode == "tailscale" else "telegram",
        "trusted_header_proxy": mode == "tailscale",
        "origin": f"{parsed.scheme}://{parsed.netloc}" if mode != "off" else "",
        "root_path": root_path,
    }
    if mode == "local":
        try:
            port = parsed.port
        except ValueError as exc:
            raise ConfigError("web URL has an invalid port") from exc
        if port is not None:
            web["port_prod"] = port
    update_override(
        home / "tiffin.toml",
        {"web": web},
    )
    port = _app(home, args.env).config.web_port
    if mode == "tailscale":
        print(f"In Tailscale Serve, route {root_path} to http://127.0.0.1:{port}.")
        print("Serve supplies the Tailscale-User-Login header used for sign-in.")
    elif mode == "proxy":
        print(f"Route {root_path or '/'} on your HTTPS proxy to http://127.0.0.1:{port}.")
        print("The proxy must preserve the public URL so Telegram sign-in links open correctly.")


def _finish(home: Path, args: argparse.Namespace) -> None:
    print("\nSetup complete.")
    print(f"Home: {home}")
    print("Send /start to your bot. Build the food catalogue with tiffin catalogue build.")
    service = _choice(
        "Run now as a background service or later in the foreground?",
        args.run_mode or "foreground",
        ("foreground", "service"),
        non_interactive=args.non_interactive,
    )
    if service == "service":
        _install_service(home, env=args.env)
    else:
        current_home = Path(os.environ.get("TIFFIN_HOME") or default_home(os.environ)).expanduser()
        if current_home != home:
            if sys.platform == "win32":
                print(f'Set the home for this terminal: $env:TIFFIN_HOME="{home}"')
            else:
                print(f"Set the home for this terminal: export TIFFIN_HOME='{home}'")
        executable = (
            "uv run tiffin" if Path(sys.executable).parent.parent.name == ".venv" else "tiffin"
        )
        environment = f" --env {args.env}" if args.env else ""
        print(f"Run in the foreground with: {executable}{environment} run")


def _install_service(home: Path, *, env: str | None = None) -> None:
    """Install the packaged service shape for the current user, with this Python and home."""
    command = [sys.executable, "-m", "tiffin.cli"]
    if env:
        command += ["--env", env]
    command.append("run")
    invocation = " ".join(f'"{part}"' if " " in part else part for part in command)
    try:
        if sys.platform == "win32":
            script = home / "state" / "run-tiffin.cmd"
            script.parent.mkdir(parents=True, exist_ok=True)
            script.write_text(
                f'@echo off\r\nset "TIFFIN_HOME={home}"\r\n' + invocation + "\r\n",
                encoding="utf-8",
            )
            subprocess.run(
                [
                    "schtasks",
                    "/Create",
                    "/F",
                    "/SC",
                    "ONLOGON",
                    "/TN",
                    "tiffin",
                    "/TR",
                    str(script),
                ],
                check=True,
            )
            subprocess.run(["schtasks", "/Run", "/TN", "tiffin"], check=True)
        elif sys.platform == "darwin":
            directory = Path.home() / "Library" / "LaunchAgents"
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / "org.example.tiffin.plist"
            payload = {
                "Label": "org.example.tiffin",
                "ProgramArguments": command,
                "EnvironmentVariables": {"TIFFIN_HOME": str(home)},
                "RunAtLoad": True,
                "KeepAlive": True,
            }
            path.write_bytes(plistlib.dumps(payload))
            subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)], check=True)
        else:
            directory = Path.home() / ".config" / "systemd" / "user"
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / "tiffin.service"
            path.write_text(
                "[Unit]\nDescription=Tiffin\nAfter=network-online.target\n"
                "[Service]\nType=simple\n"
                f'Environment="TIFFIN_HOME={home}"\n'
                f"ExecStart={invocation}\n"
                "Restart=on-failure\nRestartSec=10\n"
                "[Install]\nWantedBy=default.target\n",
                encoding="utf-8",
            )
            subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
            subprocess.run(["systemctl", "--user", "enable", "--now", "tiffin"], check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ConfigError(f"background service could not be installed: {exc}") from exc
    marker = home / "state" / "service.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"installed": True, "platform": sys.platform}), encoding="utf-8")
    print("Background service installed and started.")


def run(args: argparse.Namespace) -> int:
    home = Path(args.home or os.environ.get("TIFFIN_HOME") or default_home(os.environ)).expanduser()
    if not home.is_absolute():
        raise ConfigError("home must be an absolute path")
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    if args.command == "init":
        if not (home / "tiffin.toml").exists():
            update_override(home / "tiffin.toml", {"persons": {"owner": {"display_name": ""}}})
        _telegram(home, args)
        _household(home, args)
        _model(home, args)
        _intervals(home, args)
        _web(home, args)
        load(args.env, root=home)
        _finish(home, args)
    else:
        {"telegram": _telegram, "model": _model, "intervals": _intervals, "web": _web}[args.target](
            home, args
        )
        load(args.env, root=home)
    return 0


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--home", help="absolute data directory (or TIFFIN_HOME)")
    parser.add_argument("--non-interactive", action="store_true", help="use flags and environment")
    for flag in (
        "telegram-token",
        "owner-name",
        "weight-unit",
        "height-unit",
        "timezone",
        "country",
        "currency",
        "product-links",
        "retailers",
        "partner",
        "model-provider",
        "model-name",
        "model-key",
        "model-base-url",
        "icu-athlete",
        "icu-key",
        "web-mode",
        "web-origin",
        "run-mode",
    ):
        parser.add_argument("--" + flag)
    parser.add_argument("--owner-chat-id", type=int, help="existing owner chat ID for automation")
    parser.add_argument(
        "--person",
        choices=["owner", "partner"],
        default="owner",
        help="person to connect to intervals.icu (default: owner)",
    )
