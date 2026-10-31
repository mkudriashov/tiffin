"""Interpret dates, meals and change notes in Telegram menu requests."""

from __future__ import annotations

import datetime as dt
import re

from ..clock import parse_day

_WORDS = {"today": 0, "tonight": 0, "tomorrow": 1, "tmr": 1, "tmrw": 1, "yesterday": -1}
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def day_named(said: str, today: dt.date) -> dt.date | None:
    """Resolve a relative day, weekday or date; return None for other text."""
    word = said.strip().casefold()
    if not word:
        return today
    if word in _WORDS:
        return today + dt.timedelta(days=_WORDS[word])
    for n, name in enumerate(_WEEKDAYS):
        # A weekday name refers to its next occurrence, including when today has that name.
        if name.startswith(word) and len(word) >= 3:
            ahead = (n - today.weekday()) % 7
            return today + dt.timedelta(days=ahead or 7)
    if len(word) == 8 and word.isdigit():
        # Day buttons carry dates without separators.
        try:
            return dt.datetime.strptime(word, "%Y%m%d").replace(tzinfo=dt.UTC).date()
        except ValueError:
            return None
    try:
        return parse_day(word)
    except ValueError:
        return None


_MEALS = {
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
_FILLER = frozenset({"for", "on", "the", "my", "of", "what", "is", "whats", "what's"})


_MEAL_WORDS = {
    "breakfast": "breakfast",
    "breakfasts": "breakfast",
    "lunch": "lunch",
    "lunches": "lunch",
    "dinner": "dinner",
    "dinners": "dinner",
    "supper": "dinner",
    "snack": "snack",
    "snacks": "snack",
    "pre-bed": "pre_bed",
    "pre bed": "pre_bed",
    "bedtime": "pre_bed",
}


# Treat "pre-bed snack" as one slot.
_PRE_BED_SNACK = re.compile(r"\b(?:pre[- ]?bed|bedtime)\s+snacks?\b")
# Cues nearest a meal determine whether the person wants to keep or change that slot.
_KEEP_WORDS = r"keep|keeping|leave|leaving|same"
_CHANGE_WORDS = (
    r"change|changed|changing|touch|swap|swapped|replace|replaced|redo|rebuild|switch|new|"
    r"different|another"
)
_FINE_WORDS = r"fine|ok|okay|good|great|happy|alright|unchanged|stays?|alone"
_NEAR_CUE = re.compile(rf"\b(?:(?P<keep>{_KEEP_WORDS})|(?P<change>{_CHANGE_WORDS}))\b")
_CUE = re.compile(rf"\b(?:(?P<keep>{_KEEP_WORDS}|{_FINE_WORDS})|(?P<change>{_CHANGE_WORDS}))\b")
_NEGATED = re.compile(r"(?:\bnot|n't|\bdont|\bnever|\bno need to)\s+(?:\w+\s+)?$")
_WITHIN_TWO_WORDS = re.compile(r"(?:\s+[\w'-]+){0,2}\s*")
_CURLY = str.maketrans({"\u2019": "'"})
# An exception reverses the cue for the rest of the meals.
_EXCEPT = re.compile(
    r"\b(?:except(?:\s+for)?|excluding|besides|apart from|other than|"
    r"(?:everything|all|anything)(?:\s+else)?\s+but)\s+(?:(?:the|my|our)\s+)?$"
)
_KEPT_AFTER = re.compile(
    r"^\s*(?:is|are|was|were|has been|have been|can|as)?\s*(?:fine|ok|okay|good|stays?|stay|"
    r"as is|as it is|unchanged|the same|alone)\b"
)
_SENTENCE_END = re.compile(r"[.!?;]")


def _keeps(words: str, *, last: bool) -> bool | None:
    """Resolve the nearest keep or change cue, including a preceding negation."""
    cues = list(_CUE.finditer(words))
    if not cues:
        return None
    cue = cues[-1] if last else cues[0]
    return (cue.group("keep") is not None) != bool(_NEGATED.search(words[: cue.start()]))


def _kept(said: str, start: int, end: int) -> bool:
    """Return whether the note says to keep the meal at the given span."""
    before = said[:start]
    before = before[max((m.end() for m in _SENTENCE_END.finditer(before)), default=0) :]
    after = _SENTENCE_END.split(said[end:], maxsplit=1)[0]
    if (exc := _EXCEPT.search(before)) is not None:
        # A bare exception keeps the named meal; an explicit keep cue reverses it.
        way = _keeps(before[: exc.start()], last=True)
        if way is None:
            way = _keeps(after, last=False)
        return way is not True
    near = [c for c in _NEAR_CUE.finditer(before) if _WITHIN_TWO_WORDS.fullmatch(before[c.end() :])]
    if near:
        cue = near[-1]
        return (cue.group("keep") is not None) != bool(_NEGATED.search(before[: cue.start()]))
    return _KEPT_AFTER.search(after) is not None


def slots_named(note: str) -> set[str]:
    """Return the slots a change note names for alteration."""
    said = _PRE_BED_SNACK.sub("pre-bed", (note or "").casefold().translate(_CURLY))
    out: set[str] = set()
    for word, slot in _MEAL_WORDS.items():
        for m in re.finditer(rf"\b{word}\b", said):
            if not _kept(said, m.start(), m.end()):
                out.add(slot)
    return out


_DAY_FORMS = {
    **{name: name for name in _WEEKDAYS},
    "mon": "monday",
    "tue": "tuesday",
    "tues": "tuesday",
    "wed": "wednesday",
    "weds": "wednesday",
    "thu": "thursday",
    "thur": "thursday",
    "thurs": "thursday",
    "fri": "friday",
    "sat": "saturday",
    "sun": "sunday",
}
_DAY_BEFORE = frozenset({"on", "for", "this", "next", "by"})
_DAY_LIST = frozenset({"and", "or", "&", ","})
_BUT_NOT = frozenset({"except", "not", "apart", "besides", "excluding"})


def days_in_note(note: str, today: dt.date, run: list[dt.date]) -> list[dt.date]:
    """Return named days in order, resolving weekdays within the menu run where possible.

    A weekday needs day context so food names such as "sun dried" do not match.
    """
    toks = re.findall(r"[\w'-]+|[^\w\s]", (note or "").casefold().translate(_CURLY))
    words = [i for i, t in enumerate(toks) if t[0].isalnum()]
    out: list[dt.date] = []
    named_at = -1  # where the last weekday taken as a day was
    for i in words:
        raw = toks[i]
        word = raw.removesuffix("'s").removesuffix("s'")
        word = word[:-1] if word.endswith("days") else word  # "thursdays"
        if (word in _DAY_FORMS or word in _WORDS) and _BUT_NOT & set(toks[max(0, i - 2) : i]):
            # An excluded day is not the sole day to rebuild.
            return []
        if word in _DAY_FORMS:
            prev = toks[i - 1] if i > 0 else ""
            nxt = toks[i + 1] if i + 1 < len(toks) else ""
            listed = prev in _DAY_BEFORE or (prev in _DAY_LIST and named_at == i - 2)
            alone = word not in ("sat", "sun") and (
                word != raw  # "thursday's lunch"
                or (nxt in _MEALS and word != "sunday")  # a Sunday lunch is a roast, not a day
                or i == words[-1]  # "a different lunch thursday"
                or (i == words[0] and (not nxt or not nxt[0].isalnum()))  # "thursday: ..."
            )
            if not (listed or alone):
                continue
            named_at = i
            day = day_named(_DAY_FORMS[word], today)
            assert day is not None  # a weekday's full name always is one
            day = next((d for d in run if d.weekday() == day.weekday()), day)
        elif word in _WORDS or word[0].isdigit():
            day = day_named(word, today)
            if day is None:
                continue
        else:
            continue
        if day < today:
            continue  # A past day can explain a request but cannot be rebuilt.
        if day not in out:
            out.append(day)
    return out


def day_and_meal(said: str, today: dt.date) -> tuple[dt.date, str] | None:
    """Parse a day and meal slot, leaving longer phrases for dish name matching."""
    words = [w.removesuffix("'s").removesuffix("s'") for w in said.casefold().split()]
    words = [w for w in words if w not in _FILLER]
    meals = [w for w in words if w in _MEALS]
    if len(meals) != 1:
        return None
    rest = [w for w in words if w not in _MEALS]
    if not rest:
        return today, _MEALS[meals[0]]
    if len(rest) != 1:
        return None
    day = day_named(rest[0], today)
    return None if day is None else (day, _MEALS[meals[0]])
