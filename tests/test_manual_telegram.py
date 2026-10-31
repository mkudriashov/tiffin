"""A paired person can make the first menu and shopping list without web or model access."""

from __future__ import annotations

import datetime as dt
from typing import cast
from unittest.mock import Mock

from tiffin.db import Db
from tiffin.food import catalogue, menu
from tiffin.persons import PersonRegistry
from tiffin.telegram import menuflow
from tiffin.telegram.handlers import Handlers
from tiffin.telegram.outbox import Outbox
from tiffin.telegram.poller import Inbound

from .test_food_catalogue import BASE_YAML, write_csv, write_yaml


def test_first_manual_telegram_menu_and_list(
    config, fixture_db: Db, two_persons: dict[str, int], frozen_clock, tmp_path
) -> None:
    catalogue.build(fixture_db, write_yaml(tmp_path, BASE_YAML), write_csv(tmp_path))
    registry = PersonRegistry(fixture_db, config)
    person = registry.require("owner")
    outbox = Mock()
    handler = Handlers(fixture_db, cast(Outbox, outbox), registry, clock=frozen_clock)
    menuflow.install(handler, clock=frozen_clock)

    def say(arg: str) -> None:
        inbound = Inbound(1, person, "command", 1000001, {}, text=f"/plan {arg}")
        assert handler.handle(inbound) == "handled"

    say("foods chicken")
    assert "chicken_breast" in outbox.reply.call_args.args[1]
    say(
        "recipe Chicken and rice | 1 | 20 | chicken_breast:200, rice_basmati:100 | "
        "Cook rice; Cook chicken"
    )
    recipe_id = int(fixture_db.scalar("SELECT id FROM recipes ORDER BY id DESC LIMIT 1"))
    assert f"Recipe {recipe_id} saved" in outbox.reply.call_args.args[1]
    say(f"menu 2026-09-08 | 2 | dinner:{recipe_id}:1")
    assert "shopping list is ready" in outbox.reply.call_args.args[1]
    assert menu.accepted_menu_id(fixture_db, person.id, dt.date(2026, 9, 8)) is not None
    assert menu.accepted_menu_id(fixture_db, two_persons["partner"], dt.date(2026, 9, 8)) is None
    assert fixture_db.scalar("SELECT COUNT(*) FROM shopping_lists") == 1

    say("menu 2026-09-08 | 1 | dinner:9999:1")
    assert "available dishes" in outbox.reply.call_args.args[1]
    assert fixture_db.scalar("SELECT COUNT(*) FROM menus") == 1


def test_recipe_prompt_resumes_and_keeps_person_scope(
    config, fixture_db: Db, two_persons: dict[str, int], frozen_clock, tmp_path
) -> None:
    catalogue.build(fixture_db, write_yaml(tmp_path, BASE_YAML), write_csv(tmp_path))
    registry = PersonRegistry(fixture_db, config)
    owner = registry.require("owner")
    partner = registry.require("partner")
    outbox = Mock()

    def handler() -> Handlers:
        current = Handlers(fixture_db, cast(Outbox, outbox), registry, clock=frozen_clock)
        menuflow.install(current, clock=frozen_clock)
        return current

    def send(current: Handlers, person, message: str) -> None:
        kind = "command" if message.startswith("/") else "text"
        inbound = Inbound(1, person, kind, 1000001, {}, text=message)
        assert current.handle(inbound) == "handled"

    current = handler()
    send(current, owner, "/plan recipe")
    send(current, owner, "Chicken and rice")
    send(current, partner, "/plan recipe")
    send(current, partner, "/plan cancel")
    current = handler()  # a restart keeps the owner's unfinished recipe
    send(current, owner, "/plan recipe")
    assert "servings" in outbox.reply.call_args.args[1]
    for answer in ("1", "20", "chicken_breast:200, rice_basmati:100"):
        send(current, owner, answer)
    send(current, owner, "Cook rice; Cook chicken")
    assert "saved" in outbox.reply.call_args.args[1]
    assert fixture_db.scalar("SELECT COUNT(*) FROM recipes WHERE person_scope = 'owner'") == 1
    assert fixture_db.scalar("SELECT COUNT(*) FROM recipes WHERE person_scope = 'partner'") == 0
    assert (
        fixture_db.scalar(
            "SELECT COUNT(*) FROM conversation_state WHERE person_id = ?", (owner.id,)
        )
        == 0
    )
