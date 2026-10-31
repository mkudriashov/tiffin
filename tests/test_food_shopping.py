"""Shopping lists: pack rounding, stock reported by the person, pantry excluded,
aisle order from the YAML, the joint weekend union, and compact rendering under 120 chars."""

from __future__ import annotations

import datetime as dt

from tiffin.clock import isoday
from tiffin.db import Db
from tiffin.food import shopping
from tiffin.food.shopping import ShoppingItem

AISLES = ("produce", "meat", "fish", "dairy", "bakery", "grains", "tins", "frozen", "condiments")


def seed_ingredient(
    db: Db, code: str, name: str, aisle: str, pack_g: float, label: str, **kw
) -> int:
    base = {
        "code": code,
        "version": 1,
        "name": name,
        "cofid_code": None,
        "kcal_100": 100.0,
        "protein_100": 10.0,
        "fat_100": 5.0,
        "carb_100": 10.0,
        "fibre_100": 1.0,
        "pack_g": pack_g,
        "pack_label": label,
        "aisle": aisle,
        "pantry": 0,
        "use_whole_pack": 0,
        "approved": 1,
        "person_scope": "both",
    }
    base.update(kw)
    return db.insert("ingredients", **base)


def seed_recipe(db: Db, name: str, servings: int, grams: dict[int, float]) -> int:
    rid = db.insert(
        "recipes", name=name, servings=servings, prep_minutes=20, method_json="[]", source="manual"
    )
    for iid, g in grams.items():
        db.insert("recipe_ingredients", recipe_id=rid, ingredient_id=iid, grams=g)
    return rid


def seed_menu(
    db: Db, person_id: int, start: dt.date, days: int, slots: dict[str, tuple[int, float]]
) -> int:
    """Slots: slot -> (recipe_id, portion_multiplier), the same every day."""
    end = start + dt.timedelta(days=days - 1)
    mid = db.insert(
        "menus",
        person_id=person_id,
        segment_start=isoday(start),
        segment_end=isoday(end),
        status="accepted",
    )
    for i in range(days):
        day = start + dt.timedelta(days=i)
        did = db.insert(
            "menu_days",
            menu_id=mid,
            day=isoday(day),
            kcal=2000,
            protein_g=180,
            fat_g=70,
            carb_g=200,
            fibre_g=30,
        )
        for slot, (rid, m) in slots.items():
            db.insert(
                "meal_slots",
                menu_day_id=did,
                slot=slot,
                recipe_id=rid,
                portion_multiplier=m,
                portion_g=300,
                kcal=500,
                protein_g=45,
                fat_g=18,
                carb_g=50,
                fibre_g=8,
            )
    return mid


def test_packs_for() -> None:
    assert shopping.packs_for(1100, 500) == (3, 400.0)
    assert shopping.packs_for(1000, 500) == (2, 0.0)
    assert shopping.packs_for(1000.5, 500) == (2, 0.0)  # 1 g slack: a pinned pack stays one pack
    assert shopping.packs_for(1001.5, 500) == (3, 498.5)
    assert shopping.packs_for(0.5, 500) == (1, 499.5)
    assert shopping.packs_for(0, 500) == (0, 0.0)
    assert shopping.packs_for(-30, 500) == (0, 30.0)  # unused credit is the leftover


def test_pack_short() -> None:
    assert shopping.pack_short("500 g pack", 500) == "500 g"
    assert shopping.pack_short("1 kg bag", 1000) == "1 kg"
    assert shopping.pack_short("6 eggs", 348) == "6 eggs"
    assert shopping.pack_short("400 g tin", 400) == "400 g"
    assert shopping.pack_short("", 250) == "250 g"


def test_build_rounds_credits_and_orders(fixture_db: Db, two_persons) -> None:
    db = fixture_db
    pid = two_persons["owner"]
    chicken = seed_ingredient(db, "chicken", "Chicken breast", "meat", 500, "500 g pack")
    rice = seed_ingredient(db, "rice", "Basmati rice", "grains", 1000, "1 kg bag", pantry=1)
    broccoli = seed_ingredient(db, "broccoli", "Broccoli", "produce", 350, "350 g head")
    beans = seed_ingredient(db, "beans", "Chickpeas", "tins", 240, "400 g tin")
    # a superseded version of chicken referenced by an old recipe: grams still count, id of the
    # newest version referenced is used
    chicken_v2 = db.insert(
        "ingredients",
        code="chicken",
        version=2,
        name="Chicken breast",
        cofid_code=None,
        kcal_100=165,
        protein_100=31,
        fat_100=3.6,
        carb_100=0,
        fibre_100=0,
        pack_g=500,
        pack_label="500 g pack",
        aisle="meat",
        pantry=0,
        use_whole_pack=0,
        approved=1,
        person_scope="both",
    )
    r_lunch = seed_recipe(db, "Chicken rice", 2, {chicken: 300, rice: 200, broccoli: 200})
    r_dinner = seed_recipe(db, "Chicken beans", 1, {chicken_v2: 120, beans: 100})
    menu = seed_menu(
        db, pid, dt.date(2026, 9, 7), 5, {"lunch": (r_lunch, 1.0), "dinner": (r_dinner, 1.5)}
    )

    items = shopping.build(db, menu, aisles=AISLES)
    by = {i.code: i for i in items}
    # chicken: lunch 300/2 x 1.0 x 5 = 750; dinner 120 x 1.5 x 5 = 900 -> 1650 g -> 4 packs
    assert by["chicken"].grams_total == 1650.0 and by["chicken"].packs == 4
    assert by["chicken"].leftover_g == 350.0 and by["chicken"].ingredient_id == chicken_v2
    # broccoli 200/2 x 5 = 500 -> 2 heads, 200 g over; chickpeas 100 x 1.5 x 5 = 750 -> 4 tins
    assert by["broccoli"].packs == 2 and by["broccoli"].leftover_g == 200.0
    assert by["beans"].packs == 4 and by["beans"].leftover_g == 210.0
    # the catalogue's pantry flag no longer hides a food from a list: it was hiding every
    # grain, so a real week's shop came back with no carbohydrate on it
    assert by["rice"].packs >= 1, "a grain the week eats is a grain the week buys"
    # YAML aisle order, and the grains now sit in it rather than being hidden
    assert [i.aisle for i in items] == ["produce", "meat", "grains", "tins"]
    assert all(i.list_id == items[0].list_id and i.item_id for i in items)
    lst = db.one("SELECT * FROM shopping_lists WHERE id = ?", (items[0].list_id,))
    assert lst is not None and lst["kind"] == "segment" and lst["person_id"] == pid
    assert lst["menu_id"] == menu
    rows = db.all("SELECT * FROM shopping_items WHERE list_id = ? ORDER BY id", (items[0].list_id,))
    assert [(r["ingredient_id"], r["packs"], r["leftover_g"], r["aisle"]) for r in rows] == [
        (broccoli, 2, 200.0, "produce"),
        (chicken_v2, 4, 350.0, "meat"),
        (rice, 1, 500.0, "grains"),  # a grain is on the list now, and stays off it next week
        (beans, 4, 210.0, "tins"),
    ]
    left = shopping.current_leftovers(db, pid)
    assert left == {"broccoli": 200.0, "chicken": 350.0, "rice": 500.0, "beans": 210.0}
    assert shopping.current_leftovers(db, two_persons["partner"]) == {}

    # Rebuilding this same window cannot spend surplus that this list only planned.
    items2 = shopping.build(db, menu, aisles=AISLES)
    by2 = {i.code: i for i in items2}
    assert by2["chicken"].credit_g == 0.0 and by2["chicken"].packs == 4
    assert by2["broccoli"].packs == 2
    assert by2["rice"].packs == 1
    assert shopping.current_leftovers(db, pid) == {
        "broccoli": 200.0,
        "chicken": 350.0,
        "rice": 500.0,
        "beans": 210.0,
    }
    assert db.scalar("SELECT COUNT(*) FROM leftovers WHERE person_id = ?", (pid,)) == 8

    # A quantity the person reports is different from a surplus the plan imagined.
    db.insert("leftovers", person_id=pid, ingredient_id=broccoli, grams=900.0)
    items3 = shopping.build(db, menu, aisles=AISLES)
    assert "broccoli" not in {i.code for i in items3}
    assert shopping.current_leftovers(db, pid)["broccoli"] == 400.0


def test_joint_weekend_union(fixture_db: Db, two_persons) -> None:
    db = fixture_db
    owner_id, partner_id = two_persons["owner"], two_persons["partner"]
    salmon = seed_ingredient(
        db, "salmon", "Salmon fillets", "fish", 240, "2 fillets", use_whole_pack=1
    )
    potato = seed_ingredient(db, "potato", "Potatoes", "produce", 2000, "2 kg bag")
    oil = seed_ingredient(db, "oil", "Olive oil", "condiments", 460, "500 ml bottle", pantry=1)
    r = seed_recipe(db, "Salmon and potatoes", 1, {salmon: 150, potato: 300, oil: 10})
    # Fri 11 Sep .. Sun 13 Sep: only Sat/Sun count towards the joint list
    m_owner = seed_menu(db, owner_id, dt.date(2026, 9, 11), 3, {"dinner": (r, 1.2)})
    m_partner = seed_menu(db, partner_id, dt.date(2026, 9, 12), 2, {"dinner": (r, 0.8)})
    items = shopping.joint_weekend(db, [m_owner, m_partner], aisles=AISLES)
    by = {i.code: i for i in items}
    # salmon: his 150 x 1.2 x 2 days = 360 + hers 150 x 0.8 x 2 = 240 -> 600 g -> 3 packs
    assert (
        by["salmon"].grams_total == 600.0
        and by["salmon"].packs == 3
        and by["salmon"].leftover_g == 120.0
    )
    assert by["potato"].grams_total == 1200.0 and by["potato"].packs == 1
    assert by["oil"].packs >= 1, "shown, and the leftover credit is what stops re-buying it"
    assert [i.code for i in items] == ["potato", "salmon", "oil"]  # condiments come last
    lst = db.one("SELECT * FROM shopping_lists WHERE id = ?", (items[0].list_id,))
    assert lst is not None and lst["kind"] == "joint_weekend" and lst["person_id"] is None
    assert lst["menu_id"] == m_owner
    # HALVED, and hers too: a joint list is food two people share, and writing all of it to one
    # of them cancelled his next week against her half while discounting hers by nothing
    assert shopping.current_leftovers(db, owner_id) == {
        "salmon": 60.0,
        "potato": 400.0,
        "oil": 210.0,
    }
    assert shopping.current_leftovers(db, partner_id) == {
        "salmon": 60.0,
        "potato": 400.0,
        "oil": 210.0,
    }
    assert shopping.weekend_days(dt.date(2026, 9, 11), dt.date(2026, 9, 14)) == [
        dt.date(2026, 9, 12),
        dt.date(2026, 9, 13),
    ]


def _item(code: str, name: str, aisle: str, packs: int, pack_g: float, label: str) -> ShoppingItem:
    return ShoppingItem(
        ingredient_id=1,
        code=code,
        name=name,
        aisle=aisle,
        pack_g=pack_g,
        pack_label=label,
        grams_total=0,
        credit_g=0,
        grams_needed=0,
        packs=packs,
        leftover_g=0,
    )


def test_render_lines_grouped_and_wrapped() -> None:
    items = [
        _item("mince", "Beef mince 5%", "meat", 1, 500, "500 g pack"),
        _item("chicken", "Chicken breast", "meat", 2, 500, "500 g pack"),
        _item("broccoli", "Broccoli", "produce", 1, 350, "350 g head"),
        _item("eggs", "Eggs", "dairy", 2, 348, "6 eggs"),
    ]
    lines = shopping.render_lines(items, aisles=AISLES)
    assert lines == [
        "Produce: Broccoli 1 × 350 g head",  # noqa: RUF001
        "Meat: Beef mince 5% 1 × 500 g, Chicken breast 2 × 500 g",  # noqa: RUF001
        "Dairy: Eggs 2 × 6 eggs",  # noqa: RUF001
    ]
    many = [
        _item(f"v{i}", f"Vegetable number {i:02d}", "produce", 1, 500, "500 g pack")
        for i in range(12)
    ]
    lines = shopping.render_lines(many, aisles=AISLES)
    assert len(lines) > 1 and all(line.startswith("Produce: ") for line in lines)
    assert all(len(line) < 120 for line in lines)
    assert sum(line.count("×") for line in lines) == 12  # noqa: RUF001
    assert shopping.render_lines([], aisles=AISLES) == []


def test_the_weekend_gets_one_list_not_two(fixture_db: Db, two_persons, config, frozen_clock):
    """Both shopping jobs run at Friday 18:00.

    Giving a Monday-to-Friday cadence its own weekend segment - so the weekend had a menu at
    all - made the per-person job fire for Saturday-Sunday as well as the joint one, which
    already covers exactly those days for both people. Two lists a minute apart, double the
    packs, and the second crediting leftovers the first had just invented.
    """
    import json
    from pathlib import Path

    from tiffin import scheduler as sc
    from tiffin.app import App
    from tiffin.jobs import shopping_list
    from tiffin.secrets import Secrets

    from .fakes import FakeTelegram

    chicken = seed_ingredient(fixture_db, "chk", "Chicken breast", "meat", 500.0, "500 g pack")
    rid = seed_recipe(fixture_db, "Chicken", 1, {chicken: 300.0})
    pid = two_persons["owner"]
    seed_menu(fixture_db, pid, dt.date(2026, 9, 12), 2, {"dinner": (rid, 1.0)})
    fixture_db.insert(
        "profiles",
        person_id=pid,
        effective_from="2026-09-01",
        sex="m",
        birth_date="1990-01-01",
        height_cm=183.0,
        activity_factor=1.5,
        steps_floor=10000,
        quiet_start="22:30",
        quiet_end="07:00",
        confirm_time="20:00",
        morning_time="07:00",
        nudge_time="16:00",
        rate_pct_per_week=0.5,
        kcal_floor=1800,
        segment_days=json.dumps([[0, 1, 2, 3, 4]]),
        meals_per_day=3,
        pre_bed_protein=0,
        tone="neutral",
    )

    secrets_dir = Path(str(fixture_db.path)).parent / "s"
    secrets_dir.mkdir(exist_ok=True)
    (secrets_dir / "telegram-token-dev.txt").write_text("1:t", encoding="utf-8")
    tg = FakeTelegram()
    config.paths.ensure()
    app = App(config, Secrets(secrets_dir), clock=frozen_clock, transport=tg)

    prof = fixture_db.one("SELECT * FROM profiles WHERE person_id = ?", (pid,))
    friday = dt.date(2026, 9, 11)
    segment = shopping_list.menu_mod.next_segment(prof, friday, days_before=1)
    assert segment is not None and segment.start == dt.date(2026, 9, 12), "Saturday, from Friday"
    assert all(d.weekday() in shopping.WEEKEND for d in segment.days), "the whole segment"

    sched = sc.Scheduler(app, [shopping_list.JOB], lambda p, fn: None)
    slots = sc.slots_for(shopping_list.JOB, pid, prof, friday)
    assert sched.execute(fixture_db, slots[0]) == "skipped", "the per-person job stands down"
    assert tg.sent == [], "and sends nothing, so the weekend is not shopped for twice"
    detail = fixture_db.scalar(
        "SELECT detail FROM run_ledger WHERE job = 'shopping_list' ORDER BY started_at DESC LIMIT 1"
    )
    assert "joint" in str(detail), "and says why, rather than looking like a failure"
