"""Two people in one kitchen: twelve moments where what one of them does meets the other.

and Thu-Fri, has refused chickpeas, holds an accepted Mon-Wed menu with a chickpea salad on it
while a chickpea-free proposal waits for her Accept, and has no Thu-Fri menu. The weekend is
cooked together: one list for both on Friday evening, each plate its own
size. Only the owner has a Tailscale identity.

Every story drives the real poller, handlers, scheduler jobs or web queries, and reads what the
person gets on their own phone or page.
"""

from __future__ import annotations

import datetime as dt
import json
import types

import pytest

from tiffin.app import App
from tiffin.clock import TZ, isoday
from tiffin.db import Db
from tiffin.jobs import shopping_list
from tiffin.scheduler import Scheduler, slots_for
from tiffin.secrets import Secrets
from tiffin.telegram.poller import Poller
from tiffin.web import queries

from .fakes import FakeTelegram
from .test_food_shopping import seed_ingredient, seed_recipe

OWNER_CHAT = 1000001
PARTNER_CHAT = 1000002
X = chr(0xD7)  # the multiplication sign the list itself prints

SUN13 = dt.date(2026, 9, 13)
MON14 = dt.date(2026, 9, 14)
THU17 = dt.date(2026, 9, 17)
FRI18 = dt.date(2026, 9, 18)
SAT19 = dt.date(2026, 9, 19)
SUN20 = dt.date(2026, 9, 20)
MON21 = dt.date(2026, 9, 21)


# ---------------------------------------------------------------- props
def _profile(
    db: Db,
    pid: int,
    *,
    sex: str,
    segments: list[list[int]],
    refused: tuple[str, ...] = (),
    freezes: int = 0,
) -> None:
    db.insert(
        "profiles",
        person_id=pid,
        effective_from="2026-09-01",
        sex=sex,
        birth_date="1990-03-15",
        height_cm=175.0,
        activity_factor=1.5,
        kcal_floor=1500,
        meals_per_day=4,
        segment_days=json.dumps(segments),
        refused_foods=json.dumps(list(refused)),
        batch_slots=json.dumps(["lunch", "dinner"]),
        freezes=freezes,
    )


def _menu(
    db: Db,
    pid: int,
    start: dt.date,
    days: int,
    slots: dict[str, tuple[int, float]],
    *,
    status: str = "accepted",
    decision: str | None = None,
) -> int:
    """Slot -> (recipe, portion multiplier), the same every day of the segment."""
    mid = db.insert(
        "menus",
        person_id=pid,
        segment_start=isoday(start),
        segment_end=isoday(start + dt.timedelta(days=days - 1)),
        status=status,
    )
    for i in range(days):
        did = db.insert(
            "menu_days",
            menu_id=mid,
            day=isoday(start + dt.timedelta(days=i)),
            kcal=1900,
            protein_g=150,
            fat_g=65,
            carb_g=180,
            fibre_g=28,
        )
        for slot, (rid, mult) in slots.items():
            db.insert(
                "meal_slots",
                menu_day_id=did,
                slot=slot,
                recipe_id=rid,
                portion_multiplier=mult,
                portion_g=350,
                kcal=550,
                protein_g=45,
                fat_g=18,
                carb_g=50,
                fibre_g=8,
            )
    if decision is not None:
        db.insert("menu_decisions", menu_id=mid, decision=decision)
    return mid


def _pause(db: Db, pid: int, start: dt.date, end: dt.date) -> None:
    db.insert(
        "pause_periods",
        person_id=pid,
        start_day=isoday(start),
        end_day=isoday(end),
        reason="away",
        created_at="2026-09-12 09:00:00",
    )


def _line(text: str, needle: str) -> str:
    return next((ln for ln in text.splitlines() if needle in ln), "")


def _taps(sent: dict) -> list[str]:
    markup = sent.get("reply_markup") or {}
    if isinstance(markup, str):
        markup = json.loads(markup)
    return [b["callback_data"] for row in markup.get("inline_keyboard", []) for b in row]


# ---------------------------------------------------------------- the house
@pytest.fixture
def house(fixture_db: Db, two_persons, frozen_clock, config, tmp_path):
    """Both chats on the real poller and the real scheduler, Monday 14 Sep 12:40.

    The dev outbox drops everything not addressed to the owner; this harness flips every outbox the
    App hands out to prod, so what she would receive lands in the fake Telegram and can be read.
    Nothing leaves the process. Shop fill is off: it drives Chrome, and no story is about prices.
    """
    db = fixture_db
    frozen_clock.set_local(dt.datetime(2026, 9, 14, 12, 40, tzinfo=TZ))
    config.data.setdefault("chrome", {})["shop_fill_enabled"] = False
    his, partner = two_persons["owner"], two_persons["partner"]
    _profile(db, his, sex="m", segments=[[0, 1, 2, 3, 4]], freezes=1)
    _profile(db, partner, sex="f", segments=[[0, 1, 2], [3, 4]], refused=("chickpeas",))
    food = {
        "salmon": seed_ingredient(db, "salmon", "Salmon fillets", "fish", 240, "2 fillets"),
        "potato": seed_ingredient(db, "potato", "Potatoes", "produce", 2000, "2 kg bag"),
        "chickpeas": seed_ingredient(
            db, "chickpeas", "Chickpeas, tinned, drained", "tins", 240, "400 g tin"
        ),
        "rice": seed_ingredient(db, "rice", "Basmati rice", "grains", 1000, "1 kg bag"),
        "chicken": seed_ingredient(db, "chicken", "Chicken breast", "meat", 500, "500 g pack"),
        "feta": seed_ingredient(db, "feta", "Feta", "dairy", 200, "200 g pack"),
    }

    secrets_dir = tmp_path / "s"
    secrets_dir.mkdir()
    (secrets_dir / "telegram-token-dev.txt").write_text("1:t", encoding="utf-8")
    config.paths.ensure()
    tg = FakeTelegram()
    app = App(config, Secrets(secrets_dir), clock=frozen_clock, transport=tg)
    real_outbox = app.outbox

    def prod_outbox(d: Db):
        ob = real_outbox(d)
        ob.env = "prod"
        return ob

    app.outbox = prod_outbox  # type: ignore[method-assign]
    handlers = app.handlers(db)
    poller = Poller(
        app.api(), db, app.registry(db), handlers.handle, clock=frozen_clock, sleeper=lambda s: None
    )
    n = {"i": 0}

    def _since(chat: int, before: int) -> str:
        return "\n".join(m["text"] for m in tg.sent[before:] if m["chat_id"] == chat)

    def say(chat: int, text: str) -> str:
        before = len(tg.sent)
        n["i"] += 1
        tg.queue_update(tg.text_update(n["i"], chat, text))
        poller.run_once()
        return _since(chat, before)

    def tap(chat: int, data: str) -> str:
        before = len(tg.sent)
        n["i"] += 1
        tg.queue_update(tg.callback_update(n["i"], chat, data, message_id=n["i"]))
        poller.run_once()
        return _since(chat, before)

    def last_to(chat: int) -> dict:
        got = tg.sent_to(chat)
        assert got, f"nothing was sent to chat {chat}"
        return got[-1]

    def run_job(job, day: dt.date, pid: int = 0) -> str:
        prof = (
            db.one("SELECT * FROM profiles WHERE person_id = ? ORDER BY id DESC LIMIT 1", (pid,))
            if pid
            else None
        )
        slots = slots_for(job, pid, prof, day, db)
        assert slots, f"no slot for {job.name} on {day}"
        return Scheduler(app, [job], lambda _p, _fn: None).execute(db, slots[0])

    def at(day: dt.date, hh: int, mm: int = 0) -> None:
        frozen_clock.set_local(dt.datetime(day.year, day.month, day.day, hh, mm, tzinfo=TZ))

    return types.SimpleNamespace(
        db=db,
        tg=tg,
        owner=his,
        partner=partner,
        food=food,
        say=say,
        tap=tap,
        last_to=last_to,
        run_job=run_job,
        at=at,
    )


def _salmon_tray(h) -> int:
    return seed_recipe(h.db, "Salmon traybake", 1, {h.food["salmon"]: 150, h.food["potato"]: 300})


def _partner_real_mon_wed(h) -> tuple[int, int]:
    """Her accepted Mon-Wed with the chickpea salad, and the chickpea-free one waiting."""
    salad = seed_recipe(
        h.db,
        "Chicken, chickpea and feta salad",
        1,
        {h.food["chicken"]: 120, h.food["chickpeas"]: 80, h.food["feta"]: 30},
    )
    fresh = seed_recipe(
        h.db,
        "Chicken, rice and feta salad",
        1,
        {h.food["chicken"]: 120, h.food["rice"]: 60, h.food["feta"]: 30},
    )
    accepted = _menu(
        h.db, h.partner, MON14, 3, {"lunch": (salad, 0.8)}, status="proposed", decision="accepted"
    )
    waiting = _menu(h.db, h.partner, MON14, 3, {"lunch": (fresh, 0.8)}, status="proposed")
    return accepted, waiting


def _his_mon_fri(h) -> int:
    lunch = seed_recipe(h.db, "Chicken and rice", 1, {h.food["chicken"]: 150, h.food["rice"]: 60})
    return _menu(h.db, h.owner, MON14, 5, {"lunch": (lunch, 1.0)}, status="repeated")


# ---------------------------------------------------------------- 1. one list, two phones
def test_01_friday_evening_both_phones_get_one_weekend_list_for_both_plates(house) -> None:
    """Friday 18 Sep, 18:00. The weekend is cooked together - one salmon traybake, his plate
    bigger than hers - and each wants the same list on their own phone, salmon counted once."""
    h = house
    tray = _salmon_tray(h)
    _menu(h.db, h.owner, SAT19, 2, {"dinner": (tray, 1.2)})
    _menu(h.db, h.partner, SAT19, 2, {"dinner": (tray, 0.8)})
    h.at(FRI18, 18)
    assert h.run_job(shopping_list.JOINT_JOB, FRI18) == "ok"
    his = [m["text"] for m in h.tg.sent_to(OWNER_CHAT)]
    partners = [m["text"] for m in h.tg.sent_to(PARTNER_CHAT)]
    assert len(his) == 1 and his == partners, (his, partners)
    # his 150 x 1.2 x 2 + hers 150 x 0.8 x 2 = 600 g: three packs of two fillets, not two lists
    assert f"Salmon fillets 3 {X} 2 fillets" in _line(partners[0], "Salmon"), partners[0]
    assert len(partners[0].splitlines()) <= 10


# ---------------------------------------------------------------- 2. one person paused
def test_02_partner_is_away_for_the_weekend_and_the_list_is_their_alone(house) -> None:
    """Friday 18:00, she is away Fri-Sun and paused. the owner shops for the weekend by himself: the
    list reaches only him and buys only his plates."""
    h = house
    tray = _salmon_tray(h)
    _menu(h.db, h.owner, SAT19, 2, {"dinner": (tray, 1.2)})
    _menu(h.db, h.partner, SAT19, 2, {"dinner": (tray, 0.8)})
    _pause(h.db, h.partner, FRI18, SUN20)
    h.at(FRI18, 18)
    h.run_job(shopping_list.JOINT_JOB, FRI18)
    assert h.tg.sent_to(PARTNER_CHAT) == [], "a paused person gets no list"
    his = h.last_to(OWNER_CHAT)["text"]
    # his 360 g alone: two packs of two, not the three the pair would need
    assert f"Salmon fillets 2 {X} 2 fillets" in _line(his, "Salmon"), his


# ---------------------------------------------------------------- 3. one person with no menu
def test_03_monday_partner_asks_about_thursday_and_hears_there_is_no_menu_yet(house) -> None:
    """Monday 12:40 at her desk. She sends /menu thu to see Thursday's lunch; her Thu-Fri has no
    menu. She is told so, with the next step, and nothing of the owner's Thursday leaks into it."""
    h = house
    _partner_real_mon_wed(h)
    _his_mon_fri(h)
    partners = h.say(PARTNER_CHAT, "/menu thu")
    assert "No menu covers Thu 17 Sep" in partners and "/cook" in partners, partners
    assert "Chicken and rice" not in partners, "his Thursday lunch is not hers"
    his = h.say(OWNER_CHAT, "/menu thu")
    assert "Lunch: Chicken and rice" in his, his


# ---------------------------------------------------------------- 4. the same trolley
def test_04_in_the_shop_on_saturday_partner_opens_the_list_owner_is_ticking(house) -> None:
    """Saturday 19 Sep, 10:30, one trolley, two phones. the owner has ticked the salmon on the
    page; she
    sends /shopping to see what is left of the weekend list they were both sent."""
    h = house
    tray = _salmon_tray(h)
    _menu(h.db, h.owner, SAT19, 2, {"dinner": (tray, 1.2)})
    _menu(h.db, h.partner, SAT19, 2, {"dinner": (tray, 0.8)})
    h.at(FRI18, 18)
    h.run_job(shopping_list.JOINT_JOB, FRI18)
    salmon = h.db.scalar(
        "SELECT si.id FROM shopping_items si JOIN shopping_lists sl ON sl.id = si.list_id "
        "JOIN ingredients i ON i.id = si.ingredient_id "
        "WHERE sl.kind = 'joint_weekend' AND i.code = 'salmon'"
    )
    h.at(SAT19, 10, 30)
    assert queries.tick_item(h.db, h.owner, int(salmon), True), "the page's tick path"
    text = h.say(PARTNER_CHAT, "/shopping")
    head = text.splitlines()[0] if text else ""
    # needle is "Salmon fillets", not "Salmon": /shopping now names the dish each row is FOR,
    # and the potato row is "- Potatoes ... - Salmon traybake" - a bare "Salmon" needle finds
    # that attribution first, not the fillets row this story is actually about.
    assert "joint weekend" in head and _line(text, "Salmon fillets").startswith("x "), (
        "she and Alex were sent one weekend list and he has ticked its salmon; her /shopping "
        f"builds a private second list for her plates alone instead: {text}"
    )


# ---------------------------------------------------------------- 5. whose rice
# Pinned the credit: now runs with `shopping.count_on_home` on, because the default buys everything.
@pytest.mark.usefixtures("count_on_home")
def test_05_the_weekend_list_does_not_forget_partner_own_rice(house) -> None:
    """Sunday 20 Sep, 11:00. Her Mon-Wed list from last Sunday left 700 g of her rice in the
    cupboard; the weekend ate out of the owner's rice, not hers. She opens /shopping for the
    week and
    expects no rice on it: this week needs 510 g."""
    h = house
    his_lunch = seed_recipe(
        h.db, "Chicken and rice", 1, {h.food["chicken"]: 150, h.food["rice"]: 60}
    )
    partner_lunch = seed_recipe(
        h.db, "Rice and chicken bowl", 1, {h.food["chicken"]: 120, h.food["rice"]: 100}
    )
    _menu(h.db, h.owner, MON14, 5, {"lunch": (his_lunch, 1.0)})
    _menu(h.db, h.partner, MON14, 3, {"lunch": (partner_lunch, 1.0)})
    h.at(SUN13, 18)
    h.run_job(shopping_list.JOB, SUN13, h.owner)  # 300 g of a 1 kg bag: 700 g of his left
    h.run_job(shopping_list.JOB, SUN13, h.partner)  # 300 g of a 1 kg bag: 700 g of hers left

    bowl = seed_recipe(h.db, "Salmon rice bowl", 1, {h.food["salmon"]: 150, h.food["rice"]: 75})
    _menu(h.db, h.owner, SAT19, 2, {"dinner": (bowl, 1.0)})
    _menu(h.db, h.partner, SAT19, 2, {"dinner": (bowl, 1.0)})
    h.at(FRI18, 18)
    for owner in (h.owner, h.partner):
        h.db.insert("leftovers", person_id=owner, ingredient_id=h.food["rice"], grams=700.0)
    weekend = h.run_job(shopping_list.JOINT_JOB, FRI18)
    assert weekend == "ok"
    assert "Basmati" not in h.last_to(PARTNER_CHAT)["text"], "his 700 g covers the weekend's 300 g"

    big = seed_recipe(
        h.db, "Big rice and chicken bowl", 1, {h.food["chicken"]: 120, h.food["rice"]: 170}
    )
    _menu(h.db, h.partner, MON21, 3, {"lunch": (big, 1.0)})
    h.at(SUN20, 11)
    text = h.say(PARTNER_CHAT, "/shopping")
    assert "Chicken breast" in text, text
    rice = _line(text, "Basmati")
    assert not rice, (
        "700 g of her own rice is untouched and 400 g of his is left after the weekend; her week "
        f"needs 510 g. The weekend list overwrote her 700 g with half of his 400 g: {rice!r}"
    )


# ---------------------------------------------------------------- 6. one dish, two plates
def test_06_saturday_owner_cooks_the_shared_tray_and_the_card_is_for_both_plates(house) -> None:
    """Saturday 19 Sep, 18:30, oven on. the owner is cooking the one salmon traybake they both eat
    tonight, his plate at 1.2 and hers at 0.8. He opens Dinner from /menu to know how much salmon
    and potato go in the tray."""
    h = house
    tray = _salmon_tray(h)
    _menu(h.db, h.owner, SAT19, 2, {"dinner": (tray, 1.2)})
    _menu(h.db, h.partner, SAT19, 2, {"dinner": (tray, 0.8)})
    h.at(SAT19, 18, 30)
    h.say(OWNER_CHAT, "/menu")
    dinner = next(
        (d for d in _taps(h.last_to(OWNER_CHAT)) if d.startswith("k:meal:") and ":dinner" in d),
        None,
    )
    assert dinner, f"no Dinner button: {_taps(h.last_to(OWNER_CHAT))}"
    card = h.tap(OWNER_CHAT, dinner)
    assert "Salmon traybake" in card, card
    salmon = _line(card, "Salmon fillets")
    assert "300 g" in salmon, (
        "the weekend is cooked together and this tray feeds both plates (180 g + 120 g of "
        f"salmon); the card gives his plate alone: {salmon!r}"
    )


# ---------------------------------------------------------------- 7. her refusal, his dish
def test_07_the_weekend_list_on_partner_phone_says_whose_the_chickpeas_are(house) -> None:
    """Friday 18:00, she has asked three times for no chickpeas. The joint weekend list lands on
    her phone; the owner's Sunday stew has chickpeas and her own weekend has none. Reading it in the
    shop, she needs to see the tin is his, not a fourth broken promise."""
    h = house
    tray = _salmon_tray(h)
    stew = seed_recipe(
        h.db, "Chickpea and potato stew", 1, {h.food["chickpeas"]: 120, h.food["potato"]: 200}
    )
    _menu(h.db, h.owner, SAT19, 2, {"dinner": (stew, 1.0)})
    _menu(h.db, h.partner, SAT19, 2, {"dinner": (tray, 0.8)})
    h.at(FRI18, 18)
    h.run_job(shopping_list.JOINT_JOB, FRI18)
    partners = h.last_to(PARTNER_CHAT)["text"]
    chickpeas = _line(partners, "Chickpeas")
    assert chickpeas, f"his stew needs them, so the joint list buys them: {partners}"
    assert "Alex" in chickpeas or "stew" in chickpeas.lower(), (
        "her weekend menu has no chickpeas and she refused them; her copy of the list names "
        f"them with nothing saying they are for Alex's stew: {chickpeas!r}"
    )


# ---------------------------------------------------------------- 8. her card, his phone
def test_08_a_meal_button_for_partner_menu_does_not_open_partner_card_on_their_phone(house) -> None:
    """Monday 12:40. A meal button carrying her menu's id is tapped from the owner's chat - an old
    message, a forwarded keyboard, a crafted callback. The button is input; what the owner sees must
    come from who he is, and her lunch and her portion are hers."""
    h = house
    partner_menu, _waiting = _partner_real_mon_wed(h)
    _his_mon_fri(h)
    data = f"k:meal:{partner_menu}:lunch:{MON14:%Y%m%d}"
    own = h.tap(PARTNER_CHAT, data)
    assert "Chicken, chickpea and feta salad" in own, f"the button is real: {own}"
    seen = h.tap(OWNER_CHAT, data)
    assert "chickpea" not in seen.lower(), (
        f"Alex's chat was sent her lunch card, portion and macros included: {seen}"
    )


# ---------------------------------------------------------------- 9. what each sees of the other
def test_09_at_lunch_each_reads_their_own_menu_and_only_partners_carries_partner_refusal(
    house,
) -> None:
    """Monday 12:40, both at their desks, both send /menu. the owner gets his lunch and nothing
    about
    her chickpeas or her waiting proposal; she gets her lunch and is told it still holds the food
    she refused."""
    h = house
    _partner_real_mon_wed(h)
    _his_mon_fri(h)
    his = h.say(OWNER_CHAT, "/menu")
    assert "Lunch: Chicken and rice" in his, his
    for private in ("chickpea", "feta", "proposals", "avoid"):
        assert private not in his.lower(), f"{private!r} is hers: {his}"
    partners = h.say(PARTNER_CHAT, "/menu")
    assert "Lunch: Chicken, chickpea and feta salad" in partners, partners
    assert "still has Chickpeas" in partners and "/cook replace" in partners, partners
    assert "Chicken and rice" not in partners, partners


# ---------------------------------------------------------------- 10. the page is his
def test_10_owner_on_the_page_sees_their_list_and_never_partners(house) -> None:
    """Monday 13:00, the owner opens the Shopping tab. Both Sunday lists exist and hers - with its
    chickpeas - was built last. His page shows his list, will not open hers by id, and will not
    let him tick it."""
    h = house
    _partner_real_mon_wed(h)
    _his_mon_fri(h)
    h.at(SUN13, 18)
    h.run_job(shopping_list.JOB, SUN13, h.owner)
    h.run_job(shopping_list.JOB, SUN13, h.partner)
    partners = h.db.one(
        "SELECT id FROM shopping_lists WHERE person_id = ? ORDER BY id DESC LIMIT 1", (h.partner,)
    )
    assert partners is not None
    h.at(MON14, 13)
    page = queries.shopping(h.db, h.owner, None)
    assert page["list"] is not None and page["list"]["person_id"] == h.owner, page["list"]
    assert not any("Chickpeas" in str(i["name"]) for i in page["items"]), page["items"]
    assert queries.shopping(h.db, h.owner, int(partners["id"])) == {"list": None, "items": []}
    partner_item = h.db.scalar(
        "SELECT id FROM shopping_items WHERE list_id = ?", (int(partners["id"]),)
    )
    assert not queries.tick_item(h.db, h.owner, int(partner_item), True)


# ---------------------------------------------------------------- 11. away for her week
def test_11_partner_is_away_mon_to_wed_and_gets_no_list_for_a_week_partner_will_not_eat(
    house,
) -> None:
    """Sunday 20 Sep, 18:00. She is away Monday to Wednesday and paused for exactly those days;
    the menu for them was repeated by the lapse job. The Sunday list for Mon-Wed is food nobody
    will eat, and she should not be sent to buy it."""
    h = house
    bowl = seed_recipe(
        h.db, "Rice and chicken bowl", 1, {h.food["chicken"]: 120, h.food["rice"]: 100}
    )
    _menu(h.db, h.partner, MON21, 3, {"lunch": (bowl, 1.0)}, status="repeated")
    _pause(h.db, h.partner, MON21, dt.date(2026, 9, 23))
    h.at(SUN20, 18)
    status = h.run_job(shopping_list.JOB, SUN20, h.partner)
    lists = [m["text"] for m in h.tg.sent_to(PARTNER_CHAT) if "Shopping for" in m["text"]]
    assert not lists, (
        f"she is paused for every day of the segment and the job ran ({status}) and sent her "
        f"a list for it: {lists}"
    )


# ---------------------------------------------------------------- 12. ticking the joint list
def test_12_the_weekend_list_can_be_ticked_from_the_message_it_arrives_in(house) -> None:
    """Saturday 10:30, trolley, the Friday weekend list open on his phone. Buttons are how every
    routine answer is given; he wants to tick the salmon off the message he is reading, or at
    least be told where he can."""
    h = house
    tray = _salmon_tray(h)
    _menu(h.db, h.owner, SAT19, 2, {"dinner": (tray, 1.2)})
    _menu(h.db, h.partner, SAT19, 2, {"dinner": (tray, 0.8)})
    h.at(FRI18, 18)
    h.run_job(shopping_list.JOINT_JOB, FRI18)
    sent = h.last_to(OWNER_CHAT)
    ticks = [d for d in _taps(sent) if d.startswith("k:tick:")]
    assert ticks or "/shopping" in sent["text"] or "<a href" in sent["text"], (
        "the joint weekend list arrives with no buttons and no pointer to where it can be "
        f"ticked: {sent['text']!r} markup={sent.get('reply_markup')!r}"
    )
