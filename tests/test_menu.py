"""Menu proposal end to end: model dishes -> code macros -> solver portions -> stored menu,
the accept and change taps, the lapse-into-repeat rule, and the shopping list that follows."""

from __future__ import annotations

import datetime as dt
import json

import pytest

from tiffin import scheduler as sc
from tiffin.app import App
from tiffin.clock import TZ, FrozenClock
from tiffin.db import Db
from tiffin.energy import Macros
from tiffin.errors import Infeasible, ModelError
from tiffin.food import catalogue, menu
from tiffin.jobs import menu_propose, shopping_list
from tiffin.model.gateway import ModelResult
from tiffin.secrets import Secrets
from tiffin.telegram import menuflow
from tiffin.telegram.poller import Poller

from .fakes import FakeTelegram

OWNER_CHAT = 1000001
MONDAY = dt.date(2026, 9, 14)  # the segment the Saturday job proposes for


def seed_catalogue(db: Db) -> None:
    """Six ingredients that can hit 2250 kcal / 200 g protein inside the solver's window."""
    rows = [
        ("oats", "Porridge oats", 379, 13.2, 8.0, 60.0, 10.0, 1000, "1 kg bag", "grains"),
        ("milk", "Milk, semi-skimmed", 50, 3.6, 1.8, 4.8, 0.0, 2000, "2 l", "dairy"),
        ("whey", "Whey protein", 380, 80.0, 6.0, 6.0, 0.0, 900, "900 g tub", "grains"),
        ("chicken", "Chicken breast, raw", 165, 31.0, 3.6, 0.0, 0.0, 500, "500 g pack", "meat"),
        ("rice", "Basmati rice, dry", 355, 8.0, 1.0, 78.0, 1.4, 1000, "1 kg bag", "grains"),
        ("oil", "Olive oil", 899, 0.0, 99.9, 0.0, 0.0, 500, "500 ml", "condiments"),
        ("broccoli", "Broccoli", 35, 3.8, 0.4, 2.2, 3.0, 350, "350 g head", "produce"),
        ("yoghurt", "Greek yoghurt 0%", 57, 10.0, 0.4, 4.0, 0.0, 500, "500 g pot", "dairy"),
    ]
    for code, name, kcal, p, f, c, fib, pack, label, aisle in rows:
        db.insert(
            "ingredients",
            code=code,
            version=1,
            name=name,
            kcal_100=kcal,
            protein_100=p,
            fat_100=f,
            carb_100=c,
            fibre_100=fib,
            pack_g=pack,
            pack_label=label,
            aisle=aisle,
            pantry=0,
            use_whole_pack=0,
            approved=1,
            person_scope="both",
        )
        db.insert("ingredient_aliases", ingredient_code=code, alias=name.lower(), source="yaml")


def a_day(day: dt.date) -> dict:
    """One day of the model's answer: four slots out of the seeded catalogue (1-based indexes)."""
    return {
        "day": day.isoformat(),
        "slots": [
            {
                "slot": "breakfast",
                "recipe": {
                    "name": "Oats with whey",
                    "ingredients": [{"i": 5, "g": 80}, {"i": 4, "g": 300}, {"i": 8, "g": 30}],
                    "method": ["Cook the oats in the milk.", "Stir the whey through."],
                    "prep_minutes": 10,
                },
            },
            {
                "slot": "lunch",
                "recipe": {
                    "name": "Chicken and rice",
                    "ingredients": [
                        {"i": 2, "g": 200},
                        {"i": 7, "g": 90},
                        {"i": 1, "g": 200},
                        {"i": 6, "g": 10},
                    ],
                    "method": ["Grill the chicken.", "Boil the rice.", "Steam the broccoli."],
                    "prep_minutes": 30,
                },
            },
            {
                "slot": "dinner",
                "recipe": {
                    "name": "Chicken, rice and broccoli",
                    "ingredients": [
                        {"i": 2, "g": 220},
                        {"i": 7, "g": 90},
                        {"i": 1, "g": 200},
                        {"i": 6, "g": 10},
                    ],
                    "method": ["Grill the chicken.", "Steam the broccoli."],
                    "prep_minutes": 30,
                },
            },
            {
                "slot": "snack",
                "recipe": {
                    "name": "Yoghurt and whey",
                    "ingredients": [{"i": 3, "g": 200}, {"i": 8, "g": 20}],
                    "method": ["Stir together."],
                    "prep_minutes": 2,
                },
            },
        ],
    }


def proposal_for(segment: menu.Segment) -> dict:
    return {"days": [a_day(d) for d in segment.days]}


class FakeGateway:
    def __init__(self, payloads: list[dict | Exception], model: str = "claude-opus-5"):
        self.payloads = list(payloads)
        self.model = model
        self.calls: list[str] = []

    def json_task(self, task: str, prompt: str, schema_name: str, *, person_id: int) -> ModelResult:
        self.calls.append(prompt)
        item = self.payloads.pop(0) if self.payloads else {}
        if isinstance(item, Exception):
            raise item
        return ModelResult(text="", data=item, model_used=self.model, backend="fake")


@pytest.fixture
def world(fixture_db: Db, two_persons, frozen_clock: FrozenClock, config, tmp_path):
    pid = two_persons["owner"]
    frozen_clock.set_local(dt.datetime(2026, 9, 12, 18, 0, tzinfo=TZ))  # Saturday, two days out
    fixture_db.insert(
        "profiles",
        person_id=pid,
        effective_from="2026-09-01",
        sex="m",
        birth_date="1990-03-15",
        height_cm=183.0,
        activity_factor=1.55,
        kcal_floor=1800,
        meals_per_day=4,
        segment_days=json.dumps([[0, 1, 2, 3, 4]]),
        variety_json=json.dumps({"all": "same"}),
    )
    fixture_db.insert(
        "targets",
        person_id=pid,
        week_start="2026-09-14",
        kcal=2250,
        protein_g=200,
        fat_g=70,
        carb_g=200,
        fibre_g=30,
        source="formula",
    )
    seed_catalogue(fixture_db)
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
        gateway_factory=lambda _app, _db: gateways[0],  # type: ignore[arg-type]
    )
    handlers = app.handlers(fixture_db)
    poller = Poller(
        app.api(),
        fixture_db,
        app.registry(fixture_db),
        handlers.handle,
        clock=frozen_clock,
        sleeper=lambda s: None,
    )
    n = {"i": 0}

    def say(text: str) -> str:
        n["i"] += 1
        tg.queue_update(tg.text_update(n["i"], OWNER_CHAT, text))
        poller.run_once()
        return tg.sent[-1]["text"]

    def tap(data: str) -> str:
        n["i"] += 1
        tg.queue_update(tg.callback_update(n["i"], OWNER_CHAT, data, message_id=n["i"]))
        poller.run_once()
        return tg.sent[-1]["text"]

    def run_job(job: sc.Job, day: dt.date) -> str:
        s = sc.Scheduler(app, [job], lambda p, fn: None)
        target = pid if job.scope == "each" else 0
        prof = fixture_db.one("SELECT * FROM profiles WHERE person_id = ?", (pid,))
        slots = sc.slots_for(job, target, prof, day, fixture_db)
        assert slots, f"no slot for {job.name} on {day}"
        return s.execute(fixture_db, slots[0])

    return pid, fixture_db, tg, gateways, say, tap, run_job


def test_segments_and_next_segment(fixture_db: Db, two_persons) -> None:
    pid = two_persons["owner"]
    fixture_db.insert(
        "profiles",
        person_id=pid,
        effective_from="2026-09-01",
        sex="m",
        birth_date="1990-03-15",
        height_cm=183.0,
        activity_factor=1.55,
        kcal_floor=1800,
        segment_days=json.dumps([[0, 1, 2], [3, 4]]),
    )
    prof = fixture_db.one("SELECT * FROM profiles WHERE person_id = ?", (pid,))
    segs = menu.segments_for(prof, MONDAY)
    # the weekend is cooked together and none of the offered cadences covers it, so
    # it is added as its own segment; without it two days in seven had no menu at all
    assert [(s.start, s.end) for s in segs] == [
        (dt.date(2026, 9, 14), dt.date(2026, 9, 16)),
        (dt.date(2026, 9, 17), dt.date(2026, 9, 18)),
        (dt.date(2026, 9, 19), dt.date(2026, 9, 20)),
    ]
    assert segs[0].days == [dt.date(2026, 9, 14), dt.date(2026, 9, 15), dt.date(2026, 9, 16)]
    first = menu.next_segment(prof, dt.date(2026, 9, 12), days_before=2)
    assert first is not None and first.start == MONDAY
    second = menu.next_segment(prof, dt.date(2026, 9, 15), days_before=2)
    assert second is not None and second.start == dt.date(2026, 9, 17)
    assert menu.next_segment(prof, dt.date(2026, 9, 13), days_before=2) is None


def test_menu_proposed_stored_and_accepted(world) -> None:
    pid, db, tg, gateways, _say, tap, run_job = world
    segment = menu.Segment(MONDAY, dt.date(2026, 9, 18))
    gateways.append(FakeGateway([proposal_for(segment)]))
    assert run_job(menu_propose.JOB, dt.date(2026, 9, 12)) == "ok"
    text = tg.sent[-1]["text"]
    assert text.startswith("<b>Menu for the next cook</b>")
    # one line per meal slot, not per day: with variety "same" every day is identical, and
    # repeating the five dishes once per day was twenty wrapped lines of the same food
    assert "Breakfast: Oats with whey" in text
    assert text.count("Oats with whey") == 1, "an identical day is not repeated"
    assert "kcal a day" in text
    assert "Tap a meal for its recipe." in text
    buttons = [b["text"] for row in tg.sent[-1]["reply_markup"]["inline_keyboard"] for b in row]
    # no Accept button any more: the menu is already in force. Swapping one dish comes first
    assert buttons[:2] == ["Swap a dish", "New menu"]
    assert "Accept" not in buttons
    assert "Breakfast" in buttons, "each meal opens its own recipe card"
    assert len(text.splitlines()) <= 10
    menu_id = int(
        db.scalar("SELECT id FROM menus WHERE person_id = ? ORDER BY id DESC LIMIT 1", (pid,))
    )
    rows = db.all("SELECT * FROM menu_days WHERE menu_id = ? ORDER BY day", (menu_id,))
    assert [str(r["day"]) for r in rows] == [d.isoformat() for d in segment.days]
    for row in rows:
        assert abs(int(row["kcal"]) - 2250) <= 2250 * 0.05
        assert float(row["protein_g"]) >= 200
        assert float(row["fat_g"]) >= 70
        assert float(row["fibre_g"]) >= 25
    slots = db.all(
        "SELECT ms.* FROM meal_slots ms JOIN menu_days md ON md.id = ms.menu_day_id "
        "WHERE md.menu_id = ? AND md.day = ?",
        (menu_id, MONDAY.isoformat()),
    )
    assert sorted(s["slot"] for s in slots) == ["breakfast", "dinner", "lunch", "snack"]
    assert all(0.6 <= float(s["portion_multiplier"]) <= 1.6 for s in slots)
    # FOUR recipes, not four per day. The menu's variety is "same all segment", so the same four
    # dishes run Monday to Friday - and storing one `recipes` row per day-slot meant twenty rows
    # for four dishes, which made `/batch` report twenty dishes of one box each instead of four
    # dishes of five. A dish is identified by what a cook would call the same dish.
    assert db.scalar("SELECT COUNT(*) FROM recipes WHERE source = 'model'") == 4
    assert db.scalar("SELECT COUNT(*) FROM meal_slots") == 4 * len(segment.days)
    assert db.scalar("SELECT model_used FROM menus WHERE id = ?", (menu_id,)) == "claude-opus-5"
    # the prompt carried the numbered catalogue and the variety rule, never a macro to invent
    prompt = gateways[0].calls[0]
    assert "1. Broccoli" in prompt and "breakfast same" in prompt

    # decided already, by the job itself - nobody tapped anything
    assert menu.decision_of(db, menu_id) == "accepted"
    assert menu.meals_for_day(db, pid, MONDAY)[0] == "Oats with whey"
    assert menu.accepted_menu_id(db, pid, MONDAY) == menu_id
    # the old Accept callback, tapped on a message that no longer offers it (a forwarded or
    # pre-change one), answers politely rather than "not wired" - and twice, identically,
    # because there is no first-tap/second-tap distinction left to make
    for _ in range(2):
        tapped = tap(f"k:menu:{menu_id}")
        assert "already in force" in tapped and "/cook replace" in tapped, tapped
    assert run_job(menu_propose.JOB, dt.date(2026, 9, 12)) == "already"


def test_bad_dishes_reprompt_then_repeat(world) -> None:
    _pid, db, tg, gateways, _say, _tap, run_job = world
    segment = menu.Segment(MONDAY, dt.date(2026, 9, 18))
    # first a dish that breaks the variety rule, then one that cannot be portioned
    varied = proposal_for(segment)
    varied["days"][1]["slots"][0]["recipe"]["name"] = "Something else"
    thin = proposal_for(segment)
    for day in thin["days"]:
        for slot in day["slots"]:
            slot["recipe"]["ingredients"] = [{"i": 1, "g": 10}]
    gateways.append(FakeGateway([varied, thin]))
    assert run_job(menu_propose.JOB, dt.date(2026, 9, 12)) == "degraded"
    assert len(gateways[0].calls) == menu_propose.MAX_ATTEMPTS
    assert "The previous attempt failed:" in gateways[0].calls[1]
    assert "variety 'same'" in gateways[0].calls[1]
    # every attempt after the first carries what the earlier ones learned, so a day that
    # misses fat and then fibre is not condemned to alternate between them
    assert "variety 'same'" in gateways[0].calls[-1], "the first lesson survived to the last try"
    row = db.one("SELECT status, detail FROM run_ledger WHERE job = 'menu_propose'")
    assert row is not None and "attempt 1" in row["detail"] and "attempt 2" in row["detail"]
    # nothing to repeat and no menu. This person IS the owner, so he is told once, in words:
    # alerting the admin about himself sent the same failure twice, and the copy written for
    said = " ".join(m["text"] for m in tg.sent)
    assert "could not build a menu" in said
    assert "Alex has been told why" not in said, "he is Alex"
    assert "menu_failed" not in said, "the alert is for a failure the person cannot read"
    assert db.scalar("SELECT COUNT(*) FROM menus") == 0


def test_every_refusal_being_simplicity_says_so_not_targets(world) -> None:
    _pid, db, tg, gateways, _say, _tap, run_job = world
    segment = menu.Segment(MONDAY, dt.date(2026, 9, 18))
    fussy = proposal_for(segment)
    complex_method = [
        "Heat the oven to 200C.",
        "Air fry the chicken 15 minutes.",
        "Meanwhile boil the rice in a pan for 12 minutes.",
        "Roast the broccoli in the oven for 10 minutes.",
        "Fry the garlic in a large pan for 5 minutes.",
        "Combine everything in the pan.",
        "Serve hot.",
    ]
    for day in fussy["days"]:
        for slot in day["slots"]:
            if slot["slot"] == "dinner":
                slot["recipe"]["method"] = complex_method
    # identical every attempt: a model that never improves is refused every time, on the
    # same grounds, and the fallback still owes the person the right sentence
    gateways.append(FakeGateway([fussy] * menu_propose.MAX_ATTEMPTS))
    assert run_job(menu_propose.JOB, dt.date(2026, 9, 12)) == "degraded"
    assert len(gateways[0].calls) == menu_propose.MAX_ATTEMPTS
    assert "is not simple" in gateways[0].calls[1], "the model was re-asked with why it failed"
    said = " ".join(m["text"] for m in tg.sent)
    assert "too complex to cook" in said, said
    assert "hits your targets" not in said, f"a simplicity refusal blamed on macros: {said!r}"
    assert db.scalar("SELECT COUNT(*) FROM menus") == 0


def test_a_failed_build_repeat_answers_to_a_refusal_made_since(world) -> None:
    """6 Oct: the scheduled build failed all four attempts for the partner and repeated her last
    menu
    with nobody watching, through a path with no refused-food gate (only `/cook repeat` had one).
    A food refused since the last menu is never served back quietly: the repeat goes through
    the same swap as `/cook repeat`, and when the recipe book holds nothing to swap in - here
    every lunch and dinner holds broccoli - it says so, to the person and on the strip."""
    pid, db, tg, gateways, say, _tap, run_job = world
    first = menu.Segment(dt.date(2026, 9, 7), dt.date(2026, 9, 11))
    plan = menu.build_plan(
        db,
        pid,
        first,
        proposal_for(first),
        targets=Macros(2250, 200, 70, 200, 30),
        profile=db.one("SELECT * FROM profiles WHERE person_id = ?", (pid,)),
        items=catalogue.approved(db, pid),
        model_used="claude-opus-5",
        clock=db.clock,
    )
    db.clock.set_local(dt.datetime(2026, 9, 6, 18, 0, tzinfo=TZ))
    old_id = menu.store_plan(db, plan, status="proposed", clock=db.clock)
    menu.decide(db, old_id, "accepted")
    db.clock.set_local(dt.datetime(2026, 9, 12, 10, 0, tzinfo=TZ))
    say("I do not want broccoli")
    assert "broccoli" in catalogue.refused_terms(db, pid)
    db.clock.set_local(dt.datetime(2026, 9, 12, 18, 0, tzinfo=TZ))
    gateways.append(FakeGateway([]))
    run_job(menu_propose.JOB, dt.date(2026, 9, 12))
    assert db.one("SELECT 1 FROM menus WHERE status = 'repeated'") is not None
    row = db.one(
        "SELECT status, detail FROM run_ledger WHERE job = 'menu_propose' ORDER BY rowid DESC"
    )
    assert row["status"] == "degraded" and "refused dishes" in str(row["detail"]), dict(row)
    said = tg.sent[-1]["text"]
    assert "Nothing you have eaten before can stand in for" in said, said


def test_a_failed_build_repeats_the_last_accepted_menu(world) -> None:
    pid, db, tg, gateways, _say, _tap, run_job = world
    first = menu.Segment(dt.date(2026, 9, 7), dt.date(2026, 9, 11))
    plan = menu.build_plan(
        db,
        pid,
        first,
        proposal_for(first),
        targets=Macros(2250, 200, 70, 200, 30),
        profile=db.one("SELECT * FROM profiles WHERE person_id = ?", (pid,)),
        items=catalogue.approved(db, pid),
        model_used="claude-opus-5",
        clock=db.clock,
    )
    # last week's menu, stored when it was built - the Sunday before it - since a day that has
    # gone is never open to a new menu
    db.clock.set_local(dt.datetime(2026, 9, 6, 18, 0, tzinfo=TZ))
    old_id = menu.store_plan(db, plan, status="proposed", clock=db.clock)
    menu.decide(db, old_id, "accepted")
    db.clock.set_local(dt.datetime(2026, 9, 12, 18, 0, tzinfo=TZ))
    # every attempt at a fresh Mon-Fri menu comes back with nothing usable
    gateways.append(FakeGateway([]))
    run_job(menu_propose.JOB, dt.date(2026, 9, 12))
    repeated = db.one("SELECT * FROM menus WHERE status = 'repeated' ORDER BY id DESC LIMIT 1")
    assert repeated is not None and repeated["source_menu_id"] == old_id
    assert str(repeated["segment_start"]) == MONDAY.isoformat()
    assert menu.meals_for_day(db, pid, MONDAY) != []
    # current without anyone deciding anything at all - `status` alone is enough
    assert menu.decision_of(db, int(repeated["id"])) is None
    assert "Menu repeated" in " ".join(m["text"] for m in tg.sent)


def test_shopping_list_after_acceptance(world) -> None:
    pid, db, tg, gateways, say, tap, run_job = world
    segment = menu.Segment(MONDAY, dt.date(2026, 9, 18))
    gateways.append(FakeGateway([proposal_for(segment)]))
    run_job(menu_propose.JOB, dt.date(2026, 9, 12))
    menu_id = int(db.scalar("SELECT id FROM menus ORDER BY id DESC LIMIT 1"))
    tap(f"k:menu:{menu_id}")
    tg.sent.clear()
    assert run_job(shopping_list.JOB, dt.date(2026, 9, 13)) == "ok"
    text = tg.sent[-1]["text"]
    assert text.startswith("<b>Shopping for Mon 14 Sep</b>")
    assert "Meat:" in text and "chicken" in text.lower()
    # each pack and the days that cook from it, inside the ten lines
    assert any(
        line.startswith("Chicken") and ": Mon, Tue, Wed, Thu, Fri; " in line
        for line in text.splitlines()
    ), text
    assert len(text.splitlines()) <= 10
    items = db.all(
        "SELECT i.code, si.packs, si.grams_needed, si.leftover_g FROM shopping_items si "
        "JOIN ingredients i ON i.id = si.ingredient_id "
        "JOIN shopping_lists sl ON sl.id = si.list_id "
        "WHERE sl.person_id = ? ORDER BY i.code",
        (pid,),
    )
    assert items, "no shopping items"
    by_code = {str(r["code"]): r for r in items}
    chicken = by_code["chicken"]
    assert int(chicken["packs"]) >= 2  # 5 days x ~420 g needs at least two 500 g packs
    assert float(chicken["leftover_g"]) == pytest.approx(
        int(chicken["packs"]) * 500 - float(chicken["grams_needed"]), abs=0.5
    )
    assert db.scalar("SELECT COUNT(*) FROM leftovers WHERE person_id = ?", (pid,)) > 0
    # /shopping shows it back with tick buttons
    listed = say("/shopping")
    # the header names the days now: two lists can sit in the same chat (Friday's, and the one a
    # /cook replace made on Sunday) and an undated header cannot tell them apart
    assert listed.startswith("<b>Shopping ") and "Sep</b>" in listed.splitlines()[0], listed
    kb = [b["callback_data"] for row in tg.sent[-1]["reply_markup"]["inline_keyboard"] for b in row]
    assert kb and all(d.startswith("k:tick:") for d in kb)
    item_id = int(kb[0].split(":")[2])
    tap(kb[0])
    assert db.scalar("SELECT ticked FROM shopping_ticks WHERE item_id = ?", (item_id,)) == 1
    tap(kb[0])
    assert (
        db.scalar(
            "SELECT ticked FROM shopping_ticks WHERE item_id = ? ORDER BY id DESC LIMIT 1",
            (item_id,),
        )
        == 0
    )


def test_rate_and_recipe_commands(world) -> None:
    pid, db, tg, gateways, say, tap, run_job = world
    segment = menu.Segment(MONDAY, dt.date(2026, 9, 18))
    gateways.append(FakeGateway([proposal_for(segment)]))
    run_job(menu_propose.JOB, dt.date(2026, 9, 12))
    menu_id = int(db.scalar("SELECT id FROM menus ORDER BY id DESC LIMIT 1"))
    tap(f"k:menu:{menu_id}")
    db.clock.set_local(dt.datetime(2026, 9, 14, 21, 0, tzinfo=TZ))
    assert "How was Chicken, rice and broccoli?" in say("/rate")
    kb = [b["callback_data"] for row in tg.sent[-1]["reply_markup"]["inline_keyboard"] for b in row]
    assert len(kb) == 5
    assert "5/5" in tap(kb[4]) and "come back often" in tg.sent[-1]["text"]
    assert db.scalar("SELECT score FROM ratings WHERE person_id = ?", (pid,)) == 5
    card = say("/recipe")
    # the day's MAIN meal: ordering by ms.id DESC gave the last slot inserted, so /recipe
    # reliably answered with the snack while the dinner on the menu could not be opened
    assert "Chicken, rice and broccoli" in card
    assert "kcal" in card and len(card.splitlines()) <= menuflow.RECIPE_MAX_LINES
    favourites, blocked = menu.rated_names(db, pid)
    assert favourites == ["Chicken, rice and broccoli"] and blocked == []


def test_build_plan_rejects_bad_indexes_and_infeasible_targets(
    fixture_db: Db, two_persons, frozen_clock
) -> None:
    pid = two_persons["owner"]
    fixture_db.insert(
        "profiles",
        person_id=pid,
        effective_from="2026-09-01",
        sex="m",
        birth_date="1990-03-15",
        height_cm=183.0,
        activity_factor=1.55,
        kcal_floor=1800,
        meals_per_day=4,
    )
    seed_catalogue(fixture_db)
    profile = fixture_db.one("SELECT * FROM profiles WHERE person_id = ?", (pid,))
    items = catalogue.approved(fixture_db, pid)
    segment = menu.Segment(MONDAY, MONDAY)
    payload = proposal_for(segment)
    payload["days"][0]["slots"][0]["recipe"]["ingredients"] = [{"i": 99, "g": 100}]
    with pytest.raises(ModelError, match="index"):
        menu.build_plan(
            fixture_db,
            pid,
            segment,
            payload,
            targets=Macros(2250, 200, 70, 200, 30),
            profile=profile,
            items=items,
            clock=frozen_clock,
        )
    with pytest.raises(ModelError, match="missing"):
        menu.build_plan(
            fixture_db,
            pid,
            segment,
            {
                "days": [
                    {
                        "day": MONDAY.isoformat(),
                        "slots": [
                            {
                                "slot": "breakfast",
                                "recipe": {
                                    "name": "x",
                                    "ingredients": [{"i": 1, "g": 10}],
                                    "method": ["a"],
                                    "prep_minutes": 5,
                                },
                            }
                        ],
                    }
                ]
            },
            targets=Macros(2250, 200, 70, 200, 30),
            profile=profile,
            items=items,
            clock=frozen_clock,
        )
    with pytest.raises(Infeasible):
        menu.build_plan(
            fixture_db,
            pid,
            segment,
            proposal_for(segment),
            targets=Macros(12000, 900, 70, 200, 30),
            profile=profile,
            items=items,
            clock=frozen_clock,
        )
    assert fixture_db.scalar("SELECT COUNT(*) FROM menus") == 0


def test_a_recipe_card_is_not_a_dead_end(fixture_db, two_persons) -> None:
    from tiffin.jobs import menu_propose

    pid = two_persons["owner"]
    menu_id = fixture_db.insert(
        "menus",
        person_id=pid,
        segment_start="2026-09-08",
        segment_end="2026-09-09",
        status="proposed",
    )
    day = fixture_db.insert(
        "menu_days",
        menu_id=menu_id,
        day="2026-09-08",
        kcal=2000,
        protein_g=180.0,
        fat_g=60.0,
        carb_g=180.0,
        fibre_g=30.0,
    )
    for slot in ("breakfast", "lunch", "dinner"):
        rid = fixture_db.insert(
            "recipes",
            name=f"{slot} dish",
            servings=1,
            prep_minutes=10,
            method_json='["cook"]',
            source="model",
        )
        fixture_db.insert(
            "meal_slots",
            menu_day_id=day,
            slot=slot,
            recipe_id=rid,
            portion_multiplier=1.0,
            portion_g=300.0,
            kcal=500,
            protein_g=40.0,
            fat_g=15.0,
            carb_g=40.0,
            fibre_g=6.0,
        )

    # the menu offers every meal
    labels = [b for row in menu_propose.meal_buttons(fixture_db, menu_id) for b, _ in row]
    assert labels == ["Breakfast", "Lunch", "Dinner"]

    # a card offers the OTHER meals, never itself
    from_lunch = menu_propose.meal_buttons(fixture_db, menu_id, exclude="lunch")
    assert [b for row in from_lunch for b, _ in row] == ["Breakfast", "Dinner"]

    # and the slot resolves to a real recipe at its planned portion
    found = menu_propose.recipe_at(fixture_db, menu_id, "lunch")
    assert found is not None and found[1] == 1.0
    assert menu_propose.recipe_at(fixture_db, menu_id, "supper") is None


def test_the_menu_can_be_reopened_from_anywhere(fixture_db, two_persons) -> None:
    from tiffin.jobs import menu_propose

    pid = two_persons["owner"]
    menu_id = fixture_db.insert(
        "menus",
        person_id=pid,
        segment_start="2026-09-08",
        segment_end="2026-09-08",
        status="proposed",
    )
    day = fixture_db.insert(
        "menu_days",
        menu_id=menu_id,
        day="2026-09-08",
        kcal=2000,
        protein_g=180.0,
        fat_g=60.0,
        carb_g=180.0,
        fibre_g=30.0,
    )
    rid = fixture_db.insert(
        "recipes",
        name="Only dish",
        servings=1,
        prep_minutes=5,
        method_json='["eat"]',
        source="model",
    )
    fixture_db.insert(
        "meal_slots",
        menu_day_id=day,
        slot="dinner",
        recipe_id=rid,
        portion_multiplier=1.0,
        portion_g=300.0,
        kcal=500,
        protein_g=40.0,
        fat_g=15.0,
        carb_g=40.0,
        fibre_g=6.0,
    )
    html, payload = menu_propose.message_for(fixture_db, menu_id)
    assert "Dinner: Only dish" in html and payload["menu_id"] == menu_id


def test_a_long_shopping_list_says_what_it_held_back() -> None:
    """Cutting at ten lines meant the last aisles never reached the shop, silently."""
    from tiffin.jobs.shopping_list import _capped

    short = [f"line {n}" for n in range(6)]
    text, held = _capped(short)
    assert held == 0 and text.count("\n") == 5

    long_list = [f"line {n}" for n in range(25)]
    text, held = _capped(long_list)
    assert len(text.splitlines()) == 10, "the house cap on an unprompted message still holds"
    assert held == 16, "25 lines less the 9 kept"
    assert text.splitlines()[-1] == "16 more lines: send /shopping"
    assert "line 24" not in text


def test_the_weekend_gets_a_menu_whatever_the_weekday_cadence(fixture_db: Db, two_persons) -> None:
    """All three offered cadences cover Mon-Fri, so Saturday and Sunday had no menu at all."""
    pid = two_persons["owner"]
    for spec in ([[0, 1, 2, 3, 4]], [[0, 1, 2], [3, 4]], [[0], [1], [2], [3], [4]]):
        fixture_db.insert(
            "profiles",
            person_id=pid,
            effective_from="2026-09-01",
            sex="m",
            birth_date="1990-03-15",
            height_cm=183.0,
            activity_factor=1.55,
            kcal_floor=1800,
            segment_days=json.dumps(spec),
        )
        prof = fixture_db.one(
            "SELECT * FROM profiles WHERE person_id = ? ORDER BY id DESC LIMIT 1", (pid,)
        )
        segs = menu.segments_for(prof, MONDAY)
        covered = {
            d
            for s in segs
            for d in [
                (s.start + dt.timedelta(days=i)).weekday()
                for i in range((s.end - s.start).days + 1)
            ]
        }
        assert {5, 6} <= covered, f"{spec} still leaves the weekend without a menu"


def test_someone_who_already_cooks_at_the_weekend_gets_no_extra_segment(
    fixture_db: Db, two_persons
) -> None:
    pid = two_persons["owner"]
    fixture_db.insert(
        "profiles",
        person_id=pid,
        effective_from="2026-09-01",
        sex="m",
        birth_date="1990-03-15",
        height_cm=183.0,
        activity_factor=1.55,
        kcal_floor=1800,
        segment_days=json.dumps([[5, 6]]),
    )
    prof = fixture_db.one(
        "SELECT * FROM profiles WHERE person_id = ? ORDER BY id DESC LIMIT 1", (pid,)
    )
    assert len(menu.segments_for(prof, MONDAY)) == 1, "not invented twice"


def test_a_saturday_that_cannot_build_a_menu_still_says_so(world) -> None:
    """The scheduled job threw its own explanation away.

    _propose_for returns what the person should be told and the Saturday job discarded it, so
    a week with no menu arrived as silence - and for her the only thing sent was an alert to
    somebody else's phone. Degrade to manual, never to silence.
    """
    _pid, db, tg, gateways, _say, _tap, run_job = world
    segment = menu.Segment(MONDAY, dt.date(2026, 9, 18))
    thin = proposal_for(segment)
    for day in thin["days"]:
        for slot in day["slots"]:
            slot["recipe"]["ingredients"] = [{"i": 1, "g": 10}]
    gateways.append(FakeGateway([thin, thin]))
    assert run_job(menu_propose.JOB, dt.date(2026, 9, 12)) == "degraded"
    assert db.scalar("SELECT COUNT(*) FROM menus") == 0
    said = " ".join(m["text"] for m in tg.sent)
    assert "could not build a menu" in said, "the person heard nothing at all"


def test_the_failure_message_is_sent_once_per_segment(world) -> None:
    """It goes through the outbox with the segment as its key, so a retry of the same slot
    does not tell somebody twice that there is no menu."""
    _pid, db, tg, gateways, _say, _tap, run_job = world
    segment = menu.Segment(MONDAY, dt.date(2026, 9, 18))
    thin = proposal_for(segment)
    for day in thin["days"]:
        for slot in day["slots"]:
            slot["recipe"]["ingredients"] = [{"i": 1, "g": 10}]
    gateways.append(FakeGateway([thin, thin, thin, thin]))
    run_job(menu_propose.JOB, dt.date(2026, 9, 12))
    first = len([m for m in tg.sent if "could not build a menu" in m["text"]])
    assert first == 1
    assert (
        db.scalar(
            "SELECT COUNT(*) FROM messages_out WHERE kind = 'menu_failed' AND due_key = ?",
            (MONDAY.isoformat(),),
        )
        == 1
    )
