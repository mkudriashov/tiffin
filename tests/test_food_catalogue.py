"""Catalogue build (versioned rows, aliases once, fibre NULL blocks approval), the approved list
per person (scope, preferences, refused foods) and the prompt line rendering."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tiffin.db import Db
from tiffin.errors import ConfigError
from tiffin.food import catalogue

REPO = Path(__file__).resolve().parents[1]
CSV_HEADER = "food_code,food_name,kcal,protein_g,fat_g,carb_g,fibre_aoac_g\n"
CSV_ROWS = (
    '18-001,"Chicken, breast, raw",165,31.0,3.6,0.0,0.0\n'
    '11-002,"Rice, white, basmati, raw",351,8.1,0.5,83.7,1.1\n'
    '13-003,"Sweet potato, raw",87,1.2,0.3,21.3,\n'
    '13-004,"Mushrooms, white, raw",7,1.0,0.2,0.3,0.7\n'
    '17-005,"Oil, olive",899,0,99.9,0.0,0.0\n'
)


def write_csv(tmp_path: Path) -> Path:
    p = tmp_path / "cofid.csv"
    p.write_text(CSV_HEADER + CSV_ROWS, encoding="utf-8")
    return p


def write_yaml(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "ingredients.yaml"
    p.write_text(text, encoding="utf-8")
    return p


BASE_YAML = """
aisles: [produce, meat, grains, condiments]
ingredients:
  - code: chicken_breast
    name: Chicken breast, raw
    cofid_code: 18-001
    pack_g: 500
    pack_label: 500 g pack
    aisle: meat
    aliases: [chicken breast, chicken breast fillets]
  - code: rice_basmati
    name: Basmati rice, dry
    cofid_code: 11-002
    pack_g: 1000
    pack_label: 1 kg bag
    aisle: grains
    pantry: true
    aliases: [basmati rice, white rice]
  - code: sweet_potato
    name: Sweet potato, raw
    cofid_code: 13-003
    pack_g: 1000
    pack_label: 1 kg bag
    aisle: produce
  - code: mushrooms
    name: Mushrooms, raw
    cofid_code: 13-004
    pack_g: 250
    pack_label: 250 g pack
    aisle: produce
    aliases: [mushroom, white mushrooms]
  - code: olive_oil
    name: Olive oil
    cofid_code: 17-005
    pack_g: 460
    pack_label: 500 ml bottle
    aisle: condiments
    pantry: true
    person_scope: partner
"""


def add_profile(db: Db, pid: int, **over: object) -> int:
    base = {
        "person_id": pid,
        "effective_from": "2026-09-01",
        "sex": "m",
        "birth_date": "1990-03-15",
        "height_cm": 183.0,
        "activity_factor": 1.55,
        "kcal_floor": 1800,
    }
    base.update(over)
    return db.insert("profiles", **base)


def test_build_versions_aliases_and_fibre(fixture_db: Db, tmp_path: Path) -> None:
    csv_path = write_csv(tmp_path)
    yaml_path = write_yaml(tmp_path, BASE_YAML)
    r1 = catalogue.build(fixture_db, yaml_path, csv_path)
    assert r1.inserted == [
        "chicken_breast",
        "rice_basmati",
        "sweet_potato",
        "mushrooms",
        "olive_oil",
    ]
    assert r1.unchanged == [] and r1.missing_cofid == []
    assert r1.fibre_null == ["sweet_potato"]
    assert r1.aliases_added == 6
    row = fixture_db.latest("ingredients", code="sweet_potato")
    assert row is not None and row["approved"] == 0 and row["fibre_100"] is None
    row = fixture_db.latest("ingredients", code="chicken_breast")
    assert row is not None and row["version"] == 1 and row["approved"] == 1
    assert row["kcal_100"] == 165.0 and row["protein_100"] == 31.0 and row["fibre_100"] == 0.0
    assert row["created_at"] == fixture_db.clock.utc_stamp()

    # same inputs again: nothing new, aliases not duplicated
    r2 = catalogue.build(fixture_db, yaml_path, csv_path)
    assert r2.inserted == [] and len(r2.unchanged) == 5 and r2.aliases_added == 0
    assert fixture_db.scalar("SELECT COUNT(*) FROM ingredients") == 5
    assert fixture_db.scalar("SELECT COUNT(*) FROM ingredient_aliases") == 6

    # a changed pack and a filled fibre: new version rows, the old rows untouched
    changed = BASE_YAML.replace("pack_g: 500\n", "pack_g: 600\n").replace(
        "    cofid_code: 13-003\n", "    cofid_code: 13-003\n    fibre_100: 3.0\n"
    )
    r3 = catalogue.build(fixture_db, write_yaml(tmp_path, changed), csv_path)
    assert r3.inserted == ["chicken_breast", "sweet_potato"] and r3.fibre_null == []
    assert fixture_db.scalar("SELECT COUNT(*) FROM ingredients") == 7
    versions = fixture_db.all(
        "SELECT version, pack_g FROM ingredients WHERE code = 'chicken_breast' ORDER BY version"
    )
    assert [(v["version"], v["pack_g"]) for v in versions] == [(1, 500.0), (2, 600.0)]
    sp = fixture_db.latest("ingredients", code="sweet_potato")
    assert sp is not None and sp["version"] == 2 and sp["fibre_100"] == 3.0 and sp["approved"] == 1


def test_missing_cofid_and_label_override(fixture_db: Db, tmp_path: Path) -> None:
    yaml_text = """
aisles: [dairy]
ingredients:
  - code: skyr
    name: Skyr, natural
    cofid_code: 99-999
    pack_g: 450
    aisle: dairy
  - code: whey
    name: Whey protein
    cofid_code: null
    pack_g: 1000
    aisle: dairy
    pantry: true
    macros_100: {kcal: 400, protein_g: 80, fat_g: 7, carb_g: 5, fibre_g: 0}
"""
    r = catalogue.build(fixture_db, write_yaml(tmp_path, yaml_text), write_csv(tmp_path))
    assert r.missing_cofid == ["skyr"] and r.inserted == ["whey"]
    whey = fixture_db.latest("ingredients", code="whey")
    assert whey is not None and whey["cofid_code"] is None and whey["protein_100"] == 80.0
    assert whey["approved"] == 1 and whey["pack_label"] == "1000 g pack"
    assert fixture_db.latest("ingredients", code="skyr") is None


@pytest.mark.parametrize(
    "bad",
    [
        "aisles: [meat]\ningredients:\n  - code: Bad Code\n"
        "    name: x\n    pack_g: 1\n    aisle: meat\n",
        "aisles: [meat]\ningredients:\n  - code: a\n    name: x\n    pack_g: 0\n    aisle: meat\n",
        "aisles: [meat]\ningredients:\n  - code: a\n    name: x\n    pack_g: 1\n    aisle: fish\n",
        "aisles: [meat]\ningredients:\n  - code: a\n    name: x\n    pack_g: 1\n    aisle: meat\n"
        "    person_scope: them\n",
        "aisles: [meat]\ningredients:\n  - code: a\n    name: x\n    pack_g: 1\n    aisle: meat\n"
        "  - code: a\n    name: y\n    pack_g: 1\n    aisle: meat\n",
        "aisles: [meat]\ningredients:\n  - code: a\n    name: x\n    pack_g: 1\n    aisle: meat\n"
        "    macros_100: {kcal: 1}\n",
        "aisles: [meat]\ningredients: []\n",
    ],
)
def test_yaml_validation(tmp_path: Path, bad: str) -> None:
    with pytest.raises(ConfigError):
        catalogue.load_yaml(write_yaml(tmp_path, bad))


def test_approved_scope_preferences_and_refused_foods(
    fixture_db: Db, two_persons: dict[str, int], tmp_path: Path
) -> None:
    catalogue.build(fixture_db, write_yaml(tmp_path, BASE_YAML), write_csv(tmp_path))
    owner_id, partner_id = two_persons["owner"], two_persons["partner"]

    # ordered by code, numbered from 1; sweet_potato unapproved; olive_oil is hers only
    items = catalogue.approved(fixture_db, owner_id)
    assert [i.code for i in items] == ["chicken_breast", "mushrooms", "rice_basmati"]
    assert items[0].aliases == ("chicken breast", "chicken breast fillets")
    assert [i.code for i in catalogue.approved(fixture_db, partner_id)] == [
        "chicken_breast",
        "mushrooms",
        "olive_oil",
        "rice_basmati",
    ]
    lines = catalogue.prompt_lines(items)
    assert lines[0].startswith("1. Chicken breast, raw — 165 kcal, 31 P, 3.6 F, 0 C, 0 fibre")
    assert lines[2].endswith("(pack 1000 g, 1 kg bag)")

    # preferences: the latest row per code wins
    fixture_db.insert(
        "ingredient_preferences", person_id=owner_id, ingredient_code="mushrooms", status="refused"
    )
    assert [i.code for i in catalogue.approved(fixture_db, owner_id)] == [
        "chicken_breast",
        "rice_basmati",
    ]
    fixture_db.insert(
        "ingredient_preferences", person_id=owner_id, ingredient_code="mushrooms", status="ok"
    )
    assert len(catalogue.approved(fixture_db, owner_id)) == 3
    # her preferences do not touch his list
    fixture_db.insert(
        "ingredient_preferences",
        person_id=partner_id,
        ingredient_code="chicken_breast",
        status="refused",
    )
    assert len(catalogue.approved(fixture_db, owner_id)) == 3
    assert "chicken_breast" not in [i.code for i in catalogue.approved(fixture_db, partner_id)]

    # profile refused_foods: whole-word in the name ('mushrooms'), an alias ('white rice'),
    # case-insensitive; a substring of a word ('mush') is not a hit; the latest profile wins
    add_profile(fixture_db, owner_id, refused_foods=json.dumps(["Mushrooms", "WHITE RICE"]))
    assert [i.code for i in catalogue.approved(fixture_db, owner_id)] == ["chicken_breast"]
    add_profile(
        fixture_db, owner_id, effective_from="2026-09-05", refused_foods=json.dumps(["mush"])
    )
    assert len(catalogue.approved(fixture_db, owner_id)) == 3
    add_profile(fixture_db, owner_id, effective_from="2026-09-06", refused_foods="not json")
    assert len(catalogue.approved(fixture_db, owner_id)) == 3


def test_refused_food_typed_in_the_singular_still_matches_a_plural_name(
    fixture_db: Db, two_persons: dict[str, int], tmp_path: Path
) -> None:
    """'Chickpea' on the avoid list left 'Chickpeas, tinned, drained' approved, so the refused
    food came back in a menu and nothing said why."""
    catalogue.build(fixture_db, write_yaml(tmp_path, BASE_YAML), write_csv(tmp_path))
    owner_id = two_persons["owner"]

    add_profile(fixture_db, owner_id, refused_foods=json.dumps(["Mushroom"]))
    assert "mushrooms" not in [i.code for i in catalogue.approved(fixture_db, owner_id)]

    # and the other way round: a plural entry against a singular name
    add_profile(
        fixture_db,
        owner_id,
        effective_from="2026-09-05",
        refused_foods=json.dumps(["chicken breasts"]),
    )
    assert "chicken_breast" not in [i.code for i in catalogue.approved(fixture_db, owner_id)]

    # de-pluralising must not widen a term into a partial word
    add_profile(
        fixture_db, owner_id, effective_from="2026-09-06", refused_foods=json.dumps(["mush"])
    )
    assert len(catalogue.approved(fixture_db, owner_id)) == 3


@pytest.mark.parametrize(
    ("term", "name", "hit"),
    [
        ("chickpea", "Chickpeas, tinned, drained", True),
        ("chickpeas", "Chickpeas, tinned, drained", True),
        ("pea", "Chickpeas, tinned, drained", False),
        ("berry", "Berries, mixed, frozen", True),
        ("cress", "Cress, fresh", True),  # 'ss' is not a plural ending
        ("oat", "Oats, rolled", True),
        ("pea", "Peas, garden, frozen", True),
        ("tomatoes", "Tomato ketchup", True),  # -oes: 'tomatoe' matched nothing
        ("tomatoes", "Tomato puree", True),
        ("tomato", "Tomatoes, raw", True),
        ("potatoes", "Potato, white, raw", True),
        ("avocadoes", "Avocado, Hass", True),
        ("mangoes", "Mango, ripe", True),
        ("toes", "Toenail clippings", False),  # the -oes rule stops short of four letters
    ],
)
def test_refused_matches_singular_and_plural(term: str, name: str, hit: bool) -> None:
    ing = catalogue.Ingredient(
        id=1,
        code="x",
        version=1,
        name=name,
        cofid_code=None,
        kcal_100=100.0,
        protein_100=1.0,
        fat_100=1.0,
        carb_100=1.0,
        fibre_100=1.0,
        pack_g=500.0,
        pack_label="500 g pack",
        aisle="ambient",
        pantry=False,
        use_whole_pack=False,
        approved=True,
        person_scope="both",
    )
    assert catalogue.refused_matches(term, ing) is hit


def test_prompt_line_matches_the_plan_example() -> None:
    ing = catalogue.Ingredient(
        id=1,
        code="chicken_breast",
        version=1,
        name="Chicken breast, raw",
        cofid_code="18-001",
        kcal_100=165.0,
        protein_100=31.0,
        fat_100=3.6,
        carb_100=0.0,
        fibre_100=0.0,
        pack_g=500.0,
        pack_label="500 g pack",
        aisle="meat",
        pantry=False,
        use_whole_pack=False,
        approved=True,
        person_scope="both",
    )
    assert ing.prompt_line(12) == (
        "12. Chicken breast, raw — 165 kcal, 31 P, 3.6 F, 0 C, 0 fibre /100 g (pack 500 g)"
    )
    eggs = catalogue.Ingredient(
        id=2,
        code="eggs",
        version=1,
        name="Eggs",
        cofid_code=None,
        kcal_100=131.0,
        protein_100=12.6,
        fat_100=9.0,
        carb_100=0.0,
        fibre_100=None,
        pack_g=348.0,
        pack_label="6 eggs",
        aisle="dairy",
        pantry=False,
        use_whole_pack=False,
        approved=False,
        person_scope="both",
    )
    assert eggs.prompt_line(3).endswith("n/a fibre /100 g (pack 348 g, 6 eggs)")


def test_real_catalogue_builds_against_the_real_csv(fixture_db: Db, two_persons) -> None:
    """Config/ingredients.yaml x data/cofid/cofid_2021_proximates.csv: every cofid_code resolves,
    every approved row has fibre, the only unapproved rows are the documented ones."""
    yaml_path = REPO / "config" / "ingredients.yaml"
    csv_path = REPO / "data" / "cofid" / "cofid_2021_proximates.csv"
    r = catalogue.build(fixture_db, yaml_path, csv_path)
    assert r.missing_cofid == []
    assert len(r.inserted) >= 60
    assert set(r.fibre_null) <= {"tofu"}
    cat = catalogue.load_yaml(yaml_path)
    assert cat.aisles == catalogue.DEFAULT_AISLES
    for y in cat.ingredients:
        assert y.aliases, y.code
    items = catalogue.approved(fixture_db, two_persons["owner"])
    assert len(items) >= 60
    assert [i.code for i in items] == sorted(i.code for i in items)
    for line in catalogue.prompt_lines(items):
        assert len(line) < 120, line
        assert "n/a" not in line


def test_refusing_a_food_clears_it_from_the_real_catalogue(fixture_db: Db, two_persons) -> None:
    catalogue.build(
        fixture_db,
        REPO / "config" / "ingredients.yaml",
        REPO / "data" / "cofid" / "cofid_2021_proximates.csv",
    )
    pid = two_persons["owner"]
    everything = catalogue.approved(fixture_db, pid)
    for term, word in (("tomatoes", "tomato"), ("potatoes", "potato"), ("oats", "oat")):
        add_profile(
            fixture_db,
            pid,
            effective_from=f"2026-09-{10 + len(term) % 5}",
            refused_foods=json.dumps([term]),
        )
        # by whole word: Buckwheat groats hold the letters "oat" and are not oats
        left = [
            i.name
            for i in catalogue.approved(fixture_db, pid)
            if re.search(rf"\b{word}", i.name.lower())
        ]
        assert left == [], f"refusing {term} left {left}"
        assert len(catalogue.approved(fixture_db, pid)) < len(everything)
