"""Vendored front-end assets: the Plotly basic bundle pin (`static/VENDOR.md` is the human copy).

The page loads nothing from outside the box (CSP enforces it), so the bundle lives in the repo.
`tiffin doctor` compares the file on disk with these constants; a mismatch means the bundle was
replaced without re-pinning it here.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

STATIC_DIR = Path(__file__).with_name("static")
PLOTLY_VERSION = "3.7.0"
PLOTLY_FILE = f"plotly-basic-{PLOTLY_VERSION}.min.js"
PLOTLY_SHA256 = "c23b03591a6bdad0bd0f47a0c5c52305a5519812b73ce254be65cd841553c9c2"
PLOTLY_BYTES = 1_119_926
PLOTLY_SOURCE = f"https://cdn.plot.ly/{PLOTLY_FILE}"


def plotly_path() -> Path:
    return STATIC_DIR / PLOTLY_FILE


def check_plotly(path: Path | None = None) -> tuple[bool, str]:
    """(ok, detail) for the doctor: the vendored bundle must match the pinned hash and size."""
    p = path or plotly_path()
    try:
        data = p.read_bytes()
    except OSError as e:
        return False, f"{p.name}: {e}"
    digest = hashlib.sha256(data).hexdigest()
    if digest != PLOTLY_SHA256 or len(data) != PLOTLY_BYTES:
        return False, (
            f"{p.name}: sha256 {digest[:12]} / {len(data)} B, "
            f"pinned {PLOTLY_SHA256[:12]} / {PLOTLY_BYTES} B"
        )
    return True, f"{p.name} {len(data)} B sha256 {digest[:12]}"
