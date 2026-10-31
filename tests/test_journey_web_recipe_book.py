"""The tailnet page as a recipe book: twelve moments where somebody opens it to cook from it.

The brief, in their words: "It is really complex to right now go per day and then per meal - and
get recipe there. So the partner cannot go and check what is her recipe / meal for
Thursday-Lunch", and
"Potentially we send her links to tailscale where she can look at it ... with recipe in there?".
Earlier the same day, in a shop, the shopping list and the menu disagreed and chickpeas came back
after she had asked three times not to have them.

batch-cooks and freezes; the partner's cadence is Mon-Wed + Thu-Fri, her accepted Mon-Wed menu still
has a chickpea salad, a chickpea-free proposal is waiting for her Accept, and her Thu-Fri has no
menu at all. Only the owner has a Tailscale identity. Stories that need her on the page link her
login
first, which is the one operator step (`tiffin link-tailscale partner <login>`) the brief implies.

Each story drives the page the way the SPA does - the same GET the tab makes - and asserts what
the person reads, not what a query returns internally.
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
from tiffin.food import menu as menu_mod
from tiffin.food import recipes
from tiffin.secrets import Secrets
from tiffin.telegram import menuflow
from tiffin.telegram.render import page_link
from tiffin.web.app import create_app

from .fakes import FakeTelegram
from .web_source import app_source

HEADER = "Tailscale-User-Login"
OWNER_LOGIN = "alex@example.com"
PARTNER_LOGIN = "sam@example.com"

MONDAY = dt.date(2026, 9, 14)
THURSDAY = dt.date(2026, 9, 17)

OWNER_LUNCH_METHOD = [
    "Season the chicken and air fry at 200C for 18 minutes.",
    "Cook the rice in a pan for 12 minutes.",
    "Split into four boxes; freeze three.",
]
PARTNER_LUNCH_METHOD = [
    "Air fry the chicken at 200C for 16 minutes, then slice.",
    "Drain and rinse the chickpeas.",
    "Toss everything with the feta.",
]
PARTNER_THU_LUNCH_METHOD = ["Flake the tuna over the rice.", "Add the spinach and stir."]


def at(day: dt.date, hh: int, mm: int) -> dt.datetime:
    return dt.datetime(day.year, day.month, day.day, hh, mm, tzinfo=TZ)


# -- the world ---------------------------------------------------------------------------------


def _ingredient(db: Db, code: str, name: str, aisle: str, pack_label: str) -> int:
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
        pack_g=400.0,
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
    status: str,
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


def _profile(db: Db, person_id: int, *, segment_days: str, refused: str = "[]") -> None:
    db.insert(
        "profiles",
        person_id=person_id,
        effective_from="2026-09-01",
        sex="f" if refused != "[]" else "m",
        birth_date="1992-05-01",
        height_cm=170.0,
        activity_factor=1.5,
        kcal_floor=1500,
        steps_floor=8000,
        segment_days=segment_days,
        refused_foods=refused,
    )


@dataclasses.dataclass
class Box:
    """The page as it is on the box at one moment, with the database behind it."""

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


@pytest.fixture
def open_box(config: Config, tmp_path: Path) -> Iterator[Callable[..., Box]]:
    opened: list[Db] = []

    def make(
        when: dt.datetime,
        *,
        partner_linked: bool = True,
        partner_thu_fri: bool = False,
        lists_built: bool = True,
        root_path: str = "",
    ) -> Box:
        clock = FrozenClock(when)
        config.paths.ensure()
        db = Db.open(config.paths.db, clock=clock)
        opened.append(db)
        owner_id = db.insert("persons", slug="owner", display_name="Alex", tone="blunt")
        partner_id = db.insert("persons", slug="partner", display_name="Sam", tone="warm")
        db.insert("tailscale_identities", person_id=owner_id, login=OWNER_LOGIN)
        if partner_linked:
            db.insert("tailscale_identities", person_id=partner_id, login=PARTNER_LOGIN)
        _profile(db, owner_id, segment_days="[[0,1,2,3,4]]")
        _profile(db, partner_id, segment_days="[[0,1,2],[3,4]]", refused='["chickpeas"]')

        chicken = _ingredient(db, "chicken", "Chicken breast, raw", "meat", "500 g pack")
        rice = _ingredient(db, "rice", "Rice, basmati, dry", "grains", "1 kg bag")
        eggs = _ingredient(db, "eggs", "Eggs, medium", "dairy", "6 eggs")
        bacon = _ingredient(db, "bacon", "Bacon, back, raw", "meat", "300 g pack")
        bread = _ingredient(db, "bread", "Bread, wholemeal", "bakery", "800 g loaf")
        chickpeas = _ingredient(db, "chickpeas", "Chickpeas, tinned, drained", "tins", "400 g tin")
        feta = _ingredient(db, "feta", "Feta", "dairy", "200 g pack")
        tuna = _ingredient(db, "tuna", "Tuna, tinned in water", "tins", "145 g tin")
        spinach = _ingredient(db, "spinach", "Spinach, baby", "veg", "240 g bag")

        r: dict[str, int] = {}
        r["owner_breakfast"] = _recipe(
            db, "Eggs on toast", ["Toast the bread.", "Scramble the eggs."], {eggs: 120, bread: 80}
        )
        # batch-cooked on Monday for the whole week, three boxes frozen
        r["owner_lunch"] = _recipe(
            db,
            "Chicken and rice boxes",
            OWNER_LUNCH_METHOD,
            {chicken: 800, rice: 300},
            servings=4,
            freezable=1,
            keep_days=3,
        )
        r["partner_breakfast"] = _recipe(
            db,
            "Scrambled eggs, bacon and spinach on toast",
            ["Air fry the bacon.", "Scramble the eggs.", "Wilt the spinach.", "Toast the bread."],
            {eggs: 120, bacon: 60, spinach: 40, bread: 40},
        )
        r["partner_lunch"] = _recipe(
            db,
            "Chicken, chickpea and feta salad",
            PARTNER_LUNCH_METHOD,
            {chicken: 120, chickpeas: 80, feta: 30},
        )
        r["partner_lunch_new"] = _recipe(
            db,
            "Chicken, rice and feta salad",
            ["Air fry the chicken.", "Toss with the rice and feta."],
            {chicken: 120, rice: 60, feta: 30},
        )
        r["partner_thu_lunch"] = _recipe(
            db,
            "Tuna and spinach rice",
            PARTNER_THU_LUNCH_METHOD,
            {tuna: 145, rice: 60, spinach: 40},
        )

        m: dict[str, int] = {}
        m["owner"] = _menu(
            db,
            owner_id,
            MONDAY,
            5,
            {"breakfast": r["owner_breakfast"], "lunch": r["owner_lunch"]},
            status="repeated",
        )
        # her accepted Mon-Wed: status 'proposed' with an accepted decision, as on the box
        m["partner"] = _menu(
            db,
            partner_id,
            MONDAY,
            3,
            {"breakfast": r["partner_breakfast"], "lunch": r["partner_lunch"]},
            status="proposed",
        )
        db.insert(
            "menu_decisions",
            menu_id=m["partner"],
            decision="accepted",
            created_at="2026-09-13 09:45:30",
        )
        # the chickpea-free rebuild, waiting for her Accept
        m["partner_proposal"] = _menu(
            db,
            partner_id,
            MONDAY,
            3,
            {"breakfast": r["partner_breakfast"], "lunch": r["partner_lunch_new"]},
            status="proposed",
        )
        if partner_thu_fri:
            m["partner_thu_fri"] = _menu(
                db, partner_id, THURSDAY, 2, {"lunch": r["partner_thu_lunch"]}, status="accepted"
            )

        # Friday's joint weekend list, then (Sunday 18:00) each person's Mon segment list
        joint = db.insert(
            "shopping_lists",
            menu_id=None,
            kind="joint_weekend",
            person_id=None,
            created_at="2026-09-11 16:00:08",
        )
        db.insert(
            "shopping_items",
            list_id=joint,
            ingredient_id=bacon,
            grams_needed=300,
            packs=1,
            leftover_g=0,
            aisle="meat",
        )
        if lists_built:
            partners = db.insert(
                "shopping_lists",
                menu_id=m["partner"],
                kind="segment",
                person_id=partner_id,
                created_at="2026-09-13 16:00:27",
            )
            for iid, g in ((chickpeas, 240), (chicken, 360), (eggs, 360)):
                db.insert(
                    "shopping_items",
                    list_id=partners,
                    ingredient_id=iid,
                    grams_needed=g,
                    packs=1,
                    leftover_g=0,
                    aisle="tins",
                )
            db.insert(
                "shopping_lists",
                menu_id=m["owner"],
                kind="segment",
                person_id=owner_id,
                created_at="2026-09-13 16:00:40",
            )

        secrets_dir = tmp_path / "s"
        secrets_dir.mkdir(exist_ok=True)
        (secrets_dir / "telegram-token-dev.txt").write_text("1:t", encoding="utf-8")
        config.data["web"]["secure_cookies"] = False
        config.data["web"]["root_path"] = root_path
        app = App(config, Secrets(secrets_dir), clock=clock, transport=FakeTelegram())
        client = TestClient(create_app(app), base_url="http://testserver", root_path=root_path)
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


# -- who opens it ------------------------------------------------------------------------------


def test_01_partner_opens_the_link_before_partner_login_is_linked_and_is_told_what_to_do(
    open_box,
) -> None:
    """Monday 09:00, on her phone on the tailnet, she taps the link the owner sent. Her login is not
    linked yet - the state she is in today - so the page cannot be hers; it has to say so in a
    sentence she can act on, not hand her phone a raw JSON error."""
    box = open_box(at(MONDAY, 9, 0), partner_linked=False)
    shell = box.as_partner("")
    assert shell.status_code in (401, 403), "an unlinked login never gets a page"
    for private in ("Chicken", "kcal", "Scrambled"):
        assert private not in shell.text, f"an unlinked login saw {private!r}"
    assert shell.headers["content-type"].startswith("text/html"), (
        f"she sees {shell.headers['content-type']} {shell.text!r} instead of a page telling her "
        "this login is not linked and who can link it"
    )


def test_02_owner_opens_the_link_and_the_page_is_their(open_box) -> None:
    """Monday 07:45, the owner taps the page link from the morning message. It opens as his, on
    today, with his Mon-Fri menu behind the Menu tab."""
    box = open_box(at(MONDAY, 7, 45))
    shell = box.as_owner("")
    assert shell.status_code == 200 and shell.headers["content-type"].startswith("text/html")
    me = box.as_owner("api/me").json()
    assert me["person"]["display_name"] == "Alex" and me["today"] == MONDAY.isoformat()
    menu = box.as_owner("api/menu").json()
    assert menu["menu"]["id"] == box.menus["owner"]
    assert "Scrambled eggs, bacon" not in json.dumps(menu), "her breakfast is not on his page"


# -- finding a meal ----------------------------------------------------------------------------


def test_03_owner_on_thursday_finds_lunch_with_the_whole_method_one_tap_in(open_box) -> None:
    """
    show Thursday's lunch with every ingredient and every step."""
    box = open_box(at(THURSDAY, 12, 40))
    shell = box.as_owner("").text
    assert 'data-tab="menu"' in shell, "Menu is one tap from wherever the page opens"
    menu = box.as_owner("api/menu").json()  # exactly what renderMenu() fetches
    lunch = _slot(menu, THURSDAY, "lunch")
    assert lunch is not None, "Thursday lunch is on the Menu tab"
    assert lunch["name"] == "Chicken and rice boxes"
    assert {i["name"] for i in lunch["ingredients"]} == {
        "Chicken breast, raw",
        "Rice, basmati, dry",
    }
    assert lunch["method"] == OWNER_LUNCH_METHOD, "every step, not a summary"


def test_04_the_tab_partner_lands_on_at_lunch_says_how_to_make_lunch(open_box) -> None:
    """Monday 12:40. The page opens on Today, which lists "lunch: Chicken, chickpea and feta
    salad - 550 kcal". She wants to make it; the meal on the tab she lands on should carry its
    ingredients and method, not send her hunting through another tab."""
    box = open_box(at(MONDAY, 12, 40))
    today = box.as_partner("api/today").json()  # renderToday(), the default tab
    lunch = next((m for m in today["meals"] if m["slot"] == "lunch"), None)
    assert lunch is not None and lunch["name"] == "Chicken, chickpea and feta salad"
    assert lunch.get("method") == PARTNER_LUNCH_METHOD and lunch.get("ingredients"), (
        f"Today shows only {sorted(lunch)} for lunch - a name and numbers, no recipe"
    )


def test_05_on_monday_partner_looks_up_what_thursday_lunch_is(open_box) -> None:
    """Monday 20:00, planning the week, once her Thu-Fri menu is accepted: she wants to check
    what Thursday's lunch is and what it needs. The Menu tab is the only place a recipe lives
    on the page, so Thursday has to be reachable from it."""
    box = open_box(at(MONDAY, 20, 0), partner_thu_fri=True)
    direct = box.as_partner(f"api/menu?segment={THURSDAY.isoformat()}").json()
    thu = _slot(direct, THURSDAY, "lunch")
    assert thu is not None and thu["method"] == PARTNER_THU_LUNCH_METHOD, "the data is there"
    shown = box.as_partner("api/menu").json()  # the only call renderMenu() ever makes
    days = [d["day"] for d in shown["days"]]
    assert THURSDAY.isoformat() in days, (
        f"the Menu tab shows only {days}; Thursday's lunch exists but no tap on the page reaches it"
    )


def test_06_thursday_with_no_menu_the_page_says_what_to_do_next(open_box) -> None:
    """Thursday 12:40, her Thu-Fri has no menu. She opens the Menu tab to see lunch. The page
    has nothing to show, which is fine - but degrading to manual means saying what to do, the
    way the bot does, not only that there is nothing."""
    box = open_box(at(THURSDAY, 12, 40))
    assert box.as_partner("api/menu").json() | {"weekend": None} == {
        "menu": None,
        "days": [],
        "covers_today": False,
        "weekend": None,
    }
    assert "/cook" in menuflow._NO_MENU, "the bot names the next step"
    said = re.search(r'text: "(No menu[^"]*)"', box.app_js())
    assert said is not None, "renderMenu() has a no-menu sentence"
    assert "/cook" in said.group(1) or "Telegram" in said.group(1), (
        f"the page says only {said.group(1)!r}"
    )


# -- links between the bot and the page --------------------------------------------------------


def test_07_the_recipe_card_in_telegram_links_to_that_meal_on_the_page(open_box) -> None:
    """Thursday 12:40, she tapped Lunch in the bot and got the card. "Potentially we send her
    links to tailscale where she can look at it with recipe in there": the card should carry a
    link to this day's meal on the page, and the page should open on it."""
    from tiffin.telegram.render import menu_link

    box = open_box(at(THURSDAY, 12, 40), partner_thu_fri=True)
    card = recipes.card(box.db, box.recipes["partner_thu_lunch"], 1.0)
    # what show_meal() sends to someone whose login is linked (she is, in this box). For an
    # unlinked login it leaves the line out: the page would answer them with a 403.
    link = menu_link(box.config, THURSDAY.isoformat(), "lunch")
    text = menuflow.recipe_card_text(card, page_line=f"On the page: {link}.")
    origin = str(box.config.get("web.origin")).rstrip("/")
    assert "<a href=" in text and origin in text, f"the card links nowhere:\n{text}"
    assert f"day={THURSDAY.isoformat()}" in text and "slot=lunch" in text, (
        f"the link opens the page, not this meal on it:\n{text}"
    )
    app_js = box.app_js()
    assert "location.hash" in app_js or "location.search" in app_js, (
        "the page never reads its own address, so no link can open a day or a meal"
    )


def test_08_every_link_to_the_page_names_its_real_address(open_box) -> None:
    """Monday 07:30, the morning message says "The page has the week." The link has to be the
    tailnet address the page is actually served at, origin plus mount point, and the page at
    that address has to open and resolve its own API under the same prefix."""
    box = open_box(at(MONDAY, 7, 30), root_path="/tiffin")
    origin = str(box.config.get("web.origin")).rstrip("/")
    assert origin.startswith("https://") and origin.endswith(".ts.net")
    link = page_link(box.config, "The page")
    assert link == f'<a href="{origin}/tiffin">The page</a>', link
    shell = box.as_owner("/tiffin")
    assert shell.status_code == 200 and '<base href="/tiffin/">' in shell.text
    assert box.as_owner("/tiffin/api/menu").json()["menu"]["id"] == box.menus["owner"]


# -- cooking from the page ---------------------------------------------------------------------


def test_09_a_batch_recipe_on_the_page_says_how_many_portions_it_makes(open_box) -> None:
    """Monday 18:30, the owner batch-cooks the week's lunches from the Menu tab. The chicken and
    rice
    makes four boxes; the page lists 800 g of chicken under every weekday. Without "serves 4"
    that reads as 800 g for one lunch - the bot's card says it, the page must too."""
    box = open_box(at(MONDAY, 18, 30))
    lunch = _slot(box.as_owner("api/menu").json(), MONDAY, "lunch")
    assert lunch is not None
    bot = menuflow.recipe_card_text(recipes.card(box.db, box.recipes["owner_lunch"], 1.0))
    assert "serves 4" in bot, "the bot's card says it"
    assert lunch.get("servings") == 4, (
        f"the page's lunch carries {sorted(lunch)} - no servings, so the batch reads as one meal"
    )


def test_10_reheating_a_frozen_box_the_page_says_how_long_it_keeps(open_box) -> None:
    """Thursday 12:40, the owner takes Monday's lunch box out of the freezer and checks the page:
    is it still food, and was it meant to freeze. The bot's card ends "Keeps 3 days in the
    fridge; freezes." - the page's copy of the same recipe has to say the same."""
    box = open_box(at(THURSDAY, 12, 40))
    lunch = _slot(box.as_owner("api/menu").json(), THURSDAY, "lunch")
    assert lunch is not None
    bot = menuflow.recipe_card_text(recipes.card(box.db, box.recipes["owner_lunch"], 1.0))
    assert "Keeps 3 days in the fridge; freezes." in bot
    assert lunch.get("keep_days") == 3 and lunch.get("freezable") in (1, True), (
        f"the page's lunch carries {sorted(lunch)} - nothing on keeping or freezing"
    )


# -- the shop and the promise ------------------------------------------------------------------


def test_11_in_the_shop_on_sunday_the_page_list_is_for_the_menu_partner_shops_for(open_box) -> None:
    """Sunday 13 Sep, 11:00, in the shop for Monday to Wednesday. Her Mon-Wed menu was
    accepted at 10:45; the list for it is built at 18:00, or on demand by /shopping. She opens
    the Shopping tab instead - and it has to be the list for that menu, not Friday's weekend."""
    box = open_box(at(dt.date(2026, 9, 13), 11, 0), lists_built=False)
    shopping_for = menu_mod.accepted_menu_id(box.db, box.partner_id, MONDAY)
    assert shopping_for == box.menus["partner"]
    shown = box.as_partner("api/shopping").json()  # renderShopping()
    assert shown["list"] is not None and shown["list"]["menu_id"] == shopping_for, (
        f"the page shows list {shown['list']} ({[i['name'] for i in shown['items']]}) while "
        f"she is shopping for menu {shopping_for}"
    )


def test_12_the_menu_partner_reads_says_it_still_has_chickpeas_partner_refused(open_box) -> None:
    """Monday 12:00, after asking three times not to have chickpeas. Her accepted Mon-Wed menu
    still has the chickpea salad (it was built before she asked), and a chickpea-free rebuild
    is waiting for her Accept. The bot says "This menu still has Chickpeas in it, which you now
    avoid"; the page showing the same menu has to say it too, or point at the waiting one."""
    box = open_box(at(MONDAY, 12, 0))
    data = box.as_partner("api/menu").json()
    assert data["menu"]["id"] == box.menus["partner"]
    lunch = _slot(data, MONDAY, "lunch")
    assert lunch is not None and lunch["name"] == "Chicken, chickpea and feta salad"
    around = {k: v for k, v in data.items() if k not in ("menu", "days")}
    said = json.dumps(around).lower()
    assert "chickpea" in said or str(box.menus["partner_proposal"]) in said, (
        f"the page's menu answer carries nothing beside the dishes: {sorted(data)}"
    )
