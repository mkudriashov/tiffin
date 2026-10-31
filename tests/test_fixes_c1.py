"""Regression tests for menu build lanes, status and cooldowns."""

from __future__ import annotations

import datetime as dt
import json
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tiffin import building
from tiffin.app import App
from tiffin.clock import TZ
from tiffin.db import Db
from tiffin.errors import NothingToBuild
from tiffin.food import menu, newdish, plates
from tiffin.jobs import menu_propose, menu_shared
from tiffin.runtime import Runtime, Worker
from tiffin.secrets import Secrets

from .fakes import FakeTelegram
from .test_menu import FakeGateway, proposal_for, world  # noqa: F401
from .test_model_builds import bot_with_model  # noqa: F401
from .test_review_0926_weekend import _build, _weekend_payload, pair  # noqa: F401
from .test_review_0927_weekend_build import world as pair_world  # noqa: F401
from .test_swap import MON, OWNER_CHAT, _buttons, _headers, _profile, page  # noqa: F401
from .web_source import app_source

STATIC = Path(__file__).resolve().parents[1] / "src" / "tiffin" / "web" / "static"
SUNDAY_EVENING = dt.datetime(2026, 9, 13, 18, 0, tzinfo=TZ)
WEEK = menu.Segment(dt.date(2026, 9, 14), dt.date(2026, 9, 18))


def _js() -> str:
    return app_source()


def _block(js: str, name: str) -> str:
    start = js.index(f"function {name}(")
    return js[start : js.index("\n}\n", start)]


class Lane:
    """The person's build lane, held until the test lets it run: each build goes through
    `Worker._run_job`, which is all the Runtime's lane does with it."""

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


def _sunday(world_: Any) -> Any:
    """Sunday evening, the Mon-Fri week ahead uncovered: a /cook builds it."""
    pid, db, tg, gateways, say, tap, _run_job = world_
    db.clock.set_local(SUNDAY_EVENING)
    gateways.append(FakeGateway([proposal_for(WEEK) for _ in range(6)]))
    return pid, db, tg, gateways[0], say, tap


def _latest_menu(db: Db, pid: int) -> int:
    return int(
        db.scalar("SELECT id FROM menus WHERE person_id = ? ORDER BY id DESC LIMIT 1", (pid,))
    )


def _boom(*_a: Any, **_k: Any) -> Any:
    raise RuntimeError("something nobody expected")


# ---------------------------------------------------------------- #18 a build that raises
def test_18_a_cook_build_that_raises_is_said_and_lifts_the_cooldown(world, lane) -> None:  # noqa: F811
    """A /cook build that raises on the build lane is said to the person in
    words, and a bare /cook afterwards is not refused as "still working"."""
    pid, db, _tg, gw, say, _tap = _sunday(world)
    gw.payloads[:] = [RuntimeError("the model wrapper broke")]
    started = say("/cook replace more fish")
    assert "Working out what to cook" in started
    lane.run(db)
    told = _told(db, pid)[-1]
    assert "went wrong" in told and "/cook" in told, told
    assert building.current(db, pid, clock=db.clock) is None
    assert db.one("SELECT v FROM bot_state WHERE k = ?", (f"cook_asked:{pid}",)) is None
    again = say("/cook")
    assert "Still working on the last one" not in again


def test_18_new_dishes_that_raise_in_telegram_are_said(bot_with_model, lane, monkeypatch) -> None:  # noqa: F811
    """New dishes asked for in Telegram that raise on the build lane are
    said to the person, with the way to the dishes already had."""
    send, tap, db, pid, _model, clock = bot_with_model
    monkeypatch.setattr(newdish, "ask", _boom)
    out = send("/swap monday lunch")
    tap(_buttons(out[0])["New dishes"])
    lane.run(db)
    told = _told(db, pid)[-1]
    assert "went wrong" in told and "/swap" in told, told
    assert building.current(db, pid, clock=clock) is None


def test_18_a_page_rebuild_that_raises_is_said_on_the_page(page, lane, monkeypatch) -> None:  # noqa: F811
    """A page rebuild that raises is said on the page and in Telegram, not
    only in the log."""
    client, db, partners, _his, _r = page
    monkeypatch.setattr(menu_propose, "propose_now", _boom)
    h = _headers(client)
    started = client.post(
        "/api/menu/rebuild", json={"note": "more fish", "day": "2026-09-14"}, headers=h
    )
    assert started.status_code == 200, started.text
    lane.run(db)
    got = client.get("/api/menu", headers=h).json()
    assert "building" not in got
    assert got["built"]["id"] == started.json()["id"]
    assert "went wrong" in got["built"]["said"]
    assert any("went wrong" in t for t in _told(db, partners))


def test_18_page_new_dishes_that_raise_leave_a_word_for_the_swap(page, lane, monkeypatch) -> None:  # noqa: F811
    """New dishes asked for on the page that raise leave a word where the
    swap list shows it, and the mark goes."""
    client, db, _partners, _his, _r = page
    monkeypatch.setattr(newdish, "ask", _boom)
    h = _headers(client)
    started = client.post(
        "/api/menu/new-dish", json={"day": "2026-09-14", "slot": "lunch"}, headers=h
    )
    assert started.status_code == 200, started.text
    lane.run(db)
    got = client.get(
        "/api/menu/swap", params={"day": "2026-09-14", "slot": "lunch"}, headers=h
    ).json()
    assert got["building"] is None
    assert got["new_said"] and "went wrong" in got["new_said"]


# ---------------------------------------------------------------- #41 scheduled beside asked
def test_41_the_scheduled_build_waits_for_a_build_that_was_asked_for(world) -> None:  # noqa: F811
    """The scheduled menu build does not run beside a build the person asked
    for, so their note cannot lose to a plain menu built at the same time."""
    pid, db, _tg, gateways, _say, _tap, run_job = world
    gw = FakeGateway([proposal_for(WEEK)])
    gateways.append(gw)
    building.mark(db, pid, "more fish", clock=db.clock)
    status = run_job(menu_propose.JOB, dt.date(2026, 9, 12))
    assert status == "skipped"
    assert gw.calls == [], "no second model build for the same days"


def test_41_a_scheduled_build_is_marked_while_it_runs(world) -> None:  # noqa: F811
    """A scheduled menu build is marked while it runs, so the page does not
    start another beside it, and the mark goes when it ends."""
    pid, db, _tg, gateways, _say, _tap, run_job = world
    seen: list[Any] = []

    class Watching(FakeGateway):
        def json_task(self, task: str, prompt: str, schema_name: str, *, person_id: int) -> Any:
            seen.append(building.current(db, pid, clock=db.clock))
            return super().json_task(task, prompt, schema_name, person_id=person_id)

    gateways.append(Watching([proposal_for(WEEK)]))
    status = run_job(menu_propose.JOB, dt.date(2026, 9, 12))
    assert status == "ok"
    assert seen and seen[0] is not None and seen[0]["kind"] == building.MENU
    assert seen[0]["asked"] is False, "a scheduled build does not hold up the next scheduled one"
    assert building.current(db, pid, clock=db.clock) is None


def test_41_the_shared_weekend_waits_for_a_build_that_was_asked_for(pair_world) -> None:  # noqa: F811
    """The scheduled shared weekend does not run beside a build either person
    asked for."""
    from tiffin import scheduler as sc

    db, him, _partner, clock, app, gw = pair_world
    building.mark(db, him.id, "more fish", clock=clock)
    slot = sc.slots_for(menu_shared.JOB, 0, None, dt.date(2026, 9, 17), db)[0]
    clock.set_local(slot.due_local)
    status = sc.Scheduler(app, [menu_shared.JOB], lambda _p, _fn: None).execute(db, slot)
    assert status == "skipped"
    assert gw.calls == []


def test_41_a_scheduled_build_stays_marked_after_new_dishes_land(world) -> None:  # noqa: F811
    """A scheduled menu build that starts while new
    dishes are being written is marked too, so once the dishes are in the page cannot start a
    second build for the same days."""
    pid, db, _tg, gateways, _say, _tap, run_job = world
    dishes = building.mark(
        db, pid, "new lunch dishes", clock=db.clock, kind=building.DISHES, day=MON, slot="lunch"
    )
    seen: list[Any] = []

    class Landing(FakeGateway):
        def json_task(self, task: str, prompt: str, schema_name: str, *, person_id: int) -> Any:
            building.done(db, pid, dishes)  # the dishes are in while the menu is building
            seen.append(building.current(db, pid, clock=db.clock))
            return super().json_task(task, prompt, schema_name, person_id=person_id)

    gateways.append(Landing([proposal_for(WEEK)]))
    status = run_job(menu_propose.JOB, dt.date(2026, 9, 12))
    assert status == "ok"
    assert seen and all(s is not None and s["kind"] == building.MENU for s in seen), seen
    assert building.current(db, pid, clock=db.clock) is None, "and its mark goes when it ends"


def test_41_the_shared_weekend_stays_marked_after_new_dishes_land(pair_world) -> None:  # noqa: F811
    """The scheduled shared weekend is marked for both
    while it runs, new dishes being written for one of them or not."""
    from tiffin import scheduler as sc

    db, him, partner, clock, app, gw = pair_world
    dishes = building.mark(
        db, him.id, "new lunch dishes", clock=clock, kind=building.DISHES, day=MON, slot="lunch"
    )
    seen: list[Any] = []
    answer = gw.json_task

    def landing(task: str, prompt: str, schema_name: str, *, person_id: int) -> Any:
        building.done(db, him.id, dishes)
        seen.append([building.current(db, p.id, clock=clock) for p in (him, partner)])
        return answer(task, prompt, schema_name, person_id=person_id)

    gw.json_task = landing  # type: ignore[method-assign]
    slot = sc.slots_for(menu_shared.JOB, 0, None, dt.date(2026, 9, 17), db)[0]
    clock.set_local(slot.due_local)
    sc.Scheduler(app, [menu_shared.JOB], lambda _p, _fn: None).execute(db, slot)
    assert seen and all(m is not None for both in seen for m in both), seen
    assert all(building.current(db, p.id, clock=clock) is None for p in (him, partner))


def test_41_a_scheduled_build_does_not_promise_its_menu_to_a_cook(world, lane) -> None:  # noqa: F811
    """A /cook refused because a scheduled build is
    running names what that build is, and asks again, rather than promising the menu asked
    for."""
    pid, db, _tg, _gw, say, _tap = _sunday(world)
    say("/cook")
    lane.run(db)
    db.clock.set_local(dt.datetime(2026, 9, 16, 12, 1, tzinfo=TZ))
    building.mark(db, pid, "the weekend menu", clock=db.clock, asked=False)
    out = say("/cook replace more fish")
    assert "comes here when it is ready" not in out, out
    assert "the weekend menu" in out and "again" in out, out
    assert lane.queued == []


def test_41_two_scheduled_builds_at_once_each_keep_a_mark(fixture_db: Db, two_persons) -> None:
    """His own menu and the shared weekend, scheduled
    and running at once, each keep a mark, so the one that ends first leaves the other's: the
    page cannot start a third build beside the one still running."""
    pid, clock = two_persons["owner"], fixture_db.clock
    weekdays = building.mark(fixture_db, pid, "the menu from Mon 21 Sep", clock=clock, asked=False)
    weekend = building.mark(fixture_db, pid, "the weekend menu", clock=clock, asked=False)
    building.done(fixture_db, pid, weekend)
    now = building.current(fixture_db, pid, clock=clock)
    assert now is not None and now["id"] == weekdays, now
    building.done(fixture_db, pid, weekdays)
    assert building.current(fixture_db, pid, clock=clock) is None


def test_41_the_page_names_a_scheduled_build_it_waits_for(page) -> None:  # noqa: F811
    """The page's Build, refused while a scheduled build
    runs, names that build rather than saying the menu asked for shows here."""
    client, db, partners, _his, _r = page
    building.mark(db, partners, "the weekend menu", clock=db.clock, asked=False)
    h = _headers(client)
    got = client.post(
        "/api/menu/rebuild", json={"note": "more fish", "day": "2026-09-14"}, headers=h
    )
    assert got.status_code == 409
    assert "the weekend menu" in got.json()["detail"] and "shows here" not in got.json()["detail"]


# ---------------------------------------------------------------- #85 a restart mid-build
def test_85_a_restart_forgets_the_build_it_killed_and_says_so(
    config, frozen_clock, tmp_path: Path
) -> None:
    """A restart clears the mark of a build that died with the old process,
    and tells whoever asked for it that it did not finish."""
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
        building.mark(db, pid, "more fish", clock=frozen_clock)
        frozen_clock.advance(minutes=3)
        runtime = Runtime(app, heartbeat_s=60, tick_s=3600, jobs=[], serve_web=False)
        runtime.start()
        try:
            assert building.current(db, pid, clock=frozen_clock) is None
            told = _told(db, pid)
        finally:
            runtime.stop()
    finally:
        db.close()
    assert any("restarted" in t and "/cook" in t for t in told), told


def test_85_the_cook_a_restart_tells_them_to_send_is_not_refused(world, lane) -> None:  # noqa: F811
    """After a restart killed a bare /cook, the bare
    /cook the restart message tells them to send builds, and is not refused as "still working"
    on a build that died."""
    pid, db, _tg, _gw, say, _tap = _sunday(world)
    assert "Working out what to cook" in say("/cook")
    app, _died = lane.queued.pop(0)  # it died with the process
    db.clock.advance(minutes=2)
    Runtime(app, jobs=[], serve_web=False)._forget_builds(db)
    assert "send /cook to ask again" in _told(db, pid)[-1]
    again = say("/cook")
    assert "Still working on the last one" not in again, again
    assert "Working out what to cook" in again


# ---------------------------------------------------------------- #133 the mark's owner
def test_133_a_build_that_ran_long_does_not_clear_the_next_ones_mark(world, lane) -> None:  # noqa: F811
    """The end of a /cook build that outlived STALE_MIN leaves the mark of
    the /cook let in after it, so a third is still refused."""
    pid, db, _tg, _gw, say, _tap = _sunday(world)
    say("/cook replace more fish")
    db.clock.advance(minutes=building.STALE_MIN + 1)
    assert "Working out what to cook" in say("/cook replace less rice")
    app, first = lane.queued.pop(0)
    Worker(app, 0, threading.Event(), lane="builds")._run_job(db, first)
    now = building.current(db, pid, clock=db.clock)
    assert now is not None and now["what"] == "less rice", now
    third = say("/cook replace no rice at all")
    assert "Working out what to cook" not in third, third
    assert len(lane.queued) == 1, "only the second build is waiting"


# ---------------------------------------------------------------- #65 dishes are not a menu
def test_65_new_dishes_in_flight_are_named_as_dishes(page, lane) -> None:  # noqa: F811
    """While new dishes for one meal are written, the page and the bot say
    that, not that a whole menu is coming."""
    client, _db, _partners, _his, _r = page
    h = _headers(client)
    started = client.post(
        "/api/menu/new-dish", json={"day": "2026-09-14", "slot": "lunch"}, headers=h
    )
    assert started.status_code == 200, started.text
    now = client.get("/api/menu", headers=h).json()["building"]
    assert now["kind"] == "dishes" and now["day"] == "2026-09-14" and now["slot"] == "lunch"
    busy = client.post("/api/menu/rebuild", json={"note": "", "day": "2026-09-14"}, headers=h)
    assert busy.status_code == 409
    assert (
        "lunch dishes" in busy.json()["detail"] and "menu is already" not in busy.json()["detail"]
    )
    other = client.get("/api/menu/swap", params={"day": "2026-09-16", "slot": "dinner"}, headers=h)
    assert other.json()["building"]["slot"] == "lunch", "the page tells the meal it is for"
    said = building.busy(now)
    assert "lunch dishes" in said and "menu" not in said


def test_65_the_page_shows_each_kind_where_it_belongs() -> None:
    """The Menu tab says "Building" only for a whole menu, and a meal's Swap
    says "Writing new dishes" only for the meal they are being written for."""
    js = _js()
    panel = _block(js, "rebuildPanel")
    assert 'kind === "dishes"' in panel
    swap = _block(js, "fillSwap")
    assert "building.slot === got.slot" in swap and "building.day === got.day" in swap


# ---------------------------------------------------------------- #47 the cooldown
def test_47_just_a_new_menu_is_not_refused_by_the_cooldown(world) -> None:  # noqa: F811
    """ "Just a new menu" right after a /cook delivered its menu builds the
    new menu rather than saying "still working on the last one"."""
    pid, db, _tg, gw, say, tap = _sunday(world)
    say("/cook")
    asked = len(gw.calls)
    assert asked >= 1
    mid = _latest_menu(db, pid)
    db.clock.advance(minutes=1)  # read, then tapped: inside the five-minute cooldown
    tap(f"k:menux:{mid}")
    out = tap(f"k:rebuild:{mid}")
    assert "Still working on the last one" not in out, out
    assert len(gw.calls) > asked, "the new menu was built"
    assert _latest_menu(db, pid) != mid


# ---------------------------------------------------------------- #49 and #45 the buttons
def test_49_keep_this_one_says_the_typed_change_is_still_coming(world, lane) -> None:  # noqa: F811
    """ "Keep this one" after a typed change has started a build says that a
    new menu is on its way, and the typed change takes the prompt's buttons off."""
    pid, db, tg, _gw, say, tap = _sunday(world)
    say("/cook")
    lane.run(db)
    mid = _latest_menu(db, pid)
    tap(f"k:menux:{mid}")
    prompt = tg.sent[-1]["message_id"]
    keep = _buttons(tg.sent[-1])["Keep this one"]
    say("more fish")
    assert any(e["message_id"] == prompt for e in tg.edited), "the prompt's buttons go"
    kept = tap(keep)
    assert "Kept the menu as it is" not in kept, kept
    assert "being built" in kept and "replaces this one" in kept


def test_49_keep_this_one_says_a_build_asked_for_before_is_still_coming(world, lane) -> None:  # noqa: F811
    """ "Keep this one" on a Change tapped while a menu
    asked for earlier is still building says that menu is on its way, as does a "skip" typed
    there. A bare /cook replace works out its days only as it runs, so neither says it keeps
    this menu or replaces it (second verification)."""
    pid, db, _tg, _gw, say, tap = _sunday(world)
    say("/cook")
    lane.run(db)
    mid = _latest_menu(db, pid)
    db.clock.advance(minutes=6)
    say("/cook replace more fish")
    assert lane.queued, "a build is waiting"
    tap(f"k:menux:{mid}")
    kept = tap("k:keepmenu")
    assert "Kept the menu as it is" not in kept and "replaces" not in kept, kept
    assert "still being built" in kept
    tap(f"k:menux:{mid}")
    skipped = say("skip")
    assert "Kept the menu as it is" not in skipped and "replaces" not in skipped, skipped
    assert "still being built" in skipped
    assert len(lane.queued) == 1


def test_49_keep_does_not_say_a_build_for_other_days_replaces_this_one(world, lane) -> None:  # noqa: F811
    """ "Keep this one" on his weekend menu while a
    /cook replace builds Friday says that build is still coming, and never that it replaces the
    weekend he kept - which it does not."""
    pid, db, tg, gw, say, tap = _sunday(world)
    run_job = world[-1]
    say("/cook")
    lane.run(db)
    db.clock.set_local(dt.datetime(2026, 9, 17, 12, 0, tzinfo=TZ))
    gw.payloads[:] = [proposal_for(menu.Segment(dt.date(2026, 9, 19), dt.date(2026, 9, 20)))]
    assert run_job(menu_propose.JOB, dt.date(2026, 9, 17)) == "ok"
    weekend = menu.segment_menu_id(db, pid, dt.date(2026, 9, 19))
    assert weekend is not None
    db.clock.set_local(dt.datetime(2026, 9, 17, 19, 0, tzinfo=TZ))
    gw.payloads[:] = [proposal_for(menu.Segment(dt.date(2026, 9, 18), dt.date(2026, 9, 18)))]
    assert "Working out what to cook" in say("/cook replace more fish")
    tap(f"k:menux:{weekend}")
    keep = _buttons(tg.sent[-1])["Keep this one"]
    old = tap(f"k:keepmenu:{menu.segment_menu_id(db, pid, WEEK.start)}")  # the week's old Keep
    assert "already answered" in old, old
    kept = tap(keep)
    assert "Change taken back" in kept, "the weekend's Change was still open"
    assert "replaces this one" not in kept, kept
    assert "still being built" in kept, kept
    lane.run(db)
    assert menu.segment_menu_id(db, pid, dt.date(2026, 9, 19)) == weekend


def test_45_old_change_buttons_leave_another_question_alone(world, lane) -> None:  # noqa: F811
    """An old "Keep this one" or "Just a new menu" never deletes another
    question the person is half way through, and starts no second build."""
    pid, db, _tg, _gw, say, tap = _sunday(world)
    say("/cook")
    lane.run(db)
    mid = _latest_menu(db, pid)
    tap(f"k:menux:{mid}")
    say("more fish")
    lane.run(db)
    measuring = json.dumps({"flow": "measures", "step": "waist"})
    db.execute(
        "INSERT INTO conversation_state(person_id, state_json, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(person_id) DO UPDATE SET state_json = excluded.state_json, "
        "updated_at = excluded.updated_at",
        (pid, measuring, db.clock.utc_stamp()),
    )
    kept = tap("k:keepmenu")
    assert "Kept the menu as it is" not in kept
    again = tap(f"k:rebuild:{mid}")
    assert "Working out what to cook" not in again
    assert lane.queued == [], "no second build"
    row = db.one("SELECT state_json FROM conversation_state WHERE person_id = ?", (pid,))
    assert row is not None and row["state_json"] == measuring


# ---------------------------------------------------------------- #86 repeat during a build
def test_86_cook_repeat_waits_for_the_build_in_flight(world, lane) -> None:  # noqa: F811
    """/cook repeat while a menu is being built says so instead of
    repeating, so the older build cannot silently replace the repeat."""
    pid, db, _tg, _gw, say, _tap = _sunday(world)
    say("/cook")
    lane.run(db)
    in_force = menu.segment_menu_id(db, pid, WEEK.start)
    db.clock.advance(minutes=1)
    say("/cook replace more fish")
    out = say("/cook repeat")
    assert "/cook repeat" in out and "Menu repeated" not in out, out
    assert db.scalar("SELECT COUNT(*) FROM menus WHERE status = 'repeated'") == 0
    assert menu.segment_menu_id(db, pid, WEEK.start) == in_force, "nothing changed yet"


# ---------------------------------------------------------------- #48 and #113 the weekend
def test_48_just_a_new_menu_on_a_past_weekend_builds_nothing(pair_world) -> None:  # noqa: F811
    """ "Just a new menu" on a weekend that has gone plans nothing, and never
    the coming weekend for both of them."""
    db, him, _partner, clock, app, gw = pair_world
    built = _build(db, [him, _partner], clock)
    clock.set_local(dt.datetime(2026, 9, 21, 10, 0, tzinfo=TZ))  # the Monday after
    profile = db.one("SELECT * FROM profiles WHERE person_id = ?", (him.id,))
    with pytest.raises(NothingToBuild, match="already under way"):
        menu_propose.propose_now(
            app, db, him, profile, app.outbox(db), clock=clock, replace=True,
            for_menu=built.menu_ids[him.id],
        )  # fmt: skip
    assert gw.calls == [], "no model asked about the coming weekend"


def test_48_a_sunday_only_weekend_can_still_be_changed_on_saturday(pair_world) -> None:  # noqa: F811
    """A shared weekend rebuilt once Saturday had begun
    covers Sunday alone, and a Change on it on the Saturday still rebuilds Sunday."""
    db, him, partner, clock, app, gw = pair_world
    _build(db, [him, partner], clock)
    clock.set_local(dt.datetime(2026, 9, 19, 14, 0, tzinfo=TZ))  # Saturday, under way
    gw.payloads[:] = [{"days": [_weekend_payload()["days"][1]]} for _ in range(8)]
    menu_shared.cook_together(app, db, clock=clock, note="more fish", person=him)
    sunday = _latest_menu(db, him.id)
    row = db.one("SELECT segment_start, segment_end FROM menus WHERE id = ?", (sunday,))
    assert (row["segment_start"], row["segment_end"]) == ("2026-09-20", "2026-09-20")
    clock.set_local(dt.datetime(2026, 9, 19, 18, 0, tzinfo=TZ))
    asked = len(gw.calls)
    profile = db.one("SELECT * FROM profiles WHERE person_id = ?", (him.id,))
    menu_propose.propose_now(
        app, db, him, profile, app.outbox(db), clock=clock, replace=True, note="no rice",
        for_menu=sunday,
    )  # fmt: skip
    assert len(gw.calls) > asked, "Sunday is still to come, and is rebuilt"


def test_113_a_failed_weekend_rebuild_without_a_note_does_not_mention_one(pair_world) -> None:  # noqa: F811
    """A shared weekend rebuild asked for without a note that fails says it
    could not build a different weekend, not that it could not build one "around that"."""
    db, him, partner, clock, app, gw = pair_world
    _build(db, [him, partner], clock)
    gw.payloads[:] = [{"days": []} for _ in range(8)]
    said = menu_shared.cook_together(app, db, clock=clock, note="", person=him)
    assert "around that" not in said, said
    assert "stays" in said


# ---------------------------------------------------------------- #14 #66 #87 the page's Build
def test_14_the_page_rebuilds_the_week_not_one_resized_day(page, monkeypatch, config) -> None:  # noqa: F811
    """Build a new menu on the page rebuilds the menu's whole stretch of
    days even when its first day's plates were resized."""
    client, db, partners, _his, _r = page
    week = menu.segment_menu_id(db, partners, MON)
    # nothing batch-cooked: with boxed lunches only breakfast flexes, and it cannot carry the day
    _profile(db, partners, effective_from="2026-09-01", batch_slots=json.dumps([]))
    plates.resize(db, partners, MON, {"breakfast": "skip"}, config=config, clock=db.clock)
    assert menu.accepted_menu_id(db, partners, MON) != week, "Monday is a one-day menu of its own"
    asked: list[Any] = []

    def propose_now(*_a: Any, **kw: Any) -> str:
        asked.append(kw.get("for_menu"))
        return ""

    monkeypatch.setattr(menu_propose, "propose_now", propose_now)
    h = _headers(client)
    got = client.post(
        "/api/menu/rebuild", json={"note": "more fish", "day": "2026-09-14"}, headers=h
    )
    assert got.status_code == 200, got.text
    assert asked == [week]


def test_66_build_on_the_last_day_of_a_menu_says_why_not(page) -> None:  # noqa: F811
    """Build a new menu on a menu whose every day has begun answers on the
    page with the reason, and starts nothing."""
    client, db, partners, _his, _r = page
    db.clock.set_local(dt.datetime(2026, 9, 18, 9, 0, tzinfo=TZ))  # Friday, its last day
    h = _headers(client)
    got = client.post("/api/menu/rebuild", json={"note": "", "day": "2026-09-14"}, headers=h)
    assert got.status_code == 409, got.text
    assert "begun" in got.json()["detail"] and "/cook" in got.json()["detail"]
    assert building.current(db, partners, clock=db.clock) is None
    assert _told(db, partners) == [], "nothing goes to Telegram instead"


def test_87_a_page_build_with_no_model_says_so_on_the_page(page) -> None:  # noqa: F811
    """A page build with no model to ask says on the page that the menu is
    unchanged, and never that there is no menu yet."""
    client, db, partners, _his, _r = page
    h = _headers(client)
    started = client.post(
        "/api/menu/rebuild", json={"note": "more fish", "day": "2026-09-14"}, headers=h
    )
    assert started.status_code == 200, started.text
    got = client.get("/api/menu", headers=h).json()
    assert got["built"]["id"] == started.json()["id"]
    assert "stays as it is" in got["built"]["said"]
    assert "<code>" not in got["built"]["said"], "plain words for the page"
    told = " ".join(_told(db, partners))
    assert "stays as it is" in told and "no menu yet" not in told


def test_87_the_build_panel_shows_how_the_build_it_watched_ended() -> None:
    """The Build panel says how the build it started or watched ended."""
    panel = _block(_js(), "rebuildPanel")
    assert "data.built" in panel and "buildWatched" in panel
