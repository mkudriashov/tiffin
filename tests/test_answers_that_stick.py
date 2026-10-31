"""Answers that were taken and then thrown away, and numbers that moved without a reason.

Typing a number at a button question was accepted unbounded and then failed against a CHECK
constraint fifteen questions later, inside the single transaction that writes everything -
twenty-four answers and nothing kept, reported at the one question that had nothing to do
with it. A mis-tapped "As planned" was permanent. A goal date in the past produced "inf
kg/week" at the moment the person chooses how fast to lose weight. And a week where
recalibration did not happen was reported as though there had never been one.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from tiffin import onboarding as ob


# ---------------------------------------------------------------- bounded button answers
def test_a_typed_rate_outside_the_schemas_range_is_refused_here() -> None:
    """1.5 passed unbounded and blew up at the consent screen, rolling back everything."""
    parse = ob._float_choice(0.0, 1.0)
    assert parse("1.0", {}) == 1.0
    assert parse("0.7", {}) == 0.7
    with pytest.raises(ob.Invalid, match="outside 0-1"):
        parse("1.5", {})


def test_a_typed_activity_factor_is_bounded_too() -> None:
    parse = ob._float_choice(1.1, 2.0)
    assert parse("1.55", {}) == 1.55
    with pytest.raises(ob.Invalid, match="outside"):
        parse("2.5", {})


def test_words_still_get_the_friendly_message() -> None:
    with pytest.raises(ob.Invalid, match="buttons"):
        ob._float_choice(0.0, 1.0)("fast", {})
    with pytest.raises(ob.Invalid, match="buttons"):
        ob._int_choice(2, 8)("lots", {})


def test_every_bounded_step_matches_its_schema_check() -> None:
    """The point of bounding them is that the two agree; if the schema moves, so must this."""
    schema = Path(ob.__file__).with_name("schema.sql").read_text(encoding="utf-8")
    assert "activity_factor BETWEEN 1.1 AND 2.0" in schema
    assert "rate_pct_per_week BETWEEN 0.0 AND 1.0" in schema
    rate = next(s for s in ob.STEPS if s.id == "rate")
    with pytest.raises(ob.Invalid):
        rate.parse("1.5", {})
    act = next(s for s in ob.STEPS if s.id == "act")
    with pytest.raises(ob.Invalid):
        act.parse("2.5", {})


# ---------------------------------------------------------------- the goal date
def test_a_goal_date_in_the_past_is_refused() -> None:
    """It did no future check at all, and the next screen read "inf kg/week"."""
    with pytest.raises(ob.Invalid, match="already passed"):
        ob._future_date("2020-01-01", {})
    today = dt.date(2026, 9, 7)
    future = (today + dt.timedelta(days=90)).isoformat()
    assert ob._future_date(future, {ob.TODAY_KEY: today.isoformat()}) == future


def test_the_rate_prompt_never_prints_an_infinity(two_persons, fixture_db) -> None:
    from tiffin.persons import Person

    person = Person(1, "owner", "Alex", "neutral", 1, None, False)
    answers = {"wkg": 100.0, "gwt": 90.0, "gdt": "2020-01-01"}
    text = ob._rate_prompt(answers, person)
    assert "inf" not in text and "already passed" in text


# ---------------------------------------------------------------- the floor that lied
def test_the_calorie_floor_button_says_what_it_sets(two_persons) -> None:
    """The lowest button read "1050 kcal" and stored 1200, on the one setting whose purpose
    is "Tiffin never goes below this"."""
    from tiffin.persons import Person

    person = Person(2, "partner", "Her", "neutral", 2, None, False)
    answers = {"sex": "f", "wkg": 58.0, "hcm": 162.0, "dob": "1994-05-01"}
    for label, value in ob._floor_options(answers, person):
        assert value in label.replace(",", ""), f"{label} does not name {value}"


# ---------------------------------------------------------------- "I have no training week"
@pytest.mark.parametrize("word", ["none", "no", "skip", "nothing", "not yet", "N/A"])
def test_no_fixed_training_week_is_an_answer(word: str) -> None:
    """All of them were refused at the one mandatory free-text question, while the prompt's
    own "Unnamed days are rest" implied naming nothing should be fine."""
    week = ob.parse_plan(word, {})
    assert len(week) == 7 and {s["kind"] for s in week} == {"rest"}


def test_the_prompt_and_the_error_both_offer_that_escape() -> None:
    step = next(s for s in ob.STEPS if s.id == "plan")
    prompt = step.prompt if isinstance(step.prompt, str) else ""
    assert "none" in prompt
    with pytest.raises(ob.Invalid, match="none"):
        ob.parse_plan("I train quite a lot", {})


def test_an_error_shown_to_a_person_carries_no_raw_markup() -> None:
    """Invalid messages are sent through esc(), so a <code> tag arrives as literal &lt;code&gt;."""
    with pytest.raises(ob.Invalid) as e:
        ob.parse_plan("Mon climb 90, Tue bouldering 60", {})
    assert "<" not in str(e.value) and ">" not in str(e.value)


# ---------------------------------------------------------------- a week that did not calibrate
def test_a_skipped_calibration_is_reported_as_a_skip(fixture_db, two_persons) -> None:
    import inspect

    from tiffin.jobs import sunday_summary, weekly_calibration

    src = inspect.getsource(weekly_calibration.run)
    assert "sunday_skip" in src, "the reason is recorded"
    assert "AND confidence IS NOT NULL" in src, "and the last real confidence is kept"
    assert "sunday_skip" in inspect.getsource(sunday_summary.block_for), "and it is read"


def test_an_auto_applied_calorie_change_carries_its_reason() -> None:
    import inspect

    from tiffin.jobs import weekly_calibration

    src = inspect.getsource(weekly_calibration.run)
    assert '"note", "pace_note", "targets"' in src
    assert "verdict.notes" in src.split("notes = ")[1][:400]
