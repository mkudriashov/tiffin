"""Recipe drafts: validation against the numbered catalogue, macros vs hand-computed arithmetic,
storage pinned to ingredient version ids, and the full card."""

from __future__ import annotations

import math

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tiffin.db import Db
from tiffin.food import recipes
from tiffin.food.catalogue import Ingredient
from tiffin.food.recipes import DraftIngredient, RecipeDraft


def ing(
    code: str,
    name: str,
    kcal: float,
    p: float,
    f: float,
    c: float,
    fib: float | None,
    *,
    id: int = 0,
    approved: bool = True,
    pack_g: float = 500.0,
) -> Ingredient:
    return Ingredient(
        id=id,
        code=code,
        version=1,
        name=name,
        cofid_code=None,
        kcal_100=kcal,
        protein_100=p,
        fat_100=f,
        carb_100=c,
        fibre_100=fib,
        pack_g=pack_g,
        pack_label=f"{pack_g:g} g pack",
        aisle="meat",
        pantry=False,
        use_whole_pack=False,
        approved=approved,
        person_scope="both",
    )


CHICKEN = ing("chicken", "Chicken breast, raw", 165, 31.0, 3.6, 0.0, 0.0, id=1)
RICE = ing("rice", "Basmati rice, dry", 350, 8.0, 0.5, 84.0, 1.2, id=2, pack_g=1000)
OIL = ing("oil", "Olive oil", 899, 0.0, 99.9, 0.0, 0.0, id=3, pack_g=460)
UNAPPROVED = ing("tofu", "Tofu", 73, 8.1, 4.2, 0.7, None, id=4, approved=False)
CATALOGUE = [CHICKEN, RICE, OIL, UNAPPROVED]


def draft(**over) -> RecipeDraft:
    base = {
        "name": "Chicken and rice",
        "ingredients": (DraftIngredient(1, 140), DraftIngredient(2, 90), DraftIngredient(3, 10)),
        "method": ("Cook rice.", "Fry chicken.", "Combine."),
        "prep_minutes": 30,
        "servings": 2,
    }
    base.update(over)
    return RecipeDraft(**base)


def test_macros_match_hand_arithmetic() -> None:
    d = draft()
    kcal = 140 * 165 / 100 + 90 * 350 / 100 + 10 * 899 / 100
    protein = 140 * 31.0 / 100 + 90 * 8.0 / 100
    fat = 140 * 3.6 / 100 + 90 * 0.5 / 100 + 10 * 99.9 / 100
    carb = 90 * 84.0 / 100
    fibre = 90 * 1.2 / 100
    t = recipes.totals(d, CATALOGUE)
    assert t.kcal == math.floor(kcal + 0.5) == 636
    assert t.protein_g == round(protein, 1) == 50.6
    assert t.fat_g == round(fat, 1) == 15.5
    assert t.carb_g == round(carb, 1) == 75.6
    assert t.fibre_g == round(fibre, 1) == 1.1
    assert t.total_g == 240.0
    m = recipes.macros(d, CATALOGUE)
    assert (m.kcal, m.protein_g, m.fat_g, m.carb_g, m.fibre_g) == (636, 51, 16, 76, 1)
    s = recipes.per_serving(d, CATALOGUE)
    assert s.kcal == math.floor(kcal / 2 + 0.5) == 318
    assert s.protein_g == round(protein / 2, 1) == 25.3
    assert s.total_g == 120.0
    s15 = recipes.per_serving(d, CATALOGUE, 1.5)
    assert s15.kcal == math.floor(kcal * 1.5 / 2 + 0.5)
    assert s15.carb_g == round(carb * 1.5 / 2, 1)


@given(
    st.lists(
        st.tuples(st.integers(1, 3), st.floats(0.1, 2000, allow_nan=False)), min_size=1, max_size=3
    ),
    st.integers(1, 6),
)
def test_macros_are_the_formula(pairs: list[tuple[int, float]], servings: int) -> None:
    seen: set[int] = set()
    ings = []
    for i, g in pairs:
        if i not in seen:
            seen.add(i)
            ings.append(DraftIngredient(i, g))
    d = draft(ingredients=tuple(ings), servings=servings)
    exp_kcal = sum(g * CATALOGUE[i - 1].kcal_100 / 100 for i, g in ((x.i, x.g) for x in ings))
    exp_fat = sum(g * CATALOGUE[i - 1].fat_100 / 100 for i, g in ((x.i, x.g) for x in ings))
    t = recipes.totals(d, CATALOGUE)
    # summing the same floats in a different order can straddle an exact .5, so a value
    # sitting on the boundary may round either way; everything else must match exactly.
    tol = 1 if abs(exp_kcal % 1 - 0.5) < 1e-6 else 0
    assert abs(t.kcal - math.floor(exp_kcal + 0.5)) <= tol
    # the same boundary argument as kcal, one decimal further in: a fat total sitting exactly
    # on a .x5 can round either way depending on the order the floats were summed
    fat_tol = 0.1 if abs((exp_fat * 10) % 1 - 0.5) < 1e-6 else 0.05
    assert t.fat_g == pytest.approx(round(exp_fat, 1), abs=fat_tol)
    s = recipes.per_serving(d, CATALOGUE)
    per_tol = 1 if abs((exp_kcal / servings) % 1 - 0.5) < 1e-6 else 0
    assert abs(s.kcal - math.floor(exp_kcal / servings + 0.5)) <= per_tol
    assert recipes.validate(d, CATALOGUE) == []


def test_validate_errors() -> None:
    ok = draft()
    assert recipes.validate(ok, CATALOGUE) == []
    errs = recipes.validate(
        draft(ingredients=(DraftIngredient(0, 10), DraftIngredient(5, 10))), CATALOGUE
    )
    assert any("index 0" in e for e in errs) and any("index 5" in e for e in errs)
    errs = recipes.validate(draft(ingredients=(DraftIngredient(4, 100),)), CATALOGUE)
    assert errs == ["ingredient 4 (Tofu): not approved"]
    errs = recipes.validate(
        draft(ingredients=(DraftIngredient(1, 100), DraftIngredient(1, 50))), CATALOGUE
    )
    assert any("listed twice" in e for e in errs)
    thirteen = tuple(DraftIngredient(1 + (k % 3), 10) for k in range(13))
    errs = recipes.validate(draft(ingredients=thirteen), CATALOGUE)
    assert any("13 > 12" in e for e in errs)
    errs = recipes.validate(draft(prep_minutes=121), CATALOGUE)
    assert errs == ["prep_minutes: 121 outside 0..120"]
    errs = recipes.validate(draft(name="x" * 61), CATALOGUE)
    assert errs == ["name: 61 chars > 60"]
    assert recipes.validate(draft(name="  "), CATALOGUE) == ["name: empty"]
    assert recipes.validate(draft(method=()), CATALOGUE) == ["method: no steps"]
    errs = recipes.validate(draft(method=tuple(f"s{i}" for i in range(13))), CATALOGUE)
    assert errs == ["method: 13 steps > 12"]
    assert recipes.validate(draft(servings=0), CATALOGUE) == ["servings: 0 outside 1..20"]
    errs = recipes.validate(draft(ingredients=(DraftIngredient(1, 0),)), CATALOGUE)
    assert errs == ["ingredient 1 (Chicken breast, raw): 0 g outside 0..5000"]
    errs = recipes.validate(draft(ingredients=(DraftIngredient(1, math.nan),)), CATALOGUE)
    assert len(errs) == 1 and "outside" in errs[0]
    errs = recipes.validate(ok, CATALOGUE, blocked_names=["chicken & rice", "Chicken and Rice"])
    assert errs == ["name: 'Chicken and rice' is blocked (rated 2 or under)"]


def test_from_dict_parses_model_output() -> None:
    d = RecipeDraft.from_dict(
        {
            "name": " Chilli ",
            "ingredients": [{"i": 1, "g": 400}, {"i": "2", "g": "150.5"}],
            "method": ["Brown the mince", "Simmer"],
            "prep_minutes": 45,
            "servings": 4,
        }
    )
    assert d.name == "Chilli" and d.ingredients[1] == DraftIngredient(2, 150.5)
    assert d.method == ("Brown the mince", "Simmer") and d.servings == 4 and d.keep_days == 3
    with pytest.raises(ValueError, match="needs i and g"):
        RecipeDraft.from_dict({"name": "x", "ingredients": [{"i": 1}], "method": []})
    with pytest.raises(ValueError, match="recipe draft"):
        RecipeDraft.from_dict({"name": "x", "ingredients": "nope"})
    with pytest.raises(ValueError, match="recipe draft"):
        RecipeDraft.from_dict({"ingredients": []})


def _seed_ingredients(db: Db) -> list[Ingredient]:
    out = []
    for item in (CHICKEN, RICE, OIL):
        rid = db.insert(
            "ingredients",
            code=item.code,
            version=1,
            name=item.name,
            cofid_code=None,
            kcal_100=item.kcal_100,
            protein_100=item.protein_100,
            fat_100=item.fat_100,
            carb_100=item.carb_100,
            fibre_100=item.fibre_100,
            pack_g=item.pack_g,
            pack_label=item.pack_label,
            aisle=item.aisle,
            pantry=0,
            use_whole_pack=0,
            approved=1,
            person_scope="both",
        )
        row = db.one("SELECT * FROM ingredients WHERE id = ?", (rid,))
        assert row is not None
        out.append(Ingredient.from_row(row))
    return out


def test_store_and_card(fixture_db: Db) -> None:
    cat = _seed_ingredients(fixture_db)
    d = draft(keep_days=4, freezable=True)
    rid = recipes.store(fixture_db, d, cat, "model", "claude", "owner")
    row = fixture_db.one("SELECT * FROM recipes WHERE id = ?", (rid,))
    assert row is not None and row["servings"] == 2 and row["source"] == "model"
    assert row["model_used"] == "claude" and row["freezable"] == 1 and row["keep_days"] == 4
    rows = fixture_db.all(
        "SELECT ingredient_id, grams FROM recipe_ingredients WHERE recipe_id = ? ORDER BY id",
        (rid,),
    )
    assert [(r["ingredient_id"], r["grams"]) for r in rows] == [
        (cat[0].id, 140.0),
        (cat[1].id, 90.0),
        (cat[2].id, 10.0),
    ]
    assert recipes.stored_totals(fixture_db, rid) == recipes.totals(d, cat)
    assert recipes.stored_per_serving(fixture_db, rid, 1.5) == recipes.per_serving(d, cat, 1.5)

    card = recipes.card(fixture_db, rid, 1.5)
    assert card["name"] == "Chicken and rice" and card["servings"] == 2
    assert card["steps"] == ["Cook rice.", "Fry chicken.", "Combine."]
    assert card["prep_minutes"] == 30 and card["keep_days"] == 4 and card["freezable"] is True
    assert card["ingredients"][0] == {
        "code": "chicken",
        "name": "Chicken breast, raw",
        "grams": 210.0,
        "grams_per_serving": 105.0,
        "pack_g": 500.0,
        "pack_label": "500 g pack",
        # chicken is weighed, not counted: no unit (bread by the slice, eggs and buns have one)
        "unit_name": None,
        "unit_g": None,
    }
    per = recipes.per_serving(d, cat, 1.5)
    assert card["per_serving"] == {
        "kcal": per.kcal,
        "protein_g": per.protein_g,
        "fat_g": per.fat_g,
        "carb_g": per.carb_g,
        "fibre_g": per.fibre_g,
    }
    assert card["portion_g"] == 180.0 and card["total"]["grams"] == 360.0

    with pytest.raises(ValueError, match="invalid recipe"):
        recipes.store(fixture_db, draft(prep_minutes=200), cat, "model", None, "both")
    with pytest.raises(ValueError, match="source"):
        recipes.store(fixture_db, d, cat, "scraped", None, "both")
    with pytest.raises(LookupError):
        recipes.card(fixture_db, 999)
