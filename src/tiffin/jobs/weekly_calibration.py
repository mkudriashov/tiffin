"""weekly_calibration (each, Sunday 17:30): measured TDEE and guarded targets.

Writes a tdee_calibrations row, guarded targets, and menu biases and summary notes.

weekly_calibration_late (each, Monday 00:10) looks at the week again once its Sunday asks are
over, when the 17:30 run had too few answered days to measure (`run_late`).
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json

from .. import energy, targets
from ..advisor import facts as facts_mod
from ..advisor import prose, rules
from ..clock import isoday, parse_day
from ..guardrails import NeedsProposal, apply_guardrails
from ..scheduler import Job, JobContext, fixed

SUNDAY = frozenset({6})
MONDAY = frozenset({0})


def prior_tdee(ctx: JobContext, person_id: int, current_basis: dict) -> tuple[float, int]:
    row = ctx.db.one(
        "SELECT tdee_blended FROM tdee_calibrations "
        "WHERE person_id = ? AND tdee_blended IS NOT NULL "
        "ORDER BY id DESC LIMIT 1",
        (person_id,),
    )
    count = int(
        ctx.db.scalar(
            "SELECT COUNT(*) FROM tdee_calibrations "
            "WHERE person_id = ? AND tdee_measured IS NOT NULL",
            (person_id,),
        )
        or 0
    )
    if row is not None and row["tdee_blended"]:
        return float(row["tdee_blended"]), count
    tdee = current_basis.get("tdee")
    if not tdee:
        # a row written by an accepted proposal records only {"proposal": id}: take the TDEE
        # from the last row that has one, or the week after an accepted cut was rebuilt from
        # the formula at today's weight and what was left of the cut was arbitrary
        for r in ctx.db.all(
            "SELECT basis_json FROM targets WHERE person_id = ? AND week_start <= ? "
            "ORDER BY week_start DESC, id DESC",
            (person_id, isoday(ctx.day)),
        ):
            try:
                tdee = json.loads(r["basis_json"] or "{}").get("tdee")
            except ValueError:
                continue
            if tdee:
                break
    return float(tdee or 0.0), count


def chosen_by_tap(ctx: JobContext, person_id: int, week: dt.date) -> bool:
    """Are `week`'s targets a historical accepted proposal that the late run must preserve?"""
    row = targets.targets_for(ctx.db, person_id, week)
    return row is not None and row["proposal_id"] is not None


def run(ctx: JobContext, *, as_of: dt.date | None = None, late: bool = False) -> None:
    """The week of `ctx.day`, read as of `as_of` (the day itself, unless `run_late` says later).

    `late` is Monday's second look after Sunday evening's answers.
    """
    person = ctx.person
    if person is None or ctx.profile is None:
        ctx.handle.skip("no profile")
        return
    cfg = ctx.app.config
    g = cfg.section("guardrails")
    f = facts_mod.compute(ctx.db, person.id, ctx.day, cfg, as_of=as_of or ctx.day)
    t_row = targets.latest_targets(ctx.db, person.id, ctx.day)
    if t_row is None or f.weight_kg is None:
        ctx.handle.skip("no targets or no weight yet")
        return
    current = targets.macros_of(t_row)
    basis = json.loads(t_row["basis_json"] or "{}")
    prior, prior_count = prior_tdee(ctx, person.id, basis)
    if prior <= 0:
        _formula, formula_basis = targets.formula_targets(ctx.profile, f.weight_kg, ctx.day, cfg)
        prior = float(formula_basis["tdee"])
    cal = energy.calibrate_tdee(
        f.day_intakes,
        f.trend_delta_7,
        prior,
        prior_count,
        min_usable=int(g.get("min_usable_days", 5)),
        kcal_per_kg=float(g.get("kcal_per_kg_fat", 7700)),
    )
    if isinstance(cal, energy.Calibration):
        ctx.db.insert(
            "tdee_calibrations",
            person_id=person.id,
            week_start=isoday(f.week_start),
            usable_days=cal.usable_days,
            intake_kcal_mean=cal.intake_kcal_mean,
            trend_delta_kg=cal.trend_delta_kg,
            tdee_measured=cal.tdee_measured,
            tdee_blended=cal.tdee_blended,
            confidence=cal.confidence,
        )
        tdee = cal.tdee_blended
        confidence = cal.confidence
        ctx.db.state_set(f"sunday_skip:{person.id}", "")  # this week did recalibrate
    else:
        ctx.db.insert(
            "tdee_calibrations",
            person_id=person.id,
            week_start=isoday(f.week_start),
            usable_days=cal.usable_days,
            skipped_reason=cal.reason,
        )
        tdee = prior
        # keep the previous calibration's confidence, and record WHY this week was skipped.
        # Zeroing it made trust_line print "No calibration yet: the formula leads." to someone
        # with five calibrations behind them whose targets came from the measured TDEE - both
        # halves untrue, and the real reason ("4 of 7 days had numbers") went into a column
        # nothing reads. A week where recalibration did not happen looked exactly like one
        # where it did, which is the one thing this job must never do.
        last = ctx.db.one(
            "SELECT confidence FROM tdee_calibrations WHERE person_id = ? "
            "AND confidence IS NOT NULL ORDER BY week_start DESC, id DESC LIMIT 1",
            (person.id,),
        )
        confidence = str(last["confidence"]) if last is not None else None
        ctx.db.state_set(
            f"sunday_skip:{person.id}",
            json.dumps({"week_start": isoday(f.week_start), "reason": cal.reason}),
        )

    p = ctx.profile
    proposed = energy.weekly_targets(
        weight_kg=f.weight_kg,
        tdee=tdee,
        rate_pct=float(p["rate_pct_per_week"]),
        max_loss_kg_week=float(p["max_loss_kg_week"]),
        kcal_floor=int(p["kcal_floor"]),
        protein_g_per_kg=float(p["protein_g_per_kg"]),
        fat_g_per_kg_min=float(p["fat_g_per_kg_min"]),
        fibre_min=int(p["fibre_g_min"]),
        fibre_max=int(g.get("fibre_max", 35)),
    )
    next_week = f.week_end + dt.timedelta(days=1)
    # Historical accepted R2W cuts hold until a measured week replaces the estimate they
    # stood in for. A cut from a week since measured is already in that measurement.
    measured = ctx.db.one(
        "SELECT week_start FROM tdee_calibrations WHERE person_id = ? "
        "AND tdee_measured IS NOT NULL ORDER BY week_start DESC, id DESC LIMIT 1",
        (person.id,),
    )
    measured_week = parse_day(str(measured["week_start"])) if measured is not None else None
    weight_cut = sum(
        kcal
        for week, kcal in facts_mod.weight_cuts(ctx.db, person.id)
        if week <= next_week and (measured_week is None or week > measured_week)
    )
    if weight_cut:
        proposed = energy.Macros(
            proposed.kcal + weight_cut,
            proposed.protein_g,
            proposed.fat_g,
            max(proposed.carb_g + weight_cut // 4, 0),
            proposed.fibre_g,
        )
    cands = rules.candidates(f, max_loss=float(p["max_loss_kg_week"]))
    applied_rules: list[str] = []
    for c in cands:
        if c.kind == "targets" and c.rule == "R2":  # R1 is applied by the guardrails themselves
            proposed = energy.Macros(
                proposed.kcal + int(c.payload["kcal_delta"]),
                proposed.protein_g,
                proposed.fat_g,
                max(proposed.carb_g + int(c.payload["kcal_delta"]) // 4, 0),
                proposed.fibre_g,
            )
            applied_rules.append(c.rule)
    verdict = apply_guardrails(
        proposed,
        current,
        weight_kg=f.weight_kg,
        kcal_floor=int(p["kcal_floor"]),
        protein_g_per_kg=float(p["protein_g_per_kg"]),
        fat_g_per_kg_min=float(p["fat_g_per_kg_min"]),
        trend_delta_2w=f.trend_delta_14,
        max_loss_kg_week=float(p["max_loss_kg_week"]),
        auto_kcal=int(g.get("auto_adjust_kcal", 150)),
        auto_protein=int(g.get("auto_adjust_protein_g", 10)),
        source="calibration",
    )
    new_basis = {
        "tdee": round(tdee, 1),
        "prior_tdee": round(prior, 1),
        "confidence": confidence,
        "calibration": cal.__dict__
        if isinstance(cal, energy.Calibration)
        else {"skipped": cal.reason},
        "guardrail_notes": verdict.notes,
        "rules": applied_rules,
        "weight_kg": round(f.weight_kg, 2),
        **({"weight_cut_kcal": weight_cut} if weight_cut else {}),
    }
    if late and (isinstance(verdict, NeedsProposal) or chosen_by_tap(ctx, person.id, next_week)):
        # Preserve the current target if the change exceeds the automatic bounds, or a
        # historical proposal was accepted for this week.
        ctx.handle.note("next week's targets kept")
    elif isinstance(verdict, NeedsProposal):
        # Carry forward within the mandatory guardrails; no approval question is sent.
        carried = verdict.apply_now or current
        targets.insert_targets(
            ctx.db,
            person.id,
            next_week,
            carried,
            "calibration",
            {**new_basis, "carried_over": True},
        )
        ctx.handle.note("targets carried over")
    else:
        targets.insert_targets(
            ctx.db, person.id, next_week, verdict.macros, verdict.source, new_basis
        )
        ctx.handle.note(f"targets {verdict.source} {verdict.macros.kcal} kcal")

    biases = [c.payload["bias"] for c in cands if c.kind == "menu_bias"]
    ctx.db.state_set(f"menu_bias:{person.id}", json.dumps(biases))
    # include the rules that CHANGED the calories, and the guardrails' own notes. A target
    # that moves for no stated reason reads as a bug: R1's +150 for losing too fast made the
    # number go UP in the middle of a cut, and the explanation existed and was thrown away -
    # it went into basis_json, which nothing renders. The automatic path must state why a
    # number moved without asking.
    notes = [
        prose.reason_line(c)
        for c in cands
        if c.kind in ("note", "pace_note", "targets", "menu_bias")
    ]
    notes += [str(n) for n in verdict.notes]
    ctx.db.state_set(
        f"sunday_notes:{person.id}",
        json.dumps({"week_start": isoday(f.week_start), "lines": notes}),
    )
    ctx.db.state_set(f"sunday_confidence:{person.id}", confidence or "")
    ctx.handle.note(f"auto {len(cands)}")


def run_late(ctx: JobContext) -> None:
    """The week just ended, calibrated again at 00:10 on Monday when Sunday's run could not.

    The 17:30 run is the Sunday review's, and it counts only what had been answered by then.
    Every ask about the week comes after it: the Sunday message's open days at 19:00, and the
    evening check-in, which for a weekly rhythm is the only one the week gets. Answered there,
    the days went into no calibration at all - the next Sunday reads the next week - so someone
    who answered every week on Sunday evening was never measured and their targets never
    moved. When Sunday's run skipped for too few days and the week now has
    enough, it runs again here from the same starting point, the Sunday counted (`day_close`
    has written it at 00:05). Next week's targets are written again - the newer row is the one
    in force - and the kept review with them, so the page says what was measured, or why not.
    """
    from . import sunday_summary  # noqa: PLC0415 (the review it keeps is written again)

    person = ctx.person
    if person is None or ctx.profile is None:
        ctx.handle.skip("no profile")
        return
    sunday = ctx.day - dt.timedelta(days=1)
    week = facts_mod.compute(ctx.db, person.id, sunday, ctx.app.config, as_of=ctx.day)
    last = ctx.db.one(
        "SELECT * FROM tdee_calibrations WHERE person_id = ? AND week_start = ? "
        "ORDER BY id DESC LIMIT 1",
        (person.id, isoday(week.week_start)),
    )
    if last is None or last["tdee_measured"] is not None:
        ctx.handle.skip("measured on Sunday" if last is not None else "no Sunday run")
        return
    need = int(ctx.app.config.section("guardrails").get("min_usable_days", 5))
    probe = energy.calibrate_tdee(week.day_intakes, week.trend_delta_7, 0.0, 0, min_usable=need)
    if week.usable_days <= int(last["usable_days"]):
        ctx.handle.skip(f"nothing answered since Sunday: {week.usable_days} usable days")
    elif isinstance(probe, energy.Skip):
        # still too few, and the review says how many there are now rather than at 17:30
        ctx.db.state_set(
            f"sunday_skip:{person.id}",
            json.dumps({"week_start": isoday(week.week_start), "reason": probe.reason}),
        )
        ctx.handle.skip(probe.reason)
    else:
        run(dataclasses.replace(ctx, day=sunday), as_of=ctx.day, late=True)
    sunday_summary.refresh_review(
        ctx.db, ctx.app.config, ctx.app.registry(ctx.db), person, [sunday]
    )


JOB = Job(
    name="weekly_calibration",
    scope="each",
    kind="data",
    times=fixed("17:30"),
    run=run,
    catch_up="always",
    days=SUNDAY,
    paused_ok=True,
)

#: after day_close (00:05) has written the Sunday, and after every ask about the week
LATE_JOB = Job(
    name="weekly_calibration_late",
    scope="each",
    kind="data",
    times=fixed("00:10"),
    run=run_late,
    catch_up="always",
    days=MONDAY,
    paused_ok=True,
)
