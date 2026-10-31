"""Tasty, healthy and within her calories.

Two causes, both fixed here. The brief asked for "a deliberate source of fat (oil, nuts, seeds,
oily fish, cheese, peanut butter)", and against her tight floors the model bolted cheese onto
whatever was cooking. And the catalogue - the only foods the model may use - held no lemon, no
lime, no fresh herb and no chilli, so every dish tasted of the same garlic and paprika.
"""

from __future__ import annotations

from pathlib import Path

from tiffin.db import Db
from tiffin.food import catalogue
from tiffin.model import prompts

ROOT = Path(__file__).resolve().parents[1]
FLAVOUR = {
    "lemon_juice",
    "lime_juice",
    "coriander_fresh",
    "basil_fresh",
    "parsley_fresh",
    "chives_fresh",
    "spring_onions",
    "thyme_dried",
    "rosemary_dried",
    "chilli_sauce",
    "worcestershire_sauce",
}
WARM_VEG = {
    "leeks",
    "parsnips",
    "red_cabbage",
    "aubergine",
    "butternut_squash",
    "asparagus",
    "brussels_sprouts",
}


def _brief(name: str) -> str:
    return (ROOT / "config" / "prompts" / f"{name}.md").read_text(encoding="utf-8")


def test_every_brief_asks_for_fat_that_belongs_and_food_that_tastes_good() -> None:
    for name in ("menu_propose", "menu_weekend", "menu_shared_weekend"):
        brief = _brief(name)
        assert "never a food added only for its numbers" in brief, name
        assert "deliberate source of fat (oil, nuts, seeds, oily fish, cheese, peanut" not in brief
    assert "Taste: every dish is one people cook or order because it tastes good" in _brief(
        "menu_propose"
    )
    assert "cheddar on tuna pasta" in prompts.ONE_DISH_RULES["main"]


def test_the_real_catalogue_offers_flavour_and_warm_vegetables(tmp_path: Path) -> None:
    """Built from the box's own YAML and CoFID table, as `tiffin catalogue build` does: every
    new food has its numbers from a source and is offered to the model."""
    db = Db.open(tmp_path / "t.db")
    try:
        built = catalogue.build(
            db,
            ROOT / "config" / "ingredients.yaml",
            ROOT / "data" / "cofid" / "cofid_2021_proximates.csv",
        )
        assert not built.missing_cofid, built.missing_cofid
        offered = {i.code: i for i in catalogue.latest_versions(db) if i.approved}
        missing = (FLAVOUR | WARM_VEG) - set(offered)
        assert not missing, f"not offered to the model: {sorted(missing)}"
        assert all(offered[c].fibre_100 is not None for c in FLAVOUR | WARM_VEG)
    finally:
        db.close()
