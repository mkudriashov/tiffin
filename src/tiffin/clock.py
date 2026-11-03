"""Every 'now' and every 'today' in Tiffin comes from here.

DB stamps use UTC space form. Local dates use the configured time zone through `today_local()`.
"""

from __future__ import annotations

import datetime as dt
import os
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    TZ: dt.tzinfo = ZoneInfo("Europe/London")
except PermissionError:
    # Some restricted Windows installations deny access to the packaged zoneinfo file.
    class _London(dt.tzinfo):
        @staticmethod
        def _last_sunday(year: int, month: int) -> dt.date:
            end = dt.date(year, month + 1, 1) - dt.timedelta(days=1)
            return end - dt.timedelta(days=(end.weekday() + 1) % 7)

        def utcoffset(self, value: dt.datetime | None) -> dt.timedelta:
            return self.dst(value)

        def dst(self, value: dt.datetime | None) -> dt.timedelta:
            if value is None:
                return dt.timedelta(0)
            start = dt.datetime.combine(self._last_sunday(value.year, 3), dt.time(2))
            end = dt.datetime.combine(self._last_sunday(value.year, 10), dt.time(1))
            local = value.replace(tzinfo=None)
            if start <= local < end or (
                end <= local < end + dt.timedelta(hours=1) and value.fold == 0
            ):
                return dt.timedelta(hours=1)
            return dt.timedelta(0)

        def tzname(self, value: dt.datetime | None) -> str:
            return "BST" if self.dst(value) else "GMT"

        def fromutc(self, value: dt.datetime) -> dt.datetime:
            if value.tzinfo is not self:
                raise ValueError("foreign timezone")
            utc = value.replace(tzinfo=None)
            start = dt.datetime.combine(self._last_sunday(utc.year, 3), dt.time(1))
            end = dt.datetime.combine(self._last_sunday(utc.year, 10), dt.time(1))
            if start <= utc < end:
                return (value + dt.timedelta(hours=1)).replace(fold=0)
            return value.replace(fold=int(end <= utc < end + dt.timedelta(hours=1)))

    TZ = _London()
UTC = dt.UTC
STAMP_FMT = "%Y-%m-%d %H:%M:%S"


class Clock:
    """Provide UTC instants and local dates for production code."""

    def __init__(self, timezone: str = "Europe/London") -> None:
        self.timezone = TZ if timezone == "Europe/London" else ZoneInfo(timezone)

    def now_utc(self) -> dt.datetime:
        return dt.datetime.now(UTC)

    def now_local(self) -> dt.datetime:
        return self.now_utc().astimezone(self.timezone)

    def today_local(self) -> dt.date:
        return self.now_local().date()

    def utc_stamp(self) -> str:
        return stamp(self.now_utc())


def detected_timezone() -> str:
    """Best available local zone name for an interactive setup prompt: TZ, then the zone
    /etc/localtime points at (Linux and macOS), then what Python knows, then Europe/London."""
    from_env = os.environ.get("TZ")
    if from_env:
        try:
            ZoneInfo(from_env)
            return from_env
        except ZoneInfoNotFoundError, ValueError:
            pass
    target = os.path.realpath("/etc/localtime") if os.name != "nt" else ""
    name = target.rsplit("zoneinfo/", 1)[1] if "zoneinfo/" in target else ""
    # servers and containers keep UTC whatever the household's zone, so UTC says nothing
    if name and name.removeprefix("Etc/") not in ("UTC", "UCT", "Universal", "Zulu", "GMT"):
        try:
            ZoneInfo(name)
            return name
        except ZoneInfoNotFoundError, ValueError:
            pass
    local = dt.datetime.now().astimezone().tzinfo
    key = getattr(local, "key", None)
    return str(key) if key else "Europe/London"


class FrozenClock(Clock):
    """A clock that only moves when told to. `at` is local (Europe/London) unless tz-aware."""

    def __init__(self, at: dt.datetime) -> None:
        if at.tzinfo is None:
            at = at.replace(tzinfo=TZ)
        self.timezone = at.tzinfo
        self._now = at.astimezone(UTC)

    def now_utc(self) -> dt.datetime:
        return self._now

    def advance(self, **kwargs: float) -> None:
        self._now += dt.timedelta(**kwargs)

    def set_local(self, at: dt.datetime) -> None:
        if at.tzinfo is None:
            at = at.replace(tzinfo=self.timezone)
        self._now = at.astimezone(UTC)


def stamp(t: dt.datetime) -> str:
    """UTC space-form. A naive datetime is refused: it is the bug this function exists to stop."""
    if t.tzinfo is None:
        raise ValueError("naive datetime; stamps must be tz-aware")
    return t.astimezone(UTC).strftime(STAMP_FMT)


def parse_stamp(s: str) -> dt.datetime:
    return dt.datetime.strptime(s, STAMP_FMT).replace(tzinfo=UTC)


def local_from_stamp(s: str, timezone: dt.tzinfo = TZ) -> dt.datetime:
    return parse_stamp(s).astimezone(timezone)


def local_at(day: dt.date, hhmm: str, timezone: dt.tzinfo = TZ) -> dt.datetime | None:
    """Local wall-clock time on `day`. Returns None for a spring-forward gap (the slot is skipped);
    an ambiguous autumn time resolves to the first occurrence (fold=0) so a slot runs once."""
    h, m = (int(x) for x in hhmm.split(":"))
    aware = dt.datetime(day.year, day.month, day.day, h, m, tzinfo=timezone, fold=0)
    # A non-existent local time round-trips through UTC to a different wall clock.
    if aware.astimezone(UTC).astimezone(timezone).replace(tzinfo=None) != aware.replace(
        tzinfo=None
    ):
        return None
    return aware


def week_start(day: dt.date) -> dt.date:
    """Monday of the week containing `day`."""
    return day - dt.timedelta(days=day.weekday())


def isoday(day: dt.date) -> str:
    return day.isoformat()


def parse_day(s: str) -> dt.date:
    return dt.date.fromisoformat(s)


def meal_now(now: dt.datetime) -> str:
    """The meal a person asking "what's the recipe" at this time of day most likely means.

    Lived in `telegram/menuflow.py` until the web page needed the same rule for its own
    current/next meal indicator (`/api/today`) - moved here, which already owns every other
    "what time is it" rule, rather than have `web/` import `telegram/` for one function.
    """
    minutes = now.hour * 60 + now.minute
    if minutes < 10 * 60 + 30:
        return "breakfast"
    return "lunch" if minutes < 15 * 60 else "dinner"


DEFAULT_CLOCK = Clock()
