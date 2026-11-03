"""Terminal output for people, plain text for everything else.

Marks (a tick, a cross and an exclamation mark), colour and aligned tables are for a person at a
terminal. Colour is used only when the stream is a terminal, `NO_COLOR` is unset or empty and
`TERM` is not `dumb`; the marks fall back to ASCII where the terminal cannot show them. A pipe, a
file, `docker exec` without `-t` or a CI log gets plain words, so whatever reads that output keeps
working: commands whose output a script reads choose their format with `Style.terminal`.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TextIO

_SGR = {"bold": "1", "dim": "2", "red": "31", "green": "32", "yellow": "33", "cyan": "36"}
#: what each status looks like: (mark, ASCII mark, colour)
_MARKS = {
    "ok": ("✓", "+", "green"),
    "warn": ("!", "!", "yellow"),
    "fail": ("✗", "x", "red"),
    "info": ("·", "-", "dim"),
}
#: doctor's own words for a status, kept for its plain output
STATUS_ALIASES = {"red": "fail", "pass": "ok"}
_UNICODE_SAMPLE = "✓✗·•…"


def is_terminal(stream: object) -> bool:
    try:
        return bool(stream.isatty())  # type: ignore[attr-defined]
    except AttributeError, ValueError, OSError:
        return False


def _can_encode(stream: object, sample: str) -> bool:
    encoding = getattr(stream, "encoding", None) or "ascii"
    try:
        sample.encode(encoding)
    except UnicodeEncodeError, LookupError:
        return False
    return True


def _windows_escapes(stream: object) -> bool:
    """Switch a Windows console to understand colour codes. True when it does (or this is not
    Windows); False for an old console that cannot, which then gets no colour."""
    if sys.platform != "win32":
        return True
    try:
        import ctypes  # noqa: PLC0415
        import msvcrt  # noqa: PLC0415

        handle = msvcrt.get_osfhandle(stream.fileno())  # type: ignore[attr-defined]
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        enable_escapes = 0x0004
        if mode.value & enable_escapes:
            return True
        return bool(kernel32.SetConsoleMode(handle, mode.value | enable_escapes))
    except Exception:
        return False


@dataclass(frozen=True)
class Style:
    """How to write to one stream. `terminal` says a person is reading it."""

    terminal: bool = False
    colour: bool = False
    unicode: bool = False

    @classmethod
    def detect(
        cls, stream: TextIO | None = None, environ: Mapping[str, str] | None = None
    ) -> Style:
        stream = sys.stdout if stream is None else stream
        env = os.environ if environ is None else environ
        terminal = is_terminal(stream)
        dumb = env.get("TERM", "") == "dumb"
        unicode = terminal and not dumb and _can_encode(stream, _UNICODE_SAMPLE)
        colour = terminal and not dumb and not env.get("NO_COLOR") and _windows_escapes(stream)
        return cls(terminal=terminal, colour=colour, unicode=unicode)

    # -- words -----------------------------------------------------------------------------
    def paint(self, text: str, *styles: str) -> str:
        if not self.colour or not styles or not text:
            return text
        codes = ";".join(_SGR[s] for s in styles)
        return f"\x1b[{codes}m{text}\x1b[0m"

    def bold(self, text: str) -> str:
        return self.paint(text, "bold")

    def dim(self, text: str) -> str:
        return self.paint(text, "dim")

    def heading(self, text: str) -> str:
        return self.paint(text, "bold", "cyan")

    @property
    def dot(self) -> str:
        """The separator in "Step 3 of 8 · Telegram"."""
        return "·" if self.unicode else "-"

    @property
    def ellipsis(self) -> str:
        return "…" if self.unicode else "..."

    def step(self, number: int, total: int, title: str) -> str:
        return self.heading(f"Step {number} of {total} {self.dot} {title}")

    # -- marks -----------------------------------------------------------------------------
    def mark(self, status: str) -> str:
        """The tick, cross, ! or dot for ok, fail, warn or info, coloured when allowed."""
        status = STATUS_ALIASES.get(status, status)
        symbol, ascii_symbol, colour = _MARKS[status]
        return self.paint(symbol if self.unicode else ascii_symbol, colour)

    def line(self, status: str, text: str) -> str:
        """`text` after its mark, for a one-line result: "✓ Connected to @example_bot"."""
        return f"{self.mark(status)} {text}"

    def mask(self, secret: str) -> str:
        """Enough of a secret to recognise it, never enough to use: the last four characters
        of a long one, nothing of a short one."""
        hidden = "•" * 4 if self.unicode else "*" * 4
        cleaned = secret.strip()
        return f"{hidden}{cleaned[-4:]}" if len(cleaned) >= 16 else hidden


PLAIN = Style()


def table(
    rows: Sequence[Sequence[str]],
    *,
    indent: int = 2,
    gap: int = 2,
    paint: Callable[[int, int, str], str] | None = None,
) -> list[str]:
    """Rows as aligned columns. Widths are measured on the plain text, so `paint(row, column,
    padded_text)` may colour a cell afterwards without breaking the alignment. The last column
    is never padded."""
    if not rows:
        return []
    columns = max(len(r) for r in rows)
    widths = [max((len(r[c]) for r in rows if c < len(r)), default=0) for c in range(columns)]
    out = []
    for row_index, row in enumerate(rows):
        cells = []
        for column, cell in enumerate(row):
            padded = cell if column == len(row) - 1 else cell.ljust(widths[column])
            cells.append(paint(row_index, column, padded) if paint else padded)
        out.append((" " * indent + (" " * gap).join(cells)).rstrip())
    return out


def plural(count: int, word: str, many: str | None = None) -> str:
    return f"{count} {word if count == 1 else many or word + 's'}"
