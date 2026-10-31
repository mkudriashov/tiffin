"""Handle menu commands, menu decisions and shopping actions in Telegram."""

from __future__ import annotations

import datetime as dt
import json
import re
from typing import TYPE_CHECKING, Any

from .. import building, log
from .. import targets as targets_mod
from ..clock import DEFAULT_CLOCK, Clock, isoday, meal_now, parse_day, parse_stamp
from ..db import Db
from ..errors import NothingToBuild
from ..food import menu as menu_mod
from ..food import recipes, shopping
from ..ledger import AlreadyDone
from . import convstate
from .api import button_rows, inline_keyboard
from .menu_language import day_and_meal, day_named, days_in_note, slots_named
from .render import budget_fields, check_text, esc, few, menu_link, page_link

logger = log.get("tiffin.menuflow")

COOK_COOLDOWN_S = 300
CHANGE_FLOW = "menu_change"
CHANGE_TTL = 6 * 3600  # an unanswered "what should change?" stops listening after six hours

if TYPE_CHECKING:
    from ..app import App
    from .handlers import Handlers
    from .poller import Inbound

RATE_MIN, RATE_MAX = 1, 5


#: big enough for the largest recipe the catalogue can hold, which is the point of the card:
#: MAX_INGREDIENTS (12) + MAX_STEPS (12) + a name, a macro line, a portion line, two headings,
#: the fridge life and the rating prompt is 31, and at 30 the biggest card silently lost a line.
#: A card is asked for, so the ten-line rule for unprompted messages does not apply - the same
#: reasoning as `/shopping` at 60.
RECIPE_MAX_LINES = 34
#: `/shopping` is asked for, so it is allowed to be long; the buttons are what Telegram caps.
#: A heavier week (her real list plus ten more real foods, phone_reading/11) reached 61 lines
#: and the whole message was refused - nothing sent at all, on the one command whose entire job
#: is showing the list - so this leaves headroom rather than crashing the handler over one line.
SHOPPING_MAX_LINES = 80
#: the room a shopping line's name, pack, product and dish share, jointly (phone_reading/02,
#: /04): tighter than the general PHONE_LINE_BUDGET because a heavier week's list is one message
#: read start to end - Telegram's own 4000-character cap is the real ceiling on a ~50-line list,
#: not any one line's own readability, so this line budget stays modest even where a single item
#: on its own would have room to spare.
SHOPPING_ITEM_BUDGET = 60
TICK_BUTTONS = 12
#: one per day of the week ahead, in rows of three. More than this and the keyboard is taller
#: than the menu it sits under.
DAY_BUTTONS = 9


#: `/menu` is asked for, so the ten-line rule for unprompted messages does not apply - the same
#: reasoning as `/shopping` at 60 and the recipe card at 34. Ten was cutting a real week in half:
#: cadence is two segments in an ordinary working week.
MENU_MAX_LINES = 24
#: one sentence, one place. It was written three different ways in three branches.
_NO_MENU = "No menu covers {}. /cook builds one now."


def _capped_menu(lines: list[str], config: Any = None, *, linked: bool = True) -> list[str]:
    """Ten lines, with the tenth saying what it could not fit.

    `lines[:10]` was a cliff. Four meals a day plus pre-bed, over today and an accepted upcoming
    segment, builds twelve lines, and two dishes on an accepted menu were neither shown nor
    mentioned - or, when the dishes did fit, "Tap a meal for its recipe." was what fell off,
    leaving a keyboard nothing explained. The house pattern is next door in
    `shopping_list._capped`: the last line says how much is missing.
    """
    if len(lines) <= MENU_MAX_LINES:
        return lines
    held = len(lines) - (MENU_MAX_LINES - 1)
    if not linked:
        # the page 403s a login nobody linked; a day at a time is the route that works for them
        return [*lines[: MENU_MAX_LINES - 1], f"{held} more lines: /menu with a day shows each."]
    where = page_link(config, "the page") if config is not None else "the page"
    return [*lines[: MENU_MAX_LINES - 1], f"{held} more lines: {where} has the whole menu."]


#: a pack label that counts things ("6 eggs", "8 rashers") rather than weighing them
_COUNTED_PACK = re.compile(r"^(\d+)\s+([a-z]+)$")
_NOT_COUNTS = frozenset(
    {"g", "kg", "ml", "l", "litre", "litres", "pack", "tub", "bag", "jar", "tin", "bottle"}
)


def _singular(word: str) -> str:
    if word.endswith("oes") or word.endswith(("ches", "shes", "xes")):
        return word[:-2]
    return word[:-1] if word.endswith("s") else word


#: joins more than one dish on a shared-ingredient line. Never a comma - a dish's own name is
#: full of them ("Chicken, chickpea and feta salad"), so a reader (and `tests/test_journey_
#: phone_reading.py`'s own parser) cannot tell a dish boundary from a dish's own punctuation.
_DISH_SEP = " / "


def _short_dish(name: str) -> str:
    """ "Warm chicken" for "Warm chicken, green bean and potato salad" - the label a shared
    ingredient's line falls back to when it feeds more than one dish (owner_week/10): the full
    name of even ONE dish can use most of a line's budget, so both names in full never both
    survive the water-fill in `_shopping_line_fields`, and one going missing read as the potato
    only being for the salad when it was in the wedges too. Enough to recognise which dish, not
    the whole name a line naming only one dish still gives in full."""
    return " ".join(name.split()[:2])


def _shopping_line_fields(
    name: str, pack_label: str, product: str, dish: str
) -> tuple[str, str, str, str]:
    """Budget a shopping line's four variable lengths, protecting the ingredient's own name and
    pack first: those are what somebody standing at the shelf is actually looking for, and the
    product and dish notes are the ones that can give way (phone_reading/02, /04). Equal water-
    filling across all four sounded fair and in practice shortened "Back bacon, fat trimmed,
    raw" to "Back bacon," to leave room for a dish name nobody was trying to read the item by.
    """
    core = len(name) + len(pack_label)
    if core > SHOPPING_ITEM_BUDGET:
        # even the name and pack alone do not fit - rare, but water-fill everything rather
        # than hand the extras a negative budget
        fields = budget_fields([name, pack_label, product, dish], SHOPPING_ITEM_BUDGET)
        return fields[0], fields[1], fields[2], fields[3]
    product_t, dish_t = budget_fields([product, dish], SHOPPING_ITEM_BUDGET - core)
    return name, pack_label, product_t, dish_t


def counted(grams: float, pack_g: float, pack_label: str) -> str:
    """ "2 eggs" for 116 g out of a box of six; "" when the thing is not counted in whole ones.

    Nobody weighs an egg before work. The pack label already says what a unit is, so the count
    is grams over the unit's share of the pack - arithmetic in code, never the model's.
    """
    m = _COUNTED_PACK.match(pack_label.strip().casefold())
    if m is None or pack_g <= 0:
        return ""
    n, unit = int(m.group(1)), m.group(2)
    if n < 2 or unit in _NOT_COUNTS:
        return ""
    many = grams / (pack_g / n)
    whole = round(many)
    if whole < 1 or abs(many - whole) > 0.2 * whole:
        return ""
    word = _singular(unit) if whole == 1 else unit
    about = "" if abs(many - whole) <= 0.05 * whole else "about "
    return f"{about}{whole} {word}"


#: a pack label naming a volume ("500 ml pouch", "1 l bottle") rather than a weight
_PACK_UNIT = re.compile(r"^([\d.]+)\s*(ml|l|kg|g)\b", re.I)


def _is_liquid(pack_label: str) -> bool:
    """Sold and poured by the millilitre, not weighed. The ingredient's own pack_label
    already says which unit its pack comes in, so a stock pouch's card mirrors that back rather
    than printing the stored MASS figure - the same number, but weighed - as the only unit."""
    m = _PACK_UNIT.match(pack_label.strip())
    return m is not None and m.group(2).lower() in ("ml", "l")


def _kitchen_amount(grams: float, pack_label: str) -> str:
    """The number to read off a scale or a jug: kilograms past a kilo, millilitres for anything
    poured, and never "0 g" for a real pinch - half a gram of chilli flakes at a 0.8
    portion is still something to add to the pot, not nothing. Ordinary amounts (anything that
    does not round all the way down to zero) print exactly as they always have.
    """
    if _is_liquid(pack_label):
        return f"{grams:.0f} ml"
    if grams >= 1000:
        return f"{grams / 1000:.1f} kg".replace(".0 kg", " kg")
    whole = round(grams)
    return f"{whole} g" if whole > 0 or grams <= 0 else f"{grams:.1f} g"


def recipe_card_text(
    card: dict, *, batch_note: str = "", page_line: str = "", warn_line: str = ""
) -> str:
    """Render a recipe's ingredients and method as a Telegram card."""
    per = card.get("per_serving", {})
    lines = [
        f"<b>{esc(str(card['name']))}</b>",
        f"{per.get('kcal', 0)} kcal · P {per.get('protein_g', 0)} g · "
        f"F {per.get('fat_g', 0)} g · C {per.get('carb_g', 0)} g · "
        f"fibre {per.get('fibre_g', 0)} g",
        f"{card.get('portion_g', 0):.0f} g a portion · {card.get('prep_minutes', 0)} min"
        + (f" · serves {card['servings']}" if int(card.get("servings", 1)) > 1 else ""),
    ]
    if warn_line:
        lines.append(warn_line)
    lines.append("<b>Ingredients</b>")
    servings = int(card.get("servings", 1))
    for ing in card.get("ingredients", []):
        # the pack the grams have to come out of, which is what decision 102 asks for and what
        # `recipes.card` has always computed and this renderer threw away. 225 g of mince with
        # one 500 g brick in the freezer is a different job from 225 g out of a 250 g pack.
        pack = str(ing.get("pack_label") or "")
        grams = recipes.card_grams(ing, servings)
        # "1 bun" beats "82 g" at a hob, the way the page says it
        many = recipes.unit_words(ing.get("unit_name"), ing.get("unit_g"), grams) or counted(
            grams, float(ing.get("pack_g") or 0), pack
        )
        lines.append(
            f"{esc(str(ing['name']))} {_kitchen_amount(grams, pack)}"
            + (f", {esc(many)}" if many else "")
            + (f" ({esc(pack)})" if pack else "")
        )
    method = [f"{n}. {esc(str(step))}" for n, step in enumerate(card.get("steps", []), 1)]
    tail = [line for line in (batch_note, page_line) if line]
    keep = int(card.get("keep_days", 0))
    if keep:
        freezes = "; freezes" if card.get("freezable") else ""
        tail.append(f"Keeps {keep} days in the fridge{freezes}.")
    tail.append("Rate it 1-5 and I will propose it more or less often.")
    # `while len(lines) > MAX: lines.pop(-2)` was meant to "drop method steps from the end", and
    # the line above the rating prompt is the fridge-life one - so on a twelve-ingredient,
    # twelve-step recipe from a book, the card
    # silently lost "Keeps 3 days in the fridge; freezes" and nothing hinted anything was cut.
    # Somebody deciding whether Thursday's box is still food is reading for that line.
    room = RECIPE_MAX_LINES - len(lines) - len(tail) - (1 if method else 0)
    if method and len(method) > room:
        held = len(method) - max(0, room - 1)
        method = [*method[: max(0, room - 1)], f"and {held} more steps."]
    if method:
        lines.append("<b>Method</b>")
        lines += method
    return check_text("\n".join([*lines, *tail]), kind="recipe", max_lines=RECIPE_MAX_LINES)


def send_list_text(outbox: Any, db: Db, person: Any, list_id: int) -> list[Any]:
    """Send the shopping list as plain text to the person's chat."""
    text = shopping.forward_text(db, list_id, heading=shopping.list_heading(db, list_id))
    return [outbox.reply(person, part) for part in shopping.message_parts(text)]


from .menuflow_recipe import MenuRecipeFlow  # noqa: E402 (helpers are defined above)
from .menuflow_shopping import MenuShoppingFlow  # noqa: E402 (helpers are defined above)


class MenuFlow(MenuShoppingFlow, MenuRecipeFlow):
    def __init__(self, app: App | None = None, *, clock: Clock = DEFAULT_CLOCK):
        # the App is only needed by /cook, which builds a menu through the model gateway
        self.app = app
        self.clock = clock

    def _config(self, h: Handlers) -> Any:
        """The config, from the App if this flow has one and from the Handlers otherwise."""
        app = self.app or getattr(h, "app", None)
        return getattr(app, "config", None)

    # -- menu decisions -------------------------------------------------------------
    def decide(self, h: Handlers, inbound: Inbound, parts: list[str], accept: bool) -> str:
        if not parts or not parts[0].isdigit():
            return "ignored"
        menu_id = int(parts[0])
        row = h.db.one("SELECT * FROM menus WHERE id = ?", (menu_id,))
        if row is None or row["person_id"] != inbound.person.id:
            return "ignored"
        latest = h.db.one(
            "SELECT decision, message_in_id FROM menu_decisions WHERE menu_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (menu_id,),
        )
        if latest is not None and latest["message_in_id"] is not None:
            # a real, human decision already stands on this exact menu - Change, used once
            # already, or (pre-dating this change) a genuine Accept tap - not the auto-decision
            # every menu now carries from the moment it is built. Checked BEFORE `accept` below:
            # a menu somebody already used Change to reject is not "in force" just because
            # this old message still carries the retired Accept button.
            h.outbox.reply(
                inbound.person,
                (
                    "That menu was already changed. "
                    if str(latest["decision"]) == "rejected"
                    else "That menu is already in force. "
                )
                + "Send <code>/cook replace</code> to build a different one for those days, "
                "or /menu to see what is on it.",
            )
            return "handled"
        if accept:
            # this bullshit with Accept - I thought it just gets created and that's it, we do
            # not need to click more buttons?"). There is nothing left for it to DO, so it only
            # answers, never "that button is not wired" - but the honest answer takes two more
            # facts into account, not just "a decision exists":
            if latest is None:
                # No decision at all, not even the auto-accept `_propose_for` writes in the
                # same breath as sending - realistically a fixture, or a crash between store
                # and decide. Neither "in force" nor "replaced" is true, so say the one thing
                # that is: nothing here was ever finished.
                h.outbox.reply(
                    inbound.person,
                    "That menu was never finished. Send <code>/cook</code> for a fresh one.",
                )
                return "handled"
            # whether THIS menu is still the one governing its days: a later, unrelated rebuild
            # (a fresh `/cook replace`, not a Change tap on this same message) can have replaced
            # it without ever touching its own decision row, and "already in force" would then
            # be a plain lie to whoever is reading an old message.
            current = h.db.one("SELECT 1 FROM v_menu_current WHERE id = ?", (menu_id,)) is not None
            if current:
                h.outbox.reply(
                    inbound.person,
                    f"That menu ({parse_day(str(row['segment_start'])):%a %d %b} to "
                    f"{parse_day(str(row['segment_end'])):%a %d %b}) is already "
                    "in force. Send <code>/cook replace</code> to build a different one for "
                    "those days, or /menu to see what is on it.",
                )
            else:
                h.outbox.reply(
                    inbound.person,
                    "A newer menu has already replaced that one for those days. "
                    "/menu shows what is current now.",
                )
            return "handled"
        if self.busy_with_something_else(h.db, inbound.person.id):
            h.outbox.reply(
                inbound.person,
                "Answer the question above first, then tap Change again. "
                "The menu stays as it is until then.",
            )
            return "handled"
        from ..food import plates as plates_mod  # noqa: PLC0415 (food imports the db layer)

        # a resized day's own menu is one day: a change to "the menu" means the week it was cut
        # from, not a 2-4 minute model build that replaces one day
        menu_id = plates_mod.whole_menu_of(h.db, menu_id)
        sent = h.outbox.reply(
            inbound.person,
            "Tell me what to change - a dish, an ingredient, more fish, no rice, a different "
            "lunch - and I will build it now. Name one meal and only that meal changes. The "
            "current menu stays until the new one is ready.",
            keyboard=inline_keyboard(
                [
                    *self._one_day_rows(h, inbound.person.id, menu_id),
                    [
                        ("Just a new menu", f"k:rebuild:{menu_id}"),
                        ("Keep this one", f"k:keepmenu:{menu_id}"),
                    ],
                ]
            ),
        )
        # the prompt's own message, so a typed change can take its buttons off (`change_text`)
        self.arm_change(h.db, inbound.person.id, menu_id, prompt=sent.telegram_message_id)
        return "handled"

    def _one_day_rows(
        self, h: Handlers, person_id: int, menu_id: int
    ) -> list[list[tuple[str, str]]]:
        """For someone whose week is planned in one build: a button a weekday still to come, to
        rebuild that day alone."""
        from ..food import weekplan  # noqa: PLC0415 (food imports the db layer)

        if weekplan.of(h.db, person_id).plan_day is None:
            return []
        row = h.db.one("SELECT segment_start, segment_end FROM menus WHERE id = ?", (menu_id,))
        if row is None:
            return []
        today = self.clock.today_local()
        first, last = parse_day(str(row["segment_start"])), parse_day(str(row["segment_end"]))
        days = [
            first + dt.timedelta(days=i)
            for i in range((last - first).days + 1)
            if first + dt.timedelta(days=i) > today and (first + dt.timedelta(days=i)).weekday() < 5
        ]
        if len(days) < 2:
            return []
        buttons = [(f"Only {d:%a}", f"k:rebuild:{menu_id}:{isoday(d)}") for d in days]
        return [buttons[i : i + 3] for i in range(0, len(buttons), 3)]

    def rebuild(self, h: Handlers, inbound: Inbound, parts: list[str]) -> str:
        """k:rebuild:<menu id>[:<day>] - a new menu for that menu's days, or that one day of it,
        with nothing to say about it."""
        if not parts or not parts[0].isdigit():
            return "ignored"
        row = h.db.one("SELECT person_id FROM menus WHERE id = ?", (int(parts[0]),))
        if row is None or int(row["person_id"]) != inbound.person.id:
            return "ignored"
        from ..food import plates as plates_mod  # noqa: PLC0415 (food imports the db layer)

        h.clear_buttons(inbound)
        armed = self._change_state(h.db, inbound.person.id)
        if armed is None or armed.get("menu_id") != int(parts[0]):
            # an old prompt's button: the change was already answered (typed, kept, or this
            # tapped twice), and a second build with no note would replace the menu just built
            # around what they said
            return self._change_answered(h, inbound, menu_id=int(parts[0]))
        self.disarm_change(h.db, inbound.person.id)
        # the week a resized day was cut from, never that one day alone
        whole = plates_mod.whole_menu_of(h.db, int(parts[0]))
        only: dt.date | None = None
        if len(parts) > 1:
            try:
                only = parse_day(parts[1])
            except ValueError:
                only = None
        return self._build(h, inbound, note="", replace=True, for_menu=whole, only_day=only)

    def wish_build(self, h: Handlers, inbound: Inbound, parts: list[str]) -> str:
        """k:wishbuild:<note ids> - "Rebuild now" under a wish just kept ("less chia seed"): the
        menu she has, built again with it, as `/cook replace less chia seed` would. Only this
        button goes, so Undo stays under the wish; a wish taken back builds nothing."""
        from .foodflow import drop_tapped, notes_by_ids  # noqa: PLC0415 (foodflow imports this)

        wishes = notes_by_ids(h.db, inbound.person.id, parts)
        drop_tapped(h, inbound)
        if not wishes:
            h.outbox.reply(
                inbound.person,
                "That was taken back, so there is nothing new to build with. "
                "<code>/cook replace</code> builds a different menu any time.",
            )
            return "handled"
        return self._build(h, inbound, note="; ".join(wishes), replace=True)

    def keep_menu(self, h: Handlers, inbound: Inbound, parts: list[str]) -> str:
        """
        no id."""
        h.clear_buttons(inbound)
        tapped = int(parts[0]) if parts and parts[0].isdigit() else None
        armed = self._change_state(h.db, inbound.person.id)
        if armed is None or tapped not in (None, armed.get("menu_id")):
            return self._change_answered(h, inbound, menu_id=tapped)
        self.disarm_change(h.db, inbound.person.id)
        return self._change_answered(h, inbound, menu_id=armed.get("menu_id"), kept=True)

    def _change_answered(
        self, h: Handlers, inbound: Inbound, *, menu_id: int | None, kept: bool = False
    ) -> str:
        """A tap on the Change prompt for `menu_id` that is no longer open, or (`kept`) Change
        taken back. When a menu asked for is still building - a change typed there, or a /cook
        replace or the page's Build from before the Change - "Kept the menu as it is" was false:
        the new menu landed minutes later and replaced the one the bot had just said was kept."""
        now = building.current(h.db, inbound.person.id, clock=self.clock)
        if now is not None and now["kind"] == building.MENU and now["asked"]:
            if menu_id is not None and now["menu"] == menu_id:
                text = (
                    f"A new menu is already being built ({building.TAKES}), and it replaces this "
                    "one when it is ready. /menu shows the one in force until then."
                )
            else:
                # a build for another menu's days, or for days only the build works out (a bare
                # /cook replace): "it replaces this one" was said of a weekend it never touched,
                # so whether it does is not said
                text = (
                    ("Change taken back" if kept else "That was already answered")
                    + ". The new menu you asked for earlier is still being built "
                    f"({building.TAKES}) and comes here when it is ready; /menu shows the one "
                    "in force until then."
                )
        elif kept:
            text = "Kept the menu as it is. /menu shows it."
        else:
            text = (
                "That was already answered. /menu shows the menu in force; Change on it builds "
                "a different one."
            )
        h.outbox.reply(inbound.person, text)
        return "handled"

    # -- the change note -----------------------------------------------------------------
    def _change_state(self, db: Db, person_id: int) -> dict | None:
        row = db.one(
            "SELECT state_json, updated_at FROM conversation_state WHERE person_id = ?",
            (person_id,),
        )
        if row is None:
            return None
        st = json.loads(row["state_json"])
        if st.get("flow") != CHANGE_FLOW:
            return None
        # a flow armed yesterday must not swallow tomorrow's unrelated message
        age = (self.clock.now_utc() - parse_stamp(str(row["updated_at"]))).total_seconds()
        if age > CHANGE_TTL:
            self.disarm_change(db, person_id)
            return None
        return st

    def busy_with_something_else(self, db: Db, person_id: int) -> bool:
        return convstate.busy_with_other_than(db, person_id, CHANGE_FLOW, clock=self.clock)

    def arm_change(
        self, db: Db, person_id: int, menu_id: int | None = None, *, prompt: int | None = None
    ) -> None:
        # The menu whose Change button was tapped supplies the days to rebuild.
        state: dict[str, object] = {"flow": CHANGE_FLOW}
        if menu_id is not None:
            state["menu_id"] = menu_id
        if prompt is not None:
            state["prompt"] = prompt
        db.execute(
            "INSERT INTO conversation_state(person_id, state_json, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(person_id) DO UPDATE SET state_json = excluded.state_json, "
            "updated_at = excluded.updated_at",
            (person_id, json.dumps(state), self.clock.utc_stamp()),
        )

    def disarm_change(self, db: Db, person_id: int) -> None:
        """Close the change question, and only that: `conversation_state` holds one flow per
        person, and an old Keep button deleted a half-answered check-in or measure with it."""
        row = db.one("SELECT state_json FROM conversation_state WHERE person_id = ?", (person_id,))
        if row is None:
            return
        try:
            flow = json.loads(row["state_json"]).get("flow")
        except ValueError, AttributeError:
            return
        if flow == CHANGE_FLOW:
            db.execute(
                "DELETE FROM conversation_state WHERE person_id = ? AND state_json = ?",
                (person_id, row["state_json"]),
            )

    def change_text(self, h: Handlers, inbound: Inbound, text: str) -> str | None:
        """The message after a Change tap is the change, and it is acted on immediately."""
        armed = self._change_state(h.db, inbound.person.id)
        if armed is None:
            return None
        note = text.strip()
        if convstate.looks_like_a_question(note):
            # A question is not a change request. Keep the flow armed for the answer to
            # "what should change?" in the next message.
            return None
        self.disarm_change(h.db, inbound.person.id)
        # the question is answered, so its buttons go: left live, "Keep this one" said the menu
        # was kept while the change typed here was building, and "Just a new menu" built a
        # second one over it
        prompt = armed.get("prompt")
        if isinstance(prompt, int):
            try:
                h.outbox.api.edit_reply_markup(inbound.chat_id, prompt, None)
            except Exception:
                logger.warning("change prompt kept its buttons", extra={"event": "buttons_kept"})
        tapped = armed.get("menu_id")
        if not note or note.lower() in ("skip", "later", "no", "nothing", "never mind"):
            return self._change_answered(h, inbound, menu_id=tapped, kept=True)
        if tapped:
            # "a different lunch": only the meal named goes to the model, as two or three new
            # dishes to choose from; the rest of the menu is not rebuilt to change one meal
            one = self._one_meal(h, inbound, int(tapped), note)
            if one is not None:
                return one
        return self._build(
            h, inbound, note=note, replace=True, for_menu=int(tapped) if tapped else None
        )

    def _one_meal(self, h: Handlers, inbound: Inbound, menu_id: int, note: str) -> str | None:
        """New dishes for the one meal a change note names, or None when it names none or
        several (a whole-menu change).

        One day named ("a different lunch on thursday") is that day alone, its swap buttons
        carrying ":1": the offer covered the whole run and one tap replaced every lunch.
        Several named is the first of them alone, and the answer says the others
        need asking again: they fell back to every day still to come, Monday's lunch with
        them. No day named is every day still to come."""
        from ..food import prefs  # noqa: PLC0415 (food imports the db layer)
        from ..jobs import menu_propose  # noqa: PLC0415 (jobs import the scheduler)
        from .foodflow import start_new_dishes  # noqa: PLC0415 (foodflow imports this module)

        named = slots_named(note)
        if len(named) != 1:
            return None
        slot = next(iter(named))
        person = inbound.person
        row = h.db.one(
            "SELECT segment_start, segment_end FROM menus WHERE id = ? AND person_id = ?",
            (menu_id, person.id),
        )
        if row is None:
            return None
        today = self.clock.today_local()
        first = max(parse_day(str(row["segment_start"])), today)
        end = parse_day(str(row["segment_end"]))
        from ..food import swap  # noqa: PLC0415 (food imports the db layer)

        run = [
            d
            for d in (first + dt.timedelta(days=i) for i in range((end - first).days + 1))
            if not menu_mod.day_confirmed(h.db, person.id, d)
            and any(p.slot == slot for p in swap.plates_on(h.db, person.id, d))
        ]
        days = days_in_note(note, today, run)
        on_menu = sorted(d for d in days if d in run)
        off_menu = sorted(d for d in days if d not in run)
        if not days and not run:
            return None
        # from here this note never reaches a build, and a refusal in it is written and said
        # here or nowhere: "I don't want lentils any more, give me a different lunch" changed the
        # lunch, kept the lentils on the list the model picked from, and on every other menu
        #
        said, rows = self._note_refusal(h, person, note)
        meal = menu_propose.SLOT_LABELS.get(slot, slot).lower()
        if days and not on_menu:
            lines = [said] if said else []
            lines.append(
                f"No new dishes asked for: that menu has no {meal} on "
                f"{prefs.days_words(off_menu)} still to change. /menu shows the days it covers."
            )
            h.outbox.reply(
                person, "\n".join(lines), keyboard=inline_keyboard(rows) if rows else None
            )
            return "handled"
        if said:
            h.outbox.reply(person, said, keyboard=inline_keyboard(rows) if rows else None)
        lead = "Only that meal changes; the rest of the menu stays."
        if len(on_menu) > 1:
            lead += (
                f" These are for {on_menu[0]:%a %d %b} alone: for "
                f"{prefs.days_words(on_menu[1:])}, ask again once you have picked."
            )
        if off_menu:
            lead += f" That menu has no {meal} on {prefs.days_words(off_menu)} to change."
        return start_new_dishes(
            h,
            self.app,
            person,
            on_menu[0] if on_menu else run[0],
            slot,
            every_day=not on_menu,
            note=note,
            clock=self.clock,
            lead=lead,
        )

    def _note_refusal(
        self, h: Handlers, person: Any, note: str
    ) -> tuple[str, list[list[tuple[str, str]]]]:
        """A refusal in a change note that no menu build will read, written to the avoid list:
        the words that say so and the made-from questions' buttons, or ("", []) when it refuses
        nothing new. A build writes and says its own (`menu_propose._propose_for`)."""
        from ..jobs import menu_propose  # noqa: PLC0415 (jobs import the scheduler)
        from .foodflow import ask_rows  # noqa: PLC0415 (foodflow imports this module)

        refused = menu_propose.note_refusal(h.db, person, note, clock=self.clock)
        if refused is None:
            return "", []
        return menu_propose.refusal_said(refused, buttons=True), ask_rows(refused.asks)

    # -- shopping ticks --------------------------------------------------------------
    def tick(self, h: Handlers, inbound: Inbound, parts: list[str]) -> str:
        if not parts or not parts[0].isdigit():
            return "ignored"
        item_id = int(parts[0])
        # their own list, or a joint one with their food on it: either owner may tick that
        if not shopping.item_readable(h.db, item_id, inbound.person.id):
            return "ignored"
        last = h.db.one(
            "SELECT ticked FROM shopping_ticks WHERE item_id = ? AND person_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (item_id, inbound.person.id),
        )
        ticked = 0 if (last is not None and int(last["ticked"]) == 1) else 1
        h.db.insert("shopping_ticks", item_id=item_id, person_id=inbound.person.id, ticked=ticked)
        h.answer(inbound, "Ticked" if ticked else "Unticked")
        return "handled"

    # -- ratings ----------------------------------------------------------------------
    def rate(self, h: Handlers, inbound: Inbound, parts: list[str]) -> str:
        if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
            return "ignored"
        recipe_id, score = int(parts[0]), int(parts[1])
        if not RATE_MIN <= score <= RATE_MAX:
            return "ignored"
        row = h.db.one("SELECT name FROM recipes WHERE id = ?", (recipe_id,))
        if row is None:
            return "ignored"
        h.db.insert(
            "ratings",
            person_id=inbound.person.id,
            recipe_id=recipe_id,
            score=score,
            message_in_id=inbound.message_in_id,
        )
        tail = (
            " It will come back often."
            if score >= 4
            else (" I will stop proposing it." if score <= 2 else "")
        )
        # deliberately no clear_buttons: the card's keyboard is also its only way back to
        # the menu, and clearing it on a rating left the person stuck on the card
        h.answer(inbound, f"{row['name']}: {score}/5")
        h.outbox.reply(inbound.person, f"{esc(str(row['name']))}: {score}/5.{tail}")
        return "handled"

    # -- commands ----------------------------------------------------------------------
    def cmd_kit(self, h: Handlers, inbound: Inbound, arg: str) -> str:
        """`/kit` shows what Tiffin thinks you can cook with; `/kit oven, air fryer` sets it.

        Anyone who onboarded before the kitchen question existed has an empty list, and the
        only other way to answer it is redoing all twenty-five questions. Setting it writes a
        new profile row, because profiles are a ledger.
        """
        from ..food.menu import equipment_of  # noqa: PLC0415
        from ..onboarding import parse_kit  # noqa: PLC0415

        person = inbound.person
        current = targets_mod.profile_on(h.db, person.id, self.clock.today_local())
        if current is None:
            h.outbox.reply(person, "Finish /onboard first.")
            return "handled"
        if not arg.strip():
            have = equipment_of(current)
            h.outbox.reply(
                person,
                ("You can cook with: " + esc(", ".join(have)) + ".")
                if have
                else "I do not know what you can cook with, so menus assume an oven and a hob.",
            )
            h.outbox.reply(
                person,
                "Send <code>/kit oven, hob, air fryer</code> to set it. Anything not on that "
                "list is fine, just name it.",
            )
            return "handled"
        kit = parse_kit(arg, {})
        targets_mod.change_profile(
            h.db,
            person.id,
            {"equipment_json": json.dumps(kit)},
            today=self.clock.today_local(),
        )
        h.db.insert(
            "onboarding_answers",
            person_id=person.id,
            step="kit",
            answer_json=json.dumps(kit),
            message_in_id=inbound.message_in_id,
        )
        h.outbox.reply(
            person,
            (
                "Noted: " + esc(", ".join(k.replace("_", " ") for k in kit)) + ". Menus will only "
                "use methods that work with those."
            )
            if kit
            else "Cleared. Menus will assume an oven and a hob.",
        )
        return "handled"

    def cmd_cook(self, h: Handlers, inbound: Inbound, arg: str) -> str:
        """`/cook` asks for a menu now, instead of waiting for the scheduled proposal.

        `/cook repeat` (or `same`, or `last`) copies the last menu onto those days instead of
        building one: no model call, no solve, no wait.

        The scheduled job only fires two days before a cook segment starts, so someone who
        onboarded mid-week, ignored Saturday's proposal, or just wants to re-plan had no way
        to get a menu at all. A model call costs real money, so a second request inside five
        minutes is refused rather than queued.
        """
        # The replace word is a PREFIX, not the whole argument. Matching only the whole thing
        # meant `/cook replace no chickpeas` - which is what the bot itself tells people to
        # send - was read as a change request literally named "replace no chickpeas", and that
        # is what went to the model as the thing to cook around.
        words = arg.strip().split()
        # "same as last week" is an ordinary Sunday, and until now nothing a person could type
        # reached `menu.repeat_last` - it was only ever the fallback for a build that failed.
        # Costs no model call and arrives in force, because a repeat always has (the owner, 20 Sep
        # 2026: "I am also more than happy to have the same menu as last week if I choose").
        if words and words[0].lower() in ("repeat", "same", "last"):
            return self._repeat(h, inbound)
        replace = bool(words) and words[0].lower() in ("replace", "again", "new")
        # `/cook more fish` is a request, not a typo: anything that is not a replace word is
        # carried into the prompt rather than dropped on the floor, and asking for something
        # different is itself a reason to replace a menu that already covers those days.
        note = " ".join(words[1:] if replace else words)
        return self._build(h, inbound, note=note, replace=replace or bool(note))

    def _repeat(self, h: Handlers, inbound: Inbound) -> str:
        """`/cook repeat`: last week's food on next week's days, with no model call."""
        from ..jobs import menu_propose  # noqa: PLC0415 (jobs import the scheduler)

        person = inbound.person
        profile = targets_mod.profile_on(h.db, person.id, self.clock.today_local())
        if profile is None:
            h.outbox.reply(person, "Finish /onboard first and I can plan a cook.")
            return "handled"
        if self.app is None:
            h.outbox.reply(person, "I cannot build a menu from here.")
            return "handled"
        now = building.current(h.db, person.id, clock=self.clock)
        if now is not None and now["kind"] == building.MENU:
            # the build in flight lands after the repeat and replaces it without a word, so the
            # earlier request would win over the later one
            h.outbox.reply(
                person,
                building.busy(now) + " Send <code>/cook repeat</code> after that if you would "
                "rather have last week's again.",
            )
            return "handled"
        try:
            menu_propose.repeat_now(self.app, h.db, person, profile, h.outbox, clock=self.clock)
        except NothingToBuild as e:
            h.outbox.reply(person, str(e))
            return "handled"
        except AlreadyDone:
            h.outbox.reply(person, f"Already working on that one ({building.TAKES}).")
            return "handled"
        return "handled"

    def _busy(self, h: Handlers, person: Any, running: dict[str, Any], note: str) -> str:
        """A menu asked for while a build runs: which build is running, and - since nothing
        queues behind it - that this change was not started. A refusal in the note cannot wait
        for that: it is written and said now. "/cook replace no chickpeas", the very command the
        cooldown suggests, was refused and the chickpeas were lost without a word."""
        lines = [building.busy(running)]
        rows: list[list[tuple[str, str]]] = []
        if note:
            said, rows = self._note_refusal(h, person, note)
            if said:
                lines.append(said)
            lines.append(
                "Anything else in that change was not started: ask for it again once this one "
                "is done."
            )
        h.outbox.reply(person, "\n".join(lines), keyboard=inline_keyboard(rows) if rows else None)
        return "handled"

    def _build(
        self,
        h: Handlers,
        inbound: Inbound,
        *,
        note: str,
        replace: bool,
        for_menu: int | None = None,
        only_day: dt.date | None = None,
    ) -> str:
        from ..jobs import menu_propose  # noqa: PLC0415 (jobs import the scheduler)

        person = inbound.person
        profile = targets_mod.profile_on(h.db, person.id, self.clock.today_local())
        if profile is None:
            h.outbox.reply(person, "Finish /onboard first and I can plan a cook.")
            return "handled"
        running = building.current(h.db, person.id, clock=self.clock)
        if running is not None:
            # before the cooldown, whose answer ("say what to change ... and I will start now")
            # is not true while a build runs
            return self._busy(h, person, running, note)
        key = f"cook_asked:{person.id}"
        row = h.db.one("SELECT v FROM bot_state WHERE k = ?", (key,))
        now = self.clock.now_utc()
        # A request carrying a note is never an accidental repeat: it says something the last
        # one did not. The cooldown exists to stop a double-tapped bare /cook spending twice,
        # and it was instead refusing the exact command Tiffin had just told the person
        # to send - then offering that same command back as the way to retry, which walked
        # them into the same wall. Accepting a menu, being told "send /cook replace no
        # chickpeas", and being refused twice is not a cost problem; it is Tiffin arguing
        # with itself. Nor is "Just a new menu" (`for_menu`): a tap on the menu it replaces, not
        # a double-tapped /cook, and the bare /cook the refusal offered builds nothing there.
        if row is not None and not note and for_menu is None:
            age = (now - parse_stamp(str(row["v"]))).total_seconds()
            if age < COOK_COOLDOWN_S:
                wait = max(1, round((COOK_COOLDOWN_S - age) / 60))
                minutes = "a minute" if wait == 1 else f"{wait} minutes"
                h.outbox.reply(
                    person,
                    f"Still working on the last one. Give it {minutes}, then send "
                    "<code>/cook</code> again - or say what to change, like "
                    "<code>/cook replace no chickpeas</code>, and I will start now.",
                )
                return "handled"
        if self.app is None:
            h.outbox.reply(person, "I cannot build a menu from here.")
            return "handled"
        h.outbox.reply(
            person,
            "Working out what to cook" + (f", with {esc(note)}" if note else "") + ". "
            "This usually takes 2-4 minutes; the menu comes here, and everything else still "
            "answers in the meantime.",
        )
        # the cooldown guards model spend: stamped as the build starts ("a menu is coming"),
        # taken off again below when none came. A person's builds run one at a time, in order,
        # on their own lane, so the stamp still means what it says.
        h.db.execute(
            "INSERT INTO bot_state(k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v",
            (key, self.clock.utc_stamp()),
        )
        app = self.app
        token = building.mark(
            h.db, person.id, note or "a new menu", clock=self.clock, menu=for_menu
        )

        def build(db: Db) -> None:
            outbox = h.outbox if db is h.db else app.outbox(db)
            fresh = targets_mod.profile_on(db, person.id, self.clock.today_local())
            try:
                says = menu_propose.propose_now(
                    app,
                    db,
                    person,
                    fresh if fresh is not None else profile,
                    outbox,
                    clock=self.clock,
                    replace=replace,
                    note=note,
                    for_menu=for_menu,
                    only_day=only_day,
                )
            except NothingToBuild as e:
                # nothing was spent, so nothing is on cooldown: one of these answers is "send
                # /cook replace", and that command has to work when they send it
                db.execute("DELETE FROM bot_state WHERE k = ?", (key,))
                outbox.reply(person, str(e))
                return
            except AlreadyDone:
                # `propose_now` builds its ledger slot from a stamp to the SECOND, so a
                # double-tapped /cook collides with itself and `ledger.run` refuses the second.
                outbox.reply(person, f"Already working on that one ({building.TAKES}).")
                return
            # `propose_now` returns "" when a menu went out and a sentence when none did, and the
            # sentence for a failed change is "...or send /cook to take the ordinary one" - a
            # BARE /cook, the one form the cooldown refuses. A cooldown means "a menu is already
            # coming, wait for it"; when none is coming there is nothing to wait for.
            if says:
                db.execute("DELETE FROM bot_state WHERE k = ?", (key,))
                # HTML Tiffin wrote, so no esc() here
                outbox.reply(person, says)

        def forget_the_ask(db: Db) -> None:
            # a build that raised spent nothing that came to anything: a bare /cook must work
            db.execute("DELETE FROM bot_state WHERE k = ?", (key,))

        app.run_build(
            person.id,
            building.guarded(
                app,
                person,
                token,
                build,
                broke="Something went wrong building that menu and it did not finish. /menu "
                "shows what is planned; send /cook to try again.",
                on_crash=forget_the_ask,
            ),
            h.db,
        )
        return "handled"

    def cmd_menu(self, h: Handlers, inbound: Inbound, arg: str) -> str:
        """Today's meals, with a button per meal.

        The proposal message carries meal buttons and says "Tap a meal for its recipe"; /menu
        printed the same dish names as flat text and offered no way in. So the only route to a
        recipe was /recipe, which gives the day's MAIN meal and nothing else, or /recipe <id>
        with an id nobody is shown. Breakfast on today's menu was unreachable once the original
        proposal had scrolled away.
        """

        person = inbound.person
        today = self.clock.today_local()
        want = day_named(arg, today)
        asked = day_and_meal(arg, today) if want is None and arg.strip() else None
        if asked is not None:
            # "/menu thursday lunch" is a question about one meal, not a malformed day
            return self._open_meal_on(h, inbound, *asked)
        if want is None:
            example = isoday(today + dt.timedelta(days=1))
            h.outbox.reply(
                person,
                f"I did not read {esc(arg.strip()[:30])} as a day. Try <code>/menu tomorrow</code>"
                f", <code>/menu thu</code> or <code>/menu {example}</code>.",
            )
            return "handled"
        if arg.strip():  # one named day: that day's meals, and a button per meal on it
            return self._one_day(h, person, want)
        return self._week_ahead(h, person, today)

    def _one_day(self, h: Handlers, person: Any, want: dt.date) -> str:
        """`/menu thu`, and the shape everything else uses: ONE day, its meals, its recipes."""
        today = self.clock.today_local()
        menu_id = menu_mod.accepted_menu_id(h.db, person.id, want)
        if menu_id is None:
            lines = [f"<b>Menu {want:%a %d %b}</b>", _NO_MENU.format(f"{want:%a %d %b}")]
            # A day with nothing on it was a dead end with no button at all - asking about
            # Thursday when only Mon-Wed is planned left nowhere to go but typing another
            # command (phone_reading/08). The days that DO have a menu are one tap away either
            # way, so give the same day row `_send_day` always attaches.
            day_row = self._day_buttons(h, person, today)
            h.outbox.reply(
                person,
                check_text("\n".join(lines), kind="menu", max_lines=MENU_MAX_LINES),
                keyboard=inline_keyboard(day_row) if day_row else None,
            )
            return "handled"
        return self._send_day(h, person, menu_id, want, today=today)

    def _send_day(
        self, h: Handlers, person: Any, menu_id: int, day: dt.date, *, today: dt.date
    ) -> str:
        """Render one day's meals with day and meal buttons."""
        from ..jobs import menu_propose  # noqa: PLC0415 (jobs import the scheduler)

        rows = h.db.all(
            "SELECT ms.slot, rc.name FROM menu_days md "
            "JOIN meal_slots ms ON ms.menu_day_id = md.id "
            "JOIN recipes rc ON rc.id = ms.recipe_id WHERE md.menu_id = ? AND md.day = ? "
            "ORDER BY CASE ms.slot WHEN 'breakfast' THEN 0 WHEN 'lunch' THEN 1 "
            "WHEN 'dinner' THEN 2 WHEN 'snack' THEN 3 ELSE 4 END, ms.id",
            (menu_id, isoday(day)),
        )
        lines = [f"<b>Menu {day:%a %d %b}</b>"]
        for r in rows:
            label = menu_propose.SLOT_LABELS.get(str(r["slot"]), str(r["slot"]))
            lines.append(f"{label}: {esc(str(r['name']))}")
        if not rows:
            lines.append(_NO_MENU.format(f"{day:%a %d %b}"))
        lines += self._refusal_note(h, person, [menu_id])

        day_row = self._day_buttons(h, person, today)
        meals = menu_propose.meal_buttons(h.db, menu_id, day=day)
        keyboard = [*day_row, *meals]
        if meals:
            lines.append(
                "Tap a day, or a meal for its recipe." if day_row else "Tap a meal for its recipe."
            )
        cfg = self._config(h)
        if (
            cfg is not None
            and cfg.get("web.identity_mode", "header") == "telegram"
            and cfg.get("web.enabled", True)
        ):
            keyboard.append([("Open web page", "k:web")])
        if (
            rows
            and cfg is not None
            and person.tailscale_login
            and cfg.get("web.identity_mode", "header") == "header"
        ):
            # this day on the page, every recipe unrolled - for someone the page lets in
            link = menu_link(cfg, isoday(day), label="This day on the page")
            if link:
                lines.append(f"{link}, every recipe unrolled.")
        h.outbox.reply(
            person,
            check_text(
                "\n".join(
                    _capped_menu(
                        lines,
                        cfg,
                        linked=bool(person.tailscale_login)
                        and cfg is not None
                        and cfg.get("web.identity_mode", "header") == "header",
                    )
                ),
                kind="menu",
                max_lines=MENU_MAX_LINES,
            ),
            keyboard=inline_keyboard(keyboard) if keyboard else None,
        )
        return "handled"

    def _next_cook_day(
        self, h: Handlers, person: Any, today: dt.date
    ) -> tuple[int, dt.date] | None:
        """(menu id, day) for the day bare `/menu` should open.

        Not simply "the first day from today with a menu": a day's own accepted_menu_id was
        looked up once per PROFILE segment and reused for every day in it, so a Tue-Fri accept
        inside a Mon-Fri segment answered with Monday's OLD dish under a "Tue to Fri" heading
        the moment it existed. Re-resolving per day fixes that, but bare `/menu` also has to
        surface a change that was JUST made: the freshest decision that still has days ahead of
        it wins, clipped to today, so accepting a change one moment and checking `/menu` the
        next shows the change rather than a today that was never touched by it.
        """
        row = h.db.one(
            "SELECT segment_start FROM v_menu_current WHERE person_id = ? AND segment_end >= ? "
            "ORDER BY decided_at DESC, id DESC LIMIT 1",
            (person.id, isoday(today)),
        )
        if row is None:
            return None
        day = max(today, parse_day(str(row["segment_start"])))
        menu_id = menu_mod.accepted_menu_id(h.db, person.id, day)
        return None if menu_id is None else (menu_id, day)

    def _day_buttons(self, h: Handlers, person: Any, today: dt.date) -> list[list[tuple[str, str]]]:
        """One button per day ahead that has a menu, in rows of three.

        Re-resolves accepted_menu_id for EACH day rather than once per profile segment: a menu
        built for part of a segment (a Tue-Fri change inside a Mon-Fri cadence) covers some of
        that segment's days under one id and the rest - Monday, untouched - under another.
        """
        profile = targets_mod.profile_on(h.db, person.id, today)
        found: list[tuple[str, str]] = []
        for seg in self._segments_ahead(profile, today, weeks=menu_mod.weeks_of(h.db, person.id)):
            for day in seg.days:
                if day < today or len(found) >= DAY_BUTTONS:
                    continue
                menu_id = menu_mod.accepted_menu_id(h.db, person.id, day)
                if menu_id is None:
                    continue
                found.append((f"{day:%a %d}", f"k:mday:{menu_id}:{day:%Y%m%d}"))
        return button_rows(found)

    def _week_ahead(self, h: Handlers, person: Any, today: dt.date) -> str:
        """Bare `/menu` - the next cook DAY, with buttons to every other day that has a menu."""
        found = self._next_cook_day(h, person, today)
        if found is None:
            lines = [
                f"<b>Menu {today:%a %d %b}</b>",
                "No menu covers the days ahead. /cook builds one now.",
            ]
            h.outbox.reply(
                person, check_text("\n".join(lines), kind="menu", max_lines=MENU_MAX_LINES)
            )
            return "handled"
        menu_id, day = found
        return self._send_day(h, person, menu_id, day, today=today)

    @staticmethod
    def _segments_ahead(
        profile: Any, today: dt.date, *, weeks: menu_mod.Weeks | None = None
    ) -> list[menu_mod.Segment]:
        """Every cook segment from the one covering today to the end of next week."""
        from ..clock import week_start  # noqa: PLC0415 (a small helper, avoids a cycle)

        walk = weeks or menu_mod.profile_weeks(profile)
        out: list[menu_mod.Segment] = []
        for base in (week_start(today), week_start(today) + dt.timedelta(days=7)):
            for seg in walk(base):
                if seg.end >= today and seg not in out:
                    out.append(seg)
        return out

    def _refusal_note(self, h: Handlers, person: Any, menu_ids: list[int]) -> list[str]:
        """Explain when a menu still includes a food the person has since refused."""
        if not menu_ids:
            return []
        from ..food import catalogue  # noqa: PLC0415 (food imports the db layer)

        # the same function the page uses, so the two can never name different foods
        names = few(catalogue.refused_names_on(h.db, person.id, menu_ids))
        if not names:
            return []
        return [
            f"This menu still has {esc(names)} in it, which you now avoid. "
            "<code>/cook replace</code> rebuilds it without."
        ]

    def _list_change_note(self, h: Handlers, person: Any, menu_id: int, list_id: int) -> str | None:
        """What changed since the list this one replaced, for the SAME days.

        `shopping.superseded_list` already finds that prior list (it is how `build()` credits
        what was actually bought rather than the naive plan) - this reads it a second time
        purely to compare ingredients, so a reread after Accept says which of the things already
        in the basket are gone and what is new, instead of repeating the same header shape as
        any other list (list_vs_menu/03).
        """
        prior = shopping.superseded_list(h.db, person.id, menu_id)
        if prior is None or int(prior["id"]) == list_id:
            return None

        def named(for_list_id: int) -> set[str]:
            return {
                str(r["name"])
                for r in h.db.all(
                    "SELECT DISTINCT i.name FROM shopping_items si "
                    "JOIN ingredients i ON i.id = si.ingredient_id "
                    "WHERE si.list_id = ? AND si.packs > 0",
                    (for_list_id,),
                )
            }

        before, after = named(int(prior["id"])), named(list_id)
        dropped, added = len(before - after), len(after - before)
        if not dropped and not added:
            return None
        # deliberately NOT naming the foods themselves: /menu already named exactly what it
        # dropped ("This menu still has Chickpeas...") when the new menu was proposed, and a
        # basket-side note repeating a refused food's name here would read as offering it again.
        # Counting what changed, without naming it, still answers "is this the same list" -
        # which is the actual dead end (list_vs_menu/03: two lists with an identical header sat
        # in her chat and nothing told her they differed at all).
        bits = [
            *([f"{dropped} no longer needed"] if dropped else []),
            *([f"{added} new"] if added else []),
        ]
        return f"This list replaces the one already sent for these days ({', '.join(bits)})."


def install(
    handlers: Handlers, app: App | None = None, *, clock: Clock = DEFAULT_CLOCK
) -> MenuFlow:
    from .manual_plan import command as manual_plan_command  # noqa: PLC0415
    from .manual_plan import recipe_text  # noqa: PLC0415

    flow = MenuFlow(app, clock=clock)
    handlers.register_callback("menu", lambda h, i, p: flow.decide(h, i, p, True))
    handlers.register_callback("menux", lambda h, i, p: flow.decide(h, i, p, False))
    handlers.register_callback("wishbuild", flow.wish_build)
    handlers.register_callback("rebuild", flow.rebuild)
    handlers.register_callback("keepmenu", flow.keep_menu)
    handlers.register_callback("tick", flow.tick)
    handlers.register_callback("rate", flow.rate)
    handlers.register_callback("meal", flow.show_meal)
    handlers.register_command("page", flow.cmd_page)
    handlers.register_callback("menuv", flow.show_menu)
    handlers.register_callback("mday", flow.show_day)
    handlers.register_callback("menuw", flow.show_week)
    handlers.register_command("cook", flow.cmd_cook)
    handlers.register_command("kit", flow.cmd_kit)
    handlers.register_command("menu", flow.cmd_menu)
    handlers.register_command("plan", manual_plan_command)
    handlers.register_command("shopping", flow.cmd_shopping)
    handlers.register_command("shop", flow.cmd_shop)
    handlers.register_callback("prod", flow.choose_product)
    handlers.register_command("rate", flow.cmd_rate)
    handlers.register_command("recipe", flow.cmd_recipe)
    handlers.text_handlers.insert(0, flow.change_text)
    handlers.text_handlers.insert(0, recipe_text)
    return flow


__all__ = ["MenuFlow", "dt", "install", "meal_now"]
