"""Streamlined and personal, from the evidence (docs/research-2026-10-04.md).

research says to judge that by, and what each story here holds Tiffin to:

- at most one prompt a person per day, answerable in the message - the 22:15 reminder is gone,
  and the morning message only speaks when the day's food is new (unless they want it daily);
- a missed day is not a lost day: one tap on the evening check-in answers the week, by name,
  and a silent day is never recorded as eaten to plan;
- targets that adapt even when few days are answered: weight alone, over four weeks, as a
  yes/no proposal and never applied unasked;
- taste learned from what people do: a favourite eaten all last week rests, a swap is a signal,
  and a dish cooked together must suit both.
"""

from __future__ import annotations

import datetime as dt
import json

from tiffin import days
from tiffin.energy import Macros
from tiffin.food import swap, taste
from tiffin.jobs import evening_confirm, morning_targets
from tiffin.web import queries

from .test_journey_owner_week import (  # noqa: F401 (week is a pytest fixture)
    MON14,
    SUN20,
    THU17,
    TUE15,
    WED16,
    taps,
    text_of,
    week,
)
from .test_swap import (  # noqa: F401 (fixtures)
    MON,
    PARTNER_LOGIN,
    _headers,
    bot,
    page,
)
from .test_swap import week as partner_week  # noqa: F401


def _confirmed(db, pid: int, day: dt.date) -> bool:
    row = days.latest_confirmation(db, pid, day)
    return row is not None and str(row["status"]) == "confirmed"


def _new_profile(db, pid: int, **changes) -> None:
    row = db.one(
        "SELECT * FROM profiles WHERE person_id = ? ORDER BY effective_from DESC, id DESC LIMIT 1",
        (pid,),
    )
    fields = {k: row[k] for k in row.keys() if k not in ("id", "created_at")}  # noqa: SIM118
    fields.update(changes)
    db.insert("profiles", **fields)


# ---------------------------------------------------------------- one tap answers the week
def test_the_evening_offers_the_days_still_open_and_one_tap_answers_them(week) -> None:  # noqa: F811
    """Wednesday 20:00, Monday and Tuesday never answered. The check-in names them and one tap
    records all three - the five usable days the week's calibration needs were never reached
    one evening at a time."""
    msg = week.run(evening_confirm.JOB, WED16)
    text = text_of(msg)
    assert "Also still open: Mon, Tue" in text, text
    assert "k:cfmall:20260916" in taps(msg)
    assert msg is not None
    buttons = {b["text"] for row in msg["reply_markup"]["inline_keyboard"] for b in row}
    assert "All 3 days as planned" in buttons and "As planned" in buttons
    done = week.tap("k:cfmall:20260916")
    assert "Recorded as planned: Mon, Tue, Wed" in text_of(done)
    for d in (MON14, TUE15, WED16):
        assert _confirmed(week.db, week.his, d), d
        row = days.latest_confirmation(week.db, week.his, d)
        assert row is not None and str(row["basis"]) == "menu" and row["kcal"]
    again = week.run(evening_confirm.JOB, THU17)
    assert "still open" not in text_of(again), "nothing left to catch up"


def test_a_silent_day_is_never_recorded_as_eaten(week) -> None:  # noqa: F811
    """The evidence is plain that a day nobody answered must not be read as eaten to plan: it
    stays open, offered back by name, and the calibration leaves it out."""
    week.run(evening_confirm.JOB, WED16)
    for d in (MON14, TUE15, WED16):
        assert not _confirmed(week.db, week.his, d)
    assert days.unanswered_days(week.db, week.his, THU17) == [MON14, TUE15, WED16]


def test_the_old_all_days_button_never_records_a_day_answered_since(week) -> None:  # noqa: F811
    """The days are found again at the tap, never carried in it: Tuesday answered "not quite"
    in between stays as it was answered."""
    week.run(evening_confirm.JOB, WED16)
    days.confirm_day(
        week.db, week.his, TUE15, basis="menu",
        macros=Macros(2500, 150, 80, 250, 30), note="pizza night",
    )  # fmt: skip
    week.tap("k:cfmall:20260916")
    row = days.latest_confirmation(week.db, week.his, TUE15)
    assert row is not None and row["note"] == "pizza night"
    assert _confirmed(week.db, week.his, MON14) and _confirmed(week.db, week.his, WED16)


# ---------------------------------------------------------------- one prompt a day at most
def test_light_rhythm_says_good_morning_only_when_the_food_is_new(week) -> None:  # noqa: F811
    """Tuesday to Friday of a batch week are Monday's boxes. The same two lines every morning are
    the prompts that stopped being read; light (the default) sends Monday's and holds the rest,
    recording a skipped run so the health report does not raise a false alarm."""
    assert week.run(morning_targets.JOB, MON14) is not None, "a new week's food is news"
    assert week.run(morning_targets.JOB, TUE15) is None, "Monday's food again: nothing to say"
    row = week.db.one(
        "SELECT status FROM run_ledger WHERE job = 'morning_targets' AND person_id = ? "
        "ORDER BY rowid DESC LIMIT 1",
        (week.his,),
    )
    assert row is not None and row["status"] == "skipped"
    _new_profile(week.db, week.his, rhythm="daily", effective_from="2026-09-15")
    assert week.run(morning_targets.JOB, WED16) is not None, "daily is every day, as asked"


def test_weekly_rhythm_is_one_sunday_check_in_for_the_week(week) -> None:  # noqa: F811
    _new_profile(week.db, week.his, rhythm="weekly")
    # Tuesday: Wednesday's boxes are in the fridge already. Wednesday evening still says which
    # frozen boxes to move for Thursday, and only that
    assert week.run(evening_confirm.JOB, TUE15) is None
    row = week.db.one(
        "SELECT status, detail FROM run_ledger WHERE job = 'evening_confirm' AND person_id = ? "
        "ORDER BY rowid DESC LIMIT 1",
        (week.his,),
    )
    assert row is not None and row["status"] == "skipped" and "weekly" in row["detail"]
    sunday = week.run(evening_confirm.JOB, SUN20)
    text = text_of(sunday)
    assert "Also still open: Mon, Tue, Wed, Thu, Fri" in text, text
    assert "k:cfmall:20260920" in taps(sunday)


def test_rhythm_is_a_setting_with_buttons(bot) -> None:  # noqa: F811
    send, _tap, db, pid, _r, _clock = bot
    shown = send("/set rhythm")[0]
    assert "How often should I message you?" in shown["text"]
    assert "weekly" in send("/set rhythm weekly")[0]["text"]
    assert (
        db.scalar(
            "SELECT rhythm FROM profiles WHERE person_id = ? ORDER BY id DESC LIMIT 1", (pid,)
        )
        == "weekly"
    )


# ---------------------------------------------------------------- taste learned from behaviour
def test_a_favourite_eaten_all_week_rests_and_comes_back(partner_week) -> None:  # noqa: F811
    """Her five-star salmon traybake, eaten Mon-Fri: the week after, it waits; a month on, it is
    the first thing offered again (Meiselman 2000: the same lunch five days running is eaten
    less; Galak 2011: spaced out, repetition is welcome)."""
    db, pid, _base, _r, _clock = partner_week
    soon = taste.tastes(db, pid, ["Salmon traybake"], dt.date(2026, 9, 1))["Salmon traybake"]
    later = taste.tastes(db, pid, ["Salmon traybake"], dt.date(2026, 10, 5))["Salmon traybake"]
    assert soon.resting and not later.resting
    assert later.score > soon.score
    assert later.liking > 0.6, "rated 5: the liking holds while the boredom fades"


def test_a_swap_away_says_less_when_it_was_only_boredom(partner_week) -> None:  # noqa: F811
    db, pid, _base, _r, _clock = partner_week
    on = dt.date(2026, 9, 14)
    before = taste.tastes(db, pid, ["Chickpea salad"], on)["Chickpea salad"].liking
    # swapped away the week after a fortnight of it: boredom, a light mark
    taste.record_swap(db, pid, old="Chickpea salad", new="Salmon traybake", new_id=None,
                      day=dt.date(2026, 9, 7))  # fmt: skip
    bored = taste.tastes(db, pid, ["Chickpea salad"], on)["Chickpea salad"].liking
    # and again, a month after anybody ate it: that one says she does not want it
    taste.record_swap(db, pid, old="Chickpea salad", new="Salmon traybake", new_id=None,
                      day=dt.date(2026, 10, 5))  # fmt: skip
    meant = taste.tastes(db, pid, ["Chickpea salad"], dt.date(2026, 10, 6))["Chickpea salad"]
    assert before > bored > meant.liking
    assert (before - bored) < (bored - meant.liking), "boredom marks lighter than a real no"


def test_a_dish_either_of_two_would_not_want_is_not_offered() -> None:
    keen = taste.Taste("Burger", liking=0.9, boredom=0.0, kind_boredom=0.0, evidence=3)
    cool = taste.Taste("Burger", liking=0.3, boredom=0.0, kind_boredom=0.0, evidence=3)
    fine = taste.Taste("Burger", liking=0.6, boredom=0.0, kind_boredom=0.0, evidence=3)
    assert taste.couple_score(keen, cool) is None, "her enthusiasm does not carry his plate"
    both = taste.couple_score(keen, fine)
    assert both is not None and 0.6 < both < 0.9, "geometric: between the two, nearer the lower"


def test_a_swap_is_remembered_as_taste(partner_week, config) -> None:  # noqa: F811
    db, pid, _base, r, clock = partner_week
    swap.swap(db, pid, MON, "lunch", r["salmon"], config=config, clock=clock)
    kinds = {
        (str(x["dish"]), str(x["kind"]))
        for x in db.all("SELECT dish, kind FROM dish_events WHERE person_id = ?", (pid,))
    }
    assert kinds == {("Lentil dal", "swapped_out"), ("Salmon traybake", "swapped_in")}


def test_the_menu_asks_for_favourites_that_are_not_resting(partner_week) -> None:  # noqa: F811
    db, pid, _base, _r, _clock = partner_week
    asked = taste.favourites_now(db, pid, ["Salmon traybake", "salmon"], dt.date(2026, 9, 1))
    assert asked == ["salmon"], "the dish rests a week after its batch; the liked food stays"
    asked = taste.favourites_now(db, pid, ["Salmon traybake", "salmon"], dt.date(2026, 10, 5))
    assert asked == ["Salmon traybake", "salmon"]
    assert "Salmon traybake" in taste.resting(db, pid, dt.date(2026, 9, 1))


# ---------------------------------------------------------------- swap a dish from the menu
def test_the_menu_message_leads_with_swap_a_dish(bot) -> None:  # noqa: F811
    send, tap, db, pid, _r, _clock = bot
    menu_id = int(
        db.scalar("SELECT id FROM menus WHERE person_id = ? ORDER BY id DESC LIMIT 1", (pid,))
    )
    pick = tap(f"k:swappick:{menu_id}")[-1]
    assert pick["text"] == "Which dish?"
    labels = [b["text"] for row in pick["reply_markup"]["inline_keyboard"] for b in row]
    assert labels[:3] == [
        "Breakfast: Porridge with yoghurt",
        "Lunch: Lentil dal",
        "Dinner: Chicken and rice",
    ]
    options = tap("k:swapmeal:2026-09-14:lunch")[-1]
    assert "Swap Lentil dal" in options["text"]
    assert len(send("/swap monday lunch")) == 1


# ---------------------------------------------------------------- the page
def test_the_page_answers_the_week_and_keeps_the_rhythm(page, frozen_clock) -> None:  # noqa: F811
    client, db, partners, _his, _r = page
    frozen_clock.set_local(dt.datetime(2026, 9, 16, 21, 0))  # Wednesday evening
    q = {"Tailscale-User-Login": PARTNER_LOGIN}
    today = client.get("/api/today", headers=q).json()
    assert today["open_days"] == ["2026-09-14", "2026-09-15"]
    assert today["profile"]["rhythm"] == "light"
    h = _headers(client)
    got = client.post("/api/today/confirm", json={"as_planned": True, "with_open": True}, headers=h)
    assert got.status_code == 200, got.text
    assert got.json()["also"] == ["2026-09-14", "2026-09-15"]
    for d in (dt.date(2026, 9, 14), dt.date(2026, 9, 15), dt.date(2026, 9, 16)):
        assert _confirmed(db, partners, d)
    assert client.post("/api/rhythm", json={"rhythm": "weekly"}, headers=h).json()["rhythm"] == (
        "weekly"
    )
    assert client.post("/api/rhythm", json={"rhythm": "hourly"}, headers=h).status_code == 400
    assert queries.profile(db, partners, dt.date(2026, 9, 16))["rhythm"] == "weekly"  # type: ignore[index]
    assert json.loads(
        str(db.scalar("SELECT payload_json FROM web_events WHERE kind = 'rhythm'"))
    ) == {"rhythm": "weekly"}
