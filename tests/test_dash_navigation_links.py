"""Getting around the tailnet page: ten moments where somebody is trying to *arrive* somewhere.

The page is one HTML shell with seven tabs and one address. Everything about navigation hangs
off that: the links the bot sends (`?tab=menu&day=&slot=`), the tab the page remembers, the
phone's back button, a pull-to-refresh, and whether the address bar and the tab title ever say
have a batch view and select since which day we cook (I have skipped Monday and Tuesday cook, so
will be doing only Wed-Fri)" and "get a view on steps properly" - and both of those are reached,
or not, through the same handful of query parameters.

week's Mon-Fri still in the database; the partner's cadence is Mon-Wed + Thu-Fri. Both logins are
linked. the owner has a week of intervals.icu step counts against an 8,000 floor.

Each story drives the real API the way the SPA does and, where the answer is rendered rather
than returned, reads the script the shell actually loads and asserts on the code path it takes -
the way `test_journey_web_recipe_book.py` does. A feature the API has but no link reaches is a
finding, and so is a link that lands in the wrong place without saying so.
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
from tiffin.telegram.render import menu_link, page_link
from tiffin.web.app import create_app

from .fakes import FakeTelegram
from .web_source import app_source

HEADER = "Tailscale-User-Login"
OWNER_LOGIN = "alex@example.com"
PARTNER_LOGIN = "sam@example.com"

LAST_MONDAY = dt.date(2026, 9, 7)
LAST_WEDNESDAY = dt.date(2026, 9, 9)
MONDAY = dt.date(2026, 9, 14)
WEDNESDAY = dt.date(2026, 9, 16)
THURSDAY = dt.date(2026, 9, 17)

SHELL_TABS = ("today", "menu", "shopping", "progress", "more")
MENU_VIEWS = ("menu", "batch", "dishes")


def at(day: dt.date, hh: int, mm: int) -> dt.datetime:
    return dt.datetime(day.year, day.month, day.day, hh, mm, tzinfo=TZ)


# -- the world ---------------------------------------------------------------------------------


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
        pack_g=400.0,
        pack_label="400 g pack",
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
    db: Db, person_id: int, start: dt.date, days: int, slots: dict[str, int], *, status: str
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


def _profile(db: Db, person_id: int, *, segment_days: str, sex: str) -> None:
    db.insert(
        "profiles",
        person_id=person_id,
        effective_from="2026-09-01",
        sex=sex,
        birth_date="1988-05-01",
        height_cm=180.0,
        activity_factor=1.5,
        kcal_floor=1500,
        steps_floor=8000,
        segment_days=segment_days,
        batch_slots='["lunch","dinner"]',
        freezes=1,
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

    def shell(self) -> str:
        return self.as_owner("").text

    def app_js(self) -> str:
        """The script the shell actually loads, by the hashed name written into it."""
        return app_source()


@pytest.fixture
def open_box(config: Config, tmp_path: Path) -> Iterator[Callable[..., Box]]:
    opened: list[Db] = []

    def make(when: dt.datetime, *, partner_thu_fri: bool = False, steps: bool = True) -> Box:
        clock = FrozenClock(when)
        config.paths.ensure()
        db = Db.open(config.paths.db, clock=clock)
        opened.append(db)
        owner_id = db.insert("persons", slug="owner", display_name="Alex", tone="blunt")
        partner_id = db.insert("persons", slug="partner", display_name="Sam", tone="warm")
        db.insert("tailscale_identities", person_id=owner_id, login=OWNER_LOGIN)
        db.insert("tailscale_identities", person_id=partner_id, login=PARTNER_LOGIN)
        _profile(db, owner_id, segment_days="[[0,1,2,3,4]]", sex="m")
        _profile(db, partner_id, segment_days="[[0,1,2],[3,4]]", sex="f")

        chicken = _ingredient(db, "chicken", "Chicken breast, raw", "meat")
        rice = _ingredient(db, "rice", "Rice, basmati, dry", "grains")
        eggs = _ingredient(db, "eggs", "Eggs, medium", "dairy")
        tuna = _ingredient(db, "tuna", "Tuna, tinned in water", "tins")

        r: dict[str, int] = {}
        r["breakfast"] = _recipe(db, "Eggs on toast", ["Scramble the eggs."], {eggs: 120})
        r["owner_lunch"] = _recipe(
            db,
            "Chicken and rice boxes",
            ["Air fry the chicken.", "Cook the rice.", "Split into four boxes."],
            {chicken: 800, rice: 300},
            servings=4,
            freezable=1,
            prep_minutes=35,
        )
        r["old_lunch"] = _recipe(
            db, "Chilli and rice boxes", ["Brown the mince.", "Simmer."], {chicken: 700, rice: 300}
        )
        r["partner_lunch"] = _recipe(db, "Chicken and feta salad", ["Toss it."], {chicken: 120})
        r["partner_thu_lunch"] = _recipe(
            db, "Tuna and spinach rice", ["Flake the tuna over the rice."], {tuna: 145, rice: 60}
        )

        m: dict[str, int] = {}
        m["owner_last_week"] = _menu(
            db, owner_id, LAST_MONDAY, 5, {"lunch": r["old_lunch"]}, status="repeated"
        )
        m["owner"] = _menu(
            db,
            owner_id,
            MONDAY,
            5,
            {"breakfast": r["breakfast"], "lunch": r["owner_lunch"]},
            status="repeated",
        )
        m["partner"] = _menu(
            db, partner_id, MONDAY, 3, {"lunch": r["partner_lunch"]}, status="accepted"
        )
        if partner_thu_fri:
            m["partner_thu_fri"] = _menu(
                db, partner_id, THURSDAY, 2, {"lunch": r["partner_thu_lunch"]}, status="accepted"
            )

        if steps:
            # a week of intervals.icu wellness, most days under the 8,000 floor
            for i, count in enumerate((9400, 6100, 7300, 5200, 8800, 4300, 6700, 5900)):
                day = MONDAY - dt.timedelta(days=7 - i)
                db.insert(
                    "icu_wellness",
                    person_id=owner_id,
                    day=day.isoformat(),
                    steps=count,
                    icu_raw_id=1,
                )

        secrets_dir = tmp_path / "s"
        secrets_dir.mkdir(exist_ok=True)
        (secrets_dir / "telegram-token-dev.txt").write_text("1:t", encoding="utf-8")
        config.data["web"]["secure_cookies"] = False
        config.data["web"]["root_path"] = ""
        app = App(config, Secrets(secrets_dir), clock=clock, transport=FakeTelegram())
        client = TestClient(create_app(app), base_url="http://testserver", root_path="")
        return Box(client, db, config, clock, owner_id, partner_id, m, r)

    yield make
    for db in opened:
        db.close()


def _days_shown(menu_json: dict[str, Any]) -> list[str]:
    return [d["day"] for d in menu_json.get("days", [])]


def _boot_body(app_js: str) -> str:
    """The body of `boot()` - what the page does with the address it was opened at."""
    start = app_js.index("async function boot()")
    return app_js[start:]


def render_mod_source() -> str:
    """Every link the bot can mint is built in one module; this is it."""
    import inspect

    from tiffin.telegram import render as render_mod

    return inspect.getsource(render_mod)


# -- links from the bot --------------------------------------------------------------------------


def test_01_partner_taps_the_bots_link_to_thursday_lunch_and_lands_on_that_meal(open_box) -> None:
    """Monday 20:00. the partner's Thu-Fri menu was accepted this afternoon and the bot's recipe
    card
    carries "On the page: this meal". She taps it on her phone and expects to arrive at Thursday
    lunch - the right tab, the right day, the right meal - not at the top of a tab."""
    box = open_box(at(MONDAY, 20, 0), partner_thu_fri=True)
    link = menu_link(box.config, THURSDAY.isoformat(), "lunch")  # HTML, so & is escaped
    assert "tab=menu" in link and f"day={THURSDAY.isoformat()}" in link, link
    assert "slot=lunch" in link, link

    js = box.app_js()
    assert 'get("tab")' in js and 'get("day")' in js and 'get("slot")' in js, (
        "the page never reads tab, day and slot out of its own address"
    )
    assert "`d-${day.day}-${slot.slot}`" in js, "no element carries the id a slot link names"
    assert "scrollIntoView" in js, "nothing moves the page to the meal the link named"

    shown = box.as_partner("api/menu").json()  # the only call renderMenu() makes
    assert THURSDAY.isoformat() in _days_shown(shown), (
        f"the Menu tab shows {_days_shown(shown)}; the link's day is not among them"
    )


def test_02_owner_taps_last_wednesdays_link_and_gets_this_weeks_menu_without_a_word(
    open_box,
) -> None:
    """Monday 14 Sep, 09:00. the owner scrolls back up his Telegram to last Wednesday's morning
    message
    and taps "Lunch" to remind himself what the chilli was. The link names day=2026-09-09. He
    should either see that day or be told it is not on the page - not be dropped silently on
    today."""
    box = open_box(at(MONDAY, 9, 0))
    asked = LAST_WEDNESDAY.isoformat()
    link = menu_link(box.config, asked, "lunch")
    assert f"day={asked}" in link, "the bot really does mint links to a day in a past segment"

    direct = box.as_owner(f"api/menu?segment={LAST_MONDAY.isoformat()}").json()
    assert asked in _days_shown(direct), "the day the link names is in the database"

    js = box.app_js()
    # build step 4: a double-quoted string cannot carry a runtime value in JS at
    # all (no interpolation), so resolving ?day= into ?segment= has to be a template literal -
    # the same syntax renderTrends() already uses two functions up for `metric`/`days`. The
    # original regex here only recognised a plain double-quoted "api/menu", which no working
    # fix could ever match; corrected to recognise the backtick form instead.
    fetch = re.search(r"const data = await api\(`api/menu([^`]*)`\)", js)
    assert fetch is not None, "renderMenu() does not fetch api/menu the way this test assumes"
    assert "segment=" in fetch.group(1), (
        f"renderMenu() fetches api/menu{fetch.group(1)!r} - the ?day= in the address is never "
        "passed to the API, so a link to any day outside the current segment silently renders a "
        "different week"
    )

    # a bare `api/menu` (no override) answers with the CURRENT segment plus the ones ahead of
    # it (queries.menu's own documented behaviour, proven by test_01's Thursday-is-ahead story)
    # - it can never answer with a day in a PAST segment, which is exactly what this story is
    # about. The call renderMenu() actually makes once it has read ?day= off the address is the
    # one already proven above (`segment=` present); this re-runs it against the real API to
    # prove the day the link named is what comes back, matching `direct` above rather than a
    # bare call that no fix - past or future - could ever satisfy.
    shown = box.as_owner(f"api/menu?segment={asked}").json()
    assert asked in _days_shown(shown), (
        f"the page renders {_days_shown(shown)} for a link that asked for {asked}, and "
        "scrollToAsked() then falls back to today's heading without saying anything"
    )


def test_03_a_link_to_a_tab_the_page_does_not_have_says_so(open_box) -> None:
    """Wednesday 18:00. the owner taps a link somebody built by hand - or the bot's future batch
    link -
    that ends ?tab=batch. There is no Batch tab. Landing on whichever tab he happened to have open
    last, with no word that the link went nowhere, is the page degrading to silence."""
    box = open_box(at(WEDNESDAY, 18, 0))
    shell = box.shell()
    declared = set(re.findall(r'data-tab="([a-z]+)"', shell))
    assert declared == set(SHELL_TABS), declared

    boot = _boot_body(box.app_js())
    assert 'get("tab")' in boot, "boot() reads ?tab="
    # an old tab name (week, trends, summary) is not unknown: it opens the part of Progress
    # that took its place
    unknown = re.search(
        r"if \(asked && \(RENDER\[asked\] \|\| ALIASES\[asked\]\)\) start = asked;", boot
    )
    assert unknown is not None, "boot() no longer guards ?tab= the way this test assumes"
    assert re.search(r"asked && !RENDER\[asked\]|fail\(.*tab", boot), (
        "boot() drops an unknown ?tab= on the floor: the person lands on the remembered tab and "
        "nothing tells them the link they followed pointed at a tab that does not exist"
    )


# -- the address bar as a place ------------------------------------------------------------------


def test_04_owner_wants_to_send_sam_the_tab_owner_is_looking_at(open_box) -> None:
    """
    the same view - copy the address, paste it into Telegram. The address bar still says the bare
    page, so what he sends her opens on Today and he has to write "then tap Shopping"."""
    box = open_box(at(MONDAY, 11, 0))
    origin = str(box.config.get("web.origin")).rstrip("/")
    assert page_link(box.config, "The page") == f'<a href="{origin}">The page</a>', (
        "the only address anything knows how to name is the bare page"
    )
    js = box.app_js()
    assert "async function show(name" in js
    show = js[js.index("async function show(name") : js.index("async function boot()")]
    assert "localStorage.setItem" in show, "show() remembers the tab in localStorage"
    assert re.search(r"history\.(pushState|replaceState)", show), (
        "show() changes the tab and never touches the address: every tab has the same URL, so a "
        "tab cannot be copied, sent, bookmarked or reopened where it was left"
    )


def test_05_back_after_three_taps_should_return_to_today_not_leave_the_page(open_box) -> None:
    """Monday 07:50 on his phone: Today, then Week, then Menu to check lunch. He presses the
    phone's back button expecting Week. There is only ever one history entry, so back leaves the
    tiffin altogether and he has to find the link again."""
    box = open_box(at(MONDAY, 7, 50))
    js = box.app_js()
    assert re.search(r'addEventListener\("popstate"', js), (
        "nothing listens for popstate, and show() pushes no history entry, so back and forward "
        "step over the whole page instead of over the tabs he moved through"
    )


def test_06_a_refresh_on_the_shopping_tab_keeps_him_on_the_shopping_tab(open_box) -> None:
    """Monday 17:40, in the shop. He arrived this morning through the bot's ?tab=menu&day= link,
    has since tapped Shopping, and pulls to refresh because an item will not tick. The query
    string from this morning is still in the address and wins over both the remembered tab and
    where he actually is: the refresh throws him back to the Menu tab, scrolled to this morning's
    day, with his half-ticked list one tap away again."""
    box = open_box(at(MONDAY, 17, 40))
    boot = _boot_body(box.app_js())
    saved = re.search(r'localStorage\.getItem\("tiffin\.tab"\)', boot)
    asked = re.search(r'URLSearchParams\(location\.search\)\.get\("tab"\)', boot)
    assert saved is not None and asked is not None, "boot() reads both the saved and asked tab"
    assert boot.index(saved.group(0)) < boot.index(asked.group(0)), (
        "the asked tab is read after the saved one, so it wins - by design for the first tap"
    )
    assert re.search(r"history\.replaceState|url\.searchParams\.delete", boot), (
        "nothing ever clears ?tab=/?day= out of the address once it has been honoured, so every "
        "later refresh of that surface re-applies a link he followed hours ago"
    )


def test_07_the_tab_left_open_overnight_says_which_tab_it_is(open_box) -> None:
    """Tuesday 08:10. the owner has eleven tabs open on his laptop and one of them is Tiffin,
    parked on Trends. Every one of them is titled "tiffin", so finding the page again means
    opening tabs until he hits it - and a bookmark of the Menu tab is indistinguishable from a
    bookmark of Today."""
    box = open_box(at(MONDAY, 8, 10))
    shell = box.shell()
    assert "<title>Tiffin</title>" in shell, "the shell ships one static title"
    assert re.search(r"document\.title\s*=", box.app_js()), (
        "the title is never changed as the tab changes: the browser tab, the history entries and "
        "any bookmark all read 'tiffin' whichever of the seven sections is open"
    )


# -- steps ---------------------------------------------------------------------------------------


def test_08_the_evening_nudge_about_steps_links_to_the_steps_chart(open_box) -> None:
    """Monday 20:30. the owner finished on 5,900 steps against a floor of 8,000 and wants to know
    whether that is this week or this month. The chart exists - api/trends?metric=steps answers
    with the week and the floor - but no address opens the page on it: ?metric= is not read, and
    no message anywhere links to the Trends tab at all."""
    box = open_box(at(MONDAY, 20, 30))
    chart = box.as_owner("api/trends?metric=steps&days=30").json()
    assert chart["raw"] and any(v is not None for v in chart["raw"]), "the data is there"
    assert chart.get("target"), "and the floor line with it"

    js = box.app_js()
    read = sorted(set(re.findall(r'\.get\("([a-z]+)"\)', js)))
    assert "metric" in read, (
        f"the page reads only {read} out of its own address, so ?tab=trends&metric=steps opens "
        "the Trends tab on Weight and he has to find the metric himself"
    )
    assert "tab=trends" in render_mod_source(), (
        "and no link builder in the bot can address any tab but Menu, so nothing he is sent at "
        "20:30 can put the steps chart in front of him"
    )


def test_09_from_todays_step_count_to_the_steps_trend_in_one_tap(open_box) -> None:
    """Monday 21:00 on the sofa. The Today tab shows a Steps card: 5,900, floor 8,000, bar part
    full. The obvious next question is "and the rest of the week?" - the card should be the way
    there. It is a div, so the route is: tap Trends, open the Metric select, pick Steps, and the
    days select is still on 90."""
    box = open_box(at(MONDAY, 21, 0))
    today = box.as_owner("api/today").json()
    assert today["wellness"] and today["wellness"]["steps"] == 5900
    assert today["profile"]["steps_floor"] == 8000, "the card has both numbers"

    js = box.app_js()
    card_call = re.search(r'card\("Steps",[^;]*\);', js)
    assert card_call is not None, "renderToday() builds a Steps card"
    taps = ["Trends tab", "open the Metric select", "choose Steps"]
    assert "showTab" in card_call.group(0) or "addEventListener" in card_call.group(0), (
        f"the Steps card is inert: {len(taps)} taps ({', '.join(taps)}) separate today's step "
        "count from the steps chart, and nothing on the card hints the chart exists"
    )


# -- the cook ------------------------------------------------------------------------------------


def test_10_owner_skipped_the_mon_tue_cook_and_wants_a_link_to_the_wed_fri_batch(open_box) -> None:
    """Wednesday 18:00. the owner skipped the Monday cook and the Tuesday one; he is cooking
    tonight for
    Wednesday to Friday only. /batch in Telegram gives him the pots, and he wants the same thing
    on the page - a batch view he can keep open on the counter, reached by a link that says which
    day the cook starts from. No tab renders it, no route answers it, and plan() has no from-day."""
    box = open_box(at(WEDNESDAY, 18, 0))
    menu_row = box.db.one("SELECT * FROM menus WHERE id = ?", (box.menus["owner"],))
    assert menu_row is not None
    load = batch_mod.plan(box.db, menu_row, ("lunch",), WEDNESDAY, freezes=True)
    assert load.dishes and load.dishes[0].boxes == 5, (
        f"plan() still cooks all five days from Wednesday: {load.dishes[0].boxes} boxes, days "
        f"{[d.isoformat() for d in load.dishes[0].days]} - there is no 'since which day' to give it"
    )

    assert box.as_owner("api/batch?from=" + WEDNESDAY.isoformat()).status_code == 200, (
        "no route answers the cook, so the address ?tab=batch&from=2026-09-16 has nothing behind "
        "it: the batch plan lives only in a Telegram message that scrolls away"
    )
    assert 'data-view="batch"' in box.shell(), "and no tab renders it either"
