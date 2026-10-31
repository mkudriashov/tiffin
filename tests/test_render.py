from __future__ import annotations

from pathlib import Path

import pytest

from tiffin.telegram import render as r


def ctx(**over: str) -> dict[str, str]:
    base = {
        "day_label": "Tue 8 Sep",
        "kcal": "2,250",
        "protein_g": "202",
        "meals_line": "First up: overnight oats.",
    }
    base.update(over)
    return base


def test_morning_targets_renders_and_drops_blank_lines() -> None:
    text = r.render("morning_targets", ctx())
    lines = text.splitlines()
    # two lines since 26 Sep: the target, then the first meal
    assert lines[0] == "<b>Tue 8 Sep</b> · 2,250 kcal · protein at least 202 g"
    assert lines[1] == "First up: overnight oats."
    assert len(lines) == 2
    for tone in ("blunt", "warm"):
        assert len(r.render("morning_targets", ctx(), tone).splitlines()) == 2, tone


def test_missing_field_raises() -> None:
    c = ctx()
    del c["meals_line"]
    with pytest.raises(r.RenderError, match="meals_line"):
        r.render("morning_targets", c)


def test_too_many_lines_and_bad_tags_and_emoji() -> None:
    with pytest.raises(r.RenderError, match="lines"):
        r.check_text("\n".join(f"l{i}" for i in range(11)))
    with pytest.raises(r.RenderError, match="tag"):
        r.check_text("<u>no</u>")
    with pytest.raises(r.RenderError, match="emoji"):
        r.check_text("great job 🎉")
    assert (
        r.check_text("<b>ok</b> · <a href='x'>page</a>\n\n\nline")
        == "<b>ok</b> · <a href='x'>page</a>\nline"
    )


def test_tone_variant_is_picked_when_present(tmp_path: Path) -> None:
    (tmp_path / "hello.html").write_text("Hello {name}", encoding="utf-8")
    (tmp_path / "hello.blunt.html").write_text("{name}.", encoding="utf-8")
    assert r.render("hello", {"name": "Alex"}, "neutral", templates_dir=tmp_path) == "Hello Alex"
    assert r.render("hello", {"name": "Alex"}, "blunt", templates_dir=tmp_path) == "Alex."
    with pytest.raises(r.RenderError, match="no template"):
        r.render("nope", {}, templates_dir=tmp_path)


def test_esc_and_units() -> None:
    assert r.esc("<a & b>") == "&lt;a &amp; b&gt;"
    assert r.kg(100.44) == "100.4 kg" and r.signed_kg(-0.26) == "-0.3 kg"


def test_a_persons_emoji_is_scrubbed_rather_than_raising() -> None:
    from tiffin.telegram.render import RenderError, check_text, fit, strip_emoji

    note = "kebab and chips \U0001f32f"
    assert "\U0001f32f" not in strip_emoji(note)
    assert strip_emoji(note).strip() == "kebab and chips"
    with pytest.raises(RenderError):
        check_text(note, kind="intent")
    assert "kebab and chips" in fit(note, kind="intent"), "fit repairs where check_text refuses"


def test_fit_trims_an_over_long_model_reply_instead_of_sending_nothing() -> None:
    from tiffin.telegram.render import fit

    got = fit("\n".join(f"line {n}" for n in range(30)), max_lines=8, kind="reply")
    assert len(got.splitlines()) == 8
    assert got.startswith("line 0")
    assert got.endswith("...")


def test_fit_never_returns_an_empty_message() -> None:
    from tiffin.telegram.render import fit

    assert fit("\U0001f600\U0001f600", kind="reply").strip()


def test_the_tiffin_own_templates_stay_strict() -> None:
    """The no-emoji rule is about what Tiffin writes, not what a person types."""
    from tiffin.telegram.render import RenderError, check_text

    with pytest.raises(RenderError):
        check_text("Well done \U0001f389", kind="morning_targets")
