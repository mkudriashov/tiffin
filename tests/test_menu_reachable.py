"""The menu you can actually cook from.

Six ways the weekly menu stopped being usable: /cook fell silent on the commonest path, a
rebuilt menu was thrown away by the outbox, accepting one destroyed the buttons that open its
recipes, an unanswered proposal was swapped for last week's without a word, a mid-segment
replan left two menus current, and /recipe reliably answered with the snack.
"""

from __future__ import annotations

import datetime as dt

from tiffin import targets
from tiffin.app import App
from tiffin.clock import TZ
from tiffin.food import catalogue, menu
from tiffin.jobs import menu_propose
from tiffin.secrets import Secrets

from .fakes import FakeTelegram
from .test_menu import MONDAY, FakeGateway, proposal_for, world  # noqa: F401


def a_menu(db, person_id: int, start: dt.date, end: dt.date, *, status: str) -> int:
    mid = db.insert(
        "menus",
        person_id=person_id,
        segment_start=start.isoformat(),
        segment_end=end.isoformat(),
        status=status,
    )
    day = db.insert(
        "menu_days",
        menu_id=mid,
        day=start.isoformat(),
        kcal=2000,
        protein_g=180,
        fat_g=70,
        carb_g=200,
        fibre_g=30,
    )
    recipe = db.insert(
        "recipes",
        name=f"Dish {mid}",
        servings=1,
        prep_minutes=10,
        method_json="[]",
        source="manual",
    )
    db.insert(
        "meal_slots",
        menu_day_id=day,
        slot="dinner",
        recipe_id=recipe,
        portion_multiplier=1.0,
        portion_g=300,
        kcal=500,
        protein_g=45,
        fat_g=18,
        carb_g=50,
        fibre_g=8,
    )
    return mid


# ---------------------------------------------------------------- silence on /cook
def test_cook_says_something_when_a_menu_already_covers_those_days(world) -> None:  # noqa: F811
    """The caller decided whether to speak by testing for the word "menu" in ledger prose,
    and "A menu already covers Mon 14 Sep" contains it - so the commonest use of /cook ended
    in silence after "This takes a minute"."""
    _pid, db, _tg, gateways, say, tap, run_job = world
    segment = menu.Segment(MONDAY, dt.date(2026, 9, 18))
    gateways.append(FakeGateway([proposal_for(segment)]))
    run_job(menu_propose.JOB, dt.date(2026, 9, 12))
    menu_id = int(db.scalar("SELECT id FROM menus ORDER BY id DESC LIMIT 1"))
    tap(f"k:menu:{menu_id}")
    db.clock.set_local(dt.datetime(2026, 9, 14, 9, 0, tzinfo=TZ))

    out = say("/cook")
    assert "already covers" in out, "it answers rather than trailing off"
    assert "&lt;code&gt;" not in out, "and does not double-escape its own markup"


def test_a_rebuilt_menu_actually_reaches_the_phone(world) -> None:  # noqa: F811
    """Both menus shared the outbox key isoday(segment_start), so the second was solved,
    stored, charged for and dropped: the person cooked the food they had just rejected."""
    _pid, db, tg, gateways, say, tap, run_job = world
    segment = menu.Segment(MONDAY, dt.date(2026, 9, 18))
    # two answers a build: the fixture's dishes carry 70% of the day's carbohydrate, under the
    # floor, so each build asks once more before keeping what it has
    gateways.append(FakeGateway([proposal_for(segment)] * 4))
    run_job(menu_propose.JOB, dt.date(2026, 9, 12))
    first = int(db.scalar("SELECT id FROM menus ORDER BY id DESC LIMIT 1"))
    # Sunday: tomorrow is the Monday this menu starts on, so the rebuild covers the same days
    # and lands on the same outbox key the proposal took
    db.clock.set_local(dt.datetime(2026, 9, 13, 10, 0, tzinfo=TZ))
    assert "what to change" in tap(f"k:menux:{first}")

    before = len(tg.sent)
    say("more fish")
    second = int(db.scalar("SELECT id FROM menus ORDER BY id DESC LIMIT 1"))
    sent = [m["text"] for m in tg.sent[before:]]
    assert second != first, f"a new menu was built; got {sent}"
    assert any("Menu for the next cook" in t for t in sent), f"and sent: {sent}"
    keys = db.all("SELECT due_key FROM messages_out WHERE kind = 'menu_proposal'")
    assert len({str(r["due_key"]) for r in keys}) == 2, "one outbox key per menu"


# ------------------------------------------ the meal buttons need no accept to survive
def test_a_menu_arrives_with_its_meal_buttons_already_on_it(world) -> None:  # noqa: F811
    _pid, db, tg, gateways, _say, tap, run_job = world
    segment = menu.Segment(MONDAY, dt.date(2026, 9, 18))
    gateways.append(FakeGateway([proposal_for(segment)]))
    run_job(menu_propose.JOB, dt.date(2026, 9, 12))
    menu_id = int(db.scalar("SELECT id FROM menus ORDER BY id DESC LIMIT 1"))
    sent = [
        b["callback_data"] for row in tg.sent[-1]["reply_markup"]["inline_keyboard"] for b in row
    ]
    assert sent[:2] == [f"k:swappick:{menu_id}", f"k:menux:{menu_id}"], (
        "Swap a dish and New menu, first, in place of the old Accept row"
    )
    meal_buttons = [d for d in sent if d.startswith("k:meal:")]
    assert meal_buttons, "and the meal buttons that open a recipe are already there"
    assert not any(d.startswith("k:menu:") for d in sent), "no Accept button to tap at all"

    tap(f"k:menu:{menu_id}")
    assert tg.edited == [], "the old tap does not touch the keyboard - it never wrote one"


# ---------------------------------------------------------------- a stale tap tells the truth
def test_an_old_accept_tap_on_a_superseded_menu_says_so(world) -> None:  # noqa: F811
    _pid, db, _tg, gateways, say, tap, run_job = world
    segment = menu.Segment(MONDAY, dt.date(2026, 9, 18))
    gateways.append(FakeGateway([proposal_for(segment), proposal_for(segment)]))
    run_job(menu_propose.JOB, dt.date(2026, 9, 12))
    first = int(db.scalar("SELECT id FROM menus ORDER BY id DESC LIMIT 1"))

    db.clock.set_local(dt.datetime(2026, 9, 13, 10, 0, tzinfo=TZ))
    say("/cook replace")
    second = int(db.scalar("SELECT id FROM menus ORDER BY id DESC LIMIT 1"))
    assert second != first, "a new menu was built to replace it"

    out = tap(f"k:menu:{first}")
    assert "already replaced" in out, f"a superseded menu must not claim to be current: {out}"
    assert "in force" not in out, f"that would be false for this one: {out}"


# ---------------------------------------------------------------- one current menu
def test_a_replanned_segment_leaves_the_days_it_never_touched_alone(
    fixture_db, two_persons
) -> None:
    pid = two_persons["owner"]
    old = a_menu(fixture_db, pid, dt.date(2026, 9, 14), dt.date(2026, 9, 18), status="accepted")
    new = a_menu(fixture_db, pid, dt.date(2026, 9, 17), dt.date(2026, 9, 18), status="accepted")
    current = fixture_db.all("SELECT id FROM v_menu_current WHERE person_id = ?", (pid,))
    assert {int(r["id"]) for r in current} == {old, new}, (
        f"the replan should win Thu-Fri, not erase Mon-Wed too: {[dict(r) for r in current]}"
    )
    # per-day resolution is what actually matters: Monday still reads as the old menu, Thursday
    # as the new one
    assert menu.accepted_menu_id(fixture_db, pid, dt.date(2026, 9, 14)) == old
    assert menu.accepted_menu_id(fixture_db, pid, dt.date(2026, 9, 17)) == new


def test_two_menus_that_do_not_overlap_both_stand(fixture_db, two_persons) -> None:
    pid = two_persons["owner"]
    a = a_menu(fixture_db, pid, dt.date(2026, 9, 14), dt.date(2026, 9, 18), status="accepted")
    b = a_menu(fixture_db, pid, dt.date(2026, 9, 19), dt.date(2026, 9, 20), status="accepted")
    rows = fixture_db.all("SELECT id FROM v_menu_current WHERE person_id = ? ORDER BY id", (pid,))
    assert [int(r["id"]) for r in rows] == [a, b]


# ---------------------------------------------------------------- the right day's recipe
def test_a_meal_button_opens_the_day_being_cooked(fixture_db, two_persons) -> None:
    """Recipe_at always took the first day of the segment, so the Dinner button opened
    Monday's dinner every day of a rotating week."""
    pid = two_persons["owner"]
    mid = fixture_db.insert(
        "menus",
        person_id=pid,
        segment_start="2026-09-14",
        segment_end="2026-09-15",
        status="accepted",
    )
    ids = []
    for day, name in ((dt.date(2026, 9, 14), "Monday dinner"), (dt.date(2026, 9, 15), "Tuesday")):
        md = fixture_db.insert(
            "menu_days",
            menu_id=mid,
            day=day.isoformat(),
            kcal=2000,
            protein_g=180,
            fat_g=70,
            carb_g=200,
            fibre_g=30,
        )
        rid = fixture_db.insert(
            "recipes", name=name, servings=1, prep_minutes=10, method_json="[]", source="manual"
        )
        ids.append(rid)
        fixture_db.insert(
            "meal_slots",
            menu_day_id=md,
            slot="dinner",
            recipe_id=rid,
            portion_multiplier=1.0,
            portion_g=300,
            kcal=500,
            protein_g=45,
            fat_g=18,
            carb_g=50,
            fibre_g=8,
        )
    tuesday = menu_propose.recipe_at(fixture_db, mid, "dinner", dt.date(2026, 9, 15))
    assert tuesday is not None and tuesday[0] == ids[1]
    # a day the menu does not cover still gets something rather than nothing
    other = menu_propose.recipe_at(fixture_db, mid, "dinner", dt.date(2026, 9, 20))
    assert other is not None and other[0] == ids[0]


def test_the_change_note_is_not_lost_to_the_cooldown(world) -> None:  # noqa: F811
    _pid, db, _tg, gateways, say, tap, run_job = world
    segment = menu.Segment(MONDAY, dt.date(2026, 9, 18))
    gateways.append(FakeGateway([proposal_for(segment), proposal_for(segment)]))
    run_job(menu_propose.JOB, dt.date(2026, 9, 12))
    menu_id = int(db.scalar("SELECT id FROM menus ORDER BY id DESC LIMIT 1"))
    say("/cook")  # sets the cooldown
    tap(f"k:menux:{menu_id}")
    out = say("more fish")
    if "Still working" in out:
        assert "/cook more fish" in out, "their own words come back so nothing is lost"


def test_cook_replace_works_when_the_bot_just_told_you_to_send_it(world) -> None:  # noqa: F811
    """`/cook` on a segment that already has a menu costs nothing and answers "send /cook
    replace" - then armed a five-minute cooldown that refused exactly that command. Every
    bare /cook re-armed it, so there was no way to wait it out."""
    pid, db, _tg, gateways, say, _tap, _run_job = world
    sunday = dt.date(2026, 9, 13)  # Saturday 18:00 in the fixture, so the next segment is Sunday
    db.insert("weigh_ins", person_id=pid, day="2026-09-12", weight_kg=100.0, source="manual")
    a_menu(db, pid, sunday, sunday, status="accepted")

    first = say("/cook")
    assert "already covers" in first and "/cook replace" in first
    assert db.one("SELECT v FROM bot_state WHERE k = ?", (f"cook_asked:{pid}",)) is None, (
        "an answer that called no model must not arm the cooldown"
    )

    gateways.append(FakeGateway([proposal_for(menu.Segment(sunday, sunday))]))
    second = say("/cook replace")
    assert "Still working on the last one" not in second
    assert db.one("SELECT v FROM bot_state WHERE k = ?", (f"cook_asked:{pid}",)) is not None, (
        "a build that did call a model still arms it"
    )


def test_menu_shows_an_accepted_weekend_before_the_weekend(world) -> None:  # noqa: F811
    """Between the tap and the day itself an accepted menu was invisible: /menu spoke about an
    upcoming segment only while it was still waiting for one. Nothing waits for a yes any more -
    a menu is decided the moment it is built and sent - so a truly undecided menu is now only
    ever a raw fixture, never something the live system produces. What still matters, and is
    still worth proving, is that /menu follows the DECISION alone, not the raw `status` column,
    on both sides of one being written.
    """
    pid, db, _tg, _gateways, say, _tap, _run_job = world
    mid = a_menu(db, pid, dt.date(2026, 9, 14), dt.date(2026, 9, 18), status="proposed")
    before = say("/menu")
    assert "No menu covers the days ahead" in before, before

    menu.decide(db, mid, "accepted")
    out = say("/menu")
    assert f"Dish {mid}" in out, "the accepted menu's meals, not silence"


def test_cook_replace_carries_the_change_and_not_the_word_replace(world) -> None:  # noqa: F811
    """Tiffin tells people to send `/cook replace no chickpeas`, so that is the shape the
    parser has to read. Matching the replace word only when it was the WHOLE argument meant the
    change request handed to the model was literally "replace no chickpeas": the person's
    actual words arrived wrapped in a command keyword the model had no reason to understand."""
    pid, db, _tg, gateways, say, _tap, _run_job = world
    sunday = dt.date(2026, 9, 13)
    db.insert("weigh_ins", person_id=pid, day="2026-09-12", weight_kg=100.0, source="manual")
    a_menu(db, pid, sunday, sunday, status="accepted")
    gw = FakeGateway([proposal_for(menu.Segment(sunday, sunday))])
    gateways.append(gw)

    say("/cook replace no chickpeas")
    asked = gw.calls[-1]
    assert "no chickpeas" in asked
    assert "replace no chickpeas" not in asked, "the keyword is a command word, not part of it"


def test_a_change_the_bot_just_asked_for_is_not_refused_by_its_own_cooldown(world) -> None:  # noqa: F811
    """
    told to send `/cook replace no chickpeas`, and be refused twice - because the /cook that
    built the accepted menu had armed a five-minute cooldown moments earlier. The refusal then
    offered that same command back as the way to retry, which walks into the same wall.

    A request carrying a note says something the last one did not, so it is never the accidental
    double-tap the cooldown is for."""
    pid, db, _tg, gateways, say, _tap, _run_job = world
    sunday = dt.date(2026, 9, 13)
    db.insert("weigh_ins", person_id=pid, day="2026-09-12", weight_kg=100.0, source="manual")
    a_menu(db, pid, sunday, sunday, status="accepted")
    db.execute(
        "INSERT INTO bot_state(k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v",
        (f"cook_asked:{pid}", db.clock.utc_stamp()),  # a menu was built seconds ago
    )
    gateways.append(FakeGateway([proposal_for(menu.Segment(sunday, sunday))]))

    out = say("/cook replace no chickpeas")
    assert "Still working on the last one" not in out, (
        "Tiffin refused the command it had just told the person to send"
    )


def test_a_bare_cook_twice_over_is_still_refused_and_says_how_long(world) -> None:  # noqa: F811
    """The cooldown still does its job for the thing it was written for, and a person who is
    told to wait is told how long: "give it a minute" with four minutes left is not an answer
    they can act on."""
    pid, db, _tg, _gw, say, _tap, _run_job = world
    db.insert("weigh_ins", person_id=pid, day="2026-09-12", weight_kg=100.0, source="manual")
    db.execute(
        "INSERT INTO bot_state(k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v",
        (f"cook_asked:{pid}", db.clock.utc_stamp()),
    )
    out = say("/cook")
    assert "Still working on the last one" in out
    assert "minute" in out


def test_menu_offers_a_button_for_every_meal_on_it(world) -> None:  # noqa: F811
    """The proposal message carries meal buttons and says "Tap a meal for its recipe". /menu
    printed the same dish names as flat text with no way in, so once the proposal had scrolled
    away the only route to a recipe was /recipe - the day's MAIN meal - or /recipe <id> with an
    id nobody is ever shown. Breakfast was unreachable."""
    pid, db, tg, _gw, say, _tap, _run_job = world
    saturday = dt.date(2026, 9, 12)
    a_menu(db, pid, saturday, saturday, status="accepted")

    out = say("/menu")
    # cadence is two segments in a working week and /menu has to open either of them
    assert "a meal for its recipe" in out
    keyboard = tg.sent[-1].get("reply_markup")
    assert keyboard, "the dish names are no use without a way to open one"
    data = [b["callback_data"] for row in keyboard["inline_keyboard"] for b in row]
    assert any(d.startswith("k:meal:") for d in data)


def test_a_meal_button_from_menu_opens_that_recipe(world) -> None:  # noqa: F811
    pid, db, tg, _gw, say, tap, _run_job = world
    saturday = dt.date(2026, 9, 12)
    a_menu(db, pid, saturday, saturday, status="accepted")
    say("/menu")
    data = [
        b["callback_data"] for row in tg.sent[-1]["reply_markup"]["inline_keyboard"] for b in row
    ]
    card = tap(next(d for d in data if d.startswith("k:meal:")))
    assert "kcal" in card, "a recipe card, not a shrug"


def test_the_card_links_to_its_meal_on_the_page_only_for_a_linked_login(world) -> None:  # noqa: F811
    """ "Potentially we send her links to tailscale where she can look at it with recipe in
    there." A card for someone the page lets in carries a link to that meal on it. For anyone
    else the page answers with a 403, so the card carries no link at all - never a dead end."""
    pid, db, tg, _gw, say, tap, _run_job = world
    saturday = dt.date(2026, 9, 12)
    a_menu(db, pid, saturday, saturday, status="accepted")

    def open_a_meal() -> str:
        say("/menu")
        data = [
            b["callback_data"]
            for row in tg.sent[-1]["reply_markup"]["inline_keyboard"]
            for b in row
        ]
        return tap(next(d for d in data if d.startswith("k:meal:")))

    assert "On the page" not in open_a_meal(), "an unlinked login is sent a link to a 403"
    db.insert("tailscale_identities", person_id=pid, login="alex@example.com")
    card = open_a_meal()
    assert "On the page" in card and "tab=menu" in card, card
    assert "day=2026-09-12" in card and "slot=" in card, f"not this meal on the page: {card}"


def test_cook_from_one_list_offers_only_its_foods_and_fails_without_a_word_to_partner(
    world,  # noqa: F811
    fixture_db,
    config,
    frozen_clock,
    tmp_path,
) -> None:
    """Option A, 14 Sep: rebuild the partner's Tue-Wed from what the owner had already bought. The
    model
    picks by index from the numbered list it is given, so the list holds only those foods. A
    build that cannot be made from them says so to whoever ran it - it must not repeat her last
    menu and send it to her as "Menu repeated" when nobody asked her anything."""
    pid, db, *_rest = world
    approved = catalogue.approved(db, pid)
    kept = approved[:4]
    left_out = [
        i for i in approved[4:] if not any(i.name in k.name or k.name in i.name for k in kept)
    ]
    assert kept and left_out, "the story needs foods on both sides of the line"
    secrets_dir = tmp_path / "s2"
    secrets_dir.mkdir()
    (secrets_dir / "telegram-token-dev.txt").write_text("1:t", encoding="utf-8")
    tg = FakeTelegram()
    gateway = FakeGateway([{}, {}, {}, {}])  # four unusable answers: nothing can be built
    app = App(
        config,
        Secrets(secrets_dir),
        clock=frozen_clock,
        transport=tg,
        gateway_factory=lambda _app, _db: gateway,  # type: ignore[arg-type]
    )
    person = app.registry(db).require("owner")
    # the evening before a cook, as on 14 Sep: asked on the Saturday itself, tomorrow is in the
    # weekend and the build is for Sunday - a week the fixture has no targets for
    frozen_clock.set_local(dt.datetime(2026, 9, 13, 18, 0, tzinfo=TZ))
    profile = targets.profile_on(db, pid, frozen_clock.today_local())
    said = menu_propose.propose_now(
        app,
        db,
        person,
        profile,
        app.outbox(db),
        clock=frozen_clock,
        replace=True,
        only_codes=frozenset(i.code for i in kept),
        quiet_failure=True,
    )
    assert said.startswith("Could not build a menu from those foods"), said
    prompt = gateway.calls[0]
    assert all(k.name in prompt for k in kept), "every bought food is offered"
    assert not any(i.name in prompt for i in left_out), "and nothing that was not bought"
    sent = " ".join(m["text"] for m in tg.sent)
    assert "Menu repeated" not in sent and "Menu for the next cook" not in sent, sent


def test_page_sends_the_recipe_book_only_to_a_linked_login(world) -> None:  # noqa: F811
    """ "Where is the web recipe book? I want a link in Telegram for myself too." /page is that
    link, opened on the Menu tab at today - and for a login the page would refuse, a sentence
    saying why and what works meanwhile, never a link to a 403."""
    pid, db, _tg, _gw, say, _tap, _run_job = world
    unlinked = say("/page")
    assert "<a href" not in unlinked and "linked" in unlinked, unlinked
    db.insert("tailscale_identities", person_id=pid, login="alex@example.com")
    linked = say("/page")
    assert "<a href" in linked and "tab=menu" in linked and "day=2026-09-12" in linked, linked
    assert "day=2026-09-17" in say("/page thu"), "a named day opens on that day"


def test_menu_with_nothing_on_it_offers_no_buttons(world) -> None:  # noqa: F811
    _pid, _db, tg, _gw, say, _tap, _run_job = world
    out = say("/menu")
    # it names the day now ("No menu covers Sat 12 Sep"), rather than saying "today"
    assert "No menu covers" in out
    assert not tg.sent[-1].get("reply_markup"), "no dishes, no buttons"


# ---------------------------------------------------------------- last week again, on purpose
def test_cook_repeat_reuses_last_week_without_a_model_call(world) -> None:  # noqa: F811
    """Repeating last week reuses its menu without asking the model."""
    pid, db, tg, gateways, say, _tap, run_job = world
    segment = menu.Segment(MONDAY, dt.date(2026, 9, 18))
    gateways.append(FakeGateway([proposal_for(segment)]))
    run_job(menu_propose.JOB, dt.date(2026, 9, 12))
    first = int(db.scalar("SELECT id FROM menus ORDER BY id DESC LIMIT 1"))
    dishes = db.all(
        "SELECT rc.name FROM menu_days md JOIN meal_slots ms ON ms.menu_day_id = md.id "
        "JOIN recipes rc ON rc.id = ms.recipe_id WHERE md.menu_id = ? ORDER BY md.day, ms.id",
        (first,),
    )
    assert dishes, "setup: the first menu has food on it"

    # a week on, on the Sunday before the next cook, with NO model available at all
    db.clock.set_local(dt.datetime(2026, 9, 20, 10, 0, tzinfo=TZ))
    gateways.clear()
    before = len(tg.sent)
    say("/cook repeat")

    repeated = int(db.scalar("SELECT id FROM menus ORDER BY id DESC LIMIT 1"))
    assert repeated != first, "nothing was written"
    row = db.one("SELECT * FROM menus WHERE id = ?", (repeated,))
    assert row is not None, f"no menus row for {repeated}"
    assert str(row["status"]) == "repeated", f"status is {row['status']!r}"
    assert int(row["source_menu_id"]) == first, "a repeat says what it is a repeat OF"
    assert str(row["segment_start"]) == "2026-09-21", (
        f"the copy landed on {row['segment_start']}, not the days about to be cooked"
    )

    again = db.all(
        "SELECT rc.name FROM menu_days md JOIN meal_slots ms ON ms.menu_day_id = md.id "
        "JOIN recipes rc ON rc.id = ms.recipe_id WHERE md.menu_id = ? ORDER BY md.day, ms.id",
        (repeated,),
    )
    assert [str(r["name"]) for r in again] == [str(r["name"]) for r in dishes], (
        "the same week, or it is not a repeat"
    )

    # in force on arrival, like every repeat: `status` alone makes it current
    assert menu.accepted_menu_id(db, pid, dt.date(2026, 9, 21)) == repeated
    sent = " ".join(m["text"] for m in tg.sent[before:])
    # headed "Menu repeated", not "Menu for the next cook": he asked for last week on purpose,
    # and the message says which of the two it is, the way `message_for(repeated=True)` does
    assert "Menu repeated" in sent, f"nothing reached his phone:\n{sent}"
    assert "Tap a meal for its recipe" in sent, "and every dish is still one tap from here"


def test_cook_repeat_with_nothing_to_repeat_says_so(world) -> None:  # noqa: F811
    """His first week: there is no last menu. Degrade to manual, never to silence - it has to
    name the command that does work rather than answering with nothing."""
    _pid, db, tg, _gateways, say, _tap, _run_job = world
    db.clock.set_local(dt.datetime(2026, 9, 20, 10, 0, tzinfo=TZ))
    before = len(tg.sent)
    say("/cook repeat")
    said = " ".join(m["text"] for m in tg.sent[before:])
    assert "no earlier menu" in said, said
    assert "/cook" in said, "and it names the way forward"
    assert db.scalar("SELECT COUNT(*) FROM menus") == 0, "nothing was written"
