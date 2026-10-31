"""menu_choices (each, Sunday 08:00) and menu_choices_close (each, daily 20:00): weekly choices.

For a person who chooses their weekday meals (`profiles.choices`, food/choices.py): on Sunday
morning the coming Monday to Friday is written as a few dishes for every meal, on their own build
lane - four model calls, about ten minutes, which must not hold their other messages - and they
are told when it is ready. They pick on the page; whatever they have not set by 20:00 the evening
before the week starts is set from their picks so far, the first dish of each meal standing for
any they did not pick, and the shopping list for the week follows.

A week that cannot be built says so, and the menu for those days is then built as it always was
(jobs/menu_propose.py stands down only for days a week of choices covers or is about to).
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from .. import building, targets
from ..clock import Clock
from ..db import Db
from ..errors import ModelUnavailable, NothingToBuild
from ..food import choices
from ..scheduler import Job, JobContext, fixed
from ..telegram import render as r
from ..telegram.api import inline_keyboard

SUNDAY = frozenset({6})


def ready_text(app: Any, built: choices.Built) -> str:
    """The message when a week of choices is ready: two lines and the way to the page."""
    link = r.choose_link(app.config, "Choose on the page")
    return (
        f"<b>Your choices for {r.esc(choices.days_words(built.days))} are ready.</b>\n"
        f"{built.offered} dishes, a few for every meal, each sized so the day adds up whichever "
        "you pick. The first of each is chosen already: change what you want, then tap Use these."
        + (f"\n{link}" if link else "")
        + f"\nNot chosen by {choices.CLOSE_AT} the evening before, the first choices stand."
    )


def ready_keyboard(set_id: int) -> Any:
    return inline_keyboard([[("Keep the first choices", f"k:chs:keep:{set_id}")]])


def settled_text(app: Any, settled: choices.Settled) -> str:
    """A week set from the bot or by the evening's close: what was set, and the shopping list."""
    link = (
        r.shopping_link(app.config, "the shopping list", list_id=settled.list_id)
        if settled.list_id
        else ""
    )
    said = r.esc(settled.said())
    return said + (f"\n{link}" if link else "")


def build_for(
    app: Any, db: Db, person: Any, days: list[dt.date], *, clock: Clock, token: str
) -> None:
    """Build a week of choices and say how it went - on the build lane, its mark cleared however
    it ends (`building.guarded`)."""
    outbox = app.outbox(db)
    key = choices.days_words(days)
    try:
        built = choices.build(app, db, person.id, clock=clock, days=days)
    except NothingToBuild as e:
        # nothing to build is an answer, not a fault: said, and the menu goes on as before
        outbox.send(person, "choices_failed", key, r.esc(f"No choices this week: {e}"))
        return
    except ModelUnavailable as e:
        said = (
            f"I could not write your choices for {key}: the model could not be asked. Your menu "
            "for those days is built as before."
        )
        outbox.send(person, "choices_failed", key, r.esc(said))
        outbox.alert(app.registry(db).admin(), "menu_choices", f"person={person.id} {e}"[:300])
        return
    if built.failed:
        outbox.send(person, "choices_failed", key, r.esc(built.failed))
        outbox.alert(
            app.registry(db).admin(), "menu_choices", f"person={person.id} {built.failed}"[:300]
        )
        return
    assert built.set_id is not None  # a build that did not fail has its set
    outbox.send(
        person,
        "choices_ready",
        str(built.set_id),
        ready_text(app, built),
        ready_keyboard(built.set_id),
    )


def start(app: Any, db: Db, person: Any, *, clock: Clock, asked: bool) -> tuple[str, list[dt.date]]:
    """Mark and dispatch a build of the coming weekdays' choices on the person's build lane.
    Returns (the mark's id, the days). Raises NothingToBuild when there is nothing to build."""
    days = choices.week_days(clock.today_local())
    if not days:
        raise NothingToBuild("There are no weekdays ahead to choose for.")
    if any(choices.covering(db, person.id, d) is not None for d in days):
        raise NothingToBuild(f"Your choices for {choices.days_words(days)} are already there.")
    token = building.mark(
        db,
        person.id,
        f"your choices for {choices.days_words(days)}",
        clock=clock,
        kind=building.CHOICES,
        asked=asked,
    )
    app.run_build(
        person.id,
        building.guarded(
            app,
            person,
            token,
            lambda bdb: build_for(app, bdb, person, days, clock=clock, token=token),
            broke="Something went wrong writing your choices for the week, and it did not "
            "finish. Your menu is built as before; the page can try again.",
        ),
        db,
    )
    return token, days


def run(ctx: JobContext) -> None:
    person = ctx.person
    if person is None or ctx.profile is None:
        ctx.handle.skip("no profile")
        return
    # whether they choose the week being built, not today: choosing booked from next Monday is
    # what this Sunday's build is for, and choosing booked off from Monday is no week to build
    ahead = choices.week_days(ctx.day)
    week_profile = targets.profile_on(ctx.db, person.id, ahead[0]) if ahead else ctx.profile
    if choices.per_slot_of(week_profile) == 0:
        ctx.handle.skip("weekly choices off")
        return
    now = building.current(ctx.db, person.id, clock=ctx.clock)
    if now is not None and now.get("asked"):
        ctx.handle.skip("a build asked for is running")
        return
    try:
        _token, days = start(ctx.app, ctx.db, person, clock=ctx.clock, asked=False)
    except NothingToBuild as e:
        ctx.handle.skip(str(e))
        return
    ctx.handle.note(f"choices for {choices.days_words(days)} dispatched")


def run_close(ctx: JobContext) -> None:
    """Set an unsettled week whose first day is tomorrow (or has come) from the picks so far."""
    person = ctx.person
    if person is None:
        ctx.handle.skip("no person")
        return
    row = choices.latest_set(ctx.db, person.id, ctx.day)
    if row is None or choices.outcome(ctx.db, int(row["id"])) is not None:
        ctx.handle.skip("no week waiting to be set")
        return
    first = dt.date.fromisoformat(str(row["first_day"]))
    if first > ctx.day + dt.timedelta(days=1):
        ctx.handle.skip("the week starts later")
        return
    from . import shop_refresh  # noqa: PLC0415 (the shop order the lists are built in)

    retailers = tuple(shop_refresh.retailer_order(ctx.app.config))
    try:
        settled = choices.settle(
            ctx.app,
            ctx.db,
            person.id,
            int(row["id"]),
            how="closed",
            clock=ctx.clock,
            retailers=retailers,
        )
    except NothingToBuild as e:
        ctx.handle.skip(str(e))
        return
    ctx.outbox.send(person, "choices_set", str(row["id"]), settled_text(ctx.app, settled))
    ctx.handle.note(f"set {len(settled.menu_ids)} menus from the picks so far")


JOB = Job(
    name="menu_choices",
    scope="each",
    kind="message",
    times=fixed("08:00"),
    days=SUNDAY,
    run=run,
    catch_up="+6h",
    default_answers=(("chs", "weekly choices are off"),),
)

CLOSE_JOB = Job(
    name="menu_choices_close",
    scope="each",
    kind="message",
    times=fixed(choices.CLOSE_AT),
    run=run_close,
    catch_up="+3h",
)

__all__ = [
    "CLOSE_JOB",
    "JOB",
    "build_for",
    "ready_keyboard",
    "ready_text",
    "run",
    "run_close",
    "settled_text",
    "start",
]
