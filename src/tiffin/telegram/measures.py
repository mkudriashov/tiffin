"""Manual tape measurements and the opted-in weekly cycle phase.

Weight is never typed — the scale is its source — but the tape has no sensor, so
these two numbers are the one place a person types a body measurement. The flow is therefore
narrow on purpose: it is armed only by an explicit `/measure`, it accepts
one reply, and it disarms itself. Outside that window a bare number means nothing here and
falls through to the ordinary conversation.

`conversation_state` holds one flow per person, so arming refuses to interrupt onboarding or a
pending off-plan question; the prompt says so rather than silently doing nothing.
"""

from __future__ import annotations

import datetime as dt
import json
import re

from .. import body, log
from ..clock import DEFAULT_CLOCK, Clock, isoday, parse_stamp
from ..db import Db
from ..food import weekplan
from ..persons import Person
from . import convstate
from .api import inline_keyboard
from .handlers import Handlers, Inbound
from .render import esc

logger = log.get("tiffin.measures")

FLOW = "measures"
#: an unanswered manual tape question stops claiming messages after this long
TAPE_TTL = 12 * 3600
#: unreadable replies before it stops asking and lets the conversation carry on
TAPE_MISSES = 2
#: a site, said any of the ways a person says it, as a word of its own ("chips" holds "hip" and
#: is not one), then its number and perhaps its unit: "chest 42", "over arm 47in", "shoulder
#: width 18.5\"", "seat 104 cm", "hip 102"
_LABELLED = re.compile(
    r"\b(over[- ]?arm|shoulders?(?:\s+width)?|natural\s+waist|waist|seat|hips?|chest|necks?"
    r"|sleeves?)\b"
    r"\D{0,8}?(\d{1,3}(?:[.,]\d+)?)\s*(cm|in(?:ch(?:es)?)?\b|\"|”)?",
    re.I,
)
#: the words -> the site they are (body.TAPE_SITES)
_SITE_OF = (
    ("over", "overarm"),
    ("shoulder", "shoulder"),
    ("waist", "waist"),
    ("seat", "hip"),
    ("hip", "hip"),
    ("chest", "chest"),
    ("neck", "neck"),
    ("sleeve", "sleeve"),
)
#: the whole message, and nothing but: "84", "84 102", "84/102", "84cm 102 cm", "84,5 101,5"
_BARE = re.compile(
    r"(\d{1,3}(?:[.,]\d)?)\s*(?:cm)?(?:\s*[,/]?\s*(\d{1,3}(?:[.,]\d)?)\s*(?:cm)?)?", re.I
)
PHASE_LABELS = (
    ("Period", "menstrual"),
    ("Follicular", "follicular"),
    ("Ovulation", "ovulation"),
    ("Luteal", "luteal"),
)


def _cm(value: float, said: str | None, unit: str) -> float:
    """Centimetres, from a number read in `unit` unless it says its own."""
    word = (said or "").lower()
    inches = word.startswith("in") or word in ('"', "”") or (not word and unit == "in")
    return round(value * body.CM_PER_INCH, 1) if inches else value


def parse_tape(text: str, unit: str = "cm") -> dict[str, float] | None:
    """Each site's centimetres out of a free-text reply, or None if it is not a measurement.
    "cm" or "in" is read as it says.

    Labelled numbers win over positional ones, because "hip 102 waist 84" means what it says.
    A single unlabelled number is the waist: that is the one the advisor actually uses, and
    asking again for the hip is worse than recording what was offered.
    """
    labelled: dict[str, float] = {}
    for m in _LABELLED.finditer(text):
        word = m.group(1).lower()
        site = next(site for start, site in _SITE_OF if word.startswith(start) or start in word)
        labelled[site] = _cm(float(m.group(2).replace(",", ".")), m.group(3), unit)
    if labelled:
        return labelled
    # the WHOLE message must be a measurement. Scanning for any 1-3 digit number anywhere in
    # the text meant "I ran 45 minutes this morning" was recorded as a 45 cm waist, and then
    # the flow disarmed, so the real number was never asked for again that week. A phantom
    # measurement is worse than a missing one: the advisor reads waist against the weight
    # trend and concludes the person is losing muscle.
    m = _BARE.fullmatch(text.strip())
    if m is None:
        return None
    found = [_cm(float(g.replace(",", ".")), None, unit) for g in m.groups() if g]
    return {"waist": found[0]} if len(found) == 1 else {"waist": found[0], "hip": found[1]}


def in_unit(cm: float, plan: weekplan.WeekPlan) -> str:
    if plan.tape_unit == "in":
        return f"{cm / body.CM_PER_INCH:.1f} in"
    return f"{cm:.1f} cm"


def sites_said(plan: weekplan.WeekPlan) -> str:
    words = [body.TAPE_WORDS.get(s, s) for s in plan.sites()]
    unit = "inches" if plan.tape_unit == "in" else "cm"
    return f"{', '.join(words)}, in {unit}"


#: a plausible reply, a site at a time, for the example
_EXAMPLE = {
    "in": {
        "chest": 42,
        "neck": 16,
        "overarm": 47,
        "shoulder": 18.5,
        "waist": 36,
        "hip": 41,
        "sleeve": 34,
    },
    "cm": {
        "chest": 107,
        "neck": 41,
        "overarm": 119,
        "shoulder": 47,
        "waist": 84,
        "hip": 102,
        "sleeve": 86,
    },
}


def reply_like(plan: weekplan.WeekPlan) -> str:
    """How to answer: every site by name, in their unit; any may be left out."""
    ex = _EXAMPLE["in" if plan.tape_unit == "in" else "cm"]
    word = {"hip": "seat", "shoulder": "shoulder"}
    parts = ", ".join(f"{word.get(s, s)} {ex[s]:g}" for s in plan.sites())
    return f"Reply like <code>{parts}</code> (any you have), or <code>skip</code>."


class MeasuresFlow:
    def __init__(self, *, clock: Clock = DEFAULT_CLOCK):
        self.clock = clock

    # -- state ---------------------------------------------------------------------------
    def _state(self, db: Db, person_id: int) -> dict | None:
        row = db.one(
            "SELECT state_json, updated_at FROM conversation_state WHERE person_id = ?",
            (person_id,),
        )
        if row is None:
            return None
        st = json.loads(row["state_json"])
        if st.get("flow") != FLOW:
            return None
        # An unanswered tape question once bounced every later
        # free-text message with "I need centimetres" and returned handled - free text, the
        # main way into this system, simply stopped working until numbers were sent. The same
        # expiry the menu-change flow already has.
        age = (self.clock.now_utc() - parse_stamp(str(row["updated_at"]))).total_seconds()
        if age > TAPE_TTL:
            self.disarm(db, person_id)
            return None
        return st

    def busy_with_something_else(self, db: Db, person_id: int) -> bool:
        return convstate.busy_with_other_than(db, person_id, FLOW, clock=self.clock)

    def arm(self, db: Db, person_id: int, day: dt.date) -> None:
        db.execute(
            "INSERT INTO conversation_state(person_id, state_json, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(person_id) DO UPDATE SET state_json = excluded.state_json, "
            "updated_at = excluded.updated_at",
            (person_id, json.dumps({"flow": FLOW, "day": isoday(day)}), self.clock.utc_stamp()),
        )

    def disarm(self, db: Db, person_id: int) -> None:
        db.execute("DELETE FROM conversation_state WHERE person_id = ? ", (person_id,))

    def _remember_miss(self, db: Db, person_id: int, st: dict, misses: int) -> None:
        db.execute(
            "UPDATE conversation_state SET state_json = ? WHERE person_id = ?",
            (json.dumps({**st, "misses": misses}), person_id),
        )

    # -- prompting -----------------------------------------------------------------------
    def prompt_text(self, db: Db, person: Person) -> str:
        last = body.latest_tape(db, person.id, "waist")
        plan = weekplan.of(db, person.id)
        if plan.tape_sites:
            return f"Tape measure, please: {sites_said(plan)}. {reply_like(plan)}"
        tail = f" Last waist {last[1]:.1f} cm." if last else ""
        return (
            "Tape measure, please: waist and hip in cm, at the navel and the widest point. "
            f"Reply like <code>84 102</code>, or <code>skip</code> to leave it for now.{tail}"
        )

    def phase_keyboard(self) -> dict:
        return inline_keyboard([[(label, f"k:cyc:{value}") for label, value in PHASE_LABELS]])

    # -- replies -------------------------------------------------------------------------
    def tape_text(
        self, h: Handlers, inbound: Inbound, text: str, *, from_command: bool = False
    ) -> str | None:
        st = self._state(h.db, inbound.person.id)
        if st is None:
            return None
        plan = weekplan.of(h.db, inbound.person.id)
        parsed = parse_tape(text, plan.tape_unit)
        if parsed is None:
            # `from_command` is `/measure <words>`: the words are the argument to a command
            # that exists for exactly this, so unlike ambient free text there is no "something
            # else" for them to be, and no ambiguity to fall through on - they always get a
            # reply, same as any other unreadable /measure argument always has.
            if not from_command and convstate.asks_for_something_else(text):
                # "Send me the menu for tomorrow", typed on a Sunday afternoon with the 10:00
                # tape question still open, was answered "I need centimetres, like 84 102 for
                # waist then hip" - and the question he asked was gone. The miss counter below
                # does not save you: the FIRST miss costs a real message, and the first one is
                # what people notice. `asks_for_something_else` now also catches a message
                # shaped like a question - "can I swap Wednesday dinner?" - not just one naming
                # Tiffin's own nouns: that shape is the tell, not the digits,
                # because "about the same as last week" has no digits either and has to stay a
                # tape answer.
                #
                # A flow claims a message only when it could be an answer to its own question.
                # "About the same as last week" could be; a message naming the menu, or shaped
                # like a question about something else entirely, could not.
                return None
            # two goes, then get out of the way. Bouncing every message with "I need
            # centimetres" until numbers arrived meant free text - the main way into this
            # system - stopped working, and a real off-plan note typed on a Sunday afternoon
            # went nowhere at all.
            misses = int(st.get("misses") or 0) + 1
            if misses >= TAPE_MISSES:
                self.disarm(h.db, inbound.person.id)
                h.outbox.reply(
                    inbound.person,
                    "I will stop asking about the tape for now. Send <code>/measure 84 102</code> "
                    "when you have it.",
                )
                return "handled"
            self._remember_miss(h.db, inbound.person.id, st, misses)
            h.outbox.reply(
                inbound.person,
                reply_like(plan)
                if plan.tape_sites
                else "I need centimetres, like <code>84 102</code> for waist then hip. "
                "Send <code>skip</code> to leave it for now.",
            )
            return "handled"
        day = dt.date.fromisoformat(str(st["day"]))
        stored: list[str] = []
        for site in body.TAPE_SITES:
            if site not in parsed:
                continue
            try:
                body.record_tape(h.db, inbound.person.id, day, site, parsed[site])
            except ValueError as e:
                h.outbox.reply(inbound.person, str(e))
                return "handled"
            stored.append(f"{body.TAPE_WORDS.get(site, site)} {in_unit(parsed[site], plan)}")
        self.disarm(h.db, inbound.person.id)
        h.outbox.reply(inbound.person, "Recorded: " + ", ".join(stored) + ".")
        return "handled"

    def skip_text(self, h: Handlers, inbound: Inbound, text: str) -> str | None:
        if self._state(h.db, inbound.person.id) is None:
            return None
        if text.strip().lower() not in ("skip", "later", "no", "not today"):
            return None
        self.disarm(h.db, inbound.person.id)
        h.outbox.reply(inbound.person, "Left for now. Send <code>/measure</code> when you have it.")
        return "handled"

    def cycle_tap(self, h: Handlers, inbound: Inbound, parts: list[str]) -> str:
        if not parts:
            return "ignored"
        # deliberately no clear_buttons: a mis-tap could never be corrected, and the phase is
        # a ledger so a second tap supersedes the first rather than editing it
        try:
            body.record_cycle_phase(h.db, inbound.person.id, self.clock.today_local(), parts[0])
        except ValueError:
            return "ignored"
        label = next((lbl for lbl, code in PHASE_LABELS if code == parts[0]), parts[0])
        h.outbox.reply(
            inbound.person,
            f"Noted: {esc(label.lower())}. It only ever explains weight noise. "
            # "on this message" - and this is a NEW message with no keyboard on it; the phase
            # buttons are on the body_check message above. A person told to tap something that
            # is not there reads it as broken.
            "Tap a different one on that message if that was wrong.",
        )
        return "handled"

    # -- commands ------------------------------------------------------------------------
    def period_tap(self, h: Handlers, inbound: Inbound, parts: list[str]) -> str:
        """k:prd:<day> - her period started that day; k:prd:no - not yet (the weekly check)."""
        if not parts:
            return "ignored"
        person = inbound.person
        today = self.clock.today_local()
        if parts[0] == "no":
            h.clear_buttons(inbound)
            h.outbox.reply(person, "Noted. Next week's check asks again.")
            return "handled"
        try:
            day = dt.date.fromisoformat(parts[0])
        except ValueError:
            return "ignored"
        if day > today or (today - day).days > 40:
            h.outbox.reply(
                person,
                "That button is from an older check, so nothing was recorded. "
                "The next weekly check asks again.",
            )
            return "handled"
        with h.db.tx():
            body.record_period_start(h.db, person.id, day)
        h.clear_buttons(inbound)
        now = body.cycle_on(h.db, person.id, today)
        where = f" Today is day {now.day} of your cycle." if now is not None else ""
        h.outbox.reply(person, f"Noted: your period started on {day:%a %d %b}.{where}")
        return "handled"

    def cmd_measure(self, h: Handlers, inbound: Inbound, arg: str) -> str:
        """`/measure` alone arms the flow; `/measure 84 102` records straight away."""
        if self.busy_with_something_else(h.db, inbound.person.id):
            h.outbox.reply(
                inbound.person,
                "Answer the question above first and I will ask for the tape straight after.",
            )
            return "handled"
        if arg.strip():
            self.arm(h.db, inbound.person.id, self.clock.today_local())
            return self.tape_text(h, inbound, arg, from_command=True) or "handled"
        self.arm(h.db, inbound.person.id, self.clock.today_local())
        h.outbox.reply(inbound.person, self.prompt_text(h.db, inbound.person))
        return "handled"


def install(handlers: Handlers, *, clock: Clock = DEFAULT_CLOCK) -> MeasuresFlow:
    flow = MeasuresFlow(clock=clock)
    handlers.register_command("measure", flow.cmd_measure)
    handlers.register_callback("cyc", flow.cycle_tap)
    handlers.register_callback("prd", flow.period_tap)
    # skip first: "no" is a valid answer to the prompt and must not be read as a measurement
    handlers.text_handlers.insert(0, flow.skip_text)
    handlers.text_handlers.insert(1, flow.tape_text)
    return flow


__all__ = ["FLOW", "PHASE_LABELS", "MeasuresFlow", "install", "parse_tape"]
