"""

where we literally flick a switch, whether it's shared menu or whether it's not shared, and we
keep both menus below that mode?"
"""

from __future__ import annotations

import datetime as dt

import pytest

from tiffin.db import Db
from tiffin.errors import NothingToBuild
from tiffin.food import menu, plates, shared, weekend

from .test_dash_shopping_tab import (  # noqa: F401
    HEADER,
    MONDAY,
    OWNER_LOGIN,
    PARTNER_LOGIN,
    at,
    open_box,
)
from .test_review_0926_weekend import HIS, PARTNERS, SAT, SUN, _agree, _build, pair  # noqa: F401


def _own(db: Db, pid: int) -> int:
    """A person's own weekend, built and kept but not in force: status proposed, no decision."""
    rid = int(db.scalar("SELECT id FROM recipes ORDER BY id LIMIT 1"))
    mid = db.insert(
        "menus",
        person_id=pid,
        segment_start=SAT.isoformat(),
        segment_end=SUN.isoformat(),
        status="proposed",
    )
    for day in (SAT, SUN):
        did = db.insert(
            "menu_days", menu_id=mid, day=day.isoformat(), kcal=2000, protein_g=180, fat_g=70,
            carb_g=200, fibre_g=30,
        )  # fmt: skip
        db.insert(
            "meal_slots", menu_day_id=did, slot="lunch", recipe_id=rid, portion_multiplier=1.0,
            portion_g=300, kcal=500, protein_g=45, fat_g=18, carb_g=50, fibre_g=8,
        )  # fmt: skip
    return mid


@pytest.fixture
def both(pair):  # noqa: F811
    """The weekend built both ways: cooked together (in force) and each their own (kept)."""
    db, (him, partner), clock = pair
    for p, m in ((him, HIS), (partner, PARTNERS)):
        db.insert(
            "targets", person_id=p.id, week_start="2026-09-14", kcal=m.kcal,
            protein_g=m.protein_g, fat_g=m.fat_g, carb_g=m.carb_g, fibre_g=m.fibre_g,
            source="formula",
        )  # fmt: skip
    _agree(db, him.id)
    _agree(db, partner.id)
    built = _build(db, [him, partner], clock)
    own = {p.id: _own(db, p.id) for p in (him, partner)}
    clock.advance(minutes=5)  # the flip comes after the build, never in the same second
    return db, him, partner, clock, built, own


def test_both_versions_are_kept_and_the_switch_says_which_is_in_force(both) -> None:
    db, him, partner, clock, built, own = both
    today = clock.today_local()
    v = weekend.versions(db, menu.Segment(SAT, SUN))
    assert v.together == built.menu_ids and v.apart == own
    got = weekend.flip(db, "apart", person_id=him.id, today=today)
    assert got.missing == () and "each your own" in got.said
    for p in (him, partner):
        assert menu.accepted_menu_id(db, p.id, SAT) == own[p.id]
        assert menu.accepted_menu_id(db, p.id, SUN) == own[p.id]
    assert not shared.active(db, menu.Segment(SAT, SUN)), "apart: the shared job leaves it be"
    clock.advance(minutes=1)
    weekend.flip(db, "together", person_id=partner.id, today=today)
    for p in (him, partner):
        assert menu.accepted_menu_id(db, p.id, SAT) == built.menu_ids[p.id]
    assert shared.mode(db) == "together" and shared.active(db, menu.Segment(SAT, SUN))


def test_flipping_to_what_is_in_force_decides_nothing(both) -> None:
    """A second tap would make the weekend's list stale and rebuild it under someone shopping."""
    db, him, _partner, clock, _built, _own = both
    before = db.scalar("SELECT COUNT(*) FROM menu_decisions")
    weekend.flip(db, "together", person_id=him.id, today=clock.today_local())
    assert db.scalar("SELECT COUNT(*) FROM menu_decisions") == before


def test_a_resized_day_comes_back_with_its_version(both, config) -> None:
    db, him, _partner, clock, _built, _own = both
    today = clock.today_local()
    got = plates.resize(db, him.id, SUN, {"dinner": "skip"}, config=config, clock=clock)
    clock.advance(minutes=1)
    weekend.flip(db, "apart", person_id=him.id, today=today)
    clock.advance(minutes=1)
    weekend.flip(db, "together", person_id=him.id, today=today)
    assert menu.accepted_menu_id(db, him.id, SUN) == got.menu_id, "his skipped dinner stands"


def test_a_weekend_with_a_day_recorded_keeps_its_version(both) -> None:
    db, him, partner, clock, built, _own = both
    db.insert(
        "day_confirmations", person_id=partner.id, day=SAT.isoformat(), status="confirmed",
        basis="menu",
    )  # fmt: skip
    got = weekend.flip(db, "apart", person_id=him.id, today=clock.today_local())
    assert "Sat 26 Sep" in got.said and "built on Thursday" in got.said, got.said
    assert menu.accepted_menu_id(db, him.id, SAT) == built.menu_ids[him.id]
    assert shared.mode(db) == "apart", "it is about the next weekend now"


def test_on_the_sunday_the_switch_waits_for_next_weekend(both) -> None:
    db, him, _partner, clock, built, _own = both
    clock.set_local(dt.datetime(2026, 9, 20, 9, 0))
    got = weekend.flip(db, "apart", person_id=him.id, today=clock.today_local())
    assert "Sat 26 Sep" in got.said, got.said
    assert menu.accepted_menu_id(db, him.id, SUN) == built.menu_ids[him.id]
    page = weekend.page(db, him.id, clock.today_local())
    assert page["start"] == "2026-09-26", "on its Sunday the page is about the next weekend"


def test_together_needs_both_to_share(both) -> None:
    db, him, partner, clock, _built, _own = both
    db.insert(
        "consents",
        person_id=partner.id,
        kind="shared_meals",
        granted=0,
        wording_version="shared_v1",
    )
    with pytest.raises(NothingToBuild, match="share weekend menus"):
        weekend.flip(db, "together", person_id=him.id, today=clock.today_local())


def test_a_version_not_built_yet_is_named_for_whoever_it_is_missing(pair) -> None:  # noqa: F811
    db, (him, partner), clock = pair
    _agree(db, him.id)
    _agree(db, partner.id)
    got = weekend.flip(db, "apart", person_id=him.id, today=clock.today_local())
    assert set(got.missing) == {him.id, partner.id} and "being built" in got.said


def test_the_page_has_the_switch_and_each_persons_own_taste(open_box) -> None:  # noqa: F811
    box = open_box(at(MONDAY, 9, 0))  # early in the week: this weekend can still be switched
    csrf = box.get("api/me").json()["csrf"]
    headers = {HEADER: OWNER_LOGIN, "X-CSRF-Token": csrf}

    def post(path: str, body: dict):
        return box.client.post(path, json=body, headers=headers)

    first = box.get("api/menu").json()["weekend"]
    assert first["switch"] is None, "no switch until both of them share weekend menus"
    assert first["taste"] == "both"
    said = post("api/weekend/taste", {"taste": "sweet"})
    assert said.status_code == 200 and "sweet" in said.json()["said"]
    assert box.get("api/menu").json()["weekend"]["taste"] == "sweet"
    assert box.get("api/menu", login=PARTNER_LOGIN).json()["weekend"]["taste"] == "both", (
        "his, not hers"
    )
    assert post("api/weekend/taste", {"taste": "spicy"}).status_code == 400
    refused = post("api/weekend/mode", {"mode": "together"})
    assert refused.status_code == 409 and "share weekend menus" in refused.json()["detail"]
    for pid in (box.owner_id, box.partner_id):
        box.db.insert(
            "consents", person_id=pid, kind="anthropic_processing", granted=1,
            wording_version="v2",
        )  # fmt: skip
        box.db.insert(
            "consents", person_id=pid, kind="shared_meals", granted=1,
            wording_version="shared_v1",
        )  # fmt: skip
    queued: list[int] = []
    box.client.app.state.dispatch = lambda pid, _fn: queued.append(pid)  # type: ignore[attr-defined]
    early = post("api/weekend/mode", {"mode": "apart"})
    assert early.status_code == 200, early.text
    assert early.json()["missing"] == [] and "built on Thursday" in early.json()["said"]
    assert queued == [], "nothing is built early: Thursday's builds know that week's leftovers"
    box.clock.set_local(dt.datetime(2026, 9, 17, 9, 0))  # the Thursday before the weekend
    post("api/weekend/mode", {"mode": "together"})
    queued.clear()
    apart = post("api/weekend/mode", {"mode": "apart"})
    assert apart.status_code == 200, apart.text
    assert set(apart.json()["missing"]) == set(queued) == {box.owner_id, box.partner_id}, (
        "each own version not built yet is queued on its own person's worker"
    )
    seen = box.get("api/menu", login=PARTNER_LOGIN).json()["weekend"]["switch"]
    assert seen["mode"] == "apart" and seen["by"] == "Alex set it last."
    assert post("api/weekend/mode", {"mode": "sideways"}).status_code == 400
    who = post("api/weekend/mode", {"mode": "together", "person_id": box.partner_id})
    assert who.status_code in (400, 403), "a person is never read off the body"


def test_tastes_reach_the_prompt_named_for_nobody() -> None:
    assert "lean savoury" in weekend.taste_line(["savoury"])
    assert "lean sweet" in weekend.taste_line(["sweet", "both"])
    mixed = weekend.taste_line(["sweet", "savoury"])
    assert "one of them prefers sweet" in mixed and "Sam" not in mixed and "Alex" not in mixed
    assert "mix of sweet and savoury" in weekend.taste_line(["both", "both"])


def test_a_taste_is_the_persons_own_and_the_latest_one_holds(pair) -> None:  # noqa: F811
    db, (him, partner), _clock = pair
    assert weekend.taste(db, partner.id) == "both"
    weekend.set_taste(db, partner.id, "savoury", set_by="operator")
    weekend.set_taste(db, him.id, "sweet")
    assert (weekend.taste(db, him.id), weekend.taste(db, partner.id)) == ("sweet", "savoury")
    with pytest.raises(ValueError):
        weekend.set_taste(db, him.id, "spicy")
