"""Batch cooking: one cook day, total amounts, boxes, and what will not keep.

The thing that made this necessary: a menu that repeats Monday to Friday reads as five days
of cooking. It is one Sunday afternoon and ten boxes, and nothing in Tiffin said so - not
how much rice in total, not how many containers, and not that the Friday box would be five
days old before anyone opened it.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from tiffin.db import Db
from tiffin.food import batch
from tiffin.onboarding import Invalid

MONDAY = dt.date(2026, 9, 14)
SUNDAY = MONDAY - dt.timedelta(days=1)


# ---------------------------------------------------------------- what they can type
@pytest.mark.parametrize(
    ("said", "want"),
    [
        ("lunch dinner", ("lunch", "dinner")),
        ("lunch, dinner", ("lunch", "dinner")),
        ("Dinner", ("dinner",)),
        ("all", batch.SLOTS),
        ("everything", batch.SLOTS),
        ("all except breakfast", ("lunch", "dinner", "snack", "pre_bed")),
        ("all but breakfast and snacks", ("lunch", "dinner", "pre_bed")),
        ("everything apart from breakfast", ("lunch", "dinner", "snack", "pre_bed")),
        ("none", ()),
        ("nothing", ()),
        ("breakfast lunch dinner", ("breakfast", "lunch", "dinner")),
        ("pre bed", ("pre_bed",)),
        ("prebed", ("pre_bed",)),
    ],
)
def test_the_way_people_say_it(said: str, want: tuple[str, ...]) -> None:
    assert batch.parse_slots(said) == want


@pytest.mark.parametrize("said", ["", "   ", "brunch", "elevenses and tea"])
def test_nonsense_is_refused_with_an_example(said: str) -> None:
    with pytest.raises(batch.BadSlots) as caught:
        batch.parse_slots(said)
    assert "lunch" in str(caught.value) or "meals" in str(caught.value)


def test_a_stored_setting_that_went_wrong_falls_back_rather_than_breaking() -> None:
    assert batch.read_slots('["lunch","dinner"]') == ("lunch", "dinner")
    assert batch.read_slots("not json") == batch.DEFAULT_SLOTS
    assert batch.read_slots('{"lunch": true}') == batch.DEFAULT_SLOTS
    assert batch.read_slots('["lunch","brunch"]') == ("lunch",)  # a slot that is not a slot
    assert batch.read_slots("[]") == ()


def test_it_is_described_in_words_never_in_json() -> None:
    assert batch.describe(("lunch", "dinner")) == "lunch and dinner"
    assert batch.describe(batch.SLOTS) == "every meal"
    assert batch.describe(()) == "nothing"
    assert batch.describe(("dinner",)) == "dinner"


@pytest.mark.parametrize(
    ("grams", "want"), [(1620.0, "1.6 kg"), (2000.0, "2 kg"), (900.0, "900 g"), (4.0, "4 g")]
)
def test_amounts_are_kitchen_units(grams: float, want: str) -> None:
    assert batch.weigh(grams) == want


# ---------------------------------------------------------------- the load itself
def _ingredient(db: Db, name: str, aisle: str = "meat", pantry: int = 0) -> int:
    return db.insert(
        "ingredients",
        code=name.replace(" ", "_"),
        name=name,
        aisle=aisle,
        pantry=pantry,
        pack_g=500.0,
        pack_label="500 g",
        kcal_100=150.0,
        protein_100=20.0,
        fat_100=5.0,
        carb_100=5.0,
        fibre_100=1.0,
        version=1,
    )


def _recipe(db: Db, name: str, *, keep_days: int = 3, freezable: int = 1, servings: int = 1) -> int:
    return db.insert(
        "recipes",
        name=name,
        servings=servings,
        prep_minutes=30,
        method_json=json.dumps(["cook it"]),
        source="manual",
        keep_days=keep_days,
        freezable=freezable,
    )


def _menu(
    db: Db,
    pid: int,
    days: list[dt.date],
    plan: dict[str, tuple[int, float]],
    status: str = "accepted",
) -> int:
    menu_id = db.insert(
        "menus",
        person_id=pid,
        segment_start=days[0].isoformat(),
        segment_end=days[-1].isoformat(),
        status=status,
    )
    for day in days:
        md = db.insert(
            "menu_days",
            menu_id=menu_id,
            day=day.isoformat(),
            kcal=2200,
            protein_g=180,
            fat_g=70,
            carb_g=200,
            fibre_g=30,
        )
        for slot, (recipe_id, mult) in plan.items():
            db.insert(
                "meal_slots",
                menu_day_id=md,
                slot=slot,
                recipe_id=recipe_id,
                portion_multiplier=mult,
                portion_g=400.0,
                kcal=700,
                protein_g=50,
                fat_g=20,
                carb_g=60,
                fibre_g=8,
            )
    return menu_id


@pytest.fixture
def week(fixture_db: Db, two_persons):
    """Mon-Fri, porridge for breakfast, chilli for lunch, curry for dinner."""
    db, pid = fixture_db, two_persons["owner"]
    rice = _ingredient(db, "basmati rice", "grains")
    chicken = _ingredient(db, "chicken thigh")
    salt = _ingredient(db, "salt", "cupboard", pantry=1)
    oats = _ingredient(db, "oats", "grains")
    porridge = _recipe(db, "porridge", keep_days=1, freezable=0)
    chilli = _recipe(db, "chilli con carne", keep_days=3, freezable=1)
    curry = _recipe(db, "thai green curry", keep_days=2, freezable=0)
    db.insert("recipe_ingredients", recipe_id=porridge, ingredient_id=oats, grams=80.0)
    db.insert("recipe_ingredients", recipe_id=chilli, ingredient_id=rice, grams=200.0)
    db.insert("recipe_ingredients", recipe_id=chilli, ingredient_id=chicken, grams=160.0)
    db.insert("recipe_ingredients", recipe_id=chilli, ingredient_id=salt, grams=3.0)
    db.insert("recipe_ingredients", recipe_id=curry, ingredient_id=rice, grams=200.0)
    db.insert("recipe_ingredients", recipe_id=curry, ingredient_id=chicken, grams=180.0)
    days = [MONDAY + dt.timedelta(days=i) for i in range(5)]
    menu_id = _menu(
        db,
        pid,
        days,
        {"breakfast": (porridge, 1.0), "lunch": (chilli, 1.0), "dinner": (curry, 1.0)},
    )
    return db, pid, menu_id


def test_five_days_of_lunch_and_dinner_is_one_cook_and_ten_boxes(week) -> None:
    db, _pid, menu_id = week
    menu = db.one("SELECT * FROM menus WHERE id = ?", (menu_id,))
    load = batch.plan(db, menu, ("lunch", "dinner"), SUNDAY)
    assert load.cook_day == SUNDAY, "the cook is the day before the segment"
    assert load.boxes == 10
    assert {d.name for d in load.dishes} == {"chilli con carne", "thai green curry"}
    assert all(d.boxes == 5 for d in load.dishes)
    assert all(d.multiples == 5.0 for d in load.dishes)


def test_breakfast_is_left_out_of_the_totals(week) -> None:
    """ "Everything except breakfast" has to mean the oats are not in the cook-day list."""
    db, _pid, menu_id = week
    menu = db.one("SELECT * FROM menus WHERE id = ?", (menu_id,))
    load = batch.plan(db, menu, ("lunch", "dinner"), SUNDAY)
    names = {a.name for a in load.amounts}
    assert "oats" not in names
    assert load.fresh == ("breakfast",)
    with_breakfast = batch.plan(db, menu, batch.SLOTS, SUNDAY)
    assert "oats" in {a.name for a in with_breakfast.amounts}
    assert with_breakfast.boxes == 15


def test_the_amounts_are_the_shopping_list_arithmetic(week) -> None:
    """Same expression, so the two can never disagree about how much rice a week takes."""
    db, _pid, menu_id = week
    menu = db.one("SELECT * FROM menus WHERE id = ?", (menu_id,))
    load = batch.plan(db, menu, ("lunch", "dinner"), SUNDAY)
    got = {a.name: a.grams for a in load.amounts}
    assert got["basmati rice"] == pytest.approx(2000.0)  # 200 g x 5 days x 2 meals
    assert got["chicken thigh"] == pytest.approx(1700.0)  # (160 + 180) x 5
    assert batch.weigh(got["basmati rice"]) == "2 kg"


def test_the_heaviest_thing_is_named_first(week) -> None:
    """The four things that fill a bag come first; the teaspoon of salt comes last."""
    db, _pid, menu_id = week
    menu = db.one("SELECT * FROM menus WHERE id = ?", (menu_id,))
    load = batch.plan(db, menu, ("lunch", "dinner"), SUNDAY)
    assert [a.name for a in load.amounts][:2] == ["basmati rice", "chicken thigh"]
    assert load.amounts[-1].name == "salt"


def test_a_portion_multiplier_scales_the_shop(week) -> None:
    db, pid, _menu_id = week
    chilli = int(db.scalar("SELECT id FROM recipes WHERE name = 'chilli con carne'"))
    rice = int(db.scalar("SELECT id FROM ingredients WHERE name = 'basmati rice'"))
    days = [dt.date(2026, 10, 5) + dt.timedelta(days=i) for i in range(2)]
    big = _menu(db, pid, days, {"lunch": (chilli, 1.5)})
    menu = db.one("SELECT * FROM menus WHERE id = ?", (big,))
    load = batch.plan(db, menu, ("lunch",), days[0] - dt.timedelta(days=1))
    assert load.dishes[0].multiples == 3.0
    got = {a.name: a.grams for a in load.amounts}
    assert got["basmati rice"] == pytest.approx(600.0)  # 200 g x 1.5 x 2 days
    assert rice  # the ingredient exists; the total above is what it adds up to


def test_a_recipe_with_more_than_one_serving_is_divided(fixture_db: Db, two_persons) -> None:
    """A recipe written for four does not need four times the shopping for one box."""
    db, pid = fixture_db, two_persons["owner"]
    beans = _ingredient(db, "black beans", "tins")
    stew = _recipe(db, "bean stew", servings=4)
    db.insert("recipe_ingredients", recipe_id=stew, ingredient_id=beans, grams=800.0)
    days = [MONDAY, MONDAY + dt.timedelta(days=1)]
    menu_id = _menu(db, pid, days, {"dinner": (stew, 1.0)})
    menu = db.one("SELECT * FROM menus WHERE id = ?", (menu_id,))
    load = batch.plan(db, menu, ("dinner",), SUNDAY)
    assert load.amounts[0].grams == pytest.approx(400.0)  # 800 / 4 servings x 2 boxes


# ---------------------------------------------------------------- what will not keep
def test_the_boxes_that_will_not_last_are_named(week) -> None:
    """The whole reason a per-day menu is the wrong view: a Friday box cooked on Sunday is
    five days old, and the fridge does not care what the menu says."""
    db, _pid, menu_id = week
    menu = db.one("SELECT * FROM menus WHERE id = ?", (menu_id,))
    load = batch.plan(db, menu, ("lunch", "dinner"), SUNDAY)
    late = {dish.name: days for dish, days in load.to_freeze}
    # chilli keeps 3 days from Sunday: Mon, Tue, Wed are fine; Thu and Fri are not
    assert late["chilli con carne"] == (dt.date(2026, 9, 17), dt.date(2026, 9, 18))
    # the curry keeps 2: Wed, Thu and Fri are past it
    assert len(late["thai green curry"]) == 3


def test_a_dish_that_does_not_freeze_is_flagged_separately(week) -> None:
    db, _pid, menu_id = week
    menu = db.one("SELECT * FROM menus WHERE id = ?", (menu_id,))
    load = batch.plan(db, menu, ("lunch", "dinner"), SUNDAY)
    freezes = {dish.name: dish.freezable for dish, _days in load.to_freeze}
    assert freezes["chilli con carne"] is True
    assert freezes["thai green curry"] is False


def test_cooking_on_the_day_the_segment_starts_is_not_scheduled_backwards(week) -> None:
    """Asked on the Wednesday, the cook day is today - not the Sunday that has gone."""
    db, _pid, menu_id = week
    menu = db.one("SELECT * FROM menus WHERE id = ?", (menu_id,))
    load = batch.plan(db, menu, ("lunch",), MONDAY + dt.timedelta(days=2))
    assert load.cook_day == MONDAY + dt.timedelta(days=2)


def test_the_next_segment_wins_over_the_one_being_eaten(week) -> None:
    """Asked on a Sunday, "what am I cooking" means the week ahead."""
    db, pid, _menu_id = week
    assert batch.menu_to_batch(db, pid, SUNDAY) is not None
    picked = batch.menu_to_batch(db, pid, SUNDAY)
    assert str(picked["segment_start"]) == MONDAY.isoformat()


def test_mid_week_it_falls_back_to_the_menu_being_eaten(week) -> None:
    db, pid, _menu_id = week
    picked = batch.menu_to_batch(db, pid, MONDAY + dt.timedelta(days=2))
    assert picked is not None, "a person mid-week must still get an answer"
    assert str(picked["segment_start"]) == MONDAY.isoformat()


def test_a_day_resized_by_hand_is_never_the_cook(week) -> None:
    """The owner resized Sunday 27 Sep's plates on the page, and his Batch tab then planned a
    one-day
    cook of that Sunday's burger instead of the week ahead; mid-week, a resized Thursday took the
    tab over as "Thu to Thu". A resized day is a change to one day's plates, not a cook."""
    db, pid, menu_id = week
    for day in (SUNDAY, MONDAY, MONDAY + dt.timedelta(days=3)):
        one_day = db.insert(
            "menus",
            person_id=pid,
            segment_start=day.isoformat(),
            segment_end=day.isoformat(),
            status="proposed",
        )
        db.insert("menu_decisions", menu_id=one_day, decision="accepted")
        db.insert(
            "menu_emphasis",
            person_id=pid,
            day=day.isoformat(),
            base_menu_id=menu_id,
            menu_id=one_day,
            levels_json="{}",
        )
    friday = MONDAY + dt.timedelta(days=4)
    for asked in (SUNDAY, MONDAY + dt.timedelta(days=1)):
        picked = batch.menu_to_batch(db, pid, asked)
        assert picked is not None
        assert (str(picked["segment_start"]), str(picked["segment_end"])) == (
            MONDAY.isoformat(),
            friday.isoformat(),
        ), asked


def test_boxes_left_in_the_freezer_by_a_pause_are_counted_past_a_resized_saturday(week) -> None:
    """Last week's Thursday and Friday chilli went into the freezer and nobody was there to eat
    it; this week's cook makes two fewer. The last cook was found by reading `menus` raw, so a
    Saturday resized by hand in between stood in for it, and the freezer read as empty."""
    db, pid, menu_id = week
    chilli = int(db.scalar("SELECT id FROM recipes WHERE name = 'chilli con carne'"))
    last_week = [MONDAY - dt.timedelta(days=7 - i) for i in range(5)]
    _menu(db, pid, last_week, {"lunch": (chilli, 1.0)})
    db.insert(
        "pause_periods",
        person_id=pid,
        start_day=last_week[3].isoformat(),
        end_day=last_week[4].isoformat(),
        reason="away",
    )
    saturday = SUNDAY - dt.timedelta(days=1)
    resized = db.insert(
        "menus",
        person_id=pid,
        segment_start=saturday.isoformat(),
        segment_end=saturday.isoformat(),
        status="proposed",
    )
    db.insert("menu_decisions", menu_id=resized, decision="accepted")
    db.insert(
        "menu_emphasis",
        person_id=pid,
        day=saturday.isoformat(),
        base_menu_id=resized,
        menu_id=resized,
        levels_json="{}",
    )
    menu = db.one("SELECT * FROM menus WHERE id = ?", (menu_id,))
    load = batch.plan(db, menu, ("lunch", "dinner"), SUNDAY)
    boxes = {d.name: d.boxes for d in load.dishes}
    assert boxes["chilli con carne"] == 3, "five for the week, less the two in the freezer"


def test_no_menu_at_all_is_none_not_an_exception(fixture_db: Db, two_persons) -> None:
    assert batch.menu_to_batch(fixture_db, two_persons["owner"], MONDAY) is None


def test_the_onboarding_step_and_the_setting_share_a_parser() -> None:
    """`/set batch` reuses the interview's own step, so the two cannot drift."""
    from tiffin.onboarding import STEP_BY_ID
    from tiffin.telegram.settings import BY_NAME

    field = BY_NAME["batch"]
    step = STEP_BY_ID[field.step]
    assert step.parse("all except breakfast", {}) == ["lunch", "dinner", "snack", "pre_bed"]
    with pytest.raises(Invalid):
        step.parse("brunch", {})


# ---------------------------------------------------------------- what a person reads
@pytest.fixture
def bot(fixture_db: Db, two_persons, frozen_clock, config, tmp_path):
    """The real command, over the real poller, so the message is the one Telegram gets."""
    from tiffin.app import App
    from tiffin.secrets import Secrets
    from tiffin.telegram.poller import Poller

    from .fakes import FakeTelegram

    db, pid = fixture_db, two_persons["owner"]
    secrets_dir = tmp_path / "s"
    secrets_dir.mkdir()
    (secrets_dir / "telegram-token-dev.txt").write_text("1:t", encoding="utf-8")
    tg = FakeTelegram()
    app = App(config, Secrets(secrets_dir), clock=frozen_clock, transport=tg)
    poller = Poller(
        app.api(),
        db,
        app.registry(db),
        app.handlers(db).handle,
        clock=frozen_clock,
        sleeper=lambda s: None,
    )
    n = {"i": 0}

    def say(text: str) -> str:
        n["i"] += 1
        tg.queue_update(tg.text_update(n["i"], 1000001, text))
        poller.run_once()
        return tg.sent[-1]["text"]

    return db, pid, say


def _profile(db: Db, pid: int, batch_slots: str = '["lunch","dinner"]') -> None:
    db.insert(
        "profiles",
        person_id=pid,
        effective_from="2026-09-01",
        sex="m",
        birth_date="1990-03-15",
        height_cm=183.0,
        activity_factor=1.55,
        kcal_floor=1800,
        batch_slots=batch_slots,
    )


def _a_week(db: Db, pid: int) -> None:
    """Tue-Fri, so the cook day is the frozen today."""
    rice = _ingredient(db, "basmati rice", "grains")
    chicken = _ingredient(db, "chicken thigh")
    oats = _ingredient(db, "oats", "grains")
    porridge = _recipe(db, "porridge", keep_days=1, freezable=0)
    chilli = _recipe(db, "chilli con carne", keep_days=3, freezable=1)
    db.insert("recipe_ingredients", recipe_id=porridge, ingredient_id=oats, grams=80.0)
    db.insert("recipe_ingredients", recipe_id=chilli, ingredient_id=rice, grams=200.0)
    db.insert("recipe_ingredients", recipe_id=chilli, ingredient_id=chicken, grams=160.0)
    days = [dt.date(2026, 9, 8) + dt.timedelta(days=i) for i in range(4)]
    _menu(db, pid, days, {"breakfast": (porridge, 1.0), "lunch": (chilli, 1.0)})


def test_the_message_says_the_totals_the_boxes_and_the_freezing(bot) -> None:
    db, pid, say = bot
    _profile(db, pid)
    _a_week(db, pid)
    text = say("/batch")
    assert "Batch cook Mon 07 Sep" in text
    assert "4 boxes" in text
    assert "chilli con carne: 4 boxes" in text
    assert "basmati rice 800 g" in text
    assert "oats" not in text, "breakfast is not batched, so its ingredients are not cooked"
    assert "Breakfast stays fresh" in text
    assert "Freeze" in text and "Fri" in text, "the Friday box is four days old on the Friday"
    assert len(text.splitlines()) <= 12


def test_full_lists_every_ingredient_one_per_line(bot) -> None:
    db, pid, say = bot
    _profile(db, pid)
    _a_week(db, pid)
    text = say("/batch full")
    assert "basmati rice 800 g" in text
    assert "chicken thigh 640 g" in text
    assert "/batch full" not in text, "no point telling them about the thing they just used"


def test_someone_who_cooks_fresh_is_told_how_to_change_it(bot) -> None:
    db, pid, say = bot
    _profile(db, pid, batch_slots="[]")
    _a_week(db, pid)
    text = say("/batch")
    assert "fresh" in text.lower()
    assert "/set batch" in text


def test_no_menu_yet_points_somewhere_rather_than_stopping(bot) -> None:
    db, pid, say = bot
    _profile(db, pid)
    text = say("/batch")
    assert "/menu" in text
    assert "no menu" in text.lower()


def test_before_onboarding_it_says_what_to_do_first(bot) -> None:
    _db, _pid, say = bot
    assert "/onboard" in say("/batch")


def test_a_menu_with_none_of_the_batched_meals_says_so(bot) -> None:
    db, pid, say = bot
    _profile(db, pid, batch_slots='["pre_bed"]')
    _a_week(db, pid)
    text = say("/batch")
    assert "nothing to batch" in text.lower()


def test_the_setting_can_be_changed_from_the_bot(bot) -> None:
    db, pid, say = bot
    _profile(db, pid)
    assert "lunch and dinner" in say("/set batch")
    say("/set batch all except breakfast")
    stored = db.scalar(
        "SELECT batch_slots FROM profiles WHERE person_id = ? ORDER BY id DESC LIMIT 1", (pid,)
    )
    assert json.loads(str(stored)) == ["lunch", "dinner", "snack", "pre_bed"]
    assert "every meal" not in say("/set batch")


def test_a_meal_that_is_not_a_meal_is_refused(bot) -> None:
    db, pid, say = bot
    _profile(db, pid)
    text = say("/set batch brunch")
    assert "lunch" in text.lower()
    stored = db.scalar(
        "SELECT batch_slots FROM profiles WHERE person_id = ? ORDER BY id DESC LIMIT 1", (pid,)
    )
    assert json.loads(str(stored)) == ["lunch", "dinner"], "nothing was written"


def test_the_split_per_box_is_stated_only_when_it_is_true(week) -> None:
    """ "Two kilograms of rice across eight boxes" is the number that gets used at the
    counter. It is only a number when every box really does get the same."""
    db, _pid, menu_id = week
    menu = db.one("SELECT * FROM menus WHERE id = ?", (menu_id,))
    load = batch.plan(db, menu, ("lunch", "dinner"), SUNDAY)
    by_name = {a.name: a for a in load.amounts}
    rice = by_name["basmati rice"]
    assert rice.boxes == 10 and rice.per_box == pytest.approx(200.0)
    assert rice.uniform, "200 g in the chilli and 200 g in the curry"
    chicken = by_name["chicken thigh"]
    assert chicken.boxes == 10
    assert not chicken.uniform, "160 g in the chilli, 180 g in the curry - there is no 'each'"


def test_the_full_view_does_not_claim_an_each_that_is_not_true(bot) -> None:
    db, pid, say = bot
    rice = _ingredient(db, "basmati rice", "grains")
    one = _recipe(db, "one")
    two = _recipe(db, "two")
    db.insert("recipe_ingredients", recipe_id=one, ingredient_id=rice, grams=100.0)
    db.insert("recipe_ingredients", recipe_id=two, ingredient_id=rice, grams=300.0)
    _profile(db, pid)
    _menu(
        db,
        pid,
        [dt.date(2026, 9, 8)],
        {"lunch": (one, 1.0), "dinner": (two, 1.0)},
    )
    text = say("/batch full")
    assert "basmati rice 400 g:" in text
    assert "two 300 g (1 box, 300 g each)" in text
    assert "one 100 g (1 box, 100 g each)" in text
    assert "200 g each" not in text, "100 g and 300 g is still not 200 g each"


def test_a_proposal_nobody_has_tapped_yet_is_still_added_up(bot) -> None:
    db, pid, say = bot
    _profile(db, pid)
    rice = _ingredient(db, "basmati rice", "grains")
    dish = _recipe(db, "chilli con carne")
    db.insert("recipe_ingredients", recipe_id=dish, ingredient_id=rice, grams=200.0)
    _menu(db, pid, [dt.date(2026, 9, 8), dt.date(2026, 9, 9)], {"lunch": (dish, 1.0)}, "proposed")
    text = say("/batch")
    assert "2 boxes" in text
    assert "basmati rice 400 g" in text
    assert "waiting for your yes" not in text
    assert "/proposals" not in text


def test_a_proposal_that_was_turned_down_is_not_offered(bot) -> None:
    db, pid, say = bot
    _profile(db, pid)
    rice = _ingredient(db, "basmati rice", "grains")
    dish = _recipe(db, "chilli con carne")
    db.insert("recipe_ingredients", recipe_id=dish, ingredient_id=rice, grams=200.0)
    menu_id = _menu(db, pid, [dt.date(2026, 9, 8)], {"lunch": (dish, 1.0)}, "proposed")
    db.insert("menu_decisions", menu_id=menu_id, decision="rejected")
    assert "no menu" in say("/batch").lower()


def test_a_different_dish_in_every_box_is_not_a_batch_and_says_so(bot) -> None:
    """The real menu: a cook cadence of one cook for Mon-Fri, and eight dishes for eight
    boxes. Both settings were doing what they were told and the result was the opposite of
    what had been asked for, with nothing anywhere saying so."""
    db, pid, say = bot
    _profile(db, pid)
    rice = _ingredient(db, "basmati rice", "grains")
    plan: dict[str, tuple[int, float]] = {}
    days = [dt.date(2026, 9, 8) + dt.timedelta(days=i) for i in range(2)]
    for slot in ("lunch", "dinner"):
        r = _recipe(db, f"dish {slot}")
        db.insert("recipe_ingredients", recipe_id=r, ingredient_id=rice, grams=100.0)
        plan[slot] = (r, 1.0)
    # two days, two slots, but a fresh recipe on each day: four dishes, four boxes
    menu_id = db.insert(
        "menus",
        person_id=pid,
        segment_start=days[0].isoformat(),
        segment_end=days[-1].isoformat(),
        status="accepted",
    )
    for day in days:
        md = db.insert(
            "menu_days",
            menu_id=menu_id,
            day=day.isoformat(),
            kcal=2200,
            protein_g=180,
            fat_g=70,
            carb_g=200,
            fibre_g=30,
        )
        for slot in ("lunch", "dinner"):
            r = _recipe(db, f"{slot} {day:%a}")
            db.insert("recipe_ingredients", recipe_id=r, ingredient_id=rice, grams=100.0)
            db.insert(
                "meal_slots",
                menu_day_id=md,
                slot=slot,
                recipe_id=r,
                portion_multiplier=1.0,
                portion_g=400.0,
                kcal=700,
                protein_g=50,
                fat_g=20,
                carb_g=60,
                fibre_g=8,
            )
    text = say("/batch")
    assert "4 dishes for 4 boxes is 4 cooks, not a batch" in text
    assert "/set variety same" in text


def test_a_menu_that_actually_repeats_is_not_nagged(week) -> None:
    db, _pid, menu_id = week
    from tiffin.telegram.batchflow import BatchFlow

    menu = db.one("SELECT * FROM menus WHERE id = ?", (menu_id,))
    load = batch.plan(db, menu, ("lunch", "dinner"), SUNDAY)
    assert BatchFlow._is_it_even_a_batch(load) == [], "two dishes over ten boxes is a batch"
