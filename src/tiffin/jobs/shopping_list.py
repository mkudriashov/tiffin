"""shopping_list (each, segment start - 1 day, 18:00) and shopping_joint (Friday 18:00).

Both aggregate the menu's grams into whole packs, credit last segment's leftovers, group by
aisle and send the list. Silence never blocks a list: a lapsed menu has already been repeated,
so there is always something to shop for.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from typing import Any

from .. import targets as targets_mod
from ..clock import isoday, parse_day, week_start
from ..food import menu as menu_mod
from ..food import packs, shopping, weekplan
from ..scheduler import Job, JobContext, fixed
from ..telegram import batchflow
from ..telegram import render as r
from ..telegram.api import inline_keyboard
from . import shop_refresh

FRIDAY = frozenset({4})
#: same cap `/shopping` uses (`menuflow.TICK_BUTTONS`): Telegram's own keyboard limit, not a
#: house choice, so a joint list with more items than this still ticks the rest via /shopping.
TICK_BUTTONS = 12

LEAD_DAYS = 2

MAX_LINES = 10
#: pack lines a list message carries: the main proteins first, inside the ten-line rule
PACK_LINES = 3


def _capped(lines: list[str]) -> tuple[str, int]:
    """Ten lines, with the tenth saying what is missing rather than being a cliff.

    A shopping list read in a supermarket that stops mid-aisle is worse than a short one: the
    last aisles simply never reach the shop and nothing says they were cut. The scheduled list
    is an unprompted message, so the ten-line rule stands; /shopping shows the whole thing.
    """
    if len(lines) <= MAX_LINES:
        return "\n".join(lines), 0
    held = len(lines) - (MAX_LINES - 1)
    kept = [*lines[: MAX_LINES - 1], f"{held} more lines: send /shopping"]
    return "\n".join(kept), held


def _fill_gaps(ctx: JobContext, menu_ids: list[int]) -> None:
    """Read the shop for this menu's own foods that still have no product, then carry on.

    The weekly refresh reads twelve foods a Sunday, menu-first, and that is the right cadence
    for keeping prices fresh. It is not fast enough for a menu that changed: a `/cook replace`
    on Thursday can introduce a food the queue has never reached, and the list going out
    tomorrow is the last moment anybody can turn "Beef mince 5% 1 x 500 g" into the thing on the
    shelf. So the list closes its own gaps first, for the handful of foods on ITS menu.

    Never fatal, never even degrading: `fill_for_menus` writes its own run_ledger row and
    returns a line. A list with catalogue packs and no prices is the behaviour that shipped and
    is still a usable list; a list that never arrived is not.
    """
    note = shop_refresh.fill_for_menus(
        ctx.app, ctx.db, ctx.outbox, clock=ctx.clock, menu_ids=menu_ids
    )
    if note:
        ctx.handle.note(f"shop fill: {note}")


def _refusal_line(ctx: JobContext, person: Any, menu_id: int) -> str:
    """Explain when a list still includes a food the person has since refused."""
    from ..food import catalogue  # noqa: PLC0415 (food imports the db layer)

    names = r.few(catalogue.refused_names_on(ctx.db, person.id, [menu_id]))
    if not names:
        return ""
    return r.esc(
        f"This list still buys {names}, which you now avoid. /swap takes it off the menu now."
    )


def _covered_line(ctx: JobContext, person: Any, first: dt.date, items: list[Any]) -> str:
    """For a list a pack opened on an earlier day already covers part of: one line saying so,
    2026: "primarily through her clicking")."""
    got = packs.covered(
        ctx.db,
        person.id,
        first,
        {i.code: float(i.grams_total) for i in items},
        retailers=shop_refresh.retailer_order(ctx.app.config),
    )
    if not got:
        return ""
    names = r.few([i.name.split(",")[0] for i in items if i.code in got])
    return r.esc(
        f"Probably at home already, from an earlier pack: {names}. Tap I have it on the list."
    )


def _send(
    ctx: JobContext,
    title: str,
    items: list[shopping.ShoppingItem],
    kind: str,
    due_key: str,
    *,
    footer: str | None = None,
    warn_line: str = "",
    list_id: int | None = None,
    pack_lines: Sequence[str] = (),
) -> None:
    # what the list counts on being at home already: said, never dropped. A list
    # with nothing to buy still goes out when it counts on food at home - silence there read as
    # "no list", and the eggs it was counting on were never checked
    home = shopping.at_home(ctx.db, list_id) if list_id is not None else []
    home_line = shopping.at_home_line(home)
    if not items and not home:
        ctx.handle.skip("nothing to buy")
        return
    total = shopping.total_line(items)
    person = ctx.person or ctx.app.registry(ctx.db).admin()
    link = (
        r.shopping_link(ctx.app.config, label="The list on the page", list_id=list_id)
        if person.tailscale_login
        else ""
    )
    if link and "href" in link:
        # the list is ticked in the shop on the page, aisle by aisle; the message says it is
        size = (
            r.plural(len(items), "item") + (f", {total.lower()}" if total else "")
            if items
            else "nothing to buy"
        )
        lines = [
            f"<b>{title}</b> · {r.esc(size)}",
            *([warn_line] if warn_line else []),
            *([r.esc(home_line)] if home_line else []),
            *[r.esc(p) for p in pack_lines],
            f"{link}, ticked off as you go." if items else f"{link}.",
        ]
    else:
        # no page for this person: the list itself is the only way to see it
        lines = [
            f"<b>{title}</b>",
            *([warn_line] if warn_line else []),
            *[r.esc(line) for line in shopping.render_lines(items)],
            *([r.esc(total)] if total else []),
            *([] if items else ["Nothing to buy."]),
            *([r.esc(home_line)] if home_line else []),
            *[r.esc(p) for p in pack_lines],
            *([footer] if footer else []),
        ]
    html, held = _capped(lines)
    result = ctx.outbox.send(person, kind, due_key, html)
    note = f"{len(items)} items: {result.status}"
    ctx.handle.note(note + (f"; {held} lines held back" if held else ""))


def run_shops(ctx: JobContext, person: Any, plan: weekplan.WeekPlan) -> None:
    """For someone whose week is bought in shops of their own (food/weekplan.py): the first
    shop's list went out with the week's menu, on the day it was planned; this sends the top-up
    and nothing on any other day - a list a day for food bought on Sunday would buy it twice."""
    if plan.topup_list_day is None or ctx.day.weekday() != plan.topup_list_day:
        ctx.handle.skip("bought in their own shops; no list due today")
        return
    days = shopping.shop_days(ctx.db, person.id, ctx.day)
    if not days or days[0] <= ctx.day:
        ctx.handle.skip("no later shop this week")
        return
    if all(targets_mod.pause_active(ctx.db, person.id, day) for day in days):
        ctx.handle.skip("paused for the whole shop")
        return
    retailers = shop_refresh.retailer_order(ctx.app.config)
    made = shopping.range_list(ctx.db, person.id, ctx.day, retailers=retailers, clock=ctx.clock)
    if made is None:
        ctx.handle.skip("no menu for that shop's days")
        ctx.outbox.send(
            person,
            "shopping_list_failed",
            isoday(days[0]),
            f"No menu for {days[0]:%a %d %b} yet, so there is no top-up list. /cook builds one.",
        )
        return
    items = shopping.read_back(ctx.db, int(made["id"]))
    _send(
        ctx,
        f"Top-up shop for {packs.days_words(days)}",
        items,
        "shopping_list",
        isoday(days[0]),
        list_id=int(made["id"]),
        pack_lines=packs.lines(ctx.db, person.id, days[0], retailers=retailers, limit=PACK_LINES),
    )


def run(ctx: JobContext) -> None:
    person = ctx.person
    if person is None or ctx.profile is None:
        ctx.handle.skip("no profile")
        return
    plan = weekplan.of(ctx.db, person.id)
    if plan.shops:
        run_shops(ctx, person, plan)
        return
    weeks = menu_mod.weeks_of(ctx.db, ctx.person_id)
    segment = menu_mod.next_segment(ctx.profile, ctx.day, days_before=LEAD_DAYS, weeks=weeks)
    if segment is None:
        segment = menu_mod.next_segment(ctx.profile, ctx.day, days_before=1, weeks=weeks)
    if segment is None:
        ctx.handle.skip("no cook segment starting soon")
        return
    if all(targets_mod.pause_active(ctx.db, person.id, day) for day in segment.days):
        # the scheduler's own pause guard only ever checks TODAY (the day the job fires, which
        # is the segment's start minus one) - a pause that begins on the segment's first day,
        # like a Monday-to-Wednesday trip announced the Sunday before, starts a day too late to
        # be caught there. Food nobody will eat for a whole segment gets no list at all.
        ctx.handle.skip("paused for the whole segment")
        return
    if all(day.weekday() in shopping.WEEKEND for day in segment.days):
        # Both jobs run at Friday 18:00. Giving a Monday-to-Friday cadence its own weekend
        # segment (so the weekend had a menu at all) made this one fire for Saturday-Sunday
        # as well as shopping_joint, which already covers exactly those days for both people:
        # two lists a minute apart, double the packs, and the second one crediting leftovers
        # the first had just invented. The weekend belongs to the joint list.
        ctx.handle.skip("the weekend goes out as one joint list")
        return
    from ..food import choices  # noqa: PLC0415 (food imports the db layer)

    if all(choices.settled_on(ctx.db, person.id, day) for day in segment.days):
        # a week of choices is bought for in one go, on the Sunday its picks are set (the owner, 12
        # Oct
        # 2026: "so then she can buy things Sunday for the week"): its list went out then, and a
        # list a day for the same food would buy it twice
        ctx.handle.skip("bought with the week's choices")
        return
    menu_id = menu_mod.segment_menu_id(ctx.db, person.id, segment.start)
    if menu_id is None:
        # Degrade to manual, never to silence: a plain skip here left the list
        # simply not arriving, indistinguishable from "nothing due yet" - she found out only
        # in the shop (daily_messages/03, list_vs_menu/10). Say so, and what fixes it.
        ctx.handle.skip("no menu for that segment")
        ctx.outbox.send(
            person,
            "shopping_list_failed",
            isoday(segment.start),
            f"No menu for {segment.start:%a %d %b} yet. /cook builds one.",
        )
        return
    made = ctx.db.one("SELECT segment_start, segment_end FROM menus WHERE id = ?", (menu_id,))
    assert made is not None  # segment_menu_id read it
    first, last = parse_day(str(made["segment_start"])), parse_day(str(made["segment_end"]))
    if first < segment.start and shopping.segment_list_id(ctx.db, menu_id) is not None:
        # a week built in one go for someone who cooks each day (menu_propose.week_run) is bought
        # in one go, on the list for its first day: a list for each later day would buy it twice
        ctx.handle.skip(f"bought with the list for {first:%a %d %b}")
        return
    _fill_gaps(ctx, [menu_id])
    # `/shopping` can build this list before 18:00; reuse it when the scheduled job runs.
    items = shopping.build_once(
        ctx.db,
        menu_id,
        retailers=shop_refresh.retailer_order(ctx.app.config),
    )
    built = shopping.segment_list_id(ctx.db, menu_id)
    # the list says what to buy; this says what it turns into, which is the part that makes
    # somebody put the hour aside rather than shop and then wonder what for - with config,
    # this is a real tap into the Batch tab, not a slash command in a scrolled-away message
    cook = batchflow.summary(ctx.db, person.id, ctx.day, ctx.app.config)
    _send(
        ctx,
        f"Shopping for {segment.start:%a %d %b}"
        + (f" to {last:%a %d %b}" if last > segment.end else ""),
        items,
        "shopping_list",
        isoday(segment.start),
        footer=cook,
        warn_line=_covered_line(ctx, person, segment.start, items)
        or _refusal_line(ctx, person, menu_id),
        list_id=built,
        # each pack and the days that cook from it
        pack_lines=packs.lines(
            ctx.db,
            person.id,
            segment.start,
            retailers=shop_refresh.retailer_order(ctx.app.config),
            limit=PACK_LINES,
        ),
    )


def run_joint(ctx: JobContext) -> None:
    """Saturday and Sunday are cooked together: one list, both menus, sent to both."""
    saturday = week_start(ctx.day) + dt.timedelta(days=5)
    # only the people whose food is actually on the list. It went to every paired chat id, so
    # her first substantive message from Tiffin could be a weekend shopping list for food
    # she never chose, from a menu she cannot see, before she had told the system anything.
    # The joint scope also resolves person=None, so the scheduler's pause guard never applies:
    # a paused person got it too.
    covered: list[tuple[Any, int]] = []
    for p in ctx.app.registry(ctx.db).active():
        if p.chat_id is None or targets_mod.pause_active(ctx.db, p.id, ctx.day):
            continue
        mid = menu_mod.segment_menu_id(ctx.db, p.id, saturday)
        if mid is not None:
            covered.append((p, mid))
    persons = [p for p, _ in covered]
    menu_ids = [mid for _, mid in covered]
    if not menu_ids:
        ctx.handle.skip("no weekend menus")
        return
    _fill_gaps(ctx, menu_ids)
    # once: the page may already have built this weekend's list for whoever opened it first.
    # Each owner's OWN stock is summed for the need and paid back to whoever it came from
    # `joint_once` reads that credit, so a rebuild can
    # leave out what the weekend's own earlier list only planned to have spare
    items = shopping.joint_once(
        ctx.db,
        menu_ids,
        retailers=shop_refresh.retailer_order(ctx.app.config),
        clock=ctx.clock,
        today=ctx.day,
    )
    row = shopping.weekend_list(ctx.db, saturday)
    home = shopping.at_home(ctx.db, int(row["id"])) if row is not None else []
    home_line = shopping.at_home_line(home)
    if not items and not home:
        ctx.handle.skip("nothing to buy for the weekend")
        return
    list_id = int(row["id"]) if row is not None else items[0].list_id
    total = shopping.total_line(items)
    # whose weekend a shared-list food actually feeds - a food only in HIS dish is not "shared"
    # just because it sits on their one basket (two_people/07): a copy is rendered per reader so
    # only the one who does not cook it gets told whose it is. A shared dish (test_01's tray)
    # gets no attribution on either copy, so two people eating the same thing still get the
    # identical text they always have. Read off the plates in force on each weekend day.
    owners_by_code = shopping.joint_dish_owners(ctx.db, row) if row is not None else {}
    # "cooked together" only when it is: a list with one person's food on it is theirs alone
    title = "Weekend shopping (cooked together)" if len(persons) > 1 else "Weekend shopping"
    # every item ticks from the message it arrives in, not just from /shopping (two_people/12):
    # the trolley is standing at the shelf reading THIS message, not composing a slash command.
    rows = [
        [(str(item.name)[:20], f"k:tick:{item.item_id}")]
        for item in items
        if item.item_id is not None
    ]
    keyboard = inline_keyboard(rows[:TICK_BUTTONS]) if rows else None
    sent = 0
    for p in persons:
        link = (
            r.shopping_link(ctx.app.config, label="The list on the page", list_id=list_id)
            if p.tailscale_login
            else ""
        )
        if link and "href" in link:
            # tiffin just provides a link") - the buttons are for whoever has no page
            size = (
                r.plural(len(items), "item") + (f", {total.lower()}" if total else "")
                if items
                else "nothing to buy"
            )
            lines = [
                f"<b>{title}</b> · {r.esc(size)}",
                *([r.esc(home_line)] if home_line else []),
                *[r.esc(x) for x in packs.lines(ctx.db, p.id, saturday, limit=PACK_LINES)],
                f"{link}, ticked off as you go." if items else f"{link}.",
            ]
            reader_keyboard = None
        else:
            lines = [
                f"<b>{title}</b>",
                *[
                    r.esc(line)
                    for line in shopping.render_lines(
                        items, attribution=shopping.attribution_for(owners_by_code, p.id)
                    )
                ],
                *([r.esc(total)] if total else []),
                *([] if items else ["Nothing to buy."]),
                *([r.esc(home_line)] if home_line else []),
            ]
            reader_keyboard = keyboard
        html, _held = _capped(lines)
        result = ctx.outbox.send(
            p,
            "shopping_joint",
            isoday(saturday),
            html,
            reader_keyboard,
        )
        if result.sent:
            sent += 1
        elif result.status == "failed":
            ctx.handle.degrade(f"{p.slug}: {result.error}")
    ctx.handle.note(f"{len(items)} items to {sent}/{len(persons)}")


JOB = Job(
    name="shopping_list",
    scope="each",
    kind="message",
    times=fixed("18:00"),
    run=run,
    catch_up="12:00",
    default_answers=(("pant", "no pantry items assumed"),),
)

JOINT_JOB = Job(
    name="shopping_joint",
    scope="joint",
    kind="message",
    times=fixed("18:00"),
    run=run_joint,
    catch_up="10:00",
    days=FRIDAY,
    needs_profile=False,
)
