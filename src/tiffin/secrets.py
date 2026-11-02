"""Secrets come from environment variables or files under TIFFIN_HOME/secrets."""

from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path

from .errors import ConfigError

FILES: dict[str, str] = {
    "telegram_token": "telegram-token.txt",
    "telegram_token_dev": "telegram-token-dev.txt",
    "icu_key_owner": "icu-key-owner.txt",
    "icu_key_partner": "icu-key-partner.txt",
    "web_hmac_key": "web-hmac-key",
    "web_csrf_key": "web-csrf-key",
    "model_api_key": "model-api-key.txt",
}


class Secrets:
    def __init__(self, directory: Path | str, environ: Mapping[str, str] | None = None):
        self.directory = Path(directory)
        self.environ = os.environ if environ is None else environ

    def path(self, name: str) -> Path:
        try:
            return self.directory / FILES[name]
        except KeyError:
            raise ConfigError(f"unknown secret name: {name}") from None

    def exists(self, name: str) -> bool:
        """Present AND usable. A size check alone called a file holding one newline present, so
        `doctor` printed "secrets ok" and `tiffin run` started, and the daemon then died on the
        same secret from inside a poller thread - which is exactly what the startup check was
        written to prevent. Whatever `get` would refuse, this refuses too."""
        return self.optional(name) is not None

    def optional(self, name: str) -> str | None:
        self.path(name)
        value = self.environ.get(f"TIFFIN_{name.upper()}", "").strip()
        if value:
            return value
        p = self.path(name)
        if not p.is_file():
            return None
        # utf-8-sig, because a token saved by Notepad or by Windows PowerShell 5.1 carries a
        # byte order mark. U+FEFF is a format character, not whitespace, so strip() left it on
        # the front of the token: the file looked right, the SHA-256 nobody is allowed to print
        # differed, and the only symptom was a 401 from Telegram.
        value = p.read_text(encoding="utf-8-sig").strip()
        return value or None

    def get(self, name: str) -> str:
        value = self.optional(name)
        if value is None:
            raise ConfigError(
                f"secret missing or empty: set TIFFIN_{name.upper()} or create "
                f"{self.path(name).name} under TIFFIN_HOME/secrets"
            )
        return value

    def write(self, name: str, value: str) -> None:
        """Write a secret without placing it in config or a world-readable file."""
        cleaned = value.strip()
        if not cleaned:
            raise ConfigError(f"{name} cannot be empty")
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.directory.chmod(0o700)
        path = self.path(name)
        descriptor, temporary = tempfile.mkstemp(prefix=".secret-", dir=self.directory)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(cleaned + "\n")
            os.replace(temporary, path)
            path.chmod(0o600)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def sha256(self, name: str) -> str:
        return hashlib.sha256(self.get(name).encode("utf-8")).hexdigest()

    def missing(self, names: list[str]) -> list[str]:
        return [n for n in names if not self.exists(n)]

    def telegram_token(self, env: str) -> str:
        return self.get("telegram_token" if env == "prod" else "telegram_token_dev")

    def icu_key(self, slug: str) -> str | None:
        return self.optional(f"icu_key_{slug}")
