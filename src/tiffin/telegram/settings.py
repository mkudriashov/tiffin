"""`/set` - change an answer you gave during onboarding.

The interview asks its questions once, and most of the answers could never be changed again:
the only route was `/onboard restart`, which throws away every other answer to correct one.
Someone whose training moves to mornings, who wants the evening check-in earlier, who changes
their mind about a food, or who mistyped their height or their date of birth, had a choice
between living with it and redoing the whole interview.

Everything the interview asks is now here except the weight, which comes off the scale and is
never typed. `test_every_profile_column_is_reachable_or_declared` is what keeps that true.

Every field here is validated by the interview's OWN parser and rendered by its own prompt, so
`/set` and the interview cannot drift: adding a question to STEPS and forgetting it here shows
up as a failing test rather than as a setting nobody can reach.

`profiles` is a ledger, so a change is a new row effective today and last week's numbers still
say what they were at the time.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .. import targets as targets_mod
from ..clock import DEFAULT_CLOCK, Clock
from .render import check_text, esc

if TYPE_CHECKING:
    from .handlers import Handlers
    from .poller import Inbound


@dataclass(frozen=True)
class Field:
    """One thing a person can change, and where it lands."""

    name: str  # what they type after /set
    step: str  # the onboarding step whose parser and prompt this reuses
    column: str  # the profiles column it writes
    describe: str  # a short line for the listing
    encode: Any = None  # value -> what the column holds, when they differ
    #: whether `+ x` / `- x` may edit it in place. Only free-text lists: a fixed set of
    #: choices has to go back through its own parser or `+ brunch` would be accepted
    #: silently and then quietly dropped on the next read.
    amend: bool = False


def _json_list(value: Any) -> str:
    return json.dumps(value or [])


def _segments(value: Any) -> str:
    from ..onboarding import SEGMENTS  # noqa: PLC0415

    return json.dumps(SEGMENTS[str(value)])


def _variety(value: Any) -> str:
    return json.dumps({"all": str(value)})


#: ordered the way someone would look for them, not the way the interview asks them
FIELDS: tuple[Field, ...] = (
    Field("height", "hcm", "height_cm", "your height in cm"),
    Field("birthday", "dob", "birth_date", "your date of birth"),
    Field("sex", "sex", "sex", "which energy formula to use"),
    Field("activity", "act", "activity_factor", "how active you are outside training"),
    Field("goal", "gwt", "goal_weight_kg", "goal weight in kg"),
    Field("goal-date", "gdt", "goal_date", "the date you want to reach it"),
    Field("rate", "rate", "rate_pct_per_week", "how fast to lose, as a share of bodyweight a week"),
    Field("max-loss", "mloss", "max_loss_kg_week", "the most to lose in one week, in kg"),
    Field("floor", "floor", "kcal_floor", "the calorie floor targets never go below"),
    Field("morning", "morn", "morning_time", "when the morning message arrives"),
    Field("rhythm", "rhy", "rhythm", "how often I message you: light, daily or weekly"),
    Field("evening", "conf", "confirm_time", "when the evening check-in arrives"),
    Field("quiet-start", "qst", "quiet_start", "when messages stop for the night"),
    Field("quiet-end", "qend", "quiet_end", "when they start again"),
    Field("steps", "steps", "steps_floor", "your daily steps floor"),
    Field("meals", "meals", "meals_per_day", "meals a day"),
    Field("pre-bed", "pbed", "pre_bed_protein", "slow protein before bed"),
    Field(
        "plan",
        "pln",
        "planning",
        "how you plan: auto (menus made for you), choose (you pick each Sunday) or off",
    ),
    Field("cook-days", "segs", "segment_days", "your cook cadence", _segments),
    Field("freeze", "frz", "freezes", "whether you freeze what you batch cook"),
    Field("skip", "skip", "skip_mode", "what Skip does to the rest of the day"),
    Field("choices", "chs", "choices", "choose weekday meals each Sunday: off, 3 or 4 a meal"),
    Field("batch", "batch", "batch_slots", "which meals you batch cook", _json_list),
    Field("variety", "vary", "variety_json", "how much the menu repeats", _variety),
    Field("avoid", "refu", "refused_foods", "foods you refuse", _json_list, amend=True),
    Field("likes", "like", "liked_foods", "foods you like to see often", _json_list, amend=True),
    Field(
        "pantry",
        "pant",
        "pantry_json",
        "staples never on the shopping list",
        _json_list,
        amend=True,
    ),
    Field("tone", "tone", "tone", "how I talk to you"),
    Field("cycle", "cyc", "cycle_tracking", "whether cycle phase is tracked"),
)
BY_NAME = {f.name: f for f in FIELDS}


def optional_steps() -> frozenset[str]:
    """Steps the interview itself let you skip, so they can be cleared again afterwards.

    A goal weight set by mistake could never be taken back: the parser wants a number between
    30 and 250, so no value meant "no goal", and the pace arithmetic went on running off it.
    """
    from ..onboarding import STEPS  # noqa: PLC0415

    return frozenset(s.id for s in STEPS if s.optional)


#: answers that genuinely cannot be edited, and what to do instead. Named so the listing can
#: say so rather than leaving someone hunting for a setting that is not there.
FIXED = {
    "weight": "the scale is its source - it is never typed",
    "kitchen": "use /kit",
    "training week": "use /week to record or read it",
    "name": "use /name",
    "cloud processing": "use /consent",
}


class SettingsFlow:
    def __init__(self, *, clock: Clock = DEFAULT_CLOCK):
        self.clock = clock

    def _listing(self, h: Handlers, inbound: Inbound) -> str:
        lines = ["<b>What you can change</b>"]
        lines += [f"<code>/set {f.name}</code> - {f.describe}" for f in FIELDS]
        lines.append("<b>Elsewhere</b>")
        lines += [f"{k}: {v}" for k, v in FIXED.items()]
        lines.append("Example: <code>/set evening 21:00</code>")
        return check_text("\n".join(lines), kind="settings", max_lines=40)

    def cmd_set(self, h: Handlers, inbound: Inbound, arg: str) -> str:
        from ..onboarding import STEP_BY_ID, Invalid  # noqa: PLC0415

        person = inbound.person
        name, _, value = arg.strip().partition(" ")
        name = name.strip().lower().replace("_", "-")
        if not name:
            h.outbox.reply(person, self._listing(h, inbound))
            return "handled"
        # `/set` prints "training week: use /week" and "cloud processing: use /consent" under
        # Elsewhere, and then partitioning on the first space looked up "training" and answered
        # "I do not know a setting called training" - forgetting the hint it printed seconds
        # earlier. The four one-word keys answered properly, which is why it stayed hidden. A
        # whole argument that IS one of those names wins, before the single word is tried.
        whole = arg.strip().lower()
        if whole in FIXED and whole not in BY_NAME:
            h.outbox.reply(person, f"{esc(whole)}: {esc(FIXED[whole])}.")
            return "handled"
        field = BY_NAME.get(name)
        if field is None:
            hint = FIXED.get(name)
            h.outbox.reply(
                person,
                f"{esc(name)}: {esc(hint)}."
                if hint
                else f"I do not know a setting called {esc(name)}. Send /set to see the list.",
            )
            return "handled"

        today = self.clock.today_local()
        current = targets_mod.profile_on(h.db, person.id, today)
        if current is None:
            h.outbox.reply(person, "Finish /onboard first and then you can change any answer.")
            return "handled"
        step = STEP_BY_ID[field.step]
        answers = self._answers(h, person, current)
        value = value.strip()
        if field.amend and value[:1] in ("+", "-"):
            return self._amend(h, inbound, field, value[:1], value[1:])
        if field.amend and value[:1] == "=":
            # the whole list, replaced: asked for in so many words, never by accident
            value = value[1:].strip() or "none"
        elif field.amend and value and value.lower() not in ("none", "clear", "no", "skip"):
            # "/set avoid olives" ADDS olives. Replacing the list was the reading for years, so
            # somebody avoiding chickpeas who then sent "/set avoid lentils" had chickpeas
            # quietly allowed back - the one outcome an avoid list must never have
            return self._amend(h, inbound, field, "+", value)
        if value.lower() in ("none", "clear", "-") and field.step in optional_steps():
            return self._write(h, inbound, field, None)
        if not value.strip():
            # show the question the interview would have asked, and what it is set to now
            prompt = step.prompt(answers, person) if callable(step.prompt) else step.prompt
            shown = self._current(current, field)
            how = (
                f"<code>/set {field.name} + olives</code> adds, "
                f"<code>/set {field.name} - olives</code> takes off, "
                f"<code>/set {field.name} = a, b</code> replaces the list."
                if field.amend
                else f"Send <code>/set {field.name} VALUE</code> to change it."
            )
            h.outbox.reply(
                person,
                check_text(
                    f"<b>{esc(field.name)}</b> is {esc(shown)}.\n{esc(str(prompt))}\n{how}",
                    kind="settings",
                    max_lines=20,
                ),
                keyboard=self._options(step, answers, person, field),
            )
            return "handled"
        try:
            parsed = step.parse(value.strip(), answers)
        except Invalid as e:
            h.outbox.reply(person, esc(str(e)))
            return "handled"
        return self._write(h, inbound, field, parsed)

    def _answers(self, h: Handlers, person: Any, current: Any) -> dict[str, Any]:
        """The interview's answer shape, rebuilt from the profile.

        Two steps compute their prompt and their buttons from earlier ANSWERS rather than from
        a profile row - the rate question needs the weight to turn a percentage into kilograms,
        and the calorie floor needs sex, height, age and weight to work out a BMR. Handing them
        a profile row raised KeyError('wkg') and the whole command died with "something went
        wrong": `/set rate` and `/set floor` were unusable, which are two of the three numbers
        the weekly calibration runs on.
        """
        from .. import body  # noqa: PLC0415

        seen = body.weigh_ins(h.db, person.id)
        return {
            "wkg": seen[-1][1] if seen else 80.0,
            "sex": current["sex"],
            "hcm": current["height_cm"],
            "dob": current["birth_date"],
            "gwt": current["goal_weight_kg"],
            "gdt": current["goal_date"],
        }

    def _amend(self, h: Handlers, inbound: Inbound, field: Field, sign: str, rest: str) -> str:
        """`/set avoid + olives` and `/set avoid - mushrooms`.

        Replacing the whole list was the only way to change it, so dropping one item from a
        list of fifteen meant retyping the other fourteen exactly - which is not a thing
        anyone does, so in practice a food you had stopped refusing stayed refused for ever.
        """

        from ..food import prefs  # noqa: PLC0415 (food imports the db layer)

        person = inbound.person
        wanted = [x.strip() for x in rest.replace(",", "\n").splitlines() if x.strip()]
        if not wanted:
            h.outbox.reply(
                person,
                f"Nothing to {'add' if sign == '+' else 'remove'}. "
                f"<code>/set {field.name}</code> shows what is there.",
            )
            return "handled"
        # one door for every way a list changes (food/prefs.py), folded on the singular: "no
        # chickpeas" after "chickpea" is the entry already there, not a second one. The answer
        # names what each new entry keeps out, so "fish" is seen to mean salmon, cod and tuna
        name = {"refused_foods": "avoid", "liked_foods": "likes", "pantry_json": "pantry"}[
            field.column
        ]
        changed = prefs.amend(
            h.db,
            person.id,
            name,
            add=wanted if sign == "+" else (),
            remove=wanted if sign == "-" else (),
            clock=self.clock,
            strict=False,
        )
        shown = ", ".join(changed.terms) or "empty"
        lines = [f"{esc(field.name)} is now {esc(shown)}.", esc(changed.said())]
        if name == "avoid" and changed.added:
            still = prefs.still_on_menu(h.db, person.id, self.clock.today_local())
            if still:
                lines.append(esc(prefs.still_said(still)))
        # houmous after chickpeas: asked once, with the two answers as buttons
        from .foodflow import ask_rows  # noqa: PLC0415 (foodflow imports the handlers)

        lines += [esc(prefs.ask_said(a)) for a in changed.asks[:3]]
        rows = ask_rows(changed.asks)
        h.outbox.reply(person, "\n".join(lines), keyboard=self._keyboard(rows) if rows else None)
        return "handled"

    @staticmethod
    def _keyboard(rows: list[list[tuple[str, str]]]) -> Any:
        from .api import inline_keyboard  # noqa: PLC0415

        return inline_keyboard(rows)

    def _current(self, current: Any, field: Field) -> str:
        """What it is set to, in words. A person should never be shown stored JSON."""
        raw = current[field.column]
        if raw is None:
            return "not set"
        if field.column in ("refused_foods", "liked_foods", "pantry_json"):
            try:
                return ", ".join(json.loads(str(raw) or "[]")) or "empty"
            except ValueError:
                return "empty"
        if field.column == "variety_json":
            try:
                return str(json.loads(str(raw)).get("all", "same"))
            except ValueError:
                return "same"
        if field.column == "batch_slots":
            from ..food.batch import describe, read_slots  # noqa: PLC0415

            return describe(read_slots(raw))
        if field.column == "segment_days":
            names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
            try:
                groups = json.loads(str(raw))
            except ValueError:
                return str(raw)
            # the stored code ("mf") is internal; show the days
            return " then ".join("-".join(names[d] for d in (g[0], g[-1])) for g in groups)
        if field.column == "sex":
            return {"m": "male", "f": "female"}.get(str(raw), str(raw))
        if field.column in ("pre_bed_protein", "cycle_tracking", "freezes"):
            return "yes" if int(raw) else "no"
        return str(raw)

    def _options(self, step: Any, current: Any, person: Any, field: Field) -> dict | None:
        from .api import inline_keyboard  # noqa: PLC0415

        raw: Any = step.options(current, person) if callable(step.options) else step.options
        opts: list[tuple[str, str]] = list(raw) if raw else []
        if not opts:
            return None
        rows = [[(label, f"k:set:{field.name}:{value}")] for label, value in opts[:6]]
        return inline_keyboard(rows)

    def tap(self, h: Handlers, inbound: Inbound, parts: list[str]) -> str:
        from ..onboarding import STEP_BY_ID, Invalid  # noqa: PLC0415

        if len(parts) < 2:
            return "ignored"
        field = BY_NAME.get(parts[0])
        if field is None:
            return "ignored"
        current = targets_mod.profile_on(h.db, inbound.person.id, self.clock.today_local())
        if current is None:
            return "ignored"
        try:
            parsed = STEP_BY_ID[field.step].parse(":".join(parts[1:]), dict(current))
        except Invalid as e:
            h.outbox.reply(inbound.person, esc(str(e)))
            return "handled"
        return self._write(h, inbound, field, parsed)

    def _write(
        self, h: Handlers, inbound: Inbound, field: Field, parsed: Any, *, quiet: bool = False
    ) -> str:
        person = inbound.person
        start = write_setting(h.db, person.id, field, parsed, clock=self.clock)
        if quiet:  # the caller has a better sentence for what just happened
            return "handled"
        if parsed is None:
            h.outbox.reply(person, f"{esc(field.name)} cleared.")
            return "handled"
        shown = (", ".join(parsed) or "empty") if isinstance(parsed, list) else str(parsed)
        tail = ""
        if field.column in PLAN_COLUMNS:
            tail = (
                f" From {start:%a} {start.day} {start:%b}: this week stays as it was planned. "
                "How you plan, on the page's More, can start it tomorrow instead."
            )
        elif field.name in ("rate", "max-loss", "floor", "goal", "goal-date", "activity"):
            tail = " Next Sunday's targets will use it."
        elif field.name in ("variety", "avoid", "likes", "pantry"):
            tail = " The next menu will use it."
        h.outbox.reply(person, f"{esc(field.name)} is now {esc(shown)}.{tail}")
        return "handled"


#: how someone plans (food/planning.py): a change is booked from next Monday unless asked for
#: sooner, because the week in hand may already be bought and cooked
PLAN_COLUMNS = frozenset({"planning", "segment_days", "batch_slots", "freezes", "choices"})


def write_setting(
    db: Any,
    person_id: int,
    field: Field,
    parsed: Any,
    *,
    clock: Clock,
    starts: str = "monday",
    record_answer: bool = True,
) -> dt.date:
    """One setting, as a new profiles row: every other answer carried over, so the ledger says
    what changed and when. `/set` in the bot and `tiffin set` from the shell both write through
    here. Returns the day it counts from: today, or - for how someone plans - next Monday, or
    tomorrow when `starts` says so."""
    from ..food import planning  # noqa: PLC0415 (food imports the telegram layer's helpers)

    today = clock.today_local()
    if targets_mod.profile_on(db, person_id, today) is None:
        raise LookupError("no profile yet")
    if parsed is None:
        # a list is never NULL in profiles: cleared is the empty list. "/set likes =", as
        # the help says, died on NOT NULL and said nothing
        stored = field.encode([]) if field.amend else None
    else:
        stored = field.encode(parsed) if field.encode else parsed
    changes: dict[str, object] = {field.column: stored}
    if field.column == "choices":
        # a number of dishes is choosing, and none is the menu built as before
        changes["planning"] = "choose" if stored else "auto"
    if field.column not in PLAN_COLUMNS:
        targets_mod.change_profile(db, person_id, changes, today=today)
        if record_answer:
            db.insert(
                "onboarding_answers",
                person_id=person_id,
                step=field.step,
                answer_json=json.dumps(parsed),
            )
        return today
    effective = (
        today + dt.timedelta(days=1) if starts == "tomorrow" else planning.next_monday(today)
    )
    targets_mod.change_profile(db, person_id, changes, today=today, effective=effective)
    if record_answer:
        db.insert(
            "onboarding_answers",
            person_id=person_id,
            step=field.step,
            answer_json=json.dumps(parsed),
        )
    return effective


def install(handlers: Handlers, *, clock: Clock = DEFAULT_CLOCK) -> SettingsFlow:
    flow = SettingsFlow(clock=clock)
    handlers.register_command("set", flow.cmd_set)
    handlers.register_callback("set", flow.tap)
    return flow


__all__ = ["BY_NAME", "FIELDS", "FIXED", "Field", "SettingsFlow", "install", "write_setting"]
