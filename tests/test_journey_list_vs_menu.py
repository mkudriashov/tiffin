"""Twelve moments when the shopping list and the menu somebody cooks from might disagree.

Thu-Fri. Her Mon-Wed menu still has "Chicken, chickpea and feta salad" on it - built before she
asked to avoid it, and a fresh `/cook replace` is in force the moment it exists, never a tap on a
not need to click more buttons?"). Her Thu-Fri has no menu, and chickpeas are on her refused
list. Both lists went out at 18:00 on Sunday through the real job.
Earlier today, in a shop, the list and the menu disagreed and she got chickpeas after asking
three times not to.

Every story drives the real path (poller -> handlers -> menuflow, the scheduled jobs, the web
API) and reads what the PERSON was sent, from `messages_out`. The app's outbox is switched to
prod so her messages are really "sent" - test wiring, not a product change - which is what
makes the 18:00 idempotence and the "a list has already gone out" branch behave as they do in
production.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import types

import pytest
from fastapi.testclient import TestClient

from tiffin import scheduler as sc
from tiffin.app import App
from tiffin.clock import TZ, FrozenClock, isoday
from tiffin.db import Db
from tiffin.food import menu as menu_mod
from tiffin.food import shopping
from tiffin.jobs import shopping_list
from tiffin.secrets import Secrets
from tiffin.telegram.poller import Poller
from tiffin.web.app import create_app

from .fakes import FakeTelegram
from .test_food_shopping import seed_ingredient
from .test_journey_menu_changes import _profile, dish, put_menu
from .test_menu import FakeGateway, proposal_for, seed_catalogue

OWNER_CHAT = 1000001
PARTNER_CHAT = 1000002
LOGIN_HEADER = "Tailscale-User-Login"
OWNER_LOGIN = "alex@example.com"

FRI_BEFORE = dt.date(2026, 9, 11)
SAT_BEFORE = dt.date(2026, 9, 12)
SUN = dt.date(2026, 9, 13)
MON = dt.date(2026, 9, 14)
TUE = dt.date(2026, 9, 15)
WED = dt.date(2026, 9, 16)
THU = dt.date(2026, 9, 17)
FRI = dt.date(2026, 9, 18)

CHICKPEA_SALAD = "Chicken, chickpea and feta salad"
NEW_LUNCH = "Chicken, kidney bean and pepper wrap"


def at(clock: FrozenClock, day: dt.date, hh: int, mm: int = 0) -> None:
    clock.set_local(dt.datetime(day.year, day.month, day.day, hh, mm, tzinfo=TZ))


@pytest.fixture
def house(fixture_db: Db, two_persons, frozen_clock: FrozenClock, config, tmp_path):
    db = fixture_db
    his, partner = two_persons["owner"], two_persons["partner"]
    at(frozen_clock, SUN, 10)
    _profile(db, his, [[0, 1, 2, 3, 4]], "same", [])
    _profile(db, partner, [[0, 1, 2], [3, 4]], "rotate", ["chickpea"])
    db.insert("consents", person_id=partner, granted=1, wording_version=1)
    seed_catalogue(db)
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
    wrap = dish(db, NEW_LUNCH, {"chicken": 150, "rice": 50})
    chilli = dish(db, "Beef and kidney bean chilli with brown rice", {"chicken": 160, "rice": 80})
    old_days = {
        MON: {"breakfast": porridge, "lunch": salad, "dinner": curry, "snack": yog},
        TUE: {"breakfast": eggs, "lunch": tuna, "dinner": curry, "snack": yog},
        WED: {"breakfast": porridge, "lunch": salad, "dinner": chilli, "snack": yog},
    }
    new_days = {
        d: {"breakfast": porridge, "lunch": wrap, "dinner": chilli, "snack": yog}
        for d in (MON, TUE, WED)
    }
    partner_old = put_menu(db, partner, old_days, accepted=True)
    oats = dish(db, "Oats with whey", {"oats": 80, "whey": 30})
    rice = dish(db, "Chicken, rice and broccoli", {"chicken": 220, "rice": 90, "broccoli": 200})
    his_days = {
        MON + dt.timedelta(days=i): {"breakfast": oats, "lunch": rice, "dinner": rice, "snack": yog}
        for i in range(5)
    }
    his_menu = put_menu(db, his, his_days, status="repeated")

    def partner_replacement() -> int:
        """A fresh, chickpea-free Mon-Wed menu, in force the moment it is built and sent - what
        Accept ... we do not need to click more buttons?"). Built on demand, not at fixture
        setup, so a story that still needs the ORIGINAL chickpea salad current (the ordinary
        case: nothing has replaced it yet) is not shadowed by a second menu sitting unused.
        """
        return put_menu(db, partner, new_days, accepted=True)

    config.data.setdefault("chrome", {})["shop_fill_enabled"] = False
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
    dev_outbox = app.outbox

    def prod_outbox(d: Db):
        box = dev_outbox(d)
        box.env = "prod"  # her messages really go, as they do in production
        return box

    app.outbox = prod_outbox  # type: ignore[method-assign]
    handlers = app.handlers(db)
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

    def run_job(job: sc.Job, day: dt.date, pid: int) -> str:
        scheduler = sc.Scheduler(app, [job], lambda p, fn: None)
        prof = db.one("SELECT * FROM profiles WHERE person_id = ? ORDER BY id DESC LIMIT 1", (pid,))
        slots = sc.slots_for(job, pid, prof, day, db)
        assert slots, f"no slot for {job.name} on {day}"
        return scheduler.execute(db, slots[0])

    def sunday_lists() -> None:
        """Sunday 18:00: the real shopping_list job, for both of them."""
        at(frozen_clock, SUN, 18)
        assert run_job(shopping_list.JOB, SUN, partner) == "ok"
        assert run_job(shopping_list.JOB, SUN, his) == "ok"

    def list_for(menu_id: int) -> int | None:
        found = db.scalar(
            "SELECT id FROM shopping_lists WHERE menu_id = ? AND kind = 'segment' "
            "ORDER BY id DESC LIMIT 1",
            (menu_id,),
        )
        return None if found is None else int(found)

    return types.SimpleNamespace(
        db=db,
        clock=frozen_clock,
        tg=tg,
        app=app,
        gateways=gateways,
        owner=his,
        partner=partner,
        partner_old=partner_old,
        partner_replacement=partner_replacement,
        his_menu=his_menu,
        dishes={"porridge": porridge, "yog": yog, "rice": rice, "oats": oats, "tuna": tuna},
        say=say,
        tap=tap,
        run_job=run_job,
        newest=newest,
        said_since=said_since,
        sunday_lists=sunday_lists,
        list_for=list_for,
    )


def _page(house, config, tmp_path) -> TestClient:
    """The tailnet page over the same database, with the owner's login linked."""
    house.db.insert("tailscale_identities", person_id=house.owner, login=OWNER_LOGIN)
    config.data["web"]["secure_cookies"] = False
    config.data["web"]["root_path"] = ""
    same_db = dataclasses.replace(config, paths=dataclasses.replace(config.paths, db=house.db.path))
    app = App(same_db, Secrets(tmp_path / "s"), clock=house.clock, transport=FakeTelegram())
    return TestClient(create_app(app), base_url="http://testserver")


def _item_names(db: Db, list_id: int) -> list[str]:
    return [
        str(r["name"])
        for r in db.all(
            "SELECT i.name FROM shopping_items si JOIN ingredients i ON i.id = si.ingredient_id "
            "WHERE si.list_id = ?",
            (list_id,),
        )
    ]


def _sent(db: Db, pid: int, kind: str, due_key: str) -> str:
    row = db.one(
        "SELECT text_html FROM messages_out WHERE person_id = ? AND kind = ? AND due_key = ? "
        "ORDER BY id DESC LIMIT 1",
        (pid, kind, due_key),
    )
    return "" if row is None else str(row["text_html"])


# ---------------------------------------------------------------- the stories
def test_01_the_list_partner_rereads_in_the_shop_is_the_one_sent_last_night(house) -> None:
    """Monday 12:00, in the shop. Last night's 18:00 list has scrolled away; she sends /shopping
    and needs the same list, for the same days, to walk round with."""
    house.sunday_lists()
    sent = _sent(house.db, house.partner, "shopping_list", isoday(MON))
    assert sent.startswith("<b>Shopping for Mon 14 Sep</b>"), sent
    at(house.clock, MON, 12)
    reread = house.say(house.partner, "/shopping")
    assert "Mon 14 to Wed 16 Sep" in reread, reread
    list_id = house.list_for(house.partner_old)
    assert list_id is not None
    for name in _item_names(house.db, list_id):
        assert name in sent and name in reread, f"{name} on one surface and not the other"
    assert (
        house.db.scalar(
            "SELECT COUNT(*) FROM shopping_lists WHERE menu_id = ?", (house.partner_old,)
        )
        == 1
    ), "rereading did not build a second list with a second leftover credit"


def test_02_the_list_says_the_chickpeas_on_it_are_a_food_partner_avoids(house) -> None:
    """Monday 12:00, standing at the tins. Chickpeas are on her refused list and the accepted menu
    still cooks them; /menu warns her. She needs the list in her hand to warn her before she
    puts the tin in the basket, not after."""
    house.sunday_lists()
    at(house.clock, MON, 12)
    menu_says = house.say(house.partner, "/menu")
    assert "which you now avoid" in menu_says, "the menu surface already knows"
    listed = house.say(house.partner, "/shopping")
    assert "Chickpeas" in listed, "the tin is on the list because the accepted menu cooks it"
    assert "avoid" in listed.lower() or "/cook replace" in listed, (
        "the menu says 'This menu still has Chickpeas in it, which you now avoid' and the list "
        f"built from the same menu tells her to buy a tin with no word about it:\n{listed}"
    )


def test_03_after_a_replacement_the_reread_list_says_what_changed(house) -> None:
    """Monday 12:05, halfway round the shop with last night's list. A fresh `/cook replace`
    puts the chickpea-free menu in force the instant it is built, not after a tap that no
    to click more buttons?"). She checks /shopping and needs to know which of the things
    already in her basket are no longer wanted and what is new.
    """
    house.sunday_lists()
    at(house.clock, MON, 12, 5)
    house.partner_replacement()
    listed = house.say(house.partner, "/shopping")
    assert "Chickpeas" not in listed.split("Shelf empty?")[0], "the new list has none"
    words = ("no longer", "dropped", "instead", "changed", "replaces", "new menu", "not needed")
    assert any(w in listed.lower() for w in words), (
        "two lists for 'Mon 14 to Wed 16 Sep' now sit in her chat; the reread one has the same "
        "header shape as any list and says nothing about the one it replaces, so she cannot tell "
        f"that the chickpeas already in her basket are no longer on it:\n{listed}"
    )


def test_04_what_partner_already_ticked_is_not_asked_for_again(house) -> None:
    """Monday 17:30, in the shop. She has put the chicken in the trolley and ticked it, then
    from the aisle a fresh `/cook replace` puts the chickpea-free menu in force. The chicken is
    still in the trolley; the list she rereads must not tell her to pick up the chicken again."""
    house.sunday_lists()
    at(house.clock, MON, 17, 30)
    house.say(house.partner, "/shopping")
    old_list = house.list_for(house.partner_old)
    assert old_list is not None
    chicken_item = int(
        house.db.scalar(
            "SELECT si.id FROM shopping_items si JOIN ingredients i ON i.id = si.ingredient_id "
            "WHERE si.list_id = ? AND i.code = 'chicken'",
            (old_list,),
        )
    )
    house.tap(house.partner, f"k:tick:{chicken_item}")
    ticked = house.say(house.partner, "/shopping")
    assert any(ln.startswith("x Chicken breast") for ln in ticked.splitlines()), ticked
    at(house.clock, MON, 17, 40)
    house.partner_replacement()
    after = house.say(house.partner, "/shopping")
    chicken_lines = [ln for ln in after.splitlines() if "Chicken breast" in ln]
    assert chicken_lines, after
    assert all(ln.startswith("x ") for ln in chicken_lines), (
        "the chicken she ticked ten minutes ago is back on the list, unticked, at full packs: the "
        "rebuilt list starts from nothing and the trolley does not exist to it:\n" + after
    )


def test_05_a_peek_at_the_list_does_not_make_the_next_list_buy_less(house) -> None:
    """Sunday 15:00 he looks at /shopping, does not shop, and by 16:00 a different Mon-Fri menu
    is already in force - decided the moment it is built and sent, not by a later Accept tap
    what that menu eats - nothing was bought from the list he only looked at.
    """
    at(house.clock, SUN, 15)
    peek = house.say(house.owner, "/shopping")
    assert "Chicken breast" in peek, peek
    at(house.clock, SUN, 16)
    traybake = dish(house.db, "Chicken and broccoli traybake", {"chicken": 250, "broccoli": 150})
    new = put_menu(
        house.db,
        house.owner,
        {
            MON + dt.timedelta(days=i): {
                "breakfast": house.dishes["oats"],
                "lunch": traybake,
                "dinner": traybake,
                "snack": house.dishes["yog"],
            }
            for i in range(5)
        },
        accepted=True,
    )
    at(house.clock, SUN, 18)
    assert house.run_job(shopping_list.JOB, SUN, house.owner) == "ok"
    sent = _sent(house.db, house.owner, "shopping_list", isoday(MON))
    items = shopping.for_menu(house.db, new)
    assert items, "a list was built for the menu he accepted"
    credited = {i.name: i.credit_g for i in items if i.credit_g > 0}
    assert not credited, (
        "the 18:00 list for the menu he accepted counts food 'already in' that only exists "
        "because the 15:00 peek at the replaced menu's list wrote planned leftovers; nothing was "
        f"bought, so the week is short by exactly that. Credited {credited}; he read:\n{sent}"
    )


def test_06_asking_early_and_the_18_00_send_are_one_list(house) -> None:
    """Sunday 12:00 he asks /shopping to plan the trip; at 18:00 the scheduled list arrives. He
    needs the two to be the same list with the same packs, not a second build that credits the
    first one's leftovers."""
    at(house.clock, SUN, 12)
    early = house.say(house.owner, "/shopping")
    list_id = house.list_for(house.his_menu)
    assert list_id is not None
    packs_before = house.db.all(
        "SELECT ingredient_id, packs FROM shopping_items WHERE list_id = ? ORDER BY id", (list_id,)
    )
    at(house.clock, SUN, 18)
    assert house.run_job(shopping_list.JOB, SUN, house.owner) == "ok"
    sent = _sent(house.db, house.owner, "shopping_list", isoday(MON))
    assert house.list_for(house.his_menu) == list_id, "the 18:00 job reused the early list"
    assert (
        house.db.scalar("SELECT COUNT(*) FROM shopping_lists WHERE menu_id = ?", (house.his_menu,))
        == 1
    )
    packs_after = house.db.all(
        "SELECT ingredient_id, packs FROM shopping_items WHERE list_id = ? ORDER BY id", (list_id,)
    )
    assert [tuple(r) for r in packs_before] == [tuple(r) for r in packs_after]
    for name in _item_names(house.db, list_id):
        assert name in early and name in sent, name


def test_07_the_page_in_the_shop_on_sunday_shows_the_week_ahead(house, config, tmp_path) -> None:
    """Sunday 11:00, in the shop with the tailnet page open on his phone. He shops Sunday for
    Monday-to-Friday. The list he needs is the one for the week ahead, as /shopping gives him,
    not Friday's weekend list for the two days that are nearly over."""
    at(house.clock, FRI_BEFORE, 18)
    weekend = put_menu(
        house.db,
        house.owner,
        {d: {"dinner": house.dishes["rice"]} for d in (SAT_BEFORE, SUN)},
        accepted=True,
    )
    shopping.joint_weekend(house.db, [weekend], clock=house.clock)
    at(house.clock, SUN, 11)
    page = _page(house, config, tmp_path)
    got = page.get("api/shopping", headers={LOGIN_HEADER: OWNER_LOGIN})
    assert got.status_code == 200, got.text
    on_page = got.json()["list"]
    in_chat = house.say(house.owner, "/shopping")
    assert "Mon 14 to Fri 18 Sep" in in_chat, "the bot already answers with the week ahead"
    assert on_page is not None and on_page["menu_id"] == house.his_menu, (
        f"the page shows list {on_page} - Friday's {on_page and on_page['kind']} list for "
        f"Sat 12-Sun 13 - while the bot, asked the same thing a second later, shows Mon 14 to "
        "Fri 18. Standing in one shop, two surfaces, two different lists."
    )


def test_08_the_page_shows_the_list_for_the_menu_it_shows(house, config, tmp_path) -> None:
    """Sunday 20:30. The 18:00 list went out; by 20:00 a different Mon-Fri menu is already in
    force - decided the moment it is built and sent, not by a later Accept tap (the owner,
    about the same menu, or he buys for food he is not cooking.
    """
    house.sunday_lists()
    at(house.clock, SUN, 20)
    traybake = dish(house.db, "Salmon-style chicken traybake", {"chicken": 250, "broccoli": 150})
    new = put_menu(
        house.db,
        house.owner,
        {
            MON + dt.timedelta(days=i): {
                "breakfast": house.dishes["oats"],
                "lunch": traybake,
                "dinner": traybake,
                "snack": house.dishes["yog"],
            }
            for i in range(5)
        },
        accepted=True,
    )
    at(house.clock, SUN, 20, 30)
    page = _page(house, config, tmp_path)
    menu_json = page.get(
        "api/menu", params={"segment": isoday(MON)}, headers={LOGIN_HEADER: OWNER_LOGIN}
    ).json()
    shop_json = page.get("api/shopping", headers={LOGIN_HEADER: OWNER_LOGIN}).json()
    assert menu_json["menu"] is not None and menu_json["menu"]["id"] == new, menu_json["menu"]
    listed_for = shop_json["list"] and shop_json["list"]["menu_id"]
    assert listed_for == new, (
        f"the Menu tab shows menu {new} (the traybake he accepted at 20:00) and the Shopping tab "
        f"shows the list for menu {listed_for}, the one it replaced. The page never builds a "
        "list for the menu it shows; only the bot's /shopping does."
    )


def test_09_on_wednesday_evening_shopping_says_thursday_has_no_list(house) -> None:
    """Wednesday 19:00, on the way home. Her Thu-Fri cook is tomorrow and has no menu. She sends
    /shopping to see what to buy for Thursday and needs to be told there is nothing to buy from
    yet and why - not handed the Mon-Wed list as if it were the answer."""
    house.sunday_lists()
    at(house.clock, WED, 19)
    listed = house.say(house.partner, "/shopping")
    assert "Thu" in listed or "/cook" in listed, (
        "asked on the evening before her Thu-Fri cook, /shopping falls back to the newest list of "
        "any kind and shows the Mon-Wed list she has already shopped and nearly eaten, with no "
        f"word that Thursday has no menu:\n{listed}"
    )


def test_10_at_18_00_on_wednesday_partner_hears_that_thursday_has_no_list(house) -> None:
    """Wednesday 18:00, when a list would arrive for tomorrow's cook. Her Thu-Fri has no menu,
    so there is nothing to shop for. She needs to be told that, and what to do, instead of the
    list simply not coming."""
    house.sunday_lists()
    at(house.clock, WED, 18)
    before = house.newest()
    status = house.run_job(shopping_list.JOB, WED, house.partner)
    detail = house.db.scalar(
        "SELECT detail FROM run_ledger WHERE job = 'shopping_list' ORDER BY rowid DESC LIMIT 1"
    )
    told_partner = house.said_since(house.partner, before)
    told_owner = house.said_since(house.owner, before)
    assert told_partner, (
        f"the 18:00 job ended {status!r} ({detail!r}) and wrote nothing to her - and nothing to "
        f"Alex either ({told_owner!r}). Degrade to manual, never to silence: the list she expects "
        "tonight simply does not come, and she finds out in the shop."
    )


def test_11_a_thursday_build_is_in_force_at_once_never_a_repeat_of_chickpeas(house) -> None:
    house.sunday_lists()
    at(house.clock, TUE, 18)
    fresh = {
        d: {
            "breakfast": house.dishes["porridge"],
            "lunch": house.dishes["tuna"],
            "dinner": house.dishes["rice"],
            "snack": house.dishes["yog"],
        }
        for d in (THU, FRI)
    }
    put_menu(house.db, house.partner, fresh, accepted=True)
    at(house.clock, WED, 18)
    assert house.run_job(shopping_list.JOB, WED, house.partner) == "ok"
    sent = _sent(house.db, house.partner, "shopping_list", isoday(THU))
    assert sent.startswith("<b>Shopping for Thu 17 Sep</b>"), sent
    assert "Chickpeas" not in sent, f"the list buys no tin:\n{sent}"
    thursday = menu_mod.meals_for_day(house.db, house.partner, THU)
    assert CHICKPEA_SALAD not in thursday, f"Thursday is {thursday}, not the chickpea salad"


def test_12_a_mid_week_cook_replace_gets_a_list_for_the_new_days(house) -> None:
    """Tuesday 12:00 he sends /cook replace and a new Wed-Fri menu arrives already in force -
    there this bullshit with Accept ... we do not need to click more buttons?"). On the way home
    he shops for it. He needs a list for the menu he will cook tomorrow - by 18:00, or when he
    asks /shopping - not the Sunday list for the menu he just replaced.
    """
    house.sunday_lists()
    at(house.clock, TUE, 12)
    fresh = proposal_for(menu_mod.Segment(WED, FRI))
    for day in fresh["days"]:
        # Monday's and Tuesday's dinner is never a dinner again within six days (menu.py)
        day["slots"][2]["recipe"]["name"] = "Chicken and broccoli traybake"
    house.gateways.append(FakeGateway([fresh]))
    built = house.say(house.owner, "/cook replace")
    new = int(house.db.scalar("SELECT MAX(id) FROM menus WHERE person_id = ?", (house.owner,)))
    row = house.db.one("SELECT segment_start, segment_end FROM menus WHERE id = ?", (new,))
    assert row is not None and str(row["segment_start"]) == isoday(WED), dict(row or {})
    assert menu_mod.decision_of(house.db, new) == "accepted", "in force the moment it was built"
    at(house.clock, TUE, 18)
    before = house.newest()
    house.run_job(shopping_list.JOB, TUE, house.owner)
    evening = house.said_since(house.owner, before)
    at(house.clock, TUE, 19)
    listed = house.say(house.owner, "/shopping")
    assert "Wed 16" in listed.splitlines()[0] or "Wed 16" in evening, (
        f"the build said {built!r}; at 18:00 he was sent {evening!r}; and /shopping answers "
        f"with {listed.splitlines()[0]!r} - the Sunday list for the Mon-Fri menu he replaced. "
        "No list is ever built for the Wed-Fri menu he will cook tomorrow."
    )
