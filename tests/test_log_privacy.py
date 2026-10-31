"""Exception reporting identifies the failing code without writing private values."""

from __future__ import annotations

import json
import logging
import sys

import pytest

from tiffin.errors import ModelError
from tiffin.log import JsonFormatter, SafeConsoleFormatter
from tiffin.model.claude_cli import parse_envelope


def test_exception_value_is_omitted_from_json_and_console_logs() -> None:
    private = "Sam ate a private meal"
    try:
        raise ValueError(private)
    except ValueError:
        record = logging.LogRecord(
            "tiffin.test", logging.ERROR, __file__, 1, "failed", (), sys.exc_info()
        )
    structured = json.loads(JsonFormatter().format(record))
    console = SafeConsoleFormatter("%(message)s").format(record)
    assert structured["exc"].startswith("ValueError:")
    assert "test_log_privacy.py" in structured["exc"]
    assert private not in json.dumps(structured)
    assert private not in console


def test_claude_error_does_not_repeat_model_output() -> None:
    private = "Sam's private meal"
    with pytest.raises(ModelError) as exc:
        parse_envelope(private, "", 1)
    assert private not in str(exc.value)
