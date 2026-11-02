"""Shared fixtures. Every database is a temporary file in this checkout."""

from __future__ import annotations

import datetime as dt
import locale
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

# pytest's default temp root under %LOCALAPPDATA% is not writable by the filtered token, and a
# fixed --basetemp collides between concurrent runs; a project-local temp root gives each run
# its own numbered directory.
_TEMPROOT = Path(__file__).resolve().parents[1] / ".pytest-tmp"
_TEMPROOT.mkdir(exist_ok=True)
os.environ["PYTEST_DEBUG_TEMPROOT"] = str(_TEMPROOT)
locale.setlocale(locale.LC_TIME, "C")

from tiffin.clock import TZ, FrozenClock  # noqa: E402
from tiffin.db import Db  # noqa: E402

FROZEN_AT = dt.datetime(2026, 9, 7, 20, 30, tzinfo=TZ)


@pytest.fixture(autouse=True)
def shopping_counts_nothing_at_home_by_default() -> Iterator[None]:
    """The shopping setting is process-wide; every test starts from the default (off)."""
    from tiffin.food import shopping

    shopping.set_count_on_home(False)
    yield
    shopping.set_count_on_home(False)


@pytest.fixture
def count_on_home(monkeypatch: pytest.MonkeyPatch) -> None:
    """Turn `shopping.count_on_home` on for one test.

    An `App` applies its config on construction, so the setting is held on for the test (the
    setter ignores the default config) and reset afterwards by the autouse fixture above.
    """
    from tiffin.food import shopping

    shopping.set_count_on_home(True)
    monkeypatch.setattr(shopping, "set_count_on_home", lambda value: None)


@pytest.fixture(autouse=True)
def nothing_real_leaves_the_box(monkeypatch: pytest.MonkeyPatch) -> None:
    """Block real browser and model subprocesses during fixture tests.

    Tests exercising either integration supply their own runner. All other tests receive the
    unavailable path, so a local browser or model cannot affect fixture results.
    """
    import subprocess

    from tiffin.integrations import chrome_job
    from tiffin.model import claude_cli

    for key in tuple(os.environ):
        if key.startswith("TIFFIN_"):
            monkeypatch.delenv(key)

    check_ready = chrome_job.ChromeJob.check_ready

    def guarded_check_ready(self: chrome_job.ChromeJob) -> None:
        check_ready(self)  # the real checks first, so their own messages still come out
        if self.runner is subprocess.run:
            raise chrome_job.ChromeUnavailable("tests never drive a real browser")

    monkeypatch.setattr(chrome_job.ChromeJob, "check_ready", guarded_check_ready)

    backend_init = claude_cli.ClaudeBackend.__init__

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise OSError("tests never start a real claude -p")

    def guarded_init(self: claude_cli.ClaudeBackend, *args: object, **kwargs: object) -> None:
        backend_init(self, *args, **kwargs)  # type: ignore[arg-type]
        if self.runner is subprocess.run:
            self.runner = refuse  # type: ignore[assignment]

    monkeypatch.setattr(claude_cli.ClaudeBackend, "__init__", guarded_init)


@pytest.fixture
def frozen_clock() -> FrozenClock:
    return FrozenClock(FROZEN_AT)


@pytest.fixture
def fixture_db(tmp_path: Path, frozen_clock: FrozenClock) -> Iterator[Db]:
    path = tmp_path / "tiffin.db"
    assert path.is_relative_to(tmp_path), "tests must use the fixture directory"
    db = Db.open(path, clock=frozen_clock)
    assert db.scalar("PRAGMA journal_mode") == "wal"
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def two_persons(fixture_db: Db) -> dict[str, int]:
    """Create two example people with Telegram identities; return their IDs by slug."""
    ids: dict[str, int] = {}
    for slug, name, chat_id in (("owner", "Alex", 1000001), ("partner", "Her", 1000002)):
        pid = fixture_db.insert("persons", slug=slug, display_name=name, tone="neutral")
        fixture_db.insert("telegram_identities", person_id=pid, chat_id=chat_id, method="config")
        ids[slug] = pid
    return ids


@pytest.fixture
def config(tmp_path: Path):
    from tiffin import config as cfg

    loaded = cfg.load("dev", root=tmp_path / "root", environ={})
    # the fake models answer by index into the list in code order; the weekly shuffle has tests
    # of its own (test_personal.py)
    loaded.data["models"]["shuffle_catalogue"] = False
    # most web tests sign in with the Tailscale header; the packaged default is Telegram
    # sign-in, which test_web_signin.py and test_web_devices.py use
    loaded.data["web"]["identity_mode"] = "header"
    loaded.data["web"]["trusted_header_proxy"] = True
    loaded.data["persons"]["owner"]["chat_id"] = 1000001
    loaded.data["chrome"]["shop_refresh_enabled"] = True
    loaded.data["chrome"]["shop_fill_enabled"] = True
    loaded.data["shop"]["retailers"] = ["sainsburys", "marks_spencer"]
    loaded.data["shop"]["retailer"] = "sainsburys"
    return loaded
