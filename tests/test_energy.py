"""Energy model and guardrails: values computed in-test, plus property checks with hypothesis."""

from __future__ import annotations

import datetime as dt
import math

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tiffin import energy as en
from tiffin.guardrails import Applied, NeedsProposal, apply_guardrails


def test_mifflin_values() -> None:
    # 100.9 kg, 183 cm, 36 y male: 10*100.9 + 6.25*183 - 5*36 + 5 = 1977.75
    assert en.bmr_mifflin("m", 100.9, 183, 36) == pytest.approx(1977.75)
    assert en.bmr_mifflin("f", 65.0, 168, 30) == pytest.approx(10 * 65 + 6.25 * 168 - 150 - 161)
    with pytest.raises(ValueError):
        en.bmr_mifflin("x", 70, 170, 30)
    assert en.tdee_formula(2000, 1.5) == 3000
    assert en.age_on(dt.date(1990, 9, 8), dt.date(2026, 9, 7)) == 35
    assert en.age_on(dt.date(1990, 9, 7), dt.date(2026, 9, 7)) == 36


def test_trend_series_and_delta() -> None:
    d0 = dt.date(2026, 9, 1)
    wi = [
        (d0 + dt.timedelta(days=i), w, i)
        for i, w in enumerate([101.0, 100.0, 102.0, 99.0, 100.5, 100.0, 99.5, 99.0])
    ]
    trend = en.trend_series(wi, alpha=0.1)
    assert trend[0].trend_kg == 101.0 and trend[0].n == 1
    assert trend[1].trend_kg == pytest.approx(100.9)
    assert all(99.0 <= p.trend_kg <= 102.0 for p in trend)
    assert en.trend_delta(trend, d0 + dt.timedelta(days=7), days=7) == pytest.approx(
        trend[-1].trend_kg - trend[0].trend_kg, abs=1e-3
    )
    assert en.trend_delta(trend[:1], d0, 7) is None
    assert en.trend_delta(trend, d0, 7) is None  # nothing 7 days earlier


def test_calibration_blend_and_skip() -> None:
    d0 = dt.date(2026, 9, 7)
    days = [en.DayIntake(d0 + dt.timedelta(days=i), 2200, "confirmed") for i in range(5)]
    days += [
        en.DayIntake(d0 + dt.timedelta(days=5), 2600, "estimate"),
        en.DayIntake(d0 + dt.timedelta(days=6), 0, "unconfirmed"),
    ]
    c = en.calibrate_tdee(days, delta_kg=-0.7, prior_tdee=3000, prior_count=0)
    assert isinstance(c, en.Calibration)
    assert c.usable_days == 6
    mean = (2200 * 5 + 2600) / 6
    measured = mean + 0.7 * 7700 / 7
    assert c.tdee_measured == pytest.approx(measured, abs=0.1)
    assert c.tdee_blended == pytest.approx(3000 * 0.7 + measured * 0.3, abs=0.1)
    assert c.confidence == "low" and c.blend_k == 0.3
    assert en.calibrate_tdee(days, -0.7, 3000, 1).blend_k == 0.5  # type: ignore[union-attr]
    later = en.calibrate_tdee(days, -0.7, 3000, 4)
    assert isinstance(later, en.Calibration) and later.confidence == "high" and later.blend_k == 0.7
    skip = en.calibrate_tdee(days[:3], -0.7, 3000, 0)
    assert isinstance(skip, en.Skip) and skip.reason == "usable_days=3 < 5"
    assert isinstance(en.calibrate_tdee(days, None, 3000, 0), en.Skip)


def test_deficit_cap_and_targets() -> None:
    # 100.9 kg at 0.7 %/week = 0.706 kg/week, under the 0.8 cap
    assert en.deficit_for_rate(100.9, 0.7, 0.8) == pytest.approx(0.7063 * 7700 / 7, rel=1e-3)
    # 120 kg at 1.0 %/week would be 1.2 kg/week -> capped at 0.8
    assert en.deficit_for_rate(120, 1.0, 0.8) == pytest.approx(0.8 * 7700 / 7)
    m = en.weekly_targets(
        weight_kg=100.9, tdee=3000, rate_pct=0.7, max_loss_kg_week=0.8, kcal_floor=1800
    )
    assert m.protein_g == 202 and m.fat_g == 71
    assert m.kcal == pytest.approx(3000 - 776.9, abs=1)
    assert m.carb_g == round((m.kcal - 4 * 202 - 9 * 71) / 4)
    assert 25 <= m.fibre_g <= 35
    floor = en.weekly_targets(
        weight_kg=100.9, tdee=2000, rate_pct=1.0, max_loss_kg_week=0.8, kcal_floor=1800
    )
    assert floor.kcal >= 1800


def test_a_person_can_choose_a_kilogram_a_week() -> None:
    at_default = en.deficit_for_rate(100.9, 1.0, 0.8)
    chosen = en.deficit_for_rate(100.9, 1.0, 1.0)
    assert at_default == pytest.approx(0.8 * 7700 / 7), "the old ceiling bound his fastest rate"
    assert chosen == pytest.approx(1.0 * 7700 / 7), "1 kg a week is 1100 kcal a day"
    assert chosen > at_default

    # and it reaches the targets, where the floor is still the thing that actually protects him
    fast = en.weekly_targets(
        weight_kg=100.9, tdee=2900, rate_pct=1.0, max_loss_kg_week=1.0, kcal_floor=1800
    )
    assert fast.kcal == pytest.approx(2900 - 1100, abs=1), fast
    assert fast.kcal >= 1800

    floored = en.weekly_targets(
        weight_kg=100.9, tdee=2600, rate_pct=1.0, max_loss_kg_week=1.0, kcal_floor=1800
    )
    assert floored.kcal == 1800, "the floor holds even at the ceiling; it is the real backstop"

    # a lighter person's rate never reaches the ceiling, so raising it changes nothing for her
    partners = en.deficit_for_rate(66.0, 1.0, 1.0)
    assert partners == pytest.approx(0.66 * 7700 / 7), "1 % of 66 kg is 0.66 kg, cap or no cap"


def test_macro_split_never_cuts_protein_or_fat() -> None:
    m = en.macro_split(1000, 100.0)  # far too low for P 200 + F 70 = 1430 kcal
    assert m.protein_g == 200 and m.fat_g == 70 and m.carb_g == 0 and m.kcal == 1430


def test_pace() -> None:
    v = en.pace_check(100.9, 87.0, dt.date(2027, 1, 24), dt.date(2026, 9, 6), 0.8)
    assert v.feasible and v.required_kg_week == pytest.approx(0.694, abs=0.005)
    bad = en.pace_check(100.9, 87.0, dt.date(2026, 11, 1), dt.date(2026, 9, 6), 0.8)
    assert not bad.feasible and bad.projected_date is not None
    assert bad.projected_date == dt.date(2026, 9, 6) + dt.timedelta(days=math.ceil(13.9 / 0.8 * 7))
    done = en.pace_check(86.0, 87.0, dt.date(2027, 1, 24), dt.date(2026, 9, 6), 0.8)
    assert done.feasible and done.required_kg_week == 0.0


@settings(max_examples=200, deadline=None)
@given(
    kcal=st.floats(1200, 4500),
    weight=st.floats(45, 160),
    p=st.floats(1.6, 2.4),
    f=st.floats(0.5, 1.0),
)
def test_macro_split_properties(kcal: float, weight: float, p: float, f: float) -> None:
    m = en.macro_split(kcal, weight, protein_g_per_kg=p, fat_g_per_kg_min=f)
    assert m.protein_g >= weight * p - 1e-9
    assert m.fat_g >= weight * f - 1e-9
    assert m.carb_g >= 0
    assert 25 <= m.fibre_g <= 35
    assert abs(m.kcal - (4 * m.protein_g + 9 * m.fat_g + 4 * m.carb_g)) <= 3


@settings(max_examples=200, deadline=None)
@given(st.lists(st.floats(40, 200), min_size=1, max_size=60))
def test_ewma_stays_within_input_range(weights: list[float]) -> None:
    d0 = dt.date(2026, 1, 1)
    trend = en.trend_series([(d0 + dt.timedelta(days=i), w, None) for i, w in enumerate(weights)])
    lo, hi = min(weights), max(weights)
    assert all(lo - 1e-3 <= p.trend_kg <= hi + 1e-3 for p in trend)  # rounded to 3 dp
    assert [p.n for p in trend] == list(range(1, len(weights) + 1))


@settings(max_examples=100, deadline=None)
@given(
    intake=st.floats(1200, 4000),
    delta=st.floats(-2.0, 1.0),
    prior=st.floats(1500, 4500),
    count=st.integers(0, 6),
)
def test_calibration_monotonic_in_intake(
    intake: float, delta: float, prior: float, count: int
) -> None:
    d0 = dt.date(2026, 9, 7)
    days = [en.DayIntake(d0 + dt.timedelta(days=i), intake, "confirmed") for i in range(5)]
    more = [en.DayIntake(d0 + dt.timedelta(days=i), intake + 100, "confirmed") for i in range(5)]
    a = en.calibrate_tdee(days, delta, prior, count)
    b = en.calibrate_tdee(more, delta, prior, count)
    assert isinstance(a, en.Calibration) and isinstance(b, en.Calibration)
    assert b.tdee_measured > a.tdee_measured
    assert b.tdee_blended >= a.tdee_blended


def test_guardrails_order_and_bounds() -> None:
    prev = en.Macros(2300, 202, 71, 213, 30)
    small = en.Macros(2200, 202, 71, 188, 30)
    r = apply_guardrails(small, prev, weight_kg=100.9, kcal_floor=1800)
    assert isinstance(r, Applied) and r.source == "auto_adjust" and r.macros.kcal == 2200
    big = en.Macros(2000, 202, 71, 138, 30)
    r2 = apply_guardrails(big, prev, weight_kg=100.9, kcal_floor=1800)
    assert isinstance(r2, NeedsProposal) and "-300 kcal" in r2.reason
    low = en.Macros(1500, 150, 50, 0, 25)
    r3 = apply_guardrails(low, None, weight_kg=100.9, kcal_floor=1800, source="formula")
    assert isinstance(r3, Applied) and r3.source == "formula"
    assert r3.macros.kcal >= 1800 and r3.macros.protein_g == 202 and r3.macros.fat_g == 71
    assert len(r3.notes) == 3
    fast = apply_guardrails(prev, prev, weight_kg=100.9, kcal_floor=1800, trend_delta_2w=-2.0)
    assert isinstance(fast, Applied) and fast.macros.kcal == 2450 and fast.source == "auto_adjust"
    same = apply_guardrails(prev, prev, weight_kg=100.9, kcal_floor=1800, trend_delta_2w=-1.0)
    assert isinstance(same, Applied) and same.macros == prev and same.source == "calibration"


@settings(max_examples=150, deadline=None)
@given(
    kcal=st.integers(1200, 4000),
    prev_kcal=st.integers(1200, 4000),
    weight=st.floats(45, 160),
    floor=st.integers(1200, 2200),
)
def test_guardrails_properties(kcal: int, prev_kcal: int, weight: float, floor: int) -> None:
    proposed = en.macro_split(kcal, weight)
    previous = en.macro_split(prev_kcal, weight)
    r = apply_guardrails(proposed, previous, weight_kg=weight, kcal_floor=floor)
    m = r.macros
    assert m.kcal >= floor
    assert m.protein_g >= weight * 2.0 - 1e-9 and m.fat_g >= weight * 0.7 - 1e-9
    if isinstance(r, Applied):
        assert abs(m.kcal - previous.kcal) <= 150
    else:
        assert abs(m.kcal - previous.kcal) > 150 or abs(m.protein_g - previous.protein_g) > 10
