"""Regression tests for notes and refusals during menu builds."""

from __future__ import annotations

import datetime as dt
import threading
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any, cast

import pytest

from tiffin import building
from tiffin.app import App
from tiffin.clock import TZ
from tiffin.db import Db
from tiffin.food import catalogue, menu, prefs, swap
from tiffin.jobs import menu_propose
from tiffin.model.gateway import ModelResult
from tiffin.runtime import Worker
from tiffin.telegram import menuflow

from .test_food_shopping import seed_ingredient
from .test_menu import FakeGateway, proposal_for, world  # noqa: F401
from .test_model_builds import bot_with_model  # noqa: F401
from .test_review_0926_weekend import _build as _build_weekend
from .test_review_0926_weekend import pair  # noqa: F401
from .test_review_0927_weekend_build import world as pair_world  # noqa: F401
from .test_swap import FRI, MON, _buttons, _headers, bot, page  # noqa: F401

SUNDAY_EVENING = dt.datetime(2026, 9, 13, 18, 0, tzinfo=TZ)
WEEK = menu.Segment(MON, FRI)
THU = dt.date(2026, 9, 17)


class Lane:
    """The person's build lane, held until the test lets it run."""

    def __init__(self) -> None:
        self.queued: list[tuple[App, Callable[[Db], object]]] = []

    def run(self, db: Db) -> None:
        while self.queued:
            app, fn = self.queued.pop(0)
            Worker(app, 0, threading.Event(), lane="builds")._run_job(db, fn)


@pytest.fixture
def lane(monkeypatch) -> Lane:
    held = Lane()

    def run_build(self: App, person_id: int, fn: Callable[[Db], object], db: Db) -> bool:
        held.queued.append((self, fn))
        return True

    monkeypatch.setattr(App, "run_build", run_build)
    return held


def _told(db: Db, pid: int) -> list[str]:
    return [
        str(r["text_html"])
        for r in db.all(
            "SELECT text_html FROM messages_out WHERE person_id = ? ORDER BY id", (pid,)
        )
    ]


def _latest_menu(db: Db, pid: int) -> int:
    return int(
        db.scalar("SELECT id FROM menus WHERE person_id = ? ORDER BY id DESC LIMIT 1", (pid,))
    )


def _sunday(world_: Any) -> Any:
    """Sunday evening, the Mon-Fri week ahead uncovered: a /cook builds it."""
    pid, db, tg, gateways, say, tap, _run_job = world_
    db.clock.set_local(SUNDAY_EVENING)
    gateways.append(FakeGateway([proposal_for(WEEK) for _ in range(6)]))
    return pid, db, tg, gateways[0], say, tap


def _partner_menu(db: Db, pid: int) -> int:
    return _latest_menu(db, pid)


def _text(out: list[dict]) -> str:
    return "\n".join(str(m["text"]) for m in out)


# ---------------------------------------------------------------- #4 a refusal naming one meal
def test_4_a_refusal_in_a_change_naming_one_meal_is_kept_and_said(bot_with_model) -> None:  # noqa: F811
    """A refusal in a change note that names one meal is written to the avoid
    list before the new dishes are asked for, kept off the list the model picks from, and said."""
    send, tap, db, pid, model, _clock = bot_with_model
    tap(f"k:menux:{_partner_menu(db, pid)}")
    out = send("I don't want lentils any more, give me a different lunch")
    assert "lentils" in catalogue.refused_terms(db, pid)
    assert model.tasks == ["menu_one_dish"], "still one meal, not the whole menu"
    assert "Red lentils" not in model.prompts[-1], "the refused food is off the numbered list"
    said = _text(out)
    assert "lentils are off your menus from now on" in said, said
    assert "Only that meal changes" in said


def test_4_the_made_from_question_comes_with_a_one_meal_change(bot_with_model) -> None:  # noqa: F811
    send, tap, db, pid, _model, _clock = bot_with_model
    seed_ingredient(db, "houmous", "Houmous", "condiments", 200.0, "200 g tub")
    tap(f"k:menux:{_partner_menu(db, pid)}")
    out = send("a different lunch, no chickpeas")
    asked = next(m for m in out if "Houmous is made from chickpeas" in m["text"])
    assert {"Keep houmous off", "Houmous is fine"} <= set(_buttons(asked))


# ---------------------------------------------------------------- #19 a note while a build runs
def test_19_a_refusal_sent_while_a_build_runs_is_kept_and_said(world, lane) -> None:  # noqa: F811
    """/cook with a refusal while a menu is building writes the refusal, says
    so, and says the rest of the change was not started."""
    pid, db, _tg, _gw, say, _tap = _sunday(world)
    assert "Working out what to cook" in say("/cook replace more fish")
    out = say("/cook replace no broccoli")
    assert catalogue.refused_terms(db, pid) == ["broccoli"]
    assert "Already working on a menu" in out, out
    assert "broccoli are off your menus from now on" in out, out
    assert "not started" in out, out
    assert len(lane.queued) == 1, "no second build"


def test_19_a_bare_cook_while_a_build_runs_promises_no_start(world, lane) -> None:  # noqa: F811
    """A bare /cook while a menu is building says one is building, and never
    that a /cook with a change would start now."""
    _pid, _db, _tg, _gw, say, _tap = _sunday(world)
    say("/cook replace more fish")
    out = say("/cook")
    assert "I will start now" not in out, out
    assert "Already working on a menu" in out, out


# ---------------------------------------------------------------- #20 a refusal mid-build
class _RefusedWhileItWorks(FakeGateway):
    """The model at work while she says "I do not want chicken" on the main lane: the refusal
    is written after the build read its list of foods and before it stores the menu."""

    def __init__(self, payloads: list[Any], db: Db, pid: int) -> None:
        super().__init__(payloads)
        self.db, self.pid = db, pid

    def json_task(self, task: str, prompt: str, schema_name: str, *, person_id: int) -> ModelResult:
        if not self.calls:
            prefs.amend(self.db, self.pid, "avoid", add=["chicken"], clock=self.db.clock)
        return super().json_task(task, prompt, schema_name, person_id=person_id)


def test_20_a_menu_landing_with_a_food_refused_meanwhile_says_so(world) -> None:  # noqa: F811
    """A menu built while a food was refused says it still holds that food,
    with a button that swaps it out."""
    pid, db, tg, gateways, say, _tap, _run_job = world
    db.clock.set_local(SUNDAY_EVENING)
    gateways.append(_RefusedWhileItWorks([proposal_for(WEEK) for _ in range(6)], db, pid))
    before = len(tg.sent)
    say("/cook replace more fish")
    sent = tg.sent[before:]
    built = next(m for m in sent if "Menu for the next cook" in m["text"])
    assert catalogue.refused_names_on(db, pid, [_latest_menu(db, pid)]), "the race is real"
    assert "still has" in built["text"] and "which you now avoid" in built["text"], built["text"]
    assert _buttons(built).get("Swap it now") == "k:swapall"


def test_20_a_shared_weekend_landing_with_a_food_refused_meanwhile_says_so(pair_world) -> None:  # noqa: F811
    """A weekend cooked together, rebuilt while he
    refused broccoli, tells him it still holds broccoli with a Swap button, and tells her
    nothing of it."""
    from tiffin.jobs import menu_shared

    db, him, partner, clock, app, gw = pair_world
    _build_weekend(db, [him, partner], clock)
    asked = gw.json_task

    def json_task(task: str, prompt: str, schema_name: str, *, person_id: int) -> Any:
        if not gw.calls:
            prefs.amend(db, him.id, "avoid", add=["broccoli"], clock=clock)
        return asked(task, prompt, schema_name, person_id=person_id)

    gw.json_task = json_task  # type: ignore[method-assign]
    tg = cast("Any", app.transport)
    before = len(tg.sent)
    menu_shared.cook_together(app, db, clock=clock, note="more fish", person=him)
    mine = _latest_menu(db, him.id)
    assert catalogue.refused_names_on(db, him.id, [mine]) == ["Broccoli"], "the race is real"
    told = [m for m in tg.sent[before:] if "which you now avoid" in m["text"]]
    assert len(told) == 1, tg.sent[before:]
    assert "Broccoli" in told[0]["text"]
    assert _buttons(told[0]).get("Swap it now") == "k:swapall"
    partners = " ".join(_told(db, partner.id))
    assert "Broccoli" not in partners and "avoid" not in partners, partners


# ---------------------------------------------------------------- #94 refusal before early returns
def test_94_a_refusal_on_a_menu_whose_days_are_under_way_is_kept(bot_with_model) -> None:  # noqa: F811
    """A refusal in a change on a menu whose days have all begun is written
    and said, alongside why nothing was planned."""
    send, tap, db, pid, model, clock = bot_with_model
    clock.set_local(dt.datetime(2026, 9, 18, 19, 0, tzinfo=TZ))  # Friday evening, its last day
    tap(f"k:menux:{_partner_menu(db, pid)}")
    out = send("no chickpeas please")
    assert "chickpeas" in catalogue.refused_terms(db, pid)
    said = _text(out)
    assert "already under way" in said, said
    assert "chickpeas are off your menus from now on" in said, said
    assert model.tasks == [], "nothing was paid for"


def test_94_a_refusal_with_no_targets_yet_is_kept(world) -> None:  # noqa: F811
    """A refusal in a /cook note from someone with no targets for those days
    is written and said, alongside what is missing."""
    pid, db, _tg, gateways, say, _tap, _run_job = world
    # Saturday 12 Sep: the next day to plan is Sunday, before the first targets and with no weight
    gateways.append(FakeGateway([]))
    out = say("/cook replace no broccoli")
    assert "profile and a weight" in out, out
    assert catalogue.refused_terms(db, pid) == ["broccoli"]
    assert "broccoli are off your menus from now on" in out, out
    assert gateways[0].calls == [], "nothing was paid for"


def test_94_a_refusal_on_a_shared_weekend_that_has_ended_is_kept(pair_world) -> None:  # noqa: F811
    """ "No broccoli" on last weekend's shared menu is
    written and said with why nothing was built, never dropped."""
    from tiffin.errors import NothingToBuild

    db, him, partner, clock, app, gw = pair_world
    built = _build_weekend(db, [him, partner], clock)
    clock.set_local(dt.datetime(2026, 9, 21, 10, 0, tzinfo=TZ))  # the Monday after
    profile = db.one("SELECT * FROM profiles WHERE person_id = ?", (him.id,))
    with pytest.raises(NothingToBuild) as got:
        menu_propose.propose_now(
            app, db, him, profile, app.outbox(db), clock=clock, replace=True,
            note="no broccoli please", for_menu=built.menu_ids[him.id],
        )  # fmt: skip
    assert catalogue.refused_terms(db, him.id) == ["broccoli"]
    said = str(got.value)
    assert "already under way" in said and "broccoli are off your menus" in said, said
    assert gw.calls == []


def test_94_a_refusal_written_up_front_is_still_said_by_the_shared_build(pair_world) -> None:  # noqa: F811
    """Written once before the build and handed on, the
    refusal is still said when the weekend is rebuilt - a second write would read "already"."""
    db, him, partner, clock, app, _gw = pair_world
    built = _build_weekend(db, [him, partner], clock)
    # a food the weekend's answer does not use, so the build goes through
    seed_ingredient(db, "tofu", "Firm tofu", "produce", 400.0, "400 g block")
    profile = db.one("SELECT * FROM profiles WHERE person_id = ?", (him.id,))
    said = menu_propose.propose_now(
        app, db, him, profile, app.outbox(db), clock=clock, replace=True,
        note="no tofu please", for_menu=built.menu_ids[him.id],
    )  # fmt: skip
    assert catalogue.refused_terms(db, him.id) == ["tofu"]
    assert _latest_menu(db, him.id) != built.menu_ids[him.id], "the weekend was rebuilt"
    assert "tofu are off your menus from now on, not just this one" in said, said


def test_94_the_page_says_a_refusal_with_every_day_begun(page) -> None:  # noqa: F811
    """The page's rebuild of a menu whose days have all
    begun writes the refusal in its note, and says so with the reason nothing is built."""
    client, db, partners, _his, _r = page
    db.clock.set_local(dt.datetime(2026, 9, 18, 19, 0, tzinfo=TZ))  # Friday evening, last day
    h = _headers(client)
    got = client.post(
        "/api/menu/rebuild", json={"note": "no chickpeas", "day": "2026-09-14"}, headers=h
    )
    assert got.status_code == 409, got.text
    assert "chickpeas" in catalogue.refused_terms(db, partners)
    detail = got.json()["detail"]
    assert "has begun" in detail and "chickpeas are off your menus" in detail, detail
    assert "<" not in detail, "the page shows text, not the bot's HTML"


def test_94_the_page_says_a_refusal_while_a_build_runs(page) -> None:  # noqa: F811
    """The page's rebuild asked for while another runs
    still writes the refusal in its note, and says the rest was not started."""
    client, db, partners, _his, _r = page
    building.mark(db, partners, "more fish", clock=db.clock)
    h = _headers(client)
    got = client.post(
        "/api/menu/rebuild", json={"note": "no chickpeas", "day": "2026-09-14"}, headers=h
    )
    assert got.status_code == 409, got.text
    assert "chickpeas" in catalogue.refused_terms(db, partners)
    detail = got.json()["detail"]
    assert "already being built" in detail and "chickpeas are off your menus" in detail, detail
    assert "not started" in detail, detail


# ---------------------------------------------------------------- #95 the made-from question
def test_95_a_cook_note_refusing_chickpeas_asks_about_houmous(bot) -> None:  # noqa: F811
    """A /cook note that refuses chickpeas asks whether houmous, made from
    them, goes too."""
    send, _tap, db, pid, _r, clock = bot
    seed_ingredient(db, "houmous", "Houmous", "condiments", 200.0, "200 g tub")
    clock.set_local(dt.datetime(2026, 9, 13, 10, 0, tzinfo=TZ))
    out = send("/cook replace no chickpeas")
    assert "chickpeas" in catalogue.refused_terms(db, pid)
    said = _text(out)
    assert "Houmous is made from chickpeas: keep it off too, or is it fine?" in said, said
    assert "/avoid" in said, "the way to the two buttons"


def test_95_the_note_returns_its_questions(bot) -> None:  # noqa: F811
    """The refusal read from a note keeps the made-from questions it opens."""
    _send, _tap, db, pid, _r, clock = bot
    seed_ingredient(db, "houmous", "Houmous", "condiments", 200.0, "200 g tub")
    who = SimpleNamespace(id=pid)  # only the id is read
    got = menu_propose.note_refusal(db, who, "no chickpeas", clock=clock)  # type: ignore[arg-type]
    assert got is not None and list(got.added) == ["chickpeas"]
    assert [a["name"] for a in got.asks] == ["Houmous"]


# ---------------------------------------------------------------- #93 a repeat's own days
def test_93_a_repeat_swaps_refused_food_only_on_the_days_it_repeats(bot) -> None:  # noqa: F811
    """/cook repeat takes a refused food out of the days it repeats onto, and
    leaves today's dish, already cooked, where it is."""
    send, _tap, db, pid, _r, clock = bot
    clock.set_local(dt.datetime(2026, 9, 16, 10, 0, tzinfo=TZ))  # Wednesday
    wed = dt.date(2026, 9, 16)
    prefs.amend(db, pid, "avoid", add=["lentils"], clock=clock)  # no swap tapped
    out = send("/cook repeat")
    said = _text(out)
    lunch = {p.slot: p.name for p in swap.plates_on(db, pid, wed)}["lunch"]
    assert lunch == "Lentil dal", "Wednesday was not repeated, so its lunch stays"
    assert "Menu repeated" in said, said
    assert "Wed" not in said.split("Menu repeated")[1].split("\n")[1], said
    thu = {p.slot: p.name for p in swap.plates_on(db, pid, THU)}["lunch"]
    assert thu != "Lentil dal", "the repeated days lose the refused food"


def test_93_swap_avoided_kept_to_some_days_leaves_the_others(bot, config) -> None:  # noqa: F811
    """The clean-up of refused food can be kept to a stretch of days."""
    _send, _tap, db, pid, _r, clock = bot
    prefs.amend(db, pid, "avoid", add=["lentils"], clock=clock)
    got = swap.swap_avoided(db, pid, config=config, clock=clock, within=menu.Segment(THU, FRI))
    assert got.swapped and all(d >= THU for s in got.swapped for d in s.days)
    assert {p.slot: p.name for p in swap.plates_on(db, pid, MON)}["lunch"] == "Lentil dal"


# ---------------------------------------------------------------- #43, #111 the meal named
@pytest.mark.parametrize(
    ("note", "slots"),
    [
        ("more fish, but keep the lunch", set()),
        ("leave the lunch alone, more fish", set()),
        ("everything except lunch", set()),
        ("the lunch is fine, more fish", set()),
        ("keep the lunch, a different dinner", {"dinner"}),
        ("a different lunch please, something with chicken", {"lunch"}),
        ("no fish at dinner", {"dinner"}),
        # after verification: "except" after keep / fine / good / same names the meal to change
        ("keep everything the same except lunch", {"lunch"}),
        ("everything is fine apart from lunch", {"lunch"}),
        ("all good except dinner", {"dinner"}),
        ("other than lunch it is all fine", {"lunch"}),
        ("keep it all, besides the lunch", {"lunch"}),
        ("keep everything except lunch", {"lunch"}),
        ("happy with all of it but the lunch", {"lunch"}),
        ("don't change anything except the lunch", {"lunch"}),
        # ...and after a change word or a bare "everything", the meal to keep
        ("change everything except lunch", set()),
        ("change everything but lunch", set()),
        ("everything but lunch, more fish", set()),
        ("a new menu apart from the dinner", set()),
        ("don't change the lunch, more fish", set()),
        ("don\u2019t change the lunch, more fish", set()),  # a phone's apostrophe
        ("do not touch the lunch, more fish", set()),
        ("more fish but do not touch lunch", set()),
        ("no need to change the lunch, more fish", set()),
        ("keep my usual lunch, more fish", set()),
        ("the lunch was fine, more fish", set()),
        ("the lunches were good, more fish", set()),
        # "but" on its own is not "except": both meals are asked about
        ("a different breakfast, but lunch with fish", {"breakfast", "lunch"}),
        ("not the same lunch every day", {"lunch"}),
    ],
)
def test_43_a_meal_named_to_keep_is_not_the_meal_to_change(note: str, slots: set[str]) -> None:
    """A meal named as the one to keep is not taken as the one to change."""
    assert menuflow.slots_named(note) == slots


def test_43_keep_the_lunch_rebuilds_the_menu_not_the_lunch(bot_with_model) -> None:  # noqa: F811
    """ "More fish, but keep the lunch" goes to a whole-menu build, never to
    new lunch dishes."""
    send, tap, db, pid, model, _clock = bot_with_model
    tap(f"k:menux:{_partner_menu(db, pid)}")
    out = send("more fish, but keep the lunch")
    assert "menu_one_dish" not in model.tasks, model.tasks
    assert "Only that meal changes" not in _text(out)


@pytest.mark.parametrize(
    ("note", "task"),
    [
        ("keep everything the same except lunch", "menu_one_dish"),
        ("everything is fine apart from lunch", "menu_one_dish"),
        ("change everything but lunch", "menu_propose"),
        ("don't change the lunch, more fish", "menu_propose"),
    ],
)
def test_43_the_meal_after_except_is_read_from_the_words_around_it(
    bot_with_model,  # noqa: F811
    note: str,
    task: str,
) -> None:
    """ "Keep everything the same except lunch" changes
    the lunch alone, and "change everything but lunch" rebuilds the menu, never new lunches."""
    send, tap, db, pid, model, _clock = bot_with_model
    tap(f"k:menux:{_partner_menu(db, pid)}")
    send(note)
    assert model.tasks[:1] == [task], model.tasks


@pytest.mark.parametrize(
    "note", ["a different pre-bed snack", "a new bedtime snack", "another pre bed snack"]
)
def test_111_a_pre_bed_snack_is_one_meal(note: str) -> None:
    """ "Pre-bed snack" names the one pre-bed meal, not a snack as well."""
    assert menuflow.slots_named(note) == {"pre_bed"}


# ---------------------------------------------------------------- #44 a day named in the note
def test_44_a_day_named_in_the_change_changes_that_day_only(bot_with_model) -> None:  # noqa: F811
    """ "A different lunch on thursday" offers new dishes for Thursday alone,
    and the tap puts one in on Thursday only."""
    send, tap, db, pid, _model, _clock = bot_with_model
    tap(f"k:menux:{_partner_menu(db, pid)}")
    out = send("a different lunch on thursday")
    offer = out[-1]
    assert "New dishes for lunch on Thu 17 Sep" in offer["text"], offer["text"]
    choices = _buttons(offer)
    assert choices and all(data.endswith(":1") for data in choices.values()), choices
    tap(next(iter(choices.values())))
    lunches = {d: {p.slot: p.name for p in swap.plates_on(db, pid, d)}["lunch"] for d in WEEK.days}
    assert lunches[THU] != "Lentil dal"
    assert all(name == "Lentil dal" for d, name in lunches.items() if d != THU), lunches


@pytest.mark.parametrize(
    ("note", "named"),
    [
        ("a different lunch on thursday", [THU]),
        ("a different lunch for thu", [THU]),
        ("a different lunch on sat", [dt.date(2026, 9, 12)]),
        ("a different thursday lunch", [THU]),
        ("thursday's lunch, something lighter", [THU]),
        ("thursday: a different lunch", [THU]),
        ("a different lunch thursday", [THU]),
        ("a different lunch on thursday and friday", [THU, FRI]),
        # food and prose words that only look like a day (after verification)
        ("a different lunch with sun dried tomatoes", []),
        ("a sunday roast style dinner", []),
        ("a different lunch, something that sat well last time", []),
        ("a different dinner, a friday night takeaway kind of thing", []),
        ("sunday roast for dinner please", []),
    ],
)
def test_44_the_days_a_note_names(note: str, named: list[dt.date]) -> None:
    """A weekday is a day when it is said as one ("on
    thursday", "thursday's lunch"), never a food or a word that starts like one ("sun dried",
    "a sunday roast", "sat well")."""
    assert menuflow.days_in_note(note, dt.date(2026, 9, 7), list(WEEK.days)) == named


def test_44_sun_dried_tomatoes_name_no_day(bot_with_model) -> None:  # noqa: F811
    """ "Sun dried tomatoes" is a food, not Sunday: the
    lunch gets new dishes for every day still to come, as before."""
    send, tap, db, pid, model, _clock = bot_with_model
    tap(f"k:menux:{_partner_menu(db, pid)}")
    out = send("a different lunch with sun dried tomatoes")
    assert model.tasks == ["menu_one_dish"], model.tasks
    assert "New dishes for lunch Mon to Fri 18 Sep" in out[-1]["text"], _text(out)


def test_44_two_days_named_change_those_days_and_no_others(bot_with_model) -> None:  # noqa: F811
    """ "A different lunch on thursday and friday" never
    offers a tap that replaces every day's lunch: the first day named is offered alone, and the
    answer says the other needs its own."""
    send, tap, db, pid, _model, _clock = bot_with_model
    tap(f"k:menux:{_partner_menu(db, pid)}")
    out = send("a different lunch on thursday and friday")
    said = _text(out)
    assert "Fri 18 Sep" in said and "again" in said, said
    offer = out[-1]
    assert "New dishes for lunch on Thu 17 Sep" in offer["text"], offer["text"]
    choices = _buttons(offer)
    assert choices and all(data.endswith(":1") for data in choices.values()), choices
    tap(next(iter(choices.values())))
    lunches = {d: {p.slot: p.name for p in swap.plates_on(db, pid, d)}["lunch"] for d in WEEK.days}
    assert lunches[MON] == "Lentil dal", lunches
    assert lunches[THU] != "Lentil dal", lunches


def test_44_a_day_not_on_the_menu_changes_nothing_and_says_so(bot_with_model) -> None:  # noqa: F811
    """A day named that the tapped menu does not cover changes nothing, and
    the answer says so rather than replacing every day's lunch."""
    send, tap, db, pid, model, _clock = bot_with_model
    tap(f"k:menux:{_partner_menu(db, pid)}")
    out = send("a different lunch on saturday")
    assert model.tasks == [], "nothing asked of the model"
    said = _text(out)
    # the frozen clock is Monday 7 Sep, so the Saturday named is the 12th
    assert "Sat 12 Sep" in said and "No new dishes asked for" in said, said
    lunches = {d: {p.slot: p.name for p in swap.plates_on(db, pid, d)}["lunch"] for d in WEEK.days}
    assert set(lunches.values()) == {"Lentil dal"}, lunches


# ---------------------------------------------------------------- #127 no consent, said as such
def test_127_a_page_rebuild_without_consent_says_why_and_promises_no_retry(page) -> None:  # noqa: F811
    """A page rebuild for someone who has not agreed to Claude says that is
    why, says the menu stays, and promises no retry."""
    client, db, partners, _his, _r = page
    h = _headers(client)
    started = client.post(
        "/api/menu/rebuild", json={"note": "more fish", "day": "2026-09-14"}, headers=h
    )
    assert started.status_code == 200, started.text
    got = client.get("/api/menu", headers=h).json()
    said = got["built"]["said"]
    assert "/consent" in said and "stays as it is" in said, said
    assert "12:00" not in said and "could not reach" not in said, said
    told = " ".join(_told(db, partners))
    assert "/consent" in told and "12:00" not in told, told


def test_127_no_consent_and_no_menu_yet_promises_no_retry(world) -> None:  # noqa: F811
    """With no consent and no menu for those days, the answer names consent
    and does not promise a retry at 12:00 that would fail the same way."""
    from tiffin.errors import ModelUnavailable

    _pid, db, _tg, gateways, say, _tap, _run_job = world
    db.clock.set_local(SUNDAY_EVENING)
    gateways.append(
        FakeGateway([ModelUnavailable("menu_propose: claude unavailable (no_consent)")])
    )
    out = say("/cook")
    assert "/consent" in out and "12:00" not in out, out
    assert building.current(db, _pid, clock=db.clock) is None
