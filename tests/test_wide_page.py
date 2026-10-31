"""The page on a wide screen: an iPad in landscape, or a laptop.

the page is mostly used, so every wide rule sits behind one breakpoint in a sheet of its own,
loaded last, and the page's own blocks are only wrapped - in the order a phone already reads
them - so that a wide screen can set them side by side.
"""

from __future__ import annotations

import re
from pathlib import Path

from .web_source import app_source

STATIC = Path(__file__).resolve().parents[1] / "src" / "tiffin" / "web" / "static"


def _read(name: str) -> str:
    if name == "app.js":
        return app_source()
    return (STATIC / name).read_text(encoding="utf-8")


def test_the_wide_sheet_is_loaded_last() -> None:
    html = _read("index.html")
    sheets = re.findall(r'href="\{\{static:([\w.]+\.css)\}\}"', html)
    assert sheets[-1] == "wide.css", sheets


def test_a_phone_is_untouched_every_wide_rule_is_behind_the_breakpoint() -> None:
    css = re.sub(r"/\*.*?\*/", "", _read("wide.css"), flags=re.S)
    head, _, rest = css.partition("@media (min-width: 900px) {")
    assert rest, "the breakpoint"
    assert not head.strip(), f"a rule outside the breakpoint: {head.strip()[:80]}"
    # the media block runs to the end of the sheet: nothing after its closing brace
    depth = 1
    for i, ch in enumerate(rest):
        depth += (ch == "{") - (ch == "}")
        if depth == 0:
            assert not rest[i + 1 :].strip(), "a rule after the breakpoint"
            break
    assert depth == 0, "the media block closes"


def test_in_landscape_the_destinations_are_a_rail_and_lists_sit_side_by_side() -> None:
    css = _read("wide.css")
    # the five destinations down the left: the height is what is short in landscape
    nav = css[css.index("nav#tabs {") : css.index("}", css.index("nav#tabs {"))]
    assert "bottom: 0" in nav and "left: 0" in nav and "width: var(--rail)" in nav
    assert "grid-template-columns: 1fr" in nav
    for block in (
        ".today-cols",
        ".menu-days",
        ".cook-dishes",
        "#dishes .recipe-list",
        ".shop-aisles",
        "#progress .progress-cards",
        "#more-body",
        "#choose .choose-meal",
        "#cook-overlay",
    ):
        assert block in css, block


def test_the_blocks_are_wrapped_in_the_order_a_phone_reads_them() -> None:
    js = _read("app.js")
    for cls in ("today-top", "today-cols", "today-main", "today-side", "menu-days", "menu-day"):
        assert f'class: "{cls}"' in js, cls
    for cls in ("cook-dishes", "shop-aisles", "aisle"):
        assert f'class: "{cls}"' in js, cls
    # Today: the notes, then the food beside the rest of the day - the order it always had
    today = js[js.index("async function renderToday() {") : js.index("function rhythmPanel(")]
    assert today.index("top.append(") < today.index("main.append(") < today.index("side.append(")
    assert "detail.append(top, " in today, "the three parts, in that order"
    # Cook it: the amounts open beside the step only where there is room for both
    assert '$("#cook-ings").open = window.matchMedia("(min-width: 900px)").matches;' in js
