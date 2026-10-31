"""Mirror each person's intervals.icu activities and wellness data.

A shared local request budget protects both API keys. Activities are archived as
revisions, stream samples are stored as content-addressed sidecars, and projections are
rebuilt from the archive. A rate limit degrades the run and leaves later work for the
next slot."""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import os
import sqlite3
import threading
import time
import urllib.parse
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import body, log
from ..clock import DEFAULT_CLOCK, TZ, UTC, Clock, isoday, parse_day, stamp
from ..db import Db
from ..errors import IcuError, IcuRateLimited
from ..http import Transport, TransportError, urllib_transport
from ..persons import Person

logger = log.get("tiffin.icu")

ATHLETE = "0"  # resolves to the owner of the key; survives an athlete id change
BASE_URL = "https://intervals.icu/api/v1"
# Cloudflare blocks python-urllib's default agent outright (documented by intervals.icu itself).
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
MIN_INTERVAL_S = 0.25
MAX_ATTEMPTS = 3  # 5xx or a socket error: two retries, then it is real
REQUEST_TIMEOUT_S = 60.0
BUDGET_FILE = "icu_budget.json"
BUDGET_WINDOW_S = 3600
BUDGET_KEEP_S = 86400

# Credentials are redacted VALUE-only; the KEY survives so the archive records it existed.
REDACT_KEYS = frozenset({"icu_api_key", "icu_friend_invite_token"})
REDACTED = "[redacted by tiffin icu sync]"
# A tombstone has no payload; payload_sha256 is NOT NULL in the schema, so it carries the hash
# of nothing. state='deleted' is the discriminator, never this value.
TOMBSTONE_SHA256 = hashlib.sha256(b"").hexdigest()

# intervals.icu activity type -> planned_sessions.kind. The campaign seed (migration 012)
# widened to Tiffin's vocabulary: Run/Walk/Swim are kinds here, not 'other'. An unmapped
# type projects kind NULL and matches nothing, which is reported, never silently 'other'.
TYPE_MAP_SEED: tuple[tuple[str, str], ...] = (
    ("Ride", "ride"),
    ("VirtualRide", "ride"),
    ("GravelRide", "ride"),
    ("MountainBikeRide", "ride"),
    ("EBikeRide", "ride"),
    ("Handcycle", "ride"),
    ("Velomobile", "ride"),
    ("RockClimbing", "climb"),
    ("Climbing", "climb"),
    ("IndoorClimbing", "climb"),
    ("Bouldering", "climb"),
    ("WeightTraining", "strength"),
    ("Workout", "strength"),
    ("Crossfit", "strength"),
    ("Yoga", "mobility"),
    ("Pilates", "mobility"),
    ("Stretching", "mobility"),
    ("Run", "run"),
    ("TrailRun", "run"),
    ("VirtualRun", "run"),
    ("Walk", "walk"),
    ("Hike", "walk"),
    ("Swim", "swim"),
    ("Rowing", "other"),
    ("Elliptical", "other"),
    ("StairStepper", "other"),
    ("Other", "other"),
)

_BUDGET_LOCK = threading.Lock()


class IcuBudgetSpent(IcuError):
    """Our own local budget, not theirs. Stops the run before the request, never after."""


# ---------------------------------------------------------------- hashing and redaction
def redact(obj: Any) -> Any:
    """Deep copy with credential VALUES replaced and their KEYS kept (nested too: intervals.icu
    embeds the athlete object inside other payloads)."""
    if isinstance(obj, dict):
        return {
            k: (REDACTED if k in REDACT_KEYS and v not in (None, "") else redact(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    return obj


def canonical(payload: Any) -> str:
    """The bytes the hash is over: sorted keys, no whitespace, so a re-serialisation upstream
    does not masquerade as a change."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_bytes(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


# ---------------------------------------------------------------- the local budget
class Budget:
    """Rolling request accounting that survives a restart and is shared by both keys.

    A JSON file rather than memory because the thing protected is an IP-wide limit: two
    per-person syncs in one process must see each other's spend, and a restart must not
    reset the window. Re-read on every check and spend under one lock.
    """

    def __init__(self, path: Path | str, *, per_hour: int = 60, clock: Clock = DEFAULT_CLOCK):
        self.path = Path(path)
        self.per_hour = int(per_hour)
        self.clock = clock
        self.spent_here = 0

    def _now(self) -> float:
        return self.clock.now_utc().timestamp()

    def _load(self) -> list[float]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return [float(t) for t in data.get("stamps", [])]
        except FileNotFoundError:
            return []
        except OSError, ValueError, TypeError, AttributeError:
            # Corrupt state must not stop a sync and must not be trusted either; starting
            # empty over-counts the remaining budget by at most one window.
            logger.warning(
                "icu budget file unreadable; window restarts", extra={"event": "icu_budget"}
            )
            return []

    def _save(self, stamps: list[float]) -> None:
        cutoff = self._now() - BUDGET_KEEP_S
        kept = [t for t in stamps if t >= cutoff]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(
            json.dumps({"stamps": kept, "updated_at": self.clock.utc_stamp()}), encoding="utf-8"
        )
        os.replace(tmp, self.path)

    def used(self, seconds: int = BUDGET_WINDOW_S) -> int:
        cutoff = self._now() - seconds
        with _BUDGET_LOCK:
            return sum(1 for t in self._load() if t >= cutoff)

    def check(self) -> None:
        """Raise BEFORE the request. Refusing to start is recoverable; a ban is not."""
        used = self.used()
        if used >= self.per_hour:
            raise IcuBudgetSpent(f"local hourly icu budget spent ({used}/{self.per_hour})")

    def spend(self) -> None:
        with _BUDGET_LOCK:
            stamps = self._load()
            stamps.append(self._now())
            self._save(stamps)
        self.spent_here += 1


# ---------------------------------------------------------------- the client
class IcuClient:
    """One authenticated, budgeted, paced conversation with intervals.icu for one key.

    HTTP statuses come back from the transport as responses; only the transport failing to
    reach the host raises, and that is retried like a 5xx. 429 is never retried.
    """

    def __init__(
        self,
        key: str,
        *,
        transport: Transport = urllib_transport,
        budget: Budget,
        clock: Clock = DEFAULT_CLOCK,
        base_url: str = BASE_URL,
        user_agent: str = USER_AGENT,
        sleeper: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        timeout_s: float = REQUEST_TIMEOUT_S,
    ):
        self._auth = "Basic " + base64.b64encode(f"API_KEY:{key}".encode()).decode("ascii")
        self.transport = transport
        self.budget = budget
        self.clock = clock
        self.base_url = base_url.rstrip("/")
        self.user_agent = user_agent
        self.sleeper = sleeper
        self.monotonic = monotonic
        self.timeout_s = timeout_s
        self.requests = 0
        self.last_status: int | None = None
        self.last_headers: dict[str, str] = {}
        self._last_call: float | None = None

    def _pace(self) -> None:
        if self._last_call is not None:
            gap = MIN_INTERVAL_S - (self.monotonic() - self._last_call)
            if gap > 0:
                self.sleeper(gap)

    def get(
        self, path: str, params: Mapping[str, str] | None = None, *, optional: bool = False
    ) -> Any:
        """One GET, parsed as JSON. `optional` turns a 404 into None."""
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(dict(params))
        resource = path.rpartition("/")[2]
        headers = {
            "Authorization": self._auth,
            "User-Agent": self.user_agent,
            "Accept": "application/json",
        }
        attempt = 0
        while True:
            self.budget.check()
            self._pace()
            detail = ""
            status: int | None = None
            try:
                resp = self.transport(url, "GET", None, headers, self.timeout_s)
            except TransportError as e:
                resp = None
                detail = str(e)[:160]
            self._last_call = self.monotonic()
            self.budget.spend()
            self.requests += 1
            if resp is not None:
                status = resp.status
                self.last_status = status
                self.last_headers = {k.lower(): v for k, v in resp.headers.items()}
                if status == 429:
                    raise IcuRateLimited(
                        f"429 from intervals.icu {resource} after {self.requests} requests",
                        status=status,
                    )
                if status in (401, 403):
                    raise IcuError(
                        f"HTTP {status} for intervals.icu {resource}: key or user agent rejected",
                        status=status,
                    )
                if status == 404 and optional:
                    return None
                if 400 <= status < 500:
                    raise IcuError(f"HTTP {status} for intervals.icu {resource}", status=status)
                if status == 200:
                    try:
                        return resp.json()
                    except ValueError as e:
                        raise IcuError(
                            f"intervals.icu {resource} returned 200 but not JSON: {str(e)[:100]}"
                        ) from None
                detail = f"HTTP {status}"
            attempt += 1
            if attempt >= MAX_ATTEMPTS:
                raise IcuError(
                    f"intervals.icu {resource} failed after {attempt} attempts: {detail}",
                    status=status,
                )
            self.sleeper(2.0 * attempt)

    # -- the three endpoints this integration uses ---------------------------------------
    def activities(self, oldest: dt.date, newest: dt.date) -> list[dict[str, Any]]:
        rows = self.get(
            f"/athlete/{ATHLETE}/activities", {"oldest": isoday(oldest), "newest": isoday(newest)}
        )
        return [r for r in (rows or []) if isinstance(r, dict)]

    def wellness(self, oldest: dt.date, newest: dt.date) -> list[dict[str, Any]]:
        rows = self.get(
            f"/athlete/{ATHLETE}/wellness", {"oldest": isoday(oldest), "newest": isoday(newest)}
        )
        return [r for r in (rows or []) if isinstance(r, dict)]

    def streams(self, activity_id: str) -> list[dict[str, Any]] | None:
        rows = self.get(f"/activity/{activity_id}/streams", optional=True)
        if rows is None:
            return None
        return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


# ---------------------------------------------------------------- payload readers
def parse_icu_time(value: Any) -> dt.datetime | None:
    """An intervals.icu timestamp ('2026-08-23T05:12:34Z' or with an offset) as an aware
    datetime; a naive value is taken as UTC."""
    if not isinstance(value, str) or not value.strip():
        return None
    s = value.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        t = dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    return t if t.tzinfo is not None else t.replace(tzinfo=UTC)


def activity_day(
    payload: Mapping[str, Any], timezone: dt.tzinfo = TZ
) -> tuple[str | None, str | None]:
    """(household ISO day, UTC space-form start) of an activity from its UTC `start_date`.

    Convert through UTC so activity dates follow the configured household calendar.
    `start_date_local` is the fallback when the UTC stamp is absent.
    """
    start = parse_icu_time(payload.get("start_date"))
    if start is not None:
        return isoday(start.astimezone(timezone).date()), stamp(start)
    local = payload.get("start_date_local")
    if isinstance(local, str) and len(local) >= 10:
        return local[:10], None
    return None, None


def _int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return round(float(value))
    except TypeError, ValueError:
        return None


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except TypeError, ValueError:
        return None


def wants_streams(activity: Mapping[str, Any]) -> bool:
    """A manually entered session has no streams upstream; asking would be a wasted request.
    Bounded by `stream_types` when the list payload carries it, true otherwise."""
    if "stream_types" not in activity:
        return True
    return bool(activity.get("stream_types"))


def stream_summary(streams: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every field of every stream EXCEPT the sample arrays, whose lengths stand in for them."""
    out = []
    for s in streams:
        row = {k: v for k, v in s.items() if k not in ("data", "data2")}
        row["n"] = len(s.get("data") or [])
        row["n2"] = len(s.get("data2") or [])
        out.append(row)
    return out


# ---------------------------------------------------------------- the archive
def seed_type_map(db: Db) -> int:
    """Seed icu_type_map on first use. Rows already present (seeded or hand-added) are left
    exactly as they are; only missing seed types are inserted. Returns the rows added."""
    have = {str(r["icu_type"]) for r in db.all("SELECT icu_type FROM icu_type_map")}
    missing = [(t, k) for t, k in TYPE_MAP_SEED if t not in have]
    if missing:
        with db.tx():
            for icu_type, kind in missing:
                db.execute(
                    "INSERT INTO icu_type_map(icu_type, kind) VALUES (?, ?)", (icu_type, kind)
                )
    return len(missing)


def heads(db: Db, person_id: int, resource: str) -> dict[str, sqlite3.Row]:
    """The newest revision per source_id for one person and resource."""
    rows = db.all(
        "SELECT r.id, r.source_id, r.revision, r.state, r.payload_sha256, r.payload "
        "FROM icu_raw r WHERE r.person_id = ? AND r.resource = ? AND r.revision = ("
        "  SELECT MAX(q.revision) FROM icu_raw q WHERE q.person_id = r.person_id "
        "  AND q.resource = r.resource AND q.source_id = r.source_id)",
        (person_id, resource),
    )
    return {str(r["source_id"]): r for r in rows}


@dataclass
class _Run:
    db: Db
    person: Person
    sync_id: str
    clock: Clock

    def append(
        self,
        resource: str,
        source_id: str,
        payload: Any,
        *,
        endpoint: str,
        endpoint_class: str,
        revision: int,
        state: str = "live",
    ) -> tuple[int, str]:
        """One icu_raw revision. Returns (row id, payload_sha256)."""
        text = canonical(payload) if state == "live" else None
        sha = sha256_text(text) if text is not None else TOMBSTONE_SHA256
        raw_id = self.db.insert(
            "icu_raw",
            person_id=self.person.id,
            resource=resource,
            source_id=source_id,
            revision=revision,
            endpoint_class=endpoint_class,
            state=state,
            payload=text,
            payload_sha256=sha,
            fetched_at=self.clock.utc_stamp(),
            endpoint=endpoint,
            sync_id=self.sync_id,
        )
        return raw_id, sha

    def upsert(
        self,
        resource: str,
        source_id: str,
        payload: Any,
        head: sqlite3.Row | None,
        *,
        endpoint: str,
        endpoint_class: str,
    ) -> tuple[int, int] | None:
        """Append a new revision only when the canonical hash differs from the live head.
        Returns (row id, revision) for a new revision, None when unchanged."""
        clean = redact(payload)
        sha = sha256_text(canonical(clean))
        if head is not None and head["state"] == "live" and head["payload_sha256"] == sha:
            return None
        revision = int(head["revision"]) + 1 if head is not None else 1
        raw_id, _ = self.append(
            resource,
            source_id,
            clean,
            endpoint=endpoint,
            endpoint_class=endpoint_class,
            revision=revision,
        )
        return raw_id, revision


@dataclass
class SyncResult:
    wellness_new: int = 0
    activities_new: int = 0
    streams_new: int = 0
    deleted: int = 0
    weights_new: int = 0
    requests: int = 0

    def summary(self) -> str:
        return (
            f"wellness {self.wellness_new} new, activities {self.activities_new} new, "
            f"streams {self.streams_new}, deleted {self.deleted}, weights {self.weights_new}, "
            f"requests {self.requests}"
        )


def write_sidecar(
    streams_dir: Path, slug: str, year: str, activity_id: str, streams: list[dict[str, Any]]
) -> tuple[Path, str, int, int]:
    """The samples, to the archive, named by their own hash. Returns (path, sha256, bytes,
    samples). An identical body re-registers the identical path and writes nothing."""
    blob = canonical(redact(streams)).encode("utf-8")
    digest = sha256_bytes(blob)
    folder = Path(streams_dir) / slug / year
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{activity_id}-{digest[:16]}.json"
    if not path.exists():
        tmp = path.with_name(path.name + ".part")
        tmp.write_bytes(blob)
        os.replace(tmp, path)
    samples = max((len(s.get("data") or []) for s in streams), default=0)
    return path, digest, len(blob), samples


def _has_sidecar(db: Db, person_id: int, activity_id: str, revision: int) -> bool:
    return (
        db.one(
            "SELECT 1 FROM icu_stream_index WHERE person_id = ? AND icu_activity_id = ? "
            "AND revision = ?",
            (person_id, activity_id, revision),
        )
        is not None
    )


def _sync_activities(
    run: _Run, client: IcuClient, result: SyncResult, oldest: dt.date, newest: dt.date
) -> list[tuple[str, int, dict[str, Any]]]:
    """Step 1. Returns the activities whose streams are wanted: (id, revision, payload)."""
    db, pid = run.db, run.person.id
    path = f"/athlete/{ATHLETE}/activities"
    fetched = client.activities(oldest, newest)
    current = heads(db, pid, "activity")
    seen: set[str] = set()
    pending: list[tuple[str, int, dict[str, Any]]] = []
    with db.tx():
        for act in fetched:
            aid = str(act.get("id") or "")
            if not aid:
                continue
            if aid in seen:
                # the same id twice in one response (pagination, or an edit upstream mid-fetch)
                # made both copies read the same head, compute revision 1 and collide on the
                # UNIQUE key. That is a bare IntegrityError, not an IcuError, so it escaped the
                # job and took the whole hour with it: no wellness, no weigh-ins, no session
                # matching, and a raw SQL string DMed to the owner. Every other upstream oddity in
                # this module is skipped and logged; so is this one.
                logger.warning(
                    "activity listed twice in one response, second copy skipped",
                    extra={"event": "icu_duplicate_activity"},
                )
                continue
            seen.add(aid)
            head = current.get(aid)
            new = run.upsert("activity", aid, act, head, endpoint=path, endpoint_class="list")
            if new is None:
                # Byte-identical to the head; return only for a sidecar it is missing.
                if (
                    head is not None
                    and wants_streams(act)
                    and not _has_sidecar(db, pid, aid, int(head["revision"]))
                ):
                    pending.append((aid, int(head["revision"]), redact(act)))
                continue
            result.activities_new += 1
            if wants_streams(act):
                pending.append((aid, new[1], redact(act)))
        # Deletions by absence, strictly inside the window asked for: an id at either edge
        # could be an inclusive/exclusive quirk, and a tombstone is permanent.
        lo, hi = isoday(oldest), isoday(newest)
        for aid, head in current.items():
            if aid in seen or head["state"] != "live" or not head["payload"]:
                continue
            day, _ = activity_day(json.loads(head["payload"]), run.clock.timezone)
            if day is not None and lo < day < hi:
                run.append(
                    "activity",
                    aid,
                    None,
                    endpoint=path,
                    endpoint_class="list",
                    revision=int(head["revision"]) + 1,
                    state="deleted",
                )
                result.deleted += 1
    return pending


def _sync_streams(
    run: _Run,
    client: IcuClient,
    result: SyncResult,
    pending: list[tuple[str, int, dict[str, Any]]],
    streams_dir: Path,
) -> None:
    """Step 2. One request per wanted activity; a 404 or an empty list means nothing upstream
    (yet) and is re-asked next hour while the activity stays in the window."""
    db, pid, slug = run.db, run.person.id, run.person.slug
    for aid, revision, act in pending:
        streams = client.streams(aid)
        if not streams:
            continue
        day, _ = activity_day(act, run.clock.timezone)
        year = (day or "0000")[:4]
        path, digest, size, samples = write_sidecar(streams_dir, slug, year, aid, streams)
        with db.tx():
            db.insert(
                "icu_stream_index",
                person_id=pid,
                icu_activity_id=aid,
                revision=revision,
                sidecar=str(path),
                sha256=digest,
                bytes=size,
                samples=samples,
            )
            run.upsert(
                "streams_index",
                aid,
                {"id": aid, "streams": stream_summary(streams)},
                heads(db, pid, "streams_index").get(aid),
                endpoint=f"/activity/{aid}/streams",
                endpoint_class="detail",
            )
        result.streams_new += 1


def _sync_wellness(
    run: _Run, client: IcuClient, result: SyncResult, oldest: dt.date, newest: dt.date
) -> None:
    """Step 3. Dense by construction (one row per calendar day); never tombstoned."""
    db, pid = run.db, run.person.id
    path = f"/athlete/{ATHLETE}/wellness"
    fetched = client.wellness(oldest, newest)
    current = heads(db, pid, "wellness")
    with db.tx():
        for w in fetched:
            wid = str(w.get("id") or "")
            if not wid:
                continue
            if run.upsert(
                "wellness", wid, w, current.get(wid), endpoint=path, endpoint_class="list"
            ):
                result.wellness_new += 1


# ---------------------------------------------------------------- projections and weigh-ins
def rebuild_projections(db: Db, person_id: int) -> tuple[int, int]:
    """DELETE + INSERT icu_wellness and icu_activity for one person from the live heads.
    Returns (wellness rows, activity rows)."""
    kinds = {
        str(r["icu_type"]): str(r["kind"])
        for r in db.all("SELECT icu_type, kind FROM icu_type_map")
    }
    n_w = n_a = 0
    with db.tx():
        db.execute("DELETE FROM icu_wellness WHERE person_id = ?", (person_id,))
        for wid, head in heads(db, person_id, "wellness").items():
            if head["state"] != "live" or not head["payload"]:
                continue
            p = json.loads(head["payload"])
            ctl, atl = _num(p.get("ctl")), _num(p.get("atl"))
            db.insert(
                "icu_wellness",
                person_id=person_id,
                day=wid,
                weight_kg=_num(p.get("weight")),
                steps=_int(p.get("steps")),
                sleep_s=_int(p.get("sleepSecs")),
                hrv=_num(p.get("hrv")),
                resting_hr=_int(p.get("restingHR")),
                ctl=ctl,
                atl=atl,
                tsb=(ctl - atl) if ctl is not None and atl is not None else None,
                icu_raw_id=int(head["id"]),
            )
            n_w += 1
        db.execute("DELETE FROM icu_activity WHERE person_id = ?", (person_id,))
        for aid, head in heads(db, person_id, "activity").items():
            if head["state"] != "live" or not head["payload"]:
                continue
            p = json.loads(head["payload"])
            day, start_utc = activity_day(p, db.clock.timezone)
            if day is None:
                logger.warning("activity without a start skipped", extra={"event": "icu_no_start"})
                continue
            zones = p.get("icu_hr_zone_times")
            icu_type = p.get("type")
            db.insert(
                "icu_activity",
                person_id=person_id,
                icu_id=aid,
                day=day,
                start_utc=start_utc,
                icu_type=icu_type,
                kind=kinds.get(str(icu_type)) if icu_type is not None else None,
                name=p.get("name"),
                moving_s=_int(p.get("moving_time")),
                elapsed_s=_int(p.get("elapsed_time")),
                calories=_int(p.get("calories")),
                avg_hr=_int(p.get("average_heartrate")),
                max_hr=_int(p.get("max_heartrate")),
                load=_num(p.get("icu_training_load")),
                hr_zones_json=json.dumps(zones) if isinstance(zones, list) else None,
                icu_raw_id=int(head["id"]),
            )
            n_a += 1
    return n_w, n_a


def record_weights(db: Db, person_id: int) -> int:
    """A weigh_ins row for every projected wellness day whose weight the DB does not hold yet
    (a repeat of the same day and value writes nothing). Returns the rows written."""
    rows = db.all(
        "SELECT day, weight_kg, icu_raw_id FROM icu_wellness "
        "WHERE person_id = ? AND weight_kg IS NOT NULL ORDER BY day",
        (person_id,),
    )
    written = 0
    for r in rows:
        kg = round(float(r["weight_kg"]), 2)
        if not 30 <= kg <= 250:
            logger.warning(
                "wellness weight out of range skipped", extra={"event": "icu_weight_range"}
            )
            continue
        have = db.latest("weigh_ins", person_id=person_id, day=str(r["day"]))
        if have is not None and abs(float(have["weight_kg"]) - kg) < 0.005:
            continue
        body.record_weigh_in(
            db,
            person_id,
            parse_day(str(r["day"])),
            kg,
            "icu_wellness",
            icu_raw_id=int(r["icu_raw_id"]),
        )
        written += 1
    return written


def _finish(db: Db, person_id: int, result: SyncResult, clock: Clock) -> None:
    with db.tx():
        rebuild_projections(db, person_id)
        result.weights_new = record_weights(db, person_id)
        db.execute(
            "INSERT INTO icu_sync_state(k, v) VALUES (?, ?) "
            "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
            (
                f"last_sync:{person_id}",
                json.dumps({"at": clock.utc_stamp(), **result.__dict__}),
            ),
        )


# ---------------------------------------------------------------- the run
def sync_person(
    db: Db,
    person: Person,
    key: str,
    *,
    state_dir: Path | str,
    streams_dir: Path | str,
    days_wellness: int = 14,
    days_activity: int = 3,
    clock: Clock = DEFAULT_CLOCK,
    transport: Transport = urllib_transport,
    base_url: str = BASE_URL,
    user_agent: str = USER_AGENT,
    budget_per_hour: int = 60,
    sleeper: Callable[[float], None] = time.sleep,
) -> SyncResult:
    """One hourly pull for one person. See the module docstring for the order of requests.

    Raises IcuRateLimited (429) or IcuBudgetSpent after projecting whatever landed; any other
    IcuError (key rejected, persistent 5xx, unreachable) propagates untouched so the job fails
    loudly.
    """
    seed_type_map(db)
    budget = Budget(Path(state_dir) / BUDGET_FILE, per_hour=budget_per_hour, clock=clock)
    client = IcuClient(
        key,
        transport=transport,
        budget=budget,
        clock=clock,
        base_url=base_url,
        user_agent=user_agent,
        sleeper=sleeper,
    )
    sync_id = f"{person.slug}-{clock.now_utc().strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}"
    run = _Run(db, person, sync_id, clock)
    result = SyncResult()
    today = clock.today_local()
    try:
        pending = _sync_activities(
            run,
            client,
            result,
            today - dt.timedelta(days=days_activity),
            today + dt.timedelta(days=1),
        )
        _sync_streams(run, client, result, pending, Path(streams_dir))
        _sync_wellness(run, client, result, today - dt.timedelta(days=days_wellness), today)
    except IcuRateLimited, IcuBudgetSpent:
        result.requests = client.requests
        _finish(db, person.id, result, clock)
        raise
    result.requests = client.requests
    _finish(db, person.id, result, clock)
    logger.info("icu sync %s", result.summary(), extra={"event": "icu_sync"})
    return result


__all__ = [
    "ATHLETE",
    "BASE_URL",
    "BUDGET_FILE",
    "TYPE_MAP_SEED",
    "USER_AGENT",
    "Budget",
    "IcuBudgetSpent",
    "IcuClient",
    "SyncResult",
    "activity_day",
    "canonical",
    "heads",
    "parse_icu_time",
    "rebuild_projections",
    "record_weights",
    "redact",
    "seed_type_map",
    "sha256_text",
    "stream_summary",
    "sync_person",
    "wants_streams",
    "write_sidecar",
]
