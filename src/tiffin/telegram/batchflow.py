"""`/batch` - the cook-day view: total ingredients, how many boxes, and what has to be frozen.

The menu answers "what am I eating on Wednesday". That is the wrong question standing in a
kitchen on a Sunday with one pan. The right one is "how much rice, how much chicken, how many
boxes, and which of those boxes will not last until the day it is for" - and until now the
only way to get it was to read five days of menu and add up in your head.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Any

from .. import targets as targets_mod
from ..clock import DEFAULT_CLOCK, Clock, parse_day
from ..food import batch as batch_mod
from ..food import planning
from ..food.recipes import is_seasoning
from .api import button_rows, inline_keyboard
from .render import batch_link, check_text, esc, few, fit_joined, plural

if TYPE_CHECKING:
    from .handlers import Handlers
    from .poller import Inbound

#: how many dishes get their own line before the rest are counted instead
DISH_LINES = 4
#: how many ingredients the short view names before "/batch full"
SHORT_AMOUNTS = 6
#: how many of "/batch full"'s own ingredient lines fit the 40-line cap alongside the dish and
#: freezer lines above them; one more line is always held back for the note when this is not
#: everything, so the short view's "lists every ingredient" is never silently untrue. Leaves
#: margin for the refusal warning, which is its own two lines when a waiting proposal follows it.
FULL_AMOUNTS = 27


def _days(days: tuple[dt.date, ...]) -> str:
    """ "Mon, Wed" for a scattered few; "Mon-Fri" for a run of consecutive days - a same-all-week
    batch named every one of five days in full and ran a dish line past three phone-lines on
    its own (phone_reading/06) for what a cook reads as one span anyway.
    """
    if len(days) > 2 and all((days[i] - days[i - 1]).days == 1 for i in range(1, len(days))):
        return f"{days[0]:%a}-{days[-1]:%a}"
    return ", ".join(f"{d:%a}" for d in days)


def _freezes(profile: Any) -> bool:
    """Their own answer, tolerant of a profile row written before the column existed."""
    try:
        return bool(profile["freezes"])
    except KeyError, IndexError, TypeError:
        return False


class BatchFlow:
    def __init__(self, *, clock: Clock = DEFAULT_CLOCK):
        self.clock = clock

    def cmd_batch(self, h: Handlers, inbound: Inbound, arg: str) -> str:
        person = inbound.person
        today = self.clock.today_local()
        profile = targets_mod.profile_on(h.db, person.id, today)
        if profile is None:
            h.outbox.reply(person, "Finish /onboard first and I can work out a cook day.")
            return "handled"
        slots = planning.batched(profile)
        if not slots:
            # a batch cadence with nothing made ahead, or a cook every day: each is told its own
            # way to change it
            h.outbox.reply(
                person,
                "You cook fresh rather than in batches. "
                "<code>/set batch lunch dinner</code> changes that."
                if any(len(g) > 1 for g in planning.groups_of(profile))
                else "You cook fresh each day, so there is nothing to batch. "
                "<code>/set cook-days mf</code>, or How you plan on the page's More, changes that.",
            )
            return "handled"
        menu = batch_mod.menu_to_batch(h.db, person.id, today)
        if menu is not None and parse_day(str(menu["segment_start"])) < today:
            # `menu_to_batch` deliberately still returns the segment being eaten once nothing
            # newer exists, so a mid-week "what am I cooking" still gets an answer instead of
            # silence - but its cook day (the day BEFORE the segment starts) is by definition
            # already behind us here, so re-offering it as THIS cook told the owner to batch the
            # Tuesday's already-eaten boxes when Thu-Fri, which actually has no menu, was the
            # question. There is nothing new to cook
            # from it, so the honest answer is the same one "no menu" gives.
            menu = None
        if menu is None:
            h.outbox.reply(
                person,
                "No menu to cook from yet. /cook builds one now, and /menu shows what is planned.",
            )
            return "handled"
        load = batch_mod.plan(h.db, menu, slots, today, freezes=_freezes(profile))
        if not load.dishes:
            h.outbox.reply(
                person,
                f"The menu for {load.segment_start:%a %d %b} has no "
                f"{esc(batch_mod.describe(slots))} in it, so there is nothing to batch.",
            )
            return "handled"
        full = arg.strip().lower() in ("full", "all", "long")
        rows = self._pot_buttons(h, int(menu["id"]), load)
        h.outbox.reply(
            person,
            self._render(
                load,
                full=full,
                warn_line=self._refusal_line(h, person, int(menu["id"])),
            ),
            keyboard=inline_keyboard(rows) if rows else None,
        )
        return "handled"

    def _refusal_line(self, h: Handlers, person: Any, menu_id: int) -> str:
        """Explain when a cooking plan contains a food the person has since refused."""
        from ..food import catalogue  # noqa: PLC0415 (food imports the db layer)

        names = few(catalogue.refused_names_on(h.db, person.id, [menu_id]))
        if not names:
            return ""
        return (
            f"This cook still has {esc(names)} in it, which you now avoid.\n"
            "<code>/swap</code> swaps it for a dish you have had, at once."
        )

    def _pot_buttons(
        self, h: Handlers, menu_id: int, load: batch_mod.Load, *, exclude: str = ""
    ) -> list[list[tuple[str, str]]]:
        """A button per DISH in the cook, opening its pot; the meals made fresh open as usual.

        the owner, 14 Sep, clicking through: "/batch ... I open lunch, and it tells me only the
        number
        of ingredients per meal, which then defeats my whole purpose of trying to batch cook."
        The buttons were the menu's, so Lunch opened one box's card under a message about ten.
        """
        from ..jobs import menu_propose  # noqa: PLC0415 (jobs import the scheduler)

        first: dict[str, tuple[str, dt.date]] = {}  # dish name -> (slot, the first day it is on)
        for r in h.db.all(
            "SELECT md.day, ms.slot, rc.name FROM menu_days md "
            "JOIN meal_slots ms ON ms.menu_day_id = md.id "
            "JOIN recipes rc ON rc.id = ms.recipe_id WHERE md.menu_id = ? ORDER BY md.day, ms.id",
            (menu_id,),
        ):
            first.setdefault(str(r["name"]), (str(r["slot"]), parse_day(str(r["day"]))))
        per_slot: dict[str, int] = {}
        for d in load.dishes:
            if d.name in first:
                per_slot[first[d.name][0]] = per_slot.get(first[d.name][0], 0) + 1
        buttons: list[tuple[str, str]] = []
        for d in load.dishes:
            if d.name not in first or d.name == exclude:
                continue
            slot, day = first[d.name]
            label = menu_propose.SLOT_LABELS.get(slot, slot)
            if per_slot[slot] > 1:
                label = f"{label} {day:%a}"
            buttons.append(
                (f"{label}: {plural(d.boxes, 'box')}", f"k:meal:{menu_id}:{slot}:{day:%Y%m%d}:pot")
            )
        for slot in dict.fromkeys(s for s, _day in first.values()):
            if slot not in load.slots:
                buttons.append(
                    (menu_propose.SLOT_LABELS.get(slot, slot), f"k:meal:{menu_id}:{slot}")
                )
        return button_rows(buttons)

    def show_pot(
        self, h: Handlers, inbound: Inbound, menu_id: int, slot: str, day: dt.date | None
    ) -> str:
        """The pot behind one batched dish: the whole cook's amounts, then how to split it.

        The card is the dish's own recipe at the cook's "times the recipe", so every gram on it
        is what goes in the pot - the same figure `/batch full` gives - and the macros and the
        portion line are one box's. "ignored" when the dish is not batched any more, so the
        caller can fall back to the ordinary card.
        """
        from ..food import recipes  # noqa: PLC0415 (only the pot card needs it)
        from ..jobs import menu_propose  # noqa: PLC0415 (jobs import the scheduler)
        from .menuflow import RATE_MAX, RATE_MIN, recipe_card_text  # noqa: PLC0415

        person = inbound.person
        menu = h.db.one("SELECT * FROM menus WHERE id = ?", (menu_id,))
        if menu is None or int(menu["person_id"]) != person.id:
            return "ignored"
        today = self.clock.today_local()
        profile = targets_mod.profile_on(h.db, person.id, today)
        slots = planning.batched(profile)
        found = menu_propose.recipe_at(h.db, menu_id, slot, day or today)
        if not slots or found is None:
            return "ignored"
        name = str(h.db.scalar("SELECT name FROM recipes WHERE id = ?", (found[0],)))
        load = batch_mod.plan(h.db, menu, slots, today, freezes=_freezes(profile))
        dish = next((d for d in load.dishes if d.name == name), None)
        if dish is None or dish.boxes < 1:
            return "ignored"
        servings = int(
            h.db.scalar("SELECT servings FROM recipes WHERE id = ?", (dish.recipe_id,)) or 1
        )
        pot = recipes.card(h.db, dish.recipe_id, dish.multiples, person_id=person.id)
        box = recipes.card(
            h.db, dish.recipe_id, dish.multiples * servings / dish.boxes, person_id=person.id
        )
        # the grams are the pot's; the macros and the portion are one box's
        pot = {**pot, "per_serving": box["per_serving"], "portion_g": box["portion_g"]}
        pot["servings"] = 1
        split = f"Split it into {plural(dish.boxes, 'box')} of about {box['portion_g']:.0f} g"
        late = next((days for d, days in load.to_freeze if d.name == dish.name), ())
        if late and dish.freezable:
            split += f"; freeze the {_days(late)} " + ("box" if len(late) == 1 else "boxes")
        note = f"One pot, {dish.multiples:g} times the recipe. {split}."
        h.answer(inbound, dish.name[:60])
        text = f"<b>Batch pot: {plural(dish.boxes, 'box')}</b>\n" + recipe_card_text(
            pot, batch_note=note
        )
        rows = [
            [(str(n), f"k:rate:{dish.recipe_id}:{n}") for n in range(RATE_MIN, RATE_MAX + 1)],
            *self._pot_buttons(h, menu_id, load, exclude=dish.name),
            [("Back to the batch", "k:batchv")],
        ]
        h.outbox.reply(person, text, keyboard=inline_keyboard(rows))
        return "handled"

    def _render(
        self,
        load: batch_mod.Load,
        *,
        full: bool,
        warn_line: str = "",
    ) -> str:
        lines = [
            f"<b>Batch cook {load.cook_day:%a %d %b}</b> for "
            f"{load.segment_start:%a %d} to {load.segment_end:%a %d %b}"
        ]
        if warn_line:
            lines.append(warn_line)
        fresh = (
            f" {esc(batch_mod.describe(load.fresh)).capitalize()} stays fresh."
            if load.fresh
            else ""
        )
        lines.append(
            f"{esc(batch_mod.describe(load.slots)).capitalize()}: "
            f"{plural(load.boxes, 'box')}.{fresh}"
        )
        for dish in load.dishes[:DISH_LINES]:
            times = f"{dish.multiples:g}"
            # the day(s) its box(es) are for: a marker pen at the
            # counter needs the day next to the dish, not just a box count
            lines.append(
                f"{esc(dish.name)}: {plural(dish.boxes, 'box')}, {times} times the recipe "
                f"({_days(dish.days)})"
            )
        rest = load.dishes[DISH_LINES:]
        if rest:
            lines.append(
                f"and {plural(len(rest), 'more dish', 'more dishes')}, "
                f"{plural(sum(d.boxes for d in rest), 'box')}."
            )
        # the length of the sitting, and which dish to start - the 35-minute oven tray, not
        # the 3-minute yoghurt. `load.dishes` is already ordered longest-prep first,
        # and this comes after every per-dish line so a dish name here is never what a lookup
        # for THAT dish's own line finds first.
        total_prep = sum(d.prep_minutes for d in load.dishes)
        if total_prep:
            lines.append(f"About {total_prep} min. Start with {esc(load.dishes[0].name)}.")
        lines += self._amount_lines(load, full=full)
        lines += self._keeping_lines(load, full=full)
        lines += self._is_it_even_a_batch(load)
        if not full:
            lines.append("/batch full lists every ingredient. /set batch changes what is batched.")
        return check_text("\n".join(lines), kind="batch", max_lines=40 if full else 12)

    def _amount_lines(self, load: batch_mod.Load, *, full: bool) -> list[str]:
        shown = load.amounts if full else load.amounts[:SHORT_AMOUNTS]
        if not shown:
            return []
        if full:
            # one per line, with the split, because this is the list read while weighing
            # things out into the boxes rather than the one read in a supermarket
            listed = shown[:FULL_AMOUNTS]
            out = ["<b>Cook</b>"] + [self._spread(a) for a in listed]
            rest = len(shown) - len(listed)
            if rest > 0:
                # the short view promises "/batch full lists every ingredient" - when the
                # 40-line cap means it cannot, say what did not fit rather than dropping it
                # with nothing to show for it (phone_reading/05); the house pattern next door
                # is `shopping_list._capped`'s own trailing "N more lines" line.
                out.append(f"and {plural(rest, 'more ingredient')}, not shown here.")
            return out
        parts = ", ".join(f"{esc(a.name)} {batch_mod.weigh(a.grams)}" for a in shown)
        tail = (
            f", and {len(load.amounts) - len(shown)} more" if len(load.amounts) > len(shown) else ""
        )
        return [f"<b>Cook</b> {parts}{tail}."]

    @staticmethod
    def _spread(amount: batch_mod.Amount) -> str:
        """One ingredient, and the number somebody at the counter can act on.

        "Potatoes, raw 900 g across 10 boxes" was the whole line when two dishes used the same
        potato in different amounts - a total, and an instruction to do the division yourself
        with scales in your hand, at the one moment this message exists for. Where there is no
        single "each", the split is given per DISH, which is how the cooking is organised anyway.
        """
        if is_seasoning(amount.name):
            # Salt, pepper and oil go into the pot once, by eye, seasoning the whole batch - not
            # measured into each box. "0.8 g each" is a number no kitchen scale reads, and five
            # of them never summed to the total printed beside them.
            return f"{esc(amount.name)} {batch_mod.weigh(amount.grams)} for the pot."
        head = (
            f"{esc(amount.name)} {batch_mod.weigh(amount.grams)} "
            f"across {plural(amount.boxes, 'box')}"
        )
        if amount.uniform:
            return f"{head}, {batch_mod.weigh(amount.per_box)} each"
        if not amount.by_dish:
            return head
        parts = [
            f"{esc(d.dish)} {batch_mod.weigh(d.grams)}"
            + (
                f" ({plural(d.boxes, 'box')}, {batch_mod.weigh(d.per_box)} each)"
                if d.uniform
                # unequal boxes of the SAME dish: "2 boxes" alone is a total and a
                # box count, and leaves the actual split to whoever is holding the scales
                else f" ({', '.join(batch_mod.weigh(g) for g in d.box_grams)})"
            )
            for d in amount.by_dish
        ]
        # an ingredient shared by three or four dishes joined all of them onto one line with no
        # limit at all - six phone-lines for a spice everyone happened to use (phone_reading/06).
        # Join what fits and say how many more, checked the same way every phone-width line on
        # a phone reads against.
        head = f"{esc(amount.name)} {batch_mod.weigh(amount.grams)}: "
        return fit_joined(head, parts, "; ")

    @staticmethod
    def _is_it_even_a_batch(load: batch_mod.Load) -> list[str]:
        """A different dish in every box is not a batch, it is that many separate cooks.

        Found on the real menu: a cook cadence of one cook for Mon-Fri, and a menu with eight
        dishes for eight boxes. Both settings were doing what they were told and the result
        was the opposite of what the person had asked for, with nothing anywhere saying so.
        """
        dishes, boxes = len(load.dishes), load.boxes
        if boxes < 4 or dishes < boxes:
            return []
        return [
            f"{dishes} dishes for {boxes} boxes is {dishes} cooks, not a batch. "
            "<code>/set variety same</code> makes the menu repeat."
        ]

    def _keeping_lines(self, load: batch_mod.Load, *, full: bool) -> list[str]:
        """The part a per-day menu can never tell you.

        A Friday box cooked on Sunday is five days old when it is opened. Saying so is the
        difference between a batch that works and one that quietly goes to waste on Thursday.
        The fridge life itself is not repeated here - the recipe card already has its own
        "Keeps N days" line - so this says only what a per-day menu cannot: WHICH
        days go where.

        `full` only budgets the dish list on `/batch full`: bare `/batch` is a short, asked-for
        summary (12-line cap) that a marker pen at the counter reads once, and every dish that
        needs freezing naming its own days there is the one place that answer is complete -
        `/batch full` is the long, scrolled view where a single overlong line is what phone_
        reading/06 is about, and giving way there does not cost the short view anything.
        """
        freeze: list[str] = []
        trouble: list[str] = []
        for dish, late in load.to_freeze:
            where = f"{esc(dish.name)} {_days(late)}"
            (freeze if dish.freezable else trouble).append(where)
        out = []
        if freeze:
            # a frozen box is only edible if it moved back to the fridge the night before
            #: the Freeze line was the one place this was decided and
            # the one place that never said so.
            if full:
                out.append(fit_joined("<b>Freeze</b> ", freeze, "; ", "."))
            else:
                out.append(f"<b>Freeze</b> {'; '.join(freeze)}.")
            out.append("Move a box from the freezer to the fridge the night before you need it.")
        if trouble:
            if full:
                out.append(fit_joined("<b>Cook fresh</b> ", trouble, "; ", " and does not freeze."))
            else:
                out.append(f"<b>Cook fresh</b> {'; '.join(trouble)} and does not freeze.")
        return out


def install(handlers: Handlers, *, clock: Clock = DEFAULT_CLOCK) -> BatchFlow:
    flow = BatchFlow(clock=clock)
    handlers.register_command("batch", flow.cmd_batch)
    # "Back to the batch" on a pot card: the cook-day message again, not the menu proposal
    handlers.register_callback("batchv", lambda h, i, _p: flow.cmd_batch(h, i, ""))
    return flow


def summary(db: Any, person_id: int, today: dt.date, config: Any = None) -> str | None:
    """Summarise the cooking session for its shopping message."""
    profile = targets_mod.profile_on(db, person_id, today)
    if profile is None:
        return None
    slots = planning.batched(profile)
    if not slots:
        return None
    menu = batch_mod.menu_to_batch(db, person_id, today)
    if menu is None:
        return None
    load = batch_mod.plan(db, menu, slots, today, freezes=_freezes(profile))
    if not load.dishes:
        return None
    line = (
        f"One cook on {load.cook_day:%a %d %b} fills {plural(load.boxes, 'box')}. "
        "/batch has the amounts."
    )
    if config is not None:
        link = batch_link(config)
        return f"{line} {link}" if link else line
    return f"{line} Cook it on the page: ?tab=batch."
