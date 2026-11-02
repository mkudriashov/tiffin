"""The minutes box on Today is styled: number inputs share the other fields' look."""

from __future__ import annotations

from pathlib import Path

CSS = (Path(__file__).resolve().parents[1] / "src/tiffin/web/static/app.css").read_text(
    encoding="utf-8"
)


def test_number_inputs_share_the_field_style_and_the_session_form_wraps() -> None:
    assert 'input[type="number"] {' in CSS.split('input[type="date"]', 1)[1][:120]
    assert ".session-form { display: flex; flex-wrap: wrap;" in CSS
    assert '.session-form input[type="number"] { width: 7rem; }' in CSS
