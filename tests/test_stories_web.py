"""Twenty-five stories from the tailnet page: one browser doing something slightly off.

`test_web.py` covers what the page is for and `test_audit_web.py` sweeps every route for
crashes and strangers. This is the ground in between - a login shouted in capitals, a date
typed into the address bar, the same button pressed twice, a second tab, an asset name the
page never mints - each one a thing a person can actually do on a sofa.

The rules underneath are the house ones. The person comes from the transport and never from
the request. A ledger is appended to, never edited. A page that cannot answer refuses out
loud rather than breaking.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tiffin import body as body_mod
from tiffin import db as dbmod
from tiffin.app import App
from tiffin.clock import FrozenClock
from tiffin.config import Config
from tiffin.db import Db
from tiffin.secrets import Secrets
from tiffin.web import vendor
from tiffin.web.app import create_app

from .fakes import FakeTelegram

HEADER = "Tailscale-User-Login"
OWNER_LOGIN = "alex@example.com"
PARTNER_LOGIN = "sam@example.com"
TODAY = "2026-09-07"  # a Monday, and the frozen clock's today
#: a dish name carrying markup, the way a model or a person could have typed it
MARKUP_DISH = "<script>alert('chilli')</script>"


def seed(db: Db, person_id: int) -> dict[str, int]:
    """One accepted menu with two dishes, a shopping list with one item, one weigh-in.

    Deliberately no `day_confirmations` row: a day with a menu on it and nothing confirmed
    yet is the state the page is in from breakfast until the evening message.
    """
    ing = db.insert(
        "ingredients",
        code=f"rice{person_id}",
        version=1,
        name="Rice",
        kcal_100=350,
        protein_100=8,
        fat_100=1,
        carb_100=78,
        fibre_100=1.4,
        pack_g=1000,
        pack_label="1 kg bag",
        aisle="grains",
        pantry=0,
        use_whole_pack=0,
        approved=1,
        person_scope="both",
    )
    menu = db.insert(
        "menus",
        person_id=person_id,
        segment_start=TODAY,
        segment_end="2026-09-11",
        status="accepted",
    )
    menu_day = db.insert(
        "menu_days",
        menu_id=menu,
        day=TODAY,
        kcal=2000,
        protein_g=180,
        fat_g=70,
        carb_g=200,
        fibre_g=30,
    )
    ids: dict[str, int] = {}
    for slot, name in (("dinner", "Chilli"), ("lunch", MARKUP_DISH)):
        recipe = db.insert(
            "recipes",
            name=name,
            servings=1,
            prep_minutes=20,
            method_json='["Cook it."]',
            source="model",
        )
        db.insert("recipe_ingredients", recipe_id=recipe, ingredient_id=ing, grams=100)
        db.insert(
            "meal_slots",
            menu_day_id=menu_day,
            slot=slot,
            recipe_id=recipe,
            portion_multiplier=1.0,
            portion_g=100,
            kcal=350,
            protein_g=8,
            fat_g=1,
            carb_g=78,
            fibre_g=1.4,
        )
        ids[slot] = recipe
    shopping_list = db.insert("shopping_lists", menu_id=menu, kind="segment", person_id=person_id)
    ids["item"] = db.insert(
        "shopping_items",
        list_id=shopping_list,
        ingredient_id=ing,
        grams_needed=500,
        packs=1,
        leftover_g=500,
        aisle="grains",
    )
    body_mod.record_weigh_in(db, person_id, dt.date(2026, 9, 7), 100.9, "manual")
    return ids


def tombstone(db: Db, person_id: int) -> None:
    """What `admin.erase_person` does to the persons row, without the rest of an erasure."""
    db.con.executescript(dbmod.drop_trigger_sql("persons"))
    try:
        db.execute(
            "UPDATE persons SET erased_at = ?, tailscale_login = NULL WHERE id = ?",
            (db.clock.utc_stamp(), person_id),
        )
    finally:
        db.con.executescript(dbmod.trigger_sql("persons"))


@dataclasses.dataclass
class Page:
    """A signed-in page client and its backing application."""

    client: TestClient
    db: Db
    owner_id: int
    partner_id: int
    ids: dict[str, int]
    config: Config
    clock: FrozenClock
    secrets_dir: Path

    def as_owner(self, path: str, **kw: Any):
        return self.client.get(path, headers={HEADER: OWNER_LOGIN}, **kw)

    def as_partner(self, path: str, **kw: Any):
        return self.client.get(path, headers={HEADER: PARTNER_LOGIN}, **kw)

    def token(self) -> str:
        return str(self.as_owner("api/me").json()["csrf"])

    def write(self, path: str, body: Any):
        """A POST the way the page makes one: the header, the token it was handed, JSON."""
        headers = {HEADER: OWNER_LOGIN, "X-CSRF-Token": self.token()}
        return self.client.post(path, json=body, headers=headers)

    def hashed(self, name: str) -> str:
        """The URL the shell actually asks for, content hash and all."""
        stem, _, ext = name.rpartition(".")
        found = re.search(
            rf"static/{re.escape(stem)}\.[0-9a-f]{{10}}\.{ext}", self.as_owner("/").text
        )
        assert found is not None, f"the page does not reference {name}"
        return found.group(0)

    def variant(self, **over: Any) -> TestClient:
        """The same box and the same database, started with one setting changed."""
        config = dataclasses.replace(self.config, **over)
        app = App(config, Secrets(self.secrets_dir), clock=self.clock, transport=FakeTelegram())
        return TestClient(create_app(app), base_url="http://testserver")

    def bound_to(self, host: str) -> TestClient:
        data = json.loads(json.dumps(self.config.data))
        data["web"]["host"] = host
        return self.variant(data=data)


@pytest.fixture
def page(config, frozen_clock: FrozenClock, tmp_path: Path) -> Iterator[Page]:
    """The owner with a menu, a list and one weigh-in; her on the tailnet with nothing at all."""
    config.paths.ensure()
    db = Db.open(config.paths.db, clock=frozen_clock)
    owner_id = db.insert("persons", slug="owner", display_name="Alex", tone="blunt")
    partner_id = db.insert("persons", slug="partner", display_name="Sam", tone="warm")
    db.insert("tailscale_identities", person_id=owner_id, login=OWNER_LOGIN)
    db.insert("tailscale_identities", person_id=partner_id, login=PARTNER_LOGIN)
    ids = seed(db, owner_id)
    secrets_dir = tmp_path / "s"
    secrets_dir.mkdir()
    (secrets_dir / "telegram-token-dev.txt").write_text("1:t", encoding="utf-8")
    config.data["web"]["secure_cookies"] = False
    # served at the root in tests; Tailscale Serve supplies the /tiffin prefix in production
    config.data["web"]["root_path"] = ""
    app = App(config, Secrets(secrets_dir), clock=frozen_clock, transport=FakeTelegram())
    client = TestClient(create_app(app), base_url="http://testserver")
    try:
        yield Page(client, db, owner_id, partner_id, ids, config, frozen_clock, secrets_dir)
    finally:
        db.close()


# -- who is asking -----------------------------------------------------------------------------


def test_01_a_login_shouted_in_capitals_still_finds_the_person(page: Page) -> None:
    """The login is whatever the identity provider spelled it, and mail addresses come back
    capitalised often enough. Both identity tables say COLLATE NOCASE; if the page disagreed,
    a re-issued header would lock someone out of their own tiffin with no way to say so."""
    shouted = page.client.get("api/whoami", headers={HEADER: OWNER_LOGIN.upper()})
    assert shouted.status_code == 200
    assert shouted.json() == {"slug": "owner", "person_id": page.owner_id}


def test_02_a_login_padded_with_spaces_is_refused_rather_than_guessed_at(page: Page) -> None:
    """`login_digest` and `attach_tailscale_login` both canonicalise with `.strip()`; the
    lookup does not, so a padded header matches nobody. That is the safe direction - it
    refuses instead of guessing - and this pins which direction it falls, because the unsafe
    one would be a header that quietly resolves to whoever is nearest."""
    padded = page.client.get("api/whoami", headers={HEADER: f"  {OWNER_LOGIN} "})
    assert padded.status_code == 403
    assert padded.json()["error"] == "unknown identity"


def test_03_a_header_that_is_empty_or_names_two_people_names_nobody(page: Page) -> None:
    """Serve sets this header itself and strips any client copy, so these shapes only arrive
    from a proxy that is misconfigured or bypassed. What must never happen is the second name
    winning: that is the whole of the attack, and the reason identity is resolved before
    routing rather than inside a handler."""
    empty = page.client.get("api/whoami", headers={HEADER: ""})
    assert empty.status_code == 403 and empty.json()["error"] == "no identity"
    joined = page.client.get("api/whoami", headers={HEADER: f"{OWNER_LOGIN}, {PARTNER_LOGIN}"})
    assert joined.status_code == 403 and joined.json()["error"] == "unknown identity"
    appended = page.client.get(
        "api/whoami",
        headers=[
            (b"tailscale-user-login", OWNER_LOGIN.encode()),
            (b"tailscale-user-login", PARTNER_LOGIN.encode()),
        ],
    )
    assert appended.json().get("person_id") != page.partner_id, "an appended login names nobody"


def test_04_only_the_health_check_answers_without_a_login(page: Page) -> None:
    """The health endpoint is available unauthenticated and everything else is behind the
    header - the shell, the manifest and the script bundle included. A static file that
    answered anonymously would hand the app's whole shape to anything that can reach 7788."""
    assert page.client.get("api/health").status_code == 200
    for path in ("/", "manifest.webmanifest", "api/me", "api/today", "static/app.js"):
        anonymous = page.client.get(path)
        assert anonymous.status_code == 403, path
        if path.startswith("api/"):
            assert anonymous.json()["error"] == "no identity", path
        else:
            # a phone opening the page gets a sentence it can act on, not raw JSON - and still
            # nothing of the app's shape or anyone's food
            assert anonymous.headers["content-type"].startswith("text/html"), path
            assert "tailnet" in anonymous.text and "kcal" not in anonymous.text, path


def test_05_the_dev_header_stops_naming_anyone_once_the_env_is_prod(page: Page) -> None:
    """`X-Dev-Person: max` is a shell convenience so a browser on the box can open the page
    without Serve in front of it. On the box the header is the whole of the identity, so a
    bypass that survived into prod would make either person one curl flag away."""
    dev = page.client.get("api/whoami", headers={"X-Dev-Person": "owner"})
    assert dev.status_code == 200 and dev.json()["slug"] == "owner"
    prod = page.variant(env="prod").get("api/whoami", headers={"X-Dev-Person": "owner"})
    assert prod.status_code == 403 and prod.json()["error"] == "no identity"


def test_06_the_dev_header_stops_naming_anyone_once_the_bind_leaves_the_loopback(
    page: Page,
) -> None:
    off_box = page.bound_to("100.64.0.1")
    assert off_box.get("api/whoami", headers={"X-Dev-Person": "owner"}).status_code == 403
    on_box = page.client.get("api/whoami", headers={"X-Dev-Person": "partner"})
    assert on_box.status_code == 200 and on_box.json()["slug"] == "partner"


def test_07_a_real_login_beats_a_dev_header_naming_the_other_person(page: Page) -> None:
    """Both headers on one request, in dev, where the bypass is live. The transport's own
    answer has to win; if the convenience header could override it, reading her page would be
    a header away from anything running on the box."""
    both = page.client.get("api/whoami", headers={HEADER: OWNER_LOGIN, "X-Dev-Person": "partner"})
    assert both.status_code == 200 and both.json()["person_id"] == page.owner_id


def test_08_a_login_belonging_to_an_erased_person_is_refused(page: Page) -> None:
    """Erasure is the one carve-out in the append-only rule and it is CLI-only, so the page
    never sees it happen - it simply has to stop answering. A tombstoned person whose header
    still worked is an erasure that erased nothing anybody could check."""
    assert page.as_owner("api/whoami").status_code == 200
    tombstone(page.db, page.owner_id)
    gone = page.as_owner("api/whoami")
    assert gone.status_code == 403 and gone.json()["error"] == "unknown identity"
    assert page.as_partner("api/whoami").status_code == 200, "the other person is untouched"


def test_09_the_person_never_comes_from_the_query_string_or_the_body(page: Page) -> None:
    """The house rule with no exceptions. Every route resolves the asker before routing, so
    naming somebody in a URL is not a smaller privilege - it is no privilege at all."""
    for query in ("?person=her", f"?person_id={page.partner_id}", "?slug=her&as=her"):
        answer = page.as_owner("api/whoami" + query)
        assert answer.json() == {"slug": "owner", "person_id": page.owner_id}, query
    smuggled = page.write(
        "api/shopping/tick",
        {"item_id": page.ids["item"], "ticked": True, "person_id": page.partner_id},
    )
    assert smuggled.status_code == 400 and "person_id" in smuggled.json()["detail"]
    assert page.db.scalar("SELECT COUNT(*) FROM shopping_ticks") == 0


# -- a date typed into the address bar ---------------------------------------------------------


def test_10_a_day_outside_every_record_is_an_empty_day_not_an_error(page: Page) -> None:
    """Scrolling back through a history that has not happened yet, or one that predates the
    box, has to look like an empty day rather than a broken tab."""
    for day in ("2099-01-01", "1999-12-31"):
        answer = page.as_owner(f"api/today?date={day}")
        assert answer.status_code == 200, day
        seen = answer.json()
        assert seen["day"] == day and seen["record"] is None and seen["meals"] == []


def test_11_the_thirtieth_of_february_is_refused_as_a_bad_date(page: Page) -> None:
    bad = page.as_owner("api/today?date=2026-02-30")
    assert bad.status_code == 400 and bad.json()["detail"] == "bad date"
    assert page.as_owner("api/today?date=2026-02-28").status_code == 200


def test_12_a_date_no_browser_could_have_produced_is_refused_by_every_route(page: Page) -> None:
    """Four routes read a day out of the query string under four different names, and a
    person editing the address bar or following a stale bookmark reaches all four. One that
    parsed loosely would answer for a day nobody asked about."""
    routes = ("api/today?date=", "api/week?start=", "api/menu?segment=", "api/summary/weekly?week=")
    for shape in ("yesterday", "2026-09-07Z", "2026-09-07T00:00:00", "07/09/2026", "2026-13-01"):
        for route in routes:
            answer = page.as_owner(route + shape)
            assert answer.status_code == 400, f"{route}{shape}: {answer.status_code}"
            assert answer.json()["detail"] == "bad date", f"{route}{shape}"


def test_13_an_empty_date_in_the_address_bar_means_today(page: Page) -> None:
    """A page that builds its own URLs will build `?date=` with nothing after it sooner or
    later, and `parse_day("")` raises. Falling back to today is what keeps the commonest tab
    from opening as a 400."""
    today = page.as_owner("api/today?date=")
    assert today.status_code == 200 and today.json()["day"] == TODAY
    week = page.as_owner("api/week?start=")
    assert week.status_code == 200 and week.json()["week_start"] == TODAY
    menu = page.as_owner("api/menu?segment=")
    assert menu.status_code == 200 and menu.json()["menu"] is not None


# -- the same button twice, and bodies nobody meant to send -------------------------------------


def test_14_ticking_the_same_item_twice_appends_a_row_and_answers_twice(page: Page) -> None:
    """A tick is a ledger row, not a flag, so the second tap cannot be swallowed as a
    duplicate - it appends, and the newest row is what the aisle reads. What a person must
    never see is the second tap failing, or the item appearing twice in the list."""
    item = page.ids["item"]
    first = page.write("api/shopping/tick", {"item_id": item, "ticked": True})
    second = page.write("api/shopping/tick", {"item_id": item, "ticked": True})
    assert first.status_code == 200 and second.status_code == 200
    assert page.db.scalar("SELECT COUNT(*) FROM shopping_ticks WHERE item_id = ?", (item,)) == 2
    items = page.as_owner("api/shopping").json()["items"]
    assert [i["ticked"] for i in items] == [True], "one line in the aisle, and it is ticked"
    undone = page.write("api/shopping/tick", {"item_id": item, "ticked": False})
    assert undone.status_code == 200
    assert page.as_owner("api/shopping").json()["items"][0]["ticked"] is False


def test_15_rating_the_same_dish_twice_leaves_the_newer_score_showing(page: Page) -> None:
    """`ratings` is append-only, so changing your mind writes a second row. The read has to
    take the newest one; taking the first would freeze a dish for good at the score it got on
    the night it was undercooked, and the menu proposer reads those scores."""
    recipe = page.ids["dinner"]
    assert page.write("api/dishes/rate", {"recipe_id": recipe, "score": 5}).status_code == 200
    assert page.write("api/dishes/rate", {"recipe_id": recipe, "score": 2}).status_code == 200
    assert page.db.scalar("SELECT COUNT(*) FROM ratings WHERE recipe_id = ?", (recipe,)) == 2
    shown = {d["id"]: d["score"] for d in page.as_owner("api/dishes").json()["dishes"]}
    assert shown[recipe] == 2, "the correction is the row that wins"
    assert page.db.scalar("SELECT COUNT(*) FROM web_events WHERE kind = 'dish_rating'") == 2


def test_16_a_tick_whose_fields_arrive_as_text(page: Page) -> None:
    """Everything in a URL and most things in a hand-written body are strings. `item_id` goes
    through a real parse, but `ticked` goes through `bool()`, and `bool("false")` is True - so
    the string a form or a hand-rolled client is likeliest to send means the opposite of what
    it says. Every other field on this surface is refused when it is the wrong type."""
    item = page.ids["item"]
    numeric = page.write("api/shopping/tick", {"item_id": str(item), "ticked": True})
    assert numeric.status_code == 200, "a row id as text is still a row id"
    worded = page.write("api/shopping/tick", {"item_id": item, "ticked": "false"})
    assert (
        worded.status_code == 400 or not page.as_owner("api/shopping").json()["items"][0]["ticked"]
    )


def test_17_a_body_bigger_than_the_fence_allows_is_refused_before_it_is_read(page: Page) -> None:
    """The note on a rating is a free-text box on a phone, which is one long-press from a
    whole pasted page. The cap has to bite before the JSON is parsed, not after."""
    recipe = page.ids["dinner"]
    huge = page.write("api/dishes/rate", {"recipe_id": recipe, "score": 4, "note": "x" * 9000})
    assert huge.status_code == 413
    assert page.db.scalar("SELECT COUNT(*) FROM ratings") == 0
    sized = page.write("api/dishes/rate", {"recipe_id": recipe, "score": 4, "note": "x" * 4000})
    assert sized.status_code == 200


def test_18_a_second_tab_does_not_break_the_first_tabs_writes(page: Page) -> None:
    """Re-minting the token on every /api/me meant the second surface to load - another tab,
    or the installed app beside a browser - silently killed the first: every tick and every
    rating from it failed 403, and the message auto-hid after six seconds."""
    first = page.as_owner("api/me").json()["csrf"]
    second = page.as_owner("api/me").json()["csrf"]
    assert second == first, "the second page load keeps the token the first was handed"
    still_good = page.client.post(
        "api/shopping/tick",
        json={"item_id": page.ids["item"], "ticked": True},
        headers={HEADER: OWNER_LOGIN, "X-CSRF-Token": first},
    )
    assert still_good.status_code == 200


# -- what the page has to show when there is nothing to show ------------------------------------


def test_19_a_person_with_no_data_at_all_gets_empty_answers_not_a_broken_tab(page: Page) -> None:
    """Her first evening: paired to the tailnet, no profile, no targets, no menu, no weigh-in.
    Every tab has to open, because the first visit is the one that decides whether she comes
    back, and an empty tab she can read beats a spinner she cannot."""
    today = page.as_partner("api/today").json()
    assert today["targets"] is None and today["record"] is None and today["profile"] is None
    assert today["meals"] == [] and today["sessions"] == []
    assert page.as_partner("api/week").json()["summary"] is None
    assert page.as_partner("api/menu").json() | {"weekend": None} == {
        "menu": None,
        "days": [],
        "covers_today": False,
        "weekend": None,
    }
    assert page.as_partner("api/shopping").json() == {"list": None, "items": []}
    assert page.as_partner("api/dishes").json() == {"dishes": []}
    assert page.as_partner("api/summary/weekly").json()["text"] is None
    assert page.as_partner("/").status_code == 200


def test_20_a_day_with_a_menu_but_no_confirmation_shows_the_meals_and_no_record(page: Page) -> None:
    """Every day looks like this between the menu landing and the evening confirm, which is
    most of every day. The meals have to be there to cook from while the record is still
    empty - a page that showed nothing until something was confirmed would be blank at lunch."""
    today = page.as_owner(f"api/today?date={TODAY}").json()
    assert today["record"] is None, "nothing has been confirmed yet"
    assert [m["slot"] for m in today["meals"]] == ["lunch", "dinner"]
    assert today["meals"][1]["name"] == "Chilli"


def test_21_a_chart_with_one_point_and_a_chart_with_none(page: Page) -> None:
    """One weigh-in is what the first week looks like and an untaken tape measure is what most
    weeks look like. Both have to come back as something drawable, still naming the window
    they cover, or the Trends tab is empty with no way to tell empty from broken."""
    weight = page.as_owner("api/trends?metric=weight&days=90").json()
    assert len(weight["dates"]) == 1 and weight["raw"] == [pytest.approx(100.9)]
    waist = page.as_owner("api/trends?metric=waist&days=90").json()
    assert waist["dates"] == [] and waist["raw"] == []
    assert waist["from"] and waist["to"], "an empty chart still says which window it covers"
    assert page.as_owner("api/trends?metric=weight&days=-5").status_code == 200
    assert page.as_owner("api/trends?metric=weight&days=ages").status_code == 400


def test_22_a_dish_named_with_a_script_tag_arrives_as_data_never_as_markup(page: Page) -> None:
    """A recipe name carries whatever a person or a model typed. The page is a static shell -
    nothing on the server ever interpolates anybody's words into HTML - so the proof is that
    the name travels as a JSON string under a JSON content type that must not be sniffed, and
    that the shell served at / carries no trace of either person."""
    answer = page.as_owner("api/menu")
    assert answer.headers["content-type"].startswith("application/json")
    assert answer.headers["x-content-type-options"] == "nosniff"
    assert answer.json()["days"][0]["slots"][0]["name"] == MARKUP_DISH
    assert MARKUP_DISH in [d["name"] for d in page.as_owner("api/dishes").json()["dishes"]]
    shell = page.as_owner("/").text
    assert "alert(" not in shell and "Chilli" not in shell and "Alex" not in shell
    assert "{{" not in shell, "nothing in the template is left unrendered"


# -- the files the page loads -------------------------------------------------------------------


def test_23_a_static_name_the_page_never_mints_finds_nothing(page: Page) -> None:
    """Modules use stable, revalidated URLs; missing assets never reveal source files."""
    assert 'src="static/app.js"' in page.as_owner("/").text
    module = page.as_owner("static/app.js")
    assert module.status_code == 200
    assert module.headers["cache-control"] == "private, no-cache"
    for name in ("nope.js", "..%5Capi.py", "..%2F..%2Fapi.py", "../api.py", "index.html"):
        missed = page.as_owner(f"static/{name}")
        assert missed.status_code == 404, name
        assert "import" not in missed.text and "def " not in missed.text, name


def test_24_the_plotly_bundle_the_page_loads_is_the_pinned_one(page: Page) -> None:
    """The CSP forbids loading anything from off the box, so the chart library lives in the
    repo. `tiffin doctor` compares the file on disk against the pin; this walks the path the
    browser takes instead - the URL written into the shell - so a bundle swapped without being
    re-pinned is caught where it is served, not only where it is stored."""
    bundle = page.as_owner(page.hashed(vendor.PLOTLY_FILE))
    assert bundle.status_code == 200
    assert hashlib.sha256(bundle.content).hexdigest() == vendor.PLOTLY_SHA256
    assert len(bundle.content) == vendor.PLOTLY_BYTES
    assert bundle.headers["content-type"].startswith("text/javascript")
    assert bundle.headers["cache-control"] == "public, max-age=31536000, immutable"
    shell = page.as_owner("/").text
    assert "cdn.plot.ly" not in shell and "https://" not in shell


def test_25_an_answer_about_a_person_is_never_stored_by_anything_downstream(page: Page) -> None:
    """The shell's headers are pinned elsewhere; the JSON is the part that carries the weight,
    the day and the menu, and it crosses Serve to a phone that may be sharing a browser
    profile. Every read has to say no-store, and say it without being asked twice."""
    for path in (
        "api/me",
        "api/today",
        "api/week",
        "api/trends?metric=weight",
        "api/menu",
        "api/shopping",
        "api/dishes",
        "api/summary/weekly",
    ):
        answer = page.as_owner(path)
        assert answer.status_code == 200, path
        assert answer.headers["cache-control"] == "private, no-store", path
        assert answer.headers["x-content-type-options"] == "nosniff", path
        assert answer.headers["referrer-policy"] == "no-referrer", path
        assert "frame-ancestors 'none'" in answer.headers["content-security-policy"], path
