from __future__ import annotations

from pathlib import Path

from tiffin.clock import FrozenClock
from tiffin.heartbeat import Heartbeat, PidFile


def test_heartbeat_round_trip_and_age(tmp_path: Path, frozen_clock: FrozenClock) -> None:
    hb = Heartbeat(tmp_path / "state", frozen_clock)
    assert hb.read() is None and hb.age_s() is None
    hb.beat(pid=4242)
    got = hb.read()
    assert got is not None and got[1] == 4242
    assert hb.age_s() == 0.0
    frozen_clock.advance(seconds=95)
    assert hb.age_s() == 95.0
    assert not (tmp_path / "state" / "heartbeat.tmp").exists()


def test_pidfile(tmp_path: Path, frozen_clock: FrozenClock) -> None:
    pf = PidFile(tmp_path, frozen_clock)
    assert pf.read() is None
    pf.write(exe=str(tmp_path / "python.exe"))
    got = pf.read()
    assert got is not None and got["start_utc"] == "2026-09-07 19:30:00" and got["pid"] > 0
    pf.remove()
    assert pf.read() is None
