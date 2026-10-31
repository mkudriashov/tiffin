"""

The house as it really is on Monday morning, rebuilt on fixtures:

- the partner cooks Monday-Wednesday and Thursday-Friday, batch cooks lunch and dinner, never
freezes.
- Her Mon-Wed menu (live id 5) is in force and still has "Chicken, chickpea and feta salad" on
  Monday and Wednesday. Thursday-Friday has no menu at all. Chickpeas are on her avoid list, and
  the moment it is built and sent (the owner: "why is there this bullshit with Accept - I thought it
  just gets created and that's it"), so the replacement she gets on Monday is hers on arrival.
- the owner has a repeated Mon-Fri menu (live id 6) and freezes; he is here so that nothing of
his leaks.

What they said, which is the brief: recipes are too complex ("e.g. the partner's eggs with bacon
- we
want simply but healthy"); going per day and then per meal to reach a recipe is too much; and in a
shop the list and the menu disagreed and she got chickpeas after asking three times.

Every story drives the real path - the Poller into the handlers, or a scheduled job through the
real Scheduler - and asserts on what reached her phone. Development drops everything addressed to
her, so the Outbox is built as prod here: that is test wiring, and it changes nothing that is
composed.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import re
import types
from typing import Any

import pytest

from tiffin import body
from tiffin import scheduler as sc
from tiffin import targets as targets_mod
from tiffin.app import App
from tiffin.clock import TZ, FrozenClock, isoday
from tiffin.db import Db
from tiffin.food import catalogue, shopping
from tiffin.food import menu as menu_mod
from tiffin.jobs import evening_confirm, menu_propose, morning_targets, shop_refresh, shopping_list
from tiffin.secrets import Secrets
from tiffin.telegram import outbox as outbox_mod
from tiffin.telegram.poller import Poller

from .fakes import FakeTelegram
from .test_food_shopping import seed_ingredient
from .test_menu import FakeGateway, proposal_for, seed_catalogue

OWNER_CHAT = 1000001
PARTNER_CHAT = 1000002

SUN = dt.date(2026, 9, 13)
MON = dt.date(2026, 9, 14)
TUE = dt.date(2026, 9, 15)
WED = dt.date(2026, 9, 16)
THU = dt.date(2026, 9, 17)
FRI = dt.date(2026, 9, 18)

CHICKPEA_SALAD = "Chicken, chickpea and feta salad"
NEW_LUNCH = "Chicken, kidney bean and pepper wrap"
EGGS_BACON = "Scrambled eggs, bacon and spinach on toast"

#: her real Tuesday breakfast from the live menu (recipe 95): seven ingredients, six steps, the
#: air fryer and a pan. It is the dish the brief names as too complex.
EGGS_BACON_METHOD = [
    "Air fry the bacon at 180C for 7 to 8 minutes until cooked.",
    "Add the bread to the air fryer for the last 3 minutes to toast.",
    "Meanwhile beat the eggs and cook in a non-stick pan on a low hob, stirring, for 2 to 3 "
    "minutes.",
    "Add the spinach to the pan for the last minute so it wilts.",
    "Halve the cherry tomatoes.",
    "Serve the eggs and bacon on the toast, with the cottage cheese and tomatoes on the side.",
]

#: what "simple" means to somebody making breakfast before work: a handful of things, a
#: handful of steps. Her eggs-and-bacon is 7 and 6 and is the brief's own example of too much.
SIMPLE_BREAKFAST_INGREDIENTS = 5
SIMPLE_BREAKFAST_STEPS = 4


# ---------------------------------------------------------------- the house on Monday morning
def _ing(db: Db, code: str) -> int:
    found = db.scalar(
        "SELECT id FROM ingredients WHERE code = ? ORDER BY version DESC, id DESC LIMIT 1", (code,)
    )
    assert found is not None, code
    return int(found)


def _dish(db: Db, name: str, grams: dict[str, float], method: list[str] | None = None) -> int:
    rid = db.insert(
        "recipes",
        name=name,
        servings=1,
        prep_minutes=20,
        method_json=json.dumps(method or ["Cook it.", "Box it."]),
        source="model",
    )
    for code, g in grams.items():
        db.insert("recipe_ingredients", recipe_id=rid, ingredient_id=_ing(db, code), grams=g)
    return rid


def _put_menu(
    db: Db,
    pid: int,
    days: dict[dt.date, dict[str, int]],
    *,
    status: str = "proposed",
    accepted: bool = False,
    kcal: int = 1600,
) -> int:
    """A stored menu, day by day, in exactly the tables `store_plan` writes."""
    ordered = sorted(days)
    mid = db.insert(
        "menus",
        person_id=pid,
        segment_start=isoday(ordered[0]),
        segment_end=isoday(ordered[-1]),
        status=status,
        model_used="claude-opus-5",
    )
    for day in ordered:
        did = db.insert(
            "menu_days",
            menu_id=mid,
            day=isoday(day),
            kcal=kcal,
            protein_g=130,
            fat_g=50,
            carb_g=160,
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
                kcal=kcal // 4,
                protein_g=32,
                fat_g=12,
                carb_g=40,
                fibre_g=6,
            )
    if accepted:
        menu_mod.decide(db, mid, "accepted")
    return mid


def _profile(db: Db, pid: int, **over: Any) -> None:
    row: dict[str, Any] = {
        "person_id": pid,
        "effective_from": "2026-09-01",
        "sex": "m",
        "birth_date": "1990-03-15",
        "height_cm": 183.0,
        "activity_factor": 1.5,
        "kcal_floor": 1400,
        "meals_per_day": 4,
        "segment_days": json.dumps([[0, 1, 2, 3, 4]]),
        "variety_json": json.dumps({"all": "same"}),
        "refused_foods": json.dumps([]),
        "batch_slots": json.dumps(["lunch", "dinner"]),
        "freezes": 1,
        "confirm_time": "20:00",
        "morning_time": "06:30",
        "quiet_start": "22:00",
        "quiet_end": "06:00",
    }
    row.update(over)
    db.insert("profiles", **row)
    db.insert(
        "targets",
        person_id=pid,
        week_start=isoday(MON),
        kcal=2250,
        protein_g=200,
        fat_g=70,
        carb_g=200,
        fibre_g=30,
        source="formula",
    )


@pytest.fixture
def week(
    fixture_db: Db,
    two_persons,
    frozen_clock: FrozenClock,
    config,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    db = fixture_db
    his, partner = two_persons["owner"], two_persons["partner"]
    frozen_clock.set_local(dt.datetime(2026, 9, 13, 17, 0, tzinfo=TZ))
    _profile(db, his)
    _profile(
        db,
        partner,
        sex="f",
        birth_date="1993-06-01",
        height_cm=168.0,
        segment_days=json.dumps([[0, 1, 2], [3, 4]]),
        variety_json=json.dumps({"all": "rotate"}),
        refused_foods=json.dumps(["chickpea"]),
        freezes=0,
        confirm_time="21:00",
        morning_time="08:00",
        quiet_end="07:30",
    )
    db.insert("consents", person_id=partner, granted=1, wording_version=1)
    body.record_weigh_in(db, his, dt.date(2026, 9, 12), 100.5, "manual")
    body.record_weigh_in(db, partner, dt.date(2026, 9, 12), 66.0, "manual")

    # the catalogue: the menu tests' eight, then her breakfast foods. "zz_" keeps the eight at
    # prompt indexes 1-8, so the fake model's stock answer (`proposal_for`) means what it says.
    seed_catalogue(db)
    for code, name, aisle, pack, label, kcal, p, f, c, fib in (
        ("zz_bacon", "Back bacon, raw", "meat", 300, "300 g pack", 215, 16.5, 16.5, 0.0, 0.0),
        ("zz_bread", "Wholemeal bread", "bakery", 800, "800 g loaf", 217, 9.4, 2.5, 37.8, 7.0),
        ("zz_chickpeas", "Chickpeas, tinned", "tins", 240, "400 g tin", 115, 7.0, 2.5, 15.0, 5.0),
        ("zz_cottage", "Cottage cheese", "dairy", 300, "300 g tub", 98, 11.0, 4.3, 3.4, 0.0),
        ("zz_eggs", "Eggs, medium", "dairy", 348, "6 eggs", 131, 12.6, 9.0, 0.0, 0.0),
        ("zz_spinach", "Spinach, baby", "produce", 240, "240 g bag", 25, 2.8, 0.8, 1.6, 2.1),
        ("zz_tomato", "Cherry tomatoes", "produce", 250, "250 g punnet", 20, 0.7, 0.3, 3.0, 1.2),
    ):
        seed_ingredient(
            db, code, name, aisle, float(pack), label,
            kcal_100=float(kcal), protein_100=p, fat_100=f, carb_100=c, fibre_100=fib,
        )  # fmt: skip

    r = {
        "porridge": _dish(
            db, "Porridge with skyr and raspberries", {"oats": 50, "milk": 150, "yoghurt": 150}
        ),
        "salad": _dish(
            db,
            CHICKPEA_SALAD,
            {"chicken": 150, "zz_chickpeas": 80, "zz_tomato": 80, "oil": 10, "zz_spinach": 40},
            ["Air fry the chicken at 200C for 16 minutes.", "Drain the chickpeas.", "Toss."],
        ),
        "eggs_bacon": _dish(
            db,
            EGGS_BACON,
            {
                "zz_eggs": 116,
                "zz_bacon": 60,
                "zz_bread": 70,
                "zz_spinach": 50,
                "zz_tomato": 80,
                "zz_cottage": 100,
                "milk": 20,
            },
            EGGS_BACON_METHOD,
        ),
        "tuna": _dish(db, "Tuna, avocado and sweetcorn wrap", {"chicken": 120, "rice": 60}),
        "curry": _dish(
            db, "Chicken thigh and red lentil curry with rice", {"chicken": 180, "rice": 80}
        ),
        "chilli": _dish(
            db, "Beef and kidney bean chilli with brown rice", {"chicken": 160, "rice": 80}
        ),
        "yog": _dish(db, "Greek yoghurt with strawberries", {"yoghurt": 200}),
        "wrap": _dish(db, NEW_LUNCH, {"chicken": 120, "rice": 50}),
        "eggs2": _dish(
            db,
            "Scrambled eggs on wholemeal toast with spinach",
            {"zz_eggs": 174, "zz_bread": 70, "zz_spinach": 50},
        ),
        "soup": _dish(
            db, "Curried chicken, red lentil and carrot soup", {"chicken": 80, "rice": 50}
        ),
        "traybake": _dish(db, "Garlic chicken thigh traybake", {"chicken": 150, "rice": 60}),
        "m_oats": _dish(db, "Oats with whey", {"oats": 80, "whey": 30}),
        "m_rice": _dish(db, "Chicken, rice and broccoli", {"chicken": 220, "rice": 90}),
    }
    # her accepted Mon-Wed (live id 5): the chickpea salad on Monday and Wednesday
    partner_old = _put_menu(
        db,
        partner,
        {
            MON: {"breakfast": r["porridge"], "lunch": r["salad"], "dinner": r["curry"],
                  "snack": r["yog"]},
            TUE: {"breakfast": r["eggs_bacon"], "lunch": r["tuna"], "dinner": r["curry"],
                  "snack": r["yog"]},
            WED: {"breakfast": r["porridge"], "lunch": r["salad"], "dinner": r["chilli"],
                  "snack": r["yog"]},
        },
        accepted=True,
        kcal=1600,
    )  # fmt: skip
    # his Mon-Fri repeat (live id 6)
    his_menu = _put_menu(
        db,
        his,
        {
            MON + dt.timedelta(days=i): {"breakfast": r["m_oats"], "lunch": r["m_rice"],
                                         "dinner": r["m_rice"], "snack": r["yog"]}
            for i in range(5)
        },
        status="repeated",
        kcal=2250,
    )  # fmt: skip
    # Sunday 17:00, both segment lists are built from the menus then in force
    shopping.build(db, partner_old, clock=frozen_clock)
    shopping.build(db, his_menu, clock=frozen_clock)
    # No Sunday-evening replacement sits here waiting to be tapped: a menu is decided the moment
    # it is built and sent, so an undecided menu is not a state the live system can be in. The
    # replacement is built inside the story that asks for it (`_replace_partner_menu`), on Monday.
    frozen_clock.set_local(dt.datetime(2026, 9, 14, 8, 0, tzinfo=TZ))

    # --- the box, wired to fakes. Her messages have to be SEEN, so every Outbox is prod.
    real_init = outbox_mod.Outbox.__init__

    def prod_outbox(self, api, db_, **kw):
        kw["env"] = "prod"
        real_init(self, api, db_, **kw)

    monkeypatch.setattr(outbox_mod.Outbox, "__init__", prod_outbox)
    # the list tops up prices through a real browser first; there is none in a test
    monkeypatch.setattr(shop_refresh, "fill_for_menus", lambda *a, **k: "")

    secrets_dir = tmp_path / "s"
    secrets_dir.mkdir()
    (secrets_dir / "telegram-token-dev.txt").write_text("1:t", encoding="utf-8")
    config.paths.ensure()
    tg = FakeTelegram()
    gateways: list[FakeGateway] = []
    app = App(
        config,
        Secrets(secrets_dir),
        clock=frozen_clock,
        transport=tg,
        gateway_factory=lambda _a, _d: gateways[0] if gateways else None,  # type: ignore[arg-type]
    )
    handlers = app.handlers(db)
    poller = Poller(
        app.api(), db, app.registry(db), handlers.handle, clock=frozen_clock, sleeper=lambda s: None
    )
    n = {"i": 0}

    def to(chat: int, since: int = 0) -> list[dict[str, Any]]:
        return [m for m in tg.sent[since:] if int(m["chat_id"]) == chat]

    def say(chat: int, text: str) -> list[dict[str, Any]]:
        before = len(tg.sent)
        n["i"] += 1
        tg.queue_update(tg.text_update(n["i"], chat, text))
        poller.run_once()
        return to(chat, before)

    def tap(chat: int, data: str) -> list[dict[str, Any]]:
        before = len(tg.sent)
        n["i"] += 1
        tg.queue_update(tg.callback_update(n["i"], chat, data, message_id=n["i"] + 1000))
        poller.run_once()
        return to(chat, before)

    def at(day: dt.date, hhmm: str) -> None:
        h, m = (int(x) for x in hhmm.split(":"))
        frozen_clock.set_local(dt.datetime(day.year, day.month, day.day, h, m, tzinfo=TZ))

    def run(job: sc.Job, day: dt.date, person_id: int) -> str:
        prof = targets_mod.profile_on(db, person_id, day)
        slots = sc.slots_for(job, person_id, prof, day, db)
        assert slots, f"{job.name} has no slot on {day}"
        return sc.Scheduler(app, [job], lambda _p, _fn: None).execute(db, slots[0])

    return types.SimpleNamespace(
        db=db,
        tg=tg,
        gateways=gateways,
        his=his,
        partner=partner,
        r=r,
        partner_old=partner_old,
        his_menu=his_menu,
        say=say,
        tap=tap,
        at=at,
        run=run,
        to=to,
    )


# ---------------------------------------------------------------- reading her phone
def lines_of(msg: dict[str, Any]) -> list[str]:
    return [ln for ln in str(msg["text"]).splitlines() if ln.strip()]


def buttons(msg: dict[str, Any]) -> list[tuple[str, str]]:
    kb = msg.get("reply_markup") or {}
    if isinstance(kb, str):
        kb = json.loads(kb)
    return [
        (str(b.get("text")), str(b.get("callback_data", "")))
        for row in kb.get("inline_keyboard", [])
        for b in row
    ]


def button(msg: dict[str, Any], label: str) -> str:
    found = [d for t, d in buttons(msg) if t == label]
    assert found, f"no {label!r} button on {msg['text'][:80]!r}; buttons {buttons(msg)}"
    return found[0]


def last(msgs: list[dict[str, Any]]) -> dict[str, Any]:
    assert msgs, "nothing reached her phone"
    return msgs[-1]


def said(msgs: list[dict[str, Any]]) -> str:
    return "\n---\n".join(str(m["text"]) for m in msgs)


def is_card(text: str, name: str) -> bool:
    return "<b>Ingredients</b>" in text and "<b>Method</b>" in text and name in text


def card_parts(text: str) -> tuple[list[str], list[str]]:
    """(ingredient lines, method steps) as a person reads them off the card."""
    rows = text.splitlines()
    i, j = rows.index("<b>Ingredients</b>"), rows.index("<b>Method</b>")
    steps = [ln for ln in rows[j + 1 :] if re.match(r"\d+\. ", ln)]
    return rows[i + 1 : j], steps


def _with_breakfast(segment: menu_mod.Segment, recipe: dict[str, Any]) -> dict[str, Any]:
    """The fake model's stock answer with this breakfast on every day."""
    payload = proposal_for(segment)
    for day in payload["days"]:
        for slot in day["slots"]:
            if slot["slot"] == "breakfast":
                slot["recipe"] = copy.deepcopy(recipe)
    return payload


def _newest_menu(db: Db, pid: int) -> int:
    return int(db.scalar("SELECT MAX(id) FROM menus WHERE person_id = ?", (pid,)))


def _with_lunch(segment: menu_mod.Segment, name: str) -> dict[str, Any]:
    """The fake model's stock answer with this name on every lunch."""
    payload = proposal_for(segment)
    for day in payload["days"]:
        for slot in day["slots"]:
            if slot["slot"] == "lunch":
                slot["recipe"] = {**copy.deepcopy(slot["recipe"]), "name": name}
    return payload


def _replace_partner_menu(w: Any) -> int:
    """Build and send the partner's replacement menu immediately."""
    w.gateways.append(FakeGateway([_with_lunch(menu_mod.Segment(TUE, WED), NEW_LUNCH)]))
    w.say(PARTNER_CHAT, "/cook replace")
    built = _newest_menu(w.db, w.partner)
    assert menu_mod.decision_of(w.db, built) == "accepted", (
        f"a menu she asked for is not in force without a tap: {menu_mod.decision_of(w.db, built)}"
    )
    return built


# ---------------------------------------------------------------- Monday
def test_01_on_the_bus_from_the_brief_to_todays_lunch_recipe(week) -> None:
    """
    to make it - one command and one tap, not a hunt through days and meals."""
    w = week
    w.at(MON, "08:00")
    before = len(w.tg.sent)
    assert w.run(morning_targets.JOB, MON, w.partner) == "ok"
    brief = last(w.to(PARTNER_CHAT, before))
    # two lines since 26 Sep; her page login is not linked, so today's meals are buttons on
    # the brief itself - lunch is one tap, no command at all
    assert len(lines_of(brief)) <= 2, brief["text"]
    assert "First up:" in brief["text"] and "avoid list" in brief["text"], brief["text"]
    card = last(w.tap(PARTNER_CHAT, button(brief, "Lunch")))
    assert is_card(card["text"], CHICKPEA_SALAD), card["text"]


def test_02_at_partner_desk_at_lunchtime_recipe_lunch_opens_lunch(week) -> None:
    """Monday 12:40, at her desk. /help says /recipe is 'one dish in full'. She sends /recipe,
    and then /recipe lunch, wanting the lunch she is about to eat."""
    w = week
    w.at(MON, "12:40")
    bare = last(w.say(PARTNER_CHAT, "/recipe"))["text"]
    named = last(w.say(PARTNER_CHAT, "/recipe lunch"))["text"]
    assert is_card(bare, CHICKPEA_SALAD) or is_card(named, CHICKPEA_SALAD), (
        "at 12:40 neither /recipe nor /recipe lunch opens today's lunch.\n"
        f"/recipe answered: {bare.splitlines()[0]!r}\n/recipe lunch answered: {named!r}"
    )


def test_03_the_lunch_card_partner_cooks_from_says_it_has_the_chickpeas_partner_avoids(
    week,
) -> None:
    """Monday 12:45. She opens today's lunch card from /menu to see what to make tonight. The
    card is the thing she reads at the counter, and it is where the chickpeas are listed."""
    w = week
    w.at(MON, "12:45")
    day = last(w.say(PARTNER_CHAT, "/menu"))
    assert "which you now avoid" in day["text"], "setup: /menu itself warns"
    card = last(w.tap(PARTNER_CHAT, button(day, "Lunch")))["text"]
    assert "Chickpeas" in card, card
    low = card.lower()
    assert "avoid" in low or "refuse" in low, (
        "/menu one tap earlier said the menu has chickpeas she avoids; the recipe card she "
        f"cooks from lists them with no word at all:\n{card}"
    )


def test_04_the_menu_partner_asks_for_is_partners_the_moment_it_is_built(week) -> None:
    """Monday 12:50. The lunch card she has just read has the chickpeas she asked three times to
    avoid, so she sends /cook replace. One move, not three: no /proposals to open, no Accept to
    find, and /menu answers with the new lunch as soon as she asks."""
    w = week
    w.at(MON, "12:50")
    before = len(w.tg.sent)
    built = _replace_partner_menu(w)
    arrived = last(w.to(PARTNER_CHAT, before))
    assert not [d for _t, d in buttons(arrived) if d == f"k:menu:{built}"], (
        f"a menu already in force still offers a yes: {buttons(arrived)}"
    )
    assert "Tue to Wed" in arrived["text"], arrived["text"]
    assert NEW_LUNCH in arrived["text"] and "chickpea" not in arrived["text"].lower(), (
        f"she asked for a menu without the chickpeas and it arrived with them:\n{arrived['text']}"
    )
    w.at(TUE, "07:30")
    day = last(w.say(PARTNER_CHAT, "/menu"))["text"]
    assert f"Lunch: {NEW_LUNCH}" in day and "chickpea" not in day.lower(), day


def test_05_partner_monday_evening_cook_adds_up_the_menu_partner_is_cooking(week) -> None:
    """Monday 18:30, in the kitchen. She sends /batch for how much to cook for the week's boxes.
    She does not freeze - and the cook she is about to do is still the Mon-Wed menu with the
    chickpeas in it, so it has to say so where she can see it, in the amounts she is weighing
    out, not only in /menu three commands away."""
    w = week
    w.at(MON, "18:30")
    text = last(w.say(PARTNER_CHAT, "/batch"))["text"]
    assert "<b>Batch cook Mon 14 Sep</b>" in text, text
    assert "Chickpeas" in text and "you now avoid" in text, (
        f"the cook puts the refused food in her boxes and does not say so:\n{text}"
    )
    assert "<b>Freeze</b>" not in text, (
        f"she does not freeze and nothing outlives the fridge:\n{text}"
    )


# ---------------------------------------------------------------- Tuesday
def test_06_tuesday_check_in_asks_about_the_menu_partner_asked_for(week) -> None:
    """Tuesday 21:00, on the sofa. She switched to the new menu on Monday. The check-in asks
    whether she ate as planned, and 'planned' has to be the menu she is actually eating."""
    w = week
    w.at(MON, "12:50")
    built = _replace_partner_menu(w)
    planned = int(
        w.db.scalar(
            "SELECT kcal FROM menu_days WHERE menu_id = ? AND day = ?", (built, isoday(TUE))
        )
    )
    w.at(TUE, "21:00")
    before = len(w.tg.sent)
    assert w.run(evening_confirm.JOB, TUE, w.partner) == "ok"
    check_in = last(w.to(PARTNER_CHAT, before))
    assert len(lines_of(check_in)) <= 10
    assert f"planned {planned:,} kcal" in check_in["text"], check_in["text"]
    w.tap(PARTNER_CHAT, "k:cfm:20260915")
    kcal = w.db.scalar(
        "SELECT kcal FROM day_confirmations WHERE person_id = ? AND day = ? ORDER BY id DESC "
        "LIMIT 1",
        (w.partner, isoday(TUE)),
    )
    assert kcal == planned, kcal


def test_07_partner_marks_the_fussy_eggs_and_bacon_down_and_thursdays_menu_is_told(week) -> None:
    """Tuesday 09:00, after making Tuesday's breakfast: eggs, bacon, spinach, toast, tomatoes,
    cottage cheese. She opens the card, taps 1, and expects not to be handed it again."""
    w = week
    w.at(TUE, "09:00")
    day = last(w.say(PARTNER_CHAT, "/menu"))
    assert "Menu Tue 15 Sep" in day["text"], day["text"]
    card = last(w.tap(PARTNER_CHAT, button(day, "Breakfast")))
    assert is_card(card["text"], EGGS_BACON), card["text"]
    reply = said(w.tap(PARTNER_CHAT, button(card, "1")))
    assert "stop proposing" in reply, reply

    w.at(TUE, "18:00")
    gw = FakeGateway([proposal_for(menu_mod.Segment(THU, FRI))])
    w.gateways.append(gw)
    assert w.run(menu_propose.JOB, TUE, w.partner) == "ok"
    blocked = next(ln for ln in gw.calls[0].splitlines() if ln.startswith("Blocked"))
    assert EGGS_BACON in blocked, blocked


def test_08_tuesday_evening_partner_thursday_friday_menu_arrives_without_chickpeas(week) -> None:
    """Tuesday 18:00. Nothing covers Thursday and Friday yet and this is when Tiffin
    builds them. She wants a menu she can cook from at once, without the food she refused."""
    w = week
    w.at(TUE, "18:00")
    gw = FakeGateway([proposal_for(menu_mod.Segment(THU, FRI))])
    w.gateways.append(gw)
    before = len(w.tg.sent)
    assert w.run(menu_propose.JOB, TUE, w.partner) == "ok"
    offered = [ln for ln in gw.calls[0].splitlines() if re.match(r"\s*\d+\.", ln)]
    assert offered, "setup: the prompt numbers its ingredients"
    assert not [ln for ln in offered if "chickpea" in ln.lower()], offered
    msg = last(w.to(PARTNER_CHAT, before))
    assert "Thu to Fri" in msg["text"], msg["text"]
    # it is her menu already; the only tap it offers is the one that builds a different one
    newest = _newest_menu(w.db, w.partner)
    assert ("New menu", f"k:menux:{newest}") in buttons(msg), buttons(msg)
    assert ("Swap a dish", f"k:swappick:{newest}") in buttons(msg), buttons(msg)
    assert not [d for _t, d in buttons(msg) if d == f"k:menu:{newest}"], buttons(msg)
    assert menu_mod.decision_of(w.db, newest) == "accepted", "in force on arrival"
    assert len(lines_of(msg)) <= 10


def test_09_thursdays_breakfast_is_simple_enough_to_make_before_work(week) -> None:
    """Tuesday 18:00 the Thu-Fri menu is built and is hers at once; Thursday 07:15 she opens
    breakfast. Their words: 'Recipes are too complex (e.g. the partner's eggs with bacon) - we want
    simply but healthy.' The model offers exactly that breakfast again; a simpler one was next."""
    w = week
    idx = {i.code: n for n, i in enumerate(catalogue.approved(w.db, w.partner), start=1)}
    fussy = {
        "name": EGGS_BACON,
        "ingredients": [
            {"i": idx["zz_eggs"], "g": 116}, {"i": idx["zz_bacon"], "g": 60},
            {"i": idx["zz_bread"], "g": 70}, {"i": idx["zz_spinach"], "g": 50},
            {"i": idx["zz_tomato"], "g": 80}, {"i": idx["zz_cottage"], "g": 100},
            {"i": idx["milk"], "g": 20},
        ],
        "method": EGGS_BACON_METHOD,
        "prep_minutes": 15,
    }  # fmt: skip
    # the stock day gets its fibre from the oats this breakfast replaces; a lower floor keeps the
    # solver out of the story, which is about how the dish is to cook, not about its macros
    prof = targets_mod.profile_on(w.db, w.partner, MON)
    assert prof is not None
    row = {k: prof[k] for k in prof.keys() if k not in ("id", "created_at")}  # noqa: SIM118
    w.db.insert("profiles", **{**row, "effective_from": isoday(MON), "fibre_g_min": 20})
    seg = menu_mod.Segment(THU, FRI)
    gw = FakeGateway([_with_breakfast(seg, fussy), proposal_for(seg)])
    w.gateways.append(gw)
    w.at(TUE, "18:00")
    status = w.run(menu_propose.JOB, TUE, w.partner)
    detail = w.db.scalar(
        "SELECT detail FROM run_ledger WHERE job = 'menu_propose' AND person_id = ? "
        "ORDER BY rowid DESC LIMIT 1",
        (w.partner,),
    )
    # setup: a retry for a missed macro would be a different story from a retry for complexity
    assert status == "ok", f"setup: the fussy breakfast did not solve ({status}): {detail}"
    assert menu_mod.decision_of(w.db, _newest_menu(w.db, w.partner)) == "accepted", (
        "Tuesday's Thu-Fri menu is in force on arrival - there is no Accept tap to make here"
    )

    w.at(THU, "07:15")
    day = last(w.say(PARTNER_CHAT, "/menu"))
    assert "Menu Thu 17 Sep" in day["text"], day["text"]
    card = last(w.tap(PARTNER_CHAT, button(day, "Breakfast")))["text"]
    ingredients, steps = card_parts(card)
    assert (
        len(ingredients) <= SIMPLE_BREAKFAST_INGREDIENTS and len(steps) <= SIMPLE_BREAKFAST_STEPS
    ), (
        f"Thursday's breakfast before work is {len(ingredients)} ingredients and {len(steps)} "
        f"steps - the brief's own example of too complex, passed straight through:\n{card}"
    )


# ---------------------------------------------------------------- Wednesday
def test_10_wednesday_evening_partner_thursday_list_is_the_menu_partner_accepted(week) -> None:
    """Wednesday 18:00, leaving work. She accepted a Thu-Fri menu on Tuesday; the list for
    tomorrow's cook arrives and she reads it in the shop on the way home."""
    w = week
    r = w.r
    w.at(TUE, "19:00")
    _put_menu(
        w.db,
        w.partner,
        {
            THU: {"breakfast": r["porridge"], "lunch": r["soup"], "dinner": r["traybake"],
                  "snack": r["yog"]},
            FRI: {"breakfast": r["eggs2"], "lunch": r["wrap"], "dinner": r["curry"],
                  "snack": r["yog"]},
        },
        accepted=True,
    )  # fmt: skip
    w.at(WED, "18:00")
    before = len(w.tg.sent)
    assert w.run(shopping_list.JOB, WED, w.partner) == "ok"
    lst = last(w.to(PARTNER_CHAT, before))
    assert lst["text"].startswith("<b>Shopping for Thu 17 Sep</b>"), lst["text"]
    assert "chickpea" not in lst["text"].lower(), lst["text"]
    assert len(lines_of(lst)) <= 10, lst["text"]
    assert not w.to(OWNER_CHAT, before), "his chat heard about her list"


# ---------------------------------------------------------------- Thursday and Friday
def test_11_thursday_morning_with_no_menu_cook_plans_today(week) -> None:
    """Thursday 08:00. She never got a Thu-Fri menu. The morning message says '/cook builds
    one'; at 08:05 she sends /cook, wanting to know what to eat today."""
    w = week
    w.at(THU, "08:00")
    before = len(w.tg.sent)
    assert w.run(morning_targets.JOB, THU, w.partner) == "ok"
    brief = last(w.to(PARTNER_CHAT, before))
    assert "No menu for today yet. /cook builds one." in brief["text"], brief["text"]

    w.gateways.append(FakeGateway([proposal_for(menu_mod.Segment(THU, FRI))] * 4))
    w.at(THU, "08:05")
    reply = said(w.say(PARTNER_CHAT, "/cook"))
    covers_today = w.db.scalar(
        "SELECT COUNT(*) FROM menus WHERE person_id = ? AND segment_start <= ? "
        "AND segment_end >= ?",
        (w.partner, isoday(THU), isoday(THU)),
    )
    assert covers_today, (
        "the brief said /cook builds today's menu; /cook planned only from tomorrow, so "
        f"Thursday - the day she asked about - still has nothing:\n{reply}"
    )


def test_12_friday_evening_with_no_menu_the_check_in_still_closes_the_day(week) -> None:
    """Friday 21:00. Thursday and Friday never had a menu; she ate about her target and wants to
    close the day in one tap, and to be told what was recorded."""
    w = week
    w.at(FRI, "21:00")
    before = len(w.tg.sent)
    assert w.run(evening_confirm.JOB, FRI, w.partner) == "ok"
    check_in = last(w.to(PARTNER_CHAT, before))
    assert "No menu today" in check_in["text"], check_in["text"]
    assert len(lines_of(check_in)) <= 10
    reply = said(w.tap(PARTNER_CHAT, "k:cfm:20260918"))
    row = w.db.one(
        "SELECT status, basis, kcal, note FROM day_confirmations WHERE person_id = ? AND day = ? "
        "ORDER BY id DESC LIMIT 1",
        (w.partner, isoday(FRI)),
    )
    assert row is not None and row["status"] == "confirmed", row
    assert row["note"] == "assumed target", dict(row)
    assert f"{int(row['kcal'])} kcal" in reply, reply
