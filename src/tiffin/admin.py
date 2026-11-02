"""Owner-only export and erasure commands.

Erasure requires an export manifest. It removes a person's ledger rows in one transaction with
append-only triggers temporarily disabled, then restores the triggers and clears stored sidecars.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from . import db as dbmod
from . import links
from .clock import DEFAULT_CLOCK, Clock
from .db import Db

PERSON_SLUG_TABLES = ("exports", "erasures")  # keyed by slug, never erased
HEARTBEAT_FRESH_S = 120  # a beat younger than this means Tiffin is still running


@dataclass
class ExportManifest:
    slug: str
    path: Path
    tables: dict[str, int]
    messages: int
    made_at: str

    def as_dict(self) -> dict:
        return {
            "slug": self.slug,
            "path": str(self.path),
            "tables": self.tables,
            "messages": self.messages,
            "made_at": self.made_at,
        }


def person_tables(con: sqlite3.Connection) -> list[str]:
    return [t for t in dbmod.tables(con) if "person_id" in dbmod.columns(con, t)]


def _person(db: Db, slug: str) -> sqlite3.Row:
    row = db.one("SELECT * FROM persons WHERE slug = ?", (slug,))
    if row is None:
        raise LookupError(f"no person {slug}")
    return row


def _chat_ids(db: Db, person_id: int) -> list[int]:
    return [
        int(r["chat_id"])
        for r in db.all("SELECT chat_id FROM telegram_identities WHERE person_id = ?", (person_id,))
    ]


def child_selects(db: Db, person_id: int, chat_ids: list[int]) -> dict[str, tuple[str, tuple]]:
    """Tables without person_id that still hold this person's rows: (where-clause, params)."""
    ids = ",".join("?" for _ in chat_ids) or "NULL"
    return {
        "messages_in": (f"chat_id IN ({ids})", tuple(chat_ids)),
        "inbound_outcomes": (
            f"message_in_id IN (SELECT id FROM messages_in WHERE chat_id IN ({ids}))",
            tuple(chat_ids),
        ),
        "voice_transcripts": (
            f"message_in_id IN (SELECT id FROM messages_in WHERE chat_id IN ({ids}))",
            tuple(chat_ids),
        ),
        "pairing_uses": (
            "pairing_code_id IN (SELECT id FROM pairing_codes WHERE person_id = ?)",
            (person_id,),
        ),
        "web_link_uses": (
            "link_id IN (SELECT id FROM web_login_links WHERE person_id = ?)",
            (person_id,),
        ),
        "web_device_events": (
            "device_id IN (SELECT id FROM web_devices WHERE person_id = ?)",
            (person_id,),
        ),
        "web_device_seen": (
            "device_id IN (SELECT id FROM web_devices WHERE person_id = ?)",
            (person_id,),
        ),
        "intent_decisions": (
            "intent_id IN (SELECT id FROM intents WHERE person_id = ?)",
            (person_id,),
        ),
        "proposal_decisions": (
            "proposal_id IN (SELECT id FROM proposals WHERE person_id = ?)",
            (person_id,),
        ),
        "menu_decisions": ("menu_id IN (SELECT id FROM menus WHERE person_id = ?)", (person_id,)),
        # a week of choices, deepest first: picks and outcome, then the options, then the set
        "choice_picks": (
            "set_id IN (SELECT id FROM choice_sets WHERE person_id = ?)",
            (person_id,),
        ),
        "choice_outcomes": (
            "set_id IN (SELECT id FROM choice_sets WHERE person_id = ?)",
            (person_id,),
        ),
        "choice_options": (
            "set_id IN (SELECT id FROM choice_sets WHERE person_id = ?)",
            (person_id,),
        ),
        "meal_slots": (
            "menu_day_id IN (SELECT md.id FROM menu_days md "
            "JOIN menus m ON m.id = md.menu_id WHERE m.person_id = ?)",
            (person_id,),
        ),
        "menu_days": ("menu_id IN (SELECT id FROM menus WHERE person_id = ?)", (person_id,)),
        # a joint weekend list is written with person_id NULL and menu_id pointing at ONE
        # person's menu (`shopping.joint_weekend` - the schema has no household owner). So
        # `person_id = ?` missed it in both directions: its items survived, its list row
        # survived, and `menus` was emptied underneath it. `defer_foreign_keys` then made the
        # COMMIT fail, after every delete had already happened. A list anchored to her menu
        # goes with her menu.
        "shopping_items": (
            "list_id IN (SELECT id FROM shopping_lists WHERE person_id = ? "
            "OR menu_id IN (SELECT id FROM menus WHERE person_id = ?))",
            (person_id, person_id),
        ),
        "shopping_lists": (
            "person_id = ? OR menu_id IN (SELECT id FROM menus WHERE person_id = ?)",
            (person_id, person_id),
        ),
        "recipe_ingredients": (
            "recipe_id IN (SELECT id FROM recipes "
            "WHERE person_scope = (SELECT slug FROM persons WHERE id = ?))",
            (person_id,),
        ),
        "recipes": ("person_scope = (SELECT slug FROM persons WHERE id = ?)", (person_id,)),
        "conversation_state": ("person_id = ?", (person_id,)),
    }


def export_person(
    db: Db, slug: str, out_root: Path, *, clock: Clock = DEFAULT_CLOCK
) -> ExportManifest:
    person = _person(db, slug)
    pid = int(person["id"])
    stamp = clock.now_utc().strftime("%Y%m%dT%H%M%S")
    out = out_root / f"{slug}-{stamp}"
    (out / "tables").mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    # `child_selects` FIRST, and `person_tables` skipping what it covered. Where both name a
    # table, the child clause is the wider one - `shopping_lists` is hers by `person_id` OR by
    # holding her menu - and running the narrow one first meant the wider rows were left out of
    # the export and then deleted by the erasure. She has to be given what is about to go.
    for table, (where, params) in child_selects(db, pid, _chat_ids(db, pid)).items():
        rows = db.all(f"SELECT * FROM {table} WHERE {where} ORDER BY rowid", params)
        with (out / "tables" / f"{table}.jsonl").open("w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(dict(r), ensure_ascii=False, default=str) + "\n")
        counts[table] = len(rows)
    for table in person_tables(db.con):
        if table in PERSON_SLUG_TABLES or table in counts:
            continue
        rows = db.all(f"SELECT * FROM {table} WHERE person_id = ? ORDER BY rowid", (pid,))
        with (out / "tables" / f"{table}.jsonl").open("w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(dict(r), ensure_ascii=False, default=str) + "\n")
        counts[table] = len(rows)
    msgs = db.all(
        "SELECT created_at, kind, text_html, status FROM messages_out "
        "WHERE person_id = ? ORDER BY id",
        (pid,),
    )
    with (out / "messages.md").open("w", encoding="utf-8") as fh:
        fh.write(f"# Messages sent to {person['display_name']}\n\n")
        for m in msgs:
            fh.write(f"## {m['created_at']} {m['kind']} ({m['status']})\n\n{m['text_html']}\n\n")
    manifest = ExportManifest(slug, out, counts, len(msgs), clock.utc_stamp())
    (out / "manifest.json").write_text(json.dumps(manifest.as_dict(), indent=1), encoding="utf-8")
    db.insert(
        "exports",
        person_slug=slug,
        path=str(out),
        manifest_json=json.dumps(manifest.as_dict()),
        at=clock.utc_stamp(),
    )
    return manifest


def erase_person(
    db: Db,
    slug: str,
    *,
    manifest_path: Path,
    streams_dir: Path | None = None,
    backups_dir: Path | None = None,
    clock: Clock = DEFAULT_CLOCK,
) -> dict:
    if not manifest_path.is_file():
        raise PermissionError("erase refused: export first (no manifest)")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("slug") != slug:
        raise PermissionError("erase refused: manifest is for another person")
    person = _person(db, slug)
    pid = int(person["id"])
    chat_ids = _chat_ids(db, pid)
    ledgers = dbmod.ledger_tables(db.con)
    counts: dict[str, int] = {}
    # executescript COMMITs whatever is open before it runs its first statement, so dropping
    # the triggers from inside `db.tx()` ended the transaction under it: the deletes then ran
    # in autocommit and the closing COMMIT raised "cannot commit - no transaction is active".
    # Every erasure did its work and then reported failure. Triggers off, work in one
    # transaction, triggers back on whatever happens.
    db.con.executescript("".join(dbmod.drop_trigger_sql(t) for t in ledgers))
    try:
        _erase_rows(db, pid, chat_ids, counts, slug, manifest_path, streams_dir, backups_dir, clock)
    except BaseException:
        # `executescript` in the `finally` below COMMITs whatever is still open before its first
        # statement. So a transaction that raised on the way out - a deferred foreign key failing
        # at COMMIT - had its rollback turned into a commit by the trigger restore, and the
        # operator was told the erasure failed while every delete had in fact landed. Roll back
        # explicitly first, while there is still something to roll back.
        db.con.rollback()
        db.con.executescript("".join(dbmod.trigger_sql(t) for t in ledgers))
        raise
    db.con.executescript("".join(dbmod.trigger_sql(t) for t in ledgers))
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    db.execute("VACUUM")
    removed: list[str] = []
    if streams_dir is not None and (streams_dir / slug).exists():
        shutil.rmtree(streams_dir / slug, ignore_errors=True)
        removed.append(str(streams_dir / slug))
    if backups_dir is not None and backups_dir.exists():
        for d in backups_dir.iterdir():
            if d.is_dir():
                shutil.rmtree(d, ignore_errors=True)
                removed.append(str(d))
    return {"slug": slug, "rows": counts, "removed": removed, "at": clock.utc_stamp()}


def _erase_rows(
    db: Db,
    pid: int,
    chat_ids: list[int],
    counts: dict[str, int],
    slug: str,
    manifest_path: Path,
    streams_dir: Path | None,
    backups_dir: Path | None,
    clock: Clock,
) -> None:
    with db.tx():
        # Menus before menu days: the delete order is
        # whatever sqlite_master happens to list, so the foreign keys are checked once at
        # COMMIT rather than after each statement. Nothing here was ever tested end to end.
        db.execute("PRAGMA defer_foreign_keys = ON")
        # a person leaving ends their link first, so the other is not left linked to nobody; the
        # event has no person on it, because the rows that name this person are about to go
        from .food import shopping  # noqa: PLC0415 (food imports the db layer)

        linked = links.active_link(db)
        if linked is not None and pid in (int(linked["a_person"]), int(linked["b_person"])):
            db.insert(
                "link_events", link_id=int(linked["id"]), person_id=None,
                event="unlinked", method="system",
            )  # fmt: skip
            shopping.close_joint(db, int(linked["id"]), (), clock.today_local())
        # A login rebound from another person must not become active again when its newest
        # identity row is erased. Remove every older attachment of each login this person owns.
        current_logins = db.all(
            "SELECT t.login FROM tailscale_identities t WHERE t.person_id = ? "
            "AND t.id = (SELECT MAX(q.id) FROM tailscale_identities q "
            "WHERE q.login = t.login COLLATE NOCASE)",
            (pid,),
        )
        for login_row in current_logins:
            login = str(login_row["login"])
            cur = db.execute(
                "DELETE FROM tailscale_identities WHERE login = ? COLLATE NOCASE", (login,)
            )
            counts["tailscale_identities"] = counts.get("tailscale_identities", 0) + cur.rowcount
            db.execute(
                "UPDATE persons SET tailscale_login = NULL "
                "WHERE tailscale_login = ? COLLATE NOCASE",
                (login,),
            )
        for table, (where, params) in child_selects(db, pid, chat_ids).items():
            cur = db.execute(f"DELETE FROM {table} WHERE {where}", params)
            counts[table] = cur.rowcount
        for table in person_tables(db.con):
            if table in PERSON_SLUG_TABLES or table == "persons":
                continue
            cur = db.execute(f"DELETE FROM {table} WHERE person_id = ?", (pid,))
            counts[table] = counts.get(table, 0) + cur.rowcount
        db.execute(
            "UPDATE persons SET erased_at = ?, tailscale_login = NULL, display_name = ? "
            "WHERE id = ?",
            (clock.utc_stamp(), slug, pid),
        )
        # ask before COMMIT does. `defer_foreign_keys` means a row left pointing at something
        # deleted surfaces as a bare "FOREIGN KEY constraint failed" from the commit, naming
        # nothing; this names the table and the parent, and rolls back cleanly.
        dangling = db.all("PRAGMA foreign_key_check")
        if dangling:
            where = ", ".join(f"{r[0]} -> {r[2]}" for r in dangling[:4])
            raise sqlite3.IntegrityError(
                f"erase would leave {len(dangling)} row(s) pointing at deleted data: {where}. "
                "A table holding this person's rows is missing from child_selects."
            )
        files: list[str] = []
        if streams_dir is not None:
            files.append(str(streams_dir / slug))
        if backups_dir is not None:
            files.append(str(backups_dir))
        db.insert(
            "erasures",
            person_slug=slug,
            export_path=str(manifest_path.parent),
            row_counts_json=json.dumps(counts),
            files_json=json.dumps(files),
            at=clock.utc_stamp(),
        )


def erase_preconditions(state_dir: Path) -> str | None:
    """None when Tiffin is stopped under a maintenance flag; otherwise the reason to refuse."""
    if not (state_dir / "maintenance").exists():
        return "state/maintenance flag missing (stop Tiffin and create it first)"
    pid_file = state_dir / "tiffin.pid"
    if pid_file.exists():
        try:
            info = json.loads(pid_file.read_text(encoding="utf-8"))
            import psutil  # type: ignore[import-not-found]  # noqa: PLC0415

            if psutil.pid_exists(int(info["pid"])):
                return f"tiffin pid {info['pid']} is still alive"
        except ImportError, OSError, ValueError, KeyError, TypeError:
            pass
    hb = state_dir / "heartbeat"
    if hb.exists():
        age = DEFAULT_CLOCK.now_utc().timestamp() - hb.stat().st_mtime
        if age < HEARTBEAT_FRESH_S:
            return "heartbeat is fresh; Tiffin looks alive"
    return None
