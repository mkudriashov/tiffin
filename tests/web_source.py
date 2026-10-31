"""Read the page modules in display order for source-level assertions."""

from pathlib import Path

STATIC = Path(__file__).parents[1] / "src" / "tiffin" / "web" / "static"


def _source(name: str) -> str:
    lines = (STATIC / name).read_text(encoding="utf-8").splitlines(keepends=True)
    return "".join(line for line in lines if not line.startswith("import {")).replace("export ", "")


def _split(name: str, marker: str) -> tuple[str, str]:
    source = _source(name)
    before, after = source.split(marker, 1)
    return before, marker + after


def app_source() -> str:
    """The code behind the page, with module syntax omitted for source checks."""
    cook_overlay, batch = _split(
        "cook.js", "/* ---------------------------------------------------------------- batch */"
    )
    settings, planning = _split("more.js", "const PLAN_MENUS")
    trends, summary = _split(
        "progress_page.js",
        "/* ---------------------------------------------------------------- summary */",
    )
    dishes, add_food = _split(
        "recipes.js",
        "/* ---------------------------------------------------------------- add a food */",
    )
    shell, tail = _split("app.js", "const SEEN_FOR_MS")
    return "\n".join(
        (
            _source("helpers.js"),
            cook_overlay,
            _source("today.js"),
            settings,
            trends,
            _source("menu.js"),
            _source("shopping.js"),
            batch,
            dishes,
            summary,
            add_food,
            shell,
            planning,
            tail,
        )
    )
