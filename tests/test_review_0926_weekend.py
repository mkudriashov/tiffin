"""Tests for shared weekend menus and individual portions."""

from __future__ import annotations

import datetime as dt
import json
from typing import Any, cast

import pytest

from tiffin import days as days_mod
from tiffin.clock import TZ, FrozenClock
from tiffin.db import Db
from tiffin.energy import Macros
from tiffin.food import menu, recipes, shared
from tiffin.food.recipes import RecipeDraft
from tiffin.persons import PersonRegistry

from .test_menu import a_day, seed_catalogue

SAT = dt.date(2026, 9, 19)
SUN = dt.date(2026, 9, 20)
WEEKEND = menu.Segment(SAT, SUN)
HIS = Macros(2250, 200, 70, 200, 30)
PARTNERS = Macros(1700, 151, 53, 151, 23)


def _profile(db: Db, pid: int, **over: Any) -> None:
    row = {
        "person_id": pid,
        "effective_from": "2026-09-01",
        "sex": "m",
        "birth_date": "1990-03-15",
        "height_cm": 183.0,
        "activity_factor": 1.55,
        "kcal_floor": 1400,
        "meals_per_day": 4,
        "segment_days": json.dumps([[0, 1, 2, 3, 4]]),
        "variety_json": json.dumps({"all": "same"}),
    }
    row.update(over)
    db.insert("profiles", **row)


def _agree(db: Db, pid: int, *, shared_too: bool = True) -> None:
    db.insert(
        "consents", person_id=pid, kind="anthropic_processing", granted=1, wording_version="v2"
    )
    if shared_too:
        db.insert(
            "consents", person_id=pid, kind="shared_meals", granted=1, wording_version="shared_v1"
        )


@pytest.fixture
def pair(fixture_db: Db, two_persons, frozen_clock: FrozenClock, config):
    frozen_clock.set_local(dt.datetime(2026, 9, 17, 18, 0, tzinfo=TZ))  # Thursday
    seed_catalogue(fixture_db)
    _profile(fixture_db, two_persons["owner"])
    _profile(fixture_db, two_persons["partner"], sex="f", fibre_g_min=15)
    # a weight each: a weekend with no targets to be had for one of them is not shared
    for pid, kg in ((two_persons["owner"], 90.0), (two_persons["partner"], 64.0)):
        fixture_db.insert(
            "weigh_ins", person_id=pid, day="2026-09-15", weight_kg=kg, source="manual"
        )
    registry = PersonRegistry(fixture_db, config, clock=frozen_clock)
    persons = [registry.require("owner"), registry.require("partner")]
    return fixture_db, persons, frozen_clock


def _weekend_payload() -> dict:
    """Test_menu's day, at an ordinary single serving - which is what the weekend prompt asks
    the model for - so both plates are a scaling of it, one each side of 1.0."""
    days = [a_day(SAT), a_day(SUN)]
    for day in days:
        for slot in day["slots"]:
            for ing in slot["recipe"]["ingredients"]:
                ing["g"] = round(ing["g"] * 0.6)
    # a different dish each day, which the weekend now checks rather than only asks for
    for slot in days[1]["slots"]:
        slot["recipe"]["name"] += ", Sunday's way"
    return {"days": days}


def _build(db: Db, persons, clock) -> shared.Built:
    profiles = {
        p.id: db.one("SELECT * FROM profiles WHERE person_id = ?", (p.id,)) for p in persons
    }
    return shared.build(
        db,
        WEEKEND,
        persons,
        profiles,
        {persons[0].id: HIS, persons[1].id: PARTNERS},
        lambda _prompt, _hint: (_weekend_payload(), "fake"),
        prompt_for=lambda hint: "prompt" + hint,
        clock=clock,
    )


# ---------------------------------------------------------------- the gate
def test_nothing_is_shared_until_both_have_said_yes(pair) -> None:
    db, (him, partner), _clock = pair
    assert not shared.active(db, WEEKEND), "no consent at all"
    _agree(db, him.id)
    assert not shared.active(db, WEEKEND), "one yes is not two"
    _agree(db, partner.id)
    assert shared.active(db, WEEKEND)
    assert not shared.active(db, menu.Segment(dt.date(2026, 9, 21), dt.date(2026, 9, 25)))
    db.insert(
        "consents",
        person_id=partner.id,
        kind="shared_meals",
        granted=0,
        wording_version="shared_v1",
    )
    assert not shared.active(db, WEEKEND), "a later no wins, at once"


def test_a_food_either_refuses_is_not_on_the_shared_list(pair) -> None:
    db, (him, partner), _clock = pair
    codes_before = [i.code for i in shared.shared_items(db, [him.id, partner.id])]
    assert "broccoli" in codes_before
    partner_profile = dict(db.one("SELECT * FROM profiles WHERE person_id = ?", (partner.id,)))
    partner_profile.pop("id")
    partner_profile.pop("created_at")
    partner_profile["refused_foods"] = json.dumps(["broccoli"])
    partner_profile["effective_from"] = "2026-09-02"
    db.insert("profiles", **partner_profile)
    codes = [i.code for i in shared.shared_items(db, [him.id, partner.id])]
    assert "broccoli" not in codes, "her refusal keeps it off BOTH plates"


# ---------------------------------------------------------------- the build
def test_one_answer_two_plates_same_dishes_own_portions(pair) -> None:
    db, (him, partner), clock = pair
    _agree(db, him.id)
    _agree(db, partner.id)
    built = _build(db, [him, partner], clock)
    mine, partners = built.menu_ids[him.id], built.menu_ids[partner.id]

    def slots_of(mid: int) -> dict[tuple[str, str], Any]:
        return {
            (str(r["day"]), str(r["slot"])): r
            for r in db.all(
                "SELECT md.day, ms.slot, ms.recipe_id, ms.portion_multiplier, ms.kcal FROM "
                "menu_days md JOIN meal_slots ms ON ms.menu_day_id = md.id WHERE md.menu_id = ?",
                (mid,),
            )
        }

    a, b = slots_of(mine), slots_of(partners)
    assert a.keys() == b.keys()
    assert all(a[k]["recipe_id"] == b[k]["recipe_id"] for k in a), "the same dish for both"
    assert sum(a[k]["kcal"] for k in a) > sum(b[k]["kcal"] for k in b), "his plate is bigger"
    # each person's day reads their OWN plate - nothing downstream had to learn a new shape
    his_sat = days_mod.planned_macros(db, him.id, SAT)
    partner_sat = days_mod.planned_macros(db, partner.id, SAT)
    assert his_sat is not None and partner_sat is not None
    assert abs(his_sat.kcal - HIS.kcal) <= HIS.kcal * 0.05
    assert abs(partner_sat.kcal - PARTNERS.kcal) <= PARTNERS.kcal * 0.05
    members = db.all(
        "SELECT person_id, menu_id FROM shared_menu_members WHERE shared_menu_id = ?",
        (built.shared_id,),
    )
    assert {(int(r["person_id"]), int(r["menu_id"])) for r in members} == {
        (him.id, mine),
        (partner.id, partners),
    }


def test_each_sees_the_other_plate_only_while_both_agree(pair) -> None:
    db, (him, partner), clock = pair
    _agree(db, him.id)
    _agree(db, partner.id)
    built = _build(db, [him, partner], clock)
    seen = shared.partner_plate(db, him.id, built.menu_ids[him.id], SAT, "lunch")
    assert seen is not None and seen["name"] == partner.display_name and seen["kcal"] > 0
    db.insert(
        "consents",
        person_id=partner.id,
        kind="shared_meals",
        granted=0,
        wording_version="shared_v1",
    )
    assert shared.partner_plate(db, him.id, built.menu_ids[him.id], SAT, "lunch") is None


def test_a_slot_only_one_of_them_has_stays_theirs(pair) -> None:
    """His pre-bed pot is his: the answer carries it, and her menu never gets it."""
    db, (him, partner), clock = pair
    _agree(db, him.id)
    _agree(db, partner.id)
    prof = dict(db.one("SELECT * FROM profiles WHERE person_id = ?", (him.id,)))
    prof.pop("id")
    prof.pop("created_at")
    prof.update(effective_from="2026-09-02", pre_bed_protein=1)
    db.insert("profiles", **prof)
    payload = _weekend_payload()
    for day in payload["days"]:
        day["slots"].append(
            {
                "slot": "pre_bed",
                "recipe": {
                    "name": "Casein-style yoghurt pot",
                    "ingredients": [{"i": 3, "g": 150}, {"i": 8, "g": 20}],
                    "method": ["Stir together."],
                    "prep_minutes": 2,
                },
            }
        )
    profiles = {
        p.id: db.one(
            "SELECT * FROM profiles WHERE person_id = ? "
            "ORDER BY effective_from DESC, id DESC LIMIT 1",
            (p.id,),
        )
        for p in (him, partner)
    }
    built = shared.build(
        db,
        WEEKEND,
        [him, partner],
        profiles,
        {him.id: HIS, partner.id: PARTNERS},
        lambda _prompt, _hint: (payload, "fake"),
        prompt_for=lambda hint: "prompt" + hint,
        clock=clock,
    )

    def slots(mid: int) -> set[str]:
        return {
            str(r["slot"])
            for r in db.all(
                "SELECT ms.slot FROM menu_days md JOIN meal_slots ms ON ms.menu_day_id = md.id "
                "WHERE md.menu_id = ?",
                (mid,),
            )
        }

    assert "pre_bed" in slots(built.menu_ids[him.id])
    assert "pre_bed" not in slots(built.menu_ids[partner.id])
    assert shared.union_slots(profiles)[-1] == "pre_bed"


def test_the_weekend_has_no_effort_limits(pair) -> None:
    """The owner, 26 Sep: "we are fine to cook longer on the weekend if it's really good food". The
    first treat weekend was refused twice for a "meanwhile" in a breakfast and for chips beside
    cornflake chicken; a weekday cook is still held to after-work limits."""
    import dataclasses

    db, _persons, _clock = pair
    cat = shared.shared_items(db, [p.id for p in _persons])
    proper = RecipeDraft.from_dict(
        {
            "name": "Cornflake-crust chicken with chips and garlic mayo",
            "ingredients": [{"i": n, "g": 20} for n in range(1, min(len(cat), 12) + 1)],
            "method": [
                "Marinate the chicken in the yoghurt for 30 minutes.",
                "Cut the potatoes into chips and air fry at 200C for 25 minutes.",
                "Meanwhile, crush the cornflakes and coat the chicken.",
                "Bake the chicken in the oven for 20 minutes.",
                "Fry the onions in a large pan for 10 minutes.",
                "Warm the sauce in a small saucepan for 5 minutes.",
                *[f"Step {n}." for n in range(7, 13)],
            ],
            "prep_minutes": 110,
        }
    )
    assert recipes.simplicity_errors(proper, cat, "dinner"), "too much for after work"
    assert recipes.simplicity_errors(proper, cat, "breakfast"), "and for a weekday breakfast"
    for slot in ("breakfast", "lunch", "dinner", "snack", "pre_bed"):
        assert not recipes.simplicity_errors(proper, cat, slot, weekend=True), slot
    assert not recipes.validate(proper, cat), "still what a card and the table hold"
    too_long = dataclasses.replace(proper, prep_minutes=150)
    assert recipes.validate(too_long, cat), "the card's own ceiling still binds"


def test_the_weekend_prompts_cap_nothing_but_the_card() -> None:
    from tiffin.model import prompts

    day = [{"day": "2026-09-26", "slots": "breakfast, dinner", "kcal": 2000}]
    texts = [
        prompts.menu_shared_weekend(ingredients=["Cod 75 kcal"], days=day),
        prompts.menu_propose(
            ingredients=["Cod 75 kcal"],
            days=[{**day[0], "protein_g": 150, "fat_g": 60}],
            variety={"dinner": "different"},
            weekend=True,
        ),
    ]
    for text in texts:
        assert "no limit on effort" in text
        assert "at most 20 minutes" not in text and "at most 60 minutes" not in text
        assert "at most 2 hours" in text


def test_each_persons_own_weekend_is_built_beside_the_shared_one_and_kept(
    pair, config, tmp_path
) -> None:
    from tiffin import scheduler as sc
    from tiffin.app import App
    from tiffin.food import weekend
    from tiffin.jobs import menu_propose
    from tiffin.secrets import Secrets

    from .fakes import FakeTelegram
    from .test_menu import FakeGateway

    db, (him, partner), clock = pair
    _agree(db, him.id)
    _agree(db, partner.id)
    for pid, m in ((him.id, HIS), (partner.id, PARTNERS)):
        db.insert(
            "targets", person_id=pid, week_start="2026-09-14", kcal=m.kcal,
            protein_g=m.protein_g, fat_g=m.fat_g, carb_g=m.carb_g, fibre_g=m.fibre_g,
            source="formula",
        )  # fmt: skip
    secrets_dir = tmp_path / "s"
    secrets_dir.mkdir()
    (secrets_dir / "telegram-token-dev.txt").write_text("1:t", encoding="utf-8")
    gw = FakeGateway([_weekend_payload() for _ in range(8)])
    app = App(
        config,
        Secrets(secrets_dir),
        clock=clock,
        transport=FakeTelegram(),
        gateway_factory=lambda _a, _d: cast("Any", gw),
    )
    thursday = dt.date(2026, 9, 17)
    for p in (him, partner):
        if p is partner:
            db.insert("weekend_modes", mode="apart", person_id=partner.id)
        prof = db.one("SELECT * FROM profiles WHERE person_id = ?", (p.id,))
        slot = sc.slots_for(menu_propose.JOB, p.id, prof, thursday, db)[0]
        clock.set_local(slot.due_local)
        sc.Scheduler(app, [menu_propose.JOB], lambda _p, _fn: None).execute(db, slot)
    own = weekend.versions(db, WEEKEND).apart
    assert set(own) == {him.id, partner.id}, "each person's own version, built on Thursday"
    assert menu.accepted_menu_id(db, him.id, SAT) is None, "kept while the switch said together"
    assert menu.accepted_menu_id(db, partner.id, SAT) == own[partner.id], (
        "in force once it said apart"
    )
    sent = {
        int(r["person_id"])
        for r in db.all("SELECT person_id FROM messages_out WHERE kind = 'menu_proposal'")
    }
    assert sent == {partner.id}, "only the version in force is sent"


def test_consent_button_writes_the_shared_row(pair, config, tmp_path) -> None:
    from tiffin.app import App
    from tiffin.secrets import Secrets
    from tiffin.telegram.poller import Poller

    from .fakes import FakeTelegram

    db, (him, _partner), clock = pair
    secrets_dir = tmp_path / "s"
    secrets_dir.mkdir()
    (secrets_dir / "telegram-token-dev.txt").write_text("1:t", encoding="utf-8")
    tg = FakeTelegram()
    app = App(config, Secrets(secrets_dir), clock=clock, transport=tg)
    handlers = app.handlers(db)
    poller = Poller(
        app.api(), db, app.registry(db), handlers.handle, clock=clock, sleeper=lambda s: None
    )
    tg.queue_update(tg.callback_update(1, 1000001, "k:consent:share:1", message_id=5))
    poller.run_once()
    assert shared.consented(db, him.id)
    assert "once you have both said yes" in tg.sent[-1]["text"]
    assert "/consent" in tg.sent[-1]["text"], "and says how the other person says yes"


def test_thursday_the_job_builds_the_weekend_for_both_and_tells_both(
    pair, config, tmp_path
) -> None:
    from tiffin import scheduler as sc
    from tiffin.app import App
    from tiffin.jobs import menu_shared
    from tiffin.secrets import Secrets

    from .fakes import FakeTelegram
    from .test_menu import FakeGateway

    db, (him, partner), clock = pair
    _agree(db, him.id)
    _agree(db, partner.id)
    for pid, m in ((him.id, HIS), (partner.id, PARTNERS)):
        db.insert(
            "targets",
            person_id=pid,
            week_start="2026-09-14",
            kcal=m.kcal,
            protein_g=m.protein_g,
            fat_g=m.fat_g,
            carb_g=m.carb_g,
            fibre_g=m.fibre_g,
            source="formula",
        )
    secrets_dir = tmp_path / "s"
    secrets_dir.mkdir()
    (secrets_dir / "telegram-token-dev.txt").write_text("1:t", encoding="utf-8")
    tg = FakeTelegram()
    gw = FakeGateway([_weekend_payload()])
    app = App(
        config,
        Secrets(secrets_dir),
        clock=clock,
        transport=tg,
        gateway_factory=lambda _a, _d: cast("Any", gw),
    )
    slot = sc.slots_for(menu_shared.JOB, 0, None, dt.date(2026, 9, 17), db)[0]
    clock.set_local(slot.due_local)
    status = sc.Scheduler(app, [menu_shared.JOB], lambda _p, _fn: None).execute(db, slot)
    assert status == "ok", db.one("SELECT * FROM run_ledger WHERE job = 'menu_shared'")
    assert len(gw.calls) == 1, "one model call writes both plates"
    assert "healthy versions of indulgent food" in gw.calls[0]
    assert str(HIS.kcal) not in gw.calls[0] and str(PARTNERS.kcal) not in gw.calls[0], (
        "neither person's own target is named to the model, only a shared scale"
    )
    sent = db.all("SELECT person_id, text_html FROM messages_out WHERE kind = 'menu_proposal'")
    assert {int(r["person_id"]) for r in sent} == {him.id, partner.id}
    assert all("Weekend menu, cooked together" in str(r["text_html"]) for r in sent)
    assert menu.accepted_menu_id(db, partner.id, SAT) is not None


def test_a_same_every_day_cook_still_gets_two_different_treat_days(pair) -> None:
    """Sat 26 Sep: the owner's profile says "same" for every slot (his batch-cooked weekdays), and
    the
    weekend brief asks for a different dish each day. The weekday rule refused every answer."""
    db, (him, partner), clock = pair
    _agree(db, him.id)
    _agree(db, partner.id)
    payload = _weekend_payload()
    for slot in payload["days"][1]["slots"]:
        slot["recipe"]["name"] = slot["recipe"]["name"] + " (Sunday)"
    profiles = {
        p.id: db.one("SELECT * FROM profiles WHERE person_id = ?", (p.id,)) for p in (him, partner)
    }
    assert "same" in str(profiles[him.id]["variety_json"])
    built = shared.build(
        db,
        WEEKEND,
        [him, partner],
        profiles,
        {him.id: HIS, partner.id: PARTNERS},
        lambda _prompt, _hint: (payload, "fake"),
        prompt_for=lambda hint: "prompt" + hint,
        clock=clock,
    )
    names = {
        str(r["name"])
        for r in db.all(
            "SELECT rc.name FROM menu_days md JOIN meal_slots ms ON ms.menu_day_id = md.id "
            "JOIN recipes rc ON rc.id = ms.recipe_id WHERE md.menu_id = ?",
            (built.menu_ids[him.id],),
        )
    }
    assert any(n.endswith("(Sunday)") for n in names) and any(
        not n.endswith("(Sunday)") for n in names
    )


def test_the_shared_day_is_balanced_at_the_stricter_share(pair) -> None:
    """26 Sep: given calories alone, the model wrote lean treats and the owner's fat floor was out
    of
    reach at any portion. The day the model balances against carries protein, fat and fibre at
    the stricter of the two people's shares, and still names neither person's own numbers."""
    from tiffin.jobs import menu_shared
    from tiffin.model import prompts

    db, (him, partner), _clock = pair
    macros = {him.id: HIS, partner.id: PARTNERS}
    fibre = {him.id: 25, partner.id: 15}
    profiles = {
        p.id: db.one("SELECT * FROM profiles WHERE person_id = ?", (p.id,)) for p in (him, partner)
    }
    days = menu_shared._scale_days(WEEKEND, profiles, macros, fibre)
    day = days[0]
    assert [d["day"] for d in days] == ["2026-09-19", "2026-09-20"]
    assert day["kcal"] == round((HIS.kcal + PARTNERS.kcal) / 2)
    for pid, m in macros.items():
        scale = m.kcal / day["kcal"]
        # met at their own size, with room above: aimed exactly at a floor, the portions
        # missed it by a gram four times running
        assert day["protein_g"] * scale >= m.protein_g * 1.04, "protein floor, with room"
        assert day["fat_g"] * scale >= m.fat_g * 1.04, "fat floor, with room"
        assert day["fibre_g"] * scale >= fibre[pid] * 1.04, "fibre floor, with room"
    text = prompts.menu_shared_weekend(ingredients=["Cod 75 kcal"], days=days)
    assert f"{day['fat_g']} g fat" in text and f"{day['fibre_g']} g fibre" in text
    assert "Lean is not the aim" in text
    for m in macros.values():
        assert f"{m.kcal} kcal" not in text and f"{m.protein_g} g protein" not in text


def test_a_slot_only_one_of_them_eats_carries_none_of_partner_floors(pair) -> None:
    """28 Sep targets: a day given over all five slots put 9 g of its fibre into the snack, which
    only the owner eats, and her four plates came out 3 g short. The figures are for the dishes
    both of
    them eat; the snack is named on its own, extra on top, with its own share."""
    from tiffin.jobs import menu_shared
    from tiffin.model import prompts

    db, (him, partner), _clock = pair
    _profile(db, partner.id, sex="f", fibre_g_min=25, meals_per_day=3, effective_from="2026-09-02")
    macros = {him.id: HIS, partner.id: PARTNERS}
    fibre = {him.id: 25, partner.id: 25}
    profiles = {
        p.id: db.one(
            "SELECT * FROM profiles WHERE person_id = ? ORDER BY effective_from DESC, id DESC",
            (p.id,),
        )
        for p in (him, partner)
    }
    his_slots = menu.slots_for(profiles[him.id])
    partner_slots = menu.slots_for(profiles[partner.id])
    assert len(his_slots) == len(partner_slots) + 1, (his_slots, partner_slots)
    day = menu_shared._scale_days(WEEKEND, profiles, macros, fibre)[0]
    extra = next(s for s in his_slots if s not in partner_slots)
    assert extra not in day["slots"] and extra.replace("_", "-") in day["also"]
    assert "one of them only" in day["also"]
    # her plates are the shared dishes alone, scaled to her calories: every floor met by them
    partner_scale = PARTNERS.kcal / day["kcal"]
    assert day["fibre_g"] * partner_scale >= fibre[partner.id], "her fibre, without the snack"
    assert day["protein_g"] * partner_scale >= PARTNERS.protein_g
    assert day["fat_g"] * partner_scale >= PARTNERS.fat_g
    text = prompts.menu_shared_weekend(ingredients=["Cod 75 kcal"], days=[day])
    assert "also: " in text and "for one of them only" in text


def test_a_refused_shared_weekend_is_told_what_to_reach_for(pair) -> None:
    from tiffin.jobs import menu_shared

    db, persons, _clock = pair
    items = shared.shared_items(db, [p.id for p in persons])
    seen, text = menu_shared.retry_text(
        [], "2026-09-26: fat: max reachable 66 g < floor 71 g", items
    )
    assert "fat per calorie" in text and "Olive oil" in text, text
    _seen, text = menu_shared.retry_text(
        seen, "2026-09-27: fibre: max reachable 19 g < 25 g", items
    )
    assert "fat: max reachable" in text and "fibre: max reachable" in text, "no lesson forgotten"


def test_the_shared_job_records_a_refusal_and_retries_with_it(pair, config, tmp_path) -> None:
    from tiffin import scheduler as sc
    from tiffin.app import App
    from tiffin.jobs import menu_shared
    from tiffin.secrets import Secrets

    from .fakes import FakeTelegram
    from .test_menu import FakeGateway

    db, (him, partner), clock = pair
    _agree(db, him.id)
    _agree(db, partner.id)
    for pid, m in ((him.id, HIS), (partner.id, PARTNERS)):
        db.insert(
            "targets",
            person_id=pid,
            week_start="2026-09-14",
            kcal=m.kcal,
            protein_g=m.protein_g,
            fat_g=m.fat_g,
            carb_g=m.carb_g,
            fibre_g=m.fibre_g,
            source="formula",
        )
    secrets_dir = tmp_path / "s"
    secrets_dir.mkdir()
    (secrets_dir / "telegram-token-dev.txt").write_text("1:t", encoding="utf-8")
    gw = FakeGateway([{"days": []}, _weekend_payload()])
    app = App(
        config,
        Secrets(secrets_dir),
        clock=clock,
        transport=FakeTelegram(),
        gateway_factory=lambda _a, _d: cast("Any", gw),
    )
    slot = sc.slots_for(menu_shared.JOB, 0, None, dt.date(2026, 9, 17), db)[0]
    clock.set_local(slot.due_local)
    status = sc.Scheduler(app, [menu_shared.JOB], lambda _p, _fn: None).execute(db, slot)
    row = db.one("SELECT status, detail FROM run_ledger WHERE job = 'menu_shared'")
    assert status == "ok", row
    assert row is not None and "shared attempt 1 refused: menu proposal has no days" in str(
        row["detail"]
    )
    assert len(gw.calls) == 2
    assert "The previous attempt failed: menu proposal has no days" in gw.calls[1]
    assert "g fat" in gw.calls[0], "the first ask already carries the day's balance"


def test_a_lopsided_day_names_the_cut_dishes_and_the_shortfall() -> None:
    """26 Sep: the treat weekend met the owner's protein only by serving the burger and the muffin
    at
    half a serving and the steak at 1.65. Such a day goes back to the model with the reason."""
    from tiffin.food import solver

    opts = (
        solver.SlotOption("breakfast", 412, 36, 14, 38, 4),
        solver.SlotOption("lunch", 526, 44, 20, 44, 6),
        solver.SlotOption("dinner", 510, 49, 19, 38, 10),
        solver.SlotOption("snack", 275, 39, 4, 20, 6),
        solver.SlotOption("pre_bed", 324, 30, 17, 13, 3),
    )
    target = Macros(1914, 201, 71, 118, 27)
    problem = solver.DayProblem(dt.date(2026, 9, 26), opts, target)

    def judged(ms: dict[str, float], pinned: frozenset[str] = frozenset()):
        sol = solver.DaySolution(ms, target, 0, 0.0)
        return menu.lopsided([problem], [sol], target, 71, 25, menu.WEEKEND_SPREAD, pinned)

    said = judged({"breakfast": 0.5, "lunch": 0.5, "dinner": 1.65, "snack": 1.3, "pre_bed": 1.0})
    assert said is not None and said.spread == pytest.approx(3.3)
    text = str(said)
    # each plate at its own size: the snack is at 1.3, not the day's highest
    assert (
        "breakfast at 0.5 and lunch at 0.5 of a serving beside dinner at 1.65 and snack at 1.3"
        in text
    ), text
    assert "short of protein (9.7 g per 100 kcal, the day needs 10.5)" in text, text
    assert len(text) <= 400, "it fits the retry hint whole"
    even = {"breakfast": 0.9, "lunch": 1.0, "dinner": 1.2, "snack": 1.1, "pre_bed": 1.0}
    assert judged(even) is None


def test_the_pinned_pre_bed_pot_is_not_a_lopsided_portion() -> None:
    """26 Sep, the second build: every dish at an ordinary 0.5-0.85 beside the owner's pre-bed pot,
    which his profile holds at one serving, read as 2.0 apart and all four attempts went back."""
    from tiffin.food import solver

    opts = tuple(
        solver.SlotOption(s, 500, 45, 18, 40, 6)
        for s in ("breakfast", "lunch", "dinner", "snack", "pre_bed")
    )
    target = Macros(1914, 201, 71, 118, 27)
    problem = solver.DayProblem(dt.date(2026, 9, 27), opts, target)
    ms = {"breakfast": 0.65, "lunch": 0.6, "dinner": 0.5, "snack": 0.85, "pre_bed": 1.0}
    sol = solver.DaySolution(ms, target, 0, 0.0)
    args = ([problem], [sol], target, 71, 25, menu.WEEKEND_SPREAD)
    assert menu.lopsided(*args) is not None, "counted, the pinned pot makes it look 2x apart"
    assert menu.lopsided(*args, frozenset({"pre_bed"})) is None, "it is not a portion choice"


def test_when_every_answer_is_lopsided_the_evenest_is_kept(pair, monkeypatch) -> None:
    """A weekend both can cook beats none: after the last attempt, the evenest answer that fitted
    is stored and the ledger says so."""
    db, (him, partner), clock = pair
    _agree(db, him.id)
    _agree(db, partner.id)
    monkeypatch.setattr(menu, "WEEKEND_SPREAD", 0.99)  # every plate spreads at least 1.0
    prompts_seen: list[str] = []
    notes: list[str] = []

    def ask(prompt: str, _hint: str) -> tuple[dict, str]:
        prompts_seen.append(prompt)
        return _weekend_payload(), "fake"

    profiles = {
        p.id: db.one("SELECT * FROM profiles WHERE person_id = ?", (p.id,)) for p in (him, partner)
    }
    built = shared.build(
        db,
        WEEKEND,
        [him, partner],
        profiles,
        {him.id: HIS, partner.id: PARTNERS},
        ask,
        prompt_for=lambda hint: "prompt" + hint,
        clock=clock,
        note=notes.append,
    )
    assert len(prompts_seen) == shared.MAX_ATTEMPTS
    assert "portions too uneven" in prompts_seen[1], "the retry is told why"
    assert any("kept the evenest attempt" in n for n in notes), notes
    assert set(built.menu_ids) == {him.id, partner.id}


def test_the_partner_plate_uses_the_name_partner_was_given(pair) -> None:
    """Persons.display_name is written once at bootstrap - on the box hers reads "Her" - and the
    page said "Her's 201 g". The name set later, in person_names, is the one shown."""
    db, (him, partner), clock = pair
    _agree(db, him.id)
    _agree(db, partner.id)
    db.insert("person_names", person_id=partner.id, name="Sam", set_by="operator")
    built = _build(db, [him, partner], clock)
    seen = shared.partner_plate(db, him.id, built.menu_ids[him.id], SAT, "lunch")
    assert seen is not None and seen["name"] == "Sam"


# ---------------------------------------------------------------- the weekend's shopping list
def _a_week_menu(db: Db, pid: int) -> int:
    mid = db.insert(
        "menus",
        person_id=pid,
        segment_start="2026-09-21",
        segment_end="2026-09-25",
        status="proposed",
    )
    db.insert("menu_decisions", menu_id=mid, decision="accepted")
    return mid


def test_saturday_shows_the_weekend_list_built_for_the_menus_in_force(pair) -> None:
    """26 Sep: the treat weekend was built on the Saturday, after Friday's list had found no
    weekend menus, and the page showed next week's list - there was no weekend list anywhere."""
    from tiffin.food import shopping

    db, (him, partner), clock = pair
    _agree(db, him.id)
    _agree(db, partner.id)
    _a_week_menu(db, him.id)  # Monday's menu is upcoming: before, it always won
    clock.set_local(dt.datetime(2026, 9, 19, 9, 30, tzinfo=TZ))  # built on the Saturday
    _build(db, [him, partner], clock)
    clock.set_local(dt.datetime(2026, 9, 19, 10, 0, tzinfo=TZ))
    first = shopping.list_to_show(db, him.id, SAT, clock=clock)
    assert first is not None and first["kind"] == "joint_weekend"
    assert shopping.list_to_show(db, partner.id, SAT, clock=clock)["id"] == first["id"], "one list"
    assert shopping.list_to_show(db, him.id, SAT, clock=clock)["id"] == first["id"], "not rebuilt"

    clock.set_local(dt.datetime(2026, 9, 19, 11, 0, tzinfo=TZ))
    _build(db, [him, partner], clock)  # rebuilt: both days, decided after the list
    clock.set_local(dt.datetime(2026, 9, 19, 11, 5, tzinfo=TZ))
    again = shopping.list_to_show(db, him.id, SAT, clock=clock)
    assert again["id"] != first["id"], "a list for dishes nobody is cooking is built again"

    clock.set_local(dt.datetime(2026, 9, 20, 9, 0, tzinfo=TZ))  # Sunday: Monday's shop is next
    sunday = shopping.list_to_show(db, him.id, SUN, clock=clock)
    assert sunday is None or sunday["kind"] != "joint_weekend"


def test_the_weekend_list_is_built_once_between_the_page_and_the_job(pair) -> None:
    from tiffin.food import shopping

    db, (him, partner), clock = pair
    _agree(db, him.id)
    _agree(db, partner.id)
    built = _build(db, [him, partner], clock)
    ids = [built.menu_ids[him.id], built.menu_ids[partner.id]]
    shown = shopping.list_to_show(db, him.id, dt.date(2026, 9, 18), clock=clock)  # Friday
    assert shown is not None and shown["kind"] == "joint_weekend"
    items = shopping.joint_once(db, ids, clock=clock)
    assert items and {i.list_id for i in items} == {int(shown["id"])}, "Friday's job reuses it"
    assert db.scalar("SELECT COUNT(*) FROM shopping_lists WHERE kind = 'joint_weekend'") == 1


def test_each_live_list_points_at_the_other(pair) -> None:
    from tiffin.food import shopping

    db, (him, partner), clock = pair
    _agree(db, him.id)
    _agree(db, partner.id)
    _build(db, [him, partner], clock)
    clock.set_local(dt.datetime(2026, 9, 19, 10, 0, tzinfo=TZ))
    weekend = shopping.list_to_show(db, him.id, SAT, clock=clock)
    assert weekend is not None
    others = shopping.other_lists(db, him.id, SAT, shown_id=-1)
    assert [o["id"] for o in others] == [int(weekend["id"])]
    assert others[0]["kind"] == "joint_weekend" and others[0]["segment_start"] == "2026-09-19"
    assert shopping.other_lists(db, him.id, SAT, shown_id=int(weekend["id"])) == []


def test_a_list_message_links_to_its_own_list(config) -> None:
    from tiffin.telegram import render

    link = render.shopping_link(config, "The list on the page", list_id=12)
    if link:  # no origin configured in some fixtures
        assert "tab=shopping&amp;list=12" in link
    assert "list=" not in render.shopping_link(config, "the page")


def test_a_list_nobody_has_shopped_yet_is_not_food_in_the_fridge(pair) -> None:
    """26 Sep: Monday's list was composed at 14:08 and the weekend's at 17:22, and the weekend's
    counted Monday's planned eggs, bread and carrots as already at home."""
    from tiffin.food import shopping

    db, (him, _partner), _clock = pair
    egg = db.one("SELECT id FROM ingredients ORDER BY id LIMIT 1")
    assert egg is not None
    started = db.insert(
        "menus",
        person_id=him.id,
        segment_start="2026-09-14",
        segment_end="2026-09-18",
        status="accepted",
    )
    ahead = _a_week_menu(db, him.id)  # Mon 21 - Fri 25, after the weekend of the 19th
    db.insert(
        "leftovers", person_id=him.id, ingredient_id=egg["id"], grams=300.0, from_menu_id=started
    )
    db.insert(
        "leftovers", person_id=him.id, ingredient_id=egg["id"], grams=40.0, from_menu_id=ahead
    )
    code = str(db.scalar("SELECT code FROM ingredients WHERE id = ?", (egg["id"],)))
    assert shopping.current_leftovers(db, him.id, SAT)[code] == 300.0, "Monday's is not bought"
    monday = dt.date(2026, 9, 21)
    assert shopping.current_leftovers(db, him.id, monday)[code] == 40.0, "from Monday it is"


def test_the_other_list_link_is_styled_as_a_link_on_the_page() -> None:
    from pathlib import Path

    import tiffin

    css = (Path(tiffin.__file__).parent / "web" / "static" / "app.css").read_text("utf-8")
    assert "#shopping-body p.muted a { color: var(--accent); }" in css
