"""Review of 27 Sep: the shared weekend's build, as the reviewer found it.

Each test is one finding: a /cook or a Change that rebuilt the wrong days, a failed build that
told nobody, a refusal that did not reach the shared weekend, an even plating never tried, a
model error that threw a usable weekend away, a past day planned, her consent read once, her gap
told to him, and the weekend's own rules only asked for.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, cast

import pytest

from tiffin.clock import TZ
from tiffin.db import Db
from tiffin.energy import Macros
from tiffin.errors import ModelInvalid, ModelUnavailable, NothingToBuild
from tiffin.food import menu, shared, solver, weekend

from .test_food_shopping import seed_menu, seed_recipe
from .test_menu import FakeGateway
from .test_review_0926_weekend import (  # noqa: F401 (pair is a pytest fixture)
    HIS,
    PARTNERS,
    SAT,
    SUN,
    WEEKEND,
    _agree,
    _build,
    _weekend_payload,
    pair,
)
from .test_weekend_switch import _own


@pytest.fixture
def world(pair, config, tmp_path):  # noqa: F811
    """Both agreed, both with targets, an app whose model answers with the weekend's dishes."""
    from tiffin.app import App
    from tiffin.secrets import Secrets

    from .fakes import FakeTelegram

    db, (him, partner), clock = pair
    _agree(db, him.id)
    _agree(db, partner.id)
    for p, m in ((him, HIS), (partner, PARTNERS)):
        db.insert(
            "targets", person_id=p.id, week_start="2026-09-14", kcal=m.kcal,
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
    return db, him, partner, clock, app, gw


def _pairs(db: Db) -> int:
    return int(db.scalar("SELECT COUNT(*) FROM shared_menus"))


def test_a_plain_cook_on_friday_leaves_the_planned_weekend_alone(world) -> None:
    """A bare /cook on Friday found the weekend "next" and rebuilt both people's planned weekend
    - a second menu for her she never asked for, and the Friday list's menus replaced."""
    from tiffin.jobs import menu_propose

    db, him, partner, clock, app, gw = world
    _build(db, [him, partner], clock)
    rid = int(db.scalar("SELECT id FROM recipes ORDER BY id LIMIT 1"))
    seed_menu(db, him.id, dt.date(2026, 9, 14), 5, {"lunch": (rid, 1.0)})
    clock.set_local(dt.datetime(2026, 9, 18, 10, 0, tzinfo=TZ))  # Friday
    profile = db.one("SELECT * FROM profiles WHERE person_id = ?", (him.id,))
    with pytest.raises(NothingToBuild, match="already covers"):
        menu_propose.propose_now(app, db, him, profile, app.outbox(db), clock=clock)
    assert gw.calls == [] and _pairs(db) == 1, "no model call, no second weekend"


def test_a_change_tapped_on_the_shared_menu_rebuilds_the_weekend(world) -> None:
    """Change on Thursday's shared menu built the tapper's Friday: the tap never said which menu
    it was on. It is carried now, and "no broccoli" is his refusal before the build."""
    from tiffin.jobs import menu_propose

    db, him, partner, clock, app, gw = world
    built = _build(db, [him, partner], clock)
    clock.advance(minutes=5)
    profile = db.one("SELECT * FROM profiles WHERE person_id = ?", (him.id,))
    menu_propose.propose_now(
        app, db, him, profile, app.outbox(db), clock=clock, replace=True,
        note="no broccoli", for_menu=built.menu_ids[him.id],
    )  # fmt: skip
    assert len(gw.calls) >= 1 and "weekend menu for two people" in gw.calls[0]
    refused = db.one(
        "SELECT refused_foods FROM profiles WHERE person_id = ? ORDER BY id DESC LIMIT 1",
        (him.id,),
    )
    assert "broccoli" in str(refused["refused_foods"]).lower(), "his refusal, written first"
    assert "broccoli" not in gw.calls[0].split("Days and slots")[0].lower(), (
        "and off the numbered list both of them are cooked from"
    )
    fridays = db.scalar(
        "SELECT COUNT(*) FROM menus WHERE person_id = ? AND segment_start = '2026-09-18'",
        (him.id,),
    )
    assert fridays == 0, "his Friday is not what the tap was about"


def test_a_failed_shared_build_puts_each_own_weekend_in_force_and_says_so(world) -> None:
    """A failed Thursday build told nobody, and the first words anyone got were Saturday's "No
    menu for today". With the switch on together, each person's own version takes the weekend
    and each is told in their own words; the switch stays where it was."""
    from tiffin import scheduler as sc
    from tiffin.jobs import menu_shared

    db, him, partner, clock, app, gw = world
    seed_recipe(db, "Own weekend dish", 1, {})
    own = {p.id: _own(db, p.id) for p in (him, partner)}
    gw.payloads[:] = [{"days": []} for _ in range(8)]  # every answer is unusable
    slot = sc.slots_for(menu_shared.JOB, 0, None, dt.date(2026, 9, 17), db)[0]
    clock.set_local(slot.due_local)
    status = sc.Scheduler(app, [menu_shared.JOB], lambda _p, _fn: None).execute(db, slot)
    assert status == "degraded"
    for p in (him, partner):
        assert menu.accepted_menu_id(db, p.id, SAT) == own[p.id]
    told = db.all("SELECT person_id, text_html FROM messages_out WHERE kind = 'menu_failed'")
    assert {int(r["person_id"]) for r in told} == {him.id, partner.id}
    assert all("your own weekend menu is in force" in str(r["text_html"]) for r in told)
    assert shared.mode(db) == "together", "the switch is theirs to move, not the job's"


def test_an_even_plating_is_found_before_a_day_is_sent_back() -> None:
    """The solver picks the closest calories and never looks at the spread: this day comes back
    at 0.5 beside 1.8, while 0.75/0.8/1.35/0.95 meets every floor - and each refusal is a paid
    model call."""
    opts = (
        solver.SlotOption("breakfast", 440, 50, 22, 52, 8),
        solver.SlotOption("lunch", 430, 54, 16, 52, 6),
        solver.SlotOption("dinner", 260, 38, 14, 20, 10),
        solver.SlotOption("snack", 320, 40, 4, 22, 5),
    )
    target = Macros(1288, 170, 50, 100, 26)
    problems = [solver.DayProblem(SAT, opts, target)]
    first = solver.solve_segment(
        problems, whole_pack=[], leftovers=None, pre_bed_protein=False, fat_floor_g=50,
        fibre_min=26, lo=0.5, hi=1.8,
    )  # fmt: skip
    assert not isinstance(first, solver.Infeasible)
    assert menu.lopsided(problems, first.days, target, 50, 26, 1.8) is not None, "the setup"
    even = menu._even_plating(
        problems, whole=[], pre_bed_protein=False, fat_floor_g=50, fibre_min=26, lo=0.5,
        hi=1.8, max_spread=1.8, target=target, pinned=frozenset(),
    )  # fmt: skip
    assert even is not None
    ms = even.days[0].multipliers.values()
    assert max(ms) / min(ms) <= 1.8 + 1e-9


def test_a_model_error_after_a_usable_answer_keeps_the_weekend(pair, monkeypatch) -> None:  # noqa: F811
    db, (him, partner), clock = pair
    _agree(db, him.id)
    _agree(db, partner.id)
    monkeypatch.setattr(menu, "WEEKEND_SPREAD", 0.99)  # every answer is "uneven"
    calls: list[str] = []

    def ask(prompt: str, _hint: str) -> tuple[dict, str]:
        calls.append(prompt)
        if len(calls) > 1:
            raise ModelUnavailable("quota")
        return _weekend_payload(), "fake"

    profiles = {
        p.id: db.one("SELECT * FROM profiles WHERE person_id = ?", (p.id,)) for p in (him, partner)
    }
    built = shared.build(
        db, WEEKEND, [him, partner], profiles, {him.id: HIS, partner.id: PARTNERS}, ask,
        prompt_for=lambda hint: "prompt" + hint, clock=clock,
    )  # fmt: skip
    assert set(built.menu_ids) == {him.id, partner.id} and built.evenest is not None


def test_a_day_that_has_gone_is_never_planned(pair) -> None:  # noqa: F811
    """A Sunday /cook after a failed weekend planned the Saturday already lived without one."""
    db, (him, _partner), _clock = pair
    assert menu.open_days(db, him.id, WEEKEND, SUN) == menu.Segment(SUN, SUN)


def test_partner_consent_is_asked_before_every_call(world) -> None:
    """She withdrew during the first attempt and three more calls went out with the joint
    prompt: the build stops at the next call."""
    from tiffin.jobs import menu_shared

    db, _him, partner, clock, app, gw = world
    real = gw.json_task

    def withdrawn_during_the_call(task: str, prompt: str, schema: str, *, person_id: int):
        got = real(task, prompt, schema, person_id=person_id)
        db.insert(
            "consents", person_id=partner.id, kind="shared_meals", granted=0,
            wording_version="shared_v1",
        )  # fmt: skip
        return got

    gw.json_task = withdrawn_during_the_call  # type: ignore[method-assign]
    gw.payloads[:] = [{"days": []} for _ in range(4)]  # refused, so a second call is wanted
    with pytest.raises(NothingToBuild, match="no longer be cooked together"):
        menu_shared.build_now(app, db, clock=clock)
    assert len(gw.calls) == 1


def test_a_weekend_with_no_targets_for_partner_is_each_their_own(
    fixture_db: Db, two_persons, config
) -> None:
    """With no weight or targets for her, the shared weekend still counted as on: his weekend
    was left to a shared build that could never run, and /cook told him about her gap."""
    from .test_review_0926_weekend import _profile

    db = fixture_db
    for pid in two_persons.values():
        _profile(db, pid)
        _agree(db, pid)
    db.insert(
        "weigh_ins", person_id=two_persons["owner"], day="2026-09-15", weight_kg=90.0,
        source="manual",
    )  # fmt: skip
    assert not shared.possible(db, WEEKEND) and not shared.active(db, WEEKEND)


def test_a_weekend_repeating_a_dish_is_sent_back(pair) -> None:  # noqa: F811
    """ "A different dish each day" was only asked for: the same Saturday and Sunday passed."""
    from .test_menu import a_day

    db, (him, _partner), clock = pair
    profile = db.one("SELECT * FROM profiles WHERE person_id = ?", (him.id,))
    with pytest.raises(ModelInvalid, match="variety 'different'"):
        menu.build_plan(
            db, him.id, WEEKEND, {"days": [a_day(SAT), a_day(SUN)]}, targets=HIS,
            profile=profile, clock=clock, weekend=True,
        )  # fmt: skip


def test_both_saying_yes_tells_the_first_and_names_thursday(pair, config, tmp_path) -> None:  # noqa: F811
    """ "Send /cook to build this weekend's now" was wrong four days in seven, and whoever said
    yes first was never told sharing had started."""
    from tiffin.app import App
    from tiffin.secrets import Secrets
    from tiffin.telegram.poller import Poller

    from .fakes import FakeTelegram

    db, (him, partner), clock = pair
    clock.set_local(dt.datetime(2026, 9, 15, 9, 0, tzinfo=TZ))  # a Tuesday
    db.insert(
        "consents",
        person_id=partner.id,
        kind="shared_meals",
        granted=1,
        wording_version="shared_v1",
    )
    secrets_dir = tmp_path / "s"
    secrets_dir.mkdir()
    (secrets_dir / "telegram-token-dev.txt").write_text("1:t", encoding="utf-8")
    tg = FakeTelegram()
    app = App(config, Secrets(secrets_dir), clock=clock, transport=tg)
    poller = Poller(
        app.api(), db, app.registry(db), app.handlers(db).handle, clock=clock,
        sleeper=lambda s: None,
    )  # fmt: skip
    tg.queue_update(tg.callback_update(1, 1000001, "k:consent:share:1", message_id=5))
    poller.run_once()
    said = db.all("SELECT person_id, kind, text_html FROM messages_out ORDER BY id")
    his = [str(r["text_html"]) for r in said if int(r["person_id"]) == him.id]
    assert any("built on Thursday" in t for t in his), his
    partners = [r for r in said if int(r["person_id"]) == partner.id and r["kind"] == "shared_on"]
    assert len(partners) == 1, said
    assert weekend.taste(db, partner.id) == "both"
