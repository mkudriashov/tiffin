"""Energy model: pure arithmetic, no DB, no model. Every number Tiffin states comes from here.

Mifflin-St Jeor -> TDEE formula start; EWMA weight trend; weekly TDEE recalibration from logged
intake vs the trend delta (trusted only after 2-3 weeks); deficit from a rate of loss capped
at max_loss_kg_week; macros protein-first, fat floor, carbs fill, fibre clamped.
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

KCAL_PER_KG_FAT = 7700.0
USABLE_STATUSES = ("confirmed", "estimate")


def round_int(x: float) -> int:
    """Half-up rounding for non-negative quantities (round() is banker's).

    Lives here rather than in food/recipes.py because a target and the recipe built to hit it
    have to round the same way: 142.5 g of carbs became 142 in the target and 143 in the dish.
    """
    return math.floor(x + 0.5)


@dataclass(frozen=True)
class Macros:
    kcal: int
    protein_g: int
    fat_g: int
    carb_g: int
    fibre_g: int

    def as_dict(self) -> dict[str, int]:
        return {
            "kcal": self.kcal,
            "protein_g": self.protein_g,
            "fat_g": self.fat_g,
            "carb_g": self.carb_g,
            "fibre_g": self.fibre_g,
        }


@dataclass(frozen=True)
class TrendPoint:
    day: dt.date
    trend_kg: float
    alpha: float
    n: int
    weigh_in_id: int | None = None


@dataclass(frozen=True)
class DayIntake:
    day: dt.date
    kcal: float
    status: str  # confirmed | estimate | unconfirmed | paused


@dataclass(frozen=True)
class Calibration:
    usable_days: int
    intake_kcal_mean: float
    trend_delta_kg: float
    tdee_measured: float
    tdee_blended: float
    confidence: str  # low | medium | high
    blend_k: float


@dataclass(frozen=True)
class Skip:
    reason: str
    usable_days: int


@dataclass(frozen=True)
class PaceVerdict:
    required_kg_week: float
    cap_kg_week: float
    feasible: bool
    weeks_left: float
    projected_date: dt.date | None  # at the cap, when infeasible


# ---------------------------------------------------------------- basal, formula
def age_on(birth_date: dt.date, day: dt.date) -> int:
    years = day.year - birth_date.year
    if (day.month, day.day) < (birth_date.month, birth_date.day):
        years -= 1
    return years


def bmr_mifflin(sex: str, weight_kg: float, height_cm: float, age_years: float) -> float:
    base = 10.0 * weight_kg + 6.25 * height_cm - 5.0 * age_years
    if sex == "m":
        return base + 5.0
    if sex == "f":
        return base - 161.0
    raise ValueError(f"sex must be 'm' or 'f', got {sex!r}")


def tdee_formula(bmr: float, activity_factor: float) -> float:
    if not 1.0 <= activity_factor <= 2.5:
        raise ValueError(f"activity factor out of range: {activity_factor}")
    return bmr * activity_factor


# ---------------------------------------------------------------- trend
def ewma_next(prev: float, x: float, alpha: float = 0.10) -> float:
    return prev + alpha * (x - prev)


def trend_series(
    weigh_ins: Sequence[tuple[dt.date, float, int | None]], alpha: float = 0.10
) -> list[TrendPoint]:
    """One point per weigh-in (sorted by day); gaps do not decay the trend. First point = raw."""
    out: list[TrendPoint] = []
    prev: float | None = None
    for day, kg, wid in sorted(weigh_ins, key=lambda t: t[0]):
        value = kg if prev is None else ewma_next(prev, kg, alpha)
        out.append(TrendPoint(day, round(value, 3), alpha, len(out) + 1, wid))
        prev = value
    return out


def trend_at(trend: Sequence[TrendPoint], day: dt.date) -> TrendPoint | None:
    best: TrendPoint | None = None
    for p in trend:
        if p.day <= day and (best is None or p.day > best.day):
            best = p
    return best


def trend_delta(trend: Sequence[TrendPoint], end: dt.date, days: int = 7) -> float | None:
    """trend(end) - trend(end - days), None when either side has no point yet."""
    if len(trend) < 2:
        return None
    now = trend_at(trend, end)
    then = trend_at(trend, end - dt.timedelta(days=days))
    if now is None or then is None or now.day == then.day:
        return None
    return round(now.trend_kg - then.trend_kg, 3)


def weight_slope(weigh_ins: Sequence[tuple[dt.date, float]]) -> float | None:
    """kg a week from a least-squares line through raw weigh-ins; None under two distinct days.

    The EWMA trend takes one step per weigh-in, not per day, so for somebody who weighs once a
    week a 28-day trend delta is four steps of a 0.1 filter and reports a fraction of the real
    loss for months. A straight line through the weigh-ins themselves has no such lag, and its
    run is the real time between them however they are spaced.
    """
    if len(weigh_ins) < 2:
        return None
    first = weigh_ins[0][0]
    xs = [(day - first).days for day, _kg in weigh_ins]
    ys = [kg for _day, kg in weigh_ins]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    return round(sxy / sxx * 7.0, 3)


# ---------------------------------------------------------------- calibration
def usable(days: Iterable[DayIntake]) -> list[DayIntake]:
    return [d for d in days if d.status in USABLE_STATUSES]


def confidence_for(prior_count: int) -> str:
    if prior_count < 2:
        return "low"
    if prior_count < 4:
        return "medium"
    return "high"


def calibrate_tdee(
    days: Iterable[DayIntake],
    delta_kg: float | None,
    prior_tdee: float,
    prior_count: int,
    *,
    min_usable: int = 5,
    kcal_per_kg: float = KCAL_PER_KG_FAT,
) -> Calibration | Skip:
    """Measured TDEE = mean intake - delta_kg * 7700 / 7, blended into the prior by k."""
    use = usable(days)
    if len(use) < min_usable:
        return Skip(f"usable_days={len(use)} < {min_usable}", len(use))
    if delta_kg is None:
        return Skip("no weight trend for the week", len(use))
    mean_intake = sum(d.kcal for d in use) / len(use)
    measured = mean_intake - delta_kg * kcal_per_kg / 7.0
    k = {0: 0.3, 1: 0.5}.get(prior_count, 0.7)
    blended = prior_tdee * (1.0 - k) + measured * k
    return Calibration(
        usable_days=len(use),
        intake_kcal_mean=round(mean_intake, 1),
        trend_delta_kg=round(delta_kg, 3),
        tdee_measured=round(measured, 1),
        tdee_blended=round(blended, 1),
        confidence=confidence_for(prior_count),
        blend_k=k,
    )


# ---------------------------------------------------------------- targets
def deficit_for_rate(
    weight_kg: float,
    rate_pct: float,
    max_loss_kg_week: float,
    *,
    kcal_per_kg: float = KCAL_PER_KG_FAT,
) -> float:
    """Daily kcal deficit for losing rate_pct of bodyweight per week, capped at max_loss_kg_week."""
    loss_per_week = min(weight_kg * rate_pct / 100.0, max_loss_kg_week)
    return loss_per_week * kcal_per_kg / 7.0


def macro_split(
    kcal: float,
    weight_kg: float,
    *,
    protein_g_per_kg: float = 2.0,
    fat_g_per_kg_min: float = 0.7,
    fibre_min: int = 25,
    fibre_max: int = 35,
) -> Macros:
    """Protein first, fat at its floor, carbs fill. Negative carbs raise kcal, never cut P or F."""
    protein = math.ceil(weight_kg * protein_g_per_kg)
    fat = math.ceil(weight_kg * fat_g_per_kg_min)
    carbs = (kcal - 4 * protein - 9 * fat) / 4.0
    if carbs < 0:
        carbs = 0.0
        kcal = 4 * protein + 9 * fat
    fibre = int(min(fibre_max, max(fibre_min, round_int(kcal / 1000 * 14))))  # 14 g/1000 kcal
    return Macros(round_int(kcal), protein, fat, round_int(carbs), fibre)


def weekly_targets(
    *,
    weight_kg: float,
    tdee: float,
    rate_pct: float,
    max_loss_kg_week: float,
    kcal_floor: int,
    protein_g_per_kg: float = 2.0,
    fat_g_per_kg_min: float = 0.7,
    fibre_min: int = 25,
    fibre_max: int = 35,
) -> Macros:
    kcal = max(float(kcal_floor), tdee - deficit_for_rate(weight_kg, rate_pct, max_loss_kg_week))
    return macro_split(
        kcal,
        weight_kg,
        protein_g_per_kg=protein_g_per_kg,
        fat_g_per_kg_min=fat_g_per_kg_min,
        fibre_min=fibre_min,
        fibre_max=fibre_max,
    )


def pace_check(
    trend_now: float, goal_kg: float, goal_date: dt.date, today: dt.date, max_loss: float
) -> PaceVerdict:
    """Is the goal still reachable at or under the cap? Projected date at the cap otherwise."""
    weeks_left = max((goal_date - today).days / 7.0, 0.0)
    to_lose = max(trend_now - goal_kg, 0.0)
    if to_lose == 0.0:
        return PaceVerdict(0.0, max_loss, True, weeks_left, None)
    required = math.inf if weeks_left == 0.0 else to_lose / weeks_left
    feasible = required <= max_loss + 1e-9
    projected = None
    if not feasible:
        projected = today + dt.timedelta(days=math.ceil(to_lose / max_loss * 7))
    return PaceVerdict(
        round(required, 3) if required != math.inf else required,
        max_loss,
        feasible,
        round(weeks_left, 2),
        projected,
    )
