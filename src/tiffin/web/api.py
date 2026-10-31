"""Web API routes grouped by page area."""

from fastapi import APIRouter

from . import (
    api_account,
    api_choices,
    api_daily,
    api_library,
    api_manual,
    api_menu,
    api_preferences,
    api_shopping,
    api_today,
)
from .api_account import me
from .api_preferences import _new_said

__all__ = ["_new_said", "me", "router"]

router = APIRouter()
for area in (
    api_account,
    api_today,
    api_choices,
    api_menu,
    api_preferences,
    api_shopping,
    api_daily,
    api_library,
    api_manual,
):
    router.include_router(area.router)
