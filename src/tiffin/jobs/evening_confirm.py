"""evening_confirm at confirm_time: ate as planned? session confirm; anything off plan?"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

from .. import body, days
from ..clock import isoday, parse_day
from ..db import Db
from ..food import weekplan
from ..scheduler import Job, JobContext, daily
from ..targets import pause_active
from ..telegram import render as r
from ..telegram.api import inline_keyboard

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
SCALE_GAP_DAYS = 3
SCALE_ASKED_KEY = "scale_nudge"


@dataclass(frozen=True)
class _Fields:
    """The three fields session_part reads, so its body did not have to change."""

    db: Db
    person_id: int
    day: dt.date


SUNDAY = 6


def day_key(day) -> str:
    return day.strftime("%Y%m%d")


def rhythm_of(profile: Any) -> str:
    """daily, light or weekly (migration 0035); light for a profile from before it existed."""
    if profile is None or "rhythm" not in profile.keys():  # noqa: SIM118 (sqlite3.Row)
        return "light"
    return str(profile["rhythm"] or "light")


def day_label(day: dt.date) -> str:
    return f"{WEEKDAYS[day.weekday()]} {day.day} {day.strftime('%b')}"


def catchup_line(still_open: list[dt.date], *, also: bool = True) -> str:
    """ "Also still open: Mon, Tue, Thu." - the earlier days one tap can answer with today.

    Without `also`, today is already answered and the tap answers only these."""
    if not still_open:
        return ""
    names = ", ".join(WEEKDAYS[d.weekday()] for d in still_open)
    were = "Was it" if len(still_open) == 1 else "Were they"
    if not also:
        return f"Still open: {names}. {were} as planned?"
    return f"Also still open: {names}. {were} as planned too?"


def _names(ds: list[dt.date]) -> str:
    names = [WEEKDAYS[d.weekday()] for d in ds]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + f" and {names[-1]}"


def no_menu_line(db: Db, person_id: int, no_menu: list[dt.date]) -> str:
    """ "Sat had no menu; the target was 2,200 kcal. As the target?" - asked the way the daily
    check-in asks a day with no menu, and recorded at the target the same way (#61)."""
    target = days.target_macros(db, person_id, no_menu[0]) if no_menu else None
    if target is None:
        return ""
    return f"{_names(no_menu)} had no menu; the target was {target.kcal:,} kcal. As the target?"


def target_label(no_menu: list[dt.date]) -> str:
    if len(no_menu) == 1:
        return f"{WEEKDAYS[no_menu[0].weekday()]} at the target"
    return f"All {len(no_menu)} at the target"


def week_sessions(
    db: Db, person_id: int, week: list[dt.date]
) -> tuple[str, list[list[tuple[str, str]]], list[dt.date]]:
    """(line, rows, days) for the planned sessions of `week` that nobody has recorded.

    A session is recorded only by the evening tap, and a weekly rhythm withholds six evenings
    of seven: Monday to Saturday's sessions could never be answered at all, and without an
    intervals.icu key nothing else records them. The Sunday check-in asks
    them by day, on the same `sessm` buttons the daily check-in uses. A session intervals.icu
    has already matched is counted as done, so it is not asked.
    """
    named: list[str] = []
    rows: list[list[tuple[str, str]]] = []
    asked: list[dt.date] = []
    for d in week:
        if pause_active(db, person_id, d) or days.session_decided(db, person_id, d) is not None:
            continue
        if days.matched_session(db, person_id, d) is not None:
            continue
        planned = [
            p for p in days.planned_sessions_on(db, person_id, d) if str(p["kind"]) != "rest"
        ]
        if not planned:
            continue
        wd = WEEKDAYS[d.weekday()]
        what = " then ".join(f"{r.esc(p['kind'])} {int(p['target_minutes'])} min" for p in planned)
        named.append(f"{wd} {what}")
        k = day_key(d)
        rows.append(
            [(f"{wd}: did it", f"k:sessm:{k}:done"), (f"{wd}: skipped", f"k:sessm:{k}:skip")]
        )
        asked.append(d)
    if not named:
        return "", [], []
    return f"Sessions to record: {', '.join(named)}.", rows, asked


def week_rows(db: Db, person_id: int, day: dt.date, asked: dict) -> list[list[tuple[str, str]]]:
    """A weekly check-in's other questions, each kept on the message until it is answered: the
    days with no menu and the week's sessions it asked about (`asked` is its payload). The
    keyboard is rebuilt from this after every tap on it (dayflow._keep_session_row)."""
    named = {str(x) for x in asked.get("no_menu") or []}
    no_menu = (
        [d for d in days.unanswered_without_menu(db, person_id, day) if isoday(d) in named]
        if named
        else []
    )
    rows = [[(target_label(no_menu), f"k:cfmall:{day_key(day)}:target")]] if no_menu else []
    sessions = [parse_day(str(x)) for x in asked.get("sessions") or []]
    return rows + week_sessions(db, person_id, sessions)[1]


def planned_line(ctx: JobContext) -> tuple[str, str]:
    """(line, basis): what the day was meant to be, planned and target named apart.

    The morning said "Target 1914 kcal" and the evening "Planned: 1893 kcal", with nothing saying
    up to; the target is what the week asks for. Both, once, in words.
    """
    planned = days.planned_macros(ctx.db, ctx.person_id, ctx.day)
    target = days.target_macros(ctx.db, ctx.person_id, ctx.day)
    if planned is not None:
        vs = (
            f" (target {target.kcal:,})"
            if target is not None and target.kcal != planned.kcal
            else ""
        )
        return (f"The menu planned {planned.kcal:,} kcal{vs}.", "menu")
    if target is not None:
        return (f"No menu today; the target was {target.kcal:,} kcal.", "none")
    return ("No targets yet.", "none")


def session_part(
    db: Db, person_id: int, day: dt.date, *, has_icu_key: bool = True
) -> tuple[str, list[tuple[str, str]]]:
    """(line, buttons) for the day's training.

    Takes the three things it uses rather than a JobContext, so the handler that answers the
    food question can rebuild this row instead of wiping it off the same message.
    `has_icu_key` defaults True: a matched session can only exist when a sync actually ran,
    so the caller only needs to pass False for the one person who has no key at all
    (integrations_later/01) - "nothing from intervals.icu yet" told her to wait on a sync
    that will never happen.
    """
    ctx = _Fields(db, person_id, day)
    if days.session_decided(ctx.db, ctx.person_id, ctx.day) is not None:
        return ("Session already recorded.", [])
    matched = days.matched_session(ctx.db, ctx.person_id, ctx.day)
    if matched is not None:
        planned_kind = matched["planned_kind"] or "session"
        mins = matched["minutes"]
        got = f"{mins} min" if mins else "an activity"
        # "Garmin" is a system this box does not talk to: Garmin's personal route broke in
        # March 2026 and training arrives through intervals.icu. Someone looking at a ride
        # sitting in their Garmin app and reading "nothing from Garmin yet" would conclude
        # Tiffin was broken, when what is actually late is the icu sync.
        return (
            f"Session: {planned_kind} {int(matched['target_minutes'] or 0)} min planned; "
            f"intervals.icu shows {got}. Count it?",
            [("Confirm", f"k:sess:{matched['id']}"), ("Not this", f"k:sessx:{matched['id']}")],
        )
    planned = [
        row
        for row in days.planned_sessions_on(ctx.db, ctx.person_id, ctx.day)
        if str(row["kind"]) != "rest"
    ]
    if not planned:
        # nothing planned is nothing to ask: "Rest day." on every evening of someone with no
        # training plan was a line of noise in the one message that has to stay short
        return ("", [])
    k = day_key(ctx.day)
    named = " then ".join(f"{p['kind']} {int(p['target_minutes'])} min" for p in planned)
    tail = "nothing from intervals.icu yet." if has_icu_key else "did it happen?"
    return (
        f"Planned {named}; {tail}",
        [("Did it", f"k:sessm:{k}:done"), ("Skipped", f"k:sessm:{k}:skip")],
    )


def scale_line(db: Db, person_id: int, day: dt.date) -> str:
    """After three days off the scale, ask once per gap between weigh-ins.

    The scale stays the source, so this asks for a weigh-in and never for a number. "Once"
    is per gap, not per evening: the key records the last weigh-in this nudge was sent
    about, so a second nudge waits until they have weighed in again and stopped again.
    Someone who has never weighed in is asked once, and again after their first.
    """
    since = body.days_since_weigh_in(db, person_id, day)
    if since is not None and since < SCALE_GAP_DAYS:
        return ""
    key = f"{SCALE_ASKED_KEY}:{person_id}"
    row = db.one("SELECT v FROM bot_state WHERE k = ?", (key,))
    marker = "never" if since is None else isoday(day - dt.timedelta(days=since))
    if row is not None and str(row["v"]) == marker:
        return ""
    db.execute(
        "INSERT INTO bot_state(k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v",
        (key, marker),
    )
    if since is None:
        return "No weigh-in recorded yet. Step on the scale in the morning."
    return f"No weigh-in for {since} days. Step on the scale in the morning."


def freezer_line(db: Db, person_id: int, day: dt.date, profile: Any) -> str:
    """Tonight is the only evening moving a box out of the freezer helps tomorrow's meal.

    `/batch`'s Freeze line is read once, on the cook day - by the day it matters, whoever read
    it has forgotten which days it named. This recomputes the SAME Load `/batch` built, from
    the same historical cook day (the day before the segment started, not tonight), and asks
    only whether tomorrow needs a freezer reminder.
    """
    from ..food import batch as batch_mod  # noqa: PLC0415 (jobs import the food layer late)

    try:
        freezes = bool(profile["freezes"])
    except KeyError, IndexError, TypeError:
        freezes = False
    if not freezes:
        return ""
    from ..food import planning  # noqa: PLC0415 (jobs import the food layer late)

    slots = planning.batched(profile)
    if not slots:
        return ""
    tomorrow = day + dt.timedelta(days=1)
    menu = batch_mod.menu_to_batch(db, person_id, tomorrow)
    if menu is None:
        return ""
    start = parse_day(str(menu["segment_start"]))
    load = batch_mod.plan(db, menu, slots, start - dt.timedelta(days=1), freezes=freezes)
    names = sorted({d.name for d, late in load.to_freeze if tomorrow in late and d.freezable})
    if not names:
        return ""
    return f"Move {', '.join(names)} from the freezer to the fridge tonight for tomorrow."


def _withhold(ctx: JobContext, skip: str, why: str) -> None:
    """Leave tonight's check-in unsent - but never the freezer line, which helps only tonight.

    `freezer_line` lived inside the check-in alone, so on every evening the check-in was
    withheld - six of a weekly rhythm's seven, a day already answered - Thursday's frozen boxes
    were still frozen on Thursday and nothing said so. It goes on its own
    then, with no question and no buttons.
    """
    person = ctx.person
    assert person is not None
    freezer = freezer_line(ctx.db, ctx.person_id, ctx.day, ctx.profile)
    if not freezer:
        ctx.handle.skip(skip)
        return
    html = r.check_text(f"<b>{day_label(ctx.day)}</b>: {r.esc(freezer)}", kind="evening_confirm")
    result = ctx.outbox.send(
        person,
        "evening_confirm",
        isoday(ctx.day),
        html,
        None,
        # it asked nothing, so there is no check-in to answer
        payload={"freezer_only": True},
    )
    if result.status == "failed":
        ctx.handle.degrade(f"send failed: {result.error}")
    else:
        ctx.handle.note(f"{result.status}: the freezer line alone ({why})")


def run(ctx: JobContext) -> None:
    person = ctx.person
    if person is None or ctx.profile is None:
        ctx.handle.skip("no profile")
        return
    if weekplan.of(ctx.db, person.id).quiet:
        _withhold(ctx, "messages only when needed: no evening check-in", "quiet")
        return
    weekly = rhythm_of(ctx.profile) == "weekly"
    if weekly and ctx.day.weekday() != SUNDAY:
        # A weekly rhythm asks once on Sunday, when the person can answer for the whole week.
        _withhold(ctx, "weekly rhythm: the check-in is on Sunday", "weekly rhythm")
        return
    latest = days.latest_confirmation(ctx.db, person.id, ctx.day)
    still_open = days.unanswered_days(ctx.db, person.id, ctx.day)
    if ctx.db.state_get(f"catchup_asked:{person.id}") == isoday(ctx.day):
        # the Sunday summary asked about them a few hours ago: not twice in one evening
        still_open = []
    # and a weekly rhythm's one check-in is the only ask its week gets: a day with no menu and
    # a session nobody recorded are asked here, or never
    no_menu = days.unanswered_without_menu(ctx.db, person.id, ctx.day) if weekly else []
    week = [ctx.day - dt.timedelta(days=i) for i in range(6, 0, -1)] if weekly else []
    sessions_line, _rows, session_days = week_sessions(ctx.db, person.id, week)
    # day_close writes its own "unconfirmed" row; reading the row's existence as an answer
    # meant a closed day was never asked about again
    answered = latest is not None and str(latest["status"]) == "confirmed"
    has_icu_key = person is not None and bool(ctx.app.secrets.icu_key(person.slug))
    session_line, session_buttons = session_part(
        ctx.db, ctx.person_id, ctx.day, has_icu_key=has_icu_key
    )
    # and today's own session, which nothing but this message asks: a day recorded early
    # (/confirm, the page) withheld it with the food, and for a weekly rhythm the Sunday
    # session was never recordable at all
    if answered and not (still_open or no_menu or session_days or session_buttons):
        _withhold(ctx, "already confirmed", "already answered")
        return
    k = day_key(ctx.day)
    freezer = freezer_line(ctx.db, ctx.person_id, ctx.day, ctx.profile)
    payload = {
        # the days this message asked about: answering today keeps their buttons on it, and
        # only on it (dayflow._keep_session_row)
        "still_open": [isoday(d) for d in still_open],
        "no_menu": [isoday(d) for d in no_menu],
        "sessions": [isoday(d) for d in session_days],
    }
    rows: list[list[tuple[str, str]]] = []
    if answered:
        # today was answered before the check-in (/confirm, the page) and the days before it
        # were not: returning here as "already answered" left them unasked, and for a weekly
        # rhythm that was the whole week, aged out by the next Sunday
        lines = [
            f"<b>{day_label(ctx.day)}</b> is recorded.",
            catchup_line(still_open, also=False),
            # the answer names its day, which the check-in's own flow reads (dayflow.offplan_text)
            f"Off plan on one of them? Tell me the day and what changed (e.g. "
            f"'{still_open[0]:%A}: kebab instead of dinner')."
            if still_open
            else "",
            no_menu_line(ctx.db, person.id, no_menu),
            r.esc(session_line) if session_buttons else "",
            sessions_line,
            r.esc(freezer),
        ]
        html = r.check_text("\n".join(x for x in lines if x), kind="evening_confirm")
        if still_open:
            label = (
                f"{WEEKDAYS[still_open[0].weekday()]} as planned"
                if len(still_open) == 1
                else f"All {len(still_open)} earlier days as planned"
            )
            rows.append([(label, f"k:cfmall:{k}:before")])
        if session_buttons:
            rows.append(session_buttons)
    else:
        line, _basis = planned_line(ctx)
        # the scale reminder moved to the page with the rest of the detail:
        # the check-in carries the day's question, tonight's training question, and the freezer
        # line, which only helps if it is read tonight
        context = {
            "day_label": day_label(ctx.day),
            "planned_line": line,
            "session_line": "\n".join(x for x in (session_line, sessions_line) if x),
            "offplan_line": "Off plan? Type what you ate and I will ask what it replaced.",
            "freezer_line": freezer,
            "catchup_line": "\n".join(
                x for x in (catchup_line(still_open), no_menu_line(ctx.db, person.id, no_menu)) if x
            ),
        }
        rows = [[("As planned", f"k:cfm:{k}"), ("Not quite", f"k:cfmx:{k}")]]
        if still_open:
            rows.insert(0, [(f"All {len(still_open) + 1} days as planned", f"k:cfmall:{k}")])
        if session_buttons:
            rows.append(session_buttons)
        html = r.render("evening_confirm", context, ctx.tone)
    rows += week_rows(ctx.db, person.id, ctx.day, payload)
    result = ctx.outbox.send(
        person,
        "evening_confirm",
        isoday(ctx.day),
        html,
        inline_keyboard(rows),
        payload=payload,
    )
    if result.status == "failed":
        ctx.handle.degrade(f"send failed: {result.error}")
    else:
        ctx.handle.note(result.status)
        if result.status == "sent":
            from ..telegram.dayflow import arm_evening  # noqa: PLC0415 (telegram imports jobs)

            arm_evening(ctx.db, person.id, ctx.day, clock=ctx.clock)
            if still_open:
                # asked: a Sunday summary after an early check-in leaves them out (one ask a day)
                ctx.db.state_set(f"catchup_evening:{person.id}", isoday(ctx.clock.today_local()))


JOB = Job(
    name="evening_confirm",
    scope="each",
    kind="message",
    times=daily("confirm_time", "20:30"),
    run=run,
    catch_up="23:30",
    # A check-in at 23:00 with quiet from 22:30 was deferred to quiet_end, which for an
    # evening message means the next morning, past its own 23:30 deadline: the check-in
    # never arrived at all, every day landed unconfirmed and the calibration never got its
    # five usable days. confirm_time is by definition a time the person chose.
    quiet="send",
    default_answers=(("conf", "20:30 for check-ins"), ("rhy", "weekly message rhythm")),
)
