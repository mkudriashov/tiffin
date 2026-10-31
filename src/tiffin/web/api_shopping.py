"""Shopping routes for the web page."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from ..clock import isoday, parse_day
from ..db import Db
from ..errors import NothingToBuild
from . import queries
from .api_common import (
    _body,
    _day_arg,
    _number,
    _person,
    _read,
    _row_id,
    _today,
    _web_event,
    check_fence,
)

router = APIRouter()


@router.post("/api/shopping/product")
async def add_product(request: Request) -> dict[str, Any]:
    """Save a purchase option for the signed-in person's food on a readable list."""
    from ..food import products  # noqa: PLC0415
    from ..food import shopping as shopping_mod  # noqa: PLC0415

    person = _person(request)
    body = await _body(request)
    check_fence(request, body, {"item_id", "name", "pack_g", "price", "shop", "url"})
    item_id = _row_id(body.get("item_id"), "item id")
    name = body.get("name")
    if not isinstance(name, str):
        raise HTTPException(status_code=400, detail="product name is required")
    raw_pack = body.get("pack_g")
    if not isinstance(raw_pack, str | int | float) or isinstance(raw_pack, bool):
        raise HTTPException(status_code=400, detail="pack size must be in grams")
    pack_g = _number(raw_pack, "pack size")
    price_pence = None
    tiffin = request.app.state.tiffin
    currency = str(tiffin.config.get("units.currency", "GBP"))
    raw_price = body.get("price")
    if raw_price not in (None, ""):
        try:
            price_pence = products.price_hundredths(str(raw_price), currency)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
    shop, url = body.get("shop", ""), body.get("url", "")
    if not isinstance(shop, str) or not isinstance(url, str):
        raise HTTPException(status_code=400, detail="shop and link must be text")

    def write(db: Db) -> dict[str, Any]:
        item = db.one(
            "SELECT si.list_id, i.code FROM shopping_items si "
            "JOIN ingredients i ON i.id = si.ingredient_id WHERE si.id = ?",
            (item_id,),
        )
        if item is None or shopping_mod.list_row(db, int(item["list_id"]), person.id) is None:
            raise HTTPException(status_code=404, detail="no such shopping item")
        try:
            with db.tx():
                product_id = products.record_manual(
                    db,
                    person.id,
                    ingredient_code=str(item["code"]),
                    name=name,
                    pack_g=pack_g,
                    price_pence=price_pence,
                    shop=shop,
                    url=url,
                    currency=currency,
                )
                _web_event(db, person.id, "product_add", {"product_id": product_id})
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return {"product_id": product_id, "said": "Saved for the next shopping list."}

    return await _read(request, write)


@router.get("/api/shopping")
async def shopping(request: Request) -> dict[str, Any]:
    person = _person(request)
    raw = request.query_params.get("list")
    list_id = _row_id(raw, "list id") if raw else None
    from ..jobs import shop_refresh  # noqa: PLC0415 (jobs import the scheduler)

    tiffin = request.app.state.tiffin
    retailers = shop_refresh.retailer_order(tiffin.config)
    elsewhere = shop_refresh.bought_elsewhere(tiffin.config)
    if list_id is not None:
        # today rides along so the list named by a link can still point at the other live one
        return await _read(
            request,
            queries.shopping,
            person.id,
            list_id,
            _today(request),
            retailers,
            None,
            elsewhere,
        )
    # no list named: the one for the cook she is shopping for, as /shopping answers it
    return await _read(
        request,
        queries.shopping,
        person.id,
        None,
        _today(request),
        retailers,
        tiffin.clock,
        elsewhere,
    )


@router.post("/api/shopping/tick")
async def tick(request: Request) -> dict[str, Any]:
    person = _person(request)
    body = await _body(request)
    check_fence(request, body, {"item_id", "ticked"})
    if "item_id" not in body or "ticked" not in body:
        raise HTTPException(status_code=400, detail="item_id and ticked are required")
    item_id = _row_id(body["item_id"], "item_id")
    if not isinstance(body["ticked"], bool):
        # every other field on this fence is type-checked, and bool() is the one that fails in
        # the harmful direction: bool("false") is True, so a body that says false ticks the item
        raise HTTPException(status_code=400, detail="ticked must be true or false")
    ticked = body["ticked"]

    def write(db: Db) -> dict[str, Any]:
        with db.tx():
            if not queries.tick_item(db, person.id, item_id, ticked):
                raise HTTPException(status_code=404, detail="no such item")
            _web_event(db, person.id, "shopping_tick", {"item_id": item_id, "ticked": ticked})
        return {"item_id": item_id, "ticked": ticked}

    return await _read(request, write)


@router.post("/api/shopping/send")
async def shopping_send(request: Request) -> dict[str, Any]:
    """The list as plain text to their own Telegram, to forward to whoever does the shop.
    Only a list they may read; it goes to their chat and nobody else's."""
    person = _person(request)
    body = await _body(request)
    check_fence(request, body, {"list_id"})
    list_id = _row_id(body.get("list_id"), "list_id")
    tiffin = request.app.state.tiffin

    def write(db: Db) -> dict[str, Any]:
        from ..food import shopping as shopping_mod  # noqa: PLC0415
        from ..telegram import menuflow  # noqa: PLC0415

        if shopping_mod.list_row(db, list_id, person.id) is None:
            raise HTTPException(status_code=404, detail="no such list")
        sent = menuflow.send_list_text(tiffin.outbox(db), db, person, list_id)
        _web_event(db, person.id, "shopping_send", {"list_id": list_id})
        if any(r.status == "failed" for r in sent):
            raise HTTPException(status_code=502, detail="Telegram did not take it. Try again.")
        return {"sent": len(sent)}

    return await _read(request, write)


@router.post("/api/shopping/have")
async def shopping_have(request: Request) -> dict[str, Any]:
    """ "I have it": a line their own list need not buy for these days, or bought after all.
    The person is the header's; the days are the list's."""
    person = _person(request)
    body = await _body(request)
    check_fence(request, body, {"item_id", "have"})
    if "item_id" not in body or not isinstance(body.get("have"), bool):
        raise HTTPException(status_code=400, detail="item_id, and have as true or false")
    item_id = _row_id(body["item_id"], "item_id")
    have = body["have"]

    def write(db: Db) -> dict[str, Any]:
        with db.tx():
            name = queries.mark_have(db, person.id, item_id, have)
            if name is None:
                raise HTTPException(status_code=404, detail="That line is not on a list of yours.")
            _web_event(db, person.id, "shopping_have", {"item_id": item_id, "have": have})
        said = (
            f"{name}: at home, so this list does not buy it."
            if have
            else f"{name}: back on the list."
        )
        return {"item_id": item_id, "have": have, "said": said}

    return await _read(request, write)


@router.post("/api/packs/mains")
async def packs_mains(request: Request) -> dict[str, Any]:
    """How many meats or fish a week their menus keep to: two, three, or no limit (0)."""
    from ..food import packs  # noqa: PLC0415 (food imports the db layer)

    person = _person(request)
    body = await _body(request)
    check_fence(request, body, {"mains"})
    mains = body.get("mains")
    if not isinstance(mains, int) or isinstance(mains, bool) or mains not in packs.MAINS_ANSWERS:
        raise HTTPException(status_code=400, detail="mains is 2, 3 or 0 (no limit)")

    def write(db: Db) -> dict[str, Any]:
        with db.tx():
            said = packs.set_mains(db, person.id, mains, source="page")
            _web_event(db, person.id, "packs_mains", {"mains": mains})
        return {"mains": mains, "said": said}

    return await _read(request, write)


@router.post("/api/shopping/from")
async def shopping_from(request: Request) -> dict[str, Any]:
    """Build the shopping list from a chosen cooking day onward.

    Days already missed are excluded so the list buys only meals still to come."""
    person = _person(request)
    body = await _body(request)
    check_fence(request, body, {"list_id", "day"})
    if "list_id" not in body or "day" not in body:
        raise HTTPException(status_code=400, detail="list_id and day are required")
    list_id = _row_id(body["list_id"], "list id")
    try:
        day = parse_day(str(body["day"]))
    except ValueError:
        raise HTTPException(status_code=400, detail="day must be YYYY-MM-DD") from None
    from ..jobs import shop_refresh  # noqa: PLC0415 (jobs import the scheduler)

    tiffin = request.app.state.tiffin
    retailers = shop_refresh.retailer_order(tiffin.config)

    def write(db: Db) -> dict[str, Any]:
        with db.tx():
            new_id = queries.shop_from(db, person.id, list_id, day, retailers, tiffin.clock)
            if new_id is None:
                raise HTTPException(status_code=404, detail="That list has no such day.")
            _web_event(db, person.id, "shopping_from", {"list_id": new_id, "day": isoday(day)})
        return {"list_id": new_id, "day": isoday(day)}

    return await _read(request, write)


@router.post("/api/shopping/skip")
async def shopping_skip(request: Request) -> dict[str, Any]:
    """Skip a meal from the shopping list, or bring it back: its food comes off the list (or
    already removes it from the shopping list"). The person's own kind of Skip (`/set skip`):
    hers drops the meal and leaves the rest of the day as planned. The person is the header's."""
    from ..errors import Infeasible  # noqa: PLC0415
    from ..food import menu as menu_mod  # noqa: PLC0415 (food imports the db layer)
    from ..food import plates  # noqa: PLC0415

    person = _person(request)
    body = await _body(request)
    check_fence(request, body, {"day", "slot", "skip"})
    raw_day = body.get("day")
    if not isinstance(raw_day, str) or not raw_day:
        raise HTTPException(status_code=400, detail="day is required")
    day = _day_arg(raw_day, _today(request))
    slot = body.get("slot")
    if not isinstance(slot, str) or slot not in menu_mod.SLOT_ORDER:
        raise HTTPException(status_code=400, detail="slot is a meal of the day")
    skip = body.get("skip")
    if not isinstance(skip, bool):
        raise HTTPException(status_code=400, detail="skip is true or false")
    tiffin = request.app.state.tiffin

    def write(db: Db) -> dict[str, Any]:
        try:
            said = plates.skip_meal(
                db, person.id, day, slot, skip, config=tiffin.config, clock=tiffin.clock
            )
        except NothingToBuild as e:
            raise HTTPException(status_code=409, detail=str(e)) from None
        except Infeasible as e:
            raise HTTPException(status_code=422, detail=str(e)) from None
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from None
        _web_event(db, person.id, "shopping_skip", {"day": isoday(day), "skip": skip})
        return {"said": said, "day": isoday(day), "slot": slot, "skipped": skip}

    return await _read(request, write)


@router.post("/api/shopping/out")
async def shopping_out(request: Request) -> dict[str, Any]:
    """ "The shop had none" - the one thing a ticked-or-not row could not say (shopping_tab/10).

    Writes the same `shop_misses` row `/shopping out chickpeas` writes from Telegram: the shop
    stops being asked for it for a while, and the menu is left alone - a food this shop lacks is
    not a food the person refuses.
    """
    person = _person(request)
    body = await _body(request)
    check_fence(request, body, {"item_id", "undo"})
    if "item_id" not in body:
        raise HTTPException(status_code=400, detail="item_id is required")
    item_id = _row_id(body["item_id"], "item_id")
    # a mistaken tap taken back: the shop is asked about it again
    undo = body.get("undo", False)
    if not isinstance(undo, bool):
        raise HTTPException(status_code=400, detail="undo is true or false")
    from ..jobs import shop_refresh  # noqa: PLC0415 (jobs import the scheduler)

    retailer = shop_refresh.default_retailer(request.app.state.tiffin.config) or "other"

    def write(db: Db) -> dict[str, Any]:
        with db.tx():
            if not queries.mark_shelf_empty(db, person.id, item_id, retailer, undo=undo):
                # an Undo with no mark of theirs to take back changes nothing
                said = "Nothing to take back: that food is not marked empty." if undo else None
                raise HTTPException(status_code=404, detail=said or "no such item")
            _web_event(db, person.id, "shopping_out", {"item_id": item_id, "undo": undo})
        return {"item_id": item_id, "out": not undo}

    return await _read(request, write)
