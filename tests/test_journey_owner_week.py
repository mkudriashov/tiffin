"""

The world is his live menu 6 in shape: a repeat of an accepted Mon-Fri menu, the same five
dishes every day but a fresh recipe row per day (as the live table has it - recipes 66 to 90),
the real ingredients, steps, portion multipliers, keep-days and freezable flags. He batches
lunch and dinner on the Sunday, freezes, and has an air fryer, a hob, a microwave and an oven.

Every story drives the real Poller and handlers, the real Scheduler for the unprompted messages,
or the real web app with his Tailscale login, and asserts on what reached his phone or his page.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import types
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from tiffin import body
from tiffin import scheduler as sc
from tiffin import targets as targets_mod
from tiffin.app import App
from tiffin.clock import TZ, FrozenClock, isoday
from tiffin.db import Db
from tiffin.food import shopping
from tiffin.jobs import evening_confirm, morning_targets, shop_refresh, shopping_list
from tiffin.model.gateway import ModelResult
from tiffin.secrets import Secrets
from tiffin.telegram import outbox as outbox_mod
from tiffin.telegram.poller import Poller
from tiffin.web.app import create_app

from .fakes import FakeTelegram
from .test_food_shopping import seed_ingredient

OWNER_CHAT = 1000001
PARTNER_CHAT = 1000002
OWNER_LOGIN = "alex@example.com"
HEADER = "Tailscale-User-Login"

SUN13 = dt.date(2026, 9, 13)
MON14 = dt.date(2026, 9, 14)
TUE15 = dt.date(2026, 9, 15)
WED16 = dt.date(2026, 9, 16)
THU17 = dt.date(2026, 9, 17)
FRI18 = dt.date(2026, 9, 18)
SAT19 = dt.date(2026, 9, 19)
SUN20 = dt.date(2026, 9, 20)
MON21 = dt.date(2026, 9, 21)

# ---------------------------------------------------------------- menu 6, as it is on the box
SALAD = "Warm chicken, green bean and potato salad"
TURKEY = "Garlic turkey with air-fried carrots and potato wedges"
OATS = "Overnight oats with skyr, whey and berries"
YOGHURT = "Strawberry protein yoghurt with almonds"
SKYR = "Skyr with peanut butter and raspberries"

#: slot -> (name, prep minutes, method, {ingredient code: grams}, multiplier, slot kcal)
DISHES: dict[str, tuple[str, int, list[str], dict[str, float], float, int]] = {
    "breakfast": (
        OATS,
        5,
        [
            "Stir the whey into the skyr until smooth.",
            "Mix in the oats and frozen raspberries.",
            "Cover and chill overnight.",
            "Top with sliced strawberries and chopped almonds before eating.",
        ],
        {"oats": 40, "skyr": 150, "whey": 20, "rasp": 80, "straw": 80, "almond": 10},
        0.9,
        400,
    ),
    "lunch": (
        SALAD,
        30,
        [
            "Cut the potatoes into chunks and boil on the hob for 15 minutes.",
            "Brush the chicken with a little of the olive oil, season, and air fry 200C 18 "
            "minutes until cooked through.",
            "Boil the green beans and broccoli for the last 4 minutes with the potatoes, "
            "then drain.",
            "Slice the chicken and avocado.",
            "Toss lettuce, potatoes, greens, chicken and avocado with the remaining olive oil, "
            "salt and pepper.",
        ],
        {
            "chicken": 170,
            "potato": 100,
            "beans": 80,
            "broccoli": 80,
            "lettuce": 50,
            "avocado": 50,
            "olive": 15,
            "salt": 1,
            "pepper": 1,
        },
        0.85,
        450,
    ),
    "dinner": (
        TURKEY,
        # Simplified to one appliance and four steps (was air fryer + hob + microwave, six
        # steps) - the same "not simple" rule test_journey_simple_recipes.py measures, and
        # this is the same real dish (menu 6, live recipe 68) with its SIMPLE[68] method.
        27,
        [
            "Cut the potatoes into wedges and the carrots into batons; toss with half the oil "
            "and salt.",
            "Air fry the potatoes and carrots 200C 12 minutes.",
            "Rub the turkey with crushed garlic, the rest of the oil and pepper; add it and "
            "the broccoli and air fry 200C 15 minutes more until cooked through.",
            "Serve the turkey with the wedges, carrots and broccoli.",
        ],
        {
            "turkey": 210,
            "potato": 100,
            "carrot": 100,
            "broccoli": 100,
            "rapeseed": 17,
            "garlic": 5,
            "salt": 1,
            "pepper": 1,
        },
        0.95,
        500,
    ),
    "snack": (
        YOGHURT,
        3,
        [
            "Stir the whey into the yoghurt until smooth.",
            "Top with sliced strawberries and almonds.",
        ],
        {"yoghurt": 150, "whey": 20, "straw": 100, "almond": 15},
        1.25,
        300,
    ),
    "pre_bed": (
        SKYR,
        3,
        [
            "Microwave the raspberries 40 seconds to thaw.",
            "Stir the whey into the skyr.",
            "Top with the raspberries and peanut butter.",
        ],
        {"skyr": 125, "whey": 10, "pb": 15, "rasp": 50},
        1.0,
        265,
    ),
}

FOODS = (
    ("oats", "Porridge oats", "grains", 1000, "1 kg bag"),
    ("skyr", "Skyr, natural", "dairy", 450, "450 g tub"),
    ("whey", "Whey protein powder", "dairy", 1000, "1 kg bag"),
    ("rasp", "Raspberries, frozen", "frozen", 350, "350 g bag"),
    ("straw", "Strawberries, raw", "produce", 400, "400 g punnet"),
    ("almond", "Almonds, whole", "condiments", 200, "200 g bag"),
    ("chicken", "Chicken breast, raw", "meat", 500, "500 g pack"),
    ("potato", "Potatoes, raw", "produce", 2000, "2 kg bag"),
    ("beans", "Green beans, raw", "produce", 200, "200 g pack"),
    ("broccoli", "Broccoli, raw", "produce", 375, "375 g pack"),
    ("lettuce", "Lettuce, raw", "produce", 300, "1 lettuce"),
    ("avocado", "Avocado, Hass", "produce", 300, "2 avocados"),
    ("olive", "Olive oil", "condiments", 500, "500 ml bottle"),
    ("salt", "Salt", "condiments", 750, "750 g tub"),
    ("pepper", "Black pepper, ground", "condiments", 100, "100 g jar"),
    ("turkey", "Turkey breast, raw", "meat", 500, "500 g pack"),
    ("carrot", "Carrots, raw", "produce", 1000, "1 kg bag"),
    ("rapeseed", "Rapeseed oil", "condiments", 1000, "1 litre bottle"),
    ("garlic", "Garlic, raw", "produce", 50, "1 bulb"),
    ("yoghurt", "Greek yoghurt 0% fat", "dairy", 500, "500 g tub"),
    ("pb", "Peanut butter, smooth", "condiments", 340, "340 g jar"),
)


def _seed_week(
    db: Db, pid: int, ing: dict[str, int], start: dt.date, *, status: str, decision: str | None
) -> int:
    """A Mon-Fri menu of the five dishes, one recipe row per day-slot as the live repeat has."""
    mid = db.insert(
        "menus",
        person_id=pid,
        segment_start=isoday(start),
        segment_end=isoday(start + dt.timedelta(days=4)),
        status=status,
    )
    for n in range(5):
        day_id = db.insert(
            "menu_days",
            menu_id=mid,
            day=isoday(start + dt.timedelta(days=n)),
            kcal=1915,
            protein_g=203,
            fat_g=79,
            carb_g=106,
            fibre_g=28,
        )
        for slot, (name, prep, steps, parts, mult, kcal) in DISHES.items():
            rid = db.insert(
                "recipes",
                name=name,
                servings=1,
                prep_minutes=prep,
                method_json=json.dumps(steps),
                source="model",
                keep_days=3,
                freezable=0,
            )
            for code, grams in parts.items():
                db.insert("recipe_ingredients", recipe_id=rid, ingredient_id=ing[code], grams=grams)
            db.insert(
                "meal_slots",
                menu_day_id=day_id,
                slot=slot,
                recipe_id=rid,
                portion_multiplier=mult,
                portion_g=300,
                kcal=kcal,
                protein_g=40,
                fat_g=15,
                carb_g=20,
                fibre_g=5,
            )
    if decision is not None:
        db.insert("menu_decisions", menu_id=mid, decision=decision)
    return mid


class _Gateway:
    """The model, answering the one question this week asks it: what was that burrito."""

    def json_task(self, task: str, prompt: str, schema_name: str, *, person_id: int):
        return ModelResult(
            text="",
            data={
                "items": [{"name": "chicken burrito", "kcal": 650}],
                "kcal": 650,
                "protein_g": 35,
                "fat_g": 25,
                "carb_g": 70,
                "fibre_g": 6,
            },
            model_used="fake",
            backend="fake",
        )

    def __getattr__(self, name: str) -> Any:  # anything else the chat flow might ask for
        raise AttributeError(name)


@pytest.fixture
def week(config, tmp_path, frozen_clock: FrozenClock, monkeypatch: pytest.MonkeyPatch):
    config.paths.ensure()
    db = Db.open(config.paths.db, clock=frozen_clock)
    assert "admin" not in str(config.paths.db).lower().split("\\")
    his = db.insert("persons", slug="owner", display_name="Alex", tone="blunt")
    partner = db.insert("persons", slug="partner", display_name="Sam", tone="warm")
    db.insert("telegram_identities", person_id=his, chat_id=OWNER_CHAT, method="config")
    db.insert("telegram_identities", person_id=partner, chat_id=PARTNER_CHAT, method="config")
    db.insert("tailscale_identities", person_id=his, login=OWNER_LOGIN)
    base = {
        "effective_from": "2026-09-01",
        "birth_date": "1998-04-30",
        "activity_factor": 1.375,
        "quiet_start": "22:00",
    }
    db.insert(
        "profiles",
        person_id=his,
        sex="m",
        height_cm=186.0,
        kcal_floor=1850,
        segment_days=json.dumps([[0, 1, 2, 3, 4]]),
        meals_per_day=4,
        pre_bed_protein=1,
        variety_json=json.dumps({"all": "same"}),
        batch_slots=json.dumps(["lunch", "dinner"]),
        freezes=1,
        refused_foods=json.dumps(["Chickpea"]),
        equipment_json=json.dumps(["air_fryer", "hob", "microwave", "oven"]),
        confirm_time="20:00",
        morning_time="06:30",
        quiet_end="06:00",
        **base,
    )
    db.insert(
        "profiles",
        person_id=partner,
        sex="f",
        height_cm=168.0,
        kcal_floor=1200,
        segment_days=json.dumps([[0, 1, 2], [3, 4]]),
        batch_slots=json.dumps(["lunch", "dinner"]),
        freezes=0,
        refused_foods=json.dumps(["chickpea"]),
        confirm_time="21:00",
        morning_time="08:00",
        quiet_end="07:30",
        **base,
    )
    body.record_weigh_in(db, his, dt.date(2026, 9, 12), 100.5, "manual")
    ing = {
        code: seed_ingredient(db, code, name, aisle, pack, label)
        for code, name, aisle, pack, label in FOODS
    }
    menu6 = _seed_week(db, his, ing, MON14, status="repeated", decision="accepted")

    real_init = outbox_mod.Outbox.__init__

    def prod_outbox(self, api, db_, **kw):
        kw["env"] = "prod"
        real_init(self, api, db_, **kw)

    monkeypatch.setattr(outbox_mod.Outbox, "__init__", prod_outbox)
    monkeypatch.setattr(shop_refresh, "fill_for_menus", lambda *a, **k: "")

    secrets_dir = tmp_path / "s"
    secrets_dir.mkdir()
    (secrets_dir / "telegram-token-dev.txt").write_text("1:t", encoding="utf-8")
    config.data["web"]["secure_cookies"] = False
    tg = FakeTelegram()
    gw = _Gateway()
    app = App(
        config,
        Secrets(secrets_dir),
        clock=frozen_clock,
        transport=tg,
        gateway_factory=lambda _a, _d: cast("Any", gw),
    )
    handlers = app.handlers(db)
    poller = Poller(
        app.api(), db, app.registry(db), handlers.handle, clock=frozen_clock, sleeper=lambda s: None
    )
    n = {"i": 0}

    def last() -> dict[str, Any]:
        got = [m for m in tg.sent if int(m["chat_id"]) == OWNER_CHAT]
        assert got, "nothing reached Alex"
        return got[-1]

    def say(text: str) -> dict[str, Any]:
        n["i"] += 1
        tg.queue_update(tg.text_update(n["i"], OWNER_CHAT, text))
        poller.run_once()
        return last()

    def tap(data: str) -> dict[str, Any]:
        n["i"] += 1
        tg.queue_update(tg.callback_update(n["i"], OWNER_CHAT, data, message_id=n["i"] + 1000))
        poller.run_once()
        return last()

    def at(day: dt.date, hhmm: str) -> None:
        h, m = (int(x) for x in hhmm.split(":"))
        frozen_clock.set_local(dt.datetime(day.year, day.month, day.day, h, m, tzinfo=TZ))

    def run(job: sc.Job, day: dt.date) -> dict[str, Any] | None:
        prof = targets_mod.profile_on(db, his, day)
        slots = sc.slots_for(job, his, prof, day, db)
        assert slots, f"{job.name} has no slot on {day}"
        before = len(tg.sent)
        frozen_clock.set_local(slots[0].due_local)
        sc.Scheduler(app, [job], lambda _p, _fn: None).execute(db, slots[0])
        mine = [m for m in tg.sent[before:] if int(m["chat_id"]) == OWNER_CHAT]
        return mine[-1] if mine else None

    client_box: dict[str, TestClient] = {}

    def page(path: str) -> Any:
        if "c" not in client_box:
            client_box["c"] = TestClient(create_app(app), base_url="http://testserver")
        got = client_box["c"].get(path, headers={HEADER: OWNER_LOGIN})
        assert got.status_code == 200, f"{path}: {got.status_code} {got.text[:200]}"
        return got.json()

    ns = types.SimpleNamespace(
        db=db,
        app=app,
        tg=tg,
        his=his,
        ing=ing,
        menu6=menu6,
        say=say,
        tap=tap,
        at=at,
        run=run,
        page=page,
        clock=frozen_clock,
    )
    yield ns
    db.close()


# ---------------------------------------------------------------- reading a phone
def taps(msg: dict[str, Any]) -> list[str]:
    kb = msg.get("reply_markup") or {}
    if isinstance(kb, str):
        kb = json.loads(kb)
    return [str(b.get("callback_data", "")) for row in kb.get("inline_keyboard", []) for b in row]


def text_of(msg: dict[str, Any] | None) -> str:
    return "" if msg is None else str(msg["text"])


def line_with(text: str, needle: str) -> str:
    return next((ln for ln in text.splitlines() if needle in ln), "")


def meal_tap(msg: dict[str, Any], slot: str) -> str:
    found = next((d for d in taps(msg) if d.startswith("k:meal:") and f":{slot}" in d), "")
    assert found, f"no {slot} button: {taps(msg)}"
    return found


# ---------------------------------------------------------------- 1. Monday morning
def test_01_monday_morning_owner_reads_todays_food_and_reaches_lunch_in_two_moves(week) -> None:
    """Monday 06:30, alarm off, the morning message arrives. He wants to know what he is eating
    today and, from there, how lunch is made - one command and one tap at most."""
    msg = week.run(morning_targets.JOB, MON14)
    text = text_of(msg)
    assert text, "no morning message on Monday"
    # two lines since 26 Sep: the first meal, and the day on the page -
    # which lists every dish, with its recipe, one tap away
    assert len([ln for ln in text.splitlines() if ln.strip()]) == 2, text
    assert OATS in text and "The day on the page" in text, text
    today = week.page("/api/today?date=2026-09-14")
    for dish in (OATS, SALAD, TURKEY, YOGHURT, SKYR):
        assert dish in [m["name"] for m in today["meals"]], f"{dish} is not on the page"
    week.at(MON14, "06:35")
    menu = week.say("/menu")
    assert "Menu Mon 14 Sep" in menu["text"], menu["text"]
    card = week.tap(meal_tap(menu, "lunch"))["text"]
    assert SALAD in card and "<b>Ingredients</b>" in card and "<b>Method</b>" in card, card


# ---------------------------------------------------------------- 2. the box and the card
def test_02_thursday_lunch_card_weighs_what_owner_put_in_the_box_on_sunday(week) -> None:
    """Sunday 10:00 he weighs chicken into five lunch boxes from /batch full. Thursday 12:40, a
    box in hand, he opens Thursday's lunch card: the chicken it names is the chicken in the box."""
    week.at(SUN13, "10:00")
    batch = week.say("/batch full")["text"]
    m = re.search(r"Chicken breast, raw [\d.]+ k?g across 5 boxes, ([\d.]+) g each", batch)
    assert m, f"no per-box chicken on the cook message: {batch}"
    week.at(THU17, "12:40")
    menu = week.say("/menu")
    assert "Menu Thu 17 Sep" in menu["text"], menu["text"]
    card = week.tap(meal_tap(menu, "lunch"))["text"]
    assert f"Chicken breast, raw {m.group(1)} g" in card, (
        f"the box got {m.group(1)} g of chicken; the card says: "
        f"{line_with(card, 'Chicken breast')!r}"
    )


# ---------------------------------------------------------------- 3. freezer vs card
def test_03_what_sunday_tells_him_to_freeze_the_recipe_card_agrees_can_be_frozen(week) -> None:
    """Sunday 10:00, twenty boxes on the counter. /batch tells him which go in the freezer; he
    checks Thursday's lunch card before freezing a salad with lettuce and avocado in it."""
    week.at(SUN13, "10:00")
    batch = week.say("/batch")["text"]
    freeze = line_with(batch, "Freeze")
    assert SALAD in freeze and "Thu, Fri" in freeze, f"setup: no freeze line for the salad: {batch}"
    menu = week.say("/menu thu")
    card = week.tap(meal_tap(menu, "lunch"))["text"]
    assert SALAD in card, card
    keeps = line_with(card, "Keeps")
    assert "freez" in keeps.lower(), (
        f"/batch says {freeze!r}; Thursday's card for the same salad says {keeps!r} - "
        "one tells him to freeze it, the other says it keeps in the fridge and never freezes"
    )


# ---------------------------------------------------------------- 4. thawing
def test_04_wednesday_evening_owner_is_reminded_to_move_thursdays_boxes_to_the_fridge(week) -> None:
    """Wednesday 20:00, the evening check-in. Thursday's lunch and dinner went into the freezer on
    Sunday; tonight is the only night that moving them to the fridge makes Thursday work."""
    msg = week.run(evening_confirm.JOB, WED16)
    text = text_of(msg)
    assert text, "no evening check-in on Wednesday"
    words = ("thaw", "defrost", "freezer", "take out", "move")
    assert any(w in text.lower() for w in words), (
        f"Thursday's boxes are frozen and nothing tonight says to take them out:\n{text}"
    )


# ---------------------------------------------------------------- 5. /batch mid-week
def test_05_wednesday_night_batch_does_not_send_him_back_to_cook_the_week(week) -> None:
    """Wednesday 21:00, he half-remembers Sunday's freezer line and sends /batch to see which
    boxes are frozen. He cooked on Sunday; he is not about to cook ten boxes tonight."""
    week.at(WED16, "21:00")
    text = week.say("/batch")["text"]
    assert "Batch cook Wed 16 Sep" not in text and "10 boxes" not in text, (
        f"on Wednesday night the cook message tells him to batch cook the whole week, "
        f"Monday's and Tuesday's eaten boxes included:\n{text}"
    )


# ---------------------------------------------------------------- 6. the page
def test_06_on_the_page_their_recipe_book_lists_each_dish_once(week) -> None:
    """Tuesday 21:00, on the sofa, he opens the tailnet page to browse the week's recipes. Five
    dishes are on his menu; he wants five entries, not the same salad five times."""
    week.at(TUE15, "21:00")
    got = week.page("/api/dishes")
    names = [str(d["name"]) for d in got["dishes"]]
    assert sorted(set(names)) == sorted({OATS, SALAD, TURKEY, YOGHURT, SKYR}), names
    assert len(names) == len(set(names)), (
        f"{len(names)} entries for {len(set(names))} dishes on his recipe book: "
        f"{sorted(names)[:6]}..."
    )


# ---------------------------------------------------------------- 7. off plan
def test_07_owner_skipped_dinner_for_a_burrito_and_the_day_is_not_counted_twice(week) -> None:
    """Tuesday 20:00, the check-in. He left the turkey box in the fridge and had a burrito at
    work. He says so, accepts the estimate, and later reads the day on the page."""
    msg = week.run(evening_confirm.JOB, TUE15)
    assert "k:cfmx:20260915" in taps(msg), taps(msg)
    week.at(TUE15, "20:05")
    week.tap("k:cfmx:20260915")
    asked = week.say("skipped the turkey dinner, had a chicken burrito at work instead")
    assert "skipping dinner" in asked["text"], asked["text"]
    assert "k:mslotok:20260915" in taps(asked), taps(asked)
    card = week.tap("k:mslotok:20260915")
    accept = next((d for d in taps(card) if d.startswith("k:intent:")), "")
    assert accept, f"no estimate to accept: {card['text']}"
    said = week.tap(accept)["text"]
    assert "650" in said, said
    record = week.page("/api/today?date=2026-09-15")["record"]
    assert record is not None and record["kcal"] is not None, record
    honest = 1915 - 500 + 650  # the plan, less the dinner he did not eat, plus the burrito
    assert float(record["kcal"]) <= honest + 1, (
        f"he said he skipped the 500 kcal dinner and ate a 650 kcal burrito; the day is "
        f"recorded at {record['kcal']:.0f} kcal (the whole plan plus the burrito), not ~{honest}"
    )


# ---------------------------------------------------------------- 8. Saturday shop
def test_08_saturday_in_the_shop_shopping_is_for_the_week_ahead(week) -> None:
    """Saturday 10:00, trolley in hand, next week's menu accepted on Friday. He sends /shopping
    to buy for Monday 21 to Friday 25 - not the week that has just been eaten."""
    week.at(SUN13, "18:00")
    shopping.build_once(week.db, week.menu6)
    week.at(FRI18, "19:00")
    _seed_week(week.db, week.his, week.ing, MON21, status="proposed", decision="accepted")
    week.at(SAT19, "10:00")
    head = week.say("/shopping")["text"].splitlines()[0]
    assert "Mon 21" in head, (
        f"next week's menu is accepted; /shopping on Saturday answers with: {head!r}"
    )


# ---------------------------------------------------------------- 9. list before cook
def test_09_next_weeks_list_arrives_before_the_sunday_owner_is_told_to_cook_on(week) -> None:
    """Saturday 10:00 /batch says the cook is Sunday 20. He wants the list for it in time to
    shop before he cooks, which means before Sunday begins."""
    week.at(FRI18, "19:00")
    _seed_week(week.db, week.his, week.ing, MON21, status="proposed", decision="accepted")
    week.at(SAT19, "10:00")
    batch = week.say("/batch")["text"]
    assert "Batch cook Sun 20 Sep" in batch, batch
    sent_at: dt.datetime | None = None
    for day in (FRI18, SAT19, SUN20):
        prof = targets_mod.profile_on(week.db, week.his, day)
        for slot in sc.slots_for(shopping_list.JOB, week.his, prof, day, week.db):
            week.clock.set_local(slot.due_local)
            before = len(week.tg.sent)
            sc.Scheduler(week.app, [shopping_list.JOB], lambda _p, _fn: None).execute(week.db, slot)
            for m in week.tg.sent[before:]:
                if int(m["chat_id"]) == OWNER_CHAT and "Shopping for Mon 21" in m["text"]:
                    sent_at = sent_at or slot.due_local
    assert sent_at is not None, "no list for Mon 21 was ever sent"
    assert sent_at.date() < SUN20, (
        f"the cook is Sun 20 Sep and the list for it arrives {sent_at:%a %d %b %H:%M}"
    )


# ---------------------------------------------------------------- 10. the empty shelf
def test_10_at_the_potato_shelf_the_list_says_every_dish_they_are_for(week) -> None:
    """Sunday 11:00, the shop is out of potatoes. He reads the potato line on /shopping to decide
    whether it matters - and the potatoes go into both the salad and the turkey wedges."""
    week.at(SUN13, "11:00")
    text = week.say("/shopping")["text"]
    line = line_with(text, "Potatoes")
    assert line, text
    assert "Warm chicken" in line and "Garlic turkey" in line, (
        f"the potatoes feed the lunch salad AND the dinner wedges; the line names one: {line!r}"
    )


# ---------------------------------------------------------------- 11. a weeknight card
def test_11_mondays_dinner_card_is_a_simple_cook(week) -> None:
    """Monday 19:10, in from work, he opens tonight's dinner card to reheat or cook it. "Simple but
    healthy": a handful of foods, five steps at most, one or two appliances."""
    week.at(MON14, "19:10")
    menu = week.say("/menu")
    card = week.tap(meal_tap(menu, "dinner"))["text"]
    assert TURKEY in card, card
    body_ = card.split("<b>Method</b>", 1)
    method = body_[1] if len(body_) > 1 else ""
    steps = [ln for ln in method.splitlines() if re.match(r"\d+\.", ln)]
    ing_block = card.split("<b>Ingredients</b>", 1)[1].split("<b>Method</b>", 1)[0]
    foods = [
        ln
        for ln in ing_block.splitlines()
        if ln.strip() and not re.match(r"(Salt|Black pepper|.* oil)\b", ln)
    ]
    kit = {
        k
        for k, words in {
            "air fryer": ("air fry", "air-fr"),
            "hob": ("hob", "pan fry"),
            "microwave": ("microwave",),
            "oven": ("oven", "roast"),
        }.items()
        if any(w in card.lower() for w in words)
    }
    assert len(foods) <= 6 and len(steps) <= 5 and len(kit) <= 2, (
        f"a weeknight dinner with {len(foods)} foods, {len(steps)} steps and "
        f"{len(kit)} appliances ({', '.join(sorted(kit))})"
    )


# ---------------------------------------------------------------- 12. Friday, as planned
def test_12_friday_evening_as_planned_records_the_number_the_check_in_named(week) -> None:
    """Friday 20:00, last box of the week eaten. The check-in names the plan; he taps As planned
    and wants the same number recorded, and /today to agree the day is closed."""
    msg = week.run(evening_confirm.JOB, FRI18)
    text = text_of(msg)
    assert "planned 1,915 kcal" in text, text
    assert len([ln for ln in text.splitlines() if ln.strip()]) <= 10, text
    week.at(FRI18, "20:02")
    said = week.tap("k:cfm:20260918")["text"]
    assert "Fri 18 Sep recorded: 1915 kcal" in said, said
    today = week.say("/today")["text"]
    assert "Day: recorded" in today, today
