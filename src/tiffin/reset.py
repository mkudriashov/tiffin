"""`tiffin reset-onboarding <slug>`: forget what a person told the bot, keep what it measured.

Not erasure. Erasure is for a person leaving and takes everything, including the identity that
lets them speak to the bot at all. This is for starting the interview again: the answers and
everything computed from them go, and the record of what actually happened stays - the weight
history read off intervals.icu, the activities, the messages, the job ledger.

The split is written out table by table below rather than inferred, because the failure mode
of inferring it is silent: a table nobody classified is a table that either survives a reset
it should not have, or is deleted with data nobody could get back. `test_audit_reset` asserts
every person-scoped table appears in exactly one of the two lists, so a new one cannot ship
without someone deciding which it is.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import body
from . import db as dbmod
from .clock import DEFAULT_CLOCK, Clock
from .db import Db

#: everything a person told the bot, or that was computed from what they told it
TOLD: tuple[str, ...] = (
    "onboarding_answers",
    "conversation_state",
    "profiles",
    "consents",
    "targets",
    "tdee_calibrations",
    "training_plans",
    "planned_sessions",
    # their own plates for a day, which point at their menus, so they go first
    "menu_emphasis",
    # what their weekend treats lean to, and the times they flipped the weekend's switch
    "treat_tastes",
    "weekend_modes",
    "menus",
    "estimates",
    "day_confirmations",
    "ratings",
    # what their swaps said about their taste: the dish taken away and the one chosen
    "dish_events",
    # and what a change to one food inside a dish said: the food taken out, the one put in
    "ingredient_events",
    # dishes the model wrote for one of their meals, to choose from
    "built_dishes",
    # their weeks of choices (food/choices.py): what they were offered and what they picked
    "choice_sets",
    # what their proposals were weighed on: each dish's mean and draw
    "proposal_log",
    "ingredient_preferences",
    "product_choices",
    "manual_products",
    "leftovers",
    "shopping_lists",
    "shopping_ticks",
    # "I have it" on their lists, and their word on how many meats or fish a week (0047)
    "shopping_have",
    "pack_prefs",
    # how they plan their week, in their own word (0048)
    "week_prefs",
    "batch_ticks",
    # their half of a shared weekend: the link from the pair to THEIR menu
    "shared_menu_members",
    # what a food weighed when THIS person cooked it: their kitchen, their air fryer, their
    # weighing. It goes with everything else they wrote down.
    "cook_yields",
    "proposals",
    "intents",
    "documents",
    "document_facts",
    "tape_measures",
    "cycle_phases",
    "pause_periods",
    "person_names",
    "person_notes",
    "session_matches",
    "trend_points",
    "web_events",
)

#: what the machine collected, or what identifies them, and why the reset leaves it alone
KEPT: dict[str, str] = {
    "icu_wellness": "read off intervals.icu, not typed",
    "icu_activity": "read off intervals.icu, not typed",
    "icu_raw": "the payloads those were parsed from",
    "icu_stream_index": "content-addressed stream files on disk",
    "weigh_ins": "split by source: the scale's readings stay, the typed one goes",
    "telegram_identities": "the pairing; resetting it would mean a new code before /onboard",
    "tailscale_identities": "the web page binding",
    "web_login_links": "short-lived sign-in links are identity records",
    "web_devices": "device sessions survive a new onboarding interview",
    "pairing_codes": "spent codes are a ledger of how the pairing happened",
    "messages_in": "what was actually said, on both sides",
    "messages_out": "and what was sent back, including the send-once keys",
    # the same review rides in the Sunday message's payload, which is kept: deleting it here
    # took only the weeks whose message had been withheld, and the Summary tab fell back to an
    # older week that happened to be sent
    "weekly_reviews": "what was written for them each week, like the messages that carried it",
    "run_ledger": "what the scheduler did, which is not about this person's answers",
}

#: tables only PARTLY reset, and the clause that picks the part. The interview asks for a
#: weight and writes one weigh-in; the readings behind it came off the scale through
#: intervals.icu and are the only reason the trend and the calibration mean anything.
SPLIT: dict[str, str] = {"weigh_ins": "person_id = ? AND source = 'manual'"}

#: rows in tables with no person_id that belong to a person through a parent row.
#: DEEPEST FIRST, and before anything in TOLD: each clause finds its rows by reading through
#: the parent, so deleting the parent first leaves the child orphaned and invisible to the
#: query that was supposed to remove it. That is not a crash, it is a menu_days row pointing
#: at a menu that no longer exists.
CHILDREN: dict[str, str] = {
    # a week of choices: the picks and the outcome point at the options, the options at the set
    "choice_picks": "set_id IN (SELECT id FROM choice_sets WHERE person_id = ?)",
    "choice_outcomes": "set_id IN (SELECT id FROM choice_sets WHERE person_id = ?)",
    "choice_options": "set_id IN (SELECT id FROM choice_sets WHERE person_id = ?)",
    "meal_slots": (
        "menu_day_id IN (SELECT md.id FROM menu_days md JOIN menus m ON m.id = md.menu_id "
        "WHERE m.person_id = ?)"
    ),
    "menu_days": "menu_id IN (SELECT id FROM menus WHERE person_id = ?)",
    "menu_decisions": "menu_id IN (SELECT id FROM menus WHERE person_id = ?)",
    "shopping_items": "list_id IN (SELECT id FROM shopping_lists WHERE person_id = ?)",
    "proposal_decisions": "proposal_id IN (SELECT id FROM proposals WHERE person_id = ?)",
    "intent_decisions": "intent_id IN (SELECT id FROM intents WHERE person_id = ?)",
    "document_decisions": "document_id IN (SELECT id FROM documents WHERE person_id = ?)",
}

#: tables with no person_id that a reset must never touch, and why
SHARED: dict[str, str] = {
    "shared_menus": "the pair's header; the other person's half of the weekend hangs off it",
    "recipes": "person_scope 'both': the other person's menus are built from these too",
    "recipe_ingredients": "belongs to those recipes",
    "ingredients": "the food catalogue, shared and expensive to rebuild",
    "ingredient_aliases": "the same catalogue",
    "ingredient_units": "how much one slice or one egg weighs: about the food, not a person",
    "products": "the shop's packs and prices, shared",
    "shop_misses": "what a shop does not stock: about the shop, not about a person",
    "inbound_outcomes": "belongs to messages_in, which is kept",
    "voice_transcripts": "belongs to messages_in, which is kept",
    "pairing_uses": "belongs to pairing_codes, which is kept",
    "web_link_uses": "belongs to web_login_links, which is kept",
    "web_device_events": "belongs to web_devices, which is kept",
    "web_device_seen": "belongs to web_devices, which is kept",
    "bot_state": "the update offset; losing it replays or skips Telegram updates",
    "icu_sync_state": "where the intervals.icu sync got to",
    "icu_type_map": "the shared activity-type mapping",
    "schema_version": "the migration ledger",
    "persons": "the person themselves",
    "exports": "the erasure paper trail",
    "erasures": "the erasure paper trail",
}


@dataclass
class ResetReport:
    slug: str
    person_id: int
    removed: dict[str, int] = field(default_factory=dict)
    kept: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.removed.values())

    def as_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "person_id": self.person_id,
            "rows_removed": {k: v for k, v in sorted(self.removed.items()) if v},
            "rows_kept": {k: v for k, v in sorted(self.kept.items()) if v},
            "total_removed": self.total,
        }


def preview(db: Db, slug: str) -> ResetReport:
    """What a reset would take and what it would leave, without taking anything."""
    return _run(db, slug, apply=False)


def reset_onboarding(db: Db, slug: str, *, clock: Clock = DEFAULT_CLOCK) -> ResetReport:
    """Delete what they told the bot. The ledger triggers come off and go straight back on."""
    return _run(db, slug, apply=True, clock=clock)


def _person_id(db: Db, slug: str) -> int:
    row = db.one("SELECT id FROM persons WHERE slug = ?", (slug,))
    if row is None:
        raise LookupError(f"no person {slug}")
    return int(row["id"])


def _run(db: Db, slug: str, *, apply: bool, clock: Clock = DEFAULT_CLOCK) -> ResetReport:
    pid = _person_id(db, slug)
    report = ResetReport(slug=slug, person_id=pid)
    present = set(dbmod.tables(db.con))
    plan: list[tuple[str, str, tuple[Any, ...]]] = []
    for table, where in CHILDREN.items():
        if table in present:
            plan.append((table, where, (pid,)))
    for table in TOLD:
        if table in present:
            plan.append((table, "person_id = ?", (pid,)))
    for table, where in SPLIT.items():
        if table in present:
            plan.append((table, where, (pid,)))

    for table, where, params in plan:
        n = db.scalar(f"SELECT COUNT(*) FROM {dbmod._check_ident(table)} WHERE {where}", params)
        report.removed[table] = report.removed.get(table, 0) + int(n or 0)
    for table in KEPT:
        if table in present and "person_id" in dbmod.columns(db.con, table):
            left = db.scalar(
                f"SELECT COUNT(*) FROM {dbmod._check_ident(table)} WHERE person_id = ?",
                (pid,),
            )
            report.kept[table] = int(left or 0)
    for table, where in SPLIT.items():
        if table in present:
            report.kept[table] = int(
                db.scalar(
                    f"SELECT COUNT(*) FROM {dbmod._check_ident(table)} "
                    f"WHERE person_id = ? AND NOT ({where.replace('person_id = ? AND ', '')})",
                    (pid,),
                )
                or 0
            )
    if not apply:
        return report

    ledgers = dbmod.ledger_tables(db.con)
    # outside the transaction on purpose: executescript COMMITs whatever is open before it
    # runs, so dropping the triggers from inside `db.tx()` ends the transaction under the
    # deletes. That is the bug this found in `admin.erase_person`.
    db.con.executescript("".join(dbmod.drop_trigger_sql(t) for t in ledgers))
    try:
        with db.tx():
            # a menu is deleted before its days, a training plan before its sessions. Ordering
            # the list by hand would be right until the day somebody added a table to it, so
            # the foreign keys are checked once at COMMIT instead of after every statement -
            # by which point every related row has gone. SQLite clears this at COMMIT.
            db.execute("PRAGMA defer_foreign_keys = ON")
            for table, where, params in plan:
                db.execute(f"DELETE FROM {dbmod._check_ident(table)} WHERE {where}", params)
    finally:
        db.con.executescript("".join(dbmod.trigger_sql(t) for t in ledgers))
    # the trend is an EWMA over the weigh-ins that are left. rebuild_trend only ever adds, so
    # the points were cleared above and are recomputed here from what survived.
    body.rebuild_trend(db, pid)
    for key in (
        "followup_started",
        "followup_prompt",
        "followup_offered",
        "followup_applied",
        "onboarding_parked",
    ):
        db.state_set(f"{key}:{pid}", "")
    report.kept["trend_points"] = int(
        db.scalar("SELECT COUNT(*) FROM trend_points WHERE person_id = ?", (pid,)) or 0
    )
    return report


def write_report(path: Path, report: ResetReport) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report.as_dict(), indent=2), encoding="utf-8")
