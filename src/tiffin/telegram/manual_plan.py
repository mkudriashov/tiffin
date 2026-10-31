"""Manual recipe and menu entry through a paired Telegram chat."""

from __future__ import annotations

import datetime as dt
import json
from typing import TYPE_CHECKING

from ..clock import parse_day
from ..errors import NothingToBuild
from ..food import manual
from . import convstate
from .render import esc

if TYPE_CHECKING:
    from .handlers import Handlers
    from .poller import Inbound


HELP = (
    "Plan food without a model:\n"
    "<code>/plan foods chicken</code> finds food codes.\n"
    "<code>/plan recipe</code> asks for a recipe one answer at a time. "
    "<code>/plan recipe Chicken rice | 1 | 20 | chicken_breast:200, "
    "rice_basmati:100 | Cook rice; Cook chicken</code> saves a recipe. "
    "The numbers after the name are servings and preparation minutes; food amounts are grams.\n"
    "<code>/plan recipes</code> lists your recipe numbers.\n"
    "<code>/plan menu today | 3 | dinner:12:1</code> repeats dinner for three days "
    "and makes a shopping list. The final number is portions; separate multiple meals with commas. "
    "<code>/plan cancel</code> stops a recipe draft."
)

FLOW = "manual_recipe"
FIELDS = ("name", "servings", "minutes", "foods", "method")
PROMPTS = (
    "Recipe name? Send a short name for the dish.",
    "How many servings does the whole recipe make? Send a whole number.",
    "How many minutes does preparation take? Send a whole number.",
    "Which foods and amounts? Send <code>food_code:grams</code>, separated by commas. "
    "Use <code>/plan foods chicken</code> to find a code, then send the amounts.",
    "How do you make it? Send the steps separated by semicolons.",
)


def _draft(h: Handlers, person_id: int) -> dict[str, str] | None:
    if convstate.open_flow(h.db, person_id, clock=h.clock) != FLOW:
        return None
    row = h.db.one("SELECT state_json FROM conversation_state WHERE person_id = ?", (person_id,))
    if row is None:
        return None
    try:
        state = json.loads(str(row["state_json"]))
    except ValueError:
        return None
    return state if isinstance(state, dict) and state.get("flow") == FLOW else None


def _save_draft(h: Handlers, person_id: int, state: dict[str, str]) -> None:
    h.db.execute(
        "INSERT INTO conversation_state(person_id, state_json, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(person_id) DO UPDATE SET state_json = excluded.state_json, "
        "updated_at = excluded.updated_at",
        (person_id, json.dumps(state), h.clock.utc_stamp()),
    )


def _clear_draft(h: Handlers, person_id: int) -> None:
    h.db.execute(
        "DELETE FROM conversation_state WHERE person_id = ? "
        "AND json_extract(state_json, '$.flow') = ?",
        (person_id, FLOW),
    )


def _draft_step(state: dict[str, str]) -> int:
    return next((n for n, field in enumerate(FIELDS) if field not in state), len(FIELDS))


def recipe_text(h: Handlers, inbound: Inbound, answer: str) -> str | None:
    state = _draft(h, inbound.person.id)
    if state is None:
        return None
    step = _draft_step(state)
    if step >= len(FIELDS):
        _clear_draft(h, inbound.person.id)
        return None
    value = answer.strip()
    try:
        if step == 0 and not value:
            raise ValueError("Give the recipe a name.")
        if step in (1, 2) and (not value.isdecimal() or int(value) < (1 if step == 1 else 0)):
            raise ValueError("Send a whole number of servings or minutes.")
        if step == 3:
            _recipe([state["name"], state["servings"], state["minutes"], value, "Cook."])
        if step == 4 and not any(part.strip() for part in value.split(";")):
            raise ValueError("Send at least one method step.")
        state[FIELDS[step]] = value
        if step == len(FIELDS) - 1:
            body = _recipe([state[field] for field in FIELDS])
            with h.db.tx():
                recipe_id = manual.add_recipe(h.db, inbound.person.id, body, clock=h.clock)
                _clear_draft(h, inbound.person.id)
            h.outbox.reply(
                inbound.person, f"Recipe {recipe_id} saved. Use /plan recipes to see it."
            )
        else:
            _save_draft(h, inbound.person.id, state)
            h.outbox.reply(inbound.person, PROMPTS[step + 1])
    except (ValueError, NothingToBuild) as exc:
        h.outbox.reply(inbound.person, f"{esc(str(exc))} {PROMPTS[step]}")
    return "handled"


def _recipe(parts: list[str]) -> dict[str, object]:
    if len(parts) != 5:
        raise ValueError("Use name | servings | minutes | food:grams, ... | step; step.")
    name, servings, minutes, foods, method = parts
    try:
        ingredients = [
            {"code": code.strip(), "g": float(grams.strip())}
            for item in foods.split(",")
            for code, grams in [item.split(":", 1)]
        ]
        return {
            "name": name,
            "servings": int(servings),
            "prep_minutes": int(minutes),
            "ingredients": ingredients,
            "method": [step.strip() for step in method.split(";")],
        }
    except ValueError as exc:
        raise ValueError("Use whole servings/minutes and food_code:grams for each food.") from exc


def _menu(parts: list[str], h: Handlers) -> tuple[dt.date, int, list[dict[str, object]]]:
    if len(parts) != 3:
        raise ValueError("Use day | days | slot:recipe_number:portions, ...")
    day_text, days_text, choices = parts
    try:
        day = h.clock.today_local() if day_text.casefold() == "today" else parse_day(day_text)
        days = int(days_text)
        meals = [
            {"slot": slot.strip(), "recipe_id": int(recipe_id), "portion": float(portion)}
            for item in choices.split(",")
            for slot, recipe_id, portion in [item.strip().split(":")]
        ]
    except ValueError as exc:
        raise ValueError("Use an ISO day, whole days and slot:recipe_number:portions.") from exc
    return day, days, meals


def command(h: Handlers, inbound: Inbound, arg: str) -> str:
    person = inbound.person
    action, _, detail = arg.strip().partition(" ")
    action = action.casefold()
    if action == "cancel":
        _clear_draft(h, person.id)
        h.outbox.reply(person, "Recipe draft cancelled. Send /plan recipe to start again.")
        return "handled"
    if action == "recipe" and not detail.strip():
        state = _draft(h, person.id)
        if state is None:
            if (
                convstate.displaceable(h.db, person.id, FLOW, clock=h.clock)
                in convstate.PROTECTED_FLOWS
            ):
                h.outbox.reply(person, "Finish /onboard first, then send /plan recipe.")
                return "handled"
            state = {"flow": FLOW}
            _save_draft(h, person.id, state)
        h.outbox.reply(person, PROMPTS[_draft_step(state)])
        return "handled"
    if action == "foods":
        query = detail.strip().casefold()
        if len(query) < 2:
            h.outbox.reply(person, "Give at least two letters: <code>/plan foods chicken</code>.")
            return "handled"
        found = [
            food
            for food in manual.library(h.db, person.id)["foods"]
            if query in str(food["name"]).casefold() or query in str(food["code"]).casefold()
        ]
        lines = [
            f"<code>{esc(str(food['code']))}</code> {esc(str(food['name']))}" for food in found[:12]
        ]
        h.outbox.reply(
            person, "\n".join(lines) if lines else "No available food matches that search."
        )
        return "handled"
    if action == "recipes":
        dishes = manual.library(h.db, person.id)["recipes"]
        lines = [f"{dish['id']}: {esc(str(dish['name']))}" for dish in dishes[:20]]
        h.outbox.reply(
            person, "\n".join(lines) if lines else "No recipes yet. Send /plan for the format."
        )
        return "handled"
    if action not in {"recipe", "menu"}:
        h.outbox.reply(person, HELP)
        return "handled"
    parts = [part.strip() for part in detail.split("|")]
    try:
        if action == "recipe":
            body = _recipe(parts)
            with h.db.tx():
                recipe_id = manual.add_recipe(h.db, person.id, body, clock=h.clock)
            said = f"Recipe {recipe_id} saved. Use /plan recipes to see your dishes."
        else:
            day, days, meals = _menu(parts, h)
            with h.db.tx():
                _menu_id, list_id = manual.save_menu(
                    h.db, person.id, day, days, meals, clock=h.clock
                )
            said = (
                "Menu saved and its shopping list is ready. Send /shopping to see it."
                if list_id is not None
                else "Menu saved. Send /menu to see it."
            )
    except (ValueError, NothingToBuild) as exc:
        said = f"{esc(str(exc))} Send /plan for the format."
    h.outbox.reply(person, said)
    return "handled"
