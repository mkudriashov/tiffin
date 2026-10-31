"""Review fixes, group F3: shopping data and the export.

Taking back a "Shelf empty?" tap puts back what was there before it - the fridge credit and the
shop's own verdict - "every week" counts weeks, not the lists one week was rebuilt into, and the
export carries the rows about her that hang off her own: her voice notes as transcribed, her
lists' items and the decisions on her menus.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from tiffin.clock import isoday
from tiffin.food import products, shopping
from tiffin.jobs import shop_refresh

from .test_swap import (  # noqa: F401
    LAST_MON,
    MON,
    OWNER_LOGIN,
    PARTNER_LOGIN,
    _headers,
    _menu,
    _profile,
    page,
)


def _list(db, pid: int, menu_id: int) -> int:
    shopping.build(db, menu_id, retailers=("sainsburys",))
    return int(db.scalar("SELECT MAX(id) FROM shopping_lists WHERE person_id = ?", (pid,)))


def _item(db, list_id: int, code: str) -> int:
    return int(
        db.scalar(
            "SELECT si.id FROM shopping_items si JOIN ingredients i ON i.id = si.ingredient_id "
            "WHERE si.list_id = ? AND i.code = ?",
            (list_id, code),
        )
    )


def _base(db, pid: int) -> int:
    return int(db.scalar("SELECT MAX(id) FROM menus WHERE person_id = ?", (pid,)))


def _shown(client, list_id: int) -> dict[str, dict]:
    got = client.get(
        "/api/shopping", params={"list": list_id}, headers={"Tailscale-User-Login": PARTNER_LOGIN}
    )
    assert got.status_code == 200, got.text
    return {i["name"]: i for i in got.json()["items"]}


# ---------------------------------------------------------------- #64 the fridge credit
def test_undoing_shelf_empty_gives_the_fridge_credit_back(page) -> None:  # noqa: F811
    """A "Shelf empty?" tap taken back no longer cancels the food's at-home
    credit, so next week's list does not buy a pack that is already in the cupboard."""
    client, db, partners, _his, _r = page
    list_id = _list(db, partners, _base(db, partners))
    lentils = db.scalar("SELECT id FROM ingredients WHERE code = 'lentils'")
    db.insert("leftovers", person_id=partners, ingredient_id=lentils, grams=200.0)
    assert shopping.current_leftovers(db, partners)["lentils"] == 200.0
    h = _headers(client)
    item = _item(db, list_id, "lentils")
    assert client.post("/api/shopping/out", json={"item_id": item}, headers=h).json()["out"]
    assert "lentils" not in shopping.current_leftovers(db, partners), "the shelf was empty"
    back = client.post("/api/shopping/out", json={"item_id": item, "undo": True}, headers=h)
    assert back.status_code == 200 and back.json()["out"] is False
    assert shopping.current_leftovers(db, partners)["lentils"] == 200.0


def test_a_shelf_said_empty_on_telegram_still_cancels_the_credit(page) -> None:  # noqa: F811
    """Only the mark that was taken back stops counting; a person's own
    `/shopping out` before it still says the planned surplus was never bought."""
    client, db, partners, _his, _r = page
    list_id = _list(db, partners, _base(db, partners))
    lentils = db.scalar("SELECT id FROM ingredients WHERE code = 'lentils'")
    db.insert("leftovers", person_id=partners, ingredient_id=lentils, grams=200.0)
    db.insert(
        "shop_misses",
        ingredient_code="lentils",
        retailer="sainsburys",
        reason="the person said the shelf was empty",
        seen_at=db.clock.utc_stamp(),
    )
    h = _headers(client)
    item = _item(db, list_id, "lentils")
    client.post("/api/shopping/out", json={"item_id": item}, headers=h)
    client.post("/api/shopping/out", json={"item_id": item, "undo": True}, headers=h)
    assert "lentils" not in shopping.current_leftovers(db, partners)


# ---------------------------------------------------------------- #140 the shop's verdict
def _not_sold(db, code: str) -> None:
    """The shop's current reader read it and found the shop does not sell it, a day ago."""
    db.insert(
        "shop_misses",
        ingredient_code=code,
        retailer="sainsburys",
        reason="no broccoli on the page",
        seen_at=(db.clock.now_utc() - dt.timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S"),
        kind="unstocked",
        reader=products.READER_VERSION,
    )


def test_undoing_shelf_empty_keeps_the_shops_own_not_sold(page) -> None:  # noqa: F811
    """Taking back a "Shelf empty?" tap puts the shop's earlier "Not at
    Sainsbury's" back on the line and leaves the food parked, not asked about again."""
    client, db, partners, _his, _r = page
    list_id = _list(db, partners, _base(db, partners))
    _not_sold(db, "broccoli")
    before = _shown(client, list_id)["Broccoli, raw"]
    assert (before["shop_note"], before["shop_status"]) == ("Not at Sainsbury's", "not_sold")
    assert "broccoli" in shop_refresh._missed(db, "sainsburys")
    h = _headers(client)
    item = _item(db, list_id, "broccoli")
    assert client.post("/api/shopping/out", json={"item_id": item}, headers=h).json()["out"]
    assert _shown(client, list_id)["Broccoli, raw"]["shop_status"] == "sold_out"
    back = client.post("/api/shopping/out", json={"item_id": item, "undo": True}, headers=h)
    assert back.status_code == 200 and back.json()["out"] is False
    after = _shown(client, list_id)["Broccoli, raw"]
    assert (after["shop_note"], after["shop_status"]) == ("Not at Sainsbury's", "not_sold")
    assert "broccoli" in shop_refresh._missed(db, "sainsburys"), "still parked, not re-asked"


def test_undoing_a_shelf_never_marked_empty_is_refused(page) -> None:  # noqa: F811
    """An undo for a food nobody marked empty is a 404 and writes nothing,
    so it cannot overwrite what the shop said about it."""
    client, db, partners, _his, _r = page
    list_id = _list(db, partners, _base(db, partners))
    _not_sold(db, "broccoli")
    rows = db.scalar("SELECT COUNT(*) FROM shop_misses")
    h = _headers(client)
    item = _item(db, list_id, "broccoli")
    back = client.post("/api/shopping/out", json={"item_id": item, "undo": True}, headers=h)
    assert back.status_code == 404
    assert back.json()["detail"].startswith("Nothing to take back"), "said, not a bare 404"
    assert db.scalar("SELECT COUNT(*) FROM shop_misses") == rows
    assert "broccoli" in shop_refresh._missed(db, "sainsburys")


def test_an_undo_sent_twice_takes_back_once(page) -> None:  # noqa: F811
    """A retried undo is answered as done and writes no second row, and the
    food that had no verdict before the tap is asked about again."""
    client, db, partners, _his, _r = page
    list_id = _list(db, partners, _base(db, partners))
    h = _headers(client)
    item = _item(db, list_id, "pepper")
    client.post("/api/shopping/out", json={"item_id": item}, headers=h)
    first = client.post("/api/shopping/out", json={"item_id": item, "undo": True}, headers=h)
    rows = db.scalar("SELECT COUNT(*) FROM shop_misses")
    again = client.post("/api/shopping/out", json={"item_id": item, "undo": True}, headers=h)
    assert first.status_code == again.status_code == 200
    assert db.scalar("SELECT COUNT(*) FROM shop_misses") == rows
    assert "pepper" not in shop_refresh._missed(db, "sainsburys")
    assert _shown(client, list_id)["Red pepper, raw"]["shop_status"] == "unchecked"


def test_a_mark_sent_twice_is_taken_back_by_one_undo(page) -> None:  # noqa: F811
    client, db, partners, _his, _r = page
    list_id = _list(db, partners, _base(db, partners))
    lentils = db.scalar("SELECT id FROM ingredients WHERE code = 'lentils'")
    db.insert("leftovers", person_id=partners, ingredient_id=lentils, grams=200.0)
    _not_sold(db, "broccoli")
    h = _headers(client)
    for code in ("lentils", "broccoli"):
        item = _item(db, list_id, code)
        rows = db.scalar("SELECT COUNT(*) FROM shop_misses")
        for _ in range(2):  # the first answer never reached the phone, and Retry was tapped
            got = client.post("/api/shopping/out", json={"item_id": item}, headers=h)
            assert got.status_code == 200 and got.json()["out"]
        assert db.scalar("SELECT COUNT(*) FROM shop_misses") == rows + 1, "one mark"
        back = client.post("/api/shopping/out", json={"item_id": item, "undo": True}, headers=h)
        assert back.status_code == 200 and back.json()["out"] is False
    assert shopping.current_leftovers(db, partners)["lentils"] == 200.0
    after = _shown(client, list_id)["Broccoli, raw"]
    assert (after["shop_note"], after["shop_status"]) == ("Not at Sainsbury's", "not_sold")
    assert "broccoli" in shop_refresh._missed(db, "sainsburys")


def test_a_shelf_empty_again_on_another_day_is_a_new_mark(page, frozen_clock) -> None:  # noqa: F811
    """A tap the next day is not a retry: it says the shelf was empty again, and cancels a
    credit made since the first."""
    client, db, partners, _his, _r = page
    list_id = _list(db, partners, _base(db, partners))
    item = _item(db, list_id, "lentils")
    client.post("/api/shopping/out", json={"item_id": item}, headers=_headers(client))
    frozen_clock.advance(days=1)
    lentils = db.scalar("SELECT id FROM ingredients WHERE code = 'lentils'")
    db.insert("leftovers", person_id=partners, ingredient_id=lentils, grams=200.0)
    assert shopping.current_leftovers(db, partners)["lentils"] == 200.0
    got = client.post("/api/shopping/out", json={"item_id": item}, headers=_headers(client))
    assert got.status_code == 200 and got.json()["out"]
    assert "lentils" not in shopping.current_leftovers(db, partners)


def test_the_other_persons_shelf_empty_is_not_a_retry_of_partners(page) -> None:  # noqa: F811
    """A retry is the same person's tap sent again.
    the owner marking lentils on his own list after she marked them on hers is his own word, so his
    Undo takes back his mark only: hers still cancels her fridge credit and keeps the food
    parked, and her line still says the shelf was empty."""
    client, db, partners, his, r = page
    partner_list = _list(db, partners, _base(db, partners))
    _profile(db, his)
    his_list = _list(db, his, _menu(db, his, MON, 5, {"lunch": r["dal"]}))
    lentils = db.scalar("SELECT id FROM ingredients WHERE code = 'lentils'")
    db.insert("leftovers", person_id=partners, ingredient_id=lentils, grams=200.0)
    partners_out = {"item_id": _item(db, partner_list, "lentils")}
    assert client.post("/api/shopping/out", json=partners_out, headers=_headers(client)).json()[
        "out"
    ]
    rows = db.scalar("SELECT COUNT(*) FROM shop_misses")
    mh = _headers(client, OWNER_LOGIN)
    his_out = {"item_id": _item(db, his_list, "lentils")}
    assert client.post("/api/shopping/out", json=his_out, headers=mh).json()["out"]
    assert db.scalar("SELECT COUNT(*) FROM shop_misses") == rows + 1, "his own mark, written"
    back = client.post("/api/shopping/out", json={**his_out, "undo": True}, headers=mh)
    assert back.status_code == 200 and back.json()["out"] is False
    assert "lentils" not in shopping.current_leftovers(db, partners), "her word still stands"
    assert "lentils" in shop_refresh._missed(db, "sainsburys")
    assert _shown(client, partner_list)["Red lentils, dry"]["shop_status"] == "sold_out"


@pytest.mark.parametrize("first_back", ["partner", "his"])
def test_both_marks_taken_back_in_either_order_leave_none(page, first_back) -> None:  # noqa: F811
    """When both people mark a food and both take it
    back, neither Undo is mistaken for the other's sent again, and each takes back a mark: the
    fridge credit is back and the food is not parked on anyone's word."""
    client, db, partners, his, r = page
    partner_list = _list(db, partners, _base(db, partners))
    _profile(db, his)
    his_list = _list(db, his, _menu(db, his, MON, 5, {"lunch": r["dal"]}))
    lentils = db.scalar("SELECT id FROM ingredients WHERE code = 'lentils'")
    db.insert("leftovers", person_id=partners, ingredient_id=lentils, grams=200.0)
    taps = {
        "partner": ({"item_id": _item(db, partner_list, "lentils")}, _headers(client)),
        "his": ({"item_id": _item(db, his_list, "lentils")}, _headers(client, OWNER_LOGIN)),
    }
    for body, h in taps.values():
        assert client.post("/api/shopping/out", json=body, headers=h).json()["out"]
    for who in (first_back, "his" if first_back == "partner" else "partner"):
        body, h = taps[who]
        back = client.post("/api/shopping/out", json={**body, "undo": True}, headers=h)
        assert back.status_code == 200 and back.json()["out"] is False, who
    assert shopping.current_leftovers(db, partners).get("lentils") == 200.0
    assert "lentils" not in shop_refresh._missed(db, "sainsburys")


def test_an_undo_with_no_mark_of_ones_own_is_refused(page) -> None:  # noqa: F811
    """An Undo takes back the person's own mark. With
    only the owner's standing, hers is refused and his word still parks the food."""
    client, db, partners, his, r = page
    partner_list = _list(db, partners, _base(db, partners))
    _profile(db, his)
    his_list = _list(db, his, _menu(db, his, MON, 5, {"lunch": r["dal"]}))
    his_out = {"item_id": _item(db, his_list, "lentils")}
    mh = _headers(client, OWNER_LOGIN)
    assert client.post("/api/shopping/out", json=his_out, headers=mh).json()["out"]
    rows = db.scalar("SELECT COUNT(*) FROM shop_misses")
    partners_back = {"item_id": _item(db, partner_list, "lentils"), "undo": True}
    back = client.post("/api/shopping/out", json=partners_back, headers=_headers(client))
    assert back.status_code == 404
    assert db.scalar("SELECT COUNT(*) FROM shop_misses") == rows
    assert "lentils" in shop_refresh._missed(db, "sainsburys")


def test_a_shelf_empty_after_the_list_was_rebuilt_is_a_new_mark(page, frozen_clock) -> None:  # noqa: F811
    """A list rebuilt since the mark (a swap, a resize,
    "shop from") writes a new fridge credit, so a tap on the rebuilt list minutes later is a new
    word, not a retry, and cancels that credit too."""
    client, db, partners, _his, _r = page
    base = _base(db, partners)
    first = _list(db, partners, base)
    h = _headers(client)
    client.post("/api/shopping/out", json={"item_id": _item(db, first, "lentils")}, headers=h)
    assert "lentils" not in shopping.current_leftovers(db, partners)
    frozen_clock.advance(minutes=2)
    rebuilt = _list(db, partners, base)
    assert shopping.current_leftovers(db, partners)["lentils"] > 0, "the rebuilt list's surplus"
    frozen_clock.advance(minutes=1)
    item = _item(db, rebuilt, "lentils")
    got = client.post("/api/shopping/out", json={"item_id": item}, headers=h)
    assert got.status_code == 200 and got.json()["out"]
    assert "lentils" not in shopping.current_leftovers(db, partners)


def test_a_mark_retried_across_midnight_is_still_one_tap(page, frozen_clock) -> None:  # noqa: F811
    """A retry is judged by how long since the tap, not
    by the calendar day, so a mark retried twenty seconds later, after midnight, is still one
    mark and the one Undo gives the fridge credit back."""
    client, db, partners, _his, _r = page
    frozen_clock.set_local(dt.datetime(2026, 9, 12, 23, 59, 50))
    list_id = _list(db, partners, _base(db, partners))
    lentils = db.scalar("SELECT id FROM ingredients WHERE code = 'lentils'")
    db.insert("leftovers", person_id=partners, ingredient_id=lentils, grams=200.0)
    h = _headers(client)
    item = _item(db, list_id, "lentils")
    client.post("/api/shopping/out", json={"item_id": item}, headers=h)
    frozen_clock.advance(seconds=20)
    got = client.post("/api/shopping/out", json={"item_id": item}, headers=h)
    assert got.status_code == 200 and got.json()["out"]
    back = client.post("/api/shopping/out", json={"item_id": item, "undo": True}, headers=h)
    assert back.status_code == 200 and back.json()["out"] is False
    assert shopping.current_leftovers(db, partners).get("lentils") == 200.0


# ---------------------------------------------------------------- #67 regulars
def test_one_week_rebuilt_into_many_lists_is_not_every_week(page) -> None:  # noqa: F811
    """Five lists for the same week (a swap or a resize writes a new one each
    time) make no food a regular; it has been bought for one week only."""
    client, db, partners, _his, _r = page
    base = _base(db, partners)
    lists = [_list(db, partners, base) for _ in range(5)]
    assert not [i for i in _shown(client, lists[-1]).values() if "regular" in i]


def test_every_week_counts_weeks_and_the_last_list_of_each(page) -> None:  # noqa: F811
    """A food is "every week" when the latest list of each of the last four
    weeks carried it, however many lists one of those weeks was rebuilt into."""
    client, db, partners, _his, r = page
    shown = _base(db, partners)
    weeks = [LAST_MON + dt.timedelta(days=7 * n) for n in range(-1, 3)]
    porridge = r["porridge"]
    _list(
        db,
        partners,
        _menu(db, partners, weeks[0], 5, {"breakfast": porridge, "lunch": r["salmon"]}),
    )
    for start in weeks[1:3]:
        _list(
            db, partners, _menu(db, partners, start, 5, {"breakfast": porridge, "lunch": r["dal"]})
        )
    # the last week before this one was built twice with the chickpea salad, then swapped to
    # the dal: only the dal's list was the week's
    for _ in range(2):
        salad = _menu(db, partners, weeks[3], 5, {"breakfast": porridge, "lunch": r["chickpea"]})
        _list(db, partners, salad)
    _list(
        db, partners, _menu(db, partners, weeks[3], 5, {"breakfast": porridge, "lunch": r["dal"]})
    )
    items = _shown(client, _list(db, partners, shown))
    assert items["Porridge oats"]["regular"] == "every week"
    assert items["Red lentils, dry"]["regular"] == "3 of your last 4 weeks"
    assert "regular" not in items["Chicken breast, raw"], "only on lists its week replaced"


# ---------------------------------------------------------------- #80 the export
def test_the_export_carries_what_hangs_off_partner_own_rows(page) -> None:  # noqa: F811
    """Her export holds her transcribed voice notes, her lists' items and the
    decisions on her menus, so a menu a swap put in force reads as accepted."""
    client, db, partners, his, _r = page
    h = _headers(client)
    said = db.insert(
        "messages_in",
        update_id=901,
        person_id=partners,
        chat_id=11,
        kind="voice",
        raw_json="{}",
        received_at=db.clock.utc_stamp(),
    )
    db.insert(
        "voice_transcripts",
        message_in_id=said,
        file_id="f1",
        duration_s=4,
        text="I had a takeaway curry SECRETWORDS",
        model="test-model",
    )
    db.insert("inbound_outcomes", message_in_id=said, outcome="handled", detail=None)
    theirs = db.insert(
        "messages_in",
        update_id=902,
        person_id=his,
        chat_id=22,
        kind="voice",
        raw_json="{}",
        received_at=db.clock.utc_stamp(),
    )
    db.insert(
        "voice_transcripts",
        message_in_id=theirs,
        file_id="f2",
        duration_s=3,
        text="his words HISWORDS",
        model="test-model",
    )
    offered = client.get(
        "/api/menu/swap",
        params={"day": isoday(MON), "slot": "lunch"},
        headers={"Tailscale-User-Login": PARTNER_LOGIN},
    ).json()
    pick = offered["candidates"][0]["recipe_id"]
    swapped = client.post(
        "/api/menu/swap",
        json={"day": isoday(MON), "slot": "lunch", "recipe_id": pick},
        headers=h,
    )
    assert swapped.status_code == 200, swapped.text
    in_force = _base(db, partners)
    _list(db, partners, in_force)
    got = client.get("/api/export", headers={"Tailscale-User-Login": PARTNER_LOGIN})
    assert got.status_code == 200
    assert "SECRETWORDS" in got.text and "HISWORDS" not in got.text
    tables = json.loads(got.content)["tables"]
    assert [o["outcome"] for o in tables["inbound_outcomes"]] == ["handled"]
    lists = {int(r["id"]) for r in tables["shopping_lists"]}
    assert (
        tables["shopping_items"] and {int(i["list_id"]) for i in tables["shopping_items"]} <= lists
    )
    decided = {int(d["menu_id"]): d["decision"] for d in tables["menu_decisions"]}
    assert decided[in_force] == "accepted"
    # her messages say which chat they came in on, as the export's own comment now says; the
    # table binding that chat to her stays out
    assert [m["chat_id"] for m in tables["messages_in"]] == [11]
    assert "telegram_identities" not in tables
