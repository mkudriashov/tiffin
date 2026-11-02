"""What a link changes about weekends, and what unlinking leaves behind.

Linking is not consent and consent is not a link: shared weekends need both. Unlinking is
immediate and one-sided; weekends already built stay as two ordinary menus, the open joint
shopping list is closed and each of them is given their own copy of its lines, and nothing the
other person has already seen is taken back (it cannot be).
"""

from __future__ import annotations

import contextlib
import datetime as dt

from tiffin import links
from tiffin.db import Db
from tiffin.food import menu, plates, shared, shopping, swap, weekend
from tiffin.food.shopping import list_row, newest_readable

from .linking import link_pair
from .test_food_shopping import seed_ingredient, seed_menu, seed_recipe
from .test_review_0926_weekend import SAT, SUN, WEEKEND, _agree, _build, pair  # noqa: F401


def unlink(db: Db, who: int) -> links.Unlinked:
    return links.unlink(db, who, "telegram")


# -- the gate: a link AND two yeses ------------------------------------------------------------


def test_consent_without_a_link_shares_nothing(pair) -> None:  # noqa: F811
    db, (him, partner), _clock = pair
    unlink(db, him.id)
    _agree(db, him.id)
    _agree(db, partner.id)
    assert not shared.linked_and_both_consented(db)
    assert not shared.active(db, WEEKEND)
    assert not shared.possible(db, WEEKEND)
    assert shared.people(db) == []


def test_a_link_without_two_yeses_shares_nothing(pair) -> None:  # noqa: F811
    db, (him, partner), _clock = pair
    assert links.is_linked(db)
    assert not shared.possible(db, WEEKEND)
    _agree(db, him.id)
    assert not shared.possible(db, WEEKEND), "one yes is not two"
    _agree(db, partner.id)
    assert shared.possible(db, WEEKEND)
    unlink(db, partner.id)
    assert not shared.possible(db, WEEKEND), "unlinking switches it off at once"


def test_a_lone_user_is_told_the_weekend_is_their_own_and_gets_no_switch(pair) -> None:  # noqa: F811
    db, (him, _partner), clock = pair
    unlink(db, him.id)
    page = weekend.page(db, him.id, clock.today_local())
    assert page["switch"] is None
    assert page["note"] == "Weekend menus are your own."
    # nor can the switch be flipped by a hand-made request
    try:
        weekend.flip(db, "together", person_id=him.id, today=clock.today_local())
    except Exception as e:
        assert "linked" in str(e)
    else:
        raise AssertionError("an unlinked person flipped the weekend to together")


def test_linked_but_not_agreed_says_what_is_missing(pair) -> None:  # noqa: F811
    db, (him, _partner), clock = pair
    page = weekend.page(db, him.id, clock.today_local())
    assert page["switch"] is None
    assert "both said yes" in str(page["note"])


# -- a weekend already built, then unlinked ----------------------------------------------------


def built_together(pair):  # noqa: F811
    db, persons, clock = pair
    for p in persons:
        _agree(db, p.id)
    built = _build(db, persons, clock)
    return db, persons, clock, built


def test_a_built_weekend_is_marked_with_its_link(pair) -> None:  # noqa: F811
    db, _persons, _clock, built = built_together(pair)
    assert db.scalar("SELECT link_id FROM shared_menus WHERE id = ?", (built.shared_id,)) == (
        links.active_link_id(db)
    )


def test_after_unlinking_the_built_weekend_is_two_ordinary_menus(pair) -> None:  # noqa: F811
    db, (him, partner), _clock, built = built_together(pair)
    menu_id = built.menu_ids[him.id]
    assert shared.partner_plate(db, him.id, menu_id, SAT, "lunch") is not None
    assert plates.cooked_with(db, him.id, SAT) is not None
    unlink(db, partner.id)
    for viewer, mine in ((him, built.menu_ids[him.id]), (partner, built.menu_ids[partner.id])):
        assert shared.pair_of(db, mine) is None
        assert shared.partner_plate(db, viewer.id, mine, SAT, "lunch") is None
        assert plates.cooked_with(db, viewer.id, SAT) is None
    assert weekend.in_force(db, WEEKEND) == weekend.APART
    # both menus are still there and still in force: nothing was withdrawn from either
    for p in (him, partner):
        assert menu.accepted_menu_id(db, p.id, SAT) == built.menu_ids[p.id]


def test_a_new_link_does_not_reopen_the_old_weekend(pair) -> None:  # noqa: F811
    db, (him, partner), _clock, built = built_together(pair)
    unlink(db, him.id)
    link_pair(db, him.id, partner.id)  # a second, different link
    assert links.is_linked(db)
    assert shared.pair_of(db, built.menu_ids[him.id]) is None


def test_unlinking_during_a_build_leaves_ordinary_menus(pair) -> None:  # noqa: F811
    db, (him, partner), clock = pair
    for p in (him, partner):
        _agree(db, p.id)
    profiles = {
        p.id: db.one("SELECT * FROM profiles WHERE person_id = ?", (p.id,)) for p in (him, partner)
    }
    from .test_review_0926_weekend import HIS, PARTNERS, _weekend_payload

    def ask(_prompt: str, _hint: str):
        # the other person unlinks while the model is thinking
        unlink(db, partner.id)
        return _weekend_payload(), "fake"

    built = shared.build(
        db,
        WEEKEND,
        [him, partner],
        profiles,
        {him.id: HIS, partner.id: PARTNERS},
        ask,
        prompt_for=lambda hint: "prompt" + hint,
        clock=clock,
    )
    assert not links.is_linked(db)
    assert shared.pair_of(db, built.menu_ids[him.id]) is None
    assert plates.cooked_with(db, him.id, SAT) is None
    assert shared.partner_plate(db, him.id, built.menu_ids[him.id], SAT, "dinner") is None
    assert not shared.possible(db, WEEKEND)


def test_a_weekend_stored_with_no_link_does_not_reopen_on_a_later_link(pair) -> None:  # noqa: F811
    db, (him, partner), clock = pair
    for p in (him, partner):
        _agree(db, p.id)
    profiles = {
        p.id: db.one("SELECT * FROM profiles WHERE person_id = ?", (p.id,)) for p in (him, partner)
    }
    from .test_review_0926_weekend import HIS, PARTNERS, _weekend_payload

    def ask(_prompt: str, _hint: str):
        unlink(db, partner.id)
        return _weekend_payload(), "fake"

    built = shared.build(
        db,
        WEEKEND,
        [him, partner],
        profiles,
        {him.id: HIS, partner.id: PARTNERS},
        ask,
        prompt_for=lambda hint: "prompt" + hint,
        clock=clock,
    )
    assert built.shared_id is None
    assert db.one("SELECT COUNT(*) AS n FROM shared_menus WHERE link_id IS NULL")["n"] == 0
    link_pair(db, him.id, partner.id)  # they link again
    assert links.is_linked(db)
    for p in (him, partner):
        assert shared.pair_of(db, built.menu_ids[p.id]) is None
        assert shared.partner_plate(db, p.id, built.menu_ids[p.id], SAT, "dinner") is None


def test_swapping_a_dish_on_an_unlinked_weekend_does_not_re_pair_it(pair) -> None:  # noqa: F811
    db, (him, partner), _clock, _built = built_together(pair)
    unlink(db, him.id)
    assert swap.cooked_with(db, partner.id, SAT, "lunch") is None


def test_the_next_weekend_builds_apart_once_unlinked(pair) -> None:  # noqa: F811
    db, (him, _partner), _clock, _built = built_together(pair)
    unlink(db, him.id)
    assert not shared.active(db, shared.weekend_of(dt.date(2026, 9, 26)))
    assert not shared.possible(db, WEEKEND)
    assert shared.people(db) == []


# -- the joint shopping list -------------------------------------------------------------------


def joint_world(db: Db, two_persons):
    a, b = two_persons["owner"], two_persons["partner"]
    eggs = seed_ingredient(db, "eggs", "Eggs, whole", "dairy", 360, "6 eggs")
    rice = seed_ingredient(db, "rice", "Rice", "grains", 1000, "1 kg bag")
    dish = seed_recipe(db, "Eggs and rice", 1, {eggs: 100, rice: 120})
    sat = dt.date(2026, 10, 3)
    m1 = seed_menu(db, a, sat, 2, {"lunch": (dish, 1.0)})
    m2 = seed_menu(db, b, sat, 2, {"lunch": (dish, 1.0)})
    link_pair(db, a, b)
    items = shopping.joint_weekend(db, [m1, m2])
    assert items and items[0].list_id is not None
    return a, b, int(items[0].list_id), (m1, m2), items


def test_a_joint_list_is_stamped_with_the_link_and_both_read_it(
    fixture_db: Db, two_persons
) -> None:
    a, b, list_id, _menus, _items = joint_world(fixture_db, two_persons)
    assert fixture_db.scalar("SELECT link_id FROM shopping_lists WHERE id = ?", (list_id,)) == (
        links.active_link_id(fixture_db)
    )
    assert list_row(fixture_db, list_id, a) is not None
    assert list_row(fixture_db, list_id, b) is not None


def test_unlinking_closes_the_joint_list_and_gives_each_their_own_copy(
    fixture_db: Db, two_persons
) -> None:
    db = fixture_db
    a, b, list_id, (m1, m2), _items = joint_world(db, two_persons)
    # something already in the trolley
    first_id = int(
        db.scalar("SELECT id FROM shopping_items WHERE list_id = ? ORDER BY id", (list_id,))
    )
    db.insert("shopping_ticks", item_id=first_id, person_id=a, ticked=1)
    before = {
        str(r["name"]): (r["grams_needed"], r["packs"])
        for r in db.all(
            "SELECT i.name, si.grams_needed, si.packs FROM shopping_items si "
            "JOIN ingredients i ON i.id = si.ingredient_id WHERE si.list_id = ?",
            (list_id,),
        )
    }
    done = links.unlink(db, a, "web", today=dt.date(2026, 10, 2))
    assert (done.closed_lists, done.copied_lists) == (1, 2)

    # the joint list is nobody's now, by id and by "newest"
    assert list_row(db, list_id, a) is None and list_row(db, list_id, b) is None
    assert not shopping.item_readable(db, first_id, a)
    assert not shopping.item_readable(db, first_id, b)

    mine, theirs = newest_readable(db, a), newest_readable(db, b)
    assert mine is not None and theirs is not None and mine["id"] != theirs["id"]
    assert int(mine["person_id"]) == a and int(theirs["person_id"]) == b
    assert mine["menu_id"] == m1 and theirs["menu_id"] == m2
    # the same lines, for each of them
    for row in (mine, theirs):
        got = {
            str(r["name"]): (r["grams_needed"], r["packs"])
            for r in db.all(
                "SELECT i.name, si.grams_needed, si.packs FROM shopping_items si "
                "JOIN ingredients i ON i.id = si.ingredient_id WHERE si.list_id = ?",
                (row["id"],),
            )
        }
        assert got == before
    # and what was ticked is still ticked, on both copies
    for row, pid in ((mine, a), (theirs, b)):
        ticked = shopping.ticks_for(db, int(row["id"]), pid, joint=False)
        assert len(ticked) == 1
    # neither can open the other's copy by id
    assert list_row(db, int(mine["id"]), b) is None
    assert list_row(db, int(theirs["id"]), a) is None


def test_a_closed_list_stays_closed_when_they_link_again(fixture_db: Db, two_persons) -> None:
    db = fixture_db
    a, b, list_id, _menus, _items = joint_world(db, two_persons)
    links.unlink(db, a, "web", today=dt.date(2026, 10, 2))
    link_pair(db, a, b)
    assert list_row(db, list_id, a) is None and list_row(db, list_id, b) is None


def test_a_finished_weekends_list_is_closed_without_copies(fixture_db: Db, two_persons) -> None:
    db = fixture_db
    a, _b, list_id, _menus, _items = joint_world(db, two_persons)
    done = links.unlink(db, a, "web", today=dt.date(2026, 10, 9))
    assert (done.closed_lists, done.copied_lists) == (1, 0)
    assert db.scalar("SELECT COUNT(*) FROM shopping_lists WHERE kind = 'segment'") == 0
    assert list_row(db, list_id, a) is None


def test_unlinking_twice_closes_nothing_again(fixture_db: Db, two_persons) -> None:
    db = fixture_db
    a, _b, _list_id, _menus, _items = joint_world(db, two_persons)
    links.unlink(db, a, "web", today=dt.date(2026, 10, 2))
    n = db.scalar("SELECT COUNT(*) FROM shopping_list_closures")
    with contextlib.suppress(links.LinkError):
        links.unlink(db, a, "web", today=dt.date(2026, 10, 2))
    assert db.scalar("SELECT COUNT(*) FROM shopping_list_closures") == n == 1


def test_erasing_a_linked_person_leaves_the_other_their_copy_of_the_open_joint_list(
    fixture_db: Db, two_persons, tmp_path
) -> None:
    import json

    from tiffin import admin

    db = fixture_db
    _a, b, list_id, (_m1, m2), _items = joint_world(db, two_persons)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"slug": "owner"}), encoding="utf-8")
    admin.erase_person(
        db,
        "owner",
        manifest_path=manifest,
        clock=db.clock,  # type: ignore[arg-type]
    )
    assert not links.is_linked(db)
    assert list_row(db, list_id, b) is None, "the joint list is closed"
    theirs = newest_readable(db, b)
    assert theirs is not None and int(theirs["person_id"]) == b and theirs["menu_id"] == m2
    assert db.scalar("SELECT COUNT(*) FROM shopping_items WHERE list_id = ?", (theirs["id"],))
