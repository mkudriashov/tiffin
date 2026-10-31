"""The tailnet page on a phone: ten moments with a 400 px screen in one hand.

Both of them read this page the way the README says the bot is read - standing up, one-handed,
often in a kitchen or a shop, on a tailnet that is fast at home and nothing at all on the street.
The other web suites ask whether the right numbers arrive; this one asks whether the person can
see them, reach them and act on them at 400 CSS pixels wide.

Tuesday cook, his Mon-Fri menu is still the accepted one, intervals.icu has written his steps
every day for two months, and Sunday's shopping list is half ticked. Only the owner has a Tailscale
identity here; nothing in this file needs two people.

Each story drives the real app - the same GET the tab makes - and then asserts on the markup and
the CSS actually served, because at this size the finding is almost never in the JSON. Widths are
computed from the stylesheet with deliberately conservative numbers (see CHAR_PX): every estimate
below is the narrow end of what a real phone renders.
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
from tiffin.food import batch
from tiffin.secrets import Secrets
from tiffin.web import api as web_api
from tiffin.web.app import create_app
from tiffin.web.assets import STATIC_DIR

from .fakes import FakeTelegram
from .web_source import app_source

HEADER = "Tailscale-User-Login"
OWNER_LOGIN = "alex@example.com"

MONDAY = dt.date(2026, 9, 14)
WEDNESDAY = dt.date(2026, 9, 16)
FRIDAY = dt.date(2026, 9, 18)

#: the narrow end of what either of them holds (iPhone 13 mini / SE in portrait is 375)
PHONE_W = 400
#: `body { padding: 0 var(--pad) ... }` with `--pad: 14px`
GUTTER = 14
CONTENT_W = PHONE_W - 2 * GUTTER
#: system-ui at 0.92rem is 14.7 px; 6.5 px per character is below the ~7.4 px Segoe UI and
#: Roboto actually average for mixed-case labels, so every width computed with it is a floor
CHAR_PX = 6.5
#: `font: 16px/1.45` on body, so one line of body text is 23 px
LINE_PX = 23

#: a real model-written dinner name, the length the catalogue produces once a dish has a
#: centrepiece, a starch and a green in it
LONG_DINNER = "Slow-roasted harissa chicken thighs with lemon butter beans and spinach"
DINNER_METHOD = [
    "Heat the oven to 200C fan and rub the harissa into the chicken thighs.",
    "Roast the thighs skin side up for 25 minutes on the top shelf.",
    "Drain and rinse the butter beans and warm them through in the pan.",
    "Stir the spinach into the beans until it wilts.",
    "Squeeze over the lemon and season.",
    "Divide into three boxes; one for tomorrow, two for the fridge.",
]


def at(day: dt.date, hh: int, mm: int) -> dt.datetime:
    return dt.datetime(day.year, day.month, day.day, hh, mm, tzinfo=TZ)


def block(css: str, selector: str) -> str:
    """The declaration block of one rule, by a regex for its selector at the start of a line."""
    found = re.search(rf"(?m)^{selector}\s*\{{([^}}]*)\}}", css)
    assert found is not None, f"app.css has no rule matching {selector!r}"
    return found.group(1)


def pill_px(label: str) -> float:
    """One tab button: text, `padding: 0 14px`, `border: 1px`."""
    return len(label) * CHAR_PX + 2 * 14 + 2 * 1


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
    prep_minutes: int = 35,
) -> int:
    rid = db.insert(
        "recipes",
        name=name,
        servings=servings,
        prep_minutes=prep_minutes,
        method_json=json.dumps(method),
        source="model",
        keep_days=3,
        freezable=0,
    )
    for iid, g in grams.items():
        db.insert("recipe_ingredients", recipe_id=rid, ingredient_id=iid, grams=g)
    return rid


@dataclasses.dataclass
class Box:
    """The page as it is served on the box at one moment, and the database behind it."""

    client: TestClient
    web: Any
    db: Db
    config: Config
    clock: FrozenClock
    owner_id: int
    menu_id: int
    recipes: dict[str, int]

    def as_owner(self, path: str, **kw: Any):
        return self.client.get(path, headers={HEADER: OWNER_LOGIN}, **kw)

    def shell(self) -> str:
        return self.as_owner("").text

    def asset(self, name: str) -> str:
        """The stylesheet or script the shell really loads, by its hashed name."""
        if name == "app.js":
            return app_source()
        stem, _, ext = name.rpartition(".")
        found = re.search(rf"static/{re.escape(stem)}\.[0-9a-f]{{10}}\.{ext}", self.shell())
        assert found is not None, f"the shell does not reference {name}"
        return self.as_owner(found.group(0)).text


@pytest.fixture
def open_phone(config: Config, tmp_path: Path) -> Iterator[Callable[..., Box]]:
    opened: list[Db] = []

    def make(when: dt.datetime) -> Box:
        clock = FrozenClock(when)
        config.paths.ensure()
        db = Db.open(config.paths.db, clock=clock)
        opened.append(db)
        owner_id = db.insert("persons", slug="owner", display_name="Alex", tone="blunt")
        db.insert("tailscale_identities", person_id=owner_id, login=OWNER_LOGIN)
        db.insert(
            "profiles",
            person_id=owner_id,
            effective_from="2026-09-01",
            sex="m",
            birth_date="1990-03-15",
            height_cm=183.0,
            activity_factor=1.55,
            steps_floor=9000,
            kcal_floor=1800,
            segment_days="[[0,1,2,3,4]]",
        )
        db.insert(
            "targets",
            person_id=owner_id,
            week_start=MONDAY.isoformat(),
            kcal=2400,
            protein_g=186,
            fat_g=75,
            carb_g=240,
            fibre_g=32,
            source="formula",
        )

        chicken = _ingredient(db, "chicken", "Chicken thighs, boneless", "meat", "600 g pack")
        beans = _ingredient(db, "beans", "Butter beans, tinned, drained", "tins", "400 g tin")
        spinach = _ingredient(db, "spinach", "Spinach, baby", "veg", "240 g bag")
        rice = _ingredient(db, "rice", "Rice, basmati, dry", "grains", "1 kg bag")
        eggs = _ingredient(db, "eggs", "Eggs, medium", "dairy", "6 eggs")

        r: dict[str, int] = {}
        r["breakfast"] = _recipe(
            db,
            "Eggs on toast",
            ["Toast the bread.", "Scramble the eggs."],
            {eggs: 120},
            prep_minutes=10,
        )
        r["lunch"] = _recipe(
            db,
            "Chicken and rice boxes",
            ["Season the chicken and air fry at 200C for 18 minutes.", "Cook the rice."],
            {chicken: 800, rice: 300},
            servings=4,
            prep_minutes=30,
        )
        r["dinner"] = _recipe(
            db,
            LONG_DINNER,
            DINNER_METHOD,
            {chicken: 600, beans: 400, spinach: 200},
            servings=3,
            prep_minutes=35,
        )

        menu_id = db.insert(
            "menus",
            person_id=owner_id,
            segment_start=MONDAY.isoformat(),
            segment_end=FRIDAY.isoformat(),
            status="repeated",
        )
        for i in range(5):
            day = MONDAY + dt.timedelta(days=i)
            day_id = db.insert(
                "menu_days",
                menu_id=menu_id,
                day=day.isoformat(),
                kcal=2400,
                protein_g=186,
                fat_g=75,
                carb_g=240,
                fibre_g=32,
            )
            for slot, key, kcal in (
                ("breakfast", "breakfast", 520),
                ("lunch", "lunch", 780),
                ("dinner", "dinner", 1100),
            ):
                db.insert(
                    "meal_slots",
                    menu_day_id=day_id,
                    slot=slot,
                    recipe_id=r[key],
                    portion_multiplier=1.0,
                    portion_g=420,
                    kcal=kcal,
                    protein_g=52,
                    fat_g=22,
                    carb_g=68,
                    fibre_g=9,
                )

        # two months of steps from the hourly icu mirror, today included
        for i in range(60):
            day = WEDNESDAY - dt.timedelta(days=59 - i)
            db.insert(
                "icu_wellness",
                person_id=owner_id,
                day=day.isoformat(),
                steps=7200 + (i * 137) % 6400,
                icu_raw_id=0,
            )
        db.execute(
            "UPDATE icu_wellness SET steps = 12480 WHERE person_id = ? AND day = ?",
            (owner_id, WEDNESDAY.isoformat()),
        )

        list_id = db.insert(
            "shopping_lists",
            menu_id=menu_id,
            kind="segment",
            person_id=owner_id,
            created_at="2026-09-13 17:00:12",
        )
        for iid, grams, aisle in (
            (chicken, 3000, "meat"),
            (beans, 2000, "tins"),
            (spinach, 1000, "veg"),
        ):
            db.insert(
                "shopping_items",
                list_id=list_id,
                ingredient_id=iid,
                grams_needed=grams,
                packs=5,
                leftover_g=0,
                aisle=aisle,
            )

        secrets_dir = tmp_path / "s"
        secrets_dir.mkdir(exist_ok=True)
        (secrets_dir / "telegram-token-dev.txt").write_text("1:t", encoding="utf-8")
        config.data["web"]["secure_cookies"] = False
        config.data["web"]["root_path"] = ""
        app = App(config, Secrets(secrets_dir), clock=clock, transport=FakeTelegram())
        web = create_app(app)
        client = TestClient(web, base_url="http://testserver")
        return Box(client, web, db, config, clock, owner_id, menu_id, r)

    yield make
    for db in opened:
        db.close()


@pytest.fixture
def phone(open_phone) -> Box:
    """Render the page at a fixed kitchen-time fixture."""
    return open_phone(at(WEDNESDAY, 18, 0))


# -- reaching things ---------------------------------------------------------------------------


def test_01_reach_the_last_tab_with_one_thumb(phone: Box) -> None:
    """Wednesday 18:00, phone in one hand, a pan in the other. He wants More, the last of the
    five destinations, on a 400 px screen."""
    shell = phone.shell()
    css = phone.asset("app.css")
    tabs = re.findall(r'data-tab="([a-z]+)"[^>]*>.*?<span>([^<]+)</span>', shell, re.S)
    labels = [m[1] for m in tabs]
    assert labels == ["Today", "Menu", "Shopping", "Progress", "More"]

    nav = block(css, "nav#tabs")
    assert "position: fixed" in nav and "bottom: 0" in nav, "the bar stays in reach of the thumb"
    assert "repeat(5, minmax(0, 1fr))" in nav, "five equal columns, nothing off the edge"
    # each label in its own column at the bar's 0.74rem: CHAR_PX is measured at 0.92rem
    column = PHONE_W / len(labels) - 2 * 2
    char = CHAR_PX * 0.74 / 0.92
    cut = [label for label in labels if len(label) * char > column]
    assert not cut, f"{cut} do not fit a {column:.0f} px column"


def test_02_get_back_to_the_tabs_from_the_bottom_of_friday(phone: Box) -> None:
    """Wednesday 18:00. He has scrolled the Menu tab down to Friday's dinner to check what he
    is cooking for, and now wants Shopping."""
    css = phone.asset("app.css")
    data = phone.as_owner("api/menu").json()
    assert [d["day"] for d in data["days"]][-1] == FRIDAY.isoformat()

    px = 0.0
    for day in data["days"]:
        px += 53  # h2 plus `margin: 20px 0 8px`
        for slot in day["slots"]:
            px += 17 + 25  # `.slot` border and padding, then the h3
            px += 2 * LINE_PX  # the kcal/P/F/C/minutes line wraps at this width
            px += len(slot["ingredients"]) * LINE_PX
            px += len(slot["method"] or []) * 2 * LINE_PX  # a step is two lines at 400 px

    nav = block(css, "nav#tabs")
    assert "position: sticky" in nav or "position: fixed" in nav, (
        f"the Menu tab is about {px:.0f} px tall - {px / 700:.1f} phone screens - and the tab "
        f"row is static markup at the very top of the document. From Friday's dinner every "
        f"other tab costs a full-page flick back up, and nothing on the page goes back to the "
        f"top. `nav {{ position: sticky; top: env(safe-area-inset-top, 0px) }}` is the whole fix."
    )


# -- waiting -----------------------------------------------------------------------------------


def test_03_see_that_something_is_happening_when_menu_is_tapped(phone: Box) -> None:
    """Wednesday 18:00, on the tailnet over 4G with one bar. He taps Menu and watches the
    screen to see whether the tap registered."""
    js = phone.asset("app.js")
    css = phone.asset("app.css")

    # show() reveals the empty section first and only then awaits the fetch
    order = re.search(
        r"async function show\(name(?:, params)?\)[\s\S]*?classList\.toggle\("
        r"\"on\", section\.id === name\)[\s\S]*?await RENDER\[name\]\(\)",
        js,
    )
    assert order is not None, "show() no longer reveals the section before fetching"

    busy = [w for w in ("aria-busy", "Loading", "loading", "spinner", "skeleton") if w in js]
    assert busy or ".loading" in css, (
        "nothing on the page says it is working. show() switches the section on - an empty "
        "<div> - and then awaits api/menu, so a slow tailnet gives a blank white panel under "
        "the tab row with no spinner, no skeleton and no aria-busy. The tap looks like it "
        "missed, so she taps again, and the second render races the first."
    )


def test_04_the_page_opens_before_the_chart_library_does(phone: Box) -> None:
    """Wednesday 18:00, away from the house, opening the page cold to read tonight's dinner.
    She never touches Trends."""
    shell = phone.shell()
    tags = re.findall(r"<script[^>]*>", shell)
    weight = (STATIC_DIR / "plotly-basic-3.7.0.min.js").stat().st_size
    plotly = next((t for t in tags if "plotly" in t), None)
    if plotly is None:
        # the best answer: not in the shell at all, fetched the first time a chart is drawn
        app_js = phone.asset("app.js")
        assert "function loadPlotly" in app_js and "tiffin-plotly" in shell, (
            "the chart bundle is in no script tag and nothing loads it"
        )
        return
    app_tag = next(t for t in tags if "app." in t)
    assert tags.index(plotly) < tags.index(app_tag), "app.js is loaded after the chart bundle"

    assert "defer" in plotly or "async" in plotly or "import(" in phone.asset("app.js"), (
        f"{weight:,} bytes of Plotly are fetched and parsed before app.js runs, on every cold "
        f"load, for every tab. Nothing renders until then: boot() is what fills the header and "
        f"the first tab. Six of the seven tabs never plot anything. `defer` on both tags, or "
        f"loading the bundle only when Trends is first opened, is the difference between a "
        f"page that opens on 4G and one that does not."
    )


def test_05_the_tick_that_failed_in_the_shop(phone: Box) -> None:
    """Sunday in the shop, reading the Shopping tab off the phone: she ticks chicken, the
    tailnet has dropped behind her, and the tick springs back."""
    items = phone.as_owner("api/shopping").json()["items"]
    assert items, "the Shopping tab has items to tick"
    # the same POST the tick button makes, from a page whose session the app no longer knows
    refused = phone.client.post(
        "api/shopping/tick",
        json={"item_id": items[0]["id"], "ticked": True},
        headers={HEADER: OWNER_LOGIN},
    )
    assert refused.status_code == 403
    shown = f"{refused.status_code}: {refused.json()['detail']}"

    js = phone.asset("app.js")
    assert "new Error(response.status >= 500" in js and "err.status = response.status" in js, (
        "api() throws the server's sentence for a 4xx, says the box failed for a 5xx, and "
        "carries the status so a refusal is not offered a Retry"
    )
    hide = re.search(r"box\.hidden = true;[^}]*\},\s*(\d+)\)", js)
    assert hide is not None, "fail() no longer auto-hides"

    told = re.search(r"function fail\(message\)[\s\S]*?\r?\n\}", js)
    assert told is not None, "fail() is still the whole error surface"
    interactive = [w for w in ("button", "addEventListener", "onclick", "Retry") if w in told[0]]
    assert interactive or "location.reload(" in js, (
        f"what she gets, in a red bar at the bottom of the screen, is {shown!r} - and it "
        f"deletes itself after {int(hide.group(1)) / 1000:.0f} seconds with no way to bring it "
        f"back. There is no retry, nothing queues the tick for when the tailnet returns, and "
        f"the house rule is degrade to manual, never to silence. A phone in a shop needs 'that "
        f"tick did not save - tap to try again', and it needs to stay on screen until she does."
    )


# -- steps, both kinds -------------------------------------------------------------------------


def test_06_read_a_days_steps_off_the_trends_chart(phone: Box) -> None:
    """Wednesday 18:00. He has beaten his 9,000 floor today and wants to see how the last
    three months went, so he opens Trends and picks Steps."""
    data = phone.as_owner("api/trends?metric=steps&days=90").json()
    assert data["raw"] and data["target"][0] == 9000
    plot_w = CONTENT_W - 44 - 12  # `margin: { l: 44, r: 12 }`
    per_day = plot_w / len(data["dates"])

    js = phone.asset("app.js")
    config = re.search(r"\{ displayModeBar: [^}]*\}", js)
    assert config is not None, "the Plotly config line moved"
    assert "dragmode" in js or "staticPlot" in js or "scrollZoom" in js, (
        f"{len(data['dates'])} days are drawn across {plot_w} px - {per_day:.1f} px a day - and "
        f"the only Plotly options the page sets are {config.group(0)}. There is no touch "
        f"configuration at all: the default dragmode turns a finger dragged across the chart "
        f"into a zoom box instead of scrolling the page, and with the modebar off there is no "
        f"reset-axes button to undo it. A 320 px tall chart in the middle of a scrolling phone "
        f"page needs `dragmode: false` (or `staticPlot`), and at 5 px a day it needs a shorter "
        f"default window than 90 days."
    )


def test_07_the_notch_does_not_eat_the_first_tab(phone: Box) -> None:
    """Wednesday 18:00, reading the page on a notched phone, turned sideways to see the week
    table - and, because the manifest says standalone, from the home-screen icon."""
    shell = phone.shell()
    css = phone.asset("app.css")
    assert "viewport-fit=cover" in shell, "the viewport meta opts into the whole screen"
    assert '"display": "standalone"' in phone.as_owner("manifest.webmanifest").text

    assert "safe-area-inset" in css, (
        "index.html asks for `viewport-fit=cover`, which hands the page the area under the "
        "notch, the rounded corners and the home indicator - and app.css never takes it back: "
        "`body { padding: 0 14px 40px }` and no `env(safe-area-inset-*)` anywhere. Landscape "
        "puts the Today pill and the left edge of every card under the corner, and the error "
        "bar at `bottom: 8px` sits under the home indicator. Installed as a standalone app "
        "there is no browser chrome to absorb any of it."
    )


def test_08_a_long_dish_name_and_the_number_beside_it(phone: Box) -> None:
    """Wednesday 18:00, page open on Today, checking what dinner costs him before he cooks
    it. Tonight's dinner is the seventy-character one the model wrote."""
    meals = phone.as_owner("api/today").json()["meals"]
    dinner = next(m for m in meals if m["slot"] == "dinner")
    assert dinner["name"] == LONG_DINNER
    left = f"{dinner['slot']}: {dinner['name']}"
    right = f"{round(dinner['kcal'])} kcal"
    # body text is 16 px; 8 px a character is the low end for mixed case at that size
    need = len(left) * 8 + len(right) * 8 + 10  # plus the row's `gap: 10px`
    room = CONTENT_W - 2 * 2  # `.tickable` padding: 8px 2px

    css = phone.asset("app.css")
    rule = block(css, r"ul\.items button, \.tickable")
    assert "min-width" in rule or "nowrap" in css, (
        f"the row wants {need} px and has {room}. It is a `display: flex` with "
        f"`justify-content: space-between` and two spans, neither of which is told how to give "
        f"way: no `min-width: 0` on the name, no `flex-shrink: 0` and no `white-space: nowrap` "
        f"on the value. Both shrink in proportion to their content, so the name takes the room "
        f"and {right!r} - the number he opened the tab for - breaks into "
        f"'{round(dinner['kcal'])}' "
        f"over 'kcal' in a 40 px column. The same two-span row is used by shopping and other "
        f"compact rows."
    )


def test_09_cook_the_dinner_off_the_screen_with_wet_hands(phone: Box) -> None:
    """Wednesday 18:00 at the hob, phone propped against the kettle, cooking tonight's dinner
    from the Menu tab: six steps, one at a time, hands busy."""
    day = next(d for d in phone.as_owner("api/menu").json()["days"] if d["day"] == "2026-09-16")
    dinner = next(s for s in day["slots"] if s["slot"] == "dinner")
    assert dinner["method"] == DINNER_METHOD and len(dinner["ingredients"]) == 3

    js = phone.asset("app.js")
    renders = re.search(r"async function renderMenu\([^)]*\)[\s\S]*?\r?\n\}\r?\n", js)
    assert renders is not None
    body = renders.group(0)
    assert re.search(r'el\("p", \{ text: `\$\{i \+ 1\}\. \$\{step\}`', body), (
        "renderMenu() no longer writes the method as plain paragraphs"
    )

    marked_up = 'el("ol"' in body or '"Method"' in body or "step" in phone.asset("app.css")
    assert marked_up, (
        f"the {len(dinner['method'])} cooking steps go onto the page as bare <p> text, "
        f"immediately under {len(dinner['ingredients'])} ingredient <p>s that differ only by "
        f"being grey, with no 'Ingredients' or 'Method' heading between them and no list markup "
        f"- so at arm's length across a hob it is one undifferentiated column of sentences. "
        f"Nothing holds his place: no step numbers he can tap off, no current-step highlight, "
        f"and nothing asks for a wake lock, so the screen locks between step 2 and step 3 and "
        f"he re-finds his place with the back of a wet hand."
    )


def test_10_the_cook_itself_has_no_view_on_the_phone(phone: Box) -> None:
    """Wednesday 18:00. He skipped the Monday and Tuesday cook, so tonight he is cooking for
    Wednesday to Friday, and he wants what the bot's /batch gives him - totals, boxes, what to
    freeze - on the propped-up phone rather than by scrolling a chat."""
    menu = batch.menu_to_batch(phone.db, phone.owner_id, WEDNESDAY)
    assert menu is not None and int(menu["id"]) == phone.menu_id
    load = batch.plan(phone.db, menu, ("lunch", "dinner"), WEDNESDAY, freezes=True)
    assert load.dishes and load.amounts, "the cook-day load exists behind the bot"

    shell = phone.shell()
    js = phone.asset("app.js")
    routes = {str(getattr(r, "path", "")) for r in web_api.router.routes}
    assert 'data-tab="batch"' in shell or "batch" in js.lower(), (
        f"the page has seven tabs and none of them is the cook. `batch.plan` already returns "
        f"{load.boxes} boxes across {len(load.dishes)} dishes and {len(load.amounts)} weighed "
        f"amounts for the {load.cook_day} cook, with {len(load.to_freeze)} dish(es) that will "
        f"not keep - and the page serves {sorted(p for p in routes if p.startswith('/api'))}, "
        f"no api/batch, and renders no part of it. The one surface that can show a table of "
        f"amounts next to a list of pots, on a screen propped by the hob, is the one that does "
        f"not have it - and by story 01 the tab row has no room for an eighth pill, so it "
        f"belongs on the Menu tab as a 'Cook' block above the days."
    )


def test_11_what_the_page_hides_is_actually_hidden(phone: Box) -> None:
    """Tuesday 13:42, the page opens on his phone: two grey panels and a Close button, no header,
    no tabs, nothing in the console. Everything had rendered correctly underneath - the hands-free
    cook overlay is marked `hidden`, but its own rule sets `display: flex`, and an author display
    rule beats the attribute, so it covered the whole page from the moment it loaded."""
    shell = phone.shell()
    css = phone.asset("app.css")
    hidden = re.findall(r'id="([\w-]+)"[^>]*\shidden[\s>]', shell)
    assert hidden, "the shell starts with something hidden (the overlay, the error line)"
    # an id selector (1,0,0) outranks [hidden] (0,1,0) whatever the order, so the blanket rule
    # only actually hides these if it is !important
    blanket = re.search(r"\[hidden\][^{]*\{[^}]*display:\s*none\s*!important", css)

    def own_rule(element_id: str) -> str:
        found = re.search(rf"#{re.escape(element_id)}\s*\{{([^}}]*)\}}", css)
        return found.group(1) if found else ""

    painted = [i for i in hidden if "display" in own_rule(i)]
    assert blanket or not painted, (
        f"{painted} carry the hidden attribute and their own display rule, and nothing overrides "
        f"it: they are painted over the page from load"
    )


def test_12_the_buttons_that_choose_a_day_are_thumb_sized_and_show_which_is_chosen(
    phone: Box,
) -> None:
    """Wednesday 18:05, in the kitchen. He opens Batch and picks the day to cook from.

    Four of the page's control rows - the batch cook-from chips, the week and summary week
    navigation, the dishes window - render a bare `<button>` inside a `.row`, and nothing in the
    stylesheet ever matched them. They came out as whatever Chrome draws by default: around
    24 px tall against the 44 px every other target on this page keeps, and `.on` - the class the
    code sets on the chosen day - painted nothing at all, so the day he had picked looked exactly
    like the four he had not.
    """
    css = phone.asset("app.css")
    rule = block(css, r"\.row button")
    assert "min-height: 44px" in rule.replace("min-height:44px", "min-height: 44px"), (
        f"a row button is whatever the browser draws, not a 44 px target: {rule!r}"
    )
    chosen = block(css, r"\.row button\.on")
    assert "var(--accent)" in chosen, (
        f"the chosen day is not marked out from the others: {chosen!r}"
    )
    # a day already cooked is disabled rather than removed, so the segment still reads whole
    assert re.search(r"(?m)^\.row button:disabled\s*\{", css), (
        "a disabled day is drawn exactly like one he can still choose"
    )

    js = phone.asset("app.js")
    assert 'class: day === from ? "on" : ""' in js, "the cook-from chip is what sets .on"
