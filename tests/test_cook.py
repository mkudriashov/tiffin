"""`/cook`: asking for a menu instead of waiting for Saturday, and the exe that broke the model.

The scheduled proposal only fires two days before a cook segment starts, so someone who
onboarded mid-week had no menu at all and no way to ask for one.
"""

from __future__ import annotations

import datetime as dt
import os
import sqlite3
import stat
import subprocess
from pathlib import Path

import pytest

from tiffin.food import menu as menu_mod
from tiffin.jobs import menu_propose

MONDAY = dt.date(2026, 9, 7)
WEDNESDAY = dt.date(2026, 9, 9)
FRIDAY = dt.date(2026, 9, 11)
SATURDAY = dt.date(2026, 9, 12)
SUNDAY = dt.date(2026, 9, 13)


class FakeProfile(dict):
    """Enough of a profile row for the segment helpers."""

    def __getitem__(self, k):
        return self.get(k)

    def keys(self):
        return super().keys()


def weekdays(spec: str = "[[0, 1, 2, 3, 4]]") -> sqlite3.Row:
    return FakeProfile({"segment_days": spec, "variety_json": "{}"})  # type: ignore[return-value]


def test_mid_segment_plans_the_rest_of_the_week_from_tomorrow() -> None:
    """The case that started this: onboarded on Monday, Saturday's proposal long gone."""
    seg = menu_propose.segment_on_demand(weekdays(), MONDAY)
    assert seg is not None
    assert seg.start == dt.date(2026, 9, 8), "tomorrow, not today: today's food is already eaten"
    assert seg.end == FRIDAY, "to the end of the current cook segment"


def test_friday_plans_the_weekend_which_is_cooked_together() -> None:
    """Mon-Fri cadences left Saturday and Sunday with no menu at all; the weekend is a
    segment of its own now, so asking on Friday plans it."""
    seg = menu_propose.segment_on_demand(weekdays(), FRIDAY)
    assert seg is not None
    assert seg.start == SATURDAY and seg.end == SUNDAY


def test_saturday_plans_the_rest_of_the_weekend() -> None:
    seg = menu_propose.segment_on_demand(weekdays(), SATURDAY)
    assert seg is not None and seg.start == SUNDAY and seg.end == SUNDAY


def test_a_segment_starting_tomorrow_is_planned_whole() -> None:
    seg = menu_propose.segment_on_demand(weekdays(), SUNDAY)
    assert seg is not None and seg.start == dt.date(2026, 9, 14) and seg.end == dt.date(2026, 9, 18)


def test_someone_who_cooks_every_day_still_gets_tomorrow() -> None:
    seg = menu_propose.segment_on_demand(weekdays("[[0,1,2,3,4,5,6]]"), WEDNESDAY)
    assert seg is not None and seg.start == dt.date(2026, 9, 10)


def test_a_single_cook_day_a_week_is_understood() -> None:
    seg = menu_propose.segment_on_demand(weekdays("[[6]]"), MONDAY)
    assert seg is not None and seg.start == SUNDAY and seg.end == SUNDAY


def test_no_cook_days_at_all_is_answered_not_crashed() -> None:
    assert menu_propose.segment_on_demand(weekdays("[]"), MONDAY) is None


def test_the_scheduled_path_and_the_asked_path_share_one_builder() -> None:
    """If these drift, an on-demand menu stops obeying the guardrails the Saturday one does."""
    import inspect

    body = inspect.getsource(menu_propose.run)
    assert "_propose_for" in body
    assert "_propose_for" in inspect.getsource(menu_propose.propose_now)


# ---------------------------------------------------------------- the exe that broke everything
def test_the_claude_backend_is_built_with_a_resolved_executable(
    tmp_path: Path, config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`claude` on PATH is claude.CMD, and CreateProcess does not apply PATHEXT.

    Passing the bare name made every `claude -p` call fail instantly with WinError 2, which
    routes to the fallback, so the box answered everything with the local model and said
    nothing about it. The gateway must resolve the name to a real file.
    """
    from tiffin.app import App
    from tiffin.secrets import Secrets

    executable = tmp_path / ("claude.cmd" if os.name == "nt" else "claude")
    executable.write_text(
        "@echo off\n" if os.name == "nt" else "#!/bin/sh\nexit 0\n", encoding="utf-8"
    )
    if os.name != "nt":
        executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", str(tmp_path))
    secrets_dir = tmp_path / "s"
    secrets_dir.mkdir()
    (secrets_dir / "telegram-token-dev.txt").write_text("1:t", encoding="utf-8")
    config.paths.ensure()
    app = App(config, Secrets(secrets_dir))
    db = app.open_db()
    try:
        gw = app.gateway(db)
        assert gw is not None and gw.primary is not None
        exe = str(getattr(gw.primary, "exe", ""))  # Backend is a Protocol; exe is ClaudeBackend's
        assert Path(exe).samefile(executable), "the gateway must pass the resolved path"
    finally:
        db.close()


def test_missing_claude_cli_explains_how_to_continue(
    tmp_path: Path, config, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tiffin.errors import ModelError
    from tiffin.http import Response
    from tiffin.model.claude_cli import ClaudeBackend
    from tiffin.model.provider import configured_provider
    from tiffin.secrets import Secrets

    monkeypatch.setenv("PATH", str(tmp_path))

    def missing(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("claude")

    backend = configured_provider(
        config, Secrets(tmp_path / "secrets"), lambda *_: Response(200, b"")
    )
    assert isinstance(backend, ClaudeBackend)
    assert backend.exe == "claude"
    backend.runner = missing
    with pytest.raises(ModelError, match=r"Install Claude Code.*choose another model provider"):
        backend.invoke_raw([backend.exe, "-p"], "example prompt", 1)


def test_a_menu_already_covering_the_segment_is_not_rebuilt_silently(
    fixture_db, two_persons
) -> None:
    pid = two_persons["owner"]
    fixture_db.insert(
        "menus",
        person_id=pid,
        segment_start="2026-09-08",
        segment_end="2026-09-11",
        status="accepted",
    )
    assert menu_mod.current_menu(fixture_db, pid, dt.date(2026, 9, 8)) is not None


@pytest.mark.parametrize("word", ["replace", "again", "new"])
def test_the_replace_words_are_the_ones_people_type(word) -> None:
    assert word in ("replace", "again", "new")


def test_a_retry_names_the_foods_that_would_fix_the_shortfall() -> None:
    """A re-prompt saying only "max reachable 180 g < 201 g" fails the same way twice."""
    from tiffin.food.catalogue import Ingredient
    from tiffin.jobs.menu_propose import leanest_for

    def ing(code, name, kcal, protein, fibre=0.0):
        return Ingredient(
            id=1,
            code=code,
            version=1,
            name=name,
            cofid_code=None,
            kcal_100=kcal,
            protein_100=protein,
            fat_100=1.0,
            carb_100=0.0,
            fibre_100=fibre,
            pack_g=100.0,
            pack_label="pack",
            aisle="meat",
            pantry=False,
            use_whole_pack=False,
            approved=True,
            person_scope="both",
            aliases=(),
        )

    items = [
        ing("cod", "Cod fillets", 75, 17.5),
        ing("oil", "Olive oil", 899, 0.0),
        ing("bro", "Broccoli", 34, 4.3, fibre=4.0),
    ]
    protein = leanest_for("protein: max reachable 180 g < 201 g", items)
    assert "Cod fillets" in protein and protein.index("Cod fillets") < protein.index("Olive oil")
    fibre = leanest_for("fibre 22 g below the 27 g floor", items)
    assert "Broccoli" in fibre and fibre.index("Broccoli") < fibre.index("Olive oil")
    # a failure that is not about a macro gets no ingredient list at all
    assert leanest_for("the model returned no dishes", items) == ""


def test_a_cook_for_a_week_with_no_targets_row_works_them_out(
    fixture_db, two_persons, config, frozen_clock
) -> None:
    """
    targets for that week yet. They are set on Sunday evening."

    Nothing about the first targets row needs Sunday - it is the formula, from the profile and
    the latest weight. Sunday calibrates, and its row supersedes this one by the ordinary
    append-only rule, so working it out now costs nothing and unblocks the interview's most
    obvious next step.
    """
    from tiffin import body, targets

    db, pid = fixture_db, two_persons["owner"]
    wednesday = dt.date(2026, 9, 9)
    db.insert(
        "profiles",
        person_id=pid,
        effective_from=wednesday.isoformat(),
        sex="m",
        birth_date="1990-03-15",
        height_cm=183.0,
        activity_factor=1.55,
        kcal_floor=1800,
        segment_days="[[0, 1, 2, 3, 4]]",
    )
    body.record_weigh_in(db, pid, wednesday, 100.8, "manual")
    assert db.scalar("SELECT COUNT(*) FROM targets WHERE person_id = ?", (pid,)) == 0

    segment = menu_propose.segment_on_demand(
        db.one("SELECT * FROM profiles WHERE person_id = ?", (pid,)), wednesday
    )
    assert segment is not None
    row = targets.ensure_targets(db, pid, segment.start, config, clock=frozen_clock)
    assert row is not None, "the segment's own week gets its numbers rather than a refusal"
    assert int(row["kcal"]) > 0
    assert str(row["source"]) == "formula"


def test_sunday_still_supersedes_what_a_cook_worked_out_early(
    fixture_db, two_persons, config, frozen_clock
) -> None:
    """The reason working them out early is safe: targets are a ledger and the newest row
    wins, so the calibration is not blocked by a formula row already sitting there."""
    from tiffin import body, targets

    db, pid = fixture_db, two_persons["owner"]
    day = dt.date(2026, 9, 9)
    db.insert(
        "profiles",
        person_id=pid,
        effective_from=day.isoformat(),
        sex="m",
        birth_date="1990-03-15",
        height_cm=183.0,
        activity_factor=1.55,
        kcal_floor=1800,
    )
    body.record_weigh_in(db, pid, day, 100.8, "manual")
    early = targets.ensure_targets(db, pid, day, config, clock=frozen_clock)
    assert early is not None
    db.insert(
        "targets",
        person_id=pid,
        week_start="2026-09-07",
        kcal=int(early["kcal"]) + 200,
        protein_g=200,
        fat_g=70,
        carb_g=200,
        fibre_g=30,
        source="calibration",
    )
    now = targets.targets_for(db, pid, day)
    assert now is not None
    assert str(now["source"]) == "calibration"
    assert int(now["kcal"]) == int(early["kcal"]) + 200


# ---------------------------------------------------------------- when the solver cannot close
def _items():
    from tiffin.food.catalogue import Ingredient

    def ing(name, kcal, protein, fat, fibre):
        return Ingredient(
            id=abs(hash(name)) % 10000,
            code=name.lower().replace(" ", "_"),
            version=1,
            name=name,
            cofid_code=None,
            kcal_100=kcal,
            protein_100=protein,
            fat_100=fat,
            carb_100=0.0,
            fibre_100=fibre,
            pack_g=500.0,
            pack_label="500 g",
            aisle="x",
            pantry=False,
            use_whole_pack=False,
            approved=True,
            person_scope="both",
            aliases=(),
        )

    return [
        ing("Chicken breast, raw", 106, 24.0, 1.1, 0.0),
        ing("Cod fillets, raw", 75, 17.5, 0.6, 0.0),
        ing("Olive oil", 899, 0.0, 99.9, 0.0),
        ing("Almonds, whole", 554, 21.2, 49.9, 7.4),
        ing("Lentils, dried", 297, 24.0, 1.9, 8.9),
    ]


@pytest.mark.parametrize(
    ("reason", "expect"),
    [
        ("2026-09-10: fat: max reachable 52 g < floor 71 g within ±5% kcal", "Olive oil"),
        ("protein: max reachable 180 g < 201 g", "Cod fillets, raw"),
        ("fibre: max reachable 20 g < 27 g", "Lentils, dried"),
    ],
)
def test_every_shortfall_tells_the_model_what_to_reach_for(reason: str, expect: str) -> None:
    """The owner's /cook failed three times identically on a fat floor it was never told how to
    reach: the hint builder knew about protein and fibre and returned nothing for fat, so the
    retry got the bare solver error and picked the same lean day again."""
    hint = menu_propose.leanest_for(reason, _items())
    assert hint, f"no guidance at all for {reason!r}"
    assert expect in hint


def test_the_fat_advice_is_not_the_protein_advice() -> None:
    """ "Use richer foods only for what is left over" is right for protein and precisely
    backwards for a fat floor."""
    fat = menu_propose.leanest_for("fat: max reachable 52 g < floor 71 g", _items())
    protein = menu_propose.leanest_for("protein: max reachable 180 g < 201 g", _items())
    assert "only for what is left over" in protein
    assert "only for what is left over" not in fat
    assert "cannot reach the fat floor" in fat


def test_no_macro_the_solver_can_refuse_on_is_left_without_advice() -> None:
    """Enumerated from the solver's own vocabulary rather than the three somebody thought of."""
    for macro in ("protein", "fat", "fibre"):
        reason = f"2026-09-10: {macro}: max reachable 1 g < floor 99 g within ±5% kcal"
        assert menu_propose.leanest_for(reason, _items()), macro


@pytest.mark.parametrize(
    ("reason", "wanted"),
    [
        ("2026-09-10: fat: max reachable 52 g < floor 71 g within ±5% kcal", "52 g of fat"),
        ("protein: max reachable 180 g < 201 g", "180 g of protein"),
        ("no days", "no earlier menu to repeat"),
    ],
)
def test_the_reason_is_given_in_words_not_in_solver_notation(reason: str, wanted: str) -> None:
    assert wanted in menu_propose.why_not(reason)
    assert "max reachable" not in menu_propose.why_not(reason)


def test_the_admin_is_told_why_rather_than_told_that_owner_was_told(
    fixture_db, two_persons, config, frozen_clock, tmp_path
) -> None:
    """
    why" - addressed to the owner. He is the admin, so alerting him about himself is one message too
    many and the wrong one is the readable one."""
    from tiffin.app import App
    from tiffin.jobs import menu_propose as mp
    from tiffin.secrets import Secrets

    from .fakes import FakeTelegram

    db = fixture_db
    secrets_dir = tmp_path / "s"
    secrets_dir.mkdir()
    (secrets_dir / "telegram-token-dev.txt").write_text("1:t", encoding="utf-8")
    tg = FakeTelegram()
    app = App(config, Secrets(secrets_dir), clock=frozen_clock, transport=tg)
    person = app.registry(db).by_slug("owner")
    assert person is not None and person.is_admin, "the whole point of the branch"

    reason = "2026-09-10: fat: max reachable 52 g < floor 71 g within ±5% kcal"
    said = "I could not build a menu that hits your targets. " + mp.why_not(reason)
    assert "52 g of fat" in said
    assert "71 g" in said
    assert "Alex has been told" not in said, "he is Alex"
    assert "menu_failed" not in said
    assert "max reachable" not in said, "the solver's notation is not a sentence"


def test_partner_gets_the_same_reason_owner_does_and_owner_still_gets_the_alert() -> None:
    import inspect

    from tiffin.jobs import menu_propose as mp

    source = inspect.getsource(mp._propose_for)
    assert "ctx.outbox.alert(" in source, "the admin still hears that a job degraded"
    assert "Alex has been told why." not in source, "no message points her at a third party"
    assert source.count("why_not(hint)") == 1, "one sentence, both people"
