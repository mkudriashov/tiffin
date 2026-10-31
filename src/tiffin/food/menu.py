"""Build, store and read cook segment menus.

The model only ever chooses dishes and grams out of a numbered catalogue. Every macro here is
computed in code from grams x per-100 g, every portion comes from the solver, and a second
failure falls back to repeating the last accepted menu rather than sending nothing.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .. import log
from ..clock import DEFAULT_CLOCK, Clock, isoday, parse_day
from ..db import Db
from ..energy import Macros
from ..errors import CarbsShort, Infeasible, Lopsided, ModelInvalid
from . import catalogue, packs, recipes, solver, weekplan
from .catalogue import Ingredient
from .recipes import RecipeDraft

logger = log.get("tiffin.menu")

SLOT_ORDER = ("breakfast", "lunch", "dinner", "snack", "pre_bed")
STATUSES = ("proposed", "accepted", "repeated", "superseded")
DEFAULT_SLOTS = ("breakfast", "lunch", "dinner", "snack")
RECENT_DAYS = 14
# Portion range when one weekend dish is sized for two people.
WEEKEND_LO = 0.5
WEEKEND_HI = 1.8
# Maximum ratio between the largest and smallest portion multipliers on a shared weekend day.
WEEKEND_SPREAD = 1.8
# Weekday meals can differ more because there are more slots and the pre-bed portion is fixed.
WEEKDAY_SPREAD = 2.0
_UNEVEN = 1.25
# Ignore differences too small to warrant rebuilding a dish.
_SHORT_BY = 0.03
FAVOURITE_MIN = 4
BLOCKED_MAX = 2


@dataclass(frozen=True)
class Segment:
    start: dt.date
    end: dt.date

    @property
    def days(self) -> list[dt.date]:
        return [self.start + dt.timedelta(days=i) for i in range((self.end - self.start).days + 1)]


@dataclass
class MenuPlan:
    """A solved menu, ready to store: per day, per slot, a recipe id and a portion."""

    person_id: int
    segment: Segment
    recipe_ids: dict[str, int]  # slot key "day|slot" -> recipe id
    multipliers: dict[str, float]
    day_macros: dict[dt.date, Macros]
    slot_macros: dict[str, Macros]
    model_used: str | None = None
    notes: list[str] = field(default_factory=list)
    #: the model's other options for a slot, checked and stored but not chosen: (slot, recipe id).
    #: The swap list offers them as new dishes for that meal (food/newdish.py `built_dishes`).
    spare: list[tuple[str, int]] = field(default_factory=list)


# Space repeated dinners unless the person explicitly asks for one.
DINNER_GAP_DAYS = 6


def _words(text: str) -> str:
    # "&" and "+" are "and": "Chicken, rice & broccoli" is the dinner's dish, not another
    return " ".join(re.findall(r"[a-z0-9]+", re.sub(r"[&+]", " and ", text.casefold())))


#: what asks for the dish named just after it ("I want beef chilli", "the same beef chilli"). Not
#: "have had": "we have had beef chilli all week" is what was eaten, not what is asked for...
_ASK_BEFORE = re.compile(
    r"\b(?:again|same|repeat|another|have(?! had\b)|want|keep|more|love|fancy)\b"
)
#: ...or just before it ("beef chilli again", "beef chilli please"). Not "too": "beef chilli too
#: often" is a complaint, and "I want beef chilli too" is asked for by its "want"
_ASK_AFTER = re.compile(r"\b(?:again|please|back|as well)\b")
#: what, anywhere after the dish, complains of it whatever came first: "beef chilli too often",
#: "Beef chilli? Please, never again", "beef chilli again? No thanks"
_TIRED_OF = re.compile(
    r"\b(?:never|no thanks|boring|bored|samey|sick of|fed up|tired of|had enough|"
    r"too (?!(?:and|please)\b)\w+)\b"
)
#: what puts the dish named after it out ("something other than beef chilli", "sick of", "no").
#: Read on `_words`, so "don't" is "don t"
_PUT_OUT = re.compile(
    r"\b(?:no|not|never|nothing|none|avoid|without|skip|drop|stop|less|enough|other than|but|"
    r"instead|rather|apart from|except|besides|different|change|swap|replace|away from|"
    r"break from|sick|fed up|tired|bored|hate|dislike|don t|doesn t|didn t|won t|can t|cannot)\b"
)


def _asks_for(note: str, name: str) -> bool:
    """Whether a note asks for the dish by name: a word asking for it next to the name, nothing
    before the name putting it out and nothing after it complaining of it. "Something other than
    beef chilli" names the dish to be rid of it, and "beef chilli, not again" and "I have had
    beef chilli too often" too. Unsure is no: the gap stays."""
    said, dish = _words(note).split(), _words(name).split()
    for i in range(len(said) - len(dish) + 1) if dish else ():
        if said[i : i + len(dish)] != dish or _PUT_OUT.search(" ".join(said[:i])):
            continue
        if _TIRED_OF.search(" ".join(said[i + len(dish) :])):
            continue
        after = " ".join(said[i + len(dish) : i + len(dish) + 3])
        yes, no = _ASK_AFTER.search(after), _PUT_OUT.search(after)
        if yes is not None and (no is None or no.start() > yes.start()):
            return True
        if _ASK_BEFORE.search(" ".join(said[max(0, i - 3) : i])):
            return True
    return False


def dinner_gap(
    db: Db, person_id: int, before: dt.date, days: int = DINNER_GAP_DAYS, *, asked: str = ""
) -> list[str]:
    """The dinners on this person's plates in the `days` days before `before`.

    Less any dinner `asked` - the person's own note for this build - asks for (`_asks_for`): the
    gap is there so a dish is not worn out by a menu nobody asked to repeat, not to refuse "beef
    chilli again". It was refused in code all the same, so the note either went unhonoured
    without a word or four paid attempts ended in "name a food rather than a rule" for somebody
    who had named a dish. Only a note that asks: "something other than beef chilli" names it too.
    """
    rows = db.all(
        "SELECT DISTINCT rc.name FROM v_menu_days_current md "
        "JOIN meal_slots ms ON ms.menu_day_id = md.id JOIN recipes rc ON rc.id = ms.recipe_id "
        "WHERE md.person_id = ? AND ms.slot = 'dinner' AND md.day >= ? AND md.day < ? "
        "ORDER BY rc.name",
        (person_id, isoday(before - dt.timedelta(days=days)), isoday(before)),
    )
    return [str(r["name"]) for r in rows if not _asks_for(asked, str(r["name"]))]


def new_lately(
    db: Db, person_id: int, before: dt.date, days: int = 7, *, weekend: bool | None = None
) -> list[str]:
    """Dishes whose first day on this person's plates fell in the `days` days before `before`:
    what the novelty quota counts (about one new dish a week). `weekend` counts only the dishes
    first had on a weekend day (True) or on a weekday (False): the weekend's treats are new every
    weekend by design, and are not the weekday cook's novelty."""
    kind = "" if weekend is None else " AND (strftime('%w', first) IN ('0', '6')) = ?"
    rows = db.all(
        "SELECT rc.name, MIN(md.day) AS first FROM v_menu_days_current md "
        "JOIN meal_slots ms ON ms.menu_day_id = md.id JOIN recipes rc ON rc.id = ms.recipe_id "
        f"WHERE md.person_id = ? AND md.day < ? GROUP BY rc.name HAVING first >= ?{kind}",
        (
            person_id,
            isoday(before),
            isoday(before - dt.timedelta(days=days)),
            *(() if weekend is None else (int(weekend),)),
        ),
    )
    return sorted(str(r["name"]) for r in rows)


def slot_key(day: dt.date, slot: str) -> str:
    return f"{isoday(day)}|{slot}"


# ---------------------------------------------------------------- reading
def current_menu(db: Db, person_id: int, day: dt.date) -> sqlite3.Row | None:
    # v_menu_current can legitimately hold more than one row for the same day now (a Tue-Fri
    # accept and the Mon-Fri menu it partially replaces both cover Tuesday), so the winner is
    # the one decided most recently, not whichever `fetchone()` happens to return first.
    return db.one(
        "SELECT * FROM v_menu_current WHERE person_id = ? AND segment_start <= ? "
        "AND segment_end >= ? ORDER BY decided_at DESC, id DESC LIMIT 1",
        (person_id, isoday(day), isoday(day)),
    )


def meals_for_day(db: Db, person_id: int, day: dt.date) -> list[str]:
    """Recipe names for the day's slots from the current accepted/repeated menu, in slot order."""
    rows = db.all(
        "SELECT rc.name FROM v_menu_days_current md "
        "JOIN meal_slots ms ON ms.menu_day_id = md.id JOIN recipes rc ON rc.id = ms.recipe_id "
        "WHERE md.person_id = ? AND md.day = ? "
        "ORDER BY CASE ms.slot WHEN 'breakfast' THEN 0 WHEN 'lunch' THEN 1 WHEN 'dinner' THEN 2 "
        "WHEN 'snack' THEN 3 ELSE 4 END, ms.id",
        (person_id, isoday(day)),
    )
    return [str(r["name"]) for r in rows]


def latest_menu(db: Db, person_id: int, *, status: str | None = None) -> sqlite3.Row | None:
    sql = "SELECT * FROM menus WHERE person_id = ?"
    params: list[Any] = [person_id]
    if status is not None:
        sql += " AND status = ?"
        params.append(status)
    return db.one(sql + " ORDER BY id DESC LIMIT 1", params)


def decision_of(db: Db, menu_id: int) -> str | None:
    row = db.one(
        "SELECT decision FROM menu_decisions WHERE menu_id = ? ORDER BY id DESC LIMIT 1", (menu_id,)
    )
    return None if row is None else str(row["decision"])


def recent_names(db: Db, person_id: int, before: dt.date, days: int = RECENT_DAYS) -> list[str]:
    rows = db.all(
        "SELECT DISTINCT rc.name FROM menus m JOIN menu_days md ON md.menu_id = m.id "
        "JOIN meal_slots ms ON ms.menu_day_id = md.id JOIN recipes rc ON rc.id = ms.recipe_id "
        "WHERE m.person_id = ? AND md.day BETWEEN ? AND ? ORDER BY rc.name",
        (person_id, isoday(before - dt.timedelta(days=days)), isoday(before)),
    )
    return [str(r["name"]) for r in rows]


def rated_names(db: Db, person_id: int) -> tuple[list[str], list[str]]:
    """(favourites, blocked) for the menu prompt: dishes they rated, plus foods they asked for.

    Ratings alone were the whole of this, and ratings take weeks to accumulate. So on day one -
    the only day a new person has actually told Tiffin anything about food - the favourites
    slot read "(none)", while `profiles.liked_foods` held the answer they gave in the interview
    and the answer `/set likes` promises to use ("The next menu will use it."). Three surfaces
    wrote that column and the menu path never read it.

    A liked FOOD and a favourite DISH are not the same thing, and the prompt slot takes names
    either way: "salmon" biases the menu towards salmon the same way "Salmon traybake" does.
    Ratings come first, because a dish somebody actually ate and scored is stronger evidence
    than a word they typed once. A dish rated well that holds a food they have since refused is
    no favourite any more: it cannot be cooked from their list, and asking for it steers the
    model to the food they said no to. Nor is a liked food they have since refused
    (`cookable_terms`): "salmon" stayed a favourite after "no fish".
    """
    rows = db.all(
        # grouped on the dish NAME, not the recipe id: a repeated menu writes a fresh `recipes`
        # row every time it repeats (food/batch.py), so the old recipe_id-keyed MAX() counted
        # a dish rated once under an old row and again under a new one as two separate dishes -
        # the same name landing on both the favourite list and the blocked list of one prompt
        # (dishes_summary/03). Latest by rating id, across every recipe row sharing this name.
        "SELECT rc.name AS name, r.score AS score, rc.id AS rid FROM ratings r "
        "JOIN recipes rc ON rc.id = r.recipe_id "
        "WHERE r.person_id = ? AND r.id = (SELECT MAX(x.id) FROM ratings x "
        "JOIN recipes rx ON rx.id = x.recipe_id "
        "WHERE x.person_id = r.person_id AND rx.name = rc.name)",
        (person_id,),
    )
    liked = [r for r in rows if int(r["score"]) >= FAVOURITE_MIN]
    words = catalogue.liked_terms(db, person_id)
    allowed = {i.code for i in catalogue.approved(db, person_id)} if liked or words else set()
    good = [str(r["name"]) for r in liked if uses_only(db, int(r["rid"]), allowed)]
    bad = [str(r["name"]) for r in rows if int(r["score"]) <= BLOCKED_MAX]
    known = {n.casefold() for n in good}
    words = cookable_terms(db, words, allowed, refused=catalogue.refused_terms(db, person_id))
    good += [t for t in words if t.casefold() not in known]
    return good, bad


def cookable_terms(
    db: Db, terms: Iterable[str], codes: Collection[str], *, refused: Iterable[str] = ()
) -> list[str]:
    """The liked foods that can still be asked for: a word naming foods in the catalogue only
    while one of them is in `codes` (the list being cooked from), and never a word they also
    refused. A word naming no food at all ("spicy") is the model's to read, and stays."""
    terms = list(terms)
    if not terms:
        return []
    out = {catalogue.fold_term(t) for t in refused}
    every = catalogue.latest_versions(db)
    keep: list[str] = []
    for term in terms:
        named = [i.code for i in every if catalogue.refused_matches(term, i)]
        if catalogue.fold_term(term) in out or (named and not any(c in codes for c in named)):
            continue
        keep.append(term)
    return keep


def uses_only(db: Db, recipe_id: int, codes: Collection[str]) -> bool:
    """Whether every food in a stored dish is one of `codes`: the test the swap list puts a dish
    to (food/swap.py), for a dish held up as a favourite or an example of their taste."""
    return all(
        str(r["code"]) in codes
        for r in db.all(
            "SELECT i.code FROM recipe_ingredients ri JOIN ingredients i "
            "ON i.id = ri.ingredient_id WHERE ri.recipe_id = ?",
            (recipe_id,),
        )
    )


# ---------------------------------------------------------------- segments
def segments_for(profile: sqlite3.Row | None, week_start: dt.date) -> list[Segment]:
    """The person's cook segments in the week starting Monday; weekends are joint.
    None for someone who tracks only: no menus at all (food/planning.py)."""
    from . import planning  # noqa: PLC0415 (planning reads batch, which reads targets)

    if planning.planning_of(profile) == "off":
        return []
    raw = "[[0,1,2,3,4]]"
    if profile is not None and profile["segment_days"]:
        raw = str(profile["segment_days"])
    try:
        groups = json.loads(raw)
    except ValueError:
        groups = [[0, 1, 2, 3, 4]]
    out: list[Segment] = []
    for group in groups:
        weekdays = sorted(int(w) for w in group if 0 <= int(w) <= 6)
        if not weekdays:
            continue
        out.append(
            Segment(
                week_start + dt.timedelta(days=weekdays[0]),
                week_start + dt.timedelta(days=weekdays[-1]),
            )
        )
    # The weekend is cooked together, which is why the cadence question only ever
    # offers Monday-to-Friday shapes. Nothing then covered Saturday or Sunday, so two days in
    # seven had no menu at all: the morning message said "no menu yet" and the evening
    # check-in had no planned macros to confirm against. It is a property of the system, not
    # an answer, so it is added here rather than stored per person.
    covered = {d for group in groups for d in (int(w) for w in group if 0 <= int(w) <= 6)}
    if out and covered and not covered & {5, 6}:
        out.append(Segment(week_start + dt.timedelta(days=5), week_start + dt.timedelta(days=6)))
    return out


#: a person's cook segments for the week starting on the Monday given
Weeks = Callable[[dt.date], list[Segment]]


def week_segments(db: Db, person_id: int, week_start: dt.date) -> list[Segment]:
    """The person's cook segments in the week starting Monday, each day under the profile in
    force on it.

    How someone plans is booked from a day (food/planning.py): a change from next Monday leaves
    this week as it was planned, and one from tomorrow splits a cook that straddles it - a
    Monday-to-Friday batch changed to cooking each day from Wednesday keeps Monday and Tuesday
    as the batch's own and cooks Wednesday, Thursday and Friday each on the day. Only a change of
    the cooks splits the week: a goal or a quiet hour changed on Wednesday writes a profile row
    too, and that is no reason to cut a cook in two."""
    from ..targets import profile_on  # noqa: PLC0415 (targets imports the food layer)
    from . import planning  # noqa: PLC0415

    runs: list[tuple[sqlite3.Row | None, dt.date, dt.date]] = []
    shape: tuple[str, str] | None = None
    for i in range(7):
        day = week_start + dt.timedelta(days=i)
        profile = profile_on(db, person_id, day)
        this = (
            json.dumps(planning.groups_of(profile)) if profile is not None else "",
            planning.planning_of(profile) if profile is not None else "",
        )
        if runs and this == shape:
            runs[-1] = (runs[-1][0], runs[-1][1], day)
        else:
            runs.append((profile, day, day))
            shape = this
    out: list[Segment] = []
    for profile, first, last in runs:
        if profile is None:
            continue
        for seg in segments_for(profile, week_start):
            lo, hi = max(seg.start, first), min(seg.end, last)
            if lo <= hi:
                out.append(Segment(lo, hi))
    return out


def weeks_of(db: Db, person_id: int) -> Weeks:
    """`week_segments` for one person, for the helpers that walk the weeks."""
    return lambda base: week_segments(db, person_id, base)


def profile_weeks(profile: sqlite3.Row | None) -> Weeks:
    """Every week under one profile - what the helpers did before a change could be booked."""
    return lambda base: segments_for(profile, base)


def next_segment(
    profile: sqlite3.Row | None,
    today: dt.date,
    *,
    days_before: int = 2,
    weeks: Weeks | None = None,
) -> Segment | None:
    """The segment whose start is `days_before` days away, if the profile has one. `weeks`
    reads each week under the profile in force then (`weeks_of`); without it, `profile`."""
    from ..clock import week_start as monday  # noqa: PLC0415 (small helper, avoids a cycle)

    walk = weeks or profile_weeks(profile)
    target = today + dt.timedelta(days=days_before)
    for base in (monday(today), monday(today) + dt.timedelta(days=7)):
        for seg in walk(base):
            if seg.start == target:
                return seg
    return None


def batch_slots(profile: sqlite3.Row) -> tuple[str, ...]:
    """The slots this person batch-cooks for the week: their batch setting, and nothing at all
    when every cook of theirs is a day of its own (food/planning.py)."""
    from . import planning  # noqa: PLC0415 (planning reads batch; menu is imported first)

    if "batch_slots" not in profile.keys():  # noqa: SIM118
        return ("lunch", "dinner")
    return planning.batched(profile)


def equipment_of(profile: sqlite3.Row | None) -> list[str]:
    """What this person can cook with; empty means unstated, and the prompt assumes the basics.

    A recipe that says "roast for 40 minutes" is no use to someone with an air fryer, and the
    generator had no way of knowing which they had until the kitchen question existed.
    """
    if profile is None or "equipment_json" not in profile.keys():  # noqa: SIM118
        return []
    try:
        raw = json.loads(str(profile["equipment_json"] or "[]"))
    except ValueError:
        return []
    return [str(x).replace("_", " ") for x in raw if isinstance(x, str)][:12]


def variety_of(profile: sqlite3.Row | None, slots: Sequence[str]) -> dict[str, str]:
    raw = "{}"
    if profile is not None and profile["variety_json"]:
        raw = str(profile["variety_json"])
    try:
        data = json.loads(raw)
    except ValueError:
        data = {}
    default = str(data.get("all", "rotate"))
    return {s: str(data.get(s, default)) for s in slots}


def slots_for(profile: sqlite3.Row | None) -> list[str]:
    n = int(profile["meals_per_day"]) if profile is not None else 4
    slots: list[str] = [str(s) for s in DEFAULT_SLOTS[: max(2, min(n, 4))]]
    if profile is not None and profile["pre_bed_protein"]:
        slots.append("pre_bed")
    return slots


# ---------------------------------------------------------------- building
def _drafts_from(payload: Mapping[str, Any]) -> dict[str, RecipeDraft]:
    """{"days": [{"day": ..., "slots": [{"slot": ..., "recipe": {...}}]}]} -> key -> draft."""
    days = payload.get("days")
    if not isinstance(days, list) or not days:
        raise ModelInvalid("menu proposal has no days")
    out: dict[str, RecipeDraft] = {}
    for entry in days:
        if not isinstance(entry, Mapping):
            raise ModelInvalid("menu day is not an object")
        try:
            day = parse_day(str(entry.get("day")))
        except ValueError:
            raise ModelInvalid(f"menu day {entry.get('day')!r} is not a date") from None
        slots = entry.get("slots")
        if not isinstance(slots, list) or not slots:
            raise ModelInvalid(f"menu day {day} has no slots")
        for s in slots:
            if not isinstance(s, Mapping):
                raise ModelInvalid("menu slot is not an object")
            name = str(s.get("slot") or "")
            if name not in SLOT_ORDER:
                raise ModelInvalid(f"unknown slot {name!r}")
            recipe = s.get("recipe")
            if not isinstance(recipe, Mapping):
                raise ModelInvalid(f"slot {name} has no recipe")
            out[slot_key(day, name)] = RecipeDraft.from_dict(recipe)
    return out


#: meal preps"): the batch-cooked lunch and dinner go into flat boxes for the week
_SOUPY = re.compile(
    r"\b(soup|broth|chowder|bisque|ramen|pho|laksa|consomm|gazpacho|minestrone|bouillon)",
    re.IGNORECASE,
)
#: ...or one made mostly of stock whatever it is called: the week's lentil "soup" was 47% stock
_STOCK_SHARE = 0.35
#: ...counting only the stock left once the dry rice or pasta cooked in it has taken its share,
#: about one and a half times its own weight. A one-pot spaghetti is 45% stock before it cooks
#: build an attempt. Lentils are not on the list: a lentil pot full of stock is the soup itself.
_ABSORBING = ("rice_", "pasta_", "spaghetti_", "couscous", "quinoa", "noodles", "orzo", "bulgur")
_ABSORBS_X = 1.5


def _alternatives_from(payload: Mapping[str, Any]) -> list[tuple[str, RecipeDraft]]:
    """{"alternatives": [{"slot": ..., "recipe": {...}}]} -> (slot, draft); a malformed one is
    dropped, never fatal: an alternative is an extra the model may offer, not the menu."""
    out: list[tuple[str, RecipeDraft]] = []
    raw = payload.get("alternatives")
    if not isinstance(raw, list):
        return out
    for entry in raw:
        if not isinstance(entry, Mapping) or str(entry.get("slot") or "") not in SLOT_ORDER:
            continue
        recipe = entry.get("recipe")
        if not isinstance(recipe, Mapping):
            continue
        try:
            out.append((str(entry["slot"]), recipes.honest_minutes(RecipeDraft.from_dict(recipe))))
        except ValueError:
            continue
    return out


def _soup_errors(
    drafts: Mapping[str, RecipeDraft],
    cat: Sequence[Ingredient],
    boxed: frozenset[str],
) -> list[str]:
    """A batch-cooked weekday dish that is a soup, by its name or by how much of it is stock. The
    weekend is cooked on the day and eaten from a bowl at home, so it is never asked."""
    errors: list[str] = []
    seen: set[str] = set()
    for key, draft in drafts.items():
        day, _, slot = key.partition("|")
        if slot not in boxed or draft.name in seen or parse_day(day).weekday() >= 5:
            continue
        grams = [(cat[i.i - 1].code, i.g) for i in draft.ingredients if 1 <= i.i <= len(cat)]
        total = sum(g for _c, g in grams) or 1.0
        stock = sum(g for code, g in grams if "stock" in code or "broth" in code)
        taken = _ABSORBS_X * sum(g for code, g in grams if code.startswith(_ABSORBING))
        if _SOUPY.search(draft.name) or max(0.0, stock - taken) / total >= _STOCK_SHARE:
            seen.add(draft.name)
            errors.append(
                f"{slot} {draft.name!r} is a soup: the batch-cooked lunch and dinner go into flat "
                "meal-prep boxes and there are no soup containers - make it a dish that sits in a "
                "box (a thick curry or chilli, a traybake, a rice or pasta dish)"
            )
    return errors


def soup_errors(
    drafts: Mapping[str, RecipeDraft], cat: Sequence[Ingredient], boxed: frozenset[str]
) -> list[str]:
    """`_soup_errors`, for a dish built on its own (food/newdish.py)."""
    return _soup_errors(drafts, cat, boxed)


def _variety_errors(
    drafts: Mapping[str, RecipeDraft],
    segment: Segment,
    slots: Sequence[str],
    variety: Mapping[str, str],
) -> list[str]:
    errors: list[str] = []
    for slot in slots:
        names = [
            drafts[slot_key(d, slot)].name for d in segment.days if slot_key(d, slot) in drafts
        ]
        if not names:
            continue
        mode = variety.get(slot, "rotate")
        if mode == "same" and len(set(names)) != 1:
            errors.append(f"{slot}: variety 'same' but {len(set(names))} different dishes")
        if mode == "different" and len(set(names)) != len(names):
            errors.append(f"{slot}: variety 'different' but a dish repeats")
    return errors


def lopsided(
    problems: Sequence[solver.DayProblem],
    days: Sequence[solver.DaySolution],
    target: Macros,
    fat_floor_g: float,
    fibre_min: float,
    max_spread: float,
    pinned: frozenset[str] = frozenset(),
    carb_min: float = 0.0,
) -> Lopsided | None:
    """The worst day whose portions spread wider than `max_spread`, or None.

    Said for the model to fix the dishes rather than the portions: which slots were cut and
    which grown, and what the day's dishes at one serving each are short of - the reason the
    solver had to shrink the leanest of them to reach a floor.

    `pinned` slots are held at one serving by the profile (the pre-bed protein pot), so they say
    nothing about balance: counted, a day of ordinary 0.5-0.85 portions beside the pinned 1.0
    read as twice apart, and four good weekends were sent back for it.
    """
    worst: tuple[float, solver.DayProblem, dict[str, float]] | None = None
    for problem, sol in zip(problems, days, strict=True):
        free = {s: m for s, m in sol.multipliers.items() if s not in pinned}
        if len(free) < 2:
            continue
        spread = max(free.values()) / min(free.values())
        if spread > max_spread and (worst is None or spread > worst[0]):
            worst = (spread, problem, free)
    if worst is None:
        return None
    spread, problem, free = worst
    ms = sorted(free.values())
    middle = ms[len(ms) // 2]
    cut = [s for s, m in free.items() if m < middle / _UNEVEN] or [min(free, key=free.__getitem__)]
    grown = [s for s, m in free.items() if m > middle * _UNEVEN] or [
        max(free, key=free.__getitem__)
    ]

    def slots(names: list[str]) -> str:
        return " and ".join(n.replace("_", "-") for n in names)

    def sized(names: list[str]) -> str:
        return " and ".join(f"{n.replace('_', '-')} at {free[n]:g}" for n in names)

    # each plate at its own size: "dinner and snack at 1.65" when the snack was at 1.3 told the
    # model the wrong thing about the plate it had to fix
    moved = f"{sized(cut)} of a serving beside {sized(grown)}"
    kcal = sum(o.kcal for o in problem.slots) or 1.0
    per100 = [
        ("protein", sum(o.protein_g for o in problem.slots), target.protein_g),
        ("fat", sum(o.fat_g for o in problem.slots), fat_floor_g),
        ("fibre", sum(o.fibre_g for o in problem.slots), fibre_min),
        # a floor of 0 is no floor: `need` below skips it
        ("carbs", sum(o.carb_g for o in problem.slots), carb_min),
    ]
    short = sorted(
        (
            (need / target.kcal - have / kcal) / (need / target.kcal),
            name,
            100 * have / kcal,
            100 * need / target.kcal,
        )
        for name, have, need in per100
        if target.kcal and need and have / kcal < (need / target.kcal) * (1 - _SHORT_BY)
    )
    if short:
        _gap, name, have, need = short[-1]
        why = (
            f"the dishes are short of {name} ({have:.1f} g per 100 kcal, the day needs "
            f"{need:.1f}). Put more {name} into {slots(cut)}"
        )
    else:
        why = f"the dishes are far apart in size. Make {slots(cut)} smaller or the rest bigger"
    return Lopsided(
        f"{isoday(problem.day)}: portions too uneven - {moved}: {why}, so every dish is served "
        "near one ordinary portion.",
        spread,
    )


def _even_plating(
    problems: Sequence[solver.DayProblem],
    *,
    whole: Sequence[solver.PackRule],
    pre_bed_protein: bool,
    fat_floor_g: float,
    fibre_min: int,
    lo: float,
    hi: float,
    max_spread: float,
    target: Macros,
    pinned: frozenset[str],
    carb_min: float = 0.0,
) -> solver.SegmentSolution | None:
    """The same dishes plated within `max_spread` of one another, if any band of portion sizes
    allows it: the band [b, b x max_spread] for each b from `lo` up, the one closest to the
    calories kept. None when no band holds every floor."""
    best: tuple[int, solver.SegmentSolution] | None = None
    step = solver.DEFAULT_STEP
    b = lo
    while b <= hi + 1e-9:
        top = min(hi, b * max_spread)
        if top < b + 1e-9:
            break  # no band at all: a spread under 1.0 allows nothing to be told apart
        got = solver.solve_segment(
            problems,
            whole_pack=whole,
            leftovers=None,
            pre_bed_protein=pre_bed_protein,
            fat_floor_g=fat_floor_g,
            fibre_min=fibre_min,
            lo=b,
            hi=top,
            carb_min=carb_min,
        )
        if not isinstance(got, Infeasible) and (
            lopsided(
                problems,
                got.days,
                target,
                fat_floor_g,
                fibre_min,
                max_spread,
                pinned,
                carb_min=carb_min,
            )
            is None
        ):
            off = sum(d.kcal_dev for d in got.days)
            if best is None or off < best[0]:
                best = (off, got)
        if top >= hi - 1e-9:
            break
        b = round(b + step, 4)
    return None if best is None else best[1]


#: what a no-cook day may not use
COOKING = ("hob", "oven", "air fryer")


def dinner_in_force(
    db: Db, person_id: int, day: dt.date, cat: Sequence[Ingredient]
) -> RecipeDraft | None:
    """The dinner planned on `day` (`dish_in_force`)."""
    return dish_in_force(db, person_id, day, "dinner", cat)


def dish_in_force(
    db: Db, person_id: int, day: dt.date, slot: str, cat: Sequence[Ingredient]
) -> RecipeDraft | None:
    """The dish planned for `slot` on `day`, as a draft numbered from `cat`, or None when there
    is none, or it holds a food no longer on offer (refused since)."""
    row = db.one(
        "SELECT ms.recipe_id FROM v_menu_days_current md "
        "JOIN meal_slots ms ON ms.menu_day_id = md.id "
        "WHERE md.person_id = ? AND md.day = ? AND ms.slot = ?",
        (person_id, isoday(day), slot),
    )
    if row is None:
        return None
    rid = int(row["recipe_id"])
    index = {ing.code: n for n, ing in enumerate(cat, start=1)}
    parts: list[recipes.DraftIngredient] = []
    for s in recipes.stored_ingredients(db, rid):
        if s.ingredient.code not in index:
            return None
        parts.append(recipes.DraftIngredient(index[s.ingredient.code], s.grams))
    rc = db.one(
        "SELECT name, method_json, prep_minutes, servings, keep_days, freezable FROM recipes "
        "WHERE id = ?",
        (rid,),
    )
    if rc is None:
        return None
    try:
        method = tuple(str(x) for x in json.loads(str(rc["method_json"] or "[]")))
    except ValueError:
        return None
    return recipes.RecipeDraft(
        name=str(rc["name"]),
        ingredients=tuple(parts),
        method=method,
        prep_minutes=int(rc["prep_minutes"] or 0),
        servings=int(rc["servings"] or 1),
        keep_days=int(rc["keep_days"] or 3),
        freezable=bool(rc["freezable"]),
    )


def week_errors(
    db: Db,
    person_id: int,
    drafts: Mapping[str, RecipeDraft],
    segment: Segment,
    slots: Sequence[str],
    cat: Sequence[Ingredient],
    wp: weekplan.WeekPlan,
    *,
    followed: Collection[str] = (),
) -> list[str]:
    """How this plan breaks the way their week runs (food/weekplan.py): cooking on the no-cook
    day, breakfasts not taking turns as asked, more tinned fish in the week than they said.
    `followed` are the lunches filled in from the night before's dinner: only warmed."""
    out: list[str] = []
    if wp.nocook_day is not None:
        for day in segment.days:
            if day.weekday() != wp.nocook_day:
                continue
            for slot in slots:
                if slot_key(day, slot) in followed:
                    continue  # made the evening before, only warmed now
                if slot == "dinner" and wp.steak_day == day.weekday():
                    continue  # the steak: the one thing cooked that day
                draft = drafts.get(slot_key(day, slot))
                if draft is None:
                    continue
                cooked = [a for a in recipes.cooking_without_kettle(draft) if a in COOKING]
                if cooked:
                    out.append(
                        f"{isoday(day)} is a no-cook day: {slot} {draft.name!r} needs the "
                        f"{' and '.join(cooked)}; put it together with nothing on the hob or in "
                        "the oven"
                    )
    by_code = {i.code: i for i in cat}

    def codes_of(draft: RecipeDraft | None) -> set[str]:
        if draft is None:
            return set()
        return {cat[p.i - 1].code for p in draft.ingredients if 1 <= p.i <= len(cat)}

    if wp.steak_day is not None:
        for day in segment.days:
            if day.weekday() != wp.steak_day:
                continue
            if "dinner" in slots and not codes_of(drafts.get(slot_key(day, "dinner"))) & set(
                weekplan.STEAK_CODES
            ):
                out.append(
                    f"{isoday(day)}'s dinner is a steak: build it on the sirloin or rump steak"
                )
            if wp.nocook_day == wp.steak_day:
                fish = any(
                    any(
                        w in by_code[c].name.casefold()
                        for w in weekplan.STEAK_DAY_FISH_WORDS
                        for c in codes_of(drafts.get(slot_key(day, s)))
                        if c in by_code
                    )
                    for s in slots
                    if s != "dinner"
                )
                if not fish:
                    out.append(
                        f"{isoday(day)}'s no-cook meals have fish in at least one: tinned tuna, "
                        "sardines or smoked salmon"
                    )
    if wp.rotate_mains and len(wp.same_food) > 1:
        monday = segment.start - dt.timedelta(days=segment.start.weekday())
        seen: dict[str, str] = {}
        for group in wp.same_food:
            days_ = [monday + dt.timedelta(days=d) for d in group]
            mains: set[str] = set()
            for day in days_:
                for slot in ("lunch", "dinner"):
                    draft = (
                        drafts.get(slot_key(day, slot))
                        if day in segment.days
                        else dish_in_force(db, person_id, day, slot, cat)
                    )
                    mains |= {
                        c
                        for c in codes_of(draft)
                        if c in by_code
                        and by_code[c].aisle in packs.MAIN_AISLES
                        and packs.open_days(by_code[c].name) == packs.FRESH_OPEN_DAYS
                    }
            when = " and ".join(f"{d:%a}" for d in days_)
            for c in sorted(mains):
                if c in seen and any(d in segment.days for d in days_):
                    out.append(
                        f"{when} cook {by_code[c].name} as {seen[c]} does: a different meat "
                        "or fish for each stretch"
                    )
                seen.setdefault(c, when)
    weekdays = [d for d in segment.days if d.weekday() < 5]
    if wp.breakfasts and "breakfast" in slots and weekdays:
        names = {
            _words(drafts[slot_key(d, "breakfast")].name)
            for d in weekdays
            if slot_key(d, "breakfast") in drafts
        }
        want = min(wp.breakfasts, len(weekdays))
        if not want <= len(names) <= wp.breakfasts:
            out.append(f"breakfasts: {wp.breakfasts} different ones taking turns, not {len(names)}")
    if wp.tinned_fish is not None and weekdays:
        tins = {
            i.code
            for i in cat
            if i.aisle == "tins" and any(w in i.name.casefold() for w in weekplan.TINNED_FISH_WORDS)
        }
        meals = {
            (isoday(d), slot)
            for d in weekdays
            for slot in slots
            if (got := drafts.get(slot_key(d, slot))) is not None
            and any(
                1 <= part.i <= len(cat) and cat[part.i - 1].code in tins for part in got.ingredients
            )
        }
        monday = weekdays[0] - dt.timedelta(days=weekdays[0].weekday())
        others = [
            monday + dt.timedelta(days=i)
            for i in range(5)
            if monday + dt.timedelta(days=i) not in set(segment.days)
        ]
        if tins and others:
            marks = ",".join("?" for _ in others)
            codes = ",".join("?" for _ in tins)
            for r in db.all(
                "SELECT DISTINCT md.day, ms.slot FROM v_menu_days_current md "
                "JOIN meal_slots ms ON ms.menu_day_id = md.id "
                "JOIN recipe_ingredients ri ON ri.recipe_id = ms.recipe_id "
                "JOIN ingredients i ON i.id = ri.ingredient_id "
                f"WHERE md.person_id = ? AND md.day IN ({marks}) AND i.code IN ({codes})",
                (person_id, *(isoday(d) for d in others), *sorted(tins)),
            ):
                meals.add((str(r["day"]), str(r["slot"])))
        if len(meals) > wp.tinned_fish:
            out.append(f"tinned fish is in {len(meals)} meals this week: at most {wp.tinned_fish}")
    return out


def build_plan(
    db: Db,
    person_id: int,
    segment: Segment,
    payload: Mapping[str, Any],
    *,
    targets: Macros,
    profile: sqlite3.Row | None,
    items: Sequence[Ingredient] | None = None,
    model_used: str | None = None,
    clock: Clock = DEFAULT_CLOCK,
    fibre_min: int = 25,
    weekend: bool = False,
    max_spread: float | None = None,
    carb_min: float = 0.0,
    gap: Collection[str] = (),
    chooser: Callable[[str, Sequence[RecipeDraft]], int] | None = None,
    steer: packs.Steer | None = None,
    day_targets: Mapping[dt.date, Macros] | None = None,
) -> MenuPlan:
    """Validate the model's dishes, store the recipes, solve the portions. Raises on bad input.

    `gap` names dinners had too recently to be a dinner again (DINNER_GAP_DAYS): refused here,
    not only asked for. `chooser`, given a slot and its valid options (the model's first choice
    and any alternatives it offered for a slot that is the same all segment), returns the index
    of the one to put in: code chooses, the model only offers. The others
    are stored too, as `spare`, for the swap list.

    `max_spread` refuses a solution whose day serves some dishes at a fraction of a portion
    and others at well over one (`Lopsided`, before anything is stored).

    `weekend` lifts the after-work effort limits and the weekday variety rule: two treat days,
    cooked properly, a different dish each day.

    `carb_min` is a floor on each day's carbohydrate, in grams (none by default).

    `steer` holds the plan to the shop window's packs (food/packs.py): no new pack of meat or fish
    while one is open, and a budget of main proteins. Strict, it refuses; relaxed (the last
    attempt), it keeps the plan and says what it opened in `notes`.

    `day_targets` is a day's own targets where it has them (a training day's extra food), and
    how their week runs (food/weekplan.py) is held here, not only asked for: lunch from the day
    before's dinner, the no-cook day, breakfasts in turn, tinned fish a week, a dinner's minutes.
    """
    cat = list(items if items is not None else catalogue.approved(db, person_id))
    if not cat:
        raise ModelInvalid("the catalogue is empty for this person")
    slots = slots_for(profile)
    # the minutes on the card are the minutes the method takes, before anything is judged on them
    drafts = {k: recipes.honest_minutes(d) for k, d in _drafts_from(payload).items()}
    wp = weekplan.HOUSE if weekend else weekplan.of(db, person_id)
    #: the lunches that are the night before's dinner, filled in here
    followed: set[str] = set()
    if "lunch" in slots and "dinner" in slots:
        # lunch made the evening before is that dinner, reheated: the same dish, whatever the
        # already planned before it - Tuesday's dinner is Wednesday's lunch either way
        for day in segment.days:
            if not weekplan.follows(wp, day):
                continue
            before = day - dt.timedelta(days=1)
            dinner = (
                drafts.get(slot_key(before, "dinner"))
                if before in segment.days
                else dinner_in_force(db, person_id, before, cat)
            )
            if dinner is not None:
                drafts[slot_key(day, "lunch")] = dinner
                followed.add(slot_key(day, "lunch"))
    # Monday and Tuesday, Thursday and Friday): each takes its group's first day's dishes - this
    # build's, or the ones already planned when that day is not in it
    for group in wp.same_food:
        monday = segment.start - dt.timedelta(days=segment.start.weekday())
        days_ = [monday + dt.timedelta(days=d) for d in group]
        later = [d for d in days_[1:] if d in segment.days]
        if not later:
            continue
        for slot in ("lunch", "dinner"):
            if slot not in slots:
                continue
            first = days_[0]
            source = (
                drafts.get(slot_key(first, slot))
                if first in segment.days
                else dish_in_force(db, person_id, first, slot, cat)
            )
            if source is None:  # the first day is not planned: the first one built leads
                source = drafts.get(slot_key(later[0], slot))
            if source is not None:
                for day in later:
                    drafts[slot_key(day, slot)] = source
    missing = [slot_key(d, s) for d in segment.days for s in slots if slot_key(d, s) not in drafts]
    if missing:
        raise ModelInvalid(f"menu is missing {len(missing)} slots, first {missing[0]}")
    _favourites, blocked = rated_names(db, person_id)
    boxed_slots = frozenset(batch_slots(profile) if profile is not None else ("lunch", "dinner"))
    gap_names = {g.casefold() for g in gap}

    def one_dish_errors(draft: RecipeDraft, slot: str) -> list[str]:
        why = recipes.validate(draft, cat, blocked_names=blocked)
        why += recipes.simplicity_errors(
            draft, cat, slot, weekend=weekend, main_minutes=wp.dinner_minutes
        )
        if not weekend:
            why += _soup_errors({slot_key(segment.start, slot): draft}, cat, boxed_slots)
        if slot == "dinner" and draft.name.casefold() in gap_names:
            why.append("a dinner in the last six days")
        return why

    # slot -> its valid options, and the model's own first choice where code put in another
    options_of: dict[str, list[RecipeDraft]] = {}
    own: dict[str, RecipeDraft] = {}
    alternatives = _alternatives_from(payload)
    if alternatives and chooser is not None and not weekend:
        variety = variety_of(profile, slots)
        for slot in slots:
            offered = [alt for s, alt in alternatives if s == slot]
            keys = [slot_key(day, slot) for day in segment.days]
            if not offered or variety.get(slot) != "same":
                continue
            first = drafts[keys[0]]
            if any(drafts[k].name != first.name for k in keys):
                continue
            # an alternative named like another meal's dish would put one dish in two meals of
            # every day, and the batch cook would stir the two recipes into one pot - however it
            # is written: "Chicken rice and broccoli" is the dinner's dish without its comma
            elsewhere = {_words(d.name) for k, d in drafts.items() if k not in keys}
            valid = [
                o
                for o in [first, *offered]
                if not one_dish_errors(o, slot) and (o is first or _words(o.name) not in elsewhere)
            ]
            choices: list[RecipeDraft] = []
            for o in valid:
                if o.name not in {c.name for c in choices}:
                    choices.append(o)
            if not choices:
                continue
            pick = choices[min(max(chooser(slot, choices), 0), len(choices) - 1)]
            for k in keys:
                drafts[k] = pick
            options_of[slot] = choices
            if pick is not first and first in choices:
                own[slot] = first
    errors: list[str] = []
    for key, draft in drafts.items():
        errors += [f"{key}: {e}" for e in recipes.validate(draft, cat, blocked_names=blocked)]
    too_recent = sorted(
        {
            d.name
            for k, d in drafts.items()
            if k.endswith("|dinner") and d.name.casefold() in gap_names
        }
    )
    errors += [
        f"dinner {n!r} was a dinner in the last {DINNER_GAP_DAYS} days: a different dinner"
        for n in too_recent
    ]
    # one dish in two meals of a day, however it is spelled: two plates of it every day, and the
    # batch cook groups by name, so the two recipes went into one pot at two box sizes
    twice: dict[tuple[str, str], str] = {}
    for day in segment.days:
        seen: dict[str, str] = {}
        for slot in slots:
            name = drafts[slot_key(day, slot)].name
            also = seen.setdefault(_words(name), slot)
            if also != slot:
                twice.setdefault(
                    (also, slot),
                    f"{name!r} is in two meals of a day, {also} and {slot}: a different dish "
                    "for one of them",
                )
    errors += list(twice.values())
    # simple, per dish rather than per day: a "same all week" dish that is too long is one
    # problem, and five copies of it would crowd every other reason out of the re-prompt
    too_much: dict[str, str] = {}
    for key, draft in drafts.items():
        slot = key.split("|", 1)[1]
        said = recipes.simplicity_errors(
            draft, cat, slot, weekend=weekend, main_minutes=wp.dinner_minutes
        )
        if said and draft.name not in too_much:
            too_much[draft.name] = f"{slot} {draft.name!r} is not simple: {', '.join(said)}"
    errors += list(too_much.values())
    opened: list[str] = []
    if steer is not None:
        # Cook an open pack before opening another. Packs are named by the model's numbers.
        index = {ing.code: n for n, ing in enumerate(cat, start=1)}
        uses = packs.main_uses(drafts, cat)
        if steer.strict:
            errors += steer.problems(uses, index)
        else:
            opened = steer.problems(uses, index, person=True)
    if not weekend:
        # meal prep is boxed, and there is nothing to box a soup in: refused here, not only
        # asked for in the prompt - a rule the model is merely told is a rule it can break
        errors += _soup_errors(drafts, cat, boxed_slots)
    if not weekend:
        # a weekday cook's "same every day" is for batch cooking; the weekend is two treats,
        # different each day. Holding the weekend to the owner's "same" refused every correct answer
        # on Sat 26 Sep ("dinner: variety 'same' but 2 different dishes") four times over.
        errors += _variety_errors(drafts, segment, slots, variety_of(profile, slots))
    else:
        # ...and "different each day" was only asked for: a Saturday and Sunday of the same dishes
        # passed. The pre-bed pot is the one plate a person may want the same both nights.
        errors += _variety_errors(
            drafts, segment, slots, {s: "different" for s in slots if s != solver.PRE_BED}
        )
    # how their week runs refuses on every attempt but the last, which keeps the plan and says
    # what it missed - a cooked Wednesday is better than last week's menu repeated (a real build
    shape = week_errors(db, person_id, drafts, segment, slots, cat, wp, followed=followed)
    strict_shape = steer is None or steer.strict
    if strict_shape:
        errors += shape
    if errors:
        raise ModelInvalid("; ".join(errors[:4]))
    missed_shape = [] if strict_shape else shape

    # targets.fat_g is already the profile's floor in grams (macro_split sets fat at the floor)
    fat_floor_g = float(targets.fat_g)
    # bread by the slice, eggs and buns whole: the solver counts them as they are plated
    units = catalogue.units(db)

    def problems_for(drafts: Mapping[str, RecipeDraft]) -> list[solver.DayProblem]:
        """The solver's problem for each day, from the dishes as they stand."""
        options: dict[dt.date, list[solver.SlotOption]] = {}
        for day in segment.days:
            day_opts: list[solver.SlotOption] = []
            for slot in slots:
                draft = drafts[slot_key(day, slot)]
                # PER SERVING, not the whole recipe. A meal slot is one serving; the shopping
                # list and the batch plan have always divided recipe grams by `servings`, and this
                # did not - so a model answering "servings": 2 planned a day against double the
                # food it then bought, and every day_macros number was silently twice the meal.
                # Every shipped fixture and the prompt example say 1, where the two are
                # identical, which is why it never showed.
                m = recipes.per_serving(draft, cat).as_macros()
                day_opts.append(
                    solver.SlotOption(
                        slot,
                        m.kcal,
                        m.protein_g,
                        m.fat_g,
                        m.carb_g,
                        m.fibre_g,
                        grams={cat[i.i - 1].code: i.g / draft.servings for i in draft.ingredients},
                        units=recipes.draft_unit_parts(draft, cat, units),
                    )
                )
            options[day] = day_opts
        return [
            solver.DayProblem(day, tuple(options[day]), (day_targets or {}).get(day, targets))
            for day in segment.days
        ]

    # every main protein is rounded to whole packs too, one fewer first where the plates need
    # only a little less (solver.FEWER_WITHIN): a pack is still a ceiling, never a quota
    whole = [
        solver.PackRule(ing.code, ing.pack_g, fewer_first=ing.aisle in packs.MAIN_AISLES)
        for ing in cat
        if ing.use_whole_pack or ing.aisle in packs.MAIN_AISLES
    ]
    if wp.shops:
        # a pack is bought per shop: each shop's days rounded on their own, so Thursday and
        # Friday's 502 g of mince is one pack trimmed, not two bought
        windows: list[frozenset[dt.date]] = []
        for day in segment.days:
            if day.weekday() < 5 and not any(day in w for w in windows):
                shop = packs.window_of(db, person_id, day)
                windows.append(frozenset(d for d in shop if d in segment.days))
        whole = [
            solver.PackRule(r.code, r.pack_g, fewer_first=r.fewer_first, days=w)
            for r in whole
            for w in windows
        ]
    # one dish on two plates of different sizes needs more room either side of an ordinary
    lo, hi = (WEEKEND_LO, WEEKEND_HI) if weekend else (solver.DEFAULT_LO, solver.DEFAULT_HI)
    pre_bed = bool(profile is not None and profile["pre_bed_protein"])
    pinned = frozenset({solver.PRE_BED}) if pre_bed else frozenset()

    def plated(problems: Sequence[solver.DayProblem], carb: float) -> solver.SegmentSolution:
        """The portions, held to `carb` grams of carbohydrate a day; raises when they will not
        fit, or fit only unevenly."""
        got = solver.solve_segment(
            problems,
            whole_pack=whole,
            leftovers=None,
            pre_bed_protein=pre_bed,
            fat_floor_g=fat_floor_g,
            fibre_min=fibre_min,
            lo=lo,
            hi=hi,
            carb_min=carb,
        )
        if isinstance(got, Infeasible):
            raise got
        if max_spread is None:
            return got
        uneven = lopsided(
            problems, got.days, targets, fat_floor_g, fibre_min, max_spread, pinned, carb_min=carb
        )
        if uneven is None:
            return got
        # the solver picks the closest calories first and never looks at the spread, so a day it
        # served at 0.5 beside 1.75 may have an even plating it never tried: her plate of the
        # 26 Sep dishes came back at 3.5x while 0.6/0.6/1.0/1.0 met every floor. Each refusal is
        # a paid model call, so the bands are tried first.
        even = _even_plating(
            problems,
            whole=whole,
            pre_bed_protein=pre_bed,
            fat_floor_g=fat_floor_g,
            fibre_min=fibre_min,
            lo=lo,
            hi=hi,
            max_spread=max_spread,
            target=targets,
            pinned=pinned,
            carb_min=carb,
        )
        if even is None:
            raise uneven
        return even

    def settled(problems: Sequence[solver.DayProblem]) -> solver.SegmentSolution:
        """`plated` at the carbohydrate floor; `CarbsShort` when the floor alone is out of reach."""
        try:
            return plated(problems, carb_min)
        except Infeasible as e:
            if carb_min <= 0:
                raise
            # the carbohydrate alone, or something else? The same dishes without the floor say:
            # if they fit - evenly, where evenness is asked for - they are still a menu, and the
            # caller decides whether one more ask for carbohydrate is worth it
            try:
                loose = plated(problems, 0.0)
            except Infeasible:
                raise  # not the carbohydrate: the reason is the one without it
            # said as the carbohydrate alone, with the day that falls furthest short: the solver's
            # own "protein, fat, fibre and carbs ... not together" names protein first, and the
            # retry then asked for leaner protein - the opposite of what the day needs
            problem, day = min(
                zip(problems, loose.days, strict=True), key=lambda p: p[1].macros.carb_g
            )
            raise CarbsShort(
                f"{isoday(problem.day)}: carbs: {day.macros.carb_g} g < {round(carb_min)} g - "
                "the dishes are short of carbohydrate"
            ) from e

    problems = problems_for(drafts)
    try:
        solved = settled(problems)
    except Infeasible as chosen:
        if not own:
            raise
        for slot, first in own.items():
            for day in segment.days:
                drafts[slot_key(day, slot)] = first
        problems = problems_for(drafts)
        try:
            solved = settled(problems)
        except Infeasible as theirs:
            # an answer short only of carbohydrate is one the caller can still keep
            if isinstance(chosen, CarbsShort) and not isinstance(theirs, CarbsShort):
                raise chosen from None
            raise
    if steer is not None and steer.closes:
        # the week's meat and fish against the packs bought for them, at the portions solved: a
        # attempt kept and said - targets win over packs
        weighed = packs.MAIN_AISLES + steer.fresh
        foods = {ing.code: (ing.name, ing.aisle) for ing in cat if ing.aisle in weighed}
        plated_g: dict[str, dict[str, float]] = {}
        for problem, day_solution in zip(problems, solved.days, strict=True):
            on = isoday(problem.day)
            for opt in problem.slots:
                for code, g in opt.grams.items():
                    if code in foods:
                        by_day = plated_g.setdefault(code, {})
                        by_day[on] = by_day.get(on, 0.0) + g * day_solution.multipliers[opt.slot]
        index = {ing.code: n for n, ing in enumerate(cat, start=1)}
        if steer.strict:
            # meat and fish refuse; a bag of vegetables left is asked for in the prompt, never
            # worth an attempt of its own (the same build spent one on an avocado)
            waste = steer.wasted(plated_g, foods, index, mains_only=True)
            if waste:
                raise ModelInvalid("; ".join(waste[:4]))
        else:
            opened += steer.wasted(plated_g, foods, index, person=True, mains_only=True)
    opened += [f"Not quite as asked: {m}" for m in missed_shape[:1]]
    # the options code did not put in, for the swap list
    spare = [
        (slot, c)
        for slot, choices in options_of.items()
        for c in choices
        if c is not drafts[slot_key(segment.start, slot)]
    ]

    stored: dict[str, int] = {}
    for key, draft in drafts.items():
        stored[key] = recipes.store(db, draft, cat, "model", model_used, "both", clock=clock)
    spare_ids = [
        (slot, recipes.store(db, draft, cat, "model", model_used, "both", clock=clock))
        for slot, draft in spare
    ]
    day_macros: dict[dt.date, Macros] = {}
    slot_macros: dict[str, Macros] = {}
    multipliers: dict[str, float] = {}
    for problem, day_solution in zip(problems, solved.days, strict=True):
        day = problem.day
        day_slots: list[Macros] = []
        for opt in problem.slots:
            m = day_solution.multipliers[opt.slot]
            key = slot_key(day, opt.slot)
            multipliers[key] = m
            # `round(opt.kcal * m)` rounded TWICE: `opt.kcal` is already a per-serving figure
            # rounded at multiplier 1 (`recipes.per_serving` in the loop above), so multiplying
            # it by `m` and rounding again is not the number `recipes.card` gives for the same
            # dish at the same multiplier - that reads the stored recipe's exact grams and
            # rounds once. The bot card and the page read the same stored `meal_slots.kcal`,
            # so a mismatch there was a mismatch a person could see on their own two screens
            # for the one meal they were both looking at (numbers_units/06).
            sm = recipes.per_serving(drafts[key], cat, m, units=units).as_macros()
            slot_macros[key] = sm
            day_slots.append(sm)
        # the day heading is the sum of these same rounded meal figures, not a separate rounding
        # of the day's exact total - otherwise the two can differ by the rounding itself, which
        # is the whole of numbers_units/05 (bot and page both read `menu_days.kcal` for this).
        day_macros[day] = Macros(
            sum(sm.kcal for sm in day_slots),
            sum(sm.protein_g for sm in day_slots),
            sum(sm.fat_g for sm in day_slots),
            sum(sm.carb_g for sm in day_slots),
            sum(sm.fibre_g for sm in day_slots),
        )
    return MenuPlan(
        person_id=person_id,
        segment=segment,
        recipe_ids=stored,
        multipliers=multipliers,
        day_macros=day_macros,
        slot_macros=slot_macros,
        model_used=model_used,
        spare=spare_ids,
        notes=[f"{o}." for o in opened],
    )


def day_confirmed(db: Db, person_id: int, day: dt.date) -> bool:
    from . import menu_storage  # noqa: PLC0415  # Storage depends on the menu types above.

    return menu_storage.day_confirmed(db, person_id, day)


def open_days(db: Db, person_id: int, segment: Segment, today: dt.date) -> Segment | None:
    from . import menu_storage  # noqa: PLC0415  # Storage depends on the menu types above.

    return menu_storage.open_days(db, person_id, segment, today)


def store_plan(
    db: Db,
    plan: MenuPlan,
    *,
    status: str = "proposed",
    source_menu_id: int | None = None,
    clock: Clock | None = None,
    own_choice: bool = False,
) -> int:
    from . import menu_storage  # noqa: PLC0415  # Storage depends on the menu types above.

    return menu_storage.store_plan(
        db,
        plan,
        status=status,
        source_menu_id=source_menu_id,
        clock=clock,
        own_choice=own_choice,
    )


def repeat_last(
    db: Db, person_id: int, segment: Segment, *, clock: Clock | None = None
) -> int | None:
    from . import menu_storage  # noqa: PLC0415  # Storage depends on the menu types above.

    return menu_storage.repeat_last(db, person_id, segment, clock=clock)


def decide(db: Db, menu_id: int, decision: str, *, message_in_id: int | None = None) -> None:
    from . import menu_storage  # noqa: PLC0415  # Storage depends on the menu types above.

    menu_storage.decide(db, menu_id, decision, message_in_id=message_in_id)


def newly_placed(db: Db, menu_id: int) -> list[int]:
    from . import menu_storage  # noqa: PLC0415  # Storage depends on the menu types above.

    return menu_storage.newly_placed(db, menu_id)


def refused_brought_in(db: Db, menu_row: Any) -> list[str]:
    from . import menu_storage  # noqa: PLC0415  # Storage depends on the menu types above.

    return menu_storage.refused_brought_in(db, menu_row)


def accepted_menu_id(db: Db, person_id: int, day: dt.date) -> int | None:
    from . import menu_storage  # noqa: PLC0415  # Storage depends on the menu types above.

    return menu_storage.accepted_menu_id(db, person_id, day)


def segment_menu_id(db: Db, person_id: int, day: dt.date) -> int | None:
    from . import menu_storage  # noqa: PLC0415  # Storage depends on the menu types above.

    return menu_storage.segment_menu_id(db, person_id, day)
