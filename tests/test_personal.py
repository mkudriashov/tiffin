"""Proposals that learn without nagging: Thompson draws logged at proposal time, the model's
alternatives chosen in code, a dinner never repeated within six days, about one new dish a week,
their own loved dishes as examples, a weekly shuffle of the numbered list, fairness over shared
weekends, and a few words on why each dish is offered (docs/research-2026-10-04.md, "still worth
doing"; review subagents' personalisation list).
"""

from __future__ import annotations

import datetime as dt
import re
import statistics

import pytest

from tiffin.food import catalogue, menu, swap, taste
from tiffin.jobs import menu_propose
from tiffin.model.gateway import ModelResult

from .test_menu import MONDAY, FakeGateway, a_day, proposal_for, world  # noqa: F401
from .test_swap import MON, week  # noqa: F401

SEGMENT = menu.Segment(MONDAY, dt.date(2026, 9, 18))


# ---------------------------------------------------------------- Thompson draws
def test_a_draw_is_seeded_and_widens_with_less_known() -> None:
    known = taste.Taste("Dal", 0.7, 0.0, 0.0, 40.0)
    barely = taste.Taste("Dal", 0.7, 0.0, 0.0, 0.0)
    seed = taste.seed_for(1, MONDAY, "Dal")
    assert known.sampled(seed) == known.sampled(seed), "the same week asks, the same answer"
    assert seed != taste.seed_for(1, MONDAY + dt.timedelta(days=7), "Dal")
    spread_known = statistics.pstdev(known.sampled(f"k{i}") for i in range(300))
    spread_barely = statistics.pstdev(barely.sampled(f"b{i}") for i in range(300))
    assert spread_known < spread_barely / 2, "a dish known well draws close to its mean"
    assert abs(statistics.mean(known.sampled(f"m{i}") for i in range(300)) - 0.7) < 0.03


def test_favourites_are_ranked_by_a_draw_and_logged(week) -> None:  # noqa: F811
    db, pid, _base, _r, _clock = week
    got = taste.favourites_now(db, pid, ["Salmon traybake", "salmon"], MON, log=True)
    assert got[-1] == "salmon", "a liked food, not a dish, is kept as it is"
    rows = db.all(
        "SELECT dish, source, mu, sampled, chosen FROM proposal_log WHERE person_id = ?", (pid,)
    )
    assert [(r["dish"], r["source"]) for r in rows] == [("Salmon traybake", "favourite")]
    assert 0 < float(rows[0]["mu"]) < 1 and rows[0]["sampled"] is not None


# ---------------------------------------------------------------- why each is offered
def test_each_swap_says_why_in_a_few_words(week) -> None:  # noqa: F811
    db, pid, _base, _r, clock = week
    offered = {c.name: c for c in swap.candidates(db, pid, MON, "lunch", clock.today_local())}
    assert offered["Salmon traybake"].why == "rated 5; had lately", "eaten all of last week"
    t = taste.Taste("X", 0.5, 0.0, 0.0, 0.0)
    assert taste.why(t, score=None, last_day=None, on=MON, new=True) == "new, written for this meal"
    assert "3 weeks ago" in taste.why(
        t, score=4, last_day=MON - dt.timedelta(days=21), on=MON, new=False
    )


# ---------------------------------------------------------------- fairness on shared weekends
def test_the_couple_score_leans_to_whoever_was_short_changed(monkeypatch) -> None:
    a = taste.Taste("Burger", 0.9, 0.0, 0.0, 5.0)
    b = taste.Taste("Burger", 0.5, 0.0, 0.0, 5.0)
    even = taste.couple_score(a, b)
    toward_b = taste.couple_score(a, b, weight=0.3)
    assert even is not None and toward_b is not None and toward_b < even
    monkeypatch.setattr(taste, "shared_liking", lambda *_a, **_k: (0.4, 0.75))
    assert taste.fair_weight(None, 1, 2, MON) == pytest.approx(0.5 + taste.FAIR_MAX_LEAN)  # type: ignore[arg-type]
    monkeypatch.setattr(taste, "shared_liking", lambda *_a, **_k: (0.6, 0.55))
    assert taste.fair_weight(None, 1, 2, MON) == pytest.approx(0.45)  # type: ignore[arg-type]


# ---------------------------------------------------------------- the weekly shuffle
def test_the_list_is_shuffled_by_person_and_week(week) -> None:  # noqa: F811
    db, pid, _base, _r, _clock = week
    items = catalogue.approved(db, pid)
    a = catalogue.shuffled(items, pid, MON)
    assert a == catalogue.shuffled(items, pid, MON + dt.timedelta(days=3)), "same week, same order"
    assert sorted(i.code for i in a) == sorted(i.code for i in items), "a permutation"
    weeks = {
        tuple(i.code for i in catalogue.shuffled(items, pid, MON + dt.timedelta(weeks=w)))
        for w in range(6)
    }
    assert len(weeks) > 1, "a different order across weeks"


class ByName:
    """Answers with the indexes the prompt gave each food: what the model must do, and what any
    shuffle of the list has to survive. `order` is the list in code order, which `a_day`'s
    indexes are into."""

    def __init__(self, order: list[str]) -> None:
        self.order = order
        self.calls: list[str] = []

    def json_task(self, task: str, prompt: str, schema_name: str, *, person_id: int) -> ModelResult:
        self.calls.append(prompt)
        idx = {
            m.group(2).strip(): int(m.group(1))
            for m in re.finditer(r"^(\d+)\. (.+?) —", prompt, re.M)
        }
        days = [a_day(d) for d in SEGMENT.days]
        for day in days:
            for slot in day["slots"]:
                for ing in slot["recipe"]["ingredients"]:
                    ing["i"] = idx[self.order[ing["i"] - 1]]
        return ModelResult(text="", data={"days": days}, model_used="fake", backend="fake")


def test_the_prompt_and_the_check_share_the_shuffled_numbers(world, config) -> None:  # noqa: F811
    pid, db, _tg, gateways, _say, _tap, run_job = world
    config.data["models"]["shuffle_catalogue"] = True
    in_code_order = catalogue.approved(db, pid)
    model = ByName([i.name for i in in_code_order])
    gateways.append(model)
    assert run_job(menu_propose.JOB, dt.date(2026, 9, 12)) == "ok"
    shown = re.findall(r"^\d+\. (.+?) —", model.calls[0], re.M)
    assert shown != [i.name for i in in_code_order], "the week's own order, not code order"
    breakfast = a_day(MONDAY)["slots"][0]["recipe"]["ingredients"]
    expected = {in_code_order[x["i"] - 1].code for x in breakfast}
    codes = {
        str(r["code"])
        for r in db.all(
            "SELECT i.code FROM menus m JOIN menu_days md ON md.menu_id = m.id "
            "JOIN meal_slots ms ON ms.menu_day_id = md.id AND ms.slot = 'breakfast' "
            "JOIN recipe_ingredients ri ON ri.recipe_id = ms.recipe_id "
            "JOIN ingredients i ON i.id = ri.ingredient_id WHERE m.person_id = ?",
            (pid,),
        )
    }
    assert codes == expected, "the shuffled numbers meant the same foods"


# ---------------------------------------------------------------- the model offers, code chooses
ALT = {
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


def test_an_alternative_is_chosen_in_code_and_the_other_kept(world) -> None:  # noqa: F811
    pid, db, _tg, gateways, _say, _tap, run_job = world
    gateways.append(FakeGateway([{**proposal_for(SEGMENT), "alternatives": [ALT]}]))
    assert run_job(menu_propose.JOB, dt.date(2026, 9, 12)) == "ok"
    logged = db.all(
        "SELECT dish, chosen FROM proposal_log WHERE person_id = ? AND source = 'alternative'",
        (pid,),
    )
    assert {r["dish"] for r in logged} == {"Chicken and rice", "Chicken rice bowl"}
    # weighed again on a retry (the carbohydrate floor), seeded the same: the same choice
    chosen_set = {r["dish"] for r in logged if int(r["chosen"])}
    assert len(chosen_set) == 1, "one chosen, every time it was weighed"
    chosen = chosen_set.pop()
    other = ({r["dish"] for r in logged} - {chosen}).pop()
    assert menu.meals_for_day(db, pid, MONDAY)[1] == chosen
    kept = db.all(
        "SELECT rc.name FROM built_dishes b JOIN recipes rc ON rc.id = b.recipe_id "
        "WHERE b.person_id = ? AND b.slot = 'lunch'",
        (pid,),
    )
    assert [r["name"] for r in kept] == [other], "the other is kept for the swap list"
    assert "alternatives" in gateways[0].calls[0], "the prompt offers the choice"


# ---------------------------------------------------------------- variety
def _dinner_last_friday(db, pid: int, name: str) -> None:
    rid = db.insert(
        "recipes", name=name, servings=1, prep_minutes=20, method_json="[]", source="manual"
    )
    db.insert("recipe_ingredients", recipe_id=rid, ingredient_id=1, grams=100)
    mid = db.insert(
        "menus",
        person_id=pid,
        segment_start="2026-09-11",
        segment_end="2026-09-11",
        status="accepted",
    )
    did = db.insert(
        "menu_days",
        menu_id=mid,
        day="2026-09-11",
        kcal=500,
        protein_g=30,
        fat_g=10,
        carb_g=50,
        fibre_g=5,
    )
    db.insert(
        "meal_slots", menu_day_id=did, slot="dinner", recipe_id=rid, portion_multiplier=1.0,
        portion_g=300, kcal=500, protein_g=30, fat_g=10, carb_g=50, fibre_g=5,
    )  # fmt: skip


def test_a_dinner_from_the_last_six_days_is_refused_and_asked_again(world) -> None:  # noqa: F811
    pid, db, _tg, gateways, _say, _tap, run_job = world
    _dinner_last_friday(db, pid, "Chicken, rice and broccoli")
    again = proposal_for(SEGMENT)
    for day in again["days"]:
        day["slots"][2]["recipe"]["name"] = "Chicken and broccoli traybake"
    model = FakeGateway([proposal_for(SEGMENT), again])
    gateways.append(model)
    assert run_job(menu_propose.JOB, dt.date(2026, 9, 12)) == "ok"
    first, second = model.calls[:2]
    gap_line = "Dinners from the last 6 days (never a dinner again this time, favourite or not): "
    assert gap_line + "Chicken, rice and broccoli" in first
    assert "a dinner in the last 6 days" in second
    assert menu.meals_for_day(db, pid, MONDAY)[2] == "Chicken and broccoli traybake"


def test_about_one_new_dish_a_week(world) -> None:  # noqa: F811
    pid, db, _tg, _gateways, _say, _tap, _run_job = world
    assert "include one dish they have never had" in menu_propose.novelty_line(db, pid, MONDAY)
    _dinner_last_friday(db, pid, "Something new on Friday")
    assert "no more than one new one" in menu_propose.novelty_line(db, pid, MONDAY)


def test_their_loved_dishes_are_written_out_as_examples(week) -> None:  # noqa: F811
    db, pid, _base, _r, _clock = week
    lines = menu_propose.example_lines(db, pid, MON + dt.timedelta(weeks=4))
    assert lines and lines[0].startswith("Salmon traybake (salmon fillets 140 g")
    assert "Chicken stir-fry" not in " ".join(lines), "rated 1: never an example"
