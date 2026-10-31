"""Collect and revise a person's profile through a resumable Telegram interview."""

from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from . import body, targets
from .clock import DEFAULT_CLOCK, Clock, isoday, parse_day
from .config import Config
from .db import Db
from .energy import age_on, bmr_mifflin, pace_check
from .guardrails import MAX_LOSS_KG_WEEK
from .persons import Person, PersonRegistry
from .telegram import render as r
from .telegram.api import Keyboard, inline_keyboard
from .telegram.outbox import Outbox

if TYPE_CHECKING:
    from .telegram.handlers import Handlers
    from .telegram.poller import Inbound

FLOW = "onboarding"
PROCESSING_CONSENT_VERSION = "v3"
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
KINDS = [
    "climb",
    "ropes",
    "ride",
    "run",
    "strength",
    "mobility",
    "swim",
    "class",
    "walk",
    "other",
    "rest",
]
SEGMENTS = {
    "mf": [[0, 1, 2, 3, 4]],
    "mw": [[0, 1, 2], [3, 4]],
    "daily": [[0], [1], [2], [3], [4]],
}

Answers = dict[str, Any]


class Invalid(ValueError):
    """User-facing validation message."""


@dataclass(frozen=True)
class Step:
    id: str
    prompt: str | Callable[[Answers, Person], str]
    kind: str  # buttons | text
    parse: Callable[[str, Answers], Any]
    options: list[tuple[str, str]] | Callable[[Answers, Person], list[tuple[str, str]]] | None = (
        None
    )
    applies: Callable[[Answers, Person], bool] = field(default=lambda a, p: True)
    optional: bool = False
    hint: str = ""


# ---------------------------------------------------------------- parsers
def _number(lo: float, hi: float, unit: str) -> Callable[[str, Answers], float]:
    def parse(raw: str, answers: Answers) -> float:
        m = re.search(r"-?\d+(?:[.,]\d+)?", raw)
        if not m:
            raise Invalid(f"Send a number in {unit}.")
        value = float(m.group(0).replace(",", "."))
        if not lo <= value <= hi:
            raise Invalid(f"That is outside {lo:g}-{hi:g} {unit}.")
        return value

    return parse


def _date(raw: str, answers: Answers) -> str:
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", raw)
    if not m:
        raise Invalid("Send a date as YYYY-MM-DD.")
    try:
        d = dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        raise Invalid("That date does not exist.") from None
    return d.isoformat()


def _birth_date(raw: str, answers: Answers) -> str:
    value = _date(raw, answers)
    year = int(value[:4])
    if not 1930 <= year <= 2012:
        raise Invalid("Birth year must be between 1930 and 2012.")
    return value


#: the key under which the interview hands its own clock's date to the step functions. Never
#: stored: `_dated` passes a copy. `dt.date.today()` here read the machine's clock, so a goal
#: date's "reachable" and a birth year's age moved with the real calendar under a frozen one.
TODAY_KEY = "__today__"


def _today(answers: Answers) -> dt.date:
    got = answers.get(TODAY_KEY) if isinstance(answers, dict) else None
    return parse_day(str(got)) if got else DEFAULT_CLOCK.today_local()


def _future_date(raw: str, answers: Answers) -> str:
    """A goal date, which must actually be in the future.

    It did no future check at all despite the name, so mistyping the year - the easiest typo
    there is in a YYYY-MM-DD field early in a year - was accepted, and the very next screen,
    where they choose how fast to lose weight, read "Your goal needs inf kg/week".
    """
    value = _date(raw, answers)
    if parse_day(value) <= _today(answers):
        raise Invalid("That date has already passed; pick one in the future.")
    return value


def _time(raw: str, answers: Answers) -> str:
    m = re.search(r"([01]?\d|2[0-3])[:.]([0-5]\d)", raw)
    if not m:
        raise Invalid("Send a time as HH:MM.")
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def _choice(*values: str) -> Callable[[str, Answers], str]:
    def parse(raw: str, answers: Answers) -> str:
        v = raw.strip().lower()
        if v not in values:
            raise Invalid("Use one of the buttons.")
        return v

    return parse


def _float_choice(lo: float, hi: float) -> Callable[[str, Answers], float]:
    """A button step that also accepts a typed number - bounded like the schema's CHECK.

    Typing is a supported way to answer these, and the unbounded version accepted anything:
    "1.5" at the rate question passed here and then failed fifteen questions later against
    `CHECK (rate_pct_per_week BETWEEN 0.0 AND 1.0)`, inside `_finish`'s single transaction.
    That rolled back the profile, first weigh-in, training plan and consent at once -
    twenty-four questions answered and nothing kept - and it was reported at the consent
    screen, the one answer that had nothing to do with it.
    """

    def parse(raw: str, answers: Answers) -> float:
        try:
            value = float(raw.strip().rstrip("%"))
        except ValueError:
            raise Invalid("Use one of the buttons, or type a plain number.") from None
        if not lo <= value <= hi:
            raise Invalid(f"That is outside {lo:g}-{hi:g}. Use one of the buttons.")
        return value

    return parse


def _int_choice(lo: int, hi: int) -> Callable[[str, Answers], int]:
    def parse(raw: str, answers: Answers) -> int:
        try:
            value = int(float(raw.strip()))
        except ValueError:
            raise Invalid("Use one of the buttons, or type a plain number.") from None
        if not lo <= value <= hi:
            raise Invalid(f"That is outside {lo:g}-{hi:g}. Use one of the buttons.")
        return value

    return parse


def _choices(raw: str, answers: Answers) -> int:
    """How many dishes each weekday meal is offered: off, 3 or 4 (2 is allowed when typed)."""
    word = raw.strip().lower()
    if word in ("off", "no", "none", "0"):
        return 0
    try:
        value = int(float(word))
    except ValueError:
        raise Invalid("Use one of the buttons: off, 3 or 4.") from None
    if value not in (2, 3, 4):
        raise Invalid("Use one of the buttons: off, 3 or 4.")
    return value


def _list(raw: str, answers: Answers) -> list[str]:
    if raw.strip().lower() in ("none", "no", "-", "skip"):
        return []
    items = [x.strip() for x in re.split(r"[,\n;]+", raw) if x.strip()]
    if len(items) > 40:
        raise Invalid("That is more than 40 items; keep the main ones.")
    return items[:40]


_PLAN_ITEM = re.compile(
    r"(?P<day>mon|tue|wed|thu|fri|sat|sun)\w*\s+(?P<kind>" + "|".join(KINDS) + r")\w*"
    r"(?:\s+(?P<min>\d{1,3})\s*(?:min|m)?)?(?:\s*(?:at|@)\s*(?P<time>[0-2]?\d[:.][0-5]\d))?",
    re.IGNORECASE,
)
_DAY_WORD = re.compile(r"\b(mon|tue|wed|thu|fri|sat|sun)", re.IGNORECASE)


KIT = (
    ("oven", "Oven"),
    ("hob", "Hob"),
    ("air_fryer", "Air fryer"),
    ("microwave", "Microwave"),
    ("slow_cooker", "Slow cooker"),
    ("pressure_cooker", "Pressure cooker"),
    ("blender", "Blender"),
    ("grill", "Grill"),
)
KIT_CODES = {code for code, _ in KIT}


def parse_kit(raw: str, answers: Answers) -> list[str]:
    """What they can cook with, from a comma-separated list or the words themselves.

    A menu that says "roast for 40 minutes" is no use to someone cooking in an air fryer, and
    the generator had no way of knowing which they had. Unknown words are kept as written:
    the person knows their own kitchen better than this list does.
    """
    if raw.strip().lower() in ("none", "-", "nothing", "no", "skip"):
        return []
    found: list[str] = []
    for chunk in re.split(r"[,;]|\band\b|\bplus\b", raw.lower()):
        piece = chunk.strip(" .&+")
        if not piece:
            continue
        known = [c for c, label in KIT if c.replace("_", " ") in piece or label.lower() in piece]
        if known:
            found.extend(known)
        elif len(piece) <= 30:
            # not on the list, kept as written: they know their own kitchen better than it does
            found.append(re.sub(r"^(a|an|the|my)\s+", "", piece))
    return sorted(dict.fromkeys(found))[:12]


def parse_plan(raw: str, answers: Answers) -> list[dict[str, Any]]:
    """Parse a weekly training plan into typed sessions and rest days."""
    # "I do not have a fixed week yet" was refused every way anyone would say it, at the one
    # mandatory free-text question in the interview - and the prompt's own "Unnamed days are
    # rest" implies naming nothing should be allowed. Their realistic escapes were to tap
    # Later (which leaves them with no profile at all) or to invent a week they do not train.
    if raw.strip().lower() in ("none", "no", "-", "skip", "rest", "nothing", "not yet", "n/a"):
        return [
            {"weekday": i, "kind": "rest", "target_minutes": 0, "start_time": None}
            for i in range(7)
        ]
    found: dict[int, list[dict[str, Any]]] = {}
    for m in _PLAN_ITEM.finditer(raw):
        wd = DAYS.index(m.group("day").lower()[:3])
        kind = m.group("kind").lower()
        minutes = int(m.group("min") or (0 if kind == "rest" else 60))
        if minutes > 600:
            raise Invalid("A session over 600 minutes cannot be right.")
        time = m.group("time").replace(".", ":") if m.group("time") else None
        if time and len(time.split(":")[0]) == 1:
            time = "0" + time
        session = {"weekday": wd, "kind": kind, "target_minutes": minutes, "start_time": time}
        if kind == "rest":
            found[wd] = []  # naming a day as rest clears it rather than adding to it
        else:
            found.setdefault(wd, []).append(session)
    unreadable = _unreadable_pieces(raw)
    if unreadable:
        raise Invalid(
            "I could not read: "
            + "; ".join(unreadable[:3])
            + ". I know these kinds: "
            + ", ".join(KINDS[:-1])
            + ". Write it like: Tue ride 60. Use 'other' for anything else, so nothing turns "
            "into a rest day by accident."
        )
    if not found:  # a week explicitly named as rest is a plan; an unreadable one is not
        raise Invalid(
            "I could not read a session. Example: Mon climb 90 at 18:30, Wed ride 60, Sat ropes "
            "120. Send 'none' if you have no fixed week yet."
        )
    week: list[dict[str, Any]] = []
    for i in range(7):
        sessions = found.get(i) or []
        week += sessions or [
            {"weekday": i, "kind": "rest", "target_minutes": 0, "start_time": None}
        ]
    return week


def _unreadable_pieces(raw: str) -> list[str]:
    """Comma-separated pieces that name a day but no session the parser understood."""
    out: list[str] = []
    for piece in re.split(r"[\n;,]+", raw):
        text = piece.strip()
        if not text or not _DAY_WORD.search(text):
            continue
        if not _PLAN_ITEM.search(text):
            out.append(text[:40])
    return out


def plan_lines(plan: list[dict[str, Any]]) -> list[str]:
    out = []
    for s in plan:
        if s["kind"] == "rest":
            out.append(f"{DAYS[s['weekday']].title()} rest")
        else:
            at = f" at {s['start_time']}" if s.get("start_time") else ""
            out.append(f"{DAYS[s['weekday']].title()} {s['kind']} {s['target_minutes']} min{at}")
    return out


# ---------------------------------------------------------------- prompts that depend on answers
def _rate_options(a: Answers, p: Person) -> list[tuple[str, str]]:
    return [("0.5 %/week (gentle)", "0.5"), ("0.7 %/week", "0.7"), ("1.0 %/week (fast)", "1.0")]


def _max_loss_prompt(a: Answers, p: Person) -> str:
    """The ceiling in kilograms, which is a different question from the rate in per cent.

    The rate is a share of BODYWEIGHT, so what it comes to in kilograms depends on what somebody
    weighs: 1 % is 1.0 kg a week at 100 kg and 0.58 kg at 58 kg. This is the number the rate is
    capped at whatever the arithmetic says, and it is also what "losing too fast" is measured
    against, so raising it is a decision about both - a person who chooses 1 kg is not nagged
    of 1kg/week if needed").
    """
    w = float(a["wkg"])
    rate = float(a.get("rate") or 0.7)
    would = w * rate / 100.0
    lines = [
        "The most to lose in one week, in kilograms.",
        f"At {w:.0f} kg your {rate:g} % rate works out at {would:.2f} kg a week.",
    ]
    if would > MAX_LOSS_KG_WEEK:
        lines.append(f"The usual ceiling of {MAX_LOSS_KG_WEEK:g} kg would hold it back.")
    lines.append("Faster than this and Tiffin puts calories back on its own.")
    return "\n".join(lines)


def _rate_prompt(a: Answers, p: Person) -> str:
    """Offer goal rates that remain within the person's safe weekly limit."""
    w = float(a["wkg"])
    rates = [min(w * pct / 100, MAX_LOSS_KG_WEEK) for pct in (0.5, 0.7, 1.0)]
    lines = ["Rate of loss, as a share of bodyweight per week. Type 0 to maintain."]
    for pct, kg in zip((0.5, 0.7, 1.0), rates, strict=True):
        lines.append(f"{pct:g} % = {kg:.2f} kg/week")
    if a.get("gwt") and a.get("gdt"):
        # the fastest rate the buttons actually offer, which is what "reachable" has to mean
        v = pace_check(w, float(a["gwt"]), parse_day(a["gdt"]), _today(a), max(rates))
        if v.weeks_left <= 0:
            # never format an infinity at the moment they are choosing how fast to lose weight
            lines.append("Your goal date has already passed; send /onboard restart to change it.")
        elif v.feasible:
            lines.append(f"Your goal needs {v.required_kg_week:.2f} kg/week; that is reachable.")
        else:
            lines.append(
                f"Your goal needs {v.required_kg_week:.2f} kg/week, above the 0.8 cap; "
                f"at the cap you reach it around {v.projected_date}."
            )
    lines.append("The cap of 0.8 kg/week always wins.")
    return "\n".join(lines)


def _floor_options(a: Answers, p: Person) -> list[tuple[str, str]]:
    age = _today(a).year - int(a["dob"][:4])
    bmr = bmr_mifflin(a["sex"], float(a["wkg"]), float(a["hcm"]), age)
    base = int(round(bmr / 50.0) * 50)
    # label the value it actually sets. The lowest button read "1050 kcal" and stored 1200,
    # on the one setting whose whole purpose is "Tiffin never goes below this", and
    # nothing afterwards ever showed the floor, so the discrepancy was invisible.
    low = max(base - 200, 1200)
    lowest = f"{low} kcal" + (" (the lowest I will go)" if low > base - 200 else "")
    return [
        (f"{base} kcal (about your BMR)", str(base)),
        (f"{base + 200} kcal", str(base + 200)),
        (lowest, str(low)),
    ]


def _floor_prompt(a: Answers, p: Person) -> str:
    return "Calorie floor: Tiffin never sets a target below this, whatever the maths says."


# ---------------------------------------------------------------- the steps
def _batch_slots(raw: str, answers: Answers) -> list[str]:
    """Which meals come out of one cook. Free text as well as the buttons, because "all
    except breakfast" is how people actually say this."""
    from .food.batch import BadSlots, parse_slots  # noqa: PLC0415

    try:
        return list(parse_slots(raw))
    except BadSlots as e:
        raise Invalid(str(e)) from None


STEPS: list[Step] = [
    Step(
        "sex",
        "Sex (for the energy formula).",
        "buttons",
        _choice("m", "f"),
        [("Male", "m"), ("Female", "f")],
    ),
    Step("dob", "Date of birth, YYYY-MM-DD.", "text", _birth_date),
    Step("hcm", "Height in cm.", "text", _number(120, 230, "cm")),
    Step(
        "wkg",
        "Current weight in kg (this morning's scale if you have it).",
        "text",
        _number(30, 250, "kg"),
    ),
    Step(
        "act",
        "Activity outside training.",
        "buttons",
        _float_choice(1.1, 2.0),  # the schema CHECK, so a typo lands on this question
        [
            ("Desk, little walking", "1.2"),
            ("Light: on feet some of the day", "1.375"),
            ("Moderate: active most days", "1.55"),
            ("Very active / physical job", "1.725"),
        ],
    ),
    Step("gwt", "Goal weight in kg (or Skip).", "text", _number(30, 250, "kg"), optional=True),
    Step(
        "gdt",
        "Goal date, YYYY-MM-DD (or Skip).",
        "text",
        _future_date,
        optional=True,
        applies=lambda a, p: a.get("gwt") is not None,
    ),
    Step("rate", _rate_prompt, "buttons", _float_choice(0.0, 1.0), _rate_options),
    Step(
        "mloss",
        _max_loss_prompt,
        "buttons",
        _float_choice(0.25, 1.0),
        [("0.5 kg", "0.5"), ("0.8 kg", "0.8"), ("1 kg", "1.0")],
        optional=True,
        hint="Skip keeps 0.8 kg, which is the usual ceiling.",
    ),
    Step("floor", _floor_prompt, "buttons", _int_choice(1000, 4000), _floor_options),
    Step(
        "morn",
        "Morning message time.",
        "buttons",
        _time,
        [("06:30", "06:30"), ("07:00", "07:00"), ("07:30", "07:30"), ("08:00", "08:00")],
        hint="Or type a time as HH:MM.",
    ),
    Step(
        "rhy",
        "How often should I message you?",
        "buttons",
        _choice("light", "daily", "weekly"),
        [
            ("Once a week (suggested)", "weekly"),
            ("Light", "light"),
            ("Every morning and evening", "daily"),
        ],
        hint="Once a week: one Sunday check-in for the whole week, and a morning message only "
        "when the day's food is new. Light: the same, with a check-in every evening.",
    ),
    Step(
        "skip",
        "When you skip a meal, what should happen to the rest of that day?",
        "buttons",
        _choice("share", "drop"),
        [
            ("Share its calories over the rest", "share"),
            ("Just drop it, the rest as planned", "drop"),
        ],
        hint="Share: the other meals grow to keep the day on target, and so does their shopping. "
        "Drop: the other meals stay as planned, and the day comes in under target.",
    ),
    Step(
        "chs",
        "Each Sunday, choose next week's weekday meals from a few dishes for each meal?",
        "buttons",
        _choices,
        [("No, build my menu", "0"), ("3 dishes a meal", "3"), ("4 dishes a meal", "4")],
        hint="Each Sunday you get 3 or 4 dishes for every weekday meal, each sized so the day "
        "still adds up whichever you pick. Your picks become the week's menu and its "
        "shopping list.",
        applies=lambda a, p: a.get("pln") != "off",
    ),
    Step(
        "conf",
        "Evening check-in time (ate as planned? session?).",
        "buttons",
        _time,
        [("20:00", "20:00"), ("20:30", "20:30"), ("21:00", "21:00"), ("21:30", "21:30")],
        hint="Or type a time as HH:MM.",
    ),
    Step(
        "qst",
        "Quiet hours start (no messages after this).",
        "buttons",
        _time,
        [("22:00", "22:00"), ("22:30", "22:30"), ("23:00", "23:00")],
        hint="Or type HH:MM.",
    ),
    Step(
        "qend",
        "Quiet hours end.",
        "buttons",
        _time,
        [("06:00", "06:00"), ("06:30", "06:30"), ("07:00", "07:00"), ("07:30", "07:30")],
        hint="Or type HH:MM.",
    ),
    Step(
        "steps",
        "Daily steps floor (the page shows how far you are from it).",
        "buttons",
        _int_choice(1000, 40000),
        [("6,000", "6000"), ("8,000", "8000"), ("10,000", "10000"), ("12,000", "12000")],
    ),
    Step(
        "meals",
        "Meals per day.",
        "buttons",
        _int_choice(2, 8),
        [("3", "3"), ("4", "4"), ("5", "5")],
    ),
    Step(
        "pbed",
        "Slow protein before bed (30-40 g)?",
        "buttons",
        _choice("1", "0"),
        [("Yes", "1"), ("No", "0")],
    ),
    Step(
        "pln",
        "How do you want to plan your food?",
        "buttons",
        _choice("auto", "choose", "off"),
        [
            ("Plan my meals for me", "auto"),
            ("I choose from a few each Sunday", "choose"),
            ("Track only, no menus", "off"),
        ],
        hint="Plan: menus built to your targets, with a shopping list. Choose: each Sunday you "
        "pick every weekday meal from 3 dishes. Track only: targets and progress, no menus. "
        "How you plan, on the page's More, changes it at any time.",
    ),
    Step(
        "segs",
        "How do you cook on weekdays?",
        "buttons",
        _choice("mf", "mw", "daily"),
        [("Cook each day", "daily"), ("One batch for Mon-Fri", "mf"), ("Two batches", "mw")],
        hint="Two batches: Monday to Wednesday, then Thursday and Friday. The weekend is "
        "planned on its own.",
        applies=lambda a, p: a.get("pln") != "off",
    ),
    Step(
        "batch",
        "On a cook day, which meals do you batch cook?",
        "buttons",
        _batch_slots,
        [
            ("Lunch and dinner", "lunch dinner"),
            ("Every meal", "all"),
            ("Dinner only", "dinner"),
            ("Nothing, I cook fresh", "none"),
        ],
        hint="This is what /batch adds up: total ingredients and how many boxes to fill. "
        "You can also type it, e.g. 'all except breakfast'.",
        # only someone who batch cooks: a day of its own is cooked on the day
        applies=lambda a, p: a.get("pln") != "off" and a.get("segs") not in (None, "daily"),
    ),
    Step(
        "frz",
        "Do you freeze what you batch cook?",
        "buttons",
        _choice("1", "0"),
        [("Yes, I freeze", "1"), ("No, fridge only", "0")],
        applies=lambda a, p: (
            a.get("pln") != "off"
            and a.get("segs") not in (None, "daily")
            and bool(a.get("batch", ["lunch", "dinner"]))
        ),
    ),
    Step(
        "vary",
        "Variety between the days you cook for.",
        "buttons",
        _choice("same", "rotate", "different"),
        [
            ("Same meals every day", "same"),
            ("Rotate a few dishes", "rotate"),
            ("Different every day", "different"),
        ],
        applies=lambda a, p: a.get("pln") != "off",
    ),
    Step("refu", "Foods you refuse (comma separated, or None).", "text", _list),
    Step(
        "like",
        "Foods you like to see often (comma separated, or Skip).",
        "text",
        _list,
        optional=True,
    ),
    Step(
        "pant",
        "Pantry staples you always have (never on the shopping list; comma separated, or None).",
        "text",
        _list,
    ),
    Step(
        "kit",
        "What can you cook with? Comma separated, e.g. oven, hob, air fryer, microwave. "
        "Anything not on that list is fine too, just name it.",
        "text",
        parse_kit,
        hint="This decides whether a recipe can say 'air fry 12 minutes' or has to say 'roast 40'.",
    ),
    Step(
        "plan",
        "Your training week, one item per session, e.g.\n"
        "Mon climb 90 at 18:30, Wed ride 60, Sat ropes 120\n"
        "Kinds: climb ropes ride run strength mobility swim class walk other rest. "
        "Unnamed days are rest, and 'none' is fine if you have no fixed week yet.",
        "text",
        parse_plan,
    ),
    Step(
        "tone",
        "How should I talk to you?",
        "buttons",
        _choice("blunt", "neutral", "warm"),
        [("Blunt", "blunt"), ("Neutral", "neutral"), ("Warm", "warm")],
    ),
    Step(
        "cyc",
        "Track menstrual cycle phase? Used only to read weight noise; never to change calories.",
        "buttons",
        _choice("1", "0"),
        [("Yes", "1"), ("No", "0")],
        applies=lambda a, p: a.get("sex") == "f",
    ),
    Step(
        "shops",
        "When do you usually shop for weekday meals?",
        "buttons",
        _choice("none", "mon", "twice"),
        [("No set days", "none"), ("Monday", "mon"), ("Monday and Thursday", "twice")],
    ),
    Step(
        "measure",
        "Would you like a weekly measurement reminder?",
        "buttons",
        _choice("none", "mon", "sun"),
        [("No reminder", "none"), ("Monday", "mon"), ("Sunday", "sun")],
    ),
    Step(
        "cons",
        lambda a, p: consent_text(),
        "buttons",
        _choice("1", "0"),
        [("I agree", "1"), ("No", "0")],
    ),
]
STEP_BY_ID = {s.id: s for s in STEPS}
FIRST_STEPS = ("cons", "sex", "dob", "hcm", "wkg", "act", "rate")
FOLLOW_UP_GROUPS = (
    ("pln", "rhy", "refu"),
    ("chs", "skip", "like"),
    ("segs", "pant", "kit"),
    ("batch", "frz", "vary"),
    ("plan", "shops", "measure"),
    ("gwt", "mloss", "floor"),
    ("gdt", "morn", "conf"),
    ("qst", "qend", "steps"),
    ("meals", "pbed", "tone"),
    ("cyc",),
)
FOLLOW_UP_STEPS = tuple(key for group in FOLLOW_UP_GROUPS for key in group)
FOLLOW_UP_NAMES = {
    "pln": "planning",
    "rhy": "messages",
    "refu": "avoid",
    "chs": "choices",
    "skip": "skipped-meal",
    "like": "likes",
    "segs": "cook-days",
    "pant": "pantry",
    "kit": "kitchen",
    "batch": "batch-meals",
    "frz": "freezer",
    "vary": "variety",
    "plan": "training",
    "shops": "shopping-days",
    "measure": "measurements",
    "gwt": "goal-weight",
    "mloss": "max-loss",
    "floor": "calorie-floor",
    "gdt": "goal-date",
    "morn": "morning",
    "conf": "evening",
    "qst": "quiet-start",
    "qend": "quiet-end",
    "steps": "steps",
    "meals": "meals",
    "pbed": "bedtime-protein",
    "tone": "tone",
    "cyc": "cycle",
}
FOLLOW_UP_IDS = {name: step for step, name in FOLLOW_UP_NAMES.items()}


def answered_followups(db: Db, person_id: int) -> tuple[set[str], list[sqlite3.Row]]:
    """Answers applied to a profile, excluding rows from abandoned interviews."""
    raw = db.state_get(f"followup_applied:{person_id}")
    state = json.loads(raw) if raw else {"from": 0, "steps": []}
    rows = db.all(
        "SELECT step, answer_json FROM onboarding_answers WHERE person_id = ? AND id > ? "
        "ORDER BY id",
        (person_id, int(state["from"])),
    )
    return set(state["steps"]) | {str(row["step"]) for row in rows}, rows


def provided_followups(db: Db, person_id: int) -> set[str]:
    """Answers with a value; a skipped question still uses its default."""
    raw = db.state_get(f"followup_applied:{person_id}")
    state = json.loads(raw) if raw else {"from": 0, "provided": []}
    rows = db.all(
        "SELECT step, answer_json FROM onboarding_answers WHERE person_id = ? AND id > ?",
        (person_id, int(state["from"])),
    )
    return set(state.get("provided", [])) | {
        str(row["step"]) for row in rows if row["answer_json"] != "null"
    }


#: Explain the default rhythm at the end of the first conversation.
RHYTHM_SAID = {
    "light": "Mornings come only when the day's food changes; the check-in comes every evening. "
    "/set rhythm daily for a message every morning.",
    "daily": "A message every morning and a check-in every evening. /set rhythm light for "
    "mornings only when the day's food changes.",
    "weekly": "One check-in a week, on Sunday; mornings come only when the day's food changes. "
    "/set rhythm changes it.",
}


def consent_text(config: Config | None = None) -> str:
    provider = str(config.get("model.provider")) if config else "claude-cli"
    if provider == "none":
        return (
            "No model is configured. Menus, estimates and chat are manual until one is configured."
        )
    destination = {
        "anthropic": "Anthropic's Messages API",
        "openai": "OpenAI's API",
        "openai-compatible": str(config.get("model.base_url"))
        if config
        else "the configured server",
    }.get(provider, "Anthropic's Claude CLI")
    return (
        "What leaves this computer, and where it goes. "
        "Tiffin sends your numbers, foods and messages to "
        + destination
        + " to write menus, estimate off-plan food and answer questions. "
        "Your tiffin records stay on this computer. If you decline, those model tasks are "
        "manual; the day's numbers still work. You can change this with /consent. Agree?"
    )


# ---------------------------------------------------------------- the interview
class Onboarding:
    def __init__(
        self,
        db: Db,
        outbox: Outbox,
        registry: PersonRegistry,
        config: Config,
        *,
        clock: Clock = DEFAULT_CLOCK,
    ):
        self.db = db
        self.outbox = outbox
        self.registry = registry
        self.config = config
        self.clock = clock

    # -- state --------------------------------------------------------------------------
    def state(self, person: Person) -> dict[str, Any] | None:
        row = self.db.one(
            "SELECT state_json FROM conversation_state WHERE person_id = ?", (person.id,)
        )
        if row is None:
            return None
        st = json.loads(row["state_json"])
        return st if st.get("flow") == FLOW else None

    def _save(self, person: Person, st: dict[str, Any]) -> None:
        self.db.execute(
            "INSERT INTO conversation_state(person_id, state_json, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(person_id) DO UPDATE SET state_json = excluded.state_json, "
            "updated_at = excluded.updated_at",
            (person.id, json.dumps(st), self.clock.utc_stamp()),
        )

    def _clear(self, person: Person) -> None:
        self.db.execute("DELETE FROM conversation_state WHERE person_id = ?", (person.id,))
        self.db.state_set(self._parked_key(person), "")

    @staticmethod
    def _parked_key(person: Person) -> str:
        return f"onboarding_parked:{person.id}"

    def _park(self, person: Person, st: dict[str, Any]) -> None:
        self.db.state_set(self._parked_key(person), json.dumps(st))
        self.db.execute("DELETE FROM conversation_state WHERE person_id = ?", (person.id,))

    def parked(self, person: Person) -> dict[str, Any] | None:
        raw = self.db.state_get(self._parked_key(person)) or ""
        if not raw:
            return None
        st = json.loads(raw)
        return st if st.get("flow") == FLOW else None

    def active(self, person: Person) -> bool:
        return self.state(person) is not None

    def has_profile(self, person: Person) -> bool:
        return targets.profile_on(self.db, person.id, self.clock.today_local()) is not None

    def _reconcile_consent(self, state: dict[str, Any]) -> None:
        if self.config.get("model.provider") != "none" and (
            state.get("consent_scope") != self.config.model_scope()
        ):
            state["answers"].pop("cons", None)

    # -- flow ------------------------------------------------------------------------------
    def _next_step(self, answers: Answers, person: Person) -> Step | None:
        sequence = [
            STEP_BY_ID[key]
            for key in FIRST_STEPS
            if key != "cons" or self.config.get("model.provider") != "none"
        ]
        for step in sequence:
            if step.id in answers:
                continue
            if step.applies(answers, person):
                return step
        return None

    def start(self, person: Person, *, restart: bool = False) -> str:
        st = self.state(person) or self.parked(person)
        if st is None or restart:
            st = {
                "flow": FLOW,
                "answers": {},
                "step": None,
                "started_at": self.clock.utc_stamp(),
                "paused": False,
            }
        self._reconcile_consent(st)
        st["paused"] = False
        # A paused interview from an earlier release may be on an optional question.
        # Its answers remain in the ledger; the short interview uses the target facts it has.
        self.db.state_set(self._parked_key(person), "")  # it is live again, not parked
        step = self._next_step(st["answers"], person)
        if step is None:
            return self._finish(person, st)
        st["step"] = step.id
        self._save(person, st)
        self._ask(person, step, st["answers"])
        return "handled"

    def _dated(self, answers: Answers) -> Answers:
        """A copy of the answers carrying this interview's own date for the step functions."""
        return {**answers, TODAY_KEY: isoday(self.clock.today_local())}

    def _keyboard(self, step: Step, answers: Answers, person: Person) -> Keyboard | None:
        rows: list[list[tuple[str, str]]] = []
        opts = (
            step.options(self._dated(answers), person) if callable(step.options) else step.options
        )
        if step.id == "cons" and self.config.get("model.provider") == "none":
            opts = [("Continue", "0")]
        if opts:
            row: list[tuple[str, str]] = []
            for label, value in opts:
                row.append((label, f"k:ob:{step.id}:{value}"))
                if len(row) == 2:
                    rows.append(row)
                    row = []
            if row:
                rows.append(row)
        tail: list[tuple[str, str]] = []
        if self._previous_answered(answers, step):
            # twenty-six questions with no way back meant one mis-tap cost the whole interview:
            # "restart" was the only correction there was
            tail.append(("Back", f"k:ob:{step.id}:back"))
        if step.optional:
            tail.append(("Skip", f"k:ob:{step.id}:skip"))
        if step.id != "cons":
            tail.append(("Skip for now", f"k:ob:{step.id}:later"))
        rows.append(tail)
        return inline_keyboard(rows)

    @staticmethod
    def _previous_answered(answers: Answers, step: Step) -> Step | None:
        """The most recent step before `step` that has an answer to take back."""
        ids: list[str] = list(FIRST_STEPS)
        for key in reversed(ids[: ids.index(step.id)]):
            earlier = STEP_BY_ID[key]
            if earlier.id in answers:
                return earlier
        return None

    def _ask(self, person: Person, step: Step, answers: Answers) -> None:
        prompt = step.prompt(self._dated(answers), person) if callable(step.prompt) else step.prompt
        if step.id == "cons":
            prompt = consent_text(self.config)
        if self.config.get("units.weight", "kg") == "lb":
            prompt = prompt.replace("in kg", "in lb")
        if self.config.get("units.height", "cm") == "in":
            prompt = prompt.replace("in cm", "in inches")
        done = sum(1 for key in FIRST_STEPS if key in answers)
        total = len(FIRST_STEPS) - int(self.config.get("model.provider") == "none")
        text = f"<b>{done + 1}/{total}</b> {r.esc(prompt)}"
        if step.hint:
            text += f"\n<i>{r.esc(step.hint)}</i>"
        self.outbox.send(
            person,
            "onboarding",
            f"{step.id}:{uuid.uuid4().hex[:8]}",
            r.check_text(text, kind="onboarding"),
            self._keyboard(step, answers, person),
        )

    def answer(
        self,
        person: Person,
        raw: str,
        *,
        message_in_id: int | None = None,
        step_id: str | None = None,
    ) -> str:
        st = self.state(person)
        if st is None:
            return "ignored"
        current = STEP_BY_ID.get(str(st.get("step")))
        if current is None:
            return self.start(person)
        if step_id is not None and step_id != current.id:
            self.outbox.reply(
                person, "That button belongs to an earlier question; answer the current one."
            )
            return "ignored"
        word = raw.strip().lower()
        if word == "later":
            st["paused"] = True
            self._park(person, st)
            self.outbox.reply(
                person,
                "Paused, and your answers are kept. Send /onboard to continue where you left "
                "off. Everything else works in the meantime.",
            )
            return "handled"
        if word == "restart":
            return self.start(person, restart=True)
        answers = st["answers"]
        if word == "back":
            earlier = self._previous_answered(answers, current)
            if earlier is None:
                self.outbox.reply(person, "This is the first question; there is nothing behind it.")
                return "handled"
            # the old answer stays in the ledger: dropping it here only changes what is asked
            answers.pop(earlier.id, None)
            st["step"] = earlier.id
            self._save(person, st)
            self._ask(person, earlier, answers)
            return "handled"
        if word == "skip" and current.optional:
            value = None
        else:
            try:
                if current.id in ("wkg", "gwt") and self.config.get("units.weight", "kg") == "lb":
                    number = re.search(r"-?\d+(?:[.,]\d+)?", raw)
                    if number:
                        raw = str(float(number.group(0).replace(",", ".")) * 0.45359237)
                if current.id == "hcm" and self.config.get("units.height", "cm") == "in":
                    number = re.search(r"-?\d+(?:[.,]\d+)?", raw)
                    if number:
                        raw = str(float(number.group(0).replace(",", ".")) * 2.54)
                value = current.parse(raw, self._dated(answers))
            except Invalid as e:
                self.outbox.reply(person, r.esc(str(e)))
                return "handled"
        answers[current.id] = value
        if current.id == "cons":
            st["consent_scope"] = self.config.model_scope()
        self.db.insert(
            "onboarding_answers",
            person_id=person.id,
            step=current.id,
            answer_json=json.dumps(value),
            message_in_id=message_in_id,
        )
        self._reconcile_consent(st)
        if current.id == "plan" and value:
            # A training week can exceed the ten-line limit; its echo must never stop the flow.
            self.outbox.reply(
                person,
                r.fit("Your week:\n" + "\n".join(plan_lines(value)), kind="plan", max_lines=20),
            )
        nxt = self._next_step(answers, person)
        if nxt is None:
            return self._finish(person, st)
        st["step"] = nxt.id
        self._save(person, st)
        self._ask(person, nxt, answers)
        return "handled"

    # -- completion -------------------------------------------------------------------------
    def _finish(self, person: Person, st: dict[str, Any]) -> str:
        a = st["answers"]
        today = self.clock.today_local()
        # Redoing target facts preserves optional settings already chosen.
        prev = targets.profile_on(self.db, person.id, today)

        def old(column: str, default: Any) -> Any:
            return prev[column] if prev is not None else default

        rhythm = str(a.get("rhy") or (prev["rhythm"] if prev is not None else "") or "weekly")
        skip_mode = str(a.get("skip") or old("skip_mode", "share") or "share")
        choices = int(a["chs"]) if "chs" in a else int(old("choices", 0) or 0)
        from .food import planning as planning_mod  # noqa: PLC0415 (food imports this layer)

        planning = str(a.get("pln") or planning_mod.planning_of(prev))
        if planning == "choose" and choices == 0:
            choices = planning_mod.DEFAULT_OPTIONS
        segments = (
            json.dumps(SEGMENTS[a["segs"]])
            if a.get("segs") in SEGMENTS
            else (str(prev["segment_days"]) if prev is not None else json.dumps(SEGMENTS["mf"]))
        )
        batch_slots = (
            json.dumps(a["batch"])
            if "batch" in a
            else (str(prev["batch_slots"]) if prev is not None else json.dumps(["lunch", "dinner"]))
        )
        variety = (
            json.dumps({"all": a["vary"]})
            if a.get("vary")
            else (str(prev["variety_json"]) if prev is not None else json.dumps({"all": "rotate"}))
        )
        age = age_on(parse_day(str(a["dob"])), today)
        bmr = bmr_mifflin(str(a["sex"]), float(a["wkg"]), float(a["hcm"]), age)
        floor = max(1200, int(round(bmr / 50.0) * 50))
        with self.db.tx():
            self.db.insert(
                "profiles",
                person_id=person.id,
                effective_from=isoday(today),
                sex=a["sex"],
                birth_date=a["dob"],
                height_cm=float(a["hcm"]),
                activity_factor=float(a["act"]),
                steps_floor=int(a.get("steps") or (prev["steps_floor"] if prev else 8000)),
                quiet_start=a.get("qst") or (prev["quiet_start"] if prev else "22:30"),
                quiet_end=a.get("qend") or (prev["quiet_end"] if prev else "07:00"),
                confirm_time=a.get("conf") or (prev["confirm_time"] if prev else "20:30"),
                morning_time=a.get("morn") or (prev["morning_time"] if prev else "07:30"),
                nudge_time="16:00",
                goal_weight_kg=a["gwt"] if "gwt" in a else old("goal_weight_kg", None),
                goal_date=a["gdt"] if "gdt" in a else old("goal_date", None),
                rate_pct_per_week=float(a["rate"]),
                max_loss_kg_week=float(a.get("mloss") or old("max_loss_kg_week", MAX_LOSS_KG_WEEK)),
                kcal_floor=int(a.get("floor") or (prev["kcal_floor"] if prev else floor)),
                variety_json=variety,
                segment_days=segments,
                batch_slots=batch_slots,
                cycle_tracking=int(a.get("cyc") or old("cycle_tracking", 0)),
                meals_per_day=int(a.get("meals") or (prev["meals_per_day"] if prev else 4)),
                pre_bed_protein=int(a.get("pbed") or (prev["pre_bed_protein"] if prev else 0)),
                freezes=int(a.get("frz") or old("freezes", 0)),
                rhythm=rhythm,
                skip_mode=skip_mode,
                choices=choices,
                planning=planning,
                tone=a.get("tone") or (prev["tone"] if prev else "neutral"),
                refused_foods=json.dumps(a["refu"]) if "refu" in a else old("refused_foods", "[]"),
                liked_foods=json.dumps(a["like"]) if "like" in a else old("liked_foods", "[]"),
                pantry_json=json.dumps(a["pant"]) if "pant" in a else old("pantry_json", "[]"),
                equipment_json=json.dumps(a["kit"]) if "kit" in a else old("equipment_json", "[]"),
            )
            body.record_weigh_in(self.db, person.id, today, float(a["wkg"]), "manual")
            if "plan" in a:
                plan_id = self.db.insert(
                    "training_plans",
                    person_id=person.id,
                    effective_from=isoday(today),
                    source_text=json.dumps(a["plan"]),
                )
                for s in a["plan"]:
                    self.db.insert(
                        "planned_sessions",
                        plan_id=plan_id,
                        person_id=person.id,
                        weekday=s["weekday"],
                        kind=s["kind"],
                        target_minutes=s["target_minutes"],
                        start_time=s["start_time"],
                    )
            if "cons" in a and self.config.get("model.provider") != "none":
                self.db.insert(
                    "consents",
                    person_id=person.id,
                    kind="model_processing",
                    granted=int(a["cons"]),
                    wording_version=f"{PROCESSING_CONSENT_VERSION}:{self.config.model_scope()}",
                )
            self._clear(person)
            prior = (
                answered_followups(self.db, person.id)[0]
                if self.db.state_get(f"followup_applied:{person.id}")
                else set()
            )
            prior_provided = (
                provided_followups(self.db, person.id)
                if self.db.state_get(f"followup_applied:{person.id}")
                else set()
            )
            applied = prior | {key for key in a if key in FOLLOW_UP_STEPS}
            provided = prior_provided | {
                key for key, value in a.items() if key in FOLLOW_UP_STEPS and value is not None
            }
            last_answer = self.db.scalar(
                "SELECT MAX(id) FROM onboarding_answers WHERE person_id = ?", (person.id,)
            )
            self.db.state_set(
                f"followup_applied:{person.id}",
                json.dumps(
                    {
                        "from": int(last_answer or 0),
                        "steps": sorted(applied),
                        "provided": sorted(provided),
                    }
                ),
            )
            self.db.state_set(f"followup_started:{person.id}", isoday(today))
        t = targets.ensure_targets(self.db, person.id, today, self.config, clock=self.clock)
        lines = ["<b>Profile saved.</b>"]
        if t is not None:
            m = targets.macros_of(t)
            lines.append(
                "This week: " + r.macros_line(m.kcal, m.protein_g, m.fat_g, m.carb_g, m.fibre_g)
            )
        morning = str(a.get("morn") or old("morning_time", "07:30"))
        confirm = str(a.get("conf") or old("confirm_time", "20:30"))
        lines.append(f"Morning message {morning}, evening check-in {confirm}.")
        provider = self.config.get("model.provider")
        if provider == "none":
            lines.append("No model is configured, so menus and estimates stay manual.")
        elif not int(a.get("cons") or 0):
            lines.append("Model tasks stay manual until you grant consent with /consent.")
        evening = (
            f"on Sunday at {confirm} I ask how the week went"
            if rhythm == "weekly"
            else f"{confirm} I ask how it went"
        )
        lines.append(f"<b>Next:</b> tomorrow {morning} I send the day's targets, and {evening}.")
        lines.append(RHYTHM_SAID.get(rhythm, RHYTHM_SAID["light"]))
        lines.append(
            "I will offer a few optional questions on some days. Skip any; /followup resumes them. "
            + (
                "Send /today for today or /help for more."
                if provider == "none" or not int(a.get("cons") or 0)
                else "Send /cook for a menu, /today for today, or /help for more."
            )
        )
        if prev is None and not any(k in a for k in ("pln", "rhy", "pant", "plan")):
            lines.append("Automatic menus and weekly check-ins are the starting settings.")
            lines.append("No food at home, training sessions or measurement reminders are assumed.")
        self.outbox.send(
            person,
            "onboarding_done",
            f"{isoday(today)}:{uuid.uuid4().hex[:8]}",
            r.check_text("\n".join(lines), kind="done"),
        )
        return "handled"

    def next_followups(self, person: Person, *, scheduled: bool = False) -> list[Step]:
        if not self.has_profile(person) or not self.db.state_get(f"followup_started:{person.id}"):
            return []
        answered, rows = answered_followups(self.db, person.id)
        offered = set(json.loads(self.db.state_get(f"followup_offered:{person.id}") or "[]"))
        profile = targets.profile_on(self.db, person.id, self.clock.today_local())
        if profile is None:
            return []
        groups = str(profile["segment_days"])
        segment = next((key for key, days in SEGMENTS.items() if json.dumps(days) == groups), "mf")
        facts: Answers = {
            "sex": profile["sex"],
            "gwt": profile["goal_weight_kg"],
            "pln": profile["planning"],
            "segs": segment,
        }
        for row in rows:
            if row["answer_json"] != "null":
                facts[str(row["step"])] = json.loads(str(row["answer_json"]))
        for group in FOLLOW_UP_GROUPS:
            steps = [
                STEP_BY_ID[key]
                for key in group
                if key not in answered
                and (not scheduled or key not in offered)
                and STEP_BY_ID[key].applies(facts, person)
            ]
            if steps:
                return steps
        return []

    def ask_followup(
        self, person: Person, *, scheduled: bool = False, step_id: str | None = None
    ) -> bool:
        steps = (
            [STEP_BY_ID[step_id]]
            if step_id in FOLLOW_UP_STEPS
            else self.next_followups(person, scheduled=scheduled)
        )
        if not steps:
            return False
        if scheduled:
            from .telegram.convstate import open_flow  # noqa: PLC0415

            started = self.db.state_get(f"followup_started:{person.id}")
            day = isoday(self.clock.today_local())
            if started == day or self.db.state_get(f"followup_prompt:{person.id}") == day:
                return False
            if open_flow(self.db, person.id, clock=self.clock):
                return False
        profile = targets.profile_on(self.db, person.id, self.clock.today_local())
        weight = targets.latest_weight(self.db, person.id, self.clock.today_local())
        facts: Answers = {
            "sex": profile["sex"] if profile else "m",
            "dob": profile["birth_date"] if profile else "1990-01-01",
            "hcm": profile["height_cm"] if profile else 170,
            "wkg": weight[1] if weight else 80.0,
            "gwt": profile["goal_weight_kg"] if profile else None,
            "gdt": profile["goal_date"] if profile else None,
            "rate": profile["rate_pct_per_week"] if profile else 0.7,
        }
        lines = ["Optional questions. Answer any now, or use /followup later."]
        rows: list[list[tuple[str, str]]] = []
        for number, step in enumerate(steps, 1):
            prompt = (
                step.prompt(self._dated(facts), person) if callable(step.prompt) else step.prompt
            )
            if self.config.get("units.weight", "kg") == "lb":
                prompt = prompt.replace("in kg", "in lb")
            if step.kind == "text":
                prompt += f" Reply /followup {FOLLOW_UP_NAMES.get(step.id, step.id)} YOUR ANSWER."
            lines.append(f"{number}. {r.esc(prompt)}")
            options = (
                step.options(self._dated(facts), person) if callable(step.options) else step.options
            )
            rows.extend(
                [
                    [(f"{number}. {label}", f"k:fu:{step.id}:{value}")]
                    for label, value in options or []
                ]
            )
            rows.append([(f"{number}. Skip", f"k:fu:{step.id}:skip")])
        result = self.outbox.send(
            person,
            "followup",
            f"{steps[0].id}:{isoday(self.clock.today_local())}",
            r.check_text("\n".join(lines), kind="followup"),
            inline_keyboard(rows),
        )
        if result.status == "failed":
            raise RuntimeError("follow-up message could not be sent")
        self.db.state_set(f"followup_prompt:{person.id}", isoday(self.clock.today_local()))
        offered = set(json.loads(self.db.state_get(f"followup_offered:{person.id}") or "[]"))
        self.db.state_set(
            f"followup_offered:{person.id}",
            json.dumps(sorted(offered | {step.id for step in steps})),
        )
        return True

    def followup_answer(
        self, person: Person, step_id: str, raw: str, *, message_in_id: int | None = None
    ) -> str:
        from .telegram.settings import FIELDS, write_setting  # noqa: PLC0415

        step = STEP_BY_ID.get(step_id)
        if step is None or step_id not in FOLLOW_UP_STEPS or not self.has_profile(person):
            self.outbox.reply(person, "Unknown follow-up. Send /followup for the next question.")
            return "handled"
        profile = targets.profile_on(self.db, person.id, self.clock.today_local())
        if profile is None:
            return "ignored"
        weight = targets.latest_weight(self.db, person.id, self.clock.today_local())
        facts: Answers = {
            "sex": profile["sex"],
            "dob": profile["birth_date"],
            "hcm": profile["height_cm"],
            "wkg": weight[1] if weight else 80.0,
            "gwt": profile["goal_weight_kg"],
            "gdt": profile["goal_date"],
            "rate": profile["rate_pct_per_week"],
        }
        if raw.strip().lower() == "skip":
            value = None
        else:
            try:
                if step_id == "gwt" and self.config.get("units.weight", "kg") == "lb":
                    number = re.search(r"-?\d+(?:[.,]\d+)?", raw)
                    if number:
                        raw = str(float(number.group(0).replace(",", ".")) * 0.45359237)
                value = step.parse(raw, self._dated(facts))
            except Invalid as e:
                self.outbox.reply(person, r.esc(str(e)))
                return "handled"
        if value is not None:
            field = next((f for f in FIELDS if f.step == step_id), None)
            if field is not None:
                write_setting(
                    self.db, person.id, field, value, clock=self.clock, record_answer=False
                )
            elif step_id == "kit":
                targets.change_profile(
                    self.db,
                    person.id,
                    {"equipment_json": json.dumps(value)},
                    today=self.clock.today_local(),
                )
            elif step_id == "plan":
                plan_id = self.db.insert(
                    "training_plans",
                    person_id=person.id,
                    effective_from=isoday(self.clock.today_local()),
                    source_text=json.dumps(value),
                )
                for session in value:
                    self.db.insert(
                        "planned_sessions",
                        plan_id=plan_id,
                        person_id=person.id,
                        weekday=session["weekday"],
                        kind=session["kind"],
                        target_minutes=session["target_minutes"],
                        start_time=session["start_time"],
                    )
            elif step_id in ("shops", "measure"):
                from .food import weekplan  # noqa: PLC0415

                changes: dict[str, object] = (
                    {
                        "shops": {
                            "none": (),
                            "mon": ((0, 1, 2, 3, 4),),
                            "twice": ((0, 1, 2), (3, 4)),
                        }[value]
                    }
                    if step_id == "shops"
                    else {"check_day": {"none": None, "mon": 0, "sun": 6}[value]}
                )
                weekplan.change(self.db, person.id, source="chat", **changes)
        self.db.insert(
            "onboarding_answers",
            person_id=person.id,
            step=step_id,
            answer_json=json.dumps(value),
            message_in_id=message_in_id,
        )
        self.outbox.reply(
            person,
            "Skipped. Your current setting stays in place."
            if value is None
            else "Saved. You can change this later with /followup.",
        )
        return "handled"


# ---------------------------------------------------------------- wiring into the handlers
def install(handlers: Handlers, onboarding: Onboarding) -> None:
    def cmd_consent(h: Handlers, inbound: Inbound, arg: str) -> str:
        """Show cloud processing consent and offer the applicable change."""
        from .model.gateway import consent_granted  # noqa: PLC0415

        person = inbound.person
        provider = str(onboarding.config.get("model.provider"))
        scope = onboarding.config.model_scope()
        granted = consent_granted(h.db, person.id, provider=scope)
        state = (
            (
                "Claude processing is ON: menus, estimates and chat use Anthropic's model."
                if provider == "claude-cli"
                else f"{provider} processing is ON: menus, estimates and chat use this provider."
            )
            if granted
            else (
                "Claude processing is OFF: no menus, estimates or chat; "
                "the day's numbers still work."
                if provider == "claude-cli"
                else f"{provider} processing is OFF: model tasks are manual."
            )
        )
        token = "" if provider == "claude-cli" else f":{scope}"
        button = (
            ("Turn it off", f"k:consent:0{token}")
            if granted
            else ("Turn it on", f"k:consent:1{token}")
        )
        if provider == "none":
            state = "No model is configured. Model tasks are manual."
        # the shared weekend: on a dish you cook together, both of you see both
        # plates and their macros - never targets, weight or what anyone logged
        shared = consent_granted(h.db, person.id, "shared_meals")
        shared_state = (
            "Shared weekend menu is ON: on dishes you cook together you both see both plates."
            if shared
            else "Shared weekend menu is OFF: each of you gets your own weekend menu."
        )
        share_button = (
            ("Stop sharing weekends", "k:consent:share:0")
            if shared
            else ("Share weekend menus", "k:consent:share:1")
        )
        partner = h.registry.by_slug("partner")
        has_partner = partner is not None and partner.chat_id is not None
        rows: list[list[tuple[str, str]]] = [[share_button]] if has_partner else []
        if provider != "none":
            rows.insert(0, [button])
        h.outbox.reply(
            person,
            r.check_text(
                r.esc(consent_text(onboarding.config))
                + "\n"
                + state
                + ("\n" + shared_state if has_partner else ""),
                kind="consent",
                max_lines=20,
            ),
            keyboard=inline_keyboard(rows),
        )
        return "handled"

    def cb_consent(h: Handlers, inbound: Inbound, parts: list[str]) -> str:
        if len(parts) == 2 and parts[0] == "share" and parts[1] in ("0", "1"):
            return shared_consent(h, inbound, int(parts[1]))
        provider = str(onboarding.config.get("model.provider"))
        if provider == "none":
            h.outbox.reply(inbound.person, "No model is configured. Model tasks stay manual.")
            return "handled"
        if not parts or parts[0] not in ("0", "1"):
            return "ignored"
        if (len(parts) > 1 and parts[1] != onboarding.config.model_scope()) or (
            len(parts) == 1 and provider != "claude-cli"
        ):
            h.outbox.reply(
                inbound.person,
                "The model provider changed. Send /consent to review it before deciding.",
            )
            return "handled"
        h.clear_buttons(inbound)
        want = int(parts[0])
        h.db.insert(
            "consents",
            person_id=inbound.person.id,
            kind="model_processing",
            granted=want,
            wording_version=f"{PROCESSING_CONSENT_VERSION}:{onboarding.config.model_scope()}",
            message_in_id=inbound.message_in_id,
        )
        label = "Claude" if provider == "claude-cli" else provider
        if want:
            response = f"{label} processing is on. Menus and chat will use it."
        else:
            response = (
                f"{label} processing is off. There are no menus, estimates or chat without it; "
                "the day's numbers still work. Send /consent to turn it back on."
            )
        h.outbox.reply(inbound.person, response)
        return "handled"

    def shared_consent(h: Handlers, inbound: Inbound, want: int) -> str:
        """The shared weekend, switched on or off by the person it is about (a ledger row)."""
        from .food import shared  # noqa: PLC0415 (food imports the db layer)

        h.clear_buttons(inbound)
        h.db.insert(
            "consents",
            person_id=inbound.person.id,
            kind="shared_meals",
            granted=want,
            wording_version=shared.CONSENT_VERSION,
            message_in_id=inbound.message_in_id,
        )
        if not want:
            h.outbox.reply(
                inbound.person,
                "Weekend menus are your own again from the next weekend. Nothing about your "
                "plate is shown to anyone.",
            )
            return "handled"
        both = shared.everyone_agreed(h.db)
        if not both:
            h.outbox.reply(
                inbound.person,
                "Noted. It starts once you have both said yes: the other person sends /consent "
                "and taps Share weekend menus.",
            )
            return "handled"
        # "send /cook to build this weekend's now" was wrong four days in seven: on a Tuesday
        # /cook answers with the week already planned. The weekend is built on its Thursday.
        when = (
            "Send /cook to build this weekend's now."
            if h.clock.today_local().weekday() >= shared.THURSDAY
            else "This weekend's is built on Thursday."
        )
        h.outbox.reply(
            inbound.person,
            "Done - you have both said yes. Weekends are one menu for both of you, cooked "
            f"together, each with your own portion. {when} The page's Menu tab has a switch for "
            "any weekend you would rather each cook your own.",
        )
        # ...and whoever said yes first was never told it had started
        for pid in shared.people(h.db):
            other = h.registry.by_id(pid) if pid != inbound.person.id else None
            if other is not None:
                h.outbox.send(
                    other,
                    "shared_on",
                    f"shared_on:{inbound.message_in_id}",
                    "You have both said yes: weekends are cooked together now, each with your own "
                    "portion. The page's Menu tab has a switch for any weekend you would rather "
                    "each cook your own.",
                )
        return "handled"

    def cmd_onboard(h: Handlers, inbound: Inbound, arg: str) -> str:
        if arg.strip().lower() == "restart":
            return onboarding.start(inbound.person, restart=True)
        if onboarding.has_profile(inbound.person) and not onboarding.active(inbound.person):
            h.outbox.reply(inbound.person, "You are onboarded. Send /onboard restart to redo it.")
            return "handled"
        return onboarding.start(inbound.person)

    def cmd_start(h: Handlers, inbound: Inbound, arg: str) -> str:
        """/start on an unonboarded person opens the interview rather than naming a command.

        Pressing Start is the first thing anyone does with a new bot, and being answered with
        "now send /onboard" is a step that exists only because the code was easier that way.
        Someone already onboarded gets the greeting, because /start is also how people check
        that a bot is alive.
        """
        if onboarding.active(inbound.person) or not onboarding.has_profile(inbound.person):
            return onboarding.start(inbound.person)
        h.outbox.reply(
            inbound.person,
            (f"Hello {r.esc(inbound.person.display_name)}. " if inbound.person.display_name else "")
            + "Everything is set up. "
            "/help lists what I do.",
        )
        return "handled"

    def cb_ob(h: Handlers, inbound: Inbound, parts: list[str]) -> str:
        if len(parts) < 2:
            return "ignored"
        h.clear_buttons(inbound)
        return onboarding.answer(
            inbound.person,
            ":".join(parts[1:]),
            message_in_id=inbound.message_in_id,
            step_id=parts[0],
        )

    def cmd_followup(h: Handlers, inbound: Inbound, arg: str) -> str:
        key, _, value = arg.strip().partition(" ")
        key = FOLLOW_UP_IDS.get(key, key)
        if key and value:
            return onboarding.followup_answer(
                inbound.person, key, value, message_in_id=inbound.message_in_id
            )
        if key in FOLLOW_UP_STEPS:
            onboarding.ask_followup(inbound.person, step_id=key)
            return "handled"
        if not onboarding.ask_followup(inbound.person):
            h.outbox.reply(inbound.person, "No optional questions are waiting.")
        return "handled"

    def cb_followup(h: Handlers, inbound: Inbound, parts: list[str]) -> str:
        if len(parts) < 2:
            return "ignored"
        h.clear_buttons(inbound)
        return onboarding.followup_answer(
            inbound.person,
            parts[0],
            ":".join(parts[1:]),
            message_in_id=inbound.message_in_id,
        )

    def text(h: Handlers, inbound: Inbound, text: str) -> str | None:
        if onboarding.active(inbound.person) and not (onboarding.state(inbound.person) or {}).get(
            "paused"
        ):
            return onboarding.answer(inbound.person, text, message_in_id=inbound.message_in_id)
        return None

    handlers.register_command("onboard", cmd_onboard)
    handlers.register_command("followup", cmd_followup)
    handlers.register_callback("fu", cb_followup)
    handlers.register_command("consent", cmd_consent)
    handlers.register_callback("consent", cb_consent)
    handlers.register_command("start", cmd_start)  # replaces the placeholder in handlers.py
    handlers.register_callback("ob", cb_ob)
    handlers.text_handlers.insert(0, text)
