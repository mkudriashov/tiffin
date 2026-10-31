"""Read-only report on food preferences and menu decisions."""

from __future__ import annotations

import collections
import datetime as dt
import json

from .db import Db
from .food import catalogue, menu

GIVEN = {
    "notes": "SELECT COUNT(*) FROM person_notes WHERE person_id = ? "
    "AND created_at >= ? AND created_at < ?",
    "ratings": "SELECT COUNT(*) FROM ratings WHERE person_id = ? "
    "AND created_at >= ? AND created_at < ?",
    "swaps": "SELECT COUNT(*) FROM dish_events WHERE person_id = ? AND kind = 'swapped_in' "
    "AND created_at >= ? AND created_at < ?",
    "one-meal dishes": "SELECT COUNT(*) FROM built_dishes WHERE person_id = ? "
    "AND created_at >= ? AND created_at < ?",
    "plates resized": "SELECT COUNT(*) FROM menu_emphasis WHERE person_id = ? "
    "AND created_at >= ? AND created_at < ?",
}


def run(db: Db, until: dt.date, days: int) -> None:
    since = (until - dt.timedelta(days=days)).isoformat()
    end = (until + dt.timedelta(days=1)).isoformat()
    report(db, since, end, days)


def refused_as_of(db: Db, person_id: int, recipe_ids: list[int], at: str) -> set[str]:
    """The foods in these recipes the person had refused by `at`: their avoid list as it stood
    then. `menu.refused_brought_in` reads it as it stands now - right at the moment a menu is
    decided, wrong for one decided a month ago."""
    if not recipe_ids:
        return set()
    row = db.one(
        "SELECT refused_foods FROM profiles WHERE person_id = ? AND created_at <= ? "
        "ORDER BY id DESC LIMIT 1",
        (person_id, at),
    )
    raw = json.loads(row["refused_foods"] or "[]") if row else []
    terms = [catalogue.normalise(t) for t in raw]
    status: dict[str, str] = {}
    for r in db.all(
        "SELECT ingredient_code, status FROM ingredient_preferences WHERE person_id = ? "
        "AND created_at <= ? ORDER BY id",
        (person_id, at),
    ):
        status[str(r["ingredient_code"])] = str(r["status"])
    if not terms and "refused" not in status.values():
        return set()
    marks = ",".join("?" for _ in recipe_ids)
    codes = {
        str(r["code"])
        for r in db.all(
            "SELECT DISTINCT i.code FROM recipe_ingredients ri "
            f"JOIN ingredients i ON i.id = ri.ingredient_id WHERE ri.recipe_id IN ({marks})",
            tuple(recipe_ids),
        )
    }
    return {
        i.name.split(",")[0]
        for i in catalogue.latest_versions(db)
        if i.code in codes
        and (status.get(i.code) == "refused" or any(catalogue.refused_matches(t, i) for t in terms))
    }


def list_changes(db: Db, person_id: int, since: str, end: str) -> collections.Counter[str]:
    """Entries added to or taken off the avoid and likes lists in the window."""
    out: collections.Counter[str] = collections.Counter()
    prev: tuple[set[str], set[str]] | None = None
    for r in db.all(
        "SELECT created_at, refused_foods, liked_foods FROM profiles WHERE person_id = ? "
        "ORDER BY id",
        (person_id,),
    ):
        cur = (
            set(json.loads(r["refused_foods"] or "[]")),
            set(json.loads(r["liked_foods"] or "[]")),
        )
        if prev is not None and since <= str(r["created_at"]) < end:
            out["avoid"] += len(cur[0] ^ prev[0])
            out["likes"] += len(cur[1] ^ prev[1])
        prev = cur
    return out


def broken_promises(db: Db, person_id: int, since: str, end: str) -> list[tuple[int, str, str]]:
    """A refused food on a plate newly put there by a menu that came into force in the window,
    judged by what was refused at that moment (`menu.newly_placed`)."""
    out = []
    for m in db.all(
        "SELECT m.id, m.segment_start, (SELECT MIN(d.created_at) FROM menu_decisions d "
        "WHERE d.menu_id = m.id AND d.decision = 'accepted') AS decided FROM menus m "
        "WHERE m.person_id = ?",
        (person_id,),
    ):
        if m["decided"] is None or not since <= str(m["decided"]) < end:
            continue
        placed = menu.newly_placed(db, int(m["id"]))
        for food in sorted(refused_as_of(db, person_id, placed, str(m["decided"]))):
            out.append((int(m["id"]), str(m["segment_start"]), food))
    return out


def rebuilds(db: Db, person_id: int, since: str, end: str) -> tuple[int, int]:
    """(their own model builds, of which over days an earlier build already covered): not the
    shared weekend's two versions, not a plate resized or a dish swapped, not a repeat."""
    own = db.all(
        "SELECT id, segment_start, segment_end FROM menus m WHERE person_id = ? "
        "AND created_at >= ? AND created_at < ? AND source_menu_id IS NULL "
        "AND status != 'repeated' AND model_used IS NOT NULL AND NOT EXISTS "
        "(SELECT 1 FROM shared_menu_members s WHERE s.menu_id = m.id)",
        (person_id, since, end),
    )
    again = sum(
        1
        for m in own
        if db.scalar(
            "SELECT 1 FROM menus e WHERE e.person_id = ? AND e.id < ? "
            "AND e.model_used IS NOT NULL AND e.segment_start <= ? AND ? <= e.segment_end",
            (person_id, int(m["id"]), m["segment_end"], m["segment_start"]),
        )
    )
    return len(own), again


def report(db: Db, since: str, end: str, days: int) -> None:
    weeks = max(days / 7, 1)
    print(f"window {since} to {end} ({days} days)")
    for r in db.all("SELECT id, slug FROM persons ORDER BY id"):
        pid, slug = int(r["id"]), str(r["slug"])
        given = list_changes(db, pid, since, end)
        for name, sql in GIVEN.items():
            given[name] = int(db.scalar(sql, (pid, since, end)) or 0)
        broken = broken_promises(db, pid, since, end)
        built, again = rebuilds(db, pid, since, end)
        print(f"\n{slug}")
        print("  given:", dict(given))
        print(f"  refused food newly on a menu: {len(broken)}", broken or "")
        print(
            f"  builds {built}, rebuilt over planned days {again} ({again / weeks:.1f} a week); "
            f"swaps {given['swaps'] / weeks:.1f} a week"
        )
