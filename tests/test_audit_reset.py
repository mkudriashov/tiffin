"""The reset's table map, checked against the schema rather than against itself.

A reset is the one place where getting a table wrong is silent in both directions: a table
missing from both lists survives a reset it should not have, or - worse the other way - gets
deleted along with data nobody can get back. So the schema is the source of truth here, and
every table in it has to appear in exactly one list with a reason written next to it.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from tiffin import db as dbmod
from tiffin import reset
from tiffin.db import Db

MONDAY = dt.date(2026, 9, 7)


def test_every_person_scoped_table_is_classified(fixture_db: Db) -> None:
    """The whole point: adding a table forces a decision about what a reset does to it."""
    con = fixture_db.con
    scoped = {t for t in dbmod.tables(con) if "person_id" in dbmod.columns(con, t)}
    classified = set(reset.TOLD) | set(reset.KEPT) | set(reset.SHARED)
    assert not scoped - classified, f"not in TOLD, KEPT or SHARED: {sorted(scoped - classified)}"


def test_every_other_table_is_classified_too(fixture_db: Db) -> None:
    """The child tables reach a person through a parent row, and the shared ones must not be
    reachable at all. Between them they have to cover what is left."""
    con = fixture_db.con
    rest = {t for t in dbmod.tables(con) if "person_id" not in dbmod.columns(con, t)}
    classified = set(reset.CHILDREN) | set(reset.SHARED) | set(reset.TOLD) | set(reset.KEPT)
    assert not rest - classified, f"unclassified: {sorted(rest - classified)}"


def test_nothing_is_in_two_lists_at_once(fixture_db: Db) -> None:
    """A table in two lists is a contradiction about what a reset does to it. `weigh_ins` is
    the one that is partly reset, and that is said once, in SPLIT."""
    told, kept, shared = set(reset.TOLD), set(reset.KEPT), set(reset.SHARED)
    assert not told & kept, sorted(told & kept)
    assert not told & shared, sorted(told & shared)
    assert not kept & shared, sorted(kept & shared)
    assert not set(reset.CHILDREN) & told, sorted(set(reset.CHILDREN) & told)
    # a split table is listed as kept, because most of it is; the clause says which part goes
    assert set(reset.SPLIT) <= kept, sorted(set(reset.SPLIT) - kept)


def test_every_named_table_actually_exists(fixture_db: Db) -> None:
    """A list that names a table the schema dropped is a list nobody has read in a while."""
    present = set(dbmod.tables(fixture_db.con))
    named = set(reset.TOLD) | set(reset.KEPT) | set(reset.SHARED) | set(reset.CHILDREN)
    assert not named - present, f"named but not in the schema: {sorted(named - present)}"


def test_every_kept_table_says_why(fixture_db: Db) -> None:
    for table, why in {**reset.KEPT, **reset.SHARED}.items():
        assert len(why) > 15, f"{table}: {why!r} is not a reason"


# ---------------------------------------------------------------- what it actually does
@pytest.fixture
def lived_in(fixture_db: Db, two_persons):
    """A person a week in: an interview, a plan, a menu and a weight history
    that came off the scale rather than out of the interview."""
    db, pid = fixture_db, two_persons["owner"]
    from tiffin import body

    db.insert(
        "profiles",
        person_id=pid,
        effective_from="2026-09-01",
        sex="m",
        birth_date="1990-03-15",
        height_cm=183.0,
        activity_factor=1.55,
        kcal_floor=1800,
    )
    db.insert("onboarding_answers", person_id=pid, step="hcm", answer_json=json.dumps(183.0))
    db.insert("consents", person_id=pid, granted=1, wording_version=1)
    plan = db.insert("training_plans", person_id=pid, effective_from="2026-09-01")
    db.insert(
        "planned_sessions", plan_id=plan, person_id=pid, weekday=0, kind="climb", target_minutes=90
    )
    menu = db.insert(
        "menus",
        person_id=pid,
        segment_start="2026-09-08",
        segment_end="2026-09-11",
        status="accepted",
    )
    db.insert(
        "menu_days",
        menu_id=menu,
        day="2026-09-08",
        kcal=2200,
        protein_g=180,
        fat_g=70,
        carb_g=200,
        fibre_g=30,
    )
    db.insert(
        "day_confirmations",
        person_id=pid,
        day="2026-09-07",
        status="confirmed",
        basis="menu",
        kcal=2200,
        protein_g=180,
        fat_g=70,
        carb_g=200,
        fibre_g=30,
    )
    # the scale, through intervals.icu, plus the one weight the interview asked for
    for i in range(5):
        body.record_weigh_in(
            db, pid, MONDAY - dt.timedelta(days=10 - i), 100.5 - i * 0.1, "icu_wellness"
        )
    body.record_weigh_in(db, pid, MONDAY, 99.8, "manual")
    db.insert("icu_wellness", person_id=pid, day="2026-09-07", steps=11000, icu_raw_id=0)
    db.insert(
        "messages_in",
        chat_id=1000001,
        person_id=pid,
        kind="text",
        raw_json="{}",
        update_id=1,
        received_at="2026-09-07 19:00:00",
    )
    return db, pid


def test_the_interview_answers_and_everything_from_them_go(lived_in) -> None:
    db, pid = lived_in
    report = reset.reset_onboarding(db, "owner")
    for table in ("profiles", "onboarding_answers", "consents", "training_plans"):
        left = db.scalar(f"SELECT COUNT(*) FROM {table} WHERE person_id = ?", (pid,))
        assert left == 0, f"{table} survived the reset"
    assert db.scalar("SELECT COUNT(*) FROM menu_days") == 0, "the menu's days went with it"
    assert db.scalar("SELECT COUNT(*) FROM planned_sessions") == 0
    assert report.total > 0


def test_the_scale_history_stays_and_the_typed_weight_goes(lived_in) -> None:
    """The line the owner drew: reset what I told the bot, not intervals.icu."""
    db, pid = lived_in
    assert db.scalar("SELECT COUNT(*) FROM weigh_ins WHERE person_id = ?", (pid,)) == 6
    reset.reset_onboarding(db, "owner")
    rows = db.all("SELECT day, source FROM weigh_ins WHERE person_id = ? ORDER BY day", (pid,))
    assert len(rows) == 5
    assert {str(r["source"]) for r in rows} == {"icu_wellness"}
    assert db.scalar("SELECT COUNT(*) FROM icu_wellness WHERE person_id = ?", (pid,)) == 1


def test_the_trend_is_rebuilt_from_what_survived(lived_in) -> None:
    """Rebuild_trend only ever adds, so a stale point for a deleted weigh-in would have sat
    there for ever and the chart would have shown a weight nobody recorded."""
    db, pid = lived_in
    assert db.scalar("SELECT COUNT(*) FROM trend_points WHERE day = ?", (MONDAY.isoformat(),))
    reset.reset_onboarding(db, "owner")
    assert not db.scalar("SELECT COUNT(*) FROM trend_points WHERE day = ?", (MONDAY.isoformat(),))
    days = {
        str(r["day"]) for r in db.all("SELECT day FROM trend_points WHERE person_id = ?", (pid,))
    }
    weighed = {
        str(r["day"]) for r in db.all("SELECT day FROM weigh_ins WHERE person_id = ?", (pid,))
    }
    assert days == weighed, "a trend point for every surviving weigh-in and none besides"


def test_they_do_not_have_to_pair_again(lived_in) -> None:
    """The difference between this and erasure: the bot still knows who they are, so the next
    thing they send is /onboard rather than a code from a terminal."""
    db, pid = lived_in
    reset.reset_onboarding(db, "owner")
    assert db.scalar("SELECT COUNT(*) FROM telegram_identities WHERE person_id = ?", (pid,)) == 1
    assert db.scalar("SELECT COUNT(*) FROM persons WHERE id = ?", (pid,)) == 1
    assert db.scalar("SELECT erased_at FROM persons WHERE id = ?", (pid,)) is None


def test_the_other_person_is_untouched(lived_in) -> None:
    db, _pid = lived_in
    partner = int(db.scalar("SELECT id FROM persons WHERE slug = 'partner'") or 0)
    db.insert(
        "profiles",
        person_id=partner,
        effective_from="2026-09-01",
        sex="f",
        birth_date="1992-01-01",
        height_cm=168.0,
        activity_factor=1.4,
        kcal_floor=1400,
    )
    reset.reset_onboarding(db, "owner")
    assert db.scalar("SELECT COUNT(*) FROM profiles WHERE person_id = ?", (partner,)) == 1


def test_the_shared_catalogue_is_never_touched(lived_in) -> None:
    """Every recipe is person_scope 'both'. A per-person reset that took them would break the
    other person's menus, and they cost a model call each to make."""
    db, _pid = lived_in
    db.insert(
        "recipes",
        name="chilli",
        servings=1,
        prep_minutes=30,
        method_json="[]",
        source="model",
        person_scope="both",
    )
    reset.reset_onboarding(db, "owner")
    assert db.scalar("SELECT COUNT(*) FROM recipes") == 1


def test_the_append_only_triggers_are_back_afterwards(lived_in) -> None:
    """The reset drops them to delete. Leaving them off would quietly turn every ledger in the
    database into an ordinary table."""
    db, pid = lived_in
    reset.reset_onboarding(db, "owner")
    db.insert(
        "profiles",
        person_id=pid,
        effective_from="2026-09-08",
        sex="m",
        birth_date="1990-03-15",
        height_cm=183.0,
        activity_factor=1.55,
        kcal_floor=1800,
    )
    with pytest.raises(Exception, match="append-only"):
        db.execute("UPDATE profiles SET height_cm = 1 WHERE person_id = ?", (pid,))
    with pytest.raises(Exception, match="append-only"):
        db.execute("DELETE FROM profiles WHERE person_id = ?", (pid,))


def test_preview_changes_nothing(lived_in) -> None:
    """The default when somebody types the command. It has to be safe to type."""
    db, pid = lived_in
    before = db.scalar("SELECT COUNT(*) FROM profiles WHERE person_id = ?", (pid,))
    report = reset.preview(db, "owner")
    assert report.total > 0
    assert db.scalar("SELECT COUNT(*) FROM profiles WHERE person_id = ?", (pid,)) == before


def test_a_person_who_never_onboarded_resets_to_nothing(fixture_db: Db, two_persons) -> None:
    """Her, today. It must be a no-op that says so rather than an error."""
    report = reset.reset_onboarding(fixture_db, "partner")
    assert report.total == 0


def test_an_unknown_person_is_refused(fixture_db: Db, two_persons) -> None:
    with pytest.raises(LookupError):
        reset.reset_onboarding(fixture_db, "nobody")


def test_the_reset_leaves_no_orphans(lived_in) -> None:
    """The assertion that makes delete order impossible to get wrong quietly.

    Each child clause finds its rows by reading through the parent, so deleting the parent
    first leaves the child behind and invisible to the query meant to remove it - a menu_days
    row pointing at a menu that is gone. SQLite says so if you ask it.
    """
    db, _pid = lived_in
    reset.reset_onboarding(db, "owner")
    dangling = list(db.con.execute("PRAGMA foreign_key_check"))
    assert not dangling, [tuple(r) for r in dangling]


def test_erasure_leaves_no_orphans_either(fixture_db: Db, two_persons, tmp_path) -> None:
    """`admin.erase_person` had no test at all, and raised every time it was called."""
    from tiffin import admin

    db = fixture_db
    pid = two_persons["owner"]
    db.insert(
        "profiles",
        person_id=pid,
        effective_from="2026-09-01",
        sex="m",
        birth_date="1990-03-15",
        height_cm=183.0,
        activity_factor=1.55,
        kcal_floor=1800,
    )
    plan = db.insert("training_plans", person_id=pid, effective_from="2026-09-01")
    db.insert(
        "planned_sessions", plan_id=plan, person_id=pid, weekday=0, kind="climb", target_minutes=90
    )
    menu = db.insert(
        "menus",
        person_id=pid,
        segment_start="2026-09-08",
        segment_end="2026-09-11",
        status="accepted",
    )
    db.insert(
        "menu_days",
        menu_id=menu,
        day="2026-09-08",
        kcal=2200,
        protein_g=180,
        fat_g=70,
        carb_g=200,
        fibre_g=30,
    )
    manifest = tmp_path / "export" / "manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"slug": "owner"}), encoding="utf-8")

    out = admin.erase_person(db, "owner", manifest_path=manifest)
    assert out["slug"] == "owner"
    assert not list(db.con.execute("PRAGMA foreign_key_check"))
    assert db.scalar("SELECT COUNT(*) FROM profiles WHERE person_id = ?", (pid,)) == 0
    assert db.scalar("SELECT COUNT(*) FROM erasures") == 1
    assert db.scalar("SELECT erased_at FROM persons WHERE id = ?", (pid,)) is not None
    # and the ledgers are ledgers again
    with pytest.raises(Exception, match="append-only"):
        db.execute("DELETE FROM persons WHERE id = ?", (pid,))


def test_erasure_without_an_export_is_refused(fixture_db: Db, two_persons, tmp_path) -> None:
    from tiffin import admin

    with pytest.raises(PermissionError, match="export first"):
        admin.erase_person(fixture_db, "owner", manifest_path=tmp_path / "nope.json")
    other = tmp_path / "m.json"
    other.write_text(json.dumps({"slug": "partner"}), encoding="utf-8")
    with pytest.raises(PermissionError, match="another person"):
        admin.erase_person(fixture_db, "owner", manifest_path=other)
