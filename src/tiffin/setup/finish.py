"""After saving: pairing for a script, the food list, the background service, the same checks
as `tiffin doctor` and what to do next."""

from __future__ import annotations

import json
import os
import plistlib
import subprocess
import sys
from pathlib import Path

from ..app import App
from ..config import REPO_ROOT, default_home
from ..errors import ConfigError
from ..term import plural
from .ask import Prompter
from .state import State
from .steps import _api, _new_code, _pair_by_id, _running, _wait_and_say, web_mode


def _foods(app: App) -> int:
    db = app.open_db()
    try:
        return int(db.scalar("SELECT COUNT(*) FROM ingredients") or 0)
    finally:
        db.close()


def _build_catalogue(app: App) -> int:
    from ..food import catalogue  # noqa: PLC0415 (the food layer is large)

    db = app.bootstrap()
    try:
        catalogue.build(
            db,
            app.config.paths.config_dir / "ingredients.yaml",
            REPO_ROOT / "data" / "cofid" / "cofid_2021_proximates.csv",
        )
        return len(catalogue.latest_versions(db))
    finally:
        db.close()


def _pair_unattended(s: State, p: Prompter) -> None:
    """--non-interactive: pair after saving, so a missing pairing never blocks the settings."""
    a = s.args
    if not s.pairing_due or s.saved.paired.get("owner") or not s.secret(s.token_name):
        return
    if a.owner_chat_id:
        _pair_by_id(s, int(a.owner_chat_id))
        p.result("ok", "Your chat is paired, from --owner-chat-id.")
        return
    if a.pair == "later":
        if s.env != "prod":
            raise ConfigError("--pair later needs production mode; development pairs only here")
        s.codes["owner"] = _new_code(s, "owner", 48)
        return
    token = s.secret(s.token_name) or ""
    if not s.bot:
        s.bot = str(_api(s, token).get_me().get("username") or "")
    if not _wait_and_say(s, p, token, "owner"):
        raise ConfigError(
            "settings are saved, but your chat is not paired: run tiffin setup telegram to try "
            "again, or tiffin setup --non-interactive --pair later"
        )


def _start_lines(s: State) -> list[str]:
    if s.container:
        return ["Start the container, for example with docker compose up -d."]
    if s.service_installed:
        return ["Tiffin is running in the background, and starts again when you log in."]
    lines = []
    usual = Path(s.plain_env.get("TIFFIN_HOME") or default_home(s.plain_env)).expanduser()
    if usual != s.home:
        command = (
            f'$env:TIFFIN_HOME="{s.home}"'
            if sys.platform == "win32"
            else f"export TIFFIN_HOME='{s.home}'"
        )
        lines.append(f"Tell this terminal where Tiffin's folder is: {command}")
    tiffin = "uv run tiffin" if Path(sys.executable).parent.parent.name == ".venv" else "tiffin"
    env = f" --env {s.args.env}" if s.args.env else ""
    lines.append(f"Start Tiffin: {tiffin}{env} run")
    return lines


def _next_steps(s: State, foods: int) -> list[str]:
    bot = f"@{s.bot}" if s.bot else "your bot"
    if _running(s):
        lines = [
            "Restart the container (docker compose restart) so Tiffin uses the new settings."
            if s.container
            else "Restart Tiffin (stop it, then start it again) so it uses the new settings."
        ]
    else:
        lines = _start_lines(s)
    for slug in [slug for slug in s.codes if s.saved.paired.get(slug)]:
        del s.codes[slug]  # made, then paired another way: nothing left to send
    if "owner" in s.codes:
        lines.append(
            f"Send {s.codes['owner']} to {bot} once Tiffin is running, to pair your chat. It "
            "works once, for 48 hours; for a new one, run tiffin pair owner."
        )
    if s.fresh or "owner" in s.paired_now or "owner" in s.codes:
        lines.append(
            f"In Telegram, send /start to {bot}. It asks a few questions to set your targets."
        )
    if "partner" in s.codes:
        lines.append(
            f"Give your partner the code {s.codes['partner']} to send to {bot} once Tiffin is "
            "running. It works once, for 48 hours; for a new one, run tiffin pair partner."
        )
    elif (
        s.ran & {"people", "telegram"}
        and s.two()
        and not s.saved.paired.get("partner")
        and s.env == "prod"
    ):
        lines.append(
            f"When your partner is ready, run tiffin pair partner and they send the code to {bot}."
        )
    mode = web_mode(s)
    if "web" in s.ran and mode == "local":
        address = f"{s.get('web.origin')}{s.get('web.root_path')}/"
        lines.append(f"For the web page, open {address} and send /web to {bot} to sign in.")
    elif "web" in s.ran and mode in ("proxy", "tailscale"):
        lines.append(
            f"For the web page, send /web to {bot}: it replies with a link that signs you in."
        )
    if not foods:
        lines.append("Load Tiffin's food list: tiffin catalogue build")
    lines.append(
        "Check on Tiffin any time with tiffin doctor, and change a setting with tiffin setup."
    )
    return lines


def _checks(s: State, p: Prompter, app: App) -> int:
    """tiffin doctor's checks, without a second model request; what cannot be checked until
    Tiffin first runs is one line. Returns how many failed."""
    from .. import doctor  # noqa: PLC0415 (doctor pulls in every job)
    from ..heartbeat import Heartbeat  # noqa: PLC0415

    checks = doctor.run_checks(app, online=True, model_probe=False)
    for check in checks:
        if check.name == "Telegram API" and check.status == "ok" and not s.bot:
            s.bot = check.detail.removeprefix("@")
    asleep = Heartbeat(app.config.paths.state, app.clock).age_s() is None
    # what only a running Tiffin can show: its heartbeat, its lock, its jobs and backups
    later = {"process", "poller", "service", "backup", "expected messages", "jobs"}
    shown = [
        c
        for c in checks
        if not (asleep and (c.name in later or c.detail == doctor.NEVER_RUN))
        and not (asleep and c.name in ("web", "intervals.icu") and c.status != "red")
    ]
    for check in shown:
        if check.name != "chats" or check.status == "ok":
            continue
        if "owner" in s.codes and check.status == "red":
            check.status, check.detail = "info", "yours pairs when you send your code (below)"
        elif "partner" in s.codes and check.status == "warn":
            check.status, check.detail = "info", "your partner pairs with their code (below)"
    provider = str(app.config.get("model.provider"))
    if provider == "none":
        shown.append(doctor.Check("model", "ok", "none; menus are yours to make"))
    elif s.model_tested:
        shown.append(doctor.Check("model", "ok", "answered during setup"))
    elif s.model_tested is False:
        shown.append(doctor.Check("model", "warn", "did not answer during setup"))
    else:
        shown.append(
            doctor.Check("model", "info", "not checked; tiffin doctor sends one tiny request")
        )
    p.say("")
    p.say(p.style.heading("Checking everything"))
    for line in doctor.check_lines(shown, s.style, width=p.width):
        p.say(line)
    if asleep:
        p.result(
            "info",
            "Tiffin is not running yet. Once it is, tiffin doctor also checks that it keeps "
            "running, its scheduled jobs, its backups and the web page.",
        )
    return sum(1 for c in shown if c.status == "red")


def _finish(s: State, p: Prompter, *, saved: bool) -> int:
    a = s.args
    if saved and not p.interactive:
        _pair_unattended(s, p)
    app = s.app()
    foods = _foods(app) if app.config.paths.db.exists() else 0
    if saved and s.full and not foods:
        if p.interactive:
            p.say("")
            p.explain("Tiffin's food list is empty, and menus and shopping lists need it.")
        if p.confirm("Load the food list that comes with Tiffin now", not a.no_catalogue):
            foods = _build_catalogue(app)
            p.result("ok", f"Loaded the food list: {plural(foods, 'food')}.")
    installed = (app.config.paths.state / "service.json").is_file()
    if saved and s.full and not s.container and not installed:
        want = (
            p.confirm("Start Tiffin in the background whenever you log in to this computer", False)
            if p.interactive
            else a.run_mode == "service"
        )
        if want:
            (s.deps.install_service or _install_service)(s.home, a.env)
            s.service_installed = True
            p.result("ok", "Tiffin is running in the background.")
    failures = _checks(s, p, app)
    if saved:
        p.say("")
        p.say(p.style.heading("What next"))
        for number, line in enumerate(_next_steps(s, foods), 1):
            wrapped = p.wrap(line, p.width - 5)
            p.say(f"  {number}. {wrapped[0]}")
            for rest in wrapped[1:]:
                p.say("     " + rest)
    p.say("")
    if failures:
        p.result(
            "warn",
            f"{'Saved' if saved else 'Checked'}, but {plural(failures, 'check')} failed (above). "
            "Put it right, then run tiffin doctor.",
        )
    else:
        p.result("ok", "Tiffin is set up." if saved else "Everything checked.")
    return 0


def _install_service(home: Path, env: str | None = None) -> None:
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
