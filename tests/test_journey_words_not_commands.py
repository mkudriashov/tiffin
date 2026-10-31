"""People type sentences, not commands.

"show me tomorrow", "recipe for tomorrow's breakfast", "no bacon please", "what do I buy", "can I
swap Wednesday dinner", "make it simpler" - and the test follows the words through the real
handler chain (menu-change flow, tape question, onboarding, the evening "what changed?" question,
then chat and its tools) to what the person is actually sent.

The model is faked, but honestly: `Diligent` calls the tools it is told to and composes its
answer ONLY from what those tools return. So when a person does not hear something, it is
because nothing the chat model is allowed to read contains it - not because the fake chose to
leave it out.

the partner cooks Mon-Wed + Thu-Fri, her accepted Mon-Wed menu still has a chickpea salad in it, a
chickpea-free proposal waits for her Accept, her Thu-Fri has no menu, and chickpeas are on her
avoid list. Her replies are dropped in dev, so what she "sees" is read from `messages_out`.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tiffin.app import App
from tiffin.clock import TZ, FrozenClock, isoday
from tiffin.db import Db
from tiffin.errors import ModelUnavailable
from tiffin.food import menu as menu_mod
from tiffin.food import shopping
from tiffin.model import tools as tools_mod
from tiffin.model.gateway import ModelResult
from tiffin.secrets import Secrets
from tiffin.telegram.measures import MeasuresFlow
from tiffin.telegram.poller import Poller

from .fakes import FakeTelegram

OWNER_CHAT = 1000001
PARTNER_CHAT = 1000002
SUNDAY = dt.date(2026, 9, 13)
MONDAY = dt.date(2026, 9, 14)
TUESDAY = dt.date(2026, 9, 15)
WEDNESDAY = dt.date(2026, 9, 16)
THURSDAY = dt.date(2026, 9, 17)
FRIDAY = dt.date(2026, 9, 18)

TURKEY = "Garlic turkey with air-fried carrots and potato wedges"
STIR_FRY = "Beef stir fry with noodles"
CHICKPEA_SALAD = "Chicken, chickpea and feta salad"
SALMON = "Air-fried salmon, sweet potato and green beans"
EGGS_BACON = "Scrambled eggs, bacon and spinach on toast"
EGGS_BACON_STEPS = [
    "Fry the bacon in a dry pan until crisp, then keep it warm.",
    "Wilt the spinach in the same pan.",
    "Whisk the eggs with the milk.",
    "Scramble the eggs over a low heat.",
    "Toast the bread and pile everything on top.",
]
PEPPER_WRAP = "Chicken, kidney bean and pepper wrap"
COUSCOUS = "Chicken and couscous salad"


# ---------------------------------------------------------------- an honest fake model
class Diligent:
    """A model that reads before it speaks, and says only what its tools returned.

    `calls` are the tools it runs for this turn; `compose` turns the results into the reply.
    A write tool creates an intent, exactly as the real tool loop does.
    """

    def __init__(
        self,
        calls: list[tuple[str, dict]] | None = None,
        compose: Callable[[dict[str, Any]], str] | None = None,
        *,
        raises: Exception | None = None,
        json_raises: Exception | None = None,
        json_data: dict | None = None,
    ):
        self.calls = calls or []
        self.compose = compose or (lambda _r: "")
        self.raises = raises
        self.json_raises = json_raises
        self.json_data = json_data or {}
        self.conversations: list[list[dict]] = []
        self.results: list[dict[str, Any]] = []
        self.json_calls: list[tuple[str, str]] = []

    def converse(self, task, messages, *, person_id, tools=None):
        self.conversations.append(list(messages))
        if self.raises is not None:
            raise self.raises
        assert tools is not None
        got: dict[str, Any] = {}
        for name, args in self.calls:
            got[name] = tools.call(name, args)
        self.results.append(got)
        return ModelResult(
            text=self.compose(got), data=None, model_used="claude-opus-5", backend="fake"
        )

    def json_task(self, task, prompt, schema_name, *, person_id):
        self.json_calls.append((task, prompt))
        if self.json_raises is not None:
            raise self.json_raises
        return ModelResult(text="", data=self.json_data, model_used="claude-opus-5", backend="fake")


def every_read(day: dt.date) -> list[tuple[str, dict]]:
    """Every read tool the chat model has, pointed at `day` where a tool takes one."""
    out: list[tuple[str, dict]] = []
    for name in tools_mod.READ_TOOLS:
        props = tools_mod.BY_NAME[name].parameters["properties"]
        if "day" in props:
            out.append((name, {"day": isoday(day)}))
        elif "week" in props:
            out.append((name, {"week": isoday(day)}))
        else:
            out.append((name, {}))
    return out


def keys_in(obj: Any) -> set[str]:
    if isinstance(obj, dict):
        return {str(k) for k in obj} | {k for v in obj.values() for k in keys_in(v)}
    if isinstance(obj, list):
        return {k for v in obj for k in keys_in(v)}
    return set()


def values_for(obj: Any, wanted: set[str]) -> list[Any]:
    if isinstance(obj, dict):
        hits = [v for k, v in obj.items() if k in wanted]
        return hits + [x for v in obj.values() for x in values_for(v, wanted)]
    if isinstance(obj, list):
        return [x for v in obj for x in values_for(v, wanted)]
    return []


def meal(results: dict[str, Any], slot: str) -> str:
    meals = (results.get("get_menu") or {}).get("meals") or []
    names = [str(m["name"]) for m in meals if m.get("slot") == slot]
    return names[0] if names else ""


# ---------------------------------------------------------------- the kitchen
FOODS = [
    # code, name, aisle, pack_g, label, kcal, protein, fat, carb, fibre
    ("oats", "Porridge oats", "grains", 1000, "1 kg bag", 379, 13.2, 8.0, 60.0, 10.0),
    ("skyr", "Skyr, natural", "dairy", 450, "450 g pot", 63, 11.0, 0.2, 4.0, 0.0),
    ("rasp", "Raspberries", "produce", 150, "150 g punnet", 32, 1.2, 0.3, 5.0, 6.5),
    ("almond", "Almonds", "grains", 200, "200 g bag", 579, 21.0, 50.0, 22.0, 12.5),
    ("chicken", "Chicken breast, raw", "meat", 500, "500 g pack", 165, 31.0, 3.6, 0.0, 0.0),
    ("chickpea", "Chickpeas, tinned, drained", "tins", 240, "400 g tin", 119, 7.2, 2.1, 16.1, 4.1),
    ("feta", "Feta", "dairy", 200, "200 g block", 264, 14.0, 21.0, 4.0, 0.0),
    ("lettuce", "Little gem lettuce", "produce", 200, "2 pack", 15, 1.4, 0.2, 2.9, 1.3),
    ("salmon", "Salmon fillet, raw", "fish", 240, "2 fillets", 208, 20.0, 13.0, 0.0, 0.0),
    ("sweetpot", "Sweet potato", "produce", 1000, "1 kg bag", 86, 1.6, 0.1, 20.0, 3.0),
    ("gbeans", "Green beans", "produce", 220, "220 g pack", 31, 1.8, 0.2, 7.0, 2.7),
    ("eggs", "Eggs, whole", "dairy", 360, "6 eggs", 143, 12.6, 9.5, 0.7, 0.0),
    ("bacon", "Back bacon", "meat", 300, "300 g pack", 215, 16.0, 17.0, 0.0, 0.0),
    ("spinach", "Baby spinach", "produce", 200, "200 g bag", 23, 2.9, 0.4, 3.6, 2.2),
    ("bread", "Wholemeal bread", "bakery", 800, "800 g loaf", 247, 13.0, 3.4, 41.0, 7.0),
    ("milk", "Milk, semi-skimmed", "dairy", 2000, "2 l", 50, 3.6, 1.8, 4.8, 0.0),
    ("turkey", "Turkey breast, raw", "meat", 500, "500 g pack", 104, 24.0, 1.0, 0.0, 0.0),
    ("carrot", "Carrots", "produce", 1000, "1 kg bag", 41, 0.9, 0.2, 10.0, 2.8),
    ("potato", "Potatoes", "produce", 2000, "2 kg bag", 77, 2.0, 0.1, 17.0, 2.2),
    ("beef", "Beef strips, lean", "meat", 400, "400 g pack", 150, 22.0, 7.0, 0.0, 0.0),
    ("noodle", "Egg noodles, dry", "grains", 250, "250 g pack", 360, 12.0, 2.0, 70.0, 3.0),
    ("kidney", "Kidney beans, tinned", "tins", 240, "400 g tin", 100, 7.0, 0.5, 14.0, 6.0),
    ("pepper", "Red pepper", "produce", 480, "3 pack", 31, 1.0, 0.3, 6.0, 2.1),
    ("wrap", "Tortilla wraps", "bakery", 480, "8 pack", 300, 8.0, 7.0, 50.0, 3.0),
    ("couscous", "Couscous, dry", "grains", 500, "500 g bag", 376, 13.0, 1.0, 77.0, 5.0),
    ("oil", "Olive oil", "condiments", 500, "500 ml", 899, 0.0, 99.9, 0.0, 0.0),
]


def seed_foods(db: Db) -> dict[str, int]:
    ids: dict[str, int] = {}
    for code, name, aisle, pack, label, kcal, p, f, c, fib in FOODS:
        ids[code] = db.insert(
            "ingredients",
            code=code,
            version=1,
            name=name,
            kcal_100=kcal,
            protein_100=p,
            fat_100=f,
            carb_100=c,
            fibre_100=fib,
            pack_g=pack,
            pack_label=label,
            aisle=aisle,
            pantry=0,
            use_whole_pack=0,
            approved=1,
            person_scope="both",
        )
        db.insert("ingredient_aliases", ingredient_code=code, alias=name.lower(), source="yaml")
    return ids


def dish(
    db: Db, foods: dict[str, int], name: str, grams: dict[str, float], steps: list[str], mins: int
) -> int:
    rid = db.insert(
        "recipes",
        name=name,
        servings=1,
        prep_minutes=mins,
        method_json=json.dumps(steps),
        source="model",
    )
    for code, g in grams.items():
        db.insert("recipe_ingredients", recipe_id=rid, ingredient_id=foods[code], grams=g)
    return rid


def a_menu(
    db: Db,
    person_id: int,
    days: dict[dt.date, dict[str, int]],
    *,
    decision: str | None = "accepted",
    status: str = "proposed",
) -> int:
    ordered = sorted(days)
    mid = db.insert(
        "menus",
        person_id=person_id,
        segment_start=isoday(ordered[0]),
        segment_end=isoday(ordered[-1]),
        status=status,
    )
    for day in ordered:
        md = db.insert(
            "menu_days",
            menu_id=mid,
            day=isoday(day),
            kcal=1900,
            protein_g=150,
            fat_g=65,
            carb_g=180,
            fibre_g=30,
        )
        for slot, rid in days[day].items():
            db.insert(
                "meal_slots",
                menu_day_id=md,
                slot=slot,
                recipe_id=rid,
                portion_multiplier=1.0,
                portion_g=350,
                kcal=600,
                protein_g=45,
                fat_g=20,
                carb_g=55,
                fibre_g=8,
            )
    if decision is not None:
        menu_mod.decide(db, mid, decision)
    return mid


@pytest.fixture
def home(fixture_db: Db, two_persons, frozen_clock: FrozenClock, config, tmp_path: Path):
    """Create a fixture household with menus, a list and food preferences."""
    db = fixture_db
    me, partner = two_persons["owner"], two_persons["partner"]
    frozen_clock.set_local(dt.datetime(2026, 9, 13, 18, 0, tzinfo=TZ))  # lists are built Sunday
    db.insert(
        "profiles",
        person_id=me,
        effective_from="2026-09-01",
        sex="m",
        birth_date="1990-03-15",
        height_cm=183.0,
        activity_factor=1.55,
        kcal_floor=1800,
        meals_per_day=3,
        confirm_time="20:30",
        segment_days=json.dumps([[0, 1, 2, 3, 4]]),
        refused_foods=json.dumps(["Chickpea"]),
    )
    db.insert(
        "profiles",
        person_id=partner,
        effective_from="2026-09-01",
        sex="f",
        birth_date="1992-06-01",
        height_cm=168.0,
        activity_factor=1.45,
        kcal_floor=1400,
        meals_per_day=3,
        confirm_time="20:30",
        segment_days=json.dumps([[0, 1, 2], [3, 4]]),
        refused_foods=json.dumps(["Liver", "broccoli", "chickpea"]),
    )
    for pid, kcal in ((me, 2250), (partner, 1750)):
        db.insert(
            "targets",
            person_id=pid,
            week_start=isoday(MONDAY),
            kcal=kcal,
            protein_g=150,
            fat_g=65,
            carb_g=180,
            fibre_g=30,
            source="formula",
        )
    f = seed_foods(db)
    oats = dish(db, f, "Overnight oats with skyr", {"oats": 60, "skyr": 150}, ["Soak."], 5)
    chicken_salad = dish(
        db,
        f,
        "Warm chicken, green bean and potato salad",
        {"chicken": 150, "gbeans": 80, "potato": 200},
        ["Boil the potatoes.", "Grill the chicken."],
        30,
    )
    turkey = dish(
        db,
        f,
        TURKEY,
        {"turkey": 180, "carrot": 150, "potato": 200, "oil": 5},
        ["Air-fry the wedges.", "Pan-fry the turkey with garlic."],
        35,
    )
    stir_fry = dish(db, f, STIR_FRY, {"beef": 150, "noodle": 80}, ["Stir fry."], 20)
    # the owner: an early proposal that lapsed, the one he accepted, and the repeat that superseded
    # it
    week = [MONDAY, TUESDAY, WEDNESDAY, THURSDAY, FRIDAY]
    a_menu(db, me, {d: {"dinner": stir_fry} for d in week}, decision="lapsed")
    a_menu(db, me, {d: {"lunch": chicken_salad, "dinner": stir_fry} for d in week})
    a_menu(
        db,
        me,
        {d: {"breakfast": oats, "lunch": chicken_salad, "dinner": turkey} for d in week},
        status="repeated",
    )
    # the partner: her accepted Mon-Wed, with the chickpea salad and the eggs and bacon
    porridge = dish(
        db,
        f,
        "Porridge with skyr, raspberries and almonds",
        {"oats": 50, "skyr": 150, "rasp": 60, "almond": 15},
        ["Cook the oats.", "Top with skyr, berries and almonds."],
        8,
    )
    salad = dish(
        db,
        f,
        CHICKPEA_SALAD,
        {"chicken": 120, "chickpea": 120, "feta": 30, "lettuce": 80},
        ["Grill the chicken.", "Toss with the chickpeas, feta and leaves."],
        25,
    )
    salmon = dish(
        db,
        f,
        SALMON,
        {"salmon": 120, "sweetpot": 200, "gbeans": 80},
        ["Air-fry the salmon and sweet potato.", "Steam the beans."],
        30,
    )
    eggs_bacon = dish(
        db,
        f,
        EGGS_BACON,
        {"eggs": 120, "bacon": 60, "spinach": 40, "bread": 70, "milk": 30, "oil": 5},
        EGGS_BACON_STEPS,
        15,
    )
    partner_menu = a_menu(
        db,
        partner,
        {
            MONDAY: {"breakfast": porridge, "lunch": salad, "dinner": salmon},
            TUESDAY: {"breakfast": eggs_bacon, "lunch": salmon, "dinner": salmon},
            WEDNESDAY: {"breakfast": porridge, "lunch": salad, "dinner": salmon},
        },
    )
    # the chickpea-free proposal she has not tapped yet
    wrap = dish(
        db,
        f,
        PEPPER_WRAP,
        {"chicken": 120, "kidney": 80, "pepper": 80, "wrap": 60},
        ["Warm the wrap.", "Fill."],
        15,
    )
    proposal = a_menu(
        db,
        partner,
        {d: {"breakfast": porridge, "lunch": wrap, "dinner": salmon} for d in week[:3]},
        decision=None,
    )
    shopping.build_once(db, partner_menu, clock=frozen_clock)
    couscous = dish(
        db,
        f,
        COUSCOUS,
        {"chicken": 120, "couscous": 60, "pepper": 80},
        ["Soak the couscous.", "Add chicken."],
        15,
    )
    frozen_clock.set_local(dt.datetime(2026, 9, 14, 12, 40, tzinfo=TZ))

    secrets_dir = tmp_path / "s"
    secrets_dir.mkdir()
    (secrets_dir / "telegram-token-dev.txt").write_text("1:t", encoding="utf-8")
    tg = FakeTelegram()
    holder: list[Any] = [None]
    app = App(
        config,
        Secrets(secrets_dir),
        clock=frozen_clock,
        transport=tg,
        gateway_factory=lambda _app, _db: holder[0],
    )
    poller = Poller(
        app.api(),
        db,
        app.registry(db),
        app.handlers(db).handle,
        clock=frozen_clock,
        sleeper=lambda s: None,
    )
    n = {"i": 0}

    def mark(pid: int) -> int:
        return int(
            db.scalar("SELECT COALESCE(MAX(id), 0) FROM messages_out WHERE person_id = ?", (pid,))
        )

    def heard(pid: int, since: int) -> list[str]:
        rows = db.all(
            "SELECT text_html FROM messages_out WHERE person_id = ? AND id > ? ORDER BY id",
            (pid, since),
        )
        return [str(r["text_html"]) for r in rows]

    def say(chat: int, text: str) -> list[str]:
        """What the person is sent in answer to one typed message."""
        pid = me if chat == OWNER_CHAT else partner
        before = mark(pid)
        n["i"] += 1
        tg.queue_update(tg.text_update(n["i"], chat, text, message_id=n["i"]))
        poller.run_once()
        return heard(pid, before)

    def tap(chat: int, data: str) -> list[str]:
        pid = me if chat == OWNER_CHAT else partner
        before = mark(pid)
        n["i"] += 1
        tg.queue_update(tg.callback_update(n["i"], chat, data, message_id=n["i"]))
        poller.run_once()
        return heard(pid, before)

    return {
        "db": db,
        "me": me,
        "partner": partner,
        "clock": frozen_clock,
        "holder": holder,
        "say": say,
        "tap": tap,
        "partner_menu": partner_menu,
        "proposal": proposal,
        "couscous": couscous,
        "salmon": salmon,
    }


def one(replies: list[str]) -> str:
    assert replies, "the person was sent nothing at all"
    return "\n".join(replies)


def last_intent(db: Db, pid: int) -> int:
    return int(db.scalar("SELECT MAX(id) FROM intents WHERE person_id = ?", (pid,)) or 0)


# ---------------------------------------------------------------- the stories
def test_01_owner_asks_whats_for_dinner_and_hears_tonights_dish(home) -> None:
    """
    the one on the menu that replaced the two before it, and only that one."""
    home["clock"].set_local(dt.datetime(2026, 9, 14, 18, 30, tzinfo=TZ))
    home["holder"][0] = Diligent(
        [("get_menu", {"day": isoday(MONDAY)})],
        lambda r: f"Tonight: {meal(r, 'dinner') or 'nothing on the menu'}.",
    )
    out = one(home["say"](OWNER_CHAT, "what's for dinner"))
    assert TURKEY in out
    assert STIR_FRY not in out, "a superseded menu's dinner is not tonight's"


def test_02_sam_asks_whats_for_dinner_and_hears_partner_own(home) -> None:
    """
    accepted, never his."""
    home["holder"][0] = Diligent(
        [("get_menu", {"day": isoday(MONDAY)})],
        lambda r: f"Tonight: {meal(r, 'dinner') or 'nothing on the menu'}.",
    )
    home["clock"].set_local(dt.datetime(2026, 9, 14, 17, 50, tzinfo=TZ))
    out = one(home["say"](PARTNER_CHAT, "what's for dinner?"))
    assert SALMON in out
    assert TURKEY not in out, "his menu never reaches her"


def test_03_show_me_tomorrow_with_the_model_down_still_answers(home) -> None:
    """
    tomorrow". Tomorrow's menu is rows in the database; she should get it, or at least be
    pointed at the command that shows it - not "try again in a few minutes"."""
    home["clock"].set_local(dt.datetime(2026, 9, 14, 21, 30, tzinfo=TZ))
    home["holder"][0] = Diligent(raises=ModelUnavailable("claude quota"))
    out = one(home["say"](PARTNER_CHAT, "show me tomorrow"))
    assert EGGS_BACON in out or "/menu" in out, (
        f"the answer needed no model, but she was told: {out!r}"
    )


def test_04_recipe_for_tomorrows_breakfast_gives_how_to_make_it(home) -> None:
    """
    eggs and bacon, in the chat, without learning /menu, day buttons and meal buttons."""
    home["clock"].set_local(dt.datetime(2026, 9, 14, 21, 30, tzinfo=TZ))

    def compose(r: dict[str, Any]) -> str:
        name = meal(r, "breakfast")
        steps = [
            s
            for v in values_for(r, {"steps", "method", "method_json"})
            for s in (v if isinstance(v, list) else [v])
        ]
        if steps:
            return f"{name}: " + " ".join(str(s) for s in steps)
        return f"{name}. I can see the dish but not how it is made."

    home["holder"][0] = Diligent(every_read(TUESDAY), compose)
    out = one(home["say"](PARTNER_CHAT, "recipe for tomorrow's breakfast"))
    assert EGGS_BACON in out, "the dish itself is found"
    assert EGGS_BACON_STEPS[0] in out, (
        "a model that read every tool it has could not give her the method: " + out
    )


def test_05_no_bacon_please_says_tomorrows_breakfast_still_has_bacon(home) -> None:
    db, partner = home["db"], home["partner"]
    home["clock"].set_local(dt.datetime(2026, 9, 14, 13, 0, tzinfo=TZ))
    home["holder"][0] = None  # no model at all: the refusal does not need one
    done = one(home["say"](PARTNER_CHAT, "no bacon please"))
    assert "bacon" in done.lower() and "Kept off your menus" in done
    assert "Tue" in done or "tomorrow" in done.lower(), (
        "her accepted menu still serves bacon tomorrow and nothing says so: " + done
    )
    refused = json.loads(
        str(
            db.scalar(
                "SELECT refused_foods FROM profiles WHERE person_id = ? ORDER BY id DESC LIMIT 1",
                (partner,),
            )
        )
    )
    assert "bacon" in refused


def test_06_no_chickpeas_a_fourth_time_points_at_the_menu_waiting_for_partner(home) -> None:
    db, partner = home["db"], home["partner"]
    home["clock"].set_local(dt.datetime(2026, 9, 14, 12, 50, tzinfo=TZ))
    home["holder"][0] = None
    heard = one(home["say"](PARTNER_CHAT, "no chickpeas!!"))
    refused = json.loads(
        str(
            db.scalar(
                "SELECT refused_foods FROM profiles WHERE person_id = ? ORDER BY id DESC LIMIT 1",
                (partner,),
            )
        )
    )
    problems = []
    if "Already on the list" not in heard:
        problems.append("she is not told it was already kept off")
    if not any(w in heard for w in ("Wednesday", "Wed")):
        problems.append("nothing she was sent mentions Wednesday's salad")
    if sum("chickpea" in str(x).casefold() for x in refused) > 1:
        problems.append(f"her avoid list now holds chickpea twice: {refused!r}")
    assert not problems, f"{problems}; she was sent {heard!r}"


def test_07_whats_for_lunch_today_warns_about_the_chickpeas(home) -> None:
    """
    salad she has refused, and a chickpea-free menu is waiting for her yes. /menu says both;
    the words should too."""

    def compose(r: dict[str, Any]) -> str:
        lunch = meal(r, "lunch")
        blob = json.dumps(r).lower()
        flagged = any("refus" in k or "avoid" in k for k in keys_in(r))
        waiting = PEPPER_WRAP.lower() in blob or "waiting" in blob
        parts = [f"Lunch today: {lunch}."]
        if flagged:
            parts.append("It has something you avoid in it.")
        if waiting:
            parts.append("A new menu is waiting for your yes.")
        return " ".join(parts)

    home["holder"][0] = Diligent(every_read(MONDAY), compose)
    out = one(home["say"](PARTNER_CHAT, "what's for lunch today?"))
    assert CHICKPEA_SALAD in out
    menu_cmd = one(home["say"](PARTNER_CHAT, "/menu today"))
    assert "avoid" in out or "waiting" in out, (
        "nothing the chat model can read says she refuses chickpeas or that a chickpea-free "
        f"menu is waiting. Chat said {out!r}; /menu today said {menu_cmd!r}"
    )


def test_08_what_do_i_buy_on_wednesday_matches_the_shopping_command(home) -> None:
    """
    has accepted a Thu-Fri menu; she wants Thursday-Friday's food, and the same list /shopping
    would give her - not the Mon-Wed list she already shopped, with the chickpeas on it."""
    db, partner = home["db"], home["partner"]
    a_menu(
        db,
        partner,
        {d: {"lunch": home["couscous"], "dinner": home["salmon"]} for d in (THURSDAY, FRIDAY)},
    )
    home["clock"].set_local(dt.datetime(2026, 9, 16, 17, 30, tzinfo=TZ))

    def compose(r: dict[str, Any]) -> str:
        items = (r.get("get_shopping") or {}).get("items") or []
        return "Buy: " + ", ".join(str(i["name"]) for i in items) if items else "No list."

    home["holder"][0] = Diligent([("get_shopping", {})], compose)
    out = one(home["say"](PARTNER_CHAT, "what do I buy"))
    command = one(home["say"](PARTNER_CHAT, "/shopping"))
    assert "Couscous" in command, "the command itself knows Thursday's list"
    assert "Couscous" in out and "Chickpeas" not in out, (
        f"the words and the command disagree in the shop. Chat: {out!r}. /shopping: {command!r}"
    )


def test_09_whats_for_dinner_tomorrow_after_not_quite_is_not_logged_as_food(home) -> None:
    """
    asks "what's for dinner tomorrow?". It is a question, not his food for today."""
    db, me = home["db"], home["me"]
    home["clock"].set_local(dt.datetime(2026, 9, 14, 20, 35, tzinfo=TZ))
    gw = Diligent(
        [("get_menu", {"day": isoday(TUESDAY)})],
        lambda r: f"Tomorrow: {meal(r, 'dinner')}.",
        json_data={"items": [{"name": "dinner", "kcal": 700}], "kcal": 700},
    )
    home["holder"][0] = gw
    asked = one(home["tap"](OWNER_CHAT, f"k:cfmx:{MONDAY:%Y%m%d}"))
    assert "What changed?" in asked
    out = one(home["say"](OWNER_CHAT, "what's for dinner tomorrow?"))
    notes = [
        str(r["note"] or "")
        for r in db.all("SELECT note FROM day_confirmations WHERE person_id = ?", (me,))
    ]
    offplan = db.scalar(
        "SELECT COUNT(*) FROM intents WHERE person_id = ? AND kind = 'log_offplan'", (me,)
    )
    assert not any("dinner tomorrow" in x for x in notes) and offplan == 0, (
        f"the question became Monday's food: notes={notes!r}, off-plan cards={offplan}, "
        f"he was sent {out!r}"
    )
    assert TURKEY in out


def test_10_swap_wednesday_dinner_with_the_tape_question_open_is_not_centimetres(home) -> None:
    """
    dinner?". He wants an answer about Wednesday, not "I need centimetres"."""
    db, me = home["db"], home["me"]
    clock = home["clock"]
    clock.set_local(dt.datetime(2026, 9, 13, 10, 0, tzinfo=TZ))
    MeasuresFlow(clock=clock).arm(db, me, SUNDAY)  # a manual /measure question
    clock.set_local(dt.datetime(2026, 9, 13, 16, 50, tzinfo=TZ))
    gw = Diligent(
        [("get_menu", {"day": isoday(WEDNESDAY)})],
        lambda r: f"Wednesday dinner is {meal(r, 'dinner')}. /cook replace builds another.",
    )
    home["holder"][0] = gw
    out = one(home["say"](OWNER_CHAT, "can I swap Wednesday dinner?"))
    assert "centimetres" not in out, f"the tape question swallowed his question: {out!r}"
    assert gw.conversations, "and the question reached something that could answer it"


def test_11_a_question_after_tapping_change_is_not_read_as_the_change(home) -> None:
    """Mon 12:40, the partner taps Change on the waiting proposal, is asked what to change, and
    types "what's for lunch on thursday?" instead. That is a question; it must not be sent
    off to rebuild her week as though it were the change she wants."""
    gw = Diligent(
        [("get_menu", {"day": isoday(THURSDAY)})],
        lambda r: f"Thursday lunch: {meal(r, 'lunch') or 'no menu covers Thursday yet'}.",
        json_raises=ModelUnavailable("not needed"),
    )
    home["holder"][0] = gw
    asked = one(home["tap"](PARTNER_CHAT, f"k:menux:{home['proposal']}"))
    assert "Tell me what to change" in asked
    out = one(home["say"](PARTNER_CHAT, "what's for lunch on thursday?"))
    assert "Working out what to cook" not in out and not gw.json_calls, (
        f"her question was handed to the menu builder as the change: {out!r}"
    )
    assert gw.conversations, "the question reached chat"


def test_12_make_it_simpler_after_tapping_change_reaches_the_menu_builder(home) -> None:
    """
    words she used are what the next menu has to be built around."""
    gw = Diligent(json_raises=ModelUnavailable("recorded, not answered"))
    home["holder"][0] = gw
    home["clock"].set_local(dt.datetime(2026, 9, 14, 12, 45, tzinfo=TZ))
    one(home["tap"](PARTNER_CHAT, f"k:menux:{home['proposal']}"))
    out = one(home["say"](PARTNER_CHAT, "make it simpler"))
    assert "Working out what to cook, with make it simpler" in out
    assert gw.json_calls, f"the menu builder never asked the model: {out!r}"
    assert all("make it simpler" in prompt for _task, prompt in gw.json_calls)
    assert "try /cook again" in out.lower() or "could not" in out.lower(), (
        "and with no model it says so rather than going quiet"
    )
