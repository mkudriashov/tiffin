"""Portable health report for a self-hosted tiffin instance."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .app import App
from .config import Budget
from .db import SCHEMA_VERSION, Db
from .heartbeat import Heartbeat
from .http import get
from .jobs import all_jobs
from .model.provider import configured_provider


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""


def _listening(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _lock_held(path: Path) -> bool:
    """Inspect the run lock without keeping it or disturbing the poller."""
    if not path.exists():
        return False
    try:
        with path.open("a+b") as handle:
            if os.name == "nt":
                import msvcrt  # noqa: PLC0415

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl  # noqa: PLC0415

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        return True
    return False


def _service_running() -> bool:
    if sys.platform == "win32":
        command = ["schtasks", "/Query", "/TN", "tiffin", "/FO", "LIST"]
    elif sys.platform == "darwin":
        command = ["launchctl", "print", f"gui/{os.getuid()}/org.example.tiffin"]
    else:
        command = ["systemctl", "--user", "is-active", "--quiet", "tiffin"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=3, check=False)
    except OSError, subprocess.TimeoutExpired:
        return False
    if sys.platform == "win32":
        return result.returncode == 0 and "Running" in result.stdout
    return result.returncode == 0


def _icu_check(db: Db, persons: list) -> Check:
    problems: list[str] = []
    status = "ok"
    for person in persons:
        row = db.one(
            "SELECT status FROM run_ledger WHERE person_id = ? AND job = 'icu_sync' "
            "ORDER BY started_at DESC LIMIT 1",
            (person.id,),
        )
        if row is None or row["status"] == "ok":
            continue
        problems.append(f"{person.slug} intervals.icu: {row['status']}")
        status = "red" if row["status"] == "failed" else "warn" if status != "red" else status
    return Check("intervals.icu", status, "; ".join(problems) or "no recent failures")


def _model_cost(provider: str) -> str:
    if provider == "claude":
        return "one tiny request against the Claude subscription"
    if provider == "openai-compatible":
        return "one tiny request; local servers are free, hosted servers may charge"
    return "one tiny request billed by provider token usage"


def run_checks(app: App, *, online: bool = True) -> list[Check]:
    paths = app.config.paths
    checks = [Check("config", "ok", "tiffin.toml and packaged defaults are valid")]
    if paths.root.is_dir():
        try:
            with tempfile.TemporaryFile(dir=paths.root):
                pass
            checks.append(Check("home", "ok", str(paths.root)))
        except OSError:
            checks.append(Check("home", "red", "not writable; choose a writable TIFFIN_HOME"))
    else:
        checks.append(Check("home", "warn", "missing; run tiffin init"))
    token = "telegram_token" if app.config.is_prod else "telegram_token_dev"
    checks.append(
        Check(
            "Telegram token",
            "ok" if app.secrets.exists(token) else "red",
            "present"
            if app.secrets.exists(token)
            else f"set TIFFIN_{token.upper()} or create {app.secrets.path(token)}",
        )
    )
    if online and app.secrets.exists(token):
        try:
            bot = app.api().get_me()
            checks.append(Check("Telegram API", "ok", f"@{bot['username']}"))
        except Exception as exc:
            checks.append(Check("Telegram API", "red", f"{exc}; check the bot token"))
    if paths.db.exists():
        try:
            db = Db(paths.db)
            try:
                integrity = db.integrity_ok()
                version = db.scalar("SELECT MAX(version) FROM schema_version")
                checks.append(
                    Check(
                        "database",
                        "ok" if integrity and version == SCHEMA_VERSION else "red",
                        f"integrity={integrity} schema={version}/{SCHEMA_VERSION}; "
                        "run tiffin run to migrate",
                    )
                )
                rows = db.all(
                    "SELECT job, status, detail FROM run_ledger WHERE rowid IN "
                    "(SELECT MAX(rowid) FROM run_ledger GROUP BY job, person_id) "
                    "AND status IN ('failed', 'degraded') ORDER BY started_at DESC LIMIT 5"
                )
                if rows:
                    checks.append(
                        Check(
                            "jobs",
                            "red" if any(r["status"] == "failed" for r in rows) else "warn",
                            "; ".join(f"{r['job']}: {r['status']}" for r in rows),
                        )
                    )
                else:
                    checks.append(Check("jobs", "ok", "no recent failures"))
                checks.append(_icu_check(db, app.registry(db).active()))
                for job in all_jobs(app.config):
                    last = db.one(
                        "SELECT status, started_at FROM run_ledger WHERE job = ? "
                        "ORDER BY started_at DESC LIMIT 1",
                        (job.name,),
                    )
                    if last is None:
                        checks.append(Check(job.name, "warn", "never run; start tiffin run"))
                    else:
                        status = str(last["status"])
                        checks.append(
                            Check(
                                job.name,
                                "red"
                                if status == "failed"
                                else "warn"
                                if status == "degraded"
                                else "ok",
                                f"{status} at {last['started_at']} UTC"
                                + (
                                    "; check tiffin status"
                                    if status in ("failed", "degraded")
                                    else ""
                                ),
                            )
                        )
                paired = [p.slug for p in app.registry(db).active() if p.chat_id is not None]
                partner_waiting = (
                    app.config.get("household.partner_expected", False) and "partner" not in paired
                )
                checks.append(
                    Check(
                        "chats",
                        "red" if "owner" not in paired else "warn" if partner_waiting else "ok",
                        "owner missing; run tiffin connect telegram"
                        if "owner" not in paired
                        else "partner not paired; run tiffin pair partner"
                        if partner_waiting
                        else ", ".join(paired),
                    )
                )
            finally:
                db.close()
        except Exception as exc:
            checks.append(Check("database", "red", f"{type(exc).__name__}: {exc}"))
    else:
        checks.append(Check("database", "warn", "not created yet; run tiffin run"))
    age = Heartbeat(paths.state, app.clock).age_s()
    stale_after = int(app.config.get("ops.heartbeat_stale_s", 180))
    checks.append(
        Check(
            "process",
            "warn" if age is None else "ok" if age < stale_after else "red",
            "not running" if age is None else f"heartbeat {age:.0f} s ago",
        )
    )
    locked = _lock_held(paths.state / "tiffin.lock")
    healthy_poller = locked and age is not None and age < stale_after
    checks.append(
        Check(
            "poller",
            "ok" if healthy_poller else "warn",
            "one live process holds the lock"
            if healthy_poller
            else "no healthy locked poller; start tiffin run or inspect its logs",
        )
    )
    service = paths.state / "service.json"
    installed = service.is_file()
    service_running = installed and _service_running() and healthy_poller
    checks.append(
        Check(
            "service",
            "ok" if service_running else "warn",
            "installed and running"
            if service_running
            else "not running as a setup-installed service; run tiffin init or tiffin run",
        )
    )
    last_path = paths.state / "backup_last.json"
    if last_path.exists():
        try:
            last = json.loads(last_path.read_text(encoding="utf-8"))
            fresh = last.get("day") == app.clock.today_local().isoformat()
            checks.append(
                Check(
                    "backup",
                    "ok" if last.get("ok") and fresh else "warn",
                    f"day={last.get('day')} ok={last.get('ok')}",
                )
            )
        except (OSError, ValueError) as exc:
            checks.append(Check("backup", "warn", str(exc)))
    else:
        checks.append(Check("backup", "warn", "no backup yet"))
    if paths.root.exists():
        free_gb = shutil.disk_usage(paths.root).free / 1e9
        checks.append(
            Check(
                "disk",
                "ok" if free_gb > 2 else "warn",
                f"{free_gb:.1f} GB free" + ("; free space for backups" if free_gb <= 2 else ""),
            )
        )
    else:
        checks.append(Check("disk", "warn", "home missing; run tiffin init"))
    if online:
        if app.config.get("web.enabled", True):
            port = app.config.web_port
            listening = _listening(port)
            checks.append(
                Check(
                    "web",
                    "ok" if listening else "warn",
                    f"127.0.0.1:{port}" + ("; start tiffin run" if not listening else ""),
                )
            )
            if listening:
                origin = str(app.config.get("web.origin", "")).rstrip("/")
                root = str(app.config.get("web.root_path", "")).rstrip("/")
                try:
                    response = get(app.transport, f"{origin}{root}/api/health", timeout_s=5)
                    checks.append(
                        Check(
                            "web URL",
                            "ok" if response.status == 200 else "red",
                            origin if response.status == 200 else "check the proxy URL and route",
                        )
                    )
                except Exception:
                    checks.append(Check("web URL", "red", "check the configured proxy URL"))
        else:
            checks.append(Check("web", "ok", "off by choice"))
        for slug in ("owner", "partner"):
            key = app.secrets.icu_key(slug)
            if not key:
                if slug == "owner":
                    checks.append(
                        Check("intervals key", "warn", "not connected; tiffin connect intervals")
                    )
                continue
            try:
                import base64  # noqa: PLC0415

                auth = base64.b64encode(f"API_KEY:{key}".encode()).decode()
                day = app.clock.today_local().isoformat()
                response = get(
                    app.transport,
                    f"{app.config.get('icu.base_url')}/athlete/0/wellness?oldest={day}&newest={day}",
                    headers={"Authorization": f"Basic {auth}"},
                    timeout_s=10,
                )
                checks.append(
                    Check(
                        f"intervals {slug}",
                        "ok" if response.status == 200 else "red",
                        "valid"
                        if response.status == 200
                        else "reconnect: tiffin connect intervals",
                    )
                )
            except Exception:
                checks.append(
                    Check(f"intervals {slug}", "red", "reconnect: tiffin connect intervals")
                )
        try:
            provider = configured_provider(app.config, app.secrets, app.transport, clock=app.clock)
            if provider is None:
                checks.append(Check("model", "warn", "none; model tasks are manual; no charge"))
            elif provider.name != "claude" and app.config.get("models.cap_usd", False):
                checks.append(
                    Check(
                        "model",
                        "red",
                        "API USD cap cannot be enforced; model tasks are manual; no probe sent",
                    )
                )
            else:
                result = provider.complete_json(
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
                if result.data != {"ok": True}:
                    raise ValueError("provider did not return the expected probe answer")
                checks.append(
                    Check("model", "ok", f"{provider.name}; {_model_cost(provider.name)}")
                )
        except Exception as exc:
            configured = str(app.config.get("model.provider"))
            detail = f"{type(exc).__name__}: {exc}; {_model_cost(configured)}"
            checks.append(Check("model", "red", detail))
    return checks


def main(app: App, *, strict: bool = False, online: bool = True) -> int:
    checks = run_checks(app, online=online)
    failed = False
    for check in checks:
        colour = {"ok": "\x1b[32m", "warn": "\x1b[33m", "red": "\x1b[31m"}[check.status]
        label = {"ok": "pass", "warn": "warn", "red": "fail"}[check.status]
        status = f"{colour}{label}\x1b[0m" if sys.stdout.isatty() else label
        print(f"{status:<4} {check.name:<16} {check.detail}")
        failed |= check.status == "red" or (strict and check.status == "warn")
    print("doctor:", "FAIL" if failed else "ok")
    return int(failed)
