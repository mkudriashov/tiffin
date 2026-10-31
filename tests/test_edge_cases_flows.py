"""The edge-case sweep, part two: flows, repeat answers, clocks and the two-person wall.

Part one (`test_edge_cases.py`) is commands with bad arguments. This half is the harder
ground: the same button twice, an answer to a question that has already closed, someone
else's id, a sentence that merely contains a number, and the day boundaries.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import sqlite3

import pytest

from tiffin import body, targets
from tiffin import db as dbmod
from tiffin.clock import TZ, FrozenClock
from tiffin.config import Budget
from tiffin.db import Db
from tiffin.food import catalogue, recipes
from tiffin.model import gateway
from tiffin.persons import PersonRegistry
from tiffin.secrets import Secrets
from tiffin.telegram.render import check_text
from tiffin.web import identity

from .test_chat import FakeGateway as ChatGateway
from .test_edge_cases import SATURDAY, SUNDAY, a_menu, answers, quiet_tap
from .test_menu import MONDAY, OWNER_CHAT, world  # noqa: F401

# -- pauses ---------------------------------------------------------------------------------


def test_31_cook_while_paused_still_answers(world) -> None:  # noqa: F811
    _pid, _db, _tg, _gw, say, _tap, _run = world
    say("/pause 3d")
    assert answers(say("/cook"))


def test_32_pause_then_resume(world) -> None:  # noqa: F811
    _pid, db, _tg, _gw, say, _tap, _run = world
    say("/pause 3d")
    assert db.all("SELECT 1 FROM pause_periods") != [], "a pause is recorded"
    assert answers(say("/resume"))


def test_33_pausing_twice_over_the_same_days(world) -> None:  # noqa: F811
    _pid, _db, _tg, _gw, say, _tap, _run = world
    say("/pause 3d")
    assert answers(say("/pause 3d"))


# -- the same answer twice, and answers to questions already closed -------------------------


def test_34_confirming_the_day_twice_records_it_once_and_says_so(world) -> None:  # noqa: F811
    """Every handler is idempotent by design: the second press is answered, not re-recorded."""
    pid, db, _tg, _gw, say, _tap, _run = world
    say("/confirm")
    second = say("/confirm")
    rows = db.all("SELECT * FROM day_confirmations WHERE person_id = ?", (pid,))
    assert len(rows) == 1, "one confirmation for the day"
    assert answers(second), "and the person is told, not left wondering"


def test_35_an_intent_accepted_twice_writes_once(world) -> None:  # noqa: F811
    pid, db, _tg, _gw, _say, tap, _run = world
    iid = db.insert(
        "intents",
        person_id=pid,
        kind="log_offplan",
        payload_json=json.dumps({"description": "kebab", "day": "2026-09-12", "kcal": 900}),
    )
    tap(f"k:intent:{iid}")
    before = db.scalar("SELECT COUNT(*) FROM estimates WHERE person_id = ?", (pid,))
    tap(f"k:intent:{iid}")
    after = db.scalar("SELECT COUNT(*) FROM estimates WHERE person_id = ?", (pid,))
    assert before == after, "the second tap on a spent intent writes nothing"


def test_36_an_intent_discarded_cannot_then_be_accepted(world) -> None:  # noqa: F811
    pid, db, _tg, _gw, _say, tap, _run = world
    iid = db.insert(
        "intents",
        person_id=pid,
        kind="log_offplan",
        payload_json=json.dumps({"description": "beer", "day": "2026-09-12", "kcal": 200}),
    )
    tap(f"k:intentx:{iid}")
    tap(f"k:intent:{iid}")
    assert db.scalar("SELECT COUNT(*) FROM estimates WHERE person_id = ?", (pid,)) == 0


def test_37_partner_intent_cannot_be_accepted_from_their_chat(world, two_persons) -> None:  # noqa: F811
    _pid, db, tg, _gw, _say, tap, _run = world
    iid = db.insert(
        "intents",
        person_id=two_persons["partner"],
        kind="log_offplan",
        payload_json=json.dumps({"description": "wine", "day": "2026-09-12", "kcal": 150}),
    )
    quiet_tap(tg, tap, f"k:intent:{iid}")
    assert db.scalar("SELECT COUNT(*) FROM estimates") == 0


# -- one conversation at a time ---------------------------------------------------------------


def test_38_a_sentence_with_a_number_is_not_a_measurement(world) -> None:  # noqa: F811
    pid, db, _tg, _gw, say, _tap, _run = world
    say("/measure")
    say("I ran 45 minutes this morning")
    assert body.latest_tape(db, pid, "waist") is None


def test_39_measure_reads_the_shapes_people_actually_type() -> None:
    from tiffin.telegram.measures import parse_tape

    assert parse_tape("84 102") == {"waist": 84.0, "hip": 102.0}
    assert parse_tape("84/102") == {"waist": 84.0, "hip": 102.0}
    assert parse_tape("84cm 102 cm") == {"waist": 84.0, "hip": 102.0}
    assert parse_tape("84,5") == {"waist": 84.5}
    assert parse_tape("hip 102 waist 84") == {"waist": 84.0, "hip": 102.0}
    assert parse_tape("I ran 45 minutes this morning") is None
    assert parse_tape("") is None


def test_40_onboarding_restart_writes_no_half_profile(world) -> None:  # noqa: F811
    pid, db, _tg, _gw, say, _tap, _run = world
    before = db.scalar("SELECT COUNT(*) FROM profiles WHERE person_id = ?", (pid,))
    say("/onboard restart")
    say("m")
    after = db.scalar("SELECT COUNT(*) FROM profiles WHERE person_id = ?", (pid,))
    assert after == before, "a profile lands when the interview finishes, not during it"


def test_43_a_slot_that_is_not_a_slot(world) -> None:  # noqa: F811
    _pid, _db, _tg, _gw, say, _tap, _run = world
    assert answers(say("/supp add Creatine 5 g teatime"))


# -- what the person is shown ------------------------------------------------------------------


def test_44_no_reply_carries_an_emoji_or_a_tag_telegram_will_reject(world) -> None:  # noqa: F811
    """The ten-line rule has a deliberate carve-out - a reference card the person asked for
    has no useful ten-line version, and `/help` is 29 lines on purpose. The two rules with no
    carve-out are the tag whitelist and no emoji, so those are what a sweep should assert."""
    _pid, _db, tg, _gw, say, _tap, _run = world
    for cmd in ("/help", "/today", "/set", "/week", "/kit", "/consent"):
        say(cmd)
    for sent in tg.sent:
        check_text(sent["text"], max_lines=99)


def test_45_a_very_long_message_is_cut_before_it_reaches_a_model(world) -> None:  # noqa: F811
    """Telegram will carry 4096 characters and a person can paste a whole recipe page. The
    prompt is built from that text, so an uncut one is an unbounded token bill per message."""
    _pid, _db, _tg, gateways, say, _tap, _run = world
    gateways.append(ChatGateway(answer="Noted."))
    assert answers(say("x" * 5000))
    sent_to_model = gateways[0].conversations[-1][-1]["content"]
    assert len(sent_to_model) <= 2000, "the model sees a bounded slice, not the whole paste"


def test_46_html_in_a_persons_own_words_comes_back_escaped(world) -> None:  # noqa: F811
    pid, db, _tg, _gw, _say, tap, _run = world
    iid = db.insert(
        "intents",
        person_id=pid,
        kind="log_offplan",
        payload_json=json.dumps(
            {"description": "<b>kebab</b> and chips", "day": "2026-09-12", "kcal": 900}
        ),
    )
    out = tap(f"k:intent:{iid}")
    assert "<b>kebab</b>" not in out


def test_47_emoji_never_reach_a_phone(world) -> None:  # noqa: F811
    pid, db, _tg, _gw, _say, tap, _run = world
    iid = db.insert(
        "intents",
        person_id=pid,
        kind="log_offplan",
        payload_json=json.dumps(
            {"description": "pizza and beer", "day": "2026-09-12", "kcal": 800}
        ),
    )
    check_text(tap(f"k:intent:{iid}"), max_lines=99)


# -- the model is never trusted with a number --------------------------------------------------


def test_48_prep_and_serving_numbers_have_ceilings() -> None:
    cat = [an_ingredient()]
    assert "prep" in why(a_draft(prep_minutes=600), cat)
    assert "servings" in why(a_draft(servings=99), cat)


def test_49_a_method_with_a_hundred_steps_is_refused() -> None:
    assert "method" in why(a_draft(method=[f"step {i}" for i in range(100)]), [an_ingredient()])


# -- days, clocks and boundaries ---------------------------------------------------------------


def test_50_the_local_day_is_the_london_day() -> None:
    assert FrozenClock(dt.datetime(2026, 9, 12, 23, 45, tzinfo=TZ)).today_local() == SATURDAY
    assert FrozenClock(dt.datetime(2026, 9, 13, 0, 15, tzinfo=TZ)).today_local() == SUNDAY


def test_51_the_spring_forward_day_still_has_one_date() -> None:
    before = FrozenClock(dt.datetime(2026, 3, 29, 0, 30, tzinfo=dt.UTC).astimezone(TZ))
    after = FrozenClock(dt.datetime(2026, 3, 29, 2, 30, tzinfo=dt.UTC).astimezone(TZ))
    assert before.today_local() == after.today_local() == dt.date(2026, 3, 29)


def test_52_utc_stamps_are_space_form_never_iso_t() -> None:
    c = FrozenClock(dt.datetime(2026, 9, 12, 18, 0, tzinfo=TZ))
    assert "T" not in c.utc_stamp() and len(c.utc_stamp()) == 19


# -- the commands that answer when nothing has happened yet ------------------------------------


def test_54_today_with_nothing_recorded(world) -> None:  # noqa: F811
    _pid, _db, _tg, _gw, say, _tap, _run = world
    assert answers(say("/today"))


def test_55_week_with_no_plan_yet(world) -> None:  # noqa: F811
    _pid, _db, _tg, _gw, say, _tap, _run = world
    assert answers(say("/week"))


def test_56_shopping_with_no_list_yet(world) -> None:  # noqa: F811
    _pid, _db, _tg, _gw, say, _tap, _run = world
    assert answers(say("/shopping"))


def test_57_batch_with_no_cook_day(world) -> None:  # noqa: F811
    _pid, _db, _tg, _gw, say, _tap, _run = world
    assert answers(say("/batch"))


def test_58_kit_and_plan_answer_before_anything_is_set(world) -> None:  # noqa: F811
    _pid, _db, _tg, _gw, say, _tap, _run = world
    assert answers(say("/kit"))


def test_59_set_with_no_arguments_shows_the_answers(world) -> None:  # noqa: F811
    _pid, _db, _tg, _gw, say, _tap, _run = world
    assert answers(say("/set"))


def test_60_consent_answers(world) -> None:  # noqa: F811
    _pid, _db, _tg, _gw, say, _tap, _run = world
    assert answers(say("/consent"))


def test_61_menu_command_before_any_menu_exists(world) -> None:  # noqa: F811
    _pid, _db, _tg, _gw, say, _tap, _run = world
    assert answers(say("/menu"))


def test_62_ping_and_help_always_work(world) -> None:  # noqa: F811
    _pid, _db, _tg, _gw, say, _tap, _run = world
    assert answers(say("/ping"))
    assert answers(say("/help"))


# -- the wall between the model and the plan -------------------------------------------------


def an_ingredient(i: int = 1, *, name: str = "Oats", approved: bool = True) -> catalogue.Ingredient:
    return catalogue.Ingredient(
        id=i,
        code=f"c{i}",
        version=1,
        name=name,
        cofid_code=None,
        kcal_100=370.0,
        protein_100=13.0,
        fat_100=8.0,
        carb_100=60.0,
        fibre_100=9.0,
        pack_g=1000.0,
        pack_label="1 kg",
        aisle="cupboard",
        pantry=True,
        use_whole_pack=False,
        approved=approved,
        person_scope="both",
    )


def a_draft(**over) -> recipes.RecipeDraft:
    base = {
        "name": "Oat bowl",
        "prep_minutes": 10,
        "method": ["mix", "eat"],
        "ingredients": [{"i": 1, "g": 100}],
    }
    base.update(over)
    return recipes.RecipeDraft.from_dict(base)


def why(draft, cat) -> str:
    return " ".join(recipes.validate(draft, cat))


def test_63_an_ingredient_index_the_catalogue_does_not_have(world) -> None:  # noqa: F811
    assert "out of range" in why(a_draft(ingredients=[{"i": 9999, "g": 100}]), [an_ingredient()])


def test_64_index_zero_is_out_of_range_not_the_first_row() -> None:
    """The list the model is shown is 1-based. A 0 means it counted from the wrong end, and
    silently taking catalogue[-1] would put the last ingredient in the meal."""
    assert "out of range" in why(a_draft(ingredients=[{"i": 0, "g": 100}]), [an_ingredient()])


def test_65_a_negative_index_cannot_wrap_round_the_list() -> None:
    assert "out of range" in why(a_draft(ingredients=[{"i": -1, "g": 100}]), [an_ingredient()])


def test_66_a_food_the_person_refused_cannot_be_cooked() -> None:
    cat = [an_ingredient(1, name="Chickpeas", approved=False)]
    assert "not approved" in why(a_draft(), cat)


def test_67_the_same_ingredient_listed_twice() -> None:
    d = a_draft(ingredients=[{"i": 1, "g": 100}, {"i": 1, "g": 50}])
    assert "twice" in why(d, [an_ingredient()])


def test_68_grams_that_cannot_be_true() -> None:
    cat = [an_ingredient()]
    for bad in (0, -50, 1e9, float("inf"), float("nan")):
        assert "outside" in why(a_draft(ingredients=[{"i": 1, "g": bad}]), cat), bad


def test_69_a_recipe_with_no_name_and_a_recipe_with_no_method() -> None:
    cat = [an_ingredient()]
    assert "name: empty" in why(a_draft(name="   "), cat)
    assert "method" in why(a_draft(method=[]), cat)


def test_70_a_draft_of_the_wrong_shape_raises_before_validation() -> None:
    for bad in (
        {"name": "x", "ingredients": "oats", "method": []},
        {"name": "x", "ingredients": [{"i": 1}], "method": []},
        {"name": "x", "ingredients": [{"i": "one", "g": 10}], "method": []},
        {"ingredients": [], "method": []},
    ):
        with pytest.raises(ValueError):
            recipes.RecipeDraft.from_dict(bad)


# -- consent, and the two-person wall ---------------------------------------------------------


class _StubBackend:
    """A backend that would answer if it were ever reached. It must not be."""

    name = "stub"

    def complete_json(self, *a: object, **k: object) -> object:  # pragma: no cover
        raise AssertionError("the primary was called without consent")

    def converse(self, *a: object, **k: object) -> object:  # pragma: no cover
        raise AssertionError("the primary was called without consent")


def consent(db: Db, person_id: int, *, granted: int, kind: str = "anthropic_processing") -> int:
    return db.insert(
        "consents", person_id=person_id, kind=kind, granted=granted, wording_version="v1"
    )


def test_71_no_consent_row_means_no_consent(fixture_db: Db, two_persons) -> None:
    assert gateway.consent_granted(fixture_db, two_persons["partner"]) is False


def test_72_a_withdrawn_consent_wins_over_the_older_grant(fixture_db: Db, two_persons) -> None:
    """`consents` is append-only, so withdrawal is a second row. If the reader took the first
    row, or any row, a withdrawal would be silently ignored - the worst possible bug here."""
    partner = two_persons["partner"]
    consent(fixture_db, partner, granted=1)
    assert gateway.consent_granted(fixture_db, partner) is True
    consent(fixture_db, partner, granted=0)
    assert gateway.consent_granted(fixture_db, partner) is False


def test_73_their_consent_is_not_partners(fixture_db: Db, two_persons) -> None:
    consent(fixture_db, two_persons["owner"], granted=1)
    assert gateway.consent_granted(fixture_db, two_persons["partner"]) is False


def test_74_consent_to_something_else_is_not_consent_to_this(fixture_db: Db, two_persons) -> None:
    consent(fixture_db, two_persons["partner"], granted=1, kind="other_use")
    assert gateway.consent_granted(fixture_db, two_persons["partner"]) is False


def test_75_without_consent_the_route_says_so_before_any_call(fixture_db: Db, two_persons) -> None:
    """`claude -p` is the one thing that leaves the box. The check is a route decision taken
    before the prompt is built, not a filter applied to the answer."""

    def gw(allow: bool) -> gateway.Gateway:
        return gateway.Gateway(
            _StubBackend(),  # type: ignore[arg-type]
            fixture_db,
            lambda _pid: allow,
            budgets=lambda _task: Budget(120, 4, 0.25),
        )

    assert gw(allow=False).primary_route(two_persons["partner"]) == "no_consent"
    assert gw(allow=True).primary_route(two_persons["partner"]) is None


def test_76_their_menu_id_is_not_visible_from_partner_chat(world, two_persons) -> None:  # noqa: F811
    pid, db, tg, _gw, _say, tap, _run = world
    mine = a_menu(db, pid, MONDAY, dt.date(2026, 9, 18), status="proposed")
    partners = a_menu(db, two_persons["partner"], MONDAY, dt.date(2026, 9, 18), status="proposed")
    quiet_tap(tg, tap, f"k:menu:{partners}")
    decided = [r["menu_id"] for r in db.all("SELECT menu_id FROM menu_decisions")]
    tap(f"k:menu:{mine}")
    assert partners not in decided


def test_77_partner_weigh_in_never_appears_in_their_numbers(fixture_db: Db, two_persons) -> None:
    body.record_weigh_in(fixture_db, two_persons["partner"], SATURDAY, 61.0, "manual")
    assert body.weigh_ins(fixture_db, two_persons["owner"]) == []
    assert len(body.weigh_ins(fixture_db, two_persons["partner"])) == 1


# -- the append-only rule, from the outside ----------------------------------------------------


def test_78_a_ledger_row_cannot_be_edited_or_deleted(fixture_db: Db, two_persons) -> None:
    """Not a unit test of a trigger - the point is that a handler bug that tried to 'fix' a
    past row would fail loudly rather than quietly rewrite what the person was told."""
    pid = two_persons["owner"]
    body.record_weigh_in(fixture_db, pid, SATURDAY, 84.0, "manual")
    row = fixture_db.one("SELECT id FROM weigh_ins WHERE person_id = ?", (pid,))
    assert row is not None
    with pytest.raises(sqlite3.IntegrityError):
        fixture_db.execute("UPDATE weigh_ins SET weight_kg = 70 WHERE id = ?", (row["id"],))
    with pytest.raises(sqlite3.IntegrityError):
        fixture_db.execute("DELETE FROM weigh_ins WHERE id = ?", (row["id"],))


def test_79_a_correction_is_the_row_that_wins(fixture_db: Db, two_persons) -> None:
    pid = two_persons["owner"]
    body.record_weigh_in(fixture_db, pid, SATURDAY, 84.0, "manual")
    body.record_weigh_in(fixture_db, pid, SATURDAY, 83.2, "manual")
    days = body.weigh_ins(fixture_db, pid)
    assert len(days) == 1, "one weight for the day, not two"
    assert abs(days[0][1] - 83.2) < 0.001, "and it is the corrected one"


def test_80_the_same_message_twice_is_recorded_once(world) -> None:  # noqa: F811
    """Telegram redelivers on a timeout. A repeated update_id must not log the day twice."""
    _pid, db, tg, _gw, _say, _tap, _run = world
    tg.queue_update(tg.text_update(9001, OWNER_CHAT, "/confirm"))
    tg.queue_update(tg.text_update(9001, OWNER_CHAT, "/confirm"))
    rows = db.all("SELECT outcome FROM inbound_outcomes ORDER BY id")
    assert [r["outcome"] for r in rows].count("duplicate") <= 1


# -- what the model proposes, the person taps, and Tiffin then believes -------------------


def an_intent(db: Db, person_id: int, kind: str, payload: dict) -> int:
    return db.insert("intents", person_id=person_id, kind=kind, payload_json=json.dumps(payload))


def test_81_a_pause_whose_days_run_backwards_is_not_reported_as_a_pause(world) -> None:  # noqa: F811
    """The /pause command validates what the person typed. This is the other door into the
    same table: the model's own dates, taken from a sentence like 'pause me until Friday'.
    A reversed range pauses nothing, so telling the person they are paused is a lie they only
    discover when tomorrow's message arrives."""
    pid, db, _tg, _gw, _say, tap, _run = world
    iid = an_intent(db, pid, "set_pause", {"start_day": "2026-09-18", "end_day": "2026-09-14"})
    out = tap(f"k:intent:{iid}")
    paused = targets.pause_active(db, pid, dt.date(2026, 9, 16))
    assert paused or "paused" not in out.lower(), (
        "either the pause works or the person is told it did not"
    )


def test_82_a_pause_day_that_is_not_a_date_cannot_cancel_a_real_pause(world) -> None:  # noqa: F811
    """`pause_active` reads only the newest row, so a nonsense row silently un-pauses someone
    who really is on holiday."""
    pid, db, _tg, _gw, say, tap, _run = world
    say("/pause 7d")
    assert targets.pause_active(db, pid, dt.date(2026, 9, 14)) is True
    iid = an_intent(db, pid, "set_pause", {"start_day": "Friday", "end_day": "next week"})
    tap(f"k:intent:{iid}")
    assert targets.pause_active(db, pid, dt.date(2026, 9, 14)) is True, (
        "a pause nobody can parse must not end the pause that was already running"
    )


def test_84_a_rating_outside_one_to_five_never_lands(world) -> None:  # noqa: F811
    pid, db, _tg, _gw, _say, tap, _run = world
    rid = db.insert("recipes", name="Dish", prep_minutes=10, method_json="[]", source="model")
    iid = an_intent(db, pid, "rate_dish", {"recipe_id": rid, "score": 9})
    tap(f"k:intent:{iid}")
    assert db.all("SELECT 1 FROM ratings WHERE person_id = ?", (pid,)) == []


def test_85_a_training_week_with_an_eighth_day_never_lands(world) -> None:  # noqa: F811
    pid, db, _tg, _gw, _say, tap, _run = world
    plan = {"plan": [{"weekday": 9, "kind": "run", "target_minutes": 45}]}
    iid = an_intent(db, pid, "set_plan", plan)
    tap(f"k:intent:{iid}")
    assert db.all("SELECT 1 FROM planned_sessions WHERE person_id = ?", (pid,)) == []


def test_86_a_payload_missing_the_field_it_needs_answers_rather_than_crashes(world) -> None:  # noqa: F811
    pid, db, _tg, _gw, _say, tap, _run = world
    for kind, payload in (
        ("rate_dish", {"score": 4}),
        ("set_pause", {}),
        ("log_offplan", {}),
    ):
        iid = an_intent(db, pid, kind, payload)
        quiet = len(_tg_sent(world))
        try:
            tap(f"k:intent:{iid}")
        except (KeyError, ValueError, TypeError) as e:  # pragma: no cover - the finding
            raise AssertionError(f"{kind} with {payload} raised {type(e).__name__}") from e
        assert len(_tg_sent(world)) > quiet, f"{kind} said nothing"


def _tg_sent(world_tuple) -> list:
    return world_tuple[2].sent


# -- who is asking -----------------------------------------------------------------------------


def test_87_a_link_token_that_was_tampered_with_names_nobody() -> None:
    """Signed-link mode is dormant, which is exactly why its verifier is worth a test: it will
    be switched on in a hurry if the header route ever fails."""
    key = b"a key"
    good = identity.make_link_token(key, 7, expires_at=2_000_000_000)
    assert identity.verify_link_token(key, good, now=1_000_000_000) == 7
    encoded, _, signature = good.partition(".")
    forged = identity.make_link_token(b"another key", 7, expires_at=2_000_000_000)
    for bad in (
        f"{encoded}.{'0' * len(signature)}",  # signature replaced
        forged,  # signed with the wrong key
        f"{identity._b64(b'1:2000000000')}.{signature}",  # payload swapped for person 1
        "not a token",
        "",
        f"{encoded}.",
    ):
        assert identity.verify_link_token(key, bad, now=1_000_000_000) is None, bad


def test_88_an_expired_link_names_nobody() -> None:
    key = b"a key"
    token = identity.make_link_token(key, 7, expires_at=1_000)
    assert identity.verify_link_token(key, token, now=999) == 7
    assert identity.verify_link_token(key, token, now=1_000) is None
    assert identity.verify_link_token(key, token, now=1_001) is None


def test_89_the_dev_header_is_a_dev_thing_only(config, tmp_path) -> None:
    """`X-Dev-Person: max` is a local convenience. On the box it must be inert, because the
    tailnet page is the one surface where a header is the whole of the identity."""
    secrets_dir = tmp_path / "sec"
    secrets_dir.mkdir()
    dev = identity.settings_from(config, Secrets(secrets_dir))
    assert dev.dev_bypass is True, "loopback dev keeps the bypass"
    prod = dataclasses.replace(config, env="prod")
    assert identity.settings_from(prod, Secrets(secrets_dir)).dev_bypass is False
    off_box = dataclasses.replace(config, data=_with_host(config, "100.64.0.1"))
    assert identity.settings_from(off_box, Secrets(secrets_dir)).dev_bypass is False


def test_90_a_login_is_only_ever_logged_as_a_digest() -> None:
    login = "alex@example.com"
    digest = identity.login_digest(login)
    assert login not in digest and "@" not in digest and len(digest) == 12
    assert digest == identity.login_digest(" ALEX@Example.COM ")


def test_91_an_erased_person_cannot_come_back_through_the_header(
    fixture_db: Db, two_persons, config
) -> None:
    reg = PersonRegistry(fixture_db, config)
    reg.attach_tailscale_login("owner", "alex@example.com")
    assert reg.by_tailscale_login("alex@example.com") is not None
    assert reg.by_tailscale_login("ALEX@EXAMPLE.COM") is not None, "logins are case-insensitive"
    _tombstone(fixture_db, two_persons["owner"])
    assert reg.by_tailscale_login("alex@example.com") is None
    tombstoned = reg.by_slug("owner")
    assert tombstoned is None or tombstoned.erased


def test_92_a_login_nobody_has_claimed_resolves_to_nobody(
    fixture_db: Db, two_persons, config
) -> None:
    reg = PersonRegistry(fixture_db, config)
    assert reg.by_tailscale_login("stranger@example.com") is None
    assert reg.by_tailscale_login("") is None


def _with_host(config, host: str) -> dict:
    data = json.loads(json.dumps(config.data))
    data.setdefault("web", {})["host"] = host
    return data


def _tombstone(db: Db, person_id: int) -> None:
    """What `admin.erase_person` does to the persons row, without the rest of an erasure."""
    db.con.executescript(dbmod.drop_trigger_sql("persons"))
    try:
        db.execute(
            "UPDATE persons SET erased_at = ?, tailscale_login = NULL WHERE id = ?",
            (db.clock.utc_stamp(), person_id),
        )
    finally:
        db.con.executescript(dbmod.trigger_sql("persons"))
