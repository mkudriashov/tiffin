"""Send the day's target and first meal with a link to the page."""

from __future__ import annotations

import datetime as dt
from typing import Any

from .. import targets
from ..clock import isoday
from ..food import weekplan
from ..scheduler import Job, JobContext, daily
from ..telegram import render as r

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _refusal_tail(ctx: JobContext, menu_id: int) -> str:
    """Say when today's own menu still serves something the person has since refused.

    The bot's `/menu` and the page both already warn; the first thing read about the day's food
    must too. A menu is decided the moment it is built, so the fix that always works
    is /cook replace.
    """
    from ..food import catalogue  # noqa: PLC0415 (food imports the db layer)

    names = catalogue.refused_names_on(ctx.db, ctx.person_id, [menu_id])
    if not names:
        return ""
    verb = "is" if len(names) == 1 and not names[0].endswith("s") else "are"
    return f" {', '.join(names)} {verb} on your avoid list; /swap takes it off now."


def first_meal(ctx: JobContext) -> str:
    """The first dish of the day, or "" when there is no menu."""
    from ..food.menu import meals_for_day  # noqa: PLC0415

    meals = meals_for_day(ctx.db, ctx.person_id, ctx.day)
    return meals[0] if meals else ""


def meals_line(ctx: JobContext, menu_id: int | None, *, linked: bool) -> str:
    """ "First up: <dish>." and where the rest is - the page, or the buttons under the message."""
    from ..food import planning  # noqa: PLC0415 (jobs import the food layer late)

    first = first_meal(ctx)
    if not first:
        if planning.planning_of(ctx.profile) == "off":
            return "Tracking only: no menu, the targets are yours to eat to."
        return "No menu for today yet. /cook builds one."
    warn = _refusal_tail(ctx, menu_id) if menu_id is not None else ""
    if linked:
        link = r.page_today_link(ctx.app.config, label="The day on the page")
        tail = f" {link}." if link else ""
    else:
        tail = " Tap a meal for its recipe."
    return f"First up: {r.esc(first)}.{tail}{warn}"


def meal_keyboard(ctx: JobContext, menu_id: int | None) -> dict | None:
    """Today's meals as buttons, the same ones `/menu` puts under the same day."""
    if menu_id is None:
        return None
    from ..telegram.api import inline_keyboard  # noqa: PLC0415
    from . import menu_propose  # noqa: PLC0415 (jobs import the scheduler)

    rows = menu_propose.meal_buttons(ctx.db, menu_id, day=ctx.day)
    return inline_keyboard(rows) if rows else None


def _said(ctx: JobContext, meals: list[str]) -> bool:
    """Did a morning message for this food actually go out, on a day since it began?

    "The same food as yesterday" is only nothing new if yesterday's message - or the one before
    it, for the same boxes - was sent. A first morning that was paused, failed or lost to an
    outage left every later morning of the segment withheld as already said: no targets, no
    first meal, and for her no meal buttons, which are her only way to a recipe.
    """
    from ..food.menu import meals_for_day  # noqa: PLC0415

    day = ctx.day - dt.timedelta(days=1)
    for _ in range(7):  # a segment is a week at most
        if sorted(meals_for_day(ctx.db, ctx.person_id, day)) != sorted(meals):
            return False
        if ctx.outbox.already_sent(ctx.person_id, "morning_targets", isoday(day)) is not None:
            return True
        day -= dt.timedelta(days=1)
    return False


def nothing_new(ctx: JobContext, menu_id: int | None, t: Any) -> str:
    """Why this morning's message can be left unsent, or "" when it has something to say.

    Only a person whose rhythm is light or weekly is ever left without one, and only when today's
    dishes are yesterday's dishes, a morning message has already said them, today's menu holds
    nothing they avoid, and the targets are yesterday's. No menu at all is always said: a person
    hears about their own missing thing.
    """
    from ..food import planning  # noqa: PLC0415
    from ..food.menu import meals_for_day  # noqa: PLC0415
    from .evening_confirm import rhythm_of  # noqa: PLC0415

    rhythm = rhythm_of(ctx.profile)
    if rhythm == "daily":
        return ""
    if menu_id is None:
        # someone who tracks only has no menu by choice, so a morning is news only when the
        # targets change; anyone else's missing menu is always said
        if planning.planning_of(ctx.profile) != "off":
            return ""
        before = targets.latest_targets(ctx.db, ctx.person_id, ctx.day - dt.timedelta(days=1))
        if before is None or int(before["kcal"]) != int(t["kcal"]):
            return ""
        return f"{rhythm} rhythm: tracking only, the same targets as yesterday"
    today = meals_for_day(ctx.db, ctx.person_id, ctx.day)
    yesterday = meals_for_day(ctx.db, ctx.person_id, ctx.day - dt.timedelta(days=1))
    if not today or sorted(today) != sorted(yesterday) or not _said(ctx, today):
        return ""
    if _refusal_tail(ctx, menu_id):
        return ""
    # a new week's targets are news on the day they start: yesterday's are what to compare with
    yesterday_t = targets.latest_targets(ctx.db, ctx.person_id, ctx.day - dt.timedelta(days=1))
    if yesterday_t is not None and int(yesterday_t["kcal"]) != int(t["kcal"]):
        return ""
    return f"{rhythm} rhythm: the same food as yesterday"


def run(ctx: JobContext) -> None:
    person = ctx.person
    profile = ctx.profile
    if person is None or profile is None:
        ctx.handle.skip("no profile")
        return
    t = targets.ensure_targets(ctx.db, person.id, ctx.day, ctx.app.config, clock=ctx.clock)
    if t is None:
        ctx.handle.skip("no weight yet")
        return
    m = targets.macros_of(t)
    from ..food.menu import accepted_menu_id  # noqa: PLC0415

    menu_id = accepted_menu_id(ctx.db, person.id, ctx.day)
    quiet = nothing_new(ctx, menu_id, t)
    if weekplan.of(ctx.db, person.id).quiet:
        quiet = "messages only when needed: no morning message"
    if quiet:
        # light and weekly (migration 0035): the morning message only on a day it has something
        # to say. Tuesday to Friday of a batch-cooked week is the same boxes as Monday; the same
        # two lines every morning are the prompts that stopped being read (117 of 163 unanswered)
        ctx.handle.skip(quiet)
        return
    # the page is a 403 for a person whose login is not linked; never link them to it
    linked = bool(person.tailscale_login) and bool(r.page_today_link(ctx.app.config, ""))
    keyboard = None if linked else meal_keyboard(ctx, menu_id)
    context = {
        "day_label": f"{WEEKDAYS[ctx.day.weekday()]} {ctx.day.day} {ctx.day.strftime('%b')}",
        "kcal": f"{m.kcal:,}",
        "protein_g": m.protein_g,
        "meals_line": meals_line(ctx, menu_id, linked=linked),
    }
    html = r.render("morning_targets", context, ctx.tone)
    result = ctx.outbox.send(
        person,
        "morning_targets",
        isoday(ctx.day),
        html,
        keyboard,
        payload={"targets_id": int(t["id"])},
    )
    if not result.sent and result.status == "failed":
        ctx.handle.degrade(f"send failed: {result.error}")
    else:
        ctx.handle.note(result.status)


JOB = Job(
    name="morning_targets",
    scope="each",
    kind="message",
    times=daily("morning_time", "07:30"),
    run=run,
    catch_up="12:00",
    default_answers=(("morn", "07:30 for mornings"), ("rhy", "weekly message rhythm")),
)
