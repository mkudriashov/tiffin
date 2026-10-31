"""Review fixes, group E2: proposals that learn.

The model offers and code chooses, but code's choice must never sink a menu the model's own answer
would have built, nor put one dish in two meals of a day. What a person asks for by name is not
refused as "a dinner in the last six days". Taste learns from what each person said and did, not
from who answered their days, from a partner's skip, or from an unconfirmed plate; a single plate
rests about a week. The shared weekend never asks for a dish one of them is predicted to dislike,
every draw is logged once for the menu stored, the novelty quota asks for what the prompt allows,
and a refused food is never held up as a loved dish.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import re
from typing import Any, cast

import pytest

from tiffin.clock import TZ, isoday
from tiffin.db import Db
from tiffin.energy import Macros
from tiffin.errors import ModelInvalid
from tiffin.food import catalogue, menu, plates, prefs, taste
from tiffin.jobs import menu_propose

from .test_menu import MONDAY, FakeGateway, proposal_for, world  # noqa: F401
from .test_personal import _dinner_last_friday
from .test_plates import _shared_weekend
from .test_review_0926_weekend import (  # noqa: F401
    HIS,
    PARTNERS,
    SAT,
    SUN,
    _agree,
    _weekend_payload,
    pair,
)
from .test_swap import MON, week  # noqa: F401

SEGMENT = menu.Segment(MONDAY, dt.date(2026, 9, 18))
NEXT_MONDAY = dt.date(2026, 9, 21)
HIS_DAY = Macros(2250, 200, 70, 200, 30)


def _lunch(name: str, ingredients: list[dict[str, int]]) -> dict[str, Any]:
    """One lunch alternative, as the model offers it."""
    return {
        "slot": "lunch",
        "recipe": {
            "name": name,
            "ingredients": ingredients,
            "method": ["Boil the rice.", "Cook the rest.", "Serve."],
            "prep_minutes": 25,
        },
    }


#: rice, oil and broccoli: a lunch that passes every rule of its own and leaves no day of the
#: world fixture able to reach its protein (the reviewer's probe)
RICE_AND_BROCCOLI = _lunch(
    "Rice and broccoli", [{"i": 6, "g": 120}, {"i": 5, "g": 15}, {"i": 1, "g": 200}]
)
#: named like the world fixture's dinner, with other grams (the reviewer's probe)
LIKE_THE_DINNER = _lunch(
    "Chicken, rice and broccoli",
    [{"i": 2, "g": 180}, {"i": 7, "g": 100}, {"i": 1, "g": 220}, {"i": 6, "g": 10}],
)


def _profile(db: Db, pid: int) -> Any:
    return db.one("SELECT * FROM profiles WHERE person_id = ? ORDER BY id DESC LIMIT 1", (pid,))


def _name(db: Db, recipe_id: int) -> str:
    return str(db.scalar("SELECT name FROM recipes WHERE id = ?", (recipe_id,)))


def _kept(db: Db, pid: int, slot: str) -> list[str]:
    return [
        str(r["name"])
        for r in db.all(
            "SELECT rc.name FROM built_dishes b JOIN recipes rc ON rc.id = b.recipe_id "
            "WHERE b.person_id = ? AND b.slot = ? ORDER BY b.id",
            (pid, slot),
        )
    ]


def _plate(db: Db, pid: int, day: dt.date, slot: str, name: str) -> int:
    """One dish on one plate of one day, as an accepted one-day menu. Returns the recipe id."""
    rid = db.insert(
        "recipes", name=name, servings=1, prep_minutes=20, method_json="[]", source="manual"
    )
    db.insert("recipe_ingredients", recipe_id=rid, ingredient_id=1, grams=100)
    mid = db.insert(
        "menus", person_id=pid, segment_start=isoday(day), segment_end=isoday(day),
        status="accepted",
    )  # fmt: skip
    did = db.insert(
        "menu_days", menu_id=mid, day=isoday(day), kcal=500, protein_g=30, fat_g=10, carb_g=50,
        fibre_g=5,
    )  # fmt: skip
    db.insert(
        "meal_slots", menu_day_id=did, slot=slot, recipe_id=rid, portion_multiplier=1.0,
        portion_g=300, kcal=500, protein_g=30, fat_g=10, carb_g=50, fibre_g=5,
    )  # fmt: skip
    return rid


# ---------------------------------------------------------------- #5 code's choice gives way
def test_an_alternative_the_day_cannot_plate_gives_way_to_the_first_choice(world) -> None:  # noqa: F811
    """When the days will not solve with the alternative code chose, the model's
    own first choice is plated instead, and the alternative is kept for the swap list."""
    pid, db, _tg, _gateways, _say, _tap, _run_job = world
    offered: list[list[str]] = []

    def the_alternative(_slot: str, options: Any) -> int:
        offered.append([o.name for o in options])
        return len(options) - 1

    plan = menu.build_plan(
        db,
        pid,
        SEGMENT,
        {**proposal_for(SEGMENT), "alternatives": [RICE_AND_BROCCOLI]},
        targets=HIS_DAY,
        profile=_profile(db, pid),
        chooser=the_alternative,
    )
    assert offered == [["Chicken and rice", "Rice and broccoli"]], "code did choose the other"
    assert {_name(db, plan.recipe_ids[menu.slot_key(d, "lunch")]) for d in SEGMENT.days} == {
        "Chicken and rice"
    }, "the model's own lunch, every day"
    assert [(slot, _name(db, rid)) for slot, rid in plan.spare] == [("lunch", "Rice and broccoli")]


def test_the_week_is_built_from_the_first_answer_when_the_draw_will_not_plate(world) -> None:  # noqa: F811
    """The seeded draw that picks the alternative no longer refuses every
    attempt and leaves the owner with no menu; the first answer is built, and logged as put in."""
    pid, db, _tg, gateways, _say, _tap, run_job = world
    model = FakeGateway([{**proposal_for(SEGMENT), "alternatives": [RICE_AND_BROCCOLI]}] * 4)
    gateways.append(model)
    assert run_job(menu_propose.JOB, dt.date(2026, 9, 12)) == "ok"
    assert menu.meals_for_day(db, pid, MONDAY)[1] == "Chicken and rice"
    assert _kept(db, pid, "lunch") == ["Rice and broccoli"]
    chosen = db.all(
        "SELECT dish FROM proposal_log WHERE person_id = ? AND source = 'alternative' "
        "AND chosen = 1",
        (pid,),
    )
    assert [r["dish"] for r in chosen] == ["Chicken and rice"], "the log says what was put in"


# ---------------------------------------------------------------- #81 one dish, one meal
def test_an_alternative_named_like_another_meal_is_never_put_in(world) -> None:  # noqa: F811
    """An alternative carrying the name of another meal's dish is not a choice,
    so lunch and dinner are never the same dish all week, and it is not kept as a spare either."""
    pid, db, _tg, gateways, _say, _tap, run_job = world
    gateways.append(FakeGateway([{**proposal_for(SEGMENT), "alternatives": [LIKE_THE_DINNER]}]))
    assert run_job(menu_propose.JOB, dt.date(2026, 9, 12)) == "ok"
    for day in SEGMENT.days:
        meals = menu.meals_for_day(db, pid, day)
        assert len(set(meals)) == len(meals), f"{day}: {meals}"
    assert menu.meals_for_day(db, pid, MONDAY)[1] == "Chicken and rice"
    assert _kept(db, pid, "lunch") == []


def test_the_chooser_is_never_offered_another_meals_dish(world) -> None:  # noqa: F811
    """The options code chooses between leave out a name another meal of the
    segment already has."""
    pid, db, _tg, _gateways, _say, _tap, _run_job = world
    offered: list[list[str]] = []

    def last(_slot: str, options: Any) -> int:
        offered.append([o.name for o in options])
        return len(options) - 1

    menu.build_plan(
        db,
        pid,
        SEGMENT,
        {**proposal_for(SEGMENT), "alternatives": [LIKE_THE_DINNER]},
        targets=HIS_DAY,
        profile=_profile(db, pid),
        chooser=last,
    )
    assert all("Chicken, rice and broccoli" not in names for names in offered), offered


def test_the_chooser_is_never_offered_another_meals_dish_spelled_another_way(world) -> None:  # noqa: F811
    """ "Chicken rice and broccoli" is the dinner's dish
    without its comma, and is no more a lunch than the dinner's own name."""
    pid, db, _tg, _gateways, _say, _tap, _run_job = world
    offered: list[list[str]] = []

    def last(_slot: str, options: Any) -> int:
        offered.append([o.name for o in options])
        return len(options) - 1

    for spelled in (
        "Chicken rice and broccoli",
        "Chicken, rice & broccoli",
        "chicken, rice+broccoli",
    ):
        offered.clear()
        other = _lunch(spelled, LIKE_THE_DINNER["recipe"]["ingredients"])
        menu.build_plan(
            db,
            pid,
            SEGMENT,
            {**proposal_for(SEGMENT), "alternatives": [other]},
            targets=HIS_DAY,
            profile=_profile(db, pid),
            chooser=last,
        )
        assert offered in ([], [["Chicken and rice"]]), (spelled, offered)


def test_a_menu_with_one_dish_in_two_meals_of_a_day_is_asked_again(world) -> None:  # noqa: F811
    """The model's own answer with the dinner's dish for
    lunch as well is refused, so the model is asked again - and batch.plan never stirs two
    recipes of one name into one pot."""
    pid, db, _tg, _gateways, _say, _tap, _run_job = world
    for spelled in ("Chicken, rice and broccoli", "Chicken, rice & broccoli"):
        payload = proposal_for(SEGMENT)
        for day in payload["days"]:
            day["slots"][1]["recipe"]["name"] = spelled
        with pytest.raises(ModelInvalid, match="two meals"):
            menu.build_plan(db, pid, SEGMENT, payload, targets=HIS_DAY, profile=_profile(db, pid))


# ---------------------------------------------------------------- #97 asked for by name
def test_a_recent_dinner_they_ask_for_by_name_is_built(world) -> None:  # noqa: F811
    """A dinner from the last six days that the person's own note names is
    left out of the gap, so the prompt allows it and the build does not refuse it."""
    pid, db, _tg, gateways, say, _tap, _run_job = world
    _dinner_last_friday(db, pid, "Chicken, rice and broccoli")
    db.clock.set_local(dt.datetime(2026, 9, 13, 10, 0, tzinfo=TZ))  # type: ignore[attr-defined]
    model = FakeGateway([proposal_for(SEGMENT)] * 4)
    gateways.append(model)
    say("/cook replace chicken rice and broccoli again for dinner")
    gap = re.search(r"^Dinners from the last 6 days[^:]*: (.*)$", model.calls[0], re.M)
    assert gap is not None and gap.group(1) == "(none)", gap
    assert all("a dinner in the last 6 days" not in c for c in model.calls)
    assert menu.meals_for_day(db, pid, MONDAY)[2] == "Chicken, rice and broccoli"


def test_the_gap_still_holds_for_a_dinner_nobody_asked_for(world) -> None:  # noqa: F811
    """Only the dinner the note names comes back; the rest of the gap stays."""
    pid, db, _tg, _gateways, _say, _tap, _run_job = world
    _dinner_last_friday(db, pid, "Beef chilli")
    assert menu.dinner_gap(db, pid, MONDAY, asked="more fish") == ["Beef chilli"]
    assert menu.dinner_gap(db, pid, MONDAY, asked="beef chilli again please") == []
    assert menu.dinner_gap(db, pid, MONDAY, asked="beef") == ["Beef chilli"], "a word, not the dish"


def test_a_note_naming_a_recent_dinner_to_get_away_from_keeps_the_gap(world) -> None:  # noqa: F811
    """A note that names a recent dinner to be rid of it
    is no ask for it again - the gap stays in the prompt and in code."""
    pid, db, _tg, _gateways, _say, _tap, _run_job = world
    _dinner_last_friday(db, pid, "Beef chilli")
    for note in (
        "something other than beef chilli",
        "anything but beef chilli",
        "no beef chilli this week",
        "sick of beef chilli",
        "fish instead of beef chilli",
        "rather than beef chilli, something with fish",
        "beef chilli, not again",
        "I don't want beef chilli again",
        "beef chilli",
        # what the name is followed by can complain of it, whatever came first (verification 2)
        "I've had beef chilli too often",
        "the beef chilli was too spicy, something milder",
        "beef chilli was too much",
        "had beef chilli too many times",
        "Beef chilli? Please, never again",
        "beef chilli again? no thanks",
        "beef chilli again and it is getting boring",
        # ...and "have had" is what was eaten, not what is asked for
        "we have had beef chilli all week",
        "have had beef chilli every day, change it up",
    ):
        assert menu.dinner_gap(db, pid, MONDAY, asked=note) == ["Beef chilli"], note
    for note in (
        "I want beef chilli",
        "the same beef chilli",
        "beef chilli again, no rice",
        "the same beef chilli all week",
        "I want beef chilli too",
        "I'd like to have beef chilli again",
    ):
        assert menu.dinner_gap(db, pid, MONDAY, asked=note) == [], note


def test_a_note_complaining_of_last_weeks_dinner_does_not_bring_it_back(world) -> None:  # noqa: F811
    """ "I have had it too often" named the dinner and
    lifted it out of the gap, so the week repeated the dish the note complained of."""
    pid, db, _tg, gateways, say, _tap, _run_job = world
    _dinner_last_friday(db, pid, "Chicken, rice and broccoli")
    db.clock.set_local(dt.datetime(2026, 9, 13, 10, 0, tzinfo=TZ))  # type: ignore[attr-defined]
    model = FakeGateway([proposal_for(SEGMENT)] * 4)
    gateways.append(model)
    say("/cook I have had chicken, rice and broccoli too often, something lighter for dinner")
    gap = re.search(r"^Dinners from the last 6 days[^:]*: (.*)$", model.calls[0], re.M)
    assert gap is not None and gap.group(1) == "Chicken, rice and broccoli", gap
    assert "Chicken, rice and broccoli" not in menu.meals_for_day(db, pid, MONDAY)


def test_something_other_than_last_weeks_dinner_is_not_built_with_it(world) -> None:  # noqa: F811
    """The reviewer's probe - "something other than" the
    dinner of last Friday kept it in the gap line, and no menu repeats it."""
    pid, db, _tg, gateways, say, _tap, _run_job = world
    _dinner_last_friday(db, pid, "Chicken, rice and broccoli")
    db.clock.set_local(dt.datetime(2026, 9, 13, 10, 0, tzinfo=TZ))  # type: ignore[attr-defined]
    model = FakeGateway([proposal_for(SEGMENT)] * 4)
    gateways.append(model)
    say("/cook something other than chicken, rice and broccoli for dinner")
    gap = re.search(r"^Dinners from the last 6 days[^:]*: (.*)$", model.calls[0], re.M)
    assert gap is not None and gap.group(1) == "Chicken, rice and broccoli", gap
    assert "Chicken, rice and broccoli" not in menu.meals_for_day(db, pid, MONDAY)


# ---------------------------------------------------------------- #52 fairness from what was said
def test_fairness_does_not_lean_to_whoever_answers_less(pair) -> None:  # noqa: F811
    """Answering one's days says nothing about how the shared meals suited
    either person, so the owner confirming the weekend and the partner not leaves the weight
    even."""
    db, him, partner, _clock, built = _shared_weekend(pair)
    for day in (SAT, SUN):
        db.insert(
            "day_confirmations", person_id=him.id, day=isoday(day), status="confirmed",
            basis="menu",
        )  # fmt: skip
    assert taste.fair_weight(db, partner.id, him.id, NEXT_MONDAY) == pytest.approx(0.5)
    assert taste.fair_weight(db, him.id, partner.id, NEXT_MONDAY) == pytest.approx(0.5)
    # ...while what each of them said about a dish they shared still moves it
    lunch = int(
        db.scalar(
            "SELECT ms.recipe_id FROM menu_days md JOIN meal_slots ms ON ms.menu_day_id = md.id "
            "WHERE md.menu_id = ? AND md.day = ? AND ms.slot = 'lunch'",
            (built.menu_ids[partner.id], isoday(SAT)),
        )
    )
    db.insert("ratings", person_id=partner.id, recipe_id=lunch, score=2, note=None)
    db.insert("ratings", person_id=him.id, recipe_id=lunch, score=5, note=None)
    assert taste.fair_weight(db, partner.id, him.id, NEXT_MONDAY) > 0.5, "it suited her less"


# ---------------------------------------------------------------- #54 his skip is not hers
def test_a_skip_that_followed_the_partner_is_not_counted_against_the_dish(
    pair,  # noqa: F811
    config,
) -> None:
    """When the owner skips a dish they cook together, the partner's plate
    follows, and
    her taste for the dish is unmoved; his own skip still counts against it for him."""
    db, him, partner, clock, _built = _shared_weekend(pair)
    dinner = "Chicken, rice and broccoli, Sunday's way"
    partners_before = taste.tastes(db, partner.id, [dinner], NEXT_MONDAY)[dinner]
    his_before = taste.tastes(db, him.id, [dinner], NEXT_MONDAY)[dinner]
    plates.resize(db, him.id, SUN, {"dinner": "skip"}, config=config, clock=clock)
    st = plates.state(db, partner.id, SUN)
    assert st is not None and st["levels"]["dinner"] == "skip", "her plate did follow"
    partners = taste.tastes(db, partner.id, [dinner], NEXT_MONDAY)[dinner]
    his = taste.tastes(db, him.id, [dinner], NEXT_MONDAY)[dinner]
    assert (
        partners.liking == partners_before.liking and partners.evidence == partners_before.evidence
    )
    assert his.liking < his_before.liking, "his own skip still says something"


def test_partner_own_skip_on_a_day_cooked_together_counts_for_partner(pair, config) -> None:  # noqa: F811
    """The skip of whoever asked counts, whichever of the two it was."""
    db, him, partner, clock, _built = _shared_weekend(pair)
    dinner = "Chicken, rice and broccoli, Sunday's way"
    before = taste.tastes(db, partner.id, [dinner], NEXT_MONDAY)[dinner]
    plates.resize(db, partner.id, SUN, {"dinner": "skip"}, config=config, clock=clock)
    assert taste.tastes(db, partner.id, [dinner], NEXT_MONDAY)[dinner].liking < before.liking
    his = taste.tastes(db, him.id, [dinner], NEXT_MONDAY)[dinner]
    assert his.evidence == 0, "and his plate followed hers"


def test_their_skip_stays_their_when_partner_then_changes_another_plate(pair, config) -> None:  # noqa: F811
    """Both surfaces send every level the day has, so her
    next change carries the dinner skip that only followed his. It is still his skip, not hers,
    and his own still counts after his plates follow her change."""
    db, him, partner, clock, _built = _shared_weekend(pair)
    dinner = "Chicken, rice and broccoli, Sunday's way"
    partners_before = taste.tastes(db, partner.id, [dinner], NEXT_MONDAY)[dinner]
    his_before = taste.tastes(db, him.id, [dinner], NEXT_MONDAY)[dinner]
    plates.resize(db, him.id, SUN, {"dinner": "skip"}, config=config, clock=clock)
    st = plates.state(db, partner.id, SUN)
    assert st is not None
    plates.resize(
        db, partner.id, SUN, {**st["levels"], "snack": "more"}, config=config, clock=clock
    )
    partners = taste.tastes(db, partner.id, [dinner], NEXT_MONDAY)[dinner]
    his = taste.tastes(db, him.id, [dinner], NEXT_MONDAY)[dinner]
    assert (
        partners.liking == partners_before.liking and partners.evidence == partners_before.evidence
    )
    assert his.liking < his_before.liking, "his own skip, under his own name"
    # ...and still after he answers her change with one of his own
    st = plates.state(db, him.id, SUN)
    assert st is not None and st["levels"]["dinner"] == "skip"
    plates.resize(db, him.id, SUN, {**st["levels"], "snack": "planned"}, config=config, clock=clock)
    assert taste.tastes(db, him.id, [dinner], NEXT_MONDAY)[dinner].liking == his.liking
    assert taste.tastes(db, partner.id, [dinner], NEXT_MONDAY)[dinner].evidence == 0


def _before_0040(db: Db, rows: list[Any], at: str | None = None) -> None:
    """The `menu_emphasis` rows again, in order, as 0039 wrote them: no own_json. Stored `at` one
    moment when given (one change), else each at the moment of the row it copies."""
    for row in rows:
        db.insert(
            "menu_emphasis", person_id=row["person_id"], day=row["day"],
            base_menu_id=row["base_menu_id"], menu_id=row["menu_id"],
            levels_json=row["levels_json"], shortfall=row["shortfall"],
            created_at=at or row["created_at"],
        )  # fmt: skip


def test_a_follower_row_from_before_own_was_recorded_is_not_made_partners(pair, config) -> None:  # noqa: F811
    """A day resized before `own_json` was recorded has
    rows without it. Hers that followed his skip is not read as every meal her own choice, so her
    next change does not write his skip down as hers."""
    db, him, partner, clock, _built = _shared_weekend(pair)
    dinner = "Chicken, rice and broccoli, Sunday's way"
    partners_before = taste.tastes(db, partner.id, [dinner], NEXT_MONDAY)[dinner]
    plates.resize(db, him.id, SUN, {"dinner": "skip"}, config=config, clock=clock)
    clock.advance(minutes=1)
    _before_0040(db, db.all("SELECT * FROM menu_emphasis ORDER BY id"), clock.utc_stamp())
    st = plates.state(db, partner.id, SUN)
    assert st is not None and st["levels"]["dinner"] == "skip" and st["own"] == set()
    mine = plates.state(db, him.id, SUN)
    assert mine is not None and "dinner" in mine["own"], "his own row is still his"
    plates.resize(
        db, partner.id, SUN, {**st["levels"], "snack": "more"}, config=config, clock=clock
    )
    partners = taste.tastes(db, partner.id, [dinner], NEXT_MONDAY)[dinner]
    assert (
        partners.liking == partners_before.liking and partners.evidence == partners_before.evidence
    )


def test_partner_second_change_from_before_own_was_recorded_is_still_partners(pair, config) -> None:  # noqa: F811
    """Her second change of a day is stored straight
    after his plates followed her first. Without own_json it is still her own change, not one
    that followed his - a follower is stored in the same moment as the change it follows."""
    db, him, partner, clock, _built = _shared_weekend(pair)
    dinner = "Chicken, rice and broccoli, Sunday's way"
    before = taste.tastes(db, partner.id, [dinner], NEXT_MONDAY)[dinner]
    plates.resize(db, partner.id, SUN, {"snack": "more"}, config=config, clock=clock)
    clock.advance(minutes=1)
    plates.resize(
        db, partner.id, SUN, {"snack": "more", "dinner": "skip"}, config=config, clock=clock
    )
    _before_0040(db, db.all("SELECT * FROM menu_emphasis ORDER BY id"))
    st = plates.state(db, partner.id, SUN)
    assert st is not None and "dinner" in st["own"], "her own change"
    assert taste.tastes(db, partner.id, [dinner], NEXT_MONDAY)[dinner].liking < before.liking
    his = plates.state(db, him.id, SUN)
    assert his is not None and his["levels"]["dinner"] == "skip" and his["own"] == set()
    assert taste.tastes(db, him.id, [dinner], NEXT_MONDAY)[dinner].evidence == 0


# ---------------------------------------------------------------- #58 eaten, as far as was said
def _confirm(db: Db, pid: int, day: dt.date, basis: str) -> None:
    db.insert("day_confirmations", person_id=pid, day=isoday(day), status="confirmed", basis=basis)


def test_not_quite_counts_only_the_meals_a_change_did_not_replace(week) -> None:  # noqa: F811
    """A "Not quite" day with nothing parsed says nothing about the dishes;
    one whose accepted estimate named the meal it replaced counts the others, and so does an
    "As planned" day closed after such a change."""
    db, pid, _base, _r, _clock = week
    for i in range(5):
        _confirm(db, pid, MON + dt.timedelta(days=i), "mixed")
    on = NEXT_MONDAY
    dishes = ["Porridge with yoghurt", "Lentil dal", "Chicken and rice"]
    assert all(t.evidence == 0 for t in taste.tastes(db, pid, dishes, on).values())
    db.insert(
        "estimates", person_id=pid, day=isoday(MON), description="kebab for dinner", kcal=900,
        status="accepted", replaced_slots_json='["dinner"]',
    )  # fmt: skip
    got = taste.tastes(db, pid, dishes, on)
    assert got["Porridge with yoghurt"].evidence > 0 and got["Lentil dal"].evidence > 0
    assert got["Chicken and rice"].evidence == 0, "the kebab replaced it"
    # an "As planned" that closed the day after the change keeps the dinner out too
    _confirm(db, pid, MON, "menu")
    assert taste.tastes(db, pid, ["Chicken and rice"], on)["Chicken and rice"].evidence == 0


# ---------------------------------------------------------------- #115 a single plate rests a week
def test_a_single_plate_rests_about_a_week(world) -> None:  # noqa: F811
    """A dish had once rests for about a week, as documented, and a batch
    week still rests it for about three."""
    pid, db, _tg, _gateways, _say, _tap, _run_job = world
    _dinner_last_friday(db, pid, "Beef chilli")  # Friday 11 Sep
    friday = dt.date(2026, 9, 11)
    for days, resting in ((4, True), (6, True), (8, False)):
        on = friday + dt.timedelta(days=days)
        assert taste.tastes(db, pid, ["Beef chilli"], on)["Beef chilli"].resting is resting, days
    batch = [0.5 ** ((14 + k) / taste.BOREDOM_DAYS) for k in range(5)]
    assert taste.Taste("X", 0.8, sum(batch), 0.0, 1.0).resting, "two weeks after a batch week"
    later = [0.5 ** ((28 + k) / taste.BOREDOM_DAYS) for k in range(5)]
    assert not taste.Taste("X", 0.8, sum(later), 0.0, 1.0).resting, "four weeks after"


# ---------------------------------------------------------------- #99 the shared weekend's asks
def _shared_app(db: Db, clock: Any, config: Any, tmp_path: Any, gw: FakeGateway) -> Any:
    from tiffin.app import App
    from tiffin.secrets import Secrets

    from .fakes import FakeTelegram

    secrets_dir = tmp_path / "s"
    secrets_dir.mkdir()
    (secrets_dir / "telegram-token-dev.txt").write_text("1:t", encoding="utf-8")
    return App(
        config,
        Secrets(secrets_dir),
        clock=clock,
        transport=FakeTelegram(),
        gateway_factory=lambda _a, _d: cast("Any", gw),
    )


def test_the_shared_weekend_never_asks_for_a_dish_the_other_would_veto(
    pair,  # noqa: F811
    config,
    tmp_path,
) -> None:
    """A favourite of one that the other is predicted to dislike is not in
    the shared weekend's prompt, which the shared swap list already would not offer."""
    from tiffin.jobs import menu_shared

    db, (him, partner), clock = pair
    _agree(db, him.id)
    _agree(db, partner.id)
    for p, m in ((him, HIS), (partner, PARTNERS)):
        db.insert(
            "targets", person_id=p.id, week_start="2026-09-14", kcal=m.kcal,
            protein_g=m.protein_g, fat_g=m.fat_g, carb_g=m.carb_g, fibre_g=m.fibre_g,
            source="formula",
        )  # fmt: skip
    chicken = int(db.scalar("SELECT id FROM ingredients WHERE code = 'chicken'"))
    rice = int(db.scalar("SELECT id FROM ingredients WHERE code = 'rice'"))
    for name in ("Chicken traybake", "Chicken fried rice"):
        rid = db.insert(
            "recipes", name=name, servings=1, prep_minutes=30, method_json="[]", source="manual"
        )
        db.insert("recipe_ingredients", recipe_id=rid, ingredient_id=chicken, grams=180)
        db.insert("recipe_ingredients", recipe_id=rid, ingredient_id=rice, grams=70)
        db.insert("ratings", person_id=him.id, recipe_id=rid, score=5, note=None)
    # she swapped the traybake away on a weekend: nothing else known, so she is predicted to
    # dislike it - and the shared swap list would never offer it
    taste.record_swap(
        db, partner.id, old="Chicken traybake", new="Something else", new_id=None,
        day=dt.date(2026, 9, 13),
    )  # fmt: skip
    partners = taste.tastes(db, partner.id, ["Chicken traybake"], SAT)["Chicken traybake"]
    assert partners.liking < taste.VETO
    gw = FakeGateway([_weekend_payload() for _ in range(8)])
    menu_shared.build_now(_shared_app(db, clock, config, tmp_path, gw), db, clock=clock)
    asked = re.search(r"^Favourites[^:]*: (.*)$", gw.calls[0], re.M)
    assert asked is not None
    assert "Chicken traybake" not in asked.group(1), asked.group(1)
    assert "Chicken fried rice" in asked.group(1), "his other favourite is still asked for"


# ---------------------------------------------------------------- #110 logged once, for the stored
def test_one_build_logs_its_draws_once_for_the_menu_it_stored(world) -> None:  # noqa: F811
    """A build that was refused and asked again logs each favourite and each
    alternative once, and marks as chosen the dish the stored menu holds."""
    pid, db, _tg, gateways, _say, _tap, run_job = world
    _dinner_last_friday(db, pid, "Chicken, rice and broccoli")
    stew = _plate(db, pid, dt.date(2026, 8, 3), "dinner", "Beef stew")  # long ago, rated well
    db.insert("ratings", person_id=pid, recipe_id=stew, score=5, note=None)
    alt = {
        "slot": "lunch",
        "recipe": {
            "name": "Chicken rice bowl",
            "ingredients": [
                {"i": 2, "g": 180},
                {"i": 7, "g": 100},
                {"i": 1, "g": 220},
                {"i": 6, "g": 10},
            ],
            "method": ["Boil the rice.", "Grill the chicken.", "Steam the broccoli."],
            "prep_minutes": 30,
        },
    }
    again = proposal_for(SEGMENT)
    for day in again["days"]:
        day["slots"][2]["recipe"]["name"] = "Chicken and broccoli traybake"
    model = FakeGateway(
        [
            {**proposal_for(SEGMENT), "alternatives": [alt]},  # refused: a dinner of Friday's
            {**again, "alternatives": [alt]},
            {**again, "alternatives": [alt]},
        ]
    )
    gateways.append(model)
    run_job(menu_propose.JOB, dt.date(2026, 9, 12))
    assert len(model.calls) >= 2, "the first answer was refused"
    rows = db.all(
        "SELECT dish, source, chosen FROM proposal_log WHERE person_id = ? ORDER BY id", (pid,)
    )
    favourites = [r["dish"] for r in rows if r["source"] == "favourite"]
    assert favourites == ["Beef stew"], "the favourites asked for, once"
    options = [(r["dish"], int(r["chosen"])) for r in rows if r["source"] == "alternative"]
    assert sorted(d for d, _c in options) == ["Chicken and rice", "Chicken rice bowl"]
    chosen = [d for d, c in options if c]
    assert chosen == [menu.meals_for_day(db, pid, MONDAY)[1]], "chosen is what was stored"


# ---------------------------------------------------------------- #128 a quota the prompt allows
def test_week_two_is_not_held_to_one_new_dish_when_every_known_one_is_ruled_out(world) -> None:  # noqa: F811
    """After a model-built week every dish was new and every one of them is
    now "do not repeat", so the next prompt does not ask for no more than one new one."""
    pid, db, _tg, gateways, _say, _tap, run_job = world
    gateways.append(FakeGateway([proposal_for(SEGMENT)] * 4))
    assert run_job(menu_propose.JOB, dt.date(2026, 9, 12)) == "ok"
    db.insert(
        "targets", person_id=pid, week_start=isoday(NEXT_MONDAY), kcal=2250, protein_g=200,
        fat_g=70, carb_g=200, fibre_g=30, source="formula",
    )  # fmt: skip
    db.clock.set_local(dt.datetime(2026, 9, 19, 18, 0, tzinfo=TZ))  # type: ignore[attr-defined]
    second = FakeGateway([proposal_for(menu.Segment(NEXT_MONDAY, dt.date(2026, 9, 25)))] * 4)
    gateways[0] = second
    run_job(menu_propose.JOB, dt.date(2026, 9, 19))
    novelty = re.search(r"^Something new: (.*)$", second.calls[0], re.M)
    assert novelty is not None
    assert "no more than one new one" not in novelty.group(1), novelty.group(1)


def test_the_weekends_treats_are_not_the_weekday_cooks_novelty(world) -> None:  # noqa: F811
    """A treat new on Saturday does not hold Monday's menu back."""
    pid, db, _tg, _gateways, _say, _tap, _run_job = world
    _plate(db, pid, dt.date(2026, 9, 12), "dinner", "Smash burger")
    assert "include one dish they have never had" in menu_propose.novelty_line(db, pid, MONDAY)
    _dinner_last_friday(db, pid, "Something new on Friday")
    assert "no more than one new one" in menu_propose.novelty_line(db, pid, MONDAY)


# ---------------------------------------------------------------- #129 refused is not loved
def test_a_loved_dish_holding_a_refused_food_is_neither_example_nor_favourite(week) -> None:  # noqa: F811
    """After "no fish", the salmon traybake she rated 5 is no longer written
    into the prompt as a dish she loves, nor asked for as a favourite."""
    db, pid, _base, _r, clock = week
    later = MON + dt.timedelta(weeks=4)
    assert menu_propose.example_lines(db, pid, later)[0].startswith("Salmon traybake")
    assert "Salmon traybake" in menu.rated_names(db, pid)[0]
    prefs.amend(db, pid, "avoid", add=["fish"], clock=clock)
    assert all(i.code != "salmon" for i in catalogue.approved(db, pid))
    assert not any(line.startswith("Salmon") for line in menu_propose.example_lines(db, pid, later))
    assert "Salmon traybake" not in menu.rated_names(db, pid)[0]


def test_a_liked_food_they_have_since_refused_is_not_asked_for(week) -> None:  # noqa: F811
    """A food on their likes that "no fish" has since
    taken off their list is no favourite; a liked word naming no food at all is still said."""
    db, pid, _base, _r, clock = week
    prefs.amend(db, pid, "likes", add=["salmon", "spicy"], clock=clock)
    assert {"salmon", "spicy"} <= set(menu.rated_names(db, pid)[0])
    prefs.amend(db, pid, "avoid", add=["fish"], clock=clock)
    good = menu.rated_names(db, pid)[0]
    assert "salmon" not in good, good
    assert "spicy" in good


def test_the_shared_weekend_never_asks_for_a_food_one_of_them_refused(
    pair,  # noqa: F811
    config,
    tmp_path,
) -> None:
    """His liked yoghurt is not on the shared list once
    she refuses yoghurt, so the shared weekend's prompt does not ask for it as a favourite."""
    from tiffin.jobs import menu_shared

    db, (him, partner), clock = pair
    _agree(db, him.id)
    _agree(db, partner.id)
    for p, m in ((him, HIS), (partner, PARTNERS)):
        db.insert(
            "targets", person_id=p.id, week_start="2026-09-14", kcal=m.kcal,
            protein_g=m.protein_g, fat_g=m.fat_g, carb_g=m.carb_g, fibre_g=m.fibre_g,
            source="formula",
        )  # fmt: skip
    prefs.amend(db, him.id, "likes", add=["yoghurt", "chicken"], clock=clock)
    prefs.amend(db, partner.id, "avoid", add=["yoghurt"], clock=clock)
    gw = FakeGateway([_weekend_payload() for _ in range(8)])
    # the fixture's answer numbers a list of eight, and hers is seven now: only the ask is tested
    with contextlib.suppress(ModelInvalid):
        menu_shared.build_now(_shared_app(db, clock, config, tmp_path, gw), db, clock=clock)
    asked = re.search(r"^Favourites[^:]*: (.*)$", gw.calls[0], re.M)
    assert asked is not None
    assert "yoghurt" not in asked.group(1), asked.group(1)
    assert "chicken" in asked.group(1), "a food both can have is still asked for"


class _PromptOnly:
    """Enough of App for newdish.ask: a gateway that keeps the prompt and offers nothing."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def gateway(self, _db: Db) -> Any:
        return self

    def json_task(self, _task: str, prompt: str, _schema: str, *, person_id: int) -> Any:
        from tiffin.model.gateway import ModelResult

        self.prompts.append(prompt)
        return ModelResult(text="", data={"options": []}, model_used="fake", backend="fake")


def test_a_new_dish_for_a_day_cooked_together_never_asks_for_what_the_other_refused(
    pair,  # noqa: F811
    config,
) -> None:
    """The one-dish prompt for a shared day held its
    foods to both lists but its favourites to the asker's alone, so his liked yoghurt was asked
    for after she refused it."""
    from tiffin.food import newdish

    db, him, partner, clock, _built = _shared_weekend(pair)
    prefs.amend(db, him.id, "likes", add=["yoghurt", "chicken"], clock=clock)
    prefs.amend(db, partner.id, "avoid", add=["yoghurt"], clock=clock)
    app = _PromptOnly()
    newdish.ask(app, db, him.id, SUN, "lunch", clock=clock)
    asked = re.search(r"^Favourites[^:]*: (.*)$", app.prompts[0], re.M)
    assert asked is not None
    assert "yoghurt" not in asked.group(1), asked.group(1)
    assert "chicken" in asked.group(1), "a food both can have is still asked for"


# ---------------------------------------------------------------- #110 rows name their menu
def test_every_logged_draw_names_the_menu_it_was_weighed_for(world) -> None:  # noqa: F811
    """A weekend's own version built undecided while the
    switch says together logs too, so each row names its menu - the one eaten can be told apart
    from one nobody eats."""
    pid, db, _tg, gateways, _say, _tap, run_job = world
    stew = _plate(db, pid, dt.date(2026, 8, 3), "dinner", "Beef stew")
    db.insert("ratings", person_id=pid, recipe_id=stew, score=5, note=None)
    alt = _lunch("Chicken rice bowl", LIKE_THE_DINNER["recipe"]["ingredients"])
    gateways.append(FakeGateway([{**proposal_for(SEGMENT), "alternatives": [alt]}]))
    assert run_job(menu_propose.JOB, dt.date(2026, 9, 12)) == "ok"
    built = db.scalar(
        "SELECT MAX(id) FROM menus WHERE person_id = ? AND segment_start = ?",
        (pid, isoday(MONDAY)),
    )
    rows = db.all("SELECT source, menu_id FROM proposal_log WHERE person_id = ?", (pid,))
    assert {r["source"] for r in rows} == {"favourite", "alternative"}
    assert {r["menu_id"] for r in rows} == {built}
