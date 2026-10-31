"""Two people, one dashboard: ten moments on the tailnet page where what one of them does
meets the other.

freezes; the partner cooks Mon-Wed and Thu-Fri and has refused chickpeas. The weekend is cooked
together: one salmon traybake off one joint list, with his Sunday chickpea stew on the same
list. Both Tailscale logins are linked, which is the state the page is built for ("the partner will
be").

The rules under every story are the house ones. The person comes from the `Tailscale-User-Login`
header and never from anything typed into a URL or a body. The joint weekend list is one basket
for two phones. Nothing of the other person's week - their dishes, their numbers, their misses -
appears on your page. Each of them is measured against their own floor.

Every story drives the real API the way the SPA does, and where the page renders the answer it
reads the code path in `app.js`, as `test_journey_web_recipe_book.py` does.
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
from tiffin.clock import TZ, FrozenClock, isoday
from tiffin.config import Config
from tiffin.db import Db
from tiffin.food import batch
from tiffin.food import shopping as shopping_mod
from tiffin.secrets import Secrets
from tiffin.web.app import create_app

from .fakes import FakeTelegram
from .web_source import app_source

HEADER = "Tailscale-User-Login"
OWNER_LOGIN = "alex@example.com"
PARTNER_LOGIN = "sam@example.com"

MON = dt.date(2026, 9, 14)
TUE = dt.date(2026, 9, 15)
WED = dt.date(2026, 9, 16)
THU = dt.date(2026, 9, 17)
FRI = dt.date(2026, 9, 18)
SAT = dt.date(2026, 9, 19)
SUN = dt.date(2026, 9, 20)

#: the marker for each person's own private week - a string that exists nowhere else
HIS_DISH = "Chicken and rice boxes"
PARTNER_DISH = "Chicken, chickpea and feta salad"
PARTNER_MISS = "Two glasses of wine at Sam's"
HIS_SUMMARY = "You confirmed six days and trained four times."
PARTNER_SUMMARY = "Tuesday went over by 400 kcal and that is the whole story of the week."


def at(day: dt.date, hh: int, mm: int = 0) -> dt.datetime:
    return dt.datetime(day.year, day.month, day.day, hh, mm, tzinfo=TZ)


# -- the world ----------------------------------------------------------------------------------


def _ingredient(db: Db, code: str, name: str, aisle: str, pack_g: float, label: str) -> int:
    return db.insert(
        "ingredients",
        code=code,
        version=1,
        name=name,
        kcal_100=140.0,
        protein_100=12.0,
        fat_100=6.0,
        carb_100=10.0,
        fibre_100=1.0,
        pack_g=pack_g,
        pack_label=label,
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
    *,
    servings: int = 1,
    freezable: int = 0,
    keep_days: int = 3,
    method: tuple[str, ...] = ("Cook it.",),
) -> int:
    rid = db.insert(
        "recipes",
        name=name,
        servings=servings,
        prep_minutes=25,
        method_json=json.dumps(list(method)),
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
    days: list[tuple[dt.date, dict[str, int]]],
    *,
    status: str = "accepted",
    decision: str | None = None,
) -> int:
    mid = db.insert(
        "menus",
        person_id=person_id,
        segment_start=isoday(days[0][0]),
        segment_end=isoday(days[-1][0]),
        status=status,
    )
    for day, slots in days:
        did = db.insert(
            "menu_days",
            menu_id=mid,
            day=isoday(day),
            kcal=1900,
            protein_g=150,
            fat_g=65,
            carb_g=180,
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
                kcal=560,
                protein_g=45,
                fat_g=18,
                carb_g=50,
                fibre_g=8,
            )
    if decision is not None:
        db.insert("menu_decisions", menu_id=mid, decision=decision)
    return mid


def _profile(
    db: Db,
    person_id: int,
    *,
    sex: str,
    segments: str,
    steps_floor: int,
    freezes: int = 0,
    refused: str = "[]",
) -> None:
    db.insert(
        "profiles",
        person_id=person_id,
        effective_from="2026-09-01",
        sex=sex,
        birth_date="1990-03-15",
        height_cm=175.0,
        activity_factor=1.5,
        steps_floor=steps_floor,
        kcal_floor=1500,
        segment_days=segments,
        refused_foods=refused,
        batch_slots='["lunch","dinner"]',
        freezes=freezes,
    )


def _summary(db: Db, person_id: int, chat_id: int, text: str) -> None:
    db.insert(
        "messages_out",
        person_id=person_id,
        chat_id=chat_id,
        kind="sunday_summary",
        due_key="2026-09-13",
        text_html=f"<b>Your week</b><br>{text}",
        status="sent",
        sent_at="2026-09-13 18:00:04",
    )


@dataclasses.dataclass
class Kitchen:
    """The page as it is at one moment, with the database behind it."""

    client: TestClient
    db: Db
    config: Config
    clock: FrozenClock
    owner_id: int
    partner_id: int
    menus: dict[str, int]
    recipes: dict[str, int]
    lists: dict[str, int]

    def as_owner(self, path: str, **kw: Any):
        return self.client.get(path, headers={HEADER: OWNER_LOGIN}, **kw)

    def as_partner(self, path: str, **kw: Any):
        return self.client.get(path, headers={HEADER: PARTNER_LOGIN}, **kw)

    def post_as(self, login: str, path: str, body: dict[str, Any]):
        csrf = self.client.get("api/me", headers={HEADER: login}).json()["csrf"]
        return self.client.post(path, json=body, headers={HEADER: login, "X-CSRF-Token": csrf})

    def shell(self) -> str:
        return self.as_owner("").text

    def app_js(self) -> str:
        return app_source()

    def item(self, list_id: int, needle: str) -> int:
        row = self.db.one(
            "SELECT si.id FROM shopping_items si JOIN ingredients i ON i.id = si.ingredient_id "
            "WHERE si.list_id = ? AND i.name LIKE ?",
            (list_id, f"%{needle}%"),
        )
        assert row is not None, f"no {needle!r} on list {list_id}"
        return int(row["id"])


@pytest.fixture
def open_kitchen(config: Config, tmp_path: Path) -> Iterator[Callable[..., Kitchen]]:
    opened: list[Db] = []

    def make(when: dt.datetime, *, partner_steps: bool = True) -> Kitchen:
        clock = FrozenClock(when)
        config.paths.ensure()
        db = Db.open(config.paths.db, clock=clock)
        opened.append(db)

        owner_id = db.insert("persons", slug="owner", display_name="Alex", tone="blunt")
        partner_id = db.insert("persons", slug="partner", display_name="Sam", tone="warm")
        db.insert("telegram_identities", person_id=owner_id, chat_id=1000001, method="config")
        db.insert(
            "telegram_identities", person_id=partner_id, chat_id=1000002, method="pairing_code"
        )
        db.insert("tailscale_identities", person_id=owner_id, login=OWNER_LOGIN)
        db.insert("tailscale_identities", person_id=partner_id, login=PARTNER_LOGIN)
        _profile(db, owner_id, sex="m", segments="[[0,1,2,3,4]]", steps_floor=10000, freezes=1)
        _profile(
            db,
            partner_id,
            sex="f",
            segments="[[0,1,2],[3,4]]",
            steps_floor=7000,
            refused='["chickpeas"]',
        )

        food = {
            "salmon": _ingredient(db, "salmon", "Salmon fillets", "fish", 240, "2 fillets"),
            "potato": _ingredient(db, "potato", "Potatoes", "produce", 2000, "2 kg bag"),
            "chickpeas": _ingredient(
                db, "chickpeas", "Chickpeas, tinned, drained", "tins", 400, "400 g tin"
            ),
            "rice": _ingredient(db, "rice", "Basmati rice", "grains", 1000, "1 kg bag"),
            "chicken": _ingredient(db, "chicken", "Chicken breast", "meat", 500, "500 g pack"),
            "feta": _ingredient(db, "feta", "Feta", "dairy", 200, "200 g pack"),
        }

        r: dict[str, int] = {}
        # the weekend dish they both eat, out of one tray
        r["tray"] = _recipe(
            db,
            "Salmon traybake",
            {food["salmon"]: 300, food["potato"]: 400},
            servings=2,
            method=("Heat the oven to 200C.", "Roast the salmon and potatoes for 25 minutes."),
        )
        # his alone, on the same joint list
        r["stew"] = _recipe(
            db, "Chickpea and potato stew", {food["chickpeas"]: 240, food["potato"]: 300}
        )
        r["his_lunch"] = _recipe(
            db,
            HIS_DISH,
            {food["chicken"]: 800, food["rice"]: 300},
            servings=4,
            freezable=1,
            method=("Air fry the chicken.", "Cook the rice.", "Split into four boxes."),
        )
        r["partner_lunch"] = _recipe(
            db, PARTNER_DISH, {food["chicken"]: 120, food["chickpeas"]: 80, food["feta"]: 30}
        )

        m: dict[str, int] = {}
        m["his_week"] = _menu(
            db,
            owner_id,
            [(d, {"lunch": r["his_lunch"]}) for d in (MON, TUE, WED, THU, FRI)],
            status="repeated",
        )
        m["partner_week"] = _menu(
            db,
            partner_id,
            [(d, {"lunch": r["partner_lunch"]}) for d in (MON, TUE, WED)],
            status="proposed",
            decision="accepted",
        )
        m["his_weekend"] = _menu(
            db, owner_id, [(SAT, {"dinner": r["tray"]}), (SUN, {"dinner": r["stew"]})]
        )
        m["partner_weekend"] = _menu(
            db, partner_id, [(SAT, {"dinner": r["tray"]}), (SUN, {"dinner": r["tray"]})]
        )

        lists: dict[str, int] = {}
        # the real joint weekend list, built by the real function from both weekend menus
        shopping_mod.joint_weekend(
            db,
            [m["his_weekend"], m["partner_weekend"]],
            owner_leftovers={owner_id: {}, partner_id: {}},
        )
        lists["joint"] = int(
            db.scalar(
                "SELECT id FROM shopping_lists WHERE kind = 'joint_weekend' "
                "ORDER BY id DESC LIMIT 1"
            )
        )
        # and each person's own Mon segment list, sent on Sunday evening
        for key, pid, menu_id, needs in (
            ("his", owner_id, m["his_week"], ((food["chicken"], 800), (food["rice"], 300))),
            (
                "hers",
                partner_id,
                m["partner_week"],
                ((food["chickpeas"], 240), (food["chicken"], 360), (food["feta"], 90)),
            ),
        ):
            lid = db.insert("shopping_lists", menu_id=menu_id, kind="segment", person_id=pid)
            for iid, g in needs:
                db.insert(
                    "shopping_items",
                    list_id=lid,
                    ingredient_id=iid,
                    grams_needed=g,
                    packs=1,
                    leftover_g=0,
                    aisle="tins",
                )
            lists[key] = lid

        # the week as each of them lived it: his clean, her Tuesday over
        for day, kcal in ((MON, 2380.0), (TUE, 2410.0)):
            db.insert(
                "day_confirmations",
                person_id=owner_id,
                day=isoday(day),
                status="confirmed",
                basis="menu",
                kcal=kcal,
                protein_g=180.0,
            )
        db.insert(
            "day_confirmations",
            person_id=partner_id,
            day=isoday(MON),
            status="confirmed",
            basis="menu",
            kcal=1580.0,
            protein_g=130.0,
        )
        db.insert(
            "day_confirmations",
            person_id=partner_id,
            day=isoday(TUE),
            status="unconfirmed",
            basis="mixed",
            kcal=2010.0,
            protein_g=118.0,
            note=PARTNER_MISS,
        )
        db.insert(
            "estimates",
            person_id=partner_id,
            day=isoday(TUE),
            description=PARTNER_MISS,
            kcal=320.0,
            protein_g=0.0,
            status="accepted",
        )
        _summary(db, owner_id, 1000001, HIS_SUMMARY)
        _summary(db, partner_id, 1000002, PARTNER_SUMMARY)

        # steps: his watch talks to intervals.icu every hour; hers only if she has one
        for i in range(10):
            day = MON - dt.timedelta(days=7) + dt.timedelta(days=i)
            db.insert(
                "icu_wellness",
                person_id=owner_id,
                day=isoday(day),
                steps=11000 + i * 100,
                icu_raw_id=1,
            )
            if partner_steps:
                db.insert(
                    "icu_wellness",
                    person_id=partner_id,
                    day=isoday(day),
                    steps=6000 + i * 50,
                    icu_raw_id=1,
                )

        secrets_dir = tmp_path / "s"
        secrets_dir.mkdir(exist_ok=True)
        (secrets_dir / "telegram-token-dev.txt").write_text("1:t", encoding="utf-8")
        config.data["web"]["secure_cookies"] = False
        config.data["web"]["root_path"] = ""
        app = App(config, Secrets(secrets_dir), clock=clock, transport=FakeTelegram())
        client = TestClient(create_app(app), base_url="http://testserver", root_path="")
        return Kitchen(client, db, config, clock, owner_id, partner_id, m, r, lists)

    yield make
    for db in opened:
        db.close()


def _everything(kitchen: Kitchen, getter: Callable[[str], Any]) -> str:
    """Every tab of the page, as one blob of what that person can read."""
    paths = (
        "api/me",
        "api/today",
        "api/week",
        "api/menu",
        "api/shopping",
        "api/dishes",
        "api/summary/weekly",
        "api/trends?metric=intake&days=30",
        "api/trends?metric=steps&days=30",
    )
    out = []
    for path in paths:
        response = getter(path)
        assert response.status_code == 200, f"{path} answered {response.status_code}"
        out.append(response.text)
    return "\n".join(out)


# -- 1. the page is hers ------------------------------------------------------------------------


def test_01_partner_opens_the_page_and_every_tab_answers_as_partner(open_kitchen) -> None:
    """Monday 12:40. the partner's login is linked at last and she opens the page on her phone for
    the
    first time. Every tab has to be her week - her name at the top, her salad at lunch, her
    dishes - and nothing of his anywhere in it."""
    k = open_kitchen(at(MON, 12, 40))
    me = k.as_partner("api/me").json()
    assert me["person"]["display_name"] == "Sam" and me["person"]["slug"] == "partner"
    assert k.as_partner("api/whoami").json() == {"slug": "partner", "person_id": k.partner_id}

    lunch = next(m for m in k.as_partner("api/today").json()["meals"] if m["slot"] == "lunch")
    assert lunch["name"] == PARTNER_DISH
    names = [d["name"] for d in k.as_partner("api/dishes").json()["dishes"]]
    assert PARTNER_DISH in names and "Salmon traybake" in names, names

    partners = _everything(k, k.as_partner)
    for his in (HIS_DISH, HIS_SUMMARY):
        assert his not in partners, f"{his!r} is his and it is on her page"


# -- 2. the person is not an argument -------------------------------------------------------------


def test_02_owner_cannot_ask_the_page_for_partner_day(open_kitchen) -> None:
    """Monday 12:45. the owner, curious about how her week is going, tries the address bar: a person
    in the query string, her id, the dev header, a second login header. Scoping comes from the
    transport, so every one of them has to answer with his own Monday."""
    k = open_kitchen(at(MON, 12, 45))
    for query in (
        "api/whoami?person=her",
        f"api/whoami?person_id={k.partner_id}",
        "api/whoami?slug=her&person=her",
    ):
        assert k.as_owner(query).json()["slug"] == "owner", f"{query} switched person"

    dev = k.client.get("api/whoami", headers={HEADER: OWNER_LOGIN, "X-Dev-Person": "partner"})
    assert dev.json()["slug"] == "owner", "the dev bypass overrode a real Tailscale login"

    today = k.client.get(
        f"api/today?person=her&person_id={k.partner_id}", headers={HEADER: OWNER_LOGIN}
    )
    assert [m["name"] for m in today.json()["meals"]] == [HIS_DISH], today.json()["meals"]

    # and a person cannot be smuggled through the one thing the page does write
    sneak = k.post_as(
        OWNER_LOGIN,
        "api/shopping/tick",
        {
            "item_id": k.item(k.lists["hers"], "Chickpeas"),
            "ticked": True,
            "person_id": k.partner_id,
        },
    )
    assert sneak.status_code == 400 and "unknown fields" in sneak.json()["detail"], sneak.text


# -- 3. one trolley, two phones -------------------------------------------------------------------


def test_03_owner_ticks_the_salmon_and_partner_phone_shows_it_ticked(open_kitchen) -> None:
    """Saturday 10:40, both of them in the shop with one trolley and two phones. The weekend is
    cooked together off one list. He ticks the salmon; her Shopping tab must show it gone, or a
    second packet goes in the trolley."""
    k = open_kitchen(at(SAT, 10, 40))
    his = k.as_owner("api/shopping").json()
    partners = k.as_partner("api/shopping").json()
    for who, shown in (("his", his), ("hers", partners)):
        assert shown["list"] is not None, f"{who} Shopping tab is empty in the shop"
        assert shown["list"]["kind"] == "joint_weekend", f"{who} shows {shown['list']['kind']}"
        assert shown["list"]["person_id"] is None, f"{who} joint list belongs to somebody"
    assert [i["name"] for i in his["items"]] == [i["name"] for i in partners["items"]]

    salmon = k.item(k.lists["joint"], "Salmon")
    tick = k.post_as(OWNER_LOGIN, "api/shopping/tick", {"item_id": salmon, "ticked": True})
    assert tick.status_code == 200, tick.text
    after = k.as_partner("api/shopping").json()
    ticked = next(i for i in after["items"] if i["id"] == salmon)
    assert ticked["ticked"] is True, (
        "he ticked the salmon off the joint list and her copy still says buy it"
    )


# -- 4. her list is hers --------------------------------------------------------------------------


def test_04_partner_own_segment_list_is_partners_alone(open_kitchen) -> None:
    """Monday 13:00. Her Mon-Wed list, chickpeas and all, was built on Sunday evening. the owner
    guesses its id in the address bar and tries to open it, then tries to tick it off for her.
    A per-person list stays per-person."""
    k = open_kitchen(at(MON, 13, 0))
    trespass = k.as_owner(f"api/shopping?list={k.lists['hers']}").json()
    assert trespass == {"list": None, "items": []}, trespass
    mirror = k.as_partner(f"api/shopping?list={k.lists['his']}").json()
    assert mirror == {"list": None, "items": []}, mirror

    chickpeas = k.item(k.lists["hers"], "Chickpeas")
    refused = k.post_as(OWNER_LOGIN, "api/shopping/tick", {"item_id": chickpeas, "ticked": True})
    assert refused.status_code == 404, refused.text
    assert k.db.scalar("SELECT COUNT(*) FROM shopping_ticks") == 0

    own = k.as_partner(f"api/shopping?list={k.lists['hers']}").json()
    assert own["list"]["person_id"] == k.partner_id and own["items"], own


# -- 5. whose chickpeas ---------------------------------------------------------------------------


def test_05_the_joint_list_on_partner_page_says_whose_the_chickpeas_are(open_kitchen) -> None:
    """Saturday 10:45, in the shop, reading the joint list on the page. She has asked three
    times for no chickpeas; the tin on this list is for his Sunday stew, and her own weekend has
    none. The bot's copy of this list tells her that. The page shows her the same list."""
    k = open_kitchen(at(SAT, 10, 45))
    owners = shopping_mod.weekend_dish_owners(
        k.db, [k.menus["his_weekend"], k.menus["partner_weekend"]]
    )
    label = shopping_mod.attribution_for(owners, k.partner_id)
    assert label.get("chickpeas") == "Chickpea and potato stew", (
        f"the bot's copy of this list attributes the tin: {label}"
    )

    shown = k.as_partner("api/shopping").json()
    tin = next(i for i in shown["items"] if "Chickpeas" in i["name"])
    said = json.dumps(tin).lower()
    assert "stew" in said or "owner" in said, (
        "her page lists the chickpeas she refused with nothing saying they are for his stew - "
        f"the item carries only {sorted(tin)}"
    )


# -- 6. the weekend cook ---------------------------------------------------------------------------


def test_06_saturday_they_cook_the_weekend_together_and_want_the_cook_on_the_page(
    open_kitchen,
) -> None:
    """Saturday 11:30, back from the shop, both of them in the kitchen. One tray feeds two
    plates and his stew is tomorrow. They want what /batch gives in Telegram - totals, pots, how
    many boxes, the order to cook in - on the page they are both looking at."""
    k = open_kitchen(at(SAT, 11, 30))
    menu = batch.menu_to_batch(k.db, k.owner_id, SAT)
    assert menu is not None and int(menu["id"]) == k.menus["his_weekend"]
    load = batch.plan(k.db, menu, ("dinner",), SAT, freezes=True)
    assert load.dishes and load.amounts, "the cook is computable from the menu they both have"

    tabs = re.findall(r'data-tab="([a-z]+)"', k.shell())
    route = k.as_owner("api/batch")
    assert route.status_code != 404 or "batch" in tabs, (
        f"the page has tabs {tabs} and no api/batch, so the cook the two of them are standing "
        f"over - {load.boxes} boxes of {[d.name for d in load.dishes]} - is only in Telegram"
    )


# -- 7. the cook that starts on Wednesday ---------------------------------------------------------


def test_07_owner_skipped_monday_and_tuesday_and_cooks_from_wednesday(open_kitchen) -> None:
    """Wednesday 18:00. the owner never cooked on Sunday, so Monday and Tuesday were eaten off plan
    and his Mon-Fri menu is three days of food now, not five. She cooks her own Mon-Wed and is
    not affected. He opens Menu to cook Wed-Fri: the page has to let him say which day the cook
    starts, or it hands him five days of amounts for three days of eating."""
    k = open_kitchen(at(WED, 18, 0))
    segment = [isoday(d) for d in (MON, TUE, WED, THU, FRI)]
    whole = k.as_owner("api/menu").json()
    assert [d["day"] for d in whole["days"]][:5] == segment, "the week as the page has it"

    asked = k.as_owner(f"api/menu?from={isoday(WED)}").json()
    shown = [d["day"] for d in asked["days"] if d["day"] in segment]
    page = k.shell() + k.app_js()
    control = any(word in page for word in ("cook-from", "cookFrom", "from-day", "since"))
    assert shown == [isoday(d) for d in (WED, THU, FRI)] or control, (
        f"asked for the cook from Wednesday the page still answers with {shown}, and neither "
        "the shell nor app.js has a control for which day the cook starts"
    )


# -- 8. two floors --------------------------------------------------------------------------------


def test_08_each_of_them_is_measured_against_their_own_steps_floor(open_kitchen) -> None:
    """Wednesday 21:00, both on the sofa, both opening Trends on Steps. His floor is 10,000 and
    he clears it; hers is 7,000 and she is under. Neither line, and neither floor, may be the
    other's."""
    k = open_kitchen(at(WED, 21, 0))
    his = k.as_owner("api/trends?metric=steps&days=30").json()
    partners = k.as_partner("api/trends?metric=steps&days=30").json()
    assert his["raw"] and partners["raw"], (his, partners)
    assert set(his["target"]) == {10000}, his["target"]
    assert set(partners["target"]) == {7000}, partners["target"]
    assert all(v >= 11000 for v in his["raw"]), his["raw"]
    assert all(v < 7000 for v in partners["raw"]), partners["raw"]

    his_today = k.as_owner("api/today").json()
    partner_today = k.as_partner("api/today").json()
    assert his_today["profile"]["steps_floor"] == 10000
    assert partner_today["profile"]["steps_floor"] == 7000
    assert his_today["wellness"]["steps"] != partner_today["wellness"]["steps"]


# -- 9. the person without a watch ---------------------------------------------------------------


def test_09_partner_has_no_watch_and_the_page_says_nothing_about_partner_steps(
    open_kitchen,
) -> None:
    """Wednesday 21:05. the owner's watch feeds intervals.icu every hour; the partner has no watch
    and no
    intervals.icu account, and 7,000 steps a day is still part of her plan. She opens the page
    beside him: his shows a steps ring and a line; hers has to say something about steps -
    her floor, or where they would come from - rather than leave the subject out entirely."""
    k = open_kitchen(at(WED, 21, 5), partner_steps=False)
    assert k.as_owner("api/trends?metric=steps&days=30").json()["raw"], "his line is there"

    trend = k.as_partner("api/trends?metric=steps&days=30").json()
    today = k.as_partner("api/today").json()
    assert trend["dates"] == [] and today["wellness"] is None, "the state: nothing was read"
    empty = re.search(r'text: "(Nothing recorded[^"]*)"', k.app_js())
    assert empty is not None, "renderTrends() has an empty-metric sentence"
    assert trend["target"] or "steps" in empty.group(1).lower(), (
        f"her floor is {today['profile']['steps_floor']} steps; the Trends tab says only "
        f"{empty.group(1)!r} and the Today tab draws no steps card at all, so nothing on her "
        "page names the floor or says steps come from intervals.icu"
    )


# -- 10. her misses stay hers ---------------------------------------------------------------------


def test_10_nothing_of_partner_missed_tuesday_reaches_their_page(open_kitchen) -> None:
    """Wednesday 21:10. Her Tuesday went over - two glasses of wine at a friend's, the day left
    unconfirmed, and her Sunday summary says so. He is on the page beside her. Neither person's
    page ever carries the other's misses; hers still carries her own."""
    k = open_kitchen(at(WED, 21, 10))
    partner_tue = k.as_partner(f"api/today?date={isoday(TUE)}").json()
    assert partner_tue["record"]["status"] == "unconfirmed"
    assert [e["description"] for e in partner_tue["estimates"]] == [PARTNER_MISS]
    assert PARTNER_SUMMARY in k.as_partner("api/summary/weekly").json()["text"]["text_html"]

    his = _everything(k, k.as_owner)
    for private in (PARTNER_MISS, PARTNER_SUMMARY, PARTNER_DISH, "unconfirmed"):
        assert private not in his, f"{private!r} is hers and it is on his page"
    his_tue = k.as_owner(f"api/today?date={isoday(TUE)}").json()
    assert his_tue["record"]["status"] == "confirmed" and his_tue["estimates"] == []
    assert [d["status"] for d in k.as_owner("api/week").json()["days"]] == ["confirmed"] * 2
