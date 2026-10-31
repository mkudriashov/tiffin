"""Who owns the next message.

`conversation_state` holds one open flow per person, and several commands ask "is somebody
already waiting for an answer?" before arming their own. They each asked it slightly
differently, and all of them got the same case wrong: onboarding answered with "Later" keeps
its row so `/onboard` can resume, but nothing is waiting for anything. Treating that as busy
made `/measure` reply "answer the question above first" when there was no question above -
and the only escape was to finish an interview the person had just deferred.
"""

from __future__ import annotations

import json
import re

from ..clock import DEFAULT_CLOCK, Clock, parse_stamp
from ..db import Db

#: How long each flow may hold the next message before it stops being anybody's turn. The flows
#: that own these numbers check them on their own way in; this is the same expiry seen from the
#: OTHER side - the commands that ask "is somebody mid-question?" before arming themselves.
#:
#: Without it, `body_check` arms the tape question at Sunday 10:00, nobody answers, and the
#: evening "Not quite" button is refused at 20:30 with "Answer the question above first" -
#: pointing at a question ten hours up the chat. After 12 hours it was worse than refused: the
#: measures flow stops CLAIMING messages past its TTL but left the row in place, so the question
#: could no longer be answered ("84 102") or skipped ("skip") either, and it blocked the evening
#: button permanently. On the one day of the week the calibration reads.
FLOW_TTL_S = {
    "measures": 12 * 3600,
    "menu_change": 6 * 3600,
    # dayflow.OFFPLAN_TTL, seen from the other side: an abandoned "Not quite" from Monday made a
    "offplan": 8 * 3600,
    # a typed question of /questions (telegram/interview.py)
    "interview": 24 * 3600,
    "manual_recipe": 24 * 3600,
}


#: Tiffin's own nouns. A free-text message naming one is asking for something, not
#: answering the question that happens to be open - and `conversation_state` holds ONE flow per
#: person, so whatever is open claims every message until it is answered.
#:
#: Sunday 16:50, the 10:00 tape question still unanswered: "Send me the menu for tomorrow" was
#: answered "I need centimetres, like 84 102 for waist then hip", and the question he asked was
#: gone. With the off-plan question open instead it is worse than gone - "send me the menu for
#: tomorrow" becomes yesterday's food note, and a wrong number reaches the measured TDEE.
#:
#: Deliberately a short list of things Tiffin OWNS, not a general intent guess. "About the
#: same as last week" is a tape answer and has to stay one; the cost of being wrong here is a
#: person's reply being dropped, so the bar is a word that has no business in an answer.
_OURS = re.compile(
    r"\b(menu|menus|shopping list|recipe|recipes|cook|batch|"
    r"proposal|proposals|shopping)\b",
    re.I,
)

#: the general SHAPE a question has - a trailing "?" or a leading interrogative/auxiliary word -
#: as opposed to `_OURS`, which is a curated list of Tiffin's own nouns. "What's for dinner
#: tomorrow?" names none of those nouns and was logged as the evening check-in's food note
#:; "can I swap Wednesday dinner?" named none of them either and was read as a tape
#: measurement. Neither list can grow to cover every question a person might ask, so this is a
#: heuristic about shape, not vocabulary, and it is shared by every flow that has to tell "an
#: answer to my question" from "a question of their own".
_QUESTION_LEAD = re.compile(
    r"^\s*(what|who|whom|whose|which|when|where|why|how|"
    r"is|are|am|was|were|do|does|did|can|could|should|would|will|shall|may|might|"
    r"have|has|had)\b",
    re.I,
)


def looks_like_a_question(text: str) -> bool:
    """True when this free text has the shape of a question, not what it is about."""
    t = (text or "").strip()
    return t.endswith("?") or bool(_QUESTION_LEAD.match(t))


def asks_for_something_else(text: str) -> bool:
    """True when this free text is a request the router should answer, not a flow's answer."""
    return bool(_OURS.search(text or "")) or looks_like_a_question(text)


def food_answer_is_something_else(text: str) -> bool:
    """The same test for an answer to "what changed?", where the leading-word rule is wrong.

    "Had a kebab instead of dinner", "Was out for dinner, pizza" and "Did not eat lunch" are the
    commonest ways to say what changed, and all three start with an auxiliary, so the general
    rule sent every one of them to chat and the day's food was never recorded. Here only a
    trailing "?" or one of Tiffin's own nouns diverts.
    """
    t = (text or "").strip()
    return t.endswith("?") or bool(_OURS.search(t))


def open_flow(db: Db, person_id: int, *, clock: Clock = DEFAULT_CLOCK) -> str:
    """The flow waiting for the next message, or "" when nothing is."""
    row = db.one(
        "SELECT state_json, updated_at FROM conversation_state WHERE person_id = ?", (person_id,)
    )
    if row is None:
        return ""
    try:
        state = json.loads(row["state_json"])
    except ValueError:
        return ""
    if state.get("paused"):
        return ""
    if state.get("armed") == "evening":
        # the evening check-in arms "what changed?" so a typed answer lands in it, but nobody
        # has engaged with it yet: a Change tap or /measure must not be told to answer it first
        return ""
    flow = str(state.get("flow") or "")
    ttl = FLOW_TTL_S.get(flow)
    if ttl is not None and row["updated_at"]:
        age = (clock.now_utc() - parse_stamp(str(row["updated_at"]))).total_seconds()
        if age > ttl:
            return ""
    return flow


#: flows whose half-finished state is worth protecting from another flow arming over it.
#: `conversation_state` holds ONE row per person, so arming flow B destroys flow A's answers -
#: and for the interview that is twenty-odd typed answers and the whole thing from the start.
#:
#: Short questions can be asked again without protecting their flow. Refusing on those turned
#: a small question into a lock: an unanswered morning prompt could refuse an evening button.
#: A tap on a button is not competing for the next message the way another question would be.
PROTECTED_FLOWS = frozenset({"onboarding"})


def displaceable(db: Db, person_id: int, flow: str, *, clock: Clock = DEFAULT_CLOCK) -> str:
    """The open flow this one may arm over, "" if none is open, or the protected one to refuse."""
    open_ = open_flow(db, person_id, clock=clock)
    return open_ if open_ and open_ != flow else ""


def busy_with_other_than(
    db: Db, person_id: int, flow: str, *, clock: Clock = DEFAULT_CLOCK
) -> bool:
    """Is another flow mid-question? A paused or expired one never counts."""
    open_ = open_flow(db, person_id, clock=clock)
    return bool(open_) and open_ != flow


__all__ = [
    "FLOW_TTL_S",
    "PROTECTED_FLOWS",
    "asks_for_something_else",
    "busy_with_other_than",
    "displaceable",
    "food_answer_is_something_else",
    "looks_like_a_question",
    "open_flow",
]
