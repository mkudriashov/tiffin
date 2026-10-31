"""Every model task the code can ask for must have a budget in config.

`Gateway.json_task` looks the task's budget up by name before it does anything else, and
`Config.budget` raises `ConfigError` for a name that is not in `[budgets]`. This test reads
task names from the source because a repeated list would go stale.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tiffin import config as cfg

SRC = Path(__file__).resolve().parents[1] / "src" / "tiffin"


def task_names() -> set[str]:
    """The first-argument literal of every gateway .*_task( call under src/."""
    found: set[str] = set()
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if not node.func.attr.endswith("_task") or node.func.attr.startswith("_"):
                continue
            if node.args and isinstance(node.args[0], ast.Constant):
                value = node.args[0].value
                if isinstance(value, str):
                    found.add(value)
    return found


def test_the_source_really_does_ask_for_tasks() -> None:
    names = task_names()
    assert {"menu_propose", "offplan_estimate"} <= names, names


@pytest.mark.parametrize("task", sorted(task_names()))
def test_every_task_the_code_asks_for_has_a_budget(task: str, tmp_path) -> None:
    config = cfg.load(root=tmp_path / "root", environ={})
    budget = config.budget(task)  # raises ConfigError when the name is missing
    assert budget.timeout_s > 0 and budget.turns >= 1
    assert float(config.get(f"budgets.{task}")[2]) > 0, "a cost figure, for when caps are on"


def test_no_cost_cap_unless_asked_for(tmp_path) -> None:
    from tiffin.config import Budget
    from tiffin.model.claude_cli import ClaudeBackend

    config = cfg.load(root=tmp_path / "root", environ={})
    budget = config.budget("menu_one_dish")
    assert budget.usd is None and budget.timeout_s == 180 and budget.turns == 3
    backend = ClaudeBackend("claude", "opus", tmp_path)
    assert "--max-budget-usd" not in backend.base_argv(budget)
    assert "--max-turns" in backend.base_argv(budget)
    capped = backend.base_argv(Budget(180, 3, 2.0))
    assert capped[capped.index("--max-budget-usd") + 1] == "2.000", "and back when asked for"
