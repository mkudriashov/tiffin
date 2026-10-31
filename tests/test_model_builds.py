"""Model builds that do not hold anything else up: new dishes for one meal (the rest of the menu
untouched), a build on the person's own lane, honest timing, and a rebuild from the page.
"""

from __future__ import annotations

import datetime as dt
import re
import threading
from pathlib import Path
from typing import Any

import pytest

from tiffin import building
from tiffin.app import App
from tiffin.db import Db
from tiffin.errors import ModelUnavailable
from tiffin.food import newdish, swap
from tiffin.model.gateway import ModelResult
from tiffin.runtime import Runtime
from tiffin.secrets import Secrets

from .fakes import FakeTelegram
from .test_swap import FRI, MON, OWNER_CHAT, _buttons, _headers, _seed, page, week  # noqa: F401


def _index(prompt: str, name: str) -> int:
    """The number the prompt gave a food: the fake answers by index, as the model must."""
    m = re.search(rf"^(\d+)\. {re.escape(name)}", prompt, re.M)
    assert m, f"{name} is not on the list"
    return int(m.group(1))


class OneDishModel:
    """Answers menu_one_dish with three options: one a soup (refused for a boxed lunch), one
    already on the menu (refused), one good. And menu_propose with nothing usable."""

    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.tasks: list[str] = []

    def json_task(self, task: str, prompt: str, schema_name: str, *, person_id: int) -> ModelResult:
        self.prompts.append(prompt)
        self.tasks.append(task)
        chicken, rice, pepper = (
            _index(prompt, "Chicken breast"),
            _index(prompt, "Basmati rice"),
            _index(prompt, "Red pepper"),
        )

        def dish(name: str) -> dict[str, Any]:
            return {
                "name": name,
                "ingredients": [
                    {"i": chicken, "g": 150},
                    {"i": rice, "g": 60},
                    {"i": pepper, "g": 80},
                ],
                "method": ["Cook the rice.", "Fry the chicken and pepper in a pan for 10 minutes."],
                "prep_minutes": 20,
            }

        data = {
            "options": [dish("Chicken soup"), dish("Chicken and rice"), dish("Pepper chicken rice")]
        }
        return ModelResult(text="", data=data, model_used="fake-opus", backend="fake")


def _consent(db: Db, pid: int) -> None:
    db.insert("consents", person_id=pid, granted=1, wording_version="v2")


class _App:
    """Enough of App for newdish.ask: a gateway, and nothing on a lane."""

    def __init__(self, model: OneDishModel) -> None:
        self.model = model

    def gateway(self, _db: Db) -> OneDishModel:
        return self.model


# ---------------------------------------------------------------- new dishes for one meal
def test_new_dishes_for_one_meal_are_checked_stored_and_offered(week, config) -> None:  # noqa: F811
    db, pid, _base, _r, clock = week
    _consent(db, pid)
    model = OneDishModel()
    got = newdish.ask(_App(model), db, pid, MON, "lunch", clock=clock)
    assert model.tasks == ["menu_one_dish"], "one call, for one meal"
    prompt = model.prompts[0]
    assert "The dish going out (do not offer it, or anything like it): Lentil dal" in prompt
    assert "breakfast: Porridge with yoghurt" in prompt, "the rest of the day is named"
    assert "Lentil dal" not in prompt.split("Recipe names from the last 14 days")[1].split("\n")[0]
    assert [d["name"] for d in got.dishes] == ["Pepper chicken rice"]
    assert any("soup" in r for r in got.refused), "a boxed weekday lunch is never a soup"
    assert any("already on the menu" in r for r in got.refused), "dinner is Chicken and rice"
    assert got.days == tuple(MON + dt.timedelta(days=i) for i in range(5))
    offered = {c.name: c for c in swap.candidates(db, pid, MON, "lunch", MON, limit=None)}
    assert offered["Pepper chicken rice"].new is True
    # one tap puts it in, solved in code like any swap
    rid = next(d["recipe_id"] for d in got.dishes if d["name"] == "Pepper chicken rice")
    done = swap.swap(db, pid, MON, "lunch", rid, config=config, clock=clock)
    assert done.new == "Pepper chicken rice"
    assert [p.name for p in swap.plates_on(db, pid, FRI)][1] == "Pepper chicken rice"
    assert [p.name for p in swap.plates_on(db, pid, FRI)][2] == "Chicken and rice", (
        "the rest of the menu is untouched"
    )


def test_without_consent_nothing_leaves_the_box(week) -> None:  # noqa: F811
    db, pid, _base, _r, clock = week
    model = OneDishModel()
    with pytest.raises(ModelUnavailable):
        newdish.ask(_App(model), db, pid, MON, "lunch", clock=clock)
    assert model.prompts == [], "no consent, no call"


def test_a_build_in_flight_is_marked_and_forgotten_when_stale(week) -> None:  # noqa: F811
    db, pid, _base, _r, clock = week
    building.mark(db, pid, "a new menu", clock=clock)
    now = building.current(db, pid, clock=clock)
    assert now is not None and now["what"] == "a new menu" and now["minutes"] == 0
    clock.advance(minutes=building.STALE_MIN + 1)
    assert building.current(db, pid, clock=clock) is None, "a build that died is not shown"
    building.done(db, pid)


# ---------------------------------------------------------------- the bot
@pytest.fixture
def bot_with_model(fixture_db: Db, two_persons, frozen_clock, config, tmp_path):
    from tiffin.telegram.poller import Poller

    db, pid, clock = fixture_db, two_persons["owner"], frozen_clock
    _seed(db, pid)
    _consent(db, pid)
    secrets_dir = tmp_path / "s"
    secrets_dir.mkdir()
    (secrets_dir / "telegram-token-dev.txt").write_text("1:t", encoding="utf-8")
    tg = FakeTelegram()
    model = OneDishModel()
    app = App(
        config,
        Secrets(secrets_dir),
        clock=clock,
        transport=tg,
        gateway_factory=lambda _a, _d: model,  # type: ignore[arg-type]
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

    return send, tap, db, pid, model, clock


def test_new_dishes_from_the_swap_list_in_telegram(bot_with_model) -> None:
    send, tap, db, pid, _model, clock = bot_with_model
    out = send("/swap monday lunch")
    buttons = _buttons(out[0])
    assert "New dishes" in buttons
    got = tap(buttons["New dishes"])
    texts = [m["text"] for m in got]
    assert any("usually 1-2 minutes" in t for t in texts), "honest timing, said first"
    offer = got[-1]
    assert "New dishes for lunch Mon to Fri 18 Sep" in offer["text"]
    choices = _buttons(offer)
    assert set(choices) == {"Pepper chicken rice"}
    done = tap(choices["Pepper chicken rice"])
    assert "Pepper chicken rice in for Lentil dal" in done[-1]["text"]
    assert building.current(db, pid, clock=clock) is None, "the mark goes when the build ends"


def test_a_change_naming_one_meal_rebuilds_only_that_meal(bot_with_model) -> None:
    send, tap, db, pid, model, _clock = bot_with_model
    menu_id = int(
        db.scalar("SELECT id FROM menus WHERE person_id = ? ORDER BY id DESC LIMIT 1", (pid,))
    )
    asked = tap(f"k:menux:{menu_id}")
    assert set(_buttons(asked[-1])) == {"Just a new menu", "Keep this one"}
    got = send("a different lunch please, something with chicken")
    assert model.tasks == ["menu_one_dish"], "one meal, not the whole menu"
    assert any("Only that meal changes" in m["text"] for m in got)
    assert "New dishes for lunch" in got[-1]["text"]


def test_keep_this_one_takes_the_change_back(bot_with_model) -> None:
    send, tap, db, pid, model, _clock = bot_with_model
    menu_id = int(
        db.scalar("SELECT id FROM menus WHERE person_id = ? ORDER BY id DESC LIMIT 1", (pid,))
    )
    asked = tap(f"k:menux:{menu_id}")
    kept = tap(_buttons(asked[-1])["Keep this one"])
    assert "Kept the menu as it is" in kept[-1]["text"]
    later = send("more fish")
    assert model.tasks == [], "the change was taken back: 'more fish' is not a rebuild now"
    assert later is not None


def test_cook_says_how_long_it_really_takes(bot_with_model) -> None:
    send, _tap, _db, _pid, _model, _clock = bot_with_model
    out = send("/cook replace more fish")
    assert "usually takes 2-4 minutes" in out[0]["text"]
    assert "everything else still answers" in out[0]["text"]


# ---------------------------------------------------------------- the build lane
def test_a_build_on_its_own_lane_does_not_hold_up_partner_other_messages(
    config, frozen_clock, tmp_path: Path
) -> None:
    """A menu build is minutes of model calls. Her check-in answer, a swap, a /confirm - anything
    that writes, which the read-only lane never took - is answered while it runs."""
    from .test_lived_moment import wait_for

    tg = FakeTelegram()
    secrets_dir = tmp_path / "s"
    secrets_dir.mkdir()
    (secrets_dir / "telegram-token-dev.txt").write_text("1:t", encoding="utf-8")
    app = App(config, Secrets(secrets_dir), clock=frozen_clock, transport=tg)
    app.bootstrap().close()
    db = app.open_db()
    try:
        pid = int(
            db.scalar("SELECT person_id FROM telegram_identities WHERE chat_id = ?", (OWNER_CHAT,))
        )
    finally:
        db.close()
    runtime = Runtime(app, heartbeat_s=60, tick_s=3600, jobs=[], serve_web=False)
    assert app.background is not None, "the runtime gives builds their own lane"
    started, release = threading.Event(), threading.Event()

    def four_minutes_of_model_calls(_db: Db) -> None:
        started.set()
        release.wait(5.0)

    def answered() -> bool:
        return any("Nothing is kept off" in m["text"] or "Finish" in m["text"] for m in tg.sent)

    runtime.start()
    try:
        app.run_build(pid, four_minutes_of_model_calls, None)  # type: ignore[arg-type]
        assert started.wait(3.0), "the build is running"
        tg.queue_update(tg.text_update(1, OWNER_CHAT, "/avoid"))  # a write-capable command
        while_busy = wait_for(answered, 2.0)
    finally:
        release.set()
        runtime.stop()
    assert while_busy, "the main lane answers while the build lane works"


# ---------------------------------------------------------------- the page
def test_the_page_asks_for_new_dishes_and_offers_them(page) -> None:  # noqa: F811
    client, db, partners, _his, _r = page
    _consent(db, partners)
    model = OneDishModel()
    client.app.state.tiffin.gateway_factory = lambda _a, _d: model
    h = _headers(client)
    started = client.post(
        "/api/menu/new-dish", json={"day": "2026-09-14", "slot": "lunch"}, headers=h
    )
    assert started.status_code == 200, started.text
    assert started.json()["takes"] == "usually 1-2 minutes"
    got = client.get(
        "/api/menu/swap", params={"day": "2026-09-14", "slot": "lunch"}, headers=h
    ).json()
    new = [c for c in got["candidates"] if c["new"]]
    assert [c["name"] for c in new] == ["Pepper chicken rice"]
    assert got["building"] is None
    assert got["new_said"].startswith("New dishes for lunch")
    bad = client.post(
        "/api/menu/new-dish", json={"day": "2026-09-14", "slot": "elevenses"}, headers=h
    )
    assert bad.status_code == 400


def test_the_page_rebuilds_and_says_so(page) -> None:  # noqa: F811
    client, db, partners, _his, _r = page
    h = _headers(client)
    menu = client.get("/api/menu", headers=h).json()
    assert "building" not in menu
    # no consent: the build degrades (the old menu stays in force) and the mark is cleared
    started = client.post("/api/menu/rebuild", json={"note": "more fish"}, headers=h)
    assert started.status_code == 200, started.text
    assert started.json()["takes"] == "usually 2-4 minutes"
    assert "building" not in client.get("/api/menu", headers=h).json()
    building.mark(db, partners, "a new menu", clock=client.app.state.tiffin.clock)
    busy = client.post("/api/menu/rebuild", json={"note": ""}, headers=h)
    assert busy.status_code == 409, "one build at a time"
    assert client.get("/api/menu", headers=h).json()["building"]["what"] == "a new menu"
    long = client.post("/api/menu/rebuild", json={"note": "x" * 201}, headers=h)
    assert long.status_code in (400, 409)
