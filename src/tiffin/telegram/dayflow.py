"""Daily taps and commands: confirm the day, confirm the session, pause, today's status.

Callbacks (k:<kind>:...): cfm/cfmx <yyyymmdd>, sess/sessx <match_id>, sessm <yyyymmdd>:done|skip.
Commands: /today, /pause <days|YYYY-MM-DD>, /resume, /confirm [YYYY-MM-DD].
Every handler is idempotent: an existing decision answers "already recorded".
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import re
from typing import TYPE_CHECKING

from .. import body, days, targets
from ..clock import DEFAULT_CLOCK, Clock, isoday, parse_day, parse_stamp
from ..config import Config
from ..errors import TelegramError
from ..food import menu as menu_mod
from . import convstate
from .api import Keyboard, inline_keyboard
from .render import check_text, esc, kg, macros_line, signed_kg

if TYPE_CHECKING:
    from ..persons import Person
    from .handlers import Handlers
    from .poller import Inbound

OFFPLAN_FLOW = "offplan"
#: before this hour, a bare /confirm is about yesterday. Somebody answering the evening check-in
#: after midnight is answering it about the evening, and `day_close` at 00:05 agrees.
BEFORE_DAWN_H = 5
#: "What changed?" stops listening after this long. It is asked in the evening and answered
#: in the evening; still armed at breakfast it catches a message about a different day.
OFFPLAN_TTL = 8 * 3600
#: how far back a button carrying a date may still record that day
BACKDATE_DAYS = 7


def _day_from_key(key: str) -> dt.date | None:
    try:
        return dt.datetime.strptime(key, "%Y%m%d").replace(tzinfo=dt.UTC).date()
    except ValueError:
        return None


#: how the evening's "what changed?" answer names the planned meal it replaced. Deliberately
#: the same small, closed vocabulary `menuflow._MEALS` uses for the same job elsewhere - this is
#: recognising a slot name, not guessing intent, so a short list is the right size for it.
_SLOT_WORDS = {
    "breakfast": "breakfast",
    "lunch": "lunch",
    "dinner": "dinner",
    "tea": "dinner",
    "supper": "dinner",
    "snack": "snack",
    "pre-bed": "pre_bed",
    "prebed": "pre_bed",
    "pre_bed": "pre_bed",
    "bedtime": "pre_bed",
}
_SLOT_LABELS = {
    "breakfast": "Breakfast",
    "lunch": "Lunch",
    "dinner": "Dinner",
    "snack": "Snack",
    "pre_bed": "Pre-bed",
}


def _named_slots(text: str) -> list[str]:
    """Every planned slot the answer names, in menu order - a starting guess, never the answer.

    "I didn't have pre-bed and breakfast, had 2 Kinder and 2 Twix instead" names two, and the
    old reader took exactly one or none: two named meals meant a question with one answer, so
    only one of them could ever come off the day and the other stayed counted as eaten.
    The names are now only the pre-ticked boxes on a question the person
    confirms with one tap.
    """
    found = {
        _SLOT_WORDS[w]
        for w in re.findall(r"[a-z]+(?:-[a-z]+)?", text.casefold())
        if w in _SLOT_WORDS
    }
    return [s for s in menu_mod.SLOT_ORDER if s in found]


#: each weekday as an answer may name it, Monday first: the whole name, and the short forms the
#: Sunday message lists the open days in ("Still open from the week: Mon, Fri") - an answer that
#: mirrors it, "Fri: pizza instead of dinner", was taken as Sunday's food and Friday stayed open
_DAY_WORDS = (
    ("monday", "mon"),
    ("tuesday", "tue", "tues"),
    ("wednesday", "wed", "weds"),
    ("thursday", "thu", "thur", "thurs"),
    ("friday", "fri"),
    ("saturday",),
    ("sunday",),
)
#: "sat on the sofa" and "in the sun" are not days: these two only where they head the answer
#: ("Sun-dried tomato pasta" is not Sunday either)
_HEADED_DAY = re.compile(r"^\W*(sat|sun)\s*(?::|,|-(?!\w))|\b(sat|sun)\s*:")
#: where a word in an answer names its day: heading it, after on/for/last, or before a colon
_DAY_PLACE = re.compile(r"\b(?:on|for|last)\s+([a-z]+)\b|\b([a-z]+)\s*:")


def _days_named(text: str, today: dt.date) -> set[int]:
    """The weekdays (Monday 0) an answer names - "Wednesday: kebab instead of dinner"."""
    folded = text.casefold().replace("\u2019", "'")
    # a day inside a food is not the day it was eaten: "Leftover Sunday roast instead of lunch",
    # "Friday's curry"
    first = re.match(r"\W*([a-z]+)(?!')", folded)
    words = {w for pair in _DAY_PLACE.findall(folded) for w in pair if w}
    if first:
        words.add(first.group(1))
    named = {i for i, forms in enumerate(_DAY_WORDS) if words.intersection(forms)}
    named |= {5 if "sat" in m else 6 for m in _HEADED_DAY.findall(folded)}
    if "yesterday" in words:
        named.add((today - dt.timedelta(days=1)).weekday())
    return named


def _names_another_day(text: str, day: dt.date, today: dt.date) -> bool:
    """Does this answer name a day other than `day`?"""
    return any(i != day.weekday() for i in _days_named(text, today))


#: an answer that says its day went to plan - "Tuesday was as planned", "yesterday was fine" -
#: with nothing taking it back ("fine, but a pizza instead of dinner", "not as planned")
_AS_PLANNED = re.compile(r"\b(same|fine|ok|okay|as planned|on plan|to plan|close enough)\b")
_QUALIFIED = re.compile(r"\b(no|not|never|but|except|apart)\b|n['\u2019]t\b")
#: and one that says what was eaten: the words food is told in, a meal it names, or the day
#: heading the answer the way the check-in asks for it ("Saturday: pizza and two beers")
_FOOD_SAID = re.compile(r"\b(had|ate|eaten|instead|extra|skipped|drank)\b")
_DAY_HEADED = re.compile(r"^\W*([a-z]+)\s*[:-]")
_DAY_HEADS = {w for forms in _DAY_WORDS for w in forms} | {"sat", "sun", "yesterday"}


def _says_as_planned(text: str) -> bool:
    folded = text.casefold()
    return bool(_AS_PLANNED.search(folded)) and not _QUALIFIED.search(folded)


def _says_food(text: str) -> bool:
    folded = text.casefold()
    head = _DAY_HEADED.match(folded)
    headed = head is not None and head.group(1) in _DAY_HEADS
    return headed or bool(_named_slots(text)) or bool(_FOOD_SAID.search(folded))


def open_day_named(
    db, person_id: int, text: str, today: dt.date, *, no_menu: bool = False
) -> dt.date | None:
    """The one still-open day this answer names, or None: the day the Sunday message asks to be
    told with what changed, read without a model.

    With `no_menu`, a day that had targets but no menu is open too: the weekly check-in asks
    about one by name ("Sat had no menu"), and an answer about it was booked against Sunday."""
    named = _days_named(text, today)
    if len(named) != 1:
        return None
    still_open = days.unanswered_days(db, person_id, today)
    if no_menu:
        still_open += days.unanswered_without_menu(db, person_id, today)
    return next((d for d in still_open if d.weekday() in named), None)


def _planned_slots(db, person_id: int, day: dt.date) -> list[str]:
    """The slots the menu in force has on `day`, in menu order."""
    row = db.one(
        "SELECT md.id FROM v_menu_days_current md WHERE md.person_id = ? AND md.day = ?",
        (person_id, isoday(day)),
    )
    if row is None:
        return []
    present = {
        str(r["slot"])
        for r in db.all("SELECT slot FROM meal_slots WHERE menu_day_id = ?", (int(row["id"]),))
    }
    return [s for s in menu_mod.SLOT_ORDER if s in present]


def _slot_choice_rows(
    db, person_id: int, day: dt.date, picked: list[str]
) -> list[list[tuple[str, str]]]:
    """A toggle per planned slot, then Done and "Extra, nothing replaced"."""
    key = isoday(day).replace("-", "")
    rows = [
        [
            (
                f"{_SLOT_LABELS[s]} - skipped" if s in picked else _SLOT_LABELS[s],
                f"k:mslot:{key}:{s}",
            )
        ]
        for s in _planned_slots(db, person_id, day)
    ]
    rows.append([("Done", f"k:mslotok:{key}"), ("Extra, nothing replaced", f"k:mslot0:{key}")])
    return rows


def _choice_question(picked: list[str], day: dt.date) -> str:
    """The question names its day: an answer about Tuesday typed on Wednesday's check-in was
    asked "I read that as skipping dinner" and booked against Wednesday, and nothing said which
    day until the last tap."""
    if picked:
        named = " and ".join(_SLOT_LABELS[s].lower() for s in picked)
        return (
            f"I read that as skipping {named} on {day:%a %d %b}. "
            "Tap a meal to change that, then Done."
        )
    return (
        f"Which planned meals on {day:%a %d %b} did that replace? Tap each one you skipped, "
        "then Done. Tap Extra if it was on top of the plan."
    )


class DayFlow:
    def __init__(self, config: Config, *, clock: Clock = DEFAULT_CLOCK):
        self.config = config
        self.clock = clock

    # -- day confirmation -----------------------------------------------------------------
    def confirm(self, h: Handlers, inbound: Inbound, parts: list[str], as_planned: bool) -> str:
        day = _day_from_key(parts[0]) if parts else self.clock.today_local()
        if day is None:
            return "ignored"
        person = inbound.person
        if not self._recent(day):
            return self._too_old(h, person, day)
        existing = days.latest_confirmation(h.db, person.id, day)
        # not clear_buttons: the evening message carries two questions, and answering the food
        # one wiped the training buttons off the same message with no way to get them back
        _keep_session_row(h, inbound, person.id, day)
        if existing is not None and existing["status"] == "confirmed":
            if not as_planned:
                # a mis-tapped "As planned" was permanent: the guard fired before the branch,
                # so "Not quite" answered with a flat refusal, and _keep_session_row had
                # already taken the food buttons off the message. The day stayed recorded at
                # the full target on a day they ate 1200 or 3500, and that number goes
                # straight into the measured TDEE and next week's targets.
                self._set_flow(h, person.id, {"flow": OFFPLAN_FLOW, "day": isoday(day)})
                h.outbox.reply(
                    person,
                    f"{day:%a %d %b} is recorded as planned. What actually changed? "
                    "Tell me and I will replace it.",
                )
                return "handled"
            self._close_offplan(h, person.id)
            h.outbox.reply(person, f"{day:%a %d %b} already recorded as confirmed.")
            return "handled"
        if as_planned:
            planned = days.planned_macros(h.db, person.id, day)
            basis = "menu" if planned is not None else "none"
            macros = planned if planned is not None else days.target_macros(h.db, person.id, day)
            note = None if planned is not None else "assumed target"
            days.confirm_day(
                h.db,
                person.id,
                day,
                basis=basis,
                macros=macros,
                note=note,
                message_in_id=inbound.message_in_id,
            )
            # an unanswered "what changed?" from earlier is now moot; leaving it armed meant
            # the next morning's "what's for lunch?" was logged as yesterday's food
            self._close_offplan(h, person.id)
            self._refresh_review(h, person, [day])
            what = f"{macros.kcal} kcal" if macros else "as planned"
            h.outbox.reply(
                person,
                f"{day:%a %d %b} recorded: {what}.",
                self._tomorrow_buttons(h, person.id, day),
            )
            return "handled"
        # Not quite: ask what changed; the answer is captured by the text handler below.
        # Guarded like every other flow that arms itself. conversation_state holds one flow
        # per person and is where the interview's answers live, so tapping this button on an
        # old evening card mid-onboarding wrote straight over twenty-odd typed answers with
        # nothing saying so - and the only way back was the whole interview from the start.
        blocking = convstate.displaceable(h.db, person.id, OFFPLAN_FLOW, clock=self.clock)
        if blocking in convstate.PROTECTED_FLOWS:
            h.outbox.reply(person, "Answer the question above first, then tap Not quite again.")
            return "handled"
        self._set_flow(h, person.id, {"flow": OFFPLAN_FLOW, "day": isoday(day)})
        took_over = " The tape question can wait for next Sunday." if blocking == "measures" else ""
        h.outbox.reply(
            person,
            "What changed? Type it (e.g. 'skipped lunch, kebab and chips for dinner'). "
            "Send 'same' if it was close enough." + took_over,
        )
        return "handled"

    def confirm_all(self, h: Handlers, inbound: Inbound, parts: list[str]) -> str:
        """k:cfmall:<yyyymmdd>[:week|:before] - that day as planned, and every day before it
        still open; with a mode, only the days before it.

        One tap for the week. The days are found again now, from the ledger - never
        carried in the button - so an old message cannot record a day somebody has since
        answered differently, and a day nobody was asked about is never recorded. Each goes
        through the same `confirm_day` as a single "As planned", estimates and all.

        `week` is the Sunday summary's button, keyed on the day it asked up to (`week_open`):
        the Sunday, whose own check-in asks about it tonight, or the Monday when the summary was
        caught up after Sunday had ended. The summary is nobody's day card, so its keyboard
        simply goes. `before` is the evening check-in's own row once today is answered: the
        earlier days, and today left as it is.
        """
        from ..jobs import sunday_summary  # noqa: PLC0415 (jobs import the scheduler)

        day = _day_from_key(parts[0]) if parts else None
        if day is None:
            return "ignored"
        mode = parts[1] if len(parts) > 1 else ""
        person = inbound.person
        if not self._recent(day):
            return self._too_old(h, person, day)
        if mode == "week":
            wanted = sunday_summary.week_open(h.db, person.id, day)
        elif mode == "target":
            # a weekly check-in's days with no menu, recorded at the target as the daily
            # check-in's "As planned" records one
            wanted = days.unanswered_without_menu(h.db, person.id, day)
        else:
            wanted = days.unanswered_days(h.db, person.id, day)
        if mode not in ("week", "before", "target"):
            wanted.append(day)
        recorded: list[tuple[dt.date, int | None]] = []
        for d in wanted:
            if not self._recent(d):
                continue
            latest = days.latest_confirmation(h.db, person.id, d)
            if latest is not None and str(latest["status"]) in ("confirmed", "paused"):
                continue
            planned = days.planned_macros(h.db, person.id, d)
            macros = planned if planned is not None else days.target_macros(h.db, person.id, d)
            days.confirm_day(
                h.db,
                person.id,
                d,
                basis="menu" if planned is not None else "none",
                macros=macros,
                note=None if planned is not None else "assumed target",
                message_in_id=inbound.message_in_id,
            )
            recorded.append((d, macros.kcal if macros is not None else None))
        if mode == "week":
            # the Sunday summary is no day's check-in: rebuilding "As planned / Not quite" on it
            # put buttons for one unnamed weekday under the week's review, and "Not quite" there
            # booked a correction meant for Tuesday against Friday
            if inbound.kind == "callback":
                with contextlib.suppress(TelegramError):
                    h.clear_buttons(inbound)
        else:
            _keep_session_row(h, inbound, person.id, day)
        answered = [d for d, _k in recorded]
        self._close_offplan(h, person.id, answered)
        if not recorded:
            h.outbox.reply(person, "Those days are already recorded.")
            return "handled"
        self._refresh_review(h, person, answered)
        names = ", ".join(f"{d:%a}" for d in answered)
        # tomorrow's meals only for the evening being lived. For the Sunday summary keyed on the
        # last open day these were Saturday's or Sunday's, already eaten; and a card tapped days
        # late has had its tomorrow already
        evening = self.clock.today_local()
        if self.clock.now_local().hour < BEFORE_DAWN_H:
            evening -= dt.timedelta(days=1)
        buttons = self._tomorrow_buttons(h, person.id, day) if day == evening else None
        how = "at the target" if mode == "target" else "as planned"
        h.outbox.reply(person, f"Recorded {how}: {names}.", buttons)
        return "handled"

    def _close_offplan(
        self, h: Handlers, person_id: int, answered: list[dt.date] | None = None
    ) -> None:
        """Drop a "What changed?" the tap has made moot, and nothing else.

        `conversation_state` holds one flow per person and the interview's answers live there,
        so clearing the row whatever it held deleted a restarted onboarding a dozen answers in,
        from one tap on last night's card - the hole the "Not quite" path guards against. With
        `answered`, only a question about one of those days goes: after the earlier days are
        recorded, today's "What changed?" is still waiting for its answer.
        """
        st = self._flow(h, person_id)
        if st is None:
            return
        if answered is not None and st.get("day") not in {isoday(d) for d in answered}:
            return
        self._set_flow(h, person_id, None)

    def _refresh_review(self, h: Handlers, person: Person, answered: list[dt.date]) -> None:
        """The week's review on the page, written again when these days change what it says."""
        from ..jobs import sunday_summary  # noqa: PLC0415 (jobs import the scheduler)

        sunday_summary.refresh_review(h.db, self.config, h.registry, person, answered)

    def _recent(self, day: dt.date) -> bool:
        """Is this day still one a tap may record?

        The day travels in the callback payload and Telegram buttons never expire, so scrolling
        back to an evening message from months ago and tapping "As planned" wrote a confirmed
        day at the full target for a date long closed - which is a usable day, so it went
        straight into the measured TDEE and moved the targets. A week is the whole plausible
        window for "I forgot to answer that one".
        """
        today = self.clock.today_local()
        return -1 <= (today - day).days <= BACKDATE_DAYS

    def _too_old(self, h: Handlers, person, day: dt.date) -> str:
        h.outbox.reply(
            person,
            f"That message was about {day:%a %d %b}, which is too far back to change now. "
            "Send /today for where the day stands.",
        )
        return "handled"

    def _tomorrow_buttons(self, h: Handlers, person_id: int, day: dt.date) -> Keyboard | None:
        """Offer tomorrow's meals after the evening check-in is answered."""
        from ..jobs import menu_propose  # noqa: PLC0415 (jobs import the scheduler)

        tomorrow = day + dt.timedelta(days=1)
        menu_id = menu_mod.accepted_menu_id(h.db, person_id, tomorrow)
        if menu_id is None:
            return None
        rows = menu_propose.meal_buttons(h.db, menu_id, day=tomorrow)
        return inline_keyboard(rows) if rows else None

    def _set_flow(self, h: Handlers, person_id: int, state: dict | None) -> None:
        if state is None:
            h.db.execute("DELETE FROM conversation_state WHERE person_id = ?", (person_id,))
            return
        h.db.execute(
            "INSERT INTO conversation_state(person_id, state_json, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(person_id) DO UPDATE SET state_json = excluded.state_json, "
            "updated_at = excluded.updated_at",
            (person_id, json.dumps(state), self.clock.utc_stamp()),
        )

    def _flow(self, h: Handlers, person_id: int) -> dict | None:
        row = h.db.one(
            "SELECT state_json, updated_at FROM conversation_state WHERE person_id = ?",
            (person_id,),
        )
        if row is None:
            return None
        st = json.loads(row["state_json"])
        if st.get("flow") != OFFPLAN_FLOW:
            return None
        # expire by elapsed time as well as by date. A question asked at 20:31 and still armed
        # at 08:00 catches the morning's first message, and "yesterday" was not a tight enough
        # bound because all of the following day passed the date check. The caller says so
        # rather than swallowing the message silently.
        st["age_s"] = (self.clock.now_utc() - parse_stamp(str(row["updated_at"]))).total_seconds()
        return st

    def offplan_text(self, h: Handlers, inbound: Inbound, text: str) -> str | None:
        st = self._flow(h, inbound.person.id)
        if st is None:
            return None
        day = parse_day(st["day"])
        person = inbound.person
        # staleness is checked BEFORE "is this something else": a stale question has to say so
        # and close regardless of what the new message looks like, including one shaped like a
        # question - "what's for lunch?" the next morning must still hear that last night's
        # question has closed, not be silently waved through to chat as though nothing had been
        # asked at all.
        today = self.clock.today_local()
        if day < today - dt.timedelta(days=1) or float(st.get("age_s") or 0) > OFFPLAN_TTL:
            # the question never expired, so a message days later was logged as that day's food.
            # Answer it as ordinary conversation instead, and say why.
            self._set_flow(h, person.id, None)
            if st.get("armed") == "evening":
                return None  # never engaged with, so there is nothing to say has closed
            h.outbox.reply(
                person,
                f"That question was about {day:%a %d %b} and it has closed. "
                "Send /today to see where the day stands.",
            )
            return None
        if convstate.food_answer_is_something_else(text):
            # "What changed?" is open and they ask for the menu. Here the cost is worse than a
            # dropped message: the question's answer becomes the DAY'S FOOD NOTE, gets estimated,
            # and a number invented from "send me the menu for tomorrow" reaches the measured
            # TDEE and next week's targets. A flow claims a message only when it could be an
            # answer to its own question, and "kebab and chips for dinner" still could be. The
            # flow stays armed - the real answer may still be the next message.
            return None
        if st.get("armed") == "evening" and _names_another_day(text, day, today):
            # armed by the check-in and never engaged with, and the answer is about another day:
            # the Sunday message asks for the week's open days in these words, and after
            # tonight's check-in "Wednesday: kebab instead of dinner" was booked as Sunday's
            # food while Wednesday stayed open.
            named = open_day_named(h.db, person.id, text, today, no_menu=True)
            if named is not None and _says_as_planned(text):
                # "Tuesday was as planned" is an answer, not food: that day recorded as its own
                # "As planned" records it, and tonight's question left armed for its answer
                said = self.confirm(h, inbound, [named.strftime("%Y%m%d")], True)
                self._set_flow(h, person.id, _without_age(st))
                return said
            if (
                named is None
                or not _says_food(text)
                or getattr(h, "offplan_estimator", None) is None
            ):
                return None  # chat hears the day it names; still armed
            # one of the days still open, which the check-in offered by name: that day's food,
            # read here without a model. Left to chat, a model taking "today is the default
            # day" at its word booked Tuesday's pizza against Wednesday, and the "All N days"
            # tap then recorded Tuesday at the plan - both days wrong
            return self.ask_replaced(h, inbound, named, text.strip())
        if st.get("note") is not None:
            # The food note is held while meal buttons await a tap. A new message cannot
            # replace that note until the person answers the buttons.
            h.outbox.reply(
                person,
                "Tap the meals you skipped on the message above, then Done. "
                "Tap Extra if it was on top of the plan.",
            )
            return "handled"
        self._set_flow(h, person.id, None)
        cleaned = text.strip()
        if cleaned.lower() in ("same", "close enough", "as planned"):
            return self.confirm(h, inbound, [day.strftime("%Y%m%d")], True)
        estimator = getattr(h, "offplan_estimator", None)
        if estimator is not None:
            # day_confirmations is append-only, so writing it HERE - before the estimate exists,
            # let alone before it is Accepted - cannot be corrected later, only added to: the
            # full planned day went in now, and the accepted estimate's kcal went in a second
            # time when `intents.apply` re-confirmed on top of it, so "skipped dinner for a
            # burrito" counted the missed dinner AND the burrito. The write moves
            # to `intents.apply`, once there is a real number to write and a tap has accepted it.
            if _planned_slots(h.db, person.id, day):
                # always asked, never guessed: the words only pre-tick the boxes. A wrong guess
                # is a wrong number nobody would notice - it just feeds a slightly-off day into
                # the measured TDEE - and one tap on Done is the whole cost of being right.
                picked = _named_slots(cleaned)
                self._set_flow(
                    h,
                    person.id,
                    {
                        "flow": OFFPLAN_FLOW,
                        "day": isoday(day),
                        "note": cleaned[:500],
                        "picked": picked,
                    },
                )
                h.outbox.reply(
                    person,
                    _choice_question(picked, day),
                    inline_keyboard(_slot_choice_rows(h.db, person.id, day, picked)),
                )
                return "handled"
            return estimator(h, inbound, day, cleaned, replaced_slots=[], finalize=True)
        planned = days.planned_macros(h.db, person.id, day)
        macros = planned if planned is not None else days.target_macros(h.db, person.id, day)
        days.confirm_day(
            h.db,
            person.id,
            day,
            basis="mixed" if planned else "none",
            macros=macros,
            note=cleaned[:500],
            message_in_id=inbound.message_in_id,
        )
        self._refresh_review(h, person, [day])
        h.outbox.reply(
            person,
            f"Noted for {day:%a %d %b}: {esc(cleaned[:120])}. Recorded with the planned numbers "
            "and your note; estimates come online with the model gateway.",
        )
        return "handled"

    def ask_replaced(self, h: Handlers, inbound: Inbound, day: dt.date, text: str) -> str:
        """Food heard in conversation: ask which planned meals it replaced, then estimate it.

        Whether the answer closes the day depends on the clock, not on the words: a past day,
        or today once the evening check-in is due, closes on Accept; earlier in the day the
        food is recorded and the check-in closes the day as usual. Either way the meals it
        replaced come off (`days.confirm_day` subtracts them from the plan).
        """
        person = inbound.person
        finalize = day < self.clock.today_local() or self._past_check_in(h, person.id)
        estimator = getattr(h, "offplan_estimator", None)
        if estimator is None:
            return "ignored"
        if not _planned_slots(h.db, person.id, day):
            return str(estimator(h, inbound, day, text, replaced_slots=[], finalize=finalize))
        blocking = convstate.displaceable(h.db, person.id, OFFPLAN_FLOW, clock=self.clock)
        if blocking in convstate.PROTECTED_FLOWS:
            h.outbox.reply(person, "Answer the question above first, then tell me again.")
            return "handled"
        picked = _named_slots(text)
        self._set_flow(
            h,
            person.id,
            {
                "flow": OFFPLAN_FLOW,
                "day": isoday(day),
                "note": text.strip()[:500],
                "picked": picked,
                "finalize": finalize,
            },
        )
        h.outbox.reply(
            person,
            _choice_question(picked, day),
            inline_keyboard(_slot_choice_rows(h.db, person.id, day, picked)),
        )
        return "handled"

    def _past_check_in(self, h: Handlers, person_id: int) -> bool:
        profile = targets.profile_on(h.db, person_id, self.clock.today_local())
        when = str(profile["confirm_time"] or "20:30") if profile is not None else "20:30"
        try:
            hh, mm = (int(x) for x in when.split(":", 1))
        except ValueError:
            hh, mm = 20, 30
        now = self.clock.now_local()
        return (now.hour, now.minute) >= (hh, mm)

    def _held(self, h: Handlers, inbound: Inbound, key: str) -> tuple[dt.date, dict] | None:
        """The day and the held "what changed?" state a meal button belongs to, or None."""
        day = _day_from_key(key)
        if day is None:
            return None
        st = self._flow(h, inbound.person.id)
        if st is None or st.get("day") != isoday(day) or st.get("note") is None:
            return None  # a stale button under an old, already-answered question
        return day, st

    def replaced_slot(self, h: Handlers, inbound: Inbound, parts: list[str]) -> str:
        """A meal button on "which planned meals did that replace?": tick or untick it."""
        if len(parts) < 2 or parts[1] not in menu_mod.SLOT_ORDER:
            return "ignored"
        held = self._held(h, inbound, parts[0])
        if held is None:
            return "ignored"
        day, st = held
        slot = parts[1]
        if "picked" not in st:
            # a keyboard sent before the question became multi-choice: one tap was the answer
            return self._finish(h, inbound, day, st, [slot])
        picked = [s for s in st["picked"] if s != slot]
        if slot not in st["picked"]:
            picked = [s for s in menu_mod.SLOT_ORDER if s in {*picked, slot}]
        self._set_flow(h, inbound.person.id, {**_without_age(st), "picked": picked})
        if inbound.kind == "callback" and inbound.message_id is not None:
            markup = inline_keyboard(_slot_choice_rows(h.db, inbound.person.id, day, picked))
            with contextlib.suppress(TelegramError):
                h.outbox.api.edit_reply_markup(inbound.chat_id, inbound.message_id, markup)
        return "handled"

    def replaced_done(self, h: Handlers, inbound: Inbound, parts: list[str]) -> str:
        """Done: estimate the held food against the day minus every ticked meal."""
        held = self._held(h, inbound, parts[0]) if parts else None
        if held is None:
            return "ignored"
        day, st = held
        return self._finish(h, inbound, day, st, list(st.get("picked") or []))

    def replaced_nothing(self, h: Handlers, inbound: Inbound, parts: list[str]) -> str:
        """Extra, nothing replaced: the food was on top of the whole plan."""
        held = self._held(h, inbound, parts[0]) if parts else None
        if held is None:
            return "ignored"
        day, st = held
        return self._finish(h, inbound, day, st, [])

    def _finish(
        self, h: Handlers, inbound: Inbound, day: dt.date, st: dict, slots: list[str]
    ) -> str:
        self._set_flow(h, inbound.person.id, None)
        h.clear_buttons(inbound)
        estimator = getattr(h, "offplan_estimator", None)
        if estimator is None:
            return "ignored"  # the flow only arms this keyboard when an estimator is installed
        note = str(st.get("note") or "")
        finalize = bool(st.get("finalize", True))
        return estimator(h, inbound, day, note, replaced_slots=slots, finalize=finalize)

    # -- sessions ---------------------------------------------------------------------------
    def session(self, h: Handlers, inbound: Inbound, parts: list[str], confirm: bool) -> str:
        if not parts or not parts[0].isdigit():
            return "ignored"
        match = h.db.one("SELECT * FROM session_matches WHERE id = ?", (int(parts[0]),))
        # deliberately no clear_buttons, and no "already recorded" dead end: a mis-tapped
        # "Not this" was permanent, the buttons went with it, and nothing anywhere could put
        # it right. session_matches is append-only and every reader takes the latest row, so
        # the correction is another row - the same shape as the cycle taps.
        if match is None or match["person_id"] != inbound.person.id:
            return "ignored"
        day = parse_day(str(match["day"]))
        # `confirm` and `session_manual` both guard on `_recent` and this did not, so on ONE
        # keyboard two button rows disagreed: "As planned" on a six-week-old card answered "too
        # far back to change now" while "Session confirmed" beside it wrote a confirmed session
        # into a week already summarised and calibrated. `_recent`'s own docstring - Telegram
        # buttons never expire - is the argument, and it applies identically here.
        if not self._recent(day):
            h.outbox.reply(
                inbound.person,
                f"That was the session question for {day:%a %d %b}, too far back to change now.",
            )
            return "handled"
        status = "confirmed" if confirm else "rejected"
        decided = days.session_decided(h.db, inbound.person.id, day)
        if decided is not None and str(decided["status"]) == status:
            h.outbox.reply(inbound.person, f"Session already recorded as {status}.")
            return "handled"
        h.db.insert(
            "session_matches",
            person_id=inbound.person.id,
            day=match["day"],
            planned_session_id=match["planned_session_id"],
            icu_id=match["icu_id"],
            status=status,
            minutes=match["minutes"],
            message_in_id=inbound.message_in_id,
        )
        if decided is not None:
            h.outbox.reply(inbound.person, f"Session changed to {status}.")
            return "handled"
        h.outbox.reply(
            inbound.person,
            ("Session confirmed." if confirm else "Not counted.")
            + " Tap the other button on that message if that was wrong.",
        )
        return "handled"

    def session_manual(self, h: Handlers, inbound: Inbound, parts: list[str]) -> str:
        if len(parts) < 2:
            return "ignored"
        day = _day_from_key(parts[0])
        if day is None:
            return "ignored"
        person = inbound.person
        if not self._recent(day):
            return self._too_old(h, person, day)
        done = parts[1] == "done"
        status = "manual" if done else "missed"
        decided = days.session_decided(h.db, person.id, day)
        if decided is not None and str(decided["status"]) == status:
            h.outbox.reply(person, f"Session already recorded as {status}.")
            return "handled"
        # one row per PLANNED session, not one row for the day. `Tue ride 60, Tue strength 30`
        # is an ordinary week; recording only the first left the second unrecordable for ever,
        # so a week with one double day read 4/5 however faithfully it was trained.
        planned = [
            row
            for row in days.planned_sessions_on(h.db, person.id, day)
            if str(row["kind"]) != "rest"
        ]
        for row in planned or [None]:
            h.db.insert(
                "session_matches",
                person_id=person.id,
                day=isoday(day),
                planned_session_id=row["id"] if row is not None else None,
                status=status,
                minutes=int(row["target_minutes"]) if (row is not None and done) else None,
                message_in_id=inbound.message_in_id,
            )
        if decided is not None:
            h.outbox.reply(person, f"Session changed to {status}.")
            return "handled"
        what = "Session recorded." if done else "Recorded as skipped."
        if len(planned) > 1:
            what = (
                f"Both sessions recorded as {'done' if done else 'skipped'}."
                if done
                else "Both sessions recorded as skipped."
            )
        if day != self.clock.today_local():
            # a weekly check-in asks about the week's sessions by day, on one message
            what = f"{day:%a %d %b}: {what}"
        h.outbox.reply(person, what + " Tap the other button if that was wrong.")
        return "handled"

    # -- commands -------------------------------------------------------------------------
    def cmd_today(self, h: Handlers, inbound: Inbound, arg: str) -> str:
        person = inbound.person
        day = self.clock.today_local()
        lines = [f"<b>{day:%a %d %b}</b>"]
        t = targets.latest_targets(h.db, person.id, day)
        if t is not None:
            m = targets.macros_of(t)
            lines.append("Target " + macros_line(m.kcal, m.protein_g, m.fat_g, m.carb_g, m.fibre_g))
        else:
            lines.append("No targets yet; send /onboard.")
        conf = days.latest_confirmation(h.db, person.id, day)
        # the ledger's word ("unconfirmed") is not a person's; say what it means
        said = {
            "confirmed": "recorded",
            "unconfirmed": "closed without an answer",
            "paused": "paused",
        }
        lines.append(f"Day: {said.get(str(conf['status']), 'open')}" if conf else "Day: open")
        point = body.latest_trend(h.db, person.id, day)
        if point is not None:
            delta = body.trend_delta(h.db, person.id, day, 7)
            extra = f" ({signed_kg(delta)} over 7 days)" if delta is not None else ""
            lines.append(f"Trend {kg(point.trend_kg)}{extra}")
        planned = [
            p
            for p in days.planned_sessions_on(h.db, person.id, day)
            if str(p["kind"]) != "rest"  # a day can hold two; naming one hid the other
        ]
        if planned:
            named = " then ".join(f"{p['kind']} {int(p['target_minutes'])} min" for p in planned)
            lines.append(f"Training: {named}")
        if targets.pause_active(h.db, person.id, day):
            lines.append("Paused.")
        h.outbox.reply(person, check_text("\n".join(lines), kind="today"))
        return "handled"

    def cmd_pause(self, h: Handlers, inbound: Inbound, arg: str) -> str:
        person = inbound.person
        today = self.clock.today_local()
        arg = arg.strip().lower()
        m = re.fullmatch(r"(\d{1,3})\s*d?(?:ays?)?", arg)
        if m:
            end = today + dt.timedelta(days=int(m.group(1)) - 1)
        else:
            try:
                end = parse_day(arg)
            except ValueError:
                h.outbox.reply(person, "Use /pause 3d or give the last paused day as YYYY-MM-DD.")
                return "handled"
        if end < today:
            h.outbox.reply(person, "That date is in the past.")
            return "handled"
        h.db.insert(
            "pause_periods", person_id=person.id, start_day=isoday(today), end_day=isoday(end)
        )
        h.outbox.reply(
            person,
            f"Paused until {end:%a %d %b}: no daily messages, days count as paused, "
            "weigh-ins still read. "
            "/resume ends it early.",
        )
        return "handled"

    def cmd_resume(self, h: Handlers, inbound: Inbound, arg: str) -> str:
        """`/resume` - messages start again now. It takes no argument, and says so.

        Swallowing one is the `/rate 4` mistake in miniature: somebody who sends
        `/resume tomorrow` meant something by it, and silently resuming NOW is a wrong answer
        dressed as a right one.
        """
        person = inbound.person
        today = self.clock.today_local()
        if arg.strip():
            h.outbox.reply(
                person,
                "<code>/resume</code> starts messages again now and takes nothing after it. "
                "<code>/pause 3d</code> is how you set a length.",
            )
            return "handled"
        if not targets.pause_active(h.db, person.id, today):
            h.outbox.reply(person, "Not paused. <code>/pause 3d</code> stops messages.")
            return "handled"
        yesterday = today - dt.timedelta(days=1)
        h.db.insert(
            "pause_periods",
            person_id=person.id,
            start_day=isoday(yesterday),
            end_day=isoday(yesterday),
            reason="resumed",
        )
        h.outbox.reply(person, "Resumed. Messages are back from now.")
        return "handled"

    def cmd_confirm(self, h: Handlers, inbound: Inbound, arg: str) -> str:
        """`/confirm`, or `/confirm YYYY-MM-DD`. Before dawn it means the day that just ended.

        At 00:30 "today" is thirty minutes old and has had no food in it. A bare /confirm then
        wrote it confirmed at the full target - a fabricated 2250 kcal straight into the measured
        TDEE - and `evening_confirm` skipped that day for ever after as "already confirmed",
        while the day he actually lived stayed unconfirmed. Two days lost per late night.

        The system already knows the boundary is not midnight: `day_close` runs at 00:05 to
        close YESTERDAY, and `OFFPLAN_TTL` and `_recent` exist because "which day" is ambiguous
        here. An explicit date always wins, and the reply names the day either way.
        """
        day = self.clock.today_local()
        if not arg.strip() and self.clock.now_local().hour < BEFORE_DAWN_H:
            day -= dt.timedelta(days=1)
        if arg.strip():
            try:
                day = parse_day(arg.strip())
            except ValueError:
                h.outbox.reply(inbound.person, "Use /confirm or /confirm YYYY-MM-DD.")
                return "handled"
        return self.confirm(h, inbound, [day.strftime("%Y%m%d")], True)


def _without_age(st: dict) -> dict:
    """The stored state without the `age_s` that `_flow` adds on the way out."""
    return {k: v for k, v in st.items() if k != "age_s"}


def arm_evening(db, person_id: int, day: dt.date, *, clock: Clock = DEFAULT_CLOCK) -> bool:
    """Arm "what changed?" as the evening check-in goes out, unless another question is open.

    The check-in says "Anything off plan? Type it.", but typing without first tapping
    Not quite went to chat, which logged the food on top of the WHOLE planned day and left the
    day open; a later "As planned" then counted the skipped meal and the food that
    replaced it. Armed here, the typed answer is the answer to the question the
    message actually asked. "As planned" and "Not quite" both re-arm or clear it as before.
    """
    if convstate.open_flow(db, person_id, clock=clock):
        return False
    db.execute(
        "INSERT INTO conversation_state(person_id, state_json, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(person_id) DO UPDATE SET state_json = excluded.state_json, "
        "updated_at = excluded.updated_at",
        (
            person_id,
            json.dumps({"flow": OFFPLAN_FLOW, "day": isoday(day), "armed": "evening"}),
            clock.utc_stamp(),
        ),
    )
    return True


def _keep_session_row(h: Handlers, inbound: Inbound, person_id: int, day: dt.date) -> None:
    """Leave the training question on a message whose food question was just answered."""
    from ..jobs.evening_confirm import (  # noqa: PLC0415 (jobs import the scheduler)
        session_part,
        week_rows,
    )
    from .api import inline_keyboard  # noqa: PLC0415

    # only a button press has a bot message to edit. For /confirm or a typed "same",
    # inbound.message_id is the PERSON's own message, Telegram answers 400 "message can't be
    # edited", and the TelegramError propagated out of the handler before the day was
    # recorded - so two of the three documented ways to close a day died with a generic
    # "something went wrong", /today still said "Day: open", and retrying broke again.
    if inbound.kind != "callback" or inbound.message_id is None:
        return
    _line, buttons = session_part(h.db, person_id, day)
    rows: list[list[tuple[str, str]]] = []
    key = isoday(day).replace("-", "")
    # and the earlier days the message also asked about. Answering today took their button
    # off and left "Also still open: Mon, Tue" with nothing to answer it - worst after "Not
    # quite", the natural tap when today was off but the days before went to plan. Only on the
    # check-in that asked: the 21:45 reminder names the day alone, and a check-in after the
    # Sunday summary left the days out; a button there recorded days the message never named.
    today = h.clock.today_local()
    earlier = [
        d for d in days.unanswered_days(h.db, person_id, day) if (today - d).days <= BACKDATE_DAYS
    ]
    asked = _check_in_asked(h, inbound, person_id, day)
    if earlier and asked.get("still_open"):
        label = (
            f"{earlier[0]:%a} as planned"
            if len(earlier) == 1
            else f"All {len(earlier)} earlier days as planned"
        )
        rows.append([(label, f"k:cfmall:{key}:before")])
    rows.append([("As planned", f"k:cfm:{key}"), ("Not quite", f"k:cfmx:{key}")])
    if buttons:
        rows.append(buttons)
    # and a weekly check-in's days with no menu and the week's sessions, until each is answered
    rows += week_rows(h.db, person_id, day, asked)
    markup = inline_keyboard(rows)
    # and even on a callback this is cosmetic: it must never be the reason a day is lost
    with contextlib.suppress(TelegramError):
        h.outbox.api.edit_reply_markup(inbound.chat_id, inbound.message_id, markup)


def _check_in_asked(h: Handlers, inbound: Inbound, person_id: int, day: dt.date) -> dict:
    """What the tapped message asked besides `day`, when it is `day`'s evening check-in: its
    payload (the earlier open days, and a weekly check-in's days with no menu and sessions)."""
    row = h.db.one(
        "SELECT payload_json FROM messages_out WHERE person_id = ? AND kind = 'evening_confirm' "
        "AND due_key = ? AND status = 'sent' AND telegram_message_id = ?",
        (person_id, isoday(day), inbound.message_id),
    )
    if row is None:
        return {}
    try:
        asked = json.loads(str(row["payload_json"] or "{}"))
    except ValueError:
        return {}
    return asked if isinstance(asked, dict) else {}


def install(handlers: Handlers, config: Config, *, clock: Clock = DEFAULT_CLOCK) -> DayFlow:
    flow = DayFlow(config, clock=clock)
    handlers.register_callback("cfm", lambda h, i, p: flow.confirm(h, i, p, True))
    handlers.register_callback("cfmx", lambda h, i, p: flow.confirm(h, i, p, False))
    handlers.register_callback("cfmall", flow.confirm_all)
    handlers.register_callback("sess", lambda h, i, p: flow.session(h, i, p, True))
    handlers.register_callback("sessx", lambda h, i, p: flow.session(h, i, p, False))
    handlers.register_callback("sessm", flow.session_manual)
    handlers.register_callback("mslot", flow.replaced_slot)
    handlers.register_callback("mslotok", flow.replaced_done)
    handlers.register_callback("mslot0", flow.replaced_nothing)
    handlers.register_command("today", flow.cmd_today)
    handlers.register_command("pause", flow.cmd_pause)
    handlers.register_command("resume", flow.cmd_resume)
    handlers.register_command("confirm", flow.cmd_confirm)
    handlers.text_handlers.append(flow.offplan_text)
    handlers.offplan_asker = flow.ask_replaced  # type: ignore[attr-defined]
    # an off-plan answer closes its day in `intents.apply`, on Accept: the week's review is
    # written again from there as from every other close
    handlers.review_refresher = flow._refresh_review  # type: ignore[attr-defined]
    return flow
