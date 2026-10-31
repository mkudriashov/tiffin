"""Prompt builders: every template renders from plain data, deterministically, persona first."""

from __future__ import annotations

import pytest

from tiffin.model import prompts

# numbered as catalogue.prompt_lines numbers them: the prompt does not number them again
INGREDIENTS = [
    "1. Chicken breast, raw - 165 kcal, 31 P, 3.6 F, 0 C, 0 fibre /100 g (pack 500 g)",
    "2. Basmati rice, dry - 350 kcal, 8 P, 1 F, 78 C, 2 fibre /100 g (pack 1 kg)",
]
DAYS = [
    {
        "day": "2026-09-08",
        "slots": ["breakfast", "lunch", "dinner"],
        "kcal": 2100,
        "protein_g": 200,
    },
    {
        "day": "2026-09-09",
        "slots": ["breakfast", "lunch", "dinner"],
        "kcal": 2100,
        "protein_g": 200,
    },
]


def all_prompts() -> dict[str, str]:
    return {
        "menu_propose": prompts.menu_propose(
            ingredients=INGREDIENTS,
            days=DAYS,
            variety={"breakfast": "same", "lunch": "rotate", "dinner": "different"},
            favourites=["Chicken and rice"],
            blocked=["Liver"],
            recent=["Beef chilli"],
            leftovers=["Basmati rice 400 g"],
        ),
        "offplan_estimate": prompts.offplan_estimate("two pints of lager and a bag of crisps"),
        "sunday_prose": prompts.sunday_prose({"trend_kg": 99.8, "confirmed_days": 6}, tone="warm"),
        "plan_parse": prompts.plan_parse("Mon climb 90 at 18:30, Wed ride 60, Sat ropes"),
        "intent": prompts.intent("had two beers last night", today="2026-09-07"),
    }


def test_every_task_prompt_starts_with_the_persona_and_is_deterministic() -> None:
    first = all_prompts()
    second = all_prompts()
    persona = prompts.persona()
    assert "British English" in persona and "No emoji" in persona
    for name, text in first.items():
        assert text.startswith(persona), name
        assert "TASK:" in text and "[[" not in text, name
        assert text == second[name], name


def test_numbered_lists_carry_indexes_not_names_rule() -> None:
    menu = all_prompts()["menu_propose"]
    assert "1. Chicken breast" in menu and "2. Basmati rice" in menu
    assert "Never write kcal, macros or totals" in menu
    assert "2026-09-08: breakfast, lunch, dinner - target 2100 kcal, 200 g protein" in menu
    assert "breakfast same, dinner different, lunch rotate" in menu


def test_facts_are_rendered_sorted_and_free_text_is_cleaned() -> None:
    text = prompts.sunday_prose({"z": 1, "a": "two\x00three\t four", "none": None})
    assert "a: two three four\nz: 1" in text and "none" not in text.split("Facts")[1][:40]
    assert prompts.clean("a" * 5000) == "a" * prompts.MAX_TEXT_CHARS


def test_tone_is_validated() -> None:
    with pytest.raises(ValueError, match="tone"):
        prompts.sunday_prose({"a": 1}, tone="cheerful")
    with pytest.raises(ValueError, match="tone"):
        prompts.chat_system(tone="shouty")


def test_chat_system_carries_persona_and_scoping_rules() -> None:
    text = prompts.chat_system(tone="neutral", max_lines=5)
    assert text.startswith(prompts.persona())
    assert "scoped to that person" in text and "needs their tap" in text
    assert "never more than 5 short lines" in text and "Be brief" in text


def test_render_refuses_missing_or_unused_fields() -> None:
    with pytest.raises(prompts.PromptError, match="missing"):
        prompts.render("sunday_prose", {"facts": "x"})
    with pytest.raises(prompts.PromptError, match="unused"):
        prompts.render("sunday_prose", {"facts": "x", "tone": "neutral", "extra": 1})


def test_no_prompt_calls_food_good_or_bad() -> None:
    for name, text in all_prompts().items():
        body = text.lower()
        assert "good food" not in body and "bad food" not in body, name
        assert "cheat" not in body, name
