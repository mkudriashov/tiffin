"""Track the packs opened by each shopping window and the meals they cover.

A window is the set of days bought for in one shop. Pack credits carry forward
while food keeps, so the solver can prefer open packs before buying another."""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from ..clock import isoday, parse_day
from ..db import Db

#: the aisles a main protein comes from: the packs a window keeps to a few of
MAIN_AISLES = ("meat", "fish")
#: the aisles whose open packs go off in days, and so are worth a "use first"
FRESH_AISLES = ("meat", "fish", "dairy", "produce", "bakery", "chilled")
#: an open main pack with at least this much left is a meal still to cook from it
MAIN_MEAL_G = 100.0
#: anything else is worth naming when at least this share of its pack is left
FRESH_SHARE = 0.25
#: two main proteins a week")
MAINS_PER_WINDOW = 2
#: open packs a prompt names, main proteins first then what keeps least
PROMPT_PACKS = 8
DAY_WORDS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
#: the meals a main protein is the centre of; a breakfast's rasher of bacon is steered by the
#: breakfasts' own packs, never made the reason a dinner must be bacon
MAIN_MEALS = ("lunch", "dinner")
#: days an opened pack of raw meat or fish keeps in the fridge after the day it is opened; what
#: open before freezer")
FRESH_OPEN_DAYS = 2
#: cured meat keeps a week once opened: bacon is not frozen after two days
CURED_OPEN_DAYS = 7
CURED_WORDS = ("bacon", "ham", "chorizo", "salami", "pancetta", "prosciutto")


def open_days(name: str) -> int:
    """How long an opened pack of this meat or fish keeps in the fridge."""
    words = name.casefold()
    return CURED_OPEN_DAYS if any(w in words for w in CURED_WORDS) else FRESH_OPEN_DAYS


#: a freezer reminder smaller than this is not worth a sentence
FREEZE_MIN_G = 40.0
#: a whole week's meat or fish that eats less than this share of the packs bought for it, with at
#: 1kg of chicken that has only 200g portion over the whole week"). Half, so rounding stays: his
#: 682 g of mince in two 500 g packs is two-thirds used and kept
WASTE_SHARE = 0.5
WASTE_MIN_G = 200.0
#: a bag of vegetables or herbs, for someone holding them to their packs too: this much over is
#: worth a re-ask (a 30 g bunch of herbs with 5 g used is 25 g over, and is not)
FRESH_WASTE_MIN_G = 60.0
#: the pack sizes a prompt names
PROMPT_SIZES = 12


@dataclass
class Pack:
    """One food over a window: the whole packs its days need, and what is left of the last."""

    code: str
    name: str
    aisle: str
    pack_g: float
    pack_label: str
    by_day: dict[str, float] = field(default_factory=dict)
    #: the meals that cook from it
    slots: set[str] = field(default_factory=set)
    #: (day eaten, day cooked, grams): a batch's boxes are cooked on the cook's first day, a
    #: meal cooked each day on its own day - and it is the cooking that opens the pack
    uses: list[tuple[str, str, float]] = field(default_factory=list)
    #: (day eaten, meal, grams): which meal each use is, for the line on that meal's card
    meals: list[tuple[str, str, float]] = field(default_factory=list)

    @property
    def used_g(self) -> float:
        return sum(self.by_day.values())

    @property
    def packs(self) -> int:
        from .shopping import packs_for  # noqa: PLC0415 (shopping imports menu, which imports this)

        return packs_for(self.used_g, self.pack_g)[0]

    @property
    def left_g(self) -> float:
        from .shopping import packs_for  # noqa: PLC0415

        return packs_for(self.used_g, self.pack_g)[1]

    @property
    def main(self) -> bool:
        return self.aisle in MAIN_AISLES

    @property
    def days(self) -> list[dt.date]:
        return sorted(parse_day(d) for d, g in self.by_day.items() if g > 0)

    @property
    def fresh_until(self) -> dt.date | None:
        """The last day an opened pack of meat or fish keeps in the fridge; None for the rest."""
        if not self.main or not self.uses:
            return None
        opened = min(parse_day(cook) for _eat, cook, _g in self.uses)
        return opened + dt.timedelta(days=open_days(self.name))

    @property
    def frozen_days(self) -> list[dt.date]:
        """The days whose meal is cooked from it after the fridge has had it: from the freezer.
        Never a batch's boxes - the whole pot is cooked on the cook day."""
        last = self.fresh_until
        if last is None:
            return []
        return sorted({parse_day(eat) for eat, cook, _g in self.uses if parse_day(cook) > last})

    def to_freeze_g(self) -> float:
        """What of the opened packs goes in the freezer on the last fresh day: the later days'
        share of what is left of them (a later day's further pack is bought and opened fresh)."""
        last = self.fresh_until
        if last is None:
            return 0.0
        fresh = sum(g for _eat, cook, g in self.uses if parse_day(cook) <= last)
        later = sum(g for _eat, cook, g in self.uses if parse_day(cook) > last)
        if later <= 0 or fresh <= 0:
            return 0.0
        opened = -(-round(fresh * 10) // round(self.pack_g * 10))
        spare = opened * self.pack_g - fresh
        return float(round(min(later, max(spare, 0.0))))

    def worth_using(self) -> bool:
        """A meal's worth left (a main protein), or a good share of a pack that goes off soon."""
        if self.main:
            return self.left_g >= MAIN_MEAL_G
        return self.aisle in FRESH_AISLES and self.left_g >= FRESH_SHARE * self.pack_g


def window_of(db: Db, person_id: int, day: dt.date) -> list[dt.date]:
    """The days one shop buys for, around `day`: their own shops where they set them
    (food/weekplan.py), else the batch cook's segment it is in, else Monday to Friday (a person
    who cooks each day shops for the week's weekdays), else the weekend."""
    from . import menu, weekplan  # noqa: PLC0415 (menu imports shopping, which reads this module)

    if day.weekday() >= 5:
        saturday = day - dt.timedelta(days=day.weekday() - 5)
        return [saturday, saturday + dt.timedelta(days=1)]
    monday = day - dt.timedelta(days=day.weekday())
    shop = weekplan.of(db, person_id).shop_of(day.weekday())
    if shop is not None:
        # their week is bought in shops of its own: Sunday's for Monday to Wednesday, a top-up
        return [monday + dt.timedelta(days=d) for d in shop]
    for seg in menu.week_segments(db, person_id, monday):
        if seg.start <= day <= seg.end and len(seg.days) > 1 and seg.end.weekday() < 5:
            return seg.days
    return [monday + dt.timedelta(days=i) for i in range(5)]


def window_packs(
    db: Db,
    person_id: int,
    days: Sequence[dt.date],
    *,
    retailers: Sequence[str] = (),
) -> list[Pack]:
    """Per food, the grams each of `days` plates in force use, against the pack a list buys -
    the real product's where one is known, as the list itself does. Foods kept at home
    (their own "always at home" list) are not bought, so they are no pack."""
    from . import catalogue, products  # noqa: PLC0415
    from .shopping import range_plates  # noqa: PLC0415
    from .solver import whole_units  # noqa: PLC0415

    plates = range_plates(db, person_id, days)
    if not plates:
        return []
    marks = ",".join("?" for _ in plates)
    rows = db.all(
        "SELECT md.menu_id, md.day, ms.slot, ri.grams, ms.portion_multiplier, rc.servings, "
        "i.id AS iid, "
        "i.code, i.name, i.aisle, i.pack_g, i.pack_label "
        "FROM menu_days md JOIN meal_slots ms ON ms.menu_day_id = md.id "
        "JOIN recipes rc ON rc.id = ms.recipe_id "
        "JOIN recipe_ingredients ri ON ri.recipe_id = rc.id "
        "JOIN ingredients i ON i.id = ri.ingredient_id "
        f"WHERE md.menu_id IN ({marks}) ORDER BY i.id",
        tuple(plates),
    )
    units = catalogue.units(db)
    home = catalogue.pantry_codes(db, person_id)
    cooked_on: dict[tuple[str, str], str] = {}

    def cook_day(on: str, slot: str) -> str:
        """The day this meal is cooked: its batch's first day where it is made ahead."""
        key = (on, slot)
        if key not in cooked_on:
            from .. import targets as targets_mod  # noqa: PLC0415 (targets imports this layer)
            from . import menu, planning  # noqa: PLC0415 (menu imports shopping)

            day = parse_day(on)
            cooked_on[key] = on
            if slot in planning.batched(targets_mod.profile_on(db, person_id, day)):
                monday = day - dt.timedelta(days=day.weekday())
                for seg in menu.week_segments(db, person_id, monday):
                    if seg.start <= day <= seg.end and len(seg.days) > 1:
                        cooked_on[key] = isoday(seg.start)
        return cooked_on[key]

    out: dict[str, Pack] = {}
    for r in rows:
        on = str(r["day"])
        if on not in plates.get(int(r["menu_id"]), ()):
            continue  # another menu's plates feed that day
        code = str(r["code"])
        if code in home:
            continue
        grams = float(r["grams"]) * float(r["portion_multiplier"]) / int(r["servings"])
        if code in units:
            grams = whole_units(grams, units[code].g)
        pack = out.get(code)
        if pack is None:
            pack = out[code] = Pack(
                code, str(r["name"]), str(r["aisle"]), float(r["pack_g"]), str(r["pack_label"])
            )
        else:
            # the newest catalogue version names the food, as a list does
            pack.name, pack.pack_g, pack.pack_label = (
                str(r["name"]),
                float(r["pack_g"]),
                str(r["pack_label"]),
            )
        pack.by_day[on] = pack.by_day.get(on, 0.0) + grams
        pack.slots.add(str(r["slot"]))
        pack.uses.append((on, cook_day(on, str(r["slot"])), grams))
        pack.meals.append((on, str(r["slot"]), grams))
    if out:
        from . import weekplan  # noqa: PLC0415

        need = (
            {c: p.used_g for c, p in out.items()}
            if weekplan.of(db, person_id).least_waste
            else None
        )
        shop = products.for_shopping(
            db, person_id, {c: p.pack_g for c, p in out.items()}, retailers, need=need
        )
        for code, product in shop.items():
            if product.pack_g:
                out[code].pack_g = float(product.pack_g)
    return sorted(out.values(), key=_order)


def _order(p: Pack) -> tuple[int, int, str]:
    from .shopping import keeps_days  # noqa: PLC0415

    return (0 if p.main else 1, keeps_days(p.aisle), p.name.casefold())


def open_for(
    db: Db,
    person_id: int,
    building: Collection[dt.date],
    *,
    allowed: Collection[str] | None = None,
) -> list[Pack]:
    """The packs a build of `building` should cook from first: the window's packs from its other
    days with something worth using left - main proteins first, then what keeps least. A food
    no longer `allowed` (avoided since it was planned) is never offered back."""
    if not building:
        return []
    first = min(building)
    window = window_of(db, person_id, first)
    others = [d for d in window if d not in set(building)]
    if not others:
        return []
    got = window_packs(db, person_id, others)
    return [p for p in got if p.worth_using() and (allowed is None or p.code in set(allowed))]


def mains_in(db: Db, person_id: int, building: Collection[dt.date]) -> list[Pack]:
    """The main proteins already planned on the window's other days, open or used up."""
    if not building:
        return []
    window = window_of(db, person_id, min(building))
    others = [d for d in window if d not in set(building)]
    return [p for p in window_packs(db, person_id, others) if p.main] if others else []


#: the order meals are eaten in a day, for what is left of a pack after each
MEAL_ORDER = {"breakfast": 0, "lunch": 1, "snack": 2, "dinner": 3, "pre_bed": 4}
DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
SLOT_WORDS = {"breakfast": "breakfast", "lunch": "lunch", "dinner": "dinner", "snack": "the snack"}


def meal_lines(
    db: Db, person_id: int, days: Sequence[dt.date], *, retailers: Sequence[str] = ()
) -> dict[tuple[str, str], list[str]]:
    """(iso day, meal) -> where that meal's meat or fish comes from, one line a food: "Chicken
    "Monday Chicken (Bought 500g pack, used 200g) ... Wednesday Chicken (300g leftover from prev.
    pack)"). Read from the shop window's plates in force, the pack the list buys, so it is never
    stored and a swap changes it at once. The shop is the day before the window's first day."""
    out: dict[tuple[str, str], list[str]] = {}
    seen: set[dt.date] = set()
    for day in sorted(days):
        if day in seen or day.weekday() >= 5:
            continue
        window = window_of(db, person_id, day)
        seen |= set(window)
        bought = DAY_NAMES[(window[0] - dt.timedelta(days=1)).weekday()]
        for p in window_packs(db, person_id, window, retailers=retailers):
            if not p.main or p.pack_g <= 0:
                continue
            meals = sorted(p.meals, key=lambda m: (m[0], MEAL_ORDER.get(m[1], 9)))
            run = 0.0
            name = short(p.name)
            for n, (on, slot, grams) in enumerate(meals):
                start, run = run, run + grams
                first = int(start // p.pack_g) + 1
                # the pack this meal ends in: a meal can finish one pack and open the next
                last = max(first, -(-round(run * 10) // round(p.pack_g * 10)))
                left = last * p.pack_g - run
                which = (
                    f"{bought}'s {p.pack_g:g} g pack"
                    if p.packs <= 1
                    else f"{bought}'s {p.pack_g:g} g packs ("
                    + (f"{first} of {p.packs}" if first == last else f"{first} and {last}")
                    + ")"
                )
                later = next((m for m in meals[n + 1 :] if m[0] != on or m[1] != slot), None)
                if left < 1:
                    tail = "pack finished"
                elif later is not None and later[0] == on:
                    tail = f"{left:.0f} g left for {SLOT_WORDS.get(later[1], later[1])}"
                elif later is not None:
                    tail = f"{left:.0f} g left for {DAY_WORDS[parse_day(later[0]).weekday()]}"
                else:
                    tail = f"{left:.0f} g over"
                out.setdefault((on, slot), []).append(f"{name}: {grams:.0f} g from {which}, {tail}")
    return out


def days_words(days: Sequence[dt.date]) -> str:
    return ", ".join(DAY_WORDS[d.weekday()] for d in days)


def size_words(pack: Pack) -> str:
    """The packs bought, as a person reads them: "500 g", "2 x 500 g" -> "1 kg"."""
    return amount_words(pack.packs, pack.pack_g)


def amount_words(n: int, pack_g: float) -> str:
    total = n * pack_g
    if total >= 1000 and abs(total / 1000 - round(total / 1000, 1)) < 1e-9:
        return f"{total / 1000:g} kg"
    if n > 1:
        return f"{n} x {pack_g:g} g"
    return f"{pack_g:g} g"


def line(pack: Pack) -> str:
    """ "Chicken thigh fillets 1 kg: Tue, Wed, Thu; 120 g left" - the days a pack is for and
    what will be over."""
    name = pack.name.split(",")[0]
    left = f"{pack.left_g:.0f} g left" if pack.left_g >= 1 else "all used"
    frozen = pack.frozen_days
    if frozen:
        fresh = [d for d in pack.days if d not in frozen]
        when = f"{days_words(fresh)}, then {days_words(frozen)} from the freezer"
    else:
        when = days_words(pack.days)
    return f"{name} {size_words(pack)}: {when}; {left}"


def lines(
    db: Db,
    person_id: int,
    day: dt.date,
    *,
    retailers: Sequence[str] = (),
    limit: int = 6,
) -> list[str]:
    """One line per pack that matters in the window around `day`: every main protein, and the
    fresh foods cooked on more than one day. For Shopping, Menu and the bot's list."""
    window = window_of(db, person_id, day)
    got = window_packs(db, person_id, window, retailers=retailers)
    keep = [p for p in got if p.main or (p.aisle in FRESH_AISLES and len(p.days) > 1 and p.packs)]
    return [line(p) for p in keep[:limit]]


def window_view(
    db: Db, person_id: int, day: dt.date, *, retailers: Sequence[str] = ()
) -> dict[str, object]:
    """The page's reading: the window's days and its pack lines."""
    window = window_of(db, person_id, day)
    return {
        "from": isoday(window[0]),
        "to": isoday(window[-1]),
        "days": days_words(window),
        "span": f"{DAY_WORDS[window[0].weekday()]} to {DAY_WORDS[window[-1].weekday()]}",
        "lines": lines(db, person_id, day, retailers=retailers),
    }


def prompt_lines(packs: Sequence[Pack], index: Mapping[str, int]) -> list[str]:
    """For a build: each open pack by its number in the ingredient list the model was given."""
    out: list[str] = []
    for p in packs:
        if p.code not in index:
            continue
        days = days_words(p.days)
        last = p.fresh_until
        kept = (
            f"; in the freezer after {DAY_WORDS[last.weekday()]}" if last and p.frozen_days else ""
        )
        out.append(
            f"{index[p.code]}. {p.name}: {p.left_g:.0f} g left of a {p.pack_g:g} g pack "
            f"(opened for {days}{kept})"
        )
    return out


# ---------------------------------------------------------------- steering a build
def family(slot: str) -> str:
    return "main" if slot in MAIN_MEALS else "small"


def short(name: str) -> str:
    """ "Chicken thigh fillets, skinless, raw" -> "Chicken thigh fillets"."""
    return name.split(",", maxsplit=1)[0]


_PUT_OUT = re.compile(
    r"\b(?:no|not|never|without|avoid|less|fewer|skip|other than|instead of|rather than|"
    r"sick of|tired of|fed up|enough)\b"
)


def asked(note: str, name: str, aisle: str) -> bool:
    """Whether their note for this build names the food (its first word, or "fish" / "meat"),
    and nothing just before the name puts it out: "salmon on Thursday please" asks for a
    salmon pack; "no salmon" does not."""
    said = re.findall(r"[a-z]+", note.casefold())
    terms = {
        re.findall(r"[a-z]+", name.casefold())[0] if re.search(r"[a-z]", name.casefold()) else "",
        aisle,
    }
    for i, word in enumerate(said):
        if word and word in terms and not _PUT_OUT.search(" ".join(said[max(0, i - 3) : i])):
            return True
    return False


@dataclass(frozen=True)
class Steer:
    """What a build is held to about packs: the window's open packs (from its other days, foods
    still allowed), the main proteins those days' lunches and dinners already cook, and the budget.

    `strict` is off on the last attempt a build has: targets win, so a plan that could only be
    made by opening another pack is kept - and said (`opened`), never quietly."""

    open: tuple[Pack, ...] = ()
    planned: frozenset[str] = frozenset()
    names: Mapping[str, str] = field(default_factory=dict)
    budget: int = MAINS_PER_WINDOW
    note: str = ""
    strict: bool = True
    #: the shop's days, as the prompt says them: "Mon to Fri"
    span: str = ""
    #: food code -> day -> the grams the closed windows' other days already plate of it
    used: Mapping[str, Mapping[str, float]] = field(default_factory=dict)
    #: meat or fish code -> the pack a list buys of it, the shop's own where one is known
    sizes: Mapping[str, float] = field(default_factory=dict)
    #: food code -> every pack size the shop sells it in: the one that leaves least over is the
    #: one weighed (food/weekplan.py `least_waste`); a food not here is weighed at `sizes`
    options: Mapping[str, tuple[float, ...]] = field(default_factory=dict)
    #: the shop windows this build leaves with no weekday unplanned, as their iso days: what a
    #: pack has over in one of these stays over. The weekend is bought on one joint list for
    #: both, so one person's half of it is no measure of a pack, and is never one
    windows: tuple[tuple[str, ...], ...] = ()
    #: the aisles besides meat and fish held to their packs too (vegetables and herbs, for
    #: someone who said a bag left over goes in the bin)
    fresh: tuple[str, ...] = ()
    #: iso days whose dishes neither count to the budget nor open a pack against the rule: a
    #: steak day's steak and fish are their own (food/weekplan.py `steak_day`)
    free: frozenset[str] = frozenset()

    @property
    def closes(self) -> bool:
        return bool(self.windows)

    def relaxed(self) -> Steer:
        return replace(self, strict=False)

    def prompt(self, index: Mapping[str, int]) -> tuple[list[str], str]:
        """(the open packs by number, the main-protein line) for the prompt."""
        mains = sorted(self.names.get(c, c) for c in self.planned)
        shop = f"of this shop ({self.span})" if self.span else "of this shop"
        line = (
            (f"the lunches and dinners {shop} already cook {', '.join(mains)}. " if mains else "")
            + (
                f"Keep the lunches and dinners {shop} to at most {self.budget} main "
                "proteins (meat or fish) in all, and cook"
                if self.budget
                else "Cook"
            )
            + (
                " an open pack until it is used before opening another, unless they asked for "
                "something else."
            )
        )
        sized = sorted((index[c], g) for c, g in self.sizes.items() if c in index)
        if self.closes and sized:
            fresh = " and the vegetables" if self.fresh else ""
            # the week's whole shop is this build: the scale of each pack, so a kilo of chicken
            # is planned into the week rather than one 200 g dinner
            line += (
                f" Meat and fish{fresh} are bought in whole packs for these days ("
                + "; ".join(f"number {n}: {g:g} g" for n, g in sized[:PROMPT_SIZES])
                + "): plan each one you use into enough meals to use most of its pack."
            )
        return prompt_lines(self.open, index)[:PROMPT_PACKS], line

    def wasted(
        self,
        grams: Mapping[str, Mapping[str, float]],
        foods: Mapping[str, tuple[str, str]],
        index: Mapping[str, int],
        *,
        person: bool = False,
        mains_only: bool = False,
    ) -> list[str]:
        """The food this plan buys a pack of and leaves mostly unused, in each shop window with
        no day left to cook the rest: for the re-prompt by number, or with `person`, for the menu
        message when it was kept anyway. `grams` is what the plan plates of each food, by day;
        `foods` each food's (name, aisle). Meat and fish always; `fresh` aisles when asked for. A
        food their note asks for is theirs to have, and "no limit" (a budget of 0, their own
        answer) refuses nothing, as in `problems`."""
        if not self.windows or not self.budget:
            return []
        out: list[str] = []
        for window in self.windows:
            when = _span_words(window)
            for code in sorted(foods):
                name, aisle = foods[code]
                held = aisle in MAIN_AISLES or (aisle in self.fresh and not mains_only)
                if mains_only and open_days(name) > FRESH_OPEN_DAYS:
                    # cured meat keeps a week: a rasher of breakfast bacon is asked for in the
                    # prompt, never worth an attempt of its own
                    held = False
                total = sum(grams.get(code, {}).get(d, 0.0) for d in window)
                if not held or total <= 0:
                    continue
                total += sum(self.used.get(code, {}).get(d, 0.0) for d in window)
                best = self.least_left(code, total)
                if best is None or asked(self.note, name, aisle):
                    continue
                n, size, left = best
                floor = WASTE_MIN_G if aisle in MAIN_AISLES else FRESH_WASTE_MIN_G
                if left < floor or total >= WASTE_SHARE * n * size:
                    continue
                bought = amount_words(n, size)
                number = f" (number {index[code]})" if code in index else ""
                out.append(
                    f"{short(name)} uses {total:.0f} g of the {bought} bought for {when}: no "
                    "menu using more of it met your targets"
                    if person
                    else f"{name}{number} is bought as {bought} for {when} and those days cook "
                    f"only {total:.0f} g of it, {left:.0f} g never eaten: put it in more of "
                    "those days' meals, or use a food those days already cook"
                )
        return out

    def least_left(self, code: str, total: float) -> tuple[int, float, float] | None:
        """(packs, pack size, grams over) for `total` of a food, in the pack size that leaves
        least over (then the smaller pack); None when no size is known."""
        from .shopping import packs_for  # noqa: PLC0415 (shopping imports menu, which imports this)

        sizes = self.options.get(code) or ((self.sizes[code],) if code in self.sizes else ())
        best: tuple[int, float, float] | None = None
        for size in sizes:
            if size <= 0:
                continue
            n, left = packs_for(total, size)
            if best is None or (left, size) < (best[2], best[1]):
                best = (n, size, left)
        return best

    def problems(
        self,
        uses: Mapping[str, Collection[tuple[str, str, str, str]]],
        index: Mapping[str, int],
        *,
        person: bool = False,
    ) -> list[str]:
        """Why a plan breaks the window's packs: for the model's re-prompt, by number, or with
        `person`, for the menu message when it was kept anyway. `uses` maps a dish's key
        ("day|slot") to the main proteins it cooks, as (code, name, aisle, dish name).

        A budget of 0 is "no limit", the person's own answer: nothing is refused, and the open
        packs are still offered first in the prompt."""
        if not self.budget:
            return []
        out: list[str] = []
        new_main: dict[str, str] = {}
        said: set[tuple[str, str]] = set()
        # the open packs the plan already cooks from, by kind of meal: one in use is being used
        # up, so opening another beside it is the budget's to judge, not this rule's. Without it
        # two open packs refused each other: chicken refused while the beef was open, beef while
        in_use: dict[str, set[str]] = {}
        for key, foods in uses.items():
            in_use.setdefault(family(key.split("|", 1)[1]), set()).update(c for c, *_ in foods)
        for key, foods in sorted(uses.items()):
            slot = key.split("|", 1)[1]
            if key.split("|", 1)[0] in self.free:
                continue
            fam = family(slot)
            for code, name, aisle, dish in foods:
                if code in self.planned or asked(self.note, name, aisle):
                    continue
                if fam == "main":
                    new_main.setdefault(code, name)
                held = [
                    p
                    for p in self.open
                    if p.main
                    and p.code != code
                    and p.code not in in_use.get(fam, set())
                    and p.left_g >= MAIN_MEAL_G
                    and any(family(s) == fam for s in p.slots)
                ]
                if held and (dish, code) not in said:
                    said.add((dish, code))
                    p = held[0]
                    number = f" (number {index[p.code]})" if p.code in index else ""
                    out.append(
                        f"{short(name)} is a new pack though {p.left_g:.0f} g of "
                        f"{short(p.name)} is still open: no menu cooking from it met your targets"
                        if person
                        else f"{slot} {dish!r} opens a pack of {name} while {p.left_g:.0f} g of "
                        f"{p.name}{number} is open in this shop window: cook from that first"
                    )
        mains = set(self.planned) | set(new_main)
        if self.budget and len(mains) > self.budget and new_main:
            names = sorted({**self.names, **new_main}.get(c, c) for c in mains)
            keep = sorted(self.names.get(c, c) for c in self.planned)
            out.append(
                f"This shop's lunches and dinners now cook {len(mains)} main proteins "
                f"({', '.join(short(n) for n in names)}): no menu with fewer met your targets"
                if person
                else f"lunches and dinners over this shop window cook {len(mains)} main proteins "
                f"({', '.join(names)}): keep to {self.budget}"
                + (f", using {', '.join(keep)} first" if keep else "")
            )
        return out


def steer_for(
    db: Db,
    person_id: int,
    building: Collection[dt.date],
    *,
    allowed: Collection[str],
    note: str = "",
    budget: int = MAINS_PER_WINDOW,
    items: Sequence[Any] = (),
    retailers: Sequence[str] = (),
) -> Steer:
    """The packs a build of `building` is held to. `items` (the foods it is offered) and
    `retailers` give the pack each food is bought in, as the list will buy it.

    The open packs are the first day's shop window's, from its other days. Each window the build
    touches that is then fully planned is weighed for waste. For someone whose week is bought in
    shops of their own (food/weekplan.py), the main-protein budget is the week's: two meats a
    week, however many shops buy them."""
    from . import menu, products, weekplan  # noqa: PLC0415 (menu imports this module)

    if not building:
        return Steer(note=note, budget=budget)
    plan = weekplan.of(db, person_id)
    built = set(building)
    first = min(building)
    window = window_of(db, person_id, first)
    others = [d for d in window if d not in built]
    got = window_packs(db, person_id, others, retailers=retailers) if others else []
    allowed = set(allowed)
    seen = {p.code for p in got}
    # the main proteins already planned: the window's, or the week's when shops split it
    planned_from = got
    if plan.shops and first.weekday() < 5:
        monday = first - dt.timedelta(days=first.weekday())
        week = [monday + dt.timedelta(days=i) for i in range(5)]
        rest = [d for d in week if d not in built]
        planned_from = window_packs(db, person_id, rest) if rest else []
    fresh = ("produce",) if plan.least_waste else ()
    weighed = MAIN_AISLES + fresh
    assumed = {
        i.code: float(i.pack_g)
        for i in items
        if i.aisle in weighed and i.code in allowed and i.pack_g
    }
    sizes = {
        **assumed,
        **{
            c: float(p.pack_g)
            for c, p in products.for_shopping(db, person_id, assumed, retailers).items()
            if p.pack_g
        },
        **{p.code: p.pack_g for p in got if p.aisle in weighed},
    }
    options = (
        products.pack_sizes(db, person_id, list(assumed), retailers) if plan.least_waste else {}
    )
    # every weekday shop window the build touches whose days are then all planned
    windows: list[tuple[str, ...]] = []
    covered: set[dt.date] = set()
    used: dict[str, dict[str, float]] = {}
    for day in sorted(built):
        if day in covered or day.weekday() >= 5:
            continue
        w = window_of(db, person_id, day)
        covered |= set(w)
        rest = [d for d in w if d not in built]
        # closed when every day after the build's last in it is planned: a gap before it (a day
        # long past, a day nobody planned) leaves no later day to cook a pack's rest
        last = max(d for d in w if d in built)
        if any(menu.current_menu(db, person_id, d) is None for d in rest if d > last):
            continue
        windows.append(tuple(isoday(d) for d in w))
        for p in window_packs(db, person_id, rest, retailers=retailers) if rest else []:
            for d, g in p.by_day.items():
                used.setdefault(p.code, {})[d] = used.get(p.code, {}).get(d, 0.0) + g
    span = sorted(covered) if plan.shops and covered else window
    return Steer(
        open=tuple(p for p in got if p.worth_using() and p.code in allowed)
        + tuple(
            p for p in carried(db, person_id, first) if p.code in allowed and p.code not in seen
        ),
        planned=frozenset(
            p.code
            for p in planned_from
            if p.main
            and any(family(s) == "main" for s in p.slots)
            and any(parse_day(d).weekday() != plan.steak_day for d in p.by_day)
        ),
        names={p.code: p.name for p in planned_from if p.main},
        budget=budget,
        note=note,
        span=f"{DAY_WORDS[span[0].weekday()]} to {DAY_WORDS[span[-1].weekday()]}",
        used=used,
        sizes=sizes,
        options=options,
        windows=tuple(windows),
        fresh=fresh,
        free=frozenset(isoday(d) for d in built if d.weekday() == plan.steak_day),
    )


def _span_words(days: Sequence[str]) -> str:
    first, last = parse_day(days[0]), parse_day(days[-1])
    if first == last:
        return DAY_WORDS[first.weekday()]
    return f"{DAY_WORDS[first.weekday()]} to {DAY_WORDS[last.weekday()]}"


def main_uses(
    drafts: Mapping[str, Any], cat: Sequence[Any]
) -> dict[str, set[tuple[str, str, str, str]]]:
    """For each dish key, the main proteins it cooks, from the indexes the model gave."""
    out: dict[str, set[tuple[str, str, str, str]]] = {}
    for key, draft in drafts.items():
        got: set[tuple[str, str, str, str]] = set()
        for part in draft.ingredients:
            if 1 <= part.i <= len(cat):
                ing = cat[part.i - 1]
                if ing.aisle in MAIN_AISLES:
                    got.add((ing.code, ing.name, ing.aisle, draft.name))
        out[key] = got
    return out


# ---------------------------------------------------------------- swaps, one-meal dishes, choices
@dataclass(frozen=True)
class Kitchen:
    """What a dish for one meal on one day would cook beside: the window's open main packs for
    that kind of meal, and the main proteins its other days already cook."""

    open: Mapping[str, Pack] = field(default_factory=dict)
    planned: frozenset[str] = frozenset()

    def opens_new(self, mains: Collection[str]) -> bool:
        return any(c not in self.planned for c in mains)

    def uses_open(self, mains: Collection[str]) -> Pack | None:
        return next((self.open[c] for c in mains if c in self.open), None)


def kitchen(
    db: Db, person_id: int, day: dt.date, slot: str, *, allowed: Collection[str] | None = None
) -> Kitchen:
    """The kitchen a swap or a new dish for `slot` on `day` is ranked against."""
    window = window_of(db, person_id, day)
    others = [d for d in window if d != day]
    got = window_packs(db, person_id, others) if others else []
    fam = family(slot)
    mine = [p for p in got if p.main and any(family(s) == fam for s in p.slots)]
    return Kitchen(
        open={
            p.code: p
            for p in mine
            if p.left_g >= MAIN_MEAL_G and (allowed is None or p.code in set(allowed))
        },
        planned=frozenset(p.code for p in mine),
    )


def recipe_mains(db: Db, recipe_id: int) -> set[str]:
    """The main proteins a stored dish cooks."""
    marks = ",".join("?" for _ in MAIN_AISLES)
    return {
        str(r["code"])
        for r in db.all(
            "SELECT DISTINCT i.code FROM recipe_ingredients ri JOIN ingredients i "
            f"ON i.id = ri.ingredient_id WHERE ri.recipe_id = ? AND i.aisle IN ({marks})",
            (recipe_id, *MAIN_AISLES),
        )
    }


#: how far back what they eat says which main proteins they like to cook
HISTORY_DAYS = 56


def week_mains(
    db: Db, person_id: int, days: Sequence[dt.date], *, allowed: Collection[str]
) -> list[str]:
    """The main proteins a week of choices is built around (at most `MAINS_PER_WINDOW`): a pack
    the window already has open first, then the ones their own lunches and dinners cooked most
    in the last eight weeks - the one cooked most, and the next two taking turns week by week, so
    the week's packs still change. [] when nothing they have eaten says (no limit then)."""
    if not days:
        return []
    allowed = set(allowed)
    first = min(days)
    opened = [
        p.code
        for p in open_for(db, person_id, days, allowed=allowed)
        if p.main and any(family(s) == "main" for s in p.slots)
    ]
    marks = ",".join("?" for _ in MAIN_AISLES)
    rows = db.all(
        "SELECT i.code, COUNT(DISTINCT md.day) AS n FROM v_menu_days_current md "
        "JOIN meal_slots ms ON ms.menu_day_id = md.id "
        "JOIN recipe_ingredients ri ON ri.recipe_id = ms.recipe_id "
        "JOIN ingredients i ON i.id = ri.ingredient_id "
        f"WHERE md.person_id = ? AND ms.slot IN ('lunch', 'dinner') AND i.aisle IN ({marks}) "
        "AND md.day >= ? AND md.day < ? GROUP BY i.code ORDER BY n DESC, i.code",
        (
            person_id,
            *MAIN_AISLES,
            isoday(first - dt.timedelta(days=HISTORY_DAYS)),
            isoday(first),
        ),
    )
    liked = [str(r["code"]) for r in rows if str(r["code"]) in allowed and r["code"] not in opened]
    out = opened[:MAINS_PER_WINDOW]
    want = MAINS_PER_WINDOW - len(out)
    top = liked[:3]
    if want <= 0 or not top:
        return out
    turn = first.isocalendar()[1]
    if want >= 2 and len(top) >= 2:
        pairs = [(0, 1), (0, 2)] if len(top) == 3 else [(0, 1)]
        a, b = pairs[turn % len(pairs)]
        return [*out, top[a], top[b]]
    return [*out, top[turn % len(top)]]


# ---------------------------------------------------------------- across a window's edge
def carried(db: Db, person_id: int, start: dt.date) -> list[Pack]:
    """Packs of meat or fish the shop before `start` opened and the fridge still keeps on `start`:
    by packs" on the weekend too). Only into the weekend: the weekend is bought on one joint list
    for both, so one person's share of what it leaves is no pack of theirs, and carrying it into
    the owner's batch week refused every dish he was offered."""
    if start.weekday() != 5:
        return []
    before = start - dt.timedelta(days=1)
    if before in window_of(db, person_id, start):
        return []
    prior = window_of(db, person_id, before)
    return [
        p
        for p in window_packs(db, person_id, prior)
        if p.main
        and p.left_g >= MAIN_MEAL_G
        and p.fresh_until is not None
        and p.fresh_until >= start
    ]


# ---------------------------------------------------------------- the freezer
def freeze_tonight(db: Db, person_id: int, day: dt.date) -> list[str]:
    """What goes in the freezer this evening: the meat or fish a pack opened two days ago still
    holds for a later day. "Freeze the Chicken thigh fillets for Fri tonight: about 160 g." """
    out: list[str] = []
    for p in window_packs(db, person_id, window_of(db, person_id, day)):
        if p.fresh_until != day or not p.frozen_days:
            continue
        grams = p.to_freeze_g()
        if grams >= FREEZE_MIN_G:
            out.append(
                f"Freeze the {short(p.name)} for {days_words(p.frozen_days)} tonight: "
                f"about {grams:.0f} g."
            )
    return out


# ---------------------------------------------------------------- a list, and what is at home
def covered(
    db: Db,
    person_id: int,
    first: dt.date,
    needs: Mapping[str, float],
    *,
    retailers: Sequence[str] = (),
) -> dict[str, str]:
    """For a list whose first day is `first`: the foods a pack the same shop window opened on an
    earlier day still covers - "Mon's pack should cover this: 320 g left". This is a hint for
    a line still on the list; a person can confirm stock with "I have it"."""
    window = window_of(db, person_id, first)
    earlier = [d for d in window if d < first]
    if not earlier:
        return {}
    packs = {p.code: p for p in window_packs(db, person_id, earlier, retailers=retailers)}
    out: dict[str, str] = {}
    for code, need in needs.items():
        p = packs.get(code)
        if p is None or not p.days or p.left_g < 1 or p.left_g + 1 < need:
            continue
        said = f"{DAY_WORDS[p.days[0].weekday()]}'s pack should cover this: {p.left_g:.0f} g left"
        last = p.fresh_until
        if last is not None and first > last:
            said += f", in the freezer since {DAY_WORDS[last.weekday()]}"
        out[code] = said
    return out


def have_codes(db: Db, person_id: int, first: dt.date, last: dt.date) -> set[str]:
    """The foods this person said they already have, for exactly these days."""
    latest: dict[str, bool] = {}
    for r in db.all(
        "SELECT ingredient_code, have FROM shopping_have WHERE person_id = ? AND from_day = ? "
        "AND to_day = ? ORDER BY id",
        (person_id, isoday(first), isoday(last)),
    ):
        latest[str(r["ingredient_code"])] = bool(r["have"])
    return {code for code, yes in latest.items() if yes}


def mark_have(db: Db, person_id: int, code: str, first: dt.date, last: dt.date, have: bool) -> None:
    db.insert(
        "shopping_have",
        person_id=person_id,
        ingredient_code=code,
        from_day=isoday(first),
        to_day=isoday(last),
        have=1 if have else 0,
    )


# ---------------------------------------------------------------- their own budget
#: what a person may answer: two, three, or no limit (0)
MAINS_ANSWERS = (2, 3, 0)
MAINS_QUESTION = (
    "Your menus now cook from about two meats or fish a week, each pack used up before the next "
    "is opened. Does that suit you?"
)
MAINS_BUTTONS = (("Two is fine", 2), ("Three", 3), ("No limit", 0))


def mains_answer(db: Db, person_id: int) -> int | None:
    """Their own word on how many main proteins a shop window keeps to; None until asked."""
    got = db.scalar(
        "SELECT mains FROM pack_prefs WHERE person_id = ? ORDER BY id DESC LIMIT 1", (person_id,)
    )
    return None if got is None else int(got)


def mains_budget(db: Db, person_id: int, default: int = MAINS_PER_WINDOW) -> int:
    """The budget a build keeps to: their own answer, else the house default."""
    got = mains_answer(db, person_id)
    return default if got is None else got


def set_mains(db: Db, person_id: int, mains: int, *, source: str) -> str:
    """Their answer, kept; returns what it now does, in a sentence for the page and the bot."""
    if mains not in MAINS_ANSWERS:
        raise ValueError("two, three, or no limit")
    db.insert("pack_prefs", person_id=person_id, mains=mains, source=source)
    return mains_said(mains)


def mains_said(mains: int) -> str:
    if mains == 0:
        return "Kept: no limit. A menu opens a new pack whenever a dish wants one."
    return (
        f"Kept: up to {mains} meats or fish a week, each pack used up before the next. "
        "Your next menus are built that way."
    )
