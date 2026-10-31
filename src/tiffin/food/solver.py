"""Solve meal portions against a day's targets.

The grid search works in code, keeping whole-unit foods whole and respecting
portion bounds, food preferences and the day's macro floors."""

from __future__ import annotations

import dataclasses
import datetime as dt
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from ..energy import Macros
from ..errors import Infeasible

M = 10_000  # multiplier scale (ten-thousandths)
T = 10  # tenths of a gram / kcal
PRE_BED = "pre_bed"
#: grams a floor may be missed by and still count as met: below any label's own precision
FLOOR_SLACK_G = 0.5
DEFAULT_LO = 0.6
DEFAULT_HI = 1.6
DEFAULT_STEP = 0.05
DEFAULT_KCAL_TOL_PCT = 5
DEFAULT_FIBRE_MIN = 25
#: a use of a whole-unit food under this share of one unit is a part of one - a splash of egg wash,
#: a crumb coating - and is weighed as written rather than made a whole egg or slice
UNIT_MIN_SHARE = 0.35
#: how much smaller the plates of a main protein may be to save buying one more pack of it
FEWER_WITHIN = 0.88


def whole_units(grams: float, unit_g: float) -> float:
    """Grams of a food served whole, rounded to whole ones: 30 g of a 40 g slice is one slice.

    page's JavaScript rounds the same way); a use under UNIT_MIN_SHARE of one stays as written.
    """
    n = math.floor(grams / unit_g + 0.5)
    if n >= 1:
        return n * unit_g
    return unit_g if grams >= UNIT_MIN_SHARE * unit_g else grams


# per-slot table row: (m, kcal, protein, fat, fibre, carb, |m - 1|) all scaled by T*M except m
_Row = tuple[int, int, int, int, int, int, int]


@dataclass(frozen=True)
class UnitPart:
    """An ingredient of a slot served in whole units: its grams in one serving at x1.0, what one
    unit weighs, and what a gram of it carries."""

    grams: float
    unit_g: float
    kcal: float
    protein: float
    fat: float
    carb: float
    fibre: float


@dataclass(frozen=True)
class SlotOption:
    """One serving of the slot's recipe at multiplier 1.0.

    The macros are linear in the multiplier except for `units`: those foods go on the plate in
    whole ones, and the table the search reads counts them that way.
    """

    slot: str
    kcal: float
    protein_g: float
    fat_g: float
    carb_g: float
    fibre_g: float
    recipe_id: int | None = None
    grams: Mapping[str, float] = field(default_factory=dict)  # ingredient code -> g at x1.0
    units: tuple[UnitPart, ...] = ()


@dataclass(frozen=True)
class DaySolution:
    multipliers: dict[str, float]  # slot -> m, in slot order
    macros: Macros
    kcal_dev: int  # |kcal - target|
    sum_dev: float  # sum |m - 1|
    #: floors a best-effort day could not reach: (name, got, needed), in grams
    shortfall: tuple[tuple[str, int, int], ...] = ()


@dataclass(frozen=True)
class DayProblem:
    day: dt.date
    slots: tuple[SlotOption, ...]
    target: Macros


@dataclass(frozen=True)
class PackRule:
    code: str
    pack_g: float
    #: try one pack fewer first, when the portions it needs are within `FEWER_WITHIN` of the
    #: plan's: 1,116 g of turkey is three 500 g packs
    #: and 384 g over, or two and plates 10% smaller - and the days are solved again either way
    fewer_first: bool = False
    #: the days the rule rounds over, when not every day of the segment: one shop's days, for
    #: someone whose week is bought in two (food/weekplan.py `shops`) - a pack is bought per shop
    days: frozenset[dt.date] | None = None


@dataclass(frozen=True)
class Pin:
    code: str
    need_g: float  # before pinning
    credit_g: float
    packs: int
    total_g: float  # credit + packs x pack_g = grams cooked after pinning
    scale: float


@dataclass(frozen=True)
class SegmentSolution:
    days: list[DaySolution]
    pins: dict[str, Pin]


# ---------------------------------------------------------------- helpers
def _tenths(x: float) -> int:
    return round(x * T)


def grid(lo: float, hi: float, step: float) -> list[int]:
    """Multiplier grid in ten-thousandths, lo..hi inclusive."""
    if lo <= 0 or hi < lo or step <= 0:
        raise ValueError(f"bad grid lo={lo} hi={hi} step={step}")
    count = round((hi - lo) / step) + 1
    return [round((lo + k * step) * M) for k in range(count)]


def _unit_delta(opt: SlotOption, f: float) -> tuple[float, float, float, float, float]:
    """What serving the whole-unit foods whole adds to the linear macros at multiplier f:
    (kcal, protein, fat, carb, fibre)."""
    dk = dp = df = dc = dfb = 0.0
    for u in opt.units:
        linear = u.grams * f
        d = whole_units(linear, u.unit_g) - linear
        dk += d * u.kcal
        dp += d * u.protein
        df += d * u.fat
        dc += d * u.carb
        dfb += d * u.fibre
    return dk, dp, df, dc, dfb


def _pref_dev(m: int, prefer: float) -> int:
    """How far a multiplier is from the one asked for, relative to it and squared: moving every
    plate by the same share costs the same, so a day's change is spread over its plates rather
    than taken out of one."""
    want = max(prefer, 0.05) * M
    return round(((m - want) / want) ** 2 * 1_000_000)


def _table(opt: SlotOption, multipliers: Sequence[int], prefer: float | None = None) -> list[_Row]:
    k, p, f, fb, c = (
        _tenths(opt.kcal),
        _tenths(opt.protein_g),
        _tenths(opt.fat_g),
        _tenths(opt.fibre_g),
        _tenths(opt.carb_g),
    )
    rows: list[_Row] = []
    for m in multipliers:
        dk, dp, df, dc, dfb = _unit_delta(opt, m / M) if opt.units else (0.0,) * 5
        dev = abs(m - M) if prefer is None else _pref_dev(m, prefer)
        rows.append(
            (
                m,
                k * m + round(dk * T * M),
                p * m + round(dp * T * M),
                f * m + round(df * T * M),
                fb * m + round(dfb * T * M),
                c * m + round(dc * T * M),
                dev,
            )
        )
    return rows


def _g(v: int) -> str:
    return f"{v / (T * M):.0f}"


def _search(
    tables: Sequence[Sequence[_Row]],
    lower: int,
    upper: int,
    p_min: int,
    f_min: int,
    fb_min: int,
    t_kcal: int,
    *,
    c_min: int = 0,
    floors: bool = True,
    dev_first: bool = False,
) -> tuple[int, ...] | None:
    """Best multiplier tuple, pruning by the kcal window on partial sums.

    Ranked by least shortfall against the floors (none, while `floors` holds them as limits),
    then by default closest to the kcal target and least moved from one serving - or, with
    `dev_first`, least moved from what was asked for and then closest to the target - and the
    smaller tuple breaks a last tie, so the answer is a function of the inputs alone.
    """
    n = len(tables)
    sufmin = [0] * (n + 1)
    sufmax = [0] * (n + 1)
    for i in range(n - 1, -1, -1):
        ks = [row[1] for row in tables[i]]
        sufmin[i] = sufmin[i + 1] + min(ks)
        sufmax[i] = sufmax[i + 1] + max(ks)
    best_key: tuple[int, int, int] | None = None
    best_ms: tuple[int, ...] | None = None
    last = tables[n - 1]

    def rec(
        i: int, k: int, p: int, f: int, fb: int, c: int, dev: int, chosen: tuple[int, ...]
    ) -> None:
        nonlocal best_key, best_ms
        if i == n - 1:
            for m, ki, pi, fi, fbi, ci, di in last:
                kk = k + ki
                if kk < lower or kk > upper:
                    continue
                pp, ff, bb, cc = p + pi, f + fi, fb + fbi, c + ci
                if floors:
                    if pp < p_min or ff < f_min or bb < fb_min or cc < c_min:
                        continue
                    short = 0
                else:
                    short = (
                        max(0, p_min - pp)
                        + max(0, f_min - ff)
                        + max(0, fb_min - bb)
                        + max(0, c_min - cc)
                    )
                kd = abs(kk - t_kcal)
                sd = dev + di
                key = (short, sd, kd) if dev_first else (short, kd, sd)
                cand = (*chosen, m)
                if (
                    best_key is None
                    or key < best_key
                    or (key == best_key and best_ms is not None and cand < best_ms)
                ):
                    best_key, best_ms = key, cand
            return
        for m, ki, pi, fi, fbi, ci, di in tables[i]:
            kk = k + ki
            if kk + sufmax[i + 1] < lower or kk + sufmin[i + 1] > upper:
                continue
            rec(i + 1, kk, p + pi, f + fi, fb + fbi, c + ci, dev + di, (*chosen, m))

    rec(0, 0, 0, 0, 0, 0, 0, ())
    return best_ms


def _window_maxima(
    tables: Sequence[Sequence[_Row]], lower: int, upper: int
) -> tuple[bool, int, int, int, int]:
    """(any combination inside the kcal window, max protein, max fat, max fibre and max carb
    inside it)."""
    n = len(tables)
    found = False
    wp = wf = wfb = wc = -1

    def rec(i: int, k: int, p: int, f: int, fb: int, c: int) -> None:
        nonlocal found, wp, wf, wfb, wc
        if i == n:
            if lower <= k <= upper:
                found = True
                wp, wf, wfb, wc = max(wp, p), max(wf, f), max(wfb, fb), max(wc, c)
            return
        for _m, ki, pi, fi, fbi, ci, _di in tables[i]:
            rec(i + 1, k + ki, p + pi, f + fi, fb + fbi, c + ci)

    rec(0, 0, 0, 0, 0, 0)
    return found, wp, wf, wfb, wc


def _diagnose(
    tables: Sequence[Sequence[_Row]],
    lower: int,
    upper: int,
    p_min: int,
    f_min: int,
    fb_min: int,
    target: Macros,
    tol: float,
    c_min: int = 0,
) -> str:
    pmax = sum(max(r[2] for r in t) for t in tables)
    fmax = sum(max(r[3] for r in t) for t in tables)
    fbmax = sum(max(r[4] for r in t) for t in tables)
    cmax = sum(max(r[5] for r in t) for t in tables)
    kmin = sum(min(r[1] for r in t) for t in tables)
    kmax = sum(max(r[1] for r in t) for t in tables)
    if pmax < p_min:
        return f"protein: max reachable {_g(pmax)} g < {_g(p_min)} g"
    if fmax < f_min:
        return f"fat: max reachable {_g(fmax)} g < floor {_g(f_min)} g"
    if fbmax < fb_min:
        return f"fibre: max reachable {_g(fbmax)} g < {_g(fb_min)} g"
    if c_min and cmax < c_min:
        return f"carbs: max reachable {_g(cmax)} g < {_g(c_min)} g"
    if kmin > upper:
        return f"kcal: min reachable {_g(kmin)} > {target.kcal} +{tol:g}%"
    if kmax < lower:
        return f"kcal: max reachable {_g(kmax)} < {target.kcal} -{tol:g}%"
    found, wp, wf, wfb, wc = _window_maxima(tables, lower, upper)
    within = f"within ±{tol:g}% kcal"
    if not found:
        return f"kcal: no combination lands {within} of {target.kcal}"
    if wp < p_min:
        return f"protein: max reachable {_g(wp)} g < {_g(p_min)} g {within}"
    if wf < f_min:
        return f"fat: max reachable {_g(wf)} g < floor {_g(f_min)} g {within}"
    if wfb < fb_min:
        return f"fibre: max reachable {_g(wfb)} g < {_g(fb_min)} g {within}"
    if c_min and wc < c_min:
        return f"carbs: max reachable {_g(wc)} g < {_g(c_min)} g {within}"
    if c_min:
        return f"protein, fat, fibre and carbs are each reachable {within} but not together"
    return f"protein, fat and fibre are each reachable {within} but not together"


# ---------------------------------------------------------------- one day
def solve_day(
    slots: Sequence[SlotOption],
    target: Macros,
    *,
    lo: float = DEFAULT_LO,
    hi: float = DEFAULT_HI,
    step: float = DEFAULT_STEP,
    kcal_tol_pct: float = DEFAULT_KCAL_TOL_PCT,
    fat_floor_g: float,
    fibre_min: float = DEFAULT_FIBRE_MIN,
    fixed: Mapping[str, float] | None = None,
    prefer: Mapping[str, float] | None = None,
    near: float | None = None,
    best_effort: bool = False,
    carb_min: float = 0.0,
) -> DaySolution | Infeasible:
    """Grid-search the slots' multipliers; `fixed` pins a slot at an exact multiplier.

    `prefer` names the multiplier wanted per slot and ranks by closeness to it before closeness
    to the kcal target (a person resizing their own day). `near`, with `prefer`, keeps each such
    slot within that share of it (0.1: within 10%), so a floor is met from the room the ask
    leaves and never by turning the ask round. `best_effort` answers even when the floors cannot
    all be met - least shortfall first - and says what fell short. `carb_min` is a carbohydrate
    floor in grams; none unless asked for.
    """
    if not slots:
        return Infeasible("no slots")
    names = [s.slot for s in slots]
    if len(set(names)) != len(names):
        raise ValueError(f"duplicate slot names: {names}")
    fixed = dict(fixed or {})
    unknown = set(fixed) - set(names)
    if unknown:
        raise ValueError(f"fixed multipliers for unknown slots: {sorted(unknown)}")
    free = grid(lo, hi, step)

    def allowed(s: SlotOption) -> list[int]:
        if s.slot in fixed:
            return [round(fixed[s.slot] * M)]
        want = None if prefer is None else prefer.get(s.slot)
        if want is None or near is None:
            return list(free)
        band = want * near
        close = [m for m in free if abs(m / M - want) <= band + 1e-9]
        # a band narrower than the grid's step still holds the grid point nearest the ask
        return close or [min(free, key=lambda m: abs(m / M - want))]

    tables = [_table(s, allowed(s), None if prefer is None else prefer.get(s.slot)) for s in slots]
    t_kcal = target.kcal * T * M
    bound = math.floor(t_kcal * kcal_tol_pct / 100 + 1e-9)
    lower, upper = t_kcal - bound, t_kcal + bound
    p_min = round(target.protein_g * T * M)
    f_min = round(fat_floor_g * T * M)
    fb_min = round(fibre_min * T * M)
    c_min = round(carb_min * T * M)
    dev_first = prefer is not None
    best = _search(
        tables, lower, upper, p_min, f_min, fb_min, t_kcal, c_min=c_min, dev_first=dev_first
    )
    if best is None:
        # half a gram under a floor is rounding, not a miss: "fibre 25 g < 25 g" refused a whole
        # meets the floors exactly - a day that can is never planned under them
        slack = round(FLOOR_SLACK_G * T * M)
        best = _search(
            tables,
            lower,
            upper,
            max(0, p_min - slack),
            max(0, f_min - slack),
            max(0, fb_min - slack),
            t_kcal,
            c_min=max(0, c_min - slack) if c_min else 0,
            dev_first=dev_first,
        )
    if best is None and best_effort:
        best = _search(
            tables,
            lower,
            upper,
            p_min,
            f_min,
            fb_min,
            t_kcal,
            c_min=c_min,
            floors=False,
            dev_first=dev_first,
        )
    if best is None:
        return Infeasible(
            _diagnose(tables, lower, upper, p_min, f_min, fb_min, target, kcal_tol_pct, c_min)
        )
    sol = _solution(slots, best, target)
    short = tuple(
        (name, got, need)
        for name, got, need in (
            ("protein", sol.macros.protein_g, round(target.protein_g)),
            ("fat", sol.macros.fat_g, round(fat_floor_g)),
            ("fibre", sol.macros.fibre_g, round(fibre_min)),
            *([("carbs", sol.macros.carb_g, round(carb_min))] if carb_min > 0 else []),
        )
        if got < need
    )
    return dataclasses.replace(sol, shortfall=short) if short else sol


def _solution(slots: Sequence[SlotOption], ms: Sequence[int], target: Macros) -> DaySolution:
    kcal = protein = fat = carb = fibre = 0.0
    multipliers: dict[str, float] = {}
    for s, m in zip(slots, ms, strict=True):
        f = m / M
        multipliers[s.slot] = f
        dk, dp, df, dc, dfb = _unit_delta(s, f) if s.units else (0.0,) * 5
        kcal += s.kcal * f + dk
        protein += s.protein_g * f + dp
        fat += s.fat_g * f + df
        carb += s.carb_g * f + dc
        fibre += s.fibre_g * f + dfb
    macros = Macros(
        math.floor(kcal + 0.5),
        math.floor(protein + 0.5),
        math.floor(fat + 0.5),
        math.floor(carb + 0.5),
        math.floor(fibre + 0.5),
    )
    return DaySolution(
        multipliers=multipliers,
        macros=macros,
        kcal_dev=abs(macros.kcal - target.kcal),
        sum_dev=round(sum(abs(f - 1.0) for f in multipliers.values()), 4),
    )


# ---------------------------------------------------------------- a segment
def _ceil_packs(need_g: float, pack_g: float) -> int:
    need, pack = _tenths(need_g), _tenths(pack_g)
    if need <= 0:
        return 0
    return -(-need // pack)


def solve_segment(
    days: Sequence[DayProblem],
    *,
    whole_pack: Sequence[PackRule] = (),
    leftovers: Mapping[str, float] | None = None,
    pre_bed_protein: bool = False,
    fixed: Mapping[str, float] | None = None,
    lo: float = DEFAULT_LO,
    hi: float = DEFAULT_HI,
    step: float = DEFAULT_STEP,
    kcal_tol_pct: float = DEFAULT_KCAL_TOL_PCT,
    fat_floor_g: float,
    fibre_min: float = DEFAULT_FIBRE_MIN,
    carb_min: float = 0.0,
) -> SegmentSolution | Infeasible:
    """Solve each day, then pin every `whole_pack` ingredient to credit + whole packs.

    With pre_bed_protein the pre_bed slot is fixed at x1.0 on every day (the profile wants that
    portion as written). A pinned ingredient scales the multipliers of the unfixed slots that use
    it by (total - fixed-slot grams) / free grams, rounded down to the grid resolution so the cook
    never exceeds the packs bought; each touched day is re-solved with those slots fixed. Whole
    packs are tried rounding up first, then one pack fewer.

    That scale is capped at 1.0: the packs bought are a ceiling on what can be cooked and never
    an obligation to cook it. Using 200 g of a 500 g pack and freezing the rest is an ordinary
    week, and refusing to plan one rather than leave 300 g in the freezer is not a trade anybody
    asked for.
    """
    if not days:
        return Infeasible("no days")
    fixed_by_day: list[dict[str, float]] = []
    solutions: list[DaySolution] = []
    for d in days:
        fx = dict(fixed or {})
        if pre_bed_protein and any(s.slot == PRE_BED for s in d.slots):
            fx[PRE_BED] = 1.0
        sol = solve_day(
            d.slots,
            d.target,
            lo=lo,
            hi=hi,
            step=step,
            kcal_tol_pct=kcal_tol_pct,
            fat_floor_g=fat_floor_g,
            fibre_min=fibre_min,
            fixed=fx,
            carb_min=carb_min,
        )
        if isinstance(sol, Infeasible):
            return Infeasible(f"{d.day.isoformat()}: {sol}")
        fixed_by_day.append(fx)
        solutions.append(sol)

    credits = dict(leftovers or {})
    pins: dict[str, Pin] = {}
    for rule in whole_pack:
        need_free = need_fixed = 0.0
        for di, d in enumerate(days):
            if rule.days is not None and d.day not in rule.days:
                continue
            for s in d.slots:
                g = s.grams.get(rule.code, 0.0) * solutions[di].multipliers[s.slot]
                if s.slot in fixed_by_day[di]:
                    need_fixed += g
                else:
                    need_free += g
        need = need_free + need_fixed
        if need <= 0:
            continue
        credit = max(0.0, float(credits.get(rule.code, 0.0)))
        packs_up = _ceil_packs(need - credit, rule.pack_g)
        candidates = [packs_up] + ([packs_up - 1] if packs_up >= 1 else [])
        if rule.fewer_first and len(candidates) == 2 and need_free > 0:
            less = credit + (packs_up - 1) * rule.pack_g
            if (less - need_fixed) / need_free >= FEWER_WITHIN:
                candidates.reverse()
        if need_free <= 0:
            pins[rule.code] = Pin(rule.code, need, credit, packs_up, need, 1.0)
            continue
        last_reason = ""
        pinned = False
        for packs in candidates:
            total = credit + packs * rule.pack_g
            if total <= need_fixed:
                continue
            # A pack is a CEILING, never a quota. What you buy bounds what you can cook; it
            # does not oblige you to cook it. Scaling portions UP to finish a pack pushed them
            # against a 500 g pack, that is 2.22x, the band stops at 1.6, and she got no menu
            # at all rather than a menu and 275 g in the freezer. It bites a smaller person
            # hardest, because UK pack sizes are the same whoever is eating.
            #
            # Scaling DOWN still earns its keep: buying one pack instead of two and eating 4%
            # smaller portions is a real saving, which is why `packs_up - 1` stays a candidate.
            scale = min(1.0, (total - need_fixed) / need_free)
            trial_fixed = [dict(fx) for fx in fixed_by_day]
            trial_solutions = list(solutions)
            ok = True
            for di, d in enumerate(days):
                if rule.days is not None and d.day not in rule.days:
                    continue
                touched = [
                    s.slot
                    for s in d.slots
                    if s.grams.get(rule.code, 0.0) > 0 and s.slot not in fixed_by_day[di]
                ]
                if not touched:
                    continue
                for slot in touched:
                    m = math.floor(solutions[di].multipliers[slot] * scale * M) / M
                    if not lo - 1e-9 <= m <= hi + 1e-9:
                        ok, last_reason = False, f"{slot} at x{m:.2f} outside {lo:g}..{hi:g}"
                        break
                    trial_fixed[di][slot] = m
                if not ok:
                    break
                sol = solve_day(
                    d.slots,
                    d.target,
                    lo=lo,
                    hi=hi,
                    step=step,
                    kcal_tol_pct=kcal_tol_pct,
                    fat_floor_g=fat_floor_g,
                    fibre_min=fibre_min,
                    fixed=trial_fixed[di],
                    carb_min=carb_min,
                )
                if isinstance(sol, Infeasible):
                    ok, last_reason = False, f"{d.day.isoformat()}: {sol}"
                    break
                trial_solutions[di] = sol
            if ok:
                fixed_by_day, solutions = trial_fixed, trial_solutions
                key = rule.code if rule.days is None else f"{rule.code}@{min(rule.days)}"
                pins[key] = Pin(rule.code, need, credit, packs, total, scale)
                pinned = True
                break
        if not pinned:
            return Infeasible(
                f"whole pack {rule.code}: {need:.0f} g needs {packs_up} x {rule.pack_g:g} g; "
                f"{last_reason}"
            )
    return SegmentSolution(days=solutions, pins=pins)
