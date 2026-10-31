"""Ten places Tiffin names something and then cannot be followed there.

Not validation. Every story here is the shape of the four bugs found by living with it: a
message names a command, a dish, a state or a place, and the person who does the named thing
lands on nothing. A dead end, a loop, or a thing that exists only on a message that has gone.

Each test is one person doing one obvious thing after reading one sentence Tiffin wrote.
"""

from __future__ import annotations

import datetime as dt

from tiffin.food import menu
from tiffin.jobs import menu_propose, morning_targets
from tiffin.telegram import settings
from tiffin.telegram.menuflow import TICK_BUTTONS

from .test_menu import MONDAY, FakeGateway, proposal_for, world  # noqa: F401
from .test_menu_reachable import a_menu

SATURDAY = dt.date(2026, 9, 12)  # the world fixture's today, 18:00
SUNDAY = dt.date(2026, 9, 13)


def taps_on(sent: dict) -> list[str]:
    """Every callback payload on a sent message, so "is there a way out of here" is one call."""
    markup = sent.get("reply_markup") or {}
    return [b["callback_data"] for row in markup.get("inline_keyboard", []) for b in row]


def a_weight(db, person_id: int) -> None:
    """Targets need a weight before any menu or morning message can be worked out."""
    db.insert("weigh_ins", person_id=person_id, day="2026-09-12", weight_kg=100.0, source="manual")


def extra_ingredients(db, how_many: int) -> None:
    for i in range(how_many):
        db.insert(
            "ingredients",
            code=f"extra{i}",
            version=1,
            name=f"Extra food {i}",
            kcal_100=100.0,
            protein_100=10.0,
            fat_100=1.0,
            carb_100=10.0,
            fibre_100=1.0,
            pack_g=500,
            pack_label="500 g",
            aisle="grains",
            pantry=0,
            use_whole_pack=0,
            approved=1,
            person_scope="both",
        )


# ---------------------------------------------------------------- 1. "Send /rate 4."
def test_01_told_to_send_rate_4_the_four_is_recorded(world) -> None:  # noqa: F811
    """`intents.apply` answers an unreadable rating with "I could not read that rating. Send
    /rate 4." and `/help` describes the command as "rate a dish 1-5". `cmd_rate` never looks at
    its argument: it finds today's dinner and sends the 1-5 buttons back. So the one command the
    tiffin hands someone as the way to fix a rating does not do the thing it is named for, and
    the person has typed a score that went nowhere."""
    pid, db, _tg, _gw, say, _tap, _run = world
    a_menu(db, pid, SATURDAY, SATURDAY, status="accepted")

    out = say("/rate 4")
    scores = [
        int(r["score"]) for r in db.all("SELECT score FROM ratings WHERE person_id = ?", (pid,))
    ]
    assert scores == [4], f"the 4 was dropped; the bot said: {out}"


# ---------------------------------------------------------------- 2. a menu you said yes to
def test_02_a_menu_you_accepted_is_a_menu_batch_can_cook(world) -> None:  # noqa: F811
    """The ordinary week: Saturday's proposal for Mon-Fri, one tap on Accept, and on Sunday
    `/batch` for the amounts to cook. `menus` is append-only, so `status` stays 'proposed' for
    ever and acceptance lives in `menu_decisions`. `menu_to_batch` looks for
    `status IN ('accepted','repeated')`, then for a menu covering today, then for a proposal with
    NO decision row - and an accepted future menu fails all three. `/menu` shows the dishes in
    the same breath as `/batch` says there is nothing to cook."""
    pid, db, _tg, _gw, say, _tap, _run = world
    mid = a_menu(db, pid, MONDAY, dt.date(2026, 9, 18), status="proposed")
    menu.decide(db, mid, "accepted")

    assert f"Dish {mid}" in say("/menu"), "the menu is there to be read"
    out = say("/batch")
    assert "No menu to cook from" not in out, f"the accepted menu is invisible to /batch: {out}"


# ---------------------------------------------------------------- 4. "tap Use this again"


# ------------------------------------- 5. "send /cook to take the ordinary one"
def test_05_told_to_send_cook_for_the_ordinary_menu_cook_is_not_refused(world) -> None:  # noqa: F811
    """A change the model cannot build answers "or send /cook to take the ordinary one" - and
    `_build` has already stamped the five-minute cooldown, because the stamp happens whenever
    `propose_now` returns rather than only when a menu was sent. Bare `/cook` is the one form
    the cooldown still refuses, so the advice walks straight into a wall, and the refusal offers
    a note-carrying `/cook` back: the thing that just failed."""
    pid, db, _tg, gateways, say, _tap, _run = world
    a_weight(db, pid)
    gateways.append(FakeGateway([]))  # every attempt answers with nothing usable

    failed = say("/cook more fish")
    assert "could not build a menu around that" in failed, failed
    assert "send /cook" in failed, f"the advice this test is about has moved: {failed}"

    ordinary = say("/cook")
    assert "Still working on the last one" not in ordinary, (
        f"refused the command it had just recommended: {ordinary}"
    )


# ---------------------------------------------------------------- 6. the morning message's dishes
def test_06_the_morning_message_names_the_days_meals_and_opens_one(world) -> None:  # noqa: F811
    """The message every day begins with names today's dishes as flat text: no meal buttons, no
    /menu, no /recipe. The template even carries a `page_line` slot that is always "". This is
    the `/menu` bug that was fixed in September, still standing in the one message nobody has to
    ask for: the dishes are named and there is no route from the name to the recipe."""
    pid, db, tg, _gw, _say, _tap, run_job = world
    a_weight(db, pid)
    mid = a_menu(db, pid, SATURDAY, SATURDAY, status="accepted")

    before = len(tg.sent)
    assert run_job(morning_targets.JOB, SATURDAY) == "ok"
    morning = next(m for m in tg.sent[before:] if "First up:" in m["text"])
    assert f"Dish {mid}" in morning["text"], morning["text"]
    reachable = any(d.startswith(("k:meal:", "k:menuv:")) for d in taps_on(morning))
    assert reachable or "/menu" in morning["text"] or "/recipe" in morning["text"], (
        f"the dishes are named with no way to open one: {morning['text']}"
    )


# ---------------------------------------- 7. "the rest tick on the web page"
def test_07_the_shopping_list_that_names_the_web_page_says_where_it_is(world) -> None:  # noqa: F811
    """Telegram caps the buttons, so a long list ends "Buttons for the first 12; the rest tick on
    the web page." The page is real and tailnet-only, its address is in config as `web.origin`,
    and no message Tiffin ever sends contains it. Standing in a shop with item thirteen, the
    person has been told where to go and not where that is."""
    pid, db, _tg, _gw, say, _tap, _run = world
    extra_ingredients(db, 6)
    ids = [int(r["id"]) for r in db.all("SELECT id FROM ingredients ORDER BY id")]
    assert len(ids) > TICK_BUTTONS, "the story needs a list longer than the buttons allow"
    list_id = db.insert("shopping_lists", kind="segment", person_id=pid)
    for ingredient_id in ids:
        db.insert(
            "shopping_items",
            list_id=list_id,
            ingredient_id=ingredient_id,
            grams_needed=500.0,
            packs=1,
            aisle="grains",
        )

    # while nobody has linked this person's Tailscale login, the page would answer them with a
    # 403: it is not named at all, and the buttons that fit are the ones still to find
    out = say("/shopping")
    assert "send /shopping again" in out and "http" not in out, f"unlinked, and: {out}"

    db.insert("tailscale_identities", person_id=pid, login="alex@example.com")
    out = say("/shopping")
    assert "the rest tick on" in out, f"the sentence this is about has moved: {out}"
    assert "example" in out or "http" in out, f"the page is named and never located: {out}"


# ---------------------------------------------------------------- 8. a menu accepted for you
def test_08_a_menu_the_tiffin_accepted_for_you_is_still_changeable(world) -> None:  # noqa: F811
    _pid, db, tg, gateways, _say, tap, run_job = world
    gateways.append(FakeGateway([proposal_for(menu.Segment(MONDAY, dt.date(2026, 9, 18)))]))
    assert run_job(menu_propose.JOB, SATURDAY) == "ok"
    mid = int(db.scalar("SELECT id FROM menus ORDER BY id DESC LIMIT 1"))
    assert menu.decision_of(db, mid) == "accepted", "decided for them, not by them"
    assert tg.sent, "and said, never in silence"

    out = tap(f"k:menux:{mid}")
    assert "what to change" in out, f"Change on a menu they never chose leads nowhere: {out}"


# ---------------------------------------------------------------- 10. /set's own "Elsewhere" list
def test_10_every_setting_set_names_can_be_typed_back_at_it(world) -> None:  # noqa: F811
    """`/set` prints an "Elsewhere" block: "training week: use /week, or send it as a PDF with
    /plan". Typing the name it just printed splits on the first space, so `/set training week`
    looks up "training", finds nothing, and answers "I do not know a setting called training" -
    forgetting the hint it had printed a moment earlier. The one-word entries all answer
    properly, which is how this stayed hidden."""
    _pid, _db, _tg, _gw, say, _tap, _run = world
    listing = say("/set")
    lost = []
    for name in settings.FIXED:
        assert name in listing, f"/set no longer mentions {name}"
        if "do not know a setting" in say(f"/set {name}"):
            lost.append(name)
    assert not lost, f"/set names these and then does not recognise them: {lost}"
