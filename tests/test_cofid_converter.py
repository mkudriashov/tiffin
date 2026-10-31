"""Tools/cofid_to_csv.py against a tiny workbook built here with openpyxl (the real one is
gitignored). The fixture copies the pinned layout: three header rows, codes in row 2."""

from __future__ import annotations

import csv
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import openpyxl
import pytest

TOOL = Path(__file__).resolve().parents[1] / "tools" / "cofid_to_csv.py"
TITLES = [
    "Food Code",
    "Food Name",
    "Description",
    "Group",
    "Previous",
    "Main data references",
    "Footnote",
    "Water (g)",
    "Total nitrogen (g)",
    "Protein (g)",
    "Fat (g)",
    "Carbohydrate (g)",
    "Energy (kcal) (kcal)",
    "Energy (kJ) (kJ)",
    "Starch (g)",
    "NSP (g)",
    "AOAC fibre (g)",
]
CODES = [None] * 7 + [
    "WATER",
    "TOTNIT",
    "PROT",
    "FAT",
    "CHO",
    "KCALS",
    "KJ",
    "STAR",
    "ENGFIB",
    "AOACFIB",
]
DESCRIPTIONS = [None] * 7 + [
    "Water",
    "Total nitrogen",
    "Protein",
    "Fat",
    "Carbohydrate",
    "kcal",
    "kJ",
    "Starch",
    "Non-starch polysaccharide",
    "AOAC fibre",
]


def _row(code: str, name: str, prot: object, fat: object, cho: object, kcal: object, fib: object):
    return [
        code,
        name,
        "desc",
        "MA",
        None,
        "ref",
        None,
        "60.0",
        "3.9",
        prot,
        fat,
        cho,
        kcal,
        "690",
        "0",
        "0",
        fib,
    ]


@pytest.fixture
def tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location("cofid_to_csv", TOOL)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    # dataclasses resolve annotations through sys.modules, so register before executing
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def make_workbook(path: Path, *, drop_fibre: bool = False) -> None:
    wb = openpyxl.Workbook()
    first = wb.active
    assert first is not None
    first.title = "List of tables"
    first.append(["List of tables in CoFID 2021"])
    ws = wb.create_sheet("1.3 Proximates")
    titles, codes, descs = list(TITLES), list(CODES), list(DESCRIPTIONS)
    if drop_fibre:
        titles[-1], codes[-1], descs[-1] = "Cholesterol (mg)", "CHOL", "Cholesterol"
    ws.append(titles)
    ws.append(codes)
    ws.append(descs)
    ws.append(_row("18-001", "Chicken, breast, raw", "24.0", "2.2", "0.0", "116", "0.0"))
    ws.append(_row("11-002", "Agar, dried", "1.3", "1.2", "Tr", "16", "N"))
    ws.append(_row("13-003", "Sweet potato, raw", "1.2", "0.3", "21.3", "87", None))
    ws.append(_row("13-003", "Sweet potato, raw (duplicate)", "9", "9", "9", "9", "9"))
    ws.append(_row("17-004", "Oil, olive", 0, 99.9, 0.0, 899, 0))
    ws.append(_row("99-005", "Broken row", "1.0", "x?", "2.0", "10", "0.5"))
    ws.append([None] * len(titles))
    wb.save(path)


def test_convert_pins_columns_and_maps_values(tmp_path: Path, tool: ModuleType) -> None:
    xlsx = tmp_path / "cofid.xlsx"
    out = tmp_path / "out" / "proximates.csv"
    make_workbook(xlsx)
    summary = tool.convert(xlsx, out)
    assert summary.sheets == ["List of tables", "1.3 Proximates"]
    assert summary.columns == {
        "food_code": 0,
        "food_name": 1,
        "kcal": 12,
        "protein_g": 9,
        "fat_g": 10,
        "carb_g": 11,
        "fibre_aoac_g": 16,
    }
    assert summary.rows == 5
    assert summary.duplicates == ["13-003"]
    assert summary.trace == 1 and summary.unknown == 1 and summary.blank == 1
    assert summary.unparsed == [("99-005", "fat_g", "x?")]
    with out.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert list(rows[0].keys()) == [
        "food_code",
        "food_name",
        "kcal",
        "protein_g",
        "fat_g",
        "carb_g",
        "fibre_aoac_g",
    ]
    by_code = {r["food_code"]: r for r in rows}
    assert by_code["18-001"]["food_name"] == "Chicken, breast, raw"
    assert by_code["18-001"]["kcal"] == "116" and by_code["18-001"]["fibre_aoac_g"] == "0.0"
    assert by_code["11-002"]["carb_g"] == "0"  # Tr -> 0
    assert by_code["11-002"]["fibre_aoac_g"] == ""  # N -> empty
    assert by_code["13-003"]["fibre_aoac_g"] == ""  # blank -> empty
    assert by_code["13-003"]["protein_g"] == "1.2"  # first row of a duplicated code kept
    assert by_code["17-004"]["kcal"] == "899" and by_code["17-004"]["fat_g"] == "99.9"
    assert by_code["99-005"]["fat_g"] == ""  # unparseable -> empty, reported
    assert out.read_bytes().count(b"\r") == 0  # LF only, the repo convention


def test_missing_column_is_a_layout_error(tmp_path: Path, tool: ModuleType) -> None:
    xlsx = tmp_path / "cofid.xlsx"
    make_workbook(xlsx, drop_fibre=True)
    with pytest.raises(tool.LayoutError, match="fibre_aoac_g"):
        tool.convert(xlsx, tmp_path / "out.csv")
    assert not (tmp_path / "out.csv").exists()


def test_missing_sheet_is_a_layout_error(tmp_path: Path, tool: ModuleType) -> None:
    xlsx = tmp_path / "cofid.xlsx"
    wb = openpyxl.Workbook()
    wb.save(xlsx)
    with pytest.raises(tool.LayoutError, match="1.3 Proximates"):  # noqa: RUF043
        tool.convert(xlsx, tmp_path / "out.csv")


def test_parse_value_kinds(tool: ModuleType) -> None:
    assert tool.parse_value("Tr") == ("0", "trace")
    assert tool.parse_value("N") == ("", "unknown")
    assert tool.parse_value(None) == ("", "blank")
    assert tool.parse_value("  ") == ("", "blank")
    assert tool.parse_value("3.6") == ("3.6", "number")
    assert tool.parse_value(165) == ("165", "number")
    assert tool.parse_value(0.5) == ("0.5", "number")
    assert tool.parse_value("abc") == ("", "unparsed")
    assert tool.parse_value(True) == ("", "unparsed")


def test_main_writes_spike_line_and_finds(tmp_path: Path, tool: ModuleType, capsys) -> None:
    xlsx = tmp_path / "cofid.xlsx"
    out = tmp_path / "proximates.csv"
    log = tmp_path / "state" / "spikes.log"
    make_workbook(xlsx)
    rc = tool.main(
        ["--xlsx", str(xlsx), "--out", str(out), "--spike-log", str(log), "--find", "chicken"]
    )
    assert rc == 0
    line = log.read_text(encoding="utf-8").strip()
    assert line.startswith("S7 pass 20") and "'1.3 Proximates'" in line and "AOACFIB" not in line
    assert "fibre_aoac_g=16" in line
    printed = capsys.readouterr().out
    assert "18-001" in printed and "Chicken, breast, raw" in printed


def test_main_logs_failure(tmp_path: Path, tool: ModuleType) -> None:
    xlsx = tmp_path / "cofid.xlsx"
    make_workbook(xlsx, drop_fibre=True)
    log = tmp_path / "spikes.log"
    rc = tool.main(["--xlsx", str(xlsx), "--out", str(tmp_path / "o.csv"), "--spike-log", str(log)])
    assert rc == 1
    assert log.read_text(encoding="utf-8").startswith("S7 fail ")
