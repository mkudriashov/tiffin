"""Stories on the surfaces that decide what Tiffin advises, and what it reads from outside.

The advisor turns a week of rows into candidates, the guardrails decide which of those may be
applied without asking, and the prose turns them into six lines. Everything here is about the
moment Tiffin stops reporting and starts recommending, which is the moment a wrong answer
costs something. The reading integrations are here too: a wrong file sent as a plan is an
ordinary day, not an exception.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import subprocess

import pytest

from tiffin import dryrun
from tiffin.advisor import facts as facts_mod
from tiffin.advisor import prose, rules
from tiffin.app import App
from tiffin.clock import TZ, FrozenClock
from tiffin.db import Db
from tiffin.energy import Macros
from tiffin.guardrails import (
    AUTO_KCAL,
    MAX_LOSS_KG_WEEK,
    Applied,
    NeedsProposal,
    apply_guardrails,
)
from tiffin.integrations import chrome_job
from tiffin.model import tools
from tiffin.secrets import Secrets
from tiffin.telegram import intents
from tiffin.telegram import render as r

FROZEN = dt.datetime(2026, 9, 12, 18, 0, tzinfo=TZ)
WEEK = dt.date(2026, 9, 7)  # a Monday
SUNDAY = dt.date(2026, 9, 13)


def a_profile(db: Db, pid: int, **over: object) -> int:
    base: dict[str, object] = {
        "person_id": pid,
        "effective_from": "2026-08-01",
        "sex": "m",
        "birth_date": "1990-03-15",
        "height_cm": 183.0,
        "activity_factor": 1.55,
        "kcal_floor": 1800,
    }
    base.update(over)
    return db.insert("profiles", **base)


def some_facts(**over: object) -> facts_mod.Facts:
    f = facts_mod.Facts(1, WEEK, SUNDAY)
    f.targets = Macros(2250, 200, 70, 200, 30)
    f.weight_kg = 84.0
    for k, v in over.items():
        setattr(f, k, v)
    return f


def rules_fired(f: facts_mod.Facts) -> set[str]:
    return {c.rule for c in rules.candidates(f)}


class Reply:
    def __init__(self, lines: list[str]) -> None:
        self.data = {"lines": lines}
        self.model_used = "fake"


class ProseGateway:
    """A model that answers, but not necessarily within the house rules."""

    def __init__(self, lines: list[str]) -> None:
        self.lines = lines
        self.calls = 0

    def json_task(self, task: str, prompt: str, schema_name: str, *, person_id: int) -> Reply:
        self.calls += 1
        return Reply(self.lines)


class Broken:
    def json_task(self, *a: object, **k: object) -> object:
        raise RuntimeError("the model is down")


# -- the safety rule that must never wait for a tap --------------------------------------------


def test_01_losing_too_fast_is_corrected_now_not_when_the_person_next_taps() -> None:
    """Rule 4 of the guardrails is the only one written down as "always applied", because it is
    the one that exists to stop someone losing weight dangerously fast. If the calibration also
    moved the target that week, the combined change can exceed the auto bound and the whole
    verdict becomes a proposal: the caller then carries the OLD targets forward and the person
    keeps under-eating until they happen to tap a card.
    """
    previous = Macros(2250, 200, 70, 200, 30)
    proposed = Macros(2300, 200, 70, 210, 30)  # calibration found a higher TDEE
    out = apply_guardrails(
        proposed,
        previous,
        weight_kg=84.0,
        kcal_floor=1800,
        trend_delta_2w=-2.4,  # 1.2 kg/week, well over the 0.8 cap
    )
    assert out.macros.kcal == 2450, "the bump lands on the macros either way"
    assert isinstance(out, Applied), (
        "a correction the guardrails mandate cannot be held behind a tap"
    )


def test_02_a_calibration_change_too_big_to_apply_still_carries_the_mandated_bump() -> None:
    previous = Macros(2250, 200, 70, 200, 30)
    proposed = Macros(2700, 200, 70, 320, 30)  # far outside the auto bound on its own
    out = apply_guardrails(proposed, previous, weight_kg=84.0, kcal_floor=1800, trend_delta_2w=-2.4)
    assert isinstance(out, NeedsProposal), "a 450 kcal jump is a decision, not an adjustment"
    assert out.apply_now is not None
    assert out.apply_now.kcal == previous.kcal + AUTO_KCAL, (
        "while the big change waits, the safety bump is already in the targets that land"
    )


def test_03_the_floors_apply_to_what_lands_even_while_a_proposal_waits() -> None:
    """The protein floor is 2 g/kg. Someone who gains weight needs more protein at once, not
    after they notice a card."""
    previous = Macros(2250, 150, 70, 200, 30)
    proposed = Macros(2900, 150, 70, 360, 30)
    out = apply_guardrails(proposed, previous, weight_kg=90.0, kcal_floor=1800)
    assert isinstance(out, NeedsProposal)
    assert out.apply_now is not None
    assert out.apply_now.protein_g >= 180, "2 g/kg of 90 kg is 180 g"


def test_04_a_change_inside_the_bounds_is_simply_applied() -> None:
    previous = Macros(2250, 200, 70, 200, 30)
    out = apply_guardrails(
        Macros(2350, 205, 70, 220, 30), previous, weight_kg=84.0, kcal_floor=1800
    )
    assert isinstance(out, Applied) and out.source == "auto_adjust"


def test_05_the_very_first_week_has_nothing_to_compare_against() -> None:
    out = apply_guardrails(
        Macros(2250, 200, 70, 200, 30), None, weight_kg=84.0, kcal_floor=1800, source="formula"
    )
    assert isinstance(out, Applied) and out.source == "formula"


def test_06_a_target_under_the_floor_is_raised_and_the_person_is_told_why() -> None:
    out = apply_guardrails(Macros(1500, 200, 70, 50, 30), None, weight_kg=84.0, kcal_floor=1800)
    assert out.macros.kcal >= 1800
    assert any("floor" in n for n in out.notes), "a silent floor is a number nobody can explain"


def test_07_carbs_never_go_negative_to_balance_the_books() -> None:
    """Protein and fat at their floors can already exceed a low calorie target. Refilling carbs
    from the remainder would make them negative and the macros would stop adding up."""
    out = apply_guardrails(Macros(1200, 300, 150, 0, 30), None, weight_kg=150.0, kcal_floor=1200)
    assert out.macros.carb_g >= 0


def test_08_the_fast_loss_bump_fires_only_above_the_cap_not_at_it() -> None:
    at_the_cap = apply_guardrails(
        Macros(2250, 200, 70, 200, 30),
        None,
        weight_kg=84.0,
        kcal_floor=1800,
        trend_delta_2w=-2 * MAX_LOSS_KG_WEEK,
    )
    assert at_the_cap.macros.kcal == 2250, "0.8 kg/week is the cap, not over it"
    over = apply_guardrails(
        Macros(2250, 200, 70, 200, 30),
        None,
        weight_kg=84.0,
        kcal_floor=1800,
        trend_delta_2w=-2 * MAX_LOSS_KG_WEEK - 0.2,
    )
    assert over.macros.kcal == 2250 + AUTO_KCAL


# -- the rules, at their edges -----------------------------------------------------------------


def test_09_a_week_with_no_numbers_at_all_produces_no_advice() -> None:
    f = facts_mod.Facts(1, WEEK, SUNDAY)
    assert rules.candidates(f) == []


def test_10_fast_loss_and_slow_loss_cannot_both_fire_in_one_week() -> None:
    f = some_facts(
        trend_delta_14=-2.4,
        trend_delta_7=-1.2,
        prev_week_delta_7=-1.2,
        usable_days=7,
        prev_week_usable_days=7,
        calibration_count=3,
    )
    fired = rules_fired(f)
    assert not ("R1" in fired and "R2" in fired), "one week cannot be both too fast and too slow"


def test_11_the_cycle_guard_stops_every_calorie_change_that_week() -> None:
    f = some_facts(cycle_phase="luteal", trend_delta_14=-2.4)
    fired = rules_fired(f)
    assert "R9" in fired and "R1" not in fired


def test_12_a_goal_date_that_has_already_passed_is_explained_in_plain_words(
    fixture_db: Db, two_persons
) -> None:
    """Dividing by the weeks remaining once the date has arrived produced "goal needs inf
    kg/week", which is not a sentence anyone can act on."""
    pid = two_persons["owner"]
    a_profile(fixture_db, pid, goal_weight_kg=78.0, goal_date="2026-09-01")
    f = some_facts(trend_now=84.0, profile=fixture_db.one("SELECT * FROM profiles"))
    notes = [c for c in rules.candidates(f) if c.rule == "R10"]
    assert notes, "a goal that cannot be met still owes the person a sentence"
    assert "inf" not in notes[0].reason and "nan" not in notes[0].reason.lower()


def test_13_every_reason_line_is_short_enough_to_read_on_a_phone() -> None:
    f = some_facts(
        trend_delta_14=-2.4,
        protein_short_days=5,
        fibre_short_days=4,
        weeks_in_deficit=9,
        steps_floor_misses=5,
        sessions_done=1,
        sessions_planned=5,
        sessions_pct_prev_week=20.0,
        profile={"nudge_time": "16:00", "goal_weight_kg": None, "goal_date": None},
    )
    for c in rules.candidates(f):
        assert len(prose.reason_line(c)) <= 120, c.rule


def test_14_no_reason_line_carries_an_emoji_or_a_tag() -> None:
    f = some_facts(trend_delta_14=-2.4, protein_short_days=5, weeks_in_deficit=9)
    for c in rules.candidates(f):
        r.check_text(prose.reason_line(c), kind="reason", max_lines=99)


def test_15_candidates_come_back_most_urgent_first() -> None:
    f = some_facts(trend_delta_14=-2.4, protein_short_days=5, weeks_in_deficit=9)
    priorities = [c.priority for c in rules.candidates(f)]
    assert priorities == sorted(priorities)


# -- what the person is actually sent ----------------------------------------------------------


def test_16_a_model_that_answers_with_an_emoji_does_not_cost_the_person_their_summary() -> None:
    """The template fallback exists so that a model failure never costs the weekly review. An
    emoji is not a failure this code notices, so the model's line is kept and then dies at the
    render check on the way out: the job fails, and the person gets nothing at all.
    """
    template = ["Trend 84.0 kg (-0.4 kg this week)", "Days recorded: 5, 2 not answered"]
    out, _used = prose.maybe_model_prose(ProseGateway(["Trend 84.0 kg \U0001f4aa"]), 1, template)
    r.check_text("\n".join(out), kind="sunday_summary", max_lines=99)


def test_17_a_model_that_answers_with_a_tag_telegram_will_reject_is_ignored_too() -> None:
    template = ["Trend 84.0 kg", "Days: 5 confirmed"]
    reply = ["<script>alert(1)</script> Trend 84.0 kg"]
    out, _used = prose.maybe_model_prose(ProseGateway(reply), 1, template)
    assert out == template


def test_18_a_model_that_is_down_leaves_the_template_exactly_as_it_was() -> None:
    template = ["Trend 84.0 kg", "Days: 5 confirmed"]
    out, used = prose.maybe_model_prose(Broken(), 1, template)
    assert out == template and used is None


def test_19_no_gateway_at_all_is_not_an_error() -> None:
    template = ["Trend 84.0 kg"]
    assert prose.maybe_model_prose(None, 1, template) == (template, None)


def test_20_the_partner_block_says_the_trend_and_never_a_miss() -> None:
    f = some_facts(
        trend_now=61.0,
        trend_delta_7=-0.3,
        sessions_done=2,
        sessions_planned=4,
        protein_short_days=6,
        unconfirmed_days=5,
        steps_floor_misses=7,
    )
    line = " ".join(prose.partner_lines(f, "Sam"))
    assert "trend" in line and "sessions" in line
    for miss in ("protein", "unconfirmed", "steps", "short"):
        assert miss not in line.lower(), f"the other person never sees a {miss}"


def test_21_a_partner_with_no_name_is_not_given_an_invented_one() -> None:
    f = some_facts(trend_now=61.0)
    assert prose.partner_lines(f, "   ") == ["Your partner: trend 61.0 kg"]


def test_22_a_partner_with_nothing_recorded_contributes_no_line() -> None:
    assert prose.partner_lines(facts_mod.Facts(2, WEEK, SUNDAY), "Sam") == []


def test_23_the_summary_block_stays_within_six_lines(fixture_db: Db, two_persons) -> None:
    f = some_facts(
        trend_now=84.0,
        trend_delta_7=-0.4,
        confirmed_days=5,
        unconfirmed_days=2,
        paused_days=0,
        intake_mean=2210.0,
        sessions_done=3,
        sessions_planned=5,
    )
    lines = prose.summary_lines(f, confidence="high", next_targets=Macros(2250, 200, 70, 200, 30))
    assert len(lines) <= 6


def test_24_a_week_with_no_weigh_in_says_so_rather_than_showing_nothing() -> None:
    lines = prose.summary_lines(
        facts_mod.Facts(1, WEEK, SUNDAY), confidence=None, next_targets=None
    )
    assert any("no weigh-in" in ln.lower() for ln in lines)


# -- the browser probe, and the operator's preview ---------------------------------------------


def a_probe(returncode: int | BaseException = 0, stdout: str = "") -> chrome_job.Runner:
    def runner(*a: object, **k: object) -> subprocess.CompletedProcess[str]:
        if isinstance(returncode, BaseException):
            raise returncode
        return subprocess.CompletedProcess(
            args=["powershell.exe"], returncode=returncode, stdout=stdout, stderr=""
        )

    return runner


def test_31_a_probe_that_cannot_run_at_all_does_not_stop_a_real_job() -> None:
    """The shop reader needs the owner's own Chrome already running, and the only way
    to see a Windows command line without psutil is to shell out. If the shell-out itself is
    the thing that fails, refusing to run the job would be the probe deciding something it has
    no information about. The session will fail honestly a moment later if Chrome really is
    absent."""
    for boom in (OSError("no powershell"), subprocess.SubprocessError("timed out")):
        assert chrome_job.powershell_chrome_probe("Profile 2", runner=a_probe(boom)) is True


def test_32_a_probe_that_returns_an_error_code_also_answers_yes() -> None:
    assert chrome_job.powershell_chrome_probe("Profile 2", runner=a_probe(1, "")) is True


def test_33_a_chrome_running_the_wrong_profile_is_not_the_one_we_need() -> None:
    other = 'chrome.exe --profile-directory="Default"\nchrome.exe --profile-directory=Profile 1\n'
    probe = chrome_job.powershell_chrome_probe
    assert probe("Profile 2", runner=a_probe(0, other), open_profiles=lambda: ["Default"]) is False


def test_33b_a_chrome_started_bare_with_the_profile_open_is_the_one_we_need() -> None:
    """A browser session with the expected profile is accepted."""
    bare = '"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"\n'
    probe = chrome_job.powershell_chrome_probe
    open_both = lambda: ["Default", "Profile 2"]  # noqa: E731
    assert probe("Profile 2", runner=a_probe(0, bare), open_profiles=open_both) is True
    assert probe("Profile 2", runner=a_probe(0, bare), open_profiles=lambda: ["Default"]) is False
    # Local State outlives the browser: with no Chrome running, it says nothing
    assert probe("Profile 2", runner=a_probe(0, ""), open_profiles=open_both) is False


def test_33c_local_state_is_read_for_its_open_profiles(tmp_path) -> None:
    state = tmp_path / "Local State"
    state.write_text('{"profile": {"last_active_profiles": ["Default", "Profile 2"]}}', "utf-8")
    assert chrome_job.local_state_profiles(state) == ["Default", "Profile 2"]
    state.write_text("not json", encoding="utf-8")
    assert chrome_job.local_state_profiles(state) == []
    assert chrome_job.local_state_profiles(tmp_path / "missing") == []


def test_34_the_profile_is_found_whether_or_not_windows_quoted_it() -> None:
    for line in (
        "chrome.exe --profile-directory=Profile 2",
        'chrome.exe --profile-directory="Profile 2"',
    ):
        assert chrome_job.powershell_chrome_probe("Profile 2", runner=a_probe(0, line)) is True


def test_35_no_chrome_at_all_is_a_clear_no() -> None:
    probe = chrome_job.powershell_chrome_probe
    assert probe("Profile 2", runner=a_probe(0, ""), open_profiles=lambda: ["Profile 2"]) is False


def an_app(config) -> App:
    return App(config, Secrets(config.paths.root / "sec"), clock=FrozenClock(FROZEN))


def onboarded_app(config) -> App:
    """A real database on disk with a person who has a profile, which is what render_day copies."""
    app = an_app(config)
    db = app.bootstrap()
    try:
        a_profile(db, 1, meals_per_day=4, segment_days=json.dumps([[0, 1, 2, 3, 4]]))
        db.insert(
            "targets",
            person_id=1,
            week_start="2026-09-07",
            kcal=2250,
            protein_g=200,
            fat_g=70,
            carb_g=200,
            fibre_g=30,
            source="formula",
        )
    finally:
        db.close()
    return app


def test_36_a_dry_run_of_a_day_with_nobody_onboarded_still_produces_a_page(config) -> None:
    """The preview is what an operator opens when something looks wrong, so it has to render
    on the worst day, not just a normal one."""
    messages = dryrun.render_day(an_app(config), dt.date(2026, 9, 12))
    page = dryrun.to_html(dt.date(2026, 9, 12), messages)
    assert "Dry run 2026-09-12" in page


def test_37_a_dry_run_leaves_the_database_it_previewed_exactly_as_it_was(config) -> None:
    app = onboarded_app(config)
    before = config.paths.db.read_bytes()
    dryrun.render_day(app, dt.date(2026, 9, 12))
    assert config.paths.db.read_bytes() == before, "the preview works on a throwaway copy"


def test_38_a_preview_with_no_messages_says_so_in_both_formats() -> None:
    day = dt.date(2026, 9, 12)
    assert "No messages due" in dryrun.to_html(day, [])
    assert "No messages due" in dryrun.to_text([])


def test_39_a_preview_escapes_the_parts_a_person_chose(config) -> None:
    """The slug and the job name are ours, but a display name is not, and the page is opened in
    a browser."""
    m = dryrun.DryMessage("owner", "<script>x</script>", "2026-09-12 07:30", "Morning.", [])
    assert "<script>" not in dryrun.to_html(dt.date(2026, 9, 12), [m])


def test_40_a_preview_never_runs_the_jobs_that_touch_the_outside_world(config) -> None:
    """A dry run that synced intervals.icu or took a backup would not be a
    preview: it would be the day happening twice."""
    from tiffin.jobs import all_jobs

    messages = dryrun.render_day(onboarded_app(config), dt.date(2026, 9, 12))
    assert messages, "a person with a profile has messages due on a Saturday"
    data_jobs = {j.name for j in all_jobs() if j.kind != "message"}
    assert data_jobs, "there are data jobs to exclude"
    assert {m.kind for m in messages} & data_jobs == set()


# -- saying what you eat, in your own words -------------------------------------------------


class _Person:
    def __init__(self, pid: int) -> None:
        self.id = pid
        self.slug = "owner"


class _Handlers:
    """`intents.apply` needs a db and, for other kinds, an outbox. This branch writes only."""

    def __init__(self, db: Db) -> None:
        self.db = db


def apply_intent(db: Db, pid: int, list_name: str, *, add: list[str], remove: list[str]) -> str:
    """Queue the tool's intent and then tap Accept, the way a person does."""
    out = tools.BoundTools(db, pid).call(
        "set_food_prefs", {"list_name": list_name, "add": add, "remove": remove}
    )
    row = db.one("SELECT * FROM intents WHERE id = ?", (out["intent_id"],))
    assert row is not None
    return intents.apply(
        _Handlers(db),  # type: ignore[arg-type]
        _Person(pid),  # type: ignore[arg-type]
        row,
        clock=db.clock,
        message_in_id=None,
    )


def a_person_with_a_profile(db: Db, pid: int, **over: object) -> None:
    a_profile(db, pid, refused_foods=json.dumps(["Olives"]), **over)


def prefs(db: Db, pid: int, column: str = "refused_foods") -> list[str]:
    row = db.one(
        "SELECT * FROM profiles WHERE person_id = ? ORDER BY effective_from DESC, id DESC LIMIT 1",
        (pid,),
    )
    return [] if row is None or row[column] is None else json.loads(str(row[column]))


def test_41_avoid_chickpeas_is_something_the_model_can_actually_do(
    fixture_db: Db, two_persons
) -> None:
    """The gap this closes, in the owner's own words on 11 September: "why can I not just tell it to
    avoid chickpeas?" He could not, because the chat model had eight tools for reading his food
    and five for writing, and none of the five touched a food list. It answered by sending him
    to /set, which is a correct answer and a useless one.
    """
    pid = two_persons["owner"]
    bound = tools.BoundTools(fixture_db, pid, model_used="claude-opus-5")
    out = bound.call("set_food_prefs", {"list_name": "avoid", "add": ["chickpeas"]})
    assert out["needs_tap"] is True, "the model proposes; the person decides"
    row = fixture_db.one("SELECT * FROM intents WHERE id = ?", (out["intent_id"],))
    assert row is not None
    assert intents.describe("set_food_prefs", json.loads(row["payload_json"])) == (
        "Keep chickpeas out of your menus from now on?"
    )


def test_42_the_tap_is_what_writes_the_list(fixture_db: Db, two_persons) -> None:
    pid = two_persons["owner"]
    a_person_with_a_profile(fixture_db, pid)
    bound = tools.BoundTools(fixture_db, pid)
    bound.call("set_food_prefs", {"list_name": "avoid", "add": ["Chickpeas"]})
    assert prefs(fixture_db, pid) == ["Olives"], "nothing changes until it is tapped"


def test_43_a_food_list_goes_through_the_same_door_as_the_command(
    fixture_db: Db, two_persons
) -> None:
    """`/set avoid + x` and this tool must not be able to disagree: both amend the same list,
    casefolded, keeping the person's own spelling, and both write a new profiles row."""
    pid = two_persons["owner"]
    a_person_with_a_profile(fixture_db, pid)
    said = apply_intent(fixture_db, pid, "avoid", add=["chickpeas", "OLIVES"], remove=[])
    assert prefs(fixture_db, pid) == ["chickpeas", "Olives"], "no duplicate, the old spelling wins"
    assert "chickpeas" in said and "next menu" in said.lower()


def test_44_taking_a_food_back_off_the_avoid_list(fixture_db: Db, two_persons) -> None:
    pid = two_persons["owner"]
    a_person_with_a_profile(fixture_db, pid)
    apply_intent(fixture_db, pid, "avoid", add=[], remove=["olives"])
    assert prefs(fixture_db, pid) == []


def test_45_the_three_lists_are_separate(fixture_db: Db, two_persons) -> None:
    pid = two_persons["owner"]
    a_person_with_a_profile(fixture_db, pid)
    apply_intent(fixture_db, pid, "likes", add=["Salmon"], remove=[])
    apply_intent(fixture_db, pid, "pantry", add=["Olive oil"], remove=[])
    assert prefs(fixture_db, pid, "refused_foods") == ["Olives"]
    assert prefs(fixture_db, pid, "liked_foods") == ["Salmon"]
    assert prefs(fixture_db, pid, "pantry_json") == ["Olive oil"]


def test_46_a_refused_food_is_out_of_the_next_menu_the_moment_it_is_tapped(
    fixture_db: Db, two_persons
) -> None:
    """The whole point. The list is not a note: `catalogue.approved` reads it."""
    from tiffin.food import catalogue

    pid = two_persons["owner"]
    a_person_with_a_profile(fixture_db, pid)
    repo = pathlib.Path(__file__).resolve().parents[1]
    catalogue.build(
        fixture_db,
        repo / "config" / "ingredients.yaml",
        repo / "data" / "cofid" / "cofid_2021_proximates.csv",
    )
    assert "chickpeas" in [i.code for i in catalogue.approved(fixture_db, pid)]
    apply_intent(fixture_db, pid, "avoid", add=["chickpeas"], remove=[])
    assert "chickpeas" not in [i.code for i in catalogue.approved(fixture_db, pid)]


def test_47_the_model_cannot_invent_a_fourth_list(fixture_db: Db, two_persons) -> None:
    bound = tools.BoundTools(fixture_db, two_persons["owner"])
    for bad in ({"list_name": "banned"}, {"list_name": "avoid"}, {"list_name": "avoid", "add": []}):
        with pytest.raises(tools.ToolError):
            bound.call("set_food_prefs", bad)


def test_48_a_food_in_both_add_and_remove_is_refused_rather_than_guessed(
    fixture_db: Db, two_persons
) -> None:
    bound = tools.BoundTools(fixture_db, two_persons["owner"])
    with pytest.raises(tools.ToolError):
        bound.call(
            "set_food_prefs", {"list_name": "avoid", "add": ["olives"], "remove": ["Olives"]}
        )


def test_49_a_person_with_no_profile_is_told_what_to_do_rather_than_crashing(
    fixture_db: Db, two_persons
) -> None:
    said = apply_intent(fixture_db, two_persons["partner"], "avoid", add=["olives"], remove=[])
    assert "onboard" in said.lower()


def test_50_partner_food_list_is_not_their(fixture_db: Db, two_persons) -> None:
    his, partners = two_persons["owner"], two_persons["partner"]
    a_person_with_a_profile(fixture_db, his)
    a_profile(fixture_db, partners, refused_foods=json.dumps([]))
    apply_intent(fixture_db, partners, "avoid", add=["chickpeas"], remove=[])
    assert prefs(fixture_db, his) == ["Olives"]
    assert prefs(fixture_db, partners) == ["chickpeas"]
