"""A person's own food lists - what they avoid, what they like, what is always at home - changed
in one place, whoever asked and however.

Five doors wrote the avoid list, each its own way: `/set avoid` replaced the whole list unless a
+ came first, the chat card stored any word ("fish") and said "Done" whether or not it matched a
food, a `/cook` note kept only words naming a food and said nothing about the rest, and the page
could not see the list at all. A word that names no food is a refusal with no effect, and saying
"Done" over one is how a promise gets broken quietly.

So every door comes here. A food to avoid is taken only when it names something in the
catalogue, and the answer says what it keeps out ("lentils: Red lentils"), what it could not
place, and what was on the list already. `profiles` is a ledger: a change is a new row from
today, and last week's menu still says what it was planned against.
"""

from __future__ import annotations

import datetime as dt
import itertools
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .. import targets as targets_mod
from ..clock import Clock, isoday
from ..db import Db
from . import catalogue

#: the lists a person keeps, by the name the bot and the page use for each
LISTS: Mapping[str, str] = {
    "avoid": "refused_foods",
    "likes": "liked_foods",
    "pantry": "pantry_json",
}
#: the most a list holds; the interview's own limit
MAX_ITEMS = 40
#: the longest a single entry may be: a food, not a sentence
MAX_TERM = 40


@dataclass(frozen=True)
class Changed:
    """What one change to a list did, in the terms a reply needs."""

    list_name: str
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    #: asked to add, and the list already keeps it out (the same word, or a wider one)
    already: tuple[str, ...] = ()
    #: names no food in the catalogue: not written, because it could not do anything
    unknown: tuple[str, ...] = ()
    #: names no food yet, and written all the same because the person asked for exactly that
    #: list entry: it keeps the food off the day one is added
    unplaced: tuple[str, ...] = ()
    #: the profiles row written for today, when the list changed: what an Undo takes back
    row_id: int | None = None
    #: would leave nothing at all to eat: not written
    too_wide: tuple[str, ...] = ()
    #: asked to add to a list holding `MAX_ITEMS` already: not written
    full: tuple[str, ...] = ()
    #: asked to remove, and not on the list
    missing: tuple[str, ...] = ()
    #: asked to remove, and every food it names still kept out by an entry left on the list:
    #: word -> entry
    kept_by: Mapping[str, str] = field(default_factory=dict)
    #: foods refused by themselves that a word taken off named: allowed back, by their code
    allowed: tuple[str, ...] = ()
    #: taken off the list, and its foods still refused by themselves: word -> those foods
    alone: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    #: the list as it now stands
    terms: tuple[str, ...] = ()
    #: each entry now on the list -> the catalogue foods it keeps out (avoid and pantry only)
    hides: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    #: foods made from one just refused, asked about once rather than guessed (houmous, after
    #: chickpeas): {"id", "code", "name", "of"} each
    asks: tuple[Mapping[str, Any], ...] = ()

    @property
    def changed(self) -> bool:
        return bool(self.added or self.removed or self.allowed)

    def said(self) -> str:
        """The answer, in plain words, with the foods each new entry keeps out."""
        parts: list[str] = []
        if self.list_name == "avoid":
            # an entry naming no food yet keeps nothing off, so it is never promised as kept
            # off: it is said below to do nothing until a food is called that (#35)
            placed = [t for t in self.added if t not in self.unplaced]
            if placed:
                held = "; ".join(kept(t, self.hides.get(t, ())) for t in placed)
                parts.append(f"Kept off your menus from now on: {held}.")
            # a word whose foods something else still keeps off is not back, and is said so
            # below (#36)
            back = [t for t in self.removed if t not in self.kept_by and t not in self.alone]
            back += self.allowed
            if back:
                parts.append(f"Allowed back: {', '.join(back)}.")
        else:
            word = {"likes": "liked foods", "pantry": "foods kept at home"}[self.list_name]
            if self.added:
                parts.append(f"Added to your {word}: {', '.join(self.added)}.")
            if self.removed:
                parts.append(f"Taken off your {word}: {', '.join(self.removed)}.")
        if self.already:
            parts.append(f"Already on the list: {', '.join(self.already)}.")
        if self.unknown:
            parts.append(
                f"I have no food called {_or(self.unknown)}, so nothing changed for "
                f"{'it' if len(self.unknown) == 1 else 'them'}: name the food itself."
            )
        if self.unplaced:
            one = len(self.unplaced) == 1
            parts.append(
                f"On the list, but no food I plan with is called {_or(self.unplaced)} yet, so "
                f"{'it does' if one else 'they do'} nothing until one is."
            )
        if self.too_wide:
            parts.append(
                f"{_or(self.too_wide)} would leave nothing to eat, so it is not on the list."
            )
        if self.full:
            one = len(self.full) == 1
            parts.append(
                f"Your list holds {MAX_ITEMS} already, so {_or(self.full)} "
                f"{'is' if one else 'are'} not on it: take one off first."
            )
        for food, entry in self.kept_by.items():
            parts.append(f"{food} is still kept off by {entry!r}: take that off to have it back.")
        for foods in self.alone.values():
            one = len(foods) == 1
            parts.append(
                f"{', '.join(foods)} {'is' if one else 'are'} still kept off as a single food: "
                f"take {'it' if one else 'them'} off too to have {'it' if one else 'them'} back."
            )
        missing = [m for m in self.missing if m not in self.kept_by]
        if missing:
            parts.append(f"Not on the list: {', '.join(missing)}.")
        # the made-from questions (`asks`) are each surface's to put, with its own buttons
        return " ".join(parts) or "Nothing changed."


def ask_said(ask: Mapping[str, Any]) -> str:
    """ "Houmous is made from chickpeas: keep it off too, or is it fine?" """
    return f"{ask['name']} is made from {ask['of']}: keep it off too, or is it fine?"


def kept(term: str, foods: Sequence[str]) -> str:
    """ "lentils (Red lentils)", or the bare term when it is the food's own name."""
    shown = [f for f in foods if catalogue.fold_term(f) != catalogue.fold_term(term)]
    return f"{term} ({', '.join(shown)})" if shown else term


def _or(words: Sequence[str]) -> str:
    quoted = [f"'{w}'" for w in words]
    return quoted[0] if len(quoted) == 1 else ", ".join(quoted[:-1]) + f" or {quoted[-1]}"


def _short(name: str) -> str:
    """ "Red lentils, dry" -> "Red lentils": what a food is called out loud."""
    return name.split(",", 1)[0].strip()


def scoped(db: Db, person_id: int) -> list[catalogue.Ingredient]:
    """Every food this person could be given before any refusal: approved, and theirs or both."""
    slug = db.scalar("SELECT slug FROM persons WHERE id = ?", (person_id,))
    if slug is None:
        raise LookupError(f"no person with id {person_id}")
    return [
        i
        for i in catalogue.latest_versions(db)
        if i.approved and i.person_scope in ("both", str(slug))
    ]


def foods_for(
    term: str, foods: Iterable[catalogue.Ingredient], *, at_home: bool = False
) -> tuple[str, ...]:
    """The foods one entry keeps out, by the name they are called out loud, sorted. `at_home`
    for the pantry list, where "pepper" is the black pepper (`catalogue.refused_matches`)."""
    folded = catalogue.normalise(term)
    hit = {_short(i.name) for i in foods if catalogue.refused_matches(folded, i, at_home=at_home)}
    return tuple(sorted(hit))


def names_food(term: str, foods: Iterable[catalogue.Ingredient]) -> bool:
    """Whether a term hits one of these foods, or a food made from it (pesto from cashews)."""
    folded = catalogue.normalise(term)
    return any(catalogue.refused_matches(folded, i) or catalogue.made_of(term, i) for i in foods)


def half_read(db: Db, person_id: int, terms: Iterable[str]) -> bool:
    """Whether a term picked out of a sentence names no food while a word in it does: "chicken
    during lent". A qualifier no list knows stayed on the last food, so the sentence was not
    read whole, and none of it is to be written - not the foods before it either, which were
    said back, as before. Asked by every door a sentence comes in by: the bot, and a cook note,
    which kept "no salmon or chicken during Lent" as salmon for good."""
    foods = scoped(db, person_id)
    for term in terms:
        if names_food(term, foods):
            continue
        words = catalogue.normalise(term).split()
        # a word of three letters or more: "in" is a whole word of "Tuna in brine"
        if len(words) > 1 and any(
            catalogue.refused_matches(w, i) for w in words if len(w) >= 3 for i in foods
        ):
            return True
    return False


def terms_of(db: Db, person_id: int, list_name: str, day: dt.date | None = None) -> list[str]:
    """One of the lists as the person wrote it (not normalised), oldest entry first."""
    column = LISTS[list_name]
    row = (
        targets_mod.profile_on(db, person_id, day)
        if day is not None
        else db.one(
            "SELECT * FROM profiles WHERE person_id = ? ORDER BY effective_from DESC, id DESC "
            "LIMIT 1",
            (person_id,),
        )
    )
    if row is None or row[column] is None:
        return []
    try:
        raw = json.loads(str(row[column]))
    except ValueError:
        return []
    return [str(t) for t in raw if str(t).strip()] if isinstance(raw, list) else []


def _clean(term: Any) -> str:
    """One entry with its spaces tidied, never cut: cut to a food's length, a stored entry
    longer than that could never be matched to be taken off, and a
    new one was stored cut mid-word, matching nothing (#135)."""
    return " ".join(str(term).split())


#: what joins two foods typed as one entry: "lentils and broccoli", "salt & pepper"
_JOINED = re.compile(r"\s+and\s+|\s*&\s*", re.I)


def _split(term: str, foods: Sequence[catalogue.Ingredient]) -> list[str]:
    """ "lentils and broccoli" typed as one entry is the two foods, each an entry of its own,
    when the whole names no food and every part does: `/set avoid lentils and broccoli` stored
    ("mac and cheese") stays one entry, so it does not keep all cheese off: a part naming no
    food says the words were not a list."""
    parts = [p.strip() for p in _JOINED.split(term) if p.strip()]
    if len(parts) < 2 or names_food(term, foods):
        return [term]
    return parts if all(names_food(p, foods) for p in parts) else [term]


def amend(
    db: Db,
    person_id: int,
    list_name: str,
    *,
    add: Iterable[Any] = (),
    remove: Iterable[Any] = (),
    clock: Clock,
    strict: bool = True,
) -> Changed:
    """Add to and take from one list, and write it - a new profiles row - when anything changed.

    `strict` is for words picked out of a sentence ("I do not want lentils and the faff"): an
    entry naming no food, and no food made from it, is left out and said. Without it - somebody
    editing the list itself, "/set avoid + olives" - the entry is written as asked and said to
    match nothing yet.

    Raises LookupError when the person has no profile yet (the interview writes the first) and
    ValueError for a list that does not exist. Entries are compared folded on case and on the
    singular, so "Chickpeas" after "chickpea" is the one entry already there.
    """
    if list_name not in LISTS:
        raise ValueError(f"a list is one of {', '.join(LISTS)}")
    today = clock.today_local()
    profile = targets_mod.profile_on(db, person_id, today)
    if profile is None:
        raise LookupError("no profile yet")
    have = terms_of(db, person_id, list_name, today)
    folded: dict[str, str] = {catalogue.fold_term(t): t for t in have}
    checks_food = list_name in ("avoid", "pantry")
    at_home = list_name == "pantry"
    foods = scoped(db, person_id) if checks_food else []

    removed: list[str] = []
    missing: list[str] = []
    for raw in remove:
        term = _clean(raw)
        if not term:
            continue
        key = catalogue.fold_term(term)
        if key in folded:
            removed.append(folded.pop(key))
            continue
        parts = [catalogue.fold_term(p) for p in _split(term, foods)] if checks_food else []
        if len(parts) > 1 and all(p in folded for p in parts):
            # "lentils and broccoli" is taken off the way it went on: as two entries (#35)
            removed += [folded.pop(p) for p in parts]
            continue
        missing.append(term)
    allowed: list[str] = []
    kept_by: dict[str, str] = {}
    alone: dict[str, tuple[str, ...]] = {}
    if list_name == "avoid" and (removed or missing):
        allowed, kept_by, alone, let_back = _taken_off(
            db, person_id, foods, removed, missing, list(folded.values()), clock=clock
        )
        missing = [m for m in missing if m not in let_back]

    added: list[str] = []
    already: list[str] = []
    unknown: list[str] = []
    unplaced: list[str] = []
    too_wide: list[str] = []
    full: list[str] = []
    cleaned = [_clean(raw) for raw in add]
    if checks_food:
        cleaned = [part for term in cleaned for part in _split(term, foods)]
    for term in cleaned:
        if not term:
            continue
        if len(term) > MAX_TERM:
            # a sentence, not a food: never cut to fit, and said back whole (#135)
            unknown.append(term)
            continue
        key = catalogue.fold_term(term)
        if key in folded:
            already.append(term)
            continue
        if checks_food:
            hits = [
                i
                for i in foods
                if catalogue.refused_matches(catalogue.normalise(term), i, at_home=at_home)
            ]
            # a food nothing is called yet, that others are made from, is a real refusal: "I'm
            # allergic to cashews" is kept and asks about the pesto
            if not hits and strict and not any(catalogue.made_of(term, i) for i in foods):
                unknown.append(term)
                continue
            if not hits:
                unplaced.append(term)
            elif list_name == "avoid":
                kept_out = [
                    i
                    for i in foods
                    if any(
                        catalogue.refused_matches(catalogue.normalise(t), i)
                        for t in folded.values()
                    )
                ]
                if all(i in kept_out for i in hits):
                    # every food it names is already kept off by a wider entry ("red lentils"
                    # with "pulses" there): nothing to write, and nothing to promise
                    already.append(term)
                    continue
                left = [i for i in foods if i not in kept_out and i not in hits]
                if not left:
                    too_wide.append(term)
                    continue
        if len(folded) >= MAX_ITEMS:
            full.append(term)
            continue
        folded[key] = term
        added.append(term)

    # sorted the way the bot has always shown them, the person's own spelling kept
    terms = tuple(sorted(folded.values(), key=str.casefold))
    row_id: int | None = None
    if added or removed:
        row_id = targets_mod.change_profile(
            db, person_id, {LISTS[list_name]: json.dumps(list(terms))}, today=today
        )
        db.insert(
            "onboarding_answers",
            person_id=person_id,
            step={"avoid": "refu", "likes": "like", "pantry": "pant"}[list_name],
            answer_json=json.dumps(list(terms)),
        )
    hides = {t: foods_for(t, foods, at_home=at_home) for t in terms} if checks_food else {}
    asks = _asks_for(db, person_id, added, terms, foods) if list_name == "avoid" else ()
    return Changed(
        list_name=list_name,
        added=tuple(added),
        removed=tuple(removed),
        already=tuple(already),
        unknown=tuple(unknown),
        unplaced=tuple(unplaced),
        too_wide=tuple(too_wide),
        full=tuple(full),
        missing=tuple(missing),
        kept_by=kept_by,
        allowed=tuple(allowed),
        alone=alone,
        terms=terms,
        hides=hides,
        row_id=row_id,
        asks=asks,
    )


def _taken_off(
    db: Db,
    person_id: int,
    foods: Sequence[catalogue.Ingredient],
    removed: Sequence[str],
    missing: Sequence[str],
    left: Sequence[str],
    *,
    clock: Clock,
) -> tuple[list[str], dict[str, str], dict[str, tuple[str, ...]], set[str]]:
    """What taking words off the avoid list let back, and what it did not.

    A word not on the list that names a food refused by itself ("Keep houmous off", "Never red
    lentils again") allows that food back, by its code: "/set avoid - houmous" and "houmous is
    word whose foods are all still kept off - by a wider entry left on the list ("pulses" after
    "lentils"), or by a refusal of the food itself - is said to be, never "Allowed back" (#36).
    A word that was on the list leaves a food refused by itself as it was: Undo takes back one
    refusal, and nothing else.

    Returns the foods allowed back by their code, word -> the entry still keeping all its foods
    off, word -> its foods still refused by themselves, and the words that let a food back."""
    entries = [(t, catalogue.normalise(t)) for t in left]
    codes = catalogue.refused_codes(db, person_id)
    allowed: list[str] = []
    kept_by: dict[str, str] = {}
    alone: dict[str, tuple[str, ...]] = {}
    let_back: set[str] = set()
    for term, on_list in [*((t, True) for t in removed), *((t, False) for t in missing)]:
        hits = [i for i in foods if catalogue.refused_matches(catalogue.normalise(term), i)]
        freed = [] if on_list else [i for i in hits if i.code in codes]
        for food in freed:
            db.insert(
                "ingredient_preferences",
                person_id=person_id,
                ingredient_code=food.code,
                status="ok",
                created_at=clock.utc_stamp(),
            )
            codes.discard(food.code)
        if freed:
            let_back.add(term)
        if not hits:
            continue
        cover = next(
            (t for t, n in entries if all(catalogue.refused_matches(n, i) for i in hits)), None
        )
        still = [
            i
            for i in hits
            if i.code in codes or any(catalogue.refused_matches(n, i) for _t, n in entries)
        ]
        if cover is not None:
            kept_by[term] = cover
        elif len(still) == len(hits) and any(i.code in codes for i in hits):
            alone[term] = tuple(sorted({_short(i.name) for i in hits if i.code in codes}))
        else:
            allowed += sorted({_short(i.name) for i in freed})
    return allowed, kept_by, alone, let_back


def _decided(db: Db, person_id: int) -> dict[str, str]:
    """code -> the latest word this person gave on one food by itself (refused, ok, liked)."""
    out: dict[str, str] = {}
    for r in db.all(
        "SELECT ingredient_code, status FROM ingredient_preferences WHERE person_id = ? "
        "ORDER BY id",
        (person_id,),
    ):
        out[str(r["ingredient_code"])] = str(r["status"])
    return out


def _asks_for(
    db: Db,
    person_id: int,
    terms: Iterable[str],
    on_list: Iterable[str],
    foods: Sequence[catalogue.Ingredient],
) -> tuple[dict[str, Any], ...]:
    """The foods made from these refused terms that nobody has decided on yet, one question
    each: asked once, because the answer is kept either way (`answer`)."""
    decided = _decided(db, person_id)
    listed = [catalogue.normalise(t) for t in on_list]
    out: dict[str, dict[str, Any]] = {}
    for term in terms:
        for food in foods:
            if food.code in decided or food.code in out:
                continue
            if any(catalogue.refused_matches(t, food) for t in listed):
                continue  # already kept off by a word on the list
            if catalogue.made_of(term, food):
                out[food.code] = {
                    "id": food.id,
                    "code": food.code,
                    "name": _short(food.name),
                    "of": term,
                }
    return tuple(out.values())


def asks(db: Db, person_id: int, today: dt.date) -> list[dict[str, Any]]:
    """The made-from questions still open for this person's avoid list, for the page."""
    on_list = terms_of(db, person_id, "avoid", today)
    return list(_asks_for(db, person_id, on_list, on_list, scoped(db, person_id)))


@dataclass(frozen=True)
class FoodSaid:
    """One food refused or allowed by itself, by its code (never by a word that could reach
    further than the food tapped)."""

    name: str
    refused: bool
    changed: bool
    #: allowed back, and a word on the avoid list naming it still keeps it off
    kept_by: str = ""

    def said(self) -> str:
        if not self.refused and self.kept_by:
            # never "Allowed back" over a food the list still keeps off (#36, #126)
            return (
                f"{self.name} is still kept off by {self.kept_by!r}: take that off to have it back."
            )
        if not self.changed:
            return (
                f"{self.name} was already off your menus."
                if self.refused
                else f"{self.name} stays on your menus."
            )
        if self.refused:
            return f"Kept off your menus from now on: {self.name}."
        return f"Allowed back: {self.name}."


def _food(db: Db, person_id: int, ingredient: int | str) -> catalogue.Ingredient:
    """One of this person's foods by id or code: LookupError when it is not one of theirs."""
    for food in scoped(db, person_id):
        if ingredient in (food.code, food.id):
            return food
    # an older version's id names the same food
    if isinstance(ingredient, int):
        code = db.scalar("SELECT code FROM ingredients WHERE id = ?", (ingredient,))
        if code is not None:
            for food in scoped(db, person_id):
                if food.code == str(code):
                    return food
    raise LookupError("not one of your foods")


def refuse_food(db: Db, person_id: int, ingredient: int | str, *, clock: Clock) -> FoodSaid:
    """Never this one food again, by its code: the page's "Never again" chip and the bot's
    houmous question. A word on the avoid list can reach further than meant ("eggs" also keeps
    egg whites off); a code cannot."""
    food = _food(db, person_id, ingredient)
    if food.code in catalogue.refused_codes(db, person_id):
        return FoodSaid(_short(food.name), refused=True, changed=False)
    db.insert(
        "ingredient_preferences",
        person_id=person_id,
        ingredient_code=food.code,
        status="refused",
        created_at=clock.utc_stamp(),
    )
    return FoodSaid(_short(food.name), refused=True, changed=True)


def allow_food(db: Db, person_id: int, ingredient: int | str, *, clock: Clock) -> FoodSaid:
    """The one food back, by its code. A word on the avoid list that also names it still keeps
    it off, and the answer names that word: Undo on "Never red lentils again" with "lentils" on
    the list said "Allowed back: Red lentils."."""
    food = _food(db, person_id, ingredient)
    was = _decided(db, person_id).get(food.code)
    db.insert(
        "ingredient_preferences",
        person_id=person_id,
        ingredient_code=food.code,
        status="ok",
        created_at=clock.utc_stamp(),
    )
    entry = next(
        (
            t
            for t in terms_of(db, person_id, "avoid", clock.today_local())
            if catalogue.refused_matches(catalogue.normalise(t), food)
        ),
        "",
    )
    return FoodSaid(_short(food.name), refused=False, changed=was == "refused", kept_by=entry)


def answer(
    db: Db, person_id: int, ingredient: int | str, *, keep_off: bool, clock: Clock
) -> FoodSaid:
    """The answer to "houmous is made from chickpeas: keep it off too?", kept either way so it
    is never asked again. "It is fine" says what is true: the food stays on the menus, or was
    allowed back - never "Allowed back" over a food that was never off (#126)."""
    if keep_off:
        return refuse_food(db, person_id, ingredient, clock=clock)
    return allow_food(db, person_id, ingredient, clock=clock)


def refused_foods(db: Db, person_id: int) -> list[dict[str, Any]]:
    """The foods refused by themselves, by name, with the id to allow each back."""
    codes = catalogue.refused_codes(db, person_id)
    return sorted(
        (
            {"id": i.id, "code": i.code, "name": _short(i.name)}
            for i in catalogue.latest_versions(db)
            if i.code in codes
        ),
        key=lambda f: str(f["name"]).casefold(),
    )


#: a standing note is a sentence, not a food: the longest one may be, and the most kept at once
MAX_NOTE = 200
MAX_NOTES = 20
NOTE_SOURCES = frozenset({"document", "chat", "onboarding", "operator"})


@dataclass(frozen=True)
class Noted:
    """What one change to the standing notes did."""

    added: tuple[str, ...] = ()
    dropped: tuple[str, ...] = ()
    #: asked to add, and already standing
    already: tuple[str, ...] = ()
    #: asked to drop, and not standing
    missing: tuple[str, ...] = ()
    #: too long to be a note, or past MAX_NOTES: not written
    refused: tuple[str, ...] = ()
    #: the notes as they now stand, oldest first
    notes: tuple[str, ...] = ()


#: a note about one half of the week: "simple recipes on weekdays" is not asked of the weekend,
#: 'treat' recipes come in")
_WEEKDAYS = re.compile(r"\b(?:week ?days?|work ?days?|mon(?:day)?s? to fri(?:day)?s?)\b", re.I)
_WEEKENDS = re.compile(r"\b(?:week ?ends?|saturdays?|sundays?)\b", re.I)


def standing_notes(db: Db, person_id: int, *, weekend: bool | None = None) -> list[str]:
    """What a person wants from every menu that no list can hold, oldest first.

    only 1-2 types per dish (so no salads)", "simple recipes". A food to keep out is the avoid
    list and a food to have more often is the likes list; "less of", "how" and "what kind of dish"
    are neither, and a /cook note saying them was forgotten by the next menu. These are the
    active rows of `person_notes` - the standing notes kept for the person - and every
    menu build reads them: the segment, the weekend, the weekend cooked together, one new dish.

    `weekend` is the build's half of the week: a note naming only weekdays is left out of a
    weekend build, and one naming only the weekend out of a weekday build. None is every note."""
    rows = db.all(
        "SELECT text FROM v_person_notes_active WHERE person_id = ? ORDER BY id", (person_id,)
    )
    notes = [str(r["text"]) for r in rows]
    if weekend is None:
        return notes
    other = _WEEKDAYS if weekend else _WEEKENDS
    own = _WEEKENDS if weekend else _WEEKDAYS
    return [n for n in notes if not other.search(n) or own.search(n)]


#: the words that make a food a wish about every menu, longest first so "a lot less" wins over
#: "less". Never after "no", "any" or "not any": "no more lentils" is a refusal (the avoid list)
#: and "I don't want any more lentils" one too, not a wish for more lentils. Group 1 is how,
#: group 2 the words after it (numbered: the audit of message HTML reads a named group as a tag).
_WISH = re.compile(
    r"(?<!\bno )(?<!\bany )\b(too much|too many|too little|not enough|a lot less|"
    r"a bit less|a little less|a lot more|a bit more|a little more|lots more|loads more|"
    r"not so much|not as much|cut down on|go easy on|less|fewer|more|extra)\s+"
    r"(?:of\s+)?(?:the\s+|my\s+|that\s+|this\s+|those\s+)?([a-z][a-z' -]*)",
    re.I,
)
_MORE = frozenset(
    {
        "too little",
        "not enough",
        "a lot more",
        "a bit more",
        "a little more",
        "lots more",
        "loads more",
        "more",
        "extra",
    }
)
#: where the food in a wish ends: "too much chia seed in the pudding", "less rice please"
_WISH_END = re.compile(
    r"\b(?:and|but|or|please|pls|thanks|thank|in|on|for|next|from|with|than|then|this|that|"
    r"at|so|too|again|now|overall|instead|each|every|per)\b",
    re.I,
)
#: the words for food that no one food is called, said in wishes all the time
_FOOD_WORDS = frozenset(
    {"veg", "vegetable", "vegetables", "greens", "fruit", "fruits", "berries", "protein", "carbs"}
)


def wishes_in(db: Db, person_id: int, text: str) -> list[str]:
    """The wishes about every menu a sentence makes, each as a standing note: "oh, there was too
    much chia seed" -> ["less chia seed"]; "more chicken, less rice" -> both.

    Only a food the catalogue knows, or a word for a kind of food ("veg", "fruit"): "more time"
    and "less faff" are not about food, and a message read wrongly here never reaches the chat
    that would have answered it. A wish for a while ("more fish this week") is that week's, said
    with a rebuild, and is not kept (`catalogue.for_now`)."""
    said = (text or "").replace(chr(0x2019), "'")
    if catalogue.for_now(said):
        return []
    foods = scoped(db, person_id)
    out: list[str] = []
    for m in _WISH.finditer(said):
        rest = _WISH_END.split(m.group(2), maxsplit=1)[0].strip(" -'")
        words = rest.split()[:4]
        food = ""
        for n in range(len(words), 0, -1):
            candidate = " ".join(words[:n])
            if candidate.casefold() in _FOOD_WORDS or names_food(candidate, foods):
                food = candidate.casefold()
                break
        if not food:
            continue
        wish = f"{'more' if m.group(1).casefold() in _MORE else 'less'} {food}"
        if wish not in out:
            out.append(wish)
    return out


def less_of_liked(db: Db, person_id: int, wishes: Iterable[str], *, clock: Clock) -> Changed | None:
    """A "less" wish for a food on the likes list takes it off that list: the two would pull a
    menu opposite ways, as "less avocado" did against her liked avocado. None when none is."""
    liked = terms_of(db, person_id, "likes", clock.today_local())
    folded = {catalogue.fold_term(t): t for t in liked}
    off = [
        folded[catalogue.fold_term(w[len("less ") :])]
        for w in wishes
        if w.startswith("less ") and catalogue.fold_term(w[len("less ") :]) in folded
    ]
    if not off:
        return None
    return amend(db, person_id, "likes", remove=off, clock=clock)


def amend_notes(
    db: Db,
    person_id: int,
    *,
    add: Iterable[str] = (),
    drop: Iterable[str] = (),
    source: str,
    clock: Clock,
) -> Noted:
    """Add standing notes and take them off. Each is a new row, never an edit: a note taken off
    is a row with active 0, and `v_person_notes_active` reads the latest row for each text, so
    the history of what was asked, and when, stays. Compared ignoring case and spacing."""
    if source not in NOTE_SOURCES:
        raise ValueError(f"a note's source is one of {', '.join(sorted(NOTE_SOURCES))}")
    now = clock.utc_stamp()
    have = {_fold_note(t): t for t in standing_notes(db, person_id)}
    dropped: list[str] = []
    missing: list[str] = []
    added: list[str] = []
    already: list[str] = []
    refused: list[str] = []
    with db.tx():
        for raw in drop:
            text = _clean_note(raw)
            key = _fold_note(text)
            if not key:
                continue
            if key not in have:
                missing.append(text)
                continue
            db.insert(
                "person_notes",
                person_id=person_id,
                text=have[key],
                source=source,
                active=0,
                created_at=now,
            )
            dropped.append(have.pop(key))
        for raw in add:
            text = _clean_note(raw)
            key = _fold_note(text)
            if not key:
                continue
            if key in have:
                already.append(text)
                continue
            if len(text) > MAX_NOTE or len(have) >= MAX_NOTES:
                refused.append(text)
                continue
            db.insert(
                "person_notes",
                person_id=person_id,
                text=text,
                source=source,
                active=1,
                created_at=now,
            )
            have[key] = text
            added.append(text)
    return Noted(
        added=tuple(added),
        dropped=tuple(dropped),
        already=tuple(already),
        missing=tuple(missing),
        refused=tuple(refused),
        notes=tuple(standing_notes(db, person_id)),
    )


def _clean_note(raw: object) -> str:
    return re.sub(r"\s+", " ", str(raw or "")).strip()


def _fold_note(text: str) -> str:
    return _clean_note(text).casefold().rstrip(".")


def view(db: Db, person_id: int, today: dt.date) -> dict[str, Any]:
    """The page's "Your foods": each list with what each avoid entry keeps out, the dishes on
    menus still to come that hold a food now avoided, and the names to suggest when adding."""
    foods = scoped(db, person_id)
    avoid = terms_of(db, person_id, "avoid", today)
    return {
        "avoid": [{"term": t, "foods": list(foods_for(t, foods))} for t in avoid],
        # single foods refused by a tap, not a word: each by name, each with its own way back
        "avoid_foods": refused_foods(db, person_id),
        "asks": asks(db, person_id, today),
        "likes": terms_of(db, person_id, "likes", today),
        "pantry": [
            {"term": t, "foods": list(foods_for(t, foods, at_home=True))}
            for t in terms_of(db, person_id, "pantry", today)
        ],
        "still_on_menu": still_on_menu(db, person_id, today),
        # the names to offer while typing: the foods themselves, and the kinds that work too
        "suggest": sorted({_short(i.name) for i in foods} | set(catalogue.KINDS)),
        **planner_reads(db, person_id, today),
    }


def note_half(text: str) -> str:
    """Which builds a standing note reaches: weekdays, the weekend, or all (`standing_notes`)."""
    days, ends = bool(_WEEKDAYS.search(text)), bool(_WEEKENDS.search(text))
    return "weekdays" if days and not ends else "weekend" if ends and not days else "all"


def planner_reads(db: Db, person_id: int, today: dt.date) -> dict[str, Any]:
    """The rest of what every menu build reads, beside the lists: the standing notes, the dishes
    their ratings steer towards and away from, the dishes resting, their kitchen - and whatever
    of it the rest keeps from working (`conflicts`). A person sees all of what the planner is
    told about them, and can take any of it back."""
    from .. import targets as targets_mod  # noqa: PLC0415 (targets imports this package)
    from . import menu as menu_mod  # noqa: PLC0415 (menu reads this module)
    from . import taste  # noqa: PLC0415 (taste reads menus)

    favourites, blocked = menu_mod.rated_names(db, person_id)
    words = {t.casefold() for t in terms_of(db, person_id, "likes", today)}
    profile = targets_mod.profile_on(db, person_id, today)
    return {
        "notes": [{"text": t, "half": note_half(t)} for t in standing_notes(db, person_id)],
        "favourites": [n for n in favourites if n.casefold() not in words][:12],
        "never": blocked[:12],
        "resting": taste.resting(db, person_id, today)[:12],
        "kit": menu_mod.equipment_of(profile),
        "conflicts": conflicts(db, person_id, today),
    }


def conflicts(db: Db, person_id: int, today: dt.date) -> list[str]:
    """What one of their wishes keeps another from doing, said rather than quietly resolved:
    a liked food the avoid list keeps off entirely (menus stop asking for it), a standing note
    asking for a food they avoid (the avoid list wins, in code), and a dish they rated well that
    now holds a food they avoid (it is no longer offered)."""
    foods = scoped(db, person_id)
    allowed = {i.code for i in catalogue.approved(db, person_id)}
    avoid = terms_of(db, person_id, "avoid", today)
    out: list[str] = []
    for like in terms_of(db, person_id, "likes", today):
        hit = [i for i in foods if catalogue.refused_matches(catalogue.normalise(like), i)]
        if hit and all(i.code not in allowed for i in hit):
            out.append(
                f"You like {like}, and your avoid list keeps it off every menu, so menus no "
                "longer ask for it."
            )
    for note in standing_notes(db, person_id):
        said = f" {catalogue.normalise(note)} "
        for term in avoid:
            word = catalogue.normalise(term)
            if word and (f" {word} " in said or f" {word.rstrip('s')} " in said):
                out.append(
                    f'Your note "{note}" names {term}, which you avoid: the avoid list wins.'
                )
    rows = db.all(
        "SELECT rc.name AS name, r.score AS score, rc.id AS rid FROM ratings r "
        "JOIN recipes rc ON rc.id = r.recipe_id WHERE r.person_id = ? AND r.id = "
        "(SELECT MAX(x.id) FROM ratings x JOIN recipes rx ON rx.id = x.recipe_id "
        "WHERE x.person_id = r.person_id AND rx.name = rc.name)",
        (person_id,),
    )
    for r in rows:
        if int(r["score"]) >= 4:
            held = catalogue.refused_names_in_recipe(db, person_id, int(r["rid"]))
            if held:
                out.append(
                    f"{r['name']}, which you rated {r['score']}, holds "
                    f"{', '.join(held).lower()}, which you avoid: it is no longer offered."
                )
    return out[:8]


def still_on_menu(
    db: Db, person_id: int, today: dt.date, *, now: dt.datetime | None = None
) -> list[dict[str, Any]]:
    """Dishes on this person's plates from today on (days not yet recorded) that hold a food
    they now avoid: one entry per dish and meal, with the days it is on and what it holds.

    Today's meals already behind the clock (`swap.had_today`) are eaten, not still to come: at
    20:45 the lunch eaten at one was listed, and "Swap it now" rewrote it. `now` defaults to the
    database's own clock - the one its rows are stamped with - while `today` is its today."""
    if not catalogue.refused_terms(db, person_id) and not catalogue.refused_codes(db, person_id):
        return []
    from . import swap  # noqa: PLC0415 (swap reads menus, which import this package)

    if now is None and today == db.clock.today_local():
        now = db.clock.now_local()
    gone = swap.had_today(db, person_id, now) if now is not None and now.date() == today else ()
    rows = db.all(
        "SELECT md.day, ms.slot, ms.recipe_id, rc.name FROM v_menu_days_current md "
        "JOIN meal_slots ms ON ms.menu_day_id = md.id JOIN recipes rc ON rc.id = ms.recipe_id "
        "WHERE md.person_id = ? AND md.day >= ? ORDER BY md.day",
        (person_id, isoday(today)),
    )
    from . import menu as menu_mod  # noqa: PLC0415 (menu imports this package)

    out: dict[tuple[str, str], dict[str, Any]] = {}
    held: dict[int, list[str]] = {}
    for r in rows:
        day = str(r["day"])
        if menu_mod.day_confirmed(db, person_id, dt.date.fromisoformat(day)):
            continue
        if day == isoday(today) and str(r["slot"]) in gone:
            continue
        rid = int(r["recipe_id"])
        if rid not in held:
            held[rid] = catalogue.refused_names_in_recipe(db, person_id, rid)
        if not held[rid]:
            continue
        key = (str(r["slot"]), str(r["name"]))
        entry = out.setdefault(
            key,
            {"slot": key[0], "name": key[1], "days": [], "holds": []},
        )
        entry["days"].append(day)
        entry["holds"] = sorted(set(entry["holds"]) | set(held[rid]))
    return list(out.values())


_SLOT_WORDS = {
    "breakfast": "breakfast",
    "lunch": "lunch",
    "dinner": "dinner",
    "snack": "snack",
    "pre_bed": "pre-bed",
}


def days_words(days: Sequence[dt.date]) -> str:
    """ "Mon to Fri", "Tue and Thu", "Sat 19 Sep": the days a dish is on, said briefly."""
    if not days:
        return ""
    if len(days) == 1:
        return f"{days[0]:%a %d %b}"
    run = all((b - a).days == 1 for a, b in itertools.pairwise(days))
    if run:
        return f"{days[0]:%a} to {days[-1]:%a %d %b}"
    return ", ".join(f"{d:%a}" for d in days[:-1]) + f" and {days[-1]:%a %d %b}"


def still_said(still: Sequence[Mapping[str, Any]]) -> str:
    """The dishes still to come that hold a food now avoided, and the one-word way out."""
    dishes = "; ".join(
        f"{d['name']} ({_SLOT_WORDS.get(str(d['slot']), d['slot'])} "
        f"{days_words([dt.date.fromisoformat(x) for x in d['days']])}) has "
        f"{', '.join(d['holds'])}"
        for d in still[:4]
    )
    return (
        f"Still on your menu: {dishes}. Send /swap and I will swap "
        f"{'it' if len(still) == 1 else 'them'} for dishes you have had before, now."
    )


__all__ = [
    "LISTS",
    "Changed",
    "FoodSaid",
    "allow_food",
    "amend",
    "answer",
    "ask_said",
    "asks",
    "days_words",
    "foods_for",
    "kept",
    "refuse_food",
    "refused_foods",
    "scoped",
    "still_on_menu",
    "still_said",
    "terms_of",
    "view",
]
