"""The online doctor probes a provider once without personal data."""

from __future__ import annotations

from unittest.mock import patch

from tiffin.app import App
from tiffin.config import load
from tiffin.doctor import run_checks
from tiffin.model.gateway import ModelResult
from tiffin.secrets import Secrets


class ProbeProvider:
    name = "openai"

    def __init__(self) -> None:
        self.calls = []

    def complete_json(self, task, prompt, schema, budget):
        self.calls.append((task, prompt, schema, budget))
        return ModelResult(
            text='{"ok":true}', data={"ok": True}, model_used="example", backend="openai"
        )


def test_online_doctor_makes_one_small_provider_call(tmp_path) -> None:
    config = load(root=tmp_path, environ={})
    app = App(config, Secrets(tmp_path / "secrets", {}))
    provider = ProbeProvider()
    with (
        patch("tiffin.doctor.configured_provider", return_value=provider),
        patch("tiffin.doctor._listening", return_value=False),
    ):
        checks = run_checks(app)
    model = next(check for check in checks if check.name == "model")
    assert model.status == "ok" and "billed" in model.detail
    assert len(provider.calls) == 1
    task, prompt, schema, budget = provider.calls[0]
    assert task == "doctor" and "ok" in prompt and schema["required"] == ["ok"]
    assert budget.timeout_s == 20
