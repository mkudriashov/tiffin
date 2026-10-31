"""Ten moments on the Dishes and Summary tabs of the tailnet page.

Dishes is the recipe book with a score on it; Summary is the Sunday review read on a weekday.
Both are surfaces where the person is looking *backwards* - "was that the good one?", "what did
last week actually come to?" - and both are one tap from the Menu tab that plans forwards.

whole segment, so the same dish is written as a fresh `recipes` row for every day it repeats
(README: "A repeated menu writes a fresh recipes row every day it repeats"). He has a July dish
he liked, and two Sunday summaries behind him. the partner is linked to the tailnet and has nothing
yet - her first Sunday has not happened.

Each story drives the page the way the SPA does: the same GET or POST the tab makes, and where
the answer only matters because a tab renders it, the assertion reads the code path in `app.js`
(as `test_journey_web_recipe_book.py` does). A number the API returns that no tab puts on a
screen is not a feature the person has.
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
from tiffin.food import catalogue
from tiffin.food import menu as menu_mod
from tiffin.secrets import Secrets
from tiffin.telegram import menuflow
from tiffin.web.app import create_app

from .fakes import FakeTelegram
from .web_source import app_source

HEADER = "Tailscale-User-Login"
OWNER_LOGIN = "alex@example.com"
PARTNER_LOGIN = "sam@example.com"

LAST_MONDAY = dt.date(2026, 9, 7)
MONDAY = dt.date(2026, 9, 14)
TUESDAY = dt.date(2026, 9, 15)
SUNDAY = dt.date(2026, 9, 20)
#: cooked on a Wednesday in July - inside the recipe book's outer bound, outside its default
JULY_DAY = dt.date(2026, 7, 29)

BATCH_DISH = "Chicken and rice boxes"
JULY_DISH = "Salmon and new potato traybake"
BATCH_METHOD = [
    "Season the chicken and air fry at 200C for 18 minutes.",
    "Cook the rice in a pan for 12 minutes.",
    "Split into five boxes; freeze three.",
]


def at(day: dt.date, hh: int, mm: int) -> dt.datetime:
    return dt.datetime(day.year, day.month, day.day, hh, mm, tzinfo=TZ)


def _summary_html(sunday: dt.date) -> str:
    """A Sunday review shaped like the one `jobs/sunday_summary.py` actually sends."""
    return "\n".join(
        [
            f"<b>Week to {sunday:%d %b}</b>",
            "Trend 100.4 kg, down 0.3 kg on last week.",
            "Five days confirmed, three sessions of four.",
            "Next: /menu for the week ahead.",
        ]
    )


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


def _profile(db: Db, person_id: int, *, segment_days: str) -> None:
    db.insert(
        "profiles",
        person_id=person_id,
        effective_from="2026-07-01",
        sex="m",
        birth_date="1988-03-01",
        height_cm=180.0,
        activity_factor=1.5,
        kcal_floor=1600,
        steps_floor=8000,
        segment_days=segment_days,
        refused_foods="[]",
    )


@dataclasses.dataclass
class Box:
    """The page at one moment, the database behind it, and the row ids that matter."""

    client: TestClient
    db: Db
    config: Config
    clock: FrozenClock
    owner_id: int
    partner_id: int
    grams: dict[str, int]
    batch_ids: list[int]
    july_id: int

    def as_owner(self, path: str, **kw: Any):
        return self.client.get(path, headers={HEADER: OWNER_LOGIN}, **kw)

    def as_partner(self, path: str, **kw: Any):
        return self.client.get(path, headers={HEADER: PARTNER_LOGIN}, **kw)

    def rate(self, recipe_id: int, score: int, note: str | None = None):
        """A tap on a star, exactly as `renderDishes()` posts it (plus the note the API takes)."""
        token = self.as_owner("api/me").json()["csrf"]
        body: dict[str, Any] = {"recipe_id": recipe_id, "score": score}
        if note is not None:
            body["note"] = note
        return self.client.post(
            "api/dishes/rate", json=body, headers={HEADER: OWNER_LOGIN, "X-CSRF-Token": token}
        )

    def post_as_owner(self, path: str, body: dict[str, Any]):
        """Any POST the page makes, with the CSRF token and the same-origin header it sends."""
        token = self.as_owner("api/me").json()["csrf"]
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

    def render(self, name: str) -> str:
        """One `render*()` function's source out of the bundle the browser runs."""
        js = self.app_js()
        start = js.index(f"async function {name}(")
        rest = js[start:]
        cut = rest.find("\n/* ---")
        return rest if cut < 0 else rest[:cut]

    def cook_again(self, start: dt.date, days: int = 5) -> list[int]:
        """The segment repeats: a fresh `recipes` row per day, the shape `/cook` really writes."""
        return _batch_menu(self.db, self.owner_id, start, days, self.grams)


def _batch_menu(
    db: Db, person_id: int, start: dt.date, days: int, grams: dict[str, int]
) -> list[int]:
    end = start + dt.timedelta(days=days - 1)
    menu_id = db.insert(
        "menus",
        person_id=person_id,
        segment_start=start.isoformat(),
        segment_end=end.isoformat(),
        status="repeated",
    )
    made: list[int] = []
    for i in range(days):
        day = start + dt.timedelta(days=i)
        recipe_id = db.insert(
            "recipes",
            name=BATCH_DISH,
            servings=5,
            prep_minutes=35,
            method_json=json.dumps(BATCH_METHOD),
            source="model",
            keep_days=3,
            freezable=1,
        )
        for code, g in (("chicken", 1000), ("rice", 375)):
            db.insert("recipe_ingredients", recipe_id=recipe_id, ingredient_id=grams[code], grams=g)
        menu_day = db.insert(
            "menu_days",
            menu_id=menu_id,
            day=day.isoformat(),
            kcal=2200,
            protein_g=185,
            fat_g=70,
            carb_g=200,
            fibre_g=30,
        )
        db.insert(
            "meal_slots",
            menu_day_id=menu_day,
            slot="lunch",
            recipe_id=recipe_id,
            portion_multiplier=1.0,
            portion_g=420,
            kcal=620,
            protein_g=52,
            fat_g=18,
            carb_g=60,
            fibre_g=6,
        )
        made.append(recipe_id)
    return made


@pytest.fixture
def open_box(config: Config, tmp_path: Path) -> Iterator[Callable[..., Box]]:
    opened: list[Db] = []

    def make(
        when: dt.datetime,
        *,
        sent_summaries: tuple[str, ...] = ("2026-09-06", "2026-09-13"),
    ) -> Box:
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

        grams = {
            "chicken": _ingredient(db, "chicken", "Chicken breast, raw", "meat", "500 g pack"),
            "rice": _ingredient(db, "rice", "Rice, basmati, dry", "grains", "1 kg bag"),
            "salmon": _ingredient(db, "salmon", "Salmon fillet", "fish", "240 g pack"),
            "potato": _ingredient(db, "potato", "New potatoes", "veg", "750 g bag"),
        }

        # July: the traybake he remembers liking, two days of one segment
        july_menu = db.insert(
            "menus",
            person_id=owner_id,
            segment_start=JULY_DAY.isoformat(),
            segment_end=(JULY_DAY + dt.timedelta(days=1)).isoformat(),
            status="accepted",
        )
        july_id = db.insert(
            "recipes",
            name=JULY_DISH,
            servings=1,
            prep_minutes=30,
            method_json=json.dumps(["Roast the potatoes for 20 minutes.", "Add the salmon."]),
            source="model",
            keep_days=2,
            freezable=0,
        )
        for code, g in (("salmon", 240), ("potato", 300)):
            db.insert("recipe_ingredients", recipe_id=july_id, ingredient_id=grams[code], grams=g)
        july_day = db.insert(
            "menu_days",
            menu_id=july_menu,
            day=JULY_DAY.isoformat(),
            kcal=2100,
            protein_g=180,
            fat_g=70,
            carb_g=190,
            fibre_g=28,
        )
        db.insert(
            "meal_slots",
            menu_day_id=july_day,
            slot="dinner",
            recipe_id=july_id,
            portion_multiplier=1.0,
            portion_g=540,
            kcal=700,
            protein_g=48,
            fat_g=30,
            carb_g=55,
            fibre_g=7,
        )

        # last week's segment: one batch dish, five days, five recipes rows
        batch_ids = _batch_menu(db, owner_id, LAST_MONDAY, 5, grams)

        for due_key in sent_summaries:
            db.insert(
                "messages_out",
                person_id=owner_id,
                chat_id=1000001,
                kind="sunday_summary",
                due_key=due_key,
                text_html=_summary_html(dt.date.fromisoformat(due_key)),
                status="sent",
                sent_at=f"{due_key} 18:00:11",
            )

        secrets_dir = tmp_path / "s"
        secrets_dir.mkdir(exist_ok=True)
        (secrets_dir / "telegram-token-dev.txt").write_text("1:t", encoding="utf-8")
        config.data["web"]["secure_cookies"] = False
        config.data["web"]["root_path"] = ""
        app = App(config, Secrets(secrets_dir), clock=clock, transport=FakeTelegram())
        client = TestClient(create_app(app), base_url="http://testserver")
        return Box(client, db, config, clock, owner_id, partner_id, grams, batch_ids, july_id)

    yield make
    for db in opened:
        db.close()


def _dish(rows: list[dict[str, Any]], name: str) -> dict[str, Any]:
    found = next((r for r in rows if r["name"] == name), None)
    assert found is not None, f"{name} is not on the Dishes tab"
    return found


# -- what a rating does ------------------------------------------------------------------------


def test_01_the_score_owner_taps_on_the_page_changes_the_next_menu(open_box) -> None:
    """Friday 18 Sep, 20:50. The week's batch lunch was dry every day and he is done with it.
    He opens Dishes on his phone and taps 2. The bot's `/rate 2` answers "I will stop proposing
    it" - a score tapped on the page has to reach the same place, or the stars are decoration."""
    box = open_box(at(dt.date(2026, 9, 18), 20, 50))
    newest = max(box.batch_ids)
    assert box.rate(newest, 2).status_code == 200
    good, bad = menu_mod.rated_names(box.db, box.owner_id)
    assert BATCH_DISH in bad, (
        f"a 2 tapped on the page leaves the menu builder seeing favourites={good} blocked={bad}"
    )
    assert BATCH_DISH not in good


def test_02_the_score_owner_gave_last_week_is_still_on_the_dish_this_week(open_box) -> None:
    """Sunday 20 Sep, 19:40. He rated the batch lunch 5 last week from the page. Tonight the
    same menu repeats, which writes a fresh `recipes` row per day, and he opens Dishes to see
    what he thought of it. The score is the only memory the tab has; if a repeat wipes it, the
    recipe book is blank again every Sunday and he re-rates the same dish for ever."""
    box = open_box(at(SUNDAY, 19, 40))
    rated = max(box.batch_ids)  # the row the tab offered him last week
    assert box.rate(rated, 5).status_code == 200
    assert _dish(box.as_owner("api/dishes").json()["dishes"], BATCH_DISH)["score"] == 5

    box.cook_again(MONDAY)  # tonight's repeat, five more rows of the same dish

    rows = box.as_owner("api/dishes").json()["dishes"]
    assert [r["name"] for r in rows].count(BATCH_DISH) == 1, "one line per dish, not per day"
    assert _dish(rows, BATCH_DISH)["score"] == 5, (
        f"the dish he scored 5 last week reads {_dish(rows, BATCH_DISH)!r} after the repeat - "
        "the score is keyed to the recipes row, and the repeat wrote a new one"
    )


def test_03_a_dish_owner_marked_down_does_not_come_back_as_a_favourite(open_box) -> None:
    """Sunday 20 Sep, 19:45. Two weeks ago he scored the batch lunch 2. The tab shows it unscored
    after the repeat, so tonight - the chicken having been better - he taps 4. The menu builder
    reads ratings by dish NAME, so both rows count, and one dish is now on the favourites list
    and the blocked list of the same prompt. Whatever the model then does, one of his two taps
    is being ignored and neither he nor the page can tell which."""
    box = open_box(at(SUNDAY, 19, 45))
    assert box.rate(max(box.batch_ids), 2).status_code == 200
    fresh = box.cook_again(MONDAY)
    # the story's own setup was wrong here: this docstring's "shows it unscored after the
    # repeat" narrates the OLD bug (recipe_id-keyed lookup), but the actual fix these two tests
    # share - dishes_summary/02's requirement that a score survive a repeat - means the SAME 2
    # he tapped two weeks ago is still what the tab reads for this NAME, not blank. The fix
    # stops a re-rate from creating a second, contradictory row; it does not un-rate the dish.
    assert _dish(box.as_owner("api/dishes").json()["dishes"], BATCH_DISH)["score"] == 2
    assert box.rate(max(fresh), 4).status_code == 200

    good, bad = menu_mod.rated_names(box.db, box.owner_id)
    assert not (BATCH_DISH in good and BATCH_DISH in bad), (
        f"the next menu prompt is told {BATCH_DISH!r} is both a favourite ({good}) and blocked "
        f"({bad}) - the same dish, two rows, two contradictory scores"
    )


def test_04_the_page_says_what_tapping_a_score_will_do(open_box) -> None:
    """Friday 18 Sep, 20:51, a second after tapping 2. In Telegram the answer is the dish name,
    the score, and then "I will stop proposing it." - so he knows the tap landed and what it
    bought him. On the page the button just turns dark. Nothing on the tab says a score does
    anything at all, and a control whose effect is invisible is a control nobody trusts."""
    box = open_box(at(dt.date(2026, 9, 18), 20, 51))
    bot = inspect.getsource(menuflow)
    assert "I will stop proposing it." in bot and "It will come back often." in bot, (
        "the bot tells the person what the score does"
    )
    assert box.rate(max(box.batch_ids), 2).status_code == 200

    dishes_tab = box.app_js()
    rating = dishes_tab[
        dishes_tab.index("function dishRating(") : dishes_tab.index("async function renderDishes()")
    ]
    said = re.findall(r'"(Noted[^\"]+)"', rating)
    assert any(
        word in " ".join(said).lower() for word in ("propos", "come back", "again", "favourite")
    ), f"the whole of what the Dishes tab says is {said} - a name, and five numbered buttons"


def test_05_owner_says_why_it_was_a_two(open_box) -> None:
    """Sunday 20 Sep, 19:50. A 2 on its own is a dead end: next week's menu drops the dish when
    the problem was the sauce, not the dish. `/api/dishes/rate` takes a `note` and `ratings.note`
    stores it - so the reason can be recorded, and then nothing ever reads it back. The tab that
    took the score has to be able to take the sentence and show it again."""
    box = open_box(at(SUNDAY, 19, 50))
    rated = max(box.batch_ids)
    assert box.rate(rated, 2, note="dry - it needs a sauce, not dropping").status_code == 200
    assert box.db.scalar("SELECT note FROM ratings WHERE recipe_id = ?", (rated,))

    row = _dish(box.as_owner("api/dishes").json()["dishes"], BATCH_DISH)
    dishes_tab = box.render("renderDishes")
    assert "note" in row, (
        f"the recipe book row is {sorted(row)} - the reason he typed is stored and never read"
    )
    assert "note" in dishes_tab, "and no box on the tab to type it into"


# -- finding a dish again ------------------------------------------------------------------------


def test_06_owner_finds_the_five_star_dish_and_can_cook_it_tonight(open_box) -> None:
    """Tuesday 15 Sep, 18:20, nothing planned and the fridge half empty. He opens Dishes to pick
    something he already knows works. The tab is his recipe book: picking a name off it is only
    half the job - he needs how long it takes and what goes in it, or the tab has sent him to
    the Menu tab to hunt for a day the dish happened to be on."""
    box = open_box(at(TUESDAY, 18, 20))
    row = _dish(box.as_owner("api/dishes").json()["dishes"], BATCH_DISH)
    assert row is not None and row["prep_minutes"] == 35, "the minutes are in the answer"
    assert box.as_owner("api/menu").json() is not None

    dishes_tab = box.render("renderDishes")
    assert "prep_minutes" in dishes_tab, (
        "the Dishes tab renders a name and five buttons; the minutes it was handed never "
        "reach the screen"
    )
    assert "ingredients" in row or "method" in row or "tab=menu" in dishes_tab, (
        f"and the row is {sorted(row)} - no ingredients, no method, and no link to the day "
        "the dish is on, so a dish found here cannot be cooked from here"
    )


def test_07_the_dish_from_july_is_still_in_the_recipe_book(open_box) -> None:
    """Tuesday 15 Sep, 18:25. "That salmon traybake in the summer was good." It was cooked on
    29 July, forty-seven days ago. The recipe book is the one place on the page that looks
    further back than this segment, and the tab asks for thirty days and offers no way to ask
    for more - so a dish drops out of his own recipe book a month after he last cooked it."""
    box = open_box(at(TUESDAY, 18, 25))
    default = [d["name"] for d in box.as_owner("api/dishes").json()["dishes"]]
    wider = [d["name"] for d in box.as_owner("api/dishes?recent=90").json()["dishes"]]
    assert JULY_DISH in wider, "the dish is there when the window is wide enough"

    dishes_tab = box.render("renderDishes")
    widenable = "recent=" in box.shell() or len(re.findall(r"recent=", dishes_tab)) > 1
    assert JULY_DISH in default or widenable, (
        f"the tab asks {re.findall(r'api/dishes[^"`]*', dishes_tab)} and shows {default}; "
        f"{JULY_DISH!r} exists at recent=90 and no control on the page can ask for it"
    )


# -- the Sunday summary, read on the other six days ----------------------------------------------


def test_08_on_a_tuesday_the_summary_tab_shows_the_last_review_and_dates_it(open_box) -> None:
    """Tuesday 15 Sep, 08:05, on the train. The Summary tab is written on Sunday evening, so six
    days in seven it is either last Sunday's or nothing. Last Sunday's, clearly labelled as last
    Sunday's, is the right answer; blank is not, and last Sunday's passed off as this week's is
    worse than blank."""
    box = open_box(at(TUESDAY, 8, 5))
    data = box.as_owner("api/summary/weekly").json()
    assert data["text"] is not None, "a Tuesday is not an empty tab"
    assert data["text"]["current"] is False, "and it is not passed off as this week's"
    assert "13 Sep" in data["text"]["text_html"]
    assert data["text"]["sent_at"].startswith("2026-09-13")

    summary_tab = box.render("renderSummary")
    assert "current" in summary_tab, "the tab reads the flag"
    assert "last one" in summary_tab.lower() or "Sunday" in summary_tab, (
        "and says out loud that this is the previous week's review"
    )


def test_09_partner_first_week_has_no_summary_and_the_tab_says_when_one_comes(open_box) -> None:
    """Sunday 13 Sep, 20:10. the partner was paired on Friday and is looking round the page. The
    Summary tab has nothing for her and will not until a Sunday evening job has run for her.
    Degrade to manual, never to silence: an empty tab that says when it fills is an answer, a
    blank rectangle is a bug report."""
    box = open_box(at(dt.date(2026, 9, 13), 20, 10))
    data = box.as_partner("api/summary/weekly").json()
    assert data == {"week_start": "2026-09-07", "text": None}, data

    summary_tab = box.render("renderSummary")
    empty = re.search(r'text:\s*"(No Sunday summary[^"]*)"', summary_tab)
    assert empty is not None, "the tab has a sentence for having nothing"
    assert "Sunday" in empty.group(1), f"and it says when one arrives: {empty.group(1)!r}"


def test_10_owner_reads_this_weeks_review_next_to_last_weeks(open_box) -> None:
    """Sunday 20 Sep, 19:20, twenty minutes after the review landed in Telegram. "Down 0.3 kg" -
    is that better or worse than last week? The two summaries are one query apart and
    `/api/summary/weekly?week=` already answers for any week, but the tab only ever asks for
    today's and the page has no control that names another week, so comparing means scrolling
    a Telegram chat back seven days."""
    box = open_box(at(SUNDAY, 19, 20), sent_summaries=("2026-09-06", "2026-09-13", "2026-09-20"))
    now = box.as_owner("api/summary/weekly").json()
    assert now["text"]["current"] is True and "20 Sep" in now["text"]["text_html"]
    before = box.as_owner("api/summary/weekly?week=2026-09-07").json()
    assert "13 Sep" in before["text"]["text_html"], "the older review is one query away"

    summary_tab = box.render("renderSummary")
    shell = box.shell()
    tail = shell.split('id="summary"')[1] if 'id="summary"' in shell else ""
    markup = tail.split("</section>")[0]
    reachable = "week=" in summary_tab or "<select" in markup or "<button" in markup
    assert reachable, (
        f"renderSummary() asks {re.findall(r'api/summary[^\"`)]*', summary_tab)} and the tab's "
        "own markup has no control, so no tap on the page reaches last week's review"
    )


def test_11_the_sunday_write_up_keeps_its_line_breaks_on_the_page(open_box) -> None:
    """Tuesday 15 Sep, 08:06, still on the train, reading Sunday's review on the page.

    The write-up is one of Tiffin's own Telegram messages, shown as it was sent - and a
    Telegram message breaks its lines with newline characters, not <br>. Dropped into the page
    with innerHTML those collapse, so nine labelled lines (trend, days, intake, sessions,
    next week's targets, the calibration note) arrived as a single unbroken
    paragraph: every number in it was there and none of them could be found.
    """
    box = open_box(at(TUESDAY, 8, 6))
    data = box.as_owner("api/summary/weekly").json()
    sent = data["text"]["text_html"]
    assert sent.count("\n") >= 3, f"setup: the message itself is written in lines: {sent!r}"
    assert "<br" not in sent, "setup: and it breaks them with newlines, not markup"

    summary_tab = box.render("renderSummary")
    # the holder is the div the message HTML goes INTO, not the navigation row above it
    holder = re.search(
        r'el\("div",\s*\{\s*class:\s*"([\w-]+)"\s*\}\);\s+\w+\.innerHTML', summary_tab
    )
    assert holder is not None, (
        f"the write-up goes into a bare <div>, where its newlines collapse: {summary_tab!r}"
    )
    sheet = re.search(r"static/app\.[0-9a-f]{10}\.css", box.shell())
    assert sheet is not None, "the shell does not reference app.css"
    css = box.as_owner(sheet.group(0)).text
    rule = re.search(rf"\.{holder.group(1)}\s*\{{([^}}]*)\}}", css)
    assert rule is not None and "pre-wrap" in rule.group(1), (
        f"nothing in the stylesheet keeps the line breaks for .{holder.group(1)}"
    )


# -- adding a food ---------------------------------------------------------------------------


def test_12_a_food_owner_bought_goes_into_the_catalogue_and_into_the_next_menu(open_box) -> None:
    """Tuesday 15 Sep, unpacking a bag, holding a tub of skyr the catalogue has never heard of.

    the recipes? example being I add skyr that I bought, provide it's kcal/100g, protein/fat/carbs
    per 100g". The test of this is not that a row appears - it is that the next menu can be built
    out of it, because `catalogue.approved()` IS the list the model's prompt is numbered from.
    """
    box = open_box(at(TUESDAY, 19, 0))
    before = {i.code for i in catalogue.approved(box.db, box.owner_id)}
    assert "arla_skyr_natural" not in before

    said = box.post_as_owner(
        "api/foods",
        {
            "name": "Arla Skyr Natural",
            "kcal_100": 60, "protein_100": 10.0, "fat_100": 0.2, "carb_100": 4.0,
            "fibre_100": 0, "pack_g": 450, "pack_label": "450 g tub", "aisle": "dairy",
        },
    )  # fmt: skip
    assert said.status_code == 200, said.text
    got = said.json()
    assert got["code"] == "arla_skyr_natural" and got["version"] == 1, got
    assert not got["warning"], f"60 kcal is what 10P/0.2F/4C comes to: {got['warning']}"

    roster = {i.code: i for i in catalogue.approved(box.db, box.owner_id)}
    assert "arla_skyr_natural" in roster, "added, and not in the list the menu is built from"
    added = roster["arla_skyr_natural"]
    assert added.kcal_100 == 60 and added.protein_100 == 10.0 and added.aisle == "dairy"
    assert added.approved, "a food typed off a label is approved; nothing else has to happen"

    # and it reads back on the page the same way every other food does
    listed = box.as_owner("api/foods").json()
    assert "dairy" in listed["aisles"]
    assert any(f["code"] == "arla_skyr_natural" for f in listed["foods"]), listed["foods"][:3]


def test_13_the_same_food_twice_is_a_correction_not_a_second_food(open_box) -> None:
    """A month later the tub changed recipe, or the first typing was wrong. Adding it again under
    the same name has to mean "these are the numbers now" - the catalogue is versioned for
    exactly this, and every reader takes the latest. The recipes already built stay pointed at
    the version they were costed against: re-typing a label does not rewrite last week's menu."""
    box = open_box(at(TUESDAY, 19, 0))
    payload = {
        "name": "Arla Skyr Natural",
        "kcal_100": 60, "protein_100": 10.0, "fat_100": 0.2, "carb_100": 4.0,
        "fibre_100": 0, "pack_g": 450, "pack_label": "450 g tub", "aisle": "dairy",
    }  # fmt: skip
    assert box.post_as_owner("api/foods", payload).status_code == 200
    second = box.post_as_owner("api/foods", {**payload, "protein_100": 11.0, "kcal_100": 63})
    assert second.status_code == 200, second.text
    assert second.json()["version"] == 2, second.json()

    rows = box.db.all(
        "SELECT version, protein_100 FROM ingredients WHERE code = 'arla_skyr_natural' "
        "ORDER BY version"
    )
    assert [(r["version"], r["protein_100"]) for r in rows] == [(1, 10.0), (2, 11.0)], (
        "the first typing was edited away instead of being superseded"
    )
    roster = {i.code: i for i in catalogue.approved(box.db, box.owner_id)}
    assert roster["arla_skyr_natural"].protein_100 == 11.0, "the menu reads the latest"


def test_14_a_number_that_cannot_be_true_is_refused_and_an_odd_one_is_questioned(
    open_box,
) -> None:
    """The difference between the two is the difference between a typo and a label.

    A food whose macros weigh more than 100 g per 100 g is not unusual, it is a mistyped box,
    and taking it would put a wrong number into every menu built from then on. A food whose
    stated calories disagree with its own macros is usually the same mistake and occasionally
    just a label, so it is stored and said out loud rather than refused.
    """
    box = open_box(at(TUESDAY, 19, 0))
    base = {
        "name": "Nonsense", "kcal_100": 100, "protein_100": 5, "fat_100": 5, "carb_100": 5,
        "pack_g": 500, "pack_label": "500 g", "aisle": "dairy",
    }  # fmt: skip

    impossible = box.post_as_owner("api/foods", {**base, "protein_100": 60, "carb_100": 60})
    assert impossible.status_code == 400, impossible.text
    assert "100 g of food" in impossible.json()["detail"], impossible.json()

    hot = box.post_as_owner("api/foods", {**base, "name": "Sunbeam", "kcal_100": 2000})
    assert hot.status_code == 400 and "900" in hot.json()["detail"], hot.text

    wrong_aisle = box.post_as_owner("api/foods", {**base, "name": "Lost", "aisle": "hardware"})
    assert wrong_aisle.status_code == 400, wrong_aisle.text

    assert (
        box.db.scalar(
            "SELECT COUNT(*) FROM ingredients WHERE code IN ('nonsense', 'sunbeam', 'lost')"
        )
        == 0
    ), "one of them was written"

    # 300 kcal against 5/5/5 is not impossible, it is unlikely: stored, with a word about it
    odd = box.post_as_owner("api/foods", {**base, "name": "Odd one", "kcal_100": 300})
    assert odd.status_code == 200, odd.text
    assert "worth a second look" in odd.json()["warning"], odd.json()
    assert box.db.scalar("SELECT COUNT(*) FROM ingredients WHERE code = 'odd_one'") == 1


def test_15_a_food_added_for_one_of_them_does_not_reach_the_other(open_box) -> None:
    """ "just me" is a real answer: he is the one eating the whey. The page never takes whose it
    is from the body - the header says who is asking, the same rule as every other write here."""
    box = open_box(at(TUESDAY, 19, 0))
    said = box.post_as_owner(
        "api/foods",
        {
            "name": "His whey", "kcal_100": 380, "protein_100": 80, "fat_100": 6, "carb_100": 6,
            "fibre_100": 0, "pack_g": 1000, "pack_label": "1 kg tub", "aisle": "grains",
            "person_scope": "me",
        },
    )  # fmt: skip
    assert said.status_code == 200, said.text
    assert "his_whey" in {i.code for i in catalogue.approved(box.db, box.owner_id)}
    assert "his_whey" not in {i.code for i in catalogue.approved(box.db, box.partner_id)}, (
        "a food he added for himself turned up in her menus"
    )
    scope = box.db.scalar("SELECT person_scope FROM ingredients WHERE code = 'his_whey'")
    assert scope == "owner", f"stored as {scope!r} rather than resolved from the header"
    # ...and a person named in the body is refused, not stored as that person's food:
    for named in ("partner", "owner"):
        refused = box.post_as_owner(
            "api/foods",
            {
                "name": f"Named {named}", "kcal_100": 380, "protein_100": 80, "fat_100": 6,
                "carb_100": 6, "pack_g": 1000, "aisle": "grains", "person_scope": named,
            },
        )  # fmt: skip
        assert refused.status_code == 400, named
    assert box.db.scalar("SELECT COUNT(*) FROM ingredients WHERE name LIKE 'Named %'") == 0


def test_16_a_blank_macro_box_never_becomes_a_zero(open_box) -> None:
    """An unanswered macro field remains unknown rather than becoming zero."""
    box = open_box(at(TUESDAY, 19, 0))
    js = box.app_js()
    form = js[js.index("function foodForm(") :]
    cut = form.find("\n/* ---")
    form = form if cut < 0 else form[:cut]
    assert "body[f.key] = 0" not in form, f"an empty box is still sent as a real zero:\n{form}"
    assert "Off the label" in form, "and nothing tells her which box is missing"

    # the server is the backstop: a zero that disagrees with real macros is not silently fine
    said = box.post_as_owner(
        "api/foods",
        {
            "name": "Zero skyr", "kcal_100": 0, "protein_100": 10, "fat_100": 0.2,
            "carb_100": 4, "pack_g": 450, "pack_label": "450 g tub", "aisle": "dairy",
        },
    )  # fmt: skip
    assert said.status_code == 200, said.text
    assert said.json()["warning"], (
        "0 kcal against 10 g of protein is not a food; it passed without a word"
    )


def test_17_an_existing_catalogue_food_is_re_versioned_by_its_own_code(open_box) -> None:
    """The owner swapped his whey for a different tub and the numbers are not the same food.

    The catalogue's codes came from the YAML and are not the display name slugified - "whey",
    not "whey_protein_powder" - so re-typing the numbers under the same name would have made a
    SECOND food and left twenty-four menu lines pointing at the old one. Naming the code updates
    the food everything already references, which is what the versioning is for.
    """
    box = open_box(at(TUESDAY, 19, 0))
    box.db.insert(
        "ingredients",
        code="whey",
        version=1,
        name="Whey protein powder",
        kcal_100=400.0,
        protein_100=80.0,
        fat_100=7.0,
        carb_100=5.0,
        fibre_100=0.0,
        pack_g=1000.0,
        pack_label="1 kg bag",
        aisle="grains",
        pantry=0,
        use_whole_pack=0,
        approved=1,
        person_scope="both",
    )
    got = catalogue.add_food(
        box.db,
        code="whey",
        name="Whey protein powder",
        kcal_100=362,
        protein_100=78.1,
        fat_100=0.5,
        carb_100=9.4,
        fibre_100=0.0,
        pack_g=2264,
        pack_label="2.26 kg tub",
        aisle="grains",
    )
    assert got["code"] == "whey" and got["version"] == 2, got

    codes = [i.code for i in catalogue.approved(box.db, box.owner_id)]
    assert codes.count("whey") == 1, f"two whey foods in the roster: {codes}"
    assert "whey_protein_powder" not in codes, "the slug made a second food alongside it"
    now = next(i for i in catalogue.approved(box.db, box.owner_id) if i.code == "whey")
    assert now.fat_100 == 0.5 and now.kcal_100 == 362, "the roster still has the old tub's numbers"
