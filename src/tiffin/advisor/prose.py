"""advisor.prose: reason lines (<= 120 chars) and the Sunday lines (<= 6). Templates always
work; a model gateway, when one is wired, may rewrite them and the template is kept on
failure. Never a number the facts do not carry; never the other person's misses."""

from __future__ import annotations

from typing import Any, Protocol

from ..telegram.render import kg, macros_line, signed_kg
from .facts import Facts
from .rules import Candidate


class ProseGateway(Protocol):
    def json_task(self, task: str, prompt: str, schema_name: str, *, person_id: int) -> Any: ...


def reason_line(c: Candidate) -> str:
    line = c.reason.strip()
    return line if len(line) <= 120 else line[:117].rstrip() + "..."


def trust_line(confidence: str | None) -> str:
    return {
        None: "No calibration yet: the formula leads.",
        "low": "Formula still leads; measured TDEE blended lightly.",
        "medium": "Measured TDEE weighted 50 %.",
        "high": "Measured TDEE leads.",
    }.get(confidence, "Formula still leads.")


def summary_lines(
    facts: Facts,
    *,
    confidence: str | None,
    next_targets: Any | None,
    check_in_tonight: bool = False,
) -> list[str]:
    """The person's own block: at most 6 lines, facts only.

    `check_in_tonight` is a weekly rhythm's review, read before the one check-in that asks about
    the week: its open days are still to be asked, not days nobody answered.
    """
    lines: list[str] = []
    if facts.trend_now is not None:
        delta = (
            f" ({signed_kg(facts.trend_delta_7)} this week)"
            if facts.trend_delta_7 is not None
            else ""
        )
        lines.append(f"Trend {kg(facts.trend_now)}{delta}")
    else:
        lines.append("No weigh-ins this week.")
    to_ask = check_in_tonight and facts.unconfirmed_days > 0
    lines.append(
        # the person's words, not the ledger's: "unconfirmed" and "intake mean" were both
        # column names
        f"Days recorded: {facts.confirmed_days}"
        + ("" if to_ask else f", {facts.unconfirmed_days} not answered")
        + (f", {facts.paused_days} paused" if facts.paused_days else "")
        + (
            f"; about {facts.intake_mean:,.0f} kcal a day on average"
            if facts.intake_mean is not None
            else ""
        )
        + (f"; tonight's check-in asks about the other {facts.unconfirmed_days}" if to_ask else "")
    )
    if facts.sessions_planned and facts.sessions_measured:
        lines.append(f"Sessions {facts.sessions_done}/{facts.sessions_planned}")
    if next_targets is not None:
        m = next_targets
        lines.append("Next week: " + macros_line(m.kcal, m.protein_g, m.fat_g, m.carb_g, m.fibre_g))
    lines.append(trust_line(confidence))
    return lines[:6]


def partner_lines(facts: Facts, display_name: str) -> list[str]:
    """What the other person sees about this one: trend and sessions, never misses."""
    parts = []
    if facts.trend_now is not None:
        delta = f" ({signed_kg(facts.trend_delta_7)})" if facts.trend_delta_7 is not None else ""
        parts.append(f"trend {kg(facts.trend_now)}{delta}")
    if facts.sessions_planned and facts.sessions_measured:
        parts.append(f"sessions {facts.sessions_done}/{facts.sessions_planned}")
    if not parts:
        return []
    # a blank name is possible and honest; "Your partner" beats an invented one
    who = display_name.strip() or "Your partner"
    return [f"{who}: " + ", ".join(parts)]


def maybe_model_prose(
    gateway: ProseGateway | None, person_id: int, lines: list[str]
) -> tuple[list[str], str | None]:
    """Ask the model to tighten the lines; keep the template on any failure. (model_used, or
    None)."""
    if gateway is None:
        return lines, None
    try:
        prompt = (
            "Rewrite these lines for a weekly nutrition summary. Keep every number exactly, "
            "no emoji, food is on or off plan never good or bad, at most 6 lines:\n"
            + "\n".join(lines)
        )
        result = gateway.json_task("sunday_prose", prompt, "sunday_prose", person_id=person_id)
        data = getattr(result, "data", None) or {}
        out = [str(x) for x in data.get("lines", [])][:6]
        if out and all(len(x) <= 160 for x in out) and _sendable(out):
            return out, getattr(result, "model_used", None)
    except Exception:
        pass
    return lines, None


def _sendable(out: list[str]) -> bool:
    """Check model prose against the rules used for outgoing messages."""
    from ..telegram.render import RenderError, check_text  # noqa: PLC0415 (avoids a cycle)

    try:
        check_text("\n".join(out), kind="sunday_prose", max_lines=len(out))
    except RenderError:
        return False
    return True
