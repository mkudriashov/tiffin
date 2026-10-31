"""The prod log went silent after every restart until UTC midnight.

uvicorn's default logging config runs `logging.config.dictConfig`, which shuts down every handler
it finds - the JSON handler among them - and the handler only reopened its file on a new UTC day.
Each restart wrote its "start" line and then nothing, through twenty restarts in two days.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import logging.config
import threading
from pathlib import Path

import pytest

from tiffin import log
from tiffin.clock import TZ, FrozenClock


def _lines(log_dir: Path) -> list[str]:
    return [
        json.loads(line)["msg"]
        for p in sorted(log_dir.glob("tiffin-*.jsonl"))
        for line in p.read_text(encoding="utf-8").splitlines()
    ]


def test_the_json_log_outlives_a_config_that_closes_every_handler(tmp_path: Path) -> None:
    from uvicorn.config import LOGGING_CONFIG

    clock = FrozenClock(dt.datetime(2026, 9, 27, 10, 0, tzinfo=TZ))
    log.setup(tmp_path, console=False, clock=clock)
    try:
        logger = log.get("tiffin.test")
        logger.info("start")
        logging.config.dictConfig(LOGGING_CONFIG)  # what uvicorn does with its own config
        logger.info("the next job ran")
        assert _lines(tmp_path) == ["start", "the next job ran"]
    finally:
        log.setup(None, console=False)


def test_the_web_server_leaves_logging_alone(monkeypatch: pytest.MonkeyPatch, config) -> None:
    import uvicorn

    from tiffin.app import App
    from tiffin.secrets import Secrets
    from tiffin.web import app as web_app

    class Built(Exception):
        pass

    seen: dict = {}

    def fake_config(_asgi, **kw):
        seen.update(kw)
        raise Built

    monkeypatch.setattr(uvicorn, "Config", fake_config)
    secrets_dir = config.paths.root / "secrets-test"
    secrets_dir.mkdir(parents=True, exist_ok=True)
    tiffin_app = App(config, Secrets(secrets_dir))
    with pytest.raises(Built):
        web_app.serve(tiffin_app, threading.Event())
    assert "log_config" in seen and seen["log_config"] is None
