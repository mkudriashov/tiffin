"""Advisor observations and automatic adjustments from weekly facts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .. import energy
from ..guardrails import MAX_LOSS_KG_WEEK
from .facts import Facts

CYCLE_GUARD_PHASES = ("luteal", "menstrual")


@dataclass
class Candidate:
    kind: str  # targets | menu_bias | note | pace_note
    payload: dict[str, Any]
    auto_ok: bool
    reason: str
    rule: str
    priority: int = 5

    def as_row(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "payload": self.payload,
            "reason": self.reason,
            "rule": self.rule,
        }


def _bw_pct_loss(delta_kg: float | None, weight_kg: float | None) -> float | None:
    if delta_kg is None or not weight_kg:
        return None
    return -delta_kg / weight_kg * 100.0


def candidates(facts: Facts, *, max_loss: float = MAX_LOSS_KG_WEEK) -> list[Candidate]:
    out: list[Candidate] = []
    t = facts.targets
    w = facts.weight_kg
    guard = facts.cycle_phase in CYCLE_GUARD_PHASES

    # R9 cycle guard: no calorie change from a luteal/menstrual week; say so
    if guard:
        out.append(
            Candidate(
                "note",
                {"cycle_phase": facts.cycle_phase},
                True,
                f"{facts.cycle_phase} phase this week: weight noise expected, no calorie change from it",
                "R9",
                9,
            )
        )

    # R1 fast loss over two weeks -> +150 kcal, auto (guardrails apply it regardless)
    if not guard and facts.trend_delta_14 is not None and t is not None:
        per_week = -facts.trend_delta_14 / 2.0
        if per_week > max_loss:
            out.append(
                Candidate(
                    "targets",
                    {"kcal_delta": 150, "protein_delta": 0},
                    True,
                    f"losing {per_week:.2f} kg/week over two weeks, above the {max_loss:g} cap: +150 kcal",
                    "R1",
                    1,
                )
            )

    # R2 slow loss two weeks running with enough data and a trusted TDEE -> -150 auto
    if (
        not guard
        and t is not None
        and facts.trend_delta_7 is not None
        and facts.prev_week_delta_7 is not None
    ):
        this_pct = _bw_pct_loss(facts.trend_delta_7, w)
        prev_pct = _bw_pct_loss(facts.prev_week_delta_7, w)
        trusted = facts.calibration_count >= 2
        if (
            this_pct is not None
            and prev_pct is not None
            and this_pct < 0.5
            and prev_pct < 0.5
            and facts.usable_days >= 5
            and facts.prev_week_usable_days >= 5
            and trusted
        ):
            out.append(
                Candidate(
                    "targets",
                    {"kcal_delta": -150, "protein_delta": 0},
                    True,
                    f"under 0.5 %/week for two weeks with {facts.usable_days}+{facts.prev_week_usable_days} usable days: -150 kcal",
                    "R2",
                    2,
                )
            )

    # R5 / R6 menu biases (auto, feed the next menu)
    if facts.protein_short_days >= 3:
        out.append(
            Candidate(
                "menu_bias",
                {"bias": "protein"},
                True,
                f"protein under target on {facts.protein_short_days} days: next menu leans on protein",
                "R5",
                6,
            )
        )
    if facts.fibre_short_days >= 3:
        out.append(
            Candidate(
                "menu_bias",
                {"bias": "fibre"},
                True,
                f"fibre under 25 g on {facts.fibre_short_days} days: next menu adds fibre",
                "R6",
                6,
            )
        )

    # R8 muscle proxy from the tape (needs >= 2 rows)
    if (
        facts.tape_rows_28 >= 2
        and facts.waist_delta_28 is not None
        and facts.trend_delta_28 is not None
    ):
        weight_flat = abs(facts.trend_delta_28) < 0.3
        waist_down = facts.waist_delta_28 <= -1.0
        if waist_down and weight_flat:
            out.append(
                Candidate(
                    "note",
                    {"proxy": "recomposition"},
                    True,
                    f"waist {facts.waist_delta_28:+.1f} cm in 4 weeks with weight flat: recomposition, keep going",
                    "R8",
                    7,
                )
            )
    # R10 pace against the goal
    prof = facts.profile
    if (
        prof is not None
        and prof["goal_weight_kg"]
        and prof["goal_date"]
        and facts.trend_now is not None
    ):
        from ..clock import parse_day  # noqa: PLC0415

        verdict = energy.pace_check(
            facts.trend_now,
            float(prof["goal_weight_kg"]),
            parse_day(str(prof["goal_date"])),
            facts.week_end,
            max_loss,
        )
        if not verdict.feasible:
            # A goal date today or already past divides by zero weeks, and the message read
            # "goal needs inf kg/week". Say the plain thing instead: the date has arrived.
            if verdict.weeks_left <= 0:
                to_lose = facts.trend_now - float(prof["goal_weight_kg"])
                reason = (
                    f"the goal date has passed with {to_lose:.1f} kg still to go; "
                    f"at the {max_loss:g} kg cap that is around {verdict.projected_date}"
                )
            else:
                reason = (
                    f"goal needs {verdict.required_kg_week:.2f} kg/week, above the "
                    f"{max_loss:g} cap; at the cap you reach it around {verdict.projected_date}"
                )
            out.append(
                Candidate(
                    "pace_note",
                    {
                        "required": verdict.required_kg_week,
                        "projected": str(verdict.projected_date),
                        "weeks_left": verdict.weeks_left,
                    },
                    True,
                    reason,
                    "R10",
                    8,
                )
            )

    out.sort(key=lambda c: c.priority)
    return out
