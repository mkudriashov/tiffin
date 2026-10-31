"""Menus that use up what was bought.

for her to have only one recipe that uses chicken on Tuesday, and then Wednesday another recipe
that uses beef or pork. Ideally, if she buys 1 kilogram of chicken, we would want the recipes to
reflect using the same pack until it is used."

The world is the swap tests' (tests/test_swap.py): the partner's Monday-to-Friday batch of porridge,
a lentil dal and chicken with rice, next week, every pack 500 g; the frozen clock on Monday 7 Sep.
"""

from __future__ import annotations

import datetime as dt
import json

from tiffin.clock import isoday
from tiffin.food import menu, packs, shopping

from .test_swap import (  # noqa: F401
    FRI,
    MON,
    PARTNER_LOGIN,
    _headers,
    _menu,
    _profile,
    page,
    week,
)
from .web_source import app_source

DAILY = json.dumps([[0], [1], [2], [3], [4]])
TUE, WED, THU = (MON + dt.timedelta(days=n) for n in (1, 2, 3))


def _daily(db, pid, r) -> None:
    """Her week cooked each day instead: a menu a day, chicken on Monday, salmon on Tuesday and
    Wednesday, the dal on Thursday and Friday."""
    _profile(db, pid, segment_days=DAILY, effective_from="2026-08-02")
    _menu(db, pid, MON, 1, {"breakfast": r["porridge"], "dinner": r["chicken_rice"]})
    _menu(db, pid, TUE, 1, {"breakfast": r["porridge"], "dinner": r["salmon"]})
    _menu(db, pid, WED, 1, {"breakfast": r["porridge"], "dinner": r["salmon"]})
    _menu(db, pid, THU, 2, {"breakfast": r["porridge"], "dinner": r["dal"]})


# ---------------------------------------------------------------- the window
def test_a_batch_cooks_window_is_its_segment_and_a_daily_cooks_is_the_weekdays(week) -> None:  # noqa: F811
    db, pid, _base, _r, _clock = week
    assert packs.window_of(db, pid, WED) == menu.Segment(MON, FRI).days
    assert packs.window_of(db, pid, MON + dt.timedelta(days=6)) == [
        MON + dt.timedelta(days=5),
        MON + dt.timedelta(days=6),
    ]
    # cooking Monday-to-Wednesday and Thursday-to-Friday: two shops, two windows
    _profile(db, pid, segment_days=json.dumps([[0, 1, 2], [3, 4]]), effective_from="2026-08-02")
    assert packs.window_of(db, pid, TUE) == menu.Segment(MON, WED).days
    assert packs.window_of(db, pid, FRI) == menu.Segment(THU, FRI).days
    # cooked each day: the shop is for the week's weekdays, as a range list buys
    _profile(db, pid, segment_days=DAILY, effective_from="2026-08-03")
    assert packs.window_of(db, pid, WED) == menu.Segment(MON, FRI).days


# ---------------------------------------------------------------- the packs and their days
def test_each_pack_is_whole_and_says_its_days_and_what_is_left(week) -> None:  # noqa: F811
    db, pid, _base, _r, _clock = week
    got = {p.code: p for p in packs.window_packs(db, pid, menu.Segment(MON, FRI).days)}
    chicken = got["chicken"]
    # 180 g a day for five days in 500 g packs: two packs, 100 g over
    assert (round(chicken.used_g), chicken.packs, round(chicken.left_g)) == (900, 2, 100)
    assert chicken.main and chicken.days == menu.Segment(MON, FRI).days
    lines = packs.lines(db, pid, WED)
    assert lines[0] == "Chicken breast 1 kg: Mon, Tue, Wed, Thu, Fri; 100 g left"
    # the fresh foods cooked on more than one day follow; the dry ones, which keep, do not
    assert any(x.startswith("Broccoli 1.5 kg: ") for x in lines)
    assert not any(x.startswith("Red lentils") for x in lines)


def test_a_daily_build_is_told_the_packs_the_other_days_opened(week) -> None:  # noqa: F811
    db, pid, _base, r, _clock = week
    _daily(db, pid, r)
    opened = {p.code: round(p.left_g) for p in packs.open_for(db, pid, [WED]) if p.main}
    # Monday's 180 g of chicken and Tuesday's 140 g of salmon each opened a 500 g pack: Wednesday
    # is told both, with what is left, mains first
    assert opened == {"chicken": 320, "salmon": 360}
    assert [p.code for p in packs.open_for(db, pid, [WED])][:2] == ["chicken", "salmon"]
    # a food since refused is never offered back, though its pack is still open
    left = packs.open_for(db, pid, [WED], allowed={"salmon", "rice"})
    assert [p.code for p in left] == ["salmon"]
    # the days being built are the ones still to choose: rebuilding Monday is not told Monday
    assert "chicken" not in {p.code for p in packs.open_for(db, pid, [MON])}


def test_the_index_line_names_a_pack_by_its_number_in_the_list(week) -> None:  # noqa: F811
    db, pid, _base, r, _clock = week
    _daily(db, pid, r)
    opened = packs.open_for(db, pid, [WED])
    said = packs.prompt_lines(opened, {"chicken": 7, "salmon": 3})
    assert said[0] == "7. Chicken breast, raw: 320 g left of a 500 g pack (opened for Mon)"
    assert said[1] == "3. Salmon fillets, raw: 360 g left of a 500 g pack (opened for Tue)"


# ---------------------------------------------------------------- said on the page and in the bot
def test_menu_and_shopping_show_each_pack_and_the_days_it_is_for(page) -> None:  # noqa: F811
    client, db, partners, _his, _r = page
    base = db.scalar(
        "SELECT id FROM menus WHERE person_id = ? AND segment_start = ?", (partners, isoday(MON))
    )
    shopping.build(db, int(base))
    h = {"Tailscale-User-Login": PARTNER_LOGIN}
    on_menu = client.get(f"/api/menu?segment={isoday(MON)}", headers=h).json()["packs"]
    assert (on_menu[0]["days"], on_menu[0]["span"]) == ("Mon, Tue, Wed, Thu, Fri", "Mon to Fri")
    assert on_menu[0]["lines"][0] == "Chicken breast 1 kg: Mon, Tue, Wed, Thu, Fri; 100 g left"
    listed = client.get("/api/shopping", headers=h).json()
    assert listed["packs"]["lines"][0].startswith("Chicken breast 1 kg: Mon")


def test_the_page_draws_the_pack_lines_on_menu_and_shopping() -> None:

    js = app_source()
    assert "function packsPanel(window, question = null)" in js
    assert "for (const [n, window] of (data.packs || []).entries()) {" in js
    assert "body.append(packsPanel(data.packs))" in js


# ---------------------------------------------------------------- the list buys the window's packs
def test_a_daily_cooks_list_buys_one_pack_for_the_days_that_share_it(week) -> None:  # noqa: F811
    """Chicken on Monday and again on Wednesday, cooked each day: the list for the week buys the
    whole packs the two days need between them - one 500 g pack, not one a day - and what it
    says is left is what the pack line says."""
    db, pid, _base, r, clock = week
    _profile(db, pid, segment_days=DAILY, effective_from="2026-08-02")
    for day, dinner in ((MON, "chicken_rice"), (TUE, "salmon"), (WED, "chicken_rice")):
        _menu(db, pid, day, 1, {"breakfast": r["porridge"], "dinner": r[dinner]})
    _menu(db, pid, THU, 2, {"breakfast": r["porridge"], "dinner": r["dal"]})
    row = shopping.range_list(db, pid, MON, clock=clock)
    assert row is not None
    bought = {
        str(i["code"]): (int(i["packs"]), float(i["leftover_g"]))
        for i in db.all(
            "SELECT ing.code, si.packs, si.leftover_g FROM shopping_items si "
            "JOIN ingredients ing ON ing.id = si.ingredient_id WHERE si.list_id = ?",
            (int(row["id"]),),
        )
    }
    planned = {p.code: p for p in packs.window_packs(db, pid, menu.Segment(MON, FRI).days)}
    # 180 g on Monday and 180 g on Wednesday: one pack, 140 g over - whole packs sized to the
    # planned use, and the remainder the page and the bot say
    assert bought["chicken"] == (1, 140.0)
    for code, (n, left) in bought.items():
        assert (n, round(left)) == (planned[code].packs, round(planned[code].left_g)), code
    line = next(x for x in packs.lines(db, pid, MON) if x.startswith("Chicken breast"))
    assert line == "Chicken breast 500 g: Mon, Wed; 140 g left"
