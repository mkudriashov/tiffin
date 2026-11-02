"""Keep personal data, machine identifiers and credentials out of published files."""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MACHINE = re.compile(r"(?:[A-Z]:[\\/](?:Users|admin|lab)[\\/]|/(?:home|Users)/[^/\s]+/)", re.I)
EMAIL = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
HOST = re.compile(r"https?://([A-Z0-9.-]+)", re.I)
TAILNET = re.compile(r"[A-Z0-9.-]+\.ts\.net", re.I)
PHONE = re.compile(r"(?<!\d)(?:\+44\s?7\d{3}|07\d{3})[ -]?\d{6}\b")
CHAT_ID = re.compile(r"(?<!\d)-100\d{8,13}(?!\d)")
TOKEN = re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b")
SECRET = re.compile(r"(?i)\b(?:sk-[a-z0-9_-]{20,}|gh[pousr]_[a-z0-9]{20,}|AKIA[0-9A-Z]{16})\b")
PRIVATE_NAMES = re.compile(
    r"\b(?:" + "|".join(("Sa" + "sha", "Ole" + "ksandra", "Kud" + "riashov")) + r")\b",
    re.I,
)
OLD_NAME = re.compile(r"\b" + "track" + r"er\b", re.I)
PERSONAL_EVENT = re.compile(r"\bborn\s+[12]\d{3}-\d{2}-\d{2}\b", re.I)
PUBLIC_HOSTS = {
    "api.anthropic.com",
    "api.openai.com",
    "api.telegram.org",
    "assets.publishing.service.gov.uk",
    "cdn.plot.ly",
    "docs.astral.sh",
    "eviltesco.com",
    "files.pythonhosted.org",
    "getify.mit-license.org",
    "github.com",
    "intervals.icu",
    "json-schema.org",
    "plotly.com",
    "pypi.org",
    "scripts.sil.org",
    "syntheti.cc",
    "www.amazon.co.uk",
    "www.apache.org",
    "www.apple.com",
    "www.contributor-covenant.org",
    "www.gov.uk",
    "www.nationalarchives.gov.uk",
    "www.ocado.com",
    "www.sainsburys.co.uk",
    "www.tesco.com",
    "www.w3.org",
}


def published_texts() -> Iterator[tuple[Path, str]]:
    """Read checkout files without requiring Git or the user's Git configuration."""
    excluded = {"__pycache__", "build", "dist", "node_modules"}
    for directory, dirs, files in os.walk(ROOT):
        dirs[:] = sorted(
            name
            for name in dirs
            if (not name.startswith(".") or name == ".github") and name not in excluded
        )
        for filename in sorted(files):
            path = Path(directory) / filename
            if filename == ".git":
                continue  # a linked worktree's pointer file, never published
            if path.is_symlink():
                continue
            relative = path.relative_to(ROOT)
            try:
                content = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            if "\0" in content:
                continue
            yield relative, content


def test_no_private_strings() -> None:
    problems: list[str] = []
    for relative, content in published_texts():
        for number, line in enumerate(content.splitlines(), 1):
            findings: set[str] = set()
            for label, pattern in (
                ("machine path", MACHINE),
                ("phone number", PHONE),
                ("Telegram chat ID", CHAT_ID),
                ("bot token", TOKEN),
                ("API key", SECRET),
                ("former product name", OLD_NAME),
                ("personal event", PERSONAL_EVENT),
            ):
                if pattern.search(line):
                    findings.add(label)
            example_tailnet = "tiffin" + "." + "example" + "." + "ts" + "." + "net"
            if any(m.group().lower() != example_tailnet for m in TAILNET.finditer(line)):
                findings.add("private tailnet host")
            if any(
                not m.group().lower().endswith(("@example.com", "@example.org"))
                for m in EMAIL.finditer(line)
            ):
                findings.add("private email")
            for match in HOST.finditer(line):
                host = match.group(1).lower()
                example_tailnet_suffix = "." + "example" + "." + "ts" + "." + "net"
                if (
                    host not in PUBLIC_HOSTS
                    and host not in {"127.0.0.1", "localhost", "testserver", "example.com"}
                    and not host.endswith(
                        (".example", ".example.com", ".example.org", example_tailnet_suffix)
                    )
                ):
                    findings.add("unrecognised hostname")
            without_repo_url = line.replace("https://github.com/mkudriashov/tiffin", "")
            if PRIVATE_NAMES.search(without_repo_url) or re.search(
                r"\bMax\b(?!\s+(?:chars|loss)\b)", line
            ):
                findings.add("private name")
            problems.extend(f"{relative}:{number}: {finding}" for finding in sorted(findings))
    assert not problems, "\n".join(problems)
