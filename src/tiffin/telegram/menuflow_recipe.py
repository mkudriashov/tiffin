"""Recipe Telegram menu actions."""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Any

from .. import log
from .. import targets as targets_mod
from ..clock import Clock, isoday, meal_now, parse_day
from ..food import menu as menu_mod
from ..food import recipes
from .api import inline_keyboard
from .menu_language import day_and_meal, day_named
from .render import esc, menu_link

logger = log.get("tiffin.menuflow")

COOK_COOLDOWN_S = 300
CHANGE_FLOW = "menu_change"
CHANGE_TTL = 6 * 3600  # an unanswered "what should change?" stops listening after six hours

if TYPE_CHECKING:
    from ..app import App
    from .handlers import Handlers
    from .poller import Inbound
from .menuflow import _NO_MENU, RATE_MAX, RATE_MIN, recipe_card_text  # noqa: E402


class MenuRecipeFlow:
    clock: Clock
    app: App | None
    _config: Any
    _send_day: Any
    _week_ahead: Any

    @staticmethod
    def _recipe_named(
        h: Handlers, person_id: int, said: str, today: dt.date
    ) -> tuple[int, float] | None:
        """(recipe_id, portion multiplier) for a dish named by a person, on their own menu.

        Their own menu, not the whole `recipes` table: a name is loose input and the point is to
        open a dish they are being asked to cook. An exact name wins over a prefix, and a prefix
        over a substring, so "chilli" reaches "Chilli con carne" and a one-word answer is not
        ambiguous just because a longer dish contains the word.
        """
        rows = h.db.all(
            "SELECT ms.recipe_id, ms.portion_multiplier, rc.name FROM v_menu_days_current md "
            "JOIN meal_slots ms ON ms.menu_day_id = md.id "
            "JOIN recipes rc ON rc.id = ms.recipe_id WHERE md.person_id = ? "
            "ORDER BY ABS(JULIANDAY(md.day) - JULIANDAY(?)), ms.id",
            (person_id, isoday(today)),
        )
        want = said.casefold()
        for rank in (0, 1, 2):
            for r in rows:
                name = str(r["name"]).casefold()
                hit = (name == want, name.startswith(want), want in name)[rank]
                if hit:
                    return int(r["recipe_id"]), float(r["portion_multiplier"])
        return None

    def _open_meal_on(self, h: Handlers, inbound: Inbound, day: dt.date, slot: str) -> str:
        """One meal on one day - the card its button opens, from the menu for that day.

        Shared by `/recipe thursday lunch` and `/menu thursday lunch`, which were answered with
        "no dish called thursday lunch" and "I did not read thursday lunch as a day": the two
        """
        person = inbound.person
        today = self.clock.today_local()
        when = "today" if day == today else f"{day:%a %d %b}"
        menu_id = menu_mod.accepted_menu_id(h.db, person.id, day)
        if menu_id is None:
            h.outbox.reply(person, _NO_MENU.format(when))
            return "handled"
        if self.show_meal(h, inbound, [str(menu_id), slot, f"{day:%Y%m%d}"]) == "handled":
            return "handled"
        label = slot.replace("_", "-")
        h.outbox.reply(person, f"No {label} on your menu for {when}. /menu {day:%a} lists it.")
        return "handled"

    def cmd_recipe(self, h: Handlers, inbound: Inbound, arg: str) -> str:
        """Today's main-meal card, or /recipe <id>, or /recipe <name>.

        A name was discarded without a word and the day's main meal served as the answer - so
        `/recipe Porridge with berries` returned the chilli, and the card's rating keyboard is
        `k:rate:<chilli>`, which means a 5 tapped for the porridge was written against the
        chilli. Wrong data, silently, from a reasonable request: the id is not shown anywhere, so
        the dish's NAME is the only handle a person actually has.
        """
        person = inbound.person
        today = self.clock.today_local()
        said = arg.strip()
        if not said:
            # the meal it is time for. At 12:40 at her desk, bare /recipe answered with tonight's
            # dinner: "the day's main meal" is right in the evening and wrong at lunchtime.
            slot = meal_now(self.clock.now_local())
            menu_id = menu_mod.accepted_menu_id(h.db, person.id, today)
            if (
                menu_id is not None
                and self.show_meal(h, inbound, [str(menu_id), slot]) == "handled"
            ):
                return "handled"
        asked = day_and_meal(said, today) if said and not said.isdigit() else None
        if asked is not None:
            return self._open_meal_on(h, inbound, *asked)
        if said.isdigit():
            recipe_id: int | None = int(said)
            multiplier = 1.0
        elif said:
            named = self._recipe_named(h, person.id, said, today)
            if named is None:
                h.outbox.reply(
                    person,
                    f"No dish on your menu called {esc(said)}. /menu lists what is on it.",
                )
                return "handled"
            recipe_id, multiplier = named
        else:
            # the day's MAIN meal. Ordering by ms.id DESC took the last slot inserted, which
            # is the snack (or pre-bed), so "/recipe - one dish in full" reliably answered
            # with the yoghurt while the menu message named a dinner you could not open.
            row = h.db.one(
                "SELECT ms.recipe_id, ms.portion_multiplier, ms.slot FROM v_menu_days_current md "
                "JOIN meal_slots ms ON ms.menu_day_id = md.id "
                "WHERE md.person_id = ? AND md.day = ? "
                "ORDER BY CASE ms.slot WHEN 'dinner' THEN 0 WHEN 'lunch' THEN 1 "
                "WHEN 'breakfast' THEN 2 ELSE 3 END, ms.id LIMIT 1",
                (person.id, isoday(today)),
            )
            recipe_id = int(row["recipe_id"]) if row else None
            multiplier = float(row["portion_multiplier"]) if row else 1.0
        if recipe_id is None:
            h.outbox.reply(person, "No recipe for today. /menu lists the week, /cook makes one.")
            return "handled"
        try:
            card = recipes.card(h.db, recipe_id, multiplier, person_id=person.id)
        except LookupError:
            h.outbox.reply(person, "No recipe with that id. /menu opens them by name.")
            return "handled"
        h.outbox.reply(
            person,
            recipe_card_text(card),
            keyboard=inline_keyboard(
                [[(str(n), f"k:rate:{recipe_id}:{n}") for n in range(RATE_MIN, RATE_MAX + 1)]]
            ),
        )
        return "handled"

    def show_day(self, h: Handlers, inbound: Inbound, parts: list[str]) -> str:
        """k:mday:<menu_id>:<yyyymmdd> - switch the menu card to another day.

        The same renderer as the command, so a tap and `/menu thu` cannot drift apart: one day,
        labelled meals, a button per day and a button per meal on THIS day.
        """
        if len(parts) < 2 or not parts[0].isdigit():
            return "ignored"
        menu_id = int(parts[0])
        day = day_named(parts[1], self.clock.today_local())
        if day is None:
            return "ignored"
        row = h.db.one("SELECT person_id FROM menus WHERE id = ?", (menu_id,))
        if row is None or int(row["person_id"]) != inbound.person.id:
            return "ignored"
        # the button was built whenever this day was last rendered; a menu accepted since then
        # must win over the id it carries, or a day button from this morning's menu keeps
        # showing the dish an evening Accept replaced
        current_id = menu_mod.accepted_menu_id(h.db, inbound.person.id, day)
        if current_id is not None:
            menu_id = current_id
        if not menu_mod.meals_for_day(h.db, inbound.person.id, day):
            return "ignored"
        h.answer(inbound, f"{day:%a %d %b}")
        return self._send_day(h, inbound.person, menu_id, day, today=self.clock.today_local())

    def show_week(self, h: Handlers, inbound: Inbound, parts: list[str]) -> str:
        """k:menuw: - back to the whole week from a day's card."""
        h.answer(inbound)
        return self._week_ahead(h, inbound.person, self.clock.today_local())

    def show_meal(self, h: Handlers, inbound: Inbound, parts: list[str]) -> str:
        """k:meal:<menu_id>:<slot>[:<yyyymmdd>] - the card behind a dish, with the menu on it.

        Carrying the menu id rather than a bare recipe id is what lets the card offer the
        other meals and a way back. A card that only had rating buttons was a dead end: the
        person tapped a dish and could not reach the menu or any other meal again.

        The day is optional and defaults to today, which is right for the proposal message. A
        button built for a named day carries it, so `/menu tomorrow` opens tomorrow's dinner
        rather than tonight's.
        """
        from ..food import plates as plates_mod  # noqa: PLC0415 (food imports the db layer)
        from ..jobs import menu_propose  # noqa: PLC0415 (jobs import the scheduler)

        if len(parts) < 2 or not parts[0].isdigit():
            return "ignored"
        menu_id, slot = int(parts[0]), parts[1]
        want = day_named(parts[2], self.clock.today_local()) if len(parts) > 2 else None
        # show_day and show_menu already refuse a menu id that is not this person's; a meal
        # button carries the same kind of id and was reachable from anyone's chat with no check
        # at all - a forwarded keyboard or a crafted callback opened someone else's lunch
        row = h.db.one("SELECT person_id, status FROM menus WHERE id = ?", (menu_id,))
        if row is None or int(row["person_id"]) != inbound.person.id:
            return "ignored"
        if len(parts) > 3 and parts[3] == "pot":
            # a button on /batch: the whole pot and how to split it, not one box's card
            from .batchflow import BatchFlow  # noqa: PLC0415 (batchflow imports this module)

            if BatchFlow(clock=self.clock).show_pot(h, inbound, menu_id, slot, want) == "handled":
                return "handled"
            # no longer a batched dish: the ordinary card is still the right answer
        # today when the menu covers today, so the Dinner button opens what is being cooked
        day_for = want or self.clock.today_local()
        # the button was built whenever it was last rendered; a menu that WAS accepted or
        # repeated must win over the id it carries when a later Accept has since replaced it, or
        # yesterday's Lunch button keeps handing back the dish that later Accept replaced.
        was_decided = str(row["status"]) in ("accepted", "repeated") or (
            menu_mod.decision_of(h.db, menu_id) is not None
        )
        if was_decided:
            current_id = menu_mod.accepted_menu_id(h.db, inbound.person.id, day_for)
            if current_id is not None:
                menu_id = current_id
        # the day the dish is read from: on a menu that does not cover today a button with no
        # day opens the menu's first day, and Swap, Less and the date pointed at today, which
        # had no menu - "There is no menu for that day yet" on every tap
        card_day = self._card_day(h, menu_id, slot, day_for)
        if was_decided and card_day != day_for:
            # and that day's plate in force: after Less from this card the same button still
            # showed the portion as planned under the date, beside the size row of the smaller one
            current_id = menu_mod.accepted_menu_id(h.db, inbound.person.id, card_day)
            if current_id is not None:
                menu_id = current_id
        day_for = card_day
        found = menu_propose.recipe_at(h.db, menu_id, slot, day_for)
        if found is None:
            return "ignored"
        recipe_id, multiplier = found
        try:
            card = recipes.card(h.db, recipe_id, multiplier, person_id=inbound.person.id)
        except LookupError:
            return "ignored"
        joint = self._joint_tray_multiplier(h, inbound.person.id, day_for, slot, recipe_id)
        if joint is not None:
            # the weekend is cooked together: the
            # SAME dish, the same day and slot, on both menus, is one tray on one hob - the
            # ingredients are for both plates, not the plate this person happens to be reading
            # about. The macros and portion size stay HIS OWN, the same split the batch pot
            # card already makes between the pot's grams and one box's numbers.
            tray = recipes.card(h.db, recipe_id, multiplier + joint, person_id=inbound.person.id)
            card = {**tray, "per_serving": card["per_serving"], "portion_g": card["portion_g"]}
        h.answer(inbound, str(card["name"])[:60])
        rows = [
            [(str(n), f"k:rate:{recipe_id}:{n}") for n in range(RATE_MIN, RATE_MAX + 1)],
            *menu_propose.meal_buttons(h.db, menu_id, exclude=slot, day=want),
            # the week, not the one resized day this card may have been read from
            [("Back to the menu", f"k:menuv:{plates_mod.whole_menu_of(h.db, menu_id)}")],
        ]
        if day_for >= self.clock.today_local() and not menu_mod.day_confirmed(
            h.db, inbound.person.id, day_for
        ):
            # one dish swapped for one already eaten, in code, in a second (food/swap.py): a
            # Change rebuilt every dish of the week through the model to replace this one
            rows.insert(1, [("Swap this dish", f"k:swapmeal:{isoday(day_for)}:{slot}")])
            at = f"{isoday(day_for)}:{slot}"
            size: list[tuple[str, str]] = []
            if slot in plates_mod.one_tap(h.db, inbound.person.id, day_for, self.clock):
                # this plate smaller, bigger or left out, the rest of the day making room - the
                # page's sliders, two taps from the card.
                # Not on a box, a meal already behind the clock today, or the only plate left
                # that could move: each of those would be a tap that does nothing it says
                size = [
                    ("Less", f"k:size:{at}:less"),
                    ("More", f"k:size:{at}:more"),
                    ("Skip", f"k:size:{at}:skip"),
                ]
            if slot in plates_mod.resettable(h.db, inbound.person.id, day_for, self.clock):
                # the way back, as the page's card has it, even on the only plate left that can
                # move: "put dinner back to as planned first" had no button here
                size.append(("As planned", f"k:size:{at}:planned"))
            if size:
                rows.insert(2, size)
        # which day this card is for, when it is not today: a card that says only "Garlic turkey"
        # cannot be told from the one you are cooking tonight
        page = ""
        cfg = self._config(h)
        if (
            cfg is not None
            and inbound.person.tailscale_login
            and cfg.get("web.identity_mode", "header") == "header"
        ):
            # this meal on the page, for someone the page lets in - never a link to a 403
            link = menu_link(cfg, isoday(day_for), slot)
            page = f"On the page: {link}." if link else ""
        freezable, freezer_tail = self._freezer_facts(
            h, inbound.person, slot, day_for, str(card["name"])
        )
        if freezable is not None:
            # /batch's own answer (the recipe's flag OR'd with whether this person freezes at
            # all) wins over the recipe's flag alone, so the card and /batch never disagree
            # about the one dish they are both describing.
            card = {**card, "freezable": freezable}
        batch_note = self._batch_note(h, inbound.person, slot, card)
        if freezer_tail:
            batch_note = f"{batch_note} {freezer_tail}".strip()
        text = recipe_card_text(
            card,
            batch_note=batch_note,
            page_line=page,
            warn_line=self._card_warn_line(h, inbound.person, recipe_id),
        )
        if day_for != self.clock.today_local():
            text = f"<b>{day_for:%a %d %b}</b>\n{text}"
        h.outbox.reply(inbound.person, text, keyboard=inline_keyboard(rows))
        return "handled"

    @staticmethod
    def _card_day(h: Handlers, menu_id: int, slot: str, day: dt.date) -> dt.date:
        """The day `menu_propose.recipe_at` reads a slot from: `day` when the menu has that meal
        on it, otherwise the menu's first day that has."""
        row = h.db.one(
            "SELECT md.day FROM menu_days md JOIN meal_slots ms ON ms.menu_day_id = md.id "
            "WHERE md.menu_id = ? AND ms.slot = ? ORDER BY md.day = ? DESC, md.day LIMIT 1",
            (menu_id, slot, isoday(day)),
        )
        return day if row is None else parse_day(str(row["day"]))

    def _batch_note(self, h: Handlers, person: Any, slot: str, card: dict) -> str:
        """For a batch-cooked slot, which part of the card happens when.

        Thursday 12:40 at her desk, with a microwave and nothing else, the lunch card said "fry
        the chicken on the hob for 8 minutes". It was fried on Sunday. Without saying so, the
        card read as a recipe to start now, in an office.
        """
        from ..food import planning  # noqa: PLC0415 (only the card needs it)

        profile = targets_mod.profile_on(h.db, person.id, self.clock.today_local())
        if profile is None or slot not in planning.batched(profile):
            return ""
        heat = ("hob", "oven", "air fryer")
        steps = [str(s) for s in card.get("steps", [])]
        if not any(recipes.APPLIANCES[a].search(s) for a in heat for s in steps):
            return ""
        return (
            "Batch-cooked: the method is for cook day. On the day, eat it from the box - "
            "microwave it until piping hot if it is a hot dish."
        )

    def _freezer_facts(
        self, h: Handlers, person: Any, slot: str, day_for: dt.date, name: str
    ) -> tuple[bool | None, str]:
        """Whether /batch would freeze this box, and what a box out of the freezer needs.

        `/batch`'s freezability is the recipe's own flag OR'd with whether this person freezes
        at all (`food.batch.plan`'s own comment: "their answer wins over the model's guess") -
        the owner freezes what he batches, and Sunday's /batch line told him to freeze a salad the
        recipe itself says does not freeze. The card, reading only the recipe's flag, told him
        the same salad "keeps in the fridge" and never freezes: the same box, two answers.
        Recomputing the SAME Load /batch built off the SAME historical cook day (the day before
        the segment starts, not today) is what stops the two from disagreeing again the next
        time either one changes.

        `None` means "not part of a batch right now" - the caller leaves the recipe's own flag
        alone rather than overriding it with a fact that does not apply.
        """
        from ..food import batch as batch_mod  # noqa: PLC0415 (only the card needs it)
        from .batchflow import _freezes  # noqa: PLC0415 (batchflow's own reading of the toggle)

        profile = targets_mod.profile_on(h.db, person.id, self.clock.today_local())
        if profile is None:
            return None, ""
        from ..food import planning  # noqa: PLC0415 (only the card needs it)

        slots = planning.batched(profile)
        if slot not in slots:
            return None, ""
        menu = batch_mod.menu_to_batch(h.db, person.id, self.clock.today_local())
        if menu is None:
            return None, ""
        start = parse_day(str(menu["segment_start"]))
        load = batch_mod.plan(
            h.db, menu, slots, start - dt.timedelta(days=1), freezes=_freezes(profile)
        )
        dish = next((d for d in load.dishes if d.name == name), None)
        if dish is None:
            return None, ""
        late = next((days for d, days in load.to_freeze if d.name == name), ())
        tail = ""
        if day_for in late and dish.freezable:
            tail = (
                "This box came out of the freezer: thaw it in the fridge overnight, or "
                "reheat it from frozen until piping hot."
            )
        return dish.freezable, tail

    def _card_warn_line(self, h: Handlers, person: Any, recipe_id: int) -> str:
        """Say when the dish this card is actually FOR has something they have since refused.

        the chickpeas with no word at all, even though the menu message one tap earlier had
        already warned her. Scoped to this one recipe, not the whole menu: a card for an
        innocent Tuesday dinner must not be flagged just because Monday's dish elsewhere on the
        same menu holds the refused food.
        """
        from ..food import catalogue  # noqa: PLC0415 (food imports the db layer)

        names = ", ".join(catalogue.refused_names_in_recipe(h.db, person.id, recipe_id))
        if not names:
            return ""
        return f"{esc(names)} is on your avoid list."

    @staticmethod
    def _joint_tray_multiplier(
        h: Handlers, person_id: int, day: dt.date, slot: str, recipe_id: int
    ) -> float | None:
        """The other person's own portion multiplier, when they are having the SAME dish on the
        SAME day and slot from their own accepted menu - None otherwise.

        Two people, one profile row each, so "the other person" is simply whoever this is not.
        """
        from ..jobs import menu_propose  # noqa: PLC0415 (jobs import the scheduler)

        other = h.db.one("SELECT id FROM persons WHERE id != ?", (person_id,))
        if other is None:
            return None
        other_menu_id = menu_mod.accepted_menu_id(h.db, int(other["id"]), day)
        if other_menu_id is None:
            return None
        found = menu_propose.recipe_at(h.db, other_menu_id, slot, day)
        if found is None or found[0] != recipe_id:
            return None
        return float(found[1])

    def cmd_page(self, h: Handlers, inbound: Inbound, arg: str) -> str:
        """`/page` - the recipe book on the tailnet page, opened on today or on a named day.

        The address was in config and in no message he could find on demand.
        """
        person = inbound.person
        today = self.clock.today_local()
        day = day_named(arg, today) or today
        cfg = self._config(h)
        if cfg is not None and cfg.get("web.identity_mode", "header") == "telegram":
            if self.app is None or not cfg.get("web.enabled", True):
                h.outbox.reply(person, "The web page is off. /recipe shows your dishes here.")
                return "handled"
            from ..web.identity import issue_login_link  # noqa: PLC0415

            url = issue_login_link(self.app, h.db, person)
            h.outbox.reply(
                person,
                f'<a href="{url}">Open your recipe book</a>. This sign-in link works once '
                "for 15 minutes.",
                sensitive=True,
            )
            return "handled"
        if not person.tailscale_login:
            h.outbox.reply(
                person,
                "The page opens once your Tailscale login is linked to you. The owner can run "
                "<code>tiffin link-tailscale</code>. Until then /menu and /recipe have "
                "every recipe.",
            )
            return "handled"
        label = f"The recipe book, {day:%a %d %b}"
        link = menu_link(cfg, isoday(day), label=label) if cfg is not None else ""
        if not link:
            h.outbox.reply(person, "The page has no address configured, so there is no link.")
            return "handled"
        h.outbox.reply(person, f"{link}: every day ahead, every recipe unrolled.")
        return "handled"

    def show_menu(self, h: Handlers, inbound: Inbound, parts: list[str]) -> str:
        """k:menuv:<menu_id> - the menu again, from wherever they wandered to."""
        from ..food import plates as plates_mod  # noqa: PLC0415 (food imports the db layer)
        from ..jobs import menu_propose  # noqa: PLC0415

        if not parts or not parts[0].isdigit():
            return "ignored"
        menu_id = int(parts[0])
        row = h.db.one("SELECT person_id FROM menus WHERE id = ?", (menu_id,))
        if row is None or int(row["person_id"]) != inbound.person.id:
            return "ignored"
        # a resized day's own menu is one day: the menu, its Swap a dish and its New menu are
        # the week it was cut from
        menu_id = plates_mod.whole_menu_of(h.db, menu_id)
        html, _payload = menu_propose.message_for(h.db, menu_id)
        # no Accept: whatever this menu's decision, it was decided the moment it was built and
        # sent - this is a resend, not a fresh proposal. Change is still the way off it.
        rows = [menu_propose.change_row(menu_id), *menu_propose.meal_buttons(h.db, menu_id)]
        h.answer(inbound, "Menu")
        h.outbox.reply(inbound.person, html, keyboard=inline_keyboard(rows))
        return "handled"
