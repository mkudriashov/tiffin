"""The batch cook on the tailnet page: ten moments with a pan on and a phone on the counter.

day we cook (I have skipped Monday and Tuesday cook, so will be doing only Wed-Fri)".

and rice boxes for every lunch (serves 4, 35 min, keeps 3 days) and a lentil dhal for every
dinner (serves 2, 15 min, keeps 4 days), with eggs on toast made fresh each morning. the partner has
her own Mon-Wed menu, so every read here also has to be his and not hers.

Two moments recur. Sunday 13 Sep 18:00 is the ordinary cook: the shopping is done, ten boxes to
fill, `/batch` in Telegram answers it today. Wednesday 16 Sep 18:00 is the moment he wrote
about: Sunday's cook never happened, Monday and Tuesday were eaten off plan, and what he wants
now is the Wed-Fri half of that same menu.

Each story drives the real API the way the SPA does, and where the page renders the answer it
reads `app.js` and asserts on the code path it takes. A number the bot gives and the page cannot
is a finding; so is a number neither can give.
"""

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
from tiffin.food import batch as batch_mod
from tiffin.secrets import Secrets
from tiffin.telegram import batchflow
from tiffin.telegram.poller import Poller
from tiffin.telegram.render import menu_link, page_link
from tiffin.web.app import create_app

from .fakes import FakeTelegram
from .web_source import app_source

HEADER = "Tailscale-User-Login"
OWNER_LOGIN = "alex@example.com"
PARTNER_LOGIN = "sam@example.com"
OWNER_CHAT = 1000001
PARTNER_CHAT = 1000002

SUNDAY = dt.date(2026, 9, 13)
MONDAY = dt.date(2026, 9, 14)
WEDNESDAY = dt.date(2026, 9, 16)
FRIDAY = dt.date(2026, 9, 18)
MON_FRI = [MONDAY + dt.timedelta(days=i) for i in range(5)]
WED_FRI = MON_FRI[2:]

LUNCH_METHOD = [
    "Season the chicken and air fry at 200C for 18 minutes.",
    "Cook the rice in one pan for 12 minutes.",
    "Divide the chicken and rice between the boxes.",
]
DINNER_METHOD = [
    "Simmer the lentils for 20 minutes.",
    "Stir the spinach through at the end.",
]


def at(day: dt.date, hh: int, mm: int) -> dt.datetime:
    return dt.datetime(day.year, day.month, day.day, hh, mm, tzinfo=TZ)


# -- the kitchen -------------------------------------------------------------------------------


def _ingredient(db: Db, code: str, name: str, aisle: str) -> int:
    return db.insert(
        "ingredients",
        code=code,
        version=1,
        name=name,
        kcal_100=150.0,
        protein_100=12.0,
        fat_100=6.0,
        carb_100=10.0,
        fibre_100=1.0,
        pack_g=500.0,
        pack_label="500 g pack",
        aisle=aisle,
        pantry=0,
        use_whole_pack=0,
        approved=1,
        person_scope="both",
    )


def _recipe(
    db: Db,
    name: str,
    method: list[str],
    grams: dict[int, float],
    *,
    servings: int,
    keep_days: int,
    prep_minutes: int,
) -> int:
    rid = db.insert(
        "recipes",
        name=name,
        servings=servings,
        prep_minutes=prep_minutes,
        method_json=json.dumps(method),
        source="model",
        keep_days=keep_days,
        freezable=0,  # his own "yes I freeze" answer is what decides it, not the model's guess
    )
    for iid, g in grams.items():
        db.insert("recipe_ingredients", recipe_id=rid, ingredient_id=iid, grams=g)
    return rid


def _menu(db: Db, person_id: int, days: list[dt.date], slots: dict[str, int], status: str) -> int:
    mid = db.insert(
        "menus",
        person_id=person_id,
        segment_start=days[0].isoformat(),
        segment_end=days[-1].isoformat(),
        status=status,
    )
    for day in days:
        did = db.insert(
            "menu_days",
            menu_id=mid,
            day=day.isoformat(),
            kcal=2000,
            protein_g=160.0,
            fat_g=65.0,
            carb_g=190.0,
            fibre_g=30.0,
        )
        for slot, rid in slots.items():
            db.insert(
                "meal_slots",
                menu_day_id=did,
                slot=slot,
                recipe_id=rid,
                portion_multiplier=1.0,
                portion_g=400.0,
                kcal=600,
                protein_g=45.0,
                fat_g=18.0,
                carb_g=55.0,
                fibre_g=7.0,
            )
    return mid


def _profile(db: Db, person_id: int, *, freezes: int, segments: str) -> None:
    db.insert(
        "profiles",
        person_id=person_id,
        effective_from="2026-09-01",
        sex="m",
        birth_date="1990-03-15",
        height_cm=183.0,
        activity_factor=1.55,
        kcal_floor=1800,
        steps_floor=9000,
        batch_slots='["lunch","dinner"]',
        freezes=freezes,
        segment_days=segments,
        variety_json='{"all": "same"}',
    )


@dataclasses.dataclass
class Box:
    """The page and the bot at one moment, over one database."""

    client: TestClient
    db: Db
    config: Config
    clock: FrozenClock
    owner_id: int
    partner_id: int
    menu_id: int
    recipes: dict[str, int]
    say: Callable[[int, str], dict[str, Any]]

    def as_owner(self, path: str, **kw: Any):
        return self.client.get(path, headers={HEADER: OWNER_LOGIN}, **kw)

    def as_partner(self, path: str, **kw: Any):
        return self.client.get(path, headers={HEADER: PARTNER_LOGIN}, **kw)

    def post_as_owner(self, path: str, body: dict[str, Any]):
        me = self.as_owner("api/me")
        token = me.json().get("csrf", "")
        return self.client.post(
            path,
            json=body,
            headers={HEADER: OWNER_LOGIN, "X-CSRF-Token": token, "Sec-Fetch-Site": "same-origin"},
        )

    def shell(self) -> str:
        return self.as_owner("").text

    def app_js(self) -> str:
        """The script the shell actually loads, by the hashed name written into it."""
        return app_source()

    def load(self, today: dt.date) -> batch_mod.Load:
        """What `/batch` computes for his cook on that day: the numbers the page has to match."""
        menu = self.db.one("SELECT * FROM menus WHERE id = ?", (self.menu_id,))
        return batch_mod.plan(self.db, menu, ("lunch", "dinner"), today, freezes=True)

    def bot_batch(self, today: dt.date, *, full: bool = True) -> str:
        """The text of the `/batch` message for that cook day, rendered by the bot's own code."""
        flow = batchflow.BatchFlow(clock=self.clock)
        return flow._render(self.load(today), full=full)


@pytest.fixture
def open_box(config: Config, tmp_path: Path) -> Iterator[Callable[..., Box]]:
    opened: list[Db] = []

    def make(when: dt.datetime) -> Box:
        clock = FrozenClock(when)
        config.paths.ensure()
        db = Db.open(config.paths.db, clock=clock)
        opened.append(db)
        owner_id = db.insert("persons", slug="owner", display_name="Alex", tone="blunt")
        partner_id = db.insert("persons", slug="partner", display_name="Sam", tone="warm")
        db.insert("tailscale_identities", person_id=owner_id, login=OWNER_LOGIN)
        db.insert("tailscale_identities", person_id=partner_id, login=PARTNER_LOGIN)
        db.insert("telegram_identities", person_id=owner_id, chat_id=OWNER_CHAT, method="config")
        db.insert(
            "telegram_identities", person_id=partner_id, chat_id=PARTNER_CHAT, method="config"
        )
        _profile(db, owner_id, freezes=1, segments="[[0,1,2,3,4]]")
        _profile(db, partner_id, freezes=0, segments="[[0,1,2],[3,4]]")

        chicken = _ingredient(db, "chicken", "Chicken breast, raw", "meat")
        rice = _ingredient(db, "rice", "Rice, basmati, dry", "grains")
        lentils = _ingredient(db, "lentils", "Lentils, red, dry", "grains")
        spinach = _ingredient(db, "spinach", "Spinach, baby", "veg")
        oil = _ingredient(db, "oil", "Olive oil", "condiments")
        eggs = _ingredient(db, "eggs", "Eggs, medium", "dairy")
        bread = _ingredient(db, "bread", "Bread, wholemeal", "bakery")

        r: dict[str, int] = {}
        r["breakfast"] = _recipe(
            db,
            "Eggs on toast",
            ["Toast the bread.", "Scramble the eggs."],
            {eggs: 120.0, bread: 80.0},
            servings=1,
            keep_days=1,
            prep_minutes=8,
        )
        r["lunch"] = _recipe(
            db,
            "Chicken and rice boxes",
            LUNCH_METHOD,
            {chicken: 800.0, rice: 320.0, oil: 20.0},
            servings=4,
            keep_days=3,
            prep_minutes=35,
        )
        r["dinner"] = _recipe(
            db,
            "Lentil dhal",
            DINNER_METHOD,
            # 300 g of lentils, servings=2, over five dinners totals 750 g - MORE than the 650 g
            # of rice test_05 names as the second-heaviest amount on the page. The story's own
            # words are "1 kg of chicken, 650 g of rice, heaviest first"; 250 g keeps every other
            # assertion in this file unchanged (nothing else names the lentil total) and puts
            # rice back in second place, which is the setup the story actually describes.
            {lentils: 250.0, spinach: 120.0, rice: 100.0},
            servings=2,
            keep_days=4,
            prep_minutes=15,
        )
        menu_id = _menu(
            db,
            owner_id,
            MON_FRI,
            {"breakfast": r["breakfast"], "lunch": r["lunch"], "dinner": r["dinner"]},
            "repeated",
        )
        # hers, so every read on his page has something to be wrong about
        _menu(db, partner_id, MON_FRI[:3], {"lunch": r["dinner"]}, "repeated")

        secrets_dir = tmp_path / "s"
        secrets_dir.mkdir(exist_ok=True)
        (secrets_dir / "telegram-token-dev.txt").write_text("1:t", encoding="utf-8")
        config.data["web"]["secure_cookies"] = False
        config.data["web"]["root_path"] = ""
        tg = FakeTelegram()
        app = App(config, Secrets(secrets_dir), clock=clock, transport=tg)
        handlers = app.handlers(db)
        handlers.outbox.env = "prod"
        poller = Poller(
            app.api(),
            db,
            app.registry(db),
            handlers.handle,
            clock=clock,
            sleeper=lambda s: None,
        )
        n = {"i": 0}

        def say(chat: int, text: str) -> dict[str, Any]:
            n["i"] += 1
            tg.queue_update(tg.text_update(n["i"], chat, text))
            poller.run_once()
            got = tg.sent_to(chat)
            assert got, f"nothing was sent to chat {chat}"
            return got[-1]

        client = TestClient(create_app(app), base_url="http://testserver")
        return Box(client, db, config, clock, owner_id, partner_id, menu_id, r, say)

    yield make
    for db in opened:
        db.close()


def _batch_json(box: Box, query: str = "") -> dict[str, Any]:
    """What the Batch tab would fetch. Returns {} with the refusal when there is no such tab."""
    response = box.as_owner(f"api/batch{query}")
    if response.status_code != 200:
        return {"__status": response.status_code, "__body": response.text[:200]}
    return response.json()


def _tabs(shell: str) -> list[str]:
    return re.findall(r'data-(?:tab|view)="([a-z]+)"', shell)


def _render_keys(app_js: str) -> list[str]:
    found = re.search(r"const RENDER = \{(.*?)\};", app_js, re.S)
    return re.findall(r"^\s*([a-z]+):", found.group(1), re.M) if found else []


# -- 1. is there anywhere on the page to stand -------------------------------------------------


def test_01_wednesday_evening_owner_opens_the_page_to_cook_and_looks_for_the_batch(
    open_box,
) -> None:
    """Wednesday 16 Sep, 18:00, coat off, about to start. He opens the page on his phone to cook
    from it. Somewhere on it there has to be the cook: dishes, pots, boxes - not seven tabs of
    per-day eating. One tap from wherever the page opens, because his hands are about to be
    covered in chicken."""
    box = open_box(at(WEDNESDAY, 18, 0))
    tabs = _tabs(box.shell())
    assert "batch" in tabs, (
        f"the page offers {tabs} and no cook: standing in the kitchen there is no tab to open"
    )
    assert "batch" in _render_keys(box.app_js()), "a Batch tab that renders nothing is not a tab"


# -- 2. the moment he actually wrote about ------------------------------------------------------


def test_02_owner_skipped_the_sunday_cook_and_the_bot_has_nothing_left_to_give_him(
    open_box,
) -> None:
    """Wednesday 18:00. Sunday's cook never happened - Monday and Tuesday were eaten off plan -
    and he is cooking tonight for Wed, Thu and Fri. `/batch` in Telegram is built around a cook
    day BEFORE the segment starts, so mid-week it refuses outright. If the bot has no answer on
    the one evening he asked about, the page is the only route left."""
    box = open_box(at(WEDNESDAY, 18, 0))
    said = box.say(OWNER_CHAT, "/batch")["text"]
    assert "No menu to cook from" in said, (
        f"the premise of this story is that mid-week /batch refuses; it said:\n{said}"
    )
    data = _batch_json(box)
    assert "__status" not in data, (
        f"and the page answers api/batch with {data.get('__status')} {data.get('__body')!r} - "
        "on Wednesday evening neither surface can tell him what to cook"
    )


# -- 3. picking the day the cook starts from ----------------------------------------------------


def test_03_owner_picks_wednesday_as_the_day_the_cook_starts_from(open_box) -> None:
    """Wednesday 18:00, the ask in his own words: "select since which day we cook (I have
    skipped Monday and Tuesday cook, so will be doing only Wed-Fri)". Choosing Wednesday has to
    drop Monday's and Tuesday's boxes from every number on the view - six boxes, not ten."""
    box = open_box(at(WEDNESDAY, 18, 0))
    whole_week = box.load(WEDNESDAY)
    assert whole_week.boxes == 10, f"ten boxes over five days, not {whole_week.boxes}"
    assert [d for dish in whole_week.dishes for d in dish.days][:1] == [MONDAY], (
        "the cook plan still counts Monday's box even when asked for on Wednesday, so there is "
        "nothing underneath a from-day control either"
    )
    data = _batch_json(box, f"?from={WEDNESDAY.isoformat()}")
    assert "__status" not in data, (
        f"the page cannot be asked to start the cook on a day: api/batch?from= is "
        f"{data.get('__status')}"
    )
    days = sorted({d for dish in data.get("dishes", []) for d in dish.get("days", [])})
    assert days == [d.isoformat() for d in WED_FRI], f"it still plans {days}"
    assert data.get("boxes") == 6, f"six boxes for Wed-Fri, not {data.get('boxes')}"


# -- 4. the same numbers as Telegram ------------------------------------------------------------


def test_04_the_page_gives_the_same_cook_as_batch_in_telegram(open_box) -> None:
    """Sunday 13 Sep, 18:00, the ordinary cook. He has `/batch` open in Telegram and the page
    open beside it. Two surfaces over one menu that disagree about how many boxes or how much
    chicken is worse than one: in the kitchen he would have to decide which to believe."""
    box = open_box(at(SUNDAY, 18, 0))
    said = box.say(OWNER_CHAT, "/batch full")["text"]
    assert "10 boxes" in said and "Chicken and rice boxes: 5 boxes" in said, said
    data = _batch_json(box)
    assert "__status" not in data, (
        f"api/batch is {data.get('__status')}: the bot has the cook and the page has no copy of it"
    )
    assert data.get("boxes") == 10, f"the page says {data.get('boxes')} boxes, the bot says 10"
    by_name = {d["name"]: d for d in data.get("dishes", [])}
    assert set(by_name) == {"Chicken and rice boxes", "Lentil dhal"}, sorted(by_name)
    assert by_name["Chicken and rice boxes"]["boxes"] == 5
    assert by_name["Lentil dhal"]["boxes"] == 5


# -- 5. how much of everything -------------------------------------------------------------------


def test_05_owner_wants_the_total_of_each_ingredient_in_kitchen_units(open_box) -> None:
    """Sunday 18:00, bags on the floor. Before anything goes in a pan he wants the totals: 1 kg
    of chicken, 650 g of rice, heaviest first, so he can get it all out at once instead of
    reading five days of menu and adding up in his head."""
    box = open_box(at(SUNDAY, 18, 0))
    amounts = {a.name: a.grams for a in box.load(SUNDAY).amounts}
    assert amounts["Chicken breast, raw"] == 1000.0 and amounts["Rice, basmati, dry"] == 650.0
    data = _batch_json(box)
    assert "__status" not in data, f"api/batch is {data.get('__status')}: no totals on the page"
    shown = data.get("amounts", [])
    assert [a["name"] for a in shown][:2] == ["Chicken breast, raw", "Rice, basmati, dry"], (
        f"heaviest first; the page gives {[a.get('name') for a in shown]}"
    )
    first = shown[0]
    assert first.get("grams") == 1000.0
    assert first.get("text") == "1 kg" or "1 kg" in json.dumps(first), (
        f"nobody weighs out 1000 g of chicken; the bot writes it as 1 kg and the page gives {first}"
    )


# -- 6. one pot at a time -------------------------------------------------------------------------


def test_06_each_pot_says_its_method_and_how_to_split_it_into_boxes(open_box) -> None:
    """Sunday 18:30, the chicken is out and the pan is hot. He taps the dish and wants the pot:
    the method once, 1.25 times the recipe, and "split into 5 boxes of about 280 g" - not one
    box's card, which is what the Menu tab gives him five times over."""
    box = open_box(at(SUNDAY, 18, 0))
    dish = next(d for d in box.load(SUNDAY).dishes if d.name == "Chicken and rice boxes")
    assert dish.multiples == 1.25 and dish.boxes == 5
    data = _batch_json(box)
    assert "__status" not in data, f"api/batch is {data.get('__status')}: there is no pot to open"
    pot = next((d for d in data.get("dishes", []) if d["name"] == dish.name), None)
    assert pot is not None, f"the cook names {[d.get('name') for d in data.get('dishes', [])]}"
    assert pot.get("method") == LUNCH_METHOD, "the pot carries every step, once, for the whole pot"
    assert pot.get("multiples") == 1.25, f"{pot.get('multiples')} times the recipe"
    assert pot.get("per_box_g"), f"no grams per box on the pot: {sorted(pot)}"


# -- 6b. the amounts for the pot in front of you ---------------------------------------------------


def test_06b_the_pot_says_how_much_of_each_thing_goes_in_it(open_box) -> None:
    """Sunday 18:35, the pan is hot and he is holding a bag of rice over the scales.

    The tab gave him the method and the box count and no amounts at all: step one said "boil the
    rice" and nothing anywhere on the dish said how much rice. The whole-cook totals existed, but
    a screen and a half further down and added up across every dish in the sitting, which is a
    shopping figure - it answers "how much rice does the evening need", not "how much goes in
    this pan". A cook can guess the method from the dish's name; he cannot guess 812 g.
    """
    box = open_box(at(SUNDAY, 18, 0))
    dish = next(d for d in box.load(SUNDAY).dishes if d.name == "Chicken and rice boxes")
    data = _batch_json(box)
    assert "__status" not in data, f"api/batch is {data.get('__status')}"
    pot = next(d for d in data["dishes"] if d["name"] == dish.name)

    amounts = pot.get("pot") or []
    assert amounts, f"the pot carries its method and not its amounts: {sorted(pot)}"
    by_name = {a["name"]: a for a in amounts}
    assert set(by_name) == {i["name"] for i in pot["ingredients"]}, (
        "every ingredient of the dish is weighed, not a subset"
    )

    # the POT's grams, which is the recipe's own scaled by what this cook needs - not the one
    # portion the Menu tab shows, and not the sitting's total across every dish
    for i in pot["ingredients"]:
        want = round(float(i["grams"]) * pot["multiples"], 1)
        assert by_name[i["name"]]["grams"] == want, (
            f"{i['name']}: {by_name[i['name']]['grams']} is not {i['grams']} x {pot['multiples']}"
        )
    assert by_name["Chicken breast, raw"]["grams"] > float(
        next(i["grams"] for i in pot["ingredients"] if i["name"] == "Chicken breast, raw")
    ), "1.25 times the recipe is more chicken than the recipe"

    # kitchen units, the bot's own: `weigh()` renders them once so both surfaces round the same
    assert all(a.get("text") for a in amounts), f"no kitchen units on the amounts: {amounts}"
    assert by_name["Chicken breast, raw"]["text"].endswith(("g", "kg")), by_name

    # and it is above the steps on the page, because that is the order a cook needs them in
    js = box.app_js()
    start = js.index("async function renderBatch(")
    rest = js[start:]
    cut = rest.find("\n/* ---")
    source = rest if cut < 0 else rest[:cut]
    assert "dish.pot" in source, f"the tab never reads the pot's amounts:\n{source}"
    assert source.index("dish.pot") < source.index("dish.method"), (
        "the amounts render below the method; he weighs before he cooks"
    )


# -- 6c. into the pan, and then into the boxes -----------------------------------------------------


def test_06c_every_amount_says_what_goes_in_one_box_as_well_as_the_pan(open_box) -> None:
    """Sunday 18:40, five open boxes on the counter and a pan of rice.

    The pot total is what went in the pan. It is not what he needs now: he needs the number that
    goes in each box, and dividing 812 by 5 over a hot pan is how boxes end up uneven. The split
    was already worked out - `batch.DishShare` carries it, and the bot prints it - but the tab
    put PER box").
    """
    box = open_box(at(SUNDAY, 18, 0))
    data = _batch_json(box)
    pot = next(d for d in data["dishes"] if d["name"] == "Chicken and rice boxes")
    rice = next(a for a in pot["pot"] if a["name"] == "Rice, basmati, dry")

    assert rice["boxes"] == pot["boxes"], "the ingredient is divided between this dish's own boxes"
    assert rice["per_box"] == round(rice["grams"] / rice["boxes"], 1), (
        f"{rice['grams']} g across {rice['boxes']} boxes is not {rice['per_box']} a box"
    )
    assert rice["per_box_text"], f"no kitchen units on the per-box figure: {rice}"
    assert rice["uniform"] is True, "these five boxes are the same size"

    js = box.app_js()
    start = js.index("async function renderBatch(")
    rest = js[start:]
    cut = rest.find("\n/* ---")
    source = rest if cut < 0 else rest[:cut]
    assert "splitText" in source, f"the tab never renders the split:\n{source}"


# -- 6d. raw in, cooked out ------------------------------------------------------------------------


def test_06d_what_came_out_of_the_air_fryer_is_what_gets_split(open_box) -> None:
    """
    then goes into 400 g cooked turkey."

    The recipe's grams are raw, and the boxes get what came OUT of the pan. Only he can know that
    number, so he types it once; Tiffin divides it and writes the weighing down, because a
    yield belongs to the food rather than to tonight and the next cook can then say what to
    expect before the air fryer is on. Macros are untouched - the catalogue is raw per 100 g and
    water leaving a pan takes no calories with it.
    """
    box = open_box(at(SUNDAY, 18, 0))
    data = _batch_json(box)
    pot = next(d for d in data["dishes"] if d["name"] == "Chicken and rice boxes")
    chicken = next(a for a in pot["pot"] if a["name"] == "Chicken breast, raw")
    assert chicken["code"], (
        f"the amount carries no ingredient code to file a yield under: {chicken}"
    )
    assert chicken.get("yield") is None, "nothing has been weighed yet"

    said = box.post_as_owner(
        "api/batch/yield",
        {
            "recipe_id": pot["recipe_id"],
            "ingredient_code": chicken["code"],
            "raw_g": chicken["grams"],
            "cooked_g": chicken["grams"] * 2 / 3,
        },
    )
    assert said.status_code == 200, said.text
    assert said.json()["pct"] == 67, said.json()

    again = next(
        a
        for a in next(d for d in _batch_json(box)["dishes"] if d["recipe_id"] == pot["recipe_id"])[
            "pot"
        ]
        if a["name"] == "Chicken breast, raw"
    )
    assert again["yield"]["pct"] == 67, f"the weighing was not kept: {again}"
    assert again["yield"]["raw_g"] == chicken["grams"], again

    rows = box.db.all("SELECT raw_g, cooked_g, recipe_id FROM cook_yields")
    assert len(rows) == 1 and rows[0]["recipe_id"] == pot["recipe_id"], [dict(r) for r in rows]

    # a yield drifts week to week (the owner), so last time's number is offered and never assumed:
    # one tap fills the box with it, and filling the box writes nothing by itself
    js = box.app_js()
    calc = js[js.index("function cookedCalculator(") :]
    calc = calc[: calc.index("\n}")]
    assert "Use last time" in calc, f"no way to take last week's number:\n{calc}"
    assert calc.index("Use last time") < calc.index("api/batch/yield"), (
        "the prefill is not a separate control from the one that records a new weighing"
    )
    fills = calc[calc.index("again.addEventListener") : calc.index("cooked.addEventListener")]
    assert "api/batch/yield" not in fills, (
        "taking last time's number writes a weighing that never happened, and the next cook is "
        f"then told a number nobody measured:\n{fills}"
    )

    # and a second, different weighing is what the next cook reads: latest row wins
    second = box.post_as_owner(
        "api/batch/yield",
        {
            "recipe_id": pot["recipe_id"],
            "ingredient_code": chicken["code"],
            "raw_g": chicken["grams"],
            "cooked_g": chicken["grams"] * 0.5,
        },
    )
    assert second.status_code == 200 and second.json()["pct"] == 50, second.text
    latest = next(
        a
        for a in next(d for d in _batch_json(box)["dishes"] if d["recipe_id"] == pot["recipe_id"])[
            "pot"
        ]
        if a["name"] == "Chicken breast, raw"
    )
    assert latest["yield"]["pct"] == 50, f"the older weighing is still being shown: {latest}"
    assert box.db.scalar("SELECT COUNT(*) FROM cook_yields") == 2, "both weighings are kept"


def test_06e_a_yield_for_a_food_this_cook_does_not_use_is_refused(open_box) -> None:
    """The route takes an ingredient code, and a code is the kind of thing a client can invent.

    A yield filed against a food he is not cooking is not a harmless row: every later estimate
    for that food reads it back. Scoped like every other write on this page - the menu comes
    from the person and today, and the food has to be one the dish actually contains.
    """
    box = open_box(at(SUNDAY, 18, 0))
    pot = next(d for d in _batch_json(box)["dishes"] if d["name"] == "Chicken and rice boxes")
    said = box.post_as_owner(
        "api/batch/yield",
        {
            "recipe_id": pot["recipe_id"],
            "ingredient_code": "caviar",
            "raw_g": 600,
            "cooked_g": 400,
        },
    )
    assert said.status_code == 404, said.text
    assert box.db.scalar("SELECT COUNT(*) FROM cook_yields") == 0

    typo = box.post_as_owner(
        "api/batch/yield",
        {
            "recipe_id": pot["recipe_id"],
            "ingredient_code": "chicken",
            "raw_g": 600,
            "cooked_g": 0,
        },
    )
    assert typo.status_code == 400, "nothing comes out of a pan weighing nothing"
    assert box.db.scalar("SELECT COUNT(*) FROM cook_yields") == 0


# -- 6f. what the food is, not only how much of it ------------------------------------------------


def test_06f_each_amount_keeps_per_100_g_one_tap_away(open_box) -> None:
    """Sunday 18:45, weighing out, and again on Wednesday in the shop with a different brand.

    from that whether the tub in his hand in the shop is the same thing the menu was built from,
    and the numbers were right there in the catalogue all along: they are exactly what the day's
    macros were computed from. One dish toggle now shows them when matching a pack in the shop.
    """
    box = open_box(at(SUNDAY, 18, 0))
    pot = next(d for d in _batch_json(box)["dishes"] if d["name"] == "Chicken and rice boxes")
    chicken = next(a for a in pot["pot"] if a["name"] == "Chicken breast, raw")
    assert chicken.get("kcal_100"), f"the amount carries no per-100 g: {sorted(chicken)}"
    assert chicken.get("protein_100") is not None, chicken

    js = box.app_js()
    start = js.index("async function renderBatch(")
    rest = js[start:]
    cut = rest.find("\n/* ---")
    assert "per100Toggle(dish.pot)" in (rest if cut < 0 else rest[:cut])
    assert "function per100(" in js, "no shared formatter, so three tabs will word it three ways"


# -- 6g. two boxes of one dish that are not the same size ------------------------------------


def test_06g_an_uneven_pot_is_split_unevenly_cooked_as_well_as_raw(open_box) -> None:
    """Unequal portions retain their proportions after a cooked weight is entered."""
    box = open_box(at(SUNDAY, 18, 0))
    pot = next(d for d in _batch_json(box)["dishes"] if d["name"] == "Chicken and rice boxes")
    chicken = next(a for a in pot["pot"] if a["name"] == "Chicken breast, raw")
    assert "box_grams" in chicken, (
        f"the split is only rendered text, so a cooked weight cannot be apportioned: {chicken}"
    )
    assert [round(g, 1) for g in chicken["box_grams"]] or chicken["uniform"], chicken
    if chicken["box_grams"]:
        assert round(sum(chicken["box_grams"]), 1) == round(chicken["grams"], 1), (
            "the per-box split does not add back up to what went in the pan"
        )

    js = box.app_js()
    assert "function cookedSplit(" in js, "no shared way to divide a cooked weight"
    calc = js[js.index("function cookedSplit(") :]
    calc = calc[: calc.index("\n}")]
    assert "uniform" in calc and "box_grams" in calc, (
        f"the cooked split ignores the very flags that say the boxes differ:\n{calc}"
    )
    body = js[js.index("async function renderBatch(") :]
    body = body[: body.find("\n/* ---")]
    assert "expect / i.boxes" not in body, "the yield preview still divides evenly"


# -- 7. the freezer -------------------------------------------------------------------------------


def test_07_it_says_which_boxes_go_in_the_freezer_and_when_to_move_them_back(open_box) -> None:
    """Sunday 19:30, ten filled boxes on the counter and the fridge already full. The chicken
    keeps three days, so Thursday's and Friday's go in the freezer, and the dhal's Friday box
    with it - and a frozen box is only food if it moved down to the fridge the night before."""
    box = open_box(at(SUNDAY, 18, 0))
    freeze = {dish.name: [d.isoformat() for d in days] for dish, days in box.load(SUNDAY).to_freeze}
    assert freeze == {
        "Chicken and rice boxes": ["2026-09-17", "2026-09-18"],
        "Lentil dhal": ["2026-09-18"],
    }, freeze
    assert "Move a box from the freezer to the fridge the night before" in box.bot_batch(SUNDAY)
    data = _batch_json(box)
    assert "__status" not in data, f"api/batch is {data.get('__status')}: the page freezes nothing"
    on_page = json.dumps(data)
    assert "2026-09-17" in on_page and "freeze" in on_page.lower(), (
        "the page never says which boxes will not survive until the day they are for"
    )
    assert "night before" in on_page, (
        "and never says to move a frozen box down to the fridge the night before"
    )


# -- 8. the order, and how long it takes --------------------------------------------------------


def test_08_it_says_what_to_start_first_and_how_long_the_sitting_is(open_box) -> None:
    """Sunday 18:00, deciding whether this fits before the evening is gone. The 35-minute
    chicken goes on first and everything else happens around it; the whole sitting is about
    50 minutes. Both numbers decide whether he cooks tonight at all."""
    box = open_box(at(SUNDAY, 18, 0))
    load = box.load(SUNDAY)
    assert [d.name for d in load.dishes] == ["Chicken and rice boxes", "Lentil dhal"]
    assert sum(d.prep_minutes for d in load.dishes) == 50
    assert "About 50 min. Start with Chicken and rice boxes." in box.bot_batch(SUNDAY)
    data = _batch_json(box)
    assert "__status" not in data, f"api/batch is {data.get('__status')}: no order, no minutes"
    assert data.get("total_minutes") == 50, f"the sitting takes {data.get('total_minutes')}"
    assert next(d["name"] for d in data.get("dishes", [])) == "Chicken and rice boxes", (
        "the longest pot is the one to start, so it is the one listed first"
    )


# -- 9. ticking pots off while cooking ----------------------------------------------------------


# -- 10. getting there from the message he is already reading --------------------------------------


def test_10_the_cook_message_in_telegram_links_to_the_cook_on_the_page(open_box) -> None:
    """Sunday 18:00, the shopping message lands and ends "One cook on Sun 13 Sep fills 10 boxes.
    /batch has the amounts." He is on his phone. The meal card already links to its own day on
    the page; the cook has to link to the cook, or the page costs him a tap on every message he
    is already reading."""
    box = open_box(at(SUNDAY, 18, 0))
    line = batchflow.summary(box.db, box.owner_id, SUNDAY)
    assert line is not None and "10 boxes" in line, line
    origin = str(box.config.get("web.origin")).rstrip("/")
    assert origin and origin in menu_link(box.config, MONDAY.isoformat(), "lunch"), (
        "a meal card already knows how to link its own day on the page"
    )
    assert "tab=batch" in line or page_link(box.config) in line, (
        f"the cook line sends him to a slash command and names no page: {line!r}"
    )
