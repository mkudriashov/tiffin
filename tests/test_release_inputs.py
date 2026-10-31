"""Untrusted web values must fail cleanly before they reach SQLite."""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from tiffin.food.catalogue import FoodRejected, check_food
from tiffin.web.api_common import _grams, _number, _row_id, check_fence
from tiffin.web.identity import verify_link_token


@pytest.mark.parametrize("value", [float("inf"), float("nan"), True, 10**400])
def test_bad_food_numbers_are_refused(value: object) -> None:
    with pytest.raises(HTTPException) as exc:
        _number(value, "protein")
    assert exc.value.status_code == 400


def test_food_validation_rejects_nonfinite_values_from_other_callers() -> None:
    with pytest.raises(FoodRejected, match="finite"):
        check_food(
            name="Oats",
            kcal_100=390,
            protein_100=13,
            fat_100=7,
            carb_100=66,
            fibre_100=float("nan"),
            pack_g=500,
            aisle="Dry goods",
        )


@pytest.mark.parametrize("value", [float("inf"), 1.5, True, 10**400])
def test_bad_row_ids_are_refused(value: object) -> None:
    with pytest.raises(HTTPException) as exc:
        _row_id(value, "item")
    assert exc.value.status_code == 400


def test_oversized_grams_are_refused() -> None:
    with pytest.raises(HTTPException) as exc:
        _grams(10**400, "grams")
    assert exc.value.status_code == 400


def test_non_ascii_csrf_token_is_refused() -> None:
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/foods",
        "headers": [
            (b"cookie", b"tiffin_csrf=token"),
            (b"x-csrf-token", "tøken".encode("latin-1")),
        ],
    }
    with pytest.raises(HTTPException) as exc:
        check_fence(Request(scope), {}, set())
    assert exc.value.status_code == 403


def test_oversized_signed_link_is_refused() -> None:
    assert verify_link_token(b"key", "a" * 100_000, now=0) is None
