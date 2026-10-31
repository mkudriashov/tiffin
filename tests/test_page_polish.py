"""The page, smaller and kinder on a phone: one scrolling tab row, the chart library loaded only
for Trends, a modal that keeps the focus, the shopping list saying what each food is for and what
to check at home first, a shelf-empty tap that can be taken back, and everything Tiffin
holds about a person downloadable by that person.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path

from tiffin.food import shopping
from tiffin.jobs import shop_refresh

from .test_swap import MON, OWNER_LOGIN, PARTNER_LOGIN, _headers, _menu, page  # noqa: F401
from .web_source import app_source

STATIC = Path(__file__).resolve().parents[1] / "src" / "tiffin" / "web" / "static"


def _js() -> str:
    return app_source()


def _css() -> str:
    return (STATIC / "app.css").read_text(encoding="utf-8")


def _block(js: str, name: str) -> str:
    start = js.index(f"function {name}(")
    return js[start : js.index("\n}\n", start)]


# ---------------------------------------------------------------- the shell
def test_five_destinations_in_a_bar_every_one_in_sight() -> None:
    """All five navigation destinations remain visible on a phone."""
    css = _css()
    nav = css[css.index("nav#tabs {") : css.index("}", css.index("nav#tabs {"))]
    assert "position: fixed" in nav and "repeat(5, minmax(0, 1fr))" in nav
    assert "env(safe-area-inset-bottom" in nav, "clear of a phone's home indicator"
    assert "var(--nav-h" in css and "--nav-h" in _js(), "headings clear the sticky row as it is"
    assert 'aria-current", "page"' in _js(), "the open destination is said, not only coloured"


def test_the_chart_library_waits_for_trends() -> None:
    shell = (STATIC / "index.html").read_text(encoding="utf-8")
    assert not re.search(r"<script[^>]*plotly", shell), "not loaded in front of every tab"
    assert 'name="tiffin-plotly"' in shell
    trends = _block(_js(), "renderTrends")
    assert "loadPlotly()" in trends


def test_the_cook_overlay_keeps_the_focus() -> None:
    js = _js()
    keys = js[js.index('document.addEventListener("keydown"') :]
    assert 'e.key === "Tab"' in keys and "last.focus()" in keys and "first.focus()" in keys


def test_rows_say_open_or_shut_and_selects_say_what_they_are() -> None:
    js = _js()
    assert 'row.setAttribute("aria-expanded"' in js
    assert 'el("select", { "aria-label": "The food you weighed after cooking" })' in js
    assert "${dayName(data.day)} is not on the Menu tab." in js, "a date in words, not ISO"


def test_a_slow_answer_never_draws_over_a_newer_one() -> None:
    js = _js()
    for name in ("renderMore", "renderBatch", "renderDishes"):
        body = _block(js, name)
        assert "drawTicket(" in body and "outdated(" in body, name
    dishes = _block(js, "renderDishes")
    assert "foodFormEl" in dishes, "the half-typed food survives a window change"


# ---------------------------------------------------------------- shopping
def _list(db, pid: int, base: int) -> int:
    shopping.build(db, base, retailers=("sainsburys",))
    return int(db.scalar("SELECT MAX(id) FROM shopping_lists WHERE person_id = ?", (pid,)))


def test_each_food_says_what_it_is_for_and_what_keeps_is_checked_first(page) -> None:  # noqa: F811
    client, db, partners, _his, _r = page
    base = int(db.scalar("SELECT MAX(id) FROM menus WHERE person_id = ?", (partners,)))
    _list(db, partners, base)
    got = client.get("/api/shopping", headers={"Tailscale-User-Login": PARTNER_LOGIN}).json()
    items = {i["name"]: i for i in got["items"]}
    lentils = items["Red lentils, dry"]
    assert lentils["for_dishes"] == ["Lentil dal"]
    assert lentils["long_life"] is True, "dry lentils keep for months: checked at home first"
    assert items["Broccoli, raw"]["long_life"] is False
    assert sorted(items["Broccoli, raw"]["for_dishes"]) == ["Chicken and rice", "Lentil dal"]
    js = _js()
    assert "Check at home first" in js and 'class: "muted for"' in js


def test_a_food_on_every_list_is_a_regular(page) -> None:  # noqa: F811
    client, db, partners, _his, r = page
    base = int(db.scalar("SELECT MAX(id) FROM menus WHERE person_id = ?", (partners,)))
    # a list in each of the four weeks before this one, not five lists for one week: a week
    # rebuilt by its swaps is still one week
    plates = {"breakfast": r["porridge"], "lunch": r["dal"], "dinner": r["chicken_rice"]}
    for weeks in range(4, 0, -1):
        _list(db, partners, _menu(db, partners, MON - dt.timedelta(days=7 * weeks), 5, plates))
    _list(db, partners, base)
    got = client.get("/api/shopping", headers={"Tailscale-User-Login": PARTNER_LOGIN}).json()
    assert {i["regular"] for i in got["items"]} == {"every week"}


def test_shelf_empty_can_be_taken_back(page) -> None:  # noqa: F811
    client, db, partners, _his, _r = page
    base = int(db.scalar("SELECT MAX(id) FROM menus WHERE person_id = ?", (partners,)))
    list_id = _list(db, partners, base)
    item = int(db.scalar("SELECT MIN(id) FROM shopping_items WHERE list_id = ?", (list_id,)))
    code = db.scalar(
        "SELECT i.code FROM shopping_items si JOIN ingredients i ON i.id = si.ingredient_id "
        "WHERE si.id = ?",
        (item,),
    )
    h = _headers(client)
    assert client.post("/api/shopping/out", json={"item_id": item}, headers=h).json()["out"]
    assert code in shop_refresh._missed(db, "sainsburys")
    back = client.post("/api/shopping/out", json={"item_id": item, "undo": True}, headers=h)
    assert back.status_code == 200 and back.json()["out"] is False
    assert code not in shop_refresh._missed(db, "sainsburys"), "asked about again"
    assert "Undo" in _block(_js(), "shoppingRow")


# ---------------------------------------------------------------- everything, to keep
def test_a_person_can_download_everything_held_about_them(page) -> None:  # noqa: F811
    client, db, partners, his, _r = page
    db.insert("ratings", person_id=his, recipe_id=1, score=2, note="his, not hers")
    got = client.get("/api/export", headers={"Tailscale-User-Login": PARTNER_LOGIN})
    assert got.status_code == 200
    assert got.headers["content-disposition"].startswith("attachment;")
    assert got.headers["cache-control"] == "private, no-store"
    data = json.loads(got.content)
    tables = data["tables"]
    assert data["person"]["name"] == "Sam"
    assert tables["profiles"] and tables["menus"] and tables["meal_slots"]
    for name, rows in tables.items():
        for row in rows:
            assert row.get("person_id", partners) == partners, f"{name} has someone else's row"
    assert "tailscale_identities" not in tables, "how she is recognised is not something she told"
    assert all(r["note"] != "his, not hers" for r in tables.get("ratings", []))
    mine = client.get("/api/export", headers={"Tailscale-User-Login": OWNER_LOGIN}).json()
    assert [r["note"] for r in mine["tables"]["ratings"]] == ["his, not hers"]
