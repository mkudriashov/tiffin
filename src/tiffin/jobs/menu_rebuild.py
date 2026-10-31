"""Rebuild menu proposals."""

from __future__ import annotations

import html
import re
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any

from .. import targets
from ..clock import Clock, isoday, parse_day
from ..db import Db
from ..errors import (
    CarbsShort,
    Infeasible,
    ModelError,
    ModelInvalid,
    ModelUnavailable,
    TiffinError,
)
from ..food import catalogue, menu, packs, prefs, weekplan
from ..persons import Person
from ..scheduler import Job, JobContext, fixed
from ..telegram import packflow
from ..telegram import render as r
from ..telegram.api import inline_keyboard

if TYPE_CHECKING:
    from ..food.prefs import Changed

from .menu_propose import (
    _SHORTFALL_LINE,
    _SHORTFALLS,
    MAX_ATTEMPTS,
    WEEK_EXTRA_ATTEMPTS,
    _targets_for,
    carb_floor,
    change_row,
    first_shop_line,
    floors_line,
    lunch_follows,
    meal_buttons,
    message_for,
    propose_once,
    run,
    training_targets,
)


def why_not(reason: str) -> str:
    """The solver's reason, in words rather than in its own notation.

    not a sentence anybody should be handed. The numbers are the useful part, so they stay.
    """
    m = _SHORTFALL_LINE.search(reason or "")
    if m is None:
        return "There is no earlier menu to repeat either."
    macro, reached, needed = m.group(1).lower(), m.group(2), m.group(3)
    unit = "" if macro == "kcal" else " g"
    return (
        f"The closest it got was {reached}{unit} of {macro} against the {needed}{unit} your "
        "targets ask for, and there is no earlier menu to repeat. Send /cook again to retry, "
        "or /set rate to loosen the week."
    )


#: the marker `food/recipes.py`'s `too_much[...]` line writes; not a macro shortfall notation,
#: so `why_not`'s "max reachable" search never matches it and falls back to a sentence about
#: a menu to repeat that may not even exist.
_SIMPLICITY_MARK = "is not simple:"


def _only_simplicity(reasons: Sequence[str]) -> bool:
    """True when nothing the retry loop remembered was a macro shortfall - every refusal was
    the simplicity rule turning a dish away, so the fallback sentence should say that."""
    return bool(reasons) and all(_SIMPLICITY_MARK in r for r in reasons)


def why_not_simple(reasons: Sequence[str]) -> str:
    """Explain when every menu attempt exceeded the cooking simplicity limits.

    The result names the actual constraint rather than suggesting a target change."""
    detail = reasons[-1].split(_SIMPLICITY_MARK, 1)[-1].strip()
    return (
        f"Every attempt came back too complex to cook after work ({detail}), and there is no "
        "earlier menu to repeat. Send /cook again to have it try a plainer dish."
    )


def leanest_for(reason: str, items: list[catalogue.Ingredient], top: int = 6) -> str:
    """Name the ingredients that would fix the macro the solver could not reach.

    A re-prompt that only says "protein: max reachable 180 g < 201 g" tells the model it was
    wrong without telling it what to reach for, and the second attempt tends to fail the same
    way. Ranking the catalogue by the short macro per calorie turns the retry into a concrete
    instruction. The numbers still come from the database; the model only picks dishes.
    """
    # the solver writes "<day>: <macro>: max reachable ...", so a ":"-delimited token is the
    # reliable read. Not every reason has that shape though, so fall back to naming the macro
    # anywhere in the sentence - in a fixed order, because "fat" must not lose to a stray word.
    low = reason.lower()
    tokens = {t.strip() for t in low.split(":")}
    macro_name = next(
        (m for m in _SHORTFALLS if m in tokens),
        next((m for m in _SHORTFALLS if m in low), ""),
    )
    if not macro_name:
        return ""
    macro, label, tail = _SHORTFALLS[macro_name]
    ranked = sorted(items, key=lambda i: -(macro(i) / max(i.kcal_100, 1.0)))
    names = ", ".join(i.name for i in ranked[:top])
    return f"\nThe catalogue's best {label} is: {names}. {tail}"


def remember(seen: list[str], reason: str) -> list[str]:
    """Keep one reason per macro, newest first: the same floor missed twice is one lesson."""
    macro = next((m for m in _SHORTFALLS if m in reason.lower()), reason[:40])
    kept = [r for r in seen if (next((m for m in _SHORTFALLS if m in r.lower()), r[:40])) != macro]
    return [reason[:300], *kept][:4]


def note_refusal(
    db: Db,
    person: Person,
    note: str,
    *,
    clock: Clock,
    said: Callable[[str], None] | None = None,
) -> Changed | None:
    """Write the foods a cook note refuses into the profile. Returns the change - the foods that
    took effect, and the made-from questions they open (houmous, after chickpeas) - or None when
    the note refuses nothing.

    Persisted, not applied once, because "avoid chickpeas" is not a sentence about this week -
    people say it about a food they do not want to eat. It is reversible in one command and the
    reply says so; `profiles` is a ledger, so this is a new row and last week's menu still says
    what it was planned against.

    Only foods the catalogue actually holds. A note saying "no faff" must not quietly write a
    refusal for a word that matches nothing, and a term matching everything would empty the
    catalogue, so a term is taken only when it hits at least one ingredient and leaves some.

    The questions are kept, not dropped: every other way a food is refused asks about the foods
    made from it, and a note that kept only `added` left houmous on the list this very build
    picked from, with nothing ever asking.
    """

    wanted = catalogue.avoid_terms(note)
    if not wanted or targets.profile_on(db, person.id, clock.today_local()) is None:
        return None
    if prefs.half_read(db, person.id, wanted):
        return None  # "no salmon or chicken during Lent": not read whole, so none of it is kept
    # the one door every list change goes through (food/prefs.py): strict, because a note is
    # loose text and only the words naming a food are taken from it
    changed = prefs.amend(db, person.id, "avoid", add=wanted, clock=clock, strict=True)
    if changed.added and said is not None:
        said(f"refused from note: {', '.join(changed.added)}")
    return changed


def note_wishes(db: Db, person: Person, note: str, *, clock: Clock) -> list[str]:
    """The wishes about every menu a rebuild note makes ("less chia, more berries"), kept as
    standing notes - said with one rebuild, they were gone by the next menu - and a "less" of a
    liked food taken off the likes list. Returns only those newly kept: one already standing is
    not said again (the bot's "Rebuild now" sends the wish it has just kept)."""

    wishes = prefs.wishes_in(db, person.id, note)
    if not wishes or targets.profile_on(db, person.id, clock.today_local()) is None:
        return []
    noted = prefs.amend_notes(db, person.id, add=wishes, source="chat", clock=clock)
    prefs.less_of_liked(db, person.id, noted.added, clock=clock)
    return list(noted.added)


def wishes_said(kept: Sequence[str]) -> str:
    return (
        f"Kept for every menu from now on, not just this one: {r.esc('; '.join(kept))}. "
        "Tell me whenever you want to change them."
    )


def refuse_from_note(
    db: Db, person: Person, note: str, *, clock: Clock, said: Callable[[str], None]
) -> list[str]:
    """`note_refusal`, for a caller that needs only the foods that took effect."""
    changed = note_refusal(db, person, note, clock=clock, said=said)
    return [] if changed is None else list(changed.added)


def refusal_said(changed: Changed | None, *, rebuilt: bool = False, buttons: bool = False) -> str:
    """The refusal a note made, said back: the foods, the way to take them off the menu in force
    (or, `rebuilt`, the way to have them back), and each made-from question it opened. "" when
    the note refused nothing.

    A reply that is not a message of its own carries no buttons, so each question points at
    /avoid, which puts it with its two (the same as a chat reply does); `buttons` is for a
    caller that sends the answers itself (`telegram.foodflow.ask_rows`)."""

    if changed is None:
        return ""
    parts: list[str] = []
    if changed.added:
        names = r.esc(", ".join(changed.added))
        parts.append(
            f"{names} are off your menus from now on, not just this one. "
            f"<code>/set avoid - {names}</code> puts them back."
            if rebuilt
            else f"{names} are off your menus from now on; /swap takes them off the one you "
            "have now, at once."
        )
    # as many as the buttons beside them, or as /avoid puts with its own
    for ask in changed.asks[: 3 if buttons else 2]:
        parts.append(
            r.esc(prefs.ask_said(ask)) + ("" if buttons else " /avoid asks it with buttons.")
        )
    return " ".join(parts)


def refusal_for_page(db: Db, person: Person, note: str, *, clock: Clock) -> str:
    """A refusal in the note of a page rebuild the page turned away before any build began,
    written and said in the page's plain words ("" when it refuses nothing new): no build is
    verification)."""
    said = refusal_said(note_refusal(db, person, note, clock=clock))
    return html.unescape(re.sub(r"<[^>]+>", "", said))


def _propose_for(
    ctx: JobContext,
    segment: menu.Segment,
    *,
    note: str = "",
    refused: Changed | None = None,
    only_codes: frozenset[str] = frozenset(),
    quiet_failure: bool = False,
    in_force: bool = True,
    tell: bool = True,
    take_today: bool = False,
) -> str:
    """Build a menu for `segment` and send it. Shared by the Saturday job and `/cook`.

    `take_today` is the person's own word that today may be planned afresh though it began under
    food shopped and cooked for a menu, not a person from their own decision (store_plan's
    `own_choice`). A recorded day, or one gone, is never taken.

    `in_force=False` keeps the menu built and undecided - a weekend's own version while the
    switch says together (food/weekend.py) - and says nothing to anyone, failure included: the
    version in force is the one a person is told about. `tell=False` puts it in force silently
    (built because the switch was flipped, with the person on the page).

    `note` is what the person asked for in their own words, and it is NOT the same channel as
    `hint`: hint is the re-prompt after a failure ("protein: max reachable 180 g < 201 g") and
    is rendered as "The previous attempt failed". Feeding "more fish, please" through that
    would tell the model its own menu had failed for a reason it never gave. `refused` is what
    the note refused, written by `propose_now` before anything could turn the build away.

    Returns HTML for the person when there is something to say, or "" when the menu message
    was the answer. The scheduled job ignores it; `/cook` sends it.
    """
    person = ctx.person
    assert person is not None  # both callers check
    if refused is not None and refused.added:
        ctx.handle.note(f"refused from note: {', '.join(refused.added)}")
    said_first = refusal_said(refused)
    said_first += " " if said_first else ""
    # plan only the days a new menu may still take over, before any model call is paid for
    # store_plan applies the same rule as the last word
    today = ctx.clock.today_local()
    if (
        take_today
        and segment.start >= today
        and not any(menu.day_confirmed(ctx.db, person.id, d) for d in segment.days)
    ):
        open_: menu.Segment | None = segment
    else:
        open_ = menu.open_days(ctx.db, person.id, segment, today)
    if open_ is None:
        ctx.handle.skip("every day of that segment has started or is recorded")
        return said_first + (
            "Those days are already under way, so there is nothing left to plan. "
            "Send /cook for the next ones."
        )
    segment = open_
    # how they plan on the first day planned, not today: a change booked for next Monday is what
    # next Monday's menu is built under (food/planning.py)
    profile = targets.profile_on(ctx.db, person.id, segment.start) or ctx.profile
    macros = _targets_for(ctx, segment)
    if macros is None:
        # not a calendar gate any more: the only way here is no profile or no weight at all
        ctx.handle.skip("no profile or no weight yet")
        return said_first + (
            "I need your profile and a weight before I can plan a cook. Send /onboard."
        )
    items = catalogue.approved(ctx.db, person.id)
    if ctx.app.config.get("models.shuffle_catalogue", True):
        # the numbered list in a fresh order each week, the same order for the prompt and the
        # check of the answer: models favour what comes first (Hou 2023), and a list in code
        # order put almonds and bacon first every week of the year
        items = catalogue.shuffled(items, person.id, segment.start)
    if only_codes:
        # cook from what is already in the kitchen: the model picks by index from the list it
        # is given, so a food that is not on this list cannot reach the menu. A refused food
        # that was bought anyway is already gone - `approved` took it off first.
        items = [i for i in items if i.code in only_codes]
    if not items:
        ctx.handle.skip("catalogue empty")
        return said_first + (
            "My ingredient list is empty, so I cannot plan anything. Add ingredients first."
        )

    from . import shop_refresh  # noqa: PLC0415 (the shops, nearest first, as the list buys)

    # what the shop window has open, and the proteins it already cooks: every attempt but the
    # last is held to them, and the last keeps a plan that opens another pack and says so -
    # targets win over packs
    steer = packs.steer_for(
        ctx.db,
        person.id,
        segment.days,
        allowed={i.code for i in items},
        note=note,
        # their own answer where they gave one (/packs), else the house default
        budget=packs.mains_budget(
            ctx.db,
            person.id,
            int(ctx.app.config.get("guardrails.mains_per_window", packs.MAINS_PER_WINDOW)),
        ),
        items=items,
        retailers=shop_refresh.retailer_order(ctx.app.config),
    )
    # a little more food on a planned training day, where they asked for it
    day_targets = training_targets(ctx.db, person.id, segment, macros)
    opened: list[str] = []
    hint = ""
    seen: list[str] = []
    unreachable = False
    # the gateway's own reason when it would not ask: no consent is not an outage, and a retry at
    # 12:00 fails the same way until /consent says yes
    no_consent = False
    menu_id: int | None = None
    # an answer that fitted everything but the carbohydrate floor: asked about once, then kept
    kept: tuple[Any, str | None] | None = None

    def keep() -> int | None:
        """The kept answer, built without the carbohydrate floor and stored; None if it will
        not build after all."""
        if kept is None:
            return None
        try:
            # the note too: a dinner it asks for by name is out of the gap for this answer as well
            return propose_once(
                ctx,
                segment,
                profile,
                macros,
                items,
                note=note,
                answer=kept,
                steer=steer.relaxed(),
                opened=opened,
                day_targets=day_targets,
                take_today=take_today,
            )
        except (Infeasible, ModelInvalid) as e:
            ctx.handle.note(f"the answer kept for its carbs would not build: {str(e)[:120]}")
            return None

    # a whole week planned in one build for someone who cooks each day is twenty meals under
    # five). A batch week is a few dishes, and keeps its five
    # ...and so is any part of one: Wednesday to Friday of hers failed on all five
    whole_week = len(segment.days) >= 2 and weekplan.of(ctx.db, person.id).plan_day is not None
    attempts = MAX_ATTEMPTS + (WEEK_EXTRA_ATTEMPTS if whole_week else 0)
    for attempt in range(attempts):
        try:
            menu_id = propose_once(
                ctx,
                segment,
                profile,
                macros,
                items,
                hint=hint,
                note=note,
                carb_min=carb_floor(macros),
                steer=steer if attempt < attempts - 1 else steer.relaxed(),
                opened=opened,
                day_targets=day_targets,
                take_today=take_today,
            )
            break
        except CarbsShort as e:
            ctx.handle.note(f"attempt {attempt + 1} short of carbs: {str(e)[:200]}")
            if kept is not None:
                # asked once already: the latest answer is as good as asking again gets
                kept = e.answer
                menu_id = keep()
                break
            kept = e.answer
            seen = remember(seen, str(e))
            hint = "\n".join(seen) + "".join(leanest_for(r, items) for r in seen)
            if isinstance(e, Infeasible):
                hint += floors_line(macros, profile) + getattr(e, "dishes", "")
            continue
        except (Infeasible, ModelInvalid) as e:
            if kept is not None:
                # the one more try for carbs went wrong another way: the kept answer is a menu
                ctx.handle.note(f"attempt {attempt + 1} refused: {str(e)[:200]}")
                menu_id = keep()
                break
            seen = remember(seen, str(e))
            hint = "\n".join(seen) + "".join(leanest_for(r, items) for r in seen)
            if isinstance(e, Infeasible):
                hint += floors_line(macros, profile) + getattr(e, "dishes", "")
            # `note`, not `degrade`: a refused draft that the re-ask then fixes (a solver miss,
            # or the simplicity validator sending back a too-complex dish) is the retry working
            # as designed, not the job doing less than asked. `degrade` is sticky with no way
            # back, so a real recovery here was reporting "degraded" for a menu the person got
            # in full and on time - the run_ledger noise was indistinguishable from a genuine
            # problem. Every branch below that actually falls short (unreachable model, a note
            # that could not be honoured, a repeat, no menu at all) still calls `degrade` itself.
            ctx.handle.note(f"attempt {attempt + 1} refused: {str(e)[:200]}")
        except ModelError as e:
            if kept is not None:
                menu_id = keep()
                if menu_id is not None:
                    break
            # A model that did not answer is not a solver shortfall, and there is nothing to
            # feed back to it. Catching every ModelError above meant `unreachable` could never
            # be set: four full attempts were spent against a dead model, and the person was
            # then told their TARGETS were impossible and pointed at /set rate to loosen a week
            # that was never the problem. ModelInvalid (a bad answer) is worth another go;
            # ModelUnavailable, ModelTimeout and QuotaExhausted are not.
            ctx.handle.degrade(f"model unavailable: {e}")
            unreachable = True
            no_consent = isinstance(e, ModelUnavailable) and "no_consent" in str(e)
            break
        except TiffinError as e:
            ctx.handle.degrade(f"model unavailable: {e}")
            unreachable = True
            break
    if menu_id is None and kept is not None and not unreachable:
        # short of carbs on the last attempt there was: still a menu
        menu_id = keep()
    repeated = False
    if menu_id is None and not in_force:
        # nobody is eating this version: no repeat of last week, no message - the run says it
        ctx.handle.degrade("the weekend's own version could not be built")
        return ""
    if menu_id is None and unreachable:
        held = menu.current_menu(ctx.db, person.id, segment.start) is not None
        if no_consent:
            # the real reason, and no retry promised: the schedule's 12:00 meets the same no.
            # A page rebuild shows this on the page too (`rebuild_for_page`)
            return said_first + (
                "Menus are written by Claude, and Claude processing is off for you, so "
                + (
                    "the menu you have stays as it is. /consent turns it on; Swap on a meal "
                    "needs no model."
                    if held
                    else "there is no menu yet. /consent turns it on, then /cook builds one."
                )
            )
        if held:
            # a replacement: the menu in force stays, and nothing retries a replacement at
            return said_first + (
                "I could not reach a model just now, so the menu you have stays as it is. To "
                "change one dish now, Swap on that meal needs no model; ask for a new menu "
                "again later."
            )
        return said_first + (
            "I could not reach a model just now, so there is no menu yet. I will try again at "
            "12:00 and 18:00; /cook tries straight away."
        )
    if menu_id is None and note:
        # repeating the menu they just asked to change is worse than saying it did not work
        ctx.handle.degrade(f"change '{note[:60]}' could not be built")
        return said_first + (
            "I could not build a menu around that. Try naming a food rather than a rule "
            "(<code>more fish</code>, <code>no rice</code>), or send /cook to take the "
            "ordinary one."
        )
    if menu_id is None and quiet_failure:
        # an operator build: say why to whoever ran it, and message nobody. Repeating her last
        # menu would send it to her as "Menu repeated" when nobody asked her anything.
        ctx.handle.degrade("could not be built from the foods given")
        return "Could not build a menu from those foods. " + (hint or "The model gave nothing.")
    cleaned = None
    if menu_id is None:
        menu_id, cleaned = repeat_less_refused(
            ctx.db, ctx.app.config, person.id, segment, clock=ctx.clock, handle=ctx.handle
        )
        repeated = True
        if menu_id is None:
            ctx.handle.degrade("no menu and nothing to repeat")
            if not person.is_admin:
                # he IS the admin, so alerting him about himself sent the same failure twice:
                # once as "tiffin menu_failed owner: ..." in raw solver vocabulary, and once as
                ctx.outbox.alert(
                    ctx.app.registry(ctx.db).admin(),
                    "menu_failed",
                    f"{person.slug}: menu for {segment.start} could not be built: {hint}",
                )
            # numbers, no /cook, no /set rate, and a pointer to a third party about her own
            # targets. The reason and both remedies were computed and in hand, and the branch
            # handed them to the person who did not need them. This is her week one, every time:
            # there is no earlier menu to repeat because she has never had one. Same sentence for
            # both people; the admin still hears that a job degraded, which is a separate job.
            if _only_simplicity(seen):
                return why_not_simple(seen)
            return "I could not build a menu that hits your targets. " + why_not(hint)

    if not in_force:
        ctx.handle.note(f"menu {menu_id} built and kept for the weekend switch")
        return ""
    if not repeated:
        # created and that's it, we do not need to click more buttons?" He is right: a menu is
        # the menu the moment it exists, so it is decided here, in the same flow that stores and
        # sends it, rather than waiting on a tap that was never really a choice between two menus
        # - only between this one and the change note nobody had typed yet. The ledger still
        # keeps a row (`decide` is never skipped), it is just not a human's row. Every reader
        # (`v_menu_current`, `accepted_menu_id`, the shopping list, /batch, the page) already
        # follows the latest decision rather than the human-ness of it, so nothing downstream
        # changes. A `repeated` menu needs no decision at all: its `status` alone already makes
        # it current, which is why the Accept step never existed for that one.
        menu.decide(ctx.db, menu_id, "accepted")
        lunch_follows(ctx, segment, menu_id)
    if not tell:
        ctx.handle.note(f"menu {menu_id} built for the weekend switch")
        return ""
    # the whole week on the page, from its first day - for someone the page lets in, never a
    # link to a 403
    page_line = ""
    if person.tailscale_login:
        link = r.menu_link(ctx.app.config, isoday(segment.start), label="The whole week")
        page_line = f"{link} on the page: every recipe and the shopping list." if link else ""
    html, payload = message_for(ctx.db, menu_id, repeated=repeated, page_line=page_line)
    shop_line = first_shop_line(ctx, person, segment)
    if shop_line:
        html = f"{html}\n{shop_line}"
    if opened and not repeated:
        # a pack opened though another was open: kept because targets win, and said
        html = f"{html}\n{r.esc(opened[0])}"
        ctx.handle.note(f"new pack: {opened[0][:160]}")
    if cleaned is not None and (cleaned.swapped or cleaned.stuck):
        html = f"{html}\n{r.esc(cleaned.said())}"
    rows = [] if page_line else meal_buttons(ctx.db, menu_id)
    # no Accept: the tap it answered no longer exists. Change (k:menux) still rebuilds the days
    # this menu covers, and the meal buttons still open each dish - the only two things a menu
    # already in force leaves for a tap to do. A repeat carries them too: the repeat after every
    rows = [change_row(menu_id), *rows]
    if not repeated and packs.mains_answer(ctx.db, person.id) is None:
        html += f"\n{r.esc(packs.MAINS_QUESTION)}"
        rows = [*rows, *packflow.buttons()]
    # the foods were read before minutes of model calls, and the person is told everything else
    # still answers meanwhile: "I do not want chicken" said then was kept and confirmed with no
    # swap to offer (this menu was not stored yet), and the menu landed with chicken in it and
    # not a word. Said here, with the one tap that takes it out
    avoided = catalogue.refused_names_on(ctx.db, person.id, [menu_id])
    if avoided:
        tap = "Swap it now" if len(avoided) == 1 else "Swap them now"
        html += (
            f"\nThis menu still has {r.esc(r.few(avoided))} in it, which you now avoid: "
            f"{tap} takes {'it' if len(avoided) == 1 else 'them'} out."
        )
        rows = [*rows, [(tap, "k:swapall")]]
    keyboard = inline_keyboard(rows) if rows else None
    result = ctx.outbox.send(
        person,
        "menu_proposal",
        # one key per MENU, not per segment start. The outbox short-circuits a repeated
        # (person, kind, due_key), so a menu rebuilt for the same days - which is exactly what
        # "Change" and `/cook more fish` do - was solved, stored, charged for and then thrown
        # away, because Saturday's proposal already occupied that key. The person's stated
        # preference vanished and they cooked the food they had just rejected.
        f"menu:{menu_id}",
        html,
        keyboard,
        payload=payload,
    )
    # menus is append-only: the link lives in the message payload, not a back-fill
    ctx.handle.note(f"menu {menu_id} {'repeated' if repeated else 'proposed'}: {result.status}")
    # say that the refusal STUCK, and how to undo it. "Avoid chickpeas" that silently means only
    # this week is the same broken promise from the other side. "" when the note refused nothing:
    # the menu itself is the answer
    return refusal_said(refused, rebuilt=True)


JOB = Job(
    name="menu_propose",
    scope="each",
    kind="message",
    # two chances a day: a build that failed at 18:00 is retried at 12:00 the next day, before
    # the segment it was for has started
    times=fixed("12:00", "18:00"),
    run=run,
    catch_up="+5h",
    default_answers=(
        ("pln", "automatic menus"),
        ("segs", "one weekday batch"),
        ("refu", "no foods marked to avoid"),
        ("kit", "recipes use an oven and hob until you set your kitchen"),
        ("plan", "no fixed training sessions"),
    ),
)


def repeat_less_refused(
    db: Db,
    config: Any,
    person_id: int,
    segment: menu.Segment,
    *,
    clock: Clock,
    handle: Any,
) -> tuple[int | None, Any]:
    """`menu.repeat_last`, less what has been refused since. (menu id, swap.Cleaned), or
    (None, None) when there is nothing to repeat.

    A repeat copied the chickpea salad back in for somebody who had said "no chickpeas" in
    between. Each such dish is swapped for one already eaten, in code, before the menu is sent
    (food/swap.py) - on the days the repeat covers and no others: today's cooked boxes, left as
    they were on purpose, and a weekend cooked for two were rewritten because one person asked
    to repeat their week. Both repeats go through here: `/cook repeat`, and
    the fallback when a build fails every attempt - which is unattended, from the 11:00 and
    18:00 jobs, so a refusal has to hold there most of all."""
    from ..food import swap  # noqa: PLC0415 (food imports the db layer)

    menu_id = menu.repeat_last(db, person_id, segment, clock=clock)
    if menu_id is None:
        return None, None
    own = db.one("SELECT segment_start, segment_end FROM menus WHERE id = ?", (menu_id,))
    days = (
        menu.Segment(parse_day(str(own["segment_start"])), parse_day(str(own["segment_end"])))
        if own is not None
        else segment
    )
    cleaned = swap.swap_avoided(db, person_id, config=config, clock=clock, within=days)
    if cleaned.swapped:
        menu_id = menu.segment_menu_id(db, person_id, days.start) or menu_id
        handle.note(f"swapped {len(cleaned.swapped)} refused dishes out of the repeat")
    if cleaned.stuck:
        handle.degrade(
            f"repeat still holds {len(cleaned.stuck)} refused dishes with nothing to swap in"
        )
    return menu_id, cleaned


# ---------------------------------------------------------------- asked for, not scheduled
