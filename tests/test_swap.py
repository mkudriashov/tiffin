"""Changing food without waiting: a refusal said the way people say it, kept, shown, and acted on;
one dish swapped for another already eaten, in code, in a second.

"""

from __future__ import annotations

import datetime as dt
import json

import pytest
from fastapi.testclient import TestClient

from tiffin.app import App
from tiffin.clock import FrozenClock, isoday
from tiffin.db import Db
from tiffin.errors import NothingToBuild
from tiffin.food import catalogue, menu, prefs, swap
from tiffin.secrets import Secrets
from tiffin.web.app import create_app

from .fakes import FakeTelegram
from .test_food_shopping import seed_ingredient, seed_recipe
from .test_review_0926_weekend import pair  # noqa: F401

PARTNER_LOGIN = "sam@example.com"
OWNER_LOGIN = "alex@example.com"

MON = dt.date(2026, 9, 14)  # the frozen clock is Monday 7 Sep, so next week is still to come
FRI = dt.date(2026, 9, 18)
LAST_MON = dt.date(2026, 8, 24)  # two weeks of history, both over by the frozen Monday

FOODS = {
    #          name, aisle, kcal, protein, fat, carb, fibre per 100 g
    "oats": ("Porridge oats", "grains", 379, 13, 8, 60, 10),
    "yoghurt": ("Greek yoghurt 0% fat", "dairy", 57, 10, 0.4, 4, 0),
    "chicken": ("Chicken breast, raw", "meat", 120, 24, 2, 0, 0),
    "salmon": ("Salmon fillets, raw", "fish", 200, 20, 13, 0, 0),
    "rice": ("Basmati rice, dry", "grains", 350, 8, 1, 78, 1),
    "lentils": ("Red lentils, dry", "grains", 318, 24, 1.3, 50, 11),
    "chickpeas": ("Chickpeas, tinned, drained", "tins", 115, 7, 2.6, 13, 4.8),
    "broccoli": ("Broccoli, raw", "produce", 35, 3.8, 0.4, 2.2, 3),
    "pepper": ("Red pepper, raw", "produce", 30, 1, 0.3, 6, 1.6),
    "black_pepper": ("Black pepper, ground", "condiments", 255, 11, 3, 39, 25),
    "prawns": ("King prawns, raw", "fish", 70, 16, 0.6, 0, 0),
}


def _profile(db: Db, pid: int, **kw) -> None:
    base = {
        "person_id": pid,
        "effective_from": "2026-08-01",
        "sex": "f",
        "birth_date": "1992-05-02",
        "height_cm": 168.0,
        "activity_factor": 1.4,
        "kcal_floor": 1200,
        "meals_per_day": 3,
        "segment_days": json.dumps([[0, 1, 2, 3, 4]]),
        "fibre_g_min": 10,
        "batch_slots": json.dumps(["lunch", "dinner"]),
    }
    base.update(kw)
    db.insert("profiles", **base)


def _menu(db: Db, pid: int, start: dt.date, days: int, slots: dict[str, int]) -> int:
    """An accepted menu with real per-slot numbers, the same dishes every day at x1.0."""
    units = catalogue.units(db)
    from tiffin.food import recipes

    mid = db.insert(
        "menus",
        person_id=pid,
        segment_start=isoday(start),
        segment_end=isoday(start + dt.timedelta(days=days - 1)),
        status="accepted",
    )
    for i in range(days):
        day = start + dt.timedelta(days=i)
        each = {
            s: recipes.stored_per_serving(db, r, 1.0, units).as_macros() for s, r in slots.items()
        }
        did = db.insert(
            "menu_days",
            menu_id=mid,
            day=isoday(day),
            kcal=sum(m.kcal for m in each.values()),
            protein_g=sum(m.protein_g for m in each.values()),
            fat_g=sum(m.fat_g for m in each.values()),
            carb_g=sum(m.carb_g for m in each.values()),
            fibre_g=sum(m.fibre_g for m in each.values()),
        )
        for slot, rid in slots.items():
            m = each[slot]
            db.insert(
                "meal_slots",
                menu_day_id=did,
                slot=slot,
                recipe_id=rid,
                portion_multiplier=1.0,
                portion_g=300,
                kcal=m.kcal,
                protein_g=m.protein_g,
                fat_g=m.fat_g,
                carb_g=m.carb_g,
                fibre_g=m.fibre_g,
            )
    return mid


@pytest.fixture
def week(fixture_db: Db, two_persons, frozen_clock):
    """The partner's Mon-Fri next week: porridge, a lentil dal for lunch, chicken and rice for
    dinner.
    Two weeks ago she ate a salmon traybake and a chickpea salad for lunch, and a chicken
    stir-fry she rated 1 for dinner."""
    db, pid = fixture_db, two_persons["partner"]
    base, r = _seed(db, pid)
    return db, pid, base, r, frozen_clock


def _seed(db: Db, pid: int) -> tuple[int, dict[str, int]]:
    _profile(db, pid)
    db.insert(
        "targets",
        person_id=pid,
        week_start=isoday(MON),
        kcal=1300,
        protein_g=90,
        fat_g=25,
        carb_g=150,
        fibre_g=20,
        source="formula",
    )
    ids = {
        code: seed_ingredient(
            db,
            code,
            name,
            aisle,
            500.0,
            "500 g pack",
            kcal_100=k,
            protein_100=p,
            fat_100=f,
            carb_100=c,
            fibre_100=fb,
        )
        for code, (name, aisle, k, p, f, c, fb) in FOODS.items()
    }
    g = ids.__getitem__
    r = {
        "porridge": seed_recipe(db, "Porridge with yoghurt", 1, {g("oats"): 60, g("yoghurt"): 150}),
        "dal": seed_recipe(
            db, "Lentil dal", 1, {g("lentils"): 80, g("broccoli"): 150, g("pepper"): 80}
        ),
        "chicken_rice": seed_recipe(
            db, "Chicken and rice", 1, {g("chicken"): 180, g("rice"): 70, g("broccoli"): 100}
        ),
        "salmon": seed_recipe(
            db, "Salmon traybake", 1, {g("salmon"): 140, g("rice"): 60, g("pepper"): 100}
        ),
        "chickpea": seed_recipe(
            db,
            "Chickpea salad",
            1,
            {g("chickpeas"): 200, g("chicken"): 100, g("pepper"): 100, g("black_pepper"): 1},
        ),
        "stirfry": seed_recipe(
            db, "Chicken stir-fry", 1, {g("chicken"): 180, g("rice"): 60, g("pepper"): 80}
        ),
    }
    # history: what she has eaten before, and what she thought of it
    _menu(
        db,
        pid,
        LAST_MON,
        5,
        {"breakfast": r["porridge"], "lunch": r["salmon"], "dinner": r["chickpea"]},
    )
    _menu(
        db,
        pid,
        LAST_MON + dt.timedelta(days=7),
        5,
        {"breakfast": r["porridge"], "lunch": r["chickpea"], "dinner": r["stirfry"]},
    )
    db.insert("ratings", person_id=pid, recipe_id=r["stirfry"], score=1, note=None)
    db.insert("ratings", person_id=pid, recipe_id=r["salmon"], score=5, note=None)
    # next week, in force
    base = _menu(
        db,
        pid,
        MON,
        5,
        {"breakfast": r["porridge"], "lunch": r["dal"], "dinner": r["chicken_rice"]},
    )
    return base, r


# ---------------------------------------------------------------- said the way people say it
@pytest.mark.parametrize(
    ("said", "expect"),
    [
        ("I do not want lentils and chickpeas", ["lentils", "chickpeas"]),
        ("I don't want lentils or chickpeas anymore", ["lentils", "chickpeas"]),
        ("I don" + chr(0x2019) + "t like chickpeas", ["chickpeas"]),  # a phone's apostrophe
        ("no more lentils please", ["lentils"]),
        ("I hate mushrooms", ["mushrooms"]),
        ("not keen on tuna", ["tuna"]),
        ("Sam does not want lentils", ["lentils"]),
        ("stop giving me tuna", ["tuna"]),
        ("I can't stand coriander", ["coriander"]),
        ("she's allergic to peanuts", ["peanuts"]),
        ("I won't eat liver", ["liver"]),
    ],
)
def test_a_refusal_said_in_a_sentence_names_the_foods(said: str, expect: list[str]) -> None:
    """Her exact words came back as ["want lentils", "chickpeas"]: "want lentils" names no
    food, so the lentils were dropped without a word and only the chickpeas were kept off."""
    assert catalogue.avoid_terms(said) == expect


@pytest.mark.parametrize("said", ["not so much rice", "not too spicy", "less rice", "more fish"])
def test_less_of_a_food_is_not_a_refusal_of_it(said: str) -> None:
    """ "Not so much rice" is less rice: it may come back as words, never as "rice"."""
    assert "rice" not in catalogue.avoid_terms(said)
    assert "fish" not in catalogue.avoid_terms(said)


def _ing(name: str, aisle: str) -> catalogue.Ingredient:
    return catalogue.Ingredient(
        id=0,
        code="x",
        version=1,
        name=name,
        cofid_code=None,
        kcal_100=0,
        protein_100=0,
        fat_100=0,
        carb_100=0,
        fibre_100=0,
        pack_g=1,
        pack_label="",
        aisle=aisle,
        pantry=False,
        use_whole_pack=False,
        approved=True,
        person_scope="both",
    )


@pytest.mark.parametrize(
    ("term", "name", "aisle", "hit"),
    [
        ("fish", "Salmon fillets, raw", "fish", True),
        ("fish", "Tuna in brine, drained", "tins", True),
        ("fish", "King prawns, raw", "fish", False),
        ("seafood", "King prawns, raw", "fish", True),
        ("dairy", "Cheddar cheese", "dairy", True),
        ("dairy", "Eggs, whole, raw", "dairy", False),
        ("dairy", "Peanut butter, smooth", "condiments", False),
        ("red meat", "Beef mince 5% fat, raw", "meat", True),
        ("red meat", "Chicken breast, raw", "meat", False),
        ("pulses", "Chickpeas, tinned, drained", "tins", True),
        ("pulses", "Green beans, raw", "produce", False),
        ("peppers", "Black pepper, ground", "condiments", False),
        ("peppers", "Red pepper, raw", "produce", True),
        ("milk", "Coconut milk, tinned", "tins", False),
        ("cheese", "Halloumi", "dairy", True),
    ],
)
def test_a_kind_of_food_keeps_out_what_it_means(term, name, aisle, hit) -> None:
    """ "No fish" was written down, confirmed and matched nothing: no food is called "fish"."""
    assert catalogue.refused_matches(catalogue.normalise(term), _ing(name, aisle)) is hit


# ---------------------------------------------------------------- the list, kept and shown
def test_avoiding_says_what_it_keeps_out_and_refuses_what_names_nothing(week) -> None:
    db, pid, _base, _r, clock = week
    got = prefs.amend(db, pid, "avoid", add=["lentils", "chickpeas", "faff"], clock=clock)
    assert got.added == ("lentils", "chickpeas")
    assert got.unknown == ("faff",), "a word that names no food is not written"
    assert got.hides["lentils"] == ("Red lentils",)
    said = got.said()
    assert "lentils (Red lentils)" in said and "'faff'" in said
    assert catalogue.refused_terms(db, pid) == ["chickpeas", "lentils"], "sorted, as /set shows"
    codes = {i.code for i in catalogue.approved(db, pid)}
    assert "lentils" not in codes and "chickpeas" not in codes, "off the list the model numbers"


def test_avoiding_again_keeps_one_entry_and_a_narrower_one_is_already_covered(week) -> None:
    db, pid, _base, _r, clock = week
    prefs.amend(db, pid, "avoid", add=["fish"], clock=clock)
    again = prefs.amend(db, pid, "avoid", add=["Fish", "salmon"], clock=clock)
    assert again.added == () and set(again.already) == {"Fish", "salmon"}
    assert not again.changed
    assert catalogue.refused_terms(db, pid) == ["fish"]


def test_allowing_back_names_the_entry_that_still_keeps_it_off(week) -> None:
    db, pid, _base, _r, clock = week
    prefs.amend(db, pid, "avoid", add=["fish", "lentils"], clock=clock)
    got = prefs.amend(db, pid, "avoid", remove=["salmon", "lentil"], clock=clock)
    assert got.removed == ("lentils",), "singular and plural are the one entry"
    assert got.kept_by == {"salmon": "fish"}
    assert "kept off by 'fish'" in got.said()
    assert catalogue.refused_terms(db, pid) == ["fish"]


def test_the_list_is_a_ledger(week) -> None:
    db, pid, _base, _r, clock = week
    before = db.scalar("SELECT COUNT(*) FROM profiles WHERE person_id = ?", (pid,))
    prefs.amend(db, pid, "avoid", add=["lentils"], clock=clock)
    prefs.amend(db, pid, "avoid", add=["lentils"], clock=clock)  # nothing new: nothing written
    after = db.scalar("SELECT COUNT(*) FROM profiles WHERE person_id = ?", (pid,))
    assert after == before + 1


def test_the_menu_still_holding_an_avoided_food_is_named_by_dish(week) -> None:
    db, pid, _base, _r, clock = week
    prefs.amend(db, pid, "avoid", add=["lentils"], clock=clock)
    still = prefs.still_on_menu(db, pid, clock.today_local())
    assert len(still) == 1
    assert still[0]["name"] == "Lentil dal" and still[0]["slot"] == "lunch"
    assert still[0]["days"] == [isoday(MON + dt.timedelta(days=i)) for i in range(5)]
    assert still[0]["holds"] == ["Red lentils"]


# ---------------------------------------------------------------- one dish, swapped in code
def test_the_dishes_offered_are_partners_safe_and_not_ones_partner_never_wants_again(week) -> None:
    db, pid, _base, _r, clock = week
    prefs.amend(db, pid, "avoid", add=["chickpeas"], clock=clock)
    offered = swap.candidates(db, pid, MON, "lunch", clock.today_local())
    names = [c.name for c in offered]
    assert names[0] == "Salmon traybake", "the dish she rated 5 comes first"
    assert "Chickpea salad" not in names, "it holds a food she avoids"
    assert "Chicken stir-fry" not in names, "she rated it 1"
    assert "Lentil dal" not in names, "it is the dish going out"
    assert "Chicken and rice" not in names, "already on the day, at dinner"
    assert all(c.kcal > 0 and c.protein_g > 0 for c in offered), "numbers from code"


def test_a_swap_puts_the_dish_in_on_every_day_it_was_on_and_the_day_still_adds_up(
    week, config
) -> None:
    db, pid, base, r, clock = week
    got = swap.swap(db, pid, MON, "lunch", r["salmon"], config=config, clock=clock)
    assert got.days == tuple(MON + dt.timedelta(days=i) for i in range(5))
    for d in got.days:
        names = [p.name for p in swap.plates_on(db, pid, d)]
        assert names == ["Porridge with yoghurt", "Salmon traybake", "Chicken and rice"]
        total = got.totals[d]
        assert abs(total.kcal - 1300) <= 1300 * 0.05, "the day stays on its calories"
    assert menu.segment_menu_id(db, pid, MON) == got.menu_id, (
        "the shopping list and the batch plan read the new menu"
    )
    assert (
        db.scalar(
            "SELECT COUNT(*) FROM meal_slots WHERE menu_day_id IN "
            "(SELECT id FROM menu_days WHERE menu_id = ?)",
            (base,),
        )
        == 15
    ), "the old menu is left as it was: a ledger"
    said = got.said()
    assert "Salmon traybake in for Lentil dal" in said and "Mon to Fri" in said


def test_a_swap_can_be_for_one_day_only(week, config) -> None:
    db, pid, _base, r, clock = week
    tue = MON + dt.timedelta(days=1)
    got = swap.swap(db, pid, tue, "lunch", r["salmon"], every_day=False, config=config, clock=clock)
    assert got.days == (tue,)
    assert [p.name for p in swap.plates_on(db, pid, MON)][1] == "Lentil dal"
    assert [p.name for p in swap.plates_on(db, pid, tue)][1] == "Salmon traybake"
    assert [p.name for p in swap.plates_on(db, pid, FRI)][1] == "Lentil dal"


def test_a_dish_not_on_offer_is_refused(week, config) -> None:
    """A recipe id is never taken on trust: one she rated 1, or one holding a food she avoids."""
    db, pid, _base, r, clock = week
    with pytest.raises(ValueError, match="not one of"):
        swap.swap(db, pid, MON, "lunch", r["stirfry"], config=config, clock=clock)
    prefs.amend(db, pid, "avoid", add=["chickpeas"], clock=clock)
    with pytest.raises(ValueError, match="not one of"):
        swap.swap(db, pid, MON, "lunch", r["chickpea"], config=config, clock=clock)


def test_a_recorded_or_gone_day_cannot_be_swapped(week, config) -> None:
    db, pid, _base, r, clock = week
    db.insert("day_confirmations", person_id=pid, day=isoday(MON), status="confirmed", basis="menu")
    with pytest.raises(NothingToBuild, match="recorded"):
        swap.swap(db, pid, MON, "lunch", r["salmon"], config=config, clock=clock)
    with pytest.raises(NothingToBuild, match="gone"):
        swap.swap(db, pid, LAST_MON, "lunch", r["salmon"], config=config, clock=clock)


def test_avoiding_a_food_then_swapping_it_out_leaves_none_of_it(week, config) -> None:
    """The whole of her example: "I do not want lentils and chickpeas" - remembered, and this
    week's menu no longer holds them, without a model call."""
    db, pid, _base, _r, clock = week
    prefs.amend(
        db,
        pid,
        "avoid",
        add=catalogue.avoid_terms("I do not want lentils and chickpeas"),
        clock=clock,
    )
    cleaned = swap.swap_avoided(db, pid, config=config, clock=clock)
    assert [s.old for s in cleaned.swapped] == ["Lentil dal"]
    assert cleaned.stuck == []
    assert prefs.still_on_menu(db, pid, clock.today_local()) == []
    assert "Salmon traybake in for Lentil dal" in cleaned.said()


def test_a_dish_nothing_can_replace_is_named_with_the_way_to_new_ones(week, config) -> None:
    db, pid, _base, _r, clock = week
    prefs.amend(db, pid, "avoid", add=["lentils", "salmon", "chickpeas"], clock=clock)
    cleaned = swap.swap_avoided(db, pid, config=config, clock=clock)
    assert cleaned.swapped == []
    assert [s["name"] for s in cleaned.stuck] == ["Lentil dal"]
    assert "ask for new dishes written for that meal" in cleaned.said()


# ---------------------------------------------------------------- the page
@pytest.fixture
def page(config, frozen_clock: FrozenClock, tmp_path):
    """The same week behind the page, the partner and the owner both linked."""
    config.paths.ensure()
    db = Db.open(config.paths.db, clock=frozen_clock)
    his = db.insert("persons", slug="owner", display_name="Alex", tone="blunt")
    partners = db.insert("persons", slug="partner", display_name="Sam", tone="warm")
    db.insert("tailscale_identities", person_id=his, login=OWNER_LOGIN)
    db.insert("tailscale_identities", person_id=partners, login=PARTNER_LOGIN)
    _base, r = _seed(db, partners)
    secrets_dir = tmp_path / "s"
    secrets_dir.mkdir(exist_ok=True)
    (secrets_dir / "telegram-token-dev.txt").write_text("1:t", encoding="utf-8")
    config.data["web"]["secure_cookies"] = False
    config.data["web"]["root_path"] = ""
    app = App(config, Secrets(secrets_dir), clock=frozen_clock, transport=FakeTelegram())
    client = TestClient(create_app(app), base_url="http://testserver")
    try:
        yield client, db, partners, his, r
    finally:
        db.close()


def _headers(client: TestClient, login: str = PARTNER_LOGIN) -> dict[str, str]:
    csrf = client.get("/api/me", headers={"Tailscale-User-Login": login}).json()["csrf"]
    return {"Tailscale-User-Login": login, "X-CSRF-Token": csrf}


def test_the_page_shows_the_list_and_what_each_entry_keeps_out(page) -> None:
    client, _db, _partners, _his, _r = page
    h = _headers(client)
    got = client.post("/api/prefs", json={"list": "avoid", "add": "lentils, chickpeas"}, headers=h)
    assert got.status_code == 200, got.text
    assert "lentils (Red lentils)" in got.json()["said"]
    view = client.get("/api/prefs", headers={"Tailscale-User-Login": PARTNER_LOGIN}).json()
    assert [a["term"] for a in view["avoid"]] == ["chickpeas", "lentils"]
    assert view["still_on_menu"][0]["name"] == "Lentil dal"
    assert "fish" in view["suggest"] and "Red lentils" in view["suggest"]
    back = client.post("/api/prefs", json={"list": "avoid", "remove": ["chickpeas"]}, headers=h)
    assert "Allowed back: chickpeas" in back.json()["said"]


def test_the_page_swaps_a_dish_and_the_menu_says_so(page) -> None:
    client, db, partners, _his, _r = page
    h = _headers(client)
    offered = client.get(
        "/api/menu/swap",
        params={"day": isoday(MON), "slot": "lunch"},
        headers={"Tailscale-User-Login": PARTNER_LOGIN},
    )
    assert offered.status_code == 200, offered.text
    body = offered.json()
    assert body["current"]["name"] == "Lentil dal"
    assert len(body["days"]) == 5
    # the chickpea salad cooks from the chicken the week's dinners have open, so it comes before
    assert body["candidates"][0]["name"] == "Chickpea salad"
    assert body["candidates"][0]["kitchen"] == "cooks from the open Chicken breast"
    pick = next(c for c in body["candidates"] if c["name"] == "Salmon traybake")
    done = client.post(
        "/api/menu/swap",
        json={"day": isoday(MON), "slot": "lunch", "recipe_id": pick["recipe_id"]},
        headers=h,
    )
    assert done.status_code == 200, done.text
    assert "Salmon traybake in for Lentil dal" in done.json()["said"]
    menu_now = client.get(
        "/api/menu", params={"day": isoday(MON)}, headers={"Tailscale-User-Login": PARTNER_LOGIN}
    ).json()
    lunches = {s["name"] for d in menu_now["days"] for s in d["slots"] if s["slot"] == "lunch"}
    assert lunches == {"Salmon traybake"}
    assert (
        db.scalar(
            "SELECT COUNT(*) FROM web_events WHERE person_id = ? AND kind = ?",
            (partners, "dish_swapped"),
        )
        == 1
    )


def test_one_tap_clears_the_avoided_food_off_the_menu(page) -> None:
    client, _db, _partners, _his, _r = page
    h = _headers(client)
    client.post("/api/prefs", json={"list": "avoid", "add": ["lentils"]}, headers=h)
    got = client.post("/api/menu/swap-avoided", json={}, headers=h)
    assert got.status_code == 200, got.text
    assert got.json()["swapped"] == 1 and got.json()["stuck"] == 0
    view = client.get("/api/prefs", headers={"Tailscale-User-Login": PARTNER_LOGIN}).json()
    assert view["still_on_menu"] == []


def test_the_person_is_the_headers_never_the_bodys(page) -> None:
    """The owner's login cannot swap her lunch: his day has no menu, and no field names a person."""
    client, _db, _partners, _his, r = page
    h = _headers(client, OWNER_LOGIN)
    for path, body in (
        (
            "/api/menu/swap",
            {"day": isoday(MON), "slot": "lunch", "recipe_id": r["salmon"], "person_id": 2},
        ),
        ("/api/prefs", {"list": "avoid", "add": ["lentils"], "person": "partner"}),
    ):
        assert client.post(path, json=body, headers=h).status_code == 400, path
    mine = client.post(
        "/api/menu/swap",
        json={"day": isoday(MON), "slot": "lunch", "recipe_id": r["salmon"]},
        headers=h,
    )
    assert mine.status_code == 409, "his own Monday has no menu to swap in"
    assert "no menu" in mine.json()["detail"].lower()
    assert (
        client.get("/api/prefs", headers={"Tailscale-User-Login": OWNER_LOGIN}).json()[
            "still_on_menu"
        ]
        == []
    ), "and her menu is not his to read"


def test_a_bad_swap_is_an_answer_not_a_fault(page) -> None:
    client, _db, _partners, _his, r = page
    h = _headers(client)
    q = {"Tailscale-User-Login": PARTNER_LOGIN}
    assert (
        client.get(
            "/api/menu/swap", params={"day": isoday(MON), "slot": "brunch"}, headers=q
        ).status_code
        == 400
    )
    assert (
        client.get(
            "/api/menu/swap", params={"day": "2026-08-01", "slot": "lunch"}, headers=q
        ).status_code
        == 409
    )
    stir = client.post(
        "/api/menu/swap",
        json={"day": isoday(MON), "slot": "lunch", "recipe_id": r["stirfry"]},
        headers=h,
    )
    assert stir.status_code == 400 and "not one of" in stir.json()["detail"], "rated 1"
    assert (
        client.post("/api/prefs", json={"list": "banned", "add": "x"}, headers=h).status_code == 400
    )
    assert client.post("/api/prefs", json={"list": "avoid"}, headers=h).status_code == 400


# ---------------------------------------------------------------- the bot
#: dev sends to the owner's chat only (CLAUDE.md: development never messages her), so the bot's side
#: is driven through his chat, with her week seeded as his
OWNER_CHAT = 1000001


@pytest.fixture
def bot(fixture_db: Db, two_persons, frozen_clock, config, tmp_path):
    """A chat with the bot over the same week. Everything the bot sends is kept."""
    from tiffin.telegram.poller import Poller

    db, pid, clock = fixture_db, two_persons["owner"], frozen_clock
    _base, r = _seed(db, pid)
    secrets_dir = tmp_path / "s"
    secrets_dir.mkdir()
    (secrets_dir / "telegram-token-dev.txt").write_text("1:t", encoding="utf-8")
    tg = FakeTelegram()
    app = App(
        config,
        Secrets(secrets_dir),
        clock=clock,
        transport=tg,
        gateway_factory=lambda _a, _d: None,
    )
    poller = Poller(
        app.api(),
        db,
        app.registry(db),
        app.handlers(db).handle,
        clock=clock,
        sleeper=lambda s: None,
    )
    n = {"i": 0}

    def said(update) -> list[dict]:
        before = len(tg.sent)
        tg.queue_update(update)
        poller.run_once()
        return tg.sent[before:]

    def send(text: str) -> list[dict]:
        n["i"] += 1
        return said(tg.text_update(n["i"], OWNER_CHAT, text))

    def tap(data: str) -> list[dict]:
        n["i"] += 1
        return said(tg.callback_update(n["i"], OWNER_CHAT, data, message_id=n["i"]))

    return send, tap, db, pid, r, clock


def _buttons(msg: dict) -> dict[str, str]:
    markup = msg.get("reply_markup") or {}
    if isinstance(markup, str):
        markup = json.loads(markup)
    return {b["text"]: b["callback_data"] for row in markup.get("inline_keyboard", []) for b in row}


def test_partner_sentence_is_kept_at_once_and_the_dal_goes_in_one_tap(bot) -> None:
    """
    she ate, not a card to accept: the list, written, said back by the foods it keeps out -
    with no model, so it holds whether or not she has agreed to Claude."""
    from tiffin.telegram import dayflow

    send, tap, db, pid, _r, clock = bot
    clock.set_local(dt.datetime(2026, 9, 7, 20, 45))
    assert dayflow.arm_evening(db, pid, clock.today_local(), clock=clock), "the question is open"
    out = send("I do not want lentils and chickpeas")
    assert len(out) == 1, out
    text = out[0]["text"]
    assert "Kept off your menus from now on" in text and "Red lentils" in text
    assert "Lentil dal" in text, "the dish still holding it is named"
    assert catalogue.refused_terms(db, pid) == ["chickpeas", "lentils"]
    assert db.scalar("SELECT COUNT(*) FROM estimates WHERE person_id = ?", (pid,)) == 0, (
        "not logged as food eaten"
    )
    buttons = _buttons(out[0])
    assert set(buttons) == {"Swap it now", "Undo"}
    done = tap(buttons["Swap it now"])
    assert "in for Lentil dal" in done[-1]["text"]
    assert prefs.still_on_menu(db, pid, clock.today_local()) == []


def test_undo_takes_back_that_refusal_and_nothing_else(bot) -> None:
    send, tap, db, pid, _r, _clock = bot
    send("I hate salmon")
    out = send("no more lentils please")
    assert "lentils" in out[0]["text"]
    back = tap(_buttons(out[0])["Undo"])
    assert "Allowed back: lentils" in back[-1]["text"]
    assert catalogue.refused_terms(db, pid) == ["salmon"], "the earlier refusal stands"


@pytest.mark.parametrize(
    "said",
    [
        "I didn't eat the lentils",  # a report of the day, for the check-in
        "can you not use lentils?",  # a question, for the chat
        "I had lentils for lunch",
        "no worries",
        "I hate faff",  # names no food
    ],
)
def test_what_is_not_a_standing_refusal_is_left_alone(bot, said: str) -> None:
    from tiffin.telegram import foodflow

    send, _tap, db, pid, _r, _clock = bot
    send(said)
    assert catalogue.refused_terms(db, pid) == [], said
    assert foodflow.refusal_terms(said) == [] or said == "I hate faff"


def test_swap_from_the_meal_card_and_by_command(bot) -> None:
    send, tap, db, pid, _r, _clock = bot
    out = send("/swap monday lunch")
    assert "Swap Lentil dal" in out[0]["text"]
    buttons = _buttons(out[0])
    assert "Salmon traybake" in buttons and "Just Mon" in buttons
    one = tap(buttons["Just Mon"])
    data = _buttons(one[-1])["Salmon traybake"]
    assert data.endswith(":1"), "the one-day list puts the dish in on that day only"
    done = tap(data)
    assert "Salmon traybake in for Lentil dal, lunch on Mon 14 Sep" in done[-1]["text"]
    assert [p.name for p in swap.plates_on(db, pid, FRI)][1] == "Lentil dal"
    assert "Nothing still to come" in send("/swap")[0]["text"]
    assert "Name a day and a meal" in send("/swap whenever")[0]["text"]


def test_cook_repeat_does_not_bring_a_refused_food_back(bot, config) -> None:
    """A repeat copied last week's chickpea salad straight back for somebody who had since said
    "no chickpeas"."""
    send, _tap, db, pid, _r, clock = bot
    clock.set_local(dt.datetime(2026, 9, 12, 10, 0))  # Saturday, before the week starts
    send("I do not want chickpeas")
    out = send("/cook repeat")
    text = " ".join(m["text"] for m in out)
    assert prefs.still_on_menu(db, pid, clock.today_local()) == [], text


def test_a_dish_note_can_be_cleared(page) -> None:
    """An emptied note box was sent as no note at all, and the server carried the old note
    forward: a note once written could never be taken back."""
    client, db, partners, _his, r = page
    h = _headers(client)
    body = {"recipe_id": r["dal"], "score": 3, "note": "too dry"}
    assert client.post("/api/dishes/rate", json=body, headers=h).status_code == 200
    assert (
        client.post(
            "/api/dishes/rate", json={"recipe_id": r["dal"], "score": 4}, headers=h
        ).status_code
        == 200
    )
    assert db.scalar("SELECT note FROM ratings ORDER BY id DESC LIMIT 1") == "too dry", (
        "a score with no note keeps the one written"
    )
    cleared = {"recipe_id": r["dal"], "score": 4, "note": ""}
    assert client.post("/api/dishes/rate", json=cleared, headers=h).status_code == 200
    assert db.scalar("SELECT note FROM ratings ORDER BY id DESC LIMIT 1") is None
    assert partners


# ---------------------------------------------------------------- a weekend cooked together
def test_a_swap_on_a_weekend_cooked_together_moves_both_plates(pair, config) -> None:  # noqa: F811
    """The owner swaps Sunday's lunch: it is one dish cooked for two, so the partner's plate
    follows - the
    same dish, sized to her own day - and the two stay a pair, each seeing the other's plate."""
    from tiffin.food import shared

    from .test_plates import _shared_weekend

    db, him, partner, clock, _built = _shared_weekend(pair)
    sun = dt.date(2026, 9, 20)
    offered = swap.candidates(db, him.id, sun, "lunch", clock.today_local())
    assert offered, "Saturday's dishes can stand in for Sunday's"
    pick = offered[0]
    got = swap.swap(
        db, him.id, sun, "lunch", pick.recipe_id, every_day=False, config=config, clock=clock
    )
    assert got.together == "Sam" and got.partner_menu_id is not None
    assert "Sam's plates follow" in got.said()
    mine = {p.slot: p for p in swap.plates_on(db, him.id, sun)}
    partners = {p.slot: p for p in swap.plates_on(db, partner.id, sun)}
    assert mine["lunch"].name == partners["lunch"].name == pick.name
    seen = shared.partner_plate(
        db, partner.id, menu.accepted_menu_id(db, partner.id, sun) or 0, sun, "lunch"
    )
    assert seen is not None and seen["name"] == "Alex", "still cooked together"
