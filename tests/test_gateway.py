"""Gateway routing: consent, quota block with doubling and switch-back, retries, model_used.

Whatever stops `claude -p` from answering is a
ModelUnavailable naming the reason, and the caller degrades.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import cast

import pytest

from tiffin.clock import FrozenClock, parse_stamp
from tiffin.config import Budget
from tiffin.db import Db
from tiffin.errors import (
    ModelInvalid,
    ModelToolUnsupported,
    ModelUnavailable,
    QuotaExhausted,
    Truncated,
)
from tiffin.model.gateway import Gateway, ModelResult, ModelTimeout, QuotaState, consent_granted
from tiffin.model.tools import BoundTools
from tiffin.persons import PersonRegistry
from tiffin.telegram.handlers import Handlers
from tiffin.telegram.outbox import Outbox
from tiffin.telegram.poller import Inbound

from .fakes import ScriptedBackend

LINES = {"lines": ["Calories go up 100 kcal."]}
BUDGET = Budget(90, 1, 0.10)


def budgets(task: str) -> Budget:
    return BUDGET


def make(
    db: Db, clock: FrozenClock, primary: list | None, *, consent: bool = True
) -> tuple[Gateway, ScriptedBackend | None]:
    p = ScriptedBackend("claude", primary, model="claude-opus-4-1") if primary is not None else None
    gw = Gateway(p, db, lambda pid: consent, clock, budgets=budgets)
    return gw, p


def test_primary_is_used_with_consent_and_result_carries_model(fixture_db, frozen_clock) -> None:
    gw, p = make(fixture_db, frozen_clock, [LINES])
    r = gw.json_task("sunday_prose", "prompt", "sunday_prose", person_id=1)
    assert r.data == LINES and r.model_used == "claude-opus-4-1" and r.backend == "claude"
    assert p is not None and len(p.calls) == 1
    assert p.calls[0]["budget"] == BUDGET and p.calls[0]["prompt"] == "prompt"


def test_chat_tool_rejection_reaches_the_reply_handler(fixture_db, frozen_clock) -> None:
    gw, _ = make(fixture_db, frozen_clock, [ModelToolUnsupported("chat tools rejected")])
    with pytest.raises(ModelToolUnsupported, match="chat tools rejected"):
        gw.converse("chat", [{"role": "user", "content": "hello"}], person_id=1)


def test_without_consent_no_model_is_called(fixture_db, frozen_clock) -> None:
    gw, p = make(fixture_db, frozen_clock, [LINES], consent=False)
    with pytest.raises(ModelUnavailable, match="no_consent"):
        gw.json_task("sunday_prose", "prompt", "sunday_prose", person_id=2)
    assert p is not None and p.calls == [], "nothing about her reaches Anthropic without consent"


def test_consent_granted_reads_the_latest_consents_row(fixture_db, two_persons) -> None:
    partner = two_persons["partner"]
    assert consent_granted(fixture_db, partner) is False
    fixture_db.insert("consents", person_id=partner, granted=1, wording_version=1)
    assert consent_granted(fixture_db, partner) is True
    fixture_db.insert("consents", person_id=partner, granted=1, wording_version="v1")
    assert consent_granted(fixture_db, partner) is True
    fixture_db.insert("consents", person_id=partner, granted=0, wording_version="v1")
    assert consent_granted(fixture_db, partner) is False
    assert consent_granted(fixture_db, two_persons["owner"]) is False


def test_consent_is_bound_to_the_latest_named_provider(fixture_db, two_persons) -> None:
    person_id = two_persons["owner"]
    fixture_db.insert("consents", person_id=person_id, granted=1, wording_version="v3:anthropic")
    assert consent_granted(fixture_db, person_id, provider="anthropic")
    assert not consent_granted(fixture_db, person_id, provider="openai")
    assert not consent_granted(fixture_db, person_id, provider="claude-cli")
    fixture_db.insert("consents", person_id=person_id, granted=1, wording_version="v3:openai")
    assert consent_granted(fixture_db, person_id, provider="openai")
    assert not consent_granted(fixture_db, person_id, provider="anthropic")
    fixture_db.insert(
        "consents",
        person_id=person_id,
        kind="model_processing",
        granted=0,
        wording_version="v3:openai",
    )
    assert not consent_granted(fixture_db, person_id, provider="openai")


def test_provider_change_is_announced_once_on_next_interaction(fixture_db, two_persons) -> None:
    person_id = two_persons["owner"]
    fixture_db.insert(
        "consents",
        person_id=person_id,
        kind="anthropic_processing",
        granted=1,
        wording_version="v2",
    )

    class Replies:
        def __init__(self) -> None:
            self.messages: list[str] = []

        def reply(self, _person, message: str) -> None:
            self.messages.append(message)

    replies = Replies()
    handler = Handlers(fixture_db, cast(Outbox, replies), cast(PersonRegistry, object()))
    handler.model_provider = "openai"
    inbound = cast(
        Inbound, SimpleNamespace(person=SimpleNamespace(id=person_id), kind="text", text="hello")
    )
    handler._provider_notice(inbound)
    handler._provider_notice(inbound)
    assert len(replies.messages) == 1 and "/consent" in replies.messages[0]
    assert not consent_granted(fixture_db, person_id, provider="openai")


def test_compatible_endpoint_requires_its_own_grant(fixture_db, config, two_persons) -> None:
    person_id = two_persons["owner"]
    config.data["model"].update(
        provider="openai-compatible", model="local-example", base_url="http://localhost:11434/v1"
    )
    fixture_db.insert("consents", person_id=person_id, granted=1, wording_version="v2")
    gateway = Gateway.from_config(config, ScriptedBackend("openai-compatible", []), fixture_db)
    assert gateway.primary_route(person_id) == "no_consent"
    fixture_db.insert(
        "consents", person_id=person_id, granted=1, wording_version=f"v3:{config.model_scope()}"
    )
    assert gateway.primary_route(person_id) is None


def test_quota_exhausted_blocks_then_doubles_then_switches_back(fixture_db, frozen_clock) -> None:
    gw, p = make(
        fixture_db,
        frozen_clock,
        [QuotaExhausted("hit your limit"), QuotaExhausted("hit your limit"), LINES],
    )
    assert p is not None
    with pytest.raises(ModelUnavailable, match="quota_exhausted"):
        gw.json_task("sunday_prose", "prompt", "sunday_prose", person_id=1)
    until = parse_stamp(fixture_db.state_get("claude_blocked_until") or "")
    assert (until - frozen_clock.now_utc()).total_seconds() == 30 * 60
    assert fixture_db.state_get("claude_block_min") == "30"

    # Still blocked: the primary is not even asked.
    frozen_clock.advance(minutes=10)
    with pytest.raises(ModelUnavailable, match="blocked"):
        gw.json_task("sunday_prose", "prompt", "sunday_prose", person_id=1)
    assert len(p.calls) == 1 and gw.primary_route(1) == "blocked"

    # Block over, primary fails again: the block doubles.
    frozen_clock.advance(minutes=21)
    assert gw.primary_route(1) is None
    with pytest.raises(ModelUnavailable):
        gw.json_task("sunday_prose", "prompt", "sunday_prose", person_id=1)
    assert len(p.calls) == 2 and fixture_db.state_get("claude_block_min") == "60"

    # Block over, primary succeeds: the next exhaustion starts at 30 again.
    frozen_clock.advance(minutes=61)
    r = gw.json_task("sunday_prose", "prompt", "sunday_prose", person_id=1)
    assert r.backend == "claude" and fixture_db.state_get("claude_block_min") == "0"


def test_block_length_caps_at_the_configured_maximum(fixture_db, frozen_clock) -> None:
    q = QuotaState(fixture_db, frozen_clock, 30, 300)
    seen = [q.block() for _ in range(6)]
    assert seen == [30, 60, 120, 240, 300, 300]
    assert q.blocked() is True
    frozen_clock.advance(minutes=301)
    assert q.blocked() is False
    q.clear()
    assert q.block() == 30


def test_invalid_answer_is_retried_once_then_unavailable(fixture_db, frozen_clock) -> None:
    gw, p = make(fixture_db, frozen_clock, [ModelInvalid("schema"), Truncated("cut")])
    with pytest.raises(ModelUnavailable, match=r"model unavailable \(Truncated\)") as exc:
        gw.json_task("sunday_prose", "prompt", "sunday_prose", person_id=1)
    assert "cut" not in str(exc.value)
    assert p is not None and len(p.calls) == 2


def test_timeout_is_retried_once_and_the_retry_can_succeed(fixture_db, frozen_clock) -> None:
    gw, p = make(fixture_db, frozen_clock, [ModelTimeout("slow"), LINES])
    r = gw.json_task("sunday_prose", "prompt", "sunday_prose", person_id=1)
    assert r.backend == "claude" and p is not None and len(p.calls) == 2


def test_gateway_validates_the_data_against_the_schema(fixture_db, frozen_clock) -> None:
    gw, p = make(fixture_db, frozen_clock, [{"lines": []}, {"lines": ["x" * 201]}])
    with pytest.raises(ModelUnavailable):
        gw.json_task("sunday_prose", "prompt", "sunday_prose", person_id=1)
    assert p is not None and len(p.calls) == 2


def test_no_backend_at_all_is_model_unavailable(fixture_db, frozen_clock) -> None:
    gw, _ = make(fixture_db, frozen_clock, None)
    with pytest.raises(ModelUnavailable, match="no_primary"):
        gw.json_task("sunday_prose", "prompt", "sunday_prose", person_id=1)


def test_converse_passes_tools_and_person(fixture_db, frozen_clock, two_persons) -> None:
    reply = ModelResult(text="2100 kcal", data=None, model_used="claude-opus-4-1", backend="claude")
    gw, p = make(fixture_db, frozen_clock, [reply])
    tools = BoundTools(fixture_db, two_persons["owner"])
    msgs = [{"role": "user", "content": "targets?"}]
    r = gw.converse("chat", msgs, person_id=two_persons["owner"], tools=tools)
    assert r.text == "2100 kcal" and p is not None
    call = p.calls[0]
    assert call["kind"] == "converse" and call["tools"] is tools
    assert call["person_id"] == two_persons["owner"] and call["messages"] == msgs


def test_model_used_is_filled_when_a_backend_forgets_it(fixture_db, frozen_clock) -> None:
    bare = ModelResult(text="", data=LINES, model_used="", backend="")
    gw, _ = make(fixture_db, frozen_clock, [bare])
    r = gw.json_task("sunday_prose", "prompt", "sunday_prose", person_id=1)
    assert r.model_used == "claude" and r.backend == "claude"


def test_logs_carry_lengths_and_digests_never_the_prompt(fixture_db, frozen_clock, caplog) -> None:
    secret = "her weight is 61.2 kg and she ate a kebab"
    gw, _ = make(fixture_db, frozen_clock, [ModelInvalid("x"), LINES])
    with caplog.at_level(logging.INFO, logger="tiffin.model"):
        gw.json_task("sunday_prose", secret, "sunday_prose", person_id=1)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "kebab" not in text and "61.2" not in text
    assert f"prompt_len={len(secret)}" in text and "prompt_sha=" in text
    assert any(getattr(r, "event", None) == "model_retry" for r in caplog.records)
    assert any(getattr(r, "person_id", None) == 1 for r in caplog.records)


def test_from_config_reads_budgets_and_block_minutes(fixture_db, frozen_clock, config) -> None:
    gw = Gateway.from_config(config, ScriptedBackend("claude", [LINES]), fixture_db)
    assert gw.quota.initial_min == 30 and gw.quota.max_min == 300
    assert gw.budgets("menu_propose") == Budget(420, 3, None)
    assert gw.primary_route(1) == "no_consent"  # no consents row in the fixture DB
