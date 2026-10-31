"""JSON schemas for every model task: config/schemas/<name>.json, validated with jsonschema.

The schemas carry the shape rules the model must obey (indexes and grams, never totals; line and
step caps). Validation failures raise ModelInvalid with the path and the message, never the
payload, so the error can be logged and carried into a retry prompt.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

from ..config import REPO_ROOT
from ..errors import ModelInvalid

SCHEMAS_DIR = REPO_ROOT / "config" / "schemas"
NAMES = (
    "menu_proposal",
    "one_dish",
    "dish_options",
    "offplan_estimate",
    "sunday_prose",
    "plan_parse",
    "intent",
)


def _validator() -> Any:
    import jsonschema  # noqa: PLC0415

    return jsonschema.Draft202012Validator


def schema_path(name: str, schemas_dir: Path = SCHEMAS_DIR) -> Path:
    if name not in NAMES:
        raise KeyError(f"unknown schema: {name!r}")
    return schemas_dir / f"{name}.json"


@cache
def _load_cached(path: str) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    _validator().check_schema(data)
    return data


def load(name: str, schemas_dir: Path = SCHEMAS_DIR) -> dict[str, Any]:
    """The schema as a dict (cached, checked once against the draft meta-schema)."""
    return _load_cached(str(schema_path(name, schemas_dir)))


def for_model(schema: dict[str, Any]) -> dict[str, Any]:
    """Omit `$schema` for model backends that cannot resolve the meta-schema URI."""
    return {k: v for k, v in schema.items() if k != "$schema"}


def _without_the_payload(err: Any) -> str:
    """Remove the submitted value from validation errors before logging or retrying."""
    message = str(err.message)
    for form in (repr(err.instance), str(err.instance)):
        if form and len(form) > 1 and form in message:
            message = message.replace(form, "<value>")
    return message


def errors(schema: dict[str, Any], data: Any) -> list[str]:
    """Human-readable validation errors, best first, empty when the payload is valid."""
    found = sorted(_validator()(schema).iter_errors(data), key=lambda e: list(e.absolute_path))
    out: list[str] = []
    for err in found[:5]:
        where = "/".join(str(p) for p in err.absolute_path) or "(root)"
        out.append(f"{where}: {_without_the_payload(err)}"[:200])
    return out


def validate_data(schema: dict[str, Any], data: Any, *, name: str = "payload") -> Any:
    """Return `data` when it matches `schema`; raise ModelInvalid naming the first errors."""
    problems = errors(schema, data)
    if problems:
        raise ModelInvalid(f"{name} failed schema validation: " + "; ".join(problems))
    return data


def validate(name: str, data: Any) -> Any:
    return validate_data(load(name), data, name=name)
