"""Tests for day-specific actions and identity scoping on the page."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from tiffin import days

from .test_dash_today_tab import (  # noqa: F401 (open_page is a pytest fixture)
    HEADER,
    OWNER_LOGIN,
    THURSDAY,
    WEDNESDAY,
    at,
    open_page,
)
from .web_source import app_source


def _post(page, body: dict[str, Any]):
    token = page.as_owner("api/me").json().get("csrf", "")
    return page.client.post(
        "api/today/confirm",
        json=body,
        headers={HEADER: OWNER_LOGIN, "X-CSRF-Token": token, "Sec-Fetch-Site": "same-origin"},
    )


def test_recording_wednesday_at_00_40_records_wednesday(open_page) -> None:  # noqa: F811
    page = open_page(at(THURSDAY, 0, 40))
    got = _post(page, {"as_planned": True, "day": WEDNESDAY.isoformat()})
    assert got.status_code == 200, got.text
    wed = days.latest_confirmation(page.db, page.owner_id, WEDNESDAY)
    thu = days.latest_confirmation(page.db, page.owner_id, THURSDAY)
    assert wed is not None and wed["status"] == "confirmed", "the day on screen is recorded"
    assert thu is None, "and the day not yet eaten is left alone"


def test_a_day_not_yet_lived_or_long_gone_is_refused(open_page) -> None:  # noqa: F811
    page = open_page(at(WEDNESDAY, 20, 45))
    ahead = _post(page, {"as_planned": True, "day": THURSDAY.isoformat()})
    assert ahead.status_code == 400 and "not happened" in ahead.text
    back = _post(page, {"as_planned": True, "day": "2026-08-01"})
    assert back.status_code == 400 and "too far back" in back.text


def test_the_page_sends_the_day_it_shows_and_redraws(open_page) -> None:  # noqa: F811
    js = open_page(at(WEDNESDAY, 20, 45)).app_js()
    call = re.search(r'api\("api/today/confirm", \{ body: \{([^}]*)\}', js)
    assert call is not None and "day: data.day" in call.group(1), "the day on screen is sent"
    body = js[js.index('api("api/today/confirm"') :][:400]
    assert "renderToday()" in body, "the cards move with the record"


def test_today_is_never_pinned_to_the_first_day_drawn(open_page) -> None:  # noqa: F811
    js = open_page(at(WEDNESDAY, 9, 0)).app_js()
    render = js[
        js.index("async function renderToday") : js.index(
            "/* ---------------------------------------------------------------- settings"
        )
    ]
    assert "todayShowing = data.day" not in render, "a default is not a choice"
    assert "visibilitychange" in js and "refreshToday" in js, "a phone left open moves with the day"


def test_a_failed_start_shows_a_panel_with_a_way_back(open_page) -> None:  # noqa: F811
    page = open_page(at(WEDNESDAY, 9, 0))
    js = page.app_js()
    boot = js[js.index("async function boot") :]
    assert "bootFailed(" in boot, "a sign-in failure says so and offers Try again"
    assert 'id="boot-failed"' in page.as_owner("").text
    assert "Could not reach Tiffin" in js, "no bare 'Failed to fetch'"


# ---------------------------------------------------------------- Phase 3: the dashboard
def _rate(page, recipe_id: int, score: int, note: str | None = None):
    token = page.as_owner("api/me").json().get("csrf", "")
    body: dict[str, Any] = {"recipe_id": recipe_id, "score": score}
    if note:
        body["note"] = note
    return page.client.post(
        "api/dishes/rate",
        json=body,
        headers={HEADER: OWNER_LOGIN, "X-CSRF-Token": token, "Sec-Fetch-Site": "same-origin"},
    )


def test_a_score_from_menu_keeps_the_note_typed_on_dishes(open_page) -> None:  # noqa: F811
    page = open_page(at(WEDNESDAY, 20, 0))
    lunch = page.recipes["lunch"]
    assert _rate(page, lunch, 2, "dry - it needs a sauce").status_code == 200
    assert _rate(page, lunch, 4).status_code == 200  # a tap on the Menu card, no note
    row = page.db.one(
        "SELECT score, note FROM ratings WHERE person_id = ? ORDER BY id DESC LIMIT 1",
        (page.owner_id,),
    )
    assert row is not None and int(row["score"]) == 4
    assert row["note"] == "dry - it needs a sauce", "the note rides forward, never erased"


def test_the_menu_card_shows_the_score_already_given(open_page) -> None:  # noqa: F811
    page = open_page(at(WEDNESDAY, 20, 0))
    assert _rate(page, page.recipes["lunch"], 5).status_code == 200
    menu = page.as_owner(f"api/menu?segment={WEDNESDAY.isoformat()}").json()
    scores = {
        s["name"]: s.get("score") for d in menu["days"] for s in d["slots"] if s["slot"] == "lunch"
    }
    assert set(scores.values()) == {5}, scores


def test_the_dashboard_fixes_are_in_the_page(open_page) -> None:  # noqa: F811
    page = open_page(at(WEDNESDAY, 9, 0))
    js = page.app_js()
    shell = page.as_owner("").text
    # cook overlay: back and forward, remembered place, ingredients, Escape, wake lock again
    assert 'addEventListener("click", cookPrev)' in js and "cookPlace" in js
    assert (
        'e.key === "Escape"' in js and 'id="cook-ings"' in shell and 'aria-live="polite"' in shell
    )
    # ratings labelled, pressed state, current score
    assert "Rate it (1 = never again, 5 = more often)" in js and "slot.score === n" in js
    # Cook totals are read-only rows
    assert '"All pots together"' in js
    # Open in Menu carries the day and the meal
    assert 'show("menu", { day: data.day, slot: m.slot })' in js
    progress = (Path(__file__).resolve().parents[1] / "src" / "tiffin" / "web" / "static"
                / "progress.js").read_text(encoding="utf-8")  # fmt: skip
    assert "so far this week" in progress and "next.disabled = data.week.next === null" in progress
    assert 'setAttribute("aria-current", "page")' in js and 'tickformat: "%-d %b"' in js
    # one toast timer at a time, and a Close on every toast
    assert "clearTimeout(failTimer)" in js and '"aria-label": "Dismiss"' in js


def test_shelf_empty_is_a_small_link_not_a_second_row() -> None:
    """The cascade trap again: the rule must out-specify what it overrides, not merely exist."""
    from pathlib import Path

    css = Path(__file__).parents[1].joinpath("src/tiffin/web/static/app.css").read_text("utf-8")
    # its own row now, with an Undo beside it
    assert "ul.items li .out-row > button.link.out {" in css
    # (classes, elements): ours (4, 3); `ul.items button` (1, 2); `ul.items li.done button` (2, 3)
    assert _beats((4, 3), (1, 2)) and _beats((4, 3), (2, 3))
    # sized from the tab row as it actually is (--nav-h, set by app.js), not a guessed 120 px
    assert '[id^="d-"], [id^="plates-"], #foods { scroll-margin-top: calc(var(--nav-h' in css, (
        "deep links clear the tab bar"
    )


def _beats(ours: tuple[int, int], theirs: tuple[int, int]) -> bool:
    """(classes, elements) specificity: does `ours` win over `theirs` (no ids involved)?"""
    return list(ours) > list(theirs)


def test_the_last_section_four_items_are_in_the_page(open_page) -> None:  # noqa: F811
    js = open_page(at(WEDNESDAY, 9, 0)).app_js()
    # Trends: a labelled tick past both ends of a weight chart, on a round step
    assert "layout.yaxis.dtick = step" in js and "Math.ceil(hi / step) * step + step / 2" in js
    # shopping ticks are said, not only struck through
    assert 'button.setAttribute("aria-pressed", ticked ? "true" : "false")' in js
    # the add-a-food inputs have names a screen reader can read
    for label in ("Food name", "Pack size in grams", "Pack label", "Aisle", "Who it is for"):
        assert f'"aria-label": "{label}"' in js, label


def test_a_shared_dish_is_cooked_one_plate_at_a_time() -> None:

    js = app_source()
    start = js.index("async function renderMenu")
    menu = js[start : js.index("\n/* ---", start)]
    assert "Cooked together, one plate at a time" in menu
    assert "in all: your plate" not in menu, "no pot total to split at the end"
    assert "ingredientLines(slot.together.multiplier)" in menu, "the other plate's own amounts"
    assert "Cook your plate" in menu and "'s plate`, `${slot.name} (" in menu, "a run per plate"
