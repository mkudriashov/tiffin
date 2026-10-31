"""Expose scoped tiffin operations to configured model backends.

Tools read the person identified by the current chat or session."""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..clock import isoday, parse_day, week_start
from ..db import Db
from ..food.menu import meals_for_day

SLOT_ORDER = (
    "CASE ms.slot WHEN 'breakfast' THEN 0 WHEN 'lunch' THEN 1 WHEN 'dinner' THEN 2 "
    "WHEN 'snack' THEN 3 ELSE 4 END"
)
DAY_PATTERN = "^[0-9]{4}-[0-9]{2}-[0-9]{2}$"
Handler = Callable[..., dict[str, Any]]


class ToolError(ValueError):
    """A call the tool cannot honour (bad arguments, unknown tool); the model sees it as text."""


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Handler
    writes: bool = False
    #: reads a shopping list, which is built on the spot when there is none yet: it needs the
    #: shops within reach, nearest first, or the list it builds names products from anywhere
    shops: bool = False


# ---------------------------------------------------------------- helpers
def _params(props: Mapping[str, Any], required: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": dict(props),
        "required": list(required),
        "additionalProperties": False,
    }


def _day_prop(text: str) -> dict[str, Any]:
    return {"type": ["string", "null"], "pattern": DAY_PATTERN, "description": text}


def _day(db: Db, value: str | None) -> str:
    if value is None or value == "":
        return isoday(db.clock.today_local())
    try:
        return isoday(parse_day(value))
    except ValueError:
        raise ToolError(f"day must be YYYY-MM-DD, got {value!r}") from None


def _rows(rows: list[sqlite3.Row], *, drop: tuple[str, ...] = ("person_id",)) -> list[dict]:
    return [{k: v for k, v in dict(r).items() if k not in drop} for r in rows]


def _row(row: sqlite3.Row | None, *, drop: tuple[str, ...] = ("person_id",)) -> dict | None:
    return None if row is None else _rows([row], drop=drop)[0]


def _week(db: Db, value: str | None) -> tuple[str, str]:
    start = week_start(parse_day(_day(db, value)))
    return isoday(start), isoday(start + dt.timedelta(days=6))


# ---------------------------------------------------------------- reads
def get_targets(db: Db, person_id: int, day: str | None = None) -> dict[str, Any]:
    d = _day(db, day)
    row = db.one(
        "SELECT week_start, kcal, protein_g, fat_g, carb_g, fibre_g, source "
        "FROM v_targets_latest WHERE person_id = ? AND week_start <= ? "
        "ORDER BY week_start DESC LIMIT 1",
        (person_id, d),
    )
    return {"day": d, "targets": _row(row)}


def get_day(db: Db, person_id: int, day: str | None = None) -> dict[str, Any]:
    d = _day(db, day)
    conf = db.one(
        "SELECT day, status, basis, kcal, protein_g, fat_g, carb_g, fibre_g, note "
        "FROM v_day_latest WHERE person_id = ? AND day = ?",
        (person_id, d),
    )
    estimates = db.all(
        "SELECT id, description, kcal, protein_g, fat_g, carb_g, fibre_g, status "
        "FROM estimates e WHERE person_id = ? AND day = ? AND status <> 'discarded' "
        "AND NOT EXISTS (SELECT 1 FROM estimates y WHERE y.source_estimate_id = e.id) "
        "ORDER BY id",
        (person_id, d),
    )
    return {
        "day": d,
        "confirmation": _row(conf),
        "meals": meals_for_day(db, person_id, parse_day(d)),
        "estimates": _rows(estimates),
    }


def get_week(db: Db, person_id: int, week: str | None = None) -> dict[str, Any]:
    ws, we = _week(db, week)
    summary = db.one(
        "SELECT week_start, confirmed_days, usable_days, paused_days, trend_delta, "
        "sessions_done, sessions_planned "
        "FROM v_week_summary WHERE person_id = ? AND week_start = ?",
        (person_id, ws),
    )
    days = db.all(
        "SELECT day, status, basis, kcal, protein_g, fat_g, carb_g, fibre_g "
        "FROM v_day_latest WHERE person_id = ? AND day BETWEEN ? AND ? ORDER BY day",
        (person_id, ws, we),
    )
    return {
        "week_start": ws,
        "week_end": we,
        "summary": _row(summary),
        "days": _rows(days),
        "targets": get_targets(db, person_id, ws)["targets"],
    }


def get_trend(db: Db, person_id: int, days: int = 14) -> dict[str, Any]:
    n = max(1, min(int(days), 365))
    today = db.clock.today_local()
    since = isoday(today - dt.timedelta(days=n))
    rows = db.all(
        "SELECT day, trend_kg, n FROM v_trend_latest WHERE person_id = ? AND day BETWEEN ? AND ? "
        "ORDER BY day",
        (person_id, since, isoday(today)),
    )
    points = [{"day": r["day"], "trend_kg": round(float(r["trend_kg"]), 2)} for r in rows]
    delta = None
    if len(points) >= 2:
        delta = round(points[-1]["trend_kg"] - points[0]["trend_kg"], 2)
    return {
        "since": since,
        "points": points,
        "latest": points[-1] if points else None,
        "delta_kg": delta,
    }


def get_sessions(db: Db, person_id: int, week: str | None = None) -> dict[str, Any]:
    ws, we = _week(db, week)
    planned = db.all(
        "SELECT ps.id, ps.weekday, ps.kind, ps.target_minutes, ps.start_time "
        "FROM planned_sessions ps WHERE ps.person_id = ? AND ps.plan_id = ("
        "SELECT id FROM training_plans tp WHERE tp.person_id = ps.person_id "
        "AND tp.effective_from <= ? ORDER BY tp.effective_from DESC, tp.id DESC LIMIT 1) "
        "ORDER BY ps.weekday, ps.id",
        (person_id, we),
    )
    matches = db.all(
        "SELECT sm.day, sm.planned_session_id, sm.icu_id, sm.status, sm.minutes "
        "FROM session_matches sm WHERE sm.person_id = ? AND sm.day BETWEEN ? AND ? "
        "AND sm.id = (SELECT MAX(id) FROM session_matches x WHERE x.person_id = sm.person_id "
        "AND x.day = sm.day "
        "AND COALESCE(x.planned_session_id, -1) = COALESCE(sm.planned_session_id, -1) "
        "AND COALESCE(x.icu_id, '') = COALESCE(sm.icu_id, '')) ORDER BY sm.day, sm.id",
        (person_id, ws, we),
    )
    activities = db.all(
        "SELECT icu_id, day, kind, name, moving_s, calories, avg_hr, load "
        "FROM icu_activity WHERE person_id = ? AND day BETWEEN ? AND ? ORDER BY day, start_utc",
        (person_id, ws, we),
    )
    return {
        "week_start": ws,
        "week_end": we,
        "planned": _rows(planned),
        "matches": _rows(matches),
        "activities": _rows(activities),
    }


def _menu_status(db: Db, head: sqlite3.Row) -> str:
    """The decision, not the column. `menus` is append-only, so `menus.status` keeps the value it
    was inserted with - 'proposed' - for the life of the row, and accepting writes a
    `menu_decisions` row instead. Handing the model the column made it tell the person their
    accepted menu was "still marked as proposed" and leave them checking an obsolete decision.
    """
    from ..food import menu as menu_mod  # noqa: PLC0415 (avoids a cycle at import time)

    return menu_mod.decision_of(db, int(head["menu_id"])) or str(head["status"])


def get_menu(db: Db, person_id: int, day: str | None = None) -> dict[str, Any]:
    """The day's meals WITH their ingredients and method.

    "Recipe for tomorrow's dinner", asked in words, got "I can see the dish but not how it is
    made": this tool gave the model names and macros and nothing else, so the one question the
    person asked was the one it could not answer. The ingredients and steps are the bot card's,
    from `recipes.card`, so the words path and the tap path give the same recipe.
    """
    from ..food import recipes as recipes_mod  # noqa: PLC0415 (avoids a cycle at import time)

    d = _day(db, day)
    rows = db.all(
        "SELECT md.menu_id AS menu_id, md.segment_start, md.segment_end, md.status, ms.slot, "
        "ms.recipe_id, rc.name, ms.portion_g, ms.portion_multiplier, ms.kcal, ms.protein_g, "
        "ms.fat_g, ms.carb_g, ms.fibre_g, rc.prep_minutes FROM v_menu_days_current md "
        "JOIN meal_slots ms ON ms.menu_day_id = md.id "
        "JOIN recipes rc ON rc.id = ms.recipe_id "
        "WHERE md.person_id = ? AND md.day = ? "
        f"ORDER BY {SLOT_ORDER}, ms.id",
        (person_id, d),
    )
    totals = db.one(
        "SELECT md.kcal, md.protein_g, md.fat_g, md.carb_g, md.fibre_g "
        "FROM v_menu_days_current md WHERE md.person_id = ? AND md.day = ?",
        (person_id, d),
    )
    meals = []
    for r in rows:
        meal = {
            k: v
            for k, v in dict(r).items()
            if k not in ("menu_id", "segment_start", "segment_end", "status", "portion_multiplier")
        }
        card = recipes_mod.card(db, int(r["recipe_id"]), float(r["portion_multiplier"]))
        meal["servings"] = card["servings"]
        # one plate as plated - whole slices, eggs and buns - the same grams the page and the
        # bot's card give, never the straight-line figure beside a kcal that counts the whole bun
        lines = []
        for i in card["ingredients"]:
            g = recipes_mod.card_grams(i, int(card["servings"]))
            words = recipes_mod.unit_words(i.get("unit_name"), i.get("unit_g"), g)
            lines.append(f"{i['name']} {g:.0f} g" + (f", {words}" if words else ""))
        meal["ingredients"] = lines
        meal["steps"] = card["steps"]
        meals.append(meal)
    head = rows[0] if rows else None
    from ..food import catalogue as catalogue_mod  # noqa: PLC0415 (avoids a cycle at import time)

    refused = catalogue_mod.refused_names_on(db, person_id, [int(head["menu_id"])]) if head else []
    return {
        "day": d,
        "menu_id": head["menu_id"] if head else None,
        "segment": [head["segment_start"], head["segment_end"]] if head else None,
        "status": _menu_status(db, head) if head else None,
        "meals": meals,
        "day_totals": _row(totals),
        "refused_still_on_it": refused,
    }


def get_shopping(db: Db, person_id: int, *, retailers: Sequence[str] = ()) -> dict[str, Any]:
    from ..food import shopping  # noqa: PLC0415 (avoids a cycle at import time)

    lst = shopping.list_to_show(
        db, person_id, db.clock.today_local(), retailers=retailers, clock=db.clock
    )
    if lst is None:
        return {"list": None, "items": []}
    items = db.all(
        "SELECT si.id, ing.name, si.grams_needed, si.packs, ing.pack_label, si.aisle, "
        "COALESCE((SELECT t.ticked FROM shopping_ticks t WHERE t.item_id = si.id "
        "AND t.person_id = ? ORDER BY t.id DESC LIMIT 1), 0) AS ticked "
        "FROM shopping_items si JOIN ingredients ing ON ing.id = si.ingredient_id "
        "WHERE si.list_id = ? AND si.packs > 0 ORDER BY si.aisle, ing.name",
        (person_id, lst["id"]),
    )
    listed = _row(lst, drop=("person_id", "owner_ids"))
    # what the list counts on being at home already, and so buys none of
    home = [i.name for i in shopping.at_home(db, int(lst["id"]))]
    return {"list": listed, "items": _rows(items), "at_home": home}


def get_kitchen(db: Db, person_id: int, *, retailers: Sequence[str] = ()) -> dict[str, Any]:
    """What Tiffin believes is already in the kitchen: leftover credit, and anything ticked
    on the current list that has not been cooked from yet.

    "I have chicken and rice, what can I make" can only be answered from a tool, and none of the
    read tools knew what was already at home - the chat could see the menu and the shopping list
    but not the one fact both of those are built from. This
    wraps the same `current_leftovers` balance `/shopping` and the page trust, not a new store;
    the name-lookup and tick-reading SQL lives in `food.shopping` (person-scoped by the leftovers
    and shopping_ticks rows it starts from), not repeated here.
    """
    from ..food import shopping  # noqa: PLC0415 (avoids a cycle at import time)

    today = db.clock.today_local()
    lst = shopping.list_to_show(db, person_id, today, retailers=retailers, clock=db.clock)
    return {
        "leftovers": shopping.leftover_lines(db, person_id, today),
        "ticked_not_yet_cooked": shopping.ticked_uncooked(db, person_id, lst),
    }


# ---------------------------------------------------------------- writes (intents only)
def _intent(
    db: Db, person_id: int, kind: str, payload: Mapping[str, Any], model_used: str | None
) -> dict[str, Any]:
    intent_id = db.insert(
        "intents",
        person_id=person_id,
        kind=kind,
        payload_json=json.dumps(dict(payload), ensure_ascii=False, sort_keys=True),
        model_used=model_used,
    )
    return {
        "intent_id": intent_id,
        "kind": kind,
        "payload": dict(payload),
        "status": "pending",
        "needs_tap": True,
    }


def log_offplan(
    db: Db,
    person_id: int,
    description: str,
    day: str | None = None,
    *,
    model_used: str | None = None,
) -> dict[str, Any]:
    text = str(description).strip()
    if not text:
        raise ToolError("description must not be empty")
    return _intent(
        db, person_id, "log_offplan", {"description": text[:500], "day": _day(db, day)}, model_used
    )


def confirm_day(
    db: Db,
    person_id: int,
    as_planned: bool,
    day: str | None = None,
    note: str | None = None,
    *,
    model_used: str | None = None,
) -> dict[str, Any]:
    payload = {
        "day": _day(db, day),
        "as_planned": bool(as_planned),
        "note": (str(note).strip()[:500] or None) if note else None,
    }
    return _intent(db, person_id, "confirm_day", payload, model_used)


def rate_dish(
    db: Db,
    person_id: int,
    recipe_id: int,
    score: int,
    note: str | None = None,
    *,
    model_used: str | None = None,
) -> dict[str, Any]:
    if not 1 <= int(score) <= 5:
        raise ToolError("score must be between 1 and 5")
    known = db.one(
        "SELECT rc.name FROM menus m JOIN menu_days md ON md.menu_id = m.id "
        "JOIN meal_slots ms ON ms.menu_day_id = md.id JOIN recipes rc ON rc.id = ms.recipe_id "
        "WHERE m.person_id = ? AND ms.recipe_id = ? LIMIT 1",
        (person_id, int(recipe_id)),
    )
    if known is None:
        raise ToolError(f"recipe {recipe_id} is not on any of your menus")
    payload = {
        "recipe_id": int(recipe_id),
        "name": known["name"],
        "score": int(score),
        "note": (str(note).strip()[:500] or None) if note else None,
    }
    return _intent(db, person_id, "rate_dish", payload, model_used)


def set_pause(
    db: Db,
    person_id: int,
    start_day: str | None = None,
    end_day: str | None = None,
    reason: str | None = None,
    *,
    model_used: str | None = None,
) -> dict[str, Any]:
    start = _day(db, start_day)
    end = _day(db, end_day) if end_day else start
    if end < start:
        raise ToolError("end_day is before start_day")
    payload = {
        "start_day": start,
        "end_day": end,
        "reason": (str(reason).strip()[:200] or None) if reason else None,
    }
    return _intent(db, person_id, "set_pause", payload, model_used)


PREF_LISTS = {
    "avoid": "refused_foods",
    "likes": "liked_foods",
    "pantry": "pantry_json",
}


def set_food_prefs(
    db: Db,
    person_id: int,
    list_name: str,
    add: list[str] | None = None,
    remove: list[str] | None = None,
    *,
    model_used: str | None = None,
) -> dict[str, Any]:
    """Amend one of the three food lists, as a card the person taps.

    "Avoid chickpeas pls" had no tool at all: the model could read every menu, recipe and
    shopping list and could not act on the plainest food sentence there is. It answered by
    telling the person to go and use /set themselves, which is a correct answer and a bad one.
    """
    column = PREF_LISTS.get(str(list_name).strip().lower())
    if column is None:
        raise ToolError(f"list must be one of {', '.join(sorted(PREF_LISTS))}")
    added = _food_words(add)
    removed = _food_words(remove)
    if not added and not removed:
        raise ToolError("name at least one food to add or remove")
    overlap = {x.casefold() for x in added} & {x.casefold() for x in removed}
    if overlap:
        raise ToolError(f"{', '.join(sorted(overlap))} is in both add and remove")
    payload = {"list": str(list_name).strip().lower(), "add": added, "remove": removed}
    return _intent(db, person_id, "set_food_prefs", payload, model_used)


def _food_words(values: Any) -> list[str]:
    """Trim, drop blanks and duplicates, keep the person's own spelling and order."""
    if values is None:
        return []
    if isinstance(values, str):  # a model answering "chickpeas" rather than ["chickpeas"]
        values = [values]
    if not isinstance(values, list):
        raise ToolError("add and remove take a list of foods")
    out: list[str] = []
    seen: set[str] = set()
    for v in values:
        word = str(v).strip()[:60]
        if word and word.casefold() not in seen:
            seen.add(word.casefold())
            out.append(word)
    return out[:20]


# ---------------------------------------------------------------- the registry
TOOLS: tuple[Tool, ...] = (
    Tool(
        "get_targets",
        "This week's calorie and macro targets (kcal, protein, fat, carbs, fibre) for the person.",
        _params({"day": _day_prop("ISO date inside the week; default today")}),
        get_targets,
    ),
    Tool(
        "get_day",
        "One day: confirmation status and intake, the day's meals from the menu, "
        "and any off-plan estimates.",
        _params({"day": _day_prop("ISO date; default today")}),
        get_day,
    ),
    Tool(
        "get_week",
        "A week's summary: confirmed and usable days, trend delta, sessions, "
        "the days and the targets.",
        _params({"week": _day_prop("any ISO date in the week; default this week")}),
        get_week,
    ),
    Tool(
        "get_trend",
        "The smoothed weight trend (kg) over the last N days and its change.",
        _params({"days": {"type": "integer", "minimum": 1, "maximum": 365, "default": 14}}),
        get_trend,
    ),
    Tool(
        "get_sessions",
        "Planned training sessions, matched or confirmed sessions and recorded activities "
        "for a week.",
        _params({"week": _day_prop("any ISO date in the week; default this week")}),
        get_sessions,
    ),
    Tool(
        "get_menu",
        "The accepted menu for a day: each meal with recipe, portion, computed macros, "
        "ingredients and method steps.",
        _params({"day": _day_prop("ISO date; default today")}),
        get_menu,
    ),
    Tool(
        "get_shopping",
        "The latest shopping list with pack counts, aisles and ticks, and what it counts on "
        "being at home already (at_home), which it does not buy.",
        _params({}),
        get_shopping,
        shops=True,
    ),
    Tool(
        "get_kitchen",
        "What is already in the kitchen: leftover credit by food, and anything ticked on the "
        "current shopping list but not yet cooked from. Use for questions like 'what can I "
        "make with what I've got'.",
        _params({}),
        get_kitchen,
        shops=True,
    ),
    Tool(
        "log_offplan",
        "Queue an off-plan food or drink for estimation; the person confirms by tap.",
        _params(
            {
                "description": {"type": "string", "minLength": 1, "maxLength": 500},
                "day": _day_prop("ISO date; default today"),
            },
            ("description",),
        ),
        log_offplan,
        writes=True,
    ),
    Tool(
        "confirm_day",
        "Queue a day confirmation (as planned or not); the person confirms by tap.",
        _params(
            {
                "as_planned": {"type": "boolean"},
                "day": _day_prop("ISO date; default today"),
                "note": {"type": ["string", "null"], "maxLength": 500},
            },
            ("as_planned",),
        ),
        confirm_day,
        writes=True,
    ),
    Tool(
        "rate_dish",
        "Queue a 1-5 rating for a recipe from the person's menus; confirmed by tap.",
        _params(
            {
                "recipe_id": {"type": "integer", "minimum": 1},
                "score": {"type": "integer", "minimum": 1, "maximum": 5},
                "note": {"type": ["string", "null"], "maxLength": 500},
            },
            ("recipe_id", "score"),
        ),
        rate_dish,
        writes=True,
    ),
    Tool(
        "set_pause",
        "Queue a pause of tracking between two dates; confirmed by tap.",
        _params(
            {
                "start_day": _day_prop("ISO date; default today"),
                "end_day": _day_prop("ISO date; default start_day"),
                "reason": {"type": ["string", "null"], "maxLength": 200},
            }
        ),
        set_pause,
        writes=True,
    ),
    Tool(
        "set_food_prefs",
        "Amend a food list: avoid (never in a menu), likes (more often), pantry (never on "
        "the shop). Use when someone says what they do or do not eat. Confirmed by a tap, "
        "and changes future menus only.",
        _params(
            {
                "list_name": {"type": "string", "enum": sorted(PREF_LISTS)},
                "add": {"type": "array", "items": {"type": "string", "maxLength": 60}},
                "remove": {"type": "array", "items": {"type": "string", "maxLength": 60}},
            },
            ("list_name",),
        ),
        set_food_prefs,
        writes=True,
    ),
)
BY_NAME: dict[str, Tool] = {t.name: t for t in TOOLS}
READ_TOOLS: tuple[str, ...] = tuple(t.name for t in TOOLS if not t.writes)
WRITE_TOOLS: tuple[str, ...] = tuple(t.name for t in TOOLS if t.writes)
_IGNORED_ARGS = ("person_id", "model_used")


def to_mcp() -> list[dict[str, Any]]:
    return [
        {"name": t.name, "description": t.description, "inputSchema": t.parameters} for t in TOOLS
    ]


def call(
    db: Db,
    person_id: int,
    name: str,
    args: Mapping[str, Any] | None = None,
    *,
    model_used: str | None = None,
    retailers: Sequence[str] = (),
) -> dict[str, Any]:
    """Run one tool for the given person. Model-supplied person/model fields are dropped."""
    tool = BY_NAME.get(name)
    if tool is None:
        raise ToolError(f"unknown tool {name!r}")
    clean = {k: v for k, v in dict(args or {}).items() if k not in _IGNORED_ARGS}
    from jsonschema import ValidationError, validate  # noqa: PLC0415

    try:
        validate(clean, tool.parameters)
    except ValidationError as e:
        raise ToolError(f"{name}: {e.message}"[:300]) from None
    if tool.writes:
        return tool.handler(db, person_id, model_used=model_used, **clean)
    if tool.shops:
        return tool.handler(db, person_id, retailers=tuple(retailers), **clean)
    return tool.handler(db, person_id, **clean)


class BoundTools:
    """The registry bound to one person's identity; what a backend's tool loop calls."""

    def __init__(
        self,
        db: Db,
        person_id: int,
        *,
        model_used: str | None = None,
        retailers: Sequence[str] = (),
    ):
        self.db = db
        self.person_id = person_id
        self.model_used = model_used
        self.retailers = tuple(retailers)

    def names(self) -> list[str]:
        return [t.name for t in TOOLS]

    def to_mcp(self) -> list[dict[str, Any]]:
        return to_mcp()

    def call(self, name: str, args: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return call(
            self.db,
            self.person_id,
            name,
            args,
            model_used=self.model_used,
            retailers=self.retailers,
        )
