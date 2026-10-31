"""Portion solver: constraints hold on every solution, infeasibility names the binding constraint,
the tie-break is deterministic, four slots solve well inside 0.5 s, whole packs pin a segment."""

from __future__ import annotations

import datetime as dt
import time

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tiffin.energy import Macros
from tiffin.errors import Infeasible
from tiffin.food import solver
from tiffin.food.solver import DayProblem, DaySolution, PackRule, SlotOption

FOUR = (
    SlotOption("breakfast", 520, 40, 14, 60, 8),
    SlotOption("lunch", 650, 55, 18, 70, 9),
    SlotOption("dinner", 700, 60, 22, 65, 10),
    SlotOption("snack", 300, 25, 8, 30, 4),
)
TARGET = Macros(2200, 180, 70, 220, 30)


def check(
    sol: DaySolution,
    slots,
    target: Macros,
    *,
    fat_floor: float,
    fibre_min: float,
    tol=5,
    slack: float = 0.0,
):
    kcal = sum(s.kcal * sol.multipliers[s.slot] for s in slots)
    protein = sum(s.protein_g * sol.multipliers[s.slot] for s in slots)
    fat = sum(s.fat_g * sol.multipliers[s.slot] for s in slots)
    fibre = sum(s.fibre_g * sol.multipliers[s.slot] for s in slots)
    grain = len(slots) / (2 * solver.T) + slack
    assert abs(kcal - target.kcal) <= target.kcal * tol / 100 + grain
    assert protein >= target.protein_g - grain
    assert fat >= fat_floor - grain
    assert fibre >= fibre_min - grain
    assert sol.macros.kcal == round(kcal) or abs(sol.macros.kcal - kcal) <= 0.5


def test_feasible_day_meets_every_constraint() -> None:
    sol = solver.solve_day(FOUR, TARGET, fat_floor_g=65)
    assert isinstance(sol, DaySolution)
    check(sol, FOUR, TARGET, fat_floor=65, fibre_min=25)
    assert list(sol.multipliers) == ["breakfast", "lunch", "dinner", "snack"]
    for m in sol.multipliers.values():
        assert 0.6 <= m <= 1.6 and abs(m * 100 - round(m * 100)) < 1e-9 and round(m * 100) % 5 == 0
    assert sol.kcal_dev == abs(sol.macros.kcal - TARGET.kcal)
    assert sol.sum_dev == round(sum(abs(m - 1) for m in sol.multipliers.values()), 4)


def test_objective_and_tie_break() -> None:
    two = (SlotOption("lunch", 500, 40, 20, 50, 15), SlotOption("dinner", 500, 40, 20, 50, 15))
    sol = solver.solve_day(two, Macros(1000, 60, 30, 100, 25), fat_floor_g=30)
    assert isinstance(sol, DaySolution)
    # (1.0, 1.0), (0.95, 1.05), (0.9, 1.1)... all land exactly on 1000 kcal; sum|m-1| picks 1.0/1.0
    assert sol.multipliers == {"lunch": 1.0, "dinner": 1.0} and sol.kcal_dev == 0
    # target off-grid: nearest kcal wins over multiplier closeness
    sol = solver.solve_day(two, Macros(1050, 60, 30, 100, 25), fat_floor_g=30)
    assert isinstance(sol, DaySolution)
    assert sol.kcal_dev == 0 and sol.sum_dev == 0.1
    assert sol.multipliers == {"lunch": 1.0, "dinner": 1.1}  # lexicographically smaller tuple
    again = solver.solve_day(two, Macros(1050, 60, 30, 100, 25), fat_floor_g=30)
    assert again == sol


def test_infeasible_names_the_binding_constraint() -> None:
    r = solver.solve_day(FOUR, Macros(2200, 320, 70, 220, 30), fat_floor_g=65)
    assert isinstance(r, Infeasible) and str(r) == "protein: max reachable 288 g < 320 g"
    r = solver.solve_day(FOUR, Macros(1200, 100, 70, 220, 30), fat_floor_g=65)
    assert isinstance(r, Infeasible) and str(r) == "kcal: min reachable 1302 > 1200 +5%"
    r = solver.solve_day(FOUR, Macros(4000, 100, 70, 220, 30), fat_floor_g=65)
    assert isinstance(r, Infeasible) and str(r) == "kcal: max reachable 3472 < 4000 -5%"
    r = solver.solve_day(FOUR, Macros(2200, 100, 70, 220, 30), fat_floor_g=120)
    assert isinstance(r, Infeasible) and str(r).startswith("fat: max reachable 99 g < floor 120 g")
    r = solver.solve_day(FOUR, Macros(2200, 100, 70, 220, 30), fat_floor_g=65, fibre_min=60)
    assert isinstance(r, Infeasible) and str(r).startswith("fibre: max reachable 50 g < 60 g")
    # reachable in absolute terms, not inside the kcal window
    r = solver.solve_day(FOUR, Macros(2200, 200, 70, 220, 30), fat_floor_g=65)
    assert isinstance(r, Infeasible)
    assert str(r) == "protein: max reachable 194 g < 200 g within ±5% kcal"
    assert solver.solve_day((), TARGET, fat_floor_g=65) == Infeasible("no slots") or isinstance(
        solver.solve_day((), TARGET, fat_floor_g=65), Infeasible
    )


def test_bad_inputs_raise() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        solver.solve_day((FOUR[0], FOUR[0]), TARGET, fat_floor_g=65)
    with pytest.raises(ValueError, match="unknown slots"):
        solver.solve_day(FOUR, TARGET, fat_floor_g=65, fixed={"tea": 1.0})
    with pytest.raises(ValueError, match="bad grid"):
        solver.solve_day(FOUR, TARGET, fat_floor_g=65, lo=1.0, hi=0.5)


def test_four_slots_under_half_a_second() -> None:
    # 50% tolerance keeps the kcal window wide, so pruning barely helps: near-full enumeration
    t0 = time.perf_counter()
    sol = solver.solve_day(FOUR, Macros(2400, 100, 40, 220, 20), fat_floor_g=40, kcal_tol_pct=50)
    wide = time.perf_counter() - t0
    assert isinstance(sol, DaySolution)
    t0 = time.perf_counter()
    solver.solve_day(FOUR, TARGET, fat_floor_g=65)
    narrow = time.perf_counter() - t0
    t0 = time.perf_counter()
    solver.solve_day(FOUR, Macros(2200, 200, 70, 220, 30), fat_floor_g=65)  # infeasible + diagnosis
    infeasible = time.perf_counter() - t0
    assert wide < 0.5 and narrow < 0.5 and infeasible < 0.5, (wide, narrow, infeasible)
    assert len(solver.grid(0.6, 1.6, 0.05)) == 21 and 21**4 == 194_481


def test_fixed_multiplier_and_pre_bed_rule() -> None:
    five = (*FOUR, SlotOption("pre_bed", 200, 30, 5, 10, 1))
    sol = solver.solve_day(
        five, Macros(2400, 200, 70, 220, 30), fat_floor_g=65, fixed={"pre_bed": 1.0}
    )
    assert isinstance(sol, DaySolution) and sol.multipliers["pre_bed"] == 1.0
    sol = solver.solve_day(
        five, Macros(2400, 200, 70, 220, 30), fat_floor_g=65, fixed={"pre_bed": 1.234}
    )
    assert isinstance(sol, DaySolution) and sol.multipliers["pre_bed"] == 1.234
    days = [
        DayProblem(dt.date(2026, 9, 7) + dt.timedelta(days=i), five, Macros(2400, 200, 70, 220, 30))
        for i in range(3)
    ]
    seg = solver.solve_segment(days, pre_bed_protein=True, fat_floor_g=65)
    assert isinstance(seg, solver.SegmentSolution)
    assert all(d.multipliers["pre_bed"] == 1.0 for d in seg.days)
    # Without the rule the slot is free, not forced: the tie-break still lands on 1.0 here,
    # so the proof that it is unpinned is that an explicit override moves it.
    seg = solver.solve_segment(days, pre_bed_protein=False, fat_floor_g=65)
    assert isinstance(seg, solver.SegmentSolution)
    assert all(0.6 <= d.multipliers["pre_bed"] <= 1.6 for d in seg.days)
    moved = solver.solve_segment(
        days, pre_bed_protein=False, fixed={"pre_bed": 0.8}, fat_floor_g=65
    )
    assert isinstance(moved, solver.SegmentSolution)
    assert all(d.multipliers["pre_bed"] == 0.8 for d in moved.days)


def _mince_days(n: int, grams_per_portion: float = 100.0) -> list[DayProblem]:
    dinner = SlotOption(
        "dinner",
        700,
        60,
        22,
        65,
        10,
        recipe_id=7,
        grams={"beef_mince_5": grams_per_portion, "rice": 80},
    )
    slots = (FOUR[0], FOUR[1], dinner, FOUR[3])
    return [DayProblem(dt.date(2026, 9, 7) + dt.timedelta(days=i), slots, TARGET) for i in range(n)]


def test_whole_pack_pins_the_segment() -> None:
    days = _mince_days(5)
    free = solver.solve_segment(days, fat_floor_g=65)
    assert isinstance(free, solver.SegmentSolution)
    need = sum(100.0 * d.multipliers["dinner"] for d in free.days)
    pinned = solver.solve_segment(days, whole_pack=[PackRule("beef_mince_5", 500)], fat_floor_g=65)
    assert isinstance(pinned, solver.SegmentSolution)
    pin = pinned.pins["beef_mince_5"]
    assert pin.need_g == pytest.approx(need) and pin.credit_g == 0
    assert pin.total_g == pin.packs * 500 and pin.packs in (
        -(-int(need) // 500),
        -(-int(need) // 500) - 1,
    )
    cooked = sum(100.0 * d.multipliers["dinner"] for d in pinned.days)
    # the packs bought are a ceiling on what can be cooked, not a quota that must be finished
    assert cooked <= pin.total_g + 1e-6
    assert pin.scale <= 1.0 + 1e-9, "portions are never scaled UP to use a pack"
    for d in pinned.days:
        check(d, days[0].slots, TARGET, fat_floor=65, fibre_min=25)
    # leftover credit reduces what is bought; the credit is cooked too
    credited = solver.solve_segment(
        days,
        whole_pack=[PackRule("beef_mince_5", 500)],
        leftovers={"beef_mince_5": 200},
        fat_floor_g=65,
    )
    assert isinstance(credited, solver.SegmentSolution)
    pc = credited.pins["beef_mince_5"]
    assert pc.credit_g == 200 and pc.total_g == 200 + pc.packs * 500
    # an ingredient the segment does not use is simply not pinned
    none = solver.solve_segment(days, whole_pack=[PackRule("salmon", 240)], fat_floor_g=65)
    assert isinstance(none, solver.SegmentSolution) and none.pins == {}


def test_a_pack_far_bigger_than_the_need_is_bought_and_not_finished() -> None:
    """The partner needs 225 g of mince against a 500 g pack. Scaling her portions up to finish it
    is
    2.22x, the band stops at 1.6, and she got no menu at all rather than a menu and 275 g in the
    freezer. UK pack sizes are the same whoever is eating, so this bites a smaller person first.
    """
    days = _mince_days(1, grams_per_portion=20.0)  # 20 g against a 500 g pack
    r = solver.solve_segment(days, whole_pack=[PackRule("beef_mince_5", 500)], fat_floor_g=65)
    assert isinstance(r, solver.SegmentSolution), "a big pack is a full freezer, not a failure"
    pin = r.pins["beef_mince_5"]
    assert pin.packs == 1 and pin.total_g == 500
    assert pin.scale == pytest.approx(1.0), "the portions are the ones that were planned"
    cooked = sum(100.0 * d.multipliers["dinner"] for d in r.days)
    assert cooked < pin.total_g, "and most of the pack is left over"


def test_buying_one_pack_fewer_may_still_shrink_the_portions() -> None:
    """Scaling DOWN still earns its keep: one pack instead of two, eaten slightly smaller, is a
    real saving. Only scaling UP was ever the problem."""
    days = _mince_days(5)
    r = solver.solve_segment(days, whole_pack=[PackRule("beef_mince_5", 500)], fat_floor_g=65)
    assert isinstance(r, solver.SegmentSolution)
    assert r.pins["beef_mince_5"].scale <= 1.0 + 1e-9


def test_whole_pack_infeasible_says_why() -> None:
    r = solver.solve_segment([], fat_floor_g=65)
    assert isinstance(r, Infeasible)
    r = solver.solve_segment(_mince_days(2), fat_floor_g=200)
    assert isinstance(r, Infeasible) and str(r).startswith("2026-09-07: fat:")


slot_st = st.builds(
    SlotOption,
    slot=st.just("x"),
    kcal=st.floats(100, 900),
    protein_g=st.floats(0, 80),
    fat_g=st.floats(0, 40),
    carb_g=st.floats(0, 120),
    fibre_g=st.floats(0, 15),
)


@settings(max_examples=40, deadline=None)
@given(
    st.lists(slot_st, min_size=1, max_size=3),
    st.integers(800, 3500),
    st.integers(40, 220),
    st.integers(20, 90),
)
def test_solution_or_reason(slots: list[SlotOption], kcal: int, protein: int, fat: int) -> None:
    named = tuple(
        SlotOption(f"s{i}", s.kcal, s.protein_g, s.fat_g, s.carb_g, s.fibre_g)
        for i, s in enumerate(slots)
    )
    target = Macros(kcal, protein, fat, 100, 25)
    r = solver.solve_day(named, target, fat_floor_g=fat)
    if isinstance(r, Infeasible):
        reason = str(r)
        assert reason.split(":")[0] in {"protein", "fat", "fibre", "kcal"} or "together" in reason  # noqa: PLC0207
    else:
        # random days: some meet a floor only within the half-gram fallback
        check(r, named, target, fat_floor=fat, fibre_min=25, slack=solver.FLOOR_SLACK_G)


def test_half_a_gram_under_a_floor_is_rounding_not_a_miss() -> None:
    """A rounding gap below half a gram does not count as a nutrient miss."""

    def day(fibre: float):
        one = (SlotOption("dinner", 2200, 180, 70, 220, fibre),)
        return solver.solve_day(one, TARGET, fat_floor_g=65, fibre_min=25, fixed={"dinner": 1.0})

    assert not isinstance(day(24.7), Infeasible)
    assert isinstance(day(24.3), Infeasible)
    # a day that can meet a floor exactly is never planned under it
    sol = solver.solve_day(FOUR, TARGET, fat_floor_g=65)
    assert not isinstance(sol, Infeasible) and sol.macros.fat_g >= 65 - 0.2


def test_a_pack_rule_can_round_one_shops_days_alone() -> None:
    """Someone whose week is bought in two shops buys packs per shop (food/weekplan.py)."""
    mince = (
        SlotOption("breakfast", 520, 40, 14, 60, 8),
        SlotOption("lunch", 650, 55, 18, 70, 9, grams={"mince": 120.0}),
        SlotOption("dinner", 700, 60, 22, 65, 10, grams={"mince": 130.0}),
        SlotOption("snack", 300, 25, 8, 30, 4),
    )
    mon = dt.date(2026, 9, 14)
    days = [DayProblem(mon + dt.timedelta(days=i), mince, TARGET) for i in range(3)]
    first, last = frozenset(d.day for d in days[:2]), frozenset({days[2].day})
    rules = [
        PackRule("mince", 500.0, fewer_first=True, days=first),
        PackRule("mince", 500.0, fewer_first=True, days=last),
    ]
    got = solver.solve_segment(days, whole_pack=rules, fat_floor_g=65)
    assert not isinstance(got, Infeasible), got
    pins = got.pins
    assert set(pins) == {f"mince@{min(first)}", f"mince@{days[2].day}"}
    # each shop's need is its own days' only
    assert pins[f"mince@{days[2].day}"].need_g < pins[f"mince@{min(first)}"].need_g
