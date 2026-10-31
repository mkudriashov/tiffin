"""Apply fixed safety limits to target adjustments before recording them."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .energy import Macros, round_int

AUTO_KCAL = 150
AUTO_PROTEIN_G = 10
RATE_PCT_MIN = 0.5
RATE_PCT_MAX = 1.0
MAX_LOSS_KG_WEEK = 0.8
MAINTENANCE_PROPOSE_WEEKS = 6
MAINTENANCE_STRONG_WEEKS = 8
MIN_USABLE_DAYS = 5


@dataclass(frozen=True)
class Applied:
    macros: Macros
    source: str  # formula | calibration | auto_adjust
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class NeedsProposal:
    macros: Macros
    reason: str
    notes: list[str] = field(default_factory=list)
    apply_now: Macros | None = None
    """What must land in the targets while the proposal waits for a tap.

    Rules 1-4 are not suggestions - the calorie floor, the protein and fat minima and the
    fast-loss correction are mandated, and a person cannot be left under them because some
    OTHER part of the week's change was large enough to need asking about. The caller writes
    this for the coming week instead of carrying the previous targets forward unchanged.
    """


def _refill_carbs(kcal: float, protein: int, fat: int, fibre: int) -> Macros:
    carbs = (kcal - 4 * protein - 9 * fat) / 4.0
    if carbs < 0:
        carbs = 0.0
        kcal = 4 * protein + 9 * fat
    return Macros(round_int(kcal), protein, fat, round_int(carbs), fibre)


def apply_guardrails(
    proposed: Macros,
    previous: Macros | None,
    *,
    weight_kg: float,
    kcal_floor: int,
    protein_g_per_kg: float = 2.0,
    fat_g_per_kg_min: float = 0.7,
    trend_delta_2w: float | None = None,
    max_loss_kg_week: float = MAX_LOSS_KG_WEEK,
    auto_kcal: int = AUTO_KCAL,
    auto_protein: int = AUTO_PROTEIN_G,
    source: str = "calibration",
) -> Applied | NeedsProposal:
    def mandated(base: Macros) -> tuple[Macros, list[str], int]:
        """Rules 1-4 applied to `base`: the floors, then the fast-loss bump.

        Returned separately from the bump so the caller can measure the DISCRETIONARY part of
        a week's change. Rule 4 is written down as always applied, and it was not: a
        calibration that moved the target in the same week could push the combined change past
        the auto bound, the whole verdict became a proposal, and the safety bump then waited
        for a tap while the person carried on losing weight too fast.
        """
        notes: list[str] = []
        kcal = float(base.kcal)
        protein, fat = base.protein_g, base.fat_g
        if kcal < kcal_floor:
            notes.append(f"raised to the {kcal_floor} kcal floor")
            kcal = float(kcal_floor)
        min_protein = math.ceil(weight_kg * protein_g_per_kg)
        if protein < min_protein:
            notes.append(f"protein raised to {min_protein} g ({protein_g_per_kg:g} g/kg)")
            protein = min_protein
        min_fat = math.ceil(weight_kg * fat_g_per_kg_min)
        if fat < min_fat:
            notes.append(f"fat raised to {min_fat} g ({fat_g_per_kg_min:g} g/kg)")
            fat = min_fat
        bump = 0
        if trend_delta_2w is not None and (-trend_delta_2w / 2.0) > max_loss_kg_week:
            notes.append(
                f"losing {-trend_delta_2w / 2.0:.2f} kg/week over two weeks, above the "
                f"{max_loss_kg_week:g} cap: +{auto_kcal} kcal"
            )
            bump = auto_kcal
            kcal += auto_kcal
        return _refill_carbs(kcal, protein, fat, base.fibre_g), notes, bump

    macros, notes, bump = mandated(proposed)
    if previous is None:
        return Applied(macros, "formula" if source == "formula" else source, notes)
    d_kcal = macros.kcal - previous.kcal - bump
    d_protein = macros.protein_g - previous.protein_g
    if abs(d_kcal) <= auto_kcal and abs(d_protein) <= auto_protein:
        moved = (macros.kcal - previous.kcal) or d_protein
        return Applied(macros, "auto_adjust" if moved else source, notes)
    reason = f"change of {d_kcal:+d} kcal / {d_protein:+d} g protein is outside the auto bounds"
    floor_macros, _floor_notes, _ = mandated(previous)
    return NeedsProposal(macros, reason, notes, apply_now=floor_macros)


def clamp_rate(rate_pct: float) -> float:
    return min(RATE_PCT_MAX, max(RATE_PCT_MIN, rate_pct))
