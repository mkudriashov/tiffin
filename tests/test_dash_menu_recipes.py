"""The Menu tab as something you cook from: ten moments at a hob, a desk or a freezer door.

`test_journey_web_recipe_book.py` asks whether the recipe reaches the page at all. These ten ask
what the person reading it on a phone actually gets once it does: how many boxes the pot makes,
whether the box in the freezer is still food, how many eggs "100 g" is, whether the steps in front
of her are for today or for Sunday, and whether the meal she just ate can be rated where she read
it.

one pot of chicken and rice that makes four boxes. the partner's week is the one the README
describes -
an accepted Mon-Wed, no Thu-Fri menu at all, and the joint weekend cooked on Saturday - and her
Mon-Wed lunch still holds the chickpeas she has since refused.

Where the data is on the API but the page is what is being judged, the assertion reads `app.js`
along the code path that tab takes, the way `test_journey_web_recipe_book.py` does: a field the
API returns and no tab renders has not reached anybody.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import inspect
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
from tiffin.food import recipes
from tiffin.secrets import Secrets
from tiffin.telegram import menuflow
from tiffin.web.app import create_app

from .fakes import FakeTelegram
from .web_source import app_source

HEADER = "Tailscale-User-Login"
OWNER_LOGIN = "alex@example.com"
PARTNER_LOGIN = "sam@example.com"

MONDAY = dt.date(2026, 9, 14)
WEDNESDAY = dt.date(2026, 9, 16)
THURSDAY = dt.date(2026, 9, 17)
FRIDAY = dt.date(2026, 9, 18)
SATURDAY = dt.date(2026, 9, 19)
SUNDAY = dt.date(2026, 9, 20)

#: the pot the owner cooks on Monday: it is fried and roasted on cook day, not at his desk on
#: Thursday
OWNER_LUNCH_METHOD = [
    "Season the chicken and air fry at 200C for 18 minutes.",
    "Cook the rice in a pan for 12 minutes.",
    "Split into four boxes; freeze three.",
]
OWNER_BREAKFAST_METHOD = ["Toast the bread.", "Scramble the eggs."]
PARTNER_LUNCH_METHOD = [
    "Air fry the chicken at 200C for 16 minutes, then slice.",
    "Drain and rinse the chickpeas.",
    "Toss everything with the feta.",
]


def at(day: dt.date, hh: int, mm: int) -> dt.datetime:
    return dt.datetime(day.year, day.month, day.day, hh, mm, tzinfo=TZ)


# -- the world ---------------------------------------------------------------------------------


def _ingredient(db: Db, code: str, name: str, aisle: str, pack_g: float, pack_label: str) -> int:
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
    method: list[str],
    grams: dict[int, float],
    *,
    servings: int = 1,
    freezable: int = 0,
    keep_days: int = 3,
    prep_minutes: int = 20,
) -> int:
    rid = db.insert(
        "recipes",
        name=name,
        servings=servings,
        prep_minutes=prep_minutes,
        method_json=json.dumps(method),
        source="model",
        keep_days=keep_days,
        freezable=freezable,
    )
    for iid, g in grams.items():
        db.insert("recipe_ingredients", recipe_id=rid, ingredient_id=iid, grams=g)
    return rid


def _menu(
    db: Db,
    person_id: int,
    start: dt.date,
    days: int,
    slots: dict[str, int],
    *,
    status: str = "accepted",
) -> int:
    end = start + dt.timedelta(days=days - 1)
    mid = db.insert(
        "menus",
        person_id=person_id,
        segment_start=start.isoformat(),
        segment_end=end.isoformat(),
        status=status,
    )
    for i in range(days):
        day = start + dt.timedelta(days=i)
        did = db.insert(
            "menu_days",
            menu_id=mid,
            day=day.isoformat(),
            kcal=1800,
            protein_g=150,
            fat_g=60,
            carb_g=160,
            fibre_g=28,
        )
        for slot, rid in slots.items():
            db.insert(
                "meal_slots",
                menu_day_id=did,
                slot=slot,
                recipe_id=rid,
                portion_multiplier=1.0,
                portion_g=350,
                kcal=550,
                protein_g=45,
                fat_g=18,
                carb_g=50,
                fibre_g=8,
            )
    return mid


def _profile(
    db: Db, person_id: int, *, sex: str, segment_days: str, refused: str = "[]", freezes: int = 0
) -> None:
    db.insert(
        "profiles",
        person_id=person_id,
        effective_from="2026-09-01",
        sex=sex,
        birth_date="1992-05-01",
        height_cm=178.0 if sex == "m" else 165.0,
        activity_factor=1.5,
        kcal_floor=1500,
        steps_floor=8000,
        segment_days=segment_days,
        refused_foods=refused,
        freezes=freezes,
    )


@dataclasses.dataclass
class Box:
    """The page at one moment, with the database and the script behind it."""

    client: TestClient
    db: Db
    config: Config
    clock: FrozenClock
    owner_id: int
    partner_id: int
    menus: dict[str, int]
    recipes: dict[str, int]

    def as_owner(self, path: str, **kw: Any):
        return self.client.get(path, headers={HEADER: OWNER_LOGIN}, **kw)

    def as_partner(self, path: str, **kw: Any):
        return self.client.get(path, headers={HEADER: PARTNER_LOGIN}, **kw)

    def app_js(self) -> str:
        """The script the shell actually loads, by the hashed name written into it."""
        return app_source()

    def render(self, name: str) -> str:
        """One tab's render function, source and all: the code path that tab takes."""
        js = self.app_js()
        start = js.find(f"async function {name}(")
        assert start != -1, f"{name}() is not in app.js"
        end = js.find("\n/* ---", start)
        return js[start : end if end != -1 else len(js)]


def shown_text(source: str) -> list[str]:
    """Every literal this code path puts in front of a person, template strings included."""
    return re.findall(r"text:\s*`([^`]*)`", source) + re.findall(r'text:\s*"([^"]*)"', source)


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
        # batch_slots keeps its default (lunch and dinner): the owner's lunch IS the batch
        _profile(db, owner_id, sex="m", segment_days="[[0,1,2,3,4]]", freezes=1)
        _profile(db, partner_id, sex="f", segment_days="[[0,1,2],[3,4]]", refused='["chickpeas"]')

        chicken = _ingredient(db, "chicken", "Chicken breast, raw", "meat", 500, "500 g pack")
        rice = _ingredient(db, "rice", "Rice, basmati, dry", "grains", 1000, "1 kg bag")
        eggs = _ingredient(db, "eggs", "Eggs, medium", "dairy", 300, "6 eggs")
        bread = _ingredient(db, "bread", "Bread, wholemeal", "bakery", 800, "800 g loaf")
        chickpeas = _ingredient(db, "chickpeas", "Chickpeas, tinned", "tins", 400, "400 g tin")
        feta = _ingredient(db, "feta", "Feta", "dairy", 200, "200 g pack")

        r: dict[str, int] = {}
        r["owner_breakfast"] = _recipe(
            db,
            "Eggs on toast",
            OWNER_BREAKFAST_METHOD,
            {eggs: 100, bread: 80},
            prep_minutes=10,
            keep_days=1,
        )
        r["owner_lunch"] = _recipe(
            db,
            "Chicken and rice boxes",
            OWNER_LUNCH_METHOD,
            {chicken: 800, rice: 300},
            servings=4,
            freezable=1,
            keep_days=3,
            prep_minutes=35,
        )
        r["partner_lunch"] = _recipe(
            db,
            "Chicken, chickpea and feta salad",
            PARTNER_LUNCH_METHOD,
            {chicken: 120, chickpeas: 80, feta: 30},
        )
        r["weekend_dinner"] = _recipe(
            db, "Traybake chicken and rice", ["Roast it all together for 25 minutes."], {rice: 200}
        )

        m: dict[str, int] = {}
        m["owner"] = _menu(
            db, owner_id, MONDAY, 5, {"breakfast": r["owner_breakfast"], "lunch": r["owner_lunch"]}
        )
        # hers: Mon-Wed accepted, Thu-Fri never built (README: "half her week plans itself late
        # or not at all"), and the weekend cooked jointly on Saturday
        m["partner"] = _menu(db, partner_id, MONDAY, 3, {"lunch": r["partner_lunch"]})
        m["partner_weekend"] = _menu(db, partner_id, SATURDAY, 2, {"dinner": r["weekend_dinner"]})

        secrets_dir = tmp_path / "s"
        secrets_dir.mkdir(exist_ok=True)
        (secrets_dir / "telegram-token-dev.txt").write_text("1:t", encoding="utf-8")
        config.data["web"]["secure_cookies"] = False
        config.data["web"]["root_path"] = ""
        app = App(config, Secrets(secrets_dir), clock=clock, transport=FakeTelegram())
        client = TestClient(create_app(app), base_url="http://testserver")
        return Box(client, db, config, clock, owner_id, partner_id, m, r)

    yield make
    for db in opened:
        db.close()


def _slot(menu_json: dict[str, Any], day: dt.date, slot: str) -> dict[str, Any] | None:
    for d in menu_json["days"]:
        if d["day"] == day.isoformat():
            for s in d["slots"]:
                if s["slot"] == slot:
                    return s
    return None


# -- reading a recipe while you cook -------------------------------------------------------------


def test_01_at_the_hob_thursday_lunch_is_a_numbered_list_of_every_step(open_box) -> None:
    """
    lunch: the whole method, numbered, in order, not a summary and not the first two steps."""
    box = open_box(at(THURSDAY, 12, 40))
    assert 'data-tab="menu"' in box.as_owner("").text, "Menu is one tap from wherever it opens"
    lunch = _slot(box.as_owner("api/menu").json(), THURSDAY, "lunch")
    assert lunch is not None and lunch["method"] == OWNER_LUNCH_METHOD
    fn = box.render("renderMenu")
    assert "slot.method" in fn, "renderMenu() never reads the method it was sent"
    assert re.search(r"\$\{i \+ 1\}\. \$\{step\}", fn), (
        "the steps are not numbered on the page, so a person cooking cannot say where they are"
    )


def test_02_the_pot_that_makes_four_boxes_never_says_it_makes_four(open_box) -> None:
    """Monday 18:30, cook day. the owner stands at the hob with the Menu tab open and the chicken
    out
    of the fridge. The pot is one cook for the week - the bot's card says "serves 4" - and the
    page has to say how many boxes this is, or the amounts in front of him are one lunch."""
    box = open_box(at(MONDAY, 18, 30))
    lunch = _slot(box.as_owner("api/menu").json(), MONDAY, "lunch")
    assert lunch is not None and lunch["servings"] == 4, "the API knows the pot makes four"
    bot = menuflow.recipe_card_text(recipes.card(box.db, box.recipes["owner_lunch"], 1.0))
    assert "serves 4" in bot, "the bot's card says it"
    chicken = next(i for i in lunch["ingredients"] if i["name"].startswith("Chicken"))
    per_box = round(chicken["grams"] * lunch["portion_multiplier"] / lunch["servings"])
    fn = box.render("renderMenu")
    assert any(re.search(r"serve|box|makes", s, re.I) for s in shown_text(fn)), (
        f"the page prints 'Chicken breast, raw {per_box} g' with nothing saying that is one box "
        f"of four - so the man cooking the whole pot ({chicken['grams']:.0f} g) buys and cooks a "
        f"quarter of it. The page shows: {shown_text(fn)}"
    )


def test_03_the_box_out_of_the_freezer_is_not_told_how_long_it_keeps(open_box) -> None:
    """Thursday 12:40, freezer door open, Monday's box in his hand. Is it still food, and was it
    meant to be frozen at all? The bot's card ends "Keeps 3 days in the fridge; freezes." The
    page has the same two facts and prints neither."""
    box = open_box(at(THURSDAY, 12, 40))
    lunch = _slot(box.as_owner("api/menu").json(), THURSDAY, "lunch")
    assert lunch is not None and lunch["keep_days"] == 3 and lunch["freezable"] in (1, True)
    bot = menuflow.recipe_card_text(recipes.card(box.db, box.recipes["owner_lunch"], 1.0))
    assert "Keeps 3 days in the fridge; freezes." in bot, "the bot's card says it"
    fn = box.render("renderMenu")
    assert re.search(r"keep_days|freezable", fn), (
        "api/menu sends keep_days=3 and freezable=1 for this slot and renderMenu() reads "
        f"neither, so the page says only: {shown_text(fn)}"
    )


def test_04_a_hundred_grams_of_egg_is_two_eggs_and_the_page_says_grams(open_box) -> None:
    """Monday 07:10, one hand on the pan. Breakfast is 100 g of "Eggs, medium". Nobody weighs an
    egg before work; the bot's card says "2 eggs" and names the pack it comes out of. The page
    has the pack label in the same payload and prints the grams alone."""
    box = open_box(at(MONDAY, 7, 10))
    breakfast = _slot(box.as_owner("api/menu").json(), MONDAY, "breakfast")
    assert breakfast is not None
    egg = next(i for i in breakfast["ingredients"] if i["name"].startswith("Eggs"))
    assert egg["pack_label"] == "6 eggs", "the API sends the pack the grams come out of"
    assert menuflow.counted(float(egg["grams"]), 300.0, "6 eggs") == "2 eggs", (
        "the arithmetic that turns grams into eggs is in code already"
    )
    fn = box.render("renderMenu")
    assert re.search(r"pack_label|counted", fn), (
        "renderMenu() prints `${ing.name} ${grams} g` and drops pack_label, so the page reads "
        f"'{egg['name']} {egg['grams']:.0f} g' at a hob with no scales"
    )


def test_05_the_thursday_desk_lunch_is_a_method_for_sunday_and_does_not_say_so(open_box) -> None:
    """Thursday 12:40 at her desk, a microwave and nothing else. The page's lunch card opens
    "Season the chicken and air fry at 200C for 18 minutes" - it was air fried on Monday. The bot
    adds "Batch-cooked: the method is for cook day"; the page hands over the raw steps."""
    box = open_box(at(THURSDAY, 12, 40))
    profile = box.db.one("SELECT batch_slots FROM profiles WHERE person_id = ?", (box.owner_id,))
    assert profile is not None and "lunch" in str(profile["batch_slots"]), "lunch is batched"
    note = inspect.getsource(menuflow.MenuFlow._batch_note)
    assert "the method is for cook day" in note, "the bot says which part happens when"
    data = box.as_owner("api/menu").json()
    lunch = _slot(data, THURSDAY, "lunch")
    assert lunch is not None and lunch["method"][0].startswith("Season the chicken and air fry")
    assert "batch" in json.dumps(data).lower(), (
        f"nothing in api/menu marks this slot as batch-cooked (the slot carries {sorted(lunch)}), "
        "so at 12:40 on Thursday the page tells her to air fry chicken she has not got"
    )


# -- a week that is not one segment ---------------------------------------------------------------


def test_06_partner_menu_tab_claims_a_week_it_cannot_show(open_box) -> None:
    """Monday 20:00, the partner plans the week. Her Mon-Wed is accepted and the joint weekend is
    cooked on Saturday; Thursday and Friday were never built - the normal state of her week. The
    tab heads itself "2026-09-14 to 2026-09-20" and then simply skips the two days."""
    box = open_box(at(MONDAY, 20, 0))
    data = box.as_partner("api/menu").json()
    days = [d["day"] for d in data["days"]]
    assert days == [
        MONDAY.isoformat(),
        "2026-09-15",
        WEDNESDAY.isoformat(),
        SATURDAY.isoformat(),
        SUNDAY.isoformat(),
    ], days
    assert len(data["menus"]) == 2, "two segments came back, as the tab asks for"
    missing = [THURSDAY.isoformat(), FRIDAY.isoformat()]
    fn = box.render("renderMenu")
    span = re.search(
        r"\$\{menus\[0\]\.segment_start\} to \$\{menus\[menus\.length - 1\]\.segment_end\}", fn
    )
    assert span is None, (
        f"the tab's only heading is one span, {days[0]} to {days[-1]}, over two separate cooks - "
        f"and {missing} have no menu at all. Nothing on the page says she has to plan them, or "
        "which of the two shops each day belongs to"
    )


def test_07_the_chickpeas_partner_refused_are_named_on_the_page_with_a_way_out(open_box) -> None:
    """Monday 12:00. She asked three times not to have chickpeas; her accepted Mon-Wed was built
    before she asked and still has the salad. The bot says so; the page showing the same menu has
    to name the food and say what to do about it, not print the dish as if nothing happened."""
    box = open_box(at(MONDAY, 12, 0))
    data = box.as_partner("api/menu").json()
    named = [n.lower() for n in data["refused_still_on_it"]]
    assert any("chickpea" in n for n in named), data["refused_still_on_it"]
    fn = box.render("renderMenu")
    assert "refused_still_on_it" in fn, "renderMenu() never reads the warning it is sent"
    said = [s for s in shown_text(fn) if "avoid" in s]
    assert said and any("/cook" in s for s in said), (
        f"the page names the food but not the next step: {said}"
    )


# -- getting to the recipe, and back from it -------------------------------------------------------


def test_08_today_names_lunch_and_hides_the_recipe_it_already_has(open_box) -> None:
    """Monday 12:40. The page opens on Today, which says "lunch: Chicken, chickpea and feta salad
    - 550 kcal". She wants to make it. api/today already carries the method and the ingredients
    for that meal; the tab she is standing on throws them away."""
    box = open_box(at(MONDAY, 12, 40))
    today = box.as_partner("api/today").json()
    lunch = next(m for m in today["meals"] if m["slot"] == "lunch")
    assert lunch["method"] == PARTNER_LUNCH_METHOD and lunch["ingredients"], "the API sends both"
    fn = box.render("renderToday")
    assert re.search(r"m\.method|m\.ingredients", fn), (
        "renderToday() renders `${m.slot}: ${m.name}` and the kcal and stops, so the recipe it "
        f"was sent is invisible; the tab shows: {shown_text(fn)}"
    )


def test_09_from_todays_lunch_there_is_no_tap_that_opens_it(open_box) -> None:
    """Monday 12:42, still on Today. If the recipe is not going to be here, the meal named here
    has to be a tap to where it is - the Menu tab already gives every slot an id (`d-2026-09-14
    -lunch`) and reads ?day= and ?slot= from its own address. Nothing on Today links to it."""
    box = open_box(at(MONDAY, 12, 40))
    fn = box.render("renderToday")
    menu_fn = box.render("renderMenu")
    assert "`d-${day.day}-${slot.slot}`" in menu_fn, "the Menu tab has an anchor per meal"
    assert re.search(r"show\(\"menu\"\)|tab=menu|d-\$\{|addEventListener", fn), (
        "no meal on Today is a link or a button: to read Monday's lunch she taps Menu, then "
        f"scrolls the week for the day and the slot. Today renders: {shown_text(fn)}"
    )


def test_10_partner_cannot_rate_the_meal_on_the_recipe_partner_just_cooked_from(open_box) -> None:
    """Monday 21:10, plate in the sink, the recipe still open on the Menu tab. The bot's card
    ends "Rate it 1-5 and I will propose it more or less often" with the buttons under it. The
    page takes the rating - api/dishes/rate accepts it - but only from the Dishes tab, which
    lists dish names with no day and no slot."""
    box = open_box(at(MONDAY, 21, 10))
    recipe_id = box.recipes["partner_lunch"]
    csrf = box.as_partner("api/me").json()["csrf"]
    posted = box.client.post(
        "api/dishes/rate",
        json={"recipe_id": recipe_id, "score": 4},
        headers={HEADER: PARTNER_LOGIN, "X-CSRF-Token": csrf},
    )
    assert posted.status_code == 200, posted.text
    assert box.db.scalar("SELECT score FROM ratings WHERE recipe_id = ?", (recipe_id,)) == 4
    bot = menuflow.recipe_card_text(recipes.card(box.db, recipe_id, 1.0))
    assert "Rate it 1-5" in bot, "the bot asks for it on the card itself"
    fn = box.render("renderMenu")
    assert "api/dishes/rate" in fn, (
        "the recipe she cooked from offers no rating: she taps Dishes and matches the dish by "
        "name from a list with no day on it, which is two taps and a guess for one number"
    )
