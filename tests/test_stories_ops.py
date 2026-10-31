"""Twenty operator stories: the box being administered, not the bot being talked to.

`test_edge_cases*.py` is a person with a phone. This file is the other half of the same rule -
degrade to manual, never to silence - seen from the terminal and the service manager: a
migration added on a Sunday, a token file saved by the wrong editor, a hand-edited overlay, an
erasure typed while Tiffin is still running, a log line that must not name anybody.

Each test is one thing an operator or the box actually does that is slightly off the happy
path, and the assertion is what has to be true afterwards.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import logging
import os
import sqlite3

import pytest

from tiffin import admin, doctor, log
from tiffin import config as cfg
from tiffin import db as dbmod
from tiffin import persons as persons_mod
from tiffin.app import App
from tiffin.config import update_override
from tiffin.db import Db
from tiffin.errors import ConfigError, ModelInvalid
from tiffin.heartbeat import Heartbeat
from tiffin.log import DailyJsonHandler
from tiffin.mcp_server import McpServer
from tiffin.model import schemas
from tiffin.persons import PersonRegistry
from tiffin.secrets import Secrets

STAMP = "2026-09-07 19:30:00"

#: one valid row per carve-out table, so the "may change" claim can be exercised rather than
#: inferred from the absence of a trigger
CARVE_OUT_ROWS: dict[str, dict[str, object]] = {
    "icu_wellness": {"day": "2026-09-07", "steps": 11000, "icu_raw_id": 1},
    "icu_activity": {"icu_id": "i1", "day": "2026-09-07", "kind": "climb", "icu_raw_id": 1},
    "bot_state": {"k": "update_offset", "v": "10"},
    "icu_sync_state": {"k": "wellness_cursor", "v": "2026-09-07"},
    "conversation_state": {"state_json": "{}", "updated_at": STAMP},
    "run_ledger": {
        "run_id": "r1",
        "job": "icu_sync",
        "slot": "hourly",
        "started_at": STAMP,
        "status": "ok",
    },
    "schema_version": {"version": 99, "applied_at": STAMP},
}
CARVE_OUT_EDITS: dict[str, str] = {
    "icu_wellness": "steps = 12000",
    "icu_activity": "calories = 700",
    "bot_state": "v = '11'",
    "icu_sync_state": "v = '2026-09-08'",
    "conversation_state": "state_json = '{\"step\": 2}'",
    "run_ledger": "status = 'degraded'",
    "schema_version": "applied_at = '2026-09-08 00:00:00'",
}


def unpaired_persons(db: Db) -> dict[str, int]:
    """Both people, neither holding a Telegram id.

    `telegram_identities` is capped at two rows for the whole database, so the shared
    `two_persons` fixture uses the pairing seats up and nothing can then be paired.
    """
    return {
        slug: db.insert("persons", slug=slug, display_name=name, tone="neutral")
        for slug, name in (("owner", "Alex"), ("partner", "Sam"))
    }


def a_target(db: Db, person_id: int, kcal: int) -> None:
    db.insert(
        "targets",
        person_id=person_id,
        week_start="2026-09-07",
        kcal=kcal,
        protein_g=200,
        fat_g=70,
        carb_g=170,
        fibre_g=30,
        source="formula",
    )


def rpc(method: str, params: dict | None = None, req_id: int = 1) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}


# -- append-only: the triggers, the carve-outs, and what happens when an erasure dies --------


def test_01_a_table_added_without_rerunning_migrate_is_not_yet_a_ledger(
    fixture_db: Db, two_persons: dict[str, int]
) -> None:
    """The append-only rule lives in `migrate()`, not in `schema.sql`.

    A migration that creates a table and is applied by hand - or a schema.sql edit shipped
    without restarting Tiffin - leaves that table an ordinary mutable one. Nothing warns:
    rows can be edited and deleted until the next migrate, and by then the edits are invisible.
    """
    pid = two_persons["owner"]
    fixture_db.con.executescript(
        "CREATE TABLE IF NOT EXISTS person_moods ("
        " id INTEGER PRIMARY KEY,"
        " person_id INTEGER NOT NULL REFERENCES persons(id),"
        " day TEXT NOT NULL, mood TEXT NOT NULL, created_at TEXT NOT NULL);"
    )
    assert "person_moods" in dbmod.ledger_tables(fixture_db.con), "it counts as a ledger at once"
    rid = fixture_db.insert("person_moods", person_id=pid, day="2026-09-07", mood="fine")
    fixture_db.execute("UPDATE person_moods SET mood = 'grim' WHERE id = ?", (rid,))
    assert fixture_db.scalar("SELECT mood FROM person_moods WHERE id = ?", (rid,)) == "grim"

    dbmod.migrate(fixture_db.con, fixture_db.clock)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        fixture_db.execute("UPDATE person_moods SET mood = 'fine' WHERE id = ?", (rid,))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        fixture_db.execute("DELETE FROM person_moods WHERE id = ?", (rid,))


def test_02_the_projection_and_state_tables_really_do_accept_an_update(
    fixture_db: Db, two_persons: dict[str, int]
) -> None:
    """The carve-outs are not a documentation exercise: the hourly intervals.icu sync rewrites
    both projections in place and the scheduler updates a run_ledger row from running to ok.
    Arming either with the ledger triggers would stop those jobs dead, and the existing suite
    only checks that the triggers are absent, never that a write goes through.
    """
    carve_outs = dbmod.PROJECTION_TABLES | dbmod.STATE_TABLES
    assert set(CARVE_OUT_ROWS) == carve_outs, "a new carve-out needs a row here"
    pid = two_persons["owner"]
    for table, row in CARVE_OUT_ROWS.items():
        values = dict(row)
        if "person_id" in fixture_db.columns(table):
            values["person_id"] = pid
        fixture_db.insert(table, **values)
    for table in sorted(carve_outs):
        fixture_db.execute(f"UPDATE {table} SET {CARVE_OUT_EDITS[table]}")
        fixture_db.execute(f"DELETE FROM {table}")
        assert fixture_db.scalar(f"SELECT COUNT(*) FROM {table}") == 0, table
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        fixture_db.execute("UPDATE persons SET tone = 'blunt' WHERE id = ?", (pid,))


def test_03_an_erasure_that_dies_half_way_leaves_every_ledger_armed(
    fixture_db: Db, two_persons: dict[str, int], tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Erasure is the one thing allowed to take the triggers off the whole database.

    If it dies between dropping them and putting them back - a bug in the delete plan, a
    foreign key, the box losing power - every ledger in Tiffin silently becomes an
    ordinary table and stays that way until someone runs migrate again.
    """
    from tiffin import body

    pid = two_persons["owner"]
    body.record_weigh_in(fixture_db, pid, dt.date(2026, 9, 7), 84.0, "manual")
    manifest = tmp_path / "export" / "manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"slug": "owner"}), encoding="utf-8")

    def boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("the delete plan hit a foreign key")

    monkeypatch.setattr(admin, "_erase_rows", boom)
    with pytest.raises(RuntimeError, match="foreign key"):
        admin.erase_person(fixture_db, "owner", manifest_path=manifest)

    assert fixture_db.scalar("SELECT erased_at FROM persons WHERE id = ?", (pid,)) is None
    assert fixture_db.scalar("SELECT COUNT(*) FROM weigh_ins WHERE person_id = ?", (pid,)) == 1
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        fixture_db.execute("DELETE FROM persons WHERE id = ?", (pid,))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        fixture_db.execute("DELETE FROM weigh_ins WHERE person_id = ?", (pid,))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        fixture_db.execute("UPDATE weigh_ins SET weight_kg = 70 WHERE person_id = ?", (pid,))


# -- migrations: the numbers, schema.sql, and running them again ----------------------------


def test_04_the_highest_numbered_migration_is_the_version_the_code_records() -> None:
    """`migrate()` applies every migration numbered above the recorded version and then records
    `SCHEMA_VERSION`. A migration numbered above SCHEMA_VERSION therefore re-applies on every
    single start, because the recorded number never catches up with it; one numbered at or
    below it never reaches a box that is already current. Either way nobody is told.
    """
    numbers = sorted(
        int(p.name[:4]) for p in dbmod.MIGRATIONS_DIR.glob("[0-9][0-9][0-9][0-9]_*.sql")
    )
    assert numbers, "no migrations found; the glob or the directory has moved"
    assert numbers == list(range(1, max(numbers) + 1)), f"gaps or duplicates: {numbers}"
    assert max(numbers) == dbmod.SCHEMA_VERSION, (
        f"highest migration {max(numbers)} but SCHEMA_VERSION {dbmod.SCHEMA_VERSION}"
    )


def test_05_a_fresh_install_and_an_upgraded_box_end_up_with_the_same_columns(
    fixture_db: Db,
) -> None:
    """The house rule - a schema change edits schema.sql AND adds a numbered migration.

    A column added only in a migration is invisible here, because `migrate()` runs both - but
    it means schema.sql no longer describes the database, so anyone reading it to answer a
    question reads a lie, and a restore from `tiffin.sql` comes back a column short.
    """
    bare = sqlite3.connect(":memory:")
    try:
        bare.executescript(dbmod.schema_sql())
        from_schema = {t: dbmod.columns(bare, t) for t in dbmod.tables(bare)}
    finally:
        bare.close()
    migrated = {t: dbmod.columns(fixture_db.con, t) for t in dbmod.tables(fixture_db.con)}
    assert set(from_schema) == set(migrated), sorted(set(migrated) ^ set(from_schema))
    differences = {
        t: (from_schema[t], migrated[t]) for t in migrated if from_schema[t] != migrated[t]
    }
    assert not differences, differences


def test_06_a_database_whose_version_ledger_was_lost_migrates_again_without_damage(
    fixture_db: Db, two_persons: dict[str, int]
) -> None:
    """`schema_version` is a state table, so it can be emptied - by a restore from a partial
    dump, or by a hand that meant to delete one row. The next start then replays every
    initial schema over a database full of rows. The schema must be idempotent and
    recreate the append-only triggers without changing those rows.
    """
    pid = two_persons["owner"]
    doc = fixture_db.insert(
        "documents",
        person_id=pid,
        file_name="plan.pdf",
        mime_type="application/pdf",
        sha256="a" * 64,
        n_bytes=1024,
        n_chars=64,
        text="Protein at every meal.",
    )
    fixture_db.insert(
        "document_facts",
        document_id=doc,
        person_id=pid,
        kind="note",
        payload_json=json.dumps({"text": "Protein at every meal."}),
        label="a note",
    )
    tables_before = set(dbmod.tables(fixture_db.con))

    fixture_db.execute("DELETE FROM schema_version")
    assert dbmod.current_version(fixture_db.con) == 0
    assert dbmod.migrate(fixture_db.con, fixture_db.clock) == dbmod.SCHEMA_VERSION
    assert dbmod.current_version(fixture_db.con) == dbmod.SCHEMA_VERSION

    assert set(dbmod.tables(fixture_db.con)) == tables_before, "a rebuild left a table behind"
    assert fixture_db.scalar("SELECT COUNT(*) FROM document_facts") == 1
    assert fixture_db.scalar("SELECT COUNT(*) FROM documents") == 1
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        fixture_db.execute("DELETE FROM document_facts")


# -- secrets: files an editor wrote, and what doctor says about them -------------------------


def test_07_a_token_file_saved_with_a_byte_order_mark_is_still_the_token(tmp_path) -> None:
    """Windows editors and older PowerShell write UTF-8 with a BOM. The BOM is invisible, it is
    not whitespace so `.strip()` leaves it, and the rule here is "compare by SHA-256, never
    print" - so nobody can eyeball the difference. The bot then gets a 401 from Telegram and
    the digest never matches the one that was written down.
    """
    secrets = Secrets(tmp_path)
    (tmp_path / "telegram-token-dev.txt").write_text("﻿123456:ABCDEF\n", encoding="utf-8")
    assert (tmp_path / "telegram-token-dev.txt").read_bytes().startswith(b"\xef\xbb\xbf")
    clean = Secrets(tmp_path / "clean")
    (tmp_path / "clean").mkdir()
    (tmp_path / "clean" / "telegram-token-dev.txt").write_text("123456:ABCDEF", encoding="utf-8")
    assert secrets.get("telegram_token_dev") == "123456:ABCDEF", "a BOM is not part of the token"
    assert secrets.sha256("telegram_token_dev") == clean.sha256("telegram_token_dev")


def test_08_a_secret_survives_the_newline_and_spaces_an_editor_leaves_behind(tmp_path) -> None:
    canonical = Secrets(tmp_path / "canonical")
    (tmp_path / "canonical").mkdir()
    (tmp_path / "canonical" / "icu-key-owner.txt").write_text("icu-key-1234", encoding="utf-8")
    want = canonical.sha256("icu_key_owner")
    for i, written in enumerate(("icu-key-1234\n", "icu-key-1234\r\n", "  icu-key-1234  \n\n")):
        d = tmp_path / f"v{i}"
        d.mkdir()
        (d / "icu-key-owner.txt").write_text(written, encoding="utf-8")
        secrets = Secrets(d)
        assert secrets.get("icu_key_owner") == "icu-key-1234", written
        assert secrets.sha256("icu_key_owner") == want, written
        assert secrets.icu_key("owner") == "icu-key-1234"


def test_09_a_token_file_that_is_only_a_newline_is_reported_missing(tmp_path) -> None:
    """`doctor` asks `missing()`, which asks `exists()`, which only looks at the file size;
    `run` asks `get()`, which strips first. A file holding one newline is therefore green on
    the health check and fatal at startup - the exact shape of failure "degrade to manual,
    never to silence" exists to stop. The secret's value must never be in the message either.
    """
    secrets = Secrets(tmp_path)
    assert secrets.exists("telegram_token") is False
    assert secrets.optional("telegram_token") is None
    with pytest.raises(ConfigError, match=r"telegram-token\.txt") as caught:
        secrets.get("telegram_token")
    assert str(tmp_path) not in str(caught.value), "an error names the file, not the vault"

    (tmp_path / "telegram-token.txt").write_text("", encoding="utf-8")
    assert secrets.missing(["telegram_token"]) == ["telegram_token"]

    (tmp_path / "telegram-token.txt").write_text("\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="missing or empty"):
        secrets.get("telegram_token")
    assert secrets.missing(["telegram_token"]) == ["telegram_token"], (
        "doctor must not call a blank token present"
    )


# -- config: the launcher rule, and an overlay somebody edited by hand -----------------------


def test_11_an_overlay_the_writer_cannot_represent_is_refused_not_rewritten(tmp_path) -> None:
    """The config writer reads the box overlay, merges and writes the whole file back. The
    writer is deliberately small, and anything it cannot represent - an array of tables a
    person added by hand - must stop the write rather than vanish from the file that is
    rewritten over it.
    """
    overlay = tmp_path / "tiffin.toml"
    overlay.write_text(
        '[persons.owner]\ndisplay_name = "Alex"\n\n[[retailers]]\nname = "Tesco"\n',
        encoding="utf-8",
    )
    original = overlay.read_text(encoding="utf-8")
    with pytest.raises(ConfigError, match="cannot write"):
        update_override(overlay, {"persons": {"partner": {"display_name": "Sam"}}})
    assert overlay.read_text(encoding="utf-8") == original, "a refused write changes nothing"


def test_12_a_hand_edited_budget_of_the_wrong_shape_names_the_key(tmp_path) -> None:
    (tmp_path / "tiffin.toml").write_text('[budgets]\nchat = "fast"\n', encoding="utf-8")
    config = cfg.load(root=tmp_path, environ={})
    with pytest.raises(ConfigError, match=r"budgets\.chat"):
        config.budget("chat")
    with pytest.raises(ConfigError, match=r"missing config key: budgets\.nope"):
        config.budget("nope")
    with pytest.raises(ConfigError, match="not a table"):
        config.section("web.port_dev")
    assert config.budget("menu_propose").turns >= 1, "the sibling budgets still read"


# -- logging: what a line is allowed to carry, and what prune is allowed to delete -----------


def test_13_a_log_line_carries_the_person_id_and_nothing_a_caller_attached(
    tmp_path, frozen_clock
) -> None:
    """Logs carry person_id integers only, never names, logins or chat ids, and never message
    bodies. The guarantee has to be the formatter's, not every caller's: a job that attaches a
    chat id to a record - to debug a send, say - must not thereby write it to disk.
    """
    log_dir = tmp_path / "logs"
    logger = logging.getLogger("tiffin.stories.log")
    logger.propagate = False
    logger.setLevel(logging.INFO)
    handler = DailyJsonHandler(log_dir, frozen_clock)
    logger.addHandler(handler)
    try:
        with log.context(run_id="icu_sync:2026-09-07", person_id=2):
            logger.info(
                "activity synced",
                extra={
                    "event": "icu_sync",
                    "chat_id": 1000002,
                    "display_name": "Sam",
                    "body": "two beers and a kebab",
                },
            )
    finally:
        logger.removeHandler(handler)
        handler.close()

    written = (log_dir / "tiffin-2026-09-07.jsonl").read_text(encoding="utf-8")
    payload = json.loads(written.strip())
    assert payload["person_id"] == 2 and isinstance(payload["person_id"], int)
    assert payload["event"] == "icu_sync"
    assert payload["run_id"] == "icu_sync:2026-09-07"
    assert set(payload) == {"ts", "level", "logger", "event", "run_id", "person_id", "msg"}
    for leak in ("Sam", "1000002", "two beers", "kebab"):
        assert leak not in written, leak
    assert "T" not in payload["ts"] and len(payload["ts"]) == 19


def test_14_pruning_logs_keeps_ninety_days_and_touches_nothing_else(tmp_path, frozen_clock) -> None:
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    for day in ("2026-09-07", "2026-06-09", "2026-06-08", "2025-12-31"):
        (log_dir / f"tiffin-{day}.jsonl").write_text("{}\n", encoding="utf-8")
    (log_dir / "notify.log").write_text("not mine", encoding="utf-8")
    (log_dir / "tiffin-old.jsonl").write_text("not a dated name", encoding="utf-8")

    removed = log.prune(log_dir, keep_days=90, clock=frozen_clock)
    assert removed == 2
    assert sorted(p.name for p in log_dir.iterdir()) == [
        "notify.log",
        "tiffin-2026-06-09.jsonl",
        "tiffin-2026-09-07.jsonl",
        "tiffin-old.jsonl",
    ]
    assert log.prune(tmp_path / "never-logged", keep_days=90, clock=frozen_clock) == 0


# -- the model boundary: schema complaints, and the server that answers for one person -------


def test_15_a_schema_complaint_names_the_rule_not_the_persons_own_words() -> None:
    """A validation failure is logged by the gateway and carried back into the retry prompt, so
    whatever is in its text leaves the schema layer. `schemas` says so itself: the error names
    "the path and the message, never the payload". If the instance is quoted, a line about her
    weight or what she ate lands in tiffin-<date>.jsonl, which no log line may ever hold.
    """
    private = "two pints of lager after the climbing session, and her weight is 61.2 kg. "
    bad = {"lines": [private * 3]}  # over the 200-character line cap
    problems = schemas.errors(schemas.load("sunday_prose"), bad)
    assert problems and problems[0].startswith("lines/0:")
    joined = " ".join(problems)
    assert "lager" not in joined and "61.2" not in joined, joined
    with pytest.raises(ModelInvalid) as caught:
        schemas.validate("sunday_prose", bad)
    assert "lager" not in str(caught.value) and "61.2" not in str(caught.value)


def test_16_the_mcp_server_answers_for_its_own_person_whatever_the_call_says(
    fixture_db: Db, two_persons: dict[str, int]
) -> None:
    """`claude -p` reaches Tiffin through this server, and the person it speaks for is
    fixed by TIFFIN_PERSON_ID in the environment. A model that has seen both names in a
    conversation can put the other id in the arguments; the tool list must not even offer it.
    """
    a_target(fixture_db, two_persons["owner"], 2100)
    a_target(fixture_db, two_persons["partner"], 1600)
    server = McpServer(fixture_db, two_persons["owner"])

    listed = server.handle(rpc("tools/list"))
    assert listed is not None
    for tool in listed["result"]["tools"]:
        assert "person_id" not in tool["inputSchema"]["properties"], tool["name"]
        assert tool["inputSchema"]["additionalProperties"] is False

    out = server.handle(
        rpc(
            "tools/call",
            {
                "name": "get_targets",
                "arguments": {"person_id": two_persons["partner"], "day": None},
            },
            req_id=2,
        )
    )
    assert out is not None and out["result"]["isError"] is False
    payload = json.loads(out["result"]["content"][0]["text"])
    assert payload["targets"]["kcal"] == 2100, "the environment names the person, not the call"
    assert "person_id" not in payload["targets"]


def test_17_a_tool_that_blows_up_is_an_error_string_and_the_server_keeps_serving(
    fixture_db: Db, two_persons: dict[str, int]
) -> None:
    """The MCP server is a child process of one `claude -p` turn. If a tool call kills it, the
    turn dies with no answer at all; if it comes back as text, the model can say what went
    wrong and the person still gets a reply.
    """
    dead = Db(fixture_db.path)
    dead.close()
    server = McpServer(dead, two_persons["owner"])

    crashed = server.handle(rpc("tools/call", {"name": "get_targets", "arguments": {}}))
    assert crashed is not None and crashed["result"]["isError"] is True
    assert "ProgrammingError" in crashed["result"]["content"][0]["text"]

    unknown = server.handle(rpc("tools/call", {"name": "drop_tables", "arguments": {}}, req_id=2))
    assert unknown is not None and unknown["result"]["isError"] is True
    assert "unknown tool" in unknown["result"]["content"][0]["text"]

    assert server.handle(rpc("ping", req_id=3)) == {"jsonrpc": "2.0", "id": 3, "result": {}}
    missing = server.handle(rpc("tools/erase", req_id=4))
    assert missing is not None and missing["error"]["code"] == -32601


# -- erasure, pairing and the health check ---------------------------------------------------


def test_18_an_erasure_is_refused_while_the_tiffin_is_still_running(tmp_path) -> None:
    """Erasure drops every append-only trigger in the database and then deletes by person_id.
    A tiffin still writing underneath that is the one way to lose rows nobody asked to lose,
    so the preconditions are the whole safety catch: the flag is set and the process is gone.
    A pid file nobody can parse is not evidence that it is gone.
    """
    state = tmp_path / "state"
    state.mkdir()
    assert "maintenance" in (admin.erase_preconditions(state) or "")

    (state / "maintenance").write_text("", encoding="utf-8")
    assert admin.erase_preconditions(state) is None, "flag set, nothing running"

    pid_file = state / "tiffin.pid"
    pid_file.write_text(
        json.dumps({"pid": os.getpid(), "start_utc": STAMP, "exe": "tiffin.exe"}),
        encoding="utf-8",
    )
    Heartbeat(state).beat()
    running = admin.erase_preconditions(state)
    assert running is not None and ("alive" in running or "heartbeat" in running), running

    pid_file.write_text("{ half a json objec", encoding="utf-8")
    assert admin.erase_preconditions(state) is not None, (
        "an unreadable pid file with a fresh heartbeat is not proof Tiffin stopped"
    )


def test_19_a_pairing_code_is_shown_once_stored_as_a_hash_and_spent_on_first_use(
    fixture_db: Db, config, frozen_clock
) -> None:
    """The code is the whole of her authentication: whoever sends it becomes her as far as the
    bot is concerned. It is printed to a terminal once and must not be recoverable from the
    database afterwards, and a second sender must not be able to reuse it.
    """
    ids = unpaired_persons(fixture_db)
    registry = PersonRegistry(fixture_db, config, clock=frozen_clock)
    code = registry.create_pairing_code("partner", ttl_hours=48)

    dump = "\n".join(fixture_db.con.iterdump())
    assert persons_mod.code_hash(code) in dump, "the hash is what is stored"
    assert code not in dump and persons_mod.normalise_code(code) not in dump

    paired = registry.try_pair(9001, code.lower())
    assert paired is not None and paired.id == ids["partner"]
    assert registry.try_pair(9002, code) is None, "a spent code pairs nobody"
    assert fixture_db.scalar("SELECT COUNT(*) FROM telegram_identities") == 1

    later = registry.create_pairing_code("owner", ttl_hours=1)
    frozen_clock.advance(hours=2)
    assert registry.try_pair(9003, later) is None, "an expired code pairs nobody"


def test_20_doctor_on_a_file_that_is_not_the_tiffins_database_says_red(
    tmp_path, config, frozen_clock
) -> None:
    """The first thing typed when the box looks wrong. Pointed at the wrong root - a restore
    half done, TIFFIN_HOME set to somewhere else - it must report red and carry on through the
    rest of the checks, rather than raising and telling nobody about the disk or the secrets.
    """
    secrets_dir = tmp_path / "sec"
    secrets_dir.mkdir()
    for name in ("telegram-token.txt", "telegram-token-dev.txt", "telegram-admin-chat-id.txt"):
        (secrets_dir / name).write_text("x", encoding="utf-8")
    local = dataclasses.replace(
        config, paths=dataclasses.replace(config.paths, secrets=secrets_dir)
    )
    local.paths.ensure()
    foreign = sqlite3.connect(str(local.paths.db))
    try:
        foreign.executescript("CREATE TABLE notes(id INTEGER PRIMARY KEY, body TEXT);")
    finally:
        foreign.close()

    app = App(local, Secrets(secrets_dir), clock=frozen_clock)
    checks = {c.name: c for c in doctor.run_checks(app, online=False)}
    assert checks["Telegram token"].status == "ok"
    assert checks["database"].status == "red", checks["database"].detail
    assert checks["database"].detail, "red with no detail says nothing"
    assert "persons" not in checks, "the person checks cannot run on a database that is not ours"
    assert checks["disk"].status in ("ok", "warn")
    assert doctor.main(app, strict=False, online=False) == 1
