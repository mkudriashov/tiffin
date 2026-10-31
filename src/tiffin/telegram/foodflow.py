"""Handle immediate food changes and dish swaps in Telegram.

A refusal is stored on the person's word. Swaps offer dishes they can eat,
while model suggestions are checked before they can be chosen."""

from __future__ import annotations

import datetime as dt
import json
import re
from collections.abc import Iterable
from itertools import pairwise
from typing import TYPE_CHECKING, Any

from .. import log
from ..clock import DEFAULT_CLOCK, Clock, isoday, parse_day, parse_stamp
from ..db import Db
from ..errors import Infeasible, NothingToBuild
from ..food import catalogue, prefs, shared, subst, swap
from ..food import menu as menu_mod
from . import dayflow
from .api import MAX_CALLBACK_BYTES, inline_keyboard
from .render import esc

if TYPE_CHECKING:
    from .handlers import Handlers
    from .poller import Inbound

logger = log.get("tiffin.foodflow")

#: the words that make a sentence a standing refusal, not a report of the day: "I do not want",
#: "no more", "hate", "allergic to". A bare "no X" is a refusal only as the whole message.
_CUE = re.compile(
    r"\b(?:do not|does not|don't|doesn't|dont|never|no more|avoid|hate|dislike|detest|"
    r"can't stand|cannot stand|can not stand|allergic|intolerant|sick of|fed up with|"
    r"had enough|not keen|not a fan|stop giving|stop putting|won't eat|will not eat|"
    r"rather not|not any ?more)\b",
    re.I,
)
#: what the day was, not what the menus should be: "I didn't eat the lentils" is a report
_REPORT = re.compile(
    r"\b(?:did not|didn't|didnt|had|ate|eaten|skipped|was|were|forgot|today|tonight|"
    r"yesterday|this morning|for lunch|for dinner|for breakfast)\b",
    re.I,
)
#: the same words saying why, or when a refusal begins, not what was eaten: "I've had enough of
#: chickpeas", "no more lentils, they were awful", "from today, no more lentils". A refusal for
#: now - this week, the evenings, the shopping - is read in
#: `catalogue.for_now`, so a cook note is read the same way.
_NOT_A_REPORT = re.compile(
    r"\b(?:had enough|(?:they|it|that|those|these) (?:was|were)|(?:from|starting|as of) today)\b",
    re.I,
)
_BARE_NO = re.compile(
    r"^\s*(?:(?:please|pls)[\s,]+)?no\s+(?!problem|worries|thanks|thank|idea|way|rush)\w", re.I
)
#: the apostrophes a phone types, each read as a straight one before any cue is looked for
_APOSTROPHES = re.compile("[\u2018\u2019\u02bc`]")
#: a complaint about how much of a food a menu held is about the plan even said in the past:
#: "there was too much chia seed" is not food eaten, where "had less rice" is
_TOO = re.compile(r"\b(?:too much|too many|too little|not enough)\b", re.I)
#: ...unless they say they ate it: "I ate too much chocolate" is the day's food, the check-in's
_INTAKE = re.compile(
    r"\b(?:i|we)(?:'ve| have)?\s+(?:just\s+|really\s+|definitely\s+)?"
    r"(?:had|ate|eaten|drank|drunk|snacked|overate)\b",
    re.I,
)
#: a request asked as a question is still a request: "can we have more fish?"
_ASKING = re.compile(
    r"^\s*(?:(?:oh|ok|okay|also|and|so|hey|please)[\s,]+)?(?:can|could|would)\s+(?:you|we|i)\b",
    re.I,
)
#: the most wishes one message keeps, so each one's id fits in a button's callback data
MAX_WISHES = 6
#: the people a sentence can be about besides the sender: "my GIRLFRIEND is not keen", "my GF
_PEOPLE = (
    r"(?:girlfriend|boyfriend|gf|bf|partner|other half|better half|oh|significant other|wife|"
    r"wifey|missus|mrs|husband|hubby|fiancee?|son|daughter|kids?|children|child|mum|mom|dad|"
    r"mother|father|parents|sister|brother|friends?|mates?|flatmates?|housemates?|family|baby|"
    r"guests?)"
)
#: a third person in the sentence makes it about somebody else: "she's allergic", "he can't
#: stand", "my girlfriend is not keen". Never a bare "is", "my" or "our": the sender's own
#: sentences hold them ("the dal is horrible, no more lentils", "my stomach says no more
#: chickpeas"), and sent them to the chat. Each person changes only
#: their own list. The person word (group 1) is a food's first word as well: "the BABY spinach".
_SOMEONE_ELSE = re.compile(
    rf"\b(?:she|he|shes|hes|(?:my|our|his|her|their|the) ({_PEOPLE})|"
    rf"for (?:her|him|them|(?:my|our) {_PEOPLE}))\b",
    re.I,
)
#: "they" is the food as often as people ("they were awful"), so it counts only as the subject:
#: in the words directly before the cue, after the last comma or full stop ("they hate salmon")
_THEY = re.compile(r"\bthey\b", re.I)
#: what stands where a name would and is not one: a pronoun, or the word a sentence with its
#: "I" dropped begins with ("Honestly can't stand salmon", "Just won't eat it")
_NOT_A_NAME = (
    r"(?:it|its|this|that|which|what|who|everything|something|anything|nothing|i|im|id|ive|"
    r"we|you|really|honestly|just|seriously|still|also|actually|definitely|def|genuinely|"
    r"literally|totally|absolutely|simply|frankly|truly|personally|sadly|now|so|and|but|well|"
    r"yeah|yes|nope|nah|ugh|tbh|ok|okay|sorry|please)\b"
)
#: a name as the whole subject: "Tom doesn't eat salmon", "Tom IS allergic", "Tom's not keen",
#: never "my stomach doesn't", "the dal doesn't" or "it doesn't" - the sender's own sentences
#: (#34)
_NAMED = re.compile(
    rf"(?!{_NOT_A_NAME})[a-z]+('s| is)?"
    r"(?: (?:really|just|still|also|actually|definitely|very|so|totally|seriously))?",
    re.I,
)
#: the cues a person is said to be, not to do: "Tom IS allergic", "Tom is NOT KEEN"
_IS_CUES = ("allergic", "intolerant", "not keen", "not a fan")
#: the words that look back for the dish just eaten, as "again" does: "no more chicken and
#: rice", "I'm sick of chicken and rice" (#37)
_LOOKS_BACK = re.compile(r"\b(?:again|no more|any ?more|sick of|had enough|fed up)\b", re.I)
#: how many dishes a swap offers before "More dishes"
FIRST_OFFER = 3
#: the most dishes "Swap a dish" lists on one screen before "Later days"
PICK_DISHES = 8
#: how far back "I don't want chicken and rice again" looks for the dish just eaten
RECENT_DAYS = 14
_SLOT_WORDS = {
    "breakfast": "Breakfast",
    "lunch": "Lunch",
    "dinner": "Dinner",
    "snack": "Snack",
    "pre_bed": "Pre-bed",
}


#: the most foods /avoid offers a button for, so the message stays one screen
AVOID_BUTTONS = 10


def _drop_tapped(h: Handlers, inbound: Inbound, *, row: bool = False) -> None:
    """Take the button just tapped off its message - with `row`, the whole row, both answers to
    one question - and leave every other button where it was. Clearing them all took "Swap it
    now" and Undo off a refusal when its houmous question was answered, and every other Allow
    off /avoid at the first tap. The keyboard is the one Telegram sent
    with the tap; without it, the buttons are cleared as before."""
    message = (inbound.update.get("callback_query") or {}).get("message") or {}
    rows = (message.get("reply_markup") or {}).get("inline_keyboard")
    if inbound.message_id is None or not isinstance(rows, list):
        h.clear_buttons(inbound)
        return
    tapped = inbound.callback_data
    kept: list[list[dict[str, Any]]] = []
    for buttons in rows:
        if row and any(b.get("callback_data") == tapped for b in buttons):
            continue
        left = [b for b in buttons if b.get("callback_data") != tapped]
        if left:
            kept.append(left)
    h.outbox.api.edit_reply_markup(
        inbound.chat_id, inbound.message_id, {"inline_keyboard": kept} if kept else None
    )


#: for menuflow's "Rebuild now" under a wish, which leaves the Undo beside it
drop_tapped = _drop_tapped


def note_ids(db: Db, person_id: int, texts: Iterable[str]) -> list[int]:
    """The rows standing for these notes now: what a button under them names."""
    out: list[int] = []
    for text in texts:
        found = db.scalar(
            "SELECT id FROM v_person_notes_active WHERE person_id = ? AND text = ?",
            (person_id, text),
        )
        if found is not None:
            out.append(int(found))
    return out


def notes_by_ids(db: Db, person_id: int, parts: list[str]) -> list[str]:
    """The notes a button's ids still stand for, oldest first - none once taken back."""
    ids = [int(p) for p in ",".join(parts).split(",") if p.strip().isdigit()]
    if not ids:
        return []
    rows = db.all(
        f"SELECT text FROM v_person_notes_active WHERE person_id = ? "
        f"AND id IN ({','.join('?' * len(ids))}) ORDER BY id",
        (person_id, *ids),
    )
    return [str(r["text"]) for r in rows]


def ask_rows(asks: Any) -> list[list[tuple[str, str]]]:
    """One row per made-from question: keep it off too, or it is fine."""
    return [
        [
            (f"Keep {a['name'].lower()} off"[:40], f"k:madeof:{a['id']}:y"),
            (f"{a['name']} is fine"[:40], f"k:madeof:{a['id']}:n"),
        ]
        for a in list(asks)[:3]
    ]


def refusal_terms(text: str, *, others: Iterable[str] = (), bare_no: bool = True) -> list[str]:
    """The foods a free-text message refuses for good, or [] when it is not that kind of
    message. Deliberately narrow: a question goes to the chat, a report of the day to the
    check-in, and only a sentence that says "not this food, from now on" is taken here.

    A phone's curly apostrophe is read as a straight one: "I don't want lentils" typed on one
    missed every cue, and at 20:45 became the day's food note.
    to his kept salmon off his menus and left it on hers (#34). `bare_no` is False while the
    evening's "what changed?" waits, when "no chicken, rice instead" may be what changed today
    rather than a refusal for good (#26).
    """
    said = _APOSTROPHES.sub("'", (text or "").strip())
    if not said or said.endswith("?") or len(said) > 200:
        return []
    if _REPORT.search(_NOT_A_REPORT.sub(" ", said)):
        return []
    cue = _CUE.search(said)
    if cue is None and not (bare_no and _BARE_NO.match(said)):
        return []
    terms = catalogue.avoid_terms(said)
    if _about_someone_else(said, cue, others, terms):
        return []
    return terms


def _about_someone_else(
    said: str, cue: re.Match[str] | None, others: Iterable[str], terms: list[str]
) -> bool:
    """Whether the sentence is about somebody else's food: a third person in it, "they" or a
    doesn't like lentils" is the sender's own, and went to the chat.

    A person word that begins the food refused is the food: "I don't want the baby spinach"
    went to the chat as a sentence about the baby. A name is somebody else before "doesn't",
    before "is allergic" or "is not keen" (never the sender with "I" dropped), and, written
    with its capital, before "can't stand" or "won't eat": "Tom can't stand salmon" was kept
    off the sender's menus, "Honestly can't stand salmon" is still the sender's own."""
    before = said[: cue.start()] if cue is not None else ""
    subject = re.split(r"[,;:.!?]| - ", before)[-1].strip()
    if _THEY.search(subject) or any(
        not _starts_a_food(said, m, terms) for m in _SOMEONE_ELSE.finditer(said)
    ):
        return True
    named = _NAMED.fullmatch(subject)
    if cue is not None and named is not None:
        word = cue.group(0).casefold()
        if named.group(1):
            if word in _IS_CUES:
                return True  # "Tom is allergic to salmon"
        elif word.startswith("does") or (
            word.startswith(("can", "won", "will")) and subject[0].isupper()
        ):
            return True  # "Tom doesn't eat salmon", "Tom can't stand salmon"
    names = {n.split()[0].casefold() for n in others if n.strip()}
    return any(word in names for word in re.findall(r"[a-z]+", said.casefold()))


def _starts_a_food(said: str, person: re.Match[str], terms: list[str]) -> bool:
    """Whether the person word a `_SOMEONE_ELSE` match found is the first word of a food the
    sentence refuses: "the BABY spinach", "the baby plum tomatoes"."""
    at = person.start(1)
    if at < 0:
        return False
    rest = catalogue.normalise(said[at:])
    return any(rest == t or rest.startswith(f"{t} ") for t in terms)


def _dish_said(db: Db, person_id: int, text: str, terms: list[str]) -> tuple[str, list[str]]:
    """The dish a refusal names as two words joined by "and", one of them no food, and those
    two terms - or ("", []). "I don't want fish and chips again" is the dish, and "chips" is
    two are the dish: the rest of a list is still what it says, and "I don't want lentils,
    salmon and kale" holding back the lentils too kept nothing off."""
    if re.search(r"allerg|intoleran|coeliac|celiac", text, re.IGNORECASE):
        # an allergy list is never a dish: "allergic to prawns and sesame", with no sesame in the
        # catalogue, kept nothing off, prawns included
        return "", []
    foods = prefs.scoped(db, person_id)
    said = f" {catalogue.fold_term(text)} "
    for first, second in pairwise(terms):
        joined = f" {catalogue.fold_term(f'{first} and {second}')} "
        if joined in said and prefs.names_food(first, foods) != prefs.names_food(second, foods):
            return f"{first} and {second}", [first, second]
    return "", []


def _keep_terms(db: Db, person_id: int, terms: Iterable[str]) -> list[str]:
    """The terms that would keep a food still on the menus off, each fit for a k:avadd button:
    a word, not one product, so "olives" keeps every olive off."""
    foods = catalogue.approved(db, person_id)
    return [
        t
        for t in terms
        if f"k:avadd:{t}".isascii()
        and len(f"k:avadd:{t}".encode()) <= MAX_CALLBACK_BYTES
        and any(catalogue.refused_matches(catalogue.normalise(t), f) for f in foods)
    ]


def _evening_question(db: Db, person_id: int, *, clock: Clock) -> str:
    """Where the evening's "What changed?" stands: "asked" when a Not quite tap waits for the
    typed answer, "open" while the check-in has armed it or its meal buttons wait, and "" when
    it is not open (or has gone stale, which `dayflow` says itself)."""
    row = db.one(
        "SELECT state_json, updated_at FROM conversation_state WHERE person_id = ?", (person_id,)
    )
    if row is None:
        return ""
    try:
        state = json.loads(str(row["state_json"]))
    except ValueError:
        return ""
    if not isinstance(state, dict) or state.get("flow") != dayflow.OFFPLAN_FLOW:
        return ""
    age = (clock.now_utc() - parse_stamp(str(row["updated_at"]))).total_seconds()
    if age > dayflow.OFFPLAN_TTL:
        return ""
    return "open" if state.get("armed") or state.get("note") is not None else "asked"


def start_new_dishes(
    h: Handlers,
    app: Any,
    person: Any,
    day: dt.date,
    slot: str,
    *,
    every_day: bool,
    note: str = "",
    clock: Clock,
    lead: str = "",
) -> str:
    """Ask the model for two or three new dishes for one meal, on the person's build lane, and
    send them with a button each; the rest of the menu is untouched (food/newdish.py)."""
    from .. import building  # noqa: PLC0415
    from ..errors import ModelUnavailable  # noqa: PLC0415
    from ..food import newdish  # noqa: PLC0415 (food imports the db layer)

    meal = _SLOT_WORDS.get(slot, slot).lower()
    if app is None:
        h.outbox.reply(
            person, "I cannot ask for new dishes from here. /swap shows the ones you have had."
        )
        return "handled"
    now = building.current(h.db, person.id, clock=clock)
    if now is not None:
        h.outbox.reply(person, building.busy(now))
        return "handled"
    with_note = f", with {esc(note)}" if note else ""
    h.outbox.reply(
        person,
        (lead + " " if lead else "")
        + f"Asking for new {meal} dishes{with_note}: {newdish.TAKES}. They come here, two or "
        "three to choose from.",
    )
    token = building.mark(
        h.db, person.id, f"new {meal} dishes", clock=clock, kind=building.DISHES, day=day, slot=slot
    )
    tail = "" if every_day else ":1"

    def build(db: Any) -> None:
        outbox = h.outbox if db is h.db else app.outbox(db)
        try:
            got = newdish.ask(
                app, db, person.id, day, slot, note=note, every_day=every_day, clock=clock
            )
        except ModelUnavailable:
            outbox.reply(
                person,
                "I cannot ask for new dishes right now. "
                f"<code>{newdish.swap_command(day, meal, clock.today_local())}</code> "
                "shows the dishes you have had, at once.",
            )
            return
        except (NothingToBuild, ValueError) as e:
            outbox.reply(person, esc(str(e)) + " <code>/menu</code> shows what is planned.")
            return
        lines = [esc(got.said())]
        lines += [
            f"{esc(d['name'])}: {d['kcal']:,} kcal, P {d['protein_g']} g, {d['prep_minutes']} min"
            for d in got.dishes
        ]
        if got.together:
            lines.append(f"{esc(got.together)}'s plate changes too.")
        rows = [
            [(str(d["name"])[:40], f"k:swap:{isoday(day)}:{slot}:{d['recipe_id']}{tail}")]
            for d in got.dishes
        ]
        outbox.reply(person, "\n".join(lines), keyboard=inline_keyboard(rows) if rows else None)

    broke = (
        f"Something went wrong asking for new {meal} dishes. "
        f"<code>{newdish.swap_command(day, meal, clock.today_local())}</code> "
        "shows the dishes you have had, at once."
    )
    app.run_build(person.id, building.guarded(app, person, token, build, broke=broke), h.db)
    return "handled"


class FoodFlow:
    def __init__(self, *, config: Any = None, clock: Clock = DEFAULT_CLOCK, app: Any = None):
        self.config = config
        self.clock = clock
        self.app = app

    def new_dishes(self, h: Handlers, inbound: Inbound, parts: list[str]) -> str:
        """k:newdish:<day>:<slot>[:1] - two or three new dishes for the meal, from the model."""
        if len(parts) < 2 or parts[1] not in _SLOT_WORDS:
            return "ignored"
        try:
            day = parse_day(parts[0])
        except ValueError:
            return "ignored"
        if day < self.clock.today_local():
            return "ignored"
        every_day = not (len(parts) > 2 and "1" in parts[2])
        h.clear_buttons(inbound)
        return start_new_dishes(
            h, self.app, inbound.person, day, parts[1], every_day=every_day, clock=self.clock
        )

    # ------------------------------------------------------------ "I do not want lentils"
    def avoid_text(self, h: Handlers, inbound: Inbound, text: str) -> str | None:
        person = inbound.person
        asked = _evening_question(h.db, person.id, clock=self.clock)
        if asked == "asked":
            # "What changed?", after a Not quite tap: this message is its answer, whatever it
            # looks like. "No chicken, rice instead" is what she ate, and reading it as a
            # refusal kept both off for good and lost the day
            return None
        # the name each was given (person_names), not persons.display_name: hers is blank there
        others = [
            shared.name_of(h.db, int(r["id"]))
            for r in h.db.all("SELECT id FROM persons WHERE id != ?", (person.id,))
        ]
        terms = refusal_terms(text, others=others, bare_no=not asked)
        if not terms:
            return None
        dish = self._dish_named(h, person.id, text, terms)
        if dish is not None:
            return self._ask_about_dish(h, inbound, dish, terms)
        if prefs.half_read(h.db, person.id, terms):
            return None
        # "fish and chips" may be a dish: those two words are held back and asked about, and
        # the rest of the list is kept off as said (#37)
        phrase, pair = _dish_said(h.db, person.id, text, terms)
        written = [t for t in terms if t not in pair]
        if not written:
            return self._ask_about_phrase(h, inbound, phrase, pair)
        try:
            changed = prefs.amend(h.db, person.id, "avoid", add=written, clock=self.clock)
        except LookupError:
            return None  # no profile yet: the interview or the chat answers them
        if not changed.added and not changed.already:
            # every word named nothing on the menu ("I hate faff"): not a refusal after all
            return self._ask_about_phrase(h, inbound, phrase, pair) if phrase else None
        lines, rows = self._said_back(h, person.id, changed)
        held = _keep_terms(h.db, person.id, pair)
        if held:
            lines.append(
                f"<b>{esc(phrase.capitalize())}</b> sounds like one dish, so it is not kept "
                f"off. Keep {esc(', '.join(held))} off for good?"
            )
            rows += [[(f"Keep {t} off"[:40], f"k:avadd:{t}")] for t in held]
        # "No chicken, salmon please": the salmon may be the food asked for, so it is not
        # written - but it is named, one tap from kept off by its word, never lost (#23)
        aside = _keep_terms(h.db, person.id, catalogue.set_aside(text))
        if aside:
            each = "it" if len(aside) == 1 else "each"
            lines.append(esc(f"Not kept off: {', '.join(aside)}. Tap to keep {each} off too."))
            rows += [[(f"Keep {t} off too"[:40], f"k:avadd:{t}")] for t in aside]
        h.outbox.reply(person, "\n".join(lines), keyboard=inline_keyboard(rows) if rows else None)
        return "handled"

    def wish_text(self, h: Handlers, inbound: Inbound, text: str) -> str | None:
        """ "Oh, there was too much chia seed": a wish about every menu, kept at once as a
        says something like 'oh, there was too much chia seed' - it will rebuild again").

        Read in code, after a refusal ("no more lentils" is the avoid list) and before the
        evening check-in and the chat: from 21:00 the check-in took "too much chia seed" as what
        she ate, and the chat had nowhere to keep it. Only a food the catalogue knows, or a word
        for a kind of food, makes a wish (`prefs.wishes_in`); anything else is the chat's. Kept,
        not asked: a note is one tap from undone, and a question would be one more thing to
        answer before anything changed."""
        person = inbound.person
        if _evening_question(h.db, person.id, clock=self.clock) == "asked":
            return None  # "What changed?", after Not quite: this message is its answer
        said = _APOSTROPHES.sub("'", (text or "").strip())
        if not said or len(said) > prefs.MAX_NOTE:
            return None
        if said.endswith("?") and not _ASKING.match(said):
            return None  # a question is the chat's to answer
        if _INTAKE.search(said):
            return None  # "I ate too much chocolate" is the day's food, the check-in's
        if _TOO.search(said) is None and _REPORT.search(_NOT_A_REPORT.sub(" ", said)):
            return None  # "had less rice today" is what she ate
        wishes = prefs.wishes_in(h.db, person.id, said)[:MAX_WISHES]
        if not wishes:
            return None
        noted = prefs.amend_notes(h.db, person.id, add=wishes, source="chat", clock=self.clock)
        try:
            liked = prefs.less_of_liked(h.db, person.id, noted.added, clock=self.clock)
        except LookupError:
            liked = None  # no profile yet: nothing on a likes list to take off
        lines: list[str] = []
        if noted.added:
            lines.append(f"Kept for every menu from now on: {esc('; '.join(noted.added))}.")
        if noted.already:
            lines.append(f"Already kept: {esc('; '.join(noted.already))}.")
        if noted.refused:
            lines.append(
                f"Your notes are full ({prefs.MAX_NOTES}), so this was not kept: "
                f"{esc('; '.join(noted.refused))}."
            )
        if liked is not None and liked.removed:
            lines.append(f"Taken off your liked foods: {esc(', '.join(liked.removed))}.")
        standing = note_ids(h.db, person.id, [*noted.added, *noted.already])
        if not standing:
            h.outbox.reply(person, "\n".join(lines))
            return "handled"
        lines.append(
            "Rebuild the menu you have with it now? It usually takes 2-4 minutes, and your "
            "menu stays until the new one is ready."
        )
        rows = [[("Rebuild now", f"k:wishbuild:{','.join(map(str, standing))}")]]
        added = note_ids(h.db, person.id, noted.added)
        if added:
            rows[0].append(("Undo", f"k:wishundo:{','.join(map(str, added))}"))
        h.outbox.reply(person, "\n".join(lines), keyboard=inline_keyboard(rows))
        return "handled"

    def wish_undo(self, h: Handlers, inbound: Inbound, parts: list[str]) -> str:
        """k:wishundo:<note ids> - take back the wishes that message kept, and nothing else."""
        person = inbound.person
        texts = notes_by_ids(h.db, person.id, parts)
        h.clear_buttons(inbound)
        if not texts:
            h.outbox.reply(person, "Already taken back.")
            return "handled"
        noted = prefs.amend_notes(h.db, person.id, drop=texts, source="chat", clock=self.clock)
        h.outbox.reply(
            person,
            f"Taken back: {esc('; '.join(noted.dropped))}. Menus will not be asked for it.",
        )
        return "handled"

    def _said_back(
        self, h: Handlers, person_id: int, changed: prefs.Changed
    ) -> tuple[list[str], list[list[tuple[str, str]]]]:
        """A change to the avoid list in words and buttons: what it keeps off, the dishes still
        holding it (Swap), Undo, and the made-from questions."""
        lines = [esc(changed.said())]
        still = prefs.still_on_menu(h.db, person_id, self.clock.today_local())
        rows: list[list[tuple[str, str]]] = []
        if still:
            lines.append(esc(prefs.still_said(still).replace(" Send /swap and I will", " I can")))
            rows.append([("Swap them now" if len(still) > 1 else "Swap it now", "k:swapall")])
        if changed.added and changed.row_id is not None:
            # today's own row: one booked for a later day carries the same change, and is not
            # the one to undo from (targets.change_profile)
            rows.append([("Undo", f"k:unavoid:{changed.row_id}")])
            lines.append("<code>/avoid</code> shows the whole list.")
        for ask in changed.asks[:3]:
            lines.append(esc(prefs.ask_said(ask)))
        rows += ask_rows(changed.asks)
        return lines, rows

    def keep_term(self, h: Handlers, inbound: Inbound, parts: list[str]) -> str:
        """k:avadd:<term> - a word said beside a refusal and held back ("salmon" in "No chicken,
        salmon or tuna please", "fish" in "fish and chips"), kept off now by the word, as the
        sentence would have: "olives" is every olive, where a button per product kept one
        off. The message keeps its other buttons, so each word held back
        is a tap of its own."""
        term = ":".join(parts).strip()
        if not term:
            return "ignored"
        person = inbound.person
        try:
            with h.db.tx():
                changed = prefs.amend(h.db, person.id, "avoid", add=[term], clock=self.clock)
        except LookupError:
            return "ignored"
        lines, rows = self._said_back(h, person.id, changed)
        h.outbox.reply(person, "\n".join(lines), keyboard=inline_keyboard(rows) if rows else None)
        return "handled"

    def _dish_named(
        self, h: Handlers, person_id: int, text: str, terms: list[str]
    ) -> dict[str, Any] | None:
        """The dish a refusal names, with the days it is on, or None: one still to come first,
        else - when the sentence says "again", "no more" or "sick of" - one from the last
        fortnight's menus.

        "I don't want chicken and rice again" is the dish on the menu, not chicken and rice
        each refused for good: the sentence holds the dish's name, or
        the foods it joins with "and" are the dish's name in that order. The likeliest moment
        to say it is just after eating it, with the day recorded or the dish behind her, when
        each food was still refused for good; so with "again" (`_LOOKS_BACK`: "no more chicken
        and rice" says it as well) a recent dish counts too, and it has `to_come` False.
        """
        today = self.clock.today_local()
        again = _LOOKS_BACK.search(text) is not None
        since = today - dt.timedelta(days=RECENT_DAYS) if again else today
        said = f" {catalogue.fold_term(text)} "
        joined = f" {catalogue.fold_term(' and '.join(terms))} " if len(terms) > 1 else ""
        rows = h.db.all(
            "SELECT md.day, ms.slot, ms.recipe_id, rc.name FROM v_menu_days_current md "
            "JOIN meal_slots ms ON ms.menu_day_id = md.id JOIN recipes rc ON rc.id = ms.recipe_id "
            "WHERE md.person_id = ? AND md.day >= ? ORDER BY md.day",
            (person_id, isoday(since)),
        )
        found: dict[bool, dict[str, Any]] = {}
        for r in rows:
            name = catalogue.fold_term(str(r["name"]))
            if not ((" " in name and f" {name} " in said) or (joined and joined in f" {name} ")):
                continue
            day = parse_day(str(r["day"]))
            to_come = day >= today and not menu_mod.day_confirmed(h.db, person_id, day)
            if not (to_come or again):
                continue
            dish = found.setdefault(
                to_come,
                {
                    "slot": str(r["slot"]),
                    "recipe_id": int(r["recipe_id"]),
                    "name": str(r["name"]),
                    "days": [],
                    "to_come": to_come,
                },
            )
            if (dish["slot"], dish["recipe_id"]) == (str(r["slot"]), int(r["recipe_id"])):
                dish["days"].append(day)
        return found.get(True) or found.get(False)

    def _ask_about_phrase(
        self, h: Handlers, inbound: Inbound, phrase: str, pair: list[str]
    ) -> str | None:
        """A dish said by name ("fish and chips") that is on no menu: nothing is written, and
        the words in it naming a food are each one tap from kept off, by the word (k:avadd) -
        or, none left to keep off, it is left to the chat."""
        held = _keep_terms(h.db, inbound.person.id, pair)
        if not held:
            return None
        h.outbox.reply(
            inbound.person,
            f"<b>{esc(phrase.capitalize())}</b> sounds like one dish, so nothing is kept off. "
            f"Keep {esc(', '.join(held))} off for good?",
            keyboard=inline_keyboard([[(f"Keep {t} off"[:40], f"k:avadd:{t}")] for t in held]),
        )
        return "handled"

    def _ask_about_dish(
        self, h: Handlers, inbound: Inbound, dish: dict[str, Any], terms: list[str]
    ) -> str | None:
        """Swap the dish, or keep one of its foods off for good: asked with a button each, and
        nothing written until one is tapped. A dish behind her has nothing left to swap, so it
        is asked about its foods alone - or, naming none of them, left to the chat."""
        from ..food import recipes  # noqa: PLC0415 (food imports the db layer)

        slot: str = dish["slot"]
        first = isoday(dish["days"][0])
        folded = [catalogue.normalise(t) for t in terms]
        foods = [
            s.ingredient
            for s in recipes.stored_ingredients(h.db, dish["recipe_id"])
            if any(catalogue.refused_matches(t, s.ingredient) for t in folded)
        ]
        never = [
            [(f"Never {f.name.split(',')[0].lower()} again"[:40], f"k:never:{f.id}")]
            for f in foods[:3]
        ]
        meal = _SLOT_WORDS.get(slot, slot).lower()
        if not dish["to_come"]:
            if not foods:
                return None
            h.outbox.reply(
                inbound.person,
                f"<b>{esc(dish['name'])}</b> was on your menu "
                f"({esc(meal)} {esc(prefs.days_words(dish['days'][-1:]))}). "
                "Keep one of its foods off for good?",
                keyboard=inline_keyboard(never),
            )
            return "handled"
        rows = [[("Swap it", f"k:swapmeal:{first}:{slot}")], *never]
        if not foods:
            rows.append([("Change one food in it", f"k:subst:{first}:{slot}")])
        when = f"{meal} {prefs.days_words(dish['days'])}"
        ask = (
            "Swap the dish, or keep one of its foods off for good?"
            if foods
            else "Swap the dish, or change one food in it?"
        )
        h.outbox.reply(
            inbound.person,
            f"<b>{esc(dish['name'])}</b> is on your menu ({esc(when)}). {ask}",
            keyboard=inline_keyboard(rows),
        )
        return "handled"

    def made_of(self, h: Handlers, inbound: Inbound, parts: list[str]) -> str:
        """k:madeof:<ingredient id>:<y|n> - houmous after chickpeas: kept either way, so the
        question is asked once."""
        if len(parts) < 2 or not parts[0].isdigit() or parts[1] not in ("y", "n"):
            return "ignored"
        try:
            with h.db.tx():
                got = prefs.answer(
                    h.db,
                    inbound.person.id,
                    int(parts[0]),
                    keep_off=parts[1] == "y",
                    clock=self.clock,
                )
        except LookupError:
            return "ignored"
        _drop_tapped(h, inbound, row=True)
        if got.refused:
            return self._kept_off(h, inbound, got, int(parts[0]))
        h.outbox.reply(inbound.person, esc(got.said()))
        return "handled"

    def _kept_off(self, h: Handlers, inbound: Inbound, got: prefs.FoodSaid, food_id: int) -> str:
        person = inbound.person
        lines = [esc(got.said())]
        rows: list[list[tuple[str, str]]] = []
        still = prefs.still_on_menu(h.db, person.id, self.clock.today_local())
        if still:
            lines.append(esc(prefs.still_said(still).replace(" Send /swap and I will", " I can")))
            rows.append([("Swap them now" if len(still) > 1 else "Swap it now", "k:swapall")])
        if got.changed:
            rows.append([("Undo", f"k:avok:{food_id}")])
        h.outbox.reply(person, "\n".join(lines), keyboard=inline_keyboard(rows) if rows else None)
        return "handled"

    def never(self, h: Handlers, inbound: Inbound, parts: list[str]) -> str:
        """k:never:<ingredient id> - that one food never again, by itself: a word can reach
        further than the food tapped ("eggs" keeps egg whites off too); a code cannot."""
        if not parts or not parts[0].isdigit():
            return "ignored"
        try:
            with h.db.tx():
                got = prefs.refuse_food(h.db, inbound.person.id, int(parts[0]), clock=self.clock)
        except LookupError:
            return "ignored"
        _drop_tapped(h, inbound)
        return self._kept_off(h, inbound, got, int(parts[0]))

    def allow_food(self, h: Handlers, inbound: Inbound, parts: list[str]) -> str:
        """k:avok:<ingredient id> - one food refused by itself, back on the menus."""
        if not parts or not parts[0].isdigit():
            return "ignored"
        try:
            with h.db.tx():
                got = prefs.allow_food(h.db, inbound.person.id, int(parts[0]), clock=self.clock)
        except LookupError:
            return "ignored"
        _drop_tapped(h, inbound)
        h.outbox.reply(inbound.person, esc(got.said()))
        return "handled"

    def allow_term(self, h: Handlers, inbound: Inbound, parts: list[str]) -> str:
        """k:avrm:<profiles id>:<n> - the n-th word of the avoid list as /avoid showed it."""
        if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
            return "ignored"
        person = inbound.person
        row = h.db.one(
            "SELECT refused_foods FROM profiles WHERE id = ? AND person_id = ?",
            (int(parts[0]), person.id),
        )
        if row is None:
            return "ignored"
        try:
            shown = sorted(json.loads(str(row["refused_foods"] or "[]")), key=str.casefold)
        except ValueError:
            return "ignored"
        n = int(parts[1])
        if n >= len(shown):
            return "ignored"
        with h.db.tx():
            changed = prefs.amend(h.db, person.id, "avoid", remove=[shown[n]], clock=self.clock)
        _drop_tapped(h, inbound)
        h.outbox.reply(
            person, esc(changed.said() if changed.removed else "Already back on your menus.")
        )
        return "handled"

    def cmd_avoid(self, h: Handlers, inbound: Inbound, arg: str) -> str:
        """`/avoid` - everything kept off your menus, each with a button to have it back.
        `/avoid mushrooms, olives` - add to it."""
        person = inbound.person
        if arg.strip():
            words = [w.strip() for w in re.split(r",|\band\b", arg) if w.strip()]
            try:
                with h.db.tx():
                    changed = prefs.amend(
                        h.db, person.id, "avoid", add=words, clock=self.clock, strict=False
                    )
            except LookupError:
                h.outbox.reply(person, "Finish <code>/onboard</code> first.")
                return "handled"
            lines = [esc(changed.said())]
            rows: list[list[tuple[str, str]]] = []
            if changed.added:
                still = prefs.still_on_menu(h.db, person.id, self.clock.today_local())
                if still:
                    lines.append(
                        esc(prefs.still_said(still).replace(" Send /swap and I will", " I can"))
                    )
                    rows.append(
                        [("Swap them now" if len(still) > 1 else "Swap it now", "k:swapall")]
                    )
            for ask in changed.asks[:3]:
                lines.append(esc(prefs.ask_said(ask)))
            rows += ask_rows(changed.asks)
            h.outbox.reply(
                person, "\n".join(lines), keyboard=inline_keyboard(rows) if rows else None
            )
            return "handled"
        today = self.clock.today_local()
        try:
            view = prefs.view(h.db, person.id, today)
        except LookupError:
            return "ignored"
        words = sorted(view["avoid"], key=lambda e: str(e["term"]).casefold())
        foods = view["avoid_foods"]
        if not words and not foods:
            h.outbox.reply(
                person,
                "Nothing is kept off your menus. Say it the way you would: "
                "<i>no more mushrooms</i>, or <code>/avoid mushrooms</code>.",
            )
            return "handled"
        lines = ["<b>Kept off your menus</b>"]
        if words:
            lines.append(esc("; ".join(prefs.kept(str(e["term"]), e["foods"]) for e in words)))
        if foods:
            lines.append("By itself: " + esc(", ".join(str(f["name"]) for f in foods)))
        for ask in view["asks"][:2]:
            lines.append(esc(prefs.ask_said(ask)))
        profile_id = h.db.scalar(
            "SELECT id FROM profiles WHERE person_id = ? AND effective_from <= ? "
            "ORDER BY effective_from DESC, id DESC LIMIT 1",
            (person.id, today.isoformat()),
        )
        # a food refused by itself first: cut at ten with the words first, it had no button at
        # all once ten words were listed
        taps = [(f"Allow {f['name']}"[:30], f"k:avok:{f['id']}") for f in foods]
        shown = sorted(prefs.terms_of(h.db, person.id, "avoid", today), key=str.casefold)
        for i, term in enumerate(shown):
            taps.append((f"Allow {term}"[:30], f"k:avrm:{profile_id}:{i}"))
        # and one without a button is said how to have it back, by name, never left out
        more = (
            ", or <code>/set avoid - name</code> for one with no button"
            if len(taps) > AVOID_BUTTONS
            else ""
        )
        lines.append(f"Tap one to have it back{more}. To add: <code>/avoid mushrooms</code>.")
        taps = taps[:AVOID_BUTTONS]
        rows = [taps[i : i + 2] for i in range(0, len(taps), 2)]
        rows += ask_rows(view["asks"][:2])
        h.outbox.reply(person, "\n".join(lines), keyboard=inline_keyboard(rows))
        return "handled"

    # ------------------------------------------------------------ one plate's size
    def size_meal(self, h: Handlers, inbound: Inbound, parts: list[str]) -> str:
        """k:size:<day>:<slot>:<level>[:undo] - one plate smaller, bigger, left out, or as
        planned; the day solved again in code, every other plate where it was (food/plates.py).

        On today the meals the clock has passed are held as eaten: Less on tonight's dinner
        re-sized the breakfast and lunch already eaten, and the reply named neither. The card
        it came from keeps its buttons (a level is the same however often it is tapped), so Back
        to the menu and the other meals are still there; only a used Undo is taken off."""
        from ..food import plates  # noqa: PLC0415 (food imports the db layer)

        if len(parts) < 3 or parts[2] not in plates.LEVELS:
            return "ignored"
        try:
            day = parse_day(parts[0])
        except ValueError:
            return "ignored"
        person = inbound.person
        slot, level = parts[1], parts[2]
        meal = _SLOT_WORDS.get(slot, slot)
        eaten = plates.past_meals(h.db, person.id, day, self.clock)
        if slot in eaten:
            h.outbox.reply(
                person,
                f"{esc(meal)} is already behind you today, so it stays as it was. If it went "
                "differently, tap Not quite at the evening check-in.",
            )
            return "handled"
        had = dict((plates.state(h.db, person.id, day) or {}).get("levels", {}))
        before = str(had.get(slot, "planned"))
        levels = {**had, slot: level}
        one = plates.one_skip(had, levels)
        if one is not None and plates.skip_mode(h.db, person.id, day) == "drop":
            # their Skip just drops the meal: the rest of the day stays exactly as planned
            try:
                said = plates.skip_meal(
                    h.db, person.id, day, one[0], one[1], config=self.config, clock=self.clock
                )
            except (NothingToBuild, ValueError) as e:
                h.outbox.reply(person, esc(str(e)) + " <code>/menu</code> shows the day.")
                return "handled"
            if len(parts) > 3 and parts[3] == "undo":
                h.clear_buttons(inbound)
            undo = f"k:size:{isoday(day)}:{slot}:{before}:undo"
            h.outbox.reply(
                person,
                esc(said),
                keyboard=inline_keyboard([[("Undo", undo)]]) if before != level else None,
            )
            return "handled"
        try:
            with h.db.tx():
                got = plates.resize(
                    h.db,
                    person.id,
                    day,
                    levels,
                    eaten=eaten,
                    config=self.config,
                    clock=self.clock,
                )
        except (NothingToBuild, Infeasible, ValueError) as e:
            h.outbox.reply(person, esc(str(e)) + " <code>/menu</code> shows the day.")
            return "handled"
        if len(parts) > 3 and parts[3] == "undo":
            h.clear_buttons(inbound)
        what = {"skip": "left out", "planned": "as planned"}.get(level, level)
        undo = f"k:size:{isoday(day)}:{slot}:{before}:undo"
        rows = [[("Undo", undo)]] if before != level else []
        h.outbox.reply(
            person,
            f"{esc(meal)} {what}. " + esc(" ".join(filter(None, [got.changes_said(), got.said()]))),
            keyboard=inline_keyboard(rows) if rows else None,
        )
        return "handled"

    # ------------------------------------------------------------ one food inside a dish
    def subst_pick(self, h: Handlers, inbound: Inbound, parts: list[str]) -> str:
        """k:subst:<day>:<slot>[:1] - which food in the dish to change."""
        if len(parts) < 2:
            return "ignored"
        try:
            day = parse_day(parts[0])
        except ValueError:
            return "ignored"
        person = inbound.person
        slot = parts[1]
        tail = ":1" if len(parts) > 2 and "1" in parts[2] else ""
        plate = next((p for p in swap.plates_on(h.db, person.id, day) if p.slot == slot), None)
        if plate is None:
            return "ignored"
        from ..food import recipes  # noqa: PLC0415 (food imports the db layer)

        foods = [
            s.ingredient
            for s in recipes.stored_ingredients(h.db, plate.recipe_id)
            if not recipes.is_seasoning(s.ingredient.name)
        ]
        if not foods:
            return "ignored"
        taps = [
            (f.name.split(",")[0][:30], f"k:substi:{day.isoformat()}:{slot}:{f.id}{tail}")
            for f in foods
        ]
        rows = [taps[i : i + 2] for i in range(0, len(taps), 2)]
        h.outbox.reply(
            person,
            f"<b>Change one food in {esc(plate.name)}</b>. Which one?",
            keyboard=inline_keyboard(rows),
        )
        return "handled"

    def subst_offer(self, h: Handlers, inbound: Inbound, parts: list[str]) -> str:
        """k:substi:<day>:<slot>:<ingredient id>[:1] - what could stand in for that food."""
        if len(parts) < 3 or not parts[2].isdigit():
            return "ignored"
        try:
            day = parse_day(parts[0])
        except ValueError:
            return "ignored"
        person = inbound.person
        slot, food_id = parts[1], int(parts[2])
        tail = ":1" if len(parts) > 3 and "1" in parts[3] else ""
        try:
            got = subst.options(h.db, person.id, day, slot, food_id, self.clock.today_local())
        except NothingToBuild as e:
            h.outbox.reply(person, esc(str(e)) + " <code>/menu</code> shows what is planned.")
            return "handled"
        except ValueError:
            return "ignored"
        food = got["food"]
        never = [(f"Never {food['name'].lower()} again"[:40], f"k:never:{food['id']}")]
        if not got["options"]:
            h.outbox.reply(
                person,
                f"Nothing I plan with plays the same part as {esc(food['name'])} "
                f"({esc(got['role'])}) for you. <code>/swap</code> changes the whole dish.",
                keyboard=inline_keyboard([never]),
            )
            return "handled"
        lines = [
            f"<b>Instead of {esc(food['name'])}</b> in {esc(got['dish'])} "
            f"({esc(got['role'])}); the day resizes to stay on target:"
        ]
        rows: list[list[tuple[str, str]]] = []
        for o in got["options"][: FIRST_OFFER + 1]:
            sign = "+" if o["kcal_delta"] > 0 else ""
            why = f"; {o['why']}" if o["why"] else ""
            lines.append(
                f"{esc(o['name'])} {o['grams']:.0f} g: {sign}{o['kcal_delta']} kcal a portion"
                f"{esc(why)}"
            )
            rows.append(
                [
                    (
                        o["name"][:40],
                        f"k:substdo:{day.isoformat()}:{slot}:{food['id']}:{o['id']}{tail}",
                    )
                ]
            )
        if got["together"]:
            lines.append(f"{esc(got['together'])}'s plate changes too.")
        rows.append(never)
        h.outbox.reply(person, "\n".join(lines), keyboard=inline_keyboard(rows))
        return "handled"

    def subst_do(self, h: Handlers, inbound: Inbound, parts: list[str]) -> str:
        """k:substdo:<day>:<slot>:<ingredient id>:<new id>[:1]"""
        if len(parts) < 4 or not parts[2].isdigit() or not parts[3].isdigit():
            return "ignored"
        try:
            day = parse_day(parts[0])
        except ValueError:
            return "ignored"
        person = inbound.person
        try:
            with h.db.tx():
                got = subst.substitute(
                    h.db,
                    person.id,
                    day,
                    parts[1],
                    int(parts[2]),
                    int(parts[3]),
                    every_day=not (len(parts) > 4),
                    config=self.config,
                    clock=self.clock,
                )
        except (NothingToBuild, Infeasible, ValueError) as e:
            h.outbox.reply(person, esc(str(e)) + " <code>/menu</code> shows what is planned.")
            return "handled"
        h.clear_buttons(inbound)
        h.outbox.reply(person, esc(got.said()))
        logger.info(
            "food swapped in a dish",
            extra={"event": "food_swapped", "person_id": person.id, "menu_id": got.menu_id},
        )
        return "handled"

    def unavoid(self, h: Handlers, inbound: Inbound, parts: list[str]) -> str:
        """k:unavoid:<profiles id> - take back what that one refusal added, and nothing else."""
        if not parts or not parts[0].isdigit():
            return "ignored"
        person = inbound.person
        row = h.db.one(
            "SELECT id, refused_foods FROM profiles WHERE id = ? AND person_id = ?",
            (int(parts[0]), person.id),
        )
        if row is None:
            return "ignored"
        before = h.db.one(
            "SELECT refused_foods FROM profiles WHERE person_id = ? AND id < ? "
            "ORDER BY id DESC LIMIT 1",
            (person.id, int(row["id"])),
        )
        now = {catalogue.fold_term(t) for t in json.loads(str(row["refused_foods"] or "[]"))}
        was = (
            {catalogue.fold_term(t) for t in json.loads(str(before["refused_foods"] or "[]"))}
            if before is not None
            else set()
        )
        added = [
            t
            for t in json.loads(str(row["refused_foods"] or "[]"))
            if catalogue.fold_term(t) in now - was
        ]
        if not added:
            return "ignored"
        changed = prefs.amend(h.db, person.id, "avoid", remove=added, clock=self.clock)
        h.clear_buttons(inbound)
        h.outbox.reply(
            person,
            esc(changed.said() if changed.removed else "Already back on your menus."),
        )
        return "handled"

    # ------------------------------------------------------------ swapping
    def cmd_swap(self, h: Handlers, inbound: Inbound, arg: str) -> str:
        """`/swap` - every dish still to come holding a food you avoid, swapped for one you have
        eaten before. `/swap thursday lunch` - the dishes that could stand in for that meal."""
        from .menuflow import day_and_meal  # noqa: PLC0415 (menuflow imports this module)

        person = inbound.person
        today = self.clock.today_local()
        if arg.strip():
            found = day_and_meal(arg, today)
            if found is None:
                h.outbox.reply(
                    person,
                    "Name a day and a meal: <code>/swap thursday lunch</code>, or "
                    "<code>/swap tomorrow dinner</code>.",
                )
                return "handled"
            day, slot = found
            return self._offer(h, inbound, day, slot, every_day=True)
        if not prefs.still_on_menu(h.db, person.id, today):
            h.outbox.reply(
                person,
                "Nothing still to come holds a food you avoid. To swap one meal: "
                "<code>/swap thursday lunch</code>, or the Swap button on a meal.",
            )
            return "handled"
        return self._swap_all(h, inbound)

    def swap_all(self, h: Handlers, inbound: Inbound, parts: list[str]) -> str:
        h.clear_buttons(inbound)
        return self._swap_all(h, inbound)

    def _swap_all(self, h: Handlers, inbound: Inbound) -> str:
        with h.db.tx():
            got = swap.swap_avoided(h.db, inbound.person.id, config=self.config, clock=self.clock)
        # a dish nothing already eaten can stand in for: the two ways on, from its first day
        rows: list[list[tuple[str, str]]] = []
        for s in got.stuck[:3]:
            at = f"{s['days'][0]}:{s['slot']}"
            food, new = (
                ("Change one food in it", "New dishes")
                if len(got.stuck) == 1
                else (f"{s['name'][:24]}: change one food", f"{s['name'][:24]}: new dishes")
            )
            rows.append([(food, f"k:subst:{at}"), (new, f"k:newdish:{at}")])
        h.outbox.reply(
            inbound.person, esc(got.said()), keyboard=inline_keyboard(rows) if rows else None
        )
        return "handled"

    def show_options(self, h: Handlers, inbound: Inbound, parts: list[str]) -> str:
        """k:swapmeal:<day>:<slot>[:<flags>] - flags "1" for that day only, "m" for more."""
        if len(parts) < 2:
            return "ignored"
        try:
            day = parse_day(parts[0])
        except ValueError:
            return "ignored"
        flags = parts[2] if len(parts) > 2 else ""
        return self._offer(h, inbound, day, parts[1], every_day="1" not in flags, more="m" in flags)

    def _offer(
        self,
        h: Handlers,
        inbound: Inbound,
        day: dt.date,
        slot: str,
        *,
        every_day: bool,
        more: bool = False,
    ) -> str:
        person = inbound.person
        today = self.clock.today_local()
        try:
            got = swap.options(h.db, person.id, day, slot, today)
        except NothingToBuild as e:
            h.outbox.reply(person, esc(str(e)) + " <code>/menu</code> shows what is planned.")
            return "handled"
        except ValueError:
            h.outbox.reply(
                person,
                f"There is no {_SLOT_WORDS.get(slot, slot).lower()} on {day:%a %d %b}. "
                "<code>/menu</code> shows what is planned.",
            )
            return "handled"
        name = esc(got["current"]["name"])
        days = [parse_day(d) for d in got["days"]]
        when = prefs.days_words(days) if every_day else f"{day:%a %d %b}"
        flag = "" if every_day else ":1"
        one_food = [
            ("Change one food in it", f"k:subst:{got['day']}:{slot}{flag}"),
            ("New dishes", f"k:newdish:{got['day']}:{slot}{flag}"),
        ]
        if not got["candidates"]:
            h.outbox.reply(
                person,
                f"Nothing you have eaten before can stand in for {name} yet. Change one food in "
                "it, or tap New dishes for two or three written for this meal (usually 1-2 "
                "minutes).",
                keyboard=inline_keyboard([one_food]),
            )
            return "handled"
        lines = [f"<b>Swap {name}</b> ({_SLOT_WORDS.get(slot, slot).lower()} {esc(when)})."]
        lines.append("Dishes you have had, best first; the day resizes to stay on target:")
        tail = "" if every_day else ":1"
        # Three choices fit in the first reply; the rest are one tap away.
        shown = got["candidates"] if more else got["candidates"][:FIRST_OFFER]
        rows: list[list[tuple[str, str]]] = []
        for c in shown:
            # why each is offered, in a few words: "rated 5; last had 3 weeks ago"
            why = f" ({c['why']})" if c.get("why") else ""
            lines.append(f"{esc(c['name'])}: {c['kcal']:,} kcal, P {c['protein_g']} g{esc(why)}")
            rows.append([(c["name"][:40], f"k:swap:{got['day']}:{slot}:{c['recipe_id']}{tail}")])
        if got["together"]:
            lines.append(f"{esc(got['together'])}'s plate changes too.")
        flags = "" if every_day else "1"
        extra: list[tuple[str, str]] = []
        if not more and len(got["candidates"]) > FIRST_OFFER:
            extra.append(("More dishes", f"k:swapmeal:{got['day']}:{slot}:{flags}m"))
        if every_day and len(days) > 1:
            extra.append((f"Just {day:%a}", f"k:swapmeal:{got['day']}:{slot}:1"))
        if extra:
            rows.append(extra)
        rows.append(one_food)
        h.outbox.reply(person, "\n".join(lines), keyboard=inline_keyboard(rows))
        return "handled"

    def pick_dish(self, h: Handlers, inbound: Inbound, parts: list[str]) -> str:
        """k:swappick:<menu id>[:<day>] - "Swap a dish" under a menu: its dishes still to come
        (from <day>, under Later days), one button each, each opening the dishes that could
        stand in for it."""
        if not parts or not parts[0].isdigit():
            return "ignored"
        person = inbound.person
        row = h.db.one(
            "SELECT person_id, segment_start, segment_end FROM menus WHERE id = ?",
            (int(parts[0]),),
        )
        if row is None or int(row["person_id"]) != person.id:
            return "ignored"
        today = self.clock.today_local()
        start = max(parse_day(str(row["segment_start"])), today)
        if len(parts) > 1:
            try:
                start = max(start, parse_day(parts[1]))
            except ValueError:
                return "ignored"
        end = parse_day(str(row["segment_end"]))
        # today's meals already eaten are not still to come: a dish opens at its next day
        eaten = swap.had_today(h.db, person.id, self.clock.now_local())
        seen: dict[tuple[str, str], dt.date] = {}
        d = start
        while d <= end:
            if not menu_mod.day_confirmed(h.db, person.id, d):
                for p in swap.plates_on(h.db, person.id, d):
                    if d != today or p.slot not in eaten:
                        seen.setdefault((p.slot, p.name), d)
            d += dt.timedelta(days=1)
        if not seen:
            h.outbox.reply(
                person, "Those days are under way or recorded. <code>/menu</code> shows the rest."
            )
            return "handled"
        order = {s: i for i, s in enumerate(menu_mod.SLOT_ORDER)}
        dishes = sorted(seen.items(), key=lambda kv: (kv[1], order.get(kv[0][0], 9)))
        # eight to a screen, cut only between days, and the rest under Later days: cut at eight
        # without a word would hide later days of a menu with different dishes each day.
        days = sorted(set(seen.values()))
        shown = [d for d in days if sum(1 for v in seen.values() if v <= d) <= PICK_DISHES] or days[
            :1
        ]
        rows = [
            [
                (
                    f"{_SLOT_WORDS.get(slot, slot)}: {name}"[:60],
                    f"k:swapmeal:{day.isoformat()}:{slot}",
                )
            ]
            for (slot, name), day in dishes
            if day <= shown[-1]
        ]
        later = [d for d in days if d > shown[-1]]
        if later:
            rows.append([("Later days", f"k:swappick:{parts[0]}:{later[0].isoformat()}")])
        h.outbox.reply(person, "Which dish?", keyboard=inline_keyboard(rows))
        return "handled"

    def do_swap(self, h: Handlers, inbound: Inbound, parts: list[str]) -> str:
        """k:swap:<day>:<slot>:<recipe id>[:1]"""
        if len(parts) < 3 or not parts[2].isdigit():
            return "ignored"
        try:
            day = parse_day(parts[0])
        except ValueError:
            return "ignored"
        person = inbound.person
        try:
            with h.db.tx():
                got = swap.swap(
                    h.db,
                    person.id,
                    day,
                    parts[1],
                    int(parts[2]),
                    every_day=not (len(parts) > 3),
                    config=self.config,
                    clock=self.clock,
                )
        except (NothingToBuild, Infeasible, ValueError) as e:
            h.outbox.reply(person, esc(str(e)) + " <code>/swap</code> again shows what fits now.")
            return "handled"
        h.clear_buttons(inbound)
        h.outbox.reply(person, esc(got.said()))
        logger.info(
            "dish swapped",
            extra={"event": "dish_swapped", "person_id": person.id, "menu_id": got.menu_id},
        )
        return "handled"


def install(
    handlers: Handlers, *, config: Any = None, clock: Clock = DEFAULT_CLOCK, app: Any = None
) -> FoodFlow:
    flow = FoodFlow(config=config, clock=clock, app=app)
    handlers.register_callback("newdish", flow.new_dishes)
    handlers.register_callback("size", flow.size_meal)
    handlers.register_command("swap", flow.cmd_swap)
    handlers.register_callback("swapall", flow.swap_all)
    handlers.register_callback("unavoid", flow.unavoid)
    handlers.register_callback("swapmeal", flow.show_options)
    handlers.register_callback("swappick", flow.pick_dish)
    handlers.register_callback("swap", flow.do_swap)
    handlers.register_command("avoid", flow.cmd_avoid)
    handlers.register_callback("madeof", flow.made_of)
    handlers.register_callback("never", flow.never)
    handlers.register_callback("avadd", flow.keep_term)
    handlers.register_callback("avok", flow.allow_food)
    handlers.register_callback("avrm", flow.allow_term)
    handlers.register_callback("subst", flow.subst_pick)
    handlers.register_callback("substi", flow.subst_offer)
    handlers.register_callback("substdo", flow.subst_do)
    handlers.register_callback("wishundo", flow.wish_undo)
    # after the flows that own a question of their own (Change, the tape, the interview), before
    # the evening check-in and the chat: "I do not want lentils" at 20:45 is not food eaten
    at = next(
        (
            i
            for i, fn in enumerate(handlers.text_handlers)
            if getattr(fn, "__name__", "") in ("offplan_text", "reply")
        ),
        len(handlers.text_handlers),
    )
    handlers.text_handlers.insert(at, flow.avoid_text)
    # a wish after a refusal: "no more lentils" keeps them off, it is not a wish for more
    handlers.text_handlers.insert(at + 1, flow.wish_text)
    return flow


__all__ = [
    "FoodFlow",
    "ask_rows",
    "drop_tapped",
    "install",
    "note_ids",
    "notes_by_ids",
    "refusal_terms",
    "start_new_dishes",
]
