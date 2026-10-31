"""Regression tests for applying food preferences to menus and messages."""

from __future__ import annotations

import datetime as dt
import json
import types
from typing import Any

import pytest

from tiffin.app import App
from tiffin.clock import isoday
from tiffin.db import Db
from tiffin.food import catalogue, prefs
from tiffin.secrets import Secrets

from .fakes import FakeTelegram
from .test_food_shopping import seed_ingredient, seed_recipe
from .test_streamline import _new_profile
from .test_swap import (  # noqa: F401 (fixtures)
    MON,
    OWNER_CHAT,
    _buttons,
    _headers,
    _seed,
    bot,
    page,
    week,
)

#: an answer to the interview's "Foods you refuse" with no comma in it: one entry, kept whole
LONG = "I really don't like mushrooms or anything with olives"


def _houmous(db: Db) -> int:
    return seed_ingredient(db, "houmous", "Houmous", "condiments", 200.0, "200 g tub")


def _approved(db: Db, pid: int) -> set[str]:
    return {i.code for i in catalogue.approved(db, pid)}


def _markup(msg: dict[str, Any]) -> dict[str, Any]:
    markup = msg.get("reply_markup") or {}
    return json.loads(markup) if isinstance(markup, str) else markup


def _labels(markup: dict[str, Any]) -> list[str]:
    return [b["text"] for row in markup.get("inline_keyboard", []) for b in row]


@pytest.fixture
def chat(fixture_db: Db, two_persons, frozen_clock, config, tmp_path):
    """Test_swap's `bot` over the same week, with the fake Telegram in reach and a tap that
    carries the keyboard of the message it was on, the way Telegram's own callback does, so
    what an answer leaves on that message can be read."""
    from tiffin.telegram.poller import Poller

    db, pid = fixture_db, two_persons["owner"]
    _seed(db, pid)
    secrets_dir = tmp_path / "s"
    secrets_dir.mkdir()
    (secrets_dir / "telegram-token-dev.txt").write_text("1:t", encoding="utf-8")
    tg = FakeTelegram()
    app = App(
        config,
        Secrets(secrets_dir),
        clock=frozen_clock,
        transport=tg,
        gateway_factory=lambda _a, _d: None,
    )
    poller = Poller(
        app.api(),
        db,
        app.registry(db),
        app.handlers(db).handle,
        clock=frozen_clock,
        sleeper=lambda s: None,
    )
    n = {"i": 0}

    def said(update: dict[str, Any]) -> list[dict[str, Any]]:
        before = len(tg.sent)
        tg.queue_update(update)
        poller.run_once()
        return tg.sent[before:]

    def send(text: str) -> list[dict[str, Any]]:
        n["i"] += 1
        return said(tg.text_update(n["i"], OWNER_CHAT, text))

    def tap(msg: dict[str, Any], label: str) -> list[dict[str, Any]]:
        n["i"] += 1
        update = tg.callback_update(
            n["i"], OWNER_CHAT, _buttons(msg)[label], message_id=msg["message_id"]
        )
        update["callback_query"]["message"]["reply_markup"] = _markup(msg)
        return said(update)

    return types.SimpleNamespace(send=send, tap=tap, tg=tg, db=db, pid=pid, clock=frozen_clock)


# ---------------------------------------------------------------- #32 a food refused by itself
def test_a_food_refused_by_itself_is_allowed_back_by_its_name(week) -> None:  # noqa: F811
    """Taking a food's name off the avoid list allows back the food refused
    by itself, and says so, where it said "Not on the list" and kept it off."""
    db, pid, _base, _r, clock = week
    _houmous(db)
    prefs.refuse_food(db, pid, "houmous", clock=clock)
    got = prefs.amend(db, pid, "avoid", remove=["houmous"], strict=False, clock=clock)
    assert got.allowed == ("Houmous",) and got.changed
    assert got.said() == "Allowed back: Houmous."
    assert "houmous" not in catalogue.refused_codes(db, pid)
    assert "houmous" in _approved(db, pid)


def test_set_avoid_minus_allows_back_a_food_kept_off_from_its_question(bot) -> None:  # noqa: F811
    """After "Keep houmous off", `/set avoid - houmous` has houmous back on
    her menus and says so."""
    send, tap, db, pid, _r, _clock = bot
    _houmous(db)
    out = send("I do not want chickpeas")
    tap(_buttons(out[0])["Keep houmous off"])
    assert "houmous" not in _approved(db, pid)
    back = send("/set avoid - houmous")[0]["text"]
    assert "Allowed back: Houmous." in back and "Not on the list" not in back, back
    assert "houmous" in _approved(db, pid)
    assert catalogue.refused_terms(db, pid) == ["chickpeas"], "the word stays as it was"


# ---------------------------------------------------------------- #35 an entry naming nothing
def test_an_entry_naming_no_food_is_not_said_to_keep_anything_off(week) -> None:  # noqa: F811
    """An entry that names no food yet is said to be on the list and to
    keep nothing off, never announced as "Kept off your menus from now on"."""
    db, pid, _base, _r, clock = week
    got = prefs.amend(db, pid, "avoid", add=["lentls"], strict=False, clock=clock)
    assert got.added == ("lentls",) and got.unplaced == ("lentls",)
    said = got.said()
    assert "Kept off your menus" not in said, said
    assert "'lentls'" in said and "nothing" in said, said
    assert "lentils" in _approved(db, pid)


def test_set_avoid_x_and_y_keeps_both_foods_off(bot) -> None:  # noqa: F811
    """`/set avoid lentils and broccoli` is two entries, each keeping its
    food off, not one entry that matches nothing."""
    send, _tap, db, pid, _r, _clock = bot
    text = send("/set avoid lentils and broccoli")[0]["text"]
    assert catalogue.refused_terms(db, pid) == ["broccoli", "lentils"], text
    assert "Kept off your menus from now on" in text and "No food I plan with" not in text
    assert not {"lentils", "broccoli"} & _approved(db, pid)


def test_the_page_reads_x_and_y_as_two_foods_and_a_dish_as_one_entry(page) -> None:  # noqa: F811
    """On the page "lentils & broccoli" is two entries; a dish whose name
    joins a food to no food ("mac and cheese") stays one entry, so it does not keep all cheese
    off."""
    client, _db, _partners, _his, _r = page
    h = _headers(client)
    got = client.post("/api/prefs", json={"list": "avoid", "add": "lentils & broccoli"}, headers=h)
    assert got.status_code == 200, got.text
    assert [a["term"] for a in got.json()["prefs"]["avoid"]] == ["broccoli", "lentils"]
    dish = client.post("/api/prefs", json={"list": "avoid", "add": "mac and cheese"}, headers=h)
    assert "mac and cheese" in [a["term"] for a in dish.json()["prefs"]["avoid"]]
    assert "Kept off your menus" not in dish.json()["said"]


# ---------------------------------------------------------------- #36 a wider word still there
def test_a_word_a_wider_entry_still_covers_is_not_said_to_be_back(week) -> None:  # noqa: F811
    """Taking "lentils" off while "pulses" stays says that "pulses" still
    keeps them off, not "Allowed back"."""
    db, pid, _base, _r, clock = week
    prefs.amend(db, pid, "avoid", add=["lentils"], clock=clock)
    wider = prefs.amend(db, pid, "avoid", add=["pulses"], strict=False, clock=clock)
    assert wider.added == ("pulses",)
    got = prefs.amend(db, pid, "avoid", remove=["lentils"], clock=clock)
    assert got.removed == ("lentils",)
    said = got.said()
    assert "Allowed back" not in said, said
    assert "lentils is still kept off by 'pulses'" in said, said
    assert "lentils" not in _approved(db, pid)


def test_a_word_whose_food_is_refused_by_itself_is_not_said_to_be_back(week) -> None:  # noqa: F811
    """Taking a word off while its food is also refused by itself says the
    food is still kept off as a single food, not "Allowed back"."""
    db, pid, _base, _r, clock = week
    prefs.refuse_food(db, pid, "lentils", clock=clock)
    prefs.amend(db, pid, "avoid", add=["lentils"], clock=clock)
    said = prefs.amend(db, pid, "avoid", remove=["lentils"], clock=clock).said()
    assert "Allowed back" not in said, said
    assert "Red lentils is still kept off as a single food" in said, said
    assert "lentils" not in _approved(db, pid)


def test_undo_never_again_says_a_word_still_keeps_the_food_off(week) -> None:  # noqa: F811
    """Allowing back a food refused by itself while a word on the list
    still names it says which word keeps it off."""
    db, pid, _base, _r, clock = week
    prefs.refuse_food(db, pid, "lentils", clock=clock)
    prefs.amend(db, pid, "avoid", add=["lentils"], clock=clock)
    got = prefs.allow_food(db, pid, "lentils", clock=clock)
    assert got.changed
    assert got.said() == (
        "Red lentils is still kept off by 'lentils': take that off to have it back."
    )


# ---------------------------------------------------------------- #88 a long entry
def test_an_entry_longer_than_a_food_name_comes_off_by_its_own_button(bot) -> None:  # noqa: F811
    """An entry over 40 characters written whole (the interview's answer,
    `/set avoid = ...`) is taken off by its Allow button, not answered "Already back"."""
    send, tap, db, pid, _r, _clock = bot
    send(f"/set avoid = {LONG}")
    assert prefs.terms_of(db, pid, "avoid") == [LONG]
    out = send("/avoid")
    allow = [d for d in _buttons(out[0]).values() if d.startswith("k:avrm:")]
    assert len(allow) == 1
    back = tap(allow[0])[-1]["text"]
    assert "Already back" not in back and "Allowed back" in back, back
    assert prefs.terms_of(db, pid, "avoid") == []


def test_set_avoid_minus_and_the_page_take_a_long_entry_off(page) -> None:  # noqa: F811
    """A long entry comes off the page by its chip, however long it is."""
    client, db, partners, _his, _r = page
    longer = LONG + " and capers, and anchovies too"  # 83 characters
    _new_profile(db, partners, refused_foods=json.dumps([LONG, longer]))
    h = _headers(client)
    for entry in (LONG, longer):
        got = client.post("/api/prefs", json={"list": "avoid", "remove": [entry]}, headers=h)
        assert got.status_code == 200, got.text
        assert "Not on the list" not in got.json()["said"], got.json()["said"]
    assert prefs.terms_of(db, partners, "avoid") == []


# ---------------------------------------------------------------- #107 a full list
def test_a_full_list_says_it_is_full(week) -> None:  # noqa: F811
    """Adding to a list that holds 40 already says it is full, not that
    the food would leave nothing to eat."""
    db, pid, _base, _r, clock = week
    prefs.amend(db, pid, "likes", add=[f"dish {i}" for i in range(40)], clock=clock)
    got = prefs.amend(db, pid, "likes", add=["olives"], clock=clock)
    assert got.full == ("olives",) and got.too_wide == () and not got.changed
    said = got.said()
    assert "nothing to eat" not in said, said
    assert "holds 40 already" in said and "take one off first" in said, said


# ---------------------------------------------------------------- #126 "Allowed back" said true
def test_allowing_a_food_back_on_the_page_says_what_is_true(page) -> None:  # noqa: F811
    """The page's way back says a word still keeps the food off when one
    does, and that a food never refused stays on the menus, never a bare "Allowed back"."""
    client, _db, _partners, _his, _r = page
    h = _headers(client)
    client.post("/api/prefs", json={"list": "avoid", "add": ["lentils"]}, headers=h)
    kept = client.post("/api/prefs/food", json={"food": "lentils", "refused": False}, headers=h)
    assert kept.status_code == 200, kept.text
    assert kept.json()["said"] == (
        "Red lentils is still kept off by 'lentils': take that off to have it back."
    )
    rice = client.post("/api/prefs/food", json={"food": "rice", "refused": False}, headers=h)
    assert rice.json()["said"] == "Basmati rice stays on your menus."


# ---------------------------------------------------------------- #135 a name too long
def test_a_name_longer_than_a_food_is_refused_in_words(page) -> None:  # noqa: F811
    """A name over 40 characters is refused with a sentence for people,
    never cut to 40 and stored, and never "add is too long"."""
    client, db, partners, _his, _r = page
    h = _headers(client)
    for name in ("S" * 59, "S" * 72):
        got = client.post("/api/prefs", json={"list": "avoid", "add": name}, headers=h)
        assert got.status_code == 400, got.text
        assert got.json()["detail"] == "That is longer than a food name: 40 letters at most."
    assert prefs.terms_of(db, partners, "avoid") == []


def test_amend_never_cuts_a_long_name_to_fit(week) -> None:  # noqa: F811
    """Through any other door a name over 40 characters is not written,
    and the answer names it whole."""
    db, pid, _base, _r, clock = week
    got = prefs.amend(db, pid, "avoid", add=[LONG], strict=False, clock=clock)
    assert got.added == () and not got.changed
    assert f"'{LONG}'" in got.said()
    assert prefs.terms_of(db, pid, "avoid") == []


# ---------------------------------------------------------------- #33 the other buttons stay
def test_answering_the_made_from_question_leaves_swap_and_undo(chat) -> None:
    """Answering "Houmous is fine" takes only that question's row off its
    message; Swap it now and Undo stay."""
    _houmous(chat.db)
    msg = chat.send("I do not want lentils and chickpeas")[0]
    assert {"Swap it now", "Undo", "Houmous is fine"} <= set(_buttons(msg)), _buttons(msg)
    fine = chat.tap(msg, "Houmous is fine")
    assert "Houmous stays on your menus." in fine[-1]["text"]
    left = _labels(chat.tg.edited[-1]["reply_markup"])
    assert left == ["Swap it now", "Undo"], left


def test_one_allow_on_avoid_leaves_the_others(chat) -> None:
    """Tapping one Allow button under /avoid takes that one off and leaves
    every other Allow button where it was."""
    chat.send("/avoid lentils, salmon")
    msg = chat.send("/avoid")[0]
    chat.tap(msg, "Allow lentils")
    assert catalogue.refused_terms(chat.db, chat.pid) == ["salmon"]
    left = _labels(chat.tg.edited[-1]["reply_markup"])
    assert left == ["Allow salmon"], left
    later = chat.tap(msg, "Allow salmon")
    assert "Allowed back: salmon" in later[-1]["text"], "the button left behind still works"
    assert catalogue.refused_terms(chat.db, chat.pid) == []


# ---------------------------------------------------------------- #50 every dish reachable
def _menu_each_day(db: Db, pid: int, start: dt.date, days: list[dict[str, int]]) -> int:
    """An accepted menu with a different dish at every meal of every day."""
    mid = db.insert(
        "menus",
        person_id=pid,
        segment_start=isoday(start),
        segment_end=isoday(start + dt.timedelta(days=len(days) - 1)),
        status="accepted",
    )
    for i, slots in enumerate(days):
        did = db.insert(
            "menu_days",
            menu_id=mid,
            day=isoday(start + dt.timedelta(days=i)),
            kcal=1300,
            protein_g=90,
            fat_g=25,
            carb_g=150,
            fibre_g=20,
        )
        for slot, rid in slots.items():
            db.insert(
                "meal_slots",
                menu_day_id=did,
                slot=slot,
                recipe_id=rid,
                portion_multiplier=1.0,
                portion_g=300,
                kcal=325,
                protein_g=22,
                fat_g=6,
                carb_g=37,
                fibre_g=5,
            )
    return mid


def test_swap_a_dish_reaches_every_dish_of_a_different_every_day_week(bot) -> None:  # noqa: F811
    """ "Swap a dish" under a Mon-Fri menu with a different dish at each of
    four meals reaches Thursday's lunch, never stopping at Tuesday without a word."""
    _send, tap, db, pid, _r, _clock = bot
    oats = int(db.scalar("SELECT id FROM ingredients WHERE code = 'oats'"))
    slots = ("breakfast", "lunch", "dinner", "snack")
    days = [
        {s: seed_recipe(db, f"{d} {s}", 1, {oats: 60}) for s in slots}
        for d in ("Mon", "Tue", "Wed", "Thu", "Fri")
    ]
    mid = _menu_each_day(db, pid, MON, days)
    reached: list[str] = []
    data = f"k:swappick:{mid}"
    for _page in range(5):
        msg = tap(data)[-1]
        assert msg["text"].startswith("Which dish?"), msg["text"]
        buttons = _buttons(msg)
        assert len(buttons) <= 9, "one screen at a time"
        reached += [d for d in buttons.values() if d.startswith("k:swapmeal:")]
        later = [d for label, d in buttons.items() if label.startswith("Later days")]
        if not later:
            break
        data = later[0]
    thursday = isoday(MON + dt.timedelta(days=3))
    assert f"k:swapmeal:{thursday}:lunch" in reached, reached
    assert len(set(reached)) == 20, reached


def test_swap_a_dish_on_a_short_menu_is_still_one_list(bot) -> None:  # noqa: F811
    """A menu with few dishes still lists them all at once, with no Later
    days button."""
    _send, tap, db, pid, _r, _clock = bot
    mid = int(db.scalar("SELECT id FROM menus WHERE person_id = ? ORDER BY id DESC", (pid,)))
    labels = list(_buttons(tap(f"k:swappick:{mid}")[-1]))
    assert labels == [
        "Breakfast: Porridge with yoghurt",
        "Lunch: Lentil dal",
        "Dinner: Chicken and rice",
    ]


# ---------------------------------------------------------------- #108 a way back for each
def test_avoid_gives_a_food_refused_by_itself_a_button_past_ten_words(bot) -> None:  # noqa: F811
    """With ten words on the list, a food refused by itself still has its
    Allow button under /avoid, and a word without a button is said how to take off."""
    send, tap, db, pid, _r, clock = bot
    words = [
        "salmon",
        "chicken",
        "rice",
        "oats",
        "yoghurt",
        "broccoli",
        "pepper",
        "prawns",
        "chickpeas",
        "fish",
    ]
    send(f"/set avoid = {', '.join(words)}")
    prefs.refuse_food(db, pid, "lentils", clock=clock)
    out = send("/avoid")[0]
    buttons = _buttons(out)
    assert buttons.get("Allow Red lentils", "").startswith("k:avok:"), buttons
    assert len(buttons) <= 10
    assert "<code>/set avoid - " in out["text"], out["text"]
    back = tap(buttons["Allow Red lentils"])[-1]["text"]
    assert "Allowed back: Red lentils." in back
    assert "lentils" in _approved(db, pid)


# ---------------------------------------------------------------- #30 a list cleared
@pytest.mark.parametrize("said", ["/set likes =", "/set likes none", "/set likes clear"])
def test_clearing_likes_is_answered_and_leaves_an_empty_list(bot, said: str) -> None:  # noqa: F811
    """Clearing the likes list with /set answers, and leaves the list empty
    rather than failing on NOT NULL in silence."""
    send, _tap, db, pid, _r, _clock = bot
    send("/set likes = salmon, rice")
    assert prefs.terms_of(db, pid, "likes") == ["salmon", "rice"]
    out = send(said)
    assert out and "likes cleared" in out[0]["text"], out
    raw = db.scalar(
        "SELECT liked_foods FROM profiles WHERE person_id = ? ORDER BY id DESC LIMIT 1", (pid,)
    )
    assert raw == "[]"


# ---------------------------------------------------------------- #118 a list name not a string
@pytest.mark.parametrize("name", [["avoid"], {"avoid": 1}, 3])
def test_a_list_name_that_is_not_a_word_is_a_400(page, name: Any) -> None:  # noqa: F811
    """A list name that is not a string is answered 400 with what it must
    be, never a 500."""
    client, _db, _partners, _his, _r = page
    got = client.post("/api/prefs", json={"list": name, "add": "x"}, headers=_headers(client))
    assert got.status_code == 400, got.text
    assert got.json()["detail"] == "list is avoid, likes or pantry"
