"""The browser must be able to link every module loaded by the page."""

from __future__ import annotations

import re

from tiffin.web.assets import STATIC_DIR, Assets

IMPORT = re.compile(r'^import\s*\{([^}]+)\}\s*from\s*["\']\./([^"\']+)["\'];?', re.MULTILINE)
EXPORT = re.compile(r"^export\s+(?:async\s+)?(?:function|const|let|class)\s+([\w$]+)", re.MULTILINE)
INLINE_HANDLER = re.compile(
    r"(?<![\w.])on(?:click|change|input|submit|load|keydown)\s*=", re.IGNORECASE
)


def test_every_relative_import_resolves_to_a_served_module_and_export() -> None:
    assets = Assets("/")
    for name in assets.module_names:
        source = (STATIC_DIR / name).read_text(encoding="utf-8")
        imports = list(IMPORT.finditer(source))
        assert len(imports) == len(re.findall(r"^import\b", source, re.MULTILINE)), name
        for match in imports:
            target = match.group(2)
            assert target in assets.module_names, f"{name} imports ./{target}, which is not served"
            exports = set(EXPORT.findall((STATIC_DIR / target).read_text(encoding="utf-8")))
            for imported in match.group(1).split(","):
                symbol = imported.strip()
                assert symbol in exports, (
                    f"{name} imports {symbol} from {target}, which does not export it"
                )


def test_page_uses_the_same_url_as_importers_of_app() -> None:
    assets = Assets("/")
    index = assets.index.decode()
    assert f'<script type="module" src="{assets.url("app.js")}">' in index
    assert assets.url("app.js") == "static/app.js"
    assert any(
        'from "./app.js"' in (STATIC_DIR / name).read_text(encoding="utf-8")
        for name in assets.module_names
        if name != "app.js"
    )


def test_modules_do_not_depend_on_the_old_script_scope_or_inline_handlers() -> None:
    assets = Assets("/")
    for name in assets.module_names:
        source = (STATIC_DIR / name).read_text(encoding="utf-8")
        if name != "helpers.js" and re.search(r"\$\s*\(", source):
            assert re.search(
                r'^import\s*\{[^}]*\$[^}]*\}\s*from\s*"\./helpers.js"',
                source,
                re.MULTILINE,
            ), name
        assert not INLINE_HANDLER.search(source), name
    assert not INLINE_HANDLER.search((STATIC_DIR / "index.html").read_text(encoding="utf-8"))
