"""Read-only Claude-in-Chrome retailer adapter."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ...config import REPO_ROOT
from ..chrome_job import ChromeJob


class ClaudeChromeReader:
    def __init__(self, config: Any, state_dir: Path):
        self.config = config
        self.state_dir = state_dir

    def read(
        self, retailer: str, search_url: str, foods: Sequence[Mapping[str, Any]]
    ) -> tuple[dict[str, Any], Mapping[str, Any]]:
        job = ChromeJob(
            REPO_ROOT / "claude-jobs" / "shop-reader",
            self.config.budget("shop_refresh"),
            model=self.config.get(
                "models.chrome_model", self.config.get("models.claude_model", "opus")
            ),
            profile=self.config.get("chrome.profile_directory", "Default"),
            state_dir=self.state_dir,
        )
        prompt = json.dumps(
            {"retailer": retailer, "search_url": search_url, "foods": foods},
            indent=1,
            ensure_ascii=False,
        )
        return job.run_json(prompt)
