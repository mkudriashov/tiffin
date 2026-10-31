"""Prompt builders: config/prompts/<task>.md with [[field]] placeholders, persona preamble first.

Every builder takes plain data (dicts, lists, strings) and returns the prompt string. Numbered
lists are rendered here so the model answers with indexes (index-not-quote); the builders never
compute a total. Rendering is deterministic: same data, same bytes.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from functools import cache
from pathlib import Path
from typing import Any

from ..config import REPO_ROOT

PROMPTS_DIR = REPO_ROOT / "config" / "prompts"
TONES = ("blunt", "neutral", "warm")
MAX_TEXT_CHARS = 2000
_PLACEHOLDER = re.compile(r"\[\[([a-z_]+)\]\]")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class PromptError(ValueError):
    """A template field is missing or unused: a bug in the builder, never in the data."""


@cache
def _read(path: str) -> str:
    return Path(path).read_text(encoding="utf-8").strip()


def template(name: str, prompts_dir: Path = PROMPTS_DIR) -> str:
    return _read(str(prompts_dir / f"{name}.md"))


def persona(prompts_dir: Path = PROMPTS_DIR) -> str:
    return template("persona", prompts_dir)


def render(name: str, fields: Mapping[str, object], *, prompts_dir: Path = PROMPTS_DIR) -> str:
    """Fill [[field]] placeholders. Every placeholder must be given and every field used."""
    text = template(name, prompts_dir)
    wanted = set(_PLACEHOLDER.findall(text))
    given = set(fields)
    if wanted - given:
        raise PromptError(f"{name}: missing fields {sorted(wanted - given)}")
    if given - wanted:
        raise PromptError(f"{name}: unused fields {sorted(given - wanted)}")
    return _PLACEHOLDER.sub(lambda m: str(fields[m.group(1)]), text)


def task_prompt(name: str, fields: Mapping[str, object], *, prompts_dir: Path = PROMPTS_DIR) -> str:
    return persona(prompts_dir) + "\n\n" + render(name, fields, prompts_dir=prompts_dir)


# ---------------------------------------------------------------- rendering helpers
def clean(text: object, limit: int = MAX_TEXT_CHARS) -> str:
    """Free text from a person: control characters out, whitespace collapsed, length capped."""
    s = _CONTROL.sub(" ", str(text))
    s = re.sub(r"[ \t]+", " ", s).strip()
    return s[:limit]


def numbered(items: Iterable[object], *, empty: str = "(none)") -> str:
    lines = [f"{i}. {clean(item, 300)}" for i, item in enumerate(items, start=1)]
    return "\n".join(lines) if lines else empty


def listing(items: Iterable[object], *, empty: str = "(none)") -> str:
    parts = [clean(item, 120) for item in items]
    return ", ".join(p for p in parts if p) or empty


def foods(lines: Iterable[object]) -> str:
    """A food list already numbered by `catalogue.prompt_lines`, one a line."""
    return "\n".join(clean(x, 300) for x in lines) or "(none)"


def dishes(names: Iterable[object], *, empty: str = "(none)") -> str:
    """Dish names, which hold commas of their own, kept apart by semicolons."""
    parts = [clean(n, 160) for n in names]
    return "; ".join(p for p in parts if p) or empty


def notes_line(notes: Iterable[object]) -> str:
    """Standing notes, whole sentences: `listing` cuts at 120 characters and joins on commas,
    which a note like "raw vegetables only 1-2 kinds per dish, so no salads" holds itself."""
    parts = [clean(n, 200) for n in notes]
    return "; ".join(p for p in parts if p) or "(none)"


def facts_block(facts: Mapping[str, object]) -> str:
    """One `key: value` line per fact, keys sorted so the prompt is stable."""
    lines = [f"{k}: {clean(v, 300)}" for k, v in sorted(facts.items()) if v is not None]
    return "\n".join(lines) if lines else "(no facts)"


def check_tone(tone: str) -> str:
    if tone not in TONES:
        raise ValueError(f"tone must be one of {TONES}, got {tone!r}")
    return tone


def _day_line(day: Mapping[str, Any]) -> str:
    # every caller passes the meals already joined ("breakfast, lunch"), and `listing` walked
    # read "b, r, e, a, k, f, a, s, t, ,, l, u, n, c, h"
    raw = day.get("slots") or []
    slots = clean(raw, 300) if isinstance(raw, str) else listing(raw)
    parts = [f"{day['day']}: {slots}"]
    targets = [
        f"{day[k]} {unit}"
        for k, unit in (
            ("kcal", "kcal"),
            ("protein_g", "g protein"),
            ("fat_g", "g fat"),
            ("carb_g", "g carbs"),
            ("fibre_g", "g fibre"),
        )
        if day.get(k) is not None
    ]
    if targets:
        parts.append("target " + ", ".join(targets))
    if day.get("also"):
        # a slot only one of them eats on a shared weekend, extra on top of the shared figures
        parts.append("also: " + clean(day["also"], 300))
    return " - ".join(parts)


# ---------------------------------------------------------------- builders
def menu_propose(
    *,
    ingredients: Iterable[str],
    days: Iterable[Mapping[str, Any]],
    variety: Mapping[str, str],
    favourites: Iterable[str] = (),
    blocked: Iterable[str] = (),
    recent: Iterable[str] = (),
    leftovers: Iterable[str] = (),
    equipment: Iterable[str] = (),
    asked_for: str = "",
    weekend: bool = False,
    taste: str = "",
    examples: Iterable[str] = (),
    dinner_gap: Iterable[str] = (),
    novelty: str = "",
    notes: Iterable[str] = (),
    packs: Iterable[str] = (),
    mains: str = "",
    batched: bool = True,
    meals: Iterable[str] = (),
    week: Iterable[str] = (),
    minutes: int = 30,
) -> str:
    """`ingredients` are already-described catalogue lines (name, per-100 g, pack); `days` are
    {day, slots, kcal, protein_g, fat_g}; `variety` maps slot -> same|rotate|different.

    `notes` are what they want from every menu (`prefs.standing_notes`), where `asked_for` is
    what they asked of this one.

    `examples` are a few of their best-loved dishes written out (food, grams, method) - a
    few-shot guide to their taste; `dinner_gap` the dinners of the last six days, never a dinner
    again this time; `novelty` the line asking for about one new dish a week.

    `weekend` swaps in the weekend brief - healthy versions of indulgent food, with the room two
    `taste` says what the person's treats lean to (food/weekend.taste_line).

    `packs` are the shop window's open packs by their number in `ingredients`, and `mains` the
    line on how many main proteins the window keeps to (food/packs.py).

    `week` is how their week runs, in their own answers (food/weekplan.py: lunch from the night
    before, a no-cook day, breakfasts in turn, full steps), and `minutes` a main meal's longest
    method.
    """
    fields = {
        "ingredients": foods(ingredients),
        "days": "\n".join(_day_line(d) for d in days) or "(no days)",
        "variety": listing(f"{k} {v}" for k, v in sorted(variety.items())),
        "equipment": listing(equipment) or "an oven and a hob (nothing else stated)",
        "favourites": dishes(favourites),
        "blocked": dishes(blocked),
        "recent": dishes(recent),
        "leftovers": listing(leftovers),
        "asked_for": clean(asked_for, 300) or "(nothing in particular)",
        "examples": "; ".join(clean(e, 400) for e in examples) or "(none yet)",
        "dinner_gap": dishes(dinner_gap),
        "novelty": clean(novelty, 300) or "(nothing asked)",
        "notes": notes_line(notes),
        "packs": "; ".join(clean(p, 200) for p in packs) or "(none open)",
        "mains": clean(mains, 400) or "(no limit)",
        "meals": "; ".join(clean(m, 120) for m in meals) or "(even across the meals)",
    }
    if not weekend:
        # how their week runs, in their own answers (food/weekplan.py); `minutes` a main meal's
        # longest method, theirs where they gave one. The weekend has its own brief
        fields["week"] = " ".join(clean(w, 300) for w in week) or "(nothing more)"
        fields["minutes"] = str(int(minutes))
        # someone who cooks each day is not batch cooking, and was told they were
        fields["boxed"] = (
            "lunch and dinner are batch-cooked and eaten from flat meal-prep boxes"
            if batched
            else "lunch and dinner are cooked on the day, and lunch is often eaten from a box at "
            "work"
        )
    if weekend:
        fields["taste"] = clean(taste, 400) or "a mix of sweet and savoury treats."
    return task_prompt("menu_weekend" if weekend else "menu_propose", fields)


#: what a meal is held to, by kind: the same limits `recipes.simplicity_errors` checks in code
ONE_DISH_RULES = {
    "small": (
        "simple, healthy food made half-awake or at a desk: at most 6 ingredients (salt, pepper "
        "and oil do not count), at most 4 steps and at most 10 minutes, one appliance at most - "
        "the microwave or a single pan. Mostly assembly: stir, top, warm."
    ),
    "main": (
        "simple, healthy food for someone tired after work: at most 8 ingredients (salt, pepper "
        "and oil do not count), at most 30 minutes from start to plate, at most 6 method steps, "
        "one pan or one tray, at most two appliances, one thing at a time (never 'meanwhile'). "
        "Built around ONE cooked centrepiece - a traybake, a stir fry, a thick curry, a chilli, a "
        "rice or pasta dish, a salad. No soups or broths: it is batch-cooked into flat boxes. "
        "Protein-rich, with a proper portion of a carbohydrate food and a deliberate source of fat "
        "that belongs in the dish - never a food added only for its numbers (cheddar on tuna "
        "pasta). A known pairing, properly seasoned with garlic, herbs, spices, citrus or a sauce: "
        "a dish people cook because it tastes good."
    ),
    "weekend": (
        "the weekend, so a treat: a healthy version of indulgent food, high in protein, cooked "
        "properly with no limit on effort - at most 12 ingredients, 12 steps and 2 hours."
    ),
}


def menu_one_dish(
    *,
    ingredients: Iterable[str],
    meal: str,
    target: str,
    going: str,
    rest: Iterable[str] = (),
    equipment: Iterable[str] = (),
    favourites: Iterable[str] = (),
    blocked: Iterable[str] = (),
    recent: Iterable[str] = (),
    asked_for: str = "",
    kind: str = "main",
    notes: Iterable[str] = (),
    packs: Iterable[str] = (),
) -> str:
    """Two or three dishes for one meal (food/newdish.py): the rest of the day is named so the
    options do not clash with it, and is not offered as "do not repeat" - only the one going
    out is. `kind` is small (breakfast, snack, pre-bed), main or weekend."""
    return task_prompt(
        "menu_one_dish",
        {
            # already numbered by `catalogue.prompt_lines`: numbering them again read "3. 3. ..."
            "ingredients": "\n".join(clean(x, 300) for x in ingredients) or "(none)",
            "meal": clean(meal, 200),
            "target": clean(target, 200) or "(none)",
            "going": clean(going, 120),
            "rest": listing(rest),
            "equipment": listing(equipment) or "an oven and a hob (nothing else stated)",
            "favourites": dishes(favourites),
            "blocked": dishes(blocked),
            "recent": dishes(recent),
            "asked_for": clean(asked_for, 300) or "(nothing in particular)",
            "rules": ONE_DISH_RULES.get(kind, ONE_DISH_RULES["main"]),
            "notes": notes_line(notes),
            "packs": "; ".join(clean(p, 200) for p in packs) or "(none open)",
        },
    )


def menu_choices(
    *,
    ingredients: Iterable[str],
    meal: str,
    count: int,
    target: str,
    known: Iterable[str] = (),
    equipment: Iterable[str] = (),
    favourites: Iterable[str] = (),
    blocked: Iterable[str] = (),
    recent: Iterable[str] = (),
    kind: str = "main",
    notes: Iterable[str] = (),
    mains: Iterable[str] = (),
) -> str:
    """A week of dishes for one meal, to choose from each weekday (food/choices.py): the dishes
    they know that are offered beside them are named, so none is written twice. `kind` is small
    (breakfast, snack, pre-bed) or main."""
    return task_prompt(
        "menu_choices",
        {
            # already numbered by `catalogue.prompt_lines`
            "ingredients": "\n".join(clean(x, 300) for x in ingredients) or "(none)",
            "meal": clean(meal, 200),
            "count": str(max(1, int(count))),
            "target": clean(target, 200) or "(none)",
            "known": dishes(known),
            "equipment": listing(equipment) or "an oven and a hob (nothing else stated)",
            "favourites": dishes(favourites),
            "blocked": dishes(blocked),
            "recent": dishes(recent),
            "rules": ONE_DISH_RULES.get(kind, ONE_DISH_RULES["main"]),
            "notes": notes_line(notes),
            # the week's main proteins (food/packs.py week_mains): whatever is picked cooks from
            # those packs, so the dishes differ by kind and flavour rather than by pack
            "spread": (
                "Build every dish on these main proteins and no other meat or fish: "
                + "; ".join(clean(m, 120) for m in mains)
                + ". Vary the kinds of dish, the vegetables and the flavours, not the packs"
                if list(mains)
                else "spread the main ingredients and the kinds of dish"
            ),
        },
    )


def menu_shared_weekend(
    *,
    ingredients: Iterable[str],
    days: Iterable[Mapping[str, Any]],
    favourites: Iterable[str] = (),
    blocked: Iterable[str] = (),
    recent: Iterable[str] = (),
    leftovers: Iterable[str] = (),
    equipment: Iterable[str] = (),
    asked_for: str = "",
    taste: str = "",
    dinner_gap: Iterable[str] = (),
    notes: Iterable[str] = (),
    packs: Iterable[str] = (),
    mains: str = "",
) -> str:
    """

    `notes` are both people's standing notes, unnamed: both eat every dish.

    `favourites` come in turn order: the first ones listed are this weekend's pick.

    `days` carry one ordinary day each ({day, slots, kcal, protein_g, fat_g, fibre_g}), worked
    out in code from both people's targets (`menu_shared._scale_days`) - neither person's own
    targets are named to the model.
    """
    return task_prompt(
        "menu_shared_weekend",
        {
            "ingredients": foods(ingredients),
            "days": "\n".join(_day_line(d) for d in days) or "(no days)",
            "equipment": listing(equipment) or "an oven and a hob (nothing else stated)",
            "favourites": dishes(favourites),
            "blocked": dishes(blocked),
            "recent": dishes(recent),
            "leftovers": listing(leftovers),
            "asked_for": clean(asked_for, 300) or "(nothing in particular)",
            "taste": clean(taste, 400) or "a mix of sweet and savoury treats.",
            "dinner_gap": dishes(dinner_gap),
            "notes": notes_line(notes),
            "packs": "; ".join(clean(p, 200) for p in packs) or "(none open)",
            "mains": clean(mains, 400) or "(no limit)",
        },
    )


def offplan_estimate(description: str, *, hints: str = "") -> str:
    return task_prompt(
        "offplan_estimate",
        {"description": clean(description), "hints": clean(hints) or "(none)"},
    )


def sunday_prose(facts: Mapping[str, object], *, tone: str = "neutral") -> str:
    return task_prompt("sunday_prose", {"facts": facts_block(facts), "tone": check_tone(tone)})


def plan_parse(text: str) -> str:
    return task_prompt("plan_parse", {"text": clean(text)})


def intent(text: str, *, today: str) -> str:
    return task_prompt("intent", {"text": clean(text), "today": clean(today, 10)})


def chat_system(*, tone: str = "neutral", max_lines: int = 8, commands: str = "") -> str:
    """The persona and rules appended to the system prompt of a conversation.

    `commands` is the real command list. It keeps replies tied to places and commands that
    actually exist; an invented place looks like an answer and ends nowhere.
    """
    return render(
        "chat_system",
        {
            "persona": persona(),
            "tone": check_tone(tone),
            "max_lines": max(1, min(int(max_lines), 10)),
            "commands": commands or "(none listed)",
        },
    )
