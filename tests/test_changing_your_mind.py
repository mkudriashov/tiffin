"""Onboarding answers can be changed without restarting the interview."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tiffin import onboarding as ob
from tiffin.app import App
from tiffin.clock import FrozenClock
from tiffin.db import Db
from tiffin.secrets import Secrets
from tiffin.telegram import settings
from tiffin.telegram.poller import Poller

from .fakes import FakeTelegram

OWNER_CHAT = 1000001


@pytest.fixture
def bot(fixture_db: Db, two_persons, frozen_clock: FrozenClock, config, tmp_path: Path):
    pid = two_persons["owner"]
    fixture_db.insert(
        "profiles",
        person_id=pid,
        effective_from="2026-09-01",
        sex="m",
        birth_date="1990-03-15",
        height_cm=183.0,
        activity_factor=1.55,
        steps_floor=10000,
        quiet_start="22:30",
        quiet_end="07:00",
        confirm_time="20:30",
        morning_time="07:00",
        nudge_time="16:00",
        rate_pct_per_week=0.7,
        kcal_floor=1800,
        meals_per_day=4,
        pre_bed_protein=1,
        tone="neutral",
        segment_days=json.dumps([[0, 1, 2, 3, 4]]),
        variety_json=json.dumps({"all": "same"}),
        refused_foods=json.dumps(["mushrooms"]),
    )
    secrets_dir = tmp_path / "s"
    secrets_dir.mkdir()
    (secrets_dir / "telegram-token-dev.txt").write_text("1:t", encoding="utf-8")
    tg = FakeTelegram()
    app = App(
        config,
        Secrets(secrets_dir),
        clock=frozen_clock,
        transport=tg,
        gateway_factory=lambda _a, _d: None,
    )
    poller = Poller(
        app.api(),
        fixture_db,
        app.registry(fixture_db),
        app.handlers(fixture_db).handle,
        clock=frozen_clock,
        sleeper=lambda s: None,
    )
    n = {"i": 0}

    def say(text: str) -> str:
        n["i"] += 1
        tg.queue_update(tg.text_update(n["i"], OWNER_CHAT, text))
        poller.run_once()
        return tg.sent[-1]["text"]

    def tap(data: str) -> str:
        n["i"] += 1
        tg.queue_update(tg.callback_update(n["i"], OWNER_CHAT, data, message_id=n["i"]))
        poller.run_once()
        return tg.sent[-1]["text"]

    return say, tap, pid, fixture_db, app


# ---------------------------------------------------------------- every other answer
def test_an_answer_can_be_changed_without_redoing_the_interview(bot) -> None:
    say, _tap, pid, db, _app = bot
    assert "evening is now 21:00" in say("/set evening 21:00")
    row = db.one(
        "SELECT confirm_time, morning_time FROM profiles WHERE person_id = ? ORDER BY id DESC",
        (pid,),
    )
    assert row is not None and row["confirm_time"] == "21:00"
    assert row["morning_time"] == "07:00", "everything else is carried over, not lost"


def test_a_change_is_a_new_row_not_an_edit(bot) -> None:
    say, _tap, pid, db, _app = bot
    say("/set evening 21:00")
    assert db.scalar("SELECT COUNT(*) FROM profiles WHERE person_id = ?", (pid,)) == 2


def test_a_bad_value_is_refused_by_the_interviews_own_parser(bot) -> None:
    say, _tap, pid, db, _app = bot
    assert "HH:MM" in say("/set evening tea time")
    assert db.scalar("SELECT COUNT(*) FROM profiles WHERE person_id = ?", (pid,)) == 1
    assert "outside" in say("/set rate 1.5"), "the schema's own bound"


def test_a_list_field_replaces_and_reports(bot) -> None:
    say, _tap, pid, db, _app = bot
    out = say("/set avoid mushrooms, olives")
    assert "mushrooms, olives" in out
    row = db.one("SELECT refused_foods FROM profiles WHERE person_id = ? ORDER BY id DESC", (pid,))
    assert row is not None and json.loads(str(row["refused_foods"])) == ["mushrooms", "olives"]


def test_a_button_field_offers_its_buttons(bot) -> None:
    say, tap, pid, db, _app = bot
    out = say("/set variety")
    assert "same" in out and "{" not in out, "a person is never shown stored JSON"
    assert "rotate" in tap("k:set:variety:rotate")
    row = db.one("SELECT variety_json FROM profiles WHERE person_id = ? ORDER BY id DESC", (pid,))
    assert row is not None and json.loads(str(row["variety_json"])) == {"all": "rotate"}


def test_set_alone_lists_what_can_change_and_where_the_rest_lives(bot) -> None:
    say, _tap, _pid, _db, _app = bot
    out = say("/set")
    assert "/set evening" in out and "/set avoid" in out
    assert "/kit" in out, "and names where the fixed ones live"
    assert "/supp" not in out, "the command is absent"


def test_an_unknown_setting_points_at_the_list(bot) -> None:
    say, _tap, _pid, _db, _app = bot
    assert "Send /set to see the list" in say("/set favourite-colour blue")
    assert "/kit" in say("/set kitchen"), "a fixed one says where it lives instead"


def test_cook_days_writes_the_shape_the_menu_reads(bot) -> None:
    say, _tap, pid, db, _app = bot
    say("/set cook-days mw")
    row = db.one("SELECT segment_days FROM profiles WHERE person_id = ? ORDER BY id DESC", (pid,))
    assert row is not None and json.loads(str(row["segment_days"])) == [[0, 1, 2], [3, 4]]


# ---------------------------------------------------------------- the class, not the instance
def test_every_interview_answer_is_changeable_or_declared_fixed() -> None:
    """A new question added to STEPS must be reachable afterwards, or say where it lives.

    This is the whole point: twenty-one answers were unreachable because nobody kept a list.
    """
    covered = {f.step for f in settings.FIELDS}
    elsewhere = {"sex", "dob", "wkg", "kit", "plan", "shops", "measure", "supp", "cons"}
    missing = [s.id for s in ob.STEPS if s.id not in covered and s.id not in elsewhere]
    assert not missing, f"no way to change: {missing}"


def test_every_settings_field_names_a_real_step_and_column() -> None:
    ids = {s.id for s in ob.STEPS}
    for f in settings.FIELDS:
        assert f.step in ids, f"{f.name} points at a step that does not exist"
    schema = Path(ob.__file__).with_name("schema.sql").read_text(encoding="utf-8")
    block = schema.split("CREATE TABLE IF NOT EXISTS profiles")[1].split(");")[0]
    for f in settings.FIELDS:
        assert f.column in block, f"{f.name} writes a column profiles does not have"


# ---------------------------------------------------------------- and it stops inventing places
def test_the_assistant_is_given_the_real_commands(bot) -> None:
    """The model must use only commands the bot actually supports."""
    from tiffin.model import prompts
    from tiffin.telegram.chat import command_list

    _say, _tap, _pid, db, app = bot
    listing = command_list(app.handlers(db))
    assert "/set" in listing and "/avoid" in listing
    text = prompts.chat_system(commands=listing)
    assert "no settings screen" in text
    assert "/avoid mushrooms" in text, "the real way in is in front of it"


# ---------------------------------------------------------------- and taking things away
def test_one_item_can_be_dropped_from_a_list(bot) -> None:
    """Replacing the whole list was the only way, so dropping one of fifteen meant retyping
    the other fourteen exactly - which nobody does, so it stayed."""
    say, _tap, pid, db, _app = bot
    say("/set avoid mushrooms, olives, kale")
    assert "mushrooms, olives" in say("/set avoid - kale")
    row = db.one("SELECT refused_foods FROM profiles WHERE person_id = ? ORDER BY id DESC", (pid,))
    assert row is not None and json.loads(str(row["refused_foods"])) == ["mushrooms", "olives"]


def test_one_item_can_be_added_to_a_list(bot) -> None:
    say, _tap, pid, db, _app = bot
    assert "mushrooms, olives" in say("/set avoid + olives")
    row = db.one("SELECT refused_foods FROM profiles WHERE person_id = ? ORDER BY id DESC", (pid,))
    assert row is not None and json.loads(str(row["refused_foods"])) == ["mushrooms", "olives"]


def test_removing_what_is_not_there_says_so(bot) -> None:
    say, _tap, _pid, _db, _app = bot
    out = say("/set avoid - kale")
    assert "Not on the list: kale" in out and "mushrooms" in out


def test_an_optional_answer_can_be_cleared(bot) -> None:
    """A goal weight wants 30-250 kg, so no value meant "no goal" and it could never be
    taken back - the pace arithmetic went on running off it."""
    say, _tap, pid, db, _app = bot
    say("/set goal 90")
    assert "cleared" in say("/set goal none")
    row = db.one("SELECT goal_weight_kg FROM profiles WHERE person_id = ? ORDER BY id DESC", (pid,))
    assert row is not None and row["goal_weight_kg"] is None


def test_a_required_answer_cannot_be_cleared(bot) -> None:
    say, _tap, pid, db, _app = bot
    out = say("/set evening none")
    assert "HH:MM" in out, "it is refused by the parser, not silently nulled"
    row = db.one("SELECT confirm_time FROM profiles WHERE person_id = ? ORDER BY id DESC", (pid,))
    assert row is not None and row["confirm_time"] == "20:30"


def test_the_training_week_can_be_retyped(bot) -> None:
    """It could only be changed by a whole PDF or by redoing all twenty-seven questions."""
    say, _tap, pid, db, _app = bot
    assert "No training week yet" in say("/week")
    out = say("/week Mon climb 90, Wed ride 60 at 18:30")
    assert "Mon climb 90 min" in out and "Wed ride 60 min at 18:30" in out
    rows = db.all(
        "SELECT kind FROM planned_sessions WHERE person_id = ? AND kind <> 'rest'", (pid,)
    )
    assert {str(r["kind"]) for r in rows} == {"climb", "ride"}
    # and rewriting it is a new plan, not an edit of the old one
    say("/week Tue ride 45")
    assert db.scalar("SELECT COUNT(*) FROM training_plans WHERE person_id = ?", (pid,)) == 2


def test_a_bad_week_leaves_the_old_one_alone(bot) -> None:
    say, _tap, pid, db, _app = bot
    say("/week Mon climb 90")
    assert "could not read" in say("/week Mon bouldering 90")
    assert db.scalar("SELECT COUNT(*) FROM training_plans WHERE person_id = ?", (pid,)) == 1
