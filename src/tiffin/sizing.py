"""Calculate clothing size and body measurements from the latest tape readings.

Read-outs use inches and keep boundary cases explicit."""

from __future__ import annotations

import math
from typing import Any

from . import body
from . import targets as targets_mod
from .clock import isoday
from .db import Db
from .food import weekplan

HOW: dict[str, tuple[str, str]] = {
    "chest": (
        "Under the armpits, around the fullest part of the chest and across the shoulder "
        "blades. Keep the tape level all the way round with arms down. Measure after a normal "
        "breath out, snug but not squeezing.",
        'Your primary size. 41.5-42.5" is a 42; 43.5-44.5" a 44; 43" is the grey zone.',
    ),
    "overarm": (
        "Around the outside of both arms and chest at the widest point of the deltoids, arms "
        "hanging.",
        'Cross-check for lifters: overarm less 6-7" is roughly the jacket size. If this says 44 '
        "and the chest says 42, your shoulders and back are the constraint.",
    ),
    "shoulder": (
        "Across the back from one bony shoulder tip to the other, following the curve.",
        "Hard to get right on the body; measuring a jacket is more reliable.",
    ),
    "waist": (
        "At the navel or the narrowest point, relaxed.",
        'The drop is chest less waist. Off-the-peg suits assume a 6" drop (42 jacket, 36 trouser).',
    ),
    "hip": (
        "The fullest part of the buttocks, feet together.",
        "Whether the jacket's skirt and vents lie flat or flare.",
    ),
    "neck": (
        "Around the neck just below the Adam's apple, tape level, one finger's room under it.",
        "Your shirt collar is the neck plus about half an inch. With waist and height it gives "
        "a tape estimate of body fat.",
    ),
    "sleeve": (
        "From the shoulder tip down the outside of a slightly bent arm to the wrist bone.",
        "Not a size decider: sleeves can be altered.",
    ),
}
GREY = 0.5  # inches either side of an even size that still reads as it


def inches(cm: float) -> float:
    return cm / body.CM_PER_INCH


def jacket(chest_in: float) -> dict[str, Any]:
    """The jacket size a chest reads as: one even size, or the grey zone between two."""
    low = 2 * math.floor(chest_in / 2)
    if chest_in - low <= GREY:
        return {"size": low}
    if low + 2 - chest_in <= GREY:
        return {"size": low + 2}
    return {"grey": [low, low + 2]}


def collar(neck_in: float) -> float:
    """A shirt collar: the neck plus about half an inch, up to the next half inch."""
    return math.ceil((neck_in + 0.5) * 2) / 2


def body_fat(
    sex: str,
    *,
    waist: float | None,
    neck: float | None,
    hip: float | None,
    height_in: float,
) -> float | None:
    """The US Navy tape estimate of body fat, in percent, from inches: waist and neck (and the hip
    for a woman) against height. None without the measures it needs, or off the formula's range."""
    if waist is None or neck is None or height_in <= 0:
        return None
    if sex.lower().startswith("f"):
        if hip is None or waist + hip - neck <= 0:
            return None
        got = 163.205 * math.log10(waist + hip - neck) - 97.684 * math.log10(height_in) - 78.387
    else:
        if waist - neck <= 0:
            return None
        got = 86.010 * math.log10(waist - neck) - 70.041 * math.log10(height_in) + 36.76
    return got if 2 <= got <= 60 else None


def length(height_cm: float) -> str:
    h = inches(height_cm)
    if h < 68:
        return "short"
    if h <= 72.05:  # 6'0", with 183 cm on the line
        return "regular"
    return "long"


def card(db: Db, person_id: int) -> dict[str, Any]:
    """The latest of each site, in their unit, how each is taken, and what they read as."""
    plan = weekplan.of(db, person_id)
    unit = plan.tape_unit
    sites = plan.sites() if plan.tape_sites else body.TAPE_SITES
    rows: list[dict[str, Any]] = []
    latest_in: dict[str, float] = {}
    for site in sites:
        got = body.latest_tape(db, person_id, site)
        how, decides = HOW.get(site, ("", ""))
        value = None
        if got is not None:
            latest_in[site] = inches(got[1])
            value = round(latest_in[site], 1) if unit == "in" else round(got[1], 1)
        rows.append(
            {
                "site": site,
                "label": body.TAPE_WORDS.get(site, site).capitalize(),
                "value": value,
                "day": isoday(got[0]) if got is not None else None,
                "how": how,
                "decides": decides,
            }
        )
    reads: list[str] = []
    chest = latest_in.get("chest")
    if chest is not None:
        j = jacket(chest)
        reads.append(
            f'Chest {chest:.1f}": a {j["size"]} jacket.'
            if "size" in j
            else f'Chest {chest:.1f}": the grey zone between a {j["grey"][0]} and a {j["grey"][1]}.'
        )
    overarm = latest_in.get("overarm")
    if overarm is not None:
        lo, hi = overarm - 7, overarm - 6
        said = f'Overarm {overarm:.1f}": about a {lo:.0f}-{hi:.0f} jacket.'
        if chest is not None:
            j = jacket(chest)
            top = j.get("size") or j["grey"][1]
            if lo > top + 0.5:
                said += " More than the chest says: your shoulders and back are the constraint."
        reads.append(said)
    waist = latest_in.get("waist")
    if chest is not None and waist is not None:
        reads.append(f'Drop {chest - waist:.1f}" (chest less waist); off-the-peg suits assume 6".')
    neck = latest_in.get("neck")
    if neck is not None:
        reads.append(f'Neck {neck:.1f}": a {collar(neck):g}" shirt collar.')
    profile = targets_mod.profile_on(db, person_id, db.clock.today_local())
    if profile is not None and profile["height_cm"]:
        h = float(profile["height_cm"])
        if waist is not None:
            ratio = waist / inches(h)
            reads.append(
                f"Waist to height {ratio:.2f}: "
                + ("under 0.5, the usual healthy line." if ratio < 0.5 else "the line is 0.5.")
            )
        fat = body_fat(
            str(profile["sex"] or "m"),
            waist=waist,
            neck=neck,
            hip=latest_in.get("hip"),
            height_in=inches(h),
        )
        if fat is not None:
            reads.append(
                f"Body fat about {fat:.0f}% (the US Navy tape estimate: a trend, not a scan)."
            )
        feet, rest = divmod(round(inches(h)), 12)
        reads.append(f"Height {h:.0f} cm ({feet}'{rest}\"): {length(h)} length.")
    return {"unit": unit, "sites": rows, "reads": reads}


__all__ = ["HOW", "body_fat", "card", "collar", "jacket", "length"]
