"""A shopping list as plain text, to forward to whoever does the shop."""

from __future__ import annotations

import datetime as dt

from tiffin.food import menu, shopping
from tiffin.jobs import menu_propose, shopping_list

from .test_menu import MONDAY, FakeGateway, proposal_for, world  # noqa: F401
from .test_week_plan import page  # noqa: F401


def _list(world):  # noqa: F811
    """Tests/test_menu.py's week for the owner, its list built as Saturday's job builds it."""
    pid, db, _tg, gateways, _say, _tap, run_job = world
    segment = menu.Segment(MONDAY, dt.date(2026, 9, 18))
    gateways.append(FakeGateway([proposal_for(segment)]))
    run_job(menu_propose.JOB, dt.date(2026, 9, 12))
    assert run_job(shopping_list.JOB, dt.date(2026, 9, 13)) == "ok"
    return int(
        db.scalar("SELECT id FROM shopping_lists WHERE person_id = ? ORDER BY id DESC", (pid,))
    )


def test_the_text_is_every_line_to_buy_by_aisle_with_no_links(world) -> None:  # noqa: F811
    _pid, db, *_ = world
    list_id = _list(world)
    text = shopping.forward_text(db, list_id, heading=shopping.list_heading(db, list_id))
    lines = text.splitlines()
    assert lines[0] == "<b>Shopping, Mon 14 to Fri 18 Sep</b>"
    assert "<b>Meat</b>" in lines and any(ln.startswith("- ") and "hicken" in ln for ln in lines)
    assert "href" not in text and "?tab=" not in text
    to_buy = [i for i in shopping.read_back(db, list_id) if i.packs > 0]
    assert sum(ln.startswith("- ") for ln in lines) == len(to_buy)


def test_what_is_ticked_is_left_off(world) -> None:  # noqa: F811
    pid, db, *_ = world
    list_id = _list(world)
    first = shopping.read_back(db, list_id)[0]
    db.insert("shopping_ticks", item_id=first.item_id, person_id=pid, ticked=1)
    text = shopping.forward_text(db, list_id)
    assert shopping.shelf_text(first) not in text


def test_what_they_have_at_home_is_left_off(world) -> None:  # noqa: F811
    from tiffin.food import packs

    pid, db, *_ = world
    list_id = _list(world)
    first = shopping.read_back(db, list_id)[0]
    a, b = shopping.list_days(db, list_id) or (MONDAY, MONDAY)
    packs.mark_have(db, pid, first.code, a, b, True)
    assert shopping.shelf_text(first) not in shopping.forward_text(db, list_id)


def test_shopping_text_sends_it_to_forward(world) -> None:  # noqa: F811
    _pid, _db, tg, _gw, say, _tap, _run = world
    _list(world)
    tg.sent.clear()
    text = say("/shopping text")
    assert text.startswith("<b>Shopping, Mon 14 to Fri 18 Sep</b>") and "- " in text
    assert not tg.sent[-1].get("reply_markup")  # nothing to tap: it is for forwarding


def test_a_long_list_goes_as_several_messages_cut_at_lines() -> None:
    text = "\n".join(f"- item {n} " + "x" * 80 for n in range(100))
    parts = shopping.message_parts(text)
    assert len(parts) > 1 and all(len(p) <= shopping.MESSAGE_CHARS for p in parts)
    assert "\n".join(parts) == text


def test_the_page_sends_only_a_list_of_theirs(page) -> None:  # noqa: F811
    c, headers, _owner_id, _db = page
    shown = c.get("api/shopping", headers=headers).json()
    list_id = shown["list"]["id"]
    got = c.post("api/shopping/send", json={"list_id": list_id}, headers=headers)
    assert got.status_code == 200 and got.json()["sent"] >= 1
    assert c.post("api/shopping/send", json={"list_id": 999999}, headers=headers).status_code == 404
    assert c.post("api/shopping/send", json={}, headers=headers).status_code == 400
