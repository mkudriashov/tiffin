"""Optional product readers. The shopping core depends only on this interface."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol


class RetailerReader(Protocol):
    def read(
        self, retailer: str, search_url: str, foods: Sequence[Mapping[str, Any]]
    ) -> tuple[dict[str, Any], Mapping[str, Any]]: ...


def reader_for(config: Any, state_dir: Path) -> RetailerReader:
    """Load browser code only after the operator enables the Chrome reader."""
    from .claude_chrome import ClaudeChromeReader  # noqa: PLC0415

    return ClaudeChromeReader(config, state_dir)
