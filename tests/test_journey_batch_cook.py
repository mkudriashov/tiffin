"""The Sunday batch cook, as one sitting in a kitchen.

Twelve stories, each one person at one moment with one pan on and one question. The shape of
pre-bed Mon-Fri off a same-all-week menu and freezes; the partner batches lunch and dinner over a
rotating Mon-Wed + Thu-Fri cadence, does not freeze, has refused chickpeas, and has an accepted
Mon-Wed menu with a chickpea salad on it while a chickpea-free proposal waits for her tap.

Every story drives the real poller and handlers and reads the message the person gets.
"""

from __future__ import annotations

import datetime as dt
import json
import re

import pytest

from tiffin.app import App
from tiffin.clock import TZ
from tiffin.db import Db
from tiffin.secrets import Secrets
from tiffin.telegram.poller import Poller

from .fakes import FakeTelegram

OWNER_CHAT = 1000001
PARTNER_CHAT = 1000002
SUNDAY = dt.date(2026, 9, 13)
MONDAY = dt.date(2026, 9, 14)
MON_FRI = [MONDAY + dt.timedelta(days=i) for i in range(5)]
MON_WED = MON_FRI[:3]


# ---------------------------------------------------------------- kitchen props
def _ingredient(db: Db, code: str, name: str, *, aisle: str = "meat", pantry: int = 0) -> int:
    return db.insert(
        "ingredients",
        code=code,
        version=1,
        name=name,
        kcal_100=150.0,
        protein_100=20.0,
        fat_100=5.0,
        carb_100=5.0,
        fibre_100=1.0,
        pack_g=500.0,
        pack_label="500 g pack",
        aisle=aisle,
        pantry=pantry,
        approved=1,
        person_scope="both",
    )


def _recipe(
    db: Db,
    name: str,
    parts: dict[int, float],
    *,
    steps: list[str] | None = None,
    servings: int = 1,
    keep_days: int = 3,
    freezable: int = 0,
    prep_minutes: int = 30,
) -> int:
    rid = db.insert(
        "recipes",
        name=name,
        servings=servings,
        prep_minutes=prep_minutes,
        method_json=json.dumps(steps or [f"Cook the {name.lower()}."]),
        source="manual",
        keep_days=keep_days,
        freezable=freezable,
    )
    for ingredient_id, grams in parts.items():
        db.insert("recipe_ingredients", recipe_id=rid, ingredient_id=ingredient_id, grams=grams)
    return rid


def _menu(
    db: Db,
    pid: int,
    rows: list[tuple[dt.date, str, int, float]],
    *,
    status: str = "accepted",
    decision: str | None = None,
) -> int:
    """A menu from (day, slot, recipe, multiplier) rows, so a rotating week can be written."""
    days = sorted({r[0] for r in rows})
    menu_id = db.insert(
        "menus",
        person_id=pid,
        segment_start=days[0].isoformat(),
        segment_end=days[-1].isoformat(),
        status=status,
    )
    day_ids: dict[dt.date, int] = {}
    for day in days:
        day_ids[day] = db.insert(
            "menu_days",
            menu_id=menu_id,
            day=day.isoformat(),
            kcal=2000,
            protein_g=150.0,
            fat_g=70.0,
            carb_g=200.0,
            fibre_g=30.0,
        )
    for day, slot, recipe_id, mult in rows:
        db.insert(
            "meal_slots",
            menu_day_id=day_ids[day],
            slot=slot,
            recipe_id=recipe_id,
            portion_multiplier=mult,
            portion_g=400.0,
            kcal=600,
            protein_g=45.0,
            fat_g=18.0,
            carb_g=55.0,
            fibre_g=7.0,
        )
    if decision is not None:
        db.insert("menu_decisions", menu_id=menu_id, decision=decision)
    return menu_id


def _profile(
    db: Db,
    pid: int,
    *,
    batch_slots: list[str],
    freezes: int,
    segments: list[list[int]],
    variety: str,
    refused: list[str] | None = None,
) -> None:
    db.insert(
        "profiles",
        person_id=pid,
        effective_from="2026-09-01",
        sex="m",
        birth_date="1990-03-15",
        height_cm=180.0,
        activity_factor=1.55,
        kcal_floor=1500,
        meals_per_day=4,
        batch_slots=json.dumps(batch_slots),
        freezes=freezes,
        segment_days=json.dumps(segments),
        variety_json=json.dumps({"all": variety}),
        refused_foods=json.dumps(refused or []),
    )


def _owner_week(db: Db, pid: int) -> dict[str, int]:
    """His real menu 6 in shape: the same five dishes Mon-Fri, breakfast made fresh."""
    _profile(
        db,
        pid,
        batch_slots=["lunch", "dinner", "snack", "pre_bed"],
        freezes=1,
        segments=[[0, 1, 2, 3, 4]],
        variety="same",
    )
    oats = _ingredient(db, "oats", "Oats", aisle="grains")
    chicken = _ingredient(db, "chicken_breast", "Chicken breast")
    potato = _ingredient(db, "potato", "Potatoes", aisle="veg")
    turkey = _ingredient(db, "turkey_mince", "Turkey mince")
    yoghurt = _ingredient(db, "yoghurt", "Greek yoghurt", aisle="dairy")
    skyr = _ingredient(db, "skyr", "Skyr", aisle="dairy")
    dishes = {
        "breakfast": _recipe(db, "Overnight oats", {oats: 60.0}, prep_minutes=5),
        "lunch": _recipe(
            db,
            "Warm chicken, green bean and potato salad",
            {chicken: 150.0, potato: 200.0},
            prep_minutes=30,
        ),
        "dinner": _recipe(
            db,
            "Garlic turkey with potato wedges",
            {turkey: 180.0, potato: 250.0},
            prep_minutes=35,
        ),
        "snack": _recipe(db, "Strawberry protein yoghurt", {yoghurt: 200.0}, prep_minutes=3),
        "pre_bed": _recipe(db, "Skyr with peanut butter", {skyr: 170.0}, prep_minutes=3),
    }
    mult = {"breakfast": 0.9, "lunch": 0.85, "dinner": 0.95, "snack": 1.25, "pre_bed": 1.0}
    rows = [(d, slot, rid, mult[slot]) for d in MON_FRI for slot, rid in dishes.items()]
    _menu(db, pid, rows, status="repeated", decision="accepted")
    return dishes


def _partner_profile(db: Db, pid: int) -> None:
    _profile(
        db,
        pid,
        batch_slots=["lunch", "dinner"],
        freezes=0,
        segments=[[0, 1, 2], [3, 4]],
        variety="rotate",
        refused=["chickpeas"],
    )


# ---------------------------------------------------------------- the kitchen
@pytest.fixture
def kitchen(fixture_db: Db, two_persons, frozen_clock, config, tmp_path):
    """Both chats on the real poller. Sunday 13 Sep, 10:00, the cook about to start.

    The dev outbox drops everything not addressed to the owner; the harness flips THIS outbox to
    prod so her replies land in the fake Telegram too. Nothing leaves the process.
    """
    frozen_clock.set_local(dt.datetime(2026, 9, 13, 10, 0, tzinfo=TZ))
    secrets_dir = tmp_path / "s"
    secrets_dir.mkdir()
    (secrets_dir / "telegram-token-dev.txt").write_text("1:t", encoding="utf-8")
    tg = FakeTelegram()
    app = App(config, Secrets(secrets_dir), clock=frozen_clock, transport=tg)
    handlers = app.handlers(fixture_db)
    handlers.outbox.env = "prod"
    poller = Poller(
        app.api(),
        fixture_db,
        app.registry(fixture_db),
        handlers.handle,
        clock=frozen_clock,
        sleeper=lambda s: None,
    )
    n = {"i": 0}

    def _last(chat: int) -> dict:
        got = tg.sent_to(chat)
        assert got, f"nothing was sent to chat {chat}"
        return got[-1]

    def say(chat: int, text: str) -> dict:
        n["i"] += 1
        tg.queue_update(tg.text_update(n["i"], chat, text))
        poller.run_once()
        return _last(chat)

    def tap(chat: int, data: str) -> dict:
        n["i"] += 1
        tg.queue_update(tg.callback_update(n["i"], chat, data, message_id=n["i"]))
        poller.run_once()
        return _last(chat)

    return {
        "db": fixture_db,
        "owner": two_persons["owner"],
        "partner": two_persons["partner"],
        "clock": frozen_clock,
        "say": say,
        "tap": tap,
    }


def _taps(sent: dict) -> list[str]:
    markup = sent.get("reply_markup") or {}
    if isinstance(markup, str):
        markup = json.loads(markup)
    return [b["callback_data"] for row in markup.get("inline_keyboard", []) for b in row]


def _line_with(text: str, needle: str) -> str:
    return next((ln for ln in text.splitlines() if needle in ln), "")


# ---------------------------------------------------------------- 1. the whole sitting
def test_01_owner_starts_the_sunday_cook_and_sees_every_dish_and_box(kitchen) -> None:
    """
    which dishes, how many boxes of each, and what stays out of the cook."""
    db, pid, say = kitchen["db"], kitchen["owner"], kitchen["say"]
    _owner_week(db, pid)
    text = say(OWNER_CHAT, "/batch")["text"]
    assert "Batch cook Sun 13 Sep" in text, text
    assert "20 boxes" in text, text
    for dish in (
        "Warm chicken, green bean and potato salad: 5 boxes",
        "Garlic turkey with potato wedges: 5 boxes",
        "Strawberry protein yoghurt: 5 boxes",
        "Skyr with peanut butter: 5 boxes",
    ):
        assert dish in text, f"missing {dish!r}: {text}"
    assert "Breakfast stays fresh" in text
    assert "Oats" not in text, "breakfast is made fresh, so its oats are not in the cook"
    assert len(text.splitlines()) <= 12


# ---------------------------------------------------------------- 2. freezer or fridge
def test_02_owner_knows_which_boxes_go_in_the_freezer_and_which_in_the_fridge(kitchen) -> None:
    """Twenty filled boxes on the counter: the owner freezes, the dishes keep three days, and he
    needs to know which days go straight into the freezer before he stacks the fridge."""
    db, pid, say = kitchen["db"], kitchen["owner"], kitchen["say"]
    _owner_week(db, pid)
    text = say(OWNER_CHAT, "/batch")["text"]
    freeze = _line_with(text, "Freeze")
    assert freeze, f"no freezing line: {text}"
    for dish in ("Garlic turkey with potato wedges", "Warm chicken, green bean and potato salad"):
        assert f"{dish} Thu, Fri" in freeze, f"{dish}: Thu and Fri are 4-5 days old: {freeze}"
    assert "Wed" not in freeze, "Wednesday's box is three days old and keeps three"
    assert "Cook fresh" not in text, "he freezes, so nothing has to be cooked again mid-week"


# ---------------------------------------------------------------- 3. labels
def test_03_on_a_rotating_week_each_dish_says_which_days_boxes_it_fills(kitchen) -> None:
    """Sunday, the partner has six different dishes in six boxes and a marker pen. To label a box
    she needs the day it is for next to the dish, not just "1 box"."""
    db, pid, say = kitchen["db"], kitchen["partner"], kitchen["say"]
    _partner_profile(db, pid)
    rice = _ingredient(db, "rice", "Brown rice", aisle="grains")
    chicken = _ingredient(db, "chicken", "Chicken thigh")
    lunches = [
        _recipe(db, "Chicken and pepper wrap", {chicken: 120.0}),
        _recipe(db, "Curried lentil soup", {rice: 60.0}),
        _recipe(db, "Chicken rice bowl", {chicken: 100.0, rice: 70.0}),
    ]
    dinners = [
        _recipe(db, "Beef chilli", {rice: 80.0}),
        _recipe(db, "Chicken traybake", {chicken: 150.0}),
        _recipe(db, "Salmon and greens", {rice: 50.0}),
    ]
    rows = [(d, "lunch", lunches[i], 1.0) for i, d in enumerate(MON_WED)]
    rows += [(d, "dinner", dinners[i], 1.0) for i, d in enumerate(MON_WED)]
    _menu(db, pid, rows, decision="accepted")
    text = say(PARTNER_CHAT, "/batch")["text"]
    named = [("Chicken and pepper wrap", MON_WED[0]), ("Beef chilli", MON_WED[0])]
    named += [("Curried lentil soup", MON_WED[1]), ("Chicken traybake", MON_WED[1])]
    for dish, day in named:
        line = _line_with(text, dish)
        assert line, f"{dish} is not on the cook message: {text}"
        assert f"{day:%a}" in line, (
            f"the box of {dish} is for {day:%a} and the line does not say so - she cannot "
            f"label it: {line!r}"
        )


# ---------------------------------------------------------------- 4. thawing
def test_04_the_freeze_line_says_when_to_take_a_frozen_box_out(kitchen) -> None:
    """Sunday, the owner is putting Thursday's and Friday's boxes in the freezer. The one thing
    that decides whether Thursday's lunch is edible is moving it to the fridge the night
    before, and he wants that on the same line he is reading."""
    db, pid, say = kitchen["db"], kitchen["owner"], kitchen["say"]
    _owner_week(db, pid)
    text = say(OWNER_CHAT, "/batch full")["text"]
    assert "Freeze" in text, text
    words = ("thaw", "defrost", "take out", "night before", "move to the fridge")
    assert any(w in text.lower() for w in words), (
        "the cook message tells him to freeze Thu and Fri and never says when to take them "
        f"out: {text}"
    )


# ---------------------------------------------------------------- 5. reheating
def test_05_thursday_lunch_out_of_the_freezer_the_card_says_how_to_reheat(kitchen) -> None:
    """Thursday 12:40 at his desk, a box that was frozen on Sunday in his hand. He opens
    Lunch on the menu to find out how to heat it."""
    db, pid, say, tap = kitchen["db"], kitchen["owner"], kitchen["say"], kitchen["tap"]
    _owner_week(db, pid)
    kitchen["clock"].set_local(dt.datetime(2026, 9, 17, 12, 40, tzinfo=TZ))
    sent = say(OWNER_CHAT, "/menu")
    lunch = next((d for d in _taps(sent) if d.startswith("k:meal:") and ":lunch" in d), None)
    assert lunch, f"no Lunch button on /menu: {_taps(sent)}"
    card = tap(OWNER_CHAT, lunch)["text"]
    assert "Warm chicken, green bean and potato salad" in card, card
    words = ("reheat", "microwave", "thaw", "defrost", "eat cold", "serve cold")
    assert any(w in card.lower() for w in words), (
        f"the card for a box that came out of the freezer says nothing about heating it: {card}"
    )


# ---------------------------------------------------------------- 6. unequal boxes
def test_06_boxes_of_one_dish_in_different_sizes_say_how_much_goes_in_each(kitchen) -> None:
    """Sunday, one pot of lentil soup for Monday and Wednesday, the partner ladling it into two
    boxes. Her Monday portion is 0.8 and Wednesday's 0.7, so the boxes are not equal - she
    wants to know what goes in which one."""
    db, pid, say = kitchen["db"], kitchen["partner"], kitchen["say"]
    _partner_profile(db, pid)
    lentils = _ingredient(db, "red_lentils", "Red lentils", aisle="grains")
    chicken = _ingredient(db, "chicken", "Chicken thigh")
    soup = _recipe(db, "Curried chicken and lentil soup", {chicken: 100.0, lentils: 60.0})
    wrap = _recipe(db, "Tuna wrap", {chicken: 90.0})
    rows = [(MON_WED[0], "lunch", soup, 0.8), (MON_WED[2], "lunch", soup, 0.7)]
    rows += [(MON_WED[1], "lunch", wrap, 1.0)]
    _menu(db, pid, rows, decision="accepted")
    text = say(PARTNER_CHAT, "/batch full")["text"]
    assert "Curried chicken and lentil soup: 2 boxes" in text, text
    chicken_line = _line_with(text, "Chicken thigh")
    assert "80 g" in chicken_line and "70 g" in chicken_line, (
        "the soup's two boxes take 80 g and 70 g of chicken; the line gives her a total and "
        f"a box count and leaves the split to her: {chicken_line!r}"
    )


# ---------------------------------------------------------------- 7. the method
def test_07_every_dish_the_batch_names_opens_its_method_in_one_tap(kitchen) -> None:
    """
    one without leaving the message. Count: one tap per dish."""
    db, pid, say, tap = kitchen["db"], kitchen["partner"], kitchen["say"], kitchen["tap"]
    _partner_profile(db, pid)
    rice = _ingredient(db, "rice", "Brown rice", aisle="grains")
    lunches = [
        _recipe(db, "Chicken and pepper wrap", {rice: 50.0}, steps=["Fill the wrap."]),
        _recipe(db, "Curried lentil soup", {rice: 60.0}, steps=["Simmer the lentils."]),
    ]
    dinners = [
        _recipe(db, "Beef chilli", {rice: 80.0}, steps=["Brown the beef."]),
        _recipe(db, "Chicken traybake", {rice: 70.0}, steps=["Roast the tray."]),
    ]
    rows = [(MON_WED[0], "lunch", lunches[0], 1.0), (MON_WED[1], "lunch", lunches[1], 1.0)]
    rows += [(MON_WED[0], "dinner", dinners[0], 1.0), (MON_WED[1], "dinner", dinners[1], 1.0)]
    _menu(db, pid, rows, decision="accepted")
    sent = say(PARTNER_CHAT, "/batch")
    names = ["Chicken and pepper wrap", "Curried lentil soup", "Beef chilli", "Chicken traybake"]
    on_message = [n for n in names if n in sent["text"]]
    assert on_message == names, sent["text"]
    reached: set[str] = set()
    for data in _taps(sent):
        if data.startswith("k:meal:"):
            card = tap(PARTNER_CHAT, data)["text"]
            reached |= {n for n in names if n in card}
    missing = [n for n in names if n not in reached]
    assert not missing, (
        f"/batch names {len(names)} dishes and its buttons open {sorted(reached)}; no tap "
        f"reaches {missing}"
    )


# ---------------------------------------------------------------- 8. chickpeas
def test_08_partner_refused_chickpeas_and_the_cook_does_not_send_partner_to_cook_them(
    kitchen,
) -> None:
    """Sunday 10:00, the real state: she has asked three times for no chickpeas, and a
    chickpea-free menu for those days was built after the one with the salad in it. Since 15 Sep
    2026 the later menu is in force the moment it is built - there is no tap left owing on it -
    so the cook she is sent to do has to be that one. She sends /batch to start cooking."""
    db, pid, say = kitchen["db"], kitchen["partner"], kitchen["say"]
    _partner_profile(db, pid)
    chickpeas = _ingredient(db, "chickpeas", "Chickpeas", aisle="tins")
    db.insert(
        "ingredient_preferences", person_id=pid, ingredient_code="chickpeas", status="refused"
    )
    chicken = _ingredient(db, "chicken", "Chicken thigh")
    beans = _ingredient(db, "kidney_beans", "Kidney beans", aisle="tins")
    salad = _recipe(db, "Chicken, chickpea and feta salad", {chicken: 120.0, chickpeas: 80.0})
    curry = _recipe(db, "Chicken and red lentil curry", {chicken: 150.0})
    chilli = _recipe(db, "Beef and kidney bean chilli", {beans: 100.0})
    wrap = _recipe(db, "Chicken, kidney bean and pepper wrap", {chicken: 110.0, beans: 60.0})
    old = [(MON_WED[0], "lunch", salad, 0.8), (MON_WED[2], "lunch", salad, 0.7)]
    old += [(MON_WED[1], "lunch", wrap, 0.6)]
    old += [(d, "dinner", curry if i % 2 else chilli, 1.0) for i, d in enumerate(MON_WED)]
    _menu(db, pid, old, status="proposed", decision="accepted")
    new = [(d, "lunch", wrap, 0.85) for d in MON_WED]
    new += [(d, "dinner", chilli, 0.8) for d in MON_WED]
    _menu(db, pid, new, status="proposed", decision="accepted")
    text = say(PARTNER_CHAT, "/batch")["text"]
    assert "chickpea" not in text.lower(), (
        "she refused chickpeas and the menu in force for these days has none in it; the cook "
        f"still weighs them out and sends her to make the salad: {text}"
    )


# ---------------------------------------------------------------- 9. how long
def test_09_before_starting_owner_knows_how_long_the_cook_takes_and_what_goes_on_first(
    kitchen,
) -> None:
    """Sunday 10:00, the owner deciding whether the cook fits before lunch. He wants the length of
    the sitting and which dish to start first - the 35-minute oven tray, not the yoghurt."""
    db, pid, say = kitchen["db"], kitchen["owner"], kitchen["say"]
    _owner_week(db, pid)
    text = say(OWNER_CHAT, "/batch")["text"]
    timed = re.search(r"\d+\s*(min|minutes|hour|hours|h)\b", text)
    assert timed, (
        "every recipe carries prep_minutes (35, 30, 3, 3); the cook message gives neither a "
        f"length for the sitting nor an order to cook in: {text}"
    )


# ---------------------------------------------------------------- 10. Thu-Fri
def test_10_wednesday_evening_partner_asks_what_to_cook_for_thursday_and_friday(kitchen) -> None:
    """Wednesday 19:00, her second cook of the week. Her Thu-Fri has no menu; she sends
    /batch to find out what to cook tonight for Thursday and Friday."""
    db, pid, say = kitchen["db"], kitchen["partner"], kitchen["say"]
    _partner_profile(db, pid)
    rice = _ingredient(db, "rice", "Brown rice", aisle="grains")
    lunch = _recipe(db, "Chicken and pepper wrap", {rice: 50.0})
    dinner = _recipe(db, "Beef chilli", {rice: 80.0})
    rows = [(d, "lunch", lunch, 1.0) for d in MON_WED]
    rows += [(d, "dinner", dinner, 1.0) for d in MON_WED]
    _menu(db, pid, rows, decision="accepted")
    kitchen["clock"].set_local(dt.datetime(2026, 9, 16, 19, 0, tzinfo=TZ))
    text = say(PARTNER_CHAT, "/batch")["text"]
    assert "Mon 14" not in text, (
        "on Wednesday evening she is told to batch cook boxes for Monday and Tuesday, which "
        f"she has already eaten, instead of hearing that Thu-Fri has no menu: {text}"
    )
    assert "/cook" in text or "no menu" in text.lower(), text


# ---------------------------------------------------------------- 11. serves four
def test_11_a_stew_that_serves_four_fills_four_boxes_from_one_pot(kitchen) -> None:
    """
    one pot, not four, and the beans for one pot, split four ways."""
    db, pid, say = kitchen["db"], kitchen["owner"], kitchen["say"]
    _profile(db, pid, batch_slots=["dinner"], freezes=1, segments=[[0, 1, 2, 3, 4]], variety="same")
    beans = _ingredient(db, "black_beans", "Black beans", aisle="tins")
    stew = _recipe(db, "Bean stew", {beans: 800.0}, servings=4, keep_days=4, freezable=1)
    _menu(db, pid, [(d, "dinner", stew, 1.0) for d in MON_FRI[:4]], decision="accepted")
    text = say(OWNER_CHAT, "/batch full")["text"]
    assert "Bean stew: 4 boxes, 1 times the recipe" in text, text
    assert "Black beans 800 g across 4 boxes, 200 g each" in text, text
    assert "4 times the recipe" not in text


# ---------------------------------------------------------------- 12. two cooks
def test_12_cooking_side_by_side_each_sees_only_their_own_boxes(kitchen) -> None:
    """Sunday 10:00, both of them at the same counter, each with a phone. Each sends /batch
    and gets their own cook; neither message carries the other's dishes."""
    db, say = kitchen["db"], kitchen["say"]
    _owner_week(db, kitchen["owner"])
    partner = kitchen["partner"]
    _partner_profile(db, partner)
    rice = _ingredient(db, "rice", "Brown rice", aisle="grains")
    wrap = _recipe(db, "Chicken and pepper wrap", {rice: 50.0})
    chilli = _recipe(db, "Beef chilli", {rice: 80.0})
    rows = [(d, "lunch", wrap, 1.0) for d in MON_WED]
    rows += [(d, "dinner", chilli, 1.0) for d in MON_WED]
    _menu(db, partner, rows, decision="accepted")
    his = say(OWNER_CHAT, "/batch")["text"]
    partners = say(PARTNER_CHAT, "/batch")["text"]
    assert "Garlic turkey with potato wedges" in his and "Beef chilli" not in his, his
    assert "Beef chilli: 3 boxes" in partners and "Garlic turkey" not in partners, partners
    assert "6 boxes" in partners and "20 boxes" in his
