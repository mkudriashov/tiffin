"""A build cooks from the shop window's open packs before opening another: told them by
number, held to them by a check that re-prompts, and - targets
winning - allowed a new pack on its last attempt, which the menu message then says.

The world is tests/test_menu.py's: the owner, four meals a day, the frozen clock on Saturday 12
Sep -
here cooking each day, with Monday's chicken in force from a 1 kg pack.
"""

from __future__ import annotations

import datetime as dt
import json
import re

import pytest

from tiffin.clock import TZ, isoday
from tiffin.food import catalogue, menu, packs, solver
from tiffin.food.solver import PackRule
from tiffin.jobs import menu_propose
from tiffin.model.gateway import ModelResult

from .test_food_solver import TARGET, _mince_days, check
from .test_menu import a_day, world  # noqa: F401

MON = dt.date(2026, 9, 14)
WED = MON + dt.timedelta(days=2)
DAILY = json.dumps([[0], [1], [2], [3], [4]])


def _numbers(prompt: str) -> dict[str, int]:
    """The catalogue's numbers as the prompt gave them, by the food's first word."""
    return {
        re.findall(r"[a-z]+", name.lower())[0]: int(n)
        for n, name in re.findall(r"^(\d+)\. (?:\d+\. )?([A-Z][^\n]*)$", prompt, re.M)
    }


def _day(day: dt.date, n: dict[str, int], main: str, tag: str) -> dict:
    """Tests/test_menu.py's day, food for food (`a_day`), with `main` in the lunch and the dinner
    and the dishes named so no dinner repeats. The numbers are the prompt's, by food."""

    def dish(name: str, grams: int) -> dict:
        return {
            "name": name,
            "ingredients": [
                {"i": n[main], "g": grams},
                {"i": n["whey"], "g": 90},
                {"i": n["broccoli"], "g": 200},
                {"i": n["basmati"], "g": 10},
            ],
            "method": ["Grill it.", "Boil the rice.", "Steam the broccoli."],
            "prep_minutes": 30,
        }

    first = a_day(day)
    breakfast, snack = first["slots"][0], first["slots"][3]
    breakfast["recipe"]["ingredients"] = [
        {"i": n[w], "g": g} for w, g in (("olive", 80), ("porridge", 300), ("greek", 30))
    ]
    snack["recipe"]["ingredients"] = [
        {"i": n[w], "g": g} for w, g in (("milk", 200), ("greek", 20))
    ]
    for small in (breakfast, snack):
        small["recipe"]["name"] = f"{small['recipe']['name']} {tag}"
    return {
        "day": isoday(day),
        "slots": [
            breakfast,
            {"slot": "lunch", "recipe": dish(f"{main.title()} rice bowl {tag}", 200)},
            {"slot": "dinner", "recipe": dish(f"{main.title()} traybake {tag}", 220)},
            snack,
        ],
    }


class PackModel:
    """Writes Wednesday around turkey, until a prompt says a pack is open to cook from first -
    then around that, unless `stubborn`. Reads the numbers off the prompt, as the real one must."""

    def __init__(self, stubborn: bool = False) -> None:
        self.stubborn = stubborn
        self.calls: list[str] = []

    def json_task(self, task: str, prompt: str, schema_name: str, *, person_id: int):
        self.calls.append(prompt)
        n = _numbers(prompt)
        told = "cook from that first" in prompt and not self.stubborn
        main = "chicken" if told else "turkey"
        days = [
            # one tag a day: the scheduled build for someone who cooks each day is now Wednesday
            # to Friday in one go (menu_propose.week_run), each day its own dishes
            _day(dt.date.fromisoformat(d), n, main, d)
            for d in re.findall(r"^(\d{4}-\d\d-\d\d)", prompt, re.M)
        ]
        return ModelResult(text="", data={"days": days}, model_used="fake", backend="fake")


@pytest.fixture
def daily(world, frozen_clock):  # noqa: F811
    """The owner cooking each day; Monday's lunch and dinner are chicken from a 1 kg pack; a turkey
    pack is in the catalogue too."""
    pid, db, tg, gateways, _say, _tap, run_job = world
    db.insert(
        "profiles",
        person_id=pid,
        effective_from="2026-09-01",
        sex="m",
        birth_date="1990-03-15",
        height_cm=183.0,
        activity_factor=1.55,
        kcal_floor=1800,
        meals_per_day=4,
        segment_days=DAILY,
        variety_json=json.dumps({"all": "different"}),
    )
    for code, name, pack in (
        ("chicken", "Chicken breast, raw", 1000.0),
        ("turkey", "Turkey breast, raw", 500.0),
    ):
        db.insert(
            "ingredients",
            code=code,
            version=2 if code == "chicken" else 1,
            name=name,
            kcal_100=165,
            protein_100=31.0,
            fat_100=3.6,
            carb_100=0.0,
            fibre_100=0.0,
            pack_g=pack,
            pack_label=f"{pack:g} g pack",
            aisle="meat",
            pantry=0,
            use_whole_pack=0,
            approved=1,
            person_scope="both",
        )
    items = catalogue.approved(db, pid)
    n = {re.findall(r"[a-z]+", i.name.lower())[0]: k for k, i in enumerate(items, start=1)}
    from tiffin import targets

    t = db.one("SELECT * FROM targets WHERE person_id = ?", (pid,))
    plan = menu.build_plan(
        db,
        pid,
        menu.Segment(MON, MON),
        {"days": [_day(MON, n, "chicken", "mon")]},
        targets=targets.macros_of(t),
        profile=db.one("SELECT * FROM profiles WHERE person_id = ? ORDER BY id DESC", (pid,)),
        items=items,
    )
    mid = menu.store_plan(db, plan, status="proposed")
    menu.decide(db, mid, "accepted")
    return pid, db, tg, gateways, run_job, frozen_clock


def _wednesday(db, pid) -> set[str]:
    """The main proteins on Wednesday's plates in force."""
    winner = menu.accepted_menu_id(db, pid, WED)
    assert winner is not None, "no menu for Wednesday"
    return {
        str(r["code"])
        for r in db.all(
            "SELECT DISTINCT i.code FROM menu_days md JOIN meal_slots ms ON ms.menu_day_id = md.id "
            "JOIN recipe_ingredients ri ON ri.recipe_id = ms.recipe_id "
            "JOIN ingredients i ON i.id = ri.ingredient_id "
            "WHERE md.menu_id = ? AND md.day = ? AND i.aisle = 'meat'",
            (winner, isoday(WED)),
        )
    }


def _build_wednesday(daily, model: PackModel) -> str:
    _pid, _db, tg, gateways, run_job, clock = daily
    gateways.append(model)  # type: ignore[arg-type]
    clock.set_local(dt.datetime(2026, 9, 14, 12, 0, tzinfo=TZ))
    tg.sent.clear()
    assert run_job(menu_propose.JOB, MON) in ("ok", "degraded")
    return tg.sent[-1]["text"] if tg.sent else ""


def test_wednesday_cooks_from_mondays_open_pack_before_opening_another(daily) -> None:
    pid, db, *_ = daily
    model = PackModel()
    _build_wednesday(daily, model)
    first = model.calls[0]
    # told by number: Monday's chicken, what is left of the kilo, and the budget
    n = _numbers(first)
    assert re.search(
        rf"cook from these first: {n['chicken']}\. Chicken breast, raw: \d+ g left of a 1000 g "
        r"pack \(opened for Mon\)",
        first,
    ), first
    assert "already cook Chicken breast, raw" in first and "at most 2 main proteins" in first
    # the turkey answer was refused, by number, and the re-prompt carried it
    assert "opens a pack of Turkey breast, raw while" in model.calls[1]
    assert f"Chicken breast, raw (number {n['chicken']}) is open" in model.calls[1]
    # Wednesday cooks from the chicken pack, and the window opened one kind of meat in all: Monday
    # and Wednesday to Friday (built in one go) eat two of its 1 kg packs, and no turkey
    assert _wednesday(db, pid) == {"chicken"}
    week = packs.window_packs(db, pid, packs.window_of(db, pid, WED))
    meat = [p for p in week if p.main]
    assert [(p.code, p.packs) for p in meat] == [("chicken", 2)]


def test_targets_win_on_the_last_attempt_and_the_new_pack_is_said(daily) -> None:
    pid, db, *_ = daily
    model = PackModel(stubborn=True)
    text = _build_wednesday(daily, model)
    assert len(model.calls) == menu_propose.MAX_ATTEMPTS
    # kept on the last attempt: a menu, and a word on why there is another pack
    assert _wednesday(db, pid) == {"turkey"}
    assert "Turkey breast is a new pack though" in text
    assert "Chicken breast is still open: no menu cooking from it met your targets." in text


def test_a_note_asking_for_the_food_lets_it_open_a_pack() -> None:
    chicken = packs.Pack("chicken", "Chicken breast, raw", "meat", 1000.0, "1 kg")
    chicken.by_day = {"2026-09-14": 400.0}
    chicken.slots = {"lunch", "dinner"}
    uses = {"2026-09-16|dinner": {("salmon", "Salmon fillets, raw", "fish", "Salmon traybake")}}
    held = packs.Steer(open=(chicken,), planned=frozenset({"chicken"}), names={})
    assert held.problems(uses, {"chicken": 4})[0].startswith(
        "dinner 'Salmon traybake' opens a pack of Salmon fillets, raw while 600 g of Chicken "
        "breast, raw (number 4) is open"
    )
    for note in ("salmon on Wednesday please", "more fish"):
        asked = packs.Steer(open=(chicken,), planned=frozenset({"chicken"}), note=note)
        assert asked.problems(uses, {"chicken": 4}) == [], note
    refused = packs.Steer(open=(chicken,), planned=frozenset({"chicken"}), note="no salmon")
    assert refused.problems(uses, {"chicken": 4})


def test_a_breakfasts_open_bacon_never_makes_a_dinner_bacon() -> None:
    bacon = packs.Pack("bacon", "Back bacon, raw", "meat", 240.0, "8 rashers")
    bacon.by_day = {"2026-09-14": 40.0}
    bacon.slots = {"breakfast"}
    dinner = {"2026-09-16|dinner": {("beef", "Beef mince 5% fat, raw", "meat", "Chilli")}}
    steer = packs.Steer(open=(bacon,), planned=frozenset(), names={})
    assert steer.problems(dinner, {"bacon": 2}) == []
    breakfast = {"2026-09-16|breakfast": {("sausages", "Pork sausages, raw", "meat", "Fry-up")}}
    assert steer.problems(breakfast, {"bacon": 2})


def test_the_window_keeps_to_two_main_proteins_unless_asked() -> None:
    names = {"chicken": "Chicken breast, raw", "beef": "Beef mince 5% fat, raw"}
    steer = packs.Steer(planned=frozenset(names), names=names)
    third = {"2026-09-17|dinner": {("salmon", "Salmon fillets, raw", "fish", "Salmon traybake")}}
    said = steer.problems(third, {})
    assert said == [
        "lunches and dinners over this shop window cook 3 main proteins (Beef mince 5% fat, raw, "
        "Chicken breast, raw, Salmon fillets, raw): keep to 2, using Beef mince 5% fat, raw, "
        "Chicken breast, raw first"
    ]
    again = {"2026-09-17|dinner": {("beef", "Beef mince 5% fat, raw", "meat", "Chilli")}}
    assert steer.problems(again, {}) == []


def test_one_pack_fewer_is_tried_first_when_the_plates_need_only_a_little_less() -> None:
    # the mince only a little over one pack: macros are the dish's, grams only what is bought
    free = solver.solve_segment(_mince_days(5), fat_floor_g=65)
    assert isinstance(free, solver.SegmentSolution)
    portions = sum(d.multipliers["dinner"] for d in free.days)
    days = _mince_days(5, grams_per_portion=round(530.0 / portions, 1))
    plain = solver.solve_segment(days, whole_pack=[PackRule("beef_mince_5", 500)], fat_floor_g=65)
    fewer = solver.solve_segment(
        days, whole_pack=[PackRule("beef_mince_5", 500, fewer_first=True)], fat_floor_g=65
    )
    assert isinstance(plain, solver.SegmentSolution) and isinstance(fewer, solver.SegmentSolution)
    assert fewer.pins["beef_mince_5"].packs == plain.pins["beef_mince_5"].packs - 1
    # the days are solved again: every target still met
    for d in fewer.days:
        check(d, days[0].slots, TARGET, fat_floor=65, fibre_min=25)
    # far over a pack, the plates are not cut to save one
    far = _mince_days(5, grams_per_portion=round(700.0 / portions, 1))
    big = solver.solve_segment(
        far, whole_pack=[PackRule("beef_mince_5", 500, fewer_first=True)], fat_floor_g=65
    )
    base = solver.solve_segment(far, whole_pack=[PackRule("beef_mince_5", 500)], fat_floor_g=65)
    assert isinstance(big, solver.SegmentSolution) and isinstance(base, solver.SegmentSolution)
    assert big.pins["beef_mince_5"].packs == base.pins["beef_mince_5"].packs


def test_a_refused_answer_is_told_what_its_own_dishes_give(world) -> None:  # noqa: F811
    """The owner's builds swung between short on protein and short on fat: the re-prompt said only
    "fat 60 g < 70 g". It now carries each dish's figures at one portion and the day's, worked
    out in code - the model still only chooses."""
    pid, db, *_ = world
    items = catalogue.approved(db, pid)
    t = db.one("SELECT * FROM targets WHERE person_id = ?", (pid,))
    from tiffin import targets

    said = menu_propose.dish_figures({"days": [a_day(MON)]}, items, targets.macros_of(t))
    assert said.startswith("\nYour dishes at one portion each (Tiffin scales each between")
    assert "Oats with whey: " in said and " kcal, " in said and " g protein, " in said
    assert "The day at one portion each: " in said and "against 2250 kcal" in said
    # an answer it cannot read says nothing rather than something wrong
    assert menu_propose.dish_figures({"days": "nonsense"}, items, targets.macros_of(t)) == ""
