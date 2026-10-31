"""Prepare each person's weekly review and the household summary.

The review is kept for the page even when the Telegram message is shortened.
Open days remain actionable, and each person sees only their own body data."""

from __future__ import annotations

import datetime as dt
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass

from .. import days as days_mod
from .. import targets
from ..advisor import facts as facts_mod
from ..advisor import prose
from ..clock import isoday, local_at, week_start
from ..config import Config
from ..db import Db
from ..food import weekplan
from ..persons import Person, PersonRegistry
from ..scheduler import Job, JobContext, fixed
from ..telegram import render as r
from ..telegram.api import inline_keyboard

SUNDAY = frozenset({6})

MAX_LINES = 10

#: closes the week with somewhere to go, rather than a dead end.
#: Both commands always exist - unlike a specific day or menu id, which might not - so this
#: needs no lookup and cannot itself go stale.
ROUTE_LINE = "Next: /menu for the week ahead."

_USABLE_DAYS_RE = re.compile(r"^usable_days=(\d+) < (\d+)$")


def _human_skip_reason(reason: str) -> str:
    """Turn `energy.Skip`'s machine-shaped reason into a sentence a person can act on.

    `cal.reason` is a debugging string built for `run_ledger`, not a phone screen: sent
    verbatim (and HTML-escaped on top) it read "usable_days=0 &lt; 5" on 13 Sep - a variable
    name and an escaped operator, not an explanation (integrations_later/03).
    """
    m = _USABLE_DAYS_RE.match(reason)
    if m:
        have, need = m.group(1), m.group(2)
        return f"only {have} of the last {need} days had a confirmed log to measure from"
    return reason  # already words, e.g. "no weight trend for the week"


@dataclass(frozen=True)
class _Again:
    """What `block_for` reads off its JobContext - `app.config`, `db` and `day` - so a week's
    review can be written again outside the Sunday run (the shape `evening_confirm._Fields`
    has, for the same reason)."""

    config: Config
    db: Db
    day: dt.date

    @property
    def app(self) -> _Again:
        return self


def block_for(
    ctx: JobContext | _Again,
    person: Person,
    as_of: dt.date | None = None,
    *,
    check_in_tonight: bool = False,
) -> tuple[list[str], list[str], facts_mod.Facts]:
    """(core, extra, facts): core is the facts nobody's week review can do without (and, when
    it happened, why recalibration didn't); extra is the rules engine's optional nudges, which
    `run()` is free to trim first when the route-onward line needs the room.
    `as_of` is the day the review is read from, the Sunday unless it has ended.

    `check_in_tonight` is a weekly rhythm's review on the Sunday, before the one check-in that
    asks about the week. "6 not answered" and "No recalibration this week" were said to somebody
    who answers every week, every week, because the review and the 17:30 calibration both come
    before their only ask: the days are still to be asked, and once they
    are, `weekly_calibration_late` measures the week overnight.
    """
    cfg = ctx.app.config
    f = facts_mod.compute(ctx.db, person.id, ctx.day, cfg, as_of=as_of or ctx.day)
    confidence = ctx.db.state_get(f"sunday_confidence:{person.id}") or None
    next_row = targets.targets_for(ctx.db, person.id, f.week_end + dt.timedelta(days=1))
    if next_row is None:
        next_row = targets.latest_targets(ctx.db, person.id, f.week_end)
    next_targets = targets.macros_of(next_row) if next_row is not None else None
    core = prose.summary_lines(
        f, confidence=confidence, next_targets=next_targets, check_in_tonight=check_in_tonight
    )
    skipped = ctx.db.state_get(f"sunday_skip:{person.id}")
    if skipped:
        # say when recalibration did not happen. Without this a week that skipped looked
        # identical to one that ran, and the trust line actively misdescribed it.
        try:
            info = json.loads(skipped)
            if info.get("week_start") == isoday(f.week_start):
                raw = str(info.get("reason", "not enough data"))
                few = _USABLE_DAYS_RE.match(raw)
                if check_in_tonight and few:
                    core.append(
                        f"Tonight's check-in answers the week; with {few.group(2)} days "
                        "answered it is measured again overnight."
                    )
                else:
                    core.append(f"No recalibration this week: {_human_skip_reason(raw)}.")
        except ValueError:
            pass
    extra: list[str] = []
    raw = ctx.db.state_get(f"sunday_notes:{person.id}")
    if raw:
        try:
            notes = json.loads(raw)
            if notes.get("week_start") == isoday(f.week_start):
                extra.extend(str(x) for x in notes.get("lines", [])[:2])
        except ValueError:
            pass
    return core, extra, f


WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def _said(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + f" and {names[-1]}"


def week_open(db: Db, person_id: int, until: dt.date) -> list[dt.date]:
    """The days the week's message asks about: open, with a menu, from the Monday of the week to
    the day before `until`.

    `until` is the Sunday when the message goes out on Sunday - tonight's check-in asks about
    Sunday itself - and the Monday when it is caught up on Monday morning, when Sunday has ended
    unasked. Run late with the Sunday left out, the summary named Mon-Sat and Monday's check-in
    stood down for it, so Sunday was asked in neither. The tap finds its days again with this,
    from the same `until`, and never reaches back past the Monday into the week before.
    """
    start = week_start(until - dt.timedelta(days=1))
    return [d for d in days_mod.unanswered_days(db, person_id, until, back=7) if d >= start]


def gaps_line(still_open: list[dt.date], no_menu: Iterable[dt.date] = ()) -> str:
    """Describe unanswered days and offer a single tap to confirm the planned ones."""
    names = [WEEKDAYS[d.weekday()] for d in still_open]
    one = len(names) == 1
    line = (
        f"Still open from the week: {_said(names)}. If {'it' if one else 'they'} went as "
        f"planned, one tap records {'it' if one else 'them'}; if not, tell me the day and what "
        f"changed (e.g. '{DAY_NAMES[still_open[0].weekday()]}: kebab instead of dinner')."
    )
    rest = [WEEKDAYS[d.weekday()] for d in no_menu]
    if rest:
        it, it_is = ("it", "it is") if len(rest) == 1 else ("them", "they are")
        line += (
            f" {_said(rest)} had no menu, so the tap leaves {it} out and {it_is} not counted "
            "this week."
        )
    return line


def _tap_label(still_open: list[dt.date]) -> str:
    """ "Fri as planned" for one day - "All 1 as planned" read as a mistake - else "All 5 as
    planned"."""
    if len(still_open) == 1:
        return f"{WEEKDAYS[still_open[0].weekday()]} as planned"
    return f"All {len(still_open)} as planned"


def _check_in_tonight(ctx: JobContext, person_id: int) -> bool:
    """Is this person on the weekly rhythm, whose week is asked in its own Sunday check-in - and
    is that check-in still to come tonight?

    Not on a paused Sunday, when the scheduler sends no check-in, nor once its time has passed
    or the Sunday has ended: the review said "tonight's check-in asks about the other 6" and
    promised a measurement overnight to somebody no check-in was coming to.
    """
    profile = targets.profile_on(ctx.db, person_id, ctx.day)
    if profile is None or str(profile["rhythm"] or "light") != "weekly":
        return False
    if ctx.clock.today_local() != ctx.day or targets.pause_active(ctx.db, person_id, ctx.day):
        return False
    due = local_at(ctx.day, str(profile["confirm_time"] or "20:30"), ctx.clock.timezone)
    return due is not None and ctx.clock.now_local() < due


def _reviewed(db: Db, registry: PersonRegistry, day: dt.date) -> list[Person]:
    """Who gets a review. A person with no profile has not onboarded: sending them a weekly
    review is noise, and the joint block would hand them the other person's body weight
    before they had agreed to anything at all."""
    return [
        p
        for p in registry.active()
        if p.chat_id is not None and targets.profile_on(db, p.id, day) is not None
    ]


Block = tuple[list[str], list[str], facts_mod.Facts]


def _partner_of(p: Person, persons: list[Person], blocks: dict[int, Block]) -> list[str]:
    """The other person's trend and sessions only."""
    partner: list[str] = []
    for other in persons:
        if other.id == p.id:
            continue
        _, _, of = blocks[other.id]
        partner.extend(prose.partner_lines(of, other.display_name))
    return partner


def _full(day: dt.date, core: list[str], extra: list[str], partner: list[str]) -> str:
    """The whole review, as the page keeps it."""
    # the route-onward and the partner's own trend/sessions are never the thing trimmed for
    # space - only the rules engine's optional nudges are:
    # a review that lost her one line about him to make room for a /menu link would have
    # fixed one dead end by opening another
    header = f"<b>Week to {day:%d %b}</b>"
    fixed_count = 1 + len(core) + len(partner) + 1  # header + core + partner + route
    budget = max(MAX_LINES - fixed_count, 0)
    body_lines = [*core, *extra[:budget], *partner]
    kept = [header, *(r.esc(x) for x in body_lines)][: MAX_LINES - 1]
    return r.check_text("\n".join([*kept, ROUTE_LINE]), kind="sunday_summary")


def refresh_review(
    db: Db,
    config: Config,
    registry: PersonRegistry,
    person: Person,
    answered: Iterable[dt.date],
) -> None:
    """Write the kept review again once days it counted as not answered have been answered.

    The review is written at 19:00 on Sunday, before the message that asks for the week's open
    days. Answered from that message's button, or the page's, the review the message links to
    still said "Days recorded: 0, 6 not answered" while the reply had just said all five were
    recorded. Only the person's latest review is written again - an older week stays as it was
    read - and only when what it says has changed (a ledger: the newer row is the one in force).
    """
    for sunday in sorted({week_start(d) + dt.timedelta(days=6) for d in answered}):
        latest = db.one(
            "SELECT week_end, full_html FROM weekly_reviews WHERE person_id = ? "
            "ORDER BY week_end DESC, id DESC LIMIT 1",
            (person.id,),
        )
        if latest is None or str(latest["week_end"]) != isoday(sunday):
            continue
        persons = _reviewed(db, registry, sunday)
        if all(p.id != person.id for p in persons):
            continue
        again = _Again(config, db, sunday)
        blocks = {p.id: block_for(again, p) for p in persons}
        core, extra, _ = blocks[person.id]
        full = _full(sunday, core, extra, _partner_of(person, persons, blocks))
        if full != str(latest["full_html"]):
            db.insert(
                "weekly_reviews", person_id=person.id, week_end=isoday(sunday), full_html=full
            )


def run(ctx: JobContext) -> None:
    persons = _reviewed(ctx.db, ctx.app.registry(ctx.db), ctx.day)
    if not persons:
        ctx.handle.skip("nobody has onboarded yet")
        return
    # the day the ask actually goes out: run late, as Monday morning's catch-up, it is Monday's
    # one ask, and Monday's check-in must not make it a second time - so it asks about the
    # Sunday as well, which has ended by then and which that check-in would have asked about
    asked_on = ctx.clock.today_local()
    today = isoday(asked_on)
    until = ctx.day if asked_on <= ctx.day else ctx.day + dt.timedelta(days=1)
    # and the review counts from there too: read as of Sunday, an ended Sunday that day_close
    # had not yet written (a restart puts both in one tick) was not counted, and one message
    # said "6 not answered" above a question naming seven
    # a weekly rhythm's week is asked in its own check-in, tonight, after this is read
    weekly = {p.id for p in persons if _check_in_tonight(ctx, p.id)}
    blocks: dict[int, Block] = {
        p.id: block_for(ctx, p, as_of=until, check_in_tonight=p.id in weekly) for p in persons
    }
    sent = 0
    for p in persons:
        core, extra, facts = blocks[p.id]
        partner = _partner_of(p, persons, blocks)
        header = f"<b>Week to {ctx.day:%d %b}</b>"
        full = _full(ctx.day, core, extra, partner)
        # the review is kept for the page every week, whether or not a message carries it
        ctx.db.insert("weekly_reviews", person_id=p.id, week_end=isoday(ctx.day), full_html=full)
        if weekplan.of(ctx.db, p.id).quiet:
            continue
        # the week's gaps: the days still open, offered back once (a weekly rhythm answers the
        # week in its own Sunday check-in, so it is not asked twice - unless none is coming)
        still_open = [] if p.id in weekly else week_open(ctx.db, p.id, until)
        # a check-in at or before 19:00 has offered these days today already: one ask a day.
        # Its own key: `catchup_asked` is this message's, and says the check-in had no row
        asked = bool(still_open) and ctx.db.state_get(f"catchup_evening:{p.id}") == today
        if asked:
            still_open = []
        rows: list[list[tuple[str, str]]] = []
        gap = ""
        if still_open:
            gap = gaps_line(still_open)
            # `until`, not the last open day: the tap finds the days this message named again
            # (`week_open`). Keyed on the last open day, the tap's week reached back to the
            # Sunday before, which nobody had been asked about, and recorded it at the whole plan.
            rows = [[(_tap_label(still_open), f"k:cfmall:{until:%Y%m%d}:week")]]
        link = r.summary_link(ctx.app.config) if p.tailscale_login else ""
        if link:
            if not still_open:
                # nothing to answer: the review is on the Summary tab, and the week's message
                # is retired in its favour
                ctx.handle.note(f"{p.slug}: nothing open, review on the page")
                continue
            # headline and the week's gaps; the whole review is on the Summary tab
            html = r.check_text(
                "\n".join([header, *(r.esc(x) for x in core[:1]), r.esc(gap), f"{link}."]),
                kind="sunday_summary",
            )
        elif gap:
            # the whole review, so the question also names the days with no menu that its
            # "N not answered" counts
            no_menu = [
                d.day
                for d in facts.day_intakes
                if d.status == "unconfirmed"
                and d.day not in still_open
                and days_mod.planned_macros(ctx.db, p.id, d.day) is None
            ]
            gap = gaps_line(still_open, no_menu)
            # the question's room comes out of the optional nudges, as the route's does: the
            # partner's line, the question and the route are never what is cut (header,
            # question and route are the 3), and when the nudges are not enough the end of
            # the person's own block gives way. Cutting from the end of the whole body took the
            # partner's line first and kept both nudges.
            own = [*core, *extra][: max(MAX_LINES - 3 - len(partner), 0)]
            body = [header, *(r.esc(x) for x in [*own, *partner]), r.esc(gap), ROUTE_LINE]
            html = r.check_text("\n".join(body), kind="sunday_summary")
        else:
            html = full
        result = ctx.outbox.send(
            p,
            "sunday_summary",
            isoday(ctx.day),
            html,
            inline_keyboard(rows) if rows else None,
            payload={"full_html": full},
        )
        if result.status == "failed":
            ctx.handle.degrade(f"{p.slug}: {result.error}")
        elif result.sent:
            sent += 1
            if still_open:
                # the evening check-in tonight does not ask about them again (one prompt a day).
                # Only once the question has gone: set before the send, a summary that failed
                # silenced the evening's catch-up too, and the days were asked in neither.
                ctx.db.state_set(f"catchup_asked:{p.id}", today)
    ctx.handle.note(f"sent {sent}/{len(persons)}")


JOB = Job(
    name="sunday_summary",
    scope="joint",
    kind="message",
    times=fixed("19:00"),
    run=run,
    catch_up="12:00",
    days=SUNDAY,
    paused_ok=True,
    needs_profile=False,
)
