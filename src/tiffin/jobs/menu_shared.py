"""Build one shared weekend menu with each person's own portions.

Both people must choose shared cooking. The model receives common dishes and
code balances each person's plates separately. A failed build is recorded and
explained to each person."""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .. import building, ledger, targets
from ..clock import Clock, isoday
from ..db import Db
from ..energy import Macros
from ..errors import Infeasible, ModelError, ModelInvalid, NothingToBuild, TiffinError
from ..food import catalogue, menu, prefs, shared, weekend
from ..model import prompts
from ..persons import Person
from ..scheduler import Job, JobContext, fixed
from ..telegram import render as r
from ..telegram.api import inline_keyboard

if TYPE_CHECKING:
    from ..food.prefs import Changed

THURSDAY, FRIDAY, SATURDAY = 3, 4, 5
#: how far above the stricter floor the model is asked to aim. Aimed exactly at the floors, the
#: second balanced build on 26 Sep missed by a gram four times running (protein 200 < 201,
#: fibre 24 < 25, fat 70 < 71, fibre 24 < 25): the portions need room above a floor to land on
#: it, and the targets themselves stay exactly as strict.
HEADROOM = 1.05


def _scale_days(
    segment: menu.Segment,
    profiles: Mapping[int, sqlite3.Row | None],
    macros: Mapping[int, Macros],
    fibre_min: Mapping[int, int],
) -> list[dict[str, Any]]:
    """One ordinary day per date for the model to balance against, named for neither person.

    The figures are for the dishes BOTH of them eat. Protein, fat and fibre are floors over each
    person's own plates, and a slot only one of them has - the owner's snack - is no help to the
    other:
    given one day over all five slots on the 28 Sep targets, the model put 9 g of its fibre into
    stricter of their two shares of a day's calories, applied to the shared dishes alone, plus
    HEADROOM, and the calories are each person's day in proportion to the meals they share. A
    slot only one of them eats is named on its own, as extra on top, with its own share.
    """
    own = {pid: menu.slots_for(profiles.get(pid)) for pid in macros}
    union = [s for s in menu.SLOT_ORDER if any(s in o for o in own.values())]
    if not macros:
        return [{"day": isoday(d), "slots": ", ".join(union)} for d in segment.days]
    common = [s for s in union if all(s in o for o in own.values())] or union
    extra = [s for s in union if s not in common]
    part = {pid: macros[pid].kcal * len(common) / max(len(own[pid]), 1) for pid in macros}
    kcal = round(sum(part.values()) / len(part))

    def floor(per_person: Mapping[int, float]) -> int:
        share = max(per_person[pid] / max(macros[pid].kcal, 1) for pid in macros)
        return math.ceil(share * kcal * HEADROOM)

    spec: dict[str, Any] = {
        "kcal": kcal,
        "protein_g": floor({pid: m.protein_g for pid, m in macros.items()}),
        "fat_g": floor({pid: m.fat_g for pid, m in macros.items()}),
        "fibre_g": floor({pid: fibre_min.get(pid, 25) for pid in macros}),
    }
    if extra:
        pieces = []
        for s in extra:
            eaters = [pid for pid in macros if s in own[pid]]
            per = round(
                sum(macros[p].kcal / max(len(own[p]), 1) for p in eaters) / max(len(eaters), 1)
            )
            pieces.append(
                f"{s.replace('_', '-')} for one of them only, about {per} kcal on top of those "
                "figures, carrying its own share of protein and fibre"
            )
        spec["also"] = "; ".join(pieces)
    return [{"day": isoday(d), "slots": ", ".join(common), **spec} for d in segment.days]


def retry_text(
    seen: list[str], reason: str, items: Sequence[catalogue.Ingredient]
) -> tuple[list[str], str]:
    """Describe every unmet nutrient limit and the foods that can address it."""
    from . import menu_propose  # noqa: PLC0415

    seen = menu_propose.remember(seen, reason)
    text = "\n".join(seen) + "".join(menu_propose.leanest_for(r, list(items)) for r in seen)
    return seen, text


@dataclass(frozen=True)
class Outcome:
    """What a build did: what to say to whoever asked ("" when the menus were the answer), how
    far apart the portions were when only an uneven answer could be kept, and the menus it sent,
    by person."""

    said: str
    evenest: float | None = None
    sent: Mapping[int, int] = field(default_factory=dict)


def _open_weekend(db: Db, persons: Sequence[Person], today: Any) -> menu.Segment:
    """The days of this weekend a shared build may still take over: open for both of them (a
    day one of them recorded or began under a menu already in force is closed), and never a day
    that has gone - a Sunday /cook after a failed weekend planned the Saturday."""
    segment = shared.weekend_of(today)
    starts = []
    for p in persons:
        open_ = menu.open_days(db, p.id, segment, today)
        if open_ is None:
            raise NothingToBuild("This weekend is already under way. /cook plans the week ahead.")
        starts.append(open_.start)
    return menu.Segment(max(starts), segment.end)


def build_now(
    app: Any,
    db: Db,
    *,
    clock: Clock,
    note: str = "",
    on_refusal: Callable[[str], None] = lambda _s: None,
    in_force: bool | None = None,
    tell: bool = True,
) -> Outcome:
    """Build the coming (or current) weekend's version cooked together.

    `in_force` defaults to the switch: together puts the pair in force and sends it to both
    (`tell`); each your own keeps it, built and silent, for the switch. `on_refusal` hears each
    attempt that was sent back, so the run ledger says why a retry happened.
    """
    from . import menu_propose  # noqa: PLC0415

    today = clock.today_local()
    registry = app.registry(db)
    persons = [p for p in (registry.by_id(pid) for pid in shared.people(db)) if p is not None]
    segment = _open_weekend(db, persons, today)
    if in_force is None:
        in_force = shared.mode(db) == weekend.TOGETHER
    profiles = {p.id: targets.profile_on(db, p.id, segment.start) for p in persons}
    macros: dict[int, Macros] = {}
    for p in persons:
        row = targets.ensure_targets(db, p.id, segment.start, app.config, clock=clock)
        if row is None:
            # `shared.possible` already asks this; a race with a new person is not the other
            # person's gap to be told about, so this names nobody
            raise NothingToBuild("The weekend cannot be cooked together yet.")
        macros[p.id] = targets.macros_of(row)
    fibre_min = {
        pid: int(profile["fibre_g_min"]) if profile is not None else 25
        for pid, profile in profiles.items()
    }
    gateway = app.gateway(db)
    if gateway is None:
        raise ModelError("no model gateway")
    ids = [p.id for p in persons]
    shuffle = bool(app.config.get("models.shuffle_catalogue", True))
    items = shared.shared_items(db, ids, shuffle_on=segment.start if shuffle else None)
    favourites: list[str] = []
    blocked: list[str] = []
    recent: list[str] = []
    leftovers: list[str] = []
    gap: list[str] = []
    # whose pick it is: the one the shared meals of the last weeks suited less leads with their
    # favourites, and an even record alternates by week - fairness over a run of weekends, not
    # each one alone (Stratigi 2020). Only the order changes: nobody's numbers reach the prompt.
    from ..food import taste as taste_mod  # noqa: PLC0415 (food imports the db layer)

    order = list(ids)
    if len(ids) == 2:
        first_w = taste_mod.fair_weight(db, ids[0], ids[1], segment.start)
        turn = 0 if first_w > 0.5 else 1 if first_w < 0.5 else segment.start.isocalendar()[1] % 2
        order = [ids[turn], ids[1 - turn]]
    cookable = {i.code for i in items}
    for pid in order:
        good, bad = menu.rated_names(db, pid)
        good = taste_mod.favourites_now(db, pid, good, segment.start)
        others = [o for o in ids if o != pid]
        good = shared.for_both(db, good, others, segment.start, cookable)
        favourites += [g for g in good if g not in favourites]
        blocked += [b for b in bad if b not in blocked]
        recent += [n for n in menu.recent_names(db, pid, segment.start) if n not in recent]
        leftovers += [f for f in menu_propose.leftover_lines(db, pid, today) if f not in leftovers]
        # a dinner the note asks for by name is theirs to have again (menu.dinner_gap)
        gap += [n for n in menu.dinner_gap(db, pid, segment.start, asked=note) if n not in gap]
    equipment: list[str] = []
    for p in profiles.values():
        equipment += [e for e in menu.equipment_of(p) if e not in equipment]
    taste = weekend.taste_line([weekend.taste(db, pid) for pid in ids])
    # both people's standing notes, unnamed: both eat every dish, so her "no salads" holds on the
    # weekend cooked together as on her own week - and her "simple on weekdays" does not
    notes: list[str] = []
    for pid in ids:
        notes += [n for n in prefs.standing_notes(db, pid, weekend=True) if n not in notes]

    # the packs both of them still have open from the week (Thursday's chicken on Saturday) and
    # the smaller of their two budgets: one dish, two plates, one shop
    from ..food import packs as packs_mod  # noqa: PLC0415 (food imports the db layer)

    default = int(app.config.get("guardrails.mains_per_window", packs_mod.MAINS_PER_WINDOW))
    budgets = [packs_mod.mains_budget(db, pid, default) for pid in ids]
    limited = [b for b in budgets if b]
    opened: list[packs_mod.Pack] = []
    for pid in ids:
        for p in packs_mod.carried(db, pid, segment.start):
            if p.code in cookable and p.code not in {o.code for o in opened}:
                opened.append(p)
    steer = packs_mod.Steer(
        open=tuple(opened),
        names={p.code: p.name for p in opened},
        budget=min(limited) if limited else 0,
        note=note,
    )
    index = {ing.code: n for n, ing in enumerate(items, start=1)}
    open_lines, mains_line = steer.prompt(index)
    lessons: list[str] = []

    def prompt_for(hint: str) -> str:
        nonlocal lessons
        text = prompts.menu_shared_weekend(
            ingredients=catalogue.prompt_lines(items),
            days=_scale_days(segment, profiles, macros, fibre_min),
            favourites=favourites,
            blocked=blocked,
            recent=recent,
            leftovers=leftovers[:8],
            equipment=equipment,
            asked_for=note,
            taste=taste,
            dinner_gap=gap,
            notes=notes,
            packs=open_lines,
            mains=mains_line,
        )
        if hint:
            lessons, said = retry_text(lessons, hint, items)
            text += f"\n\nThe previous attempt failed: {said}\nFix exactly that and answer again."
        return text

    def ask(prompt: str, _hint: str) -> tuple[Mapping[str, Any], str | None]:
        # asked again before every call, not once at the start: she withdrew during the first
        # attempt and three more went out with the joint prompt
        if not shared.possible(db, shared.weekend_of(today), app.config.model_scope()):
            raise NothingToBuild("The weekend can no longer be cooked together.")
        result = gateway.json_task("menu_shared", prompt, "menu_proposal", person_id=ids[0])
        return result.data or {}, result.model_used

    built = shared.build(
        db,
        segment,
        persons,
        profiles,
        macros,
        ask,
        prompt_for=prompt_for,
        clock=clock,
        note=on_refusal,
        accept=in_force,
        shuffle=shuffle,
        gap=gap,
        steer=steer,
    )
    if in_force and tell:
        for p in persons:
            mid = built.menu_ids[p.id]
            link = r.menu_link(app.config, isoday(segment.start), label="The weekend")
            page_line = (
                f"{link} on the page: every recipe, both plates."
                if link and p.tailscale_login
                else ""
            )
            html, payload = menu_propose.message_for(
                db,
                mid,
                page_line=page_line,
                head="<b>Weekend menu, cooked together</b>",
            )
            rows = [] if page_line else menu_propose.meal_buttons(db, mid)
            rows = [menu_propose.change_row(mid), *rows]
            app.outbox(db).send(
                p,
                "menu_proposal",
                f"menu:{mid}",
                html,
                inline_keyboard(rows),
                payload={**payload, "shared": built.shared_id},
            )
    return Outcome("", built.evenest, built.menu_ids if in_force and tell else {})


def fall_back(app: Any, db: Db, segment: menu.Segment, *, clock: Clock, why: str) -> None:
    """The weekend cooked together could not be built while the switch says together: each
    person's own version, built beside it, is put in force for this weekend - the switch stays
    where it is - and each is told in their own words; one with no version of their own yet hears
    there is no weekend menu, and that /cook tries now. Once per person per weekend."""
    registry = app.registry(db)
    own = weekend.versions(db, segment).apart
    for pid in shared.people(db):
        person = registry.by_id(pid)
        if person is None:
            continue
        if pid in own:
            weekend.put_in_force(db, pid, own[pid], segment)
            text = (
                "The weekend menu for the two of you could not be built this time, so your own "
                "weekend menu is in force. The page has it."
            )
        else:
            text = "There is no weekend menu yet. I will try again, and /cook tries straight away."
        app.outbox(db).send(person, "menu_failed", f"weekend:{isoday(segment.start)}", text)
    del why  # the reason is the run ledger's, never a chat's


def run(ctx: JobContext) -> None:
    segment = shared.weekend_of(ctx.day)
    if not shared.possible(ctx.db, segment, ctx.app.config.model_scope()):
        ctx.handle.skip("each person plans their own weekend")
        return
    people = set(shared.people(ctx.db))
    if people <= set(weekend.versions(ctx.db, segment).together):
        ctx.handle.skip("the weekend already has a shared menu")
        return
    marks = [building.current(ctx.db, pid, clock=ctx.clock) for pid in sorted(people)]
    if any(m is not None and m["kind"] == building.MENU and m["asked"] for m in marks):
        # a menu one of them asked for is building - a /cook or a Change on this weekend runs
        # the same shared build, and the one that ended last would win
        ctx.handle.skip("a menu asked for is being built")
        return
    in_force = shared.mode(ctx.db) == weekend.TOGETHER
    # marked for both while it runs, so neither page starts a shared build beside it
    tokens = {
        pid: building.mark(ctx.db, pid, "the weekend menu", clock=ctx.clock, asked=False)
        for pid in sorted(people)
        if in_force
    }
    try:
        got = build_now(
            ctx.app, ctx.db, clock=ctx.clock, on_refusal=ctx.handle.note, in_force=in_force
        )
    except NothingToBuild as e:
        ctx.handle.skip(str(e))
        return
    except (Infeasible, ModelInvalid) as e:
        ctx.handle.degrade(f"shared weekend could not be built: {str(e)[:200]}")
        if in_force:
            fall_back(ctx.app, ctx.db, segment, clock=ctx.clock, why=str(e))
        return
    except (ModelError, TiffinError) as e:
        ctx.handle.degrade(f"model unavailable: {str(e)[:200]}")
        if in_force:
            fall_back(ctx.app, ctx.db, segment, clock=ctx.clock, why=str(e))
        return
    finally:
        for pid, token in tokens.items():
            building.done(ctx.db, pid, token)
    if got.evenest is not None:
        # a weekend both can cook, and still less than was asked: said on the status strip
        ctx.handle.degrade(f"kept the evenest attempt: portions {got.evenest:.1f}x apart")
        return
    ctx.handle.note(
        "shared weekend built and sent" if in_force else "shared weekend built and kept"
    )


def cook_together(
    app: Any,
    db: Db,
    *,
    clock: Clock,
    note: str = "",
    person: Person | None = None,
    refused: Changed | None = None,
) -> str:
    """`/cook` or Change over a shared weekend: rebuild it for both, in a ledger run.

    A note that refuses a food ("no broccoli") is a refusal for the person asking, written before
    the build - the shared list is the intersection of both catalogues, so the food then cannot
    reach the menu. It is said back,
    with the made-from questions it opens (houmous, after chickpeas), the way a build of one
    has written it already (`menu_propose.propose_now`): written again, it reads "already" and
    the words saying it stuck are lost.

    A food either of them refuses while the model works is on the weekend that lands, and each
    is told of their own, with the one tap that takes it out (`_still_avoided`)."""
    from . import menu_propose  # noqa: PLC0415

    slot = f"manual-{clock.utc_stamp().replace(' ', 'T').replace(':', '')}"
    with ledger.run(db, "menu_shared", slot, ledger.SYSTEM, clock=clock) as handle:
        if refused is None and person is not None and note:
            refused = menu_propose.note_refusal(db, person, note, clock=clock)
        if refused is not None and refused.added:
            handle.note(f"refused from note: {', '.join(refused.added)}")
        said_first = menu_propose.refusal_said(refused)
        said_first += " " if said_first else ""
        try:
            got = build_now(app, db, clock=clock, note=note, on_refusal=handle.note)
        except (Infeasible, ModelInvalid) as e:
            handle.degrade(f"shared weekend could not be built: {str(e)[:200]}")
            if note:
                return said_first + (
                    "I could not build a weekend that fits both of you around that. Try naming "
                    "a dish or a food, or send /cook again."
                )
            # "Just a new menu" carries no note, and "around that" named a request nobody made
            #
            held = person is not None and menu.current_menu(
                db, person.id, shared.weekend_of(clock.today_local()).start
            )
            return (
                "I could not build a different weekend that fits both of you this time, so the "
                "one you have stays. Change on it tries again."
                if held
                else "I could not build a weekend that fits both of you this time, so there is "
                "no weekend menu yet. Send /cook to try again."
            )
        except (ModelError, TiffinError) as e:
            if isinstance(e, NothingToBuild):
                # a weekend already under way: the refusal still stood, and is said with it
                if said_first:
                    raise NothingToBuild(said_first + str(e)) from e
                raise
            handle.degrade(f"model unavailable: {str(e)[:200]}")
            return said_first + "I could not reach a model just now. /cook tries again."
        if got.evenest is not None:
            handle.degrade(f"kept the evenest attempt: portions {got.evenest:.1f}x apart")
        else:
            handle.note("shared weekend rebuilt and sent")
    _still_avoided(app, db, got.sent)
    rebuilt = menu_propose.refusal_said(refused, rebuilt=True)
    return f"{rebuilt} {got.said}".strip()


def _still_avoided(app: Any, db: Db, sent: Mapping[int, int]) -> None:
    """Each person whose weekend, just sent, holds a food they refused while the model worked,
    told of their own and given the one tap that takes it out. The foods were read before
    minutes of model calls, the person is told everything else still answers meanwhile, and a
    refusal said then was confirmed with nothing yet to swap - the weekend then landed with the
    food on it and not a word. Never the other's food."""
    registry = app.registry(db)
    for pid, mid in sent.items():
        avoided = catalogue.refused_names_on(db, pid, [mid])
        person = registry.by_id(pid)
        if not avoided or person is None:
            continue
        one = len(avoided) == 1
        tap = "Swap it now" if one else "Swap them now"
        app.outbox(db).reply(
            person,
            f"The weekend menu still has {r.esc(', '.join(avoided))} in it, which you now "
            f"avoid: {tap} takes {'it' if one else 'them'} out.",
            inline_keyboard([[(tap, "k:swapall")]]),
        )


def build_quietly(app: Any, db: Db) -> None:
    """The version cooked together, built because the switch was flipped to it before anyone
    built it: in force as the switch says, and not sent - whoever flipped it is on the page."""
    clock = app.clock
    slot = f"switch-{clock.utc_stamp().replace(' ', 'T').replace(':', '')}"
    with ledger.run(db, "menu_shared", slot, ledger.SYSTEM, clock=clock) as handle:
        try:
            got = build_now(app, db, clock=clock, on_refusal=handle.note, tell=False)
        except NothingToBuild as e:
            handle.skip(str(e))
            return
        except (ModelError, TiffinError) as e:
            handle.degrade(f"shared weekend could not be built: {str(e)[:200]}")
            if shared.mode(db) == weekend.TOGETHER:
                fall_back(app, db, shared.weekend_of(clock.today_local()), clock=clock, why=str(e))
            return
        if got.evenest is not None:
            handle.degrade(f"kept the evenest attempt: portions {got.evenest:.1f}x apart")
        else:
            handle.note("shared weekend built for the switch")


JOB = Job(
    name="menu_shared",
    scope="joint",
    kind="message",
    times=fixed("12:00", "18:00"),
    run=run,
    catch_up="+5h",
    # Saturday too: a weekend that reached its first morning with no shared menu (a failed
    # Friday) is built on the day, from today (`menu.open_days` lets an empty today in)
    days=frozenset({THURSDAY, FRIDAY, SATURDAY}),
)

__all__ = ["JOB", "Outcome", "build_now", "build_quietly", "cook_together", "fall_back", "run"]
