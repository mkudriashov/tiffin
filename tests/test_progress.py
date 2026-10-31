"""Progress shows measured facts, plans and unknowns as three different things."""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tiffin import body
from tiffin.app import App
from tiffin.clock import TZ, FrozenClock
from tiffin.config import Config
from tiffin.db import Db
from tiffin.secrets import Secrets
from tiffin.web.app import create_app
from tiffin.web.queries import progress, trend

from .fakes import FakeTelegram

TODAY = dt.date(2026, 9, 7)
MONDAY = TODAY


def _profile(db: Db, person: int, day: str, floor: int, goal: float | None = None) -> None:
    db.insert(
        "profiles",
        person_id=person,
        effective_from=day,
        sex="m",
        birth_date="1988-03-11",
        height_cm=180,
        activity_factor=1.5,
        kcal_floor=1500,
        steps_floor=floor,
        goal_weight_kg=goal,
    )


def _menu(db: Db, person: int, start: dt.date, name: str = "Porridge") -> int:
    menu = db.insert(
        "menus",
        person_id=person,
        segment_start=start.isoformat(),
        segment_end=(start + dt.timedelta(days=6)).isoformat(),
        status="accepted",
    )
    recipe = db.insert(
        "recipes",
        name=name,
        servings=1,
        prep_minutes=5,
        method_json="[]",
        source="manual",
    )
    for offset in range(7):
        day = (start + dt.timedelta(days=offset)).isoformat()
        md = db.insert(
            "menu_days",
            menu_id=menu,
            day=day,
            kcal=1900,
            protein_g=200,
            fat_g=70,
            carb_g=200,
            fibre_g=30,
        )
        db.insert(
            "meal_slots",
            menu_day_id=md,
            slot="breakfast",
            recipe_id=recipe,
            portion_multiplier=1,
            portion_g=400,
            kcal=500,
            protein_g=40,
            fat_g=10,
            carb_g=50,
            fibre_g=10,
        )
    return recipe


def _wellness(db: Db, person: int, day: dt.date, steps: int | None) -> None:
    db.insert(
        "icu_wellness",
        person_id=person,
        day=day.isoformat(),
        steps=steps,
        sleep_s=25200,
        resting_hr=52,
        hrv=60,
        icu_raw_id=0,
    )


def _weight(db: Db, person: int, days: int, *, slope: float) -> None:
    for i in range(days):
        day = TODAY - dt.timedelta(days=days - 1 - i)
        db.insert(
            "weigh_ins",
            person_id=person,
            day=day.isoformat(),
            weight_kg=round(100 + slope * i, 2),
            source="manual",
        )
    body.rebuild_trend(db, person)


def _read(db: Db, person: int, weeks: int = 8, day: dt.date = TODAY) -> dict:
    return progress(db, person, weeks, day, TODAY)


def test_a_person_with_nothing_has_all_optional_sections_empty(
    fixture_db: Db, two_persons: dict
) -> None:
    result = _read(fixture_db, two_persons["partner"])
    assert len(result["days"]) == 7
    assert all(
        result["cards"][key] is None
        for key in ("weight", "steps", "training", "sleep", "recovery", "plan", "food")
    )
    assert all(value is None for value in result["charts"].values())
    assert result["cards"]["recorded"] == {
        "days": 0,
        "of": 1,
        "avg_kcal": None,
        "target_kcal": result["cards"]["recorded"]["target_kcal"],
    }
    assert result["days"][0]["planned_kcal"] is None
    assert result["week"]["next"] is None


def test_the_full_shape_keeps_plan_record_and_training_apart(
    fixture_db: Db, two_persons: dict
) -> None:
    db, person = fixture_db, two_persons["owner"]
    _profile(db, person, "2026-01-01", 8000, 85)
    _menu(db, person, MONDAY)
    db.insert(
        "targets",
        person_id=person,
        week_start=MONDAY.isoformat(),
        kcal=1914,
        protein_g=199,
        fat_g=70,
        carb_g=200,
        fibre_g=27,
        source="formula",
    )
    _weight(db, person, 30, slope=-0.04)
    for i in range(7):
        _wellness(db, person, TODAY - dt.timedelta(days=i), 9000)
    db.insert(
        "day_confirmations",
        person_id=person,
        day=TODAY.isoformat(),
        status="confirmed",
        basis="menu",
        kcal=1850,
    )
    plan = db.insert("training_plans", person_id=person, effective_from="2026-01-01")
    db.insert(
        "planned_sessions",
        plan_id=plan,
        person_id=person,
        weekday=0,
        kind="ride",
        target_minutes=60,
    )
    db.insert(
        "icu_activity",
        person_id=person,
        icu_id="ride-1",
        day=TODAY.isoformat(),
        kind="ride",
        name="Ride",
        moving_s=3600,
        icu_raw_id=0,
    )
    db.insert(
        "session_matches",
        person_id=person,
        day=TODAY.isoformat(),
        icu_id="ride-1",
        status="matched",
        minutes=60,
    )
    result = _read(db, person)
    assert set(result) == {"from", "to", "weeks", "week", "cards", "days", "sync", "charts"}
    assert result["cards"]["weight"]["weigh_ins"] == 30
    assert result["cards"]["steps"] == {"avg": 9000, "floor": 8000, "days_over": 7, "days_read": 7}
    assert result["cards"]["sleep"] == {"avg_h": 7.0, "days_read": 7}
    assert result["cards"]["training"] == {"done": 1, "planned": 1, "minutes": 60}
    assert result["cards"]["plan"]["days_planned"] == 7
    assert result["days"][0]["recorded_kcal"] == 1850
    assert result["days"][0]["planned_kcal"] == 1900
    assert result["days"][1]["future"] is True
    assert result["days"][1]["steps"] is None
    assert result["charts"]["training"]["minutes_by_kind"]["ride"][-1] == 60
    assert result["cards"]["food"]["dishes"] == 1


def test_an_unanswered_close_never_turns_the_plan_into_recorded_intake(
    fixture_db: Db, two_persons: dict
) -> None:
    db, person = fixture_db, two_persons["owner"]
    _menu(db, person, MONDAY)
    db.insert(
        "day_confirmations",
        person_id=person,
        day=TODAY.isoformat(),
        status="unconfirmed",
        basis="menu",
        kcal=1900,
    )
    result = _read(db, person)
    assert result["days"][0]["recorded"] == "not answered"
    assert result["days"][0]["recorded_kcal"] is None
    assert result["cards"]["recorded"] == {
        "days": 0,
        "of": 1,
        "avg_kcal": None,
        "target_kcal": result["cards"]["recorded"]["target_kcal"],
    }


def test_a_zero_watch_reading_is_missing_and_each_day_has_its_own_floor(
    fixture_db: Db, two_persons: dict
) -> None:
    db, person = fixture_db, two_persons["owner"]
    _profile(db, person, "2026-01-01", 7000)
    _profile(db, person, "2026-09-04", 10000)
    past = dt.date(2026, 9, 3)
    _wellness(db, person, past, 8000)
    _wellness(db, person, dt.date(2026, 9, 4), 0)
    _wellness(db, person, TODAY, 11000)
    result = _read(db, person, day=past)
    shown = {d["day"]: d for d in result["days"]}
    assert shown[past.isoformat()]["steps_floor"] == 7000
    assert shown["2026-09-04"]["steps"] is None
    assert result["cards"]["steps"]["floor"] == 10000
    assert result["cards"]["steps"]["days_read"] == 2
    assert result["cards"]["steps"]["avg"] is None


def test_training_minutes_are_grouped_by_kind_and_iso_week(
    fixture_db: Db, two_persons: dict
) -> None:
    db, person = fixture_db, two_persons["owner"]
    for day, kind, seconds, activity_id in (
        (dt.date(2026, 8, 31), "ride", 1800, "ride-1"),
        (dt.date(2026, 9, 1), "ride", 1800, "ride-2"),
        (dt.date(2026, 9, 2), "climb", 3600, "climb-1"),
        (TODAY, "ride", 4200, "ride-3"),
    ):
        db.insert(
            "icu_activity",
            person_id=person,
            icu_id=activity_id,
            day=day.isoformat(),
            kind=kind,
            name=kind,
            moving_s=seconds,
            icu_raw_id=0,
        )
    chart = _read(db, person)["charts"]["training"]
    last_week = chart["weeks"].index("2026-08-31")
    this_week = chart["weeks"].index("2026-09-07")
    assert chart["minutes_by_kind"]["ride"][last_week] == 60
    assert chart["minutes_by_kind"]["climb"][last_week] == 60
    assert chart["minutes_by_kind"]["ride"][this_week] == 70


def test_an_accepted_estimate_can_supply_a_record_without_confirming_the_day(
    fixture_db: Db, two_persons: dict
) -> None:
    db, person = fixture_db, two_persons["owner"]
    db.insert(
        "estimates",
        person_id=person,
        day=TODAY.isoformat(),
        description="Lunch",
        kcal=550,
        status="accepted",
    )
    result = _read(db, person)
    assert result["days"][0]["recorded_kcal"] == 550
    assert result["cards"]["recorded"] == {
        "days": 0,
        "of": 1,
        "avg_kcal": None,
        "target_kcal": result["cards"]["recorded"]["target_kcal"],
    }


@pytest.mark.parametrize(  # type: ignore[attr-defined]
    "slope,expected",
    [(-0.2, "about"), (-0.001, "more than two years"), (0.2, "not moving towards")],
)
def test_weight_caption_says_when_the_goal_is_reached_without_iso_dates(
    fixture_db: Db, two_persons: dict, slope: float, expected: str
) -> None:
    person = two_persons["owner"]
    _profile(fixture_db, person, "2026-01-01", 8000, 85)
    _weight(fixture_db, person, 50, slope=slope)
    result = _read(fixture_db, person)
    caption = result["charts"]["weight"]["caption"]
    assert expected in caption
    assert re.search(r"\d{4}-\d{2}-\d{2}", caption) is None
    for chart in result["charts"].values():
        if chart:
            assert re.search(r"\d{4}-\d{2}-\d{2}", chart["caption"]) is None
    if slope == -0.001:
        assert trend(fixture_db, person, "weight", 56, TODAY)["eta"] is None


def test_wide_intake_average_uses_only_confirmed_days(fixture_db: Db, two_persons: dict) -> None:
    db, person = fixture_db, two_persons["owner"]
    db.insert(
        "targets",
        person_id=person,
        week_start="2026-08-31",
        kcal=2000,
        protein_g=180,
        fat_g=70,
        carb_g=200,
        fibre_g=30,
        source="formula",
    )
    for day, status, kcal in (
        ("2026-09-01", "confirmed", 1800),
        ("2026-09-02", "unconfirmed", 9000),
    ):
        db.insert(
            "day_confirmations", person_id=person, day=day, status=status, basis="menu", kcal=kcal
        )
    result = trend(db, person, "intake", 190, TODAY)
    assert result["mean"] == 1800
    assert result["gap"] == 200
    assert "1 days with a record" in result["summary"]
    assert result["status"] == ["confirmed", "unconfirmed"]


def test_other_persons_rows_never_enter_the_response(fixture_db: Db, two_persons: dict) -> None:
    db, partner = fixture_db, two_persons["partner"]
    _profile(db, partner, "2026-01-01", 8000, 70)
    partner_recipe = _menu(db, partner, MONDAY, name="Her private dish")
    db.insert("ratings", person_id=partner, recipe_id=partner_recipe, score=5)
    _wellness(db, partner, TODAY, 12000)
    _weight(db, partner, 10, slope=-0.1)
    own = _read(db, two_persons["owner"])
    assert all(
        own["cards"][key] is None
        for key in ("weight", "steps", "sleep", "recovery", "plan", "food")
    )
    assert "Her private dish" not in str(own)


def test_food_uses_only_the_viewers_latest_ratings_for_in_force_dishes(
    fixture_db: Db, two_persons: dict
) -> None:
    db, person = fixture_db, two_persons["owner"]
    recipe = _menu(db, person, MONDAY)
    db.insert("ratings", person_id=person, recipe_id=recipe, score=2)
    db.insert("ratings", person_id=person, recipe_id=recipe, score=4)
    db.insert("ratings", person_id=two_persons["partner"], recipe_id=recipe, score=5)
    assert _read(db, person)["cards"]["food"] == {
        "dishes": 1,
        "rated": 1,
        "top": [{"name": "Porridge", "score": 4}],
    }


def test_a_rating_of_last_weeks_row_of_the_same_dish_still_counts(
    fixture_db: Db, two_persons: dict
) -> None:
    """A repeated menu writes a fresh recipes row each time (food/batch.py): a rating made against
    last week's Porridge is the rating of this week's Porridge, as the recipe book reads it."""
    db, person = fixture_db, two_persons["owner"]
    _menu(db, person, MONDAY)
    older = db.insert(
        "recipes", name="Porridge", servings=1, prep_minutes=5, method_json="[]", source="manual"
    )
    db.insert("ratings", person_id=person, recipe_id=older, score=5)
    food = _read(db, person)["cards"]["food"]
    assert food["rated"] == 1 and food["top"] == [{"name": "Porridge", "score": 5}]


def test_the_chart_captions_say_the_numbers_not_what_a_chart_is(
    fixture_db: Db, two_persons: dict
) -> None:
    db, person = fixture_db, two_persons["owner"]
    _menu(db, person, MONDAY)
    plan = _read(db, person)["charts"]["plan"]
    assert plan["caption"].startswith("This week: planned 1,900 kcal a day on average"), plan


def test_progress_route_checks_window_and_week_and_uses_request_identity(
    config: Config, tmp_path: Path
) -> None:
    clock = FrozenClock(dt.datetime(2026, 9, 7, 12, tzinfo=TZ))
    config.paths.ensure()
    db = Db.open(config.paths.db, clock=clock)
    try:
        owner_id = db.insert("persons", slug="owner", display_name="Alex", tone="neutral")
        partner_id = db.insert("persons", slug="partner", display_name="Her", tone="neutral")
        db.insert("tailscale_identities", person_id=owner_id, login="alex@example.com")
        db.insert("tailscale_identities", person_id=partner_id, login="sam@example.com")
        _weight(db, partner_id, 5, slope=-0.1)
        secrets_dir = tmp_path / "secrets"
        secrets_dir.mkdir()
        (secrets_dir / "telegram-token-dev.txt").write_text("1:t", encoding="utf-8")
        config.data["web"]["secure_cookies"] = False
        config.data["web"]["root_path"] = ""
        client = TestClient(
            create_app(App(config, Secrets(secrets_dir), clock=clock, transport=FakeTelegram())),
            base_url="http://testserver",
        )
        header = {"Tailscale-User-Login": "alex@example.com"}
        assert client.get("/api/progress", headers=header).json()["cards"]["weight"] is None
        for query in (
            "weeks=3",
            "weeks=abc",
            "week=2026-09-14",
            "week=20260907",
            "week=2026-09-99",
        ):
            assert client.get(f"/api/progress?{query}", headers=header).status_code == 422
        assert (
            client.get("/api/progress?weeks=4&week=2026-09-10", headers=header).status_code == 200
        )
    finally:
        db.close()


def test_progress_script_uses_safe_dom_and_phone_safe_plots() -> None:
    source = (Path(__file__).resolve().parents[1] / "src/tiffin/web/static/progress.js").read_text(
        encoding="utf-8"
    )
    assert "async function renderProgress()" in source
    assert "#progress-body" in source
    assert "innerHTML" not in source
    assert "dragmode: false" in source
