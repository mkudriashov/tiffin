"""Keep published documentation aligned with the files it references."""

from __future__ import annotations

import re
import runpy
from pathlib import Path

IMAGE_SUFFIXES = {".gif", ".jpeg", ".jpg", ".png", ".svg", ".webp"}
MARKDOWN_LINK = re.compile(r"\]\(([^)\s]+)\)")
HTML_IMAGE = re.compile(r"<img\b[^>]*\bsrc=[\"']([^\"']+)[\"']", re.IGNORECASE)


def image_paths(document: Path) -> set[str]:
    content = document.read_text(encoding="utf-8")
    paths = {*MARKDOWN_LINK.findall(content), *HTML_IMAGE.findall(content)}
    return {path for path in paths if Path(path).suffix.lower() in IMAGE_SUFFIXES}


def test_configuration_reference_matches_defaults() -> None:
    root = Path(__file__).resolve().parents[1]
    render = runpy.run_path(str(root / "tools" / "generate_configuration.py"))["render"]
    assert (root / "docs" / "configuration.md").read_text(encoding="utf-8") == render()


def test_documented_images_exist() -> None:
    root = Path(__file__).resolve().parents[1]
    documents = [root / "README.md", *sorted((root / "docs").glob("*.md"))]
    for document in documents:
        for path in image_paths(document):
            assert (document.parent / path).is_file(), f"{document}: {path}"


def test_screenshot_index_covers_every_image() -> None:
    root = Path(__file__).resolve().parents[1]
    indexed = {Path(path).name for path in image_paths(root / "docs" / "screenshots.md")}
    available = {path.name for path in (root / "docs" / "images").glob("*.png")}
    assert indexed == available
