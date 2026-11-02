"""Account routes for the web page."""

from __future__ import annotations

import datetime as dt
import json
import secrets as pysecrets
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from ..clock import isoday, stamp
from ..db import Db
from ..food.products import ZERO_DECIMAL_CURRENCIES, price_decimals
from ..heartbeat import Heartbeat
from .api_common import CSRF_COOKIE, _body, _db, _person, _read, _row_id, _today, check_fence
from .identity import IDLE_DAYS, SESSION_COOKIE, device_for

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


@router.get("/api/health")
async def health(request: Request) -> dict[str, Any]:
    """No identity or personal data; enough for a process health check."""
    app = request.app.state.tiffin
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
    # keep the token that is already in the cookie. Re-minting on every /api/me meant a second
    # page load - a second tab, or the PWA alongside a browser tab - invalidated the first
    # surface for good: every tick and every rating from it failed 403, the message auto-hid
    # after six seconds, and nothing told the person to reload.
    token = request.cookies.get(CSRF_COOKIE) or pysecrets.token_urlsafe(32)
    response.set_cookie(
        CSRF_COOKIE,
        token,
        secure=str(app.config.get("web.origin", "")).startswith("https://")
        and (app.config.is_prod or bool(app.config.get("web.secure_cookies", True))),
        samesite="strict",
        path=str(app.config.get("web.root_path", "/tiffin")) or "/",
        httponly=False,  # the page must read it to echo it back
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
