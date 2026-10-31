"""Name normalisation for the catalogue and food preferences.

`normalise` is the key for alias-exact matching; `tokens` and `contained` support
food-name matching. Neither touches the database and neither is clever: lowercase, punctuation to
spaces, collapse spaces.
"""

from __future__ import annotations

import re
import unicodedata

_PUNCT = re.compile(r"[\W_]+")
_DIGIT_LETTER = re.compile(r"(?<=\d)(?=[^\W\d])|(?<=[^\W\d])(?=\d)")
UNIT_TOKENS = frozenset(
    {
        "g",
        "kg",
        "mg",
        "ml",
        "l",
        "oz",
        "lb",
        "lbs",
        "cup",
        "cups",
        "tbsp",
        "tsp",
        "serving",
        "servings",
        "portion",
        "portions",
        "x",
        "pcs",
        "piece",
        "pieces",
        "slice",
        "slices",
        "of",
        "and",
        "with",
    }
)


def normalise(text: str) -> str:
    """Lowercase, accents folded, punctuation to spaces, whitespace collapsed.

    The catalogue spells everything in ASCII, and a person typing the correct British spelling
    does not: puree and puree, pate and pate, creme fraiche. Without the fold, "no puree" on
    the avoid list matched nothing and the food came back in a menu - the same shape as the
    "chickpea" and "tomatoes" bugs, one layer further out.
    """
    folded = unicodedata.normalize("NFKD", text.casefold())
    stripped = "".join(c for c in folded if not unicodedata.combining(c))
    return " ".join(_PUNCT.sub(" ", stripped).split())


def tokens(text: str) -> frozenset[str]:
    """Words for Jaccard: normalised, digits split from letters, bare numbers and units dropped."""
    spaced = _DIGIT_LETTER.sub(" ", normalise(text))
    return frozenset(t for t in spaced.split() if not t.isdigit() and t not in UNIT_TOKENS)


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


MIN_SINGLE_TOKEN = 4


def head_of(text: str) -> str:
    """The last word of a name: in English the thing it IS, not what kind of it it is."""
    words = tokens_in_order(text)
    return words[-1] if words else ""


def tokens_in_order(text: str) -> list[str]:
    spaced = _DIGIT_LETTER.sub(" ", normalise(text))
    return [t for t in spaced.split() if not t.isdigit() and t not in UNIT_TOKENS]


def contained(item: frozenset[str], alias: frozenset[str], *, head: str = "") -> float:
    """1.0 when every word of `alias` appears in `item`, else 0.0.

    Supermarket names carry brand and variety words the catalogue does not ("Pink Lady Apples",
    "Quaker Oats So Simple"), so plain Jaccard scores them far below a real match. A one-word
    alias must be at least MIN_SINGLE_TOKEN characters, so "g" or "oil" cannot grab a name.

    A one-word alias must also BE the name's head noun when a head is given. "Pink Lady Apples"
    is apples and "Apple Pie" is not: English compounds are head-final, so the last word is the
    food and everything before it says which kind. Without that rule, one shared word scored a
    flat 1.0 - the same as an exact alias - and Carrot Cake matched carrots, Butter Beans
    matched butter, and Milk Chocolate Digestives matched semi-skimmed milk. `classify` then
    reported a slice of cake as on plan. A name whose head noun is not in the catalogue now
    falls to the model hook, which is the fallback that exists for exactly this.
    """
    if not item or not alias or not alias <= item:
        return 0.0
    if len(alias) == 1:
        word = next(iter(alias))
        if len(word) < MIN_SINGLE_TOKEN:
            return 0.0
        if head and word != head:
            return 0.0
    return 1.0
