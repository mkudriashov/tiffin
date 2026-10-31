"""Twelve dead ends the partner can walk into this week, swept across the commands and callbacks.

freezer. Her menu (Mon-Wed) still has "Chicken, chickpea and feta salad" in it - built before she
asked to avoid it, and menus are append-only, so the fix is a fresh `/cook replace`, never a tap
on a second copy sitting around: a menu is decided the moment it is built and sent (the owner,
Thu-Fri has no menu at all, and chickpeas are on her avoid-list. She has no Tailscale identity,
so the web page is not hers to open.

Each story is one message or one tap she could send, and asks whether what comes back gives her a
next step that actually works: a reply that names a command which then refuses, a button that
opens the wrong thing, a refusal that lists nothing.

Her messages are read off the fake Telegram rather than as `dropped_dev` rows, because the
question is often which buttons she was given: the fixture's outbox sends as prod would, to the
fake transport only.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import types

import pytest

from tiffin import body
from tiffin import targets as targets_mod
from tiffin.app import App
from tiffin.clock import TZ, FrozenClock, isoday
from tiffin.db import Db
from tiffin.food import menu as menu_mod
from tiffin.secrets import Secrets
from tiffin.telegram.handlers import HELP_GROUPS
from tiffin.telegram.poller import Poller

from .fakes import FakeTelegram
from .test_food_catalogue import add_profile
from .test_food_shopping import seed_ingredient
from .test_menu import FakeGateway

PARTNER_CHAT = 1000002
MONDAY = dt.date(2026, 9, 14)
TUESDAY = MONDAY + dt.timedelta(days=1)
WEDNESDAY = MONDAY + dt.timedelta(days=2)
THURSDAY = MONDAY + dt.timedelta(days=3)
FRIDAY = MONDAY + dt.timedelta(days=4)

CHICKPEA_SALAD = "Chicken, chickpea and feta salad"
FETA_SALAD = "Chicken, spinach and feta salad"
WRAP = "Tuna, avocado and sweetcorn wrap"
CURRY = "Chicken thigh and red lentil curry with rice"
PAGE = "https://tiffin.example.ts.net/tiffin"

#: code, name, aisle, pack_g, pack label
FOODS = (
    ("oats", "Porridge oats", "grains", 1000.0, "1 kg bag"),
    ("skyr", "Skyr, plain", "dairy", 450.0, "450 g pot"),
    ("chicken", "Chicken breast, raw", "meat", 500.0, "500 g pack"),
    ("chickpeas", "Chickpeas, tinned, drained", "tins", 240.0, "400 g tin"),
    ("feta", "Feta cheese", "dairy", 200.0, "200 g block"),
    ("spinach", "Spinach, baby", "produce", 200.0, "200 g bag"),
    ("tuna", "Tuna, tinned in spring water", "tins", 112.0, "145 g tin"),
    ("wrap", "Tortilla wraps", "bakery", 384.0, "6 wraps"),
    ("salmon", "Salmon fillets", "fish", 240.0, "2 fillets"),
    ("lentils", "Red lentils, dry", "grains", 500.0, "500 g bag"),
    ("rice", "Basmati rice, dry", "grains", 1000.0, "1 kg bag"),
    ("yoghurt", "Greek yoghurt 0%", "dairy", 500.0, "500 g pot"),
)

#: dish -> ingredient codes and grams, and a short method
DISHES = {
    "Porridge with skyr": ({"oats": 50, "skyr": 150}, ["Cook the oats.", "Stir in the skyr."]),
    CHICKPEA_SALAD: (
        {"chicken": 120, "chickpeas": 80, "feta": 30},
        ["Grill the chicken.", "Toss with the chickpeas and feta."],
    ),
    FETA_SALAD: (
        {"chicken": 120, "spinach": 60, "feta": 30},
        ["Grill the chicken.", "Toss with the spinach and feta."],
    ),
    WRAP: ({"tuna": 100, "wrap": 64}, ["Fill the wrap.", "Roll it."]),
    "Salmon and rice": ({"salmon": 120, "rice": 60}, ["Bake the salmon.", "Boil the rice."]),
    CURRY: (
        {"chicken": 130, "lentils": 50, "rice": 60},
        ["Brown the chicken.", "Simmer with the lentils.", "Serve on rice."],
    ),
    "Yoghurt pot": ({"yoghurt": 170}, ["Spoon into a bowl."]),
}


def add_dishes(db: Db) -> tuple[dict[str, int], dict[str, int]]:
    """(dish name -> recipe id, food code -> ingredient id)."""
    ids = {
        code: seed_ingredient(db, code, name, aisle, g, label)
        for code, name, aisle, g, label in FOODS
    }
    out: dict[str, int] = {}
    for name, (grams, steps) in DISHES.items():
        rid = db.insert(
            "recipes",
            name=name,
            servings=1,
            prep_minutes=20,
            method_json=json.dumps(steps),
            source="manual",
        )
        for code, g in grams.items():
            db.insert("recipe_ingredients", recipe_id=rid, ingredient_id=ids[code], grams=g)
        out[name] = rid
    return out, ids


def a_menu(db: Db, pid: int, days: dict[dt.date, dict[str, int]], *, accepted: bool) -> int:
    """A menu shaped the way `menus` really holds them: status stays 'proposed' for ever, and a
    yes lives in `menu_decisions`."""
    ordered = sorted(days)
    mid = db.insert(
        "menus",
        person_id=pid,
        segment_start=isoday(ordered[0]),
        segment_end=isoday(ordered[-1]),
        status="proposed",
    )
    for day in ordered:
        did = db.insert(
            "menu_days",
            menu_id=mid,
            day=isoday(day),
            kcal=1600,
            protein_g=120,
            fat_g=55,
            carb_g=150,
            fibre_g=25,
        )
        for slot, rid in days[day].items():
            db.insert(
                "meal_slots",
                menu_day_id=did,
                slot=slot,
                recipe_id=rid,
                portion_multiplier=1.0,
                portion_g=300,
                kcal=400,
                protein_g=30,
                fat_g=14,
                carb_g=38,
                fibre_g=6,
            )
    if accepted:
        menu_mod.decide(db, mid, "accepted")
    return mid


def buttons(messages: list[dict]) -> list[tuple[str, str]]:
    """(label, callback) for every button on these messages."""
    out = []
    for m in messages:
        for row in (m.get("reply_markup") or {}).get("inline_keyboard", []):
            out += [(b["text"], b["callback_data"]) for b in row]
    return out


def words(messages: list[dict]) -> str:
    return "\n".join(m["text"] for m in messages)


def visible(html: str) -> str:
    return re.sub(r"<[^>]+>", "", html)


def names_a_next_step(messages: list[dict]) -> bool:
    """A command she can send, or a button she can tap."""
    return bool(buttons(messages)) or bool(
        re.search(r"(?<![\w/])/[a-z]+", visible(words(messages)))
    )


@pytest.fixture
def sam(fixture_db: Db, two_persons, frozen_clock: FrozenClock, config, tmp_path):
    """Monday 14 Sep, 12:40: her menus, her avoid-list, her cadence, as the live box holds them."""
    db = fixture_db
    partner = two_persons["partner"]
    frozen_clock.set_local(dt.datetime(2026, 9, 14, 12, 40, tzinfo=TZ))
    add_profile(
        db,
        partner,
        effective_from="2026-09-12",
        sex="f",
        birth_date="1996-04-02",
        height_cm=165.0,
        activity_factor=1.375,
        kcal_floor=1200,
        meals_per_day=3,
        pre_bed_protein=1,
        segment_days=json.dumps([[0, 1, 2], [3, 4]]),
        batch_slots=json.dumps(["lunch", "dinner"]),
        freezes=0,
        refused_foods=json.dumps(["Liver", "broccoli", "chickpea"]),
        variety_json=json.dumps({"all": "rotate"}),
    )
    body.record_weigh_in(db, partner, dt.date(2026, 9, 12), 58.0, "manual")
    targets_mod.ensure_targets(db, partner, MONDAY, config, clock=frozen_clock)
    dish, food = add_dishes(db)

    def mon_wed(lunch_mon_wed: str) -> dict[dt.date, dict[str, int]]:
        return {
            MONDAY: {
                "breakfast": dish["Porridge with skyr"],
                "lunch": dish[lunch_mon_wed],
                "dinner": dish["Salmon and rice"],
                "pre_bed": dish["Yoghurt pot"],
            },
            TUESDAY: {
                "breakfast": dish["Porridge with skyr"],
                "lunch": dish[WRAP],
                "dinner": dish[CURRY],
                "pre_bed": dish["Yoghurt pot"],
            },
            WEDNESDAY: {
                "breakfast": dish["Porridge with skyr"],
                "lunch": dish[lunch_mon_wed],
                "dinner": dish["Salmon and rice"],
                "pre_bed": dish["Yoghurt pot"],
            },
        }

    old = a_menu(db, partner, mon_wed(CHICKPEA_SALAD), accepted=True)  # accepted yesterday

    secrets_dir = tmp_path / "s"
    secrets_dir.mkdir()
    (secrets_dir / "telegram-token-dev.txt").write_text("1:t", encoding="utf-8")
    tg = FakeTelegram()
    gateways: list[FakeGateway] = []
    app = App(
        config,
        Secrets(secrets_dir),
        clock=frozen_clock,
        transport=tg,
        gateway_factory=lambda _app, _db: gateways[0] if gateways else None,  # type: ignore[arg-type]
    )
    handlers = app.handlers(db)
    handlers.outbox.env = "prod"  # so her messages reach the FAKE transport and can be read
    poller = Poller(
        app.api(), db, app.registry(db), handlers.handle, clock=frozen_clock, sleeper=lambda s: None
    )
    n = {"i": 0}

    def say(text: str) -> list[dict]:
        n["i"] += 1
        before = len(tg.sent)
        tg.queue_update(tg.text_update(n["i"], PARTNER_CHAT, text))
        poller.run_once()
        return [m for m in tg.sent[before:] if m["chat_id"] == PARTNER_CHAT]

    def tap(data: str) -> tuple[list[dict], list[str]]:
        n["i"] += 1
        before, toasts = len(tg.sent), len(tg.answered)
        tg.queue_update(
            tg.callback_update(n["i"], PARTNER_CHAT, data, message_id=n["i"], query_id=f"q{n['i']}")
        )
        poller.run_once()
        said = [m for m in tg.sent[before:] if m["chat_id"] == PARTNER_CHAT]
        return said, [str(a.get("text") or "") for a in tg.answered[toasts:]]

    def thu_fri() -> int:
        """The Thu-Fri menu her real week does not have yet: for the stories that need one."""
        return a_menu(
            db,
            partner,
            {
                THURSDAY: {"breakfast": dish["Porridge with skyr"], "lunch": dish[WRAP]},
                FRIDAY: {"breakfast": dish["Porridge with skyr"], "lunch": dish[CURRY]},
            },
            accepted=True,
        )

    return types.SimpleNamespace(
        db=db,
        partner=partner,
        tg=tg,
        app=app,
        handlers=handlers,
        clock=frozen_clock,
        say=say,
        tap=tap,
        old=old,
        mon_wed=mon_wed,
        dish=dish,
        food=food,
        thu_fri=thu_fri,
        gateways=gateways,
    )


# ---------------------------------------------------------------- 1. /recipe thursday lunch
def test_01_partner_asks_for_thursday_lunch_by_name_and_gets_the_recipe(sam) -> None:
    """Monday, 12:40, at her desk: she types what she wants in her own words, `/recipe thursday
    lunch`, to see what Thursday's lunch is and how to make it."""
    sam.thu_fri()
    said = words(sam.say("/recipe thursday lunch"))
    assert WRAP in said and "Method" in said, (
        f"asked for Thursday's lunch by day and meal, got: {said}"
    )


# ---------------------------------------------------------------- 2. /menu thursday lunch
def test_02_menu_thursday_lunch_answers_with_thursday(sam) -> None:
    """Monday, 12:40: `/menu thu` is what the bot teaches, so she tries `/menu thursday lunch` -
    the exact thing she said she cannot find. The reply should be Thursday, not a lecture on date
    formats."""
    sam.thu_fri()
    said = words(sam.say("/menu thursday lunch"))
    assert "did not read" not in said, f"a day and a meal were refused as not a day: {said}"
    assert "Thu 17 Sep" in said and WRAP in said, said


# ---------------------------------------------------------------- 3. "/cook builds one now"
def test_03_told_cook_builds_thursday_cook_does_not_refuse(sam) -> None:
    """Monday: she asks `/menu thu`, is told there is no Thursday menu and that "/cook builds one
    now", and sends /cook. What comes back has to be about Thursday, not a refusal about a menu she
    already has for Tuesday."""
    shown = words(sam.say("/menu thu"))
    assert "/cook builds one" in shown, f"the advice this story follows has moved: {shown}"
    # this is the only story in the file that drives a real /cook build, and the fixture never
    # wired a model gateway for one (every other story reads a menu inserted straight into the
    # db) - without it /cook cannot reach a model at all, which is a setup gap, not the bug
    # under test. An unusable answer still falls back to her last accepted menu.
    sam.gateways.append(FakeGateway([]))
    said = words(sam.say("/cook"))
    assert "already covers" not in said and "Thu" in said, (
        f"/menu thu sent her to /cook, and /cook answered: {said}"
    )


# ---------------------------------------------------------------- 4. the fix is /cook replace
def test_04_menu_with_chickpeas_says_cook_replace_rebuilds_it(sam) -> None:
    said = words(sam.say("/menu"))
    assert CHICKPEA_SALAD in said and "which you now avoid" in said, f"premise: {said}"
    assert "/cook replace" in said, f"the fix is /cook replace, and it is not mentioned: {said}"
    assert "/proposals" not in said, "nothing is left waiting for a tap"


# ---------------------------------------------------------------- 5. yesterday's Lunch button
def test_05_yesterdays_lunch_button_does_not_open_the_replaced_dish(sam) -> None:
    """Monday evening, `/cook replace` has built a chickpea-free menu for the same days - in
    up to yesterday's menu message and taps Lunch. It must not hand her the chickpea salad as if
    it were still the plan.
    """
    a_menu(sam.db, sam.partner, sam.mon_wed(FETA_SALAD), accepted=True)
    card, toasts = sam.tap(f"k:meal:{sam.old}:lunch")
    said = words(card) + " ".join(toasts)
    assert CHICKPEA_SALAD not in said or re.search(r"replaced|no longer|older", said), (
        f"an old button opened the dish she asked three times not to get: {said}"
    )


# ---------------------------------------------------------------- 6. this morning's Wed button
def test_06_a_day_button_from_this_mornings_menu_shows_the_menu_partner_accepted(sam) -> None:
    """Monday, 18:30: at lunch she sent /menu, and since then `/cook replace` has built a
    chickpea-free menu for the same days - already in force, not merely accepted (the owner,
    should see the menu now governing her days, not the one it replaced.
    """
    shown = sam.say("/menu")
    wed = next((d for label, d in buttons(shown) if label.startswith("Wed")), None)
    assert wed is not None, f"premise: no Wednesday button on /menu: {buttons(shown)}"
    a_menu(sam.db, sam.partner, sam.mon_wed(FETA_SALAD), accepted=True)
    sam.clock.set_local(dt.datetime(2026, 9, 14, 18, 30, tzinfo=TZ))
    day, toasts = sam.tap(wed)
    said = words(day) + " ".join(toasts)
    assert FETA_SALAD in said or re.search(r"replaced|no longer|older", said), (
        f"the Wednesday button on a menu she has replaced showed: {said}"
    )


# ---------------------------------------------------------------- 7. /shopping out chickpeas
def test_07_the_shelf_empty_hint_works_for_the_food_it_names(sam) -> None:
    """Monday, in the shop, before she has accepted anything: the list shows chickpeas and ends
    "Shelf empty? /shopping out chickpeas". She sends exactly that. The command the list itself
    teaches must not answer that there is no such food on her list."""
    listed = words(sam.say("/shopping"))
    assert "Chickpeas" in listed and "/shopping out chickpeas" in visible(listed), (
        f"premise: {listed}"
    )
    said = words(sam.say("/shopping out chickpeas"))
    assert "No food on your list" not in said, (
        f"the list names chickpeas and the command it teaches says there are none: {said}"
    )


# ---------------------------------------------------------------- 8. the list and the menu disagree
def test_08_a_shopping_list_carrying_an_avoided_food_says_so(sam) -> None:
    """Monday, in the shop: `/menu` warns that the menu still has chickpeas she now avoids. The
    shopping list built from that same menu should carry the same warning and the same way out,
    rather than quietly listing the tin."""
    menu = words(sam.say("/menu"))
    assert "which you now avoid" in menu, f"premise: /menu no longer flags it: {menu}"
    listed = words(sam.say("/shopping"))
    assert "Chickpeas" in listed, f"premise: {listed}"
    assert "avoid" in listed, (
        f"/menu flags chickpeas, the list for the same menu says nothing: {listed}"
    )


# ---------------------------------------------------------------- 9. declined, then wanted


# ---------------------------------------------------------------- 10. the rest tick on the page
def test_10_a_long_shopping_list_does_not_send_partner_to_a_page_partner_cannot_open(sam) -> None:
    """Monday, 17:50, in the shop with a fourteen-item list: the list says the items past the
    twelfth button "tick on the page". She has no Tailscale identity, so that page refuses her.
    Every item she is shopping for has to be tickable somewhere she can reach."""
    extra = (
        ("berries", "Blueberries", "produce", 150.0, "150 g punnet"),
        ("honey", "Honey, runny", "baking", 340.0, "340 g jar"),
        ("milk", "Milk, semi-skimmed", "dairy", 1136.0, "2 pint bottle"),
    )
    for code, name, aisle, g, label in extra:
        iid = seed_ingredient(sam.db, code, name, aisle, g, label)
        sam.db.insert(
            "recipe_ingredients",
            recipe_id=sam.dish["Porridge with skyr"],
            ingredient_id=iid,
            grams=40,
        )
    partner = sam.handlers.registry.by_slug("partner")
    assert partner is not None and not partner.tailscale_login, "premise: she has no web identity"
    sam.clock.set_local(dt.datetime(2026, 9, 14, 17, 50, tzinfo=TZ))
    listed = sam.say("/shopping")
    items = [line for line in visible(words(listed)).splitlines() if re.match(r"^[-x] ", line)]
    assert len(items) > 12, f"premise: the list is not longer than the buttons: {words(listed)}"
    tickable = len(buttons(listed))
    sent_to_page = PAGE in words(listed)
    assert tickable >= len(items) or not sent_to_page, (
        f"{len(items)} items, {tickable} buttons, and the rest are sent to {PAGE}, which "
        f"answers 403 to someone with no Tailscale login: {words(listed)[-200:]}"
    )


# ---------------------------------------------------------------- 11. every command /help names
def test_11_every_command_help_names_answers_and_names_only_real_commands(sam) -> None:
    """Monday evening, curious: she works down /help sending each command bare. Every one should
    answer, and every /command a reply names should be one the bot actually knows."""
    names = ["help", "start", *(name for _, items in HELP_GROUPS for name, _ in items)]
    silent, unknown = [], {}
    for name in names:
        said = sam.say(f"/{name}")
        if not said:
            silent.append(name)
            continue
        for ref in re.findall(r"(?<![\w/])/([a-z][a-z_]*)", visible(words(said))):
            if ref not in sam.handlers.commands:
                unknown.setdefault(name, []).append(ref)
    assert not silent, f"these answered nothing: {silent}"
    assert not unknown, f"replies naming commands that do not exist: {unknown}"


# ---------------------------------------------------------------- 12. old buttons never go silent
def test_12_a_button_from_an_old_message_never_answers_with_silence(sam) -> None:
    """Any evening: she taps a button from a message weeks old - a menu since replaced, a list
    since superseded, a card from before a restart. Every callback kind the bot registers must say
    something, a message or a toast with words in it."""
    silent = []
    for kind in sorted(sam.handlers.callbacks):
        said, toasts = sam.tap(f"k:{kind}:999999:20200101:all")
        if not said and not any(t.strip() for t in toasts):
            silent.append(kind)
    assert not silent, f"old taps on these kinds answer with nothing: {silent}"
