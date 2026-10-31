"""On Demand menu proposals."""

from __future__ import annotations

import datetime as dt
import sqlite3
from collections.abc import Callable
from typing import Any

from .. import ledger, targets
from ..clock import Clock, isoday, parse_day
from ..db import Db
from ..errors import (
    NothingToBuild,
)
from ..food import menu, weekplan
from ..ledger import AlreadyDone
from ..persons import Person
from ..scheduler import JobContext
from ..telegram import render as r
from ..telegram.api import inline_keyboard
from .menu_propose import DAYS_BEFORE, change_row, meal_buttons, message_for, today_gap, week_run
from .menu_rebuild import (
    JOB,
    _propose_for,
    note_refusal,
    note_wishes,
    refusal_said,
    repeat_less_refused,
    wishes_said,
)


def repeat_now(
    app: Any,
    db: Db,
    person: Person,
    profile: sqlite3.Row | None,
    outbox: Any,
    *,
    clock: Clock,
) -> str:
    """Repeat a previous menu on request without a model call.

    The repeated menu is current immediately. Refused foods are swapped out before
    it is sent, and a shopping list is built from the repeated dishes."""
    today = clock.today_local()
    segment = segment_on_demand(profile, today, covered=None)
    if segment is None:
        raise NothingToBuild(
            "Your cook days do not cover the next few days, so there is nothing to plan."
        )
    slot = f"repeat-{clock.utc_stamp().replace(' ', 'T').replace(':', '')}"
    with ledger.run(db, "menu_propose", slot, person.id, clock=clock) as handle:
        menu_id, cleaned = repeat_less_refused(
            db, app.config, person.id, segment, clock=clock, handle=handle
        )
        if menu_id is None or cleaned is None:
            handle.note("nothing to repeat")
            raise NothingToBuild(
                "I have no earlier menu to repeat yet. Send <code>/cook</code> and I will "
                "build one."
            )
        # the same page line a built menu carries, and only for someone the page lets in
        page_line = ""
        if person.tailscale_login:
            link = r.menu_link(app.config, isoday(segment.start), label="The whole week")
            page_line = f"{link} on the page: every recipe and the shopping list." if link else ""
        html, payload = message_for(db, menu_id, repeated=True, page_line=page_line)
        if cleaned.swapped or cleaned.stuck:
            html = f"{html}\n{r.esc(cleaned.said())}"
        rows = [change_row(menu_id), *([] if page_line else meal_buttons(db, menu_id))]
        outbox.send(
            person,
            "menu_proposal",
            f"menu:{menu_id}",
            html,
            inline_keyboard(rows) if rows else None,
            payload=payload,
        )
        handle.note(f"menu {menu_id} repeated from the last one: sent")
    return ""  # the menu itself is the answer, exactly as a built one is


def segment_from(
    profile: sqlite3.Row | None, day: dt.date, *, weeks: menu.Weeks | None = None
) -> menu.Segment | None:
    """The person's cook segment holding `day`, from `day` to its end; None when no segment of
    theirs holds it. Whether the days are still open to a new menu is `_propose_for`'s check."""
    from ..clock import week_start as monday  # noqa: PLC0415 (small helper, avoids a cycle)

    walk = weeks or menu.profile_weeks(profile)
    for seg in walk(monday(day)):
        if seg.start <= day <= seg.end:
            return menu.Segment(day, seg.end)
    return None


def segment_on_demand(
    profile: sqlite3.Row | None,
    today: dt.date,
    *,
    covered: Callable[[dt.date], bool] | None = None,
    weeks: menu.Weeks | None = None,
) -> menu.Segment | None:
    """What to plan when someone asks now rather than waiting for Saturday.

    The scheduled job only ever fires two days before a segment starts, so a person who
    onboarded mid-week, ignored the proposal, or simply wants to re-plan has no way to get a
    menu at all until the next Saturday. This answers "what do I cook tomorrow":

      - if tomorrow falls inside a cook segment, the rest of that segment from tomorrow;
      - otherwise the next segment that starts after today.

    `covered` (only ever passed for a plain, note-free ask - a replace or a named change always
    means "those days", not "wherever is free") answers whether a day already has an accepted
    menu. When it is given, a segment starting TODAY with nothing accepted anywhere in it takes
    today: today's food is already eaten" only holds once today's food has an answer at all - on
    a Thursday morning with no Thu-Fri menu, that answer defaulting to Friday left Thursday's
    lunch permanently unplanned. The same check, later in the week rather than on today, also
    lets a plain `/cook` reach a segment further out that has no menu at all rather than refuse
    on a nearer one that is already answered.

    None means their cook days genuinely do not include anything upcoming.
    """
    from ..clock import week_start as monday  # noqa: PLC0415 (small helper, avoids a cycle)

    tomorrow = today + dt.timedelta(days=1)
    walk = weeks or menu.profile_weeks(profile)
    if covered is not None:
        # the weekend is `segments_for`'s own addition, cooked jointly on its own
        # Friday-evening schedule, not a slot in this person's weekday cadence - a Monday ask
        # whose Mon-Fri is already covered must still say "already covers" rather than quietly
        # jump ahead to plan Saturday, which nobody asked about
        this_week = [
            seg
            for seg in walk(monday(today))
            if not (seg.start.weekday() == 5 and seg.end.weekday() == 6)
        ]
        for seg in this_week:
            if seg.start == today and not any(covered(d) for d in seg.days):
                return menu.Segment(today, seg.end)
        for seg in this_week:
            if seg.start > today and not any(covered(d) for d in seg.days):
                return menu.Segment(seg.start, seg.end)
    bases = (
        monday(today) - dt.timedelta(days=7),
        monday(today),
        monday(today) + dt.timedelta(days=7),
    )
    upcoming: list[menu.Segment] = []
    for base in bases:
        for seg in walk(base):
            if seg.start <= tomorrow <= seg.end:
                return menu.Segment(tomorrow, seg.end)
            if seg.start > today:
                upcoming.append(seg)
    return min(upcoming, key=lambda s: s.start) if upcoming else None


def propose_now(
    app: Any,
    db: Db,
    person: Person,
    profile: sqlite3.Row | None,
    outbox: Any,
    *,
    clock: Clock,
    replace: bool = False,
    note: str = "",
    only_codes: frozenset[str] = frozenset(),
    quiet_failure: bool = False,
    for_menu: int | None = None,
    day: dt.date | None = None,
    only_day: dt.date | None = None,
    take_today: bool = False,
) -> str:
    """Build and send a menu when a person asks for one."""
    today = clock.today_local()
    from ..food import shared  # noqa: PLC0415 (food imports the db layer)
    from . import menu_shared  # noqa: PLC0415

    def _covered(d: dt.date) -> bool:
        return menu.accepted_menu_id(db, person.id, d) is not None

    # a refusal in the note, written before anything below can turn the build away - a weekend
    # that has gone, cook days covering nothing - and said with whatever answer comes: both
    # and handed on, since a second write finds it "already" there and would say nothing of it
    refused = note_refusal(db, person, note, clock=clock) if note else None
    first = refusal_said(refused)
    kept = note_wishes(db, person, note, clock=clock) if note else []
    if kept:
        first = f"{first} {wishes_said(kept)}" if first else wishes_said(kept)
    first += " " if first else ""
    # a replace or a change-note names its own days ("Tue to Fri", "no chickpeas") - only a
    # plain, contextless ask should be free to land on a different, genuinely empty segment.
    # And a plain ask on a today with no menu at all starts today: "tomorrow, because today's
    # food is already eaten" is not true of a day that had nothing to eat (Sat 26 Sep). A replace
    # or a note is about the days a menu already covers, so it keeps its own days.
    plain = not replace and not note and not only_codes
    tapped = (
        db.one(
            "SELECT segment_start, segment_end FROM menus WHERE id = ? AND person_id = ?",
            (for_menu, person.id),
        )
        if for_menu is not None
        else None
    )
    if tapped is not None:
        segment: menu.Segment | None = menu.Segment(
            parse_day(str(tapped["segment_start"])), parse_day(str(tapped["segment_end"]))
        )
        if (
            only_day is not None
            and segment is not None
            and segment.start <= only_day <= segment.end
            and only_day.weekday() < 5
        ):
            segment = menu.Segment(only_day, only_day)
    elif day is not None:
        segment = segment_from(profile, day, weeks=menu.weeks_of(db, person.id))
        # someone whose week is planned in one build: from that day to Friday, as the schedule
        # would plan it - `cook her --day Wed` built Wednesday alone
        if weekplan.of(db, person.id).plan_day is not None:
            segment = week_run(db, person.id, segment, replace=replace)
    else:
        gap = today_gap(db, person.id, profile, clock.now_local()) if plain else None
        segment = gap or segment_on_demand(
            profile,
            today,
            covered=None if replace else _covered,
            weeks=menu.weeks_of(db, person.id),
        )
    if segment is None:
        from ..food import planning  # noqa: PLC0415 (food imports the db layer)

        ahead = targets.profile_on(db, person.id, today + dt.timedelta(days=1)) or profile
        if planning.planning_of(ahead) == "off":
            raise NothingToBuild(
                first + "You track only, so no menus are planned. How you plan, on the page's "
                "More, changes that."
            )
        raise NothingToBuild(
            first + "Your cook days do not cover the next few days, so there is nothing to plan."
        )
    # the weekend cooked together is one menu for both: a /cook replace, a note or a Change that
    # lands on it rebuilds it for both, never one person's half - but a plain
    # /cook on a Friday found the weekend "next", and rebuilt both people's planned weekend
    # before the "already covers" check could say so
    weekend_day = all(d.weekday() >= 5 for d in segment.days)
    if weekend_day and shared.active(db, shared.weekend_of(segment.start)):
        if plain and tapped is None and menu.current_menu(db, person.id, segment.start):
            raise NothingToBuild(
                f"A menu already covers {segment.start:%a %d %b}. "
                "Send <code>/cook replace</code> to build a different one."
            )
        if tapped is not None and segment.end < shared.weekend_of(today).start:
            # the shared build plans the weekend of today, whichever weekend was tapped: "Just a
            # new menu" on last weekend's message rebuilt the coming one, for both of them
            # this weekend - Sunday alone, rebuilt once Saturday began - is still to plan.
            raise NothingToBuild(
                first + "Those days are already under way, so there is nothing left to plan. "
                "Send /cook for the next ones."
            )
        return menu_shared.cook_together(
            app, db, clock=clock, note=note, person=person, refused=refused
        )
    if not replace and menu.current_menu(db, person.id, segment.start) is not None:
        raise NothingToBuild(
            first + f"A menu already covers {segment.start:%a %d %b}. "
            "Send <code>/cook replace</code> to build a different one."
        )
    slot = f"manual-{clock.utc_stamp().replace(' ', 'T').replace(':', '')}"
    with ledger.run(db, "menu_propose", slot, person.id, clock=clock) as handle:
        ctx = JobContext(
            app=app,
            db=db,
            job=JOB,
            person=person,
            profile=profile,
            day=segment.start
            - dt.timedelta(
                days=int(app.config.get("schedule.menu_propose_days_before", DAYS_BEFORE))
            ),
            due_local=clock.now_local(),
            now_local=clock.now_local(),
            slot=slot,
            handle=handle,
            outbox=outbox,
            clock=clock,
        )
        return _propose_for(
            ctx,
            segment,
            note=note,
            refused=refused,
            only_codes=only_codes,
            quiet_failure=quiet_failure,
            take_today=take_today,
        )


def rebuild_for_page(
    app: Any,
    db: Db,
    person: Person,
    *,
    note: str,
    for_menu: int | None,
    clock: Clock,
    token: str | None = None,
    only_day: dt.date | None = None,
) -> None:
    """Run a page-initiated menu build on the person's build lane."""
    from .. import building  # noqa: PLC0415
    from ..targets import profile_on  # noqa: PLC0415
    from . import menu_propose  # noqa: PLC0415

    outbox = app.outbox(db)
    says = ""
    try:
        says = menu_propose.propose_now(
            app,
            db,
            person,
            profile_on(db, person.id, clock.today_local()),
            outbox,
            clock=clock,
            replace=True,
            note=note,
            for_menu=for_menu,
            only_day=only_day,
        )
    except NothingToBuild as e:
        says = str(e)
    except AlreadyDone:
        says = ""
    finally:
        building.done(db, person.id, token)
    if token is not None:
        building.say(db, person.id, token, says)
    if says:
        outbox.reply(person, says)


def build_own_weekend(app: Any, db: Db, person_id: int) -> None:
    """This person's own version of the coming weekend, built because the weekend switch was
    flipped to it before anyone built it: in force as the switch says, and not sent - whoever
    flipped it is on the page. Queued on the person's own worker (web/api.py)."""
    from ..food import shared, weekend  # noqa: PLC0415

    clock = app.clock
    person = app.registry(db).by_id(person_id)
    if person is None:
        return
    today = clock.today_local()
    segment = shared.weekend_of(today)
    profile = targets.profile_on(db, person_id, segment.start)
    slot = f"switch-{clock.utc_stamp().replace(' ', 'T').replace(':', '')}"
    with ledger.run(db, "menu_propose", slot, person_id, clock=clock) as handle:
        if person_id in weekend.versions(db, segment).apart:
            handle.skip("already built")
            return
        ctx = JobContext(
            app=app,
            db=db,
            job=JOB,
            person=person,
            profile=profile,
            day=segment.start
            - dt.timedelta(
                days=int(app.config.get("schedule.menu_propose_days_before", DAYS_BEFORE))
            ),
            due_local=clock.now_local(),
            now_local=clock.now_local(),
            slot=slot,
            handle=handle,
            outbox=app.outbox(db),
            clock=clock,
        )
        _propose_for(ctx, segment, in_force=shared.mode(db) == weekend.APART, tell=False)
