"""Ten moments where the tailnet page has nothing to show, or cannot show it.

The theme is the one rule the whole box is built on - *degrade to manual, never to silence* -
applied to the web page rather than to a job. Every empty box and every refusal has to say what
to do next, in words, to the person holding the phone. A blank panel, a bare status code or a
raw `{"error": ...}` is the failure mode, and it is the state the partner was actually left in on
14 Sep when the page handed her phone `{"error":"unknown identity"}` as text.

Each story drives the real API exactly as the SPA does - same GET, same header, same token - and
then reads `static/app.js` to see what the tab makes of that payload, because the payload is only
half of what the person gets. Stories that pass are guards on empty states that already speak;
stories that fail name a place where the page goes quiet.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import re
import sqlite3
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
from tiffin.web import queries
from tiffin.web.app import create_app

from .fakes import FakeTelegram
from .web_source import app_source

HEADER = "Tailscale-User-Login"
OWNER_LOGIN = "alex@example.com"
PARTNER_LOGIN = "sam@example.com"

MONDAY = dt.date(2026, 9, 14)
WEDNESDAY = dt.date(2026, 9, 16)
THURSDAY = dt.date(2026, 9, 17)
SATURDAY = dt.date(2026, 9, 19)
SUNDAY = dt.date(2026, 9, 20)

#: phrases that count as "what to do next" - a command, the bot, or an act the person can do
NEXT_STEP = ("/cook", "/shopping", "/menu", "/recipe", "/set", "Telegram", "bot", "onboard")


def at(day: dt.date, hh: int, mm: int) -> dt.datetime:
    return dt.datetime(day.year, day.month, day.day, hh, mm, tzinfo=TZ)


# -- the box -------------------------------------------------------------------------------------


def _profile(db: Db, person_id: int) -> None:
    db.insert(
        "profiles",
        person_id=person_id,
        effective_from="2026-09-01",
        sex="m",
        birth_date="1988-03-04",
        height_cm=183.0,
        activity_factor=1.5,
        kcal_floor=1600,
        steps_floor=9000,
        segment_days="[[0,1,2,3,4]]",
    )


def _ingredient(db: Db, code: str, name: str) -> int:
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
        aisle="grains",
        pantry=0,
        use_whole_pack=0,
        approved=1,
        person_scope="both",
    )


def _menu_with_list(db: Db, person_id: int, start: dt.date, days: int) -> tuple[int, int]:
    """One accepted segment and the shopping list that was built for it."""
    rid = db.insert(
        "recipes",
        name="Chicken and rice boxes",
        servings=4,
        prep_minutes=25,
        method_json=json.dumps(["Air fry the chicken.", "Cook the rice."]),
        source="model",
        keep_days=3,
        freezable=1,
    )
    ing = _ingredient(db, "rice", "Rice, basmati, dry")
    db.insert("recipe_ingredients", recipe_id=rid, ingredient_id=ing, grams=800)
    end = start + dt.timedelta(days=days - 1)
    mid = db.insert(
        "menus",
        person_id=person_id,
        segment_start=start.isoformat(),
        segment_end=end.isoformat(),
        status="accepted",
    )
    for i in range(days):
        did = db.insert(
            "menu_days",
            menu_id=mid,
            day=(start + dt.timedelta(days=i)).isoformat(),
            kcal=1800,
            protein_g=150,
            fat_g=60,
            carb_g=160,
            fibre_g=28,
        )
        db.insert(
            "meal_slots",
            menu_day_id=did,
            slot="lunch",
            recipe_id=rid,
            portion_multiplier=1.0,
            portion_g=350,
            kcal=550,
            protein_g=45,
            fat_g=18,
            carb_g=50,
            fibre_g=8,
        )
    lid = db.insert(
        "shopping_lists",
        menu_id=mid,
        kind="segment",
        person_id=person_id,
        created_at="2026-09-13 16:00:27",
    )
    db.insert(
        "shopping_items",
        list_id=lid,
        ingredient_id=ing,
        grams_needed=800,
        packs=2,
        leftover_g=0,
        aisle="grains",
    )
    return mid, lid


@dataclasses.dataclass
class Box:
    """The page at one moment, plus the database behind it and the script it serves."""

    client: TestClient
    app: Any
    db: Db
    clock: FrozenClock
    owner_id: int
    partner_id: int

    def as_owner(self, path: str, **kw: Any):
        return self.client.get(path, headers={HEADER: OWNER_LOGIN}, **kw)

    def as_partner(self, path: str, **kw: Any):
        return self.client.get(path, headers={HEADER: PARTNER_LOGIN}, **kw)

    def app_js(self) -> str:
        """The script the shell actually loads, by the hashed name written into it."""
        return app_source()

    def fragile_client(self) -> TestClient:
        """The same app, but a 500 comes back as a response instead of re-raising."""
        return TestClient(self.app, base_url="http://testserver", raise_server_exceptions=False)


@pytest.fixture
def open_box(config: Config, tmp_path: Path) -> Iterator[Callable[..., Box]]:
    opened: list[Db] = []

    def make(when: dt.datetime, *, profile: bool = True) -> Box:
        clock = FrozenClock(when)
        config.paths.ensure()
        db = Db.open(config.paths.db, clock=clock)
        opened.append(db)
        owner_id = db.insert("persons", slug="owner", display_name="Alex", tone="blunt")
        partner_id = db.insert("persons", slug="partner", display_name="Sam", tone="warm")
        db.insert("tailscale_identities", person_id=owner_id, login=OWNER_LOGIN)
        db.insert("tailscale_identities", person_id=partner_id, login=PARTNER_LOGIN)
        if profile:
            _profile(db, owner_id)
        secrets_dir = tmp_path / "s"
        secrets_dir.mkdir(exist_ok=True)
        (secrets_dir / "telegram-token-dev.txt").write_text("1:t", encoding="utf-8")
        config.data["web"]["secure_cookies"] = False
        config.data["web"]["root_path"] = ""
        app = App(config, Secrets(secrets_dir), clock=clock, transport=FakeTelegram())
        web = create_app(app)
        return Box(
            TestClient(web, base_url="http://testserver"), web, db, clock, owner_id, partner_id
        )

    yield make
    for db in opened:
        db.close()


# -- reading the SPA -----------------------------------------------------------------------------


def render_source(js: str, name: str) -> str:
    """The body of one top-level render function, which is what a tab puts on the screen."""
    found = re.search(rf"\nasync function {name}\([^)]*\) \{{[\s\S]*?\r?\n\}}\r?\n", js)
    assert found is not None, f"{name} is not a top-level function in app.js any more"
    return found.group(0)


def strings_in(source: str) -> list[str]:
    """Every literal the person could read: "quoted", 'quoted' and `templated`."""
    out: list[str] = []
    for pattern in (r'"([^"\\\n]*)"', r"'([^'\\\n]*)'", r"`([^`\\]*)`"):
        out += re.findall(pattern, source)
    return [s for s in out if s.strip()]


def without_block(source: str, opener: str) -> str:
    """The same source with one `if (...) { ... }` block cut out, braces matched."""
    start = source.find(opener)
    assert start >= 0, f"{opener!r} is not in the source any more"
    depth = 0
    for i in range(start, len(source)):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[:start] + source[i + 1 :]
    raise AssertionError(f"{opener!r} has no matching brace")


def says_next_step(text: str) -> bool:
    return any(step in text for step in NEXT_STEP)


def next_steps_in(source: str) -> list[str]:
    """Every literal in this code that would tell the person what to do."""
    return [s for s in strings_in(source) if says_next_step(s)]


# -- 01: the first morning -----------------------------------------------------------------------


def test_01_a_new_person_opening_today_before_anything_exists_is_told_where_to_finish(
    open_box,
) -> None:
    """Monday 07:10. the owner has paired the bot and answered nothing else: no profile, no targets,
    no menu, no weigh-in. He opens the page because the morning message linked to it, and Today
    is the tab it lands on. An empty Today has to name the one place the answers go in."""
    box = open_box(at(MONDAY, 7, 10), profile=False)
    data = box.as_owner("api/today").json()
    assert data["targets"] is None and data["record"] is None and data["trend"] is None
    assert data["meals"] == [] and data["estimates"] == []

    render = render_source(box.app_js(), "renderToday")
    empty = next_steps_in(render)
    assert empty, (
        "Today's all-empty state names no next step; he sees a blank tab on the page the "
        "morning message sent him to"
    )
    assert any("Telegram" in s for s in empty), (
        f"the empty Today says {empty!r}, none of which sends him back to the bot"
    )


# -- 02: half set up -------------------------------------------------------------------------


def test_02_weighed_in_with_no_targets_yet_the_calories_card_says_what_is_missing(
    open_box,
) -> None:
    """Wednesday 07:05. The icu sync has pulled three days of weights, so Today is not empty -
    but the Sunday calibration has never run, so there is no `targets` row. The Calories card is
    the first thing on the page and it reads "no targets". That is the name of the gap, not an
    instruction: he needs to be told the week's numbers arrive on a Sunday, or how to ask now."""
    box = open_box(at(WEDNESDAY, 7, 5))
    for offset, kg in ((2, 96.4), (1, 96.1), (0, 95.9)):
        body_mod.record_weigh_in(
            box.db, box.owner_id, WEDNESDAY - dt.timedelta(days=offset), kg, "manual"
        )
    data = box.as_owner("api/today").json()
    assert data["targets"] is None, "this story needs the no-targets state"
    assert data["trend"] is not None, "and it needs the tab to be non-empty otherwise"

    render = render_source(box.app_js(), "renderToday")
    assert "no targets" in render, "the card no longer says 'no targets'; re-read this story"
    guard = re.search(r"const anything = ([\s\S]*?);", render)
    assert guard is not None and "data.trend" in guard.group(1), (
        "one weigh-in makes `anything` true, so the helpful empty state early-returns before "
        "this person ever reaches it"
    )
    # everything the tab can still print once that early return has been skipped
    reachable = without_block(render, "if (!anything) {")
    assert next_steps_in(reachable), (
        "with a weigh-in on file but no targets the page prints the words 'no targets' in the "
        "Calories card and stops. The one sentence Today knows how to say - finish the "
        "questions in Telegram - sits behind an early return that a single weigh-in skips, so "
        "nothing left on the tab says the week's numbers are set on a Sunday or how to ask for "
        f"them now. All it can print: {sorted(set(strings_in(reachable)))!r}"
    )


# -- 03: no menu -----------------------------------------------------------------------------


def test_03_sunday_evening_with_no_menu_accepted_the_menu_tab_says_how_to_build_one(
    open_box,
) -> None:
    """Sunday 18:20. Nothing was accepted on Saturday and the lapse job has not repeated
    anything either, so no menu covers tomorrow. He opens Menu to see what he is cooking."""
    box = open_box(at(SUNDAY, 18, 20))
    data = box.as_owner("api/menu").json()
    assert data["menu"] is None and data["days"] == []

    render = render_source(box.app_js(), "renderMenu")
    empty = [s for s in strings_in(render) if "No menu" in s]
    assert empty, "the Menu tab has no empty state at all"
    assert says_next_step(empty[0]), f"the empty Menu tab says {empty[0]!r} and stops there"


# -- 04: no list ------------------------------------------------------------------------------


def test_04_in_the_shop_with_no_list_the_tab_says_how_to_get_one(open_box) -> None:
    """Saturday 11:15, in Tesco. Last week's Mon-Fri segment ended on Friday, so its list is
    deliberately withheld as stale, and tonight's proposal has not been accepted yet. The
    Shopping tab is therefore empty while he is standing in the shop - the worst possible place
    for a full stop. It has to tell him to send /cook, or that tonight's list arrives at 18:00."""
    box = open_box(at(SATURDAY, 11, 15))
    _menu_with_list(box.db, box.owner_id, MONDAY, 5)  # Mon 14 - Fri 18, over by Saturday
    data = box.as_owner("api/shopping").json()
    assert data["list"] is None, "this story needs the deliberately-withheld stale list"

    render = render_source(box.app_js(), "renderShopping")
    empty = [s for s in strings_in(render) if "shopping list" in s.lower()]
    assert empty, "the Shopping tab has no empty state at all"
    assert says_next_step(empty[0]), (
        f"in the shop, with a list that exists but is being withheld as stale, he reads "
        f"{empty[0]!r} and nothing else. Menu's empty state names /cook; this one names "
        "nothing, and does not say a list is coming at 18:00"
    )


# -- 05: the sync is behind --------------------------------------------------------------------


def test_05_steps_that_have_not_synced_today_are_said_to_be_missing_not_shown_as_nothing(
    open_box,
) -> None:
    """Wednesday 19:30. The hourly icu sync has failed since 06:00 (the key expired), so
    `icu_wellness` stops at Tuesday. the owner opens Today to see whether he is above his 9,000
    floor.
    The Steps card simply does not appear. He cannot tell that from having walked nowhere, and
    the failure is in `run_ledger` where only the owner's admin chat will ever see it."""
    box = open_box(at(WEDNESDAY, 19, 30))
    box.db.insert(
        "icu_wellness",
        person_id=box.owner_id,
        day=(WEDNESDAY - dt.timedelta(days=1)).isoformat(),
        steps=11200,
        icu_raw_id=1,
    )
    box.db.insert(
        "run_ledger",
        run_id="icu_sync-2026-09-16T18",
        job="icu_sync",
        slot="2026-09-16T18",
        person_id=box.owner_id,
        started_at="2026-09-16 18:00:02",
        ended_at="2026-09-16 18:00:04",
        status="failed",
        error="401 from intervals.icu",
    )
    body_mod.record_weigh_in(box.db, box.owner_id, WEDNESDAY, 95.9, "manual")

    yesterday = box.as_owner(
        f"api/today?date={(WEDNESDAY - dt.timedelta(days=1)).isoformat()}"
    ).json()
    assert yesterday["wellness"]["steps"] == 11200, "yesterday's steps are on file"
    data = box.as_owner("api/today").json()
    assert data["wellness"] is None, "and today's are not"
    assert data["profile"]["steps_floor"] == 9000

    render = render_source(box.app_js(), "renderToday")
    step_words = [s for s in strings_in(render) if "step" in s.lower()]
    assert step_words != ["Steps"], (
        "the only thing Today can say about steps is the card label 'Steps', and the card is "
        "gated on today's wellness row existing. With the sync failed since 06:00 he sees no "
        "steps card at all - identical to a day he walked nowhere. Nothing on the tab, and "
        "nothing in the /api/today payload, distinguishes 'not synced' from 'zero', and the "
        f"payload carries no freshness field either: {sorted(data)!r}"
    )


# -- 06: a week not started ----------------------------------------------------------------------


def test_06_the_week_tab_on_monday_morning_explains_why_the_table_is_empty(open_box) -> None:
    """Monday 09:00. Nothing has been confirmed or closed yet this week, so the Week table has
    no rows and there is no `v_week_summary` row either. A header row over an empty table reads
    as broken; it has to say what puts a row there."""
    box = open_box(at(MONDAY, 9, 0))
    data = box.as_owner("api/week").json()
    assert data["summary"] is None and data["days"] == []
    assert data["week_start"] == MONDAY.isoformat()

    shown = box.as_owner("api/progress").json()
    assert len(shown["days"]) == 7 and shown["cards"]["recorded"]["days"] == 0
    shell = box.as_owner("").text
    assert 'src="static/app.js"' in shell
    assert 'from "./progress.js"' in box.as_owner("static/app.js").text
    progress = box.as_owner("static/progress.js")
    assert progress.status_code == 200
    said = [s for s in strings_in(progress.text) if "Intake recorded on" in s]
    assert said, "an empty week says nothing about why it is empty"


# -- 07: last Sunday's summary -------------------------------------------------------------------


def test_07_the_summary_tab_midweek_labels_the_older_summary_as_older(open_box) -> None:
    """Wednesday 08:40. The Sunday summary is written on Sunday evening, so six days in seven
    this tab has only last week's. Showing it silently would be worse than showing nothing: he
    would read last week's numbers as this week's. It has to be labelled and dated."""
    box = open_box(at(WEDNESDAY, 8, 40))
    box.db.insert(
        "messages_out",
        person_id=box.owner_id,
        chat_id=1000001,
        kind="sunday_summary",
        due_key="2026-09-13",
        text_html="<b>Last week</b>: five days confirmed.",
        status="sent",
        sent_at="2026-09-13 18:00:11",
    )
    data = box.as_owner("api/summary/weekly").json()
    assert data["text"] is not None, "the older summary is offered rather than nothing"
    assert data["text"]["current"] is False, "and it is flagged as not this week's"
    assert data["text"]["sent_at"] == "2026-09-13 18:00:11"

    render = render_source(box.app_js(), "renderSummary")
    labels = [s for s in strings_in(render) if "Sunday" in s]
    assert any("The last one" in s or "last" in s.lower() for s in labels), (
        f"the tab prints an older summary with no label saying so: {labels!r}"
    )


# -- 08: identity gone from an open tab ------------------------------------------------------------


def test_08_a_tab_that_loses_its_identity_reads_the_same_sentence_the_refusal_page_carries(
    open_box,
) -> None:
    """Thursday 08:10, on the kitchen tablet. The page has been open since yesterday; Tailscale
    Serve was restarted overnight and the `Tailscale-User-Login` header no longer arrives at the
    app. Every `api/*` call the open page makes now 403s. The refusal the server writes for a
    browser is a real sentence - "the bot in Telegram has everything" - but the one it writes
    for the page's own fetch is a bare code, and that is the only one this person ever sees."""
    box = open_box(at(THURSDAY, 8, 10))
    page = box.client.get("/")  # the HTML refusal: what a cold open gets
    assert page.status_code == 403
    assert "Telegram" in page.text, "the HTML refusal does carry a next step"

    refused = box.client.get("api/today")
    assert refused.status_code == 403
    payload = refused.json()

    js = box.app_js()
    assert "(await response.json()).detail" in js, "api() reads .detail and nothing else"
    assert "detail" in payload, (
        f"the page's own fetch is refused with {payload!r}. app.js reads only `.detail`, so the "
        "toast falls back to the bare HTTP status text and the person on the tablet reads "
        "'403: Forbidden'. The actionable sentence the same refusal writes into the HTML page "
        "never reaches the tab that is actually open"
    )


# -- 09: a tick the fence refuses -------------------------------------------------------------


def test_09_a_tick_refused_by_the_fence_tells_partner_to_reload_rather_than_naming_the_token(
    open_box,
) -> None:
    """Sunday 18:40, in the shop, on her phone. The list has been open since this morning; iOS
    dropped the site's cookie when it reclaimed storage, so the token the page still holds
    matches no cookie. She taps an item and it un-ticks itself. The message she gets is the
    whole of what she has to work with, and it has to name the fix - reload the page."""
    box = open_box(at(SUNDAY, 18, 40))
    _menu_with_list(box.db, box.partner_id, SUNDAY + dt.timedelta(days=1), 5)
    listing = box.as_partner("api/shopping").json()
    item_id = listing["items"][0]["id"]

    box.client.cookies.clear()  # the cookie iOS reclaimed
    refused = box.client.post(
        "api/shopping/tick",
        json={"item_id": item_id, "ticked": True},
        headers={HEADER: PARTNER_LOGIN, "X-CSRF-Token": "the-token-the-page-still-holds"},
    )
    assert refused.status_code == 403
    detail = str(refused.json()["detail"])

    js = box.app_js()
    # a 4xx is shown as the server's own sentence, markup stripped, no status code in front
    assert "new Error(response.status >= 500" in js and ": plain)" in js, "this is what she reads"
    shown = re.sub(r"<[^>]*>", "", detail)
    assert any(word in shown.lower() for word in ("reload", "refresh", "open the page again")), (
        f"she reads {shown!r} - the name of an internal check, in a toast that hides itself "
        "after six seconds, with the item silently un-ticked and no instruction to reload. "
        "api.py's own comment records this exact complaint ('nothing told the person to "
        "reload') and only the token minting was fixed, not the words"
    )


# -- 10: the API falls over --------------------------------------------------------------------


def test_10_a_query_that_blows_up_leaves_the_tab_saying_something(
    open_box, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sunday 19:05. The nightly backup has the database locked and the Week query raises. The
    page has no idea what to do with that: FastAPI's unhandled-error response is plain text, so
    `api()` cannot even read a detail out of it, and the toast it shows hides after six seconds
    and leaves the tab blank with a header row over nothing."""
    box = open_box(at(SUNDAY, 19, 5))

    def locked(*_args: Any, **_kw: Any) -> dict[str, Any]:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(queries, "week", locked)
    broken = box.fragile_client().get("api/week", headers={HEADER: OWNER_LOGIN})
    assert broken.status_code == 500

    js = box.app_js()
    assert re.search(r"setTimeout\(\(\) => \{ box\.hidden = true;[^}]*\}, 6000\)", js), (
        "the toast hides itself"
    )
    handlers = sorted(getattr(k, "__name__", str(k)) for k in box.app.exception_handlers)
    assert broken.headers["content-type"].startswith("application/json"), (
        f"a failed query comes back as {broken.headers['content-type']} "
        f"{broken.text!r}, so app.js's `(await response.json()).detail` throws and falls back to "
        "the bare status text. He reads '500: Internal Server Error' for six seconds, then the "
        "tab is blank with no error and no instruction - the one state the house rule "
        "'degrade to manual, never to silence' exists to forbid. `create_app` registers no "
        f"handler for an unhandled error, only {handlers!r}, so this is every query on every "
        "tab, not just this one"
    )
