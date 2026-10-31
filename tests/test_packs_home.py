"""What is at home, what keeps, and what each person wants:

1. A pack opened earlier in the shop window covers a later day, and "I have it" removes a line;
2. two meats or fish a week is fine, "but also ask her";
3. "assume 2 days open before freezer";
4. the shared weekend steered by packs.

The world is tests/test_swap.py's, cooked each day: chicken on Monday, Wednesday and Thursday.
"""

from __future__ import annotations

import datetime as dt
import json

from tiffin.clock import isoday
from tiffin.food import menu, packs, shopping
from tiffin.model import prompts
from tiffin.telegram import packflow

from .test_swap import (  # noqa: F401
    FRI,
    MON,
    PARTNER_LOGIN,
    _headers,
    _menu,
    _profile,
    page,
    week,
)

DAILY = json.dumps([[0], [1], [2], [3], [4]])
TUE, WED, THU = (MON + dt.timedelta(days=n) for n in (1, 2, 3))
SAT = MON + dt.timedelta(days=5)


def _daily(db, pid, r) -> None:
    _profile(db, pid, segment_days=DAILY, effective_from="2026-08-02")
    for day, dinner in (
        (MON, "chicken_rice"),
        (TUE, "dal"),
        (WED, "chicken_rice"),
        (THU, "chicken_rice"),
        (FRI, "dal"),
    ):
        _menu(db, pid, day, 1, {"breakfast": r["porridge"], "dinner": r[dinner]})


# -------------------------------------------------------------- 3. two days open, then the freezer
def test_an_opened_pack_keeps_two_days_and_a_later_day_cooks_it_from_the_freezer(week) -> None:  # noqa: F811
    db, pid, _base, r, _clock = week
    _daily(db, pid, r)
    chicken = next(
        p for p in packs.window_packs(db, pid, menu.Segment(MON, FRI).days) if p.code == "chicken"
    )
    # opened Monday: the fridge has it Tuesday and Wednesday, Thursday's comes from the freezer
    assert chicken.fresh_until == WED and chicken.frozen_days == [THU]
    assert packs.line(chicken) == (
        "Chicken breast 1 kg: Mon, Wed, then Thu from the freezer; 460 g left"
    )
    # said on the evening it has to happen, and only then
    assert packs.freeze_tonight(db, pid, WED) == [
        "Freeze the Chicken breast for Thu tonight: about 140 g."
    ]
    assert packs.freeze_tonight(db, pid, TUE) == []
    assert packs.freeze_tonight(db, pid, THU) == []


def test_a_build_is_told_when_its_open_pack_is_in_the_freezer(week) -> None:  # noqa: F811
    db, pid, _base, r, _clock = week
    _daily(db, pid, r)
    opened = [p for p in packs.open_for(db, pid, [FRI]) if p.code == "chicken"]
    said = packs.prompt_lines(opened, {"chicken": 3})
    assert said == [
        "3. Chicken breast, raw: 460 g left of a 500 g pack (opened for Mon, Wed, Thu; in the "
        "freezer after Wed)"
    ]


# ---------------------------------------------------------------- 1. "I have it"
def test_a_days_list_uses_an_earlier_pack_and_partner_can_mark_a_line_at_home(page) -> None:  # noqa: F811
    client, db, partners, _his, r = page
    _daily(db, partners, r)
    # Wednesday's own list: Monday's pack of chicken has 320 g left, and Wednesday needs 180 g
    wed_menu = int(
        db.scalar(
            "SELECT id FROM menus WHERE person_id = ? AND segment_start = ? ORDER BY id DESC",
            (partners, isoday(WED)),
        )
    )
    shopping.build(db, wed_menu)
    list_id = int(db.scalar("SELECT MAX(id) FROM shopping_lists"))
    h = {"Tailscale-User-Login": PARTNER_LOGIN}
    got = client.get(f"/api/shopping?list={list_id}", headers=h).json()
    assert not any(i["name"].startswith("Chicken breast") for i in got["items"])
    assert any(i["name"].startswith("Chicken breast") for i in got["at_home"])
    # Thursday needs more than the 140 g left after Wednesday, so it has a line to mark.
    thu_menu = int(
        db.scalar(
            "SELECT id FROM menus WHERE person_id = ? AND segment_start = ? ORDER BY id DESC",
            (partners, isoday(THU)),
        )
    )
    shopping.build(db, thu_menu)
    list_id = int(db.scalar("SELECT MAX(id) FROM shopping_lists"))
    got = client.get(f"/api/shopping?list={list_id}", headers=h).json()
    chicken = next(i for i in got["items"] if i["name"].startswith("Chicken breast"))
    assert chicken["credit_g"] == 140.0 and chicken["packs"] == 1
    assert chicken["have"] is False
    w = _headers(client)
    done = client.post(
        "/api/shopping/have", json={"item_id": chicken["id"], "have": True}, headers=w
    )
    assert done.status_code == 200, done.text
    assert done.json()["said"] == "Chicken breast: at home, so this list does not buy it."
    again = client.get(f"/api/shopping?list={list_id}", headers=h).json()
    assert next(i for i in again["items"] if i["id"] == chicken["id"])["have"] is True
    # a list built again for the same days keeps her answer: it is about the food and the days
    shopping.build(db, thu_menu)
    rebuilt = int(db.scalar("SELECT MAX(id) FROM shopping_lists"))
    assert rebuilt != list_id
    fresh = client.get(f"/api/shopping?list={rebuilt}", headers=h).json()
    assert next(i for i in fresh["items"] if i["name"].startswith("Chicken"))["have"] is True
    # and taken back with one tap
    item = next(i for i in fresh["items"] if i["name"].startswith("Chicken"))
    back = client.post("/api/shopping/have", json={"item_id": item["id"], "have": False}, headers=w)
    assert back.json()["said"] == "Chicken breast: back on the list."
    # nobody else's list: the owner cannot say what is in her cupboard
    his = _headers(client, "alex@example.com")
    refused = client.post(
        "/api/shopping/have", json={"item_id": item["id"], "have": True}, headers=his
    )
    assert refused.status_code == 404


def test_the_first_day_of_a_window_is_never_told_a_pack_covers_it(week) -> None:  # noqa: F811
    db, pid, _base, r, _clock = week
    _daily(db, pid, r)
    assert packs.covered(db, pid, MON, {"chicken": 180.0}) == {}
    # Thursday's chicken: Monday's pack has 140 g left, frozen on Wednesday evening - enough for
    # a smaller plate, not for 180 g
    got = packs.covered(db, pid, THU, {"chicken": 120.0})
    assert got == {"chicken": "Mon's pack should cover this: 140 g left, in the freezer since Wed"}
    assert packs.covered(db, pid, THU, {"chicken": 180.0}) == {}


def test_a_pack_short_of_the_days_need_is_not_said_to_cover_it(week) -> None:  # noqa: F811
    db, pid, _base, r, _clock = week
    _daily(db, pid, r)
    assert packs.covered(db, pid, WED, {"chicken": 400.0}) == {}


# ---------------------------------------------------------------- 2. asked, and theirs
def test_partner_answer_is_asked_on_the_page_kept_and_read_by_every_build(page) -> None:  # noqa: F811
    client, db, partners, _his, _r = page
    h = {"Tailscale-User-Login": PARTNER_LOGIN}
    view = client.get(f"/api/menu?segment={isoday(MON)}", headers=h).json()
    assert view["mains"] is None and "Does that suit you?" in view["mains_question"]
    assert packs.mains_budget(db, partners) == 2
    got = client.post("/api/packs/mains", json={"mains": 3}, headers=_headers(client))
    assert got.json()["said"].startswith("Kept: up to 3 meats or fish a week")
    assert packs.mains_budget(db, partners) == 3
    assert client.get(f"/api/menu?segment={isoday(MON)}", headers=h).json()["mains"] == 3
    # never anything but two, three or no limit
    bad = client.post("/api/packs/mains", json={"mains": 9}, headers=_headers(client))
    assert bad.status_code == 400
    no_limit = client.post("/api/packs/mains", json={"mains": 0}, headers=_headers(client))
    assert no_limit.json()["said"].startswith("Kept: no limit")
    assert packs.mains_budget(db, partners) == 0


def test_the_bot_asks_with_three_buttons_and_says_back_the_answer() -> None:
    assert [[label for label, _data in row] for row in packflow.buttons()] == [
        ["Two is fine", "Three", "No limit"]
    ]
    assert packflow.buttons()[0][1][1] == "k:mains:3"
    assert packflow.now_said(None).endswith("You have not said yet, so it is two.")
    assert packflow.now_said(0) == "Your menus have no limit on meats or fish a week."


# ---------------------------------------------------------------- 4. the weekend
def test_the_weekend_is_told_the_chicken_thursday_opened(week) -> None:  # noqa: F811
    db, pid, _base, r, _clock = week
    _daily(db, pid, r)
    # Friday is the dal: Thursday's pack is the one the fridge still has on Saturday
    _menu(db, pid, THU, 1, {"breakfast": r["porridge"], "dinner": r["salmon"]})
    _menu(db, pid, FRI, 1, {"breakfast": r["porridge"], "dinner": r["salmon"]})
    carried = {p.code: round(p.left_g) for p in packs.carried(db, pid, SAT)}
    # salmon opened Thursday: 140 g Thursday, 140 g Friday, 220 g left and still fresh Saturday
    assert carried == {"salmon": 220}
    # Monday's chicken was frozen on Wednesday: not "open" for the weekend's fridge
    assert "chicken" not in carried


def test_the_shared_weekend_prompt_carries_the_packs_and_the_budget() -> None:
    said = prompts.menu_shared_weekend(
        ingredients=["1. Salmon fillets, raw"],
        days=[{"day": "2026-09-19", "slots": "breakfast, dinner"}],
        packs=["1. Salmon fillets, raw: 220 g left of a 500 g pack (opened for Thu, Fri)"],
        mains="Keep the window's lunches and dinners to at most 2 main proteins",
    )
    assert "Open packs from the week, by number - cook from these first: 1. Salmon" in said
    assert "Main proteins: Keep the window's lunches and dinners to at most 2" in said
    assert "2026-09-19: breakfast, dinner" in said


def test_i_have_it_holds_on_a_range_list_too(week) -> None:  # noqa: F811
    """Found in the preview on live data: a range list ("from Wednesday until Friday") has no
    menu of its own, and the tap failed on it."""
    from tiffin.web import queries

    db, pid, _base, r, clock = week
    _daily(db, pid, r)
    row = shopping.range_list(db, pid, WED, clock=clock)
    assert row is not None
    item = db.one(
        "SELECT si.id FROM shopping_items si JOIN ingredients i ON i.id = si.ingredient_id "
        "WHERE si.list_id = ? AND i.code = 'chicken'",
        (int(row["id"]),),
    )
    assert queries.list_span(db, pid, int(row["id"])) == (WED, FRI)
    assert queries.list_span(db, pid + 100, int(row["id"])) is None
    assert queries.mark_have(db, pid, int(item["id"]), True) == "Chicken breast"
    assert packs.have_codes(db, pid, WED, FRI) == {"chicken"}


def test_cured_bacon_keeps_a_week_open_and_is_never_sent_to_the_freezer() -> None:
    """Seen on live data: the partner's breakfast bacon read "then Thu, Fri from the freezer"."""
    bacon = packs.Pack("bacon", "Back bacon, fat trimmed, raw", "meat", 240.0, "8 rashers")
    for d in (MON, TUE, THU, FRI):
        bacon.by_day[isoday(d)] = 30.0
        bacon.uses.append((isoday(d), isoday(d), 30.0))
    assert bacon.frozen_days == [] and packs.open_days(bacon.name) == 7
    thigh = packs.Pack("thigh", "Chicken thigh fillets", "meat", 500.0, "500 g")
    for d in (MON, THU):
        thigh.by_day[isoday(d)] = 150.0
        thigh.uses.append((isoday(d), isoday(d), 150.0))
    assert thigh.frozen_days == [THU]


# ---------------------------------------------------------------- edges
def test_no_limit_refuses_nothing_and_still_offers_the_open_packs() -> None:
    chicken = packs.Pack("chicken", "Chicken breast, raw", "meat", 500.0, "500 g")
    chicken.by_day = {isoday(MON): 180.0}
    chicken.slots = {"dinner"}
    uses = {f"{isoday(WED)}|dinner": {("salmon", "Salmon fillets, raw", "fish", "Salmon tray")}}
    free = packs.Steer(
        open=(chicken,),
        planned=frozenset({"chicken"}),
        names={"chicken": "Chicken breast, raw"},
        budget=0,
    )
    assert free.problems(uses, {"chicken": 1}) == []
    lines, mains = free.prompt({"chicken": 1})
    assert lines and lines[0].startswith("1. Chicken breast, raw: 320 g left")
    assert mains.startswith("the lunches and dinners of this shop already cook Chicken")
    assert "at most" not in mains


def test_a_joint_weekend_list_is_never_one_persons_cupboard(page) -> None:  # noqa: F811
    from tiffin.web import queries

    _client, db, partners, his, _r = page
    lid = db.insert("shopping_lists", kind="joint_weekend", person_id=None, menu_id=None)
    iid = db.insert(
        "shopping_items",
        list_id=lid,
        ingredient_id=int(db.scalar("SELECT id FROM ingredients WHERE code = 'chicken'")),
        grams_needed=300.0,
        packs=1,
        aisle="meat",
    )
    assert queries.mark_have(db, partners, iid, True) is None
    assert queries.mark_have(db, his, iid, True) is None


def test_an_answer_the_bot_never_offered_is_ignored(week) -> None:  # noqa: F811
    db, pid, _base, _r, _clock = week

    class H:
        def __init__(self) -> None:
            self.db = db

    class In:
        class person:
            id = pid

    flow = packflow.PackFlow()
    assert flow.tap(H(), In(), ["9"]) == "ignored"  # type: ignore[arg-type]
    assert flow.tap(H(), In(), ["x"]) == "ignored"  # type: ignore[arg-type]
    assert flow.tap(H(), In(), []) == "ignored"  # type: ignore[arg-type]
    assert packs.mains_answer(db, pid) is None


def test_a_food_refused_since_its_pack_was_opened_is_not_offered_back(week) -> None:  # noqa: F811
    db, pid, _base, r, _clock = week
    _daily(db, pid, r)
    steer = packs.steer_for(db, pid, [FRI], allowed={"rice", "oats"})
    assert [p.code for p in steer.open] == []
    # and its name stays out of the prompt: nothing from the avoid list reaches the model
    assert steer.prompt({"rice": 1})[0] == []


def test_a_day_with_no_menu_is_a_gap_not_an_error(week) -> None:  # noqa: F811
    db, pid, _base, r, _clock = week
    _profile(db, pid, segment_days=DAILY, effective_from="2026-08-02")
    _menu(db, pid, MON, 1, {"breakfast": r["porridge"], "dinner": r["chicken_rice"]})
    # the old batch still covers Tue to Fri; a window of plates is read whatever covers each day
    got = {p.code for p in packs.window_packs(db, pid, [MON, TUE])}
    assert "chicken" in got
    assert packs.covered(db, pid, MON, {}) == {}
    assert packs.freeze_tonight(db, pid, SAT) == []
