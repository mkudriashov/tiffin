"""One exception family. Callers catch the base class; specific subclasses carry the route."""

from typing import Any


class TiffinError(Exception):
    """Base for everything Tiffin raises deliberately."""


class ConfigError(TiffinError):
    """Bad or missing configuration / secret. CLI exit code 2."""


class LockConflict(TiffinError):
    """A second prod instance, or another poller on the same token. CLI exit code 3."""


class ModelError(TiffinError):
    """Base for model gateway failures."""


class QuotaExhausted(ModelError):
    """claude -p reports the subscription limit is hit; block the primary for a while."""


class ModelInvalid(ModelError):
    """The model answered but not usably (schema, budget, turns)."""


class ModelUnavailable(ModelError):
    """Neither backend produced an answer; the caller degrades to a template."""


class ModelToolUnsupported(ModelError):
    """The configured model rejected a chat request containing tools."""


class Truncated(ModelInvalid):
    """finish_reason == 'length' — the answer was cut off."""


class IntegrationError(TiffinError):
    """Base for the outside world going wrong."""


class IcuError(IntegrationError):
    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class IcuRateLimited(IcuError):
    pass


class TelegramError(IntegrationError):
    pass


class TelegramConflict(TelegramError):
    """HTTP 409: another getUpdates poller holds this token."""


class Infeasible(TiffinError):
    """The solver cannot meet the day's constraints; names the binding one."""


class Lopsided(Infeasible):
    """The day fits, but only by serving some dishes at a fraction of a portion and others at
    well over one. `spread` is the day's largest multiplier over its smallest."""

    def __init__(self, message: str, spread: float) -> None:
        super().__init__(message)
        self.spread = spread


class CarbsShort(Infeasible):
    """The day meets every other target but not the carbohydrate floor a person's own menu is
    held to - and the same dishes fit without that floor, so the answer is still a menu. The
    caller sets `answer` to what the model said, to keep it if a second try does no better."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.answer: tuple[Any, str | None] | None = None


class NothingToBuild(TiffinError):
    """`/cook` answered without calling a model: no cook days in range, or a menu is already
    there and nobody asked to replace it. The message is written for the person, and the
    caller must not spend its cooldown on it - one of these answers is "send /cook replace",
    and a cooldown armed against the command just asked for cannot be waited out, because
    every bare /cook re-arms it."""
