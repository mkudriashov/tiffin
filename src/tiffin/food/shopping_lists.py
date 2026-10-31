"""Lists shopping list operations."""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Collection, Sequence
from typing import Any

from ..clock import Clock, isoday, parse_day
from ..db import Db
from .catalogue import DEFAULT_AISLES
from .products import PERSON_SAID_LIKE, standing
from .shopping import (
    KEEPS_DAYS,
    LEFTOVER_DAYS,
    WEEKEND,
    _carry_ticks,
    _items,
    _needs,
    _pantry_for,
    _shop_for,
    _write,
    build_once,
    for_menu,
    joint_once,
    ticks_for,
    weekend_menu_ids,
    weekend_of_span,
)


def keeps_days(aisle: str) -> int:
    """How long a leftover of something from this aisle is still worth counting on."""
    return KEEPS_DAYS.get(aisle, LEFTOVER_DAYS)


def _menu_span(db: Db, menu_id: int | None) -> tuple[dt.date, dt.date] | None:
    if menu_id is None:
        return None
    row = db.one("SELECT segment_start, segment_end FROM menus WHERE id = ?", (menu_id,))
    if row is None:
        return None
    return parse_day(str(row["segment_start"])), parse_day(str(row["segment_end"]))


def _is_shared(db: Db, menu_id: int) -> bool:
    """Is this menu one half of a weekend cooked together (`food/shared.py`)?"""
    return db.one("SELECT 1 FROM shared_menu_members WHERE menu_id = ?", (menu_id,)) is not None


def _joint_stale(db: Db, joint: Any, start: dt.date, end: dt.date) -> bool:
    """A joint list built before one of the weekend's menus in force was decided.

    The list row names only the first menu it was built from, so "is it still the list" is
    asked of time instead: a Change or a rebuild decides a new menu after the list, and the
    old list is then for dishes nobody is cooking.
    """
    # the plates in force on each day, not menus with the weekend's exact dates: a Sunday
    # rebuilt on the Saturday, or resized by hand, is a change the list has to follow
    owners = _list_owners(joint)
    rows = db.all(
        "SELECT person_id, decided_at FROM v_menu_days_current WHERE day BETWEEN ? AND ?",
        (isoday(start), isoday(end)),
    )
    latest = max(
        (str(r["decided_at"]) for r in rows if owners is None or int(r["person_id"]) in owners),
        default="",
    )
    return latest > str(joint["created_at"])


def _list_owners(row: Any) -> set[int] | None:
    """Whose food a joint list is (migration 0033), or None for a list from before owners."""
    try:
        raw = row["owner_ids"]
    except IndexError, KeyError:
        return None
    if not raw:
        return None
    try:
        return {int(x) for x in json.loads(str(raw))}
    except ValueError, TypeError:
        return None


def weekend_list(db: Db, saturday: dt.date, person_id: int | None = None) -> Any:
    """The weekend's joint list as it stands, or None: the newest one, and for a person only
    one with their food on it."""
    return _joint_for(db, saturday, saturday + dt.timedelta(days=1), person_id)


def segment_list_id(db: Db, menu_id: int) -> int | None:
    """The newest segment list built for this menu, whether or not it has anything to buy."""
    row = db.one(
        "SELECT id FROM shopping_lists WHERE menu_id = ? AND kind = 'segment' "
        "ORDER BY id DESC LIMIT 1",
        (menu_id,),
    )
    return None if row is None else int(row["id"])


def _person_of(db: Db, menu_id: int) -> int | None:
    row = db.one("SELECT person_id FROM menus WHERE id = ?", (menu_id,))
    return None if row is None else int(row["person_id"])


def newest_readable(db: Db, person_id: int) -> Any:
    """The newest list this person may read: their own, or a joint one with their food on it."""
    for row in db.all(
        "SELECT id, kind, person_id, menu_id, created_at, owner_ids FROM shopping_lists "
        "WHERE person_id = ? OR person_id IS NULL ORDER BY id DESC LIMIT 20",
        (person_id,),
    ):
        if readable(row, person_id):
            return row
    return None


def list_row(db: Db, list_id: int, person_id: int) -> Any:
    """A list by id, or None when it is not one this person may read."""
    row = db.one(
        "SELECT id, kind, person_id, menu_id, created_at, owner_ids FROM shopping_lists "
        "WHERE id = ?",
        (list_id,),
    )
    return row if readable(row, person_id) else None


def item_readable(db: Db, item_id: int, person_id: int) -> bool:
    """Is this item on a list this person may read and tick? The page's tick and "shelf empty"
    asked only "own list or any joint one", so her phone could tick his owner-only weekend list."""
    row = db.one(
        "SELECT sl.person_id, sl.owner_ids FROM shopping_items si "
        "JOIN shopping_lists sl ON sl.id = si.list_id WHERE si.id = ?",
        (item_id,),
    )
    return readable(row, person_id)


def replaced_by(db: Db, row: Any, person_id: int) -> Any:
    """The list that has since taken this one's place for the same days, or None.

    A message's link names the list it was sent with. After a Change or a rebuild that list is
    for dishes nobody is cooking, and opening it from the old message showed it as if it were
    still the list. The page shows the list now in force instead, and says so.
    """
    if row is None or row["menu_id"] is None:
        return None
    span = _menu_span(db, int(row["menu_id"]))
    if span is None:
        return None
    start = span[0]
    if str(row["kind"]) == "joint_weekend":
        sat, sun = weekend_of_span(start)
        now = _joint_for(db, sat, sun, person_id)
        return now if now is not None and int(now["id"]) > int(row["id"]) else None
    # a segment list: the newest one for whatever menu now feeds its first day
    here = db.one(
        "SELECT menu_id FROM v_menu_days_current WHERE person_id = ? AND day = ?",
        (person_id, isoday(start)),
    )
    candidates = {int(row["menu_id"])} | ({int(here["menu_id"])} if here is not None else set())
    marks = ",".join("?" for _ in candidates)
    now = db.one(
        "SELECT id, kind, person_id, menu_id, created_at, owner_ids FROM shopping_lists "
        f"WHERE kind = 'segment' AND person_id = ? AND menu_id IN ({marks}) "
        "ORDER BY id DESC LIMIT 1",
        (person_id, *sorted(candidates)),
    )
    return now if now is not None and int(now["id"]) > int(row["id"]) else None


def list_owners(row: Any) -> set[int] | None:
    """Whose food a joint list is, or None when the list is from before owners were kept."""
    return _list_owners(row) if row is not None and row["person_id"] is None else None


def readable(row: Any, person_id: int) -> bool:
    """May this person read and tick this list? Their own, or a joint one they are an owner of -
    a weekend list built from his menu alone was served to her as "Weekend, both of you"."""
    if row is None:
        return False
    if row["person_id"] is not None:
        return int(row["person_id"]) == person_id
    owners = _list_owners(row)
    return owners is None or person_id in owners


def other_lists(db: Db, person_id: int, today: dt.date, shown_id: int) -> list[dict[str, Any]]:
    """The other lists still worth opening beside the one on screen. Never builds one.

    On a Saturday two are live: the weekend's, which is on screen, and Monday's, which went out
    at 18:00 with its own link. Each points at the other, so neither is a dead end.
    """
    out: list[dict[str, Any]] = []
    seen = {shown_id}
    # a range list already buys for every weekday from today: the day lists under it are not
    # other lists ("Also: the list for Thu 8 Oct to Thu 8 Oct", and one for a menu replaced)
    ranged = db.scalar("SELECT kind FROM shopping_lists WHERE id = ?", (shown_id,)) == "range"
    for menu in db.all(
        "SELECT id, segment_start, segment_end FROM v_menu_current WHERE person_id = ? "
        "AND segment_end > ? ORDER BY segment_start, decided_at DESC, id DESC",
        (person_id, isoday(today)),
    ):
        start, end = parse_day(str(menu["segment_start"])), parse_day(str(menu["segment_end"]))
        weekend = all(d.weekday() in WEEKEND for d in _days(start, end))
        if ranged and not weekend:
            continue
        found = (
            _joint_for(db, start, end, person_id)
            if weekend
            else db.one(
                "SELECT id, kind FROM shopping_lists WHERE menu_id = ? AND kind = 'segment' "
                "ORDER BY id DESC LIMIT 1",
                (int(menu["id"]),),
            )
        )
        if found is None or int(found["id"]) in seen:
            continue
        seen.add(int(found["id"]))
        out.append(
            {
                "id": int(found["id"]),
                "kind": str(found["kind"]),
                "segment_start": isoday(start),
                "segment_end": isoday(end),
            }
        )
    return out[:2]


def _days(start: dt.date, end: dt.date) -> list[dt.date]:
    return [start + dt.timedelta(days=i) for i in range((end - start).days + 1)]


def _joint_for(db: Db, start: dt.date, end: dt.date, person_id: int | None = None) -> Any:
    """The joint list already built for the weekend these dates reach, if there is one.

    Both people's accepted weekend menus have private rows. Match a joint list by date and
    confirm the person has food on it; its `menu_id` may point to either person's menu. Prefer
    the newest applicable list so existing ticks remain visible.
    """
    sat, sun = weekend_of_span(start)
    # the newest joint list whose menu sits in that weekend - a list built after a Sunday-only
    # rebuild names that menu - and, for a person, one whose food is theirs
    for row in db.all(
        "SELECT sl.id, sl.kind, sl.person_id, sl.menu_id, sl.created_at, sl.owner_ids "
        "FROM shopping_lists sl JOIN menus m ON m.id = sl.menu_id "
        "WHERE sl.kind = 'joint_weekend' AND m.segment_start >= ? AND m.segment_end <= ? "
        "ORDER BY sl.id DESC",
        (isoday(sat), isoday(sun)),
    ):
        if person_id is None or readable(row, person_id):
            return row
        return None  # the weekend's newest list is not theirs: an older one is not either
    return None


#: the last day a range list runs to: Friday. The weekend cooked together has its own joint list.
RANGE_LAST_WEEKDAY = 4


def cooks_daily(db: Db, person_id: int, day: dt.date) -> bool:
    """Whether every weekday cook of theirs is a day of its own (`/set cook-days daily`): a menu,
    and so a list, per day - which is why one list has to run over several."""
    from .. import targets as targets_mod  # noqa: PLC0415 (targets imports the food layer)

    profile = targets_mod.profile_on(db, person_id, day)
    if profile is None:
        return False
    try:
        groups = json.loads(str(profile["segment_days"]))
    except ValueError:
        return False
    return bool(groups) and all(isinstance(g, list) and len(g) == 1 for g in groups)


def range_days(db: Db, person_id: int, today: dt.date) -> list[dt.date]:
    """Return menu days from today through Friday for a daily cook.

    A Sunday with a completed week of choices covers the following Monday to Friday."""
    from . import menu as menu_mod  # noqa: PLC0415 (menu imports this module)
    from . import weekplan  # noqa: PLC0415

    if weekplan.of(db, person_id).shops:
        return [
            d
            for d in shop_days(db, person_id, today)
            if menu_mod.accepted_menu_id(db, person_id, d) is not None
        ]
    if today.weekday() > RANGE_LAST_WEEKDAY:
        if today.weekday() != WEEKEND[1]:
            return []
        from . import choices  # noqa: PLC0415 (choices imports menu, which imports this module)

        week = choices.settled_week(db, person_id, today)
        return [d for d in week if menu_mod.accepted_menu_id(db, person_id, d) is not None]
    days = [today + dt.timedelta(days=i) for i in range(RANGE_LAST_WEEKDAY - today.weekday() + 1)]
    return [d for d in days if menu_mod.accepted_menu_id(db, person_id, d) is not None]


def shop_days(db: Db, person_id: int, today: dt.date) -> list[dt.date]:
    """For someone whose week is bought in shops of their own (food/weekplan.py `shops`): the
    days the shop being bought for now covers, from today. Sunday is the first shop's (Monday to
    Wednesday); from the evening its list goes out (`topup_list_day`), the next shop's; Saturday
    is the weekend's own list. [] when there is none."""
    from . import packs, weekplan  # noqa: PLC0415 (packs imports this module)

    plan = weekplan.of(db, person_id)
    if not plan.shops or today.weekday() == WEEKEND[0]:
        return []
    if today.weekday() == WEEKEND[1]:
        monday = today + dt.timedelta(days=1)
        return packs.window_of(db, person_id, monday)
    window = packs.window_of(db, person_id, today)
    later = window[-1] + dt.timedelta(days=1)
    if (
        plan.topup_list_day is not None
        and today.weekday() >= plan.topup_list_day
        and later.weekday() <= RANGE_LAST_WEEKDAY
    ):
        return packs.window_of(db, person_id, later)
    return [d for d in window if d >= today]


def range_plates(db: Db, person_id: int, days: Sequence[dt.date]) -> dict[int, set[str]]:
    """The menu in force on each day -> the days it feeds: a skip, a swap or a rebuild makes a
    menu of its own for its day, and only the newest decides."""
    from . import menu as menu_mod  # noqa: PLC0415 (menu imports this module)

    out: dict[int, set[str]] = {}
    for d in days:
        winner = menu_mod.accepted_menu_id(db, person_id, d)
        if winner is not None:
            out.setdefault(int(winner), set()).add(isoday(d))
    return out


def range_basis(db: Db, person_id: int, days: Sequence[dt.date]) -> str:
    """What a range list was built from - each day's meals, dishes and sizes - so the list can
    tell when it no longer says what the days eat."""
    parts: list[tuple[str, str, int, float]] = []
    for menu_id, on in sorted(range_plates(db, person_id, days).items()):
        for r in db.all(
            "SELECT md.day, ms.slot, ms.recipe_id, ms.portion_multiplier FROM menu_days md "
            "JOIN meal_slots ms ON ms.menu_day_id = md.id WHERE md.menu_id = ?",
            (menu_id,),
        ):
            if str(r["day"]) in on:
                parts.append(
                    (
                        str(r["day"]),
                        str(r["slot"]),
                        int(r["recipe_id"]),
                        round(float(r["portion_multiplier"]), 3),
                    )
                )
    return json.dumps(sorted(parts))


def range_list(
    db: Db,
    person_id: int,
    today: dt.date,
    *,
    retailers: Sequence[str] = (),
    clock: Clock,
    aisles: Sequence[str] = DEFAULT_AISLES,
    days: Sequence[dt.date] | None = None,
) -> Any:
    """One list for the days from today to Friday, over the plates in force on each - built
    again whenever they change (a meal skipped, a dish swapped, a day rebuilt), with what was
    ticked carried across by food: a tick is a fact about the trolley. None when no day ahead
    has a menu. Pack credit is limited to earlier days of this shop window; the range writes no
    leftovers because each day's own list has already written its own."""
    from . import menu as menu_mod  # noqa: PLC0415 (menu imports this module)

    # `days`: a build's own days, named by the caller (the week's first shop, from the day the
    # build starts - never days already bought for)
    days = (
        [d for d in days if menu_mod.accepted_menu_id(db, person_id, d) is not None]
        if days is not None
        else range_days(db, person_id, today)
    )
    if not days:
        return None
    start, end = isoday(days[0]), isoday(days[-1])
    basis = range_basis(db, person_id, days)
    latest = db.one(
        "SELECT * FROM shopping_lists WHERE person_id = ? AND kind = 'range' AND to_day = ? "
        "ORDER BY id DESC LIMIT 1",
        (person_id, end),
    )
    if latest is not None and latest["from_day"] == start and latest["basis"] == basis:
        return latest
    on_days = range_plates(db, person_id, days)
    needs = _needs(
        db,
        sorted(on_days),
        weekend_only=False,
        pantry=_pantry_for(db, person_id),
        only_days=on_days,
    )
    from .shopping import _credits_for  # noqa: PLC0415 (shopping re-exports this module)

    credit = _credits_for(db, person_id, days[0], retailers)
    items = _items(needs, credit, aisles, _shop_for(db, person_id, needs, retailers, credit))
    first = menu_id_on(db, person_id, days[0])
    written = _write(
        db,
        items,
        kind="range",
        menu_id=first,
        person_id=person_id,
        leftover_person_ids=(),
        clock=clock,
        from_day=start,
        to_day=end,
        basis=basis,
    )
    _carry_ticks(db, latest, written, person_id, clock)
    return db.one(
        "SELECT * FROM shopping_lists WHERE person_id = ? AND kind = 'range' "
        "ORDER BY id DESC LIMIT 1",
        (person_id,),
    )


def menu_id_on(db: Db, person_id: int, day: dt.date) -> int | None:
    from . import menu as menu_mod  # noqa: PLC0415 (menu imports this module)

    return menu_mod.accepted_menu_id(db, person_id, day)


def list_to_show(
    db: Db,
    person_id: int,
    today: dt.date,
    *,
    retailers: Sequence[str] = (),
    clock: Clock,
) -> Any:
    """The list for the cook somebody is about to shop for, built now if it does not exist.

    `ORDER BY id DESC LIMIT 1` - the newest list of any kind - is Friday's joint WEEKEND list on
    a Sunday, so somebody in a shop with Monday to Friday ahead got the list for two days that
    list while she shopped for Monday. One function now answers for both.

    The next cook segment wins, then the one running now, then whatever was last sent.
    `build_once` means asking early does not cost a second list when 18:00 comes round.

    Resolution goes through `v_menu_current` (the same per-day accepted-menu view `accepted_
    menu_id` reads) rather than the person's structural `segment_days` shape: a `/cook replace`
    can accept a window - Tue-Wed inside a Mon-Wed cadence, Wed-Fri inside a Mon-Fri one - that
    starts on no day the profile would ever call a segment boundary, and asking "what starts
    tomorrow" of the PROFILE rather than of what was actually accepted is why that replacement
    menu's list never arrived.
    """
    from . import menu as menu_mod  # noqa: PLC0415 (menu imports this module)

    def _resolve(menu_id: int) -> Any:
        span = _menu_span(db, menu_id)
        if span is None:
            return None
        start, end = span
        if all(d.weekday() in WEEKEND for d in menu_mod.Segment(start, end).days):
            # weekend segments are cooked together: if the two of them were already
            # sent one shared list, that is the list to show - not a private one built fresh
            # from this person's own half of the same weekend. Only a list with their food on
            # it, though: one built from his menu alone is not hers
            sat, sun = weekend_of_span(start)
            joint = _joint_for(db, start, end, person_id)
            if joint is not None and not _joint_stale(db, joint, sat, sun):
                return joint
            if joint is not None or _is_shared(db, menu_id):
                listed = _list_owners(joint) if joint is not None else None
                current = [
                    mid
                    for mid in weekend_menu_ids(db, menu_id)
                    if listed is None or _person_of(db, mid) in listed
                ]
                joint_once(db, current or [menu_id], retailers=retailers, clock=clock, today=today)
                return _joint_for(db, start, end, person_id)
        if not for_menu(db, menu_id):
            build_once(
                db,
                menu_id,
                retailers=retailers,
                clock=clock,
            )
        return db.one(
            "SELECT id, kind, person_id, menu_id, created_at FROM shopping_lists "
            "WHERE menu_id = ? AND kind = 'segment' ORDER BY id DESC LIMIT 1",
            (menu_id,),
        )

    # a daily cook, on a weekday: today to Friday, one list over every day's own menu (the owner, 7
    # Oct
    # 2026: "it shows only shopping for Thursday whereas today is Wednesday and it should then
    # show the shopping list from Wednesday until Friday")
    # ...and on the Sunday before a week of choices set ahead, that week's Monday to Friday -
    # for any way of cooking: a week chosen on Sunday is bought on Sunday, its batches included
    from . import choices as choices_mod  # noqa: PLC0415 (choices imports menu, which imports us)

    week_chosen = choices_mod.settled_on(db, person_id, today) or bool(
        choices_mod.settled_week(db, person_id, today)
    )
    if today.weekday() != WEEKEND[0] and (cooks_daily(db, person_id, today) or week_chosen):
        ranged = range_list(db, person_id, today, retailers=retailers, clock=clock)
        if ranged is not None:
            return ranged

    # 0. the Saturday of a weekend being cooked: the weekend's own list. Until Sunday the thing
    # still to shop for is tomorrow's food; Monday's list goes out at 18:00 with a link to itself
    # (`render.shopping_link(list_id=...)`), and each list on the page links to the other.
    if today.weekday() == WEEKEND[0]:  # Saturday
        weekend = db.one(
            "SELECT id, segment_start, segment_end FROM v_menu_current WHERE person_id = ? "
            "AND segment_start <= ? AND segment_end > ? ORDER BY decided_at DESC, id DESC LIMIT 1",
            (person_id, isoday(today), isoday(today)),
        )
        if weekend is not None and all(
            d.weekday() in WEEKEND
            for d in menu_mod.Segment(
                parse_day(str(weekend["segment_start"])), parse_day(str(weekend["segment_end"]))
            ).days
        ):
            row = _resolve(int(weekend["id"]))
            if row is not None:
                return row

    # 1. the next cook still to come - "the list arrives before you shop for it"
    upcoming = db.one(
        "SELECT id FROM v_menu_current WHERE person_id = ? AND segment_start > ? "
        "ORDER BY segment_start ASC, decided_at DESC, id DESC LIMIT 1",
        (person_id, isoday(today)),
    )
    if upcoming is not None:
        row = _resolve(int(upcoming["id"]))
        if row is not None:
            return row

    # 2. the one running now - but only while it still has a day left beyond today. Once its
    # last cook day IS today there is nothing further to buy for it, and showing it as "the
    # list" hides the real answer: nothing has been accepted for what comes next.
    running = db.one(
        "SELECT id FROM v_menu_current WHERE person_id = ? AND segment_start <= ? "
        "AND segment_end > ? ORDER BY decided_at DESC, id DESC LIMIT 1",
        (person_id, isoday(today), isoday(today)),
    )
    if running is not None:
        row = _resolve(int(running["id"]))
        if row is not None:
            return row

    # 3. whatever was last sent - unless its own segment has already run its course, in which
    # case that is a stale answer to today's question, not today's list.
    fallback = newest_readable(db, person_id)
    if fallback is None:
        return None
    span = _menu_span(db, fallback["menu_id"])
    if span is not None and span[1] <= today:
        return None
    return fallback


def current_leftovers(
    db: Db,
    person_id: int,
    today: dt.date | None = None,
    *,
    exclude_menus: Collection[int] = (),
    only_stated: bool = False,
) -> dict[str, float]:
    """Latest leftovers row per ingredient code for the person, grams > 0 and recent only.

    A `shop_misses` row for the code, seen no earlier than the leftover row itself, means the
    person stood at the shelf and said it was not there: whatever surplus that build only ever
    PLANNED to have spare never existed, and crediting it anyway is why potatoes nobody bought
    came back as "already at home" the following week. `shop_
    misses` is about the shop, not the person (reset.py), so this reads it unscoped, same as
    `shop_refresh` does.

    A credit written by a list for a segment that has not started yet is not in the fridge: that
    list has not been shopped. On 26 Sep the weekend's list was built after Monday's had been
    composed that afternoon, and counted Monday's planned eggs, bread and carrots as already at
    home. With `today`, such a row is passed over and the one before it stands.

    `only_stated` ignores surplus rows written by a shopping list. Shopping calculations use it
    for stock the person reported; planned packs are calculated within their shop window.
    """
    keep, kept = standing("m")
    misses = {
        str(r["ingredient_code"]): str(r["latest"])
        for r in db.all(
            # `kind = 'unstocked'` only: a shelf that was empty cancels the credit, because the
            # food was not there to buy. A session that ran out of turns before it looked says
            # nothing about the shelf, and cancelling a real fridge credit on it buys a pack
            # that is already at home (schema 24 draws exactly this line; `_missed` honours it).
            # And only a PERSON at the shelf: the reader's own "not sold here" is a judgement
            # about a web page, and it cancelled a 380 g carton of egg whites at home and would
            # have cancelled the whey tub. Not one taken back on the page: that
            # food was there after all, and its credit stands
            "SELECT m.ingredient_code, MAX(m.seen_at) AS latest FROM shop_misses m "
            f"WHERE m.kind = 'unstocked' AND m.reason LIKE ? AND {keep} "
            "GROUP BY m.ingredient_code",
            (PERSON_SAID_LIKE, *kept),
        )
    }
    latest: dict[str, float] = {}
    for r in db.all(
        "SELECT i.code, i.aisle, l.grams, l.created_at, l.from_menu_id, m.segment_start "
        "FROM leftovers l "
        "JOIN ingredients i ON i.id = l.ingredient_id LEFT JOIN menus m ON m.id = l.from_menu_id "
        "WHERE l.person_id = ? ORDER BY l.id",
        (person_id,),
    ):
        code = str(r["code"])
        created_at = str(r["created_at"])
        if r["from_menu_id"] is not None and int(r["from_menu_id"]) in exclude_menus:
            continue  # the list being rebuilt planned this; it is not food from before it
        if only_stated and r["from_menu_id"] is not None:
            continue
        if (
            today is not None
            and r["segment_start"] is not None
            and str(r["segment_start"]) > isoday(today)
        ):
            continue  # planned by a list nobody has shopped yet: the row before stands
        if today is not None:
            # per aisle, not a flat ten days: a kilo of rice is still a kilo of rice in March
            cutoff = today - dt.timedelta(days=keeps_days(str(r["aisle"])))
            if created_at[:10] < cutoff.isoformat():
                latest.pop(code, None)  # too old to still be food
                continue
        if misses.get(code, "") >= created_at:
            latest.pop(code, None)  # said empty since (or the moment) this credit was made
            continue
        latest[code] = float(r["grams"])
    return {code: g for code, g in latest.items() if g > 0}


def leftover_lines(db: Db, person_id: int, today: dt.date) -> list[dict[str, Any]]:
    """`current_leftovers`, by name: the one name-lookup a chat tool or a page needs, written
    once so "I have chicken and rice" and the page's own reading of the fridge cannot drift
    apart."""
    grams = current_leftovers(db, person_id, today)
    if not grams:
        return []
    marks = ",".join("?" for _ in grams)
    names = {
        str(r["code"]): str(r["name"])
        for r in db.all(f"SELECT code, name FROM ingredients WHERE code IN ({marks})", tuple(grams))
    }
    return [{"name": names.get(code, code), "grams": g} for code, g in grams.items()]


def ticked_uncooked(db: Db, person_id: int, list_row: Any) -> list[dict[str, Any]]:
    """Items ticked on `list_row` (a `shopping_lists` row) - bought, but nothing has read a
    day's menu as cooked from it yet, so it is still sitting in the kitchen rather than in a
    `leftovers` credit."""
    if list_row is None:
        return []
    ticked_ids = ticks_for(db, int(list_row["id"]), person_id, joint=list_row["person_id"] is None)
    if not ticked_ids:
        return []
    marks = ",".join("?" for _ in ticked_ids)
    return [
        {"name": str(r["name"]), "grams": float(r["pack_g"] or 0.0) * int(r["packs"])}
        for r in db.all(
            "SELECT i.name, si.packs, si.pack_g FROM shopping_items si "
            f"JOIN ingredients i ON i.id = si.ingredient_id WHERE si.id IN ({marks})",
            tuple(ticked_ids),
        )
    ]


def weekend_days(segment_start: dt.date, segment_end: dt.date) -> list[dt.date]:
    return [
        segment_start + dt.timedelta(days=i)
        for i in range((segment_end - segment_start).days + 1)
        if (segment_start + dt.timedelta(days=i)).weekday() in WEEKEND
    ]


#: joins more than one dish naming a shared-ingredient line. Never a comma - a dish's own name
#: is full of them ("Chicken, chickpea and feta salad"), so a reader cannot tell a dish boundary
#: from a dish's own punctuation. Shared with `telegram.menuflow`'s own per-dish attribution.
DISH_SEP = " / "
