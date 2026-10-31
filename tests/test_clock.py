from __future__ import annotations

import datetime as dt

import pytest

from tiffin import clock as ck
from tiffin.clock import TZ, FrozenClock


def test_stamp_round_trip_is_utc_space_form() -> None:
    t = dt.datetime(2026, 9, 7, 20, 30, tzinfo=TZ)  # BST, UTC+1
    s = ck.stamp(t)
    assert s == "2026-09-07 19:30:00"
    assert "T" not in s
    back = ck.parse_stamp(s)
    assert back == t and back.tzinfo == dt.UTC
    assert ck.local_from_stamp(s).hour == 20


def test_stamp_refuses_naive() -> None:
    with pytest.raises(ValueError):
        ck.stamp(dt.datetime(2026, 9, 7, 20, 30))


def test_frozen_clock_advances(frozen_clock: FrozenClock) -> None:
    assert frozen_clock.utc_stamp() == "2026-09-07 19:30:00"
    assert frozen_clock.today_local() == dt.date(2026, 9, 7)
    frozen_clock.advance(hours=4)
    assert frozen_clock.today_local() == dt.date(2026, 9, 8)
    assert frozen_clock.now_local().hour == 0


def test_local_at_handles_dst() -> None:
    # 2026-03-29 01:30 does not exist in Europe/London (spring forward)
    assert ck.local_at(dt.date(2026, 3, 29), "01:30") is None
    ok = ck.local_at(dt.date(2026, 3, 29), "02:30")
    assert ok is not None and ok.utcoffset() == dt.timedelta(hours=1)
    # 2026-10-25 01:30 happens twice; fold=0 picks the first (BST)
    amb = ck.local_at(dt.date(2026, 10, 25), "01:30")
    assert amb is not None and amb.utcoffset() == dt.timedelta(hours=1)
    winter = ck.local_at(dt.date(2026, 12, 1), "08:00")
    assert winter is not None and winter.utcoffset() == dt.timedelta(0)


def test_week_start_is_monday() -> None:
    assert ck.week_start(dt.date(2026, 9, 7)) == dt.date(2026, 9, 7)
    assert ck.week_start(dt.date(2026, 9, 13)) == dt.date(2026, 9, 7)
    assert ck.week_start(dt.date(2026, 9, 14)) == dt.date(2026, 9, 14)


def test_isoday_parse_day() -> None:
    d = dt.date(2026, 9, 7)
    assert ck.parse_day(ck.isoday(d)) == d
