"""Page tests for navigation, daily plans and shopping actions."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from tiffin.clock import TZ

from .test_swap import OWNER_LOGIN, PARTNER_LOGIN, _headers, page  # noqa: F401
from .web_source import app_source

STATIC = Path(__file__).resolve().parents[1] / "src" / "tiffin" / "web" / "static"


def _js(name: str = "app.js") -> str:
    if name == "app.js":
        return app_source()
    return (STATIC / name).read_text(encoding="utf-8")


def _block(js: str, name: str) -> str:
    start = js.index(f"function {name}(")
    return js[start : js.index("\n}\n", start)]


def _wellness(db, person: int, day: str, **values) -> None:
    db.insert("icu_wellness", person_id=person, day=day, icu_raw_id=1, **values)


# ---------------------------------------------------------------- the bar and its addresses
def test_five_destinations_and_every_old_address_still_lands() -> None:
    shell = _js("index.html")
    tabs = [
        t for t in ("today", "menu", "shopping", "progress", "more") if f'data-tab="{t}"' in shell
    ]
    assert tabs == ["today", "menu", "shopping", "progress", "more"]
    for view in ("menu", "batch", "dishes"):
        assert f'data-view="{view}"' in shell, f"Menu's {view} view has no button"
    js = _js()
    # the bot's links name tabs the bar no longer has: each opens the place that took it over
    assert 'const ALIASES = { week: "progress", trends: "progress", summary: "progress" };' in js
    show = _block(js, "show")
    assert 'if (named === "summary") $("#progress-sunday").open = true;' in show
    assert 'named === "trends"' in show, "a Trends link opens the chart it named"


def test_menu_lights_one_button_for_its_three_views() -> None:
    js = _js()
    assert 'batch: "menu", dishes: "menu"' in js.replace("  ", " ")
    show = _block(js, "show")
    assert 'views.hidden = lit !== "menu"' in show


# ---------------------------------------------------------------- Today
def test_today_leads_with_the_plan_and_never_prints_it_as_eaten(page) -> None:  # noqa: F811
    client, db, _partners, _his, _r = page
    db.clock.set_local(dt.datetime(2026, 9, 16, 12, 30, tzinfo=TZ))
    today = client.get("/api/today", headers={"Tailscale-User-Login": PARTNER_LOGIN}).json()
    assert today["meals"], "her Wednesday has a menu"
    render = _block(_js(), "renderToday")
    # a day closed unanswered holds the plan's number: only a confirmed record is "recorded"
    assert 'rec.status === "confirmed" && rec.kcal !== null ? rec.kcal : null' in render
    assert 'card("Planned"' in render and "not answered" in render
    # the meal it is time for, first, with the tap that cooks it
    assert 'class: "next-meal"' in render and '"Cook it"' in render
    assert "rhythmPanel" not in render, "the bot's rhythm is a setting, under More"


def test_a_watch_silent_for_days_is_said_with_the_day_it_stopped(page) -> None:  # noqa: F811
    client, db, _partners, his, _r = page
    db.clock.set_local(dt.datetime(2026, 9, 16, 12, 30, tzinfo=TZ))
    for day in range(1, 9):  # a watch that was sending every day, until the 8th
        _wellness(db, his, f"2026-09-{day:02d}", steps=8000, sleep_s=25000)
    _wellness(db, his, "2026-09-11", steps=0)  # a watch on charge counts nothing
    today = client.get("/api/today", headers={"Tailscale-User-Login": OWNER_LOGIN}).json()
    assert today["steps_last"] == "2026-09-08", "a stored 0 is no reading"
    render = _block(_js(), "renderToday")
    # a week without a reading there is no empty Steps card, one line saying why
    assert "no reading from the watch since ${dayName(data.steps_last)}" in render
    assert "data.steps_last >= addDays(data.day, -7)" in render, "a week's silence, not a day's"
    assert "Steps are not counted without a watch." in render
    # and training is never "not yet" where nothing could count it
    assert "data.activity_last >= addDays(data.day, -28)" in render
    assert today["activity_last"] is None
    progress = client.get("/api/progress", headers={"Tailscale-User-Login": OWNER_LOGIN}).json()
    assert progress["sync"]["watch_last"] == "2026-09-08" and progress["sync"]["watch_days"] == 8
    assert progress["sync"]["watch_regular"] is True
    assert progress["charts"]["steps"]["caption"] == (
        "No readings in the last 7 days; the last was Tue 8 Sep."
    )
    drawn = _js("progress.js")
    assert "No steps, sleep or heart readings from the watch since" in drawn
    assert "No training has arrived from intervals.icu since" in drawn
    # and it is hers alone: her page says nothing about his watch
    partners = client.get("/api/progress", headers={"Tailscale-User-Login": PARTNER_LOGIN}).json()
    assert partners["sync"]["watch_last"] is None
    # one reading from a borrowed watch is not a feed that stopped
    _wellness(db, _partners, "2026-09-02", resting_hr=58)
    partners = client.get("/api/progress", headers={"Tailscale-User-Login": PARTNER_LOGIN}).json()
    assert (
        partners["sync"]["watch_last"] == "2026-09-02"
        and partners["sync"]["watch_regular"] is False
    )
    assert "sync.watch_regular && sync.watch_days > 2" in drawn
    assert "done is counted from the watch" in drawn, "no 0 of 6 from a watch nobody wears"


# ---------------------------------------------------------------- Shopping
def test_a_shopping_line_is_a_box_the_food_the_packs_and_the_price() -> None:
    row = _block(_js(), "shoppingRow")
    assert 'class: "box", "aria-hidden": "true"' in row
    assert "`${item.packs} x ${item.pack_label}`" in row
    assert "money(item.price_pence, item.currency)" in row
    # the rest a tap away, the shelf-empty way out included, and the shop's own note in sight
    assert 'class: "item-detail", hidden: true' in row and "per100(item)" in row
    assert row.index("const shopNote") > row.index("detail.append(outRow)")
    assert "...(shopNote ? [shopNote] : []), ...(cover ? [cover] : []), detail" in row
    # the shop tag sits under the name, never in a column a long name squeezes one letter wide
    assert "[tag, credit, whose" in row


def test_ticked_lines_wait_at_the_end_and_the_count_moves_with_every_tick() -> None:
    js = _js()
    shop = js[js.index("async function renderShopping()") : js.index("function shoppingByMeal(")]
    assert "if (item.ticked) continue;  // in the trolley: drawn together at the end" in shop
    assert "`In the trolley (${ticked.length})`" in shop
    assert "in the trolley`" in shop and "still to buy" in shop
    assert "onShoppingTick = data.items.length ? drawProgress : null;" in shop
    row = _block(js, "shoppingRow")
    assert row.count("if (onShoppingTick) onShoppingTick();") == 2, "the tick, and its undo"


def test_a_failed_cooking_from_offers_retry_on_the_day_not_a_stale_button() -> None:
    js = _js()
    shop = js[js.index("async function renderShopping()") : js.index("function shoppingByMeal(")]
    assert "retry: () => pick(day)" in shop


# ---------------------------------------------------------------- the status strip
def test_a_problem_seen_is_quiet_for_a_week_by_kind_and_the_list_stays_under_more() -> None:
    js = _js()
    assert "const SEEN_FOR_MS = 7 * 86400 * 1000;" in js
    assert "seen[i.what] && Date.now() - seen[i.what] < SEEN_FOR_MS" in _block(js, "unseen")
    status = _block(js, "renderStatus")
    assert "markSeen(fresh)" in status and "The whole list stays under More." in status
    more = _block(js, "renderMore")
    assert 'api("api/status")' in more and "The box, in the last day" in more
    assert "rhythmPanel(today.profile.rhythm)" in more


# ---------------------------------------------------------------- Progress
def test_the_recorded_week_is_averaged_only_when_there_is_a_record(page) -> None:  # noqa: F811
    client, db, _partners, his, _r = page
    db.clock.set_local(dt.datetime(2026, 9, 18, 20, 0, tzinfo=TZ))
    for day, kcal in (("2026-09-14", 2000), ("2026-09-15", 2100)):
        db.insert(
            "day_confirmations", person_id=his, day=day, status="confirmed", basis="menu",
            kcal=kcal, protein_g=180,
        )  # fmt: skip
    got = client.get("/api/progress", headers={"Tailscale-User-Login": OWNER_LOGIN}).json()
    assert got["cards"]["recorded"]["days"] == 2 and got["cards"]["recorded"]["avg_kcal"] is None
    db.insert(
        "day_confirmations", person_id=his, day="2026-09-16", status="confirmed", basis="menu",
        kcal=2200, protein_g=180,
    )  # fmt: skip
    got = client.get("/api/progress", headers={"Tailscale-User-Login": OWNER_LOGIN}).json()
    assert got["cards"]["recorded"]["avg_kcal"] == 2100, "three recorded days: (2000+2100+2200)/3"
