"""
and sleeve, in inches, kept in centimetres, and what they read as for a jacket and trousers."""

from __future__ import annotations

import datetime as dt
import json

import pytest

from tiffin import body, sizing
from tiffin.clock import isoday
from tiffin.food import weekplan
from tiffin.jobs import weekly_check
from tiffin.telegram.measures import parse_tape

from .test_menu import world  # noqa: F401

HIS = ["chest", "waist", "hip", "neck"]
MON = dt.date(2026, 9, 14)


def test_every_site_is_read_in_their_inches_and_kept_in_centimetres() -> None:
    got = parse_tape("chest 42, overarm 47, shoulder 18.5, waist 36, seat 41, sleeve 34", "in")
    assert got == {
        "chest": 106.7,
        "overarm": 119.4,
        "shoulder": 47.0,
        "waist": 91.4,
        "hip": 104.1,
        "sleeve": 86.4,
    }
    # a number that names its unit is read as it says, whatever the default
    assert parse_tape("chest 107cm, sleeve 34in", "in") == {"chest": 107.0, "sleeve": 86.4}
    assert parse_tape('shoulder width 18.5"', "cm") == {"shoulder": 47.0}
    assert parse_tape("over arm 47", "in") == {"overarm": 119.4}
    # hers is unchanged: two bare numbers in cm are waist then hip
    assert parse_tape("84 102") == {"waist": 84.0, "hip": 102.0}
    assert parse_tape("I ran 45 minutes") is None


def test_the_new_sites_are_kept(world) -> None:  # noqa: F811
    pid, db, *_ = world
    for site in (*HIS, "overarm", "shoulder", "sleeve"):
        body.record_tape(db, pid, MON, site, 100.0)
    assert body.latest_tape(db, pid, "overarm") == (MON, 100.0)
    with pytest.raises(ValueError):
        body.record_tape(db, pid, MON, "ankle", 22.0)


def test_a_chest_reads_as_a_jacket_size() -> None:
    assert sizing.jacket(42.0) == {"size": 42}
    assert sizing.jacket(41.5) == {"size": 42}
    assert sizing.jacket(42.5) == {"size": 42}
    assert sizing.jacket(43.0) == {"grey": [42, 44]}
    assert sizing.jacket(44.4) == {"size": 44}
    assert sizing.length(183.0) == "regular" and sizing.length(170.0) == "short"
    assert sizing.length(190.0) == "long"


def test_the_card_says_their_sizes(world) -> None:  # noqa: F811
    pid, db, *_ = world
    weekplan.change(db, pid, source="operator", tape_sites=HIS, tape_unit="in")
    for site, inch in (("chest", 42), ("waist", 36), ("neck", 16)):
        body.record_tape(db, pid, MON, site, inch * body.CM_PER_INCH)
    got = sizing.card(db, pid)
    assert got["unit"] == "in" and [s["site"] for s in got["sites"]] == HIS
    chest = next(s for s in got["sites"] if s["site"] == "chest")
    assert chest["value"] == 42.0 and "armpits" in chest["how"]
    reads = " ".join(got["reads"])
    assert "a 42 jacket" in reads and 'Drop 6.0"' in reads
    assert 'a 16.5" shirt collar' in reads and "Waist to height" in reads
    assert "Body fat about" in reads


def test_the_overarm_still_reads_when_someone_takes_it() -> None:
    assert sizing.collar(15.6) == 16.5 and sizing.collar(16.0) == 16.5
    got = sizing.body_fat("m", waist=36, neck=16, hip=None, height_in=72)
    assert got is not None and 15 < got < 22
    assert sizing.body_fat("m", waist=36, neck=None, hip=None, height_in=72) is None


def test_their_weekly_check_asks_for_their_sites_in_inches(world) -> None:  # noqa: F811
    pid, db, tg, _gw, say, _tap, run_job = world
    weekplan.change(db, pid, source="operator", check_day=0, tape_sites=HIS, tape_unit="in")
    tg.sent.clear()
    assert run_job(weekly_check.JOB, MON) == "ok"
    text = tg.sent[-1]["text"]
    assert "chest, waist, seat, neck, in inches" in text, text
    assert "k:prd" not in json.dumps(tg.sent[-1].get("reply_markup"))  # no cycle for him
    said = say("chest 42, waist 36, seat 41, neck 16")
    assert said.startswith("Recorded: waist 36.0 in, seat 41.0 in, chest 42.0 in, neck 16.0 in")
    assert (
        db.scalar(
            "SELECT COUNT(*) FROM tape_measures WHERE person_id = ? AND day = ?", (pid, isoday(MON))
        )
        == 4
    )
