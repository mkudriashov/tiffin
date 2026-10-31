"""The Today tab, ten moments in one Wednesday: is the first screen the one he needs right now?

Today is the tab the page opens on, the tab a link with no `?tab=` lands on, and the tab that is
open when somebody glances at their phone between meetings. These stories stand in the hours of a
real day - kettle on at 07:10, the fridge at 07:12, lunch at 12:45, the off-plan croissant at
15:10, a steps check at 16:10, the evening check-in at 20:45, magnesium at 22:10, and 00:40 with
yesterday still unanswered - and ask what the person reads, not what a query returns.

of four boxes cooked in a batch, targets of 2,250 kcal, a 9,000 step floor, creatine in the
morning and magnesium before bed. the partner has her own Wednesday so every read can be checked for
leaking hers into his.

Each story drives the same GET the tab makes (`renderToday()` fetches `api/today` and nothing
else) and, where the answer depends on what the SPA does with it, reads the code path in `app.js`
the way `test_journey_web_recipe_book.py` does. A number the API has and no tab prints is a
finding, not a pass.
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

from tiffin import body as body_mod
from tiffin.app import App
from tiffin.clock import TZ, FrozenClock
from tiffin.config import Config
from tiffin.db import Db
from tiffin.secrets import Secrets
from tiffin.telegram import dayflow, menuflow
from tiffin.web import api as web_api
from tiffin.web.app import create_app

from .fakes import FakeTelegram
from .web_source import app_source

HEADER = "Tailscale-User-Login"
OWNER_LOGIN = "alex@example.com"
PARTNER_LOGIN = "sam@example.com"

MONDAY = dt.date(2026, 9, 14)
WEDNESDAY = dt.date(2026, 9, 16)
THURSDAY = dt.date(2026, 9, 17)

BREAKFAST = "Skyr with berries and honey"
LUNCH = "Chicken and rice boxes"
DINNER = "Salmon traybake"
PRE_BED = "Cottage cheese"

BREAKFAST_METHOD = ["Spoon the skyr into a bowl.", "Top with the berries and the honey."]
LUNCH_METHOD = [
    "Season the chicken and air fry at 200C for 18 minutes.",
    "Cook the rice in one pan for 12 minutes.",
    "Split into four boxes; freeze two.",
]
DINNER_METHOD = ["Roast the potatoes for 20 minutes.", "Add the salmon for 12 minutes more."]

STEPS_FLOOR = 9000
STEPS_SO_FAR = 4200
KCAL_TARGET = 2250
CROISSANT_KCAL = 620.0


def at(day: dt.date, hh: int, mm: int) -> dt.datetime:
    return dt.datetime(day.year, day.month, day.day, hh, mm, tzinfo=TZ)


# -- the world ---------------------------------------------------------------------------------


def _ingredient(db: Db, code: str, name: str, aisle: str, pack_label: str) -> int:
    return db.insert(
        "ingredients",
        code=code,
        version=1,
        name=name,
        kcal_100=140.0,
        protein_100=14.0,
        fat_100=5.0,
        carb_100=9.0,
        fibre_100=1.2,
        pack_g=500.0,
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


def _profile(db: Db, person_id: int, *, sex: str, kcal_floor: int) -> None:
    db.insert(
        "profiles",
        person_id=person_id,
        effective_from="2026-09-01",
        sex=sex,
        birth_date="1990-03-15",
        height_cm=183.0 if sex == "m" else 166.0,
        activity_factor=1.55,
        kcal_floor=kcal_floor,
        steps_floor=STEPS_FLOOR,
        goal_weight_kg=87.0,
        goal_date="2027-01-24",
        freezes=1,
    )


def _menu(db: Db, person_id: int, slots: dict[str, int], *, kcal: int) -> int:
    """Mon-Fri, accepted, the same four slots every day - a segment cooked in one go."""
    mid = db.insert(
        "menus",
        person_id=person_id,
        segment_start=MONDAY.isoformat(),
        segment_end=(MONDAY + dt.timedelta(days=4)).isoformat(),
        status="accepted",
    )
    for i in range(5):
        day = MONDAY + dt.timedelta(days=i)
        did = db.insert(
            "menu_days",
            menu_id=mid,
            day=day.isoformat(),
            kcal=kcal,
            protein_g=200,
            fat_g=70,
            carb_g=200,
            fibre_g=30,
        )
        for slot, rid in slots.items():
            db.insert(
                "meal_slots",
                menu_day_id=did,
                slot=slot,
                recipe_id=rid,
                portion_multiplier=1.0,
                portion_g=420 if slot == "lunch" else 300,
                kcal=620 if slot == "lunch" else 420,
                protein_g=52.0,
                fat_g=18.0,
                carb_g=55.0,
                fibre_g=7.0,
            )
    return mid


@dataclasses.dataclass
class Page:
    """The page as it is at one moment, with the database behind it."""

    client: TestClient
    db: Db
    config: Config
    clock: FrozenClock
    owner_id: int
    partner_id: int
    recipes: dict[str, int]

    def as_owner(self, path: str, **kw: Any):
        return self.client.get(path, headers={HEADER: OWNER_LOGIN}, **kw)

    def today(self) -> dict[str, Any]:
        """Exactly the call `renderToday()` makes: no date, no options."""
        response = self.as_owner("api/today")
        assert response.status_code == 200, response.text
        return response.json()

    def meal(self, payload: dict[str, Any], slot: str) -> dict[str, Any]:
        found = next((m for m in payload["meals"] if m["slot"] == slot), None)
        assert found is not None, f"no {slot} on the day the page is showing"
        return found

    def app_js(self) -> str:
        """The script the shell actually loads, by the hashed name written into it."""
        return app_source()

    def render_today(self) -> str:
        """The body of `renderToday()` - everything the Today tab puts on the screen."""
        js = self.app_js()
        start = js.index("async function renderToday()")
        end = js.index("/* ---", start)
        return js[start:end]

    def post_routes(self) -> set[str]:
        """Every write the page has, from the router the app is built from."""
        return {
            str(route.path)  # type: ignore[attr-defined]
            for route in web_api.router.routes
            if "POST" in (getattr(route, "methods", None) or set())
        }


@pytest.fixture
def open_page(config: Config, tmp_path: Path) -> Iterator[Callable[..., Page]]:
    opened: list[Db] = []

    def make(when: dt.datetime, *, steps: int | None = None, offplan: bool = False) -> Page:
        clock = FrozenClock(when)
        config.paths.ensure()
        db = Db.open(config.paths.db, clock=clock)
        opened.append(db)
        owner_id = db.insert("persons", slug="owner", display_name="Alex", tone="blunt")
        partner_id = db.insert("persons", slug="partner", display_name="Sam", tone="warm")
        db.insert("tailscale_identities", person_id=owner_id, login=OWNER_LOGIN)
        db.insert("tailscale_identities", person_id=partner_id, login=PARTNER_LOGIN)
        _profile(db, owner_id, sex="m", kcal_floor=1800)
        _profile(db, partner_id, sex="f", kcal_floor=1500)

        skyr = _ingredient(db, "skyr", "Skyr, natural", "dairy", "450 g pot")
        berries = _ingredient(db, "berries", "Blueberries", "veg", "150 g punnet")
        chicken = _ingredient(db, "chicken", "Chicken breast, raw", "meat", "500 g pack")
        rice = _ingredient(db, "rice", "Rice, basmati, dry", "grains", "1 kg bag")
        salmon = _ingredient(db, "salmon", "Salmon fillet", "fish", "240 g pack")
        potato = _ingredient(db, "potato", "New potatoes", "veg", "750 g bag")
        curd = _ingredient(db, "curd", "Cottage cheese", "dairy", "300 g pot")
        tofu = _ingredient(db, "tofu", "Tofu, firm", "chilled", "280 g pack")

        r: dict[str, int] = {}
        r["breakfast"] = _recipe(
            db, BREAKFAST, BREAKFAST_METHOD, {skyr: 300, berries: 100}, prep_minutes=5
        )
        # the batch: one pot on cook day, four boxes, two of them frozen
        r["lunch"] = _recipe(
            db,
            LUNCH,
            LUNCH_METHOD,
            {chicken: 1200, rice: 600},
            servings=4,
            freezable=1,
            keep_days=3,
            prep_minutes=30,
        )
        r["dinner"] = _recipe(
            db, DINNER, DINNER_METHOD, {salmon: 180, potato: 300}, prep_minutes=25
        )
        r["pre_bed"] = _recipe(db, PRE_BED, ["Spoon it out."], {curd: 200}, prep_minutes=2)
        r["hers"] = _recipe(db, "Tofu and rice salad", ["Toss it."], {tofu: 200, rice: 80})

        _menu(
            db,
            owner_id,
            {
                "breakfast": r["breakfast"],
                "lunch": r["lunch"],
                "dinner": r["dinner"],
                "pre_bed": r["pre_bed"],
            },
            kcal=KCAL_TARGET,
        )
        _menu(db, partner_id, {"lunch": r["hers"]}, kcal=1600)

        for pid, kcal in ((owner_id, KCAL_TARGET), (partner_id, 1600)):
            db.insert(
                "targets",
                person_id=pid,
                week_start=MONDAY.isoformat(),
                kcal=kcal,
                protein_g=200 if pid == owner_id else 132,
                fat_g=70 if pid == owner_id else 47,
                carb_g=200 if pid == owner_id else 150,
                fibre_g=30 if pid == owner_id else 25,
                source="formula",
            )
        # Tuesday is closed; Wednesday is open, which is what every hour of it looks like
        db.insert(
            "day_confirmations",
            person_id=owner_id,
            day=(MONDAY + dt.timedelta(days=1)).isoformat(),
            status="confirmed",
            basis="menu",
            kcal=2180,
            protein_g=196,
            fat_g=68,
            carb_g=192,
            fibre_g=29,
        )
        body_mod.record_weigh_in(db, owner_id, WEDNESDAY, 99.4, "manual")

        plan = db.insert("training_plans", person_id=owner_id, effective_from="2026-09-01")
        db.insert(
            "planned_sessions",
            plan_id=plan,
            person_id=owner_id,
            weekday=WEDNESDAY.weekday(),
            kind="climb",
            target_minutes=90,
            start_time="19:00",
        )

        if steps is not None:
            db.insert(
                "icu_wellness",
                person_id=owner_id,
                day=WEDNESDAY.isoformat(),
                steps=steps,
                sleep_s=25200,
                icu_raw_id=0,
            )
        if offplan:
            db.insert(
                "estimates",
                person_id=owner_id,
                day=WEDNESDAY.isoformat(),
                description="Flat white and an almond croissant",
                kcal=CROISSANT_KCAL,
                protein_g=9.0,
                fat_g=32.0,
                carb_g=68.0,
                fibre_g=3.0,
                status="accepted",
            )

        secrets_dir = tmp_path / "s"
        secrets_dir.mkdir(exist_ok=True)
        (secrets_dir / "telegram-token-dev.txt").write_text("1:t", encoding="utf-8")
        config.data["web"]["secure_cookies"] = False
        config.data["web"]["root_path"] = ""
        app = App(config, Secrets(secrets_dir), clock=clock, transport=FakeTelegram())
        client = TestClient(create_app(app), base_url="http://testserver")
        return Page(client, db, config, clock, owner_id, partner_id, r)

    yield make
    for db in opened:
        db.close()


# -- breakfast ---------------------------------------------------------------------------------


def test_01_breakfast_the_first_screen_names_the_day_and_its_meals_in_order(open_page) -> None:
    """Wednesday 07:10, kettle on, phone propped against the toaster. The page opens on Today and
    has to answer the morning's question in one screen: what am I aiming at, and what am I eating,
    in the order the day happens - his, never hers."""
    page = open_page(at(WEDNESDAY, 7, 10))
    me = page.as_owner("api/me").json()
    assert me["today"] == WEDNESDAY.isoformat() and me["person"]["display_name"] == "Alex"
    shell = page.as_owner("").text
    # the section carries ARIA roles now (the tabs are a real tablist), so this matches the two
    # things the story is about - which section is first and that it is the open one - rather
    # than the exact attribute list
    opens_on_today = re.search(r'<section id="today"[^>]*class="tab on"', shell)
    assert opens_on_today, f"the page opens on Today: {shell[:400]}"
    data = page.today()
    assert data["targets"]["kcal"] == KCAL_TARGET
    assert [m["slot"] for m in data["meals"]] == ["breakfast", "lunch", "dinner", "pre_bed"], (
        f"the day arrives out of order: {[m['slot'] for m in data['meals']]}"
    )
    assert data["meals"][0]["name"] == BREAKFAST
    assert "Tofu" not in json.dumps(data), "her Wednesday is on his first screen"


def test_02_breakfast_making_the_meal_the_tab_has_just_named(open_page) -> None:
    """Wednesday 07:12, at the fridge with the skyr in one hand. Today says "breakfast: Skyr with
    berries and honey - 420 kcal" and he wants the two lines that tell him what goes in it. The
    payload carries them; the question is whether the tab he is standing on prints them, or makes
    him tap Menu and scroll to Wednesday for a five-minute breakfast."""
    page = open_page(at(WEDNESDAY, 7, 12))
    breakfast = page.meal(page.today(), "breakfast")
    assert breakfast["method"] == BREAKFAST_METHOD, "api/today carries the method"
    assert [i["name"] for i in breakfast["ingredients"]] == ["Skyr, natural", "Blueberries"]
    drawn = page.render_today()
    printed = re.findall(r"\$\{m\.[a-z_]+\}|m\.(?:method|ingredients)", drawn)
    assert "m.method" in drawn or "m.ingredients" in drawn or "tab=menu" in drawn, (
        "renderToday() prints only "
        f"{sorted(set(printed))} for a meal: the method and the ingredients arrive in the same "
        "response and no pixel of the Today tab shows them, so cooking what Today names is "
        "Menu tab plus a scroll to Wednesday - two taps away from the screen that named it"
    )


# -- lunch -------------------------------------------------------------------------------------


def test_03_lunch_the_box_out_of_the_fridge_is_one_of_four(open_page) -> None:
    """Wednesday 12:45 in the kitchen. Wednesday's lunch is one box of the four-box chicken and
    rice cooked on Sunday. Today's copy of that meal has to say which - a pot of 1,200 g of
    chicken read as one lunch is the numbers_units/04 bug, and api/menu was fixed for exactly
    this by carrying servings, the portion multiplier, keep days and whether it freezes."""
    page = open_page(at(WEDNESDAY, 12, 45))
    lunch = page.meal(page.today(), "lunch")
    chicken = next(i for i in lunch["ingredients"] if i["name"].startswith("Chicken"))
    assert chicken["grams"] == 1200, "the raw pot, straight off the recipe"
    from_menu = page.as_owner("api/menu").json()
    menu_lunch = next(
        s
        for d in from_menu["days"]
        if d["day"] == WEDNESDAY.isoformat()
        for s in d["slots"]
        if s["slot"] == "lunch"
    )
    assert menu_lunch["servings"] == 4 and menu_lunch["freezable"] == 1, "the Menu tab has it"
    assert {"servings", "portion_multiplier", "keep_days", "freezable"} <= set(lunch), (
        f"Today's lunch carries {sorted(lunch)}: no servings, so 1,200 g of chicken is printed "
        "under one lunch's kcal, and nothing says this is a box from Sunday rather than a pot "
        "to cook now - the same shape api/menu had to be fixed for"
    )


def test_04_lunch_the_meal_it_is_time_for_is_the_one_owner_wants(open_page) -> None:
    """Wednesday 12:50, thirty minutes between two calls. He opens the page for one thing: the
    meal that is now. The bot has known this for months - bare /recipe is breakfast to 10:30,
    lunch to 15:00, then dinner - and the tab that opens by default shows the same flat list of
    four meals at 07:10, 12:50 and 22:10, with nothing marking which one is next."""
    page = open_page(at(WEDNESDAY, 12, 50))
    assert menuflow.meal_now(at(WEDNESDAY, 12, 50)) == "lunch", "the bot reads the clock"
    data = page.today()
    named = {k for k in data if k in ("now", "next", "slot_now", "next_slot", "current_slot")}
    drawn = page.render_today()
    clock_aware = any(word in drawn for word in ("getHours", "meal_now", "now", "next"))
    assert named or clock_aware, (
        f"api/today answers with {sorted(data)} and renderToday() reads no clock: at 12:50 the "
        "first screen lists breakfast, lunch, dinner and pre-bed identically, with no mark on "
        "the meal it is time for and no next thing to do"
    )


def test_05_afternoon_what_is_left_after_the_croissant(open_page) -> None:
    """Wednesday 15:10. He told the bot about a flat white and an almond croissant at 11:20 and
    it was costed at 620 kcal and accepted. Deciding what dinner looks like, he opens Today: the
    one number he needs is what is left of 2,250 today. The day closes at 00:05, so the record is
    empty all day, and the Calories card reads "- / 2250" from breakfast until midnight."""
    page = open_page(at(WEDNESDAY, 15, 10), offplan=True)
    data = page.today()
    assert data["estimates"][0]["kcal"] == CROISSANT_KCAL, "the croissant is in the payload"
    assert data["record"] is None, "nothing is written about an open day until it closes"
    running = {k for k in data if k in ("eaten", "eaten_so_far", "remaining", "totals", "left")}
    drawn = page.render_today()
    eaten = re.search(r"const eaten = [^;]+;", drawn)
    assert eaten is not None, "renderToday() computes the eaten half of the Calories card"
    assert running or "estimates" in eaten.group(0), (
        f"the only number on the card is `{eaten.group(0)}`, which is null all day, and "
        f"api/today offers {sorted(data)} with no running total: the 620 kcal he has already "
        "eaten is listed under Off plan and counted nowhere, so the page cannot say what is "
        "left for dinner"
    )


# -- steps -------------------------------------------------------------------------------------


def test_06_afternoon_how_far_short_of_the_steps_floor_owner_is(open_page) -> None:
    """Wednesday 16:10, he opens the page to check his steps with the day's shape around it.
    The Steps card prints the count and the words "floor 9000" and leaves the
    subtraction, the walk it implies and the last seven days to him."""
    page = open_page(at(WEDNESDAY, 16, 10), steps=STEPS_SO_FAR)
    data = page.today()
    assert data["wellness"]["steps"] == STEPS_SO_FAR
    assert data["profile"]["steps_floor"] == STEPS_FLOOR
    short = STEPS_FLOOR - STEPS_SO_FAR
    drawn = page.render_today()
    steps_card = re.search(r'card\("Steps".*?\)\);', drawn, re.S)
    assert steps_card is not None, "there is a Steps card on Today"
    deep_link = '.get("metric")' in page.app_js()
    assert re.search(r"short|remaining|floor\s*-\s*|-\s*steps", steps_card.group(0)), (
        f"the card is `{steps_card.group(0).strip()}`: the nudge says {short:,} short and about "
        f"{max(5, round(short / 100))} minutes of walking, the card says {STEPS_SO_FAR} and "
        f"'floor {STEPS_FLOOR}'. The seven-day picture is on Trends, two taps away, and a link "
        f"cannot open it on steps (renderTrends reads a metric from the address: {deep_link})"
    )


def test_07_late_morning_the_watch_has_not_synced_and_steps_say_nothing(open_page) -> None:
    """Wednesday 11:30. intervals.icu syncs hourly and nothing has written a wellness row for
    today yet - phone left charging, or the key is gone. The morning brief handles exactly this
    by saying the floor is "not counted"; the Today tab drops the Steps card silently, so the
    screen is indistinguishable from a day he has not walked and from a day with no floor."""
    page = open_page(at(WEDNESDAY, 11, 30))
    data = page.today()
    assert data["wellness"] is None, "no row, which is the state for most of a real morning"
    assert data["profile"]["steps_floor"] == STEPS_FLOOR, "the floor is still his floor"
    drawn = page.render_today()
    guard = re.search(r"if \(data\.wellness[^)]*\) \{.*?\n  \}", drawn, re.S)
    assert guard is not None, "the Steps card is behind a wellness guard"
    assert re.search(r"(no steps|not counted|steps.*yet)", drawn, re.I), (
        "with no wellness row the Steps card is simply not drawn and nothing takes its place: "
        f"the guard is `{guard.group(0).splitlines()[0].strip()}` with no else, while the "
        "morning message for the same state says the floor is not being counted. Degrading to "
        "silence on the one tab that opens by default"
    )


# -- evening -----------------------------------------------------------------------------------


def test_08_evening_answering_as_planned_from_the_page(open_page) -> None:
    """Wednesday 20:45. The evening check-in arrived at 20:30 and he is on the sofa with the page
    open, not the chat: everything he needs to answer it - the plan, what was off plan, the
    training - is on this screen. Confirming the day is the single most-repeated action in the
    whole system and Today is where a person is standing when they want to do it."""
    page = open_page(at(WEDNESDAY, 20, 45), steps=STEPS_SO_FAR, offplan=True)
    data = page.today()
    assert data["record"] is None and len(data["meals"]) == 4, "the day is open and planned"
    posts = page.post_routes()
    attempt = page.client.post(
        "api/today/confirm", json={"as_planned": True}, headers={HEADER: OWNER_LOGIN}
    )
    assert attempt.status_code != 404, (
        f"the page can write exactly {sorted(posts)} - a shopping tick and a dish rating. "
        "The evening answer, the one the whole day is waiting on, exists only in Telegram, so "
        "the tab that shows him the day cannot record it"
    )


def test_10_after_midnight_yesterday_is_still_open_and_out_of_reach(open_page) -> None:
    """Thursday 00:40, in from the climb. Wednesday's check-in is unanswered - the bot knows this
    hour belongs to yesterday, which is why a bare /confirm before dawn means yesterday. The page
    has moved on: Today is Thursday, and the day he still has to answer for cannot be reached
    from the tab at all."""
    page = open_page(at(THURSDAY, 0, 40))
    assert dayflow.BEFORE_DAWN_H == 5, "the bot treats this hour as yesterday"
    assert page.today()["day"] == THURSDAY.isoformat(), "the page has already turned over"
    behind = page.as_owner(f"api/today?date={WEDNESDAY.isoformat()}").json()
    assert behind["meals"][0]["name"] == BREAKFAST, "the route answers for any day asked"
    js = page.app_js()
    shell = page.as_owner("").text
    called = re.search(r'api\("api/today[^"]*"\)', js)
    assert (called is not None and "date=" in called.group(0)) or 'id="day"' in shell, (
        f"renderToday() calls {called.group(0) if called else 'api/today'} and the shell has no "
        "date control on the Today tab: api/today?date= answers for Wednesday, and no tap on the "
        "page reaches it. At 00:40 the one day he needs is the one the page will not show"
    )
