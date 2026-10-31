"""Stories about the jobs the box runs when nobody is watching.

Each one is a scheduled run that goes slightly wrong: a slot claimed twice, a process killed
half way through, an hour that does not exist, an hour that happens twice, a person on holiday,
an integration that has stopped answering. The rule they all test is the house one - every run
writes a `run_ledger` row saying what it did, a failure reaches the owner, and nobody is left with
silence where a message should have been.

The stories drive the real jobs through the real `Scheduler`, so what is asserted is what the
box would actually do at 03:30 on a Sunday.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from tiffin import body, doctor
from tiffin import scheduler as sc
from tiffin import targets as targets_mod
from tiffin.app import App
from tiffin.clock import TZ, FrozenClock, isoday
from tiffin.db import Db
from tiffin.errors import (
    IcuError,
    IcuRateLimited,
    ModelUnavailable,
)
from tiffin.integrations import icu
from tiffin.integrations.chrome_job import ChromeUnavailable
from tiffin.integrations.retailers import claude_chrome
from tiffin.jobs import (
    backup,
    day_close,
    evening_confirm,
    icu_sync,
    menu_propose,
    morning_targets,
    shop_refresh,
    sunday_summary,
)
from tiffin.secrets import Secrets

from .fakes import FakeTelegram, read_notices

OWNER_CHAT = 1000001
MONDAY = dt.date(2026, 9, 7)  # the frozen day
SUNDAY = dt.date(2026, 9, 13)
SATURDAY = dt.date(2026, 9, 12)


class Box:
    """One tiffin, wired to fakes: the app, its database, and a way to run any job's slot."""

    def __init__(self, app: App, db: Db, tg: FakeTelegram, clock: FrozenClock, holder: dict):
        self.app = app
        self.db = db
        self.tg = tg
        self.clock = clock
        self.holder = holder
        self.owner = 0
        self.partner = 0

    # -- setting the world up -------------------------------------------------------
    def profile(self, person_id: int, **over: Any) -> None:
        row: dict[str, Any] = {
            "person_id": person_id,
            "effective_from": "2026-09-01",
            "sex": "m",
            "birth_date": "1990-03-15",
            "height_cm": 183.0,
            "activity_factor": 1.55,
            "kcal_floor": 1800,
        }
        row.update(over)
        self.db.insert("profiles", **row)

    def use_gateway(self, gateway: Any) -> None:
        self.holder["gateway"] = gateway

    def config_set(self, dotted: str, value: Any) -> None:
        node = self.app.config.data
        parts = dotted.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value

    # -- running a slot -------------------------------------------------------------
    def slots(self, job: sc.Job, day: dt.date, person_id: int | None = None) -> list[sc.Slot]:
        if job.scope != "each":
            return sc.slots_for(job, sc.SYSTEM, None, day, self.db)
        pid = self.owner if person_id is None else person_id
        return sc.slots_for(job, pid, targets_mod.profile_on(self.db, pid, day), day, self.db)

    def scheduler(self, *jobs: sc.Job) -> sc.Scheduler:
        return sc.Scheduler(self.app, list(jobs), lambda _p, _fn: None)

    def run(
        self, job: sc.Job, day: dt.date, *, person_id: int | None = None, index: int = 0
    ) -> str:
        slots = self.slots(job, day, person_id)
        assert slots, f"{job.name} declares no slot on {day}"
        return self.scheduler(job).execute(self.db, slots[index])

    def run_again(
        self, job: sc.Job, day: dt.date, key: str, *, person_id: int | None = None
    ) -> str:
        """The same job for the same day on a different slot key: a catch-up, or a restart."""
        slot = dataclasses.replace(self.slots(job, day, person_id)[0], key=key)
        return self.scheduler(job).execute(self.db, slot)

    # -- reading the record ---------------------------------------------------------
    def ledger_row(self, job: str, person_id: int | None = None):
        pid = self.owner if person_id is None else person_id
        row = self.db.one(
            "SELECT * FROM run_ledger WHERE job = ? AND person_id = ? ORDER BY rowid DESC LIMIT 1",
            (job, pid),
        )
        assert row is not None, f"{job} never ran for person {pid}"
        return row

    def detail(self, job: str, person_id: int | None = None) -> str:
        row = self.ledger_row(job, person_id)
        assert row is not None, f"{job} wrote no run_ledger row at all"
        return str(row["detail"] or "")

    def alerts(self) -> list[str]:
        """Ops notices for the page's status strip, as "event: text" - never a chat message."""
        sent = self.db.scalar("SELECT COUNT(*) FROM messages_out WHERE kind = 'admin_alert'")
        assert sent == 0, "an ops notice reached a chat"
        return [f"{n['event']}: {n['text']}" for n in read_notices(self.app.config.paths.state)]

    def outbound(self, kind: str, person_id: int | None = None) -> list[Any]:
        pid = self.owner if person_id is None else person_id
        return self.db.all(
            "SELECT * FROM messages_out WHERE kind = ? AND person_id = ? ORDER BY id", (kind, pid)
        )


@pytest.fixture
def box(fixture_db: Db, two_persons, frozen_clock: FrozenClock, config, tmp_path: Path):
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    (secrets_dir / "telegram-token-dev.txt").write_text("1:t", encoding="utf-8")
    (secrets_dir / "icu-key-owner.txt").write_text("k-owner", encoding="utf-8")
    config.paths.ensure()
    shutil.copyfile(fixture_db.path, config.paths.db)  # the backup job copies paths.db
    tg = FakeTelegram()
    holder: dict[str, Any] = {"gateway": None}
    app = App(
        config,
        Secrets(secrets_dir),
        clock=frozen_clock,
        transport=tg,
        gateway_factory=lambda _a, _d: holder["gateway"],
    )
    b = Box(app, fixture_db, tg, frozen_clock, holder)
    b.owner, b.partner = two_persons["owner"], two_persons["partner"]
    return b


class DeadGateway:
    """A gateway whose only model has stopped answering: the quota is gone."""

    def json_task(self, task: str, prompt: str, schema_name: str, *, person_id: int):
        raise ModelUnavailable(f"{task}: claude unavailable (quota_exhausted)")


def an_ingredient(db: Db, code: str, name: str, **over: Any) -> None:
    row: dict[str, Any] = {
        "code": code,
        "version": 1,
        "name": name,
        "kcal_100": 165.0,
        "protein_100": 31.0,
        "fat_100": 3.6,
        "carb_100": 0.0,
        "fibre_100": 0.0,
        "pack_g": 500.0,
        "pack_label": "500 g pack",
        "aisle": "meat",
        "approved": 1,
        "person_scope": "both",
    }
    row.update(over)
    db.insert("ingredients", **row)


# ---------------------------------------------------------------- the run ledger
def test_01_a_slot_dispatched_twice_closes_yesterday_once(box: Box) -> None:
    first = box.run(day_close.JOB, MONDAY)
    second = box.run(day_close.JOB, MONDAY)
    assert (first, second) == ("ok", "already")
    rows = box.db.all("SELECT day FROM day_confirmations WHERE person_id = ?", (box.owner,))
    assert [str(r["day"]) for r in rows] == ["2026-09-06"], "one close for the day, not two"
    assert box.db.scalar("SELECT COUNT(*) FROM run_ledger WHERE job = 'day_close'") == 1


def test_02_a_sync_that_dies_half_way_keeps_what_landed_and_tells_owner(
    box: Box, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Intervals.icu can reject the key part way through a pull, after some days have already
    been written. The rows that landed must stay - they are real - and the run must be `failed`
    rather than silently `ok`, because a half pull leaves the steps readout and session match
    reading a day that is not finished."""

    def half_way(db: Db, person, key: str, **_k: Any):
        db.insert("icu_wellness", person_id=person.id, day="2026-09-06", steps=9000, icu_raw_id=1)
        raise IcuError("403 key rejected")

    monkeypatch.setattr(icu, "sync_person", half_way)
    assert box.run(icu_sync.JOB, MONDAY, index=20) == "failed"
    row = box.ledger_row("icu_sync")
    assert row is not None and row["status"] == "failed"
    assert "IcuError" in str(row["error"])
    assert box.db.scalar("SELECT steps FROM icu_wellness WHERE day = '2026-09-06'") == 9000
    assert any("icu_sync" in a for a in box.alerts()), "a failed job leaves a notice"
    check = doctor._icu_check(box.db, box.app.registry(box.db).active())
    assert check.status == "red" and "intervals.icu" in check.detail


def test_03_a_run_left_running_by_a_killed_process_is_dispatched_again(box: Box) -> None:
    """A supervisor can stop a hung tiffin and the row it was working on says `running` for ever.
    Nothing else ever clears it, so without the staleness rule that day's job is lost with no
    alert and no record of a problem."""
    key = "2026-09-07T0005"
    box.db.execute(
        "INSERT INTO run_ledger(run_id, job, slot, person_id, started_at, status) "
        "VALUES ('r1', 'day_close', ?, ?, '2026-09-07 10:00:00', 'running')",
        (key, box.owner),
    )
    dispatched = box.scheduler(day_close.JOB).tick(box.db)
    assert (key, box.owner) in {(s.key, s.person_id) for s in dispatched}


def test_04_a_run_still_going_in_this_process_is_not_started_a_second_time(box: Box) -> None:
    key = "2026-09-07T0005"
    box.db.execute(
        "INSERT INTO run_ledger(run_id, job, slot, person_id, started_at, status) "
        "VALUES ('r1', 'day_close', ?, ?, ?, 'running')",
        (key, box.owner, box.clock.utc_stamp()),
    )
    dispatched = box.scheduler(day_close.JOB).tick(box.db)
    assert (key, box.owner) not in {(s.key, s.person_id) for s in dispatched}


def test_06_the_same_failure_every_hour_is_one_line_on_the_page_and_no_message(
    box: Box, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tiffin import notices

    def down(*_a: Any, **_k: Any):
        raise IcuError("host unreachable")

    monkeypatch.setattr(icu, "sync_person", down)
    box.run(icu_sync.JOB, MONDAY, index=18)
    box.run(icu_sync.JOB, MONDAY, index=19)
    assert len(box.alerts()) == 2, "every occurrence is recorded"
    shown = notices.recent(box.app.config.paths.state, clock=box.clock)
    assert len(shown) == 1 and "1910" in shown[0]["text"], "one line, the newest"


def test_07_two_problem_kinds_are_both_kept_on_the_page(box: Box) -> None:
    """One job's repeated failure must not hide a different job's first failure."""
    from tiffin import notices

    outbox = box.app.outbox(box.db)
    admin = box.app.registry(box.db).admin()
    outbox.alert(admin, "icu_sync", "intervals.icu unavailable")
    outbox.alert(admin, "shop_refresh", "shop browser unavailable")
    shown = notices.recent(box.app.config.paths.state, clock=box.clock)
    assert {item["event"] for item in shown} == {"icu_sync", "shop_refresh"}


# ---------------------------------------------------------------- the clock
def test_08_a_brief_set_for_the_spring_forward_gap_is_skipped_not_moved(box: Box) -> None:
    """01:30 does not exist on 29 March 2026. Running it an hour early or an hour late would
    both be wrong, so the slot is dropped - and the days either side must still have one."""
    box.profile(
        box.owner,
        effective_from="2026-01-01",
        morning_time="01:30",
        quiet_start="00:00",
        quiet_end="00:00",
    )
    assert box.slots(morning_targets.JOB, dt.date(2026, 3, 29)) == []
    before = box.slots(morning_targets.JOB, dt.date(2026, 3, 28))
    assert [s.due_local.hour for s in before] == [1]
    assert box.db.scalar("SELECT COUNT(*) FROM run_ledger") == 0


def test_09_a_daily_job_in_the_repeated_autumn_hour_runs_once(box: Box) -> None:
    """
    reads the clock rather than the slot key would close the previous day twice."""
    job = dataclasses.replace(day_close.JOB, times=sc.fixed("01:30"))
    day = dt.date(2026, 10, 25)
    slots = box.slots(job, day)
    assert len(slots) == 1 and slots[0].due_local.fold == 0

    box.clock.set_local(dt.datetime(2026, 10, 25, 1, 30, tzinfo=TZ, fold=0))
    first_utc = box.clock.now_utc()
    assert box.scheduler(job).execute(box.db, slots[0]) == "ok"
    box.clock.set_local(dt.datetime(2026, 10, 25, 1, 30, tzinfo=TZ, fold=1))
    assert box.clock.now_utc() - first_utc == dt.timedelta(hours=1), "two real instants"
    assert box.scheduler(job).execute(box.db, slots[0]) == "already"
    assert box.db.scalar("SELECT COUNT(*) FROM day_confirmations") == 1


def test_10_a_brief_set_inside_quiet_hours_waits_for_the_window_to_end(box: Box) -> None:
    box.profile(box.owner, morning_time="06:00", quiet_start="22:30", quiet_end="07:00")
    body.record_weigh_in(box.db, box.owner, dt.date(2026, 9, 6), 84.0, "manual")
    slot = box.slots(morning_targets.JOB, MONDAY)[0]
    assert slot.due_local == dt.datetime(2026, 9, 7, 7, 0, tzinfo=TZ), "held, not sent at 06:00"
    assert slot.deadline is not None and slot.due_local < slot.deadline, "and still catchable"
    assert box.scheduler(morning_targets.JOB).execute(box.db, slot) == "ok"
    assert [str(r["status"]) for r in box.outbound("morning_targets")] == ["sent"]


def test_12_the_evening_check_in_is_sent_inside_quiet_because_the_person_chose_the_time(
    box: Box,
) -> None:
    """Confirm_time is by definition a time they picked. Deferring it to quiet_end moved the
    check-in to the next morning, past its own 23:30 deadline, so it never arrived at all and
    every day landed unconfirmed."""
    box.profile(box.owner, confirm_time="23:00", quiet_start="22:30", quiet_end="07:00")
    slot = box.slots(evening_confirm.JOB, MONDAY)[0]
    assert slot.due_local == dt.datetime(2026, 9, 7, 23, 0, tzinfo=TZ)
    assert box.scheduler(evening_confirm.JOB).execute(box.db, slot) == "ok"
    assert [str(r["status"]) for r in box.outbound("evening_confirm")] == ["sent"]


def test_13_a_person_on_holiday_gets_no_check_in_but_the_day_still_closes(box: Box) -> None:
    box.profile(box.owner)
    box.db.insert(
        "pause_periods", person_id=box.owner, start_day="2026-09-01", end_day="2026-09-30"
    )
    assert box.run(evening_confirm.JOB, MONDAY) == "skipped"
    assert [str(r["status"]) for r in box.outbound("evening_confirm")] == ["dropped_paused"]
    assert box.run(day_close.JOB, MONDAY) == "ok"
    row = box.db.one("SELECT status FROM day_confirmations WHERE person_id = ?", (box.owner,))
    assert row is not None and str(row["status"]) == "paused", "a paused day is not a missed one"


def test_14_the_morning_after_onboarding_says_it_needs_a_weight_then_sends(box: Box) -> None:
    """Onboarding writes the profile but no weigh-in: the scale is the source and the targets
    cannot be worked out without it. The first morning must skip with a reason rather than
    invent a number, and the next one must send once the scale has been used."""
    box.profile(box.owner, effective_from=isoday(MONDAY), morning_time="07:30")
    assert box.run(morning_targets.JOB, MONDAY) == "skipped"
    assert box.detail("morning_targets") == "no weight yet"
    assert box.outbound("morning_targets") == []

    body.record_weigh_in(box.db, box.owner, MONDAY, 84.0, "manual")
    box.clock.set_local(dt.datetime(2026, 9, 8, 7, 30, tzinfo=TZ))
    assert box.run(morning_targets.JOB, dt.date(2026, 9, 8)) == "ok"
    assert [str(r["status"]) for r in box.outbound("morning_targets")] == ["sent"]


# ---------------------------------------------------------------- two people
def test_15_one_job_two_people_two_rows_and_neither_text_names_the_other(box: Box) -> None:
    for pid, weight in ((box.owner, 84.0), (box.partner, 61.0)):
        box.profile(pid, morning_time="07:30")
        body.record_weigh_in(box.db, pid, dt.date(2026, 9, 6), weight, "manual")
    box.clock.set_local(dt.datetime(2026, 9, 7, 8, 0, tzinfo=TZ))
    for pid in (box.owner, box.partner):
        assert box.run(morning_targets.JOB, MONDAY, person_id=pid) == "ok"
    rows = box.db.all("SELECT person_id, slot FROM run_ledger WHERE job = 'morning_targets'")
    assert sorted(int(r["person_id"]) for r in rows) == sorted([box.owner, box.partner])
    assert len({str(r["slot"]) for r in rows}) == 1, "the same slot key, one row each"
    his = str(box.outbound("morning_targets", box.owner)[0]["text_html"])
    partners = box.outbound("morning_targets", box.partner)
    assert "61" not in his, "her weight is not in his brief"
    assert [str(r["status"]) for r in partners] == ["dropped_dev"], "development never messages her"


def test_16_the_sunday_summary_carries_partner_trend_but_never_partner_missed_days(
    box: Box,
) -> None:
    """Each person sees the other's trend and sessions and nothing else. A partner
    block that leaked "3 unconfirmed" would turn a weekly review into a scoreboard."""
    for pid, weight in ((box.owner, 84.0), (box.partner, 61.0)):
        box.profile(pid)
        body.record_weigh_in(box.db, pid, dt.date(2026, 9, 8), weight, "manual")
    for i in range(3):
        box.db.insert(
            "day_confirmations",
            person_id=box.partner,
            day=isoday(dt.date(2026, 9, 8) + dt.timedelta(days=i)),
            status="unconfirmed",
            basis="none",
        )
    box.clock.set_local(dt.datetime(2026, 9, 13, 19, 0, tzinfo=TZ))
    assert box.run(sunday_summary.JOB, SUNDAY) == "ok"
    his = str(box.outbound("sunday_summary", box.owner)[0]["text_html"])
    partner = [line for line in his.splitlines() if line.startswith("Her:")]
    assert partner, f"her line is missing altogether: {his}"
    assert "not answered" not in partner[0], partner[0]
    assert len([line for line in his.splitlines() if line.startswith("Days recorded:")]) == 1


# ---------------------------------------------------------------- degradation
def test_18_a_rate_limited_icu_pull_degrades_without_waking_owner(
    box: Box, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 429 is normal weather, not a failure: the next hour tries again. Alerting on it would
    train the owner to ignore the one channel that tells him something is really wrong."""

    def rate_limited(*_a: Any, **_k: Any):
        raise IcuRateLimited("429 for 900 s")

    monkeypatch.setattr(icu, "sync_person", rate_limited)
    assert box.run(icu_sync.JOB, MONDAY, index=20) == "degraded"
    assert "next hour retries" in box.detail("icu_sync")
    assert box.alerts() == []


def test_21b_a_shop_refresh_that_reads_the_page_and_stores_nothing_says_so(
    box: Box, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A shop read that stores no products reports a degraded run."""
    an_ingredient(box.db, "chicken", "Chicken breast, raw")
    an_ingredient(box.db, "oats", "Porridge oats", aisle="grains")

    class WrongPage:
        def __init__(self, *_a: Any, **_k: Any) -> None: ...

        def run_json(self, prompt: str):
            # the shape a real session returns when the search showed it nothing to open
            return {
                "products": [],
                "not_found": [{"code": "oats", "reason": "nothing in the results to open"}],
            }, {}

    monkeypatch.setattr(claude_chrome, "ChromeJob", WrongPage)
    assert box.run(shop_refresh.JOB, SUNDAY) == "degraded", "an empty read is not an ok run"
    detail = box.detail("shop_refresh", sc.SYSTEM)
    assert "nothing stored" in detail and "search URL" in detail, detail
    assert shop_refresh.default_retailer(box.app.config) in detail, detail
    assert box.db.scalar("SELECT COUNT(*) FROM products") == 0


def test_21d_a_cooked_label_is_stored_for_its_pack_and_price(
    box: Box, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sainsbury's publishes fresh chicken breast "cooked as per instructions".

    137 kcal against a catalogue holding the food raw at 106 is not a number to store. The
    PRODUCT still is: the shopping list wants the pack size and the price, and it never wanted
    the shop's macros. Refusing the whole line lost a real product over a figure nobody was
    others are fine" - and he was right, it is Sainsbury's own page, not the food).
    """
    an_ingredient(box.db, "chicken", "Chicken breast, raw")

    class CookedLabel:
        def __init__(self, *_a: Any, **_k: Any) -> None: ...

        def run_json(self, prompt: str):
            return {
                "products": [
                    {
                        "code": "chicken",
                        "retailer_sku": "7874927",
                        "name": "Sainsbury's 640g Chicken Breast Fillets",
                        "pack_g": 640,
                        "price_pence": 462,
                        "kcal_100": None,
                        "protein_100": None,
                        "nutrition_basis": "cooked as per instructions",
                    }
                ],
                "not_found": [],
            }, {}

    monkeypatch.setattr(claude_chrome, "ChromeJob", CookedLabel)
    assert box.run(shop_refresh.JOB, SUNDAY) == "ok", "a usable product is not a failed run"
    row = box.db.one("SELECT * FROM products WHERE ingredient_code = 'chicken'")
    assert row is not None, "the product was thrown away over its nutrition basis"
    assert row["pack_g"] == 640.0 and row["price_pence"] == 462
    assert row["kcal_100"] is None, "the cooked figure must not read as the raw food's"
    assert row["nutrition_basis"] == "cooked as per instructions", dict(row)
    assert "pack and price only" in box.detail("shop_refresh", sc.SYSTEM), (
        "the run line cannot tell a complete read from a price-only one"
    )


def test_21e_the_weekly_read_covers_every_shop_the_list_may_name(
    box: Box, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sunday 05:00. `shop.retailers` is Sainsbury's then M&S, and the shopping list prefers a
    product from either - so both have to be kept current.

    The scheduled job called `run` with no retailer, and `run` reads one: the head of the list,
    every week, for as long as the box has been up. M&S has been in `shop.retailers` since the
    move and was refreshed automatically exactly never; every M&S price on the list came from
    somebody running `shop-backfill` by hand. A list that prefers a shop and never
    reads it is preferring a shop it knows nothing current about.
    """
    an_ingredient(box.db, "chicken", "Chicken breast, raw")
    an_ingredient(box.db, "oats", "Porridge oats", aisle="grains")
    asked: list[str] = []

    class Reader:
        def __init__(self, *_a: Any, **_k: Any) -> None: ...

        def run_json(self, prompt: str):
            import json as _json

            asked.append(_json.loads(prompt)["retailer"])
            return {"products": [], "not_found": []}, {}

    monkeypatch.setattr(claude_chrome, "ChromeJob", Reader)
    box.run(shop_refresh.JOB, SUNDAY)
    wanted = shop_refresh.retailer_order(box.app.config)
    assert len(wanted) > 1, "setup: this box shops at more than one place"
    assert asked == wanted, f"the weekly read asked {asked}; the list is allowed to name {wanted}"


def test_21c_a_batch_of_honest_refusals_is_not_an_alarm(
    box: Box, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Six foods read and six judged unusable is an ordinary Sainsbury's afternoon.

    The shop publishes cooked nutrition for rice, pasta and chicken breast while the catalogue
    holds raw, and refusing to store one as the other is the reader doing its job. Story 21b
    exists to catch a search that answers with the wrong page; if it also fires on a batch like
    this one, it trains the one person who gets the message to ignore it.
    """
    for code, name in (("chicken", "Chicken breast, raw"), ("rice", "Basmati rice, dry")):
        an_ingredient(box.db, code, name)

    class Refusals:
        def __init__(self, *_a: Any, **_k: Any) -> None: ...

        def run_json(self, prompt: str):
            import json as _json

            asked = [f["code"] for f in _json.loads(prompt)["foods"]]
            return {
                "products": [],
                "not_found": [
                    {
                        "code": code,
                        "kind": "unstocked",
                        "reason": "page shows cooked nutrition only; raw per-100g not available",
                    }
                    for code in asked
                ],
            }, {}

    monkeypatch.setattr(claude_chrome, "ChromeJob", Refusals)
    assert box.run(shop_refresh.JOB, SUNDAY) == "ok", (
        "every food was read and answered for; that is a result, not a fault"
    )
    assert not [a for a in box.alerts() if "search URL" in a], box.alerts()
    kinds = {r["kind"] for r in box.db.all("SELECT kind FROM shop_misses")}
    assert kinds == {"unstocked"}, kinds


def test_21_a_shop_refresh_with_no_browser_degrades_and_writes_no_prices(
    box: Box, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Claude-in-Chrome needs a browser that is already running, and on a box that rebooted to
    a lock screen there is none. Nothing may be invented: a missing price is better than one
    the shopping list would then round packs against."""
    an_ingredient(box.db, "chicken", "Chicken breast, raw")
    an_ingredient(box.db, "oats", "Porridge oats", aisle="grains")

    class NoBrowser:
        def __init__(self, *_a: Any, **_k: Any) -> None: ...

        def run_json(self, prompt: str):
            raise ChromeUnavailable("no Chrome is running on this profile")

    monkeypatch.setattr(claude_chrome, "ChromeJob", NoBrowser)
    assert box.run(shop_refresh.JOB, SUNDAY) == "degraded"
    detail = box.detail("shop_refresh", sc.SYSTEM)
    # the shop is whichever one config puts nearest, not a hard-coded Tesco: these two shop at
    # Sainsbury's and M&S, and a failure has to name the shop it actually tried
    assert shop_refresh.default_retailer(box.app.config) in detail and "Chrome" in detail
    assert box.db.scalar("SELECT COUNT(*) FROM products") == 0


def test_22_a_menu_night_with_no_model_tells_the_person_what_actually_went_wrong(box: Box) -> None:
    """Saturday 18:00 is when the week's menu is proposed, and the only thing that can answer
    is a model. When the subscription quota is spent, the person
    must be told that - not that their own targets are impossible, which sends them to
    `/set rate` to loosen a week that was never the problem.

    The ledger gets it right; the sentence on their phone does not, because the gateway's
    ModelUnavailable is a ModelError and the reprompt branch catches it before the
    "could not reach a model" branch can.
    """
    box.profile(box.owner, segment_days=json.dumps([[0, 1, 2, 3, 4]]), meals_per_day=4)
    box.db.insert(
        "targets",
        person_id=box.owner,
        week_start="2026-09-14",
        kcal=2250,
        protein_g=200,
        fat_g=70,
        carb_g=200,
        fibre_g=30,
        source="formula",
    )
    an_ingredient(box.db, "chicken", "Chicken breast, raw")
    an_ingredient(box.db, "oats", "Porridge oats", aisle="grains")
    box.use_gateway(DeadGateway())
    box.clock.set_local(dt.datetime(2026, 9, 12, 18, 0, tzinfo=TZ))

    assert box.run(menu_propose.JOB, SATURDAY) == "degraded"
    detail = box.detail("menu_propose")
    assert "claude unavailable (quota_exhausted)" in detail
    said = str(box.outbound("menu_failed")[0]["text_html"])
    assert "model" in said.lower(), (
        f"a model outage reported as a targets failure: {said!r}; the ledger knew: {detail!r}"
    )


# ---------------------------------------------------------------- the backup
def test_23_a_backup_with_nowhere_to_write_says_so_three_ways(
    box: Box, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The health report reads backup_last.json and knows nothing about run_ledger, so
    a backup that could not run has to land in the file as well as the ledger. Degrading rather
    than raising is deliberate: the file is already the alarm."""
    from tiffin import backup as backup_store

    monkeypatch.setattr(
        backup_store,
        "run_backup",
        lambda *a, **k: (_ for _ in ()).throw(OSError("no space on device")),
    )
    assert box.run(backup.JOB, MONDAY) == "degraded"
    assert "OSError" in box.detail("backup", sc.SYSTEM)
    last = json.loads((box.app.config.paths.state / "backup_last.json").read_text(encoding="utf-8"))
    assert last["ok"] is False and "no space" in last["error"]
    assert any("backup" in a for a in box.alerts())


def test_24_a_backup_run_twice_for_one_day_leaves_one_copy(box: Box) -> None:
    """Repeated in-process backups use one manifest format and directory on
    purpose. A catch-up after a restart must replace the day's copy, never grow a second one
    that the manifest does not describe."""
    assert box.run(backup.JOB, MONDAY) == "ok"
    assert box.run_again(backup.JOB, MONDAY, "2026-09-07T0415") == "ok"
    out = box.app.config.paths.backups / isoday(MONDAY)
    assert sorted(p.name for p in out.iterdir()) == ["manifest.json", "tiffin.db", "tiffin.sql"]
    assert box.db.scalar("SELECT COUNT(*) FROM run_ledger WHERE job = 'backup'") == 2
    last = json.loads((box.app.config.paths.state / "backup_last.json").read_text(encoding="utf-8"))
    assert last["ok"] is True


def test_backup_uses_configured_retention(box: Box) -> None:
    box.app.config.data["ops"]["backup_keep_days"] = 1
    box.app.config.data["ops"]["log_keep_days"] = 1
    old = box.app.config.paths.backups / "2026-09-05"
    old.mkdir(parents=True)
    log_dir = box.app.config.paths.logs
    log_dir.mkdir(parents=True, exist_ok=True)
    old_log = log_dir / "tiffin-2026-09-05.jsonl"
    old_log.write_text("", encoding="utf-8")
    unrelated = log_dir / "tiffin-0000-00-00.jsonl"
    unrelated.write_text("", encoding="utf-8")

    assert box.run(backup.JOB, MONDAY) == "ok"
    assert not old.exists() and not old_log.exists() and unrelated.exists()


# ---------------------------------------------------------------- sending twice on purpose
