"""Regression tests for resizing meals on a day."""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

import pytest

from tiffin.errors import Infeasible, NothingToBuild
from tiffin.food import plates, swap
from tiffin.telegram.handlers import Handlers

from .test_resize_controls import _unbox
from .test_swap import MON, _buttons, _headers, bot, page, week  # noqa: F401
from .web_source import app_source

TUE = MON + dt.timedelta(days=1)
STATIC = Path(__file__).resolve().parents[1] / "src" / "tiffin" / "web" / "static"


def _js() -> str:
    return app_source()


def _block(js: str, name: str) -> str:
    start = js.index(f"function {name}(")
    return js[start : js.index("\n}\n", start)]


def _kcal(db, pid: int, day: dt.date) -> dict[str, int]:
    return {p.slot: p.macros.kcal for p in swap.plates_on(db, pid, day)}


def _menu_id(db, pid: int) -> int:
    return int(
        db.scalar("SELECT id FROM menus WHERE person_id = ? ORDER BY id DESC LIMIT 1", (pid,))
    )


def _levels(db, pid: int, day: dt.date) -> dict[str, str]:
    st = plates.state(db, pid, day)
    assert st is not None
    return dict(st["levels"])


def _day(client, day: dt.date) -> dict:
    got = client.get(
        "/api/menu", params={"segment": day.isoformat()}, headers=_headers(client)
    ).json()
    return next(d for d in got["days"] if d["day"] == day.isoformat())


# ---------------------------------------------------------------- #3: the Telegram card
def test_a_card_tap_today_holds_the_breakfast_already_eaten(bot) -> None:  # noqa: F811
    """Less on today's dinner card at 14:00 leaves the breakfast already
    eaten as it was, and the reply names every plate that moved."""
    _send, tap, db, pid, _r, clock = bot
    _unbox(db, pid)
    clock.set_local(dt.datetime(2026, 9, 15, 14, 0))
    before = _kcal(db, pid, TUE)
    card = tap(f"k:meal:{_menu_id(db, pid)}:dinner:{TUE.isoformat()}")
    less = tap(_buttons(card[-1])["Less"])
    after = _kcal(db, pid, TUE)
    assert after["breakfast"] == before["breakfast"], "eaten at 08:00, held as it was eaten"
    # the week's targets sit above this fixture's plan, so the day grows back to them; dinner's
    # part of it is what Less makes smaller
    assert after["dinner"] / after["lunch"] < before["dinner"] / before["lunch"]
    text = less[-1]["text"]
    assert text.startswith("Dinner less.")
    assert f"dinner {before['dinner']:,} to {after['dinner']:,}" in text.lower()
    assert f"lunch {before['lunch']:,} to {after['lunch']:,}" in text.lower()
    assert "breakfast" not in text.lower(), "a plate that did not move is not named"


def _pre_bed(db, pid: int, day: dt.date, recipe_id: int) -> None:
    """A half-portion pre-bed plate added to `day` of the menu in force."""
    from tiffin.food import recipes

    did = db.scalar(
        "SELECT id FROM menu_days WHERE menu_id = ? AND day = ?",
        (_menu_id(db, pid), day.isoformat()),
    )
    m = recipes.stored_per_serving(db, recipe_id, 0.5).as_macros()
    db.insert(
        "meal_slots",
        menu_day_id=did,
        slot="pre_bed",
        recipe_id=recipe_id,
        portion_multiplier=0.5,
        portion_g=150,
        kcal=m.kcal,
        protein_g=m.protein_g,
        fat_g=m.fat_g,
        carb_g=m.carb_g,
        fibre_g=m.fibre_g,
    )


def test_dinner_is_behind_the_clock_once_the_evening_check_in_is_due(bot) -> None:  # noqa: F811
    """Verification of #3: `meal_now` says "dinner" all evening, so a Skip on the pre-bed card at
    21:30 made the dinner already eaten bigger. From the evening check-in dinner is held: the
    pre-bed card offers no row (nothing else is left to make room), an old Skip on it leaves
    dinner as it was, and dinner's own card is answered in words."""
    _send, tap, db, pid, r, clock = bot
    _unbox(db, pid)
    _pre_bed(db, pid, TUE, r["porridge"])
    clock.set_local(dt.datetime(2026, 9, 15, 19, 0))
    assert plates.past_meals(db, pid, TUE, clock) == {"breakfast", "lunch"}
    clock.set_local(dt.datetime(2026, 9, 15, 21, 30))
    assert plates.past_meals(db, pid, TUE, clock) == {"breakfast", "lunch", "dinner"}
    base = _menu_id(db, pid)
    card = tap(f"k:meal:{base}:pre_bed:{TUE.isoformat()}")
    assert "Skip" not in _buttons(card[-1]), "dinner is eaten: no other plate can make room"
    before = plates._in_force(db, pid, TUE)
    tap(f"k:size:{TUE.isoformat()}:pre_bed:skip")
    assert plates._in_force(db, pid, TUE)["dinner"] == before["dinner"], "dinner held as eaten"
    old = tap(f"k:size:{TUE.isoformat()}:dinner:less")
    assert "Dinner is already behind you today" in old[-1]["text"]


def test_the_page_sends_dinner_as_eaten_after_the_evening_check_in(page, frozen_clock) -> None:  # noqa: F811
    """Verification of #3/#15: the page's `past` flags come from the same rule, so after the
    evening check-in dinner is sent as eaten and its card has no size row."""
    client, db, partners, _his, _r = page
    _unbox(db, partners)
    frozen_clock.set_local(dt.datetime(2026, 9, 15, 21, 30))
    meals = {m["slot"]: m for m in _day(client, TUE)["plates"]["meals"]}
    assert meals["dinner"]["past"] is True and meals["dinner"]["sizable"] is False


def test_a_meal_already_behind_you_today_is_not_resized_from_its_card(bot) -> None:  # noqa: F811
    """The card of a meal the clock has passed today offers no Less, More or
    Skip, and an old button for it is answered in words without changing anything."""
    _send, tap, db, pid, _r, clock = bot
    _unbox(db, pid)
    clock.set_local(dt.datetime(2026, 9, 15, 14, 0))
    card = tap(f"k:meal:{_menu_id(db, pid)}:breakfast:{TUE.isoformat()}")
    assert "Less" not in _buttons(card[-1]), "breakfast was eaten this morning"
    menus = db.scalar("SELECT COUNT(*) FROM menus")
    old = tap(f"k:size:{TUE.isoformat()}:breakfast:less")
    assert "Breakfast is already behind you today" in old[-1]["text"]
    assert "Not quite" in old[-1]["text"], "and says what to do instead"
    assert db.scalar("SELECT COUNT(*) FROM menus") == menus, "nothing stored"


# ---------------------------------------------------------------- #15: the page's card
def test_the_page_card_holds_todays_eaten_meals(page, frozen_clock) -> None:  # noqa: F811
    """The page tells the card which of today's meals are behind the clock,
    offers no size row on them, and the card sends them as eaten."""
    client, db, partners, _his, _r = page
    _unbox(db, partners)
    frozen_clock.set_local(dt.datetime(2026, 9, 15, 14, 0))
    meals = {m["slot"]: m for m in _day(client, TUE)["plates"]["meals"]}
    assert meals["breakfast"]["past"] is True and meals["breakfast"]["sizable"] is False
    assert meals["lunch"]["past"] is False and meals["lunch"]["sizable"] is True
    assert meals["dinner"]["sizable"] is True
    assert all(
        m["past"] is False for m in _day(client, TUE + dt.timedelta(days=1))["plates"]["meals"]
    )
    row = _block(_js(), "sizeRow")
    assert "!meal.sizable" in row, "no Less, More or Skip on a meal already eaten"
    assert re.search(r"eaten = \(day\.plates\.meals[^)]*\)\.filter\(\(m\) => m\.past\)", row)
    assert re.search(r"body: \{ day: day\.day, levels: [^\n]*, eaten \}", row)
    # what the card sends: breakfast held, the rest of the day making room
    before = _kcal(db, partners, TUE)
    done = client.post(
        "/api/menu/plates",
        json={"day": TUE.isoformat(), "levels": {"dinner": "less"}, "eaten": ["breakfast"]},
        headers=_headers(client),
    )
    assert done.status_code == 200, done.text
    assert _kcal(db, partners, TUE)["breakfast"] == before["breakfast"]


# ---------------------------------------------------------------- #22: the only free plate
@pytest.mark.parametrize("level", ["less", "more"])
def test_the_only_plate_that_can_move_is_not_pretended_to_move(week, config, level) -> None:  # noqa: F811
    """On a weekday whose lunch and dinner come out of the box, Less or More
    on breakfast is refused in words and nothing is stored."""
    db, pid, _base, _r, clock = week
    menus = db.scalar("SELECT COUNT(*) FROM menus")
    with pytest.raises(NothingToBuild, match="only plate"):
        plates.resize(db, pid, TUE, {"breakfast": level}, config=config, clock=clock)
    assert db.scalar("SELECT COUNT(*) FROM menus") == menus
    assert _levels(db, pid, TUE) == {}


def test_the_page_offers_no_size_row_on_the_only_free_plate(page) -> None:  # noqa: F811
    """The page's card has no Less, More or Skip on a plate nothing else can
    make room for, and a resize asking it anyway is refused with the reason."""
    client, db, partners, _his, _r = page
    meals = {m["slot"]: m for m in _day(client, TUE)["plates"]["meals"]}
    assert meals["breakfast"]["sizable"] is False, "lunch and dinner come out of the box"
    asked = client.post(
        "/api/menu/plates",
        json={"day": TUE.isoformat(), "levels": {"breakfast": "less"}},
        headers=_headers(client),
    )
    assert asked.status_code == 409 and "only plate" in asked.json()["detail"]
    assert _levels(db, partners, TUE) == {}


def test_a_skip_leaving_one_plate_set_to_less_says_how_to_get_it(bot) -> None:  # noqa: F811
    """Verification of #22: Less on dinner at 09:00, then Skip on the pre-bed card at 19:00 with
    breakfast and lunch eaten, leaves dinner the only plate to move while it is set to less. The
    refusal is about the Skip that was tapped and names the way to it, which the dinner card has
    a button for; after it, the same Skip works."""
    _send, tap, db, pid, r, clock = bot
    _unbox(db, pid)
    _pre_bed(db, pid, TUE, r["porridge"])
    base = _menu_id(db, pid)
    clock.set_local(dt.datetime(2026, 9, 15, 9, 0))
    tap(f"k:size:{TUE.isoformat()}:dinner:less")
    clock.set_local(dt.datetime(2026, 9, 15, 19, 0))
    pot = _buttons(tap(f"k:meal:{base}:pre_bed:{TUE.isoformat()}")[-1])
    menus = db.scalar("SELECT COUNT(*) FROM menus")
    text = tap(pot["Skip"])[-1]["text"]
    assert text.startswith("With pre-bed left out, dinner is the only plate on Tue 15 Sep"), text
    assert "Put dinner back to as planned first." in text
    assert "If you eat less" not in text, "not about eating less: the ask was a Skip"
    assert db.scalar("SELECT COUNT(*) FROM menus") == menus, "nothing stored"
    dinner = _buttons(tap(f"k:meal:{base}:dinner:{TUE.isoformat()}")[-1])
    assert dinner["As planned"] == f"k:size:{TUE.isoformat()}:dinner:planned"
    tap(dinner["As planned"])
    assert _levels(db, pid, TUE)["dinner"] == "planned"
    assert tap(pot["Skip"])[-1]["text"].startswith("Pre-bed left out.")
    assert _levels(db, pid, TUE)["pre_bed"] == "skip"


def _pin_pot(db, pid: int) -> None:
    """The profile's pre-bed pot is kept as written: asked for as planned, it does not move."""
    row = db.one(
        "SELECT * FROM profiles WHERE person_id = ? ORDER BY effective_from DESC, id DESC LIMIT 1",
        (pid,),
    )
    fields = {k: row[k] for k in row.keys() if k not in ("id", "created_at")}  # noqa: SIM118
    fields["pre_bed_protein"] = 1
    db.insert("profiles", **fields)


def test_the_way_back_named_by_a_refusal_has_a_button_when_the_pot_is_pinned(bot) -> None:  # noqa: F811
    """Second verification of #22: with the profile's pre-bed pot kept as written, dinner is the
    only plate left to move once breakfast and lunch are behind the clock, so its card has no
    Less, More or Skip. The Skip on the pre-bed card says "put dinner back to as planned first",
    and dinner's card carries that one button, which works."""
    _send, tap, db, pid, r, clock = bot
    _unbox(db, pid)
    _pin_pot(db, pid)
    _pre_bed(db, pid, TUE, r["porridge"])
    base = _menu_id(db, pid)
    clock.set_local(dt.datetime(2026, 9, 15, 9, 0))
    tap(f"k:size:{TUE.isoformat()}:dinner:less")
    clock.set_local(dt.datetime(2026, 9, 15, 19, 0))
    assert plates.one_tap(db, pid, TUE, clock) == {"pre_bed"}
    pot = _buttons(tap(f"k:meal:{base}:pre_bed:{TUE.isoformat()}")[-1])
    text = tap(pot["Skip"])[-1]["text"]
    assert "Put dinner back to as planned first." in text, text
    dinner = _buttons(tap(f"k:meal:{base}:dinner:{TUE.isoformat()}")[-1])
    assert "Less" not in dinner and "Skip" not in dinner, "the only plate left that can move"
    assert dinner["As planned"] == f"k:size:{TUE.isoformat()}:dinner:planned"
    assert tap(dinner["As planned"])[-1]["text"].startswith("Dinner as planned."), "and it works"
    assert _levels(db, pid, TUE)["dinner"] == "planned"
    again = _buttons(tap(f"k:meal:{base}:dinner:{TUE.isoformat()}")[-1])
    assert "As planned" not in again, "nothing left to put back"


def test_the_page_card_offers_the_way_back_on_the_only_plate_left(page, frozen_clock) -> None:  # noqa: F811
    """Second verification of #22, on the page: dinner set to less and the only plate left to
    move takes no Less, More or Skip, but the server says its card offers As planned, and
    sizeRow draws that one button from the flag."""
    client, db, partners, _his, r = page
    _unbox(db, partners)
    _pin_pot(db, partners)
    _pre_bed(db, partners, TUE, r["porridge"])
    frozen_clock.set_local(dt.datetime(2026, 9, 15, 9, 0))
    done = client.post(
        "/api/menu/plates",
        json={"day": TUE.isoformat(), "levels": {"dinner": "less"}},
        headers=_headers(client),
    )
    assert done.status_code == 200, done.text
    frozen_clock.set_local(dt.datetime(2026, 9, 15, 19, 0))
    meals = {m["slot"]: m for m in _day(client, TUE)["plates"]["meals"]}
    assert meals["dinner"]["sizable"] is False and meals["dinner"]["resettable"] is True
    assert meals["breakfast"]["resettable"] is False, "behind the clock: as it was eaten"
    assert meals["pre_bed"]["resettable"] is False, "already as planned"
    row = _block(_js(), "sizeRow")
    assert "meal.resettable" in row
    back = client.post(
        "/api/menu/plates",
        json={
            "day": TUE.isoformat(),
            "levels": {"dinner": "planned"},
            "eaten": ["breakfast", "lunch"],
        },
        headers=_headers(client),
    )
    assert back.status_code == 200, back.text
    assert _levels(db, partners, TUE)["dinner"] == "planned"


@pytest.mark.parametrize("level", ["more", "lots"])
def test_the_only_plate_left_too_small_for_the_day_is_refused_for_that(week, config, level) -> None:  # noqa: F811
    """Second verification of #22: lunch alone on a day still to come cannot carry the day even
    at its largest, so More on it is refused for that, never "more of it would put the day
    over" on a day hundreds of kcal short."""
    db, pid, _base, _r, clock = week
    _unbox(db, pid)
    wed = TUE + dt.timedelta(days=1)
    with pytest.raises(Infeasible, match="cannot carry the whole day, even at their largest"):
        plates.resize(
            db,
            pid,
            wed,
            {"breakfast": "skip", "dinner": "skip", "lunch": level},
            config=config,
            clock=clock,
            dry_run=True,
        )


def test_a_skip_no_plate_could_make_room_for_is_refused_for_that(bot) -> None:  # noqa: F811
    """Verification of #22, the probe as found: Less on lunch at 09:00, then Skip on dinner's
    card at 12:00. With breakfast eaten, lunch could not carry the day even as planned, so the
    refusal says that, not "less of it would leave the day short", and names no way out that
    would only be refused in turn."""
    _send, tap, db, pid, _r, clock = bot
    _unbox(db, pid)
    base = _menu_id(db, pid)
    clock.set_local(dt.datetime(2026, 9, 15, 9, 0))
    tap(f"k:size:{TUE.isoformat()}:lunch:less")
    clock.set_local(dt.datetime(2026, 9, 15, 12, 0))
    card = tap(f"k:meal:{base}:dinner:{TUE.isoformat()}")
    menus = db.scalar("SELECT COUNT(*) FROM menus")
    text = tap(_buttons(card[-1])["Skip"])[-1]["text"]
    assert text.startswith("Those plates cannot carry the whole day"), text
    assert "If you eat less" not in text and "as planned first" not in text
    assert db.scalar("SELECT COUNT(*) FROM menus") == menus, "nothing stored"
    assert _levels(db, pid, TUE) == {"breakfast": "planned", "lunch": "less", "dinner": "planned"}


def test_a_skip_on_a_future_day_is_refused_for_the_ask(week, config) -> None:  # noqa: F811
    """Verification of #22, on a day still to come: breakfast left out, lunch less, then dinner
    left out too. Lunch alone could not carry the day as planned either."""
    db, pid, _base, _r, clock = week
    _unbox(db, pid)
    wed = TUE + dt.timedelta(days=1)
    plates.resize(db, pid, wed, {"breakfast": "skip"}, config=config, clock=clock)
    plates.resize(db, pid, wed, {"breakfast": "skip", "lunch": "less"}, config=config, clock=clock)
    with pytest.raises(Infeasible, match="cannot carry the whole day"):
        plates.resize(
            db,
            pid,
            wed,
            {"breakfast": "skip", "lunch": "less", "dinner": "skip"},
            config=config,
            clock=clock,
        )
    assert _levels(db, pid, wed) == {"breakfast": "skip", "lunch": "less", "dinner": "planned"}
    # the lone plate's own level asked in the same change: it could not carry the day as planned
    # either, so "it carries the day's calories as it is" would not be true
    thu = wed + dt.timedelta(days=1)
    with pytest.raises(Infeasible, match="cannot carry the whole day"):
        plates.resize(
            db,
            pid,
            thu,
            {"breakfast": "skip", "lunch": "less", "dinner": "skip"},
            config=config,
            clock=clock,
        )


def test_the_telegram_card_offers_no_size_row_on_the_only_free_plate(bot) -> None:  # noqa: F811
    """The bot's breakfast card on a boxed weekday has no Less, More or
    Skip."""
    _send, tap, db, pid, _r, _clock = bot
    card = tap(f"k:meal:{_menu_id(db, pid)}:breakfast:{TUE.isoformat()}")
    assert "Swap this dish" in _buttons(card[-1])
    assert "Less" not in _buttons(card[-1])


# ---------------------------------------------------------------- #46: the day the card shows
def test_a_card_from_a_menu_not_covering_today_acts_on_the_day_it_shows(bot) -> None:  # noqa: F811
    """A meal button with no day, on a menu that does not cover today, opens
    the menu's first day and its Swap and Less act on that day, under its date."""
    _send, tap, db, pid, _r, _clock = bot  # the clock is Monday 7 Sep: no menu today
    _unbox(db, pid)
    card = tap(f"k:meal:{_menu_id(db, pid)}:breakfast")
    assert card[-1]["text"].startswith("<b>Mon 14 Sep</b>")
    buttons = _buttons(card[-1])
    assert buttons["Swap this dish"] == f"k:swapmeal:{MON.isoformat()}:breakfast"
    assert buttons["Less"] == f"k:size:{MON.isoformat()}:breakfast:less"
    less = tap(buttons["Less"])
    assert less[-1]["text"].startswith("Breakfast less.")
    assert _levels(db, pid, MON)["breakfast"] == "less"


def test_a_card_with_no_day_shows_the_plate_in_force_on_the_day_it_shows(bot) -> None:  # noqa: F811
    """Second verification of #46: after Less from that card, the same button shows the
    resized breakfast in force on Mon 14, as the button dated Mon 14 does, not the menu's
    portion as planned under the same date."""
    _send, tap, db, pid, _r, _clock = bot  # Monday 7 Sep: no menu today
    _unbox(db, pid)
    base = _menu_id(db, pid)
    planned = tap(f"k:meal:{base}:breakfast")[-1]["text"]
    tap(_buttons(tap(f"k:meal:{base}:breakfast")[-1])["Less"])
    assert _menu_id(db, pid) != base, "Mon 14's plates in force are a one-day menu now"
    plain = tap(f"k:meal:{base}:breakfast")[-1]["text"]
    dated = tap(f"k:meal:{base}:breakfast:{MON:%Y%m%d}")[-1]["text"]
    assert plain.startswith("<b>Mon 14 Sep</b>")
    assert plain == dated, "the same day's card gives one answer"
    assert plain != planned, "the smaller breakfast, not the one as planned"


# ---------------------------------------------------------------- #73: Skip on the page's card
def test_skip_on_the_page_card_says_its_result_under_the_day(page) -> None:  # noqa: F811
    """A skipped plate has no card to say the result under, so Skip anchors
    it to the day's heading, and the result is cleared once a redraw has looked for it."""
    client, db, partners, _his, _r = page
    _unbox(db, partners)
    done = client.post(
        "/api/menu/plates",
        json={"day": TUE.isoformat(), "levels": {"breakfast": "skip"}},
        headers=_headers(client),
    )
    assert done.status_code == 200, done.text
    assert "breakfast" not in {s["slot"] for s in _day(client, TUE)["slots"]}, "no card for it"
    js = _js()
    row = _block(js, "sizeRow")
    assert 'level === "skip" ? `d-${day.day}`' in row
    menu = _block(js, "renderMenu")
    assert "toSay.anchor === head.id" in menu, "said under the day's heading"
    assert "menuSaid = null;" in menu[: menu.index("body.replaceChildren()")], "looked for once"
    assert "menuSaid.anchor === box.id" not in menu


# ---------------------------------------------------------------- #96: back to the whole menu
def test_back_to_the_menu_from_a_resized_day_is_the_whole_menu(bot) -> None:  # noqa: F811
    """After Less on a card, the card's Back to the menu and an old button to
    the one-day menu both open the whole menu, so Swap a dish and New menu act on the week."""
    _send, tap, db, pid, _r, _clock = bot
    _unbox(db, pid)
    base = _menu_id(db, pid)
    tap(_buttons(tap(f"k:meal:{base}:dinner:{TUE.isoformat()}")[-1])["Less"])
    one_day = _menu_id(db, pid)
    assert one_day != base, "the resized Tuesday is a one-day menu of its own"
    card = tap(f"k:meal:{base}:dinner:{TUE.isoformat()}")
    assert _buttons(card[-1])["Back to the menu"] == f"k:menuv:{base}"
    for menu_id in (base, one_day):
        shown = tap(f"k:menuv:{menu_id}")
        assert "Mon to Fri" in shown[-1]["text"]
        buttons = _buttons(shown[-1])
        assert buttons["Swap a dish"] == f"k:swappick:{base}"
        assert buttons["New menu"] == f"k:menux:{base}"


# ---------------------------------------------------------------- #112: the card's keyboard
def test_a_size_tap_keeps_the_meal_cards_buttons(bot, monkeypatch) -> None:  # noqa: F811
    """Less from the meal card leaves the card's own buttons (Back to the
    menu among them) where they are; only a used Undo is cleared."""
    _send, tap, db, pid, _r, _clock = bot
    _unbox(db, pid)
    cleared: list[int | None] = []
    monkeypatch.setattr(Handlers, "clear_buttons", lambda _self, i: cleared.append(i.message_id))
    card = tap(f"k:meal:{_menu_id(db, pid)}:dinner:{TUE.isoformat()}")
    less = tap(_buttons(card[-1])["Less"])
    assert cleared == [], "the card still has Back to the menu and the other meals"
    tap(_buttons(less[-1])["Undo"])
    assert len(cleared) == 1, "an Undo once used is taken off its message"


# ---------------------------------------------------------------- #122: a day cooked together
def test_the_page_card_says_the_other_plates_follow_before_the_tap() -> None:
    """On a day cooked together the card's size row says the other
    person's plates follow, before anything is tapped."""
    row = _block(_js(), "sizeRow")
    assert "day.plates.together" in row
    assert "'s plates follow" in row
