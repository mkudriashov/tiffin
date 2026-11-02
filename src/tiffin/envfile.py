"""`TIFFIN_<NAME>_FILE` names a file holding the value of `TIFFIN_<NAME>`.

This is the container-secrets convention (for example `TIFFIN_TELEGRAM_TOKEN_FILE` set to
`/run/secrets/telegram_token`). A plain `TIFFIN_<NAME>` variable wins over the file. A file that
is named but cannot be read is an error that shows the path and never the contents; an empty
file is the same as no value.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from .errors import ConfigError

SUFFIX = "_FILE"


def with_file_values(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """A copy of the environment with each `TIFFIN_<NAME>_FILE` read into `TIFFIN_<NAME>`."""
    env = dict(os.environ if environ is None else environ)
    for key, named in list(env.items()):
        if not (key.startswith("TIFFIN_") and key.endswith(SUFFIX)):
            continue
        target = key[: -len(SUFFIX)]
        if target == "TIFFIN" or env.get(target, "").strip() or not named.strip():
            continue
        try:
            value = Path(named.strip()).read_text(encoding="utf-8-sig").strip()
        except OSError as exc:
            raise ConfigError(
                f"{key} names {named.strip()}, which cannot be read ({type(exc).__name__}); "
                "check it exists and the tiffin user may read it"
            ) from None
        if value:
            env[target] = value
    return env
