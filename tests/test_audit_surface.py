"""A systematic sweep of the whole bot surface, enumerated from the code rather than listed.

Point fixes only ever reach the paths someone happened to walk. This drives EVERY registered
command and EVERY registered callback through the real handler chain, in four states a person
is actually in, and asserts the properties that must hold everywhere:

  - nothing raises, so no interaction ends in "something went wrong";
  - nothing is silent, so no interaction ends in nothing at all;
  - no reply names a command, screen or place that does not exist;
  - no reply leaks internal vocabulary or raw markup onto a phone.

Adding a command or a callback without thinking about the empty case, the junk case or the
not-onboarded case now fails here rather than being found by someone using it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from tiffin.app import App
from tiffin.clock import FrozenClock
from tiffin.db import Db
from tiffin.secrets import Secrets
from tiffin.telegram.poller import Poller

from .fakes import FakeTelegram

OWNER_CHAT = 1000001

#: internal vocabulary that must never reach a phone. These are words the code uses for its
#: own bookkeeping; a person has never been shown them and cannot act on them.
JARGON = (
    "run_ledger",
    "conversation_state",
    "person_id",
    "due_key",
    "traceback",
    "sqlite",
    "none units",
    "nonetype",
)
#: markup that arrived escaped when it should have been rendered, or vice versa
BAD_MARKUP = ("&lt;code&gt;", "&lt;b&gt;", "<code>/set VALUE</code>")


def a_person(db: Db, pid: int, *, onboarded: bool) -> None:
    if not onboarded:
        return
    db.insert(
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
    )
    db.insert(
        "targets",
        person_id=pid,
        week_start="2026-09-07",
        kcal=2250,
        protein_g=200,
        fat_g=70,
        carb_g=200,
        fibre_g=30,
        source="formula",
    )


@pytest.fixture
def world(fixture_db: Db, two_persons, frozen_clock: FrozenClock, config, tmp_path: Path):
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
    handlers = app.handlers(fixture_db)
    poller = Poller(
        app.api(),
        fixture_db,
        app.registry(fixture_db),
        handlers.handle,
        clock=frozen_clock,
        sleeper=lambda s: None,
    )
    n = {"i": 0}

    def send(text: str) -> list[str]:
        n["i"] += 1
        before = len(tg.sent)
        tg.queue_update(tg.text_update(n["i"], OWNER_CHAT, text))
        poller.run_once()
        return [m["text"] for m in tg.sent[before:]]

    def tap(data: str) -> list[str]:
        n["i"] += 1
        before = len(tg.sent)
        tg.queue_update(tg.callback_update(n["i"], OWNER_CHAT, data, message_id=n["i"]))
        poller.run_once()
        return [m["text"] for m in tg.sent[before:]]

    return handlers, send, tap, fixture_db, two_persons["owner"]


def commands(handlers: Any) -> list[str]:
    return sorted(handlers.commands)


def callbacks(handlers: Any) -> list[str]:
    return sorted(handlers.callbacks)


def check(replies: list[str], what: str, known: set[str]) -> list[str]:
    """Every property a reply must have, wherever it came from."""
    problems: list[str] = []
    if not replies:
        problems.append(f"{what}: said nothing at all")
        return problems
    joined = " ".join(replies)
    low = joined.lower()
    for word in JARGON:
        if word in low:
            problems.append(f"{what}: leaked {word!r}")
    for bad in BAD_MARKUP:
        if bad in joined:
            problems.append(f"{what}: markup arrived as {bad!r}")
    if "went wrong" in low:
        problems.append(f"{what}: crashed into the generic handler error")
    # ignore closing HTML tags: "</code>" is not a reference to a command called /code
    for named in set(re.findall(r"(?<!<)/([a-z]{2,12})\b", joined)):
        if named not in known:
            problems.append(f"{what}: names /{named}, which does not exist")
    return problems


# ---------------------------------------------------------------- the enumeration itself
def test_the_surface_is_what_we_think_it_is(world) -> None:
    """If this list changes, the sweeps below changed with it - which is the point."""
    handlers, _send, _tap, _db, _pid = world
    assert len(commands(handlers)) >= 20, commands(handlers)
    assert len(callbacks(handlers)) >= 15, callbacks(handlers)


@pytest.mark.parametrize("onboarded", [False, True], ids=["fresh", "onboarded"])
def test_every_command_answers_with_no_arguments(world, onboarded: bool) -> None:
    """The commonest thing anyone does with a command they have just seen in /help."""
    handlers, send, _tap, db, pid = world
    a_person(db, pid, onboarded=onboarded)
    known = set(commands(handlers))
    problems: list[str] = []
    for name in known:
        problems += check(send(f"/{name}"), f"/{name} (bare, onboarded={onboarded})", known)
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize("junk", ["zzz", "-1", "999999", "' OR 1=1 --"], ids=lambda s: s[:6])
def test_every_command_answers_junk_without_breaking(world, junk: str) -> None:
    handlers, send, _tap, db, pid = world
    a_person(db, pid, onboarded=True)
    known = set(commands(handlers))
    problems: list[str] = []
    for name in known:
        problems += check(send(f"/{name} {junk}"), f"/{name} {junk!r}", known)
    assert not problems, "\n".join(problems)


def test_every_command_answers_a_very_long_argument(world) -> None:
    """A pasted plan, a voice transcript that ran away, a fat-fingered hold on a key."""
    handlers, send, _tap, db, pid = world
    a_person(db, pid, onboarded=True)
    known = set(commands(handlers))
    problems: list[str] = []
    for name in known:
        problems += check(send(f"/{name} " + "word " * 400), f"/{name} (long)", known)
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize(
    "shape",
    ["", ":", ":0", ":999999", ":abc", ":-1:-1", ":0:0:0"],
    ids=["bare", "colon", "zero", "missing", "letters", "negatives", "extra"],
)
def test_every_callback_survives_a_malformed_payload(world, shape: str) -> None:
    """Telegram buttons never expire, so an id from last month can arrive at any time - and
    a callback that raises leaves the person looking at a message whose buttons have gone."""
    handlers, _send, tap, db, pid = world
    a_person(db, pid, onboarded=True)
    known = set(commands(handlers))
    problems: list[str] = []
    for kind in callbacks(handlers):
        replies = tap(f"k:{kind}{shape}")
        # a callback may legitimately ignore a payload it cannot parse; what it must not do
        # is raise, or answer with something a person cannot act on
        for word in JARGON:
            if word in " ".join(replies).lower():
                problems.append(f"k:{kind}{shape}: leaked {word!r}")
        if "went wrong" in " ".join(replies).lower():
            problems.append(f"k:{kind}{shape}: crashed")
        problems += [
            p
            for p in check(replies or ["(silent)"], f"k:{kind}{shape}", known)
            if "said nothing" not in p
        ]
    assert not problems, "\n".join(problems)


def test_a_callback_for_someone_elses_row_is_refused(world) -> None:
    """Per-person scoping comes from identity, never from the payload."""
    _handlers, _send, tap, db, pid = world
    a_person(db, pid, onboarded=True)
    partner = db.one("SELECT id FROM persons WHERE slug = 'partner'")
    assert partner is not None
    partners = db.insert(
        "menus",
        person_id=int(partner["id"]),
        segment_start="2026-09-14",
        segment_end="2026-09-18",
        status="proposed",
    )
    tap(f"k:menu:{partners}")
    assert db.scalar("SELECT COUNT(*) FROM menu_decisions") == 0, "his tap cannot accept her menu"


def test_an_unknown_command_and_an_unknown_button_both_say_so(world) -> None:
    _handlers, send, tap, db, pid = world
    a_person(db, pid, onboarded=True)
    assert "Unknown command" in " ".join(send("/wibble"))
    assert "not wired" in " ".join(tap("k:wibble:1"))


# ---------------------------------------------------------------- personalisation coverage
def profile_columns() -> set[str]:
    schema = (Path(__file__).resolve().parents[1] / "src" / "tiffin" / "schema.sql").read_text(
        encoding="utf-8"
    )
    block = schema.split("CREATE TABLE IF NOT EXISTS profiles")[1].split(");")[0]
    return {
        m.group(1)
        for m in re.finditer(r"^\s{4}([a-z_]+)\s", block, re.MULTILINE)
        if m.group(1) not in ("id", "person_id", "effective_from", "created_at")
    }


def test_every_profile_column_is_reachable_or_declared(world) -> None:
    """A setting nobody can reach is a setting that does not exist.

    Anything not in /set must be named here with the reason, so adding a column forces a
    decision about how a person changes it instead of it quietly becoming unreachable.
    """
    from tiffin.telegram import settings

    reachable = {f.column for f in settings.FIELDS}
    elsewhere = {
        "equipment_json": "/kit",
        "nudge_time": "legacy profile column; steps nudges are retired",
        # derived from the rate and the guardrails, never typed
        "protein_g_per_kg": "derived",
        "fat_g_per_kg_min": "derived",
        "fibre_g_min": "derived",
    }
    missing = profile_columns() - reachable - set(elsewhere)
    assert not missing, f"no way for a person to change: {sorted(missing)}"


def test_every_per_person_list_can_be_added_to_and_taken_from(world) -> None:
    """Each list must allow both additions and removals."""
    handlers, send, _tap, db, pid = world
    a_person(db, pid, onboarded=True)
    known = set(commands(handlers))
    assert {"kit", "set", "week", "name", "consent"} <= known

    # food lists: replace, add one, drop one
    assert "olives" in " ".join(send("/set avoid olives, kale"))
    assert "capers" in " ".join(send("/set avoid + capers"))
    dropped = " ".join(send("/set avoid - kale"))
    assert "is now capers, olives" in dropped and "Allowed back: kale" in dropped, dropped

    # the kitchen and the training week
    assert "air fryer" in " ".join(send("/kit oven, air fryer")).lower()
    assert "Cleared" in " ".join(send("/kit none"))
    assert "climb" in " ".join(send("/week Mon climb 90"))


def test_nothing_a_person_can_set_is_write_only(world) -> None:
    """Every /set field answers with its current value when asked bare."""
    from tiffin.telegram import settings

    _handlers, send, _tap, db, pid = world
    a_person(db, pid, onboarded=True)
    problems = []
    for field in settings.FIELDS:
        replies = " ".join(send(f"/set {field.name}"))
        if field.name not in replies:
            problems.append(f"/set {field.name} does not name itself back")
        if "is " not in replies:
            problems.append(f"/set {field.name} does not show a current value")
        if "{" in replies or "[" in replies:
            problems.append(f"/set {field.name} shows stored JSON")
    assert not problems, "\n".join(problems)


# ---------------------------------------------------------------- what the fresh person sees
def test_a_person_with_no_profile_is_never_left_without_a_next_step(world) -> None:
    """Her whole first week. Every command must point somewhere, not just fail."""
    handlers, send, _tap, _db, _pid = world
    known = set(commands(handlers))
    problems = []
    for name in known:
        replies = " ".join(send(f"/{name}"))
        if not replies.strip():
            problems.append(f"/{name}: nothing at all before onboarding")
        low = replies.lower()
        # it may say "finish /onboard first"; what it may not do is stop dead
        if "went wrong" in low or "not wired" in low:
            problems.append(f"/{name}: {replies[:80]}")
    assert not problems, "\n".join(problems)


# ---------------------------------------------------------------- the stale tap
#: a payload each callback accepts the SHAPE of, carrying an id or a day nothing owns.
#: Telegram buttons never expire, so every one of these arrives sooner or later.
STALE = {
    "cfm": "20260101",
    "cfmx": "20260101",
    "cfmall": "20260101",  # "All N days as planned" under a check-in long gone
    "sess": "999999",
    "sessx": "999999",
    "sessm": "20260101:done",
    "mslot": "20260101:dinner",  # which planned meal an old "what changed?" answer replaced
    "mslotok": "20260101",  # Done under an old "which meals did that replace?"
    "mslot0": "20260101",  # Extra, nothing replaced, under the same old question
    "sup": "20260101:am:all",
    "cyc": "luteal",
    "menu": "999999",
    "menux": "999999",
    "meal": "999999:dinner",
    "menuv": "999999",
    "mday": "999999:20260101",  # a day button under /menu, from a week long gone
    "menuw": "0",  # "back to the week" on a day's card
    "batchv": "",  # "back to the batch" on a pot card: re-renders /batch, which always answers
    "tick": "999999",
    "rate": "999999:5",
    "prod": "999999",
    "intent": "999999",
    "intentx": "999999",
    "ob": "sex:m",
    "fu": "pln:invalid",
    "consent": "1",
    "set": "evening:21:00",
    "swap": "2026-01-01:lunch:999999",  # a dish offered for a day long gone
    "swapmeal": "2026-01-01:lunch",  # Swap on an old meal card
    "swappick": "999999",  # "Swap a dish" under a menu that is not theirs, or long gone
    "swapall": "",  # "Swap them now" after the food has gone from every menu
    "unavoid": "999999",  # Undo under a refusal that is not theirs, or long replaced
    "madeof": "999999:y",  # houmous after chickpeas, for a food that is gone
    "never": "999999",  # "Never X again" for a food that is gone
    "avadd": "dodo",  # "Keep X off too" for a word that names no food any more
    "avok": "999999",  # "Allow X" for a food that is gone
    "avrm": "999999:0",  # "Allow X" under an /avoid list that is not theirs
    "subst": "2026-01-01:lunch",  # "Change one food in it" on a day long gone
    "substi": "2026-01-01:lunch:999999",
    "substdo": "2026-01-01:lunch:999999:999998",
    "newdish": "2026-01-01:lunch",  # "New dishes" for a day long gone
    "rebuild": "999999",  # "Just a new menu" under a menu that is not theirs
    "keepmenu": "",  # "Keep this one" long after: says the menu is kept, which it is
    "size": "2026-01-01:lunch:less",  # Less on a meal card from a day long gone
    "wishbuild": "999999",  # "Rebuild now" under a wish taken back, or not theirs
    "wishundo": "999999",  # Undo under a wish already taken back, or not theirs
    "chs": "keep:999999",  # Keep the first choices, under a week that is not theirs or is set
    "mains": "3",  # an answer under an old menu: kept again and said, never silence
    "iv": "a:999:0",  # an answer to a question there is not (/questions): the old-tap toast
    "prd": "2020-01-01",  # a period start from an old weekly check: said, nothing recorded
    "link": "old",  # a button from a /link screen long gone: said, nothing changed
    "web": "",  # an old Open web page button issues a fresh link or explains the sign-in mode
}


def test_every_callback_has_a_stale_case_in_this_list(world) -> None:
    handlers, _send, _tap, _db, _pid = world
    missing = set(callbacks(handlers)) - set(STALE)
    assert not missing, f"add a stale payload for: {sorted(missing)}"


def test_no_button_answers_an_old_tap_with_silence(
    fixture_db: Db, two_persons, frozen_clock, config, tmp_path: Path
) -> None:
    """Sixteen of twenty-three did. A person who taps an old message and gets nothing at all
    cannot tell that from a bot that has stopped working."""
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
    handlers = app.handlers(fixture_db)
    poller = Poller(
        app.api(),
        fixture_db,
        app.registry(fixture_db),
        handlers.handle,
        clock=frozen_clock,
        sleeper=lambda s: None,
    )
    a_person(fixture_db, two_persons["owner"], onboarded=True)
    silent = []
    for i, kind in enumerate(sorted(handlers.callbacks), start=1):
        before_sent, before_toast = len(tg.sent), len(tg.answered)
        tg.queue_update(tg.callback_update(i, OWNER_CHAT, f"k:{kind}:{STALE[kind]}", message_id=i))
        poller.run_once()
        said = len(tg.sent) > before_sent
        toasted = any(a.get("text") for a in tg.answered[before_toast:])
        if not said and not toasted:
            silent.append(kind)
    assert not silent, f"silent on a stale tap: {silent}"


def test_a_day_from_months_ago_cannot_be_recorded_now(
    fixture_db: Db, two_persons, frozen_clock, config, tmp_path: Path
) -> None:
    """The day travels in the payload, so scrolling back to an old evening message and
    tapping "As planned" wrote a confirmed day at the full target for a date long closed -
    a usable day, straight into the measured TDEE."""
    secrets_dir = tmp_path / "s"
    secrets_dir.mkdir()
    (secrets_dir / "telegram-token-dev.txt").write_text("1:t", encoding="utf-8")
    tg = FakeTelegram()
    app = App(config, Secrets(secrets_dir), clock=frozen_clock, transport=tg)
    poller = Poller(
        app.api(),
        fixture_db,
        app.registry(fixture_db),
        app.handlers(fixture_db).handle,
        clock=frozen_clock,
        sleeper=lambda s: None,
    )
    a_person(fixture_db, two_persons["owner"], onboarded=True)
    tg.queue_update(tg.callback_update(1, OWNER_CHAT, "k:cfm:20260101", message_id=1))
    poller.run_once()
    assert "too far back" in tg.sent[-1]["text"]
    assert fixture_db.scalar("SELECT COUNT(*) FROM day_confirmations") == 0
    # and yesterday is still perfectly answerable
    tg.queue_update(tg.callback_update(2, OWNER_CHAT, "k:cfm:20260906", message_id=2))
    poller.run_once()
    assert "recorded" in tg.sent[-1]["text"]
    assert fixture_db.scalar("SELECT COUNT(*) FROM day_confirmations") == 1
