"""A clean stop, a quick restart, and the home folders: the Linux deployment's edges.

Fake Telegram only. What only real Telegram can settle is in the module's last comment.
"""

from __future__ import annotations

import json
import signal
import threading
import time
from pathlib import Path

import pytest

from tiffin import runtime as rtmod
from tiffin.app import App
from tiffin.clock import FrozenClock
from tiffin.errors import TelegramConflict
from tiffin.http import Response
from tiffin.persons import PersonRegistry
from tiffin.runtime import Runtime, install_signal_handlers
from tiffin.secrets import Secrets
from tiffin.telegram.poller import Poller


def token_dir(tmp_path: Path) -> Path:
    d = tmp_path / "secrets"
    d.mkdir(exist_ok=True)
    (d / "telegram-token-dev.txt").write_text("1:devtoken\n", encoding="utf-8")
    return d


# ------------------------------------------------------------------ signals
def test_sigterm_and_sigint_set_the_stop_event_without_killing_anything() -> None:
    class Stub:
        def __init__(self) -> None:
            self.stopped = threading.Event()

        def request_stop(self) -> None:
            self.stopped.set()

    names = [n for n in ("SIGTERM", "SIGINT", "SIGBREAK") if hasattr(signal, n)]
    before = {n: signal.getsignal(getattr(signal, n)) for n in names}
    stub = Stub()
    try:
        install_signal_handlers(stub)  # type: ignore[arg-type]
        for n in names:
            handler = signal.getsignal(getattr(signal, n))
            assert callable(handler) and handler is not before[n]
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        handler(signal.SIGTERM, None)
        assert stub.stopped.is_set()
    finally:
        for n, h in before.items():
            signal.signal(getattr(signal, n), h)


def test_handlers_are_skipped_quietly_off_the_main_thread() -> None:
    errors: list[BaseException] = []

    def go() -> None:
        try:
            install_signal_handlers(object())  # type: ignore[arg-type]
        except BaseException as e:
            errors.append(e)

    t = threading.Thread(target=go)
    t.start()
    t.join()
    assert errors == []


# ------------------------------------------------------------------ the poller and a 409
class ConflictApi:
    """get_updates raises a conflict `n` times, then answers with nothing."""

    def __init__(self, n: float) -> None:
        self.n = n
        self.calls = 0

    def get_updates(self, offset: int, timeout_s: int | None = None) -> list[dict]:
        self.calls += 1
        if self.calls <= self.n:
            raise TelegramConflict("getUpdates: 409 terminated by other getUpdates request")
        return []


def make_poller(db, config, clock, api, ticks: list[float], **kw):
    now = [0.0]

    def monotonic() -> float:
        return now[0]

    def sleeper(s: float) -> None:
        ticks.append(s)
        now[0] += s

    registry = PersonRegistry(db, config, clock=clock)
    return Poller(
        api,  # type: ignore[arg-type]
        db,
        registry,
        lambda inbound: None,
        clock=clock,
        sleeper=sleeper,
        monotonic=monotonic,
        **kw,
    )


def test_a_conflict_that_clears_within_the_grace_is_retried_not_fatal(
    fixture_db, two_persons, config, frozen_clock
) -> None:
    api = ConflictApi(n=8)  # eight 5-second retries = 40 s, the shape of a quick restart
    ticks: list[float] = []
    p = make_poller(fixture_db, config, frozen_clock, api, ticks)
    assert not p.ready.is_set()
    p.run_forever(_OnceStop(p))  # runs until the first successful poll
    assert p.ready.is_set() and api.calls == 9 and ticks == [5.0] * 8


class _OnceStop(threading.Event):
    """A stop event that reads as set as soon as the poller reports ready."""

    def __init__(self, poller: Poller) -> None:
        super().__init__()
        self._poller = poller

    def is_set(self) -> bool:
        return self._poller.ready.is_set()


def test_a_conflict_that_persists_is_a_second_poller_and_still_raises(
    fixture_db, two_persons, config, frozen_clock
) -> None:
    api = ConflictApi(n=float("inf"))
    ticks: list[float] = []
    p = make_poller(fixture_db, config, frozen_clock, api, ticks)
    with pytest.raises(TelegramConflict):
        p.run_forever(threading.Event())
    assert sum(ticks) >= 120.0 and sum(ticks) <= 125.0  # the bounded wait, about two minutes
    assert not p.ready.is_set()  # nothing may start jobs on a token another poller holds


def test_a_conflict_after_stop_is_our_own_poll_ending(
    fixture_db, two_persons, config, frozen_clock
) -> None:
    api = ConflictApi(n=float("inf"))
    p = make_poller(fixture_db, config, frozen_clock, api, [])
    stop = threading.Event()
    stop.set()
    p.run_forever(stop)  # returns; no exception, no retry
    stop2 = threading.Event()

    class Api2(ConflictApi):
        def get_updates(self, offset, timeout_s=None):  # type: ignore[no-untyped-def]
            stop2.set()  # stop arrives while the poll is in flight, which then ends with a 409
            return super().get_updates(offset, timeout_s)

    p2 = make_poller(fixture_db, config, frozen_clock, Api2(n=float("inf")), [])
    p2.run_forever(stop2)


def test_other_telegram_errors_make_the_poller_ready_but_a_conflict_does_not(
    fixture_db, two_persons, config, frozen_clock
) -> None:
    from tiffin.errors import TelegramError

    class Down:
        def get_updates(self, offset, timeout_s=None):  # type: ignore[no-untyped-def]
            raise TelegramError("unreachable")

    p = make_poller(fixture_db, config, frozen_clock, Down(), [])
    p.run_once()
    assert p.ready.is_set()
    p2 = make_poller(fixture_db, config, frozen_clock, ConflictApi(n=1), [])
    with pytest.raises(TelegramConflict):
        p2.run_once()
    assert not p2.ready.is_set()


# ------------------------------------------------------------------ the runtime
class HeldPollTelegram:
    """A Telegram where a long poll stays open until another getUpdates arrives, which ends it
    with a 409 (the real behaviour). `hold_forever` keeps another poller on the token."""

    def __init__(self, *, hold_forever: bool = False) -> None:
        self.hold_forever = hold_forever
        self.long_polls = 0
        self.short_polls = 0
        self.other_poll_arrived = threading.Event()
        self.first_poll_open = threading.Event()
        self.calls: list[tuple[str, int | None]] = []

    def __call__(self, url, method, data, headers, timeout_s) -> Response:  # type: ignore[no-untyped-def]
        name = url.rsplit("/", 1)[-1]
        payload = json.loads(data) if data else {}
        self.calls.append((name, payload.get("timeout")))
        if name == "getUpdates":
            if self.hold_forever:
                return Response(
                    409, b'{"ok":false,"error_code":409,"description":"terminated by other"}'
                )
            if payload.get("timeout", 0) == 0:
                self.short_polls += 1
                self.other_poll_arrived.set()
                return Response(200, b'{"ok":true,"result":[]}')
            self.long_polls += 1
            self.first_poll_open.set()
            self.other_poll_arrived.wait(30)
            return Response(
                409, b'{"ok":false,"error_code":409,"description":"terminated by other"}'
            )
        return Response(200, b'{"ok":true,"result":true}')


def make_runtime(tmp_path, config, clock, tg, **kw) -> tuple[App, Runtime]:
    app = App(config, Secrets(token_dir(tmp_path)), clock=clock, transport=tg)
    app.bootstrap().close()
    return app, Runtime(app, heartbeat_s=60, tick_s=3600, jobs=[], serve_web=False, **kw)


def test_a_clean_stop_releases_the_open_poll_and_exits_zero(
    tmp_path: Path, frozen_clock: FrozenClock, config
) -> None:
    tg = HeldPollTelegram()
    _app, rt = make_runtime(tmp_path, config, frozen_clock, tg)
    rt.start()
    assert tg.first_poll_open.wait(5)
    started = time.monotonic()
    rt.stop()
    assert time.monotonic() - started < 5
    assert tg.short_polls == 1, "stop must take over the token once, with timeout 0"
    assert rt.exit_code == 0, "the 409 that ends our own poll is not a second poller"
    assert not (config.paths.state / "tiffin.pid").exists()


def test_a_real_second_poller_ends_in_exit_3_and_no_job_ever_starts(
    tmp_path: Path, frozen_clock: FrozenClock, config
) -> None:
    tg = HeldPollTelegram(hold_forever=True)
    _app, rt = make_runtime(tmp_path, config, frozen_clock, tg, conflict_grace_s=0.4)
    rt.start()
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not rt.stop_event.is_set():
            time.sleep(0.02)
        assert rt.stop_event.is_set() and rt.exit_code == 3
        assert not rt.poll_ready.is_set()
        assert rt.scheduler.ticks == 0, "the scheduler waits for the token"
    finally:
        rt.stop()


def test_the_scheduler_starts_once_the_poll_is_answered(
    tmp_path: Path, frozen_clock: FrozenClock, config
) -> None:
    from .fakes import FakeTelegram

    app = App(config, Secrets(token_dir(tmp_path)), clock=frozen_clock, transport=FakeTelegram())
    app.bootstrap().close()
    rt = Runtime(app, heartbeat_s=60, tick_s=3600, jobs=[], serve_web=False)
    rt.start()
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and rt.scheduler.ticks == 0:
            time.sleep(0.02)
        assert rt.poll_ready.is_set() and rt.scheduler.ticks >= 1
    finally:
        rt.stop()


def test_stop_does_not_wait_longer_than_its_grace_for_a_busy_worker(
    tmp_path: Path, frozen_clock: FrozenClock, config, monkeypatch: pytest.MonkeyPatch
) -> None:
    from .fakes import FakeTelegram

    monkeypatch.setattr(rtmod, "STOP_GRACE_S", 0.5)
    app = App(config, Secrets(token_dir(tmp_path)), clock=frozen_clock, transport=FakeTelegram())
    app.bootstrap().close()
    rt = Runtime(app, heartbeat_s=60, tick_s=3600, jobs=[], serve_web=False)
    rt.start()
    gate = threading.Event()
    rt.dispatch_job(0, lambda db: gate.wait(30))  # a job that outlives the stop
    time.sleep(0.2)
    started = time.monotonic()
    rt.stop()
    assert time.monotonic() - started < 3
    gate.set()


# ------------------------------------------------------------------ the home folders
def test_ensure_creates_home_and_tmp_under_a_bind_mounted_data_directory(config) -> None:
    assert not (config.paths.root / "home").exists()
    config.paths.ensure()
    for name in ("home", "tmp"):
        assert (config.paths.root / name).is_dir()


def test_bootstrap_creates_them_before_the_first_poll(
    tmp_path: Path, frozen_clock: FrozenClock, config
) -> None:
    from .fakes import FakeTelegram

    app = App(config, Secrets(token_dir(tmp_path)), clock=frozen_clock, transport=FakeTelegram())
    app.bootstrap().close()
    assert (config.paths.root / "home").is_dir() and (config.paths.root / "tmp").is_dir()


# What only real Telegram can settle: that a getUpdates with timeout 0 really ends the open one
# (this assumes the documented 409 behaviour), how long a poll cut off by SIGKILL stays open
# (about 65 s is the reasoning, not a measurement), and that docker stop's SIGTERM reaches PID 1
# with the compose `init` and `stop_grace_period` the platform sets.


def test_a_job_cut_off_by_the_stop_stays_running_and_is_taken_over_after_an_hour(
    tmp_path: Path, frozen_clock: FrozenClock, config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What a stop that ran out of grace leaves behind: nothing lost, nothing sent twice."""
    from tiffin import ledger

    from .fakes import FakeTelegram

    monkeypatch.setattr(rtmod, "STOP_GRACE_S", 0.3)
    app = App(config, Secrets(token_dir(tmp_path)), clock=frozen_clock, transport=FakeTelegram())
    app.bootstrap().close()
    rt = Runtime(app, heartbeat_s=60, tick_s=3600, jobs=[], serve_web=False)
    rt.start()
    gate = threading.Event()
    started = threading.Event()

    def job(db) -> None:  # type: ignore[no-untyped-def]
        with ledger.run(db, "morning_targets", "2026-09-07T0730", 1, clock=frozen_clock):
            started.set()
            gate.wait(30)

    rt.dispatch_job(1, job)
    assert started.wait(5)
    rt.stop()  # the grace runs out with the job inside its run
    db = app.open_db()
    try:
        row = db.one("SELECT status FROM run_ledger WHERE job = 'morning_targets'")
        assert row is not None and row["status"] == "running"
        assert ledger.live(db, "morning_targets", "2026-09-07T0730", 1, clock=frozen_clock)
        frozen_clock.advance(minutes=61)
        assert not ledger.live(db, "morning_targets", "2026-09-07T0730", 1, clock=frozen_clock)
    finally:
        db.close()
        gate.set()
