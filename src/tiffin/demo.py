"""A disposable, offline example household for the web page and screenshots."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import json
import tempfile
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

from . import links
from .app import App
from .clock import Clock, week_start
from .config import Config, deep_merge, load
from .db import Db
from .errors import ConfigError
from .http import Response, TransportError
from .secrets import Secrets

_DEMO_CAPABILITY = object()


def _offline_transport(
    url: str, method: str, data: bytes | None, headers: Mapping[str, str], timeout_s: float
) -> Response:
    raise TransportError("the demo has no external connections")


class DemoApp(App):
    """Created only by this module with a temporary home and a disabled model."""

    def __init__(
        self,
        config: Config,
        secrets: Secrets,
        *,
        clock: Clock,
        capability: object,
    ) -> None:
        if capability is not _DEMO_CAPABILITY or config.env != "dev":
            raise ConfigError("demo application must be created by tiffin demo")
        super().__init__(config, secrets, clock=clock, transport=_offline_transport)


class FakeModel:
    """Choose numbered catalogue entries deterministically, without a provider call."""

    DISHES = (
        ("Porridge with yoghurt and berries", (1, 2, 3)),
        ("Chicken, rice and greens", (4, 5, 6)),
        ("Bean and tomato bowl", (5, 6, 7)),
        ("Eggs and toast", (8, 9, 6)),
    )

    def choose(self, index: int) -> tuple[str, tuple[int, ...]]:
        return self.DISHES[index % len(self.DISHES)]


FOODS = (
    ("Oats", 370, 13, 7, 60, 10, 1000, "grains"),
    ("Plain yoghurt", 64, 5, 3, 5, 0, 500, "dairy"),
    ("Berries", 50, 1, 0, 11, 4, 300, "produce"),
    ("Chicken breast", 110, 23, 2, 0, 0, 500, "meat"),
    ("Rice", 350, 7, 1, 78, 1, 1000, "grains"),
    ("Broccoli", 34, 3, 0, 4, 3, 300, "produce"),
    ("Beans in tomato sauce", 95, 5, 1, 14, 5, 400, "tins"),
    ("Eggs", 145, 13, 10, 1, 0, 360, "dairy"),
    ("Wholemeal bread", 245, 9, 3, 41, 7, 800, "bakery"),
)


def make_app(home: Path) -> DemoApp:
    config = load("dev", root=home)
    config = replace(
        config,
        data=deep_merge(
            config.data,
            {
                "model": {"provider": "none"},
                "web": {
                    "host": "127.0.0.1",
                    "root_path": "",
                    "origin": "http://127.0.0.1",
                    "identity_mode": "telegram",
                },
                "persons": {
                    "owner": {"display_name": "Alex"},
                    "partner": {"display_name": "Sam"},
                },
                "household": {"partner_expected": True},
            },
        ),
    )
    return DemoApp(
        config,
        Secrets(
            config.paths.secrets,
            {"TIFFIN_ICU_KEY_OWNER": "demo", "TIFFIN_ICU_KEY_PARTNER": "demo"},
        ),
        clock=Clock(),
        capability=_DEMO_CAPABILITY,
    )


def _recipe(
    db: Db, name: str, indexes: tuple[int, ...], grams: tuple[int, ...]
) -> tuple[int, tuple[float, ...]]:
    recipe_id = db.insert(
        "recipes",
        name=name,
        servings=1,
        prep_minutes=20,
        method_json=json.dumps(["Prepare the ingredients.", "Cook until ready and serve."]),
        source="model",
        model_used="demo-fake",
        freezable=int("Chicken" in name or "Bean" in name),
    )
    totals = [0.0] * 5
    for number, amount in zip(indexes, grams, strict=True):
        db.insert("recipe_ingredients", recipe_id=recipe_id, ingredient_id=number, grams=amount)
        for i, value in enumerate(FOODS[number - 1][1:6]):
            totals[i] += amount * float(value) / 100
    return recipe_id, tuple(totals)


def seed(app: DemoApp) -> None:
    """Build real ledger rows from fixed catalogue indexes and code-calculated nutrients."""
    today = app.clock.today_local()
    with app.bootstrap() as db, db.tx():
        people = app.registry(db).active()
        menu_ids: list[int] = []
        for index, food in enumerate(FOODS, 1):
            name, kcal, protein, fat, carb, fibre, pack, aisle = food
            db.insert(
                "ingredients",
                code=f"demo_{index}",
                version=1,
                name=name,
                kcal_100=kcal,
                protein_100=protein,
                fat_100=fat,
                carb_100=carb,
                fibre_100=fibre,
                pack_g=pack,
                pack_label=f"{pack} g",
                aisle=aisle,
            )
        model = FakeModel()
        portions = ((80, 150, 100), (180, 90, 180), (120, 180, 160), (120, 100, 80))
        dishes = [_recipe(db, *model.choose(i), portions[i]) for i in range(4)]
        first = today - dt.timedelta(days=20)
        for person in people:
            pid = person.id
            portion_factor = 1.3 if person.slug == "owner" else 1.1
            base_weight = 83.0 if person.slug == "owner" else 68.0
            db.insert(
                "profiles",
                person_id=pid,
                effective_from=first.isoformat(),
                sex="m" if pid == 1 else "f",
                birth_date="1990-01-01",
                height_cm=178 if pid == 1 else 165,
                activity_factor=1.45,
                kcal_floor=1600 if pid == 1 else 1300,
                goal_weight_kg=79 if pid == 1 else 65,
                planning="choose",
                choices=2,
            )
            plan = db.insert(
                "training_plans",
                person_id=pid,
                effective_from=first.isoformat(),
                note="Example training week",
            )
            for weekday, kind in ((1, "run"), (3, "strength"), (5, "walk")):
                db.insert(
                    "planned_sessions",
                    plan_id=plan,
                    person_id=pid,
                    weekday=weekday,
                    kind=kind,
                    target_minutes=40,
                )
            for offset in range(22):
                day = first + dt.timedelta(days=offset)
                if day > today + dt.timedelta(days=1):
                    break
                iso = day.isoformat()
                if day.weekday() == 0 or offset == 0:
                    db.insert(
                        "targets",
                        person_id=pid,
                        week_start=week_start(day).isoformat(),
                        kcal=2200 if pid == 1 else 1850,
                        protein_g=140 if pid == 1 else 115,
                        fat_g=70 if pid == 1 else 60,
                        carb_g=240 if pid == 1 else 195,
                        fibre_g=30,
                        source="formula",
                    )
                if day <= today and offset % 2 == 0:
                    weight = round(base_weight - offset * 0.06 + (offset % 4) * 0.08, 1)
                    weigh_id = db.insert(
                        "weigh_ins", person_id=pid, day=iso, weight_kg=weight, source="manual"
                    )
                    db.insert(
                        "trend_points",
                        person_id=pid,
                        day=iso,
                        trend_kg=round(base_weight - offset * 0.05, 1),
                        alpha=0.1,
                        n=offset // 2 + 1,
                        weigh_in_id=weigh_id,
                    )
                if day <= today and offset % 7 == 0:
                    for site, cm in (
                        ("waist", 88 if pid == 1 else 76),
                        ("hip", 100 if pid == 1 else 96),
                    ):
                        db.insert(
                            "tape_measures",
                            person_id=pid,
                            day=iso,
                            site=site,
                            cm=cm - offset * 0.08,
                        )
                if day < today - dt.timedelta(days=3):
                    db.insert(
                        "day_confirmations",
                        person_id=pid,
                        day=iso,
                        status="confirmed",
                        basis="menu",
                        kcal=2100 if pid == 1 else 1770,
                        protein_g=135 if pid == 1 else 110,
                        fat_g=68 if pid == 1 else 58,
                        carb_g=225 if pid == 1 else 190,
                        fibre_g=29,
                    )
                raw_id = db.insert(
                    "icu_raw",
                    person_id=pid,
                    resource="wellness",
                    source_id=f"demo-{iso}",
                    revision=1,
                    endpoint_class="demo",
                    state="live",
                    payload="{}",
                    payload_sha256=hashlib.sha256(b"{}").hexdigest(),
                    fetched_at=app.clock.utc_stamp(),
                    endpoint="demo",
                    sync_id="demo",
                )
                db.insert(
                    "icu_wellness",
                    person_id=pid,
                    day=iso,
                    steps=7200 + offset * 90,
                    sleep_s=27000,
                    resting_hr=59,
                    icu_raw_id=raw_id,
                )
                if day.weekday() in (1, 3, 5) and day < today:
                    activity_id = f"demo-activity-{pid}-{iso}"
                    activity_raw = db.insert(
                        "icu_raw",
                        person_id=pid,
                        resource="activity",
                        source_id=activity_id,
                        revision=1,
                        endpoint_class="demo",
                        state="live",
                        payload="{}",
                        payload_sha256=hashlib.sha256(b"{}").hexdigest(),
                        fetched_at=app.clock.utc_stamp(),
                        endpoint="demo",
                        sync_id="demo",
                    )
                    kind = {1: "run", 3: "strength", 5: "walk"}[day.weekday()]
                    db.insert(
                        "icu_activity",
                        person_id=pid,
                        icu_id=activity_id,
                        day=iso,
                        kind=kind,
                        name=f"Example {kind}",
                        moving_s=2400,
                        elapsed_s=2400,
                        calories=260,
                        icu_raw_id=activity_raw,
                    )
                    db.insert(
                        "session_matches",
                        person_id=pid,
                        day=iso,
                        status="matched",
                        icu_id=activity_id,
                        minutes=40,
                    )
            menu_start = today - dt.timedelta(days=3)
            menu_end = today + dt.timedelta(days=4)
            menu_id = db.insert(
                "menus",
                person_id=pid,
                segment_start=menu_start.isoformat(),
                segment_end=menu_end.isoformat(),
                status="accepted",
                model_used="demo-fake",
            )
            menu_ids.append(menu_id)
            for offset in range(8):
                day = menu_start + dt.timedelta(days=offset)
                meal_plan = (
                    ("breakfast", 0),
                    ("lunch", 1 if offset % 2 else 2),
                    ("dinner", 3 if offset % 2 else 1),
                )
                day_macros = [
                    sum(dishes[index][1][n] * portion_factor for _, index in meal_plan)
                    for n in range(5)
                ]
                menu_day = db.insert(
                    "menu_days",
                    menu_id=menu_id,
                    day=day.isoformat(),
                    kcal=round(day_macros[0]),
                    protein_g=day_macros[1],
                    fat_g=day_macros[2],
                    carb_g=day_macros[3],
                    fibre_g=day_macros[4],
                )
                for slot, dish_index in meal_plan:
                    recipe_id, nutrients = dishes[dish_index]
                    db.insert(
                        "meal_slots",
                        menu_day_id=menu_day,
                        slot=slot,
                        recipe_id=recipe_id,
                        portion_multiplier=portion_factor,
                        portion_g=sum(portions[dish_index]) * portion_factor,
                        kcal=round(nutrients[0] * portion_factor),
                        protein_g=nutrients[1] * portion_factor,
                        fat_g=nutrients[2] * portion_factor,
                        carb_g=nutrients[3] * portion_factor,
                        fibre_g=nutrients[4] * portion_factor,
                    )
            shop_id = db.insert(
                "shopping_lists",
                menu_id=menu_id,
                kind="segment",
                person_id=pid,
                from_day=today.isoformat(),
                to_day=menu_end.isoformat(),
            )
            for ingredient_id, grams in (
                (1, 320),
                (2, 600),
                (4, 720),
                (5, 500),
                (6, 800),
                (7, 400),
            ):
                food = FOODS[ingredient_id - 1]
                pack = int(food[6])
                needed = round(grams * portion_factor)
                item_id = db.insert(
                    "shopping_items",
                    list_id=shop_id,
                    ingredient_id=ingredient_id,
                    grams_needed=needed,
                    packs=(needed + pack - 1) // pack,
                    leftover_g=((needed + pack - 1) // pack) * pack - needed,
                    aisle=food[7],
                    pack_g=pack,
                    pack_label=f"{pack} g",
                )
                if ingredient_id == 1:
                    db.insert("shopping_ticks", item_id=item_id, person_id=pid, ticked=1)
            db.insert("ratings", person_id=pid, recipe_id=dishes[1][0], score=4)
            offplan = dishes[2][1]
            db.insert(
                "estimates",
                person_id=pid,
                day=today.isoformat(),
                description="Bean and tomato bowl after a walk",
                kcal=round(offplan[0]),
                protein_g=offplan[1],
                fat_g=offplan[2],
                carb_g=offplan[3],
                fibre_g=offplan[4],
                status="accepted",
            )
            db.insert("batch_ticks", person_id=pid, menu_id=menu_id, recipe_id=dishes[1][0], done=1)
            db.insert(
                "weekly_reviews",
                person_id=pid,
                week_end=(today - dt.timedelta(days=today.weekday() + 1)).isoformat(),
                full_html=(
                    "<b>Example week</b>: meals recorded, three training sessions "
                    "and a steady weight trend."
                ),
            )
            next_monday = week_start(today) + dt.timedelta(days=7)
            choice_id = db.insert(
                "choice_sets",
                person_id=pid,
                week_start=next_monday.isoformat(),
                first_day=next_monday.isoformat(),
                last_day=(next_monday + dt.timedelta(days=4)).isoformat(),
                per_slot=2,
                slots_json='["lunch","dinner"]',
                shares_json='{"lunch":0.35,"dinner":0.4}',
                model_used="demo-fake",
            )
            for day_offset in range(5):
                option_day = (next_monday + dt.timedelta(days=day_offset)).isoformat()
                for slot in ("lunch", "dinner"):
                    for position, dish_index in enumerate((1, 2), 1):
                        recipe_id, nutrients = dishes[dish_index]
                        db.insert(
                            "choice_options",
                            set_id=choice_id,
                            day=option_day,
                            slot=slot,
                            position=position,
                            recipe_id=recipe_id,
                            portion_multiplier=portion_factor,
                            kcal=round(nutrients[0] * portion_factor),
                            protein_g=nutrients[1] * portion_factor,
                            fat_g=nutrients[2] * portion_factor,
                            carb_g=nutrients[3] * portion_factor,
                            fibre_g=nutrients[4] * portion_factor,
                            origin="new",
                        )
            db.insert(
                "consents", person_id=pid, kind="shared_meals", granted=1, wording_version="demo"
            )
        saturday = today + dt.timedelta(days=(5 - today.weekday()) % 7)
        # the two demo people are linked, as a pair that was sharing weekends would be
        link_id = links.record_migrated(db, people[0].id, people[1].id)
        shared_id = db.insert(
            "shared_menus",
            segment_start=saturday.isoformat(),
            segment_end=(saturday + dt.timedelta(days=1)).isoformat(),
            model_used="demo-fake",
            link_id=link_id,
        )
        for person, menu_id in zip(people, menu_ids, strict=True):
            db.insert(
                "shared_menu_members",
                shared_menu_id=shared_id,
                person_id=person.id,
                menu_id=menu_id,
            )
        db.insert("weekend_modes", mode="together")


CONVERSATIONS = (
    (
        "Onboarding",
        (
            ("Alex", "/start"),
            ("Tiffin", "Welcome. You can set up targets and choose how to plan meals."),
            ("Alex", "I'll start with manual planning."),
            (
                "Tiffin",
                "Manual planning is ready. Send /plan to make a menu. "
                "You can return to the optional questions with /followup.",
            ),
        ),
    ),
    (
        "A later question",
        (
            (
                "Tiffin",
                "Optional questions: How do you want to plan food? How often should I message "
                "you? Are there foods to avoid? Skip any question.",
            ),
            ("Alex", "Skip"),
            (
                "Tiffin",
                "Skipped. Manual planning stays on. Send /followup when you want to answer.",
            ),
        ),
    ),
    (
        "Morning",
        (
            (
                "Tiffin",
                "Good morning, Alex. Today's menu is ready.",
            ),
        ),
    ),
    (
        "Log a meal",
        (
            ("Alex", "I had the chicken, rice and greens for lunch."),
            ("Tiffin", "Lunch recorded as planned. You can correct the day this evening."),
        ),
    ),
    (
        "Menu",
        (
            ("Alex", "/menu"),
            (
                "Tiffin",
                "Today: porridge with yoghurt and berries; "
                "chicken, rice and greens; eggs and toast.",
            ),
        ),
    ),
    (
        "Shopping",
        (
            ("Alex", "/shopping"),
            (
                "Tiffin",
                "For this menu: oats, yoghurt, chicken, rice, broccoli and beans. "
                "Quantities are rounded to whole packs.",
            ),
        ),
    ),
    (
        "Weekly check",
        (
            (
                "Tiffin",
                "Is the plan still working for you this week?",
            ),
            ("Alex", "Yes, keep next week's targets."),
        ),
    ),
)


def write_transcript(path: Path) -> None:
    panels = []
    for title, messages in CONVERSATIONS:
        bubbles = "".join(
            f'<div class="bubble {"mine" if who == "Alex" else "bot"}">'
            f"<small>{html.escape(who)}</small>"
            f"<p>{html.escape(message)}</p></div>"
            for who, message in messages
        )
        panels.append(f"<section><h2>{html.escape(title)}</h2>{bubbles}</section>")
    document = (
        '<!doctype html><html lang="en-GB"><meta charset="utf-8"><meta name="viewport" '
        'content="width=device-width,initial-scale=1">'
        "<title>Tiffin: example Telegram chats</title><style>"
        "body{font:15px/1.45 system-ui;background:#dce8e1;color:#182b26;"
        "margin:0;padding:20px}"
        "main{max-width:820px;margin:auto}h1{font-size:1.5rem}"
        "section{background:#eaf0ed;padding:16px;margin:20px 0;"
        "border-radius:12px;box-shadow:0 3px 14px #0002}h2{font-size:1rem;margin:0 0 15px}"
        ".bubble{max-width:75%;padding:8px 12px;margin:8px 0;border-radius:12px;clear:both}"
        ".mine{background:#d4efc7;margin-left:auto}.bot{background:white;margin-right:auto}"
        "small{font-weight:700;color:#40675c}p{margin:3px 0 0}</style>"
        "<main><h1>Example Telegram conversations</h1>" + "".join(panels) + "</main></html>"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(document, encoding="utf-8")


def run(args: argparse.Namespace) -> int:
    if args.telegram_transcript is not None:
        write_transcript(args.telegram_transcript)
        print(f"Example transcript: {args.telegram_transcript}")
        return 0
    if not 1 <= args.port <= 65535:
        raise ConfigError("demo port must be between 1 and 65535")
    import uvicorn  # noqa: PLC0415

    from .web.app import create_app  # noqa: PLC0415

    with tempfile.TemporaryDirectory(prefix=".tiffin-demo-", dir=Path.cwd()) as home:
        app = make_app(Path(home))
        seed(app)
        with app.open_db() as db:
            person = app.registry(db).require("owner")
        web = create_app(app, demo_person=person)
        print(f"Demo on http://127.0.0.1:{args.port}/ (temporary data; Ctrl+C to close)")
        uvicorn.run(web, host="127.0.0.1", port=args.port, access_log=False, log_level="warning")
    return 0
