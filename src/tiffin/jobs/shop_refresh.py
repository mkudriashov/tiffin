"""Optional supermarket product reader.

Product observations are append-only. The reader prioritises foods on current menus
and never changes a menu or its nutritional calculations.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Collection, Sequence
from typing import Any

from .. import ledger
from ..clock import Clock, isoday
from ..db import Db
from ..errors import TiffinError
from ..food import catalogue, products
from ..scheduler import SYSTEM, Job, JobContext, fixed

SUNDAY = frozenset({6})
#: how many foods one weekly run looks up; each is a search plus a product page
BATCH = 12
#: a product seen inside this many days is left alone
#: how long a price read stays good enough to skip. Fourteen days was right while the
#: catalogue was empty and the job was filling it: everything was stale, so the queue was simply
#: "everything" and the order decided what got read. Now that every food has a product, fourteen
#: days is a promise the cadence cannot keep - one weekly pass of six foods refreshes twelve in
#: a fortnight, against eighty-four foods at each of two shops, so the queue would be permanently
#: stale and the ordering would be the only thing that mattered anyway. Sixty says what a weekly
#: six can actually hold, and a price that has moved more than a little in two months is a price
#: worth re-reading rather than one the list quietly kept.
FRESH_DAYS = 60
#: how many times running a food may be reported 'unread' at one shop before it is left alone
#: like an unstocked one. An 'unread' miss is deliberately not parked - the session fell short,
#: not the shop - but "never parked" and "read the whole catalogue back to back" together make a
#: loop: `stale_foods` is ordered, so six foods that cannot be read lead every single pass and a
#: forty-pass backfill spends all forty on them. Three tries is enough to tell a session that ran
#: out of turns from a food this shop will not show at all (Ocado wants a postcode; a page needs a
#: sign-in). It resets by itself: one successful read makes the food fresh and the count moot.
UNREAD_TRIES = products.UNREAD_TRIES

#: a food the shop did not stock is left alone this long. Longer than FRESH_DAYS because a
#: range changes far more slowly than a price, and shorter than forever because it does change.
MISS_DAYS = 30
#: how many missing foods a shopping list will stop to read before it goes out. Small on
#: purpose: this runs in front of a message somebody is waiting for, and the weekly slice is
#: what covers the long tail.
FILL_LIMIT = 6


def _catalogue(db) -> list[catalogue.Ingredient]:
    """The approved foods, without a person.

    This job is `scope="system"`: there is no person, and `catalogue.approved(db, 0)` was
    asking the person-scoped reader for person zero. That reader starts with
    `SELECT slug FROM persons WHERE id = ?` and raises `LookupError: no person with id 0`,
    and the slug CHECK in the schema means person zero can never exist - so every run since
    this job was written has failed on its first line, no product has ever been recorded, and
    the shopping list has been rounding to hand-typed packs the whole time. Refusals are
    per-person and belong to the reader; a shop read is not.
    """
    return [i for i in catalogue.latest_versions(db) if i.approved]


def menu_codes(db: Db, menu_ids: Sequence[int] | None = None) -> set[str]:
    """The ingredient codes a menu actually cooks with - both people's, by default.

    `v_menu_current` is per-person, so with no argument this is the union of whatever each of
    them is eating now. That union is the right priority list precisely because it is a union:
    her menu and his draw on different catalogues, and a queue that favoured one person's foods
    would leave the other reading generic lines for a month.
    """
    if menu_ids is None:
        rows = db.all(
            "SELECT DISTINCT i.code FROM v_menu_current m "
            "JOIN menu_days md ON md.menu_id = m.id "
            "JOIN meal_slots ms ON ms.menu_day_id = md.id "
            "JOIN recipe_ingredients ri ON ri.recipe_id = ms.recipe_id "
            "JOIN ingredients i ON i.id = ri.ingredient_id"
        )
        return {str(r["code"]) for r in rows}
    if not menu_ids:
        return set()
    rows = db.all(
        "SELECT DISTINCT i.code FROM menu_days md "
        "JOIN meal_slots ms ON ms.menu_day_id = md.id "
        "JOIN recipe_ingredients ri ON ri.recipe_id = ms.recipe_id "
        "JOIN ingredients i ON i.id = ri.ingredient_id "
        f"WHERE md.menu_id IN ({','.join('?' for _ in menu_ids)})",
        tuple(menu_ids),
    )
    return {str(r["code"]) for r in rows}


def _since(db: Db, days: int) -> str:
    """The stamp `days` ago by Tiffin's own clock. `datetime('now')` is SQLite's, which is
    not the clock the rows were stamped with: every window read off it drifted away from a frozen
    test clock, and three tests were set to fail on 7 Oct with no change to the code."""
    return (db.clock.now_utc() - dt.timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


def _fresh(db: Db, retailer: str) -> set[str]:
    """Codes with a product seen at this shop inside FRESH_DAYS."""
    return {
        str(r["ingredient_code"])
        for r in db.all(
            "SELECT DISTINCT ingredient_code FROM products WHERE retailer = ? AND seen_at >= ?",
            (retailer, _since(db, FRESH_DAYS)),
        )
    }


def _latest_misses(db: Db, retailer: str) -> dict[str, str]:
    """Read each shop's latest current verdict for a food."""
    counts = "(m.reader = ? OR m.reason LIKE ?)"
    keep, kept = products.standing("x")
    return {
        str(r["ingredient_code"]): str(r["kind"])
        for r in db.all(
            f"SELECT m.ingredient_code, m.kind FROM shop_misses m WHERE m.retailer = ? "
            f"AND m.seen_at >= ? AND {counts} AND m.id = (SELECT MAX(x.id) FROM shop_misses x "
            "WHERE x.retailer = m.retailer AND x.ingredient_code = m.ingredient_code "
            f"AND {counts.replace('m.', 'x.')} AND {keep})",
            (
                retailer,
                _since(db, MISS_DAYS),
                products.READER_VERSION,
                products.PERSON_SAID_LIKE,
                products.READER_VERSION,
                products.PERSON_SAID_LIKE,
                *kept,
            ),
        )
    }


def _missed(db: Db, retailer: str) -> set[str]:
    """Codes this shop was asked for and did not HAVE, inside MISS_DAYS - its latest verdict.

    `kind = 'unstocked'` only. A food the session never got to - out of turns, out of budget,
    blocked - is not a food this shop does not sell, and parking it for thirty days on that
    basis is how ten foods on a five-day shopping list stayed unpriced while the queue reported
    itself empty. Those come back round the next time something reads.
    """
    return {code for code, kind in _latest_misses(db, retailer).items() if kind == "unstocked"}


def _basis(db: Db, retailer: str) -> set[str]:
    """Codes the shop has, whose page gave figures only for the cooked food UNREAD_TRIES times.

    Asked again until then: the product, its pack and its price are still wanted and the next
    read may store them. After that the answer is the page's, and asking again
    spent the batch on the same foods every hour.
    """
    return {
        str(r["ingredient_code"])
        for r in db.all(
            "SELECT ingredient_code FROM shop_misses WHERE retailer = ? AND kind = 'basis' "
            "AND reader = ? AND seen_at >= ? GROUP BY ingredient_code HAVING COUNT(*) >= ?",
            (retailer, products.READER_VERSION, _since(db, MISS_DAYS), UNREAD_TRIES),
        )
    }


def _unreadable(db: Db, retailer: str) -> set[str]:
    """Codes this shop has failed to SHOW, UNREAD_TRIES times over, inside MISS_DAYS.

    Not the same thing as `_missed`, which is the shop's own answer ("we do not sell that").
    This is the session's, repeated often enough to stop being about the session: after three
    goes at one shop, whatever is in the way is not a run of bad luck, and a batch of six has
    better uses for the slot. A read that works clears it, because the food is then fresh.
    """
    return {
        str(r["ingredient_code"])
        for r in db.all(
            "SELECT ingredient_code FROM shop_misses WHERE retailer = ? AND kind = 'unread' "
            "AND reader = ? AND seen_at >= ? GROUP BY ingredient_code HAVING COUNT(*) >= ?",
            (retailer, products.READER_VERSION, _since(db, MISS_DAYS), UNREAD_TRIES),
        )
    }


def stale_foods(
    ctx: JobContext,
    retailer: str,
    limit: int = BATCH,
    *,
    codes: Collection[str] | None = None,
) -> list[dict[str, Any]]:
    """Prioritise menu foods without a recent product at a shop."""
    app = getattr(ctx, "app", None)
    skip = (
        _fresh(ctx.db, retailer)
        | _missed(ctx.db, retailer)
        | _unreadable(ctx.db, retailer)
        | _basis(ctx.db, retailer)
        # bought somewhere else on purpose (the owner's own whey): never read, by any path - the
        # Sunday read and the list's top-up still queued it, and its "not sold" verdict then
        # cancelled the tub already at home
        | (bought_elsewhere(app.config) if app is not None else frozenset())
    )
    on_menu = menu_codes(ctx.db)
    wanted = None if codes is None else set(codes)
    out: list[dict[str, Any]] = []
    # `code not in on_menu` sorts False (0) before True (1), so the menu's own foods lead
    for item in sorted(_catalogue(ctx.db), key=lambda i: (i.code not in on_menu, i.pantry, i.code)):
        if item.code in skip or (wanted is not None and item.code not in wanted):
            continue
        out.append(
            {
                "code": item.code,
                "name": item.name,
                "kcal_100": item.kcal_100,
                "protein_100": item.protein_100,
                "typical_pack_g": item.pack_g,
            }
        )
        if len(out) >= limit:
            break
    return out


def search_url_for(config: Any, retailer: str) -> str:
    """The search URL template for a shop, or "" when nobody has configured one.

    Kept in config rather than in the browser skill so that adding a shop is a line of TOML and
    a name in the products.retailer CHECK, not an edit to a prompt the job reads at runtime.
    """
    urls = config.get("shop.search_urls", {})
    return str(urls.get(retailer, "")) if isinstance(urls, dict) else ""


def _sku_of(row: dict[str, Any]) -> str:
    """The product's id, or a stand-in that keys it the same way next time.

    A product reported from the search results, because its own page would not load, may come
    without the id - and was dropped for it, so a food the shop lists read as not found (26 Sep
    2026). The link's last path segment is the id on these sites; failing that, the name.
    """
    sku = str(row.get("retailer_sku") or "").strip()
    if sku:
        return sku
    tail = str(row.get("url") or "").split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
    if tail and "." not in tail:
        return tail
    name = " ".join(str(row.get("name") or "").lower().split())
    return f"name:{name}" if name else ""


def store(ctx: JobContext, retailer: str, rows: list[dict[str, Any]]) -> tuple[int, list[str]]:
    """Write each product the session read. Returns (written, notes worth a human's eye)."""
    known = {i.code: i for i in _catalogue(ctx.db)}
    written = 0
    notes: list[str] = []
    for row in rows:
        code = str(row.get("code") or "")
        item = known.get(code)
        sku = _sku_of(row)
        if item is None or not sku or not row.get("name"):
            continue
        pid = products.record(
            ctx.db,
            ingredient_code=code,
            retailer=retailer,
            retailer_sku=sku,
            name=str(row["name"]),
            source="chrome",
            clock=ctx.clock,
            url=str(row.get("url") or "")[:400] or None,
            pack_g=_num(row.get("pack_g")),
            pack_label=str(row.get("pack_label") or "")[:80] or None,
            price_pence=_int(row.get("price_pence")),
            price_per_kg_pence=_int(row.get("price_per_kg_pence")),
            kcal_100=_num(row.get("kcal_100")),
            protein_100=_num(row.get("protein_100")),
            fat_100=_num(row.get("fat_100")),
            carb_100=_num(row.get("carb_100")),
            fibre_100=_num(row.get("fibre_100")),
            salt_100=_num(row.get("salt_100")),
            # why the figures above are missing, when the page had figures but for the cooked
            # food: the pack and the price are still real and the list still wants them
            nutrition_basis=str(row.get("nutrition_basis") or "")[:40] or None,
        )
        written += 1
        found = [p for p in products.for_ingredient(ctx.db, code, retailer) if p.id == pid]
        if found:
            why = products.suspect(found[0], item.kcal_100)
            if why:
                notes.append(why)
    return written, notes


#: words a session uses when it stopped before reading a food rather than deciding about one.
#: The session is asked for `kind` directly (see the shop-reader skill); this is the fallback
#: for one that answers with prose alone, and for the sessions that already have.
_UNREAD_WORDS = (
    "token constraint",
    "not looked up",
    "not yet searched",
    "time exhausted",
    "incomplete",
    "blocked clicks",
    "requires live inventory",
    "ran out",
)


#: words a session uses about the PAGE rather than the shelf: the food is there, and its table
#: is for a different state of it. Not a miss to park - the pack and the price are still wanted.
_BASIS_WORDS = (
    "cooked",
    "boiled",
    "as consumed",
    "pan fried",
    "pan-fried",
    "oven cooked",
)


def _interrupted(row: dict[str, Any]) -> bool:
    """Does this miss READ as a session that never reached the page, whatever it called itself?

    Read from the reason rather than the session's `kind`, because the alarm below exists for
    the case where the session's own account of itself is what cannot be trusted.
    """
    reason = str(row.get("reason") or "").lower()
    return any(word in reason for word in _UNREAD_WORDS) or not reason


#: words that say the box, not the session, was the problem: nothing about the foods is known
_NOT_THE_SESSION = (
    "usage limit",
    "limit reached",
    "rate limit",
    "rate_limit",
    "quota",
    "log in",
    "login",
    "logged out",
    "sign in",
    "not connected",
    "extension",
    "overloaded",
)


def session_fell_short(error: str) -> bool:
    """Did the session run and stop short (out of turns or budget, or ending without an answer),
    as against never getting going (a usage limit, a login, the extension not connected)?"""
    low = error.casefold()
    return not any(word in low for word in _NOT_THE_SESSION)


def miss_kind(row: dict[str, Any]) -> str:
    """Classify a missing product result by its latest recorded reason."""
    said = str(row.get("kind") or "").strip().lower()
    if said in ("unstocked", "unread", "basis"):
        return said
    reason = str(row.get("reason") or "").lower()
    if any(word in reason for word in _UNREAD_WORDS):
        return "unread"
    # a page whose figures are for the cooked food is not a shop that does not sell it: the
    # product, its pack and its price are all still there, so this must not park the food
    if any(word in reason for word in _BASIS_WORDS):
        return "basis"
    return "unstocked"


def record_misses(ctx: JobContext, retailer: str, rows: list[dict[str, Any]]) -> int:
    """Write down the foods this shop did not have, so next week asks for something else.

    Only codes the catalogue knows: the session answers with what it was given, but a reply is
    still untrusted input and a made-up code must not become a row that silences a real food.
    """
    known = {i.code for i in _catalogue(ctx.db)}
    n = 0
    for row in rows:
        code = str(row.get("code") or "")
        if code not in known:
            continue
        ctx.db.insert(
            "shop_misses",
            ingredient_code=code,
            retailer=retailer,
            reason=str(row.get("reason") or "")[:200] or None,
            kind=miss_kind(row),
            seen_at=ctx.clock.utc_stamp(),
            reader=products.READER_VERSION,
        )
        n += 1
    return n


def run(
    ctx: JobContext,
    *,
    retailer: str | None = None,
    batch: int | None = None,
    codes: Collection[str] | None = None,
) -> None:
    app = ctx.app
    if not app.config.get("chrome.shop_refresh_enabled", False):
        ctx.handle.skip("chrome.shop_refresh_enabled is off")
        return
    if app.config.get("model.provider") != "claude-cli":
        ctx.handle.degrade(
            "shop refresh needs Claude CLI's Chrome browser tools; "
            "use the manual catalogue with this model provider"
        )
        return
    retailer = retailer or default_retailer(app.config)
    if not retailer:
        ctx.handle.skip("no retailer adapter configured")
        return
    search_url = search_url_for(app.config, retailer)
    if not search_url:
        # a shop with nowhere to look is a configuration mistake, not a bad shopping day
        ctx.handle.degrade(f"{retailer}: no [shop.search_urls] entry, so there is nowhere to look")
        return
    limit = batch or int(app.config.get("shop.batch", BATCH))
    foods = stale_foods(ctx, retailer, limit=limit, codes=codes)
    if not foods:
        ctx.handle.note(f"{retailer}: nothing stale to read")
        return
    from ..integrations.retailers import reader_for  # noqa: PLC0415

    try:
        data, envelope = reader_for(app.config, app.config.paths.state).read(
            retailer, search_url, foods
        )
    except TiffinError as e:
        # a session that ran and handed back nothing: its foods are still unknown and are asked
        # again, three times at most (`_unreadable`), so a food no session can reach ends up
        # but only when the SESSION fell short. A usage limit, a logged-out profile or an
        # extension not connected is the box's problem, like a closed Chrome: parking five foods
        # on it left them "not checked" for a month after one bad hour
        if not session_fell_short(str(e)):
            ctx.handle.degrade(f"{retailer}: {type(e).__name__}: {e}")
            return
        record_misses(
            ctx,
            retailer,
            [
                {"code": f["code"], "kind": "unread", "reason": "the session returned no answer"}
                for f in foods
            ],
        )
        ctx.handle.degrade(f"{retailer}: {type(e).__name__}: {e}")
        return

    rows = data.get("products")
    written, notes = store(ctx, retailer, rows if isinstance(rows, list) else [])
    missing = data.get("not_found")
    n_missing = record_misses(ctx, retailer, missing if isinstance(missing, list) else [])
    # the gate line says what the unattended session was allowed to do in the owner's browser
    gate = str(envelope.get("gate") or "")
    priced_only = sum(
        1
        for r in (rows if isinstance(rows, list) else [])
        if r.get("nutrition_basis") and r.get("kcal_100") is None
    )
    kept = f"stored {written}" + (f" ({priced_only} pack and price only)" if priced_only else "")
    line = f"{retailer}: asked for {len(foods)}, {kept}, {n_missing} not found" + (
        f"; {gate}" if gate else ""
    )
    unread = [row for row in (missing if isinstance(missing, list) else []) if _interrupted(row)]
    accounted = n_missing - len(unread)
    if foods and not written and accounted < len(foods):
        ctx.handle.degrade(
            line + " - nothing stored and nothing read; check this shop's search URL"
        )
        return
    ctx.handle.note(line)
    if notes:
        ctx.outbox.alert(
            app.registry(ctx.db).admin(),
            "shop_refresh",
            "Products worth checking:\n" + "\n".join(notes[:6]),
        )


#: one batch an hour of the foods a coming menu is waiting on an answer for. Five, not six: a
#: session that had to find the product page for every food ran out of budget on six
CHECK_BATCH = 5


def bought_elsewhere(config: Any) -> frozenset[str]:
    """Foods bought somewhere other than the shops on purpose (the owner's own whey): never asked
    for."""
    value = config.get("shop.bought_elsewhere", [])
    return frozenset(str(v) for v in value) if isinstance(value, list | tuple) else frozenset()


def unanswered(db: Db, retailer: str, today: dt.date, elsewhere: Collection[str] = ()) -> list[str]:
    """Foods on a menu still to come that this shop has not answered for, nearest menu first.

    Answered means a product was found there, or the shop was read and does not sell the food
    (`unstocked`), or three sessions running could not get to it (`_unreadable`), in which case
    the list says "not checked" rather than the check waiting for ever.
    """
    rows = db.all(
        "SELECT i.code, MIN(m.segment_start) AS first FROM v_menu_current m "
        "JOIN menu_days md ON md.menu_id = m.id JOIN meal_slots ms ON ms.menu_day_id = md.id "
        "JOIN recipe_ingredients ri ON ri.recipe_id = ms.recipe_id "
        "JOIN ingredients i ON i.id = ri.ingredient_id "
        "WHERE m.segment_end >= ? GROUP BY i.code ORDER BY first, i.code",
        (isoday(today),),
    )
    found = {
        str(r["ingredient_code"])
        for r in db.all(
            "SELECT DISTINCT ingredient_code FROM products WHERE retailer = ?", (retailer,)
        )
    }
    # a page with figures only for the cooked food is still the shop selling it: an answer.
    # Left out, such a food was asked again every hour for ever
    done = (
        found
        | _missed(db, retailer)
        | _unreadable(db, retailer)
        | _basis(db, retailer)
        | set(elsewhere)
    )
    return [str(r["code"]) for r in rows if str(r["code"]) not in done]


def run_check(ctx: JobContext) -> None:
    """Can the menus still to come be bought at the head shop? One batch of the answer an hour.

    The list's own top-up reads six foods at most and the Sunday read six a shop, so a menu that
    brought in more new foods than that went to the shop partly unchecked. This keeps asking,
    five at a time, until every food on every coming menu has an answer.
    """
    shop = default_retailer(ctx.app.config)
    if not shop or not ctx.app.config.get("chrome.shop_refresh_enabled", False):
        ctx.handle.skip("no retailer adapter configured")
        return
    todo = unanswered(ctx.db, shop, ctx.day, bought_elsewhere(ctx.app.config))
    if not todo:
        ctx.handle.skip(f"{shop}: every food on a coming menu has an answer")
        return
    batch = todo[:CHECK_BATCH]
    run(ctx, retailer=shop, batch=len(batch), codes=batch)
    if len(todo) > len(batch):
        ctx.handle.note(f"{len(todo) - len(batch)} more to ask next hour")


def default_retailer(config: Any) -> str:
    """The shop a read goes to. First of `shop.retailers`, or `shop.retailer`."""
    ordered = retailer_order(config)
    return ordered[0] if ordered else ""


def retailer_order(config: Any) -> list[str]:
    """Enabled shops in preference order, including the legacy single-shop setting."""
    value = config.get("shop.retailers", None)
    if isinstance(value, str):
        value = [value]
    if isinstance(value, list | tuple):
        ordered = [str(v) for v in value if str(v) in products.RETAILERS]
        if ordered:
            return ordered
    one = str(config.get("shop.retailer", ""))
    return [one] if one in products.RETAILERS else []


def remaining(ctx: JobContext, retailer: str) -> int:
    """How many approved foods still have no product seen at this retailer inside FRESH_DAYS."""
    return len(stale_foods(ctx, retailer, limit=10_000))


def _context(app: Any, db: Db, outbox: Any, *, clock: Clock, slot: str, handle: Any) -> JobContext:
    return JobContext(
        app=app,
        db=db,
        job=JOB,
        person=None,
        profile=None,
        day=clock.today_local(),
        due_local=clock.now_local(),
        now_local=clock.now_local(),
        slot=slot,
        handle=handle,
        outbox=outbox,
        clock=clock,
    )


def backfill_once(
    app: Any,
    db: Db,
    outbox: Any,
    *,
    clock: Clock,
    retailer: str,
    batch: int,
    codes: Collection[str] | None = None,
) -> tuple[str, int]:
    """One refresh pass now, against a manual slot. Returns (ledger detail, foods left after).

    A slice a week is the right cadence for keeping prices fresh and the wrong one for getting
    started: at twelve foods a week an eighty-five food catalogue takes seven Sundays, and until
    then a shopping list names a generic food for everything the slice has not reached yet.

    Runs the scheduled job's own `run`, so the on-demand path cannot drift from the Sunday one:
    same Chrome session, same store, same suspect-label alert, same run_ledger row.
    """
    slot = f"manual-{clock.utc_stamp().replace(' ', 'T').replace(':', '')}"
    with ledger.run(db, "shop_refresh", slot, SYSTEM, clock=clock) as handle:
        ctx = _context(app, db, outbox, clock=clock, slot=slot, handle=handle)
        run(ctx, retailer=retailer, batch=batch, codes=codes)
        return handle.detail or "", remaining(ctx, retailer)


def fill_for_menus(
    app: Any,
    db: Db,
    outbox: Any,
    *,
    clock: Clock,
    menu_ids: Sequence[int],
    limit: int = FILL_LIMIT,
) -> str:
    """Read the shop for the foods on THESE menus that have no product yet. Never raises.

    This runs in front of a shopping list, which is the only moment the gap is visible and the
    last moment it can be closed: after the message goes out, a generic "Chicken breast 2 x 500
    g" is what somebody carries round the shop. A menu built on Saturday for a food the weekly
    slice has not reached is exactly the case the alphabetical walk cannot serve, however
    patiently it walks.

    Everything here is best-effort by construction. Chrome may not be running, the shop may not
    stock the food, the session may cost more than its budget - and none of that may stop a
    shopping list being sent. The failure is written to this job's own `run_ledger` row (where
    it reads as a shop problem, which is what it is) and the caller gets one line for its own.
    """
    if not menu_ids:
        return ""
    if not app.config.get("chrome.shop_fill_enabled", False):
        return ""
    retailer = default_retailer(app.config)
    if not retailer:
        return ""
    slot = f"fill-{clock.utc_stamp().replace(' ', 'T').replace(':', '')}"
    try:
        with ledger.run(db, "shop_refresh", slot, SYSTEM, clock=clock) as handle:
            ctx = _context(app, db, outbox, clock=clock, slot=slot, handle=handle)
            wanted = menu_codes(db, menu_ids)
            if not wanted:
                return ""
            gaps = stale_foods(ctx, retailer, limit=limit, codes=wanted)
            if not gaps:
                return ""
            run(ctx, retailer=retailer, batch=limit, codes=wanted)
            return handle.detail or ""
    except TiffinError as e:
        # a shopping list is worth more than a price. Say what happened and send the list.
        return f"shop fill failed: {type(e).__name__}: {e}"


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _int(value: Any) -> int | None:
    n = _num(value)
    return None if n is None else round(n)


def run_weekly(ctx: JobContext) -> None:
    """Read each enabled shop sequentially, with one browser budget per shop."""
    shops = retailer_order(ctx.app.config)
    if not shops:
        ctx.handle.skip("no retailer adapter configured")
        return
    for shop in shops:
        run(ctx, retailer=shop)


CHECK_JOB = Job(
    name="shop_check",
    scope="system",
    kind="data",
    # five past the hour: the hour's own jobs (menus at 12:00 and 18:00, lists at 18:00) are
    # queued on the system worker ahead of it, so a long session never holds one of them up
    times=fixed(*[f"{h:02d}:05" for h in range(7, 23)]),
    run=run_check,
    catch_up="none",
    needs_profile=False,
)

JOB = Job(
    name="shop_refresh",
    scope="system",
    kind="data",
    times=fixed("05:00"),
    run=run_weekly,
    catch_up="12:00",
    days=SUNDAY,
    needs_profile=False,
)
