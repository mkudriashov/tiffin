"""One plate smaller, bigger or left out, from wherever the plate is: two taps on the Telegram
meal card, one on the page's card, and a preview before Apply on the page's sliders.
"""

from __future__ import annotations

import datetime as dt
import json

from tiffin.food import plates, swap

from .test_swap import MON, _buttons, _headers, bot, page, week  # noqa: F401

TUE = MON + dt.timedelta(days=1)


def _unbox(db, pid: int) -> None:
    """Her lunch and dinner are not batch-cooked from here on: every plate can move."""
    row = db.one(
        "SELECT * FROM profiles WHERE person_id = ? ORDER BY effective_from DESC, id DESC LIMIT 1",
        (pid,),
    )
    fields = {k: row[k] for k in row.keys() if k not in ("id", "created_at")}  # noqa: SIM118
    fields["batch_slots"] = json.dumps([])
    db.insert("profiles", **fields)


def _levels(db, pid: int) -> dict[str, str]:
    st = plates.state(db, pid, TUE)
    assert st is not None
    return dict(st["levels"])


def test_a_preview_says_what_apply_would_do_and_stores_nothing(week, config) -> None:  # noqa: F811
    db, pid, _base, _r, clock = week
    _unbox(db, pid)
    menus = db.scalar("SELECT COUNT(*) FROM menus")
    before = {p.slot: p.macros.kcal for p in swap.plates_on(db, pid, TUE)}
    got = plates.resize(db, pid, TUE, {"dinner": "less"}, config=config, clock=clock, dry_run=True)
    assert db.scalar("SELECT COUNT(*) FROM menus") == menus, "a preview writes nothing"
    assert {p.slot: p.macros.kcal for p in swap.plates_on(db, pid, TUE)} == before
    sizes = {s: (a, b) for s, a, b in got.plates}
    assert sizes["dinner"][0] == before["dinner"] and sizes["dinner"][1] < before["dinner"]
    assert sum(b for _a, b in sizes.values()) == got.macros.kcal, "the plates add up to the day"
    said = got.preview_said()
    assert f"to {sizes['dinner'][1]:,}" in said and said.split(" kcal.")[0].count(" to ") >= 1
    assert "The day:" in said


def test_the_page_previews_then_applies(page) -> None:  # noqa: F811
    client, db, partners, _his, _r = page
    _unbox(db, partners)
    h = _headers(client)
    body = {"day": TUE.isoformat(), "levels": {"dinner": "less"}}
    seen = client.post("/api/menu/plates", json={**body, "preview": True}, headers=h)
    assert seen.status_code == 200, seen.text
    got = seen.json()
    assert got["preview"] is True and got["said"].startswith(("Breakfast", "Lunch", "Dinner"))
    assert {p["slot"] for p in got["plates"]} >= {"breakfast", "lunch", "dinner"}
    assert db.scalar("SELECT COUNT(*) FROM web_events WHERE kind = 'plates_resized'") == 0
    done = client.post("/api/menu/plates", json=body, headers=h)
    assert done.status_code == 200
    assert _levels(db, partners)["dinner"] == "less"
    bad = client.post("/api/menu/plates", json={**body, "preview": "yes"}, headers=h)
    assert bad.status_code == 400


def test_less_and_skip_from_the_telegram_meal_card(bot) -> None:  # noqa: F811
    _send, tap, db, pid, _r, _clock = bot
    _unbox(db, pid)
    menu_id = db.scalar("SELECT id FROM menus WHERE person_id = ? ORDER BY id DESC LIMIT 1", (pid,))
    card = tap(f"k:meal:{menu_id}:dinner:{TUE.isoformat()}")
    buttons = _buttons(card[-1])
    assert {"Less", "More", "Skip"} <= set(buttons)
    less = tap(buttons["Less"])
    assert less[-1]["text"].startswith("Dinner less.")
    assert "kcal" in less[-1]["text"]
    assert _levels(db, pid)["dinner"] == "less"
    undo = tap(_buttons(less[-1])["Undo"])
    assert undo[-1]["text"].startswith("Dinner as planned.")
    assert _levels(db, pid)["dinner"] == "planned"


def test_a_box_cooked_for_the_week_has_no_size_buttons(bot) -> None:  # noqa: F811
    _send, tap, db, pid, _r, _clock = bot
    menu_id = db.scalar("SELECT id FROM menus WHERE person_id = ? ORDER BY id DESC LIMIT 1", (pid,))
    card = tap(f"k:meal:{menu_id}:lunch:{TUE.isoformat()}")
    assert "Less" not in _buttons(card[-1]), "it is eaten as it was cooked"
