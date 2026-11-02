"""Tests that need two people already linked get it here, the way the importer records a pair
that was sharing weekends before links existed: no code, no consent rows."""

from __future__ import annotations

from tiffin import links
from tiffin.db import Db


def link_pair(db: Db, a: int, b: int) -> int:
    """Record `a` and `b` as linked (method 'migrated'); returns the link id."""
    return links.record_migrated(db, a, b)
