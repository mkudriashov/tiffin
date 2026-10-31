"""Rank dishes by a person's stated preferences and recent experience.

Ratings, refusals, ingredient affinities and repetition affect the score.
The score orders candidates; it never overrides a refusal."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import random
from collections import defaultdict
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass

from ..clock import isoday, parse_day
from ..db import Db
from . import catalogue

#: how long a preference holds before it counts half
PREFERENCE_DAYS = 180.0
#: how long a day of a dish on the plate weighs on wanting it again
BOREDOM_DAYS = 7.0
#: the same, for the main protein (chicken, salmon...) of whatever dish it was in
KIND_DAYS = 3.0
#: the boredom a dish rests above: one plate, BOREDOM_DAYS ago
REST_AT = 0.5
#: how much the ingredients' prior weighs against the dish's own evidence
PRIOR_STRENGTH = 2.0
#: the most boredom can take off a score
BOREDOM_WEIGHT = 0.35
KIND_WEIGHT = 0.10
#: a dish predicted below this for either of two people cooking together is not offered
VETO = 0.35
#: a swap away this soon after eating it is boredom, not dislike
BOREDOM_SWAP_DAYS = 14
#: how far back the plates are read
LOOKBACK_DAYS = 365


@dataclass(frozen=True)
class Taste:
    name: str
    liking: float
    boredom: float
    kind_boredom: float
    evidence: float

    @property
    def resting(self) -> bool:
        """Eaten recently enough that it should wait: more on the plate lately than one plate
        BOREDOM_DAYS ago weighs. A single plate rests for that long, a batch week about three
        weeks after it. (More than half the boredom penalty, the rule before, let a plate go
        after 3.7 days: a favourite eaten on Saturday was back by Wednesday.)"""
        return self.boredom > REST_AT

    @property
    def penalty(self) -> float:
        """What having it lately takes off: the dish itself, then its main protein."""
        return BOREDOM_WEIGHT * (1.0 - math.exp(-self.boredom)) + KIND_WEIGHT * (
            1.0 - math.exp(-self.kind_boredom)
        )

    @property
    def score(self) -> float:
        return self.liking - self.penalty

    def sampled(self, seed: str) -> float:
        """One Thompson draw of the score: liking drawn from its Beta posterior (the prior and
        the evidence it rests on), less the same boredom penalty. A dish known well draws close
        to its mean; a dish barely known draws wide, so it gets its chance now and then rather
        than never (Thompson 1933; the "explore" half of a recommender). Seeded per person, week
        and dish, so the same week asks the same question twice and gets the same answer."""
        n = PRIOR_STRENGTH + self.evidence
        a = max(self.liking * n, 1e-3)
        b = max((1.0 - self.liking) * n, 1e-3)
        return random.Random(_seed_int(seed)).betavariate(a, b) - self.penalty


def _seed_int(seed: str) -> int:
    return int.from_bytes(hashlib.sha256(seed.encode("utf-8")).digest()[:8], "big")


def seed_for(person_id: int, on: dt.date, dish: str) -> str:
    """The seed of one Thompson draw: this person, this ISO week, this dish."""
    year, week, _ = on.isocalendar()
    return f"{person_id}:{year}-W{week:02d}:{dish.casefold()}"


def _fade(age_days: float, half_life: float) -> float:
    return 0.5 ** (max(age_days, 0.0) / half_life)


def _main_protein(db: Db, recipe_id: int) -> str | None:
    """The code of the ingredient carrying most of a dish's protein: what it is "a dish of"."""
    best: tuple[float, str] | None = None
    for r in db.all(
        "SELECT i.code, ri.grams, i.protein_100 FROM recipe_ingredients ri "
        "JOIN ingredients i ON i.id = ri.ingredient_id WHERE ri.recipe_id = ?",
        (recipe_id,),
    ):
        protein = float(r["grams"]) * float(r["protein_100"] or 0.0) / 100.0
        if best is None or protein > best[0]:
            best = (protein, str(r["code"]))
    return best[1] if best is not None and best[0] > 0 else None


def _ingredients(db: Db, recipe_id: int) -> list[tuple[str, float]]:
    return [
        (str(r["code"]), float(r["grams"]))
        for r in db.all(
            "SELECT i.code, ri.grams FROM recipe_ingredients ri "
            "JOIN ingredients i ON i.id = ri.ingredient_id WHERE ri.recipe_id = ?",
            (recipe_id,),
        )
    ]


class _History:
    """One person's evidence, read once and asked about many dishes."""

    def __init__(self, db: Db, person_id: int, on: dt.date):
        self.db = db
        self.on = on
        since = isoday(on - dt.timedelta(days=LOOKBACK_DAYS))
        # the plates in force on each day before `on`: what was on the plate, answered or not
        self.eaten: dict[str, list[dt.date]] = defaultdict(list)
        self.recipe_of: dict[str, int] = {}
        kind_days: dict[str, list[dt.date]] = defaultdict(list)
        plates: list[tuple[str, str, str]] = []  # (day, slot, dish)
        for r in db.all(
            "SELECT md.day, ms.slot, rc.name, rc.id FROM v_menu_days_current md "
            "JOIN meal_slots ms ON ms.menu_day_id = md.id JOIN recipes rc ON rc.id = ms.recipe_id "
            "WHERE md.person_id = ? AND md.day < ? AND md.day >= ?",
            (person_id, isoday(on), since),
        ):
            key = str(r["name"]).casefold()
            day = parse_day(str(r["day"]))
            self.eaten[key].append(day)
            self.recipe_of.setdefault(key, int(r["id"]))
            plates.append((str(r["day"]), str(r["slot"]), key))
        self.kind_of: dict[str, str | None] = {}
        for key, rid in self.recipe_of.items():
            kind = _main_protein(db, rid)
            self.kind_of[key] = kind
            if kind is not None:
                kind_days[kind] += self.eaten[key]
        self.kind_days = kind_days
        # the plates they ate, as far as they said: a day answered as planned, less any meal a
        # change replaced (an accepted estimate names it); a "Not quite" day only where such a
        # change was parsed - a note alone says nothing about which dishes were eaten.
        basis = {
            str(r["day"]): str(r["basis"])
            for r in db.all(
                "SELECT c.day, c.basis FROM day_confirmations c WHERE c.person_id = ? "
                "AND c.day >= ? AND c.status = 'confirmed' AND c.id = (SELECT MAX(x.id) "
                "FROM day_confirmations x WHERE x.person_id = c.person_id AND x.day = c.day)",
                (person_id, since),
            )
        }
        replaced: dict[str, set[str]] = defaultdict(set)
        for r in db.all(
            "SELECT e.day, e.replaced_slots_json FROM estimates e WHERE e.person_id = ? "
            "AND e.day >= ? AND e.status = 'accepted' AND e.id = (SELECT MAX(x.id) FROM "
            "estimates x WHERE x.person_id = e.person_id "
            "AND COALESCE(x.source_estimate_id, x.id) = COALESCE(e.source_estimate_id, e.id))",
            (person_id, since),
        ):
            try:
                named = json.loads(str(r["replaced_slots_json"] or "[]"))
            except ValueError:
                named = []
            replaced[str(r["day"])].update(s for s in named if isinstance(s, str))
        self.ate: dict[str, list[dt.date]] = defaultdict(list)
        for day, slot, key in plates:
            said = basis.get(day)
            if said == "menu" or (said == "mixed" and day in replaced):
                ate = slot not in replaced.get(day, ())
            else:
                ate = False
            if ate:
                self.ate[key].append(parse_day(day))
        # the latest rating per dish name, with its age, and its recipe for the ingredient prior
        self.rated: dict[str, tuple[int, float, int]] = {}
        for r in db.all(
            "SELECT rc.name AS name, r.score AS score, r.created_at AS at, rc.id AS rid "
            "FROM ratings r JOIN recipes rc ON rc.id = r.recipe_id WHERE r.person_id = ? "
            "AND r.id = (SELECT MAX(x.id) FROM ratings x JOIN recipes rx ON rx.id = x.recipe_id "
            "WHERE x.person_id = r.person_id AND rx.name = rc.name)",
            (person_id,),
        ):
            age = (on - parse_day(str(r["at"])[:10])).days
            self.rated[str(r["name"]).casefold()] = (int(r["score"]), float(age), int(r["rid"]))
        # what their swaps said: (kind, day) per dish
        self.swaps: dict[str, list[tuple[str, dt.date]]] = defaultdict(list)
        for r in db.all(
            "SELECT dish, kind, day FROM dish_events WHERE person_id = ? AND day >= ?",
            (person_id, since),
        ):
            self.swaps[str(r["dish"]).casefold()].append((str(r["kind"]), parse_day(str(r["day"]))))
        # the meals they skipped on a day they resized (food/plates.py): the dish in the base
        # menu at that slot on that day. Only a skip they chose (`own_json`): on a day cooked
        # together `plates.resize` moves the other's plates too, and the owner skipping the shared
        # carried into every change of hers that day after it. A row from before that was
        # recorded is left out when it is the one that followed (`plates.FOLLOWED`), the test
        # `plates.state` puts the same row to.
        from .plates import FOLLOWED  # noqa: PLC0415 (plates imports the menu layer)

        self.skipped: dict[str, list[dt.date]] = defaultdict(list)
        for r in db.all(
            "SELECT e.day, e.base_menu_id, e.levels_json, e.own_json FROM menu_emphasis e "
            "WHERE e.person_id = ? AND e.day >= ? AND e.id = (SELECT MAX(x.id) FROM "
            "menu_emphasis x WHERE x.person_id = e.person_id AND x.day = e.day) "
            f"AND (e.own_json IS NOT NULL OR NOT {FOLLOWED})",
            (person_id, since),
        ):
            try:
                levels = dict(json.loads(str(r["levels_json"])))
                own = None if r["own_json"] is None else set(json.loads(str(r["own_json"])))
            except ValueError, TypeError:
                continue
            for slot, level in levels.items():
                if level != "skip" or (own is not None and slot not in own):
                    continue
                dish = db.scalar(
                    "SELECT rc.name FROM menu_days md JOIN meal_slots ms ON ms.menu_day_id = md.id "
                    "JOIN recipes rc ON rc.id = ms.recipe_id "
                    "WHERE md.menu_id = ? AND md.day = ? AND ms.slot = ?",
                    (int(r["base_menu_id"]), str(r["day"]), slot),
                )
                if dish:
                    self.skipped[str(dish).casefold()].append(parse_day(str(r["day"])))
        # the ingredient prior: each ingredient's lean, from the dishes rated, plus what they
        # said they like
        lean: dict[str, float] = defaultdict(float)
        weight: dict[str, float] = defaultdict(float)
        for score, age, rid in self.rated.values():
            w = _fade(age, PREFERENCE_DAYS)
            for code, _g in _ingredients(db, rid):
                lean[code] += w * (score - 3) / 2.0
                weight[code] += w
        # a food changed inside a dish (food/subst.py): the one put in leans in, the one taken
        # out leans away - more quietly than a refusal, which takes it off the list altogether
        for r in db.all(
            "SELECT code, kind, day FROM ingredient_events WHERE person_id = ? AND day >= ?",
            (person_id, since),
        ):
            w = _fade((on - parse_day(str(r["day"]))).days, PREFERENCE_DAYS)
            lean[str(r["code"])] += w * (1.0 if str(r["kind"]) == "swapped_in" else -1.0)
            weight[str(r["code"])] += w
        self.affinity = {code: lean[code] / (weight[code] + 2.0) for code in lean}
        liked = catalogue.liked_terms(db, person_id)
        if liked:
            for ing in catalogue.latest_versions(db):
                if any(catalogue.refused_matches(t, ing) for t in liked):
                    self.affinity[ing.code] = self.affinity.get(ing.code, 0.0) + 0.5

    def prior(self, recipe_id: int | None) -> float:
        """What the dish's ingredients predict, 0-1, before anything is known of the dish."""
        if recipe_id is None:
            return 0.5
        parts = _ingredients(self.db, recipe_id)
        total = sum(g for _c, g in parts)
        if not total:
            return 0.5
        lean = sum(self.affinity.get(c, 0.0) * g for c, g in parts) / total
        return 0.5 + 0.3 * math.tanh(lean)

    def prior_of(self, grams: Mapping[str, float]) -> float:
        """What a dish never stored predicts from its foods (code -> grams), 0-1."""
        total = sum(grams.values())
        if not total:
            return 0.5
        lean = sum(self.affinity.get(c, 0.0) * g for c, g in grams.items()) / total
        return 0.5 + 0.3 * math.tanh(lean)

    def taste(self, name: str, recipe_id: int | None = None, *, answered: bool = True) -> Taste:
        """`answered=False` leaves out the plates they said they ate, and keeps only what they
        said of the dish and did with it: what two people can be compared on (`shared_liking`)."""
        key = name.casefold()
        rid = recipe_id if recipe_id is not None else self.recipe_of.get(key)
        pos = neg = 0.0
        rated = self.rated.get(key)
        if rated is not None:
            score, age, _rid = rated
            w = _fade(age, PREFERENCE_DAYS)
            pos += w * 2.0 * (score - 1) / 4.0
            neg += w * 2.0 * (5 - score) / 4.0
        eaten = self.eaten.get(key, [])
        # eaten, as they said: once per week it was on the plate, not once per box
        weeks = {d.isocalendar()[:2] for d in self.ate.get(key, [])} if answered else set()
        for year, week in weeks:
            monday = dt.date.fromisocalendar(year, week, 1)
            pos += _fade((self.on - monday).days, PREFERENCE_DAYS)
        for kind, day in self.swaps.get(key, []):
            w = _fade((self.on - day).days, PREFERENCE_DAYS)
            if kind == "swapped_in":
                pos += 1.5 * w
            else:
                bored = any(0 <= (day - d).days <= BOREDOM_SWAP_DAYS for d in eaten)
                neg += (0.3 if bored else 1.0) * w
        for day in self.skipped.get(key, []):
            neg += 0.3 * _fade((self.on - day).days, PREFERENCE_DAYS)
        m0 = self.prior(rid)
        liking = (PRIOR_STRENGTH * m0 + pos) / (PRIOR_STRENGTH + pos + neg)
        boredom = sum(_fade((self.on - d).days, BOREDOM_DAYS) for d in eaten)
        kind = self.kind_of.get(key)
        if kind is None and rid is not None:
            kind = _main_protein(self.db, rid)
        kind_boredom = (
            sum(_fade((self.on - d).days, KIND_DAYS) for d in self.kind_days.get(kind, []))
            if kind is not None
            else 0.0
        )
        return Taste(name, round(liking, 4), boredom, kind_boredom, pos + neg)


def tastes(
    db: Db, person_id: int, dishes: Mapping[str, int | None] | Iterable[str], on: dt.date
) -> dict[str, Taste]:
    """name -> Taste for each dish, for one person on one day. `dishes` maps a name to a recipe
    id (for the ingredient prior of a dish never eaten), or is just names."""
    history = _History(db, person_id, on)
    pairs = dishes.items() if isinstance(dishes, Mapping) else ((n, None) for n in dishes)
    return {name: history.taste(name, rid) for name, rid in pairs}


def draft_tastes(
    db: Db, person_id: int, on: dt.date, dishes: Mapping[str, Mapping[str, float]]
) -> dict[str, Taste]:
    """name -> Taste for dishes the model has just written, each given as code -> grams: one
    they have had before by its own evidence, a new one by what its foods predict."""
    history = _History(db, person_id, on)
    out: dict[str, Taste] = {}
    for name, grams in dishes.items():
        key = name.casefold()
        if key in history.eaten or key in history.rated or key in history.swaps:
            out[name] = history.taste(name)
        else:
            out[name] = Taste(name, round(history.prior_of(grams), 4), 0.0, 0.0, 0.0)
    return out


def affinity(db: Db, person_id: int, on: dt.date) -> dict[str, float]:
    """code -> how far this person leans to a food (0 is no word either way; about +-1 at the
    most): from the dishes they rated, the foods they put in or took out of a dish, and the foods
    they said they like. What an ingredient swap ranks by (food/subst.py)."""
    return dict(_History(db, person_id, on).affinity)


def couple_score(a: Taste, b: Taste, weight: float = 0.5) -> float | None:
    """Both people's score for one dish they would cook together, or None when either is
    predicted to dislike it. Geometric, so one lukewarm plate is not carried by the other.

    `weight` is the first person's share (0.5 is even): fairness over time leans it toward
    whoever the recent shared meals suited less (`fair_weight`)."""
    if a.liking < VETO or b.liking < VETO:
        return None
    w = min(max(weight, 0.0), 1.0)
    return max(a.score, 0.01) ** w * max(b.score, 0.01) ** (1.0 - w)


#: how far back shared meals count for fairness, and how far it may lean
FAIR_DAYS = 56
FAIR_MAX_LEAN = 0.2


def shared_liking(db: Db, person_id: int, other_id: int, on: dt.date) -> tuple[float, float]:
    """How much each of the two liked the dishes they ate together lately, on average (0-1
    each): the dishes on both plates on a shared weekend day in the last FAIR_DAYS days.

    Read from what each of them said and did about a dish - ratings, swaps, skips - and only
    for a dish both said something about. The plates of an answered day were evidence too, and
    only one of them may answer their days: whoever did looked as if every shared dish suited
    them, and the weight leant for good toward whoever answered less."""
    since = isoday(on - dt.timedelta(days=FAIR_DAYS))
    rows = db.all(
        "SELECT DISTINCT a.day, ra.name FROM v_menu_days_current a "
        "JOIN meal_slots sa ON sa.menu_day_id = a.id JOIN recipes ra ON ra.id = sa.recipe_id "
        "JOIN v_menu_days_current b ON b.day = a.day AND b.person_id = ? "
        "JOIN meal_slots sb ON sb.menu_day_id = b.id AND sb.slot = sa.slot "
        "JOIN recipes rb ON rb.id = sb.recipe_id AND rb.name = ra.name "
        "WHERE a.person_id = ? AND a.day >= ? AND a.day < ? "
        "AND strftime('%w', a.day) IN ('0', '6')",
        (other_id, person_id, since, isoday(on)),
    )
    names = sorted({str(r["name"]) for r in rows})
    if not names:
        return (0.5, 0.5)
    mine, theirs = _History(db, person_id, on), _History(db, other_id, on)
    pairs = [(mine.taste(n, answered=False), theirs.taste(n, answered=False)) for n in names]
    said = [(a, b) for a, b in pairs if a.evidence > 0 and b.evidence > 0]
    if not said:
        return (0.5, 0.5)
    return (
        sum(a.liking for a, _b in said) / len(said),
        sum(b.liking for _a, b in said) / len(said),
    )


def fair_weight(db: Db, person_id: int, other_id: int, on: dt.date) -> float:
    """This person's share in the couple's score: more than half when the dishes they have
    shared lately suited them less than the other, up to FAIR_MAX_LEAN either way (Stratigi
    2020: fairness in group recommendation is over a sequence, not one meal)."""
    mine, theirs = shared_liking(db, person_id, other_id, on)
    return 0.5 + max(-FAIR_MAX_LEAN, min(FAIR_MAX_LEAN, theirs - mine))


def why(
    t: Taste,
    *,
    score: int | None,
    last_day: dt.date | None,
    on: dt.date,
    new: bool,
    had: Collection[dt.date] | None = None,
    chose: bool = False,
) -> str:
    """A few words on why a dish is offered: "rated 5; last had 3 weeks ago". A rationale is
    part of a choice that stays the person's own (Ntoumanis 2021).

    `on` is the day it is read - today, not the day the dish would be eaten, so "last had"
    counts back from now. `had` is the days before it the dish was on their plate, answered or
    not (only `last_day`, when not given): what "had" means in these words. The evidence behind
    the score is something else - it counts answered days and swaps - and read as "had" it
    called a dish on her plate for two weeks "not had yet", and one only ever swapped in "you
    often have it". `chose` is a dish they swapped in themselves.
    """
    plates = sorted(d for d in (had if had is not None else [last_day]) if d is not None and d < on)
    parts: list[str] = []
    if new:
        parts.append("new, written for this meal")
    elif score is not None:
        parts.append(f"rated {score}")
    elif len({d.isocalendar()[:2] for d in plates}) >= 2:
        parts.append("you often have it")
    elif not plates and chose:
        parts.append("you chose it before")
    elif not plates:
        parts.append("its foods suit you" if t.liking > 0.55 else "not had yet")
    if plates and not new:
        # resting as of `on`, from these plates: more than half the boredom penalty, as `Taste`
        boredom = sum(_fade((on - d).days, BOREDOM_DAYS) for d in plates)
        if (1.0 - math.exp(-boredom)) > 0.5:
            parts.append("had lately")
        else:
            parts.append(f"last had {_ago((on - plates[-1]).days)}")
    return "; ".join(parts)


def _ago(days: int) -> str:
    if days < 7:
        return "this week" if days <= 1 else f"{days} days ago"
    weeks = round(days / 7)
    if weeks < 9:
        return "a week ago" if weeks == 1 else f"{weeks} weeks ago"
    return f"{round(days / 30)} months ago"


def favourites_now(
    db: Db,
    person_id: int,
    names: Iterable[str],
    on: dt.date,
    *,
    log: bool = False,
    menu_id: int | None = None,
) -> list[str]:
    """The favourites worth asking for this week: none that is resting - a dish eaten in a
    batch last week reads as a favourite and is the one that wears out fastest - in the order of
    one Thompson draw each, seeded by person, week and dish. The best known still lead most
    weeks; a favourite with little behind it gets its turn now and then.

    `log` writes each one's mean and draw to `proposal_log`, for the menu `menu_id`, so what the
    menu was asked for can be checked against what was eaten."""
    names = list(names)
    known = tastes(db, person_id, names, on)
    dishes = [n for n in names if known[n].evidence > 0]
    words = [n for n in names if known[n].evidence == 0]  # a liked FOOD, not a dish: kept as is
    draws = {n: known[n].sampled(seed_for(person_id, on, n)) for n in dishes}
    ranked = sorted((n for n in dishes if not known[n].resting), key=lambda n: (-draws[n], n))
    if log:
        for n in dishes:
            db.insert(
                "proposal_log",
                person_id=person_id,
                day=isoday(on),
                dish=n,
                source="favourite",
                mu=round(known[n].liking, 4),
                sampled=round(draws[n], 4),
                chosen=0 if known[n].resting else 1,
                menu_id=menu_id,
            )
    return ranked + words


def resting(db: Db, person_id: int, on: dt.date) -> list[str]:
    """Dishes eaten recently enough that the next menu should let them rest, by name."""
    history = _History(db, person_id, on)
    out = []
    for key, days in history.eaten.items():
        if not days:
            continue
        rid = history.recipe_of.get(key)
        name = str(db.scalar("SELECT name FROM recipes WHERE id = ?", (rid,)) or key)
        if history.taste(name, rid).resting:
            out.append(name)
    return sorted(out)


def record_swap(
    db: Db, person_id: int, *, old: str, new: str, new_id: int | None, day: dt.date
) -> None:
    """A swap, as two rows: the dish taken away and the one chosen instead."""
    db.insert("dish_events", person_id=person_id, dish=old, recipe_id=None, kind="swapped_out",
              day=isoday(day))  # fmt: skip
    db.insert(
        "dish_events", person_id=person_id, dish=new, recipe_id=new_id, kind="swapped_in",
        day=isoday(day),
    )  # fmt: skip


__all__ = [
    "VETO",
    "Taste",
    "affinity",
    "couple_score",
    "draft_tastes",
    "fair_weight",
    "favourites_now",
    "record_swap",
    "resting",
    "seed_for",
    "shared_liking",
    "tastes",
    "why",
]
