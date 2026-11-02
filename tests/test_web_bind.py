"""The page binds loopback unless the operator opts in to a trusted network."""

from __future__ import annotations

import threading

import pytest

from tiffin.app import App
from tiffin.errors import ConfigError
from tiffin.secrets import Secrets
from tiffin.web import app as web_app


class _FakeServer:
    started = False
    should_exit = False
    seen: list = []

    def __init__(self, config) -> None:
        _FakeServer.seen.append(config)

    def run(self) -> None:
        return None


def _app(config, frozen_clock, **web) -> App:
    config.data["web"].update(web)
    return App(config, Secrets(config.paths.secrets, {}), clock=frozen_clock)


def test_default_bind_is_loopback_and_off(config) -> None:
    assert config.get("web.host") == "127.0.0.1"
    assert config.get("web.bind_trusted_network") is False


@pytest.mark.parametrize("host", ["0.0.0.0", "10.1.2.3", "::", "example.com"])
def test_a_non_loopback_host_is_refused_without_the_opt_in(config, frozen_clock, host) -> None:
    app = _app(config, frozen_clock, host=host)
    with pytest.raises(ConfigError, match="bind_trusted_network"):
        web_app.serve(app, threading.Event())


@pytest.mark.parametrize("flag", ["true", 1, "yes", None])
def test_only_a_real_true_opts_in(config, frozen_clock, flag) -> None:
    app = _app(config, frozen_clock, host="0.0.0.0", bind_trusted_network=flag)
    with pytest.raises(ConfigError):
        web_app.serve(app, threading.Event())


def test_the_opt_in_lets_a_container_bind_and_nothing_else_changes(
    config, frozen_clock, monkeypatch
) -> None:
    import uvicorn

    _FakeServer.seen = []
    monkeypatch.setattr(uvicorn, "Server", _FakeServer)
    app = _app(config, frozen_clock, host="0.0.0.0", bind_trusted_network=True)
    web_app.serve(app, threading.Event())
    assert _FakeServer.seen[0].host == "0.0.0.0"


def test_loopback_still_binds_without_the_flag(config, frozen_clock, monkeypatch) -> None:
    import uvicorn

    _FakeServer.seen = []
    monkeypatch.setattr(uvicorn, "Server", _FakeServer)
    web_app.serve(_app(config, frozen_clock), threading.Event())
    assert _FakeServer.seen[0].host == "127.0.0.1"


def test_the_demo_identity_still_needs_loopback(config, frozen_clock) -> None:
    from tiffin.demo import DemoApp  # noqa: F401  (the class check is the guard)

    app = _app(config, frozen_clock, host="0.0.0.0", bind_trusted_network=True)
    with pytest.raises(ConfigError, match="demo"):
        web_app.create_app(app, demo_person=object())  # type: ignore[arg-type]


def test_a_non_boolean_flag_is_a_config_error(tmp_path) -> None:
    from tiffin import config as cfg

    (tmp_path / "tiffin.toml").write_text('[web]\nbind_trusted_network = "yes"\n')
    with pytest.raises(ConfigError, match="bind_trusted_network"):
        cfg.load("dev", root=tmp_path, environ={})
