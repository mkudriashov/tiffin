"""Stories about the messages nobody asks for.

The morning brief (and the page it links to), the evening check-in, the
shopping list, the menu proposal and the Sunday review. Each is read
the way the person reads it on their phone: is it ten lines or fewer, does it contradict another
message sent the same day, and - the question the brief keeps asking - does it put the thing
they need next (today's recipe, tomorrow's lunch, the list) one tap away?

The world is the real one on Monday 14 September 2026, copied from the live database:

- the owner cooks Monday to Friday in one go and freezes. His menu (the repeat) covers all five
days.
- the partner cooks Monday-Wednesday and Thursday-Friday. Her accepted menu for Mon-Wed still
has the
  chickpea salad on it; a chickpea-free proposal is waiting for her Accept; Thursday-Friday has
  no menu at all. Chickpeas are on her avoid list.

Every story drives the real job through the real Scheduler, or a real tap through the Poller,
and asserts on what reached the fake Telegram. Development drops everything addressed to her
(`dropped_dev`), so the fixture builds the Outbox as prod: that is the only way to see her
keyboards, and it changes nothing about what is composed.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import types
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tiffin import body
from tiffin import scheduler as sc
from tiffin import targets as targets_mod
from tiffin.app import App
from tiffin.clock import TZ, FrozenClock, isoday
from tiffin.db import Db
from tiffin.food import batch as batch_mod
from tiffin.jobs import (
    evening_confirm,
    menu_propose,
    morning_targets,
    shop_refresh,
    shopping_list,
    sunday_summary,
)
from tiffin.secrets import Secrets
from tiffin.telegram import outbox as outbox_mod
from tiffin.telegram.api import inline_keyboard
from tiffin.telegram.poller import Poller
from tiffin.web.app import create_app

from .fakes import FakeTelegram
from .test_food_shopping import seed_ingredient

OWNER_CHAT = 1000001
PARTNER_CHAT = 1000002
OWNER_LOGIN = "alex@example.com"
PARTNER_LOGIN = "sam@example.com"

SUN = dt.date(2026, 9, 13)
MON = dt.date(2026, 9, 14)
TUE = dt.date(2026, 9, 15)
WED = dt.date(2026, 9, 16)
THU = dt.date(2026, 9, 17)

#: taps that only move around; anything else (Accept, As planned, a rating) changes state
NAV_CALLBACKS = ("k:meal:", "k:mday:", "k:menuv:", "k:menuw:")
NAV_COMMANDS = {"menu", "recipe", "batch", "proposals", "shopping"}


# ---------------------------------------------------------------- the world
def _profile(db: Db, person_id: int, **over: Any) -> None:
    row: dict[str, Any] = {
        "person_id": person_id,
        "effective_from": "2026-09-01",
        "sex": "m",
        "birth_date": "1990-03-15",
        "height_cm": 183.0,
        "activity_factor": 1.55,
        "kcal_floor": 1800,
    }
    row.update(over)
    db.insert("profiles", **row)


def _recipe(db: Db, name: str, grams: dict[int, float], *, prep: int = 15, steps: int = 3) -> int:
    rid = db.insert(
        "recipes",
        name=name,
        servings=1,
        prep_minutes=prep,
        method_json=json.dumps([f"Step {n} of {name}." for n in range(1, steps + 1)]),
        source="model",
    )
    for iid, g in grams.items():
        db.insert("recipe_ingredients", recipe_id=rid, ingredient_id=iid, grams=g)
    return rid


def _menu(
    db: Db,
    person_id: int,
    start: dt.date,
    plan: list[dict[str, int]],
    *,
    status: str = "proposed",
    decision: str | None = "accepted",
    kcal: int = 1600,
) -> int:
    """One menu, one dict of slot -> recipe id per day, and (optionally) its decision row."""
    mid = db.insert(
        "menus",
        person_id=person_id,
        segment_start=isoday(start),
        segment_end=isoday(start + dt.timedelta(days=len(plan) - 1)),
        status=status,
    )
    for i, slots in enumerate(plan):
        day_id = db.insert(
            "menu_days",
            menu_id=mid,
            day=isoday(start + dt.timedelta(days=i)),
            kcal=kcal,
            protein_g=130,
            fat_g=50,
            carb_g=160,
            fibre_g=25,
        )
        for slot, rid in slots.items():
            db.insert(
                "meal_slots",
                menu_day_id=day_id,
                slot=slot,
                recipe_id=rid,
                portion_multiplier=1.0,
                portion_g=300,
                kcal=kcal // max(1, len(slots)),
                protein_g=30,
                fat_g=10,
                carb_g=35,
                fibre_g=5,
            )
    if decision is not None:
        db.insert("menu_decisions", menu_id=mid, decision=decision)
    return mid


@pytest.fixture
def home(
    fixture_db: Db,
    two_persons,
    frozen_clock: FrozenClock,
    config,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    db = fixture_db
    his, partner = two_persons["owner"], two_persons["partner"]

    # --- the two of them, as the live profiles say
    _profile(
        db,
        his,
        segment_days=json.dumps([[0, 1, 2, 3, 4]]),
        meals_per_day=4,
        pre_bed_protein=1,
        variety_json=json.dumps({"all": "same"}),
        batch_slots=json.dumps(["lunch", "dinner"]),
        freezes=1,
        refused_foods=json.dumps(["Chickpea"]),
        confirm_time="20:00",
        morning_time="06:30",
        quiet_start="22:00",
        quiet_end="06:00",
    )
    _profile(
        db,
        partner,
        sex="f",
        birth_date="1993-06-01",
        height_cm=168.0,
        activity_factor=1.375,
        kcal_floor=1200,
        segment_days=json.dumps([[0, 1, 2], [3, 4]]),
        meals_per_day=3,
        pre_bed_protein=1,
        variety_json=json.dumps({"all": "rotate"}),
        batch_slots=json.dumps(["lunch", "dinner"]),
        freezes=0,
        refused_foods=json.dumps(["Liver", "broccoli", "chickpea"]),
        confirm_time="21:00",
        morning_time="08:00",
        quiet_start="22:00",
        quiet_end="07:30",
        cycle_tracking=1,
    )
    body.record_weigh_in(db, his, dt.date(2026, 9, 12), 100.5, "manual")
    body.record_weigh_in(db, partner, dt.date(2026, 9, 12), 66.0, "manual")

    # --- the food
    ing: dict[str, int] = {}
    for code, name, aisle, pack, label in (
        ("chicken", "Chicken breast, raw", "meat", 500, "500 g pack"),
        ("chickpeas", "Chickpeas, tinned, drained", "tins", 240, "400 g tin"),
        ("feta", "Feta cheese", "dairy", 200, "200 g block"),
        ("lettuce", "Lettuce, raw", "produce", 300, "1 lettuce"),
        ("oats", "Porridge oats", "grains", 1000, "1 kg bag"),
        ("skyr", "Skyr, plain", "dairy", 450, "450 g pot"),
        ("salmon", "Salmon fillet, raw", "fish", 240, "2 fillets"),
        ("tuna", "Tuna, tinned in water", "tins", 145, "145 g tin"),
        ("wrap", "Tortilla wrap", "bakery", 480, "8 wraps"),
        ("eggs", "Eggs, medium", "dairy", 348, "6 eggs"),
        ("bacon", "Back bacon, raw", "meat", 300, "300 g pack"),
        ("lentils", "Red lentils, dry", "grains", 500, "500 g bag"),
        ("beans", "Kidney beans, tinned", "tins", 240, "400 g tin"),
        ("beef", "Beef mince 5%, raw", "meat", 500, "500 g pack"),
        ("rice", "Brown rice, dry", "grains", 1000, "1 kg bag"),
        ("yoghurt", "Greek yoghurt 0%", "dairy", 500, "500 g pot"),
        ("potato", "Potatoes, raw", "produce", 2000, "2 kg bag"),
        ("turkey", "Turkey breast, raw", "meat", 500, "500 g pack"),
    ):
        ing[code] = seed_ingredient(db, code, name, aisle, pack, label)

    r = {
        "porridge": _recipe(db, "Porridge with skyr, raspberries and almonds", {ing["oats"]: 50}),
        "chickpea": _recipe(
            db,
            "Chicken, chickpea and feta salad",
            {ing["chicken"]: 120, ing["chickpeas"]: 120, ing["feta"]: 30, ing["lettuce"]: 60},
        ),
        "salmon": _recipe(
            db, "Air-fried salmon, sweet potato and green beans", {ing["salmon"]: 120}
        ),
        "yoghurt": _recipe(
            db, "Greek yoghurt with strawberries and almonds", {ing["yoghurt"]: 170}
        ),
        "eggs": _recipe(
            db, "Scrambled eggs, bacon and spinach on toast", {ing["eggs"]: 120, ing["bacon"]: 60}
        ),
        "tuna": _recipe(
            db, "Tuna, avocado and sweetcorn wrap", {ing["tuna"]: 100, ing["wrap"]: 60}
        ),
        "curry": _recipe(
            db,
            "Chicken thigh and red lentil curry with rice",
            {ing["chicken"]: 150, ing["rice"]: 60},
        ),
        "chilli": _recipe(
            db,
            "Beef and kidney bean chilli with brown rice",
            {ing["beef"]: 125, ing["beans"]: 80, ing["rice"]: 60},
        ),
        "kbwrap": _recipe(
            db, "Chicken, kidney bean and pepper wrap", {ing["chicken"]: 120, ing["beans"]: 60}
        ),
        "soup": _recipe(
            db,
            "Curried chicken, red lentil and carrot soup",
            {ing["chicken"]: 100, ing["lentils"]: 50},
        ),
        "eggs2": _recipe(
            db, "Scrambled eggs on wholemeal toast with avocado and spinach", {ing["eggs"]: 120}
        ),
        "m_oats": _recipe(db, "Overnight oats with skyr, whey and berries", {ing["oats"]: 60}),
        "m_salad": _recipe(
            db,
            "Warm chicken, green bean and potato salad",
            {ing["chicken"]: 200, ing["potato"]: 200},
        ),
        "m_turkey": _recipe(
            db,
            "Garlic turkey with air-fried carrots and potato wedges",
            {ing["turkey"]: 200, ing["potato"]: 200},
        ),
        "m_snack": _recipe(db, "Strawberry protein yoghurt with almonds", {ing["yoghurt"]: 200}),
        "m_bed": _recipe(db, "Skyr with peanut butter and raspberries", {ing["skyr"]: 200}),
    }

    # her accepted Mon-Wed menu (live id 5): the chickpea salad on Monday and Wednesday
    partner_accepted = _menu(
        db,
        partner,
        MON,
        [
            {"breakfast": r["porridge"], "lunch": r["chickpea"], "dinner": r["salmon"],
             "pre_bed": r["yoghurt"]},
            {"breakfast": r["eggs"], "lunch": r["tuna"], "dinner": r["curry"],
             "pre_bed": r["yoghurt"]},
            {"breakfast": r["porridge"], "lunch": r["chickpea"], "dinner": r["chilli"],
             "pre_bed": r["yoghurt"]},
        ],
    )  # fmt: skip
    # his repeated Mon-Fri menu (live id 6): the same five dishes every day
    his_menu = _menu(
        db,
        his,
        MON,
        [
            {"breakfast": r["m_oats"], "lunch": r["m_salad"], "dinner": r["m_turkey"],
             "snack": r["m_snack"], "pre_bed": r["m_bed"]}
            for _ in range(5)
        ],
        status="repeated",
        kcal=1915,
    )  # fmt: skip
    # her chickpea-free proposal (live id 7), waiting for a tap
    partner_proposal = _menu(
        db,
        partner,
        MON,
        [
            {"breakfast": r["porridge"], "lunch": r["kbwrap"], "dinner": r["chilli"],
             "pre_bed": r["yoghurt"]},
            {"breakfast": r["eggs2"], "lunch": r["soup"], "dinner": r["curry"],
             "pre_bed": r["yoghurt"]},
            {"breakfast": r["porridge"], "lunch": r["kbwrap"], "dinner": r["chilli"],
             "pre_bed": r["yoghurt"]},
        ],
        decision=None,
    )  # fmt: skip

    # --- the box, wired to fakes. Her messages have to be SEEN, so the Outbox is built as prod.
    real_init = outbox_mod.Outbox.__init__

    def prod_outbox(self, api, db_, **kw):
        kw["env"] = "prod"
        real_init(self, api, db_, **kw)

    monkeypatch.setattr(outbox_mod.Outbox, "__init__", prod_outbox)
    # the list tops up prices through a real browser first; there is none in a test, and the
    # list is what is under test, not the shop
    monkeypatch.setattr(shop_refresh, "fill_for_menus", lambda *a, **k: "")

    secrets_dir = tmp_path / "s"
    secrets_dir.mkdir()
    (secrets_dir / "telegram-token-dev.txt").write_text("1:t", encoding="utf-8")
    config.paths.ensure()
    tg = FakeTelegram()
    app = App(
        config,
        Secrets(secrets_dir),
        clock=frozen_clock,
        transport=tg,
        gateway_factory=lambda _a, _d: None,
    )
    handlers = app.handlers(db)
    poller = Poller(
        app.api(), db, app.registry(db), handlers.handle, clock=frozen_clock, sleeper=lambda s: None
    )
    n = {"i": 0}

    def say(chat: int, text: str) -> None:
        n["i"] += 1
        tg.queue_update(tg.text_update(n["i"], chat, text))
        poller.run_once()

    def tap(chat: int, data: str) -> None:
        n["i"] += 1
        tg.queue_update(tg.callback_update(n["i"], chat, data, message_id=n["i"] + 1000))
        poller.run_once()

    def at(day: dt.date, hhmm: str) -> None:
        h, m = (int(x) for x in hhmm.split(":"))
        frozen_clock.set_local(dt.datetime(day.year, day.month, day.day, h, m, tzinfo=TZ))

    def run(job: sc.Job, day: dt.date, person_id: int | None = None) -> str:
        if job.scope == "each":
            assert person_id is not None
            prof = targets_mod.profile_on(db, person_id, day)
            slots = sc.slots_for(job, person_id, prof, day, db)
        else:
            slots = sc.slots_for(job, sc.SYSTEM, None, day, db)
        assert slots, f"{job.name} has no slot on {day}"
        return sc.Scheduler(app, [job], lambda _p, _fn: None).execute(db, slots[0])

    def to(chat: int, since: int = 0) -> list[dict[str, Any]]:
        return [m for m in tg.sent[since:] if int(m["chat_id"]) == chat]

    return types.SimpleNamespace(
        db=db,
        app=app,
        tg=tg,
        his=his,
        partner=partner,
        r=r,
        ing=ing,
        partner_accepted=partner_accepted,
        his_menu=his_menu,
        partner_proposal=partner_proposal,
        say=say,
        tap=tap,
        at=at,
        run=run,
        to=to,
        clock=frozen_clock,
    )


# ---------------------------------------------------------------- reading a phone
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


def tappable(msg: dict[str, Any]) -> list[tuple[str, str]]:
    """What a thumb can do from this message without changing anything: the navigation buttons,
    and the /commands Telegram turns into links (not the ones inside <code>, which it does not)."""
    out = [("tap", d) for _t, d in buttons(msg) if d.startswith(NAV_CALLBACKS)]
    text = re.sub(r"<code>.*?</code>", "", str(msg["text"]), flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    for cmd in re.findall(r"(?<![\w/:])/([a-z_]+)\b", text):
        if cmd in NAV_COMMANDS and ("say", f"/{cmd}") not in out:
            out.append(("say", f"/{cmd}"))
    return out


def is_card(name: str):
    """A recipe card for `name` - not merely a message that mentions it."""
    return lambda text: "<b>Ingredients</b>" in text and name in text


def taps_to(w, chat: int, start: list[dict[str, Any]], want, depth: int = 3) -> int | None:
    """Fewest taps from these messages to one satisfying `want`, or None within `depth`."""
    frontier = list(start)
    seen: set[tuple[str, str]] = set()
    for taps in range(depth + 1):
        if any(want(str(m["text"])) for m in frontier):
            return taps
        nxt: list[dict[str, Any]] = []
        for m in frontier:
            for kind, what in tappable(m):
                if (kind, what) in seen:
                    continue
                seen.add((kind, what))
                before = len(w.tg.sent)
                (w.tap if kind == "tap" else w.say)(chat, what)
                nxt += w.to(chat, before)
        frontier = nxt
    return None


def last_to(w, chat: int, since: int = 0) -> dict[str, Any]:
    got = w.to(chat, since)
    assert got, "nothing reached the phone"
    return got[-1]


# ---------------------------------------------------------------- the morning
def test_01_owner_opens_todays_lunch_from_the_morning_brief_in_one_tap(home) -> None:
    """Monday 06:30, the owner reads the brief over coffee and wants the method for today's lunch
    before he opens the freezer - one tap from the message that names it."""
    w = home
    w.at(MON, "06:30")
    before = len(w.tg.sent)
    assert w.run(morning_targets.JOB, MON, w.his) == "ok"
    brief = last_to(w, OWNER_CHAT, before)
    # two lines since 26 Sep: the brief names the first meal, and lunch is one of its buttons
    # (his page login is not linked in this world, so the buttons are the way in)
    assert "First up:" in brief["text"]
    assert len(lines_of(brief)) <= 2

    taps = taps_to(w, OWNER_CHAT, [brief], is_card("Warm chicken, green bean and potato salad"))
    assert taps == 1, (
        f"today's lunch recipe is {taps} taps from the brief that names it; the brief has "
        f"buttons {buttons(brief)} and says {brief['text'].splitlines()[2]!r}"
    )


def test_02_partner_monday_brief_does_not_serve_the_chickpeas_partner_refused_without_a_word(
    home,
) -> None:
    """Monday 08:00, the partner reads today's meals. Chickpeas are on her avoid list and a
    chickpea-free menu is waiting for her yes - the brief is where she would learn either."""
    w = home
    w.at(MON, "08:00")
    before = len(w.tg.sent)
    assert w.run(morning_targets.JOB, MON, w.partner) == "ok"
    brief = last_to(w, PARTNER_CHAT, before)
    text = brief["text"]
    assert "chickpea" in text.lower(), "setup: the accepted menu still has the salad"
    warned = any(k in text.lower() for k in ("avoid", "waiting", "/proposals", "refuse"))
    assert warned, (
        "her morning brief names a dish with a food she refused, and says nothing about it or "
        f"about the chickpea-free menu waiting for her tap:\n{text}"
    )


# ---------------------------------------------------------------- the shopping list
def test_03_partner_wednesday_evening_says_something_about_thursdays_shop(home) -> None:
    """Wednesday 18:00, the evening before her Thursday-Friday cook. There is no menu for
    those days, so there can be no list - and she needs to hear that tonight, not in the shop."""
    w = home
    w.at(WED, "18:00")
    before = len(w.tg.sent)
    status = w.run(shopping_list.JOB, WED, w.partner)
    got = w.to(PARTNER_CHAT, before)
    detail = w.db.scalar(
        "SELECT detail FROM run_ledger WHERE job = 'shopping_list' AND person_id = ? "
        "ORDER BY rowid DESC LIMIT 1",
        (w.partner,),
    )
    assert got, (
        f"the shopping_list run was {status!r} ({detail!r}) and nothing reached her; "
        f"Alex's chat got {[m['text'][:80] for m in w.to(OWNER_CHAT, before)]}"
    )
    assert "thu" in got[-1]["text"].lower() and "/cook" in got[-1]["text"]


def test_04_partner_sunday_list_does_not_send_partner_to_buy_chickpeas(home) -> None:
    """Sunday 18:00, the partner gets the list for Monday's cook and takes it to the shop. She has
    asked three times not to have chickpeas; the list is the last place that can say so."""
    w = home
    # the proposal came after the list in real life; it must not be what this list reads
    w.db.insert("menu_decisions", menu_id=w.partner_proposal, decision="rejected")
    w.at(SUN, "18:00")
    before = len(w.tg.sent)
    assert w.run(shopping_list.JOB, SUN, w.partner) == "ok"
    lst = last_to(w, PARTNER_CHAT, before)
    assert lst["text"].startswith("<b>Shopping for Mon 14 Sep</b>")
    assert len(lines_of(lst)) <= 10
    text = lst["text"].lower()
    flagged = "avoid" in text or "refuse" in text
    assert "chickpea" not in text or flagged, (
        f"the list sends her to buy chickpeas, which she refused, without a word:\n{lst['text']}"
    )


def test_05_accepting_the_new_menu_after_the_list_went_out_gets_partner_a_list_that_matches(
    home,
) -> None:
    """Monday lunchtime, in the shop. The chickpea-free menu was built this morning and is hers
    from that moment - there is nothing to tap. The list she was sent last night was built from
    the old one, so /shopping has to answer for the menu she is actually holding."""
    w = home
    w.db.insert("menu_decisions", menu_id=w.partner_proposal, decision="rejected")
    w.at(SUN, "18:00")
    assert w.run(shopping_list.JOB, SUN, w.partner) == "ok"
    # ...and it is rebuilt: a fresh menu row, in force the moment it exists, as the live one is
    _menu(
        w.db,
        w.partner,
        MON,
        [
            {"breakfast": w.r["porridge"], "lunch": w.r["kbwrap"], "dinner": w.r["chilli"]},
            {"breakfast": w.r["eggs2"], "lunch": w.r["soup"], "dinner": w.r["curry"]},
            {"breakfast": w.r["porridge"], "lunch": w.r["kbwrap"], "dinner": w.r["chilli"]},
        ],
        decision="accepted",
    )
    w.at(MON, "12:40")
    before = len(w.tg.sent)
    w.say(PARTNER_CHAT, "/shopping")
    lst = last_to(w, PARTNER_CHAT, before)["text"]
    # the fixed hint "<code>/shopping out chickpeas</code>" is an example, not an item
    items = re.sub(r"<code>.*?</code>", "", lst, flags=re.S)
    assert "chickpea" not in items.lower(), f"the list still has chickpeas:\n{lst}"
    assert "kidney" in lst.lower(), f"the list is not the new menu's:\n{lst}"


# ---------------------------------------------------------------- the menu proposal
def test_06_partner_can_read_tuesdays_lunch_recipe_before_partner_accepts(home) -> None:
    """Sunday evening on the sofa, the partner reads the proposal. Tuesday's lunch is a soup she has
    never made, and she wants to see how much work it is before she says yes."""
    w = home
    w.at(SUN, "20:00")
    # exactly what menu_propose._propose_for sends for a fresh proposal (lines 507-525)
    html, payload = menu_propose.message_for(w.db, w.partner_proposal)
    rows = [
        [("Accept", f"k:menu:{w.partner_proposal}"), ("Change", f"k:menux:{w.partner_proposal}")],
        *menu_propose.meal_buttons(w.db, w.partner_proposal),
    ]
    person = w.app.registry(w.db).by_id(w.partner)
    assert person is not None
    before = len(w.tg.sent)
    w.app.outbox(w.db).send(
        person, "menu_proposal", f"menu:{w.partner_proposal}", html, inline_keyboard(rows),
        payload=payload,
    )  # fmt: skip
    proposal = last_to(w, PARTNER_CHAT, before)
    assert "Curried chicken, red lentil and carrot soup (Tue)" in proposal["text"]
    assert len(lines_of(proposal)) <= 10

    taps = taps_to(
        w, PARTNER_CHAT, [proposal], is_card("Curried chicken, red lentil and carrot soup")
    )
    assert taps is not None and taps <= 2, (
        f"Tuesday's lunch is named on the proposal and unreachable from it (taps={taps}); the "
        f"only Lunch button is {[b for b in buttons(proposal) if b[0] == 'Lunch']}"
    )


# ---------------------------------------------------------------- the evening
def test_07_after_answering_the_check_in_tomorrows_lunch_is_one_tap_away(home) -> None:
    """Monday 21:00, the partner taps As planned. The next thing on her mind is what to put in her
    bag for tomorrow - Tuesday's lunch is a different dish from today's."""
    w = home
    w.at(MON, "21:00")
    before = len(w.tg.sent)
    assert w.run(evening_confirm.JOB, MON, w.partner) == "ok"
    check_in = last_to(w, PARTNER_CHAT, before)
    assert len(lines_of(check_in)) <= 10
    assert ("As planned", "k:cfm:20260914") in buttons(check_in)

    before = len(w.tg.sent)
    w.tap(PARTNER_CHAT, "k:cfm:20260914")
    reply = w.to(PARTNER_CHAT, before)
    assert reply, "the tap was answered with silence"
    taps = taps_to(
        w, PARTNER_CHAT, [check_in, *reply], is_card("Tuna, avocado and sweetcorn wrap"), depth=2
    )
    assert taps is not None and taps <= 1, (
        f"tomorrow's lunch is {taps} taps from the evening check-in and its answer "
        f"{[m['text'] for m in reply]!r}"
    )


def test_08_the_evening_before_a_frozen_box_owner_is_told_to_take_it_out(home) -> None:
    """Wednesday 20:00. the owner cooked Sunday and froze Thursday's and Friday's boxes because they
    would not keep. Tonight is the only moment a reminder to move Thursday's lunch to the fridge
    is any use."""
    w = home
    menu = w.db.one("SELECT * FROM menus WHERE id = ?", (w.his_menu,))
    load = batch_mod.plan(w.db, menu, ("lunch", "dinner"), SUN, freezes=True)
    frozen = {d.name for d, late in load.to_freeze if THU in late}
    assert "Warm chicken, green bean and potato salad" in frozen, "setup: /batch froze it"

    w.at(WED, "20:00")
    before = len(w.tg.sent)
    assert w.run(evening_confirm.JOB, WED, w.his) == "ok"
    check_in = last_to(w, OWNER_CHAT, before)
    text = check_in["text"].lower()
    assert any(k in text for k in ("freezer", "defrost", "thaw")), (
        f"Thursday's boxes are in the freezer and Wednesday evening does not say so:\n"
        f"{check_in['text']}"
    )


def test_09_partner_brief_link_to_the_page_opens_partner_week(home) -> None:
    w = home
    # today only the owner is linked; she has no Tailscale identity at all
    w.db.insert("tailscale_identities", person_id=w.his, login=OWNER_LOGIN)
    w.at(MON, "08:00")
    before = len(w.tg.sent)
    assert w.run(morning_targets.JOB, MON, w.partner) == "ok"
    brief = last_to(w, PARTNER_CHAT, before)
    assert "<a href" not in brief["text"], (
        f"her brief links a page that will not let her in:\n{brief['text']}"
    )

    # the owner links her login; Tuesday's brief has the link and it is hers
    w.db.insert("tailscale_identities", person_id=w.partner, login=PARTNER_LOGIN)
    w.at(TUE, "08:00")
    before = len(w.tg.sent)
    assert w.run(morning_targets.JOB, TUE, w.partner) == "ok"
    brief = last_to(w, PARTNER_CHAT, before)
    link = re.search(r'<a href="([^"]+)">([^<]+)</a>', brief["text"])
    assert link is not None, f"linked now, and the brief still has no link:\n{brief['text']}"

    # the page opens its own connections at config.paths.db: put exactly this world there
    target = w.app.config.paths.db
    if target.exists():
        target.unlink()
    w.db.execute("VACUUM INTO ?", (str(target),))
    w.app.config.data["web"]["secure_cookies"] = False
    w.app.config.data["web"]["root_path"] = ""
    page = TestClient(create_app(w.app), base_url="http://testserver")
    assert page.get("api/menu", headers={"Tailscale-User-Login": OWNER_LOGIN}).status_code == 200

    # whatever login her phone presents, nothing links it to her
    partners = page.get("api/menu", headers={"Tailscale-User-Login": PARTNER_LOGIN})
    assert partners.status_code == 200, (
        f"her morning brief sends her to {link.group(1)} ({link.group(2)!r}); the page answers "
        f"her with {partners.status_code} {partners.text}"
    )


# ---------------------------------------------------------------- Sunday
def test_11_partner_sunday_review_points_at_tomorrows_menu(home) -> None:
    """Sunday 19:00, the partner reads the week's review. The next thing she needs is tomorrow's
    food - the menu, and the new proposal waiting for her yes."""
    w = home
    w.at(SUN, "19:00")
    before = len(w.tg.sent)
    assert w.run(sunday_summary.JOB, SUN) == "ok"
    review = last_to(w, PARTNER_CHAT, before)
    assert len(lines_of(review)) <= 10
    routes = tappable(review) + [("tap", d) for _t, d in buttons(review)]
    assert routes, (
        "the weekly review ends the week with no route to the next one - no button, no "
        f"/menu, no /proposals:\n{review['text']}"
    )
