"""Linking two people: the rules of `links.py`, with an attacker's eye.

A code is minted by one and entered by the other. Each test tries the thing that should not
work - a guess, an old code, a code used twice, a third person, a self-link - and reads the
ledger afterwards, because a refusal that left no trace is a refusal nobody can audit.
"""

from __future__ import annotations

import sqlite3

import pytest

from tiffin import links
from tiffin.clock import FrozenClock
from tiffin.db import Db
from tiffin.persons import CODE_ALPHABET

from .linking import link_pair


def events(db: Db, link_id: int) -> list[str]:
    return [
        str(r["event"])
        for r in db.all("SELECT event FROM link_events WHERE link_id = ? ORDER BY id", (link_id,))
    ]


def ids(two_persons: dict[str, int]) -> tuple[int, int]:
    return two_persons["owner"], two_persons["partner"]


def link_of(db: Db, code: str) -> int:
    return int(db.scalar("SELECT id FROM links WHERE code_sha256 = ?", (links.code_hash(code),)))


# -- the happy path --------------------------------------------------------------------------


def test_one_makes_a_code_and_the_other_enters_it(fixture_db: Db, two_persons) -> None:
    a, b = ids(two_persons)
    code = links.mint(fixture_db, a, "telegram")
    assert len(code) == 9 and code[4] == "-"
    assert set(code.replace("-", "")) <= set(CODE_ALPHABET)
    assert links.state(fixture_db, a).kind == "offered"
    assert links.partner_of(fixture_db, a) is None
    link_id = links.accept(fixture_db, b, code.lower(), "web")  # case and the dash do not matter
    assert links.partner_of(fixture_db, a) == b and links.partner_of(fixture_db, b) == a
    assert events(fixture_db, link_id) == ["offered", "accepted"]
    assert links.state(fixture_db, b).kind == "linked"


def test_only_the_hash_of_a_code_is_stored(fixture_db: Db, two_persons) -> None:
    a, _b = ids(two_persons)
    code = links.mint(fixture_db, a, "web")
    plain = code.replace("-", "")
    stored = fixture_db.all("SELECT * FROM links")
    assert len(stored) == 1
    assert plain not in " ".join(str(v) for v in dict(stored[0]).values())
    assert stored[0]["code_sha256"] == links.code_hash(code)


def test_codes_are_not_a_pattern(fixture_db: Db, two_persons) -> None:
    a, _b = ids(two_persons)
    seen = set()
    for _ in range(4):
        seen.add(links.mint(fixture_db, a, "web"))
    assert len(seen) == 4


def test_a_partner_who_has_not_joined_gets_words_not_a_code(fixture_db: Db) -> None:
    owner = fixture_db.insert("persons", slug="owner", display_name="A", tone="neutral")
    fixture_db.insert("persons", slug="partner", display_name="B", tone="neutral")
    fixture_db.insert("telegram_identities", person_id=owner, chat_id=1, method="config")
    with pytest.raises(links.LinkError, match="has not joined yet"):
        links.mint(fixture_db, owner, "telegram")
    assert fixture_db.scalar("SELECT COUNT(*) FROM links") == 0


# -- guesses, expiry, reuse --------------------------------------------------------------------


def test_a_wrong_code_is_refused_and_counted(fixture_db: Db, two_persons) -> None:
    a, b = ids(two_persons)
    links.mint(fixture_db, a, "web")
    with pytest.raises(links.LinkError, match="not right"):
        links.accept(fixture_db, b, "ABCD-EFGH", "web")
    assert fixture_db.scalar("SELECT COUNT(*) FROM link_attempts WHERE ok = 0") == 1
    assert links.partner_of(fixture_db, b) is None


def test_guessing_runs_into_a_limit_even_with_the_right_code_after(
    fixture_db: Db, two_persons
) -> None:
    a, b = ids(two_persons)
    code = links.mint(fixture_db, a, "web")
    for guess in ("AAAA-AAAA", "BBBB-BBBB", "CCCC-CCCC", "DDDD-DDDD", "EEEE-EEEE"):
        with pytest.raises(links.LinkError, match="not right"):
            links.accept(fixture_db, b, guess, "web")
    # the sixth try is refused WITHOUT looking at the code, so a lucky guess cannot get through
    with pytest.raises(links.LinkError, match="Too many codes"):
        links.accept(fixture_db, b, code, "web")
    assert links.partner_of(fixture_db, a) is None
    # an hour later the door is open again
    clock = fixture_db.clock
    assert isinstance(clock, FrozenClock)
    clock.advance(minutes=61)
    links.accept(fixture_db, b, code, "web")
    assert links.partner_of(fixture_db, a) == b


def test_malformed_codes_count_as_guesses(fixture_db: Db, two_persons) -> None:
    a, b = ids(two_persons)
    links.mint(fixture_db, a, "web")
    for text in ("", "short", "x" * 500, "'; DROP TABLE links;--"):
        with pytest.raises(links.LinkError):
            links.accept(fixture_db, b, text, "web")
    assert fixture_db.scalar("SELECT COUNT(*) FROM links") == 1


def test_an_expired_code_is_refused_in_words_and_the_expiry_is_written(
    fixture_db: Db, two_persons
) -> None:
    a, b = ids(two_persons)
    code = links.mint(fixture_db, a, "telegram")
    fixture_db.clock.advance(hours=48, seconds=1)  # type: ignore[attr-defined]
    with pytest.raises(links.LinkError, match="run out"):
        links.accept(fixture_db, b, code, "telegram")
    assert events(fixture_db, link_of(fixture_db, code)) == ["offered", "expired"]
    assert links.partner_of(fixture_db, a) is None
    assert links.state(fixture_db, a).kind == "none"


def test_a_code_still_works_just_inside_48_hours(fixture_db: Db, two_persons) -> None:
    a, b = ids(two_persons)
    code = links.mint(fixture_db, a, "telegram")
    fixture_db.clock.advance(hours=47, minutes=59)  # type: ignore[attr-defined]
    links.accept(fixture_db, b, code, "telegram")
    assert links.is_linked(fixture_db)


def test_a_code_works_once(fixture_db: Db, two_persons) -> None:
    a, b = ids(two_persons)
    code = links.mint(fixture_db, a, "telegram")
    links.accept(fixture_db, b, code, "telegram")
    links.unlink(fixture_db, a, "telegram")
    with pytest.raises(links.LinkError, match="used or withdrawn"):
        links.accept(fixture_db, b, code, "telegram")
    assert not links.is_linked(fixture_db), "a spent code must not re-link them"


def test_the_ledger_refuses_two_acceptances_of_one_code(fixture_db: Db, two_persons) -> None:
    """Even if two phones race, the database holds the line."""
    a, b = ids(two_persons)
    code = links.mint(fixture_db, a, "web")
    link_id = link_of(fixture_db, code)
    fixture_db.insert("link_events", link_id=link_id, person_id=b, event="accepted", method="web")
    with pytest.raises(sqlite3.IntegrityError):
        fixture_db.insert(
            "link_events", link_id=link_id, person_id=b, event="accepted", method="web"
        )


def test_a_newer_code_withdraws_the_older_one(fixture_db: Db, two_persons) -> None:
    a, b = ids(two_persons)
    first = links.mint(fixture_db, a, "web")
    second = links.mint(fixture_db, a, "web")
    with pytest.raises(links.LinkError, match="used or withdrawn"):
        links.accept(fixture_db, b, first, "web")
    links.accept(fixture_db, b, second, "web")
    assert events(fixture_db, link_of(fixture_db, first)) == ["offered", "declined"]


def test_a_withdrawn_code_does_not_work(fixture_db: Db, two_persons) -> None:
    a, b = ids(two_persons)
    code = links.mint(fixture_db, a, "web")
    assert links.decline(fixture_db, a, "web") is True
    assert links.decline(fixture_db, a, "web") is False
    with pytest.raises(links.LinkError, match="used or withdrawn"):
        links.accept(fixture_db, b, code, "web")


def test_codes_cannot_be_made_without_limit(fixture_db: Db, two_persons) -> None:
    a, _b = ids(two_persons)
    for _ in range(links.MAX_MINTS):
        links.mint(fixture_db, a, "web")
    with pytest.raises(links.LinkError, match="lot of codes"):
        links.mint(fixture_db, a, "web")


# -- people ----------------------------------------------------------------------------------


def test_you_cannot_link_to_yourself(fixture_db: Db, two_persons) -> None:
    a, _b = ids(two_persons)
    code = links.mint(fixture_db, a, "web")
    with pytest.raises(links.LinkError, match="the code you made"):
        links.accept(fixture_db, a, code, "web")
    assert not links.is_linked(fixture_db)
    with pytest.raises(sqlite3.IntegrityError):
        fixture_db.insert("links", a_person=a, b_person=a, code_sha256="x" * 64, expires_at="2099")


def test_a_third_person_cannot_use_a_code_made_for_someone_else(
    fixture_db: Db, two_persons
) -> None:
    a, _b = ids(two_persons)
    code = links.mint(fixture_db, a, "web")
    # a third person cannot exist: the slug is owner or partner, and a third chat is refused
    with pytest.raises(sqlite3.IntegrityError):
        fixture_db.insert("persons", slug="third", display_name="C", tone="neutral")
    with pytest.raises(sqlite3.IntegrityError):
        fixture_db.insert("telegram_identities", person_id=9999, chat_id=77, method="config")
    # and the maker cannot be the one who enters it
    with pytest.raises(links.LinkError, match="the code you made"):
        links.accept(fixture_db, a, code, "web")
    assert not links.is_linked(fixture_db)


def test_while_linked_you_can_neither_make_nor_enter_a_code(fixture_db: Db, two_persons) -> None:
    a, b = ids(two_persons)
    code = links.mint(fixture_db, a, "web")
    links.accept(fixture_db, b, code, "web")
    with pytest.raises(links.LinkError, match="linked already"):
        links.mint(fixture_db, a, "web")
    with pytest.raises(links.LinkError, match="linked already"):
        links.mint(fixture_db, b, "telegram")
    with pytest.raises(links.LinkError, match="linked already"):
        links.accept(fixture_db, b, "ABCD-EFGH", "web")
    assert fixture_db.scalar("SELECT COUNT(*) FROM links") == 1


def test_an_erased_person_is_not_someone_to_link_to(fixture_db: Db, two_persons) -> None:
    a, b = ids(two_persons)
    fixture_db.execute("DROP TRIGGER trg_persons_noupd")
    fixture_db.execute("UPDATE persons SET erased_at = '2026-09-07 00:00:00' WHERE id = ?", (b,))
    with pytest.raises(links.LinkError, match="has not joined yet"):
        links.mint(fixture_db, a, "web")


# -- unlinking -------------------------------------------------------------------------------


def test_either_side_can_unlink_at_once_with_one_event_row(fixture_db: Db, two_persons) -> None:
    a, b = ids(two_persons)
    for who in (a, b):
        code = links.mint(fixture_db, a, "web")
        link_id = links.accept(fixture_db, b, code, "web")
        before = fixture_db.scalar("SELECT COUNT(*) FROM link_events")
        done = links.unlink(fixture_db, who, "telegram")
        assert fixture_db.scalar("SELECT COUNT(*) FROM link_events") == before + 1
        assert done.other_id == (b if who == a else a)
        assert events(fixture_db, link_id)[-1] == "unlinked"
        assert not links.is_linked(fixture_db)
        assert links.partner_of(fixture_db, a) is None and links.partner_of(fixture_db, b) is None


def test_unlinking_when_not_linked_says_so(fixture_db: Db, two_persons) -> None:
    a, _b = ids(two_persons)
    with pytest.raises(links.LinkError, match="not linked"):
        links.unlink(fixture_db, a, "web")


def test_they_can_link_again_afterwards_with_a_new_code(fixture_db: Db, two_persons) -> None:
    a, b = ids(two_persons)
    first = links.accept(fixture_db, b, links.mint(fixture_db, a, "web"), "web")
    links.unlink(fixture_db, b, "web")
    second = links.accept(fixture_db, a, links.mint(fixture_db, b, "web"), "web")
    assert first != second and links.is_linked(fixture_db)


def test_the_ledgers_are_append_only(fixture_db: Db, two_persons) -> None:
    a, b = ids(two_persons)
    code = links.mint(fixture_db, a, "web")
    links.accept(fixture_db, b, code, "web")
    for sql in (
        "UPDATE links SET expires_at = '2099-01-01 00:00:00'",
        "DELETE FROM links",
        "UPDATE link_events SET event = 'declined'",
        "DELETE FROM link_events",
        "DELETE FROM link_attempts",
    ):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            fixture_db.execute(sql)


# -- the migration hook (what the importer calls) ---------------------------------------------


def test_an_existing_pair_is_recorded_as_linked_without_a_consent_row(
    fixture_db: Db, two_persons
) -> None:
    a, b = ids(two_persons)
    for pid in (a, b):
        fixture_db.insert(
            "consents", person_id=pid, kind="shared_meals", granted=1, wording_version="shared_v1"
        )
    consents = fixture_db.scalar("SELECT COUNT(*) FROM consents")
    link_id = links.record_migrated(fixture_db, a, b)
    assert links.partner_of(fixture_db, a) == b
    rows = fixture_db.all(
        "SELECT event, method, person_id FROM link_events WHERE link_id = ? ORDER BY id", (link_id,)
    )
    assert [(r["event"], r["method"]) for r in rows] == [
        ("offered", "migrated"),
        ("accepted", "migrated"),
    ]
    assert fixture_db.scalar("SELECT COUNT(*) FROM consents") == consents


def test_recording_the_same_pair_twice_is_one_link(fixture_db: Db, two_persons) -> None:
    a, b = ids(two_persons)
    first = links.record_migrated(fixture_db, a, b)
    assert links.record_migrated(fixture_db, b, a) == first
    assert fixture_db.scalar("SELECT COUNT(*) FROM links") == 1


def test_a_migrated_link_can_be_unlinked_like_any_other(fixture_db: Db, two_persons) -> None:
    a, b = ids(two_persons)
    link_pair(fixture_db, a, b)
    links.unlink(fixture_db, a, "telegram")
    assert not links.is_linked(fixture_db)


def test_a_migrated_placeholder_code_cannot_be_typed(fixture_db: Db, two_persons) -> None:
    a, b = ids(two_persons)
    link_pair(fixture_db, a, b)
    links.unlink(fixture_db, a, "web")
    with pytest.raises(links.LinkError, match="not right"):
        links.accept(fixture_db, b, "ABCD-EFGH", "web")


def test_the_migration_hook_refuses_nonsense(fixture_db: Db, two_persons) -> None:
    a, b = ids(two_persons)
    with pytest.raises(links.LinkError):
        links.record_migrated(fixture_db, a, a)
    with pytest.raises(links.LinkError):
        links.record_migrated(fixture_db, a, 9999)
    assert fixture_db.scalar("SELECT COUNT(*) FROM links") == 0
