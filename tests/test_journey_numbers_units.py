"""Journey tests for ingredient quantities and pack units."""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import re
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tiffin.app import App
from tiffin.clock import TZ, FrozenClock
from tiffin.config import Config
from tiffin.db import Db
from tiffin.energy import Macros
from tiffin.food import menu as menu_mod
from tiffin.food import products, recipes
from tiffin.secrets import Secrets
from tiffin.telegram.poller import Poller
from tiffin.web.app import create_app

from .fakes import FakeTelegram
from .test_menu import proposal_for, seed_catalogue
from .web_source import app_source

OWNER_CHAT = 1000001
PARTNER_CHAT = 1000002
HEADER = "Tailscale-User-Login"
OWNER_LOGIN = "alex@example.com"

SUNDAY = dt.date(2026, 9, 13)
MONDAY = dt.date(2026, 9, 14)
TUESDAY = dt.date(2026, 9, 15)
SATURDAY = dt.date(2026, 9, 19)
MON_FRI = [MONDAY + dt.timedelta(days=i) for i in range(5)]
MON_WED = MON_FRI[:3]


# ---------------------------------------------------------------- the box
@dataclasses.dataclass
class Box:
    db: Db
    clock: FrozenClock
    config: Config
    owner_id: int
    partner_id: int
    say: Callable[[int, str], dict]
    tap: Callable[[int, str], dict]
    client: TestClient
    telegram: FakeTelegram

    def at(self, day: dt.date, hh: int, mm: int) -> None:
        self.clock.set_local(dt.datetime(day.year, day.month, day.day, hh, mm, tzinfo=TZ))

    def page(self, path: str) -> Any:
        return self.client.get(path, headers={HEADER: OWNER_LOGIN})

    def app_js(self) -> str:
        return app_source()


@pytest.fixture
def box(config: Config, tmp_path: Path) -> Iterator[Box]:
    """Both chats on the real poller and the owner on the real page, one database behind both."""
    clock = FrozenClock(dt.datetime(2026, 9, 14, 9, 0, tzinfo=TZ))
    config.paths.ensure()
    db = Db.open(config.paths.db, clock=clock)
    owner_id = db.insert("persons", slug="owner", display_name="Alex", tone="neutral")
    partner_id = db.insert("persons", slug="partner", display_name="Sam", tone="warm")
    db.insert("telegram_identities", person_id=owner_id, chat_id=OWNER_CHAT, method="config")
    db.insert("telegram_identities", person_id=partner_id, chat_id=PARTNER_CHAT, method="config")
    db.insert("tailscale_identities", person_id=owner_id, login=OWNER_LOGIN)
    secrets_dir = tmp_path / "s"
    secrets_dir.mkdir()
    (secrets_dir / "telegram-token-dev.txt").write_text("1:t", encoding="utf-8")
    config.data["web"]["secure_cookies"] = False
    config.data["web"]["root_path"] = ""
    tg = FakeTelegram()
    app = App(config, Secrets(secrets_dir), clock=clock, transport=tg)
    handlers = app.handlers(db)
    handlers.outbox.env = "prod"  # her replies land in the fake too; nothing leaves the process
    poller = Poller(
        app.api(), db, app.registry(db), handlers.handle, clock=clock, sleeper=lambda s: None
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

    client = TestClient(create_app(app), base_url="http://testserver")
    try:
        yield Box(db, clock, config, owner_id, partner_id, say, tap, client, tg)
    finally:
        db.close()


# ---------------------------------------------------------------- kitchen props
def _food(
    db: Db,
    code: str,
    name: str,
    kcal: float,
    protein: float,
    fat: float,
    carb: float,
    pack_g: float,
    pack_label: str,
    aisle: str,
) -> int:
    return db.insert(
        "ingredients",
        code=code,
        version=1,
        name=name,
        kcal_100=kcal,
        protein_100=protein,
        fat_100=fat,
        carb_100=carb,
        fibre_100=1.0,
        pack_g=pack_g,
        pack_label=pack_label,
        aisle=aisle,
        pantry=0,
        use_whole_pack=0,
        approved=1,
        person_scope="both",
    )


def _recipe(
    db: Db,
    name: str,
    grams: dict[int, float],
    steps: list[str],
    *,
    servings: int = 1,
    prep_minutes: int = 20,
    keep_days: int = 3,
    freezable: int = 0,
) -> int:
    rid = db.insert(
        "recipes",
        name=name,
        servings=servings,
        prep_minutes=prep_minutes,
        method_json=json.dumps(steps),
        source="model",
        person_scope="both",
        keep_days=keep_days,
        freezable=freezable,
    )
    for iid, g in grams.items():
        db.insert("recipe_ingredients", recipe_id=rid, ingredient_id=iid, grams=g)
    return rid


def _menu(
    db: Db,
    pid: int,
    rows: list[tuple[dt.date, str, int, float]],
    *,
    status: str = "proposed",
    decision: str | None = "accepted",
) -> int:
    """A menu from (day, slot, recipe, multiplier) rows, each slot's numbers per serving from the
    stored grams - the shape `store_plan` writes - and each day the sum of its slots."""
    days = sorted({r[0] for r in rows})
    menu_id = db.insert(
        "menus",
        person_id=pid,
        segment_start=days[0].isoformat(),
        segment_end=days[-1].isoformat(),
        status=status,
    )
    for day in days:
        mine = [r for r in rows if r[0] == day]
        numbers = [(r, recipes.stored_per_serving(db, r[2], r[3])) for r in mine]
        day_id = db.insert(
            "menu_days",
            menu_id=menu_id,
            day=day.isoformat(),
            kcal=sum(m.kcal for _, m in numbers),
            protein_g=round(sum(m.protein_g for _, m in numbers)),
            fat_g=round(sum(m.fat_g for _, m in numbers)),
            carb_g=round(sum(m.carb_g for _, m in numbers)),
            fibre_g=round(sum(m.fibre_g for _, m in numbers)),
        )
        for (_day, slot, rid, mult), m in numbers:
            db.insert(
                "meal_slots",
                menu_day_id=day_id,
                slot=slot,
                recipe_id=rid,
                portion_multiplier=mult,
                portion_g=m.total_g,
                kcal=m.kcal,
                protein_g=round(m.protein_g),
                fat_g=round(m.fat_g),
                carb_g=round(m.carb_g),
                fibre_g=round(m.fibre_g),
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


def _owner_profile(db: Db, pid: int) -> None:
    _profile(
        db,
        pid,
        batch_slots=["lunch", "dinner"],
        freezes=1,
        segments=[[0, 1, 2, 3, 4]],
        variety="same",
    )


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


def _owner_week(db: Db, pid: int) -> dict[str, int]:
    """His menu 6 as it is on the box: the same dishes Mon-Fri, lunch x0.85, dinner x0.95."""
    _owner_profile(db, pid)
    f = {
        "oats": _food(db, "oats", "Porridge oats", 379, 13, 8, 60, 1000, "1 kg bag", "grains"),
        "skyr": _food(db, "skyr", "Skyr, natural", 63, 11, 0.2, 4, 450, "450 g tub", "dairy"),
        "chicken": _food(
            db, "chicken", "Chicken breast, raw", 106, 24, 1.1, 0, 500, "500 g pack", "meat"
        ),
        "potato": _food(db, "potato", "Potatoes, raw", 77, 2, 0.1, 17, 2000, "2 kg bag", "produce"),
        "beans": _food(
            db, "green_beans", "Green beans, raw", 24, 1.9, 0.5, 3.2, 200, "200 g pack", "produce"
        ),
        "turkey": _food(
            db, "turkey", "Turkey breast, raw", 105, 24, 0.8, 0, 500, "500 g pack", "meat"
        ),
        "carrot": _food(
            db, "carrot", "Carrots, raw", 34, 0.6, 0.3, 7.9, 1000, "1 kg bag", "produce"
        ),
        "oil": _food(
            db, "olive_oil", "Olive oil", 899, 0, 99.9, 0, 460, "500 ml bottle", "condiments"
        ),
        "salt": _food(db, "salt", "Salt", 0, 0, 0, 0, 750, "750 g tub", "condiments"),
        "pepper": _food(
            db,
            "black_pepper",
            "Black pepper, ground",
            255,
            11,
            3.3,
            39,
            100,
            "100 g jar",
            "condiments",
        ),
    }
    dishes = {
        "breakfast": _recipe(
            db,
            "Overnight oats with skyr",
            {f["oats"]: 40, f["skyr"]: 150},
            ["Stir the oats into the skyr.", "Leave in the fridge overnight."],
            prep_minutes=5,
        ),
        "lunch": _recipe(
            db,
            "Warm chicken, green bean and potato salad",
            {
                f["chicken"]: 170,
                f["potato"]: 100,
                f["beans"]: 80,
                f["oil"]: 15,
                f["salt"]: 1,
                f["pepper"]: 1,
            },
            ["Boil the potatoes.", "Air fry the chicken.", "Blanch the beans.", "Dress and toss."],
            prep_minutes=30,
        ),
        "dinner": _recipe(
            db,
            "Garlic turkey with potato wedges",
            {
                f["turkey"]: 210,
                f["potato"]: 100,
                f["carrot"]: 100,
                f["oil"]: 17,
                f["salt"]: 1,
                f["pepper"]: 1,
            },
            ["Cut the wedges.", "Air fry the turkey and wedges.", "Roast the carrots."],
            prep_minutes=35,
        ),
    }
    mult = {"breakfast": 0.9, "lunch": 0.85, "dinner": 0.95}
    rows = [(d, slot, rid, mult[slot]) for d in MON_FRI for slot, rid in dishes.items()]
    dishes["menu"] = _menu(db, pid, rows, status="repeated", decision="accepted")
    return dishes


def _partner_week(db: Db, pid: int) -> dict[str, int]:
    """Her Mon-Wed, accepted, with the real eggs-and-bacon breakfast, the chickpea salad, a
    chilli and a soup made with stock."""
    _partner_profile(db, pid)
    f = {
        "eggs": _food(db, "eggs", "Eggs, whole, raw", 131, 12.6, 9, 0, 348, "6 eggs", "dairy"),
        "bacon": _food(
            db, "bacon", "Back bacon, fat trimmed, raw", 136, 20, 6, 0, 240, "8 rashers", "meat"
        ),
        "bread": _food(
            db, "bread", "Wholemeal bread", 222, 9.4, 2.5, 37, 800, "800 g loaf", "bakery"
        ),
        "spinach": _food(
            db, "spinach", "Spinach, baby, raw", 25, 2.8, 0.8, 1.6, 240, "240 g bag", "produce"
        ),
        "tomato": _food(
            db,
            "cherry_tomato",
            "Cherry tomatoes, raw",
            18,
            0.7,
            0.3,
            3,
            250,
            "250 g punnet",
            "produce",
        ),
        "cottage": _food(
            db, "cottage", "Cottage cheese", 98, 11, 4.3, 3.4, 300, "300 g tub", "dairy"
        ),
        "pepper": _food(
            db,
            "black_pepper",
            "Black pepper, ground",
            255,
            11,
            3.3,
            39,
            100,
            "100 g jar",
            "condiments",
        ),
        "chicken": _food(
            db, "chicken", "Chicken breast, raw", 106, 24, 1.1, 0, 500, "500 g pack", "meat"
        ),
        "chickpeas": _food(
            db, "chickpeas", "Chickpeas, tinned, drained", 115, 7, 2, 16, 240, "400 g tin", "tins"
        ),
        "feta": _food(db, "feta", "Feta cheese", 250, 16, 20, 1.5, 200, "200 g pack", "dairy"),
        "mince": _food(
            db, "mince", "Beef mince 5% fat, raw", 124, 21, 5, 0, 500, "500 g pack", "meat"
        ),
        "kidney": _food(
            db,
            "kidney_beans",
            "Kidney beans, tinned, drained",
            100,
            7,
            0.5,
            15,
            240,
            "400 g tin",
            "tins",
        ),
        "chopped": _food(
            db,
            "chopped_tomatoes",
            "Chopped tomatoes, tinned",
            20,
            1,
            0.1,
            3,
            400,
            "400 g tin",
            "tins",
        ),
        "chilli": _food(
            db,
            "chilli_flakes",
            "Chilli flakes, dried",
            282,
            12,
            14,
            50,
            40,
            "40 g jar",
            "condiments",
        ),
        "salt": _food(db, "salt", "Salt", 0, 0, 0, 0, 750, "750 g tub", "condiments"),
        "lentils": _food(
            db, "red_lentils", "Red lentils, dry", 318, 24, 1.3, 53, 500, "500 g bag", "grains"
        ),
        "carrot": _food(
            db, "carrot", "Carrots, raw", 34, 0.6, 0.3, 7.9, 1000, "1 kg bag", "produce"
        ),
        "stock": _food(
            db,
            "chicken_stock",
            "Chicken stock, ready made",
            5,
            0.9,
            0.1,
            0.2,
            500,
            "500 ml pouch",
            "tins",
        ),
    }
    r = {
        # the real recipe 95, grams as stored
        "eggs": _recipe(
            db,
            "Scrambled eggs, bacon and spinach on toast",
            {
                f["eggs"]: 116,
                f["bacon"]: 60,
                f["bread"]: 70,
                f["spinach"]: 50,
                f["tomato"]: 80,
                f["cottage"]: 100,
                f["pepper"]: 1,
            },
            [
                "Air fry the bacon.",
                "Scramble the eggs.",
                "Wilt the spinach.",
                "Toast the bread.",
                "Halve the tomatoes.",
                "Serve with the cottage cheese.",
            ],
            prep_minutes=15,
        ),
        "salad": _recipe(
            db,
            "Chicken, chickpea and feta salad",
            {f["chicken"]: 150, f["chickpeas"]: 80, f["feta"]: 30, f["salt"]: 1},
            ["Air fry the chicken.", "Drain the chickpeas.", "Toss with the feta."],
        ),
        "chilli": _recipe(
            db,
            "Beef and kidney bean chilli",
            {f["mince"]: 150, f["kidney"]: 80, f["chopped"]: 150, f["chilli"]: 0.5, f["salt"]: 1},
            ["Brown the mince.", "Add the tomatoes, beans and chilli.", "Simmer 20 minutes."],
            prep_minutes=40,
        ),
        # the real recipe 108's stock, which she pours rather than weighs
        "soup": _recipe(
            db,
            "Curried chicken, red lentil and carrot soup",
            {f["chicken"]: 80, f["lentils"]: 50, f["carrot"]: 120, f["stock"]: 250, f["salt"]: 1},
            [
                "Soften the carrots.",
                "Add the lentils and the stock.",
                "Simmer 20 minutes.",
                "Stir in the chicken.",
            ],
            prep_minutes=35,
        ),
    }
    rows = [
        (MON_WED[0], "breakfast", r["eggs"], 1.0),
        (MON_WED[0], "lunch", r["salad"], 0.8),
        (MON_WED[0], "dinner", r["chilli"], 0.8),
        (MON_WED[1], "breakfast", r["eggs"], 1.1),
        (MON_WED[1], "lunch", r["soup"], 1.35),
        (MON_WED[1], "dinner", r["chilli"], 0.8),
        (MON_WED[2], "breakfast", r["eggs"], 1.0),
        (MON_WED[2], "lunch", r["salad"], 0.7),
        (MON_WED[2], "dinner", r["soup"], 1.0),
    ]
    r["menu"] = _menu(db, pid, rows)
    return r


def _taps(sent: dict) -> list[str]:
    markup = sent.get("reply_markup") or {}
    if isinstance(markup, str):
        markup = json.loads(markup)
    return [b["callback_data"] for row in markup.get("inline_keyboard", []) for b in row]


def _meal_tap(sent: dict, slot: str) -> str:
    found = next((d for d in _taps(sent) if d.startswith("k:meal:") and f":{slot}" in d), None)
    assert found, f"no {slot} button on the message: {_taps(sent)}"
    return found


def _line_with(text: str, needle: str) -> str:
    return next((ln for ln in text.splitlines() if needle in ln), "")


def _grams(text: str) -> float:
    """'1.2 kg' or '722 g' -> grams, the way Tiffin writes kitchen amounts."""
    m = re.search(r"([\d.]+)\s*(kg|g)\b", text)
    assert m, f"no amount in {text!r}"
    return float(m.group(1)) * (1000 if m.group(2) == "kg" else 1)


def _solved_week(box: Box) -> int:
    """The owner's Mon-Fri built the way the Saturday job builds it: the model's dishes, macros from
    grams in code, the solver's portions, stored by `store_plan`, then accepted."""
    _owner_profile(box.db, box.owner_id)
    seed_catalogue(box.db)
    profile = box.db.one("SELECT * FROM profiles WHERE person_id = ?", (box.owner_id,))
    segment = menu_mod.Segment(MONDAY, MON_FRI[-1])
    plan = menu_mod.build_plan(
        box.db,
        box.owner_id,
        segment,
        proposal_for(segment),
        targets=Macros(2250, 200, 70, 200, 30),
        profile=profile,
        model_used="claude-opus-5",
        clock=box.clock,
    )
    menu_id = menu_mod.store_plan(box.db, plan)
    menu_mod.decide(box.db, menu_id, "accepted")
    return menu_id


# ---------------------------------------------------------------- 1. eggs
def test_01_partner_breakfast_card_says_how_many_eggs_not_grams_of_egg(box: Box) -> None:
    """Tuesday 07:10, the partner at the hob with the egg box open, reading the card for her
    scrambled eggs and bacon. Nobody weighs eggs: she wants to know how many to crack."""
    _partner_week(box.db, box.partner_id)
    box.at(TUESDAY, 7, 10)
    menu = box.say(PARTNER_CHAT, "/menu")
    card = box.tap(PARTNER_CHAT, _meal_tap(menu, "breakfast"))["text"]
    assert "Scrambled eggs, bacon and spinach on toast" in card, card
    eggs = _line_with(card, "Eggs, whole, raw")
    assert eggs, card
    outside_pack = re.sub(r"\([^)]*\)", "", eggs)
    counted = re.search(
        r"\b\d+(?:\.\d+)?\s*(?:x\s*)?(?:medium |large )?eggs?\b", outside_pack, re.I
    )
    assert counted, (
        f"the card gives egg as a weight - {eggs!r} - and the only count on the line is the box "
        "she bought; 128 g is two eggs and a bit, and she has to work that out at the hob"
    )


# ---------------------------------------------------------------- 2. a pinch
def test_02_a_pinch_of_chilli_is_not_printed_as_0_g(box: Box) -> None:
    """Monday 18:30, the partner making the chilli from its card. The recipe has half a gram of
    chilli flakes at her 0.8 portion; the card must not tell her to add nothing."""
    _partner_week(box.db, box.partner_id)
    box.at(MONDAY, 18, 30)
    menu = box.say(PARTNER_CHAT, "/menu")
    card = box.tap(PARTNER_CHAT, _meal_tap(menu, "dinner"))["text"]
    assert "Beef and kidney bean chilli" in card, card
    zero = [ln for ln in card.splitlines() if re.search(r"\s0 g\b", ln)]
    assert not zero, f"the card lists an ingredient at zero grams: {zero}"


# ---------------------------------------------------------------- 3. ml
def test_03_the_stock_for_partner_soup_is_given_in_ml_for_the_jug(box: Box) -> None:
    """Tuesday 12:10, the partner making the lentil soup at her 1.35 portion, a measuring jug in her
    hand and a pouch sold in millilitres. The card has to say how much stock to pour."""
    _partner_week(box.db, box.partner_id)
    box.at(TUESDAY, 12, 10)
    menu = box.say(PARTNER_CHAT, "/menu")
    card = box.tap(PARTNER_CHAT, _meal_tap(menu, "lunch"))["text"]
    stock = _line_with(card, "Chicken stock")
    assert stock, card
    outside_pack = re.sub(r"\([^)]*\)", "", stock)
    assert re.search(r"\d+\s*ml\b", outside_pack), (
        f"the card weighs a liquid - {stock!r} - so the only millilitres on the line are the "
        "pouch's; a jug reads ml"
    )


# ---------------------------------------------------------------- 4. the page, a batch recipe
def test_04_the_page_lists_one_boxs_grams_under_one_boxs_kcal(box: Box) -> None:
    """Monday 18:30, the owner on the page cooking the week's lunches: one pot that serves four.
    Under "lunch - 596 kcal" the ingredients must be one box's worth, or a person weighing out
    a single box puts four boxes of chicken in it."""
    _owner_profile(box.db, box.owner_id)
    chicken = _food(
        box.db, "chicken", "Chicken breast, raw", 165, 31, 3.6, 0, 500, "500 g pack", "meat"
    )
    rice = _food(box.db, "rice", "Basmati rice, dry", 355, 8, 1, 78, 1000, "1 kg bag", "grains")
    lunch = _recipe(
        box.db,
        "Chicken and rice boxes",
        {chicken: 800, rice: 300},
        ["Air fry the chicken.", "Cook the rice.", "Split into four boxes."],
        servings=4,
        freezable=1,
    )
    _menu(box.db, box.owner_id, [(d, "lunch", lunch, 1.0) for d in MON_FRI], status="repeated")
    box.at(MONDAY, 18, 30)
    data = box.page("api/menu").json()
    slot = next(s for s in data["days"][0]["slots"] if s["slot"] == "lunch")
    # what renderMenu() prints for each ingredient, read from the script the page loads
    js = box.app_js()
    expr = re.search(r"\$\{ing\.name\} \$\{([^}]*)\} g", js)
    assert expr is not None, "renderMenu prints each ingredient's grams"
    divides = "servings" in expr.group(1)
    per100 = {"Chicken breast, raw": 165, "Basmati rice, dry": 355}
    shown = {
        i["name"]: round(
            i["grams"] * slot["portion_multiplier"] / (slot.get("servings", 1) if divides else 1)
        )
        for i in slot["ingredients"]
    }
    implied = sum(g * per100[n] / 100 for n, g in shown.items())
    heading = round(slot["kcal"])
    assert abs(implied - heading) <= 0.15 * heading, (
        f"the page prints {shown} under '{heading} kcal' - {implied:.0f} kcal of food for one "
        f"lunch box, because renderMenu prints `{expr.group(1)}` and the recipe serves 4"
    )


# ---------------------------------------------------------------- 5. the page, the day
def test_05_the_page_day_total_is_the_sum_of_the_meals_under_it(box: Box) -> None:
    """Monday 08:00, the owner checks today on the page: the day's heading says so many kcal and the
    four meals under it each say theirs. Adding them up has to give the heading."""
    _solved_week(box)
    box.at(MONDAY, 8, 0)
    data = box.page("api/menu").json()
    wrong = []
    for day in data["days"]:
        heading = round(day["kcal"])
        meals = [round(s["kcal"]) for s in day["slots"]]
        if sum(meals) != heading:
            wrong.append(f"{day['day']}: heading {heading}, meals {meals} = {sum(meals)}")
    assert not wrong, "the day heading and its meals disagree: " + "; ".join(wrong)


# ---------------------------------------------------------------- 6. card and page
def test_06_the_bot_card_and_the_page_give_one_meal_the_same_kcal(box: Box) -> None:
    """Monday 12:40, the owner logging lunch: he taps Lunch in the bot and has the page open on the
    same day. The same meal, the same portion - the kcal has to be one number."""
    menu_id = _solved_week(box)
    box.at(MONDAY, 12, 40)
    page = box.page("api/menu").json()
    assert page["menu"]["id"] == menu_id
    monday = next(d for d in page["days"] if d["day"] == MONDAY.isoformat())
    sent = box.say(OWNER_CHAT, "/menu")
    wrong = []
    for slot in monday["slots"]:
        card = box.tap(OWNER_CHAT, _meal_tap(sent, slot["slot"]))["text"]
        said = re.search(r"^(\d+) kcal", card, re.M)
        assert said, card
        if int(said.group(1)) != round(slot["kcal"]):
            wrong.append(f"{slot['slot']}: card {said.group(1)}, page {round(slot['kcal'])}")
    assert not wrong, "the card and the page disagree about the same meal: " + "; ".join(wrong)


# ---------------------------------------------------------------- 7. salt each
def test_07_the_batch_does_not_ask_for_a_fraction_of_a_gram_of_salt_a_box(box: Box) -> None:
    """Sunday 10:00, the owner at the counter with ten boxes and a salt tub. /batch full is the list
    he weighs out from. "0.8 g each" is a number no kitchen scale reads, and five of them do
    not make the total printed beside them."""
    _owner_week(box.db, box.owner_id)
    box.at(SUNDAY, 10, 0)
    text = box.say(OWNER_CHAT, "/batch full")["text"]
    salt = _line_with(text, "Salt")
    assert salt, text
    fractions = re.findall(r"\b0\.\d+ g each", salt)
    assert not fractions, (
        f"the salt line asks for {fractions} per box - {salt!r} - where the salt goes in the pot"
    )


# ---------------------------------------------------------------- 8. from batch to the pot
def test_08_the_lunch_button_on_batch_opens_the_pot_not_one_box(box: Box) -> None:
    """Sunday 10:05, pot on, the owner reads "Warm chicken ... 5 boxes, 4.25 times the recipe" and
    taps Lunch on the same message to cook it. He is cooking five boxes; the card he lands on
    has to give the pot's amounts, or say it is one box of five."""
    _owner_week(box.db, box.owner_id)
    box.at(SUNDAY, 10, 5)
    batch = box.say(OWNER_CHAT, "/batch")
    assert (
        "Warm chicken, green bean and potato salad: 5 boxes, 4.25 times the recipe" in batch["text"]
    )
    full = box.say(OWNER_CHAT, "/batch full")["text"]
    chicken_pot = _grams(_line_with(full, "Chicken breast, raw"))
    card = box.tap(OWNER_CHAT, _meal_tap(batch, "lunch"))["text"]
    assert "Warm chicken, green bean and potato salad" in card, card
    on_card = _grams(_line_with(card, "Chicken breast, raw"))
    says_boxes = re.search(r"\b5 boxes\b|\bone box\b|\bper box\b|4\.25", card)
    assert on_card >= chicken_pot - 5 or says_boxes, (
        f"/batch has him cooking {chicken_pot:.0f} g of chicken and its Lunch button opens a "
        f"card for {on_card:.0f} g, with nothing saying it is one box of five:\n{card}"
    )


# ---------------------------------------------------------------- 9. shop covers the cook
def test_09_the_shopping_list_buys_enough_for_what_batch_says_to_cook(box: Box) -> None:
    """Sunday 11:00, in the shop with /shopping, then home to /batch full. Every pack count on
    the list has to hold at least what the cook tells him to weigh out."""
    _owner_week(box.db, box.owner_id)
    box.at(SUNDAY, 11, 0)
    shop = box.say(OWNER_CHAT, "/shopping")["text"]
    cook = box.say(OWNER_CHAT, "/batch full")["text"]
    short = []
    for name in ("Chicken breast, raw", "Turkey breast, raw", "Potatoes, raw"):
        line = _line_with(shop, name)
        m = re.search(rf"{re.escape(name)} (\d+) x ([\d.]+ (?:kg|g))", line)
        assert m, f"{name} is not on the list: {shop}"
        bought = int(m.group(1)) * _grams(m.group(2))
        needed = _grams(_line_with(cook, name).replace(name, ""))
        if bought < needed:
            short.append(f"{name}: buys {bought:.0f} g, cook needs {needed:.0f} g")
    assert not short, "; ".join(short)


# ---------------------------------------------------------------- 10. serves four, five boxes
def test_10_a_stew_for_four_across_five_boxes_says_how_much_to_make(box: Box) -> None:
    """Sunday 10:00, a bean stew written to serve four and five dinner boxes to fill. The two
    numbers on /batch - how many times the recipe and how many grams - must be one answer."""
    _owner_profile(box.db, box.owner_id)
    beans = _food(box.db, "black_beans", "Black beans", 110, 7, 0.5, 16, 400, "400 g tin", "tins")
    stew = _recipe(
        box.db, "Bean stew", {beans: 800}, ["Simmer the beans."], servings=4, keep_days=4
    )
    _menu(box.db, box.owner_id, [(d, "dinner", stew, 1.0) for d in MON_FRI])
    box.at(SUNDAY, 10, 0)
    text = box.say(OWNER_CHAT, "/batch full")["text"]
    assert "Bean stew: 5 boxes, 1.25 times the recipe" in text, text
    assert "Black beans 1 kg across 5 boxes, 200 g each" in text, text


# ---------------------------------------------------------------- 11. the pack
def test_11_the_card_names_the_pack_the_shopping_list_bought(box: Box) -> None:
    """Monday 12:40, the owner cooking lunch from the card with the chicken he bought on Sunday from
    the list: two 375 g packs of the product he chose. The card says which pack the grams come
    out of - it has to be the pack in his fridge."""
    _owner_week(box.db, box.owner_id)
    pid = products.record(
        box.db,
        ingredient_code="chicken",
        retailer=products.RETAILERS[0],
        retailer_sku="7890",
        name="British Chicken Breast Fillets 375g",
        source="manual",
        pack_g=375.0,
        pack_label="375 g pack",
        price_pence=450,
    )
    products.choose(box.db, box.owner_id, "chicken", pid)
    box.at(SUNDAY, 11, 0)
    shop = box.say(OWNER_CHAT, "/shopping")["text"]
    listed = _line_with(shop, "Chicken breast, raw")
    assert "375 g" in listed, f"the list bought the chosen 375 g pack: {listed!r}"
    box.at(MONDAY, 12, 40)
    menu = box.say(OWNER_CHAT, "/menu")
    card = box.tap(OWNER_CHAT, _meal_tap(menu, "lunch"))["text"]
    on_card = _line_with(card, "Chicken breast, raw")
    assert "500 g pack" not in on_card, (
        f"the list he shopped from says {listed!r}; the card tells him the grams come out of a "
        f"500 g pack: {on_card!r}"
    )


# ---------------------------------------------------------------- 12. cooked for two
def test_12_saturday_dinner_cooked_for_two_gives_grams_for_both_plates(box: Box) -> None:
    """Saturday 18:00, the weekend is cooked together: both menus have the same salmon traybake
    and the joint list bought for both. the owner opens Dinner to cook it - for two plates, his at
    1.2 and hers at 0.8 - and the card has to give him the grams for the tray, not one plate."""
    _owner_profile(box.db, box.owner_id)
    _partner_profile(box.db, box.partner_id)
    salmon = _food(
        box.db, "salmon", "Salmon fillets, raw", 208, 20, 13, 0, 240, "2 fillets", "fish"
    )
    potato = _food(
        box.db, "sweet_potato", "Sweet potato, raw", 86, 1.6, 0.1, 20, 1000, "1 kg bag", "produce"
    )
    tray = _recipe(
        box.db,
        "Salmon and sweet potato traybake",
        {salmon: 120, potato: 200},
        ["Roast the sweet potato 20 minutes.", "Add the salmon for 12 minutes."],
    )
    weekend = [SATURDAY, SATURDAY + dt.timedelta(days=1)]
    _menu(box.db, box.owner_id, [(d, "dinner", tray, 1.2) for d in weekend])
    _menu(box.db, box.partner_id, [(d, "dinner", tray, 0.8) for d in weekend])
    box.at(SATURDAY, 18, 0)
    menu = box.say(OWNER_CHAT, "/menu")
    card = box.tap(OWNER_CHAT, _meal_tap(menu, "dinner"))["text"]
    assert "Salmon and sweet potato traybake" in card, card
    salmon_g = _grams(_line_with(card, "Salmon fillets, raw"))
    both = re.search(r"\bfor (?:two|2)\b|\b2 (?:plates|portions)\b|Sam|both", card, re.I)
    assert salmon_g >= 235 or both, (
        f"the tray is for two plates (120 g x 1.2 + 120 g x 0.8 = 240 g of salmon, two fillets on "
        f"the joint list) and the card gives {salmon_g:.0f} g - his plate only:\n{card}"
    )
