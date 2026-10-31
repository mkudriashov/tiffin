"""Build static assets once when the web app starts.

ES modules import each other by relative, unhashed URL and are revalidated on every use.
Stylesheets and the Plotly bundle use content-hashed URLs with immutable caching.
`index.html` and `manifest.webmanifest` replace `{{ROOT}}` with the mount path and
`{{static:name}}` with the URL for that asset.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

STATIC_DIR = Path(__file__).with_name("static")
TEMPLATES = ("index.html", "manifest.webmanifest")
SKIP_SUFFIXES = frozenset({".md", ".py", ".pyc"})
CONTENT_TYPES = {
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".webmanifest": "application/manifest+json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".txt": "text/plain; charset=utf-8",
    ".json": "application/json",
    ".woff2": "font/woff2",
}
_REF = re.compile(r"\{\{static:([A-Za-z0-9_.\-]+)\}\}")


@dataclass(frozen=True)
class Asset:
    name: str
    hashed: str
    content_type: str
    body: bytes
    sha256: str


def hashed_name(name: str, digest: str) -> str:
    stem, _, ext = name.rpartition(".")
    return f"{stem}.{digest[:10]}.{ext}" if stem else f"{name}.{digest[:10]}"


class Assets:
    """The built asset table: `by_name` (logical) and `by_hashed` (URL name), plus the rendered
    `index` and `manifest` bytes."""

    def __init__(self, root_path: str, directory: Path = STATIC_DIR):
        self.root = root_path.rstrip("/") + "/"
        self.directory = directory
        self.by_name: dict[str, Asset] = {}
        self.by_hashed: dict[str, Asset] = {}
        self.module_names: set[str] = set()
        self.index = b""
        self.manifest = b""
        self._build()

    def _build(self) -> None:
        for path in sorted(self.directory.iterdir()):
            if (
                not path.is_file()
                or path.name in TEMPLATES
                or path.suffix in SKIP_SUFFIXES
                or path.name.startswith(".")
            ):
                continue
            body = path.read_bytes()
            if path.suffix == ".js" and not path.name.endswith(".min.js"):
                self.module_names.add(path.name)
            digest = hashlib.sha256(body).hexdigest()
            asset = Asset(
                name=path.name,
                hashed=hashed_name(path.name, digest),
                content_type=CONTENT_TYPES.get(path.suffix, "application/octet-stream"),
                body=body,
                sha256=digest,
            )
            self.by_name[asset.name] = asset
            self.by_hashed[asset.hashed] = asset
        self.index = self.render("index.html")
        self.manifest = self.render("manifest.webmanifest")

    def url(self, name: str) -> str:
        """Relative URL of a logical asset name (resolved by the page's <base href>)."""
        asset = self.by_name[name]
        return f"static/{name if name in self.module_names else asset.hashed}"

    def render(self, template: str) -> bytes:
        text = (self.directory / template).read_text(encoding="utf-8")
        text = text.replace("{{ROOT}}", self.root)

        def sub(m: re.Match[str]) -> str:
            try:
                return self.url(m.group(1))
            except KeyError:
                raise FileNotFoundError(
                    f"{template} refers to missing asset {m.group(1)}"
                ) from None

        return _REF.sub(sub, text).encode("utf-8")
