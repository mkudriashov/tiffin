"""Portable health report for a self-hosted tiffin instance.

Three ways to print it. Plain text, when the output is not a terminal, is one line per check,
`{pass|warn|fail:<4} {name:<16} {detail}`, then `doctor: ok` or `doctor: FAIL`; scripts read the
first two words of each line and the exit code, so that format does not change. A terminal gets
the same checks grouped, with marks and aligned, wrapped columns. `--json` prints them as JSON.
Exit code 1 when a check fails (or, with --strict, warns), 0 otherwise, whatever the format.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import textwrap
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from . import expected
from .app import App
from .config import Budget, public_https_origin
from .db import SCHEMA_VERSION, Db
from .heartbeat import Heartbeat
from .http import get
from .jobs import all_jobs
from .model.provider import configured_provider
from .term import Style, plural


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""


#: the plain-text words for each status; scripts read these
LABELS = {"ok": "pass", "warn": "warn", "red": "fail"}
#: the detail of a scheduled job that has never run
NEVER_RUN = "never run; start tiffin run"
#: how the terminal report groups the checks; anything not named is a scheduled job
GROUPS: tuple[tuple[str, frozenset[str]], ...] = (
    (
        "Set-up",
        frozenset(
            {"config", "home", "Telegram token", "database", "chats", "consent wording", "disk"}
        ),
    ),
    (
        "Connections",
        frozenset(
            {
                "Telegram API",
                "web",
                "web URL",
                "intervals key",
                "intervals owner",
                "intervals partner",
                "model",
            }
        ),
    ),
    (
        "Running",
        frozenset(
            {"process", "poller", "service", "backup", "jobs", "intervals.icu", "expected messages"}
        ),
    ),
)


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


_CONTAINER_MARKERS = ("/.dockerenv", "/run/.containerenv")
_CGROUP_WORDS = ("docker", "kubepods", "containerd", "libpod", "podman")


def container_runtime(
    environ: Mapping[str, str] | None = None,
    exists: Callable[[str], bool] = os.path.exists,
    read_cgroup: Callable[[], str] | None = None,
) -> str | None:
    """The container runtime this process seems to run in, or None.

    None means "no sign of one", not "certainly none": a runtime that leaves no marker
    (and no `TIFFIN_CONTAINER` variable) cannot be told from a bare machine, and the report
    says so rather than guess. Only Linux is looked at.
    """
    env = os.environ if environ is None else environ
    declared = env.get("TIFFIN_CONTAINER", "").strip()
    if declared and declared not in ("0", "false", "no"):
        return declared if declared not in ("1", "true", "yes") else "container"
    if sys.platform != "linux" and read_cgroup is None:
        return None
    for marker in _CONTAINER_MARKERS:
        if exists(marker):
            return "podman" if "containerenv" in marker else "docker"
    try:
        text = read_cgroup() if read_cgroup else Path("/proc/1/cgroup").read_text(encoding="utf-8")
    except OSError:
        return None
    lowered = text.lower()
    return next((w for w in _CGROUP_WORDS if w in lowered), None)


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


def run_checks(app: App, *, online: bool = True, model_probe: bool = True) -> list[Check]:
    """Every check, in report order. `model_probe=False` leaves out the model check, which sends
    a request; `tiffin setup` uses it after testing the model itself."""
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
        checks.append(Check("home", "warn", "missing; run tiffin setup"))
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
                healthy = integrity and version == SCHEMA_VERSION
                checks.append(
                    Check(
                        "database",
                        "ok" if healthy else "red",
                        "sound and up to date"
                        if healthy
                        else f"integrity={integrity} schema={version}/{SCHEMA_VERSION}; "
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
                status_word, line = expected.summary(
                    expected.missing(db, app.registry(db), all_jobs(app.config), app.clock)
                )
                checks.append(Check("expected messages", status_word, line))
                for job in all_jobs(app.config):
                    last = db.one(
                        "SELECT status, started_at FROM run_ledger WHERE job = ? "
                        "ORDER BY started_at DESC LIMIT 1",
                        (job.name,),
                    )
                    if last is None:
                        checks.append(Check(job.name, "warn", NEVER_RUN))
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
                        "owner missing; run tiffin setup telegram"
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
    runtime = container_runtime()
    if runtime is not None:
        # inside a container the runtime is the supervisor: there is no service for setup to
        # install, and asking systemd or launchd from in here would only ever say no
        checks.append(
            Check(
                "service",
                "ok" if healthy_poller else "warn",
                f"running in a {runtime} container; its restart policy is the supervisor"
                if healthy_poller
                else f"in a {runtime} container, but no healthy locked poller; read the "
                "container's logs",
            )
        )
    else:
        service = paths.state / "service.json"
        installed = service.is_file()
        service_running = installed and _service_running() and healthy_poller
        checks.append(
            Check(
                "service",
                "ok" if service_running else "warn",
                "installed and running"
                if service_running
                else "no setup-installed service found. If a container runtime or another "
                "supervisor starts tiffin, this report cannot tell; check it there. "
                "Otherwise run tiffin setup or tiffin run",
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
        checks.append(Check("disk", "warn", "home missing; run tiffin setup"))
    if public_https_origin(str(app.config.get("web.origin", ""))):
        records = str(app.config.get("hosting.records", "this computer")).strip()
        front = str(app.config.get("hosting.web_front", "")).strip()
        if records == "this computer" or not front:
            checks.append(
                Check(
                    "consent wording",
                    "warn",
                    "web.origin is a public https address, but hosting.records still says "
                    "'this computer' or hosting.web_front is empty, so the consent text would "
                    "tell people where their records are held wrongly; set both in tiffin.toml "
                    "or with tiffin setup web",
                )
            )
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
                        Check("intervals key", "warn", "not connected; tiffin setup training")
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
                        "valid" if response.status == 200 else "reconnect: tiffin setup training",
                    )
                )
            except Exception:
                checks.append(Check(f"intervals {slug}", "red", "reconnect: tiffin setup training"))
        if not model_probe:
            return checks
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


def failed(checks: Sequence[Check], *, strict: bool = False) -> bool:
    return any(c.status == "red" or (strict and c.status == "warn") for c in checks)


def plain_lines(checks: Sequence[Check], *, strict: bool = False) -> list[str]:
    """The format scripts read: unchanged since the first release."""
    lines = [f"{LABELS[c.status]:<4} {c.name:<16} {c.detail}" for c in checks]
    lines.append("doctor: " + ("FAIL" if failed(checks, strict=strict) else "ok"))
    return lines


def json_text(checks: Sequence[Check], *, strict: bool = False) -> str:
    return json.dumps(
        {
            "ok": not failed(checks, strict=strict),
            "strict": strict,
            "checks": [
                {"name": c.name, "status": LABELS[c.status], "detail": c.detail} for c in checks
            ],
        },
        indent=1,
    )


def check_lines(
    checks: Sequence[Check], style: Style, *, width: int | None = None, name_width: int = 0
) -> list[str]:
    """One check a line: mark, name, detail, with long details wrapped under their column."""
    width = width or shutil.get_terminal_size((100, 24)).columns
    name_width = max([name_width, *(len(c.name) for c in checks)])
    lead = 2 + 1 + 2 + name_width + 2
    room = max(30, width - lead - 1)
    status_of = {"ok": "ok", "warn": "warn", "red": "fail"}
    out = []
    for check in checks:
        # a path or an address is never broken in two, even where it has a hyphen
        pieces = textwrap.wrap(
            check.detail, room, break_on_hyphens=False, break_long_words=False
        ) or [""]
        mark = style.mark(status_of.get(check.status, check.status))
        out.append(f"  {mark}  {check.name.ljust(name_width)}  {pieces[0]}".rstrip())
        out.extend(" " * lead + piece for piece in pieces[1:])
    return out


def grouped(checks: Sequence[Check]) -> list[tuple[str, list[Check]]]:
    """The checks under their headings. Scheduled jobs that have never run become one line."""
    named = {name for _, names in GROUPS for name in names}
    sections = [(title, [c for c in checks if c.name in names]) for title, names in GROUPS]
    jobs = [c for c in checks if c.name not in named]
    if jobs and all(c.detail == NEVER_RUN for c in jobs):
        count = plural(len(jobs), "scheduled job")
        jobs = [Check("not run yet", "warn", f"{count}; they start with tiffin run")]
    sections.append(("Scheduled jobs", jobs))
    return [(title, items) for title, items in sections if items]


def terminal_lines(
    checks: Sequence[Check],
    style: Style,
    *,
    strict: bool = False,
    context: str = "",
    width: int | None = None,
) -> list[str]:
    """Grouped, marked and aligned, for a person."""
    sections = grouped(checks)
    name_width = max((len(c.name) for _, items in sections for c in items), default=0)
    out = [style.heading("Tiffin health check")]
    if context:
        out.append(style.dim(context))
    for title, items in sections:
        out.append("")
        out.append(style.bold(title))
        out.extend(check_lines(items, style, width=width, name_width=name_width))
    counts = {s: sum(1 for c in checks if c.status == s) for s in ("ok", "warn", "red")}
    out.append("")
    if failed(checks, strict=strict):
        bad = counts["red"] + (counts["warn"] if strict else 0)
        parts = [plural(bad, "check") + " failed"]
        if counts["warn"] and not strict:
            parts.append(f"{counts['warn']} to look at")
        parts.append(f"{counts['ok']} fine")
        verdict = style.line("fail", ", ".join(parts) + ".")
        if strict and counts["warn"]:
            verdict += style.dim(" (--strict counts warnings as failures)")
    elif counts["warn"]:
        verdict = style.line("warn", f"{counts['ok']} fine, {counts['warn']} to look at.")
    else:
        verdict = style.line("ok", f"All {plural(len(checks), 'check')} fine.")
    out.append(verdict)
    return out


def report(
    checks: Sequence[Check],
    *,
    strict: bool = False,
    as_json: bool = False,
    out: TextIO | None = None,
    style: Style | None = None,
    context: str = "",
) -> int:
    """Print the checks in the format that suits the reader; the exit code is the same in all."""
    out = sys.stdout if out is None else out
    style = Style.detect(out) if style is None else style
    if as_json:
        lines = [json_text(checks, strict=strict)]
    elif style.terminal:
        lines = terminal_lines(checks, style, strict=strict, context=context)
    else:
        lines = plain_lines(checks, strict=strict)
    for line in lines:
        print(line, file=out)
    return int(failed(checks, strict=strict))


def main(
    app: App,
    *,
    strict: bool = False,
    online: bool = True,
    as_json: bool = False,
    out: TextIO | None = None,
    style: Style | None = None,
) -> int:
    out = sys.stdout if out is None else out
    style = Style.detect(out) if style is None else style
    mode = "production" if app.config.is_prod else "development"
    context = f"{app.config.paths.root} {style.dot} {mode}, version {app.version}"
    return report(
        run_checks(app, online=online),
        strict=strict,
        as_json=as_json,
        out=out,
        style=style,
        context=context,
    )
