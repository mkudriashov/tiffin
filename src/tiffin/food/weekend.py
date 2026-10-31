"""Select shared or individual weekend menus without rebuilding either version.

Each switch writes a new decision. Both people must consent before a shared menu can be
selected. A recorded day cannot be replaced; a late switch applies to the following
weekend."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from ..clock import isoday
from ..db import Db
from ..errors import NothingToBuild
from . import menu, shared

TOGETHER, APART = shared.MODES
TASTES = ("sweet", "savoury", "both")
#: what the page says back when a person sets their taste
TASTE_SAID = {
    "sweet": "Your weekend treats lean sweet from the next weekend built.",
    "savoury": "Your weekend treats lean savoury from the next weekend built.",
    "both": "Your weekend treats are a mix of sweet and savoury from the next weekend built.",
}


# ---------------------------------------------------------------- taste
def taste(db: Db, person_id: int) -> str:
    """What this person's weekend treats lean to: both until they say otherwise."""
    row = db.one(
        "SELECT taste FROM treat_tastes WHERE person_id = ? ORDER BY id DESC LIMIT 1",
        (person_id,),
    )
    return "both" if row is None else str(row["taste"])


def set_taste(db: Db, person_id: int, value: str, *, set_by: str = "person") -> None:
    if value not in TASTES:
        raise ValueError(f"a taste is one of {', '.join(TASTES)}, not {value}")
    db.insert("treat_tastes", person_id=person_id, taste=value, set_by=set_by)


_LEAN = {
    "sweet": (
        "the treats lean sweet: sweet dishes are welcome at breakfast, the snack and pre-bed; "
        "lunch and dinner stay savoury."
    ),
    "savoury": (
        "the treats lean savoury: make breakfast, the snack and pre-bed savoury (ideas below), "
        "with at most one sweet dish in the whole weekend."
    ),
    "both": "a mix of sweet and savoury treats across the weekend.",
}


def taste_line(tastes: list[str]) -> str:
    """What the weekend's treats lean to, for the prompt - named for nobody, so a shared
    weekend's prompt never says whose taste is whose. One who likes both follows the other."""
    leaning = {t for t in tastes if t in TASTES} - {"both"}
    if len(leaning) == 1:
        return _LEAN[leaning.pop()]
    if not leaning:
        return _LEAN["both"]
    return (
        "their tastes differ: one of them prefers sweet treats, the other savoury. Give each day "
        "at least one sweet treat and one savoury one, and make the savoury ones as good as the "
        "sweet - never an afterthought."
    )


# ---------------------------------------------------------------- the two versions
@dataclass(frozen=True)
class Versions:
    """Each version of one weekend built so far, as person id -> that person's menu for it."""

    together: dict[int, int] = field(default_factory=dict)
    apart: dict[int, int] = field(default_factory=dict)

    def of(self, which: str) -> dict[int, int]:
        return self.together if which == TOGETHER else self.apart


def versions(db: Db, weekend: menu.Segment) -> Versions:
    """The versions of this weekend built so far.

    Together is the newest pair built for these days; apart is each person's newest menu of their
    own for them - a rebuild supersedes an earlier one, and a refused one is no version. A day's
    plates resized by hand (`menu_emphasis`) belong to neither: they are a change made on top of
    a version, and they come back with it (`flip`).
    """
    lo, hi = isoday(weekend.start), isoday(weekend.end)
    pair = db.one(
        "SELECT id FROM shared_menus WHERE segment_start BETWEEN ? AND ? ORDER BY id DESC LIMIT 1",
        (lo, hi),
    )
    together: dict[int, int] = {}
    if pair is not None:
        for r in db.all(
            "SELECT sm.person_id, sm.menu_id FROM shared_menu_members sm "
            "WHERE sm.shared_menu_id = ? "
            "AND NOT EXISTS (SELECT 1 FROM menu_emphasis e WHERE e.menu_id = sm.menu_id) "
            "ORDER BY sm.id",
            (int(pair["id"]),),
        ):
            together[int(r["person_id"])] = int(r["menu_id"])
    apart = {
        int(r["person_id"]): int(r["menu_id"])
        for r in db.all(
            "SELECT m.person_id, MAX(m.id) AS menu_id FROM menus m "
            "WHERE m.segment_start BETWEEN ? AND ? AND m.segment_end <= ? "
            "AND NOT EXISTS (SELECT 1 FROM shared_menu_members sm WHERE sm.menu_id = m.id) "
            "AND NOT EXISTS (SELECT 1 FROM menu_emphasis e WHERE e.menu_id = m.id) "
            "AND COALESCE((SELECT d.decision FROM menu_decisions d WHERE d.menu_id = m.id "
            "ORDER BY d.id DESC LIMIT 1), 'none') NOT IN ('rejected', 'lapsed') "
            "GROUP BY m.person_id",
            (lo, hi, hi),
        )
    }
    return Versions(together, apart)


def in_force(db: Db, weekend: menu.Segment) -> str | None:
    """Which version the weekend's days are served from now - together when every plate in force
    belongs to a pair (a resized day counts as the version it was made on), apart when none does,
    None when nothing is in force at all."""
    rows = db.all(
        "SELECT v.menu_id, COALESCE((SELECT e.base_menu_id FROM menu_emphasis e "
        "WHERE e.menu_id = v.menu_id ORDER BY e.id DESC LIMIT 1), v.menu_id) AS base "
        "FROM v_menu_days_current v WHERE v.day BETWEEN ? AND ?",
        (isoday(weekend.start), isoday(weekend.end)),
    )
    if not rows:
        return None
    paired = [shared.pair_of(db, int(r["base"])) is not None for r in rows]
    return TOGETHER if all(paired) else APART if not any(paired) else None


def too_late(db: Db, weekend: menu.Segment, today: dt.date) -> str | None:
    """Why this weekend can no longer be switched, or None when it still can."""
    if today >= weekend.end:
        return "This weekend is half eaten."
    for pid in shared.people(db):
        if any(menu.day_confirmed(db, pid, d) for d in weekend.days):
            return "A day of this weekend is already recorded."
    return None


def target(db: Db, today: dt.date) -> menu.Segment:
    """The weekend the switch is about: this one while it can still change, else the next. On a
    Sunday the page offered the weekend already half eaten, which no flip could reach."""
    weekend = shared.weekend_of(today)
    if too_late(db, weekend, today) is not None:
        weekend = shared.weekend_of(weekend.end + dt.timedelta(days=1))
    return weekend


#: how many days before its Saturday a weekend's versions are built (its Thursday)
BUILT_DAYS_AHEAD = 2


@dataclass(frozen=True)
class Flipped:
    mode: str
    #: whose menus for the chosen version are not built yet: the caller has them built
    missing: tuple[int, ...]
    said: str


def flip(
    db: Db, to: str, *, person_id: int | None, today: dt.date, provider: str = "claude-cli"
) -> Flipped:
    """Set the switch and, while this weekend can still change, put that version in force."""
    if to not in shared.MODES:
        raise ValueError(f"the weekend is {TOGETHER} or {APART}, not {to}")
    if to == TOGETHER and not shared.everyone_agreed(db):
        raise NothingToBuild(
            "Cooking together needs you both to share weekend menus first: send /consent to the "
            "bot and tap Share weekend menus."
        )
    weekend = target(db, today)
    which = "cooked together" if to == TOGETHER else "each your own"
    named = f"The weekend of {weekend.start:%a %d %b}"
    with db.tx():
        db.insert("weekend_modes", mode=to, person_id=person_id)
        if to == TOGETHER and not shared.possible(db, weekend, provider):
            # somebody is away or not set up that weekend: that is theirs to say, not the page's
            return Flipped(
                to,
                (),
                f"{named} cannot be cooked together, so it stays each your own. The switch holds "
                "for the weekends after.",
            )
        chosen = versions(db, weekend).of(to)
        for pid, menu_id in chosen.items():
            put_in_force(db, pid, menu_id, weekend)
    missing = tuple(pid for pid in shared.people(db) if pid not in chosen)
    if missing and (weekend.start - today).days > BUILT_DAYS_AHEAD:
        # both versions are built on the weekend's Thursday, knowing that week's leftovers:
        # before then the switch only says which of them will be in force
        return Flipped(to, (), f"{named} is {which}: it is built on Thursday.")
    if not missing:
        return Flipped(to, (), f"{named} is {which}.")
    if to == TOGETHER:
        return Flipped(to, missing, f"{named} is cooked together: its menu is being built now.")
    return Flipped(
        to, missing, f"{named} is each your own: the menus still missing are being built now."
    )


def put_in_force(db: Db, person_id: int, menu_id: int, weekend: menu.Segment) -> None:
    """Decide one version's menu again, unless it already serves every day it covers - a second
    tap would otherwise make the weekend's list stale and rebuild it under somebody shopping.
    The person's own resized days on that version come back with it, decided after it."""
    days = {
        str(r["day"]) for r in db.all("SELECT day FROM menu_days WHERE menu_id = ?", (menu_id,))
    }
    serving = {
        str(r["day"])
        for r in db.all(
            "SELECT v.day FROM v_menu_days_current v "
            "LEFT JOIN menu_emphasis e ON e.menu_id = v.menu_id "
            "WHERE v.person_id = ? AND (v.menu_id = ? OR e.base_menu_id = ?)",
            (person_id, menu_id, menu_id),
        )
    }
    if days and days <= serving:
        return
    menu.decide(db, menu_id, "accepted")
    for r in db.all(
        "SELECT e.menu_id FROM menu_emphasis e WHERE e.person_id = ? AND e.base_menu_id = ? "
        "AND e.day BETWEEN ? AND ? AND e.id = (SELECT MAX(x.id) FROM menu_emphasis x "
        "WHERE x.person_id = e.person_id AND x.day = e.day AND x.base_menu_id = e.base_menu_id) "
        "ORDER BY e.id",
        (person_id, menu_id, isoday(weekend.start), isoday(weekend.end)),
    ):
        menu.decide(db, int(r["menu_id"]), "accepted")


def page(db: Db, person_id: int, today: dt.date, provider: str = "claude-cli") -> dict[str, object]:
    """What the Menu tab's weekend panel needs: the switch - only while sharing is possible at all -
    this person's own taste, and whether their version of this weekend exists yet."""
    weekend = target(db, today)
    out: dict[str, object] = {
        "start": isoday(weekend.start),
        "end": isoday(weekend.end),
        "taste": taste(db, person_id),
        "switch": None,
    }
    if not shared.everyone_agreed(db):
        return out
    mode = shared.mode(db)
    built = person_id in versions(db, weekend).of(mode)
    if mode == TOGETHER and not shared.possible(db, weekend, provider):
        note = "That weekend cannot be cooked together, so it is each your own."
    elif not built and (weekend.start - today).days > BUILT_DAYS_AHEAD:
        note = "Both versions are built on Thursday; the switch picks which one you cook."
    elif not built:
        note = "Your menu for it is being built now."
    else:
        note = "Both versions are ready: the switch picks which one you cook."
    flipped = db.one("SELECT person_id FROM weekend_modes ORDER BY id DESC LIMIT 1")
    by = ""
    if flipped is not None and flipped["person_id"] not in (None, person_id):
        by = f"{shared.name_of(db, int(flipped['person_id']))} set it last."
    out["switch"] = {"mode": mode, "note": note, "by": by}
    return out


__all__ = [
    "APART",
    "TASTES",
    "TASTE_SAID",
    "TOGETHER",
    "Flipped",
    "Versions",
    "flip",
    "in_force",
    "page",
    "put_in_force",
    "set_taste",
    "target",
    "taste",
    "taste_line",
    "too_late",
    "versions",
]
