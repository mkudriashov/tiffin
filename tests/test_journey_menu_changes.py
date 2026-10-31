"""Twelve moments when a menu changes, and whether the person can still tell which one is real.

and Thu-Fri. Her Mon-Wed menu, in force, still has "Chicken, chickpea and feta salad" on it, and
her Thu-Fri has no menu at all. Chickpeas are on her refused list, and she has asked three times.

bullshit with Accept - I thought it just gets created and that's it, we do not need to click more
buttons?" - so nothing here waits for a yes. That removes a whole class of these twelve moments
(two menus both half-real, a silent 17:00 lapse deciding for her) and sharpens the rest: what a
change takes effect on, and whether the person can still tell which menu is real.

Every story drives the real path (poller -> handlers -> menuflow -> jobs) and reads
what the PERSON was sent. Her messages are read from `messages_out`, because a shell is dev and
the scheduler's outbox drops everything addressed to her. The handlers' outbox is switched to prod
here so her keyboards can be read too. That is test wiring, not a product change.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import types

import pytest
from fastapi.testclient import TestClient

from tiffin import scheduler as sc
from tiffin.app import App
from tiffin.clock import TZ, FrozenClock, isoday
from tiffin.db import Db
from tiffin.food import menu as menu_mod
from tiffin.food import shopping
from tiffin.jobs import menu_propose
from tiffin.secrets import Secrets
from tiffin.telegram.poller import Poller
from tiffin.web.app import create_app

from .fakes import FakeTelegram
from .test_food_shopping import seed_ingredient
from .test_menu import FakeGateway, proposal_for, seed_catalogue

OWNER_CHAT = 1000001
PARTNER_CHAT = 1000002
LOGIN_HEADER = "Tailscale-User-Login"
OWNER_LOGIN = "alex@example.com"

MON = dt.date(2026, 9, 14)
TUE = dt.date(2026, 9, 15)
WED = dt.date(2026, 9, 16)
THU = dt.date(2026, 9, 17)
FRI = dt.date(2026, 9, 18)

CHICKPEA_SALAD = "Chicken, chickpea and feta salad"
NEW_LUNCH = "Chicken, kidney bean and pepper wrap"


# ---------------------------------------------------------------- the house as it is today
def _ing(db: Db, code: str) -> int:
    found = db.scalar(
        "SELECT id FROM ingredients WHERE code = ? ORDER BY version DESC, id DESC LIMIT 1", (code,)
    )
    assert found is not None, code
    return int(found)


def dish(db: Db, name: str, grams: dict[str, float]) -> int:
    rid = db.insert(
        "recipes",
        name=name,
        servings=1,
        prep_minutes=20,
        method_json=json.dumps(["Cook it.", "Box it."]),
        source="model",
    )
    for code, g in grams.items():
        db.insert("recipe_ingredients", recipe_id=rid, ingredient_id=_ing(db, code), grams=g)
    return rid


def put_menu(
    db: Db,
    pid: int,
    days: dict[dt.date, dict[str, int]],
    *,
    status: str = "proposed",
    accepted: bool = False,
) -> int:
    """A stored menu, day by day, exactly the tables `store_plan` writes."""
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
            kcal=2000,
            protein_g=150,
            fat_g=70,
            carb_g=200,
            fibre_g=30,
        )
        for slot, rid in days[day].items():
            db.insert(
                "meal_slots",
                menu_day_id=did,
                slot=slot,
                recipe_id=rid,
                portion_multiplier=1.0,
                portion_g=300,
                kcal=500,
                protein_g=38,
                fat_g=18,
                carb_g=50,
                fibre_g=8,
            )
    if accepted:
        menu_mod.decide(db, mid, "accepted")
    return mid


def _profile(db: Db, pid: int, segments: list[list[int]], variety: str, refused: list[str]) -> None:
    db.insert(
        "profiles",
        person_id=pid,
        effective_from="2026-09-01",
        sex="m" if not refused else "f",
        birth_date="1990-03-15",
        height_cm=175.0,
        activity_factor=1.5,
        kcal_floor=1400,
        meals_per_day=4,
        segment_days=json.dumps(segments),
        variety_json=json.dumps({"all": variety}),
        refused_foods=json.dumps(refused),
    )
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
def home(fixture_db: Db, two_persons, frozen_clock: FrozenClock, config, tmp_path):
    db = fixture_db
    his, partner = two_persons["owner"], two_persons["partner"]
    frozen_clock.set_local(dt.datetime(2026, 9, 13, 17, 0, tzinfo=TZ))
    _profile(db, his, [[0, 1, 2, 3, 4]], "same", [])
    _profile(db, partner, [[0, 1, 2], [3, 4]], "rotate", ["chickpea"])
    db.insert("consents", person_id=partner, granted=1, wording_version=1)
    seed_catalogue(db)
    # "zz_" keeps the seeded catalogue's prompt numbering (ordered by code) the same for both
    seed_ingredient(
        db,
        "zz_chickpeas",
        "Chickpeas, tinned, drained",
        "tins",
        400.0,
        "400 g tin",
        kcal_100=115.0,
        protein_100=7.0,
        fat_100=2.5,
        carb_100=15.0,
        fibre_100=5.0,
    )
    eggs = dish(db, "Scrambled eggs, bacon and spinach on toast", {"milk": 60, "oats": 40})
    salad = dish(db, CHICKPEA_SALAD, {"chicken": 150, "zz_chickpeas": 120, "oil": 10})
    tuna = dish(db, "Tuna, avocado and sweetcorn wrap", {"chicken": 120, "rice": 60})
    curry = dish(db, "Chicken thigh and red lentil curry with rice", {"chicken": 180, "rice": 80})
    yog = dish(db, "Greek yoghurt with strawberries", {"yoghurt": 200})
    porridge = dish(db, "Porridge with skyr and raspberries", {"oats": 60, "milk": 200})
    # the wrap is no longer seeded here: the replacement she asks for is built by the model
    # (`_renamed(..., {"lunch": NEW_LUNCH})`), which stores its own recipe row
    chilli = dish(db, "Beef and kidney bean chilli with brown rice", {"chicken": 160, "rice": 80})
    old_days = {
        MON: {"breakfast": porridge, "lunch": salad, "dinner": curry, "snack": yog},
        TUE: {"breakfast": eggs, "lunch": tuna, "dinner": curry, "snack": yog},
        WED: {"breakfast": porridge, "lunch": salad, "dinner": chilli, "snack": yog},
    }
    # her Saturday proposal, accepted on Sunday morning: the one with the chickpea salad
    partner_old = put_menu(db, partner, old_days, accepted=True)
    # his Mon-Fri, a repeat
    oats = dish(db, "Oats with whey", {"oats": 80, "whey": 30})
    rice = dish(db, "Chicken, rice and broccoli", {"chicken": 220, "rice": 90, "broccoli": 200})
    his_days = {
        MON + dt.timedelta(days=i): {"breakfast": oats, "lunch": rice, "dinner": rice, "snack": yog}
        for i in range(5)
    }
    his_menu = put_menu(db, his, his_days, status="repeated")
    # Sunday 17:00, both segment lists go out
    shopping.build(db, partner_old, clock=frozen_clock)
    shopping.build(db, his_menu, clock=frozen_clock)
    # No never-tapped replacement sits here: a menu is decided the moment it is built and sent,
    # so an undecided menu is not a state the live system can reach. `new_days` is what a
    # replacement she asks for looks like, and the stories that need one build it.
    frozen_clock.set_local(dt.datetime(2026, 9, 14, 12, 0, tzinfo=TZ))

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
    handlers.outbox.env = "prod"  # so her keyboards reach the fake Telegram and can be read
    poller = Poller(
        app.api(), db, app.registry(db), handlers.handle, clock=frozen_clock, sleeper=lambda s: None
    )
    n = {"i": 0}

    def newest() -> int:
        return int(db.scalar("SELECT COALESCE(MAX(id), 0) FROM messages_out") or 0)

    def said_since(pid: int, after: int) -> str:
        rows = db.all(
            "SELECT text_html FROM messages_out WHERE person_id = ? AND id > ? ORDER BY id",
            (pid, after),
        )
        return "\n".join(str(r["text_html"]) for r in rows)

    def chat(pid: int) -> int:
        return OWNER_CHAT if pid == his else PARTNER_CHAT

    def say(pid: int, text: str) -> str:
        before = newest()
        n["i"] += 1
        tg.queue_update(tg.text_update(n["i"], chat(pid), text))
        poller.run_once()
        return said_since(pid, before)

    def tap(pid: int, data: str) -> str:
        before = newest()
        n["i"] += 1
        tg.queue_update(tg.callback_update(n["i"], chat(pid), data, message_id=n["i"]))
        poller.run_once()
        return said_since(pid, before)

    def buttons(pid: int) -> list[str]:
        mine = [m for m in tg.sent if m["chat_id"] == chat(pid)]
        if not mine:
            return []
        markup = mine[-1].get("reply_markup") or {"inline_keyboard": []}
        return [b["callback_data"] for row in markup["inline_keyboard"] for b in row]

    def run_job(job: sc.Job, day: dt.date, pid: int) -> str:
        scheduler = sc.Scheduler(app, [job], lambda p, fn: None)
        prof = db.one("SELECT * FROM profiles WHERE person_id = ? ORDER BY id DESC LIMIT 1", (pid,))
        slots = sc.slots_for(job, pid, prof, day, db)
        assert slots, f"no slot for {job.name} on {day}"
        return scheduler.execute(db, slots[0])

    def at(when: dt.datetime) -> None:
        frozen_clock.set_local(when)

    return types.SimpleNamespace(
        db=db,
        clock=frozen_clock,
        tg=tg,
        gateways=gateways,
        owner=his,
        partner=partner,
        partner_old=partner_old,
        his_menu=his_menu,
        dish=lambda name, grams: dish(db, name, grams),
        put_menu=lambda pid, days, **kw: put_menu(db, pid, days, **kw),
        say=say,
        tap=tap,
        buttons=buttons,
        run_job=run_job,
        newest=newest,
        said_since=said_since,
        at=at,
    )


def _renamed(payload: dict, names: dict[str, str]) -> dict:
    """The fake model's answer with dish names changed per slot, so a new menu is recognisable."""
    for day in payload["days"]:
        for slot in day["slots"]:
            if slot["slot"] in names:
                slot["recipe"]["name"] = names[slot["slot"]]
    return payload


def _newest_menu(db: Db, pid: int) -> int:
    return int(db.scalar("SELECT MAX(id) FROM menus WHERE person_id = ?", (pid,)))


# ---------------------------------------------------------------- the stories
def test_01_menu_names_the_food_partner_avoids_and_the_one_command_that_changes_it(home) -> None:
    """Monday 12:00, at her desk. She asked three times for no chickpeas and opens /menu to see
    what lunch is. Nothing is waiting for a yes any more, so the whole job of this answer is the
    truth about today plus a way out that does not need her to find an old message."""
    said = home.say(home.partner, "/menu")
    assert CHICKPEA_SALAD in said, "the menu in force is the one she is eating today"
    assert "which you now avoid" in said, (
        "she has asked three times and /menu lists it like any other lunch:\n" + said
    )
    assert "/cook replace" in said, (
        "/menu names the food she avoids and no way to be rid of it:\n" + said
    )


def test_02_the_replacement_partner_asks_for_needs_no_second_tap(home) -> None:
    """Monday 12:05. She sends /cook replace no chickpeas and then opens /menu for the days it
    covers. One command: the menu is hers when it arrives, not when she finds a button on it."""
    payload = _renamed(proposal_for(menu_mod.Segment(TUE, WED)), {"lunch": NEW_LUNCH})
    home.gateways.append(FakeGateway([payload]))
    built = home.say(home.partner, "/cook replace no chickpeas")
    assert "Tue to Wed" in built, built
    rebuild = _newest_menu(home.db, home.partner)
    assert menu_mod.decision_of(home.db, rebuild) == "accepted", (
        "a menu she asked for and was sent is not in force until something taps for her"
    )
    said = home.say(home.partner, "/menu tue")
    assert NEW_LUNCH in said, said
    assert "chickpea" not in said.lower(), said


def test_03_the_list_partner_reads_in_the_shop_is_the_menu_partner_changed(home) -> None:
    """Monday 17:30, in the shop, phone in hand. She replaced Tuesday and Wednesday at lunch and
    opens /shopping to see what to buy.

    This is the moment the brief is about: "in a shop the list and the menu disagreed and she got
    chickpeas after asking three times." The change took effect the moment it was built, with no
    yes in between, so there is no window here in which the list and the menu can disagree - and
    the list has to be for the days she actually changed.
    """
    payload = _renamed(proposal_for(menu_mod.Segment(TUE, WED)), {"lunch": NEW_LUNCH})
    home.gateways.append(FakeGateway([payload]))
    home.say(home.partner, "/cook replace no chickpeas")
    home.at(dt.datetime(2026, 9, 14, 17, 30, tzinfo=TZ))
    said = home.say(home.partner, "/shopping")
    assert "Shopping Tue 15 to Wed 16 Sep" in said, said
    to_buy = [ln for ln in said.splitlines() if ln.startswith("- ")]
    assert to_buy, f"a shopping list with nothing to buy on it:\n{said}"
    assert not [ln for ln in to_buy if "chickpea" in ln.lower()], (
        "she asked for a menu without them four hours ago and the list in her hand still says "
        "buy them:\n" + said
    )


def test_04_thursday_lunch_is_one_command_and_one_tap_away(home) -> None:
    """Thursday 12:40, at her desk. Both of her segments have menus; she wants to know what
    lunch is today and how to make it."""
    bowl = home.dish("Tuna and sweetcorn rice bowl", {"chicken": 130, "rice": 70})
    yog = home.dish("Yoghurt and whey pot", {"yoghurt": 200, "whey": 20})
    oats = home.dish("Overnight oats", {"oats": 60, "milk": 200})
    curry = home.dish("Chicken curry", {"chicken": 180, "rice": 80})
    thu_fri = home.put_menu(
        home.partner,
        {d: {"breakfast": oats, "lunch": bowl, "dinner": curry, "snack": yog} for d in (THU, FRI)},
        accepted=True,  # built and sent on Tuesday, in force from that moment
    )
    home.at(dt.datetime(2026, 9, 17, 12, 40, tzinfo=TZ))

    said = home.say(home.partner, "/menu")  # tap 1
    assert "Menu Thu 17 Sep" in said and "Lunch: Tuna and sweetcorn rice bowl" in said, said
    lunch = f"k:meal:{thu_fri}:lunch:20260917"
    assert lunch in home.buttons(home.partner), home.buttons(home.partner)
    card = home.tap(home.partner, lunch)  # tap 2
    assert "Tuna and sweetcorn rice bowl" in card and "<b>Method</b>" in card, card
    assert "Basmati rice" in card, "the ingredients are on the card"


def test_05_nothing_decides_for_partner_the_day_before(home) -> None:
    home.at(dt.datetime(2026, 9, 15, 18, 5, tzinfo=TZ))
    home.gateways.append(FakeGateway([proposal_for(menu_mod.Segment(THU, FRI))]))
    assert home.run_job(menu_propose.JOB, TUE, home.partner) == "ok"
    proposed = _newest_menu(home.db, home.partner)
    start = home.db.scalar("SELECT segment_start FROM menus WHERE id = ?", (proposed,))
    assert start == "2026-09-17", "setup: Tuesday's job built Thu-Fri"
    assert menu_mod.decision_of(home.db, proposed) == "accepted", "in force on arrival"
    assert not hasattr(menu_propose, "LAPSE_JOB"), (
        "the 17:00 lapse job is retired with Accept; while it exists it can still overwrite a "
        "menu nobody was asked about"
    )

    home.at(dt.datetime(2026, 9, 16, 19, 0, tzinfo=TZ))
    thursday = home.say(home.partner, "/menu thu")
    assert "chickpea" not in thursday.lower(), (
        "Thursday is not the menu she was sent on Tuesday:\n" + thursday
    )
    assert _newest_menu(home.db, home.partner) == proposed, "and nothing built another behind her"


def test_07_asking_about_thursday_and_doing_as_told_gets_a_thursday_menu(home) -> None:
    """Monday 12:40. She has no Thu-Fri menu, asks about Thursday, and is told /cook builds one.
    She sends /cook, expecting Thursday to be planned."""
    home.gateways.append(FakeGateway([proposal_for(menu_mod.Segment(THU, FRI)), {}, {}, {}]))
    home.at(dt.datetime(2026, 9, 14, 12, 40, tzinfo=TZ))
    asked = home.say(home.partner, "/menu thu")
    assert "No menu covers Thu 17 Sep. /cook builds one now." in asked, asked
    reply = home.say(home.partner, "/cook")
    covers_thursday = home.db.scalar(
        "SELECT COUNT(*) FROM menus WHERE person_id = ? AND segment_start <= ? "
        "AND segment_end >= ?",
        (home.partner, isoday(THU), isoday(THU)),
    )
    assert covers_thursday, (
        "Tiffin said /cook builds Thursday; /cook looked at TUESDAY instead and answered:\n" + reply
    )


def test_08_owner_accepts_a_tuesday_to_friday_change_and_menu_shows_tomorrow(home) -> None:
    """Monday 20:00, on the sofa. the owner asks for more fish from tomorrow, then sends /menu to
    see
    what he is cooking tomorrow. There is nothing to tap in between."""
    payload = _renamed(proposal_for(menu_mod.Segment(TUE, FRI)), {"dinner": "Fish-night rice bowl"})
    home.gateways.append(FakeGateway([payload]))
    home.at(dt.datetime(2026, 9, 14, 20, 0, tzinfo=TZ))
    built = home.say(home.owner, "/cook replace more fish")
    assert "Tue to Fri" in built, built
    fish = _newest_menu(home.db, home.owner)
    assert menu_mod.decision_of(home.db, fish) == "accepted", "in force the moment it arrived"
    said = home.say(home.owner, "/menu")
    assert "Fish-night rice bowl" in said, (
        "he asked for Tue-Fri a moment ago and bare /menu cannot find it:\n" + said
    )


def _page(home, config, tmp_path) -> TestClient:
    """The tailnet page, over the same database file the bot writes, with the owner's login
    linked."""
    home.db.insert("tailscale_identities", person_id=home.owner, login=OWNER_LOGIN)
    config.data["web"]["secure_cookies"] = False
    config.data["web"]["root_path"] = ""
    same_db = dataclasses.replace(config, paths=dataclasses.replace(config.paths, db=home.db.path))
    app = App(same_db, Secrets(tmp_path / "s"), clock=home.clock, transport=FakeTelegram())
    return TestClient(create_app(app), base_url="http://testserver")


def test_09_the_page_still_shows_tonights_dinner_after_a_mid_week_change(
    home, config, tmp_path
) -> None:
    """Monday 20:10, the tailnet page open on the laptop in the kitchen. A Tue-Fri change came
    in ten minutes ago and is already in force; he opens the Menu tab to cook tonight's dinner."""
    fish = home.dish("Fish-night rice bowl", {"chicken": 200, "rice": 90})
    oats = home.dish("Oats with whey", {"oats": 80, "whey": 30})
    home.at(dt.datetime(2026, 9, 14, 20, 0, tzinfo=TZ))
    home.put_menu(
        home.owner,
        {
            TUE + dt.timedelta(days=i): {"breakfast": oats, "lunch": fish, "dinner": fish}
            for i in range(4)
        },
        accepted=True,
    )
    home.at(dt.datetime(2026, 9, 14, 20, 10, tzinfo=TZ))
    # the two GETs the Menu and Today tabs make, identity from the Tailscale header only
    page = _page(home, config, tmp_path)
    got = page.get("api/menu", headers={LOGIN_HEADER: OWNER_LOGIN})
    assert got.status_code == 200, got.text
    menu_json = got.json()
    today_json = page.get("api/today", headers={LOGIN_HEADER: OWNER_LOGIN}).json()
    dinner = [m["name"] for m in today_json["meals"] if m["slot"] == "dinner"]
    assert menu_json["menu"] is not None and dinner == ["Chicken, rice and broccoli"], (
        f"the Menu tab gets menu={menu_json['menu']} and Today's meals are {today_json['meals']}, "
        "while Monday's dinner from the Mon-Fri menu is what he is cooking tonight"
    )


def test_10_the_menu_built_last_is_the_menu_in_force(home) -> None:
    """Monday 13:00. She asked for a Tue-Wed rebuild at 09:00 and, not liking it, asked again at
    lunch. Two menus now cover Tuesday and only one of them can be real - and with no tap
    deciding it, "which one" is settled by which was built last, not by which was tapped last."""
    tuna = home.dish("Tuna pasta salad", {"chicken": 140, "rice": 70})
    yog = home.dish("Yoghurt and whey pot", {"yoghurt": 200, "whey": 20})
    home.at(dt.datetime(2026, 9, 14, 9, 0, tzinfo=TZ))
    home.put_menu(
        home.partner,
        {d: {"breakfast": yog, "lunch": tuna, "dinner": tuna, "snack": yog} for d in (TUE, WED)},
        accepted=True,
    )
    home.at(dt.datetime(2026, 9, 14, 13, 0, tzinfo=TZ))
    payload = _renamed(proposal_for(menu_mod.Segment(TUE, WED)), {"lunch": NEW_LUNCH})
    home.gateways.append(FakeGateway([payload]))
    home.say(home.partner, "/cook replace no chickpeas")
    tuesday = home.say(home.partner, "/menu tue")
    assert NEW_LUNCH in tuesday and "Tuna pasta salad" not in tuesday, (
        "Tuesday is still the menu she asked to be rid of four hours ago:\n" + tuesday
    )


def test_11_change_then_skip_keeps_the_menu_partner_is_holding(home) -> None:
    asked = home.tap(home.partner, f"k:menux:{home.partner_old}")
    assert "Tell me what to change" in asked, asked
    left = home.say(home.partner, "skip")
    assert "Kept the menu" in left, left
    assert menu_mod.accepted_menu_id(home.db, home.partner, TUE) == home.partner_old, (
        "skip must leave the menu she was holding in force"
    )
    shown = home.say(home.partner, "/menu tue")
    assert "no menu" not in shown.lower(), shown


def test_12_cook_no_chickpeas_then_accept_then_the_list_in_the_shop(home) -> None:
    """Monday, lunch then the shop. She sends /cook no chickpeas and at 17:30, in the shop,
    opens /shopping for the menu that came back - which is hers already, with nothing tapped."""
    home.gateways.append(FakeGateway([proposal_for(menu_mod.Segment(TUE, WED))]))
    built = home.say(home.partner, "/cook no chickpeas")
    new_menu = built.split("<b>Menu for the next cook</b>")[-1]
    assert "Tue to Wed" in new_menu and "chickpea" not in new_menu.lower(), built
    rebuild = _newest_menu(home.db, home.partner)
    assert menu_mod.decision_of(home.db, rebuild) == "accepted", "hers on arrival"
    home.at(dt.datetime(2026, 9, 14, 17, 30, tzinfo=TZ))
    said = home.say(home.partner, "/shopping")
    assert "Chickpeas" not in said, (
        "she accepted a chickpea-free menu and the list in her hand is the old one:\n" + said
    )
