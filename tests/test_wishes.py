"""A wish about every menu, said in the chat: kept, and the menu rebuilt with it on a tap.

and it starts the rebuild + if she says something like 'oh, there was too much chia seed' - it
will rebuild again". The page's "Build a new menu" and the bot's New menu already rebuilt on a
tap; the sentence said on its own reached nothing that could keep it, and from the evening
check-in it was booked as food eaten. And: "simple recipes are weekday only, weekend is where the
'treat' recipes come in."
"""

from __future__ import annotations

import datetime as dt

import pytest

from tiffin.food import prefs
from tiffin.jobs import menu_propose

from .test_menu import FakeGateway, world  # noqa: F401
from .test_review_0926_weekend import pair  # noqa: F401
from .test_review_0927_weekend_build import world as pair_world  # noqa: F401
from .test_swap import _buttons, bot, week  # noqa: F401


# ---------------------------------------------------------------- what a sentence wishes
@pytest.mark.parametrize(
    ("said", "expect"),
    [
        ("oh, there was too much rice", ["less rice"]),
        ("there were too many oats in the porridge", ["less oats"]),
        (
            "I want more chicken, less rice, more veg and cook with the air fryer more",
            ["more chicken", "less rice", "more veg"],
        ),
        ("not so much rice please", ["less rice"]),
        ("not enough chicken", ["more chicken"]),
        ("can we have more salmon?", ["more salmon"]),
        ("a bit less broccoli", ["less broccoli"]),
        # a refusal is the avoid list's, never a wish for more of the food
        ("no more lentils", []),
        ("I don't want any more lentils", []),
        # names no food: the chat's
        ("more time to cook", []),
        ("less faff", []),
        # for a while: said with that week's rebuild, not kept for every menu
        ("more salmon this week", []),
    ],
)
def test_the_wishes_a_sentence_makes(week, said: str, expect: list[str]) -> None:  # noqa: F811
    db, pid, *_ = week
    assert prefs.wishes_in(db, pid, said) == expect


# ---------------------------------------------------------------- said in the chat
def test_too_much_rice_is_kept_and_the_menu_rebuilt_on_one_tap(bot) -> None:  # noqa: F811
    send, tap, db, pid, _r, _clock = bot
    out = send("oh, there was too much rice")
    assert len(out) == 1, out
    text = out[0]["text"]
    assert "Kept for every menu from now on: less rice." in text, text
    assert prefs.standing_notes(db, pid) == ["less rice"]
    buttons = _buttons(out[0])
    assert set(buttons) == {"Rebuild now", "Undo"}
    built = tap(buttons["Rebuild now"])
    assert any("Working out what to cook, with less rice" in m["text"] for m in built), built
    assert prefs.standing_notes(db, pid) == ["less rice"], "kept once, not again by the build"


def test_undo_takes_the_wish_back_and_rebuild_then_builds_nothing(bot) -> None:  # noqa: F811
    send, tap, db, pid, _r, _clock = bot
    out = send("less rice please")
    buttons = _buttons(out[0])
    back = tap(buttons["Undo"])
    assert "Taken back: less rice" in back[-1]["text"]
    assert prefs.standing_notes(db, pid) == []
    late = tap(buttons["Rebuild now"])
    assert "taken back" in late[-1]["text"], late
    assert not any("Working out what to cook" in m["text"] for m in late)


def test_at_the_evening_check_in_a_complaint_is_not_food_eaten(bot) -> None:  # noqa: F811
    """21:10, the check-in open: "there was too much rice" is about the menu. It was booked as
    the day's off-plan food and sent for a calorie estimate."""
    from tiffin.telegram import dayflow

    send, _tap, db, pid, _r, clock = bot
    clock.set_local(dt.datetime(2026, 9, 7, 21, 10))
    assert dayflow.arm_evening(db, pid, clock.today_local(), clock=clock)
    out = send("there was too much rice")
    assert "less rice" in out[0]["text"], out
    assert db.scalar("SELECT COUNT(*) FROM estimates WHERE person_id = ?", (pid,)) == 0


@pytest.mark.parametrize(
    "said",
    [
        "I had less rice today",  # what she ate: the check-in's
        "I ate too much chocolate",  # what she ate, however it is put
        "what is for lunch?",  # a question: the chat's
        "more time please",  # names no food
    ],
)
def test_not_a_wish(bot, said: str) -> None:  # noqa: F811
    send, _tap, db, pid, *_ = bot
    send(said)
    assert prefs.standing_notes(db, pid) == []


def test_less_of_a_liked_food_takes_it_off_the_likes(bot) -> None:  # noqa: F811
    send, _tap, db, pid, _r, clock = bot
    prefs.amend(db, pid, "likes", add=["rice", "salmon"], clock=clock, strict=False)
    out = send("too much rice")
    assert "Taken off your liked foods: rice." in out[0]["text"], out
    assert prefs.terms_of(db, pid, "likes", clock.today_local()) == ["salmon"]


def test_a_wish_said_with_a_rebuild_is_kept_for_the_menus_after_it(world) -> None:  # noqa: F811
    """`/cook replace less rice`, the page's note or a change typed after New menu: all three
    reach `propose_now`, which now keeps the wish as well as the build's note."""
    pid, db, _tg, gateways, say, _tap, _run_job = world
    gateways.append(FakeGateway([]))
    say("/cook replace less rice")
    assert prefs.standing_notes(db, pid) == ["less rice"]


# ---------------------------------------------------------------- a repeat can be changed too
def test_a_repeated_menu_carries_new_menu(world) -> None:  # noqa: F811
    """The partner's repeat of 6 Oct came with no button at all: the menu most worth changing."""
    from tiffin.energy import Macros
    from tiffin.food import catalogue, menu

    from .test_menu import TZ, proposal_for

    pid, db, tg, gateways, _say, _tap, run_job = world
    first = menu.Segment(dt.date(2026, 9, 7), dt.date(2026, 9, 11))
    plan = menu.build_plan(
        db,
        pid,
        first,
        proposal_for(first),
        targets=Macros(2250, 200, 70, 200, 30),
        profile=db.one("SELECT * FROM profiles WHERE person_id = ?", (pid,)),
        items=catalogue.approved(db, pid),
        model_used="claude-opus-5",
        clock=db.clock,
    )
    db.clock.set_local(dt.datetime(2026, 9, 6, 18, 0, tzinfo=TZ))
    old_id = menu.store_plan(db, plan, status="proposed", clock=db.clock)
    menu.decide(db, old_id, "accepted")
    db.clock.set_local(dt.datetime(2026, 9, 12, 18, 0, tzinfo=TZ))
    gateways.append(FakeGateway([]))
    run_job(menu_propose.JOB, dt.date(2026, 9, 12))
    sent = [m for m in tg.sent if "repeated" in m["text"].lower()]
    assert sent, [m["text"][:60] for m in tg.sent]
    assert "New menu" in _buttons(sent[-1]), _buttons(sent[-1])


# ---------------------------------------------------------------- a named day
def test_a_named_day_is_planned_from_that_day(pair_world) -> None:  # noqa: F811
    """
    means tomorrow, so Friday could not be asked for by itself. The segment holding the day, from
    it."""
    from tiffin.clock import TZ
    from tiffin.food import menu

    from .test_menu import proposal_for

    db, him, _partner, clock, app, gw = pair_world
    clock.set_local(dt.datetime(2026, 9, 15, 9, 0, tzinfo=TZ))  # Tuesday: tomorrow is Wednesday
    profile = db.one("SELECT * FROM profiles WHERE person_id = ?", (him.id,))
    fri = dt.date(2026, 9, 18)
    seg = menu_propose.segment_from(profile, fri)
    assert seg is not None and seg == menu.Segment(fri, fri), "his Mon-Fri segment, from Friday"
    gw.payloads[:] = [proposal_for(seg) for _ in range(4)]
    said = menu_propose.propose_now(
        app, db, him, profile, app.outbox(db), clock=clock, replace=True, quiet_failure=True,
        day=fri,
    )  # fmt: skip
    newest = db.one(
        "SELECT segment_start, segment_end FROM menus WHERE person_id = ? ORDER BY id DESC",
        (him.id,),
    )
    assert newest is not None, said
    assert (str(newest["segment_start"]), str(newest["segment_end"])) == ("2026-09-18",) * 2


# ---------------------------------------------------------------- weekdays and the weekend
def test_simple_on_weekdays_is_not_asked_of_the_weekend(week) -> None:  # noqa: F811
    db, pid, _base, _r, clock = week
    notes = ["simple recipes on weekdays", "less rice", "proper treats at the weekend"]
    prefs.amend_notes(db, pid, add=notes, source="operator", clock=clock)
    assert prefs.standing_notes(db, pid, weekend=True) == notes[1:]
    assert prefs.standing_notes(db, pid, weekend=False) == notes[:2]
    assert prefs.standing_notes(db, pid) == notes
