"""Consent-gated model calls with bounded retries and manual fallback."""

from __future__ import annotations

import datetime as dt
import hashlib
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from .. import log
from ..clock import DEFAULT_CLOCK, Clock, parse_stamp, stamp
from ..config import Budget, Config
from ..db import Db
from ..errors import (
    ModelError,
    ModelInvalid,
    ModelToolUnsupported,
    ModelUnavailable,
    QuotaExhausted,
)
from ..http import TransportError
from . import schemas

if TYPE_CHECKING:
    from .tools import BoundTools

logger = log.get("tiffin.model")

Messages = list[dict[str, Any]]


class ModelTimeout(ModelError):
    """The backend did not answer inside the task's budget."""


class BudgetExceeded(ModelInvalid):
    """`--max-budget-usd` tripped after the turn ran: the answer is discarded and the cost is
    spent, so a retry at the same budget cannot succeed (S3). Never retried."""


class ModelRepairExhausted(ModelInvalid):
    """An API provider has already used its single JSON repair retry."""


@dataclass
class ModelResult:
    text: str
    data: Any
    model_used: str
    backend: str
    cost_usd: float | None = None
    duration_ms: int = 0
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    raw: Any = None


class Backend(Protocol):
    name: str

    def complete_json(
        self, task: str, prompt: str, schema: dict[str, Any], budget: Budget
    ) -> ModelResult: ...

    def converse(
        self,
        task: str,
        messages: Messages,
        tools: BoundTools | None,
        person_id: int,
        budget: Budget,
    ) -> ModelResult: ...


def consent_granted(
    db: Db, person_id: int, kind: str = "model_processing", *, provider: str = "claude-cli"
) -> bool:
    """A grant applies only to the provider named when the person gave it."""
    model_kind = kind in ("model_processing", "anthropic_processing")
    if model_kind:
        row = db.one(
            "SELECT granted, wording_version FROM consents WHERE person_id = ? "
            "AND kind IN ('model_processing', 'anthropic_processing') ORDER BY id DESC LIMIT 1",
            (person_id,),
        )
    else:
        row = db.one(
            "SELECT granted, wording_version FROM consents WHERE person_id = ? AND kind = ? "
            "ORDER BY id DESC LIMIT 1",
            (person_id, kind),
        )
    if row is None or int(row["granted"]) != 1:
        return False
    if not model_kind:
        return True
    wording = str(row["wording_version"])
    # v3 grants were given under the older wording and stay honoured; v4 is the wording that
    # names the machine and the services in between. Neither is re-asked.
    return wording in (f"v3:{provider}", f"v4:{provider}") or (
        provider == "claude-cli" and wording in ("1", "v1", "v2")
    )


def prompt_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class QuotaState:
    """Persist a provider's quota backoff without blocking another provider."""

    def __init__(
        self, db: Db, clock: Clock, initial_min: int, max_min: int, provider: str = "claude"
    ):
        self.db = db
        self.clock = clock
        prefix = "claude" if provider == "claude" else f"model_{provider}"
        self.KEY_UNTIL = f"{prefix}_blocked_until"
        self.KEY_MIN = f"{prefix}_block_min"
        self.initial_min = max(1, int(initial_min))
        self.max_min = max(self.initial_min, int(max_min))

    def blocked_until(self) -> dt.datetime | None:
        raw = self.db.state_get(self.KEY_UNTIL)
        if not raw:
            return None
        try:
            return parse_stamp(raw)
        except ValueError:
            return None

    def blocked(self) -> bool:
        until = self.blocked_until()
        return until is not None and until > self.clock.now_utc()

    def block(self) -> int:
        """Start (or lengthen) a block; returns its length in minutes."""
        previous = int(self.db.state_get(self.KEY_MIN, "0") or 0)
        minutes = self.initial_min if previous <= 0 else min(self.max_min, previous * 2)
        until = self.clock.now_utc() + dt.timedelta(minutes=minutes)
        self.db.state_set(self.KEY_UNTIL, stamp(until))
        self.db.state_set(self.KEY_MIN, str(minutes))
        return minutes

    def clear(self) -> None:
        """A successful primary call ends the doubling; the next exhaustion starts small."""
        if self.db.state_get(self.KEY_MIN, "0") != "0":
            self.db.state_set(self.KEY_MIN, "0")


class Gateway:
    def __init__(
        self,
        primary: Backend | None,
        db: Db,
        consent_check: Callable[[int], bool],
        clock: Clock = DEFAULT_CLOCK,
        *,
        budgets: Callable[[str], Budget],
        block_initial_min: int = 30,
        block_max_min: int = 300,
    ):
        self.primary = primary
        self.db = db
        self.consent_check = consent_check
        self.clock = clock
        self.budgets = budgets
        self.quota = QuotaState(
            db,
            clock,
            block_initial_min,
            block_max_min,
            primary.name if primary is not None else "claude",
        )

    @classmethod
    def from_config(
        cls,
        config: Config,
        primary: Backend | None,
        db: Db,
        consent_check: Callable[[int], bool] | None = None,
        clock: Clock = DEFAULT_CLOCK,
    ) -> Gateway:
        models = config.section("models")
        return cls(
            primary,
            db,
            consent_check or (lambda pid: consent_granted(db, pid, provider=config.model_scope())),
            clock,
            budgets=config.budget,
            block_initial_min=int(models.get("claude_block_initial_min", 30)),
            block_max_min=int(models.get("claude_block_max_min", 300)),
        )

    # -- public ------------------------------------------------------------------------
    def json_task(self, task: str, prompt: str, schema_name: str, *, person_id: int) -> ModelResult:
        budget = self.budgets(task)
        schema = schemas.load(schema_name)

        def call(backend: Backend) -> ModelResult:
            result = backend.complete_json(task, prompt, schema, budget)
            schemas.validate_data(schema, result.data, name=schema_name)
            return result

        return self._run(
            task, person_id, call, prompt_len=len(prompt), digest=prompt_digest(prompt)
        )

    def converse(
        self,
        task: str,
        messages: Messages,
        *,
        person_id: int,
        tools: BoundTools | None = None,
    ) -> ModelResult:
        budget = self.budgets(task)
        joined = "\n".join(str(m.get("content") or "") for m in messages)

        def call(backend: Backend) -> ModelResult:
            return backend.converse(task, messages, tools, person_id, budget)

        return self._run(
            task, person_id, call, prompt_len=len(joined), digest=prompt_digest(joined)
        )

    def primary_route(self, person_id: int) -> str | None:
        """None when the primary may be used; otherwise why it may not."""
        if self.primary is None:
            return "no_primary"
        if not self.consent_check(person_id):
            return "no_consent"
        if self.quota.blocked():
            return "blocked"
        return None

    # -- routing -----------------------------------------------------------------------
    def _run(
        self,
        task: str,
        person_id: int,
        call: Callable[[Backend], ModelResult],
        *,
        prompt_len: int,
        digest: str,
    ) -> ModelResult:
        reason = self.primary_route(person_id)
        extra: Mapping[str, Any] = {"event": "model_call", "person_id": person_id}
        if reason is None and self.primary is not None:
            try:
                result = self._attempt(self.primary, call, task)
            except QuotaExhausted as e:
                minutes = self.quota.block()
                logger.warning(
                    "task=%s primary=%s quota exhausted; blocked for %d min type=%s",
                    task,
                    self.primary.name,
                    minutes,
                    type(e).__name__,
                    extra={"event": "quota_exhausted", "person_id": person_id},
                )
                reason = "quota_exhausted"
            except (ModelError, TransportError) as e:
                logger.warning(
                    "task=%s primary=%s failed type=%s",
                    task,
                    self.primary.name,
                    type(e).__name__,
                    extra={"event": "primary_failed", "person_id": person_id},
                )
                if isinstance(e, ModelToolUnsupported):
                    raise
                reason = type(e).__name__
            else:
                self.quota.clear()
                self._log_result(task, result, prompt_len, digest, extra)
                return result

        # the only model there is could not be used: say which reason, and let the caller degrade
        raise ModelUnavailable(f"{task}: model unavailable ({reason})")

    def _attempt(
        self, backend: Backend, call: Callable[[Backend], ModelResult], task: str
    ) -> ModelResult:
        """One call, and a second one after a timeout or an unusable answer."""
        t0 = time.monotonic()
        try:
            result = call(backend)
        except BudgetExceeded, ModelRepairExhausted:
            raise
        except (ModelTimeout, ModelInvalid) as e:
            logger.info(
                "task=%s backend=%s retry after %s",
                task,
                backend.name,
                type(e).__name__,
                extra={"event": "model_retry"},
            )
            result = call(backend)
        if not result.model_used:
            result.model_used = backend.name
        if not result.backend:
            result.backend = backend.name
        if not result.duration_ms:
            result.duration_ms = int((time.monotonic() - t0) * 1000)
        return result

    @staticmethod
    def _log_result(
        task: str, result: ModelResult, prompt_len: int, digest: str, extra: Mapping[str, Any]
    ) -> None:
        logger.info(
            "task=%s backend=%s model=%s prompt_len=%d prompt_sha=%s duration_ms=%d "
            "cost_usd=%s tool_calls=%d",
            task,
            result.backend,
            result.model_used,
            prompt_len,
            digest,
            result.duration_ms,
            f"{result.cost_usd:.4f}" if result.cost_usd is not None else "unknown",
            len(result.tool_calls),
            extra=dict(extra),
        )
