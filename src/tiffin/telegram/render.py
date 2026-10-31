"""Message templates: config/prompts/messages/<kind>[.<tone>].html, Python format fields.

Rules enforced here, not suggested: at most 10 lines, HTML tags b i code pre a only, no emoji,
blank lines produced by empty fragments are dropped. A missing context field is a bug and
raises. Tone (blunt/neutral/warm) selects <kind>.<tone>.html when it exists.
"""

from __future__ import annotations

import html
import re
import textwrap
from collections.abc import Mapping, Sequence
from functools import cache
from pathlib import Path
from typing import Any

from ..config import REPO_ROOT

DEFAULT_DIR = REPO_ROOT / "config" / "prompts" / "messages"
MAX_LINES = 10
ALLOWED_TAGS = {"b", "i", "code", "pre", "a"}
_TAG_RE = re.compile(r"</?([a-zA-Z0-9]+)(\s[^>]*)?>")
_EMOJI_RE = re.compile(
    "[\U0001f000-\U0001faff\U00002600-\U000027bf\U0001f900-\U0001f9ff⭐⭕⌚-⌛⏩-⏺▪-◾⤴⤵〰〽㊗㊙️]"
)


class RenderError(ValueError):
    pass


class _Ctx(dict):
    def __missing__(self, key: str) -> str:
        raise RenderError(f"template field missing from context: {key}")


def esc(text: object) -> str:
    return html.escape(str(text), quote=False)


def few(names: Sequence[str], limit: int = 3) -> str:
    """ "Chickpeas, Curry paste, Green beans and 2 more": a list said in a message that has to
    fit a phone. Every food on it grew the line - five avoided foods took four lines on 7 Oct -
    and the swap the message offers takes all of them out, named or not."""
    names = list(names)
    if len(names) <= limit:
        return ", ".join(names)
    return f"{', '.join(names[:limit])} and {len(names) - limit} more"


def strip_emoji(text: str) -> str:
    """Take the emoji out of somebody's own words before they are stored.

    The house rule is that Tiffin writes no emoji. It was enforced at render time, which
    also caught text the *person* typed: an emoji in an off-plan note raised RenderError, the
    check-in died with no reply, and because the note was already in the intent payload every
    later message re-rendered it and died the same way. Scrubbing on the way in keeps the rule
    for everything Tiffin composes while letting a person write however they like.
    """
    return _EMOJI_RE.sub("", text)


def fit(text: str, *, max_lines: int = MAX_LINES, kind: str = "message") -> str:
    """Make foreign text safe to send: no emoji, no blank lines, no more than max_lines.

    `check_text` raises, which is right for a template Tiffin wrote and wrong for a model
    reply or a person's note, where raising means sending nothing at all.
    """
    lines = [ln.rstrip() for ln in strip_emoji(text).splitlines() if ln.strip()]
    if len(lines) > max_lines:
        lines = [*lines[: max_lines - 1], lines[max_lines - 1][:200] + " ..."]
    return check_text("\n".join(lines) or "(nothing to say)", kind=kind, max_lines=max_lines)


def check_text(text: str, *, kind: str = "message", max_lines: int = MAX_LINES) -> str:
    """Ten lines is the rule for a message Tiffin sends unprompted.

    Something the person asked to see is different: a recipe card is a list of ingredients and
    steps and there is no useful ten-line version of it. Those pass their own `max_lines`.
    """
    lines = [ln.rstrip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln.strip()]
    if len(lines) > max_lines:
        raise RenderError(f"{kind}: {len(lines)} lines > {max_lines}")
    for m in _TAG_RE.finditer(text):
        if m.group(1).lower() not in ALLOWED_TAGS:
            raise RenderError(f"{kind}: tag <{m.group(1)}> not allowed")
    if _EMOJI_RE.search(text):
        raise RenderError(f"{kind}: emoji are not allowed")
    return "\n".join(lines)


def template_path(kind: str, tone: str | None, templates_dir: Path = DEFAULT_DIR) -> Path:
    if tone:
        p = templates_dir / f"{kind}.{tone}.html"
        if p.is_file():
            return p
    p = templates_dir / f"{kind}.html"
    if not p.is_file():
        raise RenderError(f"no template for {kind}")
    return p


@cache
def _template(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def render(
    kind: str,
    ctx: Mapping[str, Any],
    tone: str = "neutral",
    *,
    templates_dir: Path = DEFAULT_DIR,
) -> str:
    template = _template(template_path(kind, tone, templates_dir))
    text = template.format_map(_Ctx(ctx))
    return check_text(text, kind=kind)


#: characters to a line in a phone's message bubble, and how many of those lines a line reads
#: "at a glance" in - the same two numbers `phone_reading` measures against. A raw character
#: count divided by 36 is only an estimate of how many rows a line wraps to: `textwrap`'s own
#: word-boundary wrapping can waste the end of a row on a word that almost fit, so `fit_joined`
#: below checks with `textwrap.wrap` itself rather than trusting arithmetic on a budget.
PHONE_COLS = 36
PHONE_ROWS = 3


def truncate(text: str, limit: int, *, ellipsis: str = "…") -> str:
    """Cut `text` to `limit` characters at the last whole word, with an ellipsis - never mid-word
    and never silent about it.

    A hard `text[:n]` reads as a typo on a phone: "Chicken, chickpea and feta s" said nothing was
    left off, and a person could not tell a cut name from a short one (phone_reading/02). Text
    already inside the limit is returned exactly as it was.
    """
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    budget = limit - len(ellipsis)
    if budget <= 0:
        return ellipsis[:limit]
    cut = text[:budget]
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    cut = cut.rstrip()
    return (cut + ellipsis) if cut else ellipsis[:limit]


def budget_fields(fields: Sequence[str], total_budget: int) -> list[str]:
    """Shrink `fields` together to `total_budget` characters, water-filled: a field shorter than
    an equal share is left alone and the room it does not need goes to the fields that do, each
    cut (never mid-word - see `truncate`) rather than one field being budgeted alone while
    another sits next to it, untouched, over the same limit.

    A shopping-list line had four variable lengths on it - the ingredient name, the pack label,
    the product and the dish it is for - and only two of them were ever capped, at fixed sizes
    regardless of what the others needed (phone_reading/02, /04): a long dish name alone could
    push a short product name off a line neither had actually been budgeted against.
    """
    out = list(fields)
    if not out or sum(len(f) for f in out) <= total_budget:
        return out
    settled: dict[int, int] = {}
    pending = list(range(len(out)))
    remaining_budget = total_budget
    while pending:
        share = max(1, remaining_budget // len(pending))
        short_ones = [i for i in pending if len(out[i]) <= share]
        if not short_ones:
            for i in pending:
                settled[i] = share
            break
        for i in short_ones:
            settled[i] = len(out[i])
            remaining_budget -= len(out[i])
        pending = [i for i in pending if i not in short_ones]
    return [truncate(f, settled[i]) for i, f in enumerate(out)]


def _rows(text: str, *, cols: int = PHONE_COLS) -> int:
    """How many phone-rows `text` wraps to, measured the way `phone_reading` measures it: on
    the VISIBLE text - a `<b>` or `</b>` costs nothing on a phone - and by real word-boundary
    wrapping, not `len(text) // cols`, which can undercount: a word that almost fits a row is
    carried whole to the next one, wasting the end of the row it did not fit."""
    visible = _TAG_RE.sub("", text)
    return max(1, len(textwrap.wrap(visible, cols))) if visible.strip() else 0


def fit_joined(
    prefix: str,
    parts: Sequence[str],
    sep: str,
    suffix: str = "",
    *,
    max_rows: int = PHONE_ROWS,
    cols: int = PHONE_COLS,
) -> str:
    """`prefix + sep.join(parts) + suffix`, keeping the WHOLE line inside `max_rows` phone-rows:
    as many WHOLE parts as fit, then a "+N more" note for the rest IF that note itself still
    fits - rather than a joined line running past what a phone shows in the three lines a
    bubble is read in (phone_reading/06, /09) - a shared ingredient or a rotating week can hold
    more dishes than a line has room to name.

    Checked by actually wrapping the candidate line (`_rows`), not a raw character budget: a
    fixed character count is only an estimate of the row count a real phone wraps to, and
    tuning one number to satisfy a tight case starved an ordinary one sitting right next to it
    within the same estimate's margin of error.
    """
    if not parts:
        return prefix + suffix
    full = prefix + sep.join(parts) + suffix
    if _rows(full, cols=cols) <= max_rows:
        return full
    kept: list[str] = []
    for part in parts:
        trial = [*kept, part]
        rest = len(parts) - len(trial)
        note = f"{sep}+{rest} more" if rest else ""
        candidate = prefix + sep.join(trial) + note + suffix
        if _rows(candidate, cols=cols) > max_rows:
            break
        kept.append(part)
    if not kept:
        room = cols * max_rows - len(prefix) - len(suffix)
        return prefix + truncate(parts[0], room) + suffix
    rest = len(parts) - len(kept)
    note = f"{sep}+{rest} more" if rest else ""
    candidate = prefix + sep.join(kept) + note + suffix
    if note and _rows(candidate, cols=cols) > max_rows:
        candidate = prefix + sep.join(kept) + suffix
    return candidate


def macros_line(kcal: int, protein_g: int, fat_g: int, carb_g: int, fibre_g: int) -> str:
    return f"{kcal} kcal · P {protein_g} g · F {fat_g} g · C {carb_g} g · fibre {fibre_g} g"


#: English plurals a naive +"s" gets wrong. Small on purpose: this exists to stop "1 boxes",
#: not to conjugate arbitrary nouns, and a caller with an irregular word passes it explicitly.
_ES = ("x", "s", "z", "ch", "sh")


def plural(n: int, one: str, many: str = "") -> str:
    """ "1 box", "5 boxes". A message that says "1 boxes" reads as a machine talking.

    The count comes with it, because every caller wants both and building them separately is
    how "1" and "boxes" end up in different f-strings agreeing with nothing.
    """
    if n == 1:
        return f"{n} {one}"
    if many:
        return f"{n} {many}"
    return f"{n} {one}{'es' if one.endswith(_ES) else 's'}"


def kg(value: float) -> str:
    return f"{value:.1f} kg"


def signed_kg(value: float) -> str:
    return f"{value:+.1f} kg"


def page_link(config: Any, label: str = "the page") -> str:
    """An <a> to Tiffin's own page, or a plain phrase when no origin is configured.

    Two messages sent somebody to "the web page" and the address appeared in no message
    anywhere, while sitting in config the whole time - so standing in a shop at item thirteen,
    the route out was a place Tiffin would not name. The page is tailnet-only, which is
    exactly why naming it is safe: the link works on their phone and nowhere else.
    """
    origin = str(config.get("web.origin", "")).rstrip("/")
    root = str(config.get("web.root_path", "/tiffin")).rstrip("/")
    if not origin:
        return label
    return f'<a href="{esc(origin + root)}">{esc(label)}</a>'


def page_today_link(config: Any, day: str = "", label: str = "the day on the page") -> str:
    """An <a> to the page's Today tab (on `day` when given), or "" when no origin is configured.

    written things to the app and Tiffin just provides a link"), so this is the link
    nearly every message ends with. Only for a person whose login is linked - the caller checks.
    """
    origin = str(config.get("web.origin", "")).rstrip("/")
    root = str(config.get("web.root_path", "/tiffin")).rstrip("/")
    if not origin:
        return ""
    query = "tab=today" + (f"&date={day}" if day else "")
    return f'<a href="{esc(f"{origin}{root}/?{query}")}">{esc(label)}</a>'


def summary_link(config: Any, label: str = "The whole week on the page") -> str:
    """An <a> to the page's Summary tab, or "" when no origin is configured."""
    origin = str(config.get("web.origin", "")).rstrip("/")
    root = str(config.get("web.root_path", "/tiffin")).rstrip("/")
    if not origin:
        return ""
    return f'<a href="{esc(f"{origin}{root}/?tab=summary")}">{esc(label)}</a>'


def menu_link(
    config: Any, day: str = "", slot: str = "", label: str = "this meal on the page"
) -> str:
    """An <a> to the page's Menu tab - one meal, one day, or the whole run of menus when neither
    is given - or "" when no origin is configured.

    "Potentially we send her links to tailscale where she can look at it with recipe in there"
    person whose login is linked: for anyone else the page is a 403, and the caller leaves the
    link out.
    """
    origin = str(config.get("web.origin", "")).rstrip("/")
    root = str(config.get("web.root_path", "/tiffin")).rstrip("/")
    if not origin:
        return ""
    query = "tab=menu" + (f"&day={day}" if day else "") + (f"&slot={slot}" if day and slot else "")
    href = f"{origin}{root}/?{query}"
    return f'<a href="{esc(href)}">{esc(label)}</a>'


def batch_link(config: Any, label: str = "the cook on the page") -> str:
    """An <a> to the page's Batch tab, or "" when no origin is configured - mirrors
    `menu_link()`, but the cook has no day or slot of its own to name, only the tab."""
    origin = str(config.get("web.origin", "")).rstrip("/")
    root = str(config.get("web.root_path", "/tiffin")).rstrip("/")
    if not origin:
        return ""
    href = f"{origin}{root}/?tab=batch"
    return f'<a href="{esc(href)}">{esc(label)}</a>'


def choose_link(config: Any, label: str = "Choose on the page") -> str:
    """An <a> to the page's weekly choices (Menu, then Choose), or "" when no origin is
    configured - mirrors `batch_link()`."""
    origin = str(config.get("web.origin", "")).rstrip("/")
    root = str(config.get("web.root_path", "/tiffin")).rstrip("/")
    if not origin:
        return ""
    return f'<a href="{esc(f"{origin}{root}/?tab=choose")}">{esc(label)}</a>'


def shopping_link(config: Any, label: str = "the page", list_id: int | None = None) -> str:
    """An <a> to the page's Shopping tab, or "" when no origin is configured - mirrors
    `batch_link()`. `page_link()` is deliberately generic (no tab), used everywhere a message
    just points at "the page" as a whole; the overflow line in `/shopping` is naming a specific
    tab - a trolley and a phone at item thirteen has no patience for the extra tap `page_link()`
    alone would cost landing on whatever tab was last open (shopping_tab/06)."""
    origin = str(config.get("web.origin", "")).rstrip("/")
    root = str(config.get("web.root_path", "/tiffin")).rstrip("/")
    if not origin:
        return ""
    href = f"{origin}{root}/?tab=shopping"
    if list_id is not None:
        # the list the message is about, not whichever the page would pick today: on a Saturday
        # the weekend's and Monday's are both live
        href += f"&list={int(list_id)}"
    return f'<a href="{esc(href)}">{esc(label)}</a>'


def trends_link(config: Any, metric: str = "", label: str = "the chart on the page") -> str:
    """Link to a selected trend metric when the web page is configured."""
    origin = str(config.get("web.origin", "")).rstrip("/")
    root = str(config.get("web.root_path", "/tiffin")).rstrip("/")
    if not origin:
        return ""
    query = "tab=trends" + (f"&metric={metric}" if metric else "")
    href = f"{origin}{root}/?{query}"
    return f'<a href="{esc(href)}">{esc(label)}</a>'
