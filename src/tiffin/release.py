"""Which release and commit this is, for the version endpoint. Never a secret, a path or a person.

The release id comes from `TIFFIN_RELEASE` (the image sets it from the build argument
`RELEASE_ID`), the commit from `TIFFIN_COMMIT` (`RELEASE_COMMIT`) or, failing that, from the
`git archive` stamp in `_stamp.py`. Anything that does not look like an id or a hex commit reads
as "unknown" rather than being echoed.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping

from . import _stamp

UNKNOWN = "unknown"
_RELEASE = re.compile(r"[0-9A-Za-z][0-9A-Za-z._-]{0,39}")
_COMMIT = re.compile(r"[0-9a-f]{7,40}")


def release(environ: Mapping[str, str] | None = None) -> str:
    env = os.environ if environ is None else environ
    value = env.get("TIFFIN_RELEASE", "").strip()
    return value if _RELEASE.fullmatch(value) else UNKNOWN


def commit(environ: Mapping[str, str] | None = None) -> str:
    env = os.environ if environ is None else environ
    for candidate in (env.get("TIFFIN_COMMIT", "").strip(), _stamp.COMMIT.strip()):
        if _COMMIT.fullmatch(candidate):
            return candidate
    return UNKNOWN


def info(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    return {"component": "tiffin", "release": release(environ), "commit": commit(environ)}
