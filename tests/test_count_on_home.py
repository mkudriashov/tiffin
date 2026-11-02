"""`shopping.count_on_home`: whether a list counts on food already at home.

Off (the default): every list buys everything its menu eats, even where an earlier day of the
same shop window opened a pack. On: a pack opened earlier in the window covers a later day.
The `leftovers` rows are written either way, because the menu builder reads them.

The world is tests/test_swap.py's, cooked each day: chicken on Monday, Wednesday and Thursday.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tiffin import config as cfg
from tiffin.app import App
from tiffin.errors import ConfigError
from tiffin.food import shopping
from tiffin.secrets import Secrets

from .test_packs_home import DAILY, THU, WED, _daily
from .test_swap import _profile, week  # noqa: F401


def _menu_starting(db, pid, day) -> int:
    return int(
        db.scalar(
            "SELECT id FROM menus WHERE person_id = ? AND segment_start = ? ORDER BY id DESC",
            (pid, day.isoformat()),
        )
    )


def _chicken(items):
    return next(i for i in items if i.code == "chicken")


def test_the_default_is_off_and_buys_everything(tmp_path: Path) -> None:
    conf = cfg.load(root=tmp_path, environ={})
    assert conf.get("shopping.count_on_home") is False
    assert shopping.COUNT_ON_HOME is False


def test_off_a_later_day_buys_the_whole_of_what_it_eats(week) -> None:  # noqa: F811
    db, pid, _base, r, _clock = week
    _daily(db, pid, r)
    got = _chicken(shopping.build(db, _menu_starting(db, pid, WED)))
    # Monday opened the pack, but Wednesday's list does not count on what is left of it
    assert got.credit_g == 0.0
    assert got.grams_needed == got.grams_total > 0
    later = _chicken(shopping.build(db, _menu_starting(db, pid, THU)))
    assert later.credit_g == 0.0 and later.grams_needed == later.grams_total
    assert later.packs >= 1


def test_on_a_later_day_counts_the_pack_an_earlier_day_opened(week, count_on_home) -> None:  # noqa: F811
    db, pid, _base, r, _clock = week
    _daily(db, pid, r)
    # Wednesday: Monday's pack has the 180 g left, so nothing is bought and the list says why
    wed = _menu_starting(db, pid, WED)
    assert shopping.build(db, wed) == []
    list_id = shopping.segment_list_id(db, wed)
    assert list_id is not None
    assert "chicken" in {i.code for i in shopping.at_home(db, list_id)}
    # Thursday: 140 g left of it, 40 g more to buy, which is one more pack
    got = _chicken(shopping.build(db, _menu_starting(db, pid, THU)))
    assert got.credit_g == 140.0 and got.grams_needed == pytest.approx(got.grams_total - 140.0)
    assert got.packs == 1


def test_the_setting_changes_only_the_credit(week, monkeypatch) -> None:  # noqa: F811
    db, pid, _base, r, _clock = week
    _daily(db, pid, r)
    menu_id = _menu_starting(db, pid, THU)
    off = {i.code: i for i in shopping.build(db, menu_id)}
    monkeypatch.setattr(shopping, "COUNT_ON_HOME", True)
    on = {i.code: i for i in shopping.build(db, menu_id)}
    assert set(on) < set(off)  # on buys fewer foods: the rest are counted as at home
    assert all(off[c].grams_total == on[c].grams_total for c in on)
    assert all(i.credit_g == 0.0 for i in off.values())
    assert all(i.credit_g > 0 for i in on.values())
    assert sum(i.packs for i in on.values()) < sum(i.packs for i in off.values())


def test_off_the_leftovers_are_still_written_for_the_menu_builder(week) -> None:  # noqa: F811
    db, pid, _base, r, _clock = week
    _daily(db, pid, r)
    before = int(db.scalar("SELECT COUNT(*) FROM leftovers"))
    shopping.build(db, _menu_starting(db, pid, THU))
    assert int(db.scalar("SELECT COUNT(*) FROM leftovers")) > before


def test_off_a_replacement_menu_still_buys_everything(week) -> None:  # noqa: F811
    db, pid, _base, r, _clock = week
    _daily(db, pid, r)
    menu_id = _menu_starting(db, pid, WED)
    first = {i.code: i for i in shopping.build(db, menu_id)}
    # the same days again, as a replaced menu would be built: nothing is counted as at home
    second = {i.code: i for i in shopping.build(db, menu_id)}
    assert all(i.credit_g == 0.0 for i in second.values())
    assert {c: i.packs for c, i in second.items()} == {c: i.packs for c, i in first.items()}


def test_off_a_range_list_buys_everything_its_days_eat(week) -> None:  # noqa: F811
    db, pid, _base, r, clock = week
    _daily(db, pid, r)
    made = shopping.range_list(db, pid, WED, clock=clock)
    assert made is not None
    items = shopping.read_back(db, int(made["id"]))
    assert items and all(i.credit_g == 0.0 for i in items)
    assert all(i.grams_needed == i.grams_total for i in items)


def test_on_a_range_list_counts_packs_from_earlier_days(week, count_on_home) -> None:  # noqa: F811
    db, pid, _base, r, clock = week
    _daily(db, pid, r)
    made = shopping.range_list(db, pid, WED, clock=clock)
    assert made is not None
    items = shopping.read_back(db, int(made["id"]))
    assert any(i.credit_g > 0 for i in items)


def test_a_persons_always_at_home_list_still_keeps_a_food_off(week) -> None:  # noqa: F811
    db, pid, _base, r, _clock = week
    _daily(db, pid, r)
    menu_id = _menu_starting(db, pid, THU)
    assert "chicken" in {i.code for i in shopping.build(db, menu_id)}
    _profile(
        db,
        pid,
        segment_days=DAILY,
        effective_from="2026-08-03",
        pantry_json=json.dumps(["Chicken breast"]),
    )
    # theirs to say, with the setting off as with it on
    assert "chicken" not in {i.code for i in shopping.build(db, menu_id)}


def test_the_app_applies_the_setting_from_config(tmp_path: Path) -> None:
    (tmp_path / "tiffin.toml").write_text("[shopping]\ncount_on_home = true\n", encoding="utf-8")
    conf = cfg.load(root=tmp_path, environ={})
    App(conf, Secrets(tmp_path / "secrets", {}))
    assert shopping.COUNT_ON_HOME is True
    (tmp_path / "tiffin.toml").write_text("[shopping]\ncount_on_home = false\n", encoding="utf-8")
    App(cfg.load(root=tmp_path, environ={}), Secrets(tmp_path / "secrets", {}))
    assert shopping.COUNT_ON_HOME is False


@pytest.mark.parametrize("value", ['"yes"', "1", "[]"])
def test_a_setting_that_is_not_true_or_false_is_refused(tmp_path: Path, value: str) -> None:
    (tmp_path / "tiffin.toml").write_text(
        f"[shopping]\ncount_on_home = {value}\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError, match=r"shopping\.count_on_home"):
        cfg.load(root=tmp_path, environ={})
