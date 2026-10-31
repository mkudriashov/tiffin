"""The shop reader can use the catalogue without inventing a person."""

from __future__ import annotations

from typing import Any, cast

import pytest

from tiffin.db import Db
from tiffin.food import catalogue

from .test_food_shopping import seed_ingredient


# ---------------------------------------------------------------- the shop reader
def test_the_catalogue_a_system_job_reads_needs_no_person(fixture_db: Db, two_persons) -> None:
    """Catalogue.approved(db, 0) raises: there is no person 0 and the schema forbids one."""
    from tiffin.jobs import shop_refresh

    seed_ingredient(fixture_db, "chk", "Chicken breast", "meat", 500.0, "500 g pack")
    seed_ingredient(fixture_db, "oil", "Olive oil", "condiments", 500.0, "500 ml", pantry=1)
    fixture_db.insert(
        "ingredients",
        code="off",
        version=1,
        name="Not approved",
        cofid_code=None,
        kcal_100=1.0,
        protein_100=0.0,
        fat_100=0.0,
        carb_100=0.0,
        fibre_100=0.0,
        pack_g=100.0,
        pack_label="x",
        aisle="meat",
        pantry=0,
        use_whole_pack=0,
        approved=0,
        person_scope="both",
    )

    with pytest.raises(LookupError, match="no person with id 0"):
        catalogue.approved(fixture_db, 0)

    got = shop_refresh._catalogue(fixture_db)
    assert {i.code for i in got} == {"chk", "oil"}, "approved foods, no person needed"


def test_stale_foods_runs_at_all(fixture_db: Db, two_persons, config, frozen_clock) -> None:
    """It raised on its first line, so every weekly run failed and DMed the owner a cryptic line."""
    from tiffin.jobs import shop_refresh
    from tiffin.scheduler import JobContext

    seed_ingredient(fixture_db, "chk", "Chicken breast", "meat", 500.0, "500 g pack")
    seed_ingredient(fixture_db, "oil", "Olive oil", "condiments", 500.0, "500 ml", pantry=1)
    ctx = cast(Any, type("Ctx", (), {"db": fixture_db})())
    rows = shop_refresh.stale_foods(cast("JobContext", ctx), "tesco", limit=5)
    assert [r["code"] for r in rows] == ["chk", "oil"], "pantry last"
    assert all("name" in r for r in rows)
