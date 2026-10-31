"""Intervals.icu mirror: client, budget, hash dedupe, revisions, tombstones, sidecars,
projections, weigh-ins, the DST-safe day, and the hourly job's outcomes. No network; every
path is under tmp_path."""

from __future__ import annotations

import datetime as dt
import itertools
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from tiffin import scheduler as sc
from tiffin.app import App
from tiffin.clock import FrozenClock
from tiffin.config import Config
from tiffin.db import Db
from tiffin.errors import IcuError, IcuRateLimited
from tiffin.http import Response, TransportError
from tiffin.integrations import icu
from tiffin.jobs import icu_sync
from tiffin.persons import Person
from tiffin.secrets import Secrets

from .fakes import FakeTelegram, ScriptedTransport, read_notices

FIXTURES = Path(__file__).parent / "fixtures" / "icu"
NO_SLEEP = lambda s: None  # noqa: E731


# ---------------------------------------------------------------- builders
def resp(payload: Any, status: int = 200, headers: dict[str, str] | None = None) -> Response:
    return Response(status, json.dumps(payload).encode("utf-8"), headers or {})


def activity(
    aid: str,
    start_utc: str,
    *,
    type_: str = "Ride",
    moving: int = 3600,
    elapsed: int = 3700,
    stream_types: list[str] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": aid,
        "type": type_,
        "name": f"{type_} {aid}",
        "start_date": start_utc,
        "start_date_local": start_utc[:-1],
        "moving_time": moving,
        "elapsed_time": elapsed,
        "calories": 512,
        "average_heartrate": 131.4,
        "max_heartrate": 166,
        "icu_training_load": 47.5,
        "icu_hr_zone_times": [600, 1800, 900, 300, 0, 0, 0],
        "stream_types": ["time", "heartrate"] if stream_types is None else stream_types,
    }
    base.update(extra)
    return base


def wellness(day: str, **fields: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": day,
        "weight": None,
        "steps": None,
        "sleepSecs": None,
        "hrv": None,
        "restingHR": None,
        "ctl": 5.0,
        "atl": 1.5,
        "updated": f"{day}T06:00:00.000+00:00",
    }
    row.update(fields)
    return row


def streams(n: int = 5) -> list[dict[str, Any]]:
    return [
        {"type": "time", "data": list(range(n))},
        {"type": "heartrate", "data": [120 + i for i in range(n)]},
    ]


def person(pid: int, slug: str = "owner") -> Person:
    return Person(pid, slug, slug.title(), "neutral", 1000001, None, False)


def run_sync(
    db: Db,
    who: Person,
    script: list[Response | Exception],
    tmp_path: Path,
    clock: FrozenClock,
    **kw: Any,
) -> tuple[icu.SyncResult, ScriptedTransport]:
    transport = ScriptedTransport(script)
    result = icu.sync_person(
        db,
        who,
        "not-a-real-key",
        state_dir=tmp_path / "state",
        streams_dir=tmp_path / "streams",
        clock=clock,
        transport=transport,
        sleeper=NO_SLEEP,
        **kw,
    )
    return result, transport


def raw_rows(db: Db, resource: str, source_id: str) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in db.all(
            "SELECT * FROM icu_raw WHERE resource = ? AND source_id = ? ORDER BY revision",
            (resource, source_id),
        )
    ]


@pytest.fixture
def pid(two_persons: dict[str, int]) -> int:
    return two_persons["owner"]


# Frozen at 2026-09-07 20:30 London: activities window 09-04..09-08, wellness 08-24..09-07.
A1 = activity("i1", "2026-09-06T06:00:00Z", type_="RockClimbing", moving=4800, elapsed=5000)
A2 = activity("i2", "2026-09-05T17:30:00Z")
W1 = wellness("2026-09-06", weight=100.4, steps=8123, sleepSecs=25200, hrv=55.5, restingHR=46)
W2 = wellness("2026-09-07", steps=512.0, ctl=4.8, atl=1.1)


# ---------------------------------------------------------------- sync: archive
def test_sync_appends_raw_rows_then_dedupes_by_hash(
    fixture_db: Db, pid: int, tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    result, transport = run_sync(
        fixture_db,
        person(pid),
        [resp([A1, A2]), resp(streams()), resp(streams(7)), resp([W1, W2])],
        tmp_path,
        frozen_clock,
    )
    assert (result.activities_new, result.streams_new, result.wellness_new) == (2, 2, 2)
    assert (result.deleted, result.weights_new, result.requests) == (0, 1, 4)
    urls = [c["url"] for c in transport.calls]
    assert urls[0].endswith("/athlete/0/activities?oldest=2026-09-04&newest=2026-09-08")
    assert urls[1:3] == [
        "https://intervals.icu/api/v1/activity/i1/streams",
        "https://intervals.icu/api/v1/activity/i2/streams",
    ]
    assert urls[3].endswith("/athlete/0/wellness?oldest=2026-08-24&newest=2026-09-07")
    assert (
        fixture_db.scalar("SELECT COUNT(*) FROM icu_raw") == 6
    )  # 2 act + 2 streams_index + 2 well
    assert fixture_db.scalar("SELECT COUNT(*) FROM icu_raw WHERE revision <> 1") == 0
    row = raw_rows(fixture_db, "activity", "i1")[0]
    assert row["person_id"] == pid and row["state"] == "live" and row["endpoint_class"] == "list"
    assert row["fetched_at"] == "2026-09-07 19:30:00" and row["sync_id"].startswith("owner-")
    assert row["payload_sha256"] == icu.sha256_text(icu.canonical(A1))

    # Same payloads again (keys reordered): nothing new, no stream requests at all.
    again, transport2 = run_sync(
        fixture_db,
        person(pid),
        [resp([dict(reversed(list(A2.items()))), A1]), resp([W2, W1])],
        tmp_path,
        frozen_clock,
    )
    assert again == icu.SyncResult(requests=2)
    assert len(transport2.calls) == 2
    assert fixture_db.scalar("SELECT COUNT(*) FROM icu_raw") == 6


def test_changed_payload_bumps_revision_and_refetches_streams(
    fixture_db: Db, pid: int, tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    run_sync(
        fixture_db, person(pid), [resp([A1]), resp(streams()), resp([])], tmp_path, frozen_clock
    )
    edited = dict(A1, moving_time=5400, name="renamed upstream")
    result, _ = run_sync(
        fixture_db, person(pid), [resp([edited]), resp(streams()), resp([])], tmp_path, frozen_clock
    )
    assert result.activities_new == 1 and result.streams_new == 1
    rows = raw_rows(fixture_db, "activity", "i1")
    assert [r["revision"] for r in rows] == [1, 2]
    assert json.loads(rows[0]["payload"])["moving_time"] == 4800  # the old revision is untouched
    assert json.loads(rows[1]["payload"])["moving_time"] == 5400
    proj = fixture_db.one("SELECT moving_s, name, icu_raw_id FROM icu_activity WHERE icu_id = 'i1'")
    assert proj is not None and proj["moving_s"] == 5400 and proj["icu_raw_id"] == rows[1]["id"]
    # Identical stream content: one sidecar file, two index rows (one per activity revision).
    index = fixture_db.all(
        "SELECT revision, sidecar FROM icu_stream_index WHERE icu_activity_id = 'i1' "
        "ORDER BY revision"
    )
    assert [r["revision"] for r in index] == [1, 2]
    assert index[0]["sidecar"] == index[1]["sidecar"]
    assert len(list((tmp_path / "streams" / "owner" / "2026").glob("i1-*.json"))) == 1
    # The streams_index archive row did not change, so it was not re-written.
    assert len(raw_rows(fixture_db, "streams_index", "i1")) == 1


def test_absent_activity_inside_window_is_tombstoned_once(
    fixture_db: Db, pid: int, tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    edge = activity("i3", "2026-09-04T08:00:00Z", stream_types=[])  # on the window's oldest day
    run_sync(
        fixture_db,
        person(pid),
        [resp([A1, edge]), resp(streams()), resp([])],
        tmp_path,
        frozen_clock,
    )
    result, _ = run_sync(fixture_db, person(pid), [resp([]), resp([])], tmp_path, frozen_clock)
    assert result.deleted == 1 and result.activities_new == 0
    rows = raw_rows(fixture_db, "activity", "i1")
    assert [(r["revision"], r["state"], r["payload"]) for r in rows] == [
        (1, "live", icu.canonical(A1)),
        (2, "deleted", None),
    ]
    assert rows[1]["payload_sha256"] == icu.TOMBSTONE_SHA256
    assert len(raw_rows(fixture_db, "activity", "i3")) == 1  # the edge day is never tombstoned
    assert [r["icu_id"] for r in fixture_db.all("SELECT icu_id FROM icu_activity")] == ["i3"]
    # Still absent: no second tombstone. Back again: a new live revision.
    again, _ = run_sync(fixture_db, person(pid), [resp([]), resp([])], tmp_path, frozen_clock)
    assert again.deleted == 0 and len(raw_rows(fixture_db, "activity", "i1")) == 2
    back, _ = run_sync(
        fixture_db, person(pid), [resp([A1]), resp(streams()), resp([])], tmp_path, frozen_clock
    )
    assert back.activities_new == 1
    assert raw_rows(fixture_db, "activity", "i1")[-1]["revision"] == 3
    assert fixture_db.scalar("SELECT COUNT(*) FROM icu_activity WHERE icu_id = 'i1'") == 1


def test_sidecar_is_content_addressed_and_missing_streams_are_re_asked(
    fixture_db: Db, pid: int, tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    # First sight: streams not there yet (404); no sidecar, nothing indexed.
    result, _ = run_sync(
        fixture_db, person(pid), [resp([A1]), Response(404, b""), resp([])], tmp_path, frozen_clock
    )
    assert result.streams_new == 0
    assert fixture_db.scalar("SELECT COUNT(*) FROM icu_stream_index") == 0
    # Unchanged activity, streams now upstream: fetched once, written once.
    result, _ = run_sync(
        fixture_db, person(pid), [resp([A1]), resp(streams(9)), resp([])], tmp_path, frozen_clock
    )
    assert result.streams_new == 1 and result.activities_new == 0
    row = fixture_db.one("SELECT * FROM icu_stream_index")
    assert row is not None and row["person_id"] == pid and row["revision"] == 1
    path = Path(row["sidecar"])
    body = icu.canonical(streams(9)).encode("utf-8")
    assert path.parent == tmp_path / "streams" / "owner" / "2026"
    assert path.name == f"i1-{icu.sha256_bytes(body)[:16]}.json"
    assert path.read_bytes() == body and row["sha256"] == icu.sha256_bytes(body)
    assert row["bytes"] == len(body) and row["samples"] == 9
    # Nothing in icu_raw carries a sample array; the streams_index summary carries lengths.
    summary = json.loads(raw_rows(fixture_db, "streams_index", "i1")[0]["payload"])
    assert summary["streams"] == [
        {"type": "heartrate", "n": 9, "n2": 0},
        {"type": "time", "n": 9, "n2": 0},
    ] or summary["streams"] == [
        {"type": "time", "n": 9, "n2": 0},
        {"type": "heartrate", "n": 9, "n2": 0},
    ]
    assert "data" not in json.dumps(summary)
    # Third run: sidecar held, so no streams request (the script would be exhausted otherwise).
    run_sync(fixture_db, person(pid), [resp([A1]), resp([])], tmp_path, frozen_clock)
    # A manually entered session (stream_types empty) is never asked for streams.
    manual = activity("i9", "2026-09-07T10:00:00Z", stream_types=[])
    result, transport = run_sync(
        fixture_db, person(pid), [resp([A1, manual]), resp([])], tmp_path, frozen_clock
    )
    assert result.activities_new == 1 and len(transport.calls) == 2


def test_credentials_are_redacted_before_hashing_and_storing(
    fixture_db: Db, pid: int, tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    leaky = activity(
        "i1", "2026-09-06T06:00:00Z", stream_types=[], athlete={"icu_api_key": "SECRET"}
    )
    run_sync(fixture_db, person(pid), [resp([leaky]), resp([])], tmp_path, frozen_clock)
    stored = raw_rows(fixture_db, "activity", "i1")[0]
    assert "SECRET" not in stored["payload"]
    assert json.loads(stored["payload"])["athlete"]["icu_api_key"] == icu.REDACTED
    assert stored["payload_sha256"] == icu.sha256_text(icu.canonical(icu.redact(leaky)))


# ---------------------------------------------------------------- sync: projections and weigh-ins
def test_projections_are_rebuilt_from_the_live_heads(
    fixture_db: Db, pid: int, tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    odd = activity(
        "i4",
        "2026-09-07T21:15:00Z",
        type_="Kitesurf",
        stream_types=[],
        icu_hr_zone_times=None,
        calories=None,
        max_heartrate=None,
    )
    run_sync(
        fixture_db,
        person(pid),
        [resp([A1, odd]), resp(streams()), resp([W1, W2])],
        tmp_path,
        frozen_clock,
    )
    w = {
        r["day"]: dict(r)
        for r in fixture_db.all("SELECT * FROM icu_wellness WHERE person_id = ?", (pid,))
    }
    assert set(w) == {"2026-09-06", "2026-09-07"}
    assert (w["2026-09-06"]["weight_kg"], w["2026-09-06"]["steps"]) == (100.4, 8123)
    assert (w["2026-09-06"]["sleep_s"], w["2026-09-06"]["hrv"], w["2026-09-06"]["resting_hr"]) == (
        25200,
        55.5,
        46,
    )
    assert (w["2026-09-06"]["ctl"], w["2026-09-06"]["atl"], w["2026-09-06"]["tsb"]) == (
        5.0,
        1.5,
        3.5,
    )
    assert w["2026-09-06"]["icu_raw_id"] == raw_rows(fixture_db, "wellness", "2026-09-06")[0]["id"]
    assert w["2026-09-07"]["weight_kg"] is None and w["2026-09-07"]["steps"] == 512
    assert round(w["2026-09-07"]["tsb"], 3) == 3.7
    a = {
        r["icu_id"]: dict(r)
        for r in fixture_db.all("SELECT * FROM icu_activity WHERE person_id = ?", (pid,))
    }
    climb = a["i1"]
    assert (climb["day"], climb["start_utc"]) == ("2026-09-06", "2026-09-06 06:00:00")
    assert (climb["icu_type"], climb["kind"], climb["name"]) == (
        "RockClimbing",
        "climb",
        "RockClimbing i1",
    )
    assert (climb["moving_s"], climb["elapsed_s"], climb["calories"]) == (4800, 5000, 512)
    assert (climb["avg_hr"], climb["max_hr"], climb["load"]) == (131, 166, 47.5)
    assert json.loads(climb["hr_zones_json"]) == [600, 1800, 900, 300, 0, 0, 0]
    assert a["i4"]["kind"] is None and a["i4"]["day"] == "2026-09-07"  # unmapped type, 22:15 BST
    assert a["i4"]["hr_zones_json"] is None and a["i4"]["calories"] is None
    # The rebuild is DELETE + INSERT: calling it again leaves exactly the same rows.
    assert icu.rebuild_projections(fixture_db, pid) == (2, 2)
    assert fixture_db.scalar("SELECT COUNT(*) FROM icu_activity") == 2


def test_projection_reads_interval_payload_shapes(
    fixture_db: Db, pid: int, tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    """Synthetic fixtures retain the field names that intervals.icu sends."""
    acts = json.loads((FIXTURES / "activities.json").read_text(encoding="utf-8"))
    wells = json.loads((FIXTURES / "wellness.json").read_text(encoding="utf-8"))
    assert acts and wells
    script: list[Response | Exception] = [resp(acts)]
    script += [Response(404, b"")] * sum(1 for a in acts if a.get("stream_types"))
    script.append(resp(wells))
    result, _ = run_sync(fixture_db, person(pid), script, tmp_path, frozen_clock)
    assert result.activities_new == len(acts) and result.wellness_new == len(wells)
    sample = next(a for a in acts if a["id"] == "example-climb")
    row = fixture_db.one("SELECT * FROM icu_activity WHERE icu_id = ?", (sample["id"],))
    assert row is not None
    assert (row["day"], row["start_utc"]) == ("2026-09-05", "2026-09-05 09:00:00")
    assert (row["icu_type"], row["kind"]) == ("RockClimbing", "climb")
    assert (row["moving_s"], row["elapsed_s"], row["calories"]) == (1800, 1900, 200)
    assert (row["avg_hr"], row["max_hr"], row["load"]) == (120, 150, 20.0)
    assert len(json.loads(row["hr_zones_json"])) == 7
    kinds = {r["kind"] for r in fixture_db.all("SELECT kind FROM icu_activity")}
    assert None not in kinds  # every fixture type is in the seed map
    got = fixture_db.all("SELECT * FROM icu_wellness ORDER BY day")
    assert len(got) == len(wells)
    with_weight = [r for r in got if r["weight_kg"] is not None]
    assert len(with_weight) == sum(1 for w in wells if w.get("weight") is not None)
    assert all(r["ctl"] is not None and r["atl"] is not None for r in got)
    assert result.weights_new == len(with_weight)


def test_weigh_ins_recorded_once_per_new_weight(
    fixture_db: Db, pid: int, tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    rows = [
        wellness("2026-09-04", weight=101.05),
        wellness("2026-09-05", weight=20.0),  # out of range: skipped, said in the log
        wellness("2026-09-06", weight=100.4),
        wellness("2026-09-07"),
    ]
    result, _ = run_sync(fixture_db, person(pid), [resp([]), resp(rows)], tmp_path, frozen_clock)
    assert result.weights_new == 2
    got = fixture_db.all("SELECT day, weight_kg, source, icu_raw_id FROM weigh_ins ORDER BY day")
    assert [(r["day"], r["weight_kg"], r["source"]) for r in got] == [
        ("2026-09-04", 101.05, "icu_wellness"),
        ("2026-09-06", 100.4, "icu_wellness"),
    ]
    assert got[0]["icu_raw_id"] == raw_rows(fixture_db, "wellness", "2026-09-04")[0]["id"]
    assert fixture_db.scalar("SELECT COUNT(*) FROM trend_points") == 2
    # Second sync, same weights: nothing new. A corrected weight upstream: one correction row.
    again, _ = run_sync(fixture_db, person(pid), [resp([]), resp(rows)], tmp_path, frozen_clock)
    assert again.weights_new == 0 and fixture_db.scalar("SELECT COUNT(*) FROM weigh_ins") == 2
    rows[2] = wellness("2026-09-06", weight=100.9)
    third, _ = run_sync(fixture_db, person(pid), [resp([]), resp(rows)], tmp_path, frozen_clock)
    assert third.weights_new == 1 and third.wellness_new == 1
    latest = fixture_db.latest("weigh_ins", person_id=pid, day="2026-09-06")
    assert latest is not None and latest["weight_kg"] == 100.9
    assert fixture_db.scalar("SELECT COUNT(*) FROM weigh_ins") == 3


def test_type_map_is_seeded_on_first_use_and_hand_rows_survive(fixture_db: Db) -> None:
    fixture_db.insert("icu_type_map", icu_type="Kitesurf", kind="other")
    fixture_db.insert("icu_type_map", icu_type="Workout", kind="other")  # overrides the seed
    assert icu.seed_type_map(fixture_db) == len(icu.TYPE_MAP_SEED) - 1
    assert icu.seed_type_map(fixture_db) == 0
    rows = {r["icu_type"]: r["kind"] for r in fixture_db.all("SELECT * FROM icu_type_map")}
    assert rows["Kitesurf"] == "other" and rows["Workout"] == "other"
    assert rows["Ride"] == "ride" and rows["Run"] == "run" and rows["RockClimbing"] == "climb"


# ---------------------------------------------------------------- 429, budget, client
def test_429_stops_the_run_and_keeps_what_landed(
    fixture_db: Db, pid: int, tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    with pytest.raises(IcuRateLimited, match="429"):
        run_sync(
            fixture_db,
            person(pid),
            [resp([A1]), Response(429, b"slow down"), resp([W1])],
            tmp_path,
            frozen_clock,
        )
    # The activity landed and was projected; wellness was never asked for.
    assert len(raw_rows(fixture_db, "activity", "i1")) == 1
    assert fixture_db.scalar("SELECT COUNT(*) FROM icu_activity") == 1
    assert fixture_db.scalar("SELECT COUNT(*) FROM icu_raw WHERE resource = 'wellness'") == 0
    state = fixture_db.one("SELECT v FROM icu_sync_state WHERE k = ?", (f"last_sync:{pid}",))
    assert state is not None and json.loads(state["v"])["requests"] == 2
    budget = json.loads((tmp_path / "state" / icu.BUDGET_FILE).read_text(encoding="utf-8"))
    assert len(budget["stamps"]) == 2 and budget["updated_at"] == "2026-09-07 19:30:00"


def test_budget_file_is_shared_persisted_and_refuses_before_the_request(
    tmp_path: Path, frozen_clock: FrozenClock, fixture_db: Db, pid: int
) -> None:
    path = tmp_path / "state" / icu.BUDGET_FILE
    b1 = icu.Budget(path, per_hour=2, clock=frozen_clock)
    b1.spend()
    b2 = icu.Budget(path, per_hour=2, clock=frozen_clock)  # the other person's sync
    assert b2.used() == 1
    b2.spend()
    with pytest.raises(icu.IcuBudgetSpent):
        b1.check()
    assert b1.spent_here == 1 and len(json.loads(path.read_text())["stamps"]) == 2
    frozen_clock.advance(seconds=3601)
    b1.check()  # the window rolled
    assert b1.used() == 0
    # Inside a sync the refusal happens before the transport is touched.
    transport = ScriptedTransport([resp([A1]), resp(streams())])
    with pytest.raises(icu.IcuBudgetSpent):
        icu.sync_person(
            fixture_db,
            person(pid),
            "k",
            state_dir=tmp_path / "state",
            streams_dir=tmp_path / "streams",
            clock=frozen_clock,
            transport=transport,
            sleeper=NO_SLEEP,
            budget_per_hour=1,
        )
    assert len(transport.calls) == 1 and len(raw_rows(fixture_db, "activity", "i1")) == 1
    (tmp_path / "state" / icu.BUDGET_FILE).write_text("not json", encoding="utf-8")
    assert (
        icu.Budget(path, per_hour=2, clock=frozen_clock).used() == 0
    )  # corrupt: restart, no crash


class RecordingTransport:
    def __init__(self, script: list[Response | Exception]):
        self.inner = ScriptedTransport(script)
        self.headers: list[Mapping[str, str]] = []

    def __call__(
        self,
        url: str,
        method: str,
        data: bytes | None,
        headers: Mapping[str, str],
        timeout_s: float,
    ) -> Response:
        self.headers.append(dict(headers))
        return self.inner(url, method, data, headers, timeout_s)


def make_client(
    script: list[Response | Exception], tmp_path: Path, clock: FrozenClock
) -> tuple[icu.IcuClient, RecordingTransport, list[float]]:
    transport = RecordingTransport(script)
    slept: list[float] = []
    ticks = itertools.count(0.0, 1.0)  # a full second between calls: pacing never sleeps
    client = icu.IcuClient(
        "k3y",
        transport=transport,
        budget=icu.Budget(tmp_path / "b.json", per_hour=50, clock=clock),
        clock=clock,
        sleeper=slept.append,
        monotonic=lambda: next(ticks),
    )
    return client, transport, slept


def test_client_auth_header_browser_ua_and_retry_rules(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    client, transport, slept = make_client(
        [Response(500, b"boom"), resp({"ok": 1}), Response(404, b""), Response(401, b"nope")],
        tmp_path,
        frozen_clock,
    )
    assert client.get("/athlete/0") == {"ok": 1}
    assert client.requests == 2 and slept == [2.0]
    h = transport.headers[0]
    assert h["Authorization"] == "Basic QVBJX0tFWTprM3k="  # base64("API_KEY:k3y")
    assert h["User-Agent"].startswith("Mozilla/5.0") and "urllib" not in h["User-Agent"]
    assert "k3y" not in transport.inner.calls[0]["url"]
    assert client.get("/activity/x/streams", optional=True) is None
    with pytest.raises(IcuError, match="401"):
        client.get("/athlete/0")
    client2, _, slept2 = make_client(
        [TransportError("GET intervals.icu: refused")] * 3, tmp_path, frozen_clock
    )
    with pytest.raises(IcuError, match="3 attempts"):
        client2.get("/athlete/0/wellness")
    assert client2.requests == 3 and slept2 == [2.0, 4.0]
    client3, _, _ = make_client([Response(404, b"gone")], tmp_path, frozen_clock)
    with pytest.raises(IcuError, match="404"):
        client3.get("/athlete/0/activities")
    client4, _, _ = make_client([Response(200, b"<html>")], tmp_path, frozen_clock)
    with pytest.raises(IcuError, match="not JSON"):
        client4.get("/athlete/0/activities")


def test_client_errors_do_not_echo_activity_ids_or_response_bodies(
    tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    client, _, _ = make_client([Response(400, b"Alex's private activity")], tmp_path, frozen_clock)
    with pytest.raises(IcuError) as caught:
        client.get("/activity/private-id/streams")
    assert "private-id" not in str(caught.value)
    assert "Alex" not in str(caught.value)
    assert "streams" in str(caught.value)


def test_client_paces_requests_by_min_interval(tmp_path: Path, frozen_clock: FrozenClock) -> None:
    # monotonic() is read once after each call and once before each paced call:
    # after #1 = 0.0; before #2 = 0.05 (sleep 0.2); after #2 = 0.3; before #3 = 1.5 (no sleep).
    ticks = iter([0.0, 0.05, 0.3, 1.5, 1.6])
    slept: list[float] = []
    client = icu.IcuClient(
        "k",
        transport=ScriptedTransport([resp([]), resp([]), resp([])]),
        budget=icu.Budget(tmp_path / "b.json", clock=frozen_clock),
        clock=frozen_clock,
        sleeper=slept.append,
        monotonic=lambda: next(ticks),
    )
    for _ in range(3):
        client.get("/x")
    assert [round(s, 2) for s in slept] == [0.2]


# ---------------------------------------------------------------- the DST-safe day
@pytest.mark.parametrize(
    ("start", "day", "start_utc"),
    [
        ("2026-07-15T23:30:00Z", "2026-07-16", "2026-07-15 23:30:00"),  # BST: next London day
        ("2026-01-15T23:30:00Z", "2026-01-15", "2026-01-15 23:30:00"),  # GMT: same day
        ("2026-10-25T00:30:00Z", "2026-10-25", "2026-10-25 00:30:00"),  # the autumn fold, once
        ("2026-03-29T00:59:00+00:00", "2026-03-29", "2026-03-29 00:59:00"),
        ("2026-08-19T19:25:11.250Z", "2026-08-19", "2026-08-19 19:25:11"),
        ("2026-08-19T20:25:11", "2026-08-19", "2026-08-19 20:25:11"),  # naive -> taken as UTC
    ],
)
def test_activity_day_is_the_london_day_of_the_utc_start(
    start: str, day: str, start_utc: str
) -> None:
    assert icu.activity_day({"start_date": start, "start_date_local": "2000-01-01T00:00:00"}) == (
        day,
        start_utc,
    )


def test_activity_day_falls_back_to_local_then_none() -> None:
    assert icu.activity_day({"start_date_local": "2026-09-06T20:25:11"}) == ("2026-09-06", None)
    assert icu.activity_day({"start_date": "garbage"}) == (None, None)
    assert icu.activity_day({}) == (None, None)


# ---------------------------------------------------------------- the hourly job
class Router:
    """Intervals.icu goes to the script; everything else is the fake Telegram."""

    def __init__(self, script: list[Response | Exception]):
        self.icu = ScriptedTransport(script)
        self.tg = FakeTelegram()

    def __call__(
        self,
        url: str,
        method: str,
        data: bytes | None,
        headers: Mapping[str, str],
        timeout_s: float,
    ) -> Response:
        if "intervals.icu" in url:
            return self.icu(url, method, data, headers, timeout_s)
        return self.tg(url, method, data, headers, timeout_s)


def make_app(
    tmp_path: Path,
    frozen_clock: FrozenClock,
    script: list[Response | Exception],
    *,
    key: bool = True,
) -> tuple[App, Router]:
    from tiffin import config as cfg

    root = tmp_path / "root"
    root.mkdir(exist_ok=True)
    config: Config = cfg.load("dev", root=root, environ={})
    assert str(config.paths.icu_streams).startswith(str(tmp_path))
    secrets_dir = tmp_path / "s"
    secrets_dir.mkdir(exist_ok=True)
    (secrets_dir / "telegram-token-dev.txt").write_text("1:t", encoding="utf-8")
    if key:
        (secrets_dir / "icu-key-owner.txt").write_text("k\n", encoding="utf-8")
    else:
        (secrets_dir / "icu-key-owner.txt").unlink(missing_ok=True)
    router = Router(script)
    return App(config, Secrets(secrets_dir), clock=frozen_clock, transport=router), router


def slot_for(pid: int) -> sc.Slot:
    slots = sc.slots_for(icu_sync.JOB, pid, None, dt.date(2026, 9, 7))
    assert [s.key for s in slots][:2] == ["2026-09-07T0010", "2026-09-07T0110"]
    return slots[20]  # 20:10, the last one due at the frozen 20:30


def test_job_definition_and_config_minute(config: Config) -> None:
    job = icu_sync.JOB
    assert (job.name, job.scope, job.kind, job.catch_up) == ("icu_sync", "each", "data", "none")
    assert job.needs_profile is False
    assert job.times(None, dt.date(2026, 9, 7))[:2] == ["00:10", "01:10"]
    assert icu_sync.job_for(config) is job
    data = dict(config.data, schedule=dict(config.data["schedule"], icu_sync_minute=25))
    tweaked = Config(env=config.env, data=data, paths=config.paths)
    assert icu_sync.job_for(tweaked).times(None, dt.date(2026, 9, 7))[0] == "00:25"
    from tiffin.jobs import all_jobs

    assert [j.name for j in all_jobs()][:2] == ["morning_targets", "icu_sync"]


def test_job_ok_run_syncs_and_matches(
    fixture_db: Db, pid: int, tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    plan = fixture_db.insert("training_plans", person_id=pid, effective_from="2026-09-01")
    fixture_db.insert(
        "planned_sessions", plan_id=plan, person_id=pid, weekday=6, kind="climb", target_minutes=90
    )  # Sunday 09-06: A1 is a 80-min climb that day
    app, router = make_app(tmp_path, frozen_clock, [resp([A1]), resp(streams()), resp([W1, W2])])
    s = sc.Scheduler(app, [icu_sync.JOB], lambda p, fn: None)
    assert s.execute(fixture_db, slot_for(pid)) == "ok"
    row = fixture_db.one("SELECT * FROM run_ledger WHERE job = 'icu_sync'")
    assert row is not None and row["person_id"] == pid and row["status"] == "ok"
    assert "activities 1 new" in row["detail"] and "session matches 1" in row["detail"]
    assert router.tg.sent == []  # a good run says nothing to anyone
    match = fixture_db.one("SELECT * FROM session_matches")
    assert match is not None and match["status"] == "matched" and match["icu_id"] == "i1"
    assert (app.config.paths.icu_streams / "owner" / "2026").exists()
    assert (app.config.paths.state / icu.BUDGET_FILE).exists()


def test_job_degrades_on_429_and_skips_without_key(
    fixture_db: Db, pid: int, tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    app, router = make_app(tmp_path, frozen_clock, [Response(429, b"")])
    s = sc.Scheduler(app, [icu_sync.JOB], lambda p, fn: None)
    assert s.execute(fixture_db, slot_for(pid)) == "degraded"
    row = fixture_db.one("SELECT status, detail FROM run_ledger WHERE job = 'icu_sync'")
    assert row is not None and row["status"] == "degraded"
    assert "stopped early: 429" in row["detail"] and "session matches 0" in row["detail"]
    assert router.tg.sent == []  # degraded is recorded, not alerted
    fixture_db.execute("DELETE FROM run_ledger")
    app2, _ = make_app(tmp_path, frozen_clock, [], key=False)
    s2 = sc.Scheduler(app2, [icu_sync.JOB], lambda p, fn: None)
    assert s2.execute(fixture_db, slot_for(pid)) == "skipped"
    row = fixture_db.one("SELECT status, detail FROM run_ledger WHERE job = 'icu_sync'")
    assert row is not None and (row["status"], row["detail"]) == ("skipped", "no icu key")


def test_job_fails_loudly_on_a_rejected_key(
    fixture_db: Db, pid: int, tmp_path: Path, frozen_clock: FrozenClock
) -> None:
    app, router = make_app(tmp_path, frozen_clock, [Response(401, b"bad key")])
    s = sc.Scheduler(app, [icu_sync.JOB], lambda p, fn: None)
    assert s.execute(fixture_db, slot_for(pid)) == "failed"
    assert router.tg.sent == [], "loud on the page, not in a chat"
    notes = read_notices(app.config.paths.state)
    assert notes and "401" in notes[-1]["text"]
    assert "k" not in notes[-1]["text"].split("HTTP 401")[0][-2:]  # never the key


def test_the_warm_pass_looks_further_back_than_the_hourly_one() -> None:
    """Nothing else ever looks past the hourly window.

    Without a wide weekly pass, a box that was off for four days loses those activities for
    good, an activity edited later is never re-read, and a weight added retroactively more
    than a fortnight back never arrives at all.
    """
    from tiffin.jobs import icu_sync

    assert icu_sync.WARM_JOB.days == frozenset({6})
    assert icu_sync.WARM_JOB.times(None, dt.date(2026, 9, 13)) == ["04:40"]
    assert icu_sync.WARM_JOB.catch_up == "always", "a missed warm pass is worth running late"
    assert icu_sync.JOB.catch_up == "none", "the hourly one is not"


def test_the_warm_windows_are_wider_in_config(tmp_path: Path) -> None:
    from tiffin import config as cfg

    data = cfg.load(root=tmp_path, environ={"TIFFIN_ENV": "dev"})
    assert data.get("icu.wellness_days_warm") > data.get("icu.wellness_days")
    assert data.get("icu.activity_days_warm") > data.get("icu.activity_days")
