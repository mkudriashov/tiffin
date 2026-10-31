"""Intents: the model's write tools only ever create `intents` rows (§B.5). The person sees
"Log kebab and chips, 900 kcal?" with Accept / Discard; the tap writes the real row here.

propose(): render + send one intent for confirmation. Callbacks: k:intent:<id>, k:intentx:<id>.
apply(): the real write per kind, inside the accepting transaction.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import sqlite3
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from .. import days, energy, targets
from ..clock import DEFAULT_CLOCK, Clock, isoday, parse_day
from ..persons import Person
from .api import inline_keyboard
from .outbox import SendResult
from .render import esc, fit

if TYPE_CHECKING:
    from .handlers import Handlers
    from .poller import Inbound

RATING_MIN, RATING_MAX = 1, 5  # the ratings.score CHECK, restated where the model is read
MAX_WEEKDAY = 6
MAX_SESSION_MINUTES = 600  # the planned_sessions.target_minutes CHECK

KINDS = (
    "log_offplan",
    "confirm_day",
    "rate_dish",
    "set_pause",
    "set_plan",
    "set_profile",
    "set_food_prefs",
)
#: the three amendable food lists, and the profile column each one lives in
PREF_COLUMNS = {
    "avoid": "refused_foods",
    "likes": "liked_foods",
    "pantry": "pantry_json",
}
#: one sentence per list and direction, because a generic template produced "From now on,
#: keep in the cupboard olive oil?" - the card is the only thing the person reads before they
#: tap, so it says what will happen in the words they would use
PREF_SENTENCES = {
    ("avoid", "add"): "Keep {foods} out of your menus from now on?",
    ("avoid", "remove"): "Allow {foods} back into your menus?",
    ("likes", "add"): "Put {foods} on the menu more often?",
    ("likes", "remove"): "Stop favouring {foods}?",
    ("pantry", "add"): "Treat {foods} as a cupboard staple, so it never reaches the shop?",
    ("pantry", "remove"): "Put {foods} back on the shopping list?",
}


def and_list(words: list[str]) -> str:
    """salmon · salmon and eggs · salmon, eggs and oats."""
    if len(words) <= 1:
        return "".join(words)
    return f"{', '.join(words[:-1])} and {words[-1]}"


def _food_prefs_sentence(payload: dict[str, Any]) -> str:
    which = str(payload.get("list") or "")
    parts = [
        PREF_SENTENCES[(which, direction)].format(foods=and_list(payload[direction]))
        for direction in ("add", "remove")
        if payload.get(direction) and (which, direction) in PREF_SENTENCES
    ]
    return " ".join(parts) if parts else "Change your food lists?"


PROFILE_FIELDS = {
    "morning_time",
    "confirm_time",
    "quiet_start",
    "quiet_end",
    "steps_floor",
    "tone",
    "meals_per_day",
    "pre_bed_protein",
    "rate_pct_per_week",
    "goal_weight_kg",
    "goal_date",
}


SLOT_WORDS = {
    "breakfast": "breakfast",
    "lunch": "lunch",
    "dinner": "dinner",
    "snack": "the snack",
    "pre_bed": "pre-bed",
}


def _day_words(value: Any) -> str:
    """A payload day as "Fri 25 Sep", never "2026-09-25"; "today" when there is none."""
    day = _day(value, None) if value else None
    return "today" if day is None else f"{day:%a %d %b}"


def describe(kind: str, payload: dict[str, Any]) -> str:
    if kind == "log_offplan":
        kcal = payload.get("kcal")
        what = payload.get("description") or ", ".join(
            i.get("name", "?") for i in payload.get("items", [])
        )
        when = _day_words(payload.get("day"))
        skipped = [s for s in (payload.get("replaced_slots") or []) if isinstance(s, str)]
        instead = (
            f" instead of {' and '.join(SLOT_WORDS.get(s, s) for s in skipped)}" if skipped else ""
        )
        return (
            f"Log {when}: {what} ({float(kcal):.0f} kcal){instead}?"
            if kcal
            else f"Log {when}: {what}?"
        )
    if kind == "confirm_day":
        planned = "as planned" if payload.get("as_planned", True) else "not quite"
        return f"Record {_day_words(payload.get('day'))} as {planned}?"
    if kind == "rate_dish":
        name = payload.get("name") or "that dish"
        return f"Rate {name} {payload.get('score')} out of 5?"
    if kind == "set_pause":
        return (
            f"Pause from {_day_words(payload.get('start_day'))} "
            f"to {_day_words(payload.get('end_day'))}?"
        )
    if kind == "set_plan":
        return f"Replace the training week with {len(payload.get('plan', []))} typed days?"
    if kind == "set_profile":
        changes = {k: v for k, v in payload.items() if k in PROFILE_FIELDS}
        return "Change " + ", ".join(f"{k} to {v}" for k, v in changes.items()) + "?"
    if kind == "set_food_prefs":
        return _food_prefs_sentence(payload)
    return f"{kind}?"


def create(
    db,
    person: Person,
    kind: str,
    payload: dict[str, Any],
    *,
    model_used: str | None = None,
    message_in_id: int | None = None,
) -> int:
    if kind not in KINDS:
        raise ValueError(f"unknown intent kind {kind}")
    return db.insert(
        "intents",
        person_id=person.id,
        kind=kind,
        payload_json=json.dumps(payload),
        model_used=model_used,
        message_in_id=message_in_id,
    )


def propose(
    h: Handlers,
    person: Person,
    kind: str,
    payload: dict[str, Any],
    *,
    model_used: str | None = None,
    message_in_id: int | None = None,
) -> tuple[int, SendResult]:
    iid = create(h.db, person, kind, payload, model_used=model_used, message_in_id=message_in_id)
    # a payload written before the scrubbing existed can still carry an emoji, and a card
    # that raises here is a check-in that never arrives; repair rather than refuse
    text = fit(esc(describe(kind, payload)), kind="intent")
    keyboard = inline_keyboard([[("Accept", f"k:intent:{iid}"), ("Discard", f"k:intentx:{iid}")]])
    result = h.outbox.send(
        person, "intent_confirm", f"intent:{iid}", text, keyboard, model_used=model_used
    )
    return iid, result


# ---------------------------------------------------------------- reading the model's payload
# Every command handler validates what the PERSON typed. `apply` is the other door into the
# same tables, and what comes through it was written by a model, so it gets the same doubt.
# Two of these were real: a set_pause whose days ran backwards answered "Paused until ..." and
# paused nothing, and - worse - `targets.pause_active` reads only the newest pause row, so a
# pause with an unparseable day silently ENDED a holiday that was already running. The rule
# here is the house one: refuse in a sentence the person can act on, never write the row and
# never raise, because a raise gives them "something went wrong" and tells them nothing.


def _day(value: Any, default: dt.date | None) -> dt.date | None:
    if value is None:
        return default
    try:
        return parse_day(str(value))
    except ValueError:
        return None


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except TypeError, ValueError:
        return None


def _positive(value: Any) -> float | None:
    try:
        n = float(value)
    except TypeError, ValueError:
        return None
    return n if math.isfinite(n) and n > 0 else None


def _sessions(plan: Any) -> list[tuple[int, str, int, Any]] | None:
    """The whole training week or none of it: a half-applied week is worse than a refusal."""
    from ..onboarding import KINDS as SESSION_KINDS  # noqa: PLC0415 (avoids a cycle)

    if not isinstance(plan, list) or not plan:
        return None
    out: list[tuple[int, str, int, Any]] = []
    for s in plan:
        if not isinstance(s, Mapping):
            return None
        weekday, minutes = _int(s.get("weekday")), _int(s.get("target_minutes", 0))
        skind = str(s.get("kind", ""))
        if weekday is None or minutes is None or skind not in SESSION_KINDS:
            return None
        if not 0 <= weekday <= MAX_WEEKDAY or not 0 <= minutes <= MAX_SESSION_MINUTES:
            return None
        out.append((weekday, skind, minutes, s.get("start_time")))
    return out


def apply(
    h: Handlers, person: Person, row: sqlite3.Row, *, clock: Clock, message_in_id: int | None
) -> str:
    kind = str(row["kind"])
    payload = json.loads(row["payload_json"] or "{}")
    today = clock.today_local()
    if kind == "log_offplan":
        day = _day(payload.get("day"), today)
        # a card with no estimate on it is not a food with no calories. `or 0.0` here stored
        # 2 Kinder sticks and 2 Twix fingers as 0 kcal, accepted, and the reply said "Logged 0
        # kcal off plan". IntentFlow.decide sends a bare card to the
        # estimate before it can reach this line; anything else that arrives without a real
        # number is refused rather than written.
        kcal = _positive(payload.get("kcal"))
        if day is None or kcal is None:
            return "I could not read that. Tell me what you ate and roughly how many calories."
        slots = [
            str(s) for s in (payload.get("replaced_slots") or [payload.get("replaced_slot")]) if s
        ]
        h.db.insert(
            "estimates",
            person_id=person.id,
            day=isoday(day),
            description=str(payload.get("description") or "off plan"),
            items_json=json.dumps(payload.get("items", [])),
            kcal=kcal,
            protein_g=payload.get("protein_g"),
            fat_g=payload.get("fat_g"),
            carb_g=payload.get("carb_g"),
            fibre_g=payload.get("fibre_g"),
            model_used=row["model_used"],
            status="accepted",
            message_in_id=message_in_id,
            replaced_slots_json=json.dumps(slots),
        )
        latest = days.latest_confirmation(h.db, person.id, day)
        # The evening flow closes the day after accepting its estimate. A daytime estimate
        # reconfirms only a day already confirmed; an open day waits for the evening flow,
        # which includes accepted estimates when it closes the day.
        finalize = bool(payload.get("finalize"))
        if finalize or (latest is not None and latest["status"] == "confirmed"):
            # the whole plan: `confirm_day` takes off every meal an accepted estimate replaced
            # (this one's included) and adds every estimate back, so the subtraction lives in
            # one place for every way a day is closed
            base = days.planned_macros(h.db, person.id, day)
            planned = base
            if finalize or latest is None:
                basis = "mixed" if planned else "none"
                note = payload.get("description")
            else:
                basis = str(latest["basis"])
                note = latest["note"]
            days.confirm_day(
                h.db,
                person.id,
                day,
                basis=basis,
                macros=base or days.target_macros(h.db, person.id, day),
                note=note,
                message_in_id=message_in_id,
            )
        closed = days.latest_confirmation(h.db, person.id, day)
        if closed is not None and closed["status"] == "confirmed" and closed["kcal"] is not None:
            return (
                f"Logged {kcal:.0f} kcal off plan. {day:%a %d %b} is now "
                f"{float(closed['kcal']):.0f} kcal."
            )
        return f"Logged {kcal:.0f} kcal off plan for {day:%a %d %b}."
    if kind == "confirm_day":
        day = _day(payload.get("day"), today)
        if day is None:
            return f"I could not read that date. Send /confirm, or /confirm {isoday(today)}."
        if not payload.get("as_planned", True):
            # "not quite" with nothing said about what changed is not a day at the planned
            # numbers - recording the whole plan here is how a skipped breakfast stayed eaten.
            # Ask the same question the evening button asks.
            h.db.execute(
                "INSERT INTO conversation_state(person_id, state_json, updated_at) "
                "VALUES (?, ?, ?) ON CONFLICT(person_id) DO UPDATE SET "
                "state_json = excluded.state_json, updated_at = excluded.updated_at",
                (
                    person.id,
                    json.dumps({"flow": "offplan", "day": isoday(day)}),
                    clock.utc_stamp(),
                ),
            )
            return (
                f"What changed on {day:%a %d %b}? Say what you ate instead "
                "(e.g. 'skipped lunch, kebab and chips for dinner')."
            )
        planned = days.planned_macros(h.db, person.id, day)
        macros = planned or days.target_macros(h.db, person.id, day)
        days.confirm_day(
            h.db,
            person.id,
            day,
            basis="menu" if planned else "none",
            macros=macros,
            note=payload.get("note"),
            message_in_id=message_in_id,
        )
        return f"{day:%a %d %b} recorded."
    if kind == "rate_dish":
        recipe_id = _int(payload.get("recipe_id"))
        score = _int(payload.get("score"))
        if recipe_id is None or score is None:
            return "I could not read that rating. Send /rate 4."
        if not RATING_MIN <= score <= RATING_MAX:
            return f"A rating is {RATING_MIN} to {RATING_MAX}."
        h.db.insert(
            "ratings",
            person_id=person.id,
            recipe_id=recipe_id,
            score=score,
            note=payload.get("note"),
            message_in_id=message_in_id,
        )
        return "Rating recorded."
    if kind == "set_pause":
        start = _day(payload.get("start_day"), None)
        end = _day(payload.get("end_day"), None)
        if start is None or end is None:
            return "I could not read those dates. Send /pause 3d."
        if end < start:
            return "That pause would end before it starts. Send /pause 3d."
        h.db.insert(
            "pause_periods",
            person_id=person.id,
            start_day=isoday(start),
            end_day=isoday(end),
            reason=payload.get("reason"),
        )
        return f"Paused until {end:%a %d %b}."
    if kind == "set_plan":
        sessions = _sessions(payload.get("plan"))
        if sessions is None:
            return "I could not read that training week. Send /week Mon run 45."
        plan_id = h.db.insert(
            "training_plans",
            person_id=person.id,
            effective_from=isoday(today),
            source_text=json.dumps(payload.get("plan", [])),
            model_used=row["model_used"],
        )
        for weekday, skind, minutes, start_time in sessions:
            h.db.insert(
                "planned_sessions",
                plan_id=plan_id,
                person_id=person.id,
                weekday=weekday,
                kind=skind,
                target_minutes=minutes,
                start_time=start_time,
            )
        return "Training week replaced."
    if kind == "set_food_prefs":
        from ..food import prefs as prefs_mod  # noqa: PLC0415 (food imports the db layer)

        current = targets.profile_on(h.db, person.id, today)
        if current is None:
            return "No profile yet; send /onboard."

        name = str(payload.get("list") or "")
        column = PREF_COLUMNS.get(name)
        if column is None:
            return "I could not tell which list that was. Send /set to change them yourself."
        # one door for every way a list changes (food/prefs.py): folded on the singular, so "no
        # chickpeas!!" a fourth time amends the entry already there, and the answer says what an
        # entry keeps out - "fish" said "Done" and kept nothing out until kinds were matched
        changed = prefs_mod.amend(
            h.db,
            person.id,
            name,
            add=[str(x) for x in payload.get("add") or []],
            remove=[str(x) for x in payload.get("remove") or []],
            clock=clock,
            strict=False,
        )
        shown = ", ".join(changed.terms) or "empty"
        note = ""
        if column == "refused_foods" and payload.get("add"):
            # "no bacon please" is a promise about the NEXT menu; this checks whether the one
            # already accepted is the reason it got asked in the first place - "no chickpeas"
            # said three times before it stopped serving chickpeas is how this house lost trust.
            still = prefs_mod.still_on_menu(h.db, person.id, today)
            if still:
                note = " " + prefs_mod.still_said(still)
        for ask in changed.asks[:2]:
            # houmous after chickpeas: /avoid puts the question with its two buttons
            note += f" {prefs_mod.ask_said(ask)} /avoid asks it with buttons."
        return (
            f"Done. {changed.said()} Your {name} list is now {shown}. The next menu will use "
            f"it.{note}"
        )
    if kind == "set_profile":
        if targets.profile_on(h.db, person.id, today) is None:
            return "No profile yet; send /onboard."
        changes = {k: v for k, v in payload.items() if k in PROFILE_FIELDS}
        targets.change_profile(h.db, person.id, changes, today=today)
        return "Profile updated."
    return "Recorded."


class IntentFlow:
    def __init__(self, *, clock: Clock = DEFAULT_CLOCK):
        self.clock = clock

    def decide(self, h: Handlers, inbound: Inbound, parts: list[str], accept: bool) -> str:
        if not parts or not parts[0].isdigit():
            return "ignored"
        iid = int(parts[0])
        row = h.db.one("SELECT * FROM intents WHERE id = ?", (iid,))
        h.clear_buttons(inbound)
        if row is None or row["person_id"] != inbound.person.id:
            return "ignored"
        existing = h.db.one("SELECT decision FROM intent_decisions WHERE intent_id = ?", (iid,))
        if existing is not None:
            h.outbox.reply(inbound.person, _ALREADY.get(str(existing["decision"]), "Already done."))
            return "handled"
        payload = json.loads(row["payload_json"] or "{}")
        kind = str(row["kind"])
        day = _day(payload.get("day"), None) if kind in _DATED else None
        if accept and day is not None and not _recent(day, self.clock.today_local()):
            # Telegram buttons never expire; DayFlow._recent exists because an old card tapped
            # weeks later rewrote a closed, already-calibrated day. Intents had no such guard.
            h.db.insert("intent_decisions", intent_id=iid, decision="lapsed")
            h.outbox.reply(
                inbound.person,
                f"That card was about {day:%a %d %b}, which is too far back to change now.",
            )
            return "handled"
        if accept and kind == "log_offplan" and _positive(payload.get("kcal")) is None:
            # a food card that never had its estimate (written before
            # the chat flow showed every card): estimate it now rather than store nothing
            h.db.insert("intent_decisions", intent_id=iid, decision="lapsed")
            asker = getattr(h, "offplan_asker", None) or getattr(h, "offplan_estimator", None)
            if asker is None or not str(payload.get("description") or "").strip():
                h.outbox.reply(
                    inbound.person, "I could not read that card. Tell me what you ate instead."
                )
                return "handled"
            when = day or self.clock.today_local()
            return str(asker(h, inbound, when, str(payload.get("description") or "")))
        with h.db.tx():
            h.db.insert(
                "intent_decisions",
                intent_id=iid,
                decision="accepted" if accept else "discarded",
                message_in_id=inbound.message_in_id,
            )
            text = (
                apply(h, inbound.person, row, clock=self.clock, message_in_id=inbound.message_in_id)
                if accept
                else _discarded(kind)
            )
        refresh = getattr(h, "review_refresher", None)
        if accept and kind in _DATED and refresh is not None:
            # the week's review on the page, written again when this closed one of its days -
            # as As planned does. An off-plan day closed here left it saying "not answered"
            # while the reply said the day was recorded
            refresh(h, inbound.person, [day or self.clock.today_local()])
        h.outbox.reply(inbound.person, esc(text))
        return "handled"


#: what a second tap on an answered card says, in words rather than the ledger's own
_ALREADY = {
    "accepted": "Already done.",
    "discarded": "Already discarded.",
    "lapsed": "That card has expired. Tell me again if it still matters.",
}
#: intent kinds whose payload names the day they change
_DATED = ("log_offplan", "confirm_day")
BACKDATE_DAYS = 7


def _recent(day: dt.date, today: dt.date) -> bool:
    return -1 <= (today - day).days <= BACKDATE_DAYS


def _discarded(kind: str) -> str:
    if kind == "log_offplan":
        # "Discarded." was a dead end: the day stayed open and nothing said what to do next
        return "Discarded. Tap As planned or Not quite on tonight's check-in to close the day."
    return "Discarded."


def lapse_open(db, person_id: int, before_stamp: str) -> int:
    rows = db.all(
        "SELECT i.id FROM intents i LEFT JOIN intent_decisions d ON d.intent_id = i.id "
        "WHERE i.person_id = ? AND d.id IS NULL AND i.created_at < ?",
        (person_id, before_stamp),
    )
    for r in rows:
        db.insert("intent_decisions", intent_id=int(r["id"]), decision="lapsed")
    return len(rows)


def install(handlers: Handlers, *, clock: Clock = DEFAULT_CLOCK) -> IntentFlow:
    flow = IntentFlow(clock=clock)
    handlers.register_callback("intent", lambda h, i, p: flow.decide(h, i, p, True))
    handlers.register_callback("intentx", lambda h, i, p: flow.decide(h, i, p, False))
    return flow


__all__ = [
    "KINDS",
    "IntentFlow",
    "apply",
    "create",
    "describe",
    "energy",
    "install",
    "lapse_open",
    "propose",
]
