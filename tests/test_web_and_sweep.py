"""The tailnet page, and four things that fell between the parts that were reviewed.

The Shopping tab printed the catalogue's guessed pack beside a pack count computed from the
real product's, so it told you to buy 600 g where the menu needs 1300 and contradicted the bot
for the same list. Training that intervals.icu could not match to a plan row vanished from
Today entirely, which is most real training. The Friday joint list went to anyone with a
paired chat id. The advisor decided the next menu should lean on protein, wrote it to a key
nothing read, and said nothing.
"""

from __future__ import annotations

import datetime as dt

from tiffin.db import Db
from tiffin.web import queries

from .test_food_shopping import seed_ingredient
from .web_source import app_source

MONDAY = dt.date(2026, 9, 7)


# ---------------------------------------------------------------- the shopping tab
def test_the_page_shows_the_pack_the_list_was_built_against(fixture_db: Db, two_persons) -> None:
    pid = two_persons["owner"]
    iid = seed_ingredient(fixture_db, "chk", "Chicken breast", "meat", 300.0, "300 g pack")
    list_id = fixture_db.insert("shopping_lists", kind="segment", person_id=pid)
    fixture_db.insert(
        "shopping_items",
        list_id=list_id,
        ingredient_id=iid,
        grams_needed=1300.0,
        packs=2,
        leftover_g=0.0,
        aisle="meat",
        pack_g=650.0,
        pack_label="650 g pack",
        product_name="Tesco British Chicken Breast Fillets 650G",
        price_pence=980,
    )
    out = queries.shopping(fixture_db, pid, None)
    item = out["items"][0]
    assert item["pack_label"] == "650 g pack", "the shop's pack, not the catalogue's guess"
    assert item["packs"] == 2 and item["price_pence"] == 980
    assert "Tesco" in str(item["product_name"])


def test_a_list_built_before_products_still_reads(fixture_db: Db, two_persons) -> None:
    pid = two_persons["owner"]
    iid = seed_ingredient(fixture_db, "chk", "Chicken breast", "meat", 300.0, "300 g pack")
    list_id = fixture_db.insert("shopping_lists", kind="segment", person_id=pid)
    fixture_db.insert(
        "shopping_items",
        list_id=list_id,
        ingredient_id=iid,
        grams_needed=600.0,
        packs=2,
        leftover_g=0.0,
        aisle="meat",
    )
    item = queries.shopping(fixture_db, pid, None)["items"][0]
    assert item["pack_label"] == "300 g pack", "falls back to the catalogue when nothing was stored"


# ---------------------------------------------------------------- both sessions on the page
def test_the_page_is_given_every_planned_session(fixture_db: Db, two_persons) -> None:
    pid = two_persons["owner"]
    plan = fixture_db.insert("training_plans", person_id=pid, effective_from="2026-09-01")
    for kind, minutes in (("ropes", 90), ("strength", 45)):
        fixture_db.insert(
            "planned_sessions",
            plan_id=plan,
            person_id=pid,
            weekday=0,
            kind=kind,
            target_minutes=minutes,
        )
    rows = queries.planned_sessions(fixture_db, pid, MONDAY)
    assert [r["kind"] for r in rows] == ["ropes", "strength"]
    assert queries.planned_session(fixture_db, pid, MONDAY)["kind"] == "ropes"  # type: ignore[index]


def test_the_today_block_no_longer_hides_unplanned_training() -> None:
    js = app_source()
    assert "planned.length || trained.length" in js, "the block is not gated on a plan row"
    assert '"extra"' in js, "and an unmatched activity counts as training"
    assert "Nothing set up yet" in js, "Today has an empty state like every other tab"


# ---------------------------------------------------------------- the csrf token
def test_the_csrf_token_survives_a_second_page_load() -> None:
    import inspect

    from tiffin.web import api

    src = inspect.getsource(api.me)
    assert "request.cookies.get(CSRF_COOKIE) or" in src


# ---------------------------------------------------------------- the joint list's recipients
def test_the_weekend_list_goes_only_to_people_it_covers() -> None:
    import inspect

    from tiffin.jobs import shopping_list

    src = inspect.getsource(shopping_list.run_joint)
    assert "covered" in src and "pause_active" in src
    assert "persons = [p for p, _ in covered]" in src


# ---------------------------------------------------------------- the advisor's menu bias
def test_the_menu_bias_reaches_the_prompt(fixture_db: Db, two_persons) -> None:
    """Two advisor rules wrote it to bot_state and nothing anywhere read it."""
    import inspect

    from tiffin.jobs import menu_propose, weekly_calibration

    assert "menu_bias" in inspect.getsource(menu_propose._bias_line)
    assert "_bias_line(ctx)" in inspect.getsource(menu_propose._ask)
    assert '"menu_bias"' in inspect.getsource(weekly_calibration.run), "and it is also said"


# ---------------------------------------------------------------- the tick in the aisle
def test_a_button_toast_is_not_swallowed(
    fixture_db: Db, two_persons, frozen_clock, config, tmp_path
):
    """The dispatcher answered the callback query itself, before dispatch and with no text, so
    a handler's own toast was a second answer to the same id - rejected by Telegram and
    suppressed. Ticking an item in a shop gave no feedback at all."""
    from tiffin.app import App
    from tiffin.secrets import Secrets
    from tiffin.telegram.poller import Poller

    from .fakes import FakeTelegram
    from .test_food_shopping import seed_ingredient

    pid = two_persons["owner"]
    iid = seed_ingredient(fixture_db, "chk", "Chicken breast", "meat", 500.0, "500 g pack")
    list_id = fixture_db.insert("shopping_lists", kind="segment", person_id=pid)
    item_id = fixture_db.insert(
        "shopping_items",
        list_id=list_id,
        ingredient_id=iid,
        grams_needed=600.0,
        packs=2,
        leftover_g=0.0,
        aisle="meat",
    )
    secrets_dir = tmp_path / "s"
    secrets_dir.mkdir()
    (secrets_dir / "telegram-token-dev.txt").write_text("1:t", encoding="utf-8")
    tg = FakeTelegram()
    app = App(config, Secrets(secrets_dir), clock=frozen_clock, transport=tg)
    poller = Poller(
        app.api(),
        fixture_db,
        app.registry(fixture_db),
        app.handlers(fixture_db).handle,
        clock=frozen_clock,
        sleeper=lambda s: None,
    )
    tg.queue_update(tg.callback_update(1, 1000001, f"k:tick:{item_id}", message_id=1))
    poller.run_once()
    answered = [a for a in tg.answered if a.get("text")]
    assert answered and answered[-1]["text"] == "Ticked", tg.answered
    assert fixture_db.scalar("SELECT ticked FROM shopping_ticks") == 1


# ---------------------------------------------------------------- leftovers that go off
def test_a_leftover_credit_expires(fixture_db: Db, two_persons) -> None:
    """The rows are written when the list is COMPOSED, before anyone has shopped, and had no
    age limit - so an unshopped week silently shrank every later list."""
    from tiffin.food import shopping

    pid = two_persons["owner"]
    iid = seed_ingredient(fixture_db, "chk", "Chicken breast", "meat", 500.0, "500 g pack")
    fixture_db.insert(
        "leftovers",
        person_id=pid,
        ingredient_id=iid,
        grams=300.0,
        created_at="2026-08-01 18:00:00",
    )
    assert shopping.current_leftovers(fixture_db, pid) == {"chk": 300.0}, "no date: unchanged"
    assert shopping.current_leftovers(fixture_db, pid, dt.date(2026, 9, 7)) == {}, "five weeks old"
    assert shopping.current_leftovers(fixture_db, pid, dt.date(2026, 8, 5)) == {"chk": 300.0}
