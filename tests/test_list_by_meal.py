"""Her shopping list from today to Friday, by food or by meal, with Skip on a meal taking its food
off the list.

shopping for Thursday whereas today is Wednesday and it should then show the shopping list from
Wednesday until Friday ... she proposed to generate the shopping list on the meal level, as well
as that ability to skip meals where you can literally say I skip this meal and it already removes
it from the shopping list". And Skip on her days just drops the meal: "her other meals stay
exactly as planned" (his answer, `/set skip drop`).
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from tiffin.clock import TZ
from tiffin.errors import NothingToBuild
from tiffin.food import plates, shopping
from tiffin.telegram import settings

from .test_swap import FRI, MON, PARTNER_LOGIN, _headers, _profile, bot, page  # noqa: F401

WED, THU = dt.date(2026, 9, 16), dt.date(2026, 9, 17)
DAILY = json.dumps([[0], [1], [2], [3], [4]])


def _partner_daily(db, partners, clock, *, mode: str = "drop") -> None:
    """Wednesday morning, her week cooked a day at a time, her Skip as asked for."""
    _profile(db, partners, effective_from="2026-09-01", segment_days=DAILY, skip_mode=mode)
    clock.set_local(dt.datetime(2026, 9, 16, 9, 0, tzinfo=TZ))


def _read(client) -> dict:
    return client.get("/api/shopping", headers={"Tailscale-User-Login": PARTNER_LOGIN}).json()


def _grams(data: dict, name: str) -> float:
    return sum(
        float(i["grams_needed"]) + float(i.get("credit_g") or 0)
        for i in data["items"]
        if i["name"].startswith(name)
    )


# ---------------------------------------------------------------- today to Friday
# Pinned the credit: now runs with `shopping.count_on_home` on, because the default buys everything.
@pytest.mark.usefixtures("count_on_home")
def test_partner_list_runs_from_today_to_friday(page) -> None:  # noqa: F811
    from tiffin.food import menu

    client, db, partners, _his, _r = page
    _partner_daily(db, partners, db.clock)
    # the week's own list exists too, as a day list under a range would
    shopping.build_once(db, int(menu.segment_menu_id(db, partners, MON) or 0), clock=db.clock)
    data = _read(client)
    assert data["list"]["kind"] == "range"
    assert (data["list"]["segment_start"], data["list"]["segment_end"]) == (
        "2026-09-16",
        "2026-09-18",
    ), "Wednesday to Friday - not Thursday alone, and not Monday's days already gone"
    assert _grams(data, "Chicken breast") == 3 * 180, "three dinners of chicken and rice"
    chicken = next(i for i in data["items"] if i["name"].startswith("Chicken breast"))
    assert chicken["credit_g"] == 140.0, "Monday's pack is used before another is bought"
    assert not [a for a in data["also"] if a["kind"] == "segment"], (
        "no link to a day list the range already buys for"
    )
    # and the same list in the bot, said with its own days
    row = shopping.list_to_show(db, partners, WED, clock=db.clock)
    assert row is not None and int(row["id"]) == int(data["list"]["id"]), "read again, not rebuilt"


def test_a_daily_cook_on_the_weekend_and_a_weekly_cook_keep_their_lists(page) -> None:  # noqa: F811
    client, db, partners, _his, _r = page
    db.clock.set_local(dt.datetime(2026, 9, 16, 9, 0, tzinfo=TZ))
    assert _read(client)["list"]["kind"] == "segment", "her Mon-Fri cook keeps its own list"
    assert shopping.range_days(db, partners, dt.date(2026, 9, 19)) == [], "the weekend has its own"


# ---------------------------------------------------------------- by meal, and Skip
def test_by_meal_each_meal_says_what_it_needs(page) -> None:  # noqa: F811
    client, db, partners, _his, _r = page
    _partner_daily(db, partners, db.clock)
    meals = _read(client)["meals"]
    assert [(m["day"], m["slot"]) for m in meals[:3]] == [
        ("2026-09-16", "breakfast"),
        ("2026-09-16", "lunch"),
        ("2026-09-16", "dinner"),
    ]
    assert len(meals) == 9, "three meals a day, Wednesday to Friday"
    dinner = meals[2]
    assert dinner["name"] == "Chicken and rice" and not dinner["skipped"]
    assert {"name": "Chicken breast", "grams": 180} in dinner["ingredients"]
    assert meals[0]["can_change"], "Wednesday's breakfast is still to come at 09:00"


def test_skipping_a_meal_takes_its_food_off_and_leaves_the_day_as_it_was(page) -> None:  # noqa: F811
    client, db, partners, _his, _r = page
    _partner_daily(db, partners, db.clock)
    h = _headers(client)
    before = _read(client)
    chicken = next(i for i in before["items"] if i["name"].startswith("Chicken"))
    client.post("/api/shopping/tick", json={"item_id": chicken["id"], "ticked": True}, headers=h)
    lunch_before = plates._in_force(db, partners, THU)["lunch"]
    got = client.post(
        "/api/shopping/skip", json={"day": "2026-09-17", "slot": "dinner", "skip": True}, headers=h
    )
    assert got.status_code == 200, got.text
    assert "Dinner skipped: Chicken and rice" in got.json()["said"]
    assert "under your" in got.json()["said"] and "nothing else moved" in got.json()["said"]
    assert plates._in_force(db, partners, THU)["lunch"] == lunch_before, "the rest as planned"
    after = _read(client)
    assert _grams(after, "Chicken breast") == 2 * 180, "Thursday's dinner is off the list"
    assert next(i for i in after["items"] if i["name"].startswith("Chicken"))["ticked"], (
        "what was in the trolley stays ticked on the list built again"
    )
    thu_dinner = next(
        m for m in after["meals"] if m["day"] == "2026-09-17" and m["slot"] == "dinner"
    )
    assert thu_dinner["skipped"] and thu_dinner["can_change"]
    back = client.post(
        "/api/shopping/skip", json={"day": "2026-09-17", "slot": "dinner", "skip": False}, headers=h
    )
    assert back.status_code == 200, back.text
    assert "Dinner is back" in back.json()["said"]
    assert _grams(_read(client), "Chicken breast") == 3 * 180


def test_skip_keeps_one_meal_and_never_a_meal_already_eaten(page) -> None:  # noqa: F811
    _client, db, partners, _his, _r = page
    _partner_daily(db, partners, db.clock)
    db.clock.set_local(dt.datetime(2026, 9, 16, 16, 0, tzinfo=TZ))  # lunch is behind her
    with pytest.raises(NothingToBuild, match="eaten already"):
        plates.drop(db, partners, WED, "lunch", config=None, clock=db.clock)
    plates.drop(db, partners, THU, "breakfast", config=None, clock=db.clock)
    plates.drop(db, partners, THU, "lunch", config=None, clock=db.clock)
    with pytest.raises(NothingToBuild, match="at least one meal"):
        plates.drop(db, partners, THU, "dinner", config=None, clock=db.clock)


def test_partner_one_tap_skip_on_a_meal_drops_it_too(page) -> None:  # noqa: F811
    """The meal card's Skip follows `/set skip` as the list's does: the rest stays as planned."""
    client, db, partners, _his, _r = page
    _partner_daily(db, partners, db.clock)
    lunch_before = plates._in_force(db, partners, THU)["lunch"]
    got = client.post(
        "/api/menu/plates",
        json={"day": "2026-09-17", "levels": {"dinner": "skip"}},
        headers=_headers(client),
    )
    assert got.status_code == 200, got.text
    assert "nothing else moved" in got.json()["said"]
    assert plates._in_force(db, partners, THU)["lunch"] == lunch_before


# ---------------------------------------------------------------- the setting
def test_skip_is_a_setting_of_its_own_never_asked_in_the_interview() -> None:
    from tiffin.onboarding import FIRST_STEPS, FOLLOW_UP_STEPS, STEP_BY_ID

    field = settings.BY_NAME["skip"]
    assert field.column == "skip_mode"
    step = STEP_BY_ID[field.step]
    assert step.parse("drop", {}) == "drop" and step.parse("share", {}) == "share"
    assert step.id not in FIRST_STEPS and step.id in FOLLOW_UP_STEPS


# ---------------------------------------------------------------- the bot says the days
def test_the_bot_says_the_days_of_partner_list(bot) -> None:  # noqa: F811
    send, _tap, db, pid, _r, clock = bot
    _profile(db, pid, effective_from="2026-09-01", segment_days=DAILY)
    clock.set_local(dt.datetime(2026, 9, 16, 9, 0, tzinfo=TZ))
    text = send("/shopping")[-1]["text"]
    assert text.startswith("<b>Shopping Wed 16 to Fri 18 Sep</b>"), text
