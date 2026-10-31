"""`/questions`: what a person wants from Tiffin, asked one question at a time (the owner, 18
Oct
2026: "ask her at least 50 questions ... she answers and then you build").

Run in tests/test_menu.py's world, through the poller as a person would: taps and typed replies.
"""

from __future__ import annotations

import contextlib
import json

from tiffin.telegram import convstate, interview

from .test_menu import world  # noqa: F401


def _rows(db, pid) -> list[tuple[str, str | None]]:
    return [
        (str(r["step"]), json.loads(str(r["answer_json"]))["answer"])
        for r in db.all(
            "SELECT step, answer_json FROM onboarding_answers WHERE person_id = ? ORDER BY id",
            (pid,),
        )
    ]


def test_the_questions_are_enough_and_well_formed() -> None:
    qs = interview.QUESTIONS
    assert len(qs) >= 50
    assert len({q.key for q in qs}) == len(qs)
    for n, q in enumerate(qs, start=1):
        # Telegram's callback data limit, and no emoji anywhere (CLAUDE.md)
        for row in interview.keyboard(n, q)["inline_keyboard"]:
            for button in row:
                assert len(button["callback_data"].encode()) <= 64
                assert button["text"].isascii(), button["text"]
        assert q.text.isascii() and q.text.endswith(("?", "Skip.", "few.")), q.text
        assert len(q.options) <= 4
    # the opening says who reads the answers, and how to stop and carry on
    assert "configured model reads them" in interview.INTRO and "/questions" in interview.INTRO
    assert len(interview.INTRO.splitlines()) <= 10


def test_the_opening_goes_once(world, config, tmp_path, frozen_clock) -> None:  # noqa: F811
    _pid, db, tg, *_ = world
    from tiffin.app import App
    from tiffin.secrets import Secrets

    secrets = tmp_path / "s"  # the world's own token, as `tiffin interview` reads the app's
    app = App(config, Secrets(secrets), clock=frozen_clock, transport=tg)
    person = app.registry(db).require("owner")
    sent = len(tg.sent)
    first = interview.send_intro(app.outbox(db), person)
    again = interview.send_intro(app.outbox(db), person)
    assert first.status == "sent" and again.status == "sent"
    assert len(tg.sent) == sent + 1  # the second is the first, not another message
    assert "k:iv:go" in json.dumps(tg.sent[-1].get("reply_markup"))


def test_tap_type_pause_and_carry_on(world, frozen_clock) -> None:  # noqa: F811
    pid, db, _tg, _gw, say, tap, _run = world
    text = tap("k:iv:go")
    assert text.startswith(f"<b>Question 1 of {len(interview.QUESTIONS)}</b>")
    # a tap is the answer, and the next question follows
    text = tap("k:iv:a:1:1:0")
    assert _rows(db, pid) == [(f"{interview.ROUND}:aim", interview.QUESTIONS[0].options[0])]
    assert text.startswith("<b>Question 2 of") and "Type your answer" in text
    assert convstate.open_flow(db, pid, clock=frozen_clock) == "interview"
    # a typed question takes the next message, then lets go once answered
    text = say("Less time deciding what to eat")
    assert _rows(db, pid)[-1] == (f"{interview.ROUND}:works", "Less time deciding what to eat")
    assert text.startswith("<b>Question 3 of")
    # Pause: nothing typed after it is taken as an answer
    text = tap("k:iv:p")
    assert "Paused" in text and convstate.open_flow(db, pid, clock=frozen_clock) == ""
    before = len(_rows(db, pid))
    say("hello")
    assert len(_rows(db, pid)) == before
    # /questions carries on from the first unanswered, and Skip records a skip
    text = say("/questions")
    assert text.startswith("<b>Question 3 of")
    text = tap("k:iv:s:1:3")
    assert _rows(db, pid)[-1] == (f"{interview.ROUND}:annoys", None)
    assert text.startswith("<b>Question 4 of")


def test_a_bad_or_old_tap_is_not_an_answer(world) -> None:  # noqa: F811
    pid, db, tg, _gw, _say, tap, _run = world
    for data in ("k:iv:a:1:999:0", "k:iv:a:1:1:9", "k:iv:a:x:1:0", "k:iv:a:9:1:0", "k:iv:s"):
        # nothing sent: the tap is told it no longer works, by toast
        with contextlib.suppress(IndexError):
            tap(data)
    assert _rows(db, pid) == []
    assert tg.sent == []


def test_a_typed_question_lets_go_after_a_day(world, frozen_clock) -> None:  # noqa: F811
    pid, db, _tg, _gw, say, tap, _run = world
    tap("k:iv:go")
    tap("k:iv:a:1:1:0")  # now on a typed question
    frozen_clock.advance(hours=25)
    before = len(_rows(db, pid))
    say("hello")
    assert len(_rows(db, pid)) == before


def test_every_question_answered_ends_with_thanks(world, frozen_clock) -> None:  # noqa: F811
    pid, db, _tg, _gw, say, tap, _run = world
    tap("k:iv:go")
    text = ""
    for n, q in enumerate(interview.QUESTIONS, start=1):
        text = tap(f"k:iv:a:1:{n}:0") if q.options else say(f"answer {n}")
    assert "That is every question" in text
    said = interview.answered(db, pid)
    assert len(said) == len(interview.QUESTIONS)
    assert convstate.open_flow(db, pid, clock=frozen_clock) == ""
    assert "answered every question" in say("/questions")


def test_the_second_round_explains_each_piece_then_asks(world) -> None:  # noqa: F811
    pid, db, _tg, _gw, _say, tap, _run = world
    plan = interview.ROUNDS[1]
    for n, q in enumerate(plan.questions, start=1):
        text = interview.question_text(n, q, 2)
        # an example and a question in one message, inside the ten-line rule, no emoji
        assert q.explain and len(text.splitlines()) <= 10, q.key
        assert all(ord(c) < 0x2000 for c in q.explain + q.text), q.key
        for row in interview.keyboard(n, q, 2)["inline_keyboard"]:
            for button in row:
                assert len(button["callback_data"].encode()) <= 64
    # the first round done; the second is never asked before its opening went to them
    for q in interview.QUESTIONS:
        interview.record(db, pid, q, None, None)
    assert interview.next_question(db, pid) is None
    db.insert(
        "messages_out",
        person_id=pid,
        chat_id=1,
        kind="interview",
        due_key=f"{plan.key}:intro",
        text_html="x",
        status="sent",
    )
    text = tap("k:iv:go")
    assert text.startswith(f"<b>1 of {len(plan.questions)}</b>")
    assert plan.questions[0].explain.split(",")[0] in text
    text = tap("k:iv:a:2:1:0")
    assert interview.answered(db, pid, plan) == {"p_sunday": plan.questions[0].options[0]}
    assert text.startswith(f"<b>2 of {len(plan.questions)}</b>")
    # the first round's old buttons, with no round in them, still answer the first round
    tap("k:iv:a:1:2")
    assert interview.answered(db, pid)["aim"] == interview.QUESTIONS[0].options[2]


def test_every_later_round_explains_each_question_inside_the_rules() -> None:
    for r, round_ in enumerate(interview.ROUNDS[1:], start=2):
        assert len(round_.intro.splitlines()) <= 10
        for n, q in enumerate(round_.questions, start=1):
            text = interview.question_text(n, q, r)
            assert q.explain and q.text.endswith("?") and len(text.splitlines()) <= 10, q.key
            assert all(ord(c) < 0x2000 for c in q.explain + q.text), q.key
            for row in interview.keyboard(n, q, r)["inline_keyboard"]:
                for button in row:
                    assert len(button["callback_data"].encode()) <= 64
    keys = [f"{x.key}:{q.key}" for x in interview.ROUNDS for q in x.questions]
    assert len(keys) == len(set(keys))
