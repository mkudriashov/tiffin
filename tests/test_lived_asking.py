"""Asking Tiffin an ordinary question, at an ordinary moment, in ordinary words.

Both stories here started as screenshots. A person sent "Send me the menu for tomorrow" and was
answered "I need centimetres, like 84 102 for waist then hip" - because a tape question
was still open and the measures flow claimed every free-text message. And `/menu` showed today
and tomorrow together with no way to ask about one day, because the command took an argument and
never read it.

Both are the same failure underneath: something the person said was received, understood by
nobody, and answered as though they had said something else.
"""

from __future__ import annotations

import datetime as dt

import pytest

from tiffin.clock import TZ, FrozenClock
from tiffin.db import Db
from tiffin.telegram import measures
from tiffin.telegram.menuflow import day_named

SUNDAY = dt.date(2026, 9, 13)


# ---------------------------------------------------------------- a flow that claims everything
def test_a_flow_does_not_claim_a_message_that_cannot_be_an_answer_to_it(
    fixture_db: Db, two_persons, frozen_clock: FrozenClock
) -> None:
    """Sunday 16:50. The 10:00 tape question is unanswered. He asks about tomorrow's menu.

    `conversation_state` holds one open flow per person and the measures flow answered EVERY
    free-text message with "I need centimetres" - so the question he actually asked was consumed
    and never reached the router that could have answered it. The miss counter gets the flow out
    of the way after two goes, which does not help: the first miss costs a real message, and the
    first one is what people notice.
    """
    frozen_clock.set_local(dt.datetime(2026, 9, 13, 16, 50, tzinfo=TZ))
    flow = measures.MeasuresFlow(clock=frozen_clock)
    pid = two_persons["owner"]
    flow.arm(fixture_db, pid, SUNDAY)
    assert flow._state(fixture_db, pid) is not None, "the question is genuinely open"

    class Spy:
        def __init__(self):
            self.said: list[str] = []

        def reply(self, _person, html, *a, **kw):
            self.said.append(html)

    spy = Spy()
    h = type("H", (), {"db": fixture_db, "outbox": spy})()
    person = type("P", (), {"id": pid})()
    inbound = type("I", (), {"person": person})()

    claimed = flow.tape_text(h, inbound, "Send me the menu for tomorrow")  # type: ignore[arg-type]

    assert claimed is None, (
        "a message with no number in it cannot be a tape measurement, so the flow has no "
        f"business claiming it; it answered {spy.said!r}"
    )
    assert not spy.said, "and it says nothing, so the router can"
    assert flow._state(fixture_db, pid) is not None, "the tape question is still open for later"


def test_the_tape_question_still_answers_something_that_could_be_a_measurement(
    fixture_db: Db, two_persons, frozen_clock: FrozenClock
) -> None:
    """The guard is "no digits", not "not a measurement".

    "I ran 45 minutes this morning" has a number in it and is genuinely ambiguous, so one
    correction is right and the miss counter still gets out of the way after two. Widening the
    guard until it caught that too would put back the phantom-45-cm-waist bug.
    """
    frozen_clock.set_local(dt.datetime(2026, 9, 13, 16, 50, tzinfo=TZ))
    flow = measures.MeasuresFlow(clock=frozen_clock)
    pid = two_persons["owner"]
    flow.arm(fixture_db, pid, SUNDAY)

    class Spy:
        def __init__(self):
            self.said: list[str] = []

        def reply(self, _person, html, *a, **kw):
            self.said.append(html)

    spy = Spy()
    h = type("H", (), {"db": fixture_db, "outbox": spy})()
    inbound = type("I", (), {"person": type("P", (), {"id": pid})()})()

    assert flow.tape_text(h, inbound, "I ran 45 minutes this morning") == "handled"  # type: ignore[arg-type]
    assert spy.said and "centimetres" in spy.said[0]
    assert not fixture_db.all("SELECT id FROM tape_measures"), "and nothing phantom was recorded"


def test_real_centimetres_still_land(
    fixture_db: Db, two_persons, frozen_clock: FrozenClock
) -> None:
    frozen_clock.set_local(dt.datetime(2026, 9, 13, 16, 50, tzinfo=TZ))
    flow = measures.MeasuresFlow(clock=frozen_clock)
    pid = two_persons["owner"]
    flow.arm(fixture_db, pid, SUNDAY)

    class Spy:
        def __init__(self):
            self.said: list[str] = []

        def reply(self, _person, html, *a, **kw):
            self.said.append(html)

    spy = Spy()
    h = type("H", (), {"db": fixture_db, "outbox": spy})()
    inbound = type("I", (), {"person": type("P", (), {"id": pid})()})()

    assert flow.tape_text(h, inbound, "84 102") == "handled"  # type: ignore[arg-type]
    rows = fixture_db.all("SELECT site, cm FROM tape_measures ORDER BY site")
    assert {str(r["site"]): float(r["cm"]) for r in rows} == {"hip": 102.0, "waist": 84.0}


# ---------------------------------------------------------------- asking about another day
@pytest.mark.parametrize(
    ("said", "expect"),
    [
        ("", dt.date(2026, 9, 13)),
        ("today", dt.date(2026, 9, 13)),
        ("tomorrow", dt.date(2026, 9, 14)),
        ("Tomorrow", dt.date(2026, 9, 14)),
        ("tmr", dt.date(2026, 9, 14)),
        ("thu", dt.date(2026, 9, 17)),
        ("thursday", dt.date(2026, 9, 17)),
        ("mon", dt.date(2026, 9, 14)),
        ("2026-09-16", dt.date(2026, 9, 16)),
    ],
)
def test_a_day_named_the_way_people_name_days(said: str, expect: dt.date) -> None:
    """`/menu tomorrow` was accepted, ignored, and answered with today's menu.

    The same shape as `/rate 4` dropping the four and `/recipe Porridge` answering with the
    chilli: an argument that reaches the handler and is never read, so the reply looks like a
    perfectly good reply to a question nobody asked.
    """
    assert day_named(said, SUNDAY) == expect


def test_a_weekday_that_has_gone_means_the_next_one() -> None:
    """Asked on a Sunday, "sun" is next Sunday: nobody asks a menu bot about a day that is over,
    and answering with today's menu under tomorrow's heading is worse than asking again."""
    assert day_named("sun", SUNDAY) == dt.date(2026, 9, 20)


def test_words_that_are_not_a_day_are_refused_rather_than_guessed() -> None:
    """None, so the caller can say "I did not read that as a day" - which is the whole point.
    Falling back to today is what made the argument invisible in the first place."""
    assert day_named("banana", SUNDAY) is None
    assert day_named("next quarter", SUNDAY) is None


def test_the_offplan_question_does_not_turn_a_request_into_food(
    fixture_db: Db, two_persons, frozen_clock: FrozenClock
) -> None:
    """ "What changed?" is open, and he asks for the menu. Here a swallowed message is worse
    than a swallowed message: the answer becomes the DAY'S FOOD NOTE, gets estimated, and a
    number invented out of "send me the menu for tomorrow" reaches the measured TDEE and next
    week's targets. A wrong number is harder to notice than a missing one and lasts longer."""
    from tiffin.telegram import convstate

    assert convstate.asks_for_something_else("Send me the menu for tomorrow")
    # and the notes this flow exists for are still its own
    for note in (
        "skipped lunch, kebab and chips for dinner",
        "about the same",
        "two beers and a takeaway",
        "same",
    ):
        assert not convstate.asks_for_something_else(note), note
