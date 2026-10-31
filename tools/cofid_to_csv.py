"""CoFID 2021 workbook -> data/cofid/cofid_2021_proximates.csv (dev-only; needs openpyxl).

Source: McCance and Widdowson's Composition of Foods Integrated Dataset 2021 (gov.uk, Open
Government Licence v3 — see data/cofid/LICENCE-OGL3.txt). The workbook itself is gitignored;
the CSV this writes is committed.

Workbook layout: sheet '1.3 Proximates'; row 1 = column titles, row 2 =
CoFID field codes, row 3 = descriptions, data from row 4. Columns are located by the row-2 field
code (KCALS, PROT, FAT, CHO, AOACFIB) and the row-1 titles 'Food Code' / 'Food Name', never by
position, so a re-ordered column fails loudly instead of shifting every number.

Values: 'Tr' (trace) -> 0; 'N' (present in significant quantity, amount unknown) -> empty;
blank -> empty. An empty fibre blocks approval downstream (catalogue.build). A duplicate food code
keeps its first row and is reported.

    .venv\\Scripts\\python.exe tools\\cofid_to_csv.py
    .venv\\Scripts\\python.exe tools\\cofid_to_csv.py --find "chicken, breast" --find "oats"
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import openpyxl

from tiffin.clock import DEFAULT_CLOCK

REPO = Path(__file__).resolve().parents[1]
DEFAULT_XLSX = REPO / "data" / "cofid" / "cofid_2021.xlsx"
DEFAULT_CSV = REPO / "data" / "cofid" / "cofid_2021_proximates.csv"
SHEET = "1.3 Proximates"
HEADER_ROWS = 3

OUT_COLUMNS = ("food_code", "food_name", "kcal", "protein_g", "fat_g", "carb_g", "fibre_aoac_g")
# output column -> (row-1 title | None, row-2 field code | None); one of the two locates it
LOCATORS: dict[str, tuple[str | None, str | None]] = {
    "food_code": ("Food Code", None),
    "food_name": ("Food Name", None),
    "kcal": (None, "KCALS"),
    "protein_g": (None, "PROT"),
    "fat_g": (None, "FAT"),
    "carb_g": (None, "CHO"),
    "fibre_aoac_g": (None, "AOACFIB"),
}
TRACE = "Tr"
UNKNOWN = "N"


@dataclass
class Summary:
    sheets: list[str]
    columns: dict[str, int]
    rows: int = 0
    trace: int = 0
    unknown: int = 0
    blank: int = 0
    unparsed: list[tuple[str, str, str]] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)

    def line(self) -> str:
        cols = ",".join(f"{k}={v}" for k, v in self.columns.items())
        return (
            f"sheet={SHEET!r} rows={self.rows} Tr={self.trace} N={self.unknown} "
            f"blank={self.blank} unparsed={len(self.unparsed)} dup={len(self.duplicates)} "
            f"columns[{cols}] sheets={self.sheets}"
        )


class LayoutError(RuntimeError):
    """The workbook does not carry the pinned sheet/columns; nothing is written."""


def _cell(v: object) -> str:
    return "" if v is None else str(v).strip()


def locate_columns(titles: Sequence[object], codes: Sequence[object]) -> dict[str, int]:
    """Column index per output column, by title (row 1) or CoFID field code (row 2)."""
    t = [_cell(x) for x in titles]
    c = [_cell(x) for x in codes]
    found: dict[str, int] = {}
    missing: list[str] = []
    for out, (title, code) in LOCATORS.items():
        idx = -1
        if title is not None and title in t:
            idx = t.index(title)
        elif code is not None and code in c:
            idx = c.index(code)
        if idx < 0:
            missing.append(out)
        else:
            found[out] = idx
    if missing:
        raise LayoutError(f"columns not found in {SHEET!r}: {missing}; row-2 codes seen: {c}")
    return found


def parse_value(raw: object) -> tuple[str, str]:
    """(csv text, kind) with kind in number | trace | unknown | blank | unparsed."""
    if raw is None:
        return "", "blank"
    if isinstance(raw, bool):
        return "", "unparsed"
    if isinstance(raw, int | float):
        return f"{raw:g}", "number"
    s = str(raw).strip()
    if s == "":
        return "", "blank"
    if s == TRACE:
        return "0", "trace"
    if s == UNKNOWN:
        return "", "unknown"
    try:
        float(s)
    except ValueError:
        return "", "unparsed"
    return s, "number"


def convert(xlsx: Path, out: Path, *, sheet: str = SHEET) -> Summary:
    wb = openpyxl.load_workbook(xlsx, read_only=True, data_only=True)
    try:
        if sheet not in wb.sheetnames:
            raise LayoutError(f"sheet {sheet!r} not in workbook; sheets: {wb.sheetnames}")
        ws = wb[sheet]
        rows = ws.iter_rows(values_only=True)
        titles = list(next(rows, ()))
        codes = list(next(rows, ()))
        next(rows, None)  # row 3: descriptions
        columns = locate_columns(titles, codes)
        summary = Summary(sheets=list(wb.sheetnames), columns=columns)
        seen: set[str] = set()
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh, lineterminator="\n")
            w.writerow(OUT_COLUMNS)
            for row in rows:
                cells = list(row)
                code = (
                    _cell(cells[columns["food_code"]]) if len(cells) > columns["food_code"] else ""
                )
                if code == "":
                    continue
                if code in seen:
                    summary.duplicates.append(code)
                    continue
                seen.add(code)
                name = _cell(cells[columns["food_name"]])
                values: list[str] = [code, name]
                for col in OUT_COLUMNS[2:]:
                    idx = columns[col]
                    raw = cells[idx] if idx < len(cells) else None
                    text, kind = parse_value(raw)
                    if kind == "trace":
                        summary.trace += 1
                    elif kind == "unknown":
                        summary.unknown += 1
                    elif kind == "blank":
                        summary.blank += 1
                    elif kind == "unparsed":
                        summary.unparsed.append((code, col, _cell(raw)))
                    values.append(text)
                w.writerow(values)
                summary.rows += 1
        return summary
    finally:
        wb.close()


def find(csv_path: Path, needle: str) -> list[dict[str, str]]:
    """Rows whose food_name contains `needle` (case-insensitive)."""
    n = needle.lower()
    with csv_path.open(encoding="utf-8", newline="") as fh:
        return [r for r in csv.DictReader(fh) if n in r["food_name"].lower()]


def spike_line(ok: bool, detail: str) -> str:
    return f"S7 {'pass' if ok else 'fail'} {DEFAULT_CLOCK.utc_stamp()} {detail}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--xlsx", type=Path, default=DEFAULT_XLSX)
    ap.add_argument("--out", type=Path, default=DEFAULT_CSV)
    ap.add_argument("--find", action="append", default=[], help="print CSV rows matching a name")
    ap.add_argument(
        "--no-convert", action="store_true", help="only --find against the existing CSV"
    )
    ap.add_argument("--spike-log", type=Path, default=None, help="append the S7 line to this file")
    args = ap.parse_args(argv)

    status = 0
    if not args.no_convert:
        try:
            summary = convert(args.xlsx, args.out)
        except (LayoutError, FileNotFoundError, StopIteration) as exc:
            msg = f"{type(exc).__name__}: {exc}"
            print(msg, file=sys.stderr)
            if args.spike_log is not None:
                args.spike_log.parent.mkdir(parents=True, exist_ok=True)
                with args.spike_log.open("a", encoding="utf-8") as fh:
                    fh.write(spike_line(False, msg) + "\n")
            return 1
        print(f"wrote {args.out} ({summary.rows} rows)")
        print(summary.line())
        for code, col, raw in summary.unparsed:
            print(f"  unparsed {code} {col}: {raw!r}", file=sys.stderr)
        for code in summary.duplicates:
            print(f"  duplicate food code kept first row: {code}", file=sys.stderr)
        if args.spike_log is not None:
            args.spike_log.parent.mkdir(parents=True, exist_ok=True)
            with args.spike_log.open("a", encoding="utf-8") as fh:
                fh.write(spike_line(True, summary.line()) + "\n")
    for needle in args.find:
        rows = find(args.out, needle)
        print(f"--- {needle!r}: {len(rows)} rows")
        for r in rows:
            print(
                f"{r['food_code']:>8}  {r['food_name'][:60]:<60} kcal={r['kcal']:>5} "
                f"P={r['protein_g']:>5} F={r['fat_g']:>5} C={r['carb_g']:>5} "
                f"fib={r['fibre_aoac_g']}"
            )
    return status


if __name__ == "__main__":
    sys.exit(main())
