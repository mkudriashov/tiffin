"""Record a changed meal or completed training session from the web page.

A changed meal creates an accepted estimate for the meal it replaces. Daily and weekly readers
then use the same ledger entry as the Telegram flow. Completed sessions use `session_matches`.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

from . import days
from .clock import isoday
from .db import Db
from .errors import ModelError, TiffinError
from .model import prompts
from .persons import Person

MAX_TEXT = 300
SLOT_WORDS = {
    "breakfast": "breakfast",
    "lunch": "lunch",
    "dinner": "dinner",
    "snack": "snack",
    "pre_bed": "pre-bed",
}


def record_different(
    app: Any, db: Db, person: Person, day: dt.date, slot: str, text: str
) -> dict[str, Any]:
    """What was eaten in place of the planned `slot` on `day`, estimated and counted for that day.
    Returns {"ok", "said", "kcal"}; never raises for a model that will not answer - it says so,
    and nothing is written (no number is never recorded as 0 kcal)."""
    words = text.strip()[:MAX_TEXT]
    meal = SLOT_WORDS.get(slot, slot)
    gateway = app.gateway(db)
    if gateway is None:
        return {
            "ok": False,
            "kcal": None,
            "said": "Nothing is recorded: estimating food needs Claude, which is not "
            "reachable just now. Try again in a little while.",
        }
    prompt = prompts.offplan_estimate(
        words, hints=f"The day is {isoday(day)}. It was eaten in place of the planned {meal}."
    )
    try:
        result = gateway.json_task(
            "offplan_estimate", prompt, "offplan_estimate", person_id=person.id
        )
    except (ModelError, TiffinError) as e:
        no_consent = "no_consent" in str(e)
        return {
            "ok": False,
            "kcal": None,
            "said": (
                "Nothing is recorded: estimating food is done by Claude, and Claude processing "
                "is off for you. /consent in Telegram turns it on."
                if no_consent
                else "Nothing is recorded: Claude did not answer just now. Try again in a little "
                "while."
            ),
        }
    data = result.data or {}
    kcal = float(data.get("kcal") or 0)
    if kcal <= 0 or not data.get("items"):
        return {
            "ok": False,
            "kcal": None,
            "said": "That did not read as food, so nothing is recorded. Say what you ate, like "
            "'a chicken wrap from Pret'.",
        }
    with db.tx():
        db.insert(
            "estimates",
            person_id=person.id,
            day=isoday(day),
            description=words,
            items_json=json.dumps(data.get("items", [])),
            kcal=kcal,
            protein_g=data.get("protein_g"),
            fat_g=data.get("fat_g"),
            carb_g=data.get("carb_g"),
            fibre_g=data.get("fibre_g"),
            model_used=result.model_used,
            status="accepted",
            message_in_id=None,
            replaced_slots_json=json.dumps([slot]),
        )
        latest = days.latest_confirmation(db, person.id, day)
        if latest is not None and latest["status"] == "confirmed":
            # a day already recorded is recorded again with it: the planned meal off, this on
            base = days.planned_macros(db, person.id, day)
            days.confirm_day(
                db,
                person.id,
                day,
                basis=str(latest["basis"]),
                macros=base or days.target_macros(db, person.id, day),
                note=latest["note"],
            )
    return {
        "ok": True,
        "kcal": round(kcal),
        "said": f"Recorded for {day:%a %d %b}: about {round(kcal)} kcal in place of the planned "
        f"{meal}. The rest of the day stays as it is.",
    }


def record_session(db: Db, person: Person, day: dt.date, *, minutes: int | None, done: bool) -> str:
    """A session trained on `day` (or a planned one skipped), as the bot's buttons record it: one
    row a planned session, or one unplanned row."""
    status = "manual" if done else "missed"
    planned = list(days.planned_sessions_on(db, person.id, day))
    if not done and not planned:
        return "Nothing was planned that day, so there is nothing to skip."
    with db.tx():
        for row in planned or [None]:
            db.insert(
                "session_matches",
                person_id=person.id,
                day=isoday(day),
                planned_session_id=row["id"] if row is not None else None,
                status=status,
                minutes=minutes if done else None,
                message_in_id=None,
            )
    if done:
        return f"Recorded: you trained on {day:%a %d %b}" + (
            f", {minutes} minutes." if minutes else "."
        )
    return f"Recorded: the session planned for {day:%a %d %b} was skipped."


__all__ = ["record_different", "record_session"]
