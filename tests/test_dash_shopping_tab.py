"""The Shopping tab as it is used: a phone, one hand, a trolley, a shop that walks in aisles.

Ten moments from the theme the owner set - "shopping on the page in a shop on a phone: the right
list,
ticking with one thumb, aisle order, what is already at home, packs and prices, a list the bot and
the page both show, a missing item".

accepted; the list for it was built at 16:00 and holds fourteen lines over nine aisles, three of
them naming a real product with a real price from two different shops, and the chicken buys one
pack fewer because 350 g is already in the fridge. Friday's joint weekend list is still the newest
`shopping_lists` row. the partner is linked to the page. Where a story needs the weekend, the
fixture
moves the world to Saturday, where the two of them shop one joint list from two phones.

Each story drives the API the Shopping tab itself calls - `GET api/shopping`, `POST
api/shopping/tick` - and, where the question is what the person reads or taps, the code path
`renderShopping()` takes in `app.js`, as `test_journey_web_recipe_book.py` does.
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
from tiffin.food import shopping as shopping_mod
from tiffin.food.catalogue import DEFAULT_AISLES
from tiffin.secrets import Secrets
from tiffin.telegram import menuflow
from tiffin.telegram.render import shopping_link
from tiffin.web import api as api_module
from tiffin.web.app import create_app

from .fakes import FakeTelegram
from .web_source import app_source

HEADER = "Tailscale-User-Login"
OWNER_LOGIN = "alex@example.com"
PARTNER_LOGIN = "sam@example.com"

SUNDAY = dt.date(2026, 9, 13)
MONDAY = dt.date(2026, 9, 14)
FRIDAY = dt.date(2026, 9, 18)
SATURDAY = dt.date(2026, 9, 19)
SUNDAY_NEXT = dt.date(2026, 9, 20)

#: the list the 18:00 job built for Mon-Fri, exactly as `shopping.build` writes a row:
#: code, name, aisle, pack_g, pack_label, packs, credit_g, product_name, price_pence, retailer
LIST_ROWS: tuple[tuple[str, str, str, float, str, int, float, str, int | None, str], ...] = (
    (
        "broccoli",
        "Broccoli",
        "produce",
        375.0,
        "375 g pack",
        2,
        0.0,
        "Tesco Broccoli 375G",
        95,
        "tesco",
    ),
    ("potatoes", "Potatoes, white", "produce", 2000.0, "2 kg bag", 1, 0.0, "", None, ""),
    ("spinach", "Spinach, baby", "produce", 240.0, "240 g bag", 1, 0.0, "", None, ""),
    (
        "chicken",
        "Chicken breast, raw",
        "meat",
        640.0,
        "640 g pack",
        3,
        350.0,
        "Sainsbury's British Chicken Breast Fillets 640g",
        545,
        "sainsburys",
    ),
    ("mince", "Beef mince 5%", "meat", 500.0, "500 g pack", 1, 0.0, "", None, ""),
    (
        "salmon",
        "Salmon fillets",
        "fish",
        240.0,
        "240 g pack",
        2,
        0.0,
        "Scottish Lochmuir Salmon Fillets 240g",
        650,
        "marks_spencer",
    ),
    ("skyr", "Skyr, natural", "dairy", 450.0, "450 g pot", 3, 0.0, "", None, ""),
    ("eggs", "Eggs, medium", "dairy", 300.0, "6 eggs", 1, 0.0, "", None, ""),
    ("feta", "Feta", "dairy", 200.0, "200 g pack", 1, 0.0, "", None, ""),
    ("bread", "Bread, wholemeal", "bakery", 800.0, "800 g loaf", 1, 0.0, "", None, ""),
    ("rice", "Rice, basmati, dry", "grains", 1000.0, "1 kg bag", 1, 0.0, "", None, ""),
    ("chickpeas", "Chickpeas, tinned", "tins", 400.0, "400 g tin", 2, 0.0, "", None, ""),
    ("peas", "Peas, garden, frozen", "frozen", 900.0, "900 g bag", 1, 0.0, "", None, ""),
    ("oil", "Olive oil", "condiments", 500.0, "500 ml bottle", 1, 0.0, "", None, ""),
)


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


def _profile(db: Db, person_id: int, *, segment_days: str) -> None:
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
        segment_days=segment_days,
    )


def _menu(db: Db, person_id: int, start: dt.date, end: dt.date, decided: str | None = None) -> int:
    return db.insert(
        "menus",
        person_id=person_id,
        segment_start=start.isoformat(),
        segment_end=end.isoformat(),
        status="accepted",
        **({"created_at": decided} if decided else {}),
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
    lists: dict[str, int]
    ingredients: dict[str, int]

    def get(self, path: str, login: str = OWNER_LOGIN, **kw: Any):
        return self.client.get(path, headers={HEADER: login}, **kw)

    def tick(self, item_id: int, ticked: bool, login: str = OWNER_LOGIN):
        csrf = self.get("api/me", login).json()["csrf"]
        return self.client.post(
            "api/shopping/tick",
            json={"item_id": item_id, "ticked": ticked},
            headers={HEADER: login, "X-CSRF-Token": csrf},
        )

    def asset(self, name: str) -> str:
        """The script or stylesheet the shell actually loads, by its hashed name."""
        if name == "app.js":
            return app_source()
        stem, _, ext = name.rpartition(".")
        found = re.search(rf"static/{stem}\.[0-9a-f]{{10}}\.{ext}", self.get("").text)
        assert found is not None, f"the shell does not reference {name}"
        return self.get(found.group(0)).text

    def render_shopping(self) -> str:
        """Just `renderShopping()` out of app.js - the function that draws the tab."""
        js = self.asset("app.js")
        start = js.index("async function renderShopping()")
        return js[start : js.index("\n/* ---", start)]

    def item(self, payload: dict[str, Any], name: str) -> dict[str, Any]:
        found = next((i for i in payload["items"] if i["name"] == name), None)
        there = [i["name"] for i in payload["items"]]
        assert found is not None, f"{name} is not on the list: {there}"
        return found


@pytest.fixture
def open_box(config: Config, tmp_path: Path) -> Iterator[Callable[..., Box]]:
    opened: list[Db] = []

    def make(when: dt.datetime, *, weekend: bool = False) -> Box:
        clock = FrozenClock(when)
        config.paths.ensure()
        db = Db.open(config.paths.db, clock=clock)
        opened.append(db)
        owner_id = db.insert("persons", slug="owner", display_name="Alex", tone="blunt")
        partner_id = db.insert("persons", slug="partner", display_name="Sam", tone="warm")
        db.insert("tailscale_identities", person_id=owner_id, login=OWNER_LOGIN)
        db.insert("tailscale_identities", person_id=partner_id, login=PARTNER_LOGIN)
        _profile(db, owner_id, segment_days="[[0,1,2,3,4]]")
        _profile(db, partner_id, segment_days="[[0,1,2],[3,4]]")

        ing = {
            code: _ingredient(db, code, name, aisle, pack_g, pack_label)
            for code, name, aisle, pack_g, pack_label, *_ in LIST_ROWS
        }

        m: dict[str, int] = {"owner": _menu(db, owner_id, MONDAY, FRIDAY)}
        # one dish per weekday, so the list's foods are foods the menu really eats
        recipe = db.insert(
            "recipes",
            name="Chicken and rice boxes",
            servings=4,
            prep_minutes=25,
            method_json=json.dumps(["Air fry the chicken.", "Cook the rice.", "Box it up."]),
            source="model",
            keep_days=3,
            freezable=1,
        )
        db.insert("recipe_ingredients", recipe_id=recipe, ingredient_id=ing["chicken"], grams=800)
        db.insert("recipe_ingredients", recipe_id=recipe, ingredient_id=ing["rice"], grams=300)
        for i in range(5):
            day = db.insert(
                "menu_days",
                menu_id=m["owner"],
                day=(MONDAY + dt.timedelta(days=i)).isoformat(),
                kcal=2200,
                protein_g=180,
                fat_g=70,
                carb_g=200,
                fibre_g=30,
            )
            db.insert(
                "meal_slots",
                menu_day_id=day,
                slot="lunch",
                recipe_id=recipe,
                portion_multiplier=1.0,
                portion_g=350,
                kcal=600,
                protein_g=50,
                fat_g=18,
                carb_g=55,
                fibre_g=6,
            )

        lists: dict[str, int] = {}
        lists["segment"] = db.insert(
            "shopping_lists",
            menu_id=m["owner"],
            kind="segment",
            person_id=owner_id,
            created_at="2026-09-13 15:00:12",
        )
        for (
            code,
            _n,
            aisle,
            pack_g,
            pack_label,
            packs,
            credit,
            product,
            price,
            retailer,
        ) in LIST_ROWS:
            db.insert(
                "shopping_items",
                list_id=lists["segment"],
                ingredient_id=ing[code],
                grams_needed=round(packs * pack_g - 40, 1),
                packs=packs,
                leftover_g=40.0,
                aisle=aisle,
                pack_g=pack_g,
                pack_label=pack_label,
                product_name=product or None,
                price_pence=price,
                retailer=retailer or None,
                credit_g=credit,
            )
        # Friday's joint weekend list is the NEWEST shopping_lists row on Sunday evening: the
        # trap `list_to_show` exists to avoid
        lists["stale_joint"] = db.insert(
            "shopping_lists",
            menu_id=None,
            kind="joint_weekend",
            person_id=None,
            created_at="2026-09-11 16:00:08",
        )
        db.insert(
            "shopping_items",
            list_id=lists["stale_joint"],
            ingredient_id=ing["mince"],
            grams_needed=500,
            packs=1,
            leftover_g=0,
            aisle="meat",
            pack_g=500.0,
            pack_label="500 g pack",
        )

        if weekend:
            # decided Thursday, before Friday's list was built from them: a menu decided after
            # its list is a Change the list has not caught up with, and is rebuilt
            m["owner_weekend"] = _menu(db, owner_id, SATURDAY, SUNDAY_NEXT, "2026-09-17 11:00:05")
            m["partner_weekend"] = _menu(
                db, partner_id, SATURDAY, SUNDAY_NEXT, "2026-09-17 11:00:05"
            )
            lists["joint"] = db.insert(
                "shopping_lists",
                menu_id=m["owner_weekend"],
                kind="joint_weekend",
                person_id=None,
                created_at="2026-09-18 16:00:20",
            )
            for code in ("salmon", "broccoli", "bread"):
                row = next(r for r in LIST_ROWS if r[0] == code)
                db.insert(
                    "shopping_items",
                    list_id=lists["joint"],
                    ingredient_id=ing[code],
                    grams_needed=row[3],
                    packs=1,
                    leftover_g=0,
                    aisle=row[2],
                    pack_g=row[3],
                    pack_label=row[4],
                )

        secrets_dir = tmp_path / "s"
        secrets_dir.mkdir(exist_ok=True)
        (secrets_dir / "telegram-token-dev.txt").write_text("1:t", encoding="utf-8")
        config.data["web"]["secure_cookies"] = False
        config.data["web"]["root_path"] = ""
        app = App(config, Secrets(secrets_dir), clock=clock, transport=FakeTelegram())
        client = TestClient(create_app(app), base_url="http://testserver")
        return Box(client, db, config, clock, owner_id, partner_id, m, lists, ing)

    yield make
    for db in opened:
        db.close()


def _bot_lines(box: Box, menu_id: int) -> list[str]:
    """The aisle lines the 18:00 job sent for this menu - the bot's copy of the same list."""
    return shopping_mod.render_lines(shopping_mod.for_menu(box.db, menu_id))


# -- the right list ------------------------------------------------------------------------------


def test_01_sunday_evening_the_tab_opens_on_the_list_for_tomorrows_cook(open_box) -> None:
    """Sunday 13 Sep, 17:40, coat on, about to drive to the shop for Monday to Friday. He opens
    the Shopping tab. The newest list row in the database is Friday's joint weekend list; the
    list he needs is the Mon-Fri one built an hour ago. The tab has to open on the second."""
    box = open_box(at(SUNDAY, 17, 40))
    shown = box.get("api/shopping").json()  # the only call renderShopping() makes
    assert shown["list"] is not None, "the tab has no list at all the evening before the cook"
    assert shown["list"]["id"] == box.lists["segment"], (
        f"the tab shows list {shown['list']} ({[i['name'] for i in shown['items']]}) - the stale "
        f"weekend list {box.lists['stale_joint']}, not Mon-Fri's"
    )
    assert shown["list"]["menu_id"] == box.menus["owner"]
    assert len(shown["items"]) == len(LIST_ROWS)


def test_02_in_the_aisles_the_list_walks_the_shop_not_the_alphabet(open_box) -> None:
    """Sunday 18:10, through the door at the produce end with a trolley. The list has to run
    produce, meat, fish, dairy, bakery, grains, tins, frozen, condiments - the way the shop is
    laid out - because an alphabetical list sends him back past the dairy twice."""
    box = open_box(at(SUNDAY, 18, 10))
    items = box.get("api/shopping").json()["items"]
    aisles = [i["aisle"] for i in items]
    order = [a for a in DEFAULT_AISLES if a in aisles]
    assert aisles == sorted(aisles, key=order.index), f"the walk is out of order: {aisles}"
    names = [i["name"] for i in items]
    assert names != sorted(names, key=str.casefold), "the list is in alphabetical order"
    render = box.render_shopping()
    assert "item.aisle !== aisle" in render and 'el("h2"' in render, (
        "the tab does not head each aisle, so the order is invisible while walking it"
    )


def test_03_one_thumb_ticks_the_broccoli_and_it_stays_ticked(open_box) -> None:
    """Sunday 18:14, trolley in one hand, phone in the other. Two packs of broccoli go in. One
    thumb on the line has to tick it, and it has to still be ticked when the phone relocks and
    he opens the page again at the tills."""
    box = open_box(at(SUNDAY, 18, 14))
    broccoli = box.item(box.get("api/shopping").json(), "Broccoli")
    assert broccoli["ticked"] is False
    assert box.tick(broccoli["id"], True).status_code == 200
    again = box.item(box.get("api/shopping").json(), "Broccoli")
    assert again["ticked"] is True, "the tick did not survive reopening the page"
    assert box.tick(broccoli["id"], False).status_code == 200
    assert box.item(box.get("api/shopping").json(), "Broccoli")["ticked"] is False
    css = box.asset("app.css")
    assert re.search(r"ul\.items button[^}]*min-height: 44px", css, re.S), (
        "the tickable line is smaller than a thumb"
    )
    render = box.render_shopping()
    assert "api/shopping/tick" in render and "li.classList.toggle" in render, (
        "the tab has no one-tap tick on the line"
    )


def test_04_two_phones_one_trolley_and_the_salmon_is_only_bought_once(open_box) -> None:
    """Saturday 19 Sep, 10:30, the two of them in the shop with the joint weekend list, a phone
    each. He ticks the salmon. Her page has to show it ticked - one trolley, one salmon."""
    box = open_box(at(SATURDAY, 10, 30), weekend=True)
    his = box.get("api/shopping").json()
    partners = box.get("api/shopping", PARTNER_LOGIN).json()
    assert his["list"]["id"] == box.lists["joint"] == partners["list"]["id"], (
        f"they are looking at different lists: {his['list']} and {partners['list']}"
    )
    salmon = box.item(his, "Salmon fillets")
    assert box.tick(salmon["id"], True).status_code == 200
    on_partner_phone = box.item(box.get("api/shopping", PARTNER_LOGIN).json(), "Salmon fillets")
    assert on_partner_phone["ticked"] is True, (
        "his tick on the shared list is invisible on her phone, so the second salmon goes in"
    )


def test_05_every_line_the_bots_buttons_could_not_reach_ticks_on_the_page(open_box) -> None:
    """Sunday 18:20, at the tins. Telegram caps the bot at twelve tick buttons and its own
    message says the rest tick on the page. Fourteen lines were sent, so the page is the only
    place the last two can be ticked: it has to show all fourteen, each with its own tap."""
    box = open_box(at(SUNDAY, 18, 20))
    assert len(LIST_ROWS) > menuflow.TICK_BUTTONS, "the fixture has to overflow the bot's buttons"
    source = inspect.getsource(menuflow.MenuFlow.cmd_shopping)
    assert "the rest tick on" in source, "the bot promises the page finishes the job"
    items = box.get("api/shopping").json()["items"]
    assert len(items) == len(LIST_ROWS), f"the page shows only {len(items)} of {len(LIST_ROWS)}"
    render = box.render_shopping()
    assert "for (const item of data.items)" in render and "slice" not in render, (
        "renderShopping() does not draw every item the API returns"
    )
    last = box.item({"items": items}, "Olive oil")
    assert box.tick(last["id"], True).status_code == 200, "the fourteenth line cannot be ticked"


def test_06_the_link_the_bot_gives_lands_on_the_shopping_tab(open_box) -> None:
    """Sunday 18:20, still at the tins, he taps the link in "Buttons for the first 12; the rest
    tick on the page." He has one thumb and a trolley: the link has to open the Shopping tab, not
    whichever tab he last had open on another day."""
    box = open_box(at(SUNDAY, 18, 20))
    source = inspect.getsource(menuflow.MenuFlow.cmd_shopping)
    # the story's own setup was wrong: it asserted this on the GENERIC `page_link()` (bare
    # "the page", no tab), which test_dash_navigation_links.py:395 pins to carry no query string
    # at all - a real, currently-green contract `page_link()` has to keep for every other
    # message that just points at "the page" as a whole. The overflow line here names a
    # SPECIFIC tab, which is what the new `shopping_link()` helper is for (dashboard spec Step 5).
    assert "shopping_link(" in source, "the overflow line is where the bot hands over to the page"
    app_js = box.asset("app.js")
    assert 'get("tab")' in app_js and "shopping: renderShopping" in app_js, (
        "the page cannot be asked for a tab at all"
    )
    link = shopping_link(box.config, "the page")
    assert "tab=shopping" in link, (
        f"the bot sends him to {link} - the page then opens on whatever localStorage last saved, "
        "so a man holding a trolley pays a second tap to find the list he was already reading"
    )


# -- what the line has to say ---------------------------------------------------------------------


def test_07_the_heading_says_which_days_this_list_is_for(open_box) -> None:
    """Sunday 18:00, two lists in the chat and one page: Friday's weekend list and this one. He
    opens the tab to check he is shopping from the right one. The bot's own heading reads
    "Shopping Mon 14 to Fri 18 Sep"; the page's heading has to name the days too, or he cannot
    tell one list from the other."""
    box = open_box(at(SUNDAY, 18, 0))
    shown = box.get("api/shopping").json()
    priced = [i for i in shown["items"] if i["price_pence"] is not None]
    total = sum(i["price_pence"] for i in priced)
    head = (
        f"{shown['list']['kind'].replace('_', ' ')} · {len(shown['items'])} items · "
        f"about £{total / 100:.2f} ({len(priced)} of {len(shown['items'])} priced)"
    )  # exactly what renderShopping() writes into #shopping-head
    assert MONDAY.isoformat() in json.dumps(shown["list"]) or "segment_start" in shown["list"], (
        f"the tab's heading reads {head!r} and the list it came from carries "
        f"{sorted(shown['list'])} - no days anywhere, so the list for Mon-Fri and the list for "
        "the weekend read the same"
    )


def test_08_the_line_says_what_is_already_in_the_fridge(open_box) -> None:
    """Sunday 18:05, at the meat counter. The week eats 1570 g of chicken and the list says three
    packs, because 350 g is credited from last week. If the fridge is actually empty the week is
    a pack short. The bot's line says "(350 g in)"; the page's line has to say it too."""
    box = open_box(at(SUNDAY, 18, 5))
    chicken = box.item(box.get("api/shopping").json(), "Chicken breast, raw")
    assert chicken["credit_g"] == 350.0, "the API carries the credit"
    bot = "\n".join(_bot_lines(box, box.menus["owner"]))
    assert "(350 g in)" in bot, f"the bot's own list says it:\n{bot}"
    render = box.render_shopping()
    assert "credit_g" in render, (
        "renderShopping() draws only name, product and packs, so the page says buy 3 and never "
        "says it is counting on 350 g being at home"
    )


def test_09_the_line_says_which_shop_it_is_from(open_box) -> None:
    """Sunday 18:30, in Sainsbury's with the list open. The salmon on it is an M&S line - the
    list draws on two shops because M&S is read for what Sainsbury's does not stock. Standing in
    one shop holding a line for the other one is the whole failure, so the line has to say."""
    box = open_box(at(SUNDAY, 18, 30))
    bot = "\n".join(_bot_lines(box, box.menus["owner"]))
    assert "[M&S]" in bot, f"the bot's list names the shop:\n{bot}"
    salmon = box.item(box.get("api/shopping").json(), "Salmon fillets")
    assert "M&S" in json.dumps(salmon) or salmon.get("retailer"), (
        f"the page's salmon line is {salmon} - the shop is on the shopping_items row and the "
        "page's own query never selects it, so the page sends him to the wrong shop"
    )


def test_10_the_shelf_is_empty_and_partner_can_say_so_from_the_page(open_box) -> None:
    """Sunday 18:35, in front of the tins, and there are no chickpeas. On the bot that is
    "/shopping out chickpeas" and the shop stops being asked for 30 days. On the page the row is
    the one dead end: tick means bought, and there is no way to say the shelf was empty."""
    box = open_box(at(SUNDAY, 18, 35))
    assert "Shelf empty?" in inspect.getsource(menuflow.MenuFlow.cmd_shopping), (
        "the bot offers the way out"
    )
    writes = sorted(
        str(getattr(route, "path", ""))
        for route in api_module.router.routes
        if "POST" in getattr(route, "methods", set())
    )
    render = box.render_shopping()
    assert re.search(r"shelf|had none|out of stock|/shopping out", render, re.I), (
        f"nothing on the Shopping tab says the shelf was empty (its writes are {writes}), so she "
        "either leaves the row untouched and the menu keeps planning around a tin nobody can buy, "
        "or ticks it and Tiffin believes it is in the fridge"
    )


def test_in_the_shop_a_line_says_what_the_food_is_not_only_how_much(open_box) -> None:
    """Sunday 18:20, holding a tub of skyr in front of the chiller.

    breakdown of this skyr / how many calories it provides. Since if I had that (or at least
    it's nutrition per 100g) - I would be able to compare directly in-store."

    The list named the food and the packs and nothing about the food itself, so the one question
    actually being asked in the aisle - is the tub in my hand the same thing the menu was built
    from? - had no answer on the page. The catalogue's own per-100 g is that answer, and it is
    the very number the day's macros were computed from, so the two cannot drift apart.
    """
    box = open_box(at(SUNDAY, 18, 20))
    items = box.get("api/shopping").json()["items"]
    row = items[0]
    assert "kcal_100" in row, f"the shopping row says nothing about the food: {sorted(row)}"
    assert all(i.get("kcal_100") is not None for i in items), (
        f"some lines carry no nutrition: {[i['name'] for i in items if i.get('kcal_100') is None]}"
    )
    assert all("protein_100" in i for i in items), "protein is what these two menus are built on"

    render = box.render_shopping()
    assert "per100(" in render, f"the tab never renders it:\n{render}"


def test_the_per_100_g_line_carries_what_a_label_carries(open_box) -> None:
    """
    and calories, not only protein?"

    The point of the line is comparing a pack in the hand with the food the menu was built from,
    and a label leads with energy, fat, carbohydrate and protein. Showing two of those made the
    comparison half-possible. Fibre only where there is some, because otherwise every meat and
    dairy row carries "fibre 0" and there are four numbers to find instead of three.
    """
    box = open_box(at(SUNDAY, 18, 20))
    items = box.get("api/shopping").json()["items"]
    for key in ("kcal_100", "protein_100", "fat_100", "carb_100", "fibre_100"):
        assert all(key in i for i in items), f"the API does not send {key}"

    js = box.asset("app.js")
    fn = js[js.index("function per100(") :]
    fn = fn[: fn.index("\n}")]
    for label in ("kcal", "P ", "F ", "C "):
        assert label in fn, f"the line never shows {label.strip()!r}:\n{fn}"
    assert "fibre" in fn, "fibre is on the label too, where there is any"
    assert "i.fibre_100)" in fn, (
        "fibre is printed unconditionally, so every meat row reads 'fibre 0'"
    )
