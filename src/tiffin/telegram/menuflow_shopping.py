"""Shopping Telegram menu actions."""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Any

from .. import log
from ..clock import Clock, isoday, parse_day
from ..food import shopping
from .api import inline_keyboard
from .render import check_text, esc, shopping_link

logger = log.get("tiffin.menuflow")

COOK_COOLDOWN_S = 300
CHANGE_FLOW = "menu_change"
CHANGE_TTL = 6 * 3600  # an unanswered "what should change?" stops listening after six hours

if TYPE_CHECKING:
    from .handlers import Handlers
    from .poller import Inbound
from .menuflow import (  # noqa: E402
    _DISH_SEP,
    RATE_MAX,
    RATE_MIN,
    SHOPPING_MAX_LINES,
    TICK_BUTTONS,
    _shopping_line_fields,
    _short_dish,
    send_list_text,
)


class MenuShoppingFlow:
    clock: Clock
    _config: Any
    _list_change_note: Any
    _refusal_note: Any
    rate: Any

    def _list_to_show(self, h: Handlers, person: Any) -> Any:
        """Find or build the list for the next cooking session."""
        from ..jobs import shop_refresh  # noqa: PLC0415 (jobs import the scheduler)

        # the page's Shopping tab asks the same function, so the two cannot disagree again
        cfg = self._config(h)
        return shopping.list_to_show(
            h.db,
            person.id,
            self.clock.today_local(),
            retailers=shop_refresh.retailer_order(cfg) if cfg else (),
            clock=self.clock,
        )

    def cmd_shopping(self, h: Handlers, inbound: Inbound, arg: str) -> str:
        person = inbound.person
        said = arg.strip()
        if said.split(" ")[0].casefold() in ("out", "none", "missing"):
            return self._shelf_empty(h, inbound, said.split(" ", 1)[1] if " " in said else "")
        row = self._list_to_show(h, person)
        if said.casefold() in ("text", "forward", "share", "export") and row is not None:
            send_list_text(h.outbox, h.db, person, int(row["id"]))
            return "handled"
        if row is None:
            h.outbox.reply(
                person,
                "No shopping list yet, and no menu to build one from. "
                "<code>/cook</code> makes a menu and the list follows.",
            )
            return "handled"
        items = h.db.all(
            # coalesce, not join-and-hope: the pack the list was built against is the one it
            # was sent with, and the catalogue's guess may since have changed under it
            "SELECT si.id, si.packs, si.price_pence, si.product_name, si.aisle, si.credit_g, "
            "COALESCE(si.pack_label, i.pack_label) AS pack_label, i.name AS name, "
            "i.id AS ingredient_id FROM shopping_items si "
            "JOIN ingredients i ON i.id = si.ingredient_id WHERE si.list_id = ? AND si.packs > 0",
            (int(row["id"]),),
        )
        items = sorted(items, key=shopping.row_sort_key())
        # what the list counts on being at home already: said, never dropped
        home = shopping.at_home_line(shopping.at_home(h.db, int(row["id"])))
        if not items:
            h.outbox.reply(
                person,
                "Nothing to buy on this list."
                + (f" {esc(home)}" if home else "")
                + " /menu shows what it is for.",
            )
            return "handled"
        ticked = shopping.ticks_for(h.db, int(row["id"]), person.id, joint=row["person_id"] is None)
        span = h.db.one(
            "SELECT segment_start, segment_end FROM menus WHERE id = ?", (row["menu_id"],)
        )
        if row["kind"] == "range":
            # today to Friday over each day's own menu (a daily cook): the list's own days
            span = h.db.one(
                "SELECT from_day AS segment_start, to_day AS segment_end FROM shopping_lists "
                "WHERE id = ?",
                (int(row["id"]),),
            )
        segment_start: dt.date | None = None
        if span is not None:
            # a list built again from a chosen day buys from that day (migration 0034)
            from_day = h.db.scalar(
                "SELECT from_day FROM shopping_lists WHERE id = ?", (int(row["id"]),)
            )
            segment_start = parse_day(str(from_day or span["segment_start"]))
            end = parse_day(str(span["segment_end"]))
            when = (
                f"{segment_start:%a %d} to {end:%a %d %b}"
                if segment_start != end
                else f"{segment_start:%a %d %b}"
            )
        else:
            when = f"made {str(row['created_at'])[:10]}"
        kind = "" if row["kind"] == "range" else f" ({row['kind'].replace('_', ' ')})"
        lines = [f"<b>Shopping {esc(when)}</b>{kind}"]
        # a rebuilt list has the SAME header shape as any other ("Shopping Mon 14 to Wed 16")
        # as the list it replaces, so rereading it after an Accept looked identical to the
        # basket she is already halfway round with - nothing said the chickpeas in her hand
        # are no longer wanted (list_vs_menu/03). The two lists differ by ingredient, not by
        # header, so that is where the "what changed" line has to come from.
        if row["menu_id"] is not None:
            changed = self._list_change_note(h, person, int(row["menu_id"]), int(row["id"]))
            if changed:
                lines.append(changed)
        # standing at the tins is BEFORE the basket, not after: /menu already warns that this
        # menu still holds something she now avoids, and the list built from the same menu said
        # nothing at all about the tin it was sending her to buy.
        if row["menu_id"] is not None:
            lines += self._refusal_note(h, person, [int(row["menu_id"])])
        rows = []
        dishes: dict[int, list[str]] = {}
        for d in h.db.all(
            "SELECT ri.ingredient_id, rc.name FROM menu_days md "
            "JOIN meal_slots ms ON ms.menu_day_id = md.id "
            "JOIN recipes rc ON rc.id = ms.recipe_id "
            "JOIN recipe_ingredients ri ON ri.recipe_id = rc.id "
            "WHERE md.menu_id = ? ORDER BY ms.id",
            (row["menu_id"],),
        ):
            named = dishes.setdefault(int(d["ingredient_id"]), [])
            name = str(d["name"])
            if name not in named:
                named.append(name)
        total = 0
        priced = 0
        for item in items:
            mark = "x" if int(item["id"]) in ticked else "-"
            product = str(item["product_name"] or "")
            # what the tin is FOR. One join from the row, and the difference between picking up
            # a tin and knowing whether this is the chilli's tin or the curry's - which is the
            # question at the shelf, and the answer to "they had none, does it matter".
            fed = dishes.get(int(item["ingredient_id"]), [])
            for_dish = fed[0] if len(fed) == 1 else _DISH_SEP.join(_short_dish(n) for n in fed)
            # four variable lengths on one line - the ingredient name, its pack label, the
            # product bought and the dish it is for - budgeted TOGETHER, at a word boundary with
            # an ellipsis when one has to give way (phone_reading/02, /04): two of the four used
            # to be sliced to a fixed length mid-word and the other two not bounded at all, so a
            # long dish name alone could push a line past three phone-lines on its own.
            name_t, pack_label_t, product_t, dish_t = _shopping_line_fields(
                str(item["name"]), str(item["pack_label"]), product, for_dish
            )
            packs = f"{item['packs']} x {esc(pack_label_t)}"
            tail = f" ({esc(product_t)})" if product_t else ""
            # what the list is counting on already being in the fridge, so buying less for it
            # is not a silent guess: the same note `shelf_text`
            # puts on the SENT list, missing here because this reread never selected the column.
            # No parens here (unlike `_credit_note`'s SENT-list style): a second "(...)" on this
            # line, with no product tail before it, was read as the product name by anyone
            # parsing the line that way and failed against the real one (phone_reading/02).
            credit = float(item["credit_g"] or 0.0)
            credited = f"; {shopping.num(credit)} g in" if credit > 0 else ""
            says = f" - {esc(dish_t)}" if dish_t else ""
            lines.append(f"{mark} {esc(name_t)} {packs}{tail}{credited}{says}")
            rows.append([(f"{item['name'][:20]}", f"k:tick:{item['id']}")])
            if item["price_pence"] is not None:
                total += int(item["price_pence"])
                priced += 1
        if priced:
            share = "" if priced == len(items) else f" ({priced} of {len(items)} priced)"
            lines.append(f"About £{total / 100:.2f}{share}. Nothing is ever bought for you.")
        if home:
            lines.append(esc(home))
        # what is still to find first, so the buttons that fit are the ones that matter and
        # "send /shopping again" really does reach the rest
        rows.sort(key=lambda row: int(row[0][1].rsplit(":", 1)[1]) in ticked)
        if len(rows) > TICK_BUTTONS:
            cfg = self._config(h)
            if (
                person.tailscale_login
                and cfg is not None
                and cfg.get("web.identity_mode", "header") == "header"
            ):
                # names the Shopping tab specifically, not "the page" generically: a trolley and
                # a phone at item thirteen has no patience for a second tap to find the list he
                # was already reading (shopping_tab/06)
                where = shopping_link(self._config(h), "the page")
                lines.append(f"Buttons for the first {TICK_BUTTONS}; the rest tick on {where}.")
            else:
                # the page answers an unlinked login with a 403, so it is not a place to send her
                lines.append(
                    f"Buttons for the first {TICK_BUTTONS} not yet ticked; "
                    "send /shopping again for the rest."
                )
        lines.append("Shelf empty? <code>/shopping out chickpeas</code> and I will stop asking.")
        h.outbox.reply(
            person,
            check_text("\n".join(lines), kind="shopping", max_lines=SHOPPING_MAX_LINES),
            inline_keyboard(rows[:TICK_BUTTONS]),
        )
        return "handled"

    def _shelf_empty(self, h: Handlers, inbound: Inbound, said: str) -> str:
        """`/shopping out chickpeas` - the shop did not have it, from the person at the shelf.

        Everywhere else a row on a list opens something. The shopping row was the one dead end,
        in front of the one empty shelf, so a week kept being planned around a tin nobody could
        buy and the refresh queue kept asking the same shop for it.

        It writes the same `shop_misses` row the browser session writes for a `not_found`: the
        weekly refresh stops spending a page load on it for MISS_DAYS, and the shopping list
        stops naming a product that is not there. Deliberately NOT a change to the menu - a food
        this shop lacks is not a food they refuse, and `/set avoid` is how you say that.
        """
        from ..food import catalogue  # noqa: PLC0415 (food imports the db layer)
        from ..jobs import shop_refresh  # noqa: PLC0415 (jobs import the scheduler)

        person = inbound.person
        if not said.strip():
            h.outbox.reply(person, "Which food? <code>/shopping out chickpeas</code>.")
            return "handled"
        term = catalogue.normalise(said)
        # match against what is actually ON THE LIST the shop hint names, not the approved
        # catalogue: a food refused since the list's menu was accepted (chickpeas, here) is
        # already gone from `catalogue.approved`, so matching there for a term the LIST itself
        # is naming (dead_ends/07 - the list says "/shopping out chickpeas" and then denies
        # there is any such food) is exactly backwards.
        row = self._list_to_show(h, person)
        listed_ids: set[int] = set()
        if row is not None:
            listed_ids = {
                int(r["ingredient_id"])
                for r in h.db.all(
                    "SELECT DISTINCT ingredient_id FROM shopping_items WHERE list_id = ?",
                    (int(row["id"]),),
                )
            }
        hits = [
            i
            for i in (catalogue.by_id(h.db, iid) for iid in listed_ids)
            if catalogue.refused_matches(term, i)
        ]
        if not hits:
            h.outbox.reply(
                person,
                f"No food on your list called {esc(said.strip()[:40])}. "
                "<code>/shopping</code> names everything on it.",
            )
            return "handled"
        retailer = (
            shop_refresh.default_retailer(self._config(h)) if self._config(h) else ""
        ) or "other"
        for item in hits[:3]:
            h.db.insert(
                "shop_misses",
                ingredient_code=item.code,
                retailer=retailer,
                reason="the person said the shelf was empty",
                seen_at=self.clock.utc_stamp(),
            )
        names = ", ".join(i.name for i in hits[:3])
        if retailer == "other":
            said = f"Noted: {esc(names[:60])} was unavailable."
        else:
            where = shopping.shop_name(retailer) or retailer
            said = (
                f"Noted: {esc(where)} had no {esc(names[:60])}. I will stop looking there for "
                f"{shop_refresh.MISS_DAYS} days."
            )
        h.outbox.reply(person, said + " <code>/cook replace</code> builds a menu without it.")
        return "handled"

    # -- which product a food actually is ------------------------------------------------
    def cmd_shop(self, h: Handlers, inbound: Inbound, arg: str) -> str:
        """`/shop chicken` - the real products behind a food, and which one you buy.

        Nothing here buys anything and nothing is ever ordered: a choice only decides the pack
        the shopping list rounds to and the price it totals.
        """
        from ..food import products  # noqa: PLC0415

        person = inbound.person
        want = arg.strip().lower()
        if not want:
            picked = products.chosen(h.db, person.id)
            if not picked:
                h.outbox.reply(
                    person,
                    "Send <code>/shop chicken</code> to see what a food is in the shop and "
                    "pick the pack you buy. I never order anything.",
                )
                return "handled"
            lines = ["<b>What you buy</b>"] + [
                f"{esc(code)}: {esc(p.name[:50])} {p.price}" for code, p in sorted(picked.items())
            ]
            h.outbox.reply(person, check_text("\n".join(lines), kind="shop", max_lines=40))
            return "handled"
        from ..food import catalogue  # noqa: PLC0415

        matches = [
            ing
            for ing in catalogue.approved(h.db, person.id)
            if want in ing.name.lower() or want in ing.code.lower()
        ]
        if not matches:
            h.outbox.reply(
                person,
                f"No food called {esc(want)} in the catalogue. "
                "<code>/shopping</code> names what is on your list.",
            )
            return "handled"
        food = matches[0]
        code = food.code
        options = products.for_ingredient(h.db, code)
        if not options:
            h.outbox.reply(
                person,
                f"I have not read a shop for {esc(food.name)} yet. That happens "
                "on Sunday mornings, a dozen foods a week.",
            )
            return "handled"
        # a product whose pack size could not be read cannot set a pack, so offering it as
        # "the one you buy" would change nothing except to price the wrong pack
        options = [p for p in options if p.pack_g]
        if not options:
            h.outbox.reply(
                person,
                f"I have products for {esc(food.name)} but could not read a pack size off any "
                "of them, so the shopping list keeps its own pack.",
            )
            return "handled"
        current = products.chosen(h.db, person.id).get(code)
        lines = [f"<b>{esc(food.name)}</b>"]
        buttons: list[list[tuple[str, str]]] = []
        for p in options[:6]:
            mark = "x" if current is not None and current.id == p.id else "-"
            size = f"{p.pack_g:.0f} g" if p.pack_g else "size unknown"
            lines.append(f"{mark} {esc(p.name[:48])} {size} {p.price}")
            buttons.append([(p.name[:24], f"k:prod:{p.id}")])
        lines.append("Tap the one you buy. It sets the pack and the price, and nothing else.")
        h.outbox.reply(
            person,
            check_text("\n".join(lines), kind="shop", max_lines=20),
            inline_keyboard(buttons),
        )
        return "handled"

    def choose_product(self, h: Handlers, inbound: Inbound, parts: list[str]) -> str:
        from ..food import products  # noqa: PLC0415

        if not parts or not parts[0].isdigit():
            return "ignored"
        row = h.db.one("SELECT * FROM products WHERE id = ?", (int(parts[0]),))
        if row is None:
            return "ignored"
        code = str(row["ingredient_code"])
        products.choose(h.db, inbound.person.id, code, int(row["id"]))
        # "Your shopping list will round to that pack" was present tense about the list in their
        # hand, and nothing rebuilds a sent list: they walked off with 2 x 500 g for 900 g of
        # need. The count is arithmetic on numbers already stored, so say it here rather than
        # promising a list that will not change.
        count = self._pack_count(h, inbound.person.id, code, row)
        h.outbox.reply(
            inbound.person,
            f"Noted: {esc(str(row['name'])[:60])}. {count}"
            " Tap a different one on that message to change it.",
        )
        return "handled"

    @staticmethod
    def _pack_count(h: Handlers, person_id: int, code: str, product: Any) -> str:
        """How many of THIS pack the open list's grams need, or the old future-tense promise."""
        from ..food.shopping import packs_for  # noqa: PLC0415 (a sibling food module)

        pack_g = product["pack_g"]
        # the newest line buying this food on a list this person may read
        open_item = next(
            (
                r
                for r in h.db.all(
                    "SELECT si.grams_needed, sl.person_id, sl.owner_ids FROM shopping_items si "
                    "JOIN shopping_lists sl ON sl.id = si.list_id "
                    "JOIN ingredients i ON i.id = si.ingredient_id "
                    "WHERE i.code = ? AND si.packs > 0 "
                    "AND (sl.person_id = ? OR sl.person_id IS NULL) "
                    "ORDER BY si.id DESC LIMIT 5",
                    (code, person_id),
                )
                if shopping.readable(r, person_id)
            ),
            None,
        )
        if open_item is None or not pack_g:
            return "The next list will use that pack."
        grams = float(open_item["grams_needed"])
        packs, _left = packs_for(grams, float(pack_g))
        return (
            f"Your open list needs {grams:.0f} g, so that is {packs} of them. "
            "The next list will say so itself."
        )

    def cmd_rate(self, h: Handlers, inbound: Inbound, arg: str) -> str:
        """`/rate` asks about today's main meal; `/rate 4` records the four.

        Two things were wrong here. `intents` answers an unreadable rating with "Send /rate 4."
        and `/help` calls it "rate a dish 1-5", and the argument was never read: the number they
        were told to send was dropped and the buttons came back instead.

        And the dish was `ms.slot = 'dinner'`. Two meals a day is an answer the interview
        accepts, so a menu can have no dinner in it, and then the command named after rating
        always said "Nothing to rate today." while a lunch and a breakfast sat on the menu. Same
        main-meal ordering as `/recipe`, which had already learned this.
        """
        person = inbound.person
        today = self.clock.today_local()
        row = h.db.one(
            "SELECT rc.id, rc.name FROM v_menu_days_current md "
            "JOIN meal_slots ms ON ms.menu_day_id = md.id JOIN recipes rc ON rc.id = ms.recipe_id "
            "WHERE md.person_id = ? AND md.day = ? "
            "ORDER BY CASE ms.slot WHEN 'dinner' THEN 0 WHEN 'lunch' THEN 1 "
            "WHEN 'breakfast' THEN 2 ELSE 3 END, ms.id LIMIT 1",
            (person.id, isoday(today)),
        )
        if row is None:
            h.outbox.reply(person, "Nothing to rate today. /menu shows what is planned.")
            return "handled"
        said = arg.strip()
        if said.isdigit() and RATE_MIN <= int(said) <= RATE_MAX:
            return self.rate(h, inbound, [str(row["id"]), said])
        keyboard = inline_keyboard(
            [[(str(n), f"k:rate:{row['id']}:{n}") for n in range(RATE_MIN, RATE_MAX + 1)]]
        )
        h.outbox.reply(person, f"How was {esc(str(row['name']))}?", keyboard)
        return "handled"
