"""Ten moments from the second person's first fortnight.

something for him. the partner joined yesterday: no accepted menu, no ratings, no confirmed days, no
calibration, no second weigh-in. These stories stand in the places where Tiffin already
holds the right answer and ask whether she can reach it from where she is standing - the first
message, the first empty-handed command, the first menu that will not build, the first Sunday,
and the day she asks to be forgotten.
"""

from __future__ import annotations

import datetime as dt
import json
import types

import pytest

from tiffin import body
from tiffin import onboarding as ob
from tiffin import scheduler as sc
from tiffin import targets as targets_mod
from tiffin.advisor import facts as facts_mod
from tiffin.advisor import prose
from tiffin.app import App
from tiffin.clock import TZ, FrozenClock, isoday
from tiffin.db import Db
from tiffin.energy import pace_check
from tiffin.food import shopping
from tiffin.jobs import sunday_summary
from tiffin.model.gateway import Gateway, ModelResult, consent_granted
from tiffin.secrets import Secrets
from tiffin.telegram import render as r
from tiffin.telegram.poller import Poller

from .fakes import FakeTelegram
from .test_food_catalogue import add_profile
from .test_food_shopping import seed_ingredient, seed_recipe

OWNER_CHAT = 1000001
PARTNER_CHAT = 1000002
NEW_PHONE_CHAT = 6011223344

SATURDAY = dt.date(2026, 9, 12)  # the day she finished the interview
SUNDAY = dt.date(2026, 9, 13)  # her first Sunday: it is day two
MONDAY = dt.date(2026, 9, 14)  # the first cook segment she will ever be offered

#: her numbers, from a real second person: 58 kg at 165 cm is two thirds of his bodyweight
PARTNER_KG = 58.0
PARTNER_CM = 165.0
PARTNER_DOB = "1996-04-02"

#: five foods, ordered by code the way `catalogue.approved` numbers them for the prompt:
#: chick(1) oats(2) oil(3) salmon(4) veg(5)
FOODS = (
    ("chick", "Chicken breast, raw", "meat", 500.0, "500 g pack", 165.0, 31.0, 3.6, 0.0, 0.0),
    ("oats", "Porridge oats", "grains", 1000.0, "1 kg bag", 379.0, 13.2, 8.0, 60.0, 10.0),
    ("oil", "Olive oil", "condiments", 500.0, "500 ml", 899.0, 0.0, 99.9, 0.0, 0.0),
    ("salmon", "Salmon fillets", "fish", 240.0, "2 fillets", 200.0, 24.0, 12.0, 0.0, 0.0),
    ("veg", "Mixed vegetables", "produce", 350.0, "350 g bag", 35.0, 3.8, 0.4, 2.2, 3.0),
)

#: a day her targets can actually be solved to, out of the foods above
PARTNER_DAY = {
    "breakfast": [(2, 60), (3, 5)],
    "lunch": [(1, 150), (5, 150)],
    "dinner": [(1, 150), (5, 200), (3, 15)],
    "snack": [(2, 50), (5, 100)],
}
#: the same shape but oats only: nothing here reaches 2 g of protein per kg, for either of them
THIN_DAY = {slot: [(2, 90)] for slot in ("breakfast", "lunch", "dinner", "snack")}

SEGMENT_DAYS = [MONDAY + dt.timedelta(days=i) for i in range(5)]


def seed_foods(db: Db) -> None:
    for code, name, aisle, pack_g, label, kcal, p, f, c, fib in FOODS:
        seed_ingredient(
            db,
            code,
            name,
            aisle,
            pack_g,
            label,
            kcal_100=kcal,
            protein_100=p,
            fat_100=f,
            carb_100=c,
            fibre_100=fib,
        )


def a_payload(days: list[dt.date], shape: dict[str, list[tuple[int, int]]]) -> dict:
    """The model's answer: the same four slots every day, built from FOODS' indexes."""
    return {
        "days": [
            {
                "day": isoday(day),
                "slots": [
                    {
                        "slot": slot,
                        "recipe": {
                            "name": f"{slot.capitalize()} bowl",
                            "ingredients": [{"i": i, "g": g} for i, g in items],
                            "method": ["Cook it.", "Eat it."],
                            # within the small-slot cap (10 min), so this story is about
                            # her targets and not about a breakfast being too long to make
                            "prep_minutes": 10,
                        },
                    }
                    for slot, items in shape.items()
                ],
            }
            for day in days
        ]
    }


class FakeGateway:
    """Records every prompt; answers with the queued payloads, then with an empty object."""

    def __init__(self, payloads: list[dict] | None = None, model: str = "claude-opus-5"):
        self.payloads = list(payloads or [])
        self.model = model
        self.prompts: list[str] = []

    def json_task(self, task: str, prompt: str, schema_name: str, *, person_id: int) -> ModelResult:
        self.prompts.append(prompt)
        data = self.payloads.pop(0) if self.payloads else {}
        return ModelResult(text="", data=data, model_used=self.model, backend="fake")


def last_said(db: Db, person_id: int) -> str:
    """What Tiffin last composed FOR her.

    Not `tg.sent`: a shell is always dev and dev sends only to the owner, so every message of
    hers is
    a `dropped_dev` row. The row still carries the words, and the words are the story.
    """
    row = db.one(
        "SELECT text_html FROM messages_out WHERE person_id = ? ORDER BY id DESC LIMIT 1",
        (person_id,),
    )
    return "" if row is None else str(row["text_html"])


def said_since(db: Db, person_id: int, after_id: int) -> str:
    rows = db.all(
        "SELECT text_html FROM messages_out WHERE person_id = ? AND id > ? ORDER BY id",
        (person_id, after_id),
    )
    return "\n".join(str(row["text_html"]) for row in rows)


def newest_message_id(db: Db) -> int:
    return int(db.scalar("SELECT COALESCE(MAX(id), 0) FROM messages_out") or 0)


def give_owner_a_month(db: Db, pid: int) -> None:
    """A month of weigh-ins and a settled week: the whole difference between the two of them."""
    add_profile(db, pid, effective_from="2026-08-10", kcal_floor=1800)
    for i in range(28):
        body.record_weigh_in(
            db, pid, dt.date(2026, 8, 17) + dt.timedelta(days=i), 100.9 - i * 0.07, "icu_wellness"
        )
    db.insert(
        "targets",
        person_id=pid,
        week_start="2026-09-07",
        kcal=2289,
        protein_g=202,
        fat_g=71,
        carb_g=210,
        fibre_g=32,
        source="formula",
    )


@pytest.fixture
def partners(fixture_db: Db, two_persons, frozen_clock: FrozenClock, config, tmp_path):
    """The partner has completed the interview but has no other records yet."""
    db = fixture_db
    partner, his = two_persons["partner"], two_persons["owner"]
    frozen_clock.set_local(dt.datetime(2026, 9, 13, 13, 0, tzinfo=TZ))
    add_profile(
        db,
        partner,
        effective_from=isoday(SATURDAY),
        sex="f",
        birth_date=PARTNER_DOB,
        height_cm=PARTNER_CM,
        activity_factor=1.375,
        kcal_floor=1200,
        tone="warm",
        liked_foods=json.dumps(["salmon"]),  # the interview's "foods you like to see often"
    )
    body.record_weigh_in(db, partner, SATURDAY, PARTNER_KG, "manual")
    plan = db.insert("training_plans", person_id=partner, effective_from=isoday(SATURDAY))
    for weekday, kind, minutes in ((1, "climb", 90), (3, "ride", 60)):
        db.insert(
            "planned_sessions",
            plan_id=plan,
            person_id=partner,
            weekday=weekday,
            kind=kind,
            target_minutes=minutes,
        )
    # exactly what `_finish` writes: a consents row with no `kind`, and the first formula week
    db.insert("consents", person_id=partner, granted=1, wording_version="v2")
    targets_mod.ensure_targets(db, partner, SATURDAY, config, clock=frozen_clock)
    seed_foods(db)

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
    poller = Poller(
        app.api(),
        db,
        app.registry(db),
        handlers.handle,
        clock=frozen_clock,
        sleeper=lambda s: None,
    )
    n = {"i": 0}

    def send(chat_id: int, text: str) -> None:
        n["i"] += 1
        tg.queue_update(tg.text_update(n["i"], chat_id, text))
        poller.run_once()

    def say(text: str) -> str:
        send(PARTNER_CHAT, text)
        return last_said(db, partner)

    def tap(data: str) -> str:
        n["i"] += 1
        tg.queue_update(tg.callback_update(n["i"], PARTNER_CHAT, data, message_id=n["i"]))
        poller.run_once()
        return last_said(db, partner)

    def run_job(job: sc.Job, day: dt.date, *, person_id: int = 0) -> str:
        scheduler = sc.Scheduler(app, [job], lambda p, fn: None)
        target = person_id or (partner if job.scope == "each" else 0)
        prof = db.one(
            "SELECT * FROM profiles WHERE person_id = ? ORDER BY id DESC LIMIT 1",
            (target or partner,),
        )
        slots = sc.slots_for(job, target, prof, day, db)
        assert slots, f"no slot for {job.name} on {day}"
        return scheduler.execute(db, slots[0])

    return types.SimpleNamespace(
        db=db,
        partner=partner,
        his=his,
        app=app,
        tg=tg,
        gateways=gateways,
        registry=app.registry(db),
        send=send,
        say=say,
        tap=tap,
        run_job=run_job,
        clock=frozen_clock,
    )


# -- first contact ---------------------------------------------------------------------------


def test_01_the_code_for_partner_new_phone_is_swallowed_with_no_word_to_anyone(partners) -> None:
    """She changes phone in week two. the owner issues a fresh code, she sends it, and nothing
    happens.

    `trg_ident_cap` counts ROWS in telegram_identities, not people, so the second identity in the
    table is the last one there will ever be. Her code is valid, unexpired and unused, and the
    tiffin knows whose it is - but the insert aborts inside `poller.ingest`'s single transaction,
    which takes the `messages_in` row down with it while the offset moves on regardless.
    """
    code = partners.registry.create_pairing_code("partner")
    before = newest_message_id(partners.db)

    partners.send(NEW_PHONE_CHAT, code)

    bound = partners.db.one(
        "SELECT * FROM telegram_identities WHERE chat_id = ?", (NEW_PHONE_CHAT,)
    )
    spoken = partners.db.all("SELECT kind, text_html FROM messages_out WHERE id > ?", (before,))
    assert bound is not None or spoken, (
        "a code the operator has just issued must either bind the new chat or say why not - to "
        "her, or as an admin alert to Alex. Instead: "
        f"identities={partners.db.scalar('SELECT COUNT(*) FROM telegram_identities')}, "
        f"messages_in={partners.db.scalar('SELECT COUNT(*) FROM messages_in')}, "
        f"pairing_uses={partners.db.scalar('SELECT COUNT(*) FROM pairing_uses')}, "
        f"offset={partners.db.state_get('update_offset')} - her message left no trace at all"
    )


# -- her first week, with nothing behind her -------------------------------------------------


def test_02_partner_empty_handed_food_commands_name_the_one_that_makes_a_menu(partners) -> None:
    """Day two: no menu exists yet, and `/cook` is the command that makes one now.

    `/menu` answers in two lines and stops. `/batch` sends her to `/menu` and to "Sunday's
    proposal", which is not a thing: a menu is proposed two days before its cook segment starts,
    so hers would be Saturday's, and the Sunday job proposes targets rather than food.
    """
    menu = partners.say("/menu")
    batch = partners.say("/batch")

    # it names the day rather than saying "today", so /menu tomorrow reads the same way
    assert "No menu covers" in menu and "No menu to cook from yet" in batch
    assert "/cook" in menu and "/cook" in batch, (
        "both dead ends withhold the one command that would feed her today.\n"
        f"/menu: {menu!r}\n/batch: {batch!r}"
    )


def test_03_partner_first_today_answers_without_inventing_a_week_partner_has_not_had(
    partners,
) -> None:
    """One weigh-in, no confirmed day, no menu: every line is either true or absent."""
    row = targets_mod.latest_targets(partners.db, partners.partner, SUNDAY)
    assert row is not None, "the interview's own formula week is there from day one"
    m = targets_mod.macros_of(row)

    today = partners.say("/today")

    assert today.startswith("<b>Sun 13 Sep</b>"), today
    assert "Target " + r.macros_line(m.kcal, m.protein_g, m.fat_g, m.carb_g, m.fibre_g) in today
    assert "Day: open" in today and "Paused" not in today
    assert "Trend 58.0 kg" in today and "over 7 days" not in today, (
        "one weigh-in is a trend of one; a seven-day delta needs two"
    )


def test_04_the_rate_question_never_calls_a_goal_reachable_that_no_button_reaches(partners) -> None:
    """Her fastest option is 1.0 % of 58 kg; the "reachable" test is against 0.8 kg/week.

    The cap is a kilogram number and the buttons are percentages, so for a smaller person the cap
    is not the constraint that binds - her own rate is. Both numbers are printed in the same
    message, four lines apart, and they disagree.
    """
    today = SUNDAY
    goal_date = today + dt.timedelta(days=40)
    answers = {
        "wkg": PARTNER_KG,
        "gwt": PARTNER_KG - 4.0,
        "gdt": isoday(goal_date),
        "sex": "f",
        ob.TODAY_KEY: isoday(today),
    }
    person = partners.registry.require("partner")

    prompt = ob._rate_prompt(answers, person)

    fastest = max(
        min(PARTNER_KG * float(value) / 100.0, 0.8)
        for _label, value in ob._rate_options(answers, person)
    )
    needed = pace_check(PARTNER_KG, PARTNER_KG - 4.0, goal_date, today, 0.8).required_kg_week
    assert needed > fastest, "the fixture must sit between her fastest rate and the 0.8 kg cap"
    assert "reachable" not in prompt, (
        f"her goal needs {needed:.2f} kg/week and the fastest rate she is offered is "
        f"{fastest:.2f} kg/week, yet the same screen calls it reachable:\n{prompt}"
    )


def test_05_the_foods_partner_said_partner_likes_reach_the_menu_partner_is_offered(
    partners,
) -> None:
    """Favourites come from ratings, and she has rated nothing.

    "Foods you like to see often" is question twenty-two of the interview and `/set likes`
    answers "The next menu will use it", but nothing between `profiles.liked_foods` and the menu
    prompt reads that column. For him the gap is invisible: a month of ratings fills the same
    slot in the prompt.
    """
    gateway = FakeGateway([a_payload(SEGMENT_DAYS, PARTNER_DAY)])
    partners.gateways.append(gateway)

    partners.say("/cook")

    assert gateway.prompts, "the menu prompt was never built"
    favourites = next(
        (ln for ln in gateway.prompts[0].splitlines() if ln.startswith("Favourites")), ""
    )
    assert "salmon" in favourites.lower(), (
        f"she told the interview she likes salmon and the prompt says {favourites!r}"
    )


def test_06_a_menu_partner_cannot_have_gives_partner_the_reason_and_the_way_out(partners) -> None:
    """Her first menu will not solve and there is no accepted menu to repeat - her week-one case.

    `why_not` holds the shortfall in words and names the two commands that answer it. Which of
    them sees it is decided by `person.is_admin`, so the same failure reads as an instruction to
    """
    give_owner_a_month(partners.db, partners.his)
    # after 16:00: before it, a plain /cook on a Sunday with nothing planned plans Sunday first
    # , and this story is about next week's shortfall, not about Sunday
    partners.clock.set_local(dt.datetime(2026, 9, 13, 17, 0, tzinfo=TZ))
    partners.gateways.append(FakeGateway([a_payload(SEGMENT_DAYS, THIN_DAY)] * 8))

    before = newest_message_id(partners.db)
    partners.send(OWNER_CHAT, "/cook")
    partners.send(PARTNER_CHAT, "/cook")
    told_him = said_since(partners.db, partners.his, before)
    told_partner = said_since(partners.db, partners.partner, before)

    assert "could not build a menu" in told_him and "could not build a menu" in told_partner
    assert "/set rate" in told_him, f"the remedy exists: {told_him!r}"
    assert "/set rate" in told_partner, (
        "the same shortfall, the same two remedies, and the person whose week cannot be planned "
        f"is shown neither of them:\n{told_partner}"
    )


def test_07_declining_the_cloud_changes_partner_route_and_one_tap_changes_it_back(partners) -> None:
    """What actually changes if she says no, and whether she can see it and undo it.

    The interview writes its consents row with no `kind` at all, leaning on the column default,
    and the gate reads exactly one kind - so this is also a check that the two ends still meet.
    """
    partners.db.insert("consents", person_id=partners.partner, granted=0, wording_version="v2")
    gateway = Gateway(
        types.SimpleNamespace(name="claude"),  # type: ignore[arg-type]
        partners.db,
        lambda pid: consent_granted(partners.db, pid),
        partners.clock,
        budgets=partners.app.config.budget,
    )
    assert consent_granted(partners.db, partners.partner) is False
    assert gateway.primary_route(partners.partner) == "no_consent"

    shown = partners.say("/consent")
    assert "Claude processing is OFF" in shown, shown
    assert "local model" in shown or "this computer" in shown, "it says what happens instead"

    partners.tap("k:consent:1")
    assert consent_granted(partners.db, partners.partner) is True, (
        "one tap, not the interview again"
    )
    assert gateway.primary_route(partners.partner) is None


# -- her first Sunday -----------------------------------------------------------------------


def test_08_partner_first_sunday_does_not_score_the_days_before_partner_joined(partners) -> None:
    """She finished the interview on Saturday; the review week runs Monday to Sunday.

    `facts.day_intakes` walks all seven days and calls every day with no confirmation row
    unconfirmed, so her first weekly summary opens with five days of failure that happened before
    Tiffin had heard of her. The day she joined is in `profiles.effective_from`.
    """
    f = facts_mod.compute(partners.db, partners.partner, SUNDAY, partners.app.config)
    days_line = next(
        ln
        for ln in prose.summary_lines(f, confidence=None, next_targets=None)
        if ln.startswith("Days recorded:")
    )

    assert f.unconfirmed_days <= 2, (
        f"she has been on Tiffin for two days of this week and it reads {days_line!r}"
    )


def test_09_what_owner_reads_about_partner_is_a_trend_and_a_name_partner_chose(partners) -> None:
    """The whole of what he is shown about her, in a week where she has confirmed nothing."""
    give_owner_a_month(partners.db, partners.his)
    partners.registry.set_display_name("partner", "Sam", set_by="operator")

    assert partners.run_job(sunday_summary.JOB, SUNDAY) == "ok"
    his = last_said(partners.db, partners.his)

    assert "Sam: trend 58.0 kg" in his, his
    tail = his.split("Sam:")[1]
    assert "unconfirmed" not in tail and "Days:" not in tail, f"never her misses: {tail!r}"
    assert "Her:" not in his, "a slug is not a name"
    assert len(his.splitlines()) <= 10


# -- she leaves ------------------------------------------------------------------------------


def test_10_erasing_partner_leaves_no_joint_list_pointing_at_a_menu_that_is_gone(
    fixture_db: Db, two_persons, tmp_path
) -> None:
    """The weekend is cooked together, so the joint list carries the FIRST covered person's menu
    id and no person of its own. In her first fortnight she is often the only one with an accepted
    weekend menu - he is paused, or away, or his lapsed - and then that id is hers. Erasing her
    deletes the menu and leaves the household list naming it.
    """
    from tiffin import admin

    db, partner = fixture_db, two_persons["partner"]
    mince = seed_ingredient(db, "mince", "Beef mince 5%", "meat", 500.0, "500 g pack")
    dish = seed_recipe(db, "Chilli", 1, {mince: 225.0})
    menu_id = db.insert(
        "menus",
        person_id=partner,
        segment_start=isoday(SATURDAY),
        segment_end=isoday(SATURDAY + dt.timedelta(days=1)),
        status="accepted",
    )
    for i in range(2):
        day_id = db.insert(
            "menu_days",
            menu_id=menu_id,
            day=isoday(SATURDAY + dt.timedelta(days=i)),
            kcal=1341,
            protein_g=116,
            fat_g=41,
            carb_g=127,
            fibre_g=25,
        )
        db.insert(
            "meal_slots",
            menu_day_id=day_id,
            slot="dinner",
            recipe_id=dish,
            portion_multiplier=1.0,
            portion_g=225.0,
            kcal=382,
            protein_g=45,
            fat_g=11,
            carb_g=0,
            fibre_g=0,
        )
    shopping.joint_weekend(db, [menu_id])
    exports = tmp_path / "exports"
    exports.mkdir()
    manifest = admin.export_person(db, "partner", exports).path / "manifest.json"

    raised = ""
    try:
        admin.erase_person(db, "partner", manifest_path=manifest)
    except Exception as e:
        raised = f"{type(e).__name__}: {e}"
    dangling = [dict(row) for row in db.con.execute("PRAGMA foreign_key_check")]

    assert not raised and not dangling, (
        f"erase_person raised {raised!r} and left {dangling} behind, while having already "
        f"committed the deletes: menus={db.scalar('SELECT COUNT(*) FROM menus')}, "
        f"erasures={db.scalar('SELECT COUNT(*) FROM erasures')}, "
        f"joint_items={db.scalar('SELECT COUNT(*) FROM shopping_items')}"
    )
