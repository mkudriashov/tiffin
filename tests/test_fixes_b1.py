"""Regression tests for reading food refusals from everyday language."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from tiffin.db import Db
from tiffin.food import catalogue, prefs
from tiffin.food import menu as menu_mod
from tiffin.telegram import dayflow, foodflow

from .test_food_catalogue import add_profile
from .test_food_shopping import seed_ingredient
from .test_swap import (  # noqa: F401 (fixtures)
    _buttons,
    bot,
    week,
)

REPO = Path(__file__).resolve().parents[1]
YAML = REPO / "config" / "ingredients.yaml"
CSV = REPO / "data" / "cofid" / "cofid_2021_proximates.csv"
CURLY = chr(0x2019)  # what an iPhone and most Android keyboards type for an apostrophe
MON_EVENING = dt.datetime(2026, 9, 14, 20, 45)


@pytest.fixture
def two_persons(fixture_db: Db) -> dict[str, int]:
    """The owner and the partner, each with a Telegram identity, as the bootstrap leaves them: her
    persons.display_name blank (config/defaults.toml) and her name set later in person_names, the
    way the bot's /name does (`bot` builds on this one in this module)."""
    ids: dict[str, int] = {}
    for slug, name, chat_id in (("owner", "Alex", 1000001), ("partner", "", 1000002)):
        pid = fixture_db.insert("persons", slug=slug, display_name=name, tone="neutral")
        fixture_db.insert("telegram_identities", person_id=pid, chat_id=chat_id, method="config")
        ids[slug] = pid
    fixture_db.insert("person_names", person_id=ids["partner"], name="Sam", set_by="self")
    return ids


def _state(db: Db, pid: int) -> dict:
    raw = db.scalar("SELECT state_json FROM conversation_state WHERE person_id = ?", (pid,))
    return json.loads(raw) if raw else {}


# ---------------------------------------------------------------- #16 the phone's apostrophe
@pytest.mark.parametrize(
    ("said", "food"),
    [
        (f"I don{CURLY}t want lentils", "lentils"),
        (f"I can{CURLY}t stand salmon", "salmon"),
        (f"I won{CURLY}t eat chickpeas", "chickpeas"),
    ],
)
def test_a_refusal_typed_with_a_curly_apostrophe_is_kept(bot, said: str, food: str) -> None:  # noqa: F811
    """A refusal typed with a phone's curly apostrophe is kept exactly as the
    same sentence with a straight one."""
    send, _tap, db, pid, _r, _clock = bot
    out = send(said)
    assert "Kept off your menus from now on" in out[0]["text"], out
    assert catalogue.refused_terms(db, pid) == [food]


def test_a_curly_refusal_at_the_evening_question_is_not_food_eaten(bot) -> None:  # noqa: F811
    """At 20:45, with the evening question armed, a curly-apostrophe refusal
    is kept as a refusal and never stored as the day's off-plan food note."""
    send, _tap, db, pid, _r, clock = bot
    clock.set_local(MON_EVENING)
    assert dayflow.arm_evening(db, pid, clock.today_local(), clock=clock)
    out = send(f"I don{CURLY}t want lentils and chickpeas")
    assert "Kept off your menus from now on" in out[0]["text"], out
    assert catalogue.refused_terms(db, pid) == ["chickpeas", "lentils"]
    assert "note" not in _state(db, pid), "not taken as the answer to 'what changed?'"


# ---------------------------------------------------------------- #23 the food asked for instead
@pytest.mark.parametrize(
    ("said", "expect"),
    [
        ("No chicken, salmon instead please", ["chicken"]),
        ("no chicken; salmon or tuna instead", ["chicken"]),
        ("I don't want chicken, give me salmon instead", ["chicken"]),
        ("No chicken, salmon please", ["chicken"]),
        # a list is still a list: joined by or/and, each with its own "no", or by commas alone
        ("no rice, chickpeas or lentils", ["rice", "chickpeas", "lentils"]),
        ("no beef, no pork", ["beef", "pork"]),
        ("no rice, chickpeas, lentils", ["rice", "chickpeas", "lentils"]),
        ("I hate salmon, chickpeas, lentils", ["salmon", "chickpeas", "lentils"]),
        ("no mushrooms, olives, anchovies", ["mushrooms", "olives", "anchovies"]),
        ("no rice, chickpeas, lentils please", ["rice", "chickpeas", "lentils"]),
        ("no rice, chickpeas", ["rice", "chickpeas"]),
        ("No chickpeas no lentils", ["chickpeas", "lentils"]),
        ("no chickpeas no lentils no mushrooms please", ["chickpeas", "lentils", "mushrooms"]),
    ],
)
def test_the_food_asked_for_instead_is_not_refused(said: str, expect: list[str]) -> None:
    """A replacement food is offered without being added to the refusal list."""
    assert catalogue.avoid_terms(said) == expect


def test_no_chicken_salmon_instead_keeps_only_the_chicken_off(bot) -> None:  # noqa: F811
    """Through the bot, "No chicken, salmon instead please" keeps chicken off
    and leaves salmon on her menus."""
    send, _tap, db, pid, _r, _clock = bot
    out = send("No chicken, salmon instead please")
    assert catalogue.refused_terms(db, pid) == ["chicken"]
    assert "Salmon" not in out[0]["text"], out[0]["text"]


def test_a_comma_list_through_the_bot_keeps_every_food(bot) -> None:  # noqa: F811
    """ "I hate salmon, chickpeas, lentils" keeps all
    three off, and the reply names all three."""
    send, _tap, db, pid, _r, _clock = bot
    out = send("I hate salmon, chickpeas, lentils")
    assert catalogue.refused_terms(db, pid) == ["chickpeas", "lentils", "salmon"]
    assert all(f in out[0]["text"] for f in ("salmon", "chickpeas", "lentils")), out[0]["text"]


def test_a_food_set_aside_is_named_with_a_button(bot) -> None:  # noqa: F811
    """ "No chicken, salmon please" keeps the chicken off
    and may be asking FOR salmon, so the salmon is not written - but it is named, with a button
    to keep it off too, never lost without a word."""
    send, tap, db, pid, _r, _clock = bot
    out = send("No chicken, salmon please")
    assert catalogue.refused_terms(db, pid) == ["chicken"]
    text = out[0]["text"]
    assert "salmon" in text.casefold(), text
    assert len(text.splitlines()) <= 10
    buttons = _buttons(out[0])
    assert buttons.get("Keep salmon off too") == "k:avadd:salmon", buttons
    kept = tap(buttons["Keep salmon off too"])
    assert "Kept off your menus from now on: salmon (Salmon fillets)." in kept[-1]["text"]
    assert catalogue.refused_terms(db, pid) == ["chicken", "salmon"]


# ---------------------------------------------------------------- #28 wordings that were missed
@pytest.mark.parametrize(
    ("said", "expect"),
    [
        ("I'm not a fan of salmon", ["salmon"]),
        ("I don't want any more lentils", ["lentils"]),
        ("I don't want more chickpeas", ["chickpeas"]),
        ("I don't ever want salmon", ["salmon"]),
        ("stop putting mushrooms in everything", ["mushrooms"]),
        ("stop putting mushrooms in my food", ["mushrooms"]),
        ("please stop putting mushrooms in things", ["mushrooms"]),
        ("stop putting chickpeas in the salads", ["chickpeas"]),
    ],
)
def test_common_refusal_wordings_name_the_food(said: str, expect: list[str]) -> None:
    """ "Not a fan of", "any more", "more", "ever" and "stop putting" leave
    the food itself, not "fan of salmon", "want" or nothing - and the place it is put ("in my
    food", "in things") is not part of the food."""
    assert catalogue.avoid_terms(said) == expect
    assert foodflow.refusal_terms(said) == expect


@pytest.mark.parametrize(
    ("said", "food"),
    [("I'm not a fan of salmon", "salmon"), ("I don't want any more lentils", "lentils")],
)
def test_the_bot_keeps_those_wordings(bot, said: str, food: str) -> None:  # noqa: F811
    """Through the bot, each of those wordings is kept without the chat."""
    send, _tap, db, pid, _r, _clock = bot
    out = send(said)
    assert "Kept off your menus from now on" in out[0]["text"], out
    assert catalogue.refused_terms(db, pid) == [food]


# ---------------------------------------------------------------- #31 pepper, kept at home
def _shipped(fixture_db: Db, two_persons) -> int:
    catalogue.build(fixture_db, YAML, CSV)
    pid = two_persons["partner"]
    add_profile(fixture_db, pid)
    return pid


def test_pepper_kept_at_home_is_the_black_pepper(fixture_db: Db, two_persons, frozen_clock) -> None:
    """ "Pepper" on the pantry list hides the black pepper from the shopping
    and leaves the red peppers a menu needs on it."""
    pid = _shipped(fixture_db, two_persons)
    got = prefs.amend(fixture_db, pid, "pantry", add=["salt", "pepper"], clock=frozen_clock)
    assert got.hides["pepper"] == ("Black pepper",)
    assert catalogue.pantry_codes(fixture_db, pid) == {"salt", "black_pepper"}
    view = prefs.view(fixture_db, pid, frozen_clock.today_local())
    assert {e["term"]: e["foods"] for e in view["pantry"]}["pepper"] == ["Black pepper"]


def test_peppers_avoided_are_still_the_vegetable(fixture_db: Db, two_persons, frozen_clock) -> None:
    """ "No peppers" on the avoid list still keeps the red pepper off and
    leaves the black pepper in every recipe alone."""
    pid = _shipped(fixture_db, two_persons)
    got = prefs.amend(fixture_db, pid, "avoid", add=["peppers"], clock=frozen_clock)
    assert got.hides["peppers"] == ("Red pepper",)
    codes = {i.code for i in catalogue.approved(fixture_db, pid)}
    assert "red_pepper" not in codes and "black_pepper" in codes


# ---------------------------------------------------------------- #37 a dish or a day
@pytest.mark.parametrize(
    "said",
    [
        "I don't want the chicken and rice on Thursday",
        "no salmon tomorrow",
        "I don't want lentils on Fridays",
    ],
)
def test_a_day_makes_it_a_one_off_not_a_refusal(bot, said: str) -> None:  # noqa: F811
    """A sentence about one day is a one-off, not a refusal for good."""
    send, _tap, db, pid, _r, _clock = bot
    assert foodflow.refusal_terms(said) == []
    send(said)
    assert catalogue.refused_terms(db, pid) == []


def test_a_dish_on_the_menu_is_asked_about_not_refused_food_by_food(bot) -> None:  # noqa: F811
    """ "I don't want chicken and rice again" names the dish on her menu, so
    the bot asks - swap the dish, or keep one of its foods off - and refuses nothing itself."""
    send, tap, db, pid, _r, _clock = bot
    out = send("I don't want chicken and rice again")
    assert catalogue.refused_terms(db, pid) == [], "chicken and rice are not refused one by one"
    text = out[0]["text"]
    assert "Chicken and rice" in text and "dinner" in text, text
    assert len(text.splitlines()) <= 10
    buttons = _buttons(out[0])
    assert buttons["Swap it"] == "k:swapmeal:2026-09-14:dinner"
    assert {"Never chicken breast again", "Never basmati rice again"} <= set(buttons)
    offered = tap(buttons["Swap it"])
    assert "Swap Chicken and rice" in offered[-1]["text"]
    never = tap(buttons["Never chicken breast again"])
    assert "Kept off your menus from now on: Chicken breast." in never[-1]["text"]
    assert "chicken" in catalogue.refused_codes(db, pid)


@pytest.mark.parametrize(
    "now",
    [
        dt.datetime(2026, 9, 18, 20, 45),  # the last day of it, already recorded
        dt.datetime(2026, 9, 22, 12, 0),  # the next week, the dish behind her
    ],
)
def test_a_dish_just_eaten_is_asked_about_too(bot, now: dt.datetime) -> None:  # noqa: F811
    """The most natural moment to say "I don't want
    chicken and rice again" is after eating it, when the dish is behind her or the day is
    recorded. It is still the dish: asked about, not each food refused for good - and with no
    Swap, since there is nothing left to swap."""
    send, tap, db, pid, _r, clock = bot
    clock.set_local(now)
    if now.date() == dt.date(2026, 9, 18):
        tap("k:cfm:20260918")
        assert menu_mod.day_confirmed(db, pid, now.date())
    out = send("I don't want chicken and rice again")
    assert catalogue.refused_terms(db, pid) == [], "chicken and rice are not refused one by one"
    text = out[0]["text"]
    assert "Chicken and rice" in text and "dinner" in text, text
    buttons = _buttons(out[0])
    assert "Swap it" not in buttons, buttons
    assert {"Never chicken breast again", "Never basmati rice again"} <= set(buttons)


@pytest.mark.parametrize(
    ("said", "expect"),
    [
        ("I've had enough of chickpeas", ["chickpeas"]),
        ("no more lentils, they were awful", ["lentils"]),
    ],
)
def test_a_refusal_with_its_reason_is_not_a_report(said: str, expect: list[str]) -> None:
    """ "Had enough of" and a reason ("they were awful") do not make a
    refusal into a report of the day."""
    assert foodflow.refusal_terms(said) == expect


# ---------------------------------------------------------------- #90 made from a refused kind
def test_allergic_to_nuts_asks_about_red_pesto(fixture_db: Db, two_persons, frozen_clock) -> None:
    """Refusing nuts or dairy asks once about red pesto, which is made with
    both and whose name says neither."""
    pid = _shipped(fixture_db, two_persons)
    nuts = prefs.amend(
        fixture_db,
        pid,
        "avoid",
        add=catalogue.avoid_terms("I'm allergic to nuts"),
        clock=frozen_clock,
    )
    assert "Pesto" in [a["name"] for a in nuts.asks]
    assert prefs.ask_said(next(a for a in nuts.asks if a["name"] == "Pesto")) == (
        "Pesto is made from nuts: keep it off too, or is it fine?"
    )
    assert "pesto_red" in {i.code for i in catalogue.approved(fixture_db, pid)}, (
        "asked, not assumed"
    )
    prefs.answer(fixture_db, pid, "pesto_red", keep_off=False, clock=frozen_clock)
    dairy = prefs.amend(fixture_db, pid, "avoid", add=["dairy"], clock=frozen_clock)
    assert "Pesto" not in [a["name"] for a in dairy.asks], "asked once: the answer is kept"


def test_no_dairy_asks_about_red_pesto(fixture_db: Db, two_persons, frozen_clock) -> None:
    """Refusing dairy, or cheese, asks about red pesto too."""
    pid = _shipped(fixture_db, two_persons)
    for word in ("dairy", "cheese"):
        got = prefs.amend(fixture_db, pid, "avoid", add=[word], strict=False, clock=frozen_clock)
        assert "Pesto" in [a["name"] for a in got.asks], word
        prefs.amend(fixture_db, pid, "avoid", remove=[word], clock=frozen_clock)


def test_the_bot_asks_about_pesto_after_an_allergy(bot) -> None:  # noqa: F811
    """Through the bot, "I'm allergic to nuts" asks about pesto with a
    button for each answer."""
    send, tap, db, pid, _r, _clock = bot
    seed_ingredient(db, "almonds", "Almonds, whole", "condiments", 200.0, "200 g bag")
    seed_ingredient(db, "pesto_red", "Pesto, red", "condiments", 190.0, "190 g jar")
    out = send("I'm allergic to nuts")
    assert "Pesto is made from nuts: keep it off too, or is it fine?" in out[0]["text"]
    buttons = _buttons(out[0])
    kept = tap(buttons["Keep pesto off"])
    assert "Kept off your menus from now on: Pesto." in kept[-1]["text"]
    assert "pesto_red" not in {i.code for i in catalogue.approved(db, pid)}


@pytest.mark.parametrize("nut", ["cashews", "pine nuts"])
def test_an_allergy_to_one_nut_from_a_sentence_asks_about_pesto(
    fixture_db: Db, two_persons, frozen_clock, nut: str
) -> None:
    """A sentence is read strictly, and no food in the
    catalogue is called "cashews", so "I'm allergic to cashews" kept nothing and never asked
    about the pesto made from them. The allergy is kept, said to name no food yet, and asked."""
    pid = _shipped(fixture_db, two_persons)
    terms = catalogue.avoid_terms(f"I'm allergic to {nut}")
    got = prefs.amend(fixture_db, pid, "avoid", add=terms, strict=True, clock=frozen_clock)
    assert got.added == (nut,) and got.unknown == ()
    assert [a["name"] for a in got.asks] == ["Pesto"]
    assert nut in catalogue.refused_terms(fixture_db, pid)


def test_the_bot_asks_about_pesto_after_a_pine_nut_allergy(bot) -> None:  # noqa: F811
    """Through the bot, "I'm allergic to pine nuts" is
    kept and asks about pesto, rather than going to the chat."""
    send, _tap, db, pid, _r, _clock = bot
    seed_ingredient(db, "almonds", "Almonds, whole", "condiments", 200.0, "200 g bag")
    seed_ingredient(db, "pesto_red", "Pesto, red", "condiments", 190.0, "190 g jar")
    out = send("I'm allergic to pine nuts")
    text = out[0]["text"]
    assert "Pesto is made from pine nuts: keep it off too, or is it fine?" in text, text
    assert "pine nuts" in catalogue.refused_terms(db, pid)
    assert "Keep pesto off" in _buttons(out[0])


# ---------------------------------------------------------------- #26 the evening's own question
def test_the_answer_to_what_changed_is_not_a_refusal(bot) -> None:  # noqa: F811
    """After Not quite, "No chicken, rice instead" is the answer to "What
    changed?" - recorded for the day - and refuses nothing."""
    send, tap, db, pid, _r, clock = bot
    clock.set_local(dt.datetime(2026, 9, 14, 20, 40))
    asked = tap("k:cfmx:20260914")
    assert "What changed?" in asked[-1]["text"]
    out = send("No chicken, rice instead")
    assert catalogue.refused_terms(db, pid) == []
    assert "did that replace?" in out[-1]["text"], out
    assert _state(db, pid)["note"] == "No chicken, rice instead", "the day's answer is held"


def test_a_bare_no_at_the_armed_evening_question_is_left_to_it(bot) -> None:  # noqa: F811
    """While the evening check-in waits, a bare "no X" may be what changed
    today, so it is left to the check-in; a sentence that says "from now on" is still kept."""
    send, _tap, db, pid, _r, clock = bot
    clock.set_local(MON_EVENING)
    assert dayflow.arm_evening(db, pid, clock.today_local(), clock=clock)
    out = send("No chicken, rice instead")
    assert catalogue.refused_terms(db, pid) == []
    assert "did that replace?" in out[-1]["text"], out
    assert foodflow.refusal_terms("no salmon", bare_no=False) == []
    assert foodflow.refusal_terms("no more salmon", bare_no=False) == ["salmon"]


# ---------------------------------------------------------------- #27 for now, and the shopping
@pytest.mark.parametrize(
    "said",
    [
        "We don't need rice this week",
        "Don't need yoghurt, still got some",
        "No salmon this week please",
        "no lentils for now",
        "no salmon or chicken for the time being",
        "I don't want salmon or chicken this evening",
        "no lentils or chickpeas at the weekend",
        "no salmon or rice for a few days",
        "no chickpeas until next month",
        "Don't get rice, we've still got some",
        "don't get chickpeas",
    ],
)
def test_this_week_and_the_shopping_are_not_refusals(bot, said: str) -> None:  # noqa: F811
    """A refusal limited to this week, or a remark about the shopping, is not
    written to the avoid list for good."""
    send, _tap, db, pid, _r, _clock = bot
    assert foodflow.refusal_terms(said) == []
    send(said)
    assert catalogue.refused_terms(db, pid) == []


def test_a_sentence_not_read_whole_writes_none_of_it(bot) -> None:  # noqa: F811
    """A qualifier no list knows ("during Lent") stays on
    the last food, which then names nothing, while the foods before it were written for good. A
    term holding a food word and more is a sentence not read whole, so none of it is written."""
    send, _tap, db, pid, _r, _clock = bot
    said = "no salmon or chicken during Lent"
    assert foodflow.refusal_terms(said) == ["salmon", "chicken during lent"]
    out = send(said)
    assert catalogue.refused_terms(db, pid) == []
    assert all("Kept off your menus" not in m["text"] for m in out), out
    # a word that names no food at all is still said back, and the food beside it kept
    out = send("I don't want lentils and the faff")
    assert catalogue.refused_terms(db, pid) == ["lentils"]
    assert "I have no food called 'faff'" in out[0]["text"], out[0]["text"]


# ---------------------------------------------------------------- #34 about the other person
@pytest.mark.parametrize(
    "said",
    [
        "Sam doesn't eat salmon",
        "she's allergic to lentils",
        "he can't stand chickpeas",
        "my girlfriend is not keen on salmon",
    ],
)
def test_a_sentence_about_someone_else_is_not_written_to_mine(bot, said: str) -> None:  # noqa: F811
    """A sentence about another person never lands on the sender's own avoid
    list."""
    send, _tap, db, pid, _r, _clock = bot
    assert foodflow.refusal_terms(said) == []
    send(said)
    assert catalogue.refused_terms(db, pid) == []


def test_the_other_persons_name_is_someone_else(bot) -> None:  # noqa: F811
    """The other person's name before the refusal makes it theirs, and the
    sender's own first-person sentence is still kept."""
    assert foodflow.refusal_terms("Sam never eats salmon", others=["Sam"]) == []
    assert foodflow.refusal_terms("Sam can't stand coriander", others=["Sam"]) == []
    assert foodflow.refusal_terms("I can't stand coriander", others=["Sam"]) == ["coriander"]
    send, _tap, db, pid, _r, _clock = bot
    send("Sam never eats salmon")
    assert catalogue.refused_terms(db, pid) == [], "the bot reads the name she was given"
    send("I can't stand salmon")
    assert catalogue.refused_terms(db, pid) == ["salmon"]


@pytest.mark.parametrize(
    "said",
    [
        "Sam can't stand coriander or salmon",
        "No more salmon or tuna for Sam",
        "no salmon or chickpeas for her",
        "I don't want lentils for him",
        "no more chickpeas for my girlfriend",
        "no salmon in her lunch",
    ],
)
def test_partner_name_or_for_partner_anywhere_is_someone_else(bot, said: str) -> None:  # noqa: F811
    """Her name lives in person_names (her
    persons.display_name is blank in prod), and a name or "for her" after the refusal makes it
    about her as much as one before it. Nothing is written to the owner's list."""
    send, _tap, db, pid, _r, _clock = bot
    out = send(said)
    assert catalogue.refused_terms(db, pid) == [], out
    assert all("Kept off your menus" not in m["text"] for m in out), out


@pytest.mark.parametrize(
    ("said", "food"),
    [
        ("The dal is horrible, no more lentils", "lentils"),
        ("My stomach says no more chickpeas", "chickpeas"),
        ("They were awful, no more lentils", "lentils"),
        ("Our dal is lovely but I don't want lentils", "lentils"),
    ],
)
def test_my_own_sentence_with_is_or_my_is_still_mine(bot, said: str, food: str) -> None:  # noqa: F811
    """ "Is", "my", "our" and a "they" meaning the food
    are in the sender's own sentences. Only a third person as the subject - she, he, they, a
    name, or "my girlfriend" - makes it somebody else's."""
    send, _tap, db, pid, _r, _clock = bot
    out = send(said)
    assert "Kept off your menus from now on" in out[0]["text"], out
    assert catalogue.refused_terms(db, pid) == [food]


# ---------------------------------------------------------------- #125 other everyday wordings
@pytest.mark.parametrize(
    "said",
    [
        "Please no lentils",
        "lentils, never again",
        "Never again lentils",
        "no more lentils for me thanks",
        "lentils, never again please",
        "Lentils, no more thanks",
        "lentils - never again, thank you!",
    ],
)
def test_other_everyday_wordings_are_kept(bot, said: str) -> None:  # noqa: F811
    """ "Please no X", "X, never again" and "no more X for me" are kept."""
    assert foodflow.refusal_terms(said) == ["lentils"]
    send, _tap, db, pid, _r, _clock = bot
    out = send(said)
    assert "Kept off your menus from now on" in out[0]["text"], out
    assert catalogue.refused_terms(db, pid) == ["lentils"]


# ---------------------------------------------------------------- the second verification
@pytest.mark.parametrize(
    "said",
    [
        "no rice in the evenings",
        "I don't want rice in the evening",
        "no salmon in the week",
        "no chicken in the mornings",
        "no salmon at lunch",
        "stop putting chickpeas in my lunches",
    ],
)
def test_a_time_of_day_is_not_a_place_and_not_for_good(bot, said: str) -> None:  # noqa: F811
    assert catalogue.avoid_terms(said) == []
    assert foodflow.refusal_terms(said) == []
    send, _tap, db, pid, _r, _clock = bot
    out = send(said)
    assert catalogue.refused_terms(db, pid) == [], out
    assert all("Kept off your menus" not in m["text"] for m in out), out


@pytest.mark.parametrize(
    ("said", "food"),
    [
        ("I'm allergic to salmon, I need to avoid it", "salmon"),
        ("From tomorrow, no more lentils", "lentils"),
        ("Starting next week, no more lentils", "lentils"),
        ("From Monday no more lentils", "lentils"),
        ("From today, no more lentils", "lentils"),
        ("I'm intolerant to lentils, not just for now", "lentils"),
    ],
)
def test_a_refusal_that_explains_itself_or_starts_later_is_kept(
    bot,  # noqa: F811
    said: str,
    food: str,
) -> None:
    send, _tap, db, pid, _r, _clock = bot
    out = send(said)
    assert "Kept off your menus from now on" in out[0]["text"], out
    assert catalogue.refused_terms(db, pid) == [food]


@pytest.mark.parametrize(
    "said",
    [
        "no rice, we have plenty",
        "No yoghurt, we've got loads",
        "no rice, there's loads in the cupboard",
        "No chickpeas, we already have some",
        "we don't need rice",
        "no lentils, I've got lots",
    ],
)
def test_more_shopping_remarks_are_not_refusals(bot, said: str) -> None:  # noqa: F811
    assert foodflow.refusal_terms(said) == []
    send, _tap, db, pid, _r, _clock = bot
    send(said)
    assert catalogue.refused_terms(db, pid) == []


def test_a_cook_note_for_this_week_is_not_written_for_good(bot) -> None:  # noqa: F811
    from tiffin.jobs import menu_propose
    from tiffin.persons import Person

    _send, _tap, db, pid, _r, clock = bot
    owner_ = Person(pid, "owner", "Alex", "neutral", 1000001, None, False)
    for note in ("No salmon this week please", "no rice for now", "no chicken in the evenings"):
        assert catalogue.avoid_terms(note) == [], note
        took = menu_propose.refuse_from_note(db, owner_, note, clock=clock, said=lambda _s: None)
        assert took == [], note
    assert catalogue.refused_terms(db, pid) == []
    took = menu_propose.refuse_from_note(db, owner_, "no salmon", clock=clock, said=lambda _s: None)
    assert took == ["salmon"], "a note for good is still kept"


@pytest.mark.parametrize(
    "said",
    [
        "My gf can't stand salmon",
        "the missus won't eat salmon",
        "My other half can't stand salmon",
        "my bf can't stand salmon",
        "the wife won't eat salmon",
        "my hubby can't stand salmon",
        "the mrs won't eat salmon",
        "My mate can't stand salmon",
        "Tom doesn't eat salmon",
    ],
)
def test_other_words_for_the_other_person_are_someone_else(bot, said: str) -> None:  # noqa: F811
    assert foodflow.refusal_terms(said) == []
    send, _tap, db, pid, _r, _clock = bot
    send(said)
    assert catalogue.refused_terms(db, pid) == []


@pytest.mark.parametrize(
    ("said", "food"),
    [
        ("My tummy doesn't like chickpeas", "chickpeas"),
        ("My stomach doesn't like lentils", "lentils"),
        ("My gut does not like chickpeas", "chickpeas"),
    ],
)
def test_my_own_body_doesnt_is_still_mine(bot, said: str, food: str) -> None:  # noqa: F811
    send, _tap, db, pid, _r, _clock = bot
    out = send(said)
    assert "Kept off your menus from now on" in out[0]["text"], out
    assert catalogue.refused_terms(db, pid) == [food]


def test_peppers_kept_at_home_are_still_the_vegetable(
    fixture_db: Db, two_persons, frozen_clock
) -> None:
    pid = _shipped(fixture_db, two_persons)
    got = prefs.amend(fixture_db, pid, "pantry", add=["peppers"], clock=frozen_clock)
    assert got.hides["peppers"] == ("Red pepper",)
    assert catalogue.pantry_codes(fixture_db, pid) == {"red_pepper"}


def test_a_dish_name_with_no_food_in_half_of_it_is_asked(bot) -> None:  # noqa: F811
    send, tap, db, pid, _r, _clock = bot
    out = send("I don't want fish and chips again")
    assert catalogue.refused_terms(db, pid) == []
    text = out[0]["text"]
    assert "fish and chips" in text.casefold() and "Kept off your menus" not in text, text
    assert len(text.splitlines()) <= 10
    buttons = _buttons(out[0])
    assert buttons.get("Keep fish off") == "k:avadd:fish", buttons
    kept = tap(buttons["Keep fish off"])
    assert "Kept off your menus from now on: fish (" in kept[-1]["text"], kept[-1]["text"]
    assert catalogue.refused_terms(db, pid) == ["fish"]


@pytest.mark.parametrize(
    "said",
    [
        "No more chicken and rice",
        "I'm sick of chicken and rice",
        "I've had enough of chicken and rice",
        "fed up with chicken and rice",
    ],
)
def test_no_more_a_dish_just_eaten_is_asked_about(bot, said: str) -> None:  # noqa: F811
    send, _tap, db, pid, _r, clock = bot
    clock.set_local(dt.datetime(2026, 9, 19, 12, 0))  # Saturday, the week's dish behind her
    out = send(said)
    assert catalogue.refused_terms(db, pid) == [], out
    assert "Chicken and rice" in out[0]["text"], out
    assert {"Never chicken breast again", "Never basmati rice again"} <= set(_buttons(out[0]))


@pytest.mark.parametrize(
    ("said", "refused", "aside"),
    [
        ("No chicken, salmon thanks", ["chicken"], ["salmon"]),
        ("no chicken, salmon thank you", ["chicken"], ["salmon"]),
        ("No chicken, salmon or tuna please", ["chicken"], ["salmon", "tuna"]),
        ("No chicken, salmon and rice please", ["chicken", "salmon", "rice"], []),
        ("no chicken, nor salmon please", ["chicken", "salmon"], []),
    ],
)
def test_thanks_and_an_or_set_the_food_asked_for_aside(
    said: str, refused: list[str], aside: list[str]
) -> None:
    assert catalogue.avoid_terms(said) == refused
    assert catalogue.set_aside(said) == aside


def test_no_chicken_salmon_thanks_keeps_only_the_chicken_off(bot) -> None:  # noqa: F811
    send, _tap, db, pid, _r, _clock = bot
    out = send("No chicken, salmon thanks")
    assert catalogue.refused_terms(db, pid) == ["chicken"]
    assert "Keep salmon off too" in _buttons(out[0])


@pytest.mark.parametrize(
    ("said", "expect"),
    [
        ("please stop putting mushrooms on everything", ["mushrooms"]),
        ("stop putting chickpeas into everything", ["chickpeas"]),
        ("stop putting mushrooms into things", ["mushrooms"]),
        ("stop putting cheese onto my food", ["cheese"]),
        ("no tuna in brine", ["tuna in brine"]),
    ],
)
def test_on_and_into_are_places_too(said: str, expect: list[str]) -> None:
    assert catalogue.avoid_terms(said) == expect
    assert foodflow.refusal_terms(said) == expect


def test_stop_putting_into_everything_through_the_bot(bot) -> None:  # noqa: F811
    send, _tap, db, pid, _r, _clock = bot
    out = send("stop putting chickpeas into everything")
    assert "Kept off your menus from now on" in out[0]["text"], out
    assert catalogue.refused_terms(db, pid) == ["chickpeas"]


@pytest.mark.parametrize("said", ["lentils - not any more", "lentils, not anymore"])
def test_not_any_more_after_the_food_is_kept(bot, said: str) -> None:  # noqa: F811
    assert foodflow.refusal_terms(said) == ["lentils"]
    send, _tap, db, pid, _r, _clock = bot
    out = send(said)
    assert "Kept off your menus from now on" in out[0]["text"], out
    assert catalogue.refused_terms(db, pid) == ["lentils"]


# ---------------------------------------------------------------- the third verification
def _owner(pid: int):
    from tiffin.persons import Person

    return Person(pid, "owner", "Alex", "neutral", 1000001, None, False)


def _cleared(monkeypatch) -> list[int | None]:
    """Every keyboard a handler takes off its message, by message id."""
    from tiffin.telegram.handlers import Handlers

    taken: list[int | None] = []
    monkeypatch.setattr(
        Handlers, "clear_buttons", lambda _h, inbound: taken.append(inbound.message_id)
    )
    return taken


@pytest.mark.parametrize(
    ("said", "refused", "aside"),
    [
        ("No mushrooms, olives and anchovies please", ["mushrooms", "olives", "anchovies"], []),
        ("no rice, chickpeas and lentils please", ["rice", "chickpeas", "lentils"], []),
        ("no rice, chickpeas & lentils thanks", ["rice", "chickpeas", "lentils"], []),
        ("no rice, pasta or bread please", ["rice"], ["pasta", "bread"]),
        ("No chicken, salmon please", ["chicken"], ["salmon"]),
    ],
)
def test_a_list_joined_by_and_is_every_food_in_it(
    said: str, refused: list[str], aside: list[str]
) -> None:
    assert catalogue.avoid_terms(said) == refused
    assert catalogue.set_aside(said) == aside


def test_and_through_the_bot_keeps_every_kind_of_olive_off(bot) -> None:  # noqa: F811
    send, _tap, db, pid, _r, _clock = bot
    seed_ingredient(db, "mushrooms", "Mushrooms, raw", "produce", 250.0, "250 g punnet")
    for code, name in (
        ("olives_g", "Olives, green, in brine"),
        ("olives_b", "Olives, black, in brine"),
        ("olives_k", "Olives, kalamata"),
    ):
        seed_ingredient(db, code, name, "tins", 200.0, "200 g jar")
    seed_ingredient(db, "anchovies", "Anchovies, tinned", "tins", 50.0, "50 g tin")
    out = send("No mushrooms, olives and anchovies please")
    assert "Kept off your menus from now on" in out[0]["text"], out
    assert catalogue.refused_terms(db, pid) == ["anchovies", "mushrooms", "olives"]
    left = {i.code for i in catalogue.approved(db, pid)}
    assert not left & {"mushrooms", "olives_g", "olives_b", "olives_k", "anchovies"}, left


def test_each_food_set_aside_is_one_tap_by_its_word(bot, monkeypatch) -> None:  # noqa: F811
    send, tap, db, pid, _r, _clock = bot
    seed_ingredient(db, "rice_brown", "Brown rice, dry", "grains", 500.0, "500 g bag")
    out = send("no chicken, salmon or rice please")
    assert catalogue.refused_terms(db, pid) == ["chicken"]
    buttons = _buttons(out[0])
    assert buttons.get("Keep salmon off too") == "k:avadd:salmon", buttons
    assert buttons.get("Keep rice off too") == "k:avadd:rice", buttons
    taken = _cleared(monkeypatch)
    kept = tap(buttons["Keep rice off too"])
    assert "Kept off your menus from now on: rice (" in kept[-1]["text"], kept[-1]["text"]
    left = {i.code for i in catalogue.approved(db, pid)}
    assert "rice" not in left and "rice_brown" not in left, "every rice, not one product"
    assert taken == [], "the salmon's button is still there to tap"
    tap(buttons["Keep salmon off too"])
    assert catalogue.refused_terms(db, pid) == ["chicken", "rice", "salmon"]
    again = tap(buttons["Keep salmon off too"])
    assert "Already on the list: salmon." in again[-1]["text"], again


@pytest.mark.parametrize(
    "said",
    [
        "no chicken in my meals during Lent",
        "no chicken in my meals midweek",
        "no salmon in my food while I'm pregnant",
        "no salmon on the menu during Lent",
    ],
)
def test_a_place_with_more_after_it_does_not_end_the_sentence(bot, said: str) -> None:  # noqa: F811
    from tiffin.jobs import menu_propose

    send, _tap, db, pid, _r, clock = bot
    out = send(said)
    assert catalogue.refused_terms(db, pid) == [], out
    assert all("Kept off your menus" not in m["text"] for m in out), out
    took = menu_propose.refuse_from_note(db, _owner(pid), said, clock=clock, said=lambda _s: None)
    assert took == [] and catalogue.refused_terms(db, pid) == []


@pytest.mark.parametrize(
    ("said", "expect"),
    [
        ("stop putting mushrooms in everything, I hate them", ["mushrooms"]),
        ("stop putting mushrooms in my food please", ["mushrooms"]),
        ("no mushrooms in my meals any more", ["mushrooms"]),
        ("stop putting mushrooms on everything!", ["mushrooms"]),
    ],
)
def test_a_place_at_the_end_still_ends_the_list(said: str, expect: list[str]) -> None:
    assert catalogue.avoid_terms(said) == expect


def test_a_dish_in_a_list_holds_back_only_its_two_words(bot, monkeypatch) -> None:  # noqa: F811
    send, tap, db, pid, _r, _clock = bot
    out = send("I don't want lentils, salmon and kale")
    assert catalogue.refused_terms(db, pid) == ["lentils"]
    text = out[0]["text"]
    assert "Kept off your menus from now on: lentils" in text, text
    assert "Salmon and kale" in text, text
    assert len(text.splitlines()) <= 10
    buttons = _buttons(out[0])
    assert buttons.get("Keep salmon off") == "k:avadd:salmon", buttons
    assert "Undo" in buttons
    taken = _cleared(monkeypatch)
    kept = tap(buttons["Keep salmon off"])
    assert "Kept off your menus from now on: salmon (Salmon fillets)." in kept[-1]["text"]
    assert catalogue.refused_terms(db, pid) == ["lentils", "salmon"]
    assert taken == []


def test_a_dish_alone_is_still_asked_by_its_word(bot) -> None:  # noqa: F811
    send, tap, db, pid, _r, _clock = bot
    out = send("no more salmon and kale")
    assert catalogue.refused_terms(db, pid) == []
    assert "Salmon and kale" in out[0]["text"], out
    tap(_buttons(out[0])["Keep salmon off"])
    assert catalogue.refused_terms(db, pid) == ["salmon"]


@pytest.mark.parametrize(
    ("said", "expect"),
    [
        ("I don't want the baby spinach", ["baby spinach"]),
        ("Stop putting the baby spinach in everything", ["baby spinach"]),
        ("I'm sick of the baby plum tomatoes", ["baby plum tomatoes"]),
    ],
)
def test_a_person_word_that_starts_the_food_is_the_food(said: str, expect: list[str]) -> None:
    assert foodflow.refusal_terms(said, others=["Alex"]) == expect


def test_the_baby_spinach_through_the_bot(bot) -> None:  # noqa: F811
    send, _tap, db, pid, _r, _clock = bot
    seed_ingredient(db, "spinach_baby", "Baby spinach, raw", "produce", 200.0, "200 g bag")
    out = send("I don't want the baby spinach")
    assert "Kept off your menus from now on" in out[0]["text"], out
    assert catalogue.refused_terms(db, pid) == ["baby spinach"]
    assert foodflow.refusal_terms("the baby is allergic to nuts") == []
    assert foodflow.refusal_terms("no more salmon for the baby") == []


@pytest.mark.parametrize(
    "said",
    [
        "No rice, we have tons",
        "no rice, we've got heaps",
        "No rice, we have a big bag",
        "no rice, we're well stocked",
        "no rice, plenty at home",
        "No lentils, we're stocked up",
        "no chickpeas, we've two tins left",
    ],
)
def test_other_words_for_having_it_at_home_are_the_shopping(bot, said: str) -> None:  # noqa: F811
    assert catalogue.for_now(said)
    assert foodflow.refusal_terms(said) == []
    send, _tap, db, pid, _r, _clock = bot
    send(said)
    assert catalogue.refused_terms(db, pid) == []


@pytest.mark.parametrize(
    ("said", "food"),
    [
        ("no chickpeas, I have IBS", "chickpeas"),
        ("No lentils, I have a sensitive stomach", "lentils"),
    ],
)
def test_a_reason_that_is_not_the_shopping_is_still_kept(bot, said: str, food: str) -> None:  # noqa: F811
    send, _tap, db, pid, _r, _clock = bot
    out = send(said)
    assert "Kept off your menus from now on" in out[0]["text"], out
    assert catalogue.refused_terms(db, pid) == [food]


def test_a_cook_note_not_read_whole_writes_none_of_it(bot) -> None:  # noqa: F811
    from tiffin.jobs import menu_propose

    _send, _tap, db, pid, _r, clock = bot
    note = "no salmon or chicken during Lent"
    took = menu_propose.refuse_from_note(db, _owner(pid), note, clock=clock, said=lambda _s: None)
    assert took == []
    assert catalogue.refused_terms(db, pid) == []


@pytest.mark.parametrize(
    "said",
    ["No more lentils from tomorrow", "no more lentils from now on", "No lentils from Monday"],
)
def test_from_tomorrow_after_the_food_is_not_part_of_it(bot, said: str) -> None:  # noqa: F811
    assert foodflow.refusal_terms(said) == ["lentils"]
    send, _tap, db, pid, _r, _clock = bot
    out = send(said)
    assert "Kept off your menus from now on" in out[0]["text"], out
    assert catalogue.refused_terms(db, pid) == ["lentils"]


@pytest.mark.parametrize(
    "said",
    [
        "Tom is allergic to salmon",
        "Tom's allergic to salmon",
        "tom is intolerant to salmon",
        "Tom is not keen on salmon",
        "Tom is really not a fan of salmon",
        "Tom can't stand salmon",
        "Tom won't eat salmon",
        "Mum really can't stand salmon",
    ],
)
def test_a_name_before_any_cue_is_someone_else(bot, said: str) -> None:  # noqa: F811
    assert foodflow.refusal_terms(said) == []
    send, _tap, db, pid, _r, _clock = bot
    send(said)
    assert catalogue.refused_terms(db, pid) == []


@pytest.mark.parametrize(
    "said",
    [
        "Honestly can't stand salmon",
        "Really won't eat salmon",
        "Seriously can't stand salmon",
        "I can't stand salmon",
        "I'm allergic to salmon",
        "I'm not keen on salmon",
        "Just can't stand salmon",
    ],
)
def test_my_own_sentence_with_the_i_dropped_is_still_mine(said: str) -> None:
    assert foodflow.refusal_terms(said) == ["salmon"]
