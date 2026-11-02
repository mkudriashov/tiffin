"""Linking to a partner from the page: the same rules as /link in the bot (`links.py`).

The person is the signed-in one, as everywhere. Nothing here takes a person from the request,
and nothing returns anything of the partner but a name and whether you are linked.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from .. import links
from ..db import Db
from ..food import shared
from ..telegram.linkflow import UNLINKED_TO_OTHER, linked_message
from ..telegram.render import esc
from .api_common import _body, _person, _read, _web_event, check_fence

router = APIRouter()
logger = logging.getLogger(__name__)


def _view(db: Db, person_id: int) -> dict[str, Any]:
    st = links.state(db, person_id)
    return {
        "state": st.kind,
        "partner": shared.name_of(db, st.partner_id)
        if st.kind == "linked" and st.partner_id
        else None,
        "expires": st.expires_at,
        "partner_joined": st.partner_joined,
        # linked, and both have said yes to sharing weekend menus
        "sharing": st.kind == "linked" and shared.linked_and_both_consented(db),
    }


def _tell(request: Request, person_id: int, kind: str, key: str, text: str) -> bool:
    """Say it in the other person's chat. A message that cannot go is logged and reported; the
    link itself has already changed and stays so."""
    app = request.app.state.tiffin

    def send(db: Db) -> bool:
        target = app.registry(db).by_id(person_id)
        if target is None or target.chat_id is None:
            return False
        return bool(app.outbox(db).send(target, kind, key, esc(text)).sent)

    return send_safely(request, send)


def send_safely(request: Request, fn) -> bool:
    app = request.app.state.tiffin
    db = Db.open(app.config.paths.db, clock=app.clock, migrate_first=False)
    try:
        return bool(fn(db))
    except Exception:
        logger.warning("link notice not sent", extra={"event": "link_notice_failed"})
        return False
    finally:
        db.close()


@router.get("/api/link")
async def link_state(request: Request) -> dict[str, Any]:
    person = _person(request)
    return await _read(request, _view, person.id)


@router.post("/api/link/mint")
async def mint(request: Request) -> dict[str, Any]:
    person = _person(request)
    body = await _body(request)
    check_fence(request, body, set())

    def write(db: Db) -> dict[str, Any]:
        try:
            code = links.mint(db, person.id, "web")
        except links.LinkError as e:
            raise HTTPException(status_code=409, detail=str(e)) from None
        _web_event(db, person.id, "link_mint", {})
        return {"code": code, **_view(db, person.id)}

    return await _read(request, write)


@router.post("/api/link/accept")
async def accept(request: Request) -> dict[str, Any]:
    person = _person(request)
    body = await _body(request)
    check_fence(request, body, {"code"})
    code = body.get("code")
    if not isinstance(code, str) or len(code) > 40:
        raise HTTPException(status_code=400, detail="Type the code your partner gave you.")

    def write(db: Db) -> dict[str, Any]:
        try:
            link_id = links.accept(db, person.id, code, "web")
        except links.LinkError as e:
            raise HTTPException(status_code=409, detail=str(e)) from None
        _web_event(db, person.id, "link_accept", {"link_id": link_id})
        partner = links.partner_of(db, person.id)
        both = shared.linked_and_both_consented(db)
        return {
            **_view(db, person.id),
            "_partner_id": partner,
            "_link_id": link_id,
            "_text": linked_message(shared.name_of(db, person.id), both_agreed=both),
        }

    out = await _read(request, write)
    if out["_partner_id"] is not None:
        _tell(
            request,
            out["_partner_id"],
            "link_accepted",
            f"link_accepted:{out['_link_id']}",
            out["_text"],
        )
    return {k: v for k, v in out.items() if not k.startswith("_")}


@router.post("/api/link/cancel")
async def cancel(request: Request) -> dict[str, Any]:
    person = _person(request)
    body = await _body(request)
    check_fence(request, body, set())

    def write(db: Db) -> dict[str, Any]:
        if links.decline(db, person.id, "web"):
            _web_event(db, person.id, "link_cancel", {})
        return _view(db, person.id)

    return await _read(request, write)


@router.post("/api/link/unlink")
async def unlink(request: Request) -> dict[str, Any]:
    person = _person(request)
    body = await _body(request)
    check_fence(request, body, set())

    def write(db: Db) -> dict[str, Any]:
        try:
            done = links.unlink(db, person.id, "web")
        except links.LinkError as e:
            raise HTTPException(status_code=409, detail=str(e)) from None
        _web_event(db, person.id, "link_unlink", {"link_id": done.link_id})
        return {
            **_view(db, person.id),
            "_other": done.other_id,
            "_link_id": done.link_id,
            "_name": shared.name_of(db, person.id),
        }

    out = await _read(request, write)
    _tell(
        request,
        out["_other"],
        "link_ended",
        f"link_ended:{out['_link_id']}",
        UNLINKED_TO_OTHER.format(name=out["_name"] or "Your partner"),
    )
    return {k: v for k, v in out.items() if not k.startswith("_")}
