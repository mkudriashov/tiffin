"""Account routes for the web page."""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .. import release
from ..clock import isoday, stamp
from ..db import Db
from ..food.products import ZERO_DECIMAL_CURRENCIES, price_decimals
from ..heartbeat import Heartbeat
from .api_common import (
    LEGACY_CSRF_COOKIE,
    _body,
    _db,
    _person,
    _read,
    _row_id,
    _today,
    check_fence,
    csrf_token_for,
)
from .identity import HEADER, IDLE_DAYS, SESSION_COOKIE, Resolver, device_for, is_loopback

router = APIRouter()


@router.get("/api/devices")
async def devices(request: Request) -> dict[str, Any]:
    person = _person(request)

    def read(db: Db) -> dict[str, Any]:
        rows = db.all(
            "SELECT d.id, d.created_at FROM web_devices d "
            "LEFT JOIN web_device_events e ON e.device_id = d.id "
            "WHERE d.person_id = ? AND d.expires_at > ? AND e.id IS NULL "
            "AND COALESCE((SELECT MAX(s.created_at) FROM web_device_seen s "
            "WHERE s.device_id = d.id), d.created_at) > ? ORDER BY d.id DESC",
            (
                person.id,
                db.clock.utc_stamp(),
                stamp(db.clock.now_utc() - dt.timedelta(days=IDLE_DAYS)),
            ),
        )
        token = request.cookies.get(SESSION_COOKIE, "")
        current = device_for(request.app.state.tiffin, token) if token else None
        return {
            "devices": [
                {
                    "id": r["id"],
                    "created_at": r["created_at"],
                    "current": current is not None and current[1] == r["id"],
                }
                for r in rows
            ]
        }

    return await _read(request, read)


@router.post("/api/devices/sign-out")
async def sign_out_device(request: Request) -> Response:
    person = _person(request)
    body = await _body(request)
    check_fence(request, body, {"device_id"})
    device_id = body.get("device_id")
    if not isinstance(device_id, int) or isinstance(device_id, bool):
        raise HTTPException(status_code=400, detail="Choose a device")
    device_id = _row_id(device_id, "device")

    def write(db: Db) -> bool:
        row = db.one(
            "SELECT d.id FROM web_devices d LEFT JOIN web_device_events e ON e.device_id = d.id "
            "WHERE d.id = ? AND d.person_id = ? AND e.id IS NULL",
            (device_id, person.id),
        )
        if row is None:
            return False
        db.insert("web_device_events", device_id=device_id, action="sign_out")
        return True

    if not await _read(request, write):
        raise HTTPException(status_code=404, detail="Device not found")
    response = JSONResponse({"ok": True})
    current = request.cookies.get(SESSION_COOKIE, "")
    found = (
        await run_in_threadpool(device_for, request.app.state.tiffin, current) if current else None
    )
    if found is None:
        response.delete_cookie(
            SESSION_COOKIE,
            path=str(request.app.state.tiffin.config.get("web.root_path", "/tiffin")) or "/",
        )
    return response


#: headers a reverse proxy adds; a request that carries one did not come from the container itself
_FORWARDED = ("x-forwarded-for", "forwarded", "x-real-ip", "x-forwarded-host")


async def _may_see_health_detail(request: Request) -> bool:
    """Full health detail goes to the container's own loopback check and to a signed-in owner;
    anyone else on the internet gets the status word and nothing more."""
    client = request.client.host if request.client else ""
    if is_loopback(client) and not any(h in request.headers for h in _FORWARDED):
        return True
    app = request.app.state.tiffin
    person = None
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        found = await run_in_threadpool(device_for, app, token)
        person = None if found is None else found[0]
    elif (
        str(app.config.get("web.identity_mode", "telegram")) == "header"
        and app.config.get("web.trusted_header_proxy", False) is True
        and request.headers.get(HEADER)
    ):
        person = await run_in_threadpool(Resolver(app).by_login, request.headers[HEADER])
    return person is not None and person.is_admin


@router.get("/api/version")
@router.get("/version")
async def version(request: Request) -> dict[str, str]:
    """Release and commit, for the container's own check and the signed-in owner. Anyone else,
    a stranger or the partner through the proxy, is refused like every other route. No secret,
    path or person in the reply."""
    if not await _may_see_health_detail(request):
        raise HTTPException(status_code=403, detail="Not allowed")
    return release.info()


@router.get("/api/health")
async def health(request: Request) -> dict[str, Any]:
    """No identity or personal data. The public answer is the status word alone; the detail
    below goes only to the owner when signed in, or to the container's own check."""
    app = request.app.state.tiffin
    detail = await _may_see_health_detail(request)
    paths = app.config.paths
    hb = Heartbeat(paths.state, app.clock).age_s()
    backup: dict[str, Any] | None = None
    try:
        backup = json.loads((paths.state / "backup_last.json").read_text(encoding="utf-8"))
    except OSError, ValueError:
        backup = None
    db_ok = False
    try:
        db = _db(request)
        try:
            db_ok = db.scalar("SELECT 1") == 1
        finally:
            db.close()
    except Exception:
        db_ok = False
    started = getattr(request.app.state, "started_at", None)
    stale_after = int(app.config.get("ops.heartbeat_stale_s", 180))
    degraded = (not db_ok) or (app.config.is_prod and (hb is None or hb >= stale_after))
    if not detail:
        return {"status": "degraded" if degraded else "ok"}
    return {
        "status": "degraded" if degraded else "ok",
        "env": app.config.env,
        "version": app.version,
        "uptime_s": None if started is None else round(app.clock.now_utc().timestamp() - started),
        "heartbeat_age_s": hb,
        "db": db_ok,
        "last_backup": None
        if backup is None
        else {"ok": backup.get("ok"), "day": backup.get("day")},
    }


@router.get("/api/me")
async def me(request: Request, response: Response) -> dict[str, Any]:
    person = _person(request)
    app = request.app.state.tiffin
    # The token is signed to this session, so it is the same every time for the same session: a
    # second tab, or the PWA alongside a browser tab, holds a token that still works. No cookie
    # carries it, so nothing on the same address can plant one.
    token = csrf_token_for(request)
    if request.cookies.get(LEGACY_CSRF_COOKIE) is not None:
        response.delete_cookie(
            LEGACY_CSRF_COOKIE, path=str(app.config.get("web.root_path", "/tiffin")) or "/"
        )

    def choosing(db: Db) -> dict[str, Any]:
        from ..food import choices, planning  # noqa: PLC0415 (food imports the db layer)
        from ..targets import profile_on  # noqa: PLC0415

        today = _today(request)
        ahead = choices.week_days(today)
        # the week a build now would be for: choosing booked from tomorrow shows Choose today
        week = profile_on(db, person.id, ahead[0] if ahead else today)
        return {"choices": choices.per_slot_of(week), "plan": planning.view(db, person.id, today)}

    seen = await _read(request, choosing)
    return {
        "person": {"slug": person.slug, "display_name": person.display_name, "tone": person.tone},
        "units": {
            **app.config.section("units"),
            "price_decimals": price_decimals(str(app.config.get("units.currency", "GBP"))),
            "zero_decimal_currencies": sorted(ZERO_DECIMAL_CURRENCIES),
        },
        # whether Menu shows Choose: weekly choices on, and how many dishes a meal
        "choices": seen["choices"],
        # how they plan now, and any change booked for a later day (food/planning.py)
        "plan": seen["plan"],
        "today": isoday(_today(request)),
        "tz": str(app.config.get("env.tz", "Europe/London")),
        "csrf": token,
    }


#: what each job is called on the status strip; anything unlisted shows its own name
JOB_LABELS = {
    "menu_propose": "Menu build",
    "shopping_list": "Shopping list",
    "shopping_joint": "Weekend shopping list",
    "shop_refresh": "Shop prices",
    "shop_check": "Shop availability check",
    "weekly_calibration": "Weekly calibration",
    "backup": "Backup",
    "morning_targets": "Morning message",
    "evening_confirm": "Evening check-in",
}


@router.get("/api/status")
async def status(request: Request) -> dict[str, Any]:
    """Return recent job failures for the owner's status strip.

    The strip combines recorded notices and degraded or failed job runs."""
    from .. import notices  # noqa: PLC0415 (tiny, and only this route needs it)

    person = _person(request)
    tiffin = request.app.state.tiffin
    if not person.is_admin:
        return {"items": []}
    clock = tiffin.clock
    since = (clock.now_utc() - dt.timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")

    def read(db: Db) -> list[dict[str, Any]]:
        rows = db.all(
            "SELECT job, person_id, status, COALESCE(detail, error, '') AS why, "
            "COALESCE(ended_at, started_at) AS at FROM run_ledger "
            "WHERE status IN ('degraded', 'failed') AND started_at >= ? "
            "ORDER BY started_at DESC",
            (since,),
        )
        names = {p.id: p.display_name or p.slug for p in tiffin.registry(db).active()}
        latest: dict[tuple[str, int], dict[str, Any]] = {}
        for r in rows:
            key = (str(r["job"]), int(r["person_id"] or 0))
            if key in latest:
                continue
            who = names.get(int(r["person_id"] or 0))
            label = JOB_LABELS.get(str(r["job"]), str(r["job"]).replace("_", " "))
            latest[key] = {
                "at": str(r["at"]),
                "what": f"{label}{f' ({who})' if who else ''}: {r['status']}",
                "why": str(r["why"])[:200],
            }
        return list(latest.values())

    items = await _read(request, read)
    # the same day as the ledger rows: the strip says "in the last day", and read notices from
    # two days back under that heading while counting only the first five of what it had
    for n in notices.recent(tiffin.config.paths.state, hours=24, clock=clock):
        items.append({"at": n["at"], "what": n["event"].replace("_", " "), "why": n["text"][:200]})
    items.sort(key=lambda i: i["at"], reverse=True)
    return {"items": items[:8], "total": len(items)}
