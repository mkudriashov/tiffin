"""Doctor in a container, the shop-reader probe off Windows, secrets from files, the health check
and the Docker files as text (a Linux run proves what text cannot)."""

from __future__ import annotations

import re
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from tiffin import config as cfg
from tiffin import doctor, healthcheck
from tiffin.app import App
from tiffin.envfile import with_file_values
from tiffin.errors import ConfigError
from tiffin.heartbeat import Heartbeat
from tiffin.integrations import chrome_job
from tiffin.secrets import Secrets

ROOT = Path(__file__).resolve().parents[1]
NL = chr(10)
BOM = chr(0xFEFF)


# -- recognising a container ---------------------------------------------------------------------
def _no(_path: str) -> bool:
    return False


def test_the_declared_variable_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.sys, "platform", "linux")
    assert doctor.container_runtime({"TIFFIN_CONTAINER": "1"}, _no, lambda: "") == "container"
    assert doctor.container_runtime({"TIFFIN_CONTAINER": "podman"}, _no, lambda: "") == "podman"
    assert doctor.container_runtime({"TIFFIN_CONTAINER": "0"}, _no, lambda: "") is None


def test_marker_files_and_cgroups_are_recognised(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.sys, "platform", "linux")
    assert doctor.container_runtime({}, lambda p: p == "/.dockerenv", lambda: "") == "docker"
    assert doctor.container_runtime({}, lambda p: p == "/run/.containerenv", lambda: "") == "podman"
    assert doctor.container_runtime({}, _no, lambda: "0::/docker/abc123") == "docker"
    assert doctor.container_runtime({}, _no, lambda: "0::/kubepods/burstable/x") == "kubepods"


def test_no_sign_means_none_and_an_unreadable_cgroup_is_not_a_guess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(doctor.sys, "platform", "linux")
    assert doctor.container_runtime({}, _no, lambda: "0::/user.slice/session-1.scope") is None

    def unreadable() -> str:
        raise OSError("no /proc")

    assert doctor.container_runtime({}, _no, unreadable) is None
    monkeypatch.setattr(doctor.sys, "platform", "win32")
    assert doctor.container_runtime({}, lambda p: True) is None


def _app(config, fixture_db, frozen_clock) -> App:
    config = replace(config, paths=replace(config.paths, db=fixture_db.path))
    config.paths.root.mkdir(parents=True)
    config.data["model"]["provider"] = "none"
    config.data["web"]["enabled"] = False
    secrets = Secrets(config.paths.secrets, {})
    secrets.write("telegram_token_dev", "example-token")
    return App(config, secrets, clock=frozen_clock, transport=lambda *a, **k: None)  # type: ignore[arg-type]


def _checks(app: App) -> dict[str, doctor.Check]:
    return {c.name: c for c in doctor.run_checks(app, online=False)}


def test_doctor_in_a_container_does_not_ask_a_service_manager(
    config, fixture_db, two_persons, frozen_clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(doctor, "container_runtime", lambda: "docker")

    def refuse(*_a, **_k):
        raise AssertionError("asked a service manager from inside a container")

    monkeypatch.setattr(doctor.subprocess, "run", refuse)
    service = _checks(_app(config, fixture_db, frozen_clock))["service"]
    assert "docker container" in service.detail
    assert service.status == "warn"  # no healthy poller in this fixture, and it says so
    assert "container's logs" in service.detail


def test_doctor_in_a_container_with_a_live_poller_is_ok(
    config, fixture_db, two_persons, frozen_clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _app(config, fixture_db, frozen_clock)
    Heartbeat(app.config.paths.state, frozen_clock).beat()
    monkeypatch.setattr(doctor, "container_runtime", lambda: "docker")
    monkeypatch.setattr(doctor, "_lock_held", lambda _p: True)
    service = _checks(app)["service"]
    assert service.status == "ok" and "restart policy" in service.detail


def test_doctor_outside_a_container_says_plainly_what_it_cannot_tell(
    config, fixture_db, two_persons, frozen_clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(doctor, "container_runtime", lambda: None)
    service = _checks(_app(config, fixture_db, frozen_clock))["service"]
    assert service.status == "warn" and "cannot tell" in service.detail


# -- the shop reader's Windows probe -------------------------------------------------------------
def test_the_windows_probe_is_never_run_elsewhere(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(chrome_job.sys, "platform", "linux")

    def boom(*_a, **_k):
        raise AssertionError("ran powershell off Windows")

    monkeypatch.setattr(subprocess, "run", boom)
    assert chrome_job.powershell_chrome_probe("Default", runner=subprocess.run) is True


# -- the container files, checked as text --------------------------------------------------------
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")
COMPOSE = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")


def test_base_images_are_pinned_to_a_version_and_a_digest_and_the_cli_to_a_version() -> None:
    images = re.findall(r"^FROM\s+(\S+)", DOCKERFILE, re.M)
    images += re.findall(r"--from=(\S+)", DOCKERFILE)
    images = [i for i in images if i not in ("claude", "build")]
    assert len(images) == 4, images
    for image in images:
        assert re.search(r":\d+(\.\d+)+\S*@sha256:[0-9a-f]{64}$", image), image
    assert re.search(r"@anthropic-ai/claude-code@\d+\.\d+\.\d+", DOCKERFILE)
    assert ":latest" not in DOCKERFILE


def test_the_image_runs_as_a_non_root_user_with_home_and_tmp_under_data() -> None:
    assert re.search(r"^USER\s+\S+", DOCKERFILE, re.M)
    assert not re.search(r"^USER\s+(root|0)\b", DOCKERFILE, re.M)
    assert "ARG TIFFIN_UID=" in DOCKERFILE
    for line in ("TIFFIN_HOME=/data", "HOME=/data/home", "TMPDIR=/data/tmp"):
        assert line in DOCKERFILE
    assert "PYTHONDONTWRITEBYTECODE=1" in DOCKERFILE
    assert "TIFFIN_CONTAINER=docker" in DOCKERFILE


def test_the_container_command_does_not_use_uv_and_runs_the_health_check() -> None:
    cmd = re.search(r"^CMD\s+(.+)$", DOCKERFILE, re.M | re.S)
    assert cmd is not None
    runs = re.findall(r"^CMD\s+(\[.*\])\s*$", DOCKERFILE, re.M)
    assert '["tiffin", "run"]' in runs
    assert not any("uv" in run for run in runs)
    assert "HEALTHCHECK" in DOCKERFILE and "tiffin.healthcheck" in DOCKERFILE


def test_the_compose_example_is_hardened() -> None:
    for needle in (
        "read_only: true",
        "cap_drop:",
        "- ALL",
        "no-new-privileges:true",
        "mem_limit:",
        "pids_limit:",
        "cpus:",
        "max-size:",
        "tmpfs:",
        "/tmp",
        "bind_trusted_network",
    ):
        assert needle in COMPOSE, needle
    assert not re.search(r"^\s+ports:", COMPOSE, re.M), "the port must not be published"
    assert "privileged" not in COMPOSE and "network_mode: host" not in COMPOSE


def test_the_compose_example_keeps_secrets_out_of_the_environment_block() -> None:
    env = re.search(r"environment:\n((?:\s+.*\n)+)", COMPOSE)
    assert env is not None
    leftover = env.group(1)
    for allowed in ("TIFFIN_TELEGRAM_TOKEN_FILE", "TIFFIN_CLAUDE_OAUTH_TOKEN_FILE"):
        leftover = leftover.replace(allowed, "")
    assert "TOKEN" not in leftover
    assert "secrets:" in COMPOSE and "/run/secrets/" in COMPOSE


def test_the_ignore_file_keeps_local_state_out_of_the_image() -> None:
    ignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").split()
    for line in (".venv", ".git", "*.db", ".env", "tests", "secrets"):
        assert line in ignore, line


# -- secrets from files, the token as a secret ----------------------------------------------------
def test_a_value_can_be_named_as_a_file(tmp_path: Path) -> None:
    named = tmp_path / "tg"
    named.write_text(BOM + "  file-token " + NL, encoding="utf-8")
    env = with_file_values({"TIFFIN_TELEGRAM_TOKEN_FILE": str(named)})
    assert env["TIFFIN_TELEGRAM_TOKEN"] == "file-token"
    both = with_file_values(
        {"TIFFIN_TELEGRAM_TOKEN_FILE": str(named), "TIFFIN_TELEGRAM_TOKEN": "env-token"}
    )
    assert both["TIFFIN_TELEGRAM_TOKEN"] == "env-token"
    assert "TIFFIN_X" not in with_file_values({"TIFFIN_X_FILE": ""})
    assert "TIFFIN" not in with_file_values({"TIFFIN_FILE": str(named)})


def test_the_app_reads_its_secrets_from_named_files(tmp_path: Path) -> None:
    named = tmp_path / "tg"
    named.write_text("file-token" + NL, encoding="utf-8")
    app = App.from_env(
        "dev",
        environ={
            "TIFFIN_HOME": str(tmp_path / "home"),
            "TIFFIN_TELEGRAM_TOKEN_DEV_FILE": str(named),
        },
    )
    assert app.secrets.get("telegram_token_dev") == "file-token"
    assert app.secrets.exists("telegram_token_dev")


def test_a_named_file_that_cannot_be_read_is_a_loud_error_without_its_contents(
    tmp_path: Path,
) -> None:
    with pytest.raises(ConfigError, match="cannot be read") as caught:
        with_file_values({"TIFFIN_TELEGRAM_TOKEN_FILE": str(tmp_path / "missing")})
    assert "missing" in str(caught.value)
    empty = tmp_path / "empty"
    empty.write_text(NL, encoding="utf-8")
    env = with_file_values({"TIFFIN_TELEGRAM_TOKEN_FILE": str(empty)})
    assert "TIFFIN_TELEGRAM_TOKEN" not in env


def test_the_claude_token_secret_reaches_only_the_claude_child(
    config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tiffin.model.provider import configured_provider

    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "outer")
    secrets = Secrets(tmp_path, {"TIFFIN_CLAUDE_OAUTH_TOKEN": "sub-token"})
    backend = configured_provider(config, secrets, transport=None, clock=None)  # type: ignore[arg-type]
    assert backend is not None
    env = backend.env  # type: ignore[attr-defined]
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sub-token"
    assert "CLAUDE_CODE_SESSION_ID" not in env


# -- the health check ------------------------------------------------------------------------------
def test_the_health_check_asks_the_configured_root_path(tmp_path: Path) -> None:
    (tmp_path / "tiffin.toml").write_text('[web]\nroot_path = "/tiffin"\n', encoding="utf-8")
    config = cfg.load("prod", root=tmp_path, environ={})
    assert healthcheck.health_url(config) == "http://127.0.0.1:7788/tiffin/api/health"
    (tmp_path / "tiffin.toml").write_text('[web]\nroot_path = "/x"\n', encoding="utf-8")
    config = cfg.load("prod", root=tmp_path, environ={})
    assert healthcheck.health_url(config) == "http://127.0.0.1:7788/x/api/health"


class _Reply:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_a) -> None:
        return None

    def read(self, _n: int) -> bytes:
        return self.body


def test_the_health_check_wants_ok_and_nothing_else(tmp_path: Path) -> None:
    config = cfg.load("prod", root=tmp_path, environ={})

    def says(body: bytes):
        return lambda *_a, **_k: _Reply(body)

    assert healthcheck.check(config, opener=says(b'{"status": "ok"}'))[0] is True
    assert healthcheck.check(config, opener=says(b'{"status": "degraded"}'))[0] is False
    assert healthcheck.check(config, opener=says(b"not json"))[0] is False
    assert healthcheck.check(config, opener=says(b"[]"))[0] is False

    def down(*_a, **_k):
        raise ConnectionRefusedError

    assert healthcheck.check(config, opener=down)[0] is False


def test_with_the_page_off_the_health_check_reads_the_heartbeat(
    tmp_path: Path, frozen_clock
) -> None:
    (tmp_path / "tiffin.toml").write_text("[web]\nenabled = false\n", encoding="utf-8")
    config = cfg.load("prod", root=tmp_path, environ={})
    assert healthcheck.check(config, clock=frozen_clock)[0] is False
    Heartbeat(config.paths.state, frozen_clock).beat()
    assert healthcheck.check(config, clock=frozen_clock)[0] is True
    frozen_clock.advance(minutes=10)
    assert healthcheck.check(config, clock=frozen_clock)[0] is False


def test_a_broken_config_is_an_unhealthy_container(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "tiffin.toml").write_text('[web]\nport_prod = "x"\n', encoding="utf-8")
    assert healthcheck.main({"TIFFIN_HOME": str(tmp_path)}) == 1
    assert "config" in capsys.readouterr().err


def test_the_image_is_stamped_and_the_compose_example_stops_cleanly() -> None:
    assert "ARG RELEASE_ID=unknown" in DOCKERFILE and "ARG RELEASE_COMMIT=unknown" in DOCKERFILE
    assert "TIFFIN_RELEASE=${RELEASE_ID}" in DOCKERFILE
    assert "TIFFIN_COMMIT=${RELEASE_COMMIT}" in DOCKERFILE
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "init: true" in compose and "stop_grace_period: 30s" in compose
