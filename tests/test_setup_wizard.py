"""`tiffin setup`, driven the way a person drives it: answers typed in order, the screen read back.

The stand-in world (tests/wizard.py) answers for Telegram, intervals.icu and the model. Each test
says what a person would see or what would be saved, and every one checks that no token or key
was ever printed.
"""

from __future__ import annotations

import re
import stat
import sys
import tomllib
from pathlib import Path

import pytest

from tiffin import cli, doctor
from tiffin import setup as wizard_module
from tiffin.clock import FrozenClock
from tiffin.config import load
from tiffin.db import Db
from tiffin.errors import ConfigError, ModelError
from tiffin.persons import PersonRegistry

from .wizard import (
    BAD_ICU,
    BAD_TOKEN,
    CHAT_ID,
    GOOD_ICU,
    GOOD_TOKEN,
    MODEL_KEY,
    OWNER_CODE,
    SECOND_CODE,
    Keyboard,
    World,
    assert_no_secret_shown,
    deps,
    predictable_codes,
    setup,
)

#: step 1 for a person in London, every suggestion taken
HOME_ANSWERS = ["Europe/London", "", "", "", ""]


@pytest.fixture(autouse=True)
def nothing_listens_here(monkeypatch: pytest.MonkeyPatch) -> None:
    """The checks at the end look for the page on its port; whatever this machine runs there is
    not the Tiffin under test."""
    monkeypatch.setattr(doctor, "_listening", lambda port: False)
    predictable_codes(monkeypatch)


@pytest.fixture
def home(tmp_path: Path) -> Path:
    return tmp_path / "tiffin-home"


def settings(home: Path) -> dict:
    return tomllib.loads((home / "tiffin.toml").read_text(encoding="utf-8"))


def owner_chat(home: Path, clock: FrozenClock) -> int | None:
    with Db.open(home / "tiffin.db", clock=clock) as db:
        return PersonRegistry(db, load(root=home, environ={})).admin().chat_id


def first_run(home: Path, clock: FrozenClock, *answers: str, **kw) -> tuple[int, Keyboard]:
    world = kw.pop("world", None) or World(sent=[OWNER_CODE])
    keys = Keyboard(answers)
    environ = {"TIFFIN_HOME": str(home), **kw.pop("environ", {})}
    code = setup(["setup", *kw.pop("argv", [])], deps(world, keys, clock, environ=environ, **kw))
    return code, keys


#: one person, no model, no training data, no page, no supermarket, nothing loaded at the end
PLAIN = [
    *HOME_ANSWERS,
    "1",  # just me
    "Alex",
    GOOD_TOKEN,
    "1",  # pair now: the world sends the code
    "1",  # no model
    "",  # no intervals.icu key
    "1",  # page off
    "n",  # no supermarket products
    "",  # save
    "n",  # not the food list now
    "n",  # no background service
]


# -------------------------------------------------------------------------------- first run
def test_a_first_run_walks_eight_steps_and_saves_what_was_said(home, frozen_clock) -> None:
    code, keys = first_run(home, frozen_clock, *PLAIN)
    screen = keys.screen
    assert code == 0, screen
    for number, title in enumerate(
        (
            "Home and time zone",
            "People",
            "Telegram",
            "AI model",
            "Training data",
            "Web page",
            "Shopping",
            "Save",
        ),
        1,
    ):
        assert f"Step {number} of 8 · {title}" in screen
    assert "✓ Connected to @example_tiffin_bot." in screen
    assert "✓ Paired with your chat." in screen
    assert "What next" in screen and "send /start to @example_tiffin_bot" in screen
    assert screen.rstrip().endswith("✓ Tiffin is set up.")
    saved = settings(home)
    assert saved["env"] == {"tz": "Europe/London", "country": "GB"}
    assert saved["persons"]["owner"]["display_name"] == "Alex"
    assert saved["model"]["provider"] == "none"
    assert saved["web"]["enabled"] is False
    assert GOOD_TOKEN not in (home / "tiffin.toml").read_text(encoding="utf-8")
    token = (home / "secrets" / "telegram-token.txt").read_text(encoding="utf-8").strip()
    assert token == GOOD_TOKEN
    assert owner_chat(home, frozen_clock) == CHAT_ID
    load(root=home, environ={})  # what was saved is a valid configuration
    assert_no_secret_shown(screen)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_secrets_are_owner_only_files_in_an_owner_only_folder(home, frozen_clock) -> None:
    code, _ = first_run(home, frozen_clock, *PLAIN)
    assert code == 0
    folder = home / "secrets"
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    assert stat.S_IMODE((folder / "telegram-token.txt").stat().st_mode) == 0o600


def test_a_wrong_token_is_said_in_words_and_asked_again(home, frozen_clock) -> None:
    answers = list(PLAIN)
    answers[answers.index(GOOD_TOKEN)] = BAD_TOKEN
    answers.insert(answers.index("Alex") + 2, GOOD_TOKEN)
    code, keys = first_run(home, frozen_clock, *answers)
    assert code == 0, keys.screen
    assert "✗ Telegram does not know that token. Copy it again from @BotFather." in keys.screen
    assert keys.prompts.count("Bot token: ") == 2
    saved = (home / "secrets" / "telegram-token.txt").read_text(encoding="utf-8").strip()
    assert saved == GOOD_TOKEN
    assert_no_secret_shown(keys.screen)


def test_something_that_is_not_a_token_is_refused_before_asking_telegram(
    home, frozen_clock
) -> None:
    world = World(sent=[OWNER_CODE])
    answers = list(PLAIN)
    answers.insert(answers.index(GOOD_TOKEN), "my-bot-password")
    code, keys = first_run(home, frozen_clock, *answers, world=world)
    assert code == 0, keys.screen
    assert "That does not look like a bot token" in keys.screen
    assert "my-bot-password" not in keys.screen
    assert sum("getMe" in url for url in world.urls) >= 1


def test_an_unreachable_telegram_says_so_and_the_person_can_stop(home, frozen_clock) -> None:
    world = World(telegram_down=True)
    code, keys = first_run(
        home, frozen_clock, *HOME_ANSWERS, "1", "Alex", GOOD_TOKEN, "quit", world=world
    )
    assert code == 1
    assert "Could not reach Telegram" in keys.screen
    assert "Setup stopped. Nothing was saved." in keys.screen
    assert not (home / "tiffin.toml").exists()


def test_back_goes_to_the_previous_step_and_quit_saves_nothing(home, frozen_clock) -> None:
    code, keys = first_run(
        home,
        frozen_clock,
        *HOME_ANSWERS,
        "back",  # at "Who will use Tiffin": back to step 1
        *HOME_ANSWERS,
        "1",
        "Alex",
        "quit",  # at the bot token
    )
    assert code == 1
    assert keys.screen.count("Step 1 of 8 · Home and time zone") == 2
    assert "Setup stopped. Nothing was saved." in keys.screen
    assert not (home / "tiffin.toml").exists()
    assert not (home / "secrets").exists()


def test_telegram_cannot_be_skipped_on_a_first_run_but_the_model_can(home, frozen_clock) -> None:
    answers = list(PLAIN)
    answers.insert(answers.index(GOOD_TOKEN), "skip")
    model_choice = answers.index("1", answers.index(GOOD_TOKEN) + 2)
    answers[model_choice] = "skip"
    code, keys = first_run(home, frozen_clock, *answers)
    assert code == 0, keys.screen
    assert "Tiffin needs this step, so it cannot be skipped." in keys.screen
    assert "Skipped: no model for now; tiffin setup model adds one." in keys.screen
    assert settings(home)["model"]["provider"] == "none", "a skip never switches a model on"


def test_two_people_get_a_code_for_the_partner_at_the_end(home, frozen_clock) -> None:
    answers = [
        *HOME_ANSWERS,
        "2",  # me and a partner
        "Alex",
        "Sam",
        GOOD_TOKEN,
        "1",  # pair now
        "1",  # a code for the partner
        "1",  # no model
        "",  # no key for me
        "",  # none for my partner
        "1",  # page off
        "n",
        "",
        "n",
        "n",
    ]
    code, keys = first_run(home, frozen_clock, *answers)
    assert code == 0, keys.screen
    assert f"Their code is {SECOND_CODE}" in keys.screen
    assert f"Give your partner the code {SECOND_CODE}" in keys.screen
    assert "your partner pairs with their code (below)" in keys.screen
    assert settings(home)["household"]["partner_expected"] is True
    with Db.open(home / "tiffin.db", clock=frozen_clock) as db:
        registry = PersonRegistry(db, load(root=home, environ={}))
        assert registry.require("partner").display_name == "Sam"
        assert registry.require("partner").chat_id is None


def test_pairing_later_shows_a_code_that_works_once_tiffin_runs(home, frozen_clock) -> None:
    answers = list(PLAIN)
    answers[answers.index(GOOD_TOKEN) + 1] = "2"  # later
    code, keys = first_run(home, frozen_clock, *answers, world=World())
    assert code == 0, keys.screen
    assert f"Send {OWNER_CODE} to @example_tiffin_bot once Tiffin is running" in keys.screen
    assert "yours pairs when you send your code (below)" in keys.screen
    assert owner_chat(home, frozen_clock) is None


# -------------------------------------------------------------------------------- the model
def test_the_model_check_says_what_it_may_cost_and_can_be_declined(home, frozen_clock) -> None:
    probes = []
    answers = list(PLAIN)
    at = answers.index("1", answers.index(GOOD_TOKEN) + 2)
    answers[at : at + 1] = ["3", MODEL_KEY, "n"]  # Anthropic, its key, no check
    code, keys = first_run(home, frozen_clock, *answers, probe=probes.append)
    assert code == 0, keys.screen
    cost = keys.screen.index("costs a fraction of a penny")
    assert cost < keys.screen.index("Check it now [Y/n]: n")
    assert probes == [], "declined: no request was sent"
    assert re.search(r"model +not checked; tiffin doctor sends one tiny request", keys.screen)
    assert settings(home)["model"]["provider"] == "anthropic"
    assert (home / "secrets" / "model-api-key.txt").read_text(encoding="utf-8").strip() == MODEL_KEY
    assert_no_secret_shown(keys.screen)


def test_a_model_that_refuses_the_key_can_be_kept_anyway(home, frozen_clock) -> None:
    def refuse(app) -> None:
        raise ModelError("anthropic: HTTP 401")

    answers = list(PLAIN)
    at = answers.index("1", answers.index(GOOD_TOKEN) + 2)
    answers[at : at + 1] = ["3", MODEL_KEY, "y", "1", "3"]  # check, try again, then keep
    code, keys = first_run(home, frozen_clock, *answers, probe=refuse)
    assert code == 0, keys.screen
    assert keys.screen.count("✗ The key was not accepted. Check it, or make a new one.") == 2
    assert re.search(r"model +did not answer during setup", keys.screen)


def test_changing_the_answers_after_a_failed_check_asks_the_step_again(home, frozen_clock) -> None:
    calls = []

    def first_fails(app) -> None:
        calls.append(app.config.get("model.provider"))
        if len(calls) == 1:
            raise ModelError("openai: connection failed")

    answers = list(PLAIN)
    at = answers.index("1", answers.index(GOOD_TOKEN) + 2)
    answers[at : at + 1] = ["4", MODEL_KEY, "y", "2", "3", MODEL_KEY, "y"]
    code, keys = first_run(home, frozen_clock, *answers, probe=first_fails)
    assert code == 0, keys.screen
    assert "Could not connect to the service" in keys.screen
    assert calls == ["openai", "anthropic"]
    assert keys.screen.count("Step 4 of 8 · AI model") == 2
    assert re.search(r"model +answered during setup", keys.screen)


# -------------------------------------------------------------------------------- intervals
def test_a_refused_intervals_key_is_said_and_can_then_be_skipped(home, frozen_clock) -> None:
    answers = list(PLAIN)
    at = answers.index("", answers.index(GOOD_TOKEN) + 2)  # the intervals.icu key
    answers[at : at + 1] = [BAD_ICU, ""]
    code, keys = first_run(home, frozen_clock, *answers)
    assert code == 0, keys.screen
    assert "intervals.icu did not accept that key" in keys.screen
    assert "Skipped; add it later with tiffin setup training." in keys.screen
    assert not (home / "secrets" / "icu-key-owner.txt").exists()
    assert_no_secret_shown(keys.screen)


def test_an_accepted_intervals_key_is_saved_and_checked_again_at_the_end(
    home, frozen_clock
) -> None:
    answers = list(PLAIN)
    at = answers.index("", answers.index(GOOD_TOKEN) + 2)
    answers[at] = GOOD_ICU
    code, keys = first_run(home, frozen_clock, *answers)
    assert code == 0, keys.screen
    assert "✓ intervals.icu accepted the key." in keys.screen
    assert re.search(r"intervals owner +valid", keys.screen)
    assert (home / "secrets" / "icu-key-owner.txt").read_text(encoding="utf-8").strip() == GOOD_ICU


# -------------------------------------------------------------------------------- the web page
def test_a_page_behind_a_proxy_in_a_container_listens_on_its_network(home, frozen_clock) -> None:
    answers = list(PLAIN)
    at = answers.index("1", answers.index("", answers.index(GOOD_TOKEN) + 2))  # page: off
    answers[at : at + 1] = [
        "3",
        "tiffin.example.com/tiffin",  # https:// is added
        "",  # path, from the address
        "",  # in a container the suggestion is "another container"
        "a rented server in the EU, run by Alex",
        "Cloudflare",
    ]
    code, keys = first_run(home, frozen_clock, *answers, container="docker")
    assert code == 0, keys.screen
    web = settings(home)["web"]
    assert web["origin"] == "https://tiffin.example.com"
    assert web["root_path"] == "/tiffin"
    assert web["host"] == "0.0.0.0" and web["bind_trusted_network"] is True
    assert web["identity_mode"] == "telegram" and web["trusted_header_proxy"] is False
    assert settings(home)["hosting"] == {
        "records": "a rented server in the EU, run by Alex",
        "web_front": "Cloudflare",
    }
    assert "Never publish that port to the internet" in keys.screen
    assert "Start the container" in keys.screen


def test_a_page_address_must_be_https_and_is_asked_again(home, frozen_clock) -> None:
    answers = list(PLAIN)
    at = answers.index("1", answers.index("", answers.index(GOOD_TOKEN) + 2))
    answers[at : at + 1] = ["3", "http://tiffin.example.com", "https://tiffin.example.com", "", "1"]
    answers[at + 5 : at + 5] = ["", ""]  # records and what the page passes through
    code, keys = first_run(home, frozen_clock, *answers)
    assert code == 0, keys.screen
    assert "The address must start with https://" in keys.screen
    assert settings(home)["web"]["root_path"] == ""


def test_tailscale_sets_header_sign_in_and_declares_its_proxy(home, frozen_clock) -> None:
    code, keys = first_run(
        home,
        frozen_clock,
        argv=[
            "--non-interactive",
            "--owner-name",
            "Alex",
            "--timezone",
            "Europe/London",
            "--web-mode",
            "tailscale",
            "--web-origin",
            "https://tiffin.example.ts.net",
            "--pair",
            "later",
            "--no-catalogue",
        ],
        environ={"TIFFIN_TELEGRAM_TOKEN": GOOD_TOKEN},
    )
    assert code == 0, keys.screen
    config = load(root=home, environ={})
    assert config.get("web.identity_mode") == "header"
    assert config.get("web.trusted_header_proxy") is True
    assert config.get("web.root_path") == "/tiffin"


# -------------------------------------------------------------------------------- re-running
def installed(home: Path, clock: FrozenClock, *extra: str) -> None:
    """A finished install, made the scripted way."""
    code, keys = first_run(
        home,
        clock,
        argv=[
            "--non-interactive",
            "--owner-name",
            "Alex",
            "--timezone",
            "Europe/London",
            "--model-provider",
            "none",
            "--web-mode",
            "proxy",
            "--web-origin",
            "https://tiffin.example.com/tiffin",
            "--hosting-records",
            "a rented server in the EU",
            "--no-catalogue",
            *extra,
        ],
        environ={"TIFFIN_TELEGRAM_TOKEN": GOOD_TOKEN},
    )
    assert code == 0, keys.screen


def rerun(home: Path, clock: FrozenClock, *answers: str, argv: list[str] | None = None, **kw):
    keys = Keyboard(answers)
    world = kw.pop("world", None) or World()
    the_deps = deps(world, keys, clock, environ={"TIFFIN_HOME": str(home)}, **kw)
    return setup(["setup", *(argv or [])], the_deps), keys


def test_a_rerun_shows_what_is_set_and_changes_one_part_only(home, frozen_clock) -> None:
    installed(home, frozen_clock)
    path = home / "tiffin.toml"
    path.write_text(
        path.read_text(encoding="utf-8").replace("[web]", "# my own note, kept by setup\n[web]"),
        encoding="utf-8",
    )
    before = settings(home)
    code, keys = rerun(
        home,
        frozen_clock,
        "6",  # the web page
        "",  # still behind a proxy
        "",  # same address
        "/",  # at the top now
        "",  # on this computer
        "",  # same records
        "",  # nothing in front
        "",  # save
    )
    screen = keys.screen
    assert code == 0, screen
    assert "Settings found in" in screen
    assert re.search(r"6  Web page +at " + re.escape("https://tiffin.example.com/tiffin/"), screen)
    assert "Step 1 of 2 · Web page" in screen and "Step 2 of 2 · Save" in screen
    assert "Here is what will change:" in screen
    assert "was: at https://tiffin.example.com/tiffin/" in screen
    after = settings(home)
    assert after["web"]["root_path"] == ""
    assert {k: v for k, v in after.items() if k != "web"} == {
        k: v for k, v in before.items() if k != "web"
    }
    text = path.read_text(encoding="utf-8")
    assert "# my own note, kept by setup" in text, "setup keeps the lines it does not change"
    backups = list((home / "state" / "setup").iterdir())
    assert len(backups) == 1 and "/tiffin" in backups[0].read_text(encoding="utf-8")
    assert "The previous tiffin.toml is kept as" in screen
    assert "Load the food list that comes with Tiffin now" not in screen, "only on a full run"
    assert_no_secret_shown(screen)


def test_a_rerun_keeps_a_saved_token_on_enter_and_shows_only_its_mask(home, frozen_clock) -> None:
    installed(home, frozen_clock)
    token_file = home / "secrets" / "telegram-token.txt"
    written = token_file.stat().st_mtime_ns
    code, keys = rerun(home, frozen_clock, "", argv=["telegram"], world=World(sent=[]))
    assert code == 0, keys.screen
    assert "Bot token [saved, ••••GGGG; Enter keeps it]: " in keys.prompts
    assert "Nothing has changed, so there is nothing to save." in keys.screen
    assert token_file.stat().st_mtime_ns == written
    assert not (home / "state" / "setup").exists(), "nothing saved, nothing backed up"
    assert_no_secret_shown(keys.screen)


def test_a_rerun_that_skips_leaves_everything_as_it_was(home, frozen_clock) -> None:
    installed(home, frozen_clock)
    before = (home / "tiffin.toml").read_text(encoding="utf-8")
    code, keys = rerun(home, frozen_clock, "skip", argv=["model"])
    assert code == 0, keys.screen
    assert "Skipped: left as it was." in keys.screen
    assert (home / "tiffin.toml").read_text(encoding="utf-8") == before


def test_back_from_the_first_part_of_a_rerun_returns_to_the_list(home, frozen_clock) -> None:
    installed(home, frozen_clock)
    code, keys = rerun(home, frozen_clock, "4", "back", "q")
    assert code == 0, keys.screen
    assert keys.screen.count("Settings found in") == 2
    assert "Nothing changed." in keys.screen


def test_q_at_the_list_changes_nothing(home, frozen_clock) -> None:
    installed(home, frozen_clock)
    code, keys = rerun(home, frozen_clock, "")
    assert code == 0
    assert "Nothing changed." in keys.screen


def test_c_at_the_list_runs_the_checks_without_saving(home, frozen_clock) -> None:
    installed(home, frozen_clock)
    before = (home / "tiffin.toml").read_text(encoding="utf-8")
    code, keys = rerun(home, frozen_clock, "c")
    assert code == 0, keys.screen
    assert "Checking everything" in keys.screen
    assert re.search(r"Telegram API +@example_tiffin_bot", keys.screen)
    assert (home / "tiffin.toml").read_text(encoding="utf-8") == before


# -------------------------------------------------------------------------------- unattended
def test_unattended_first_run_takes_flags_and_environment_and_pairs_later(
    home, frozen_clock
) -> None:
    code, keys = first_run(
        home,
        frozen_clock,
        argv=[
            "--non-interactive",
            "--owner-name",
            "Alex",
            "--timezone",
            "Europe/Paris",
            "--model-provider",
            "none",
            "--web-mode",
            "off",
            "--pair",
            "later",
        ],
        environ={"TIFFIN_TELEGRAM_TOKEN": GOOD_TOKEN},
    )
    assert code == 0, keys.screen
    assert keys.prompts == [], "nothing is asked"
    config = load(root=home, environ={})
    assert config.get("env.tz") == "Europe/Paris" and config.get("env.country") == "FR"
    assert config.get("units.currency") == "EUR"
    assert "UK CoFID data" in keys.screen
    assert f"Send {OWNER_CODE} to @example_tiffin_bot once Tiffin is running" in keys.screen
    assert "Loaded the food list:" in keys.screen, "an empty food list is loaded unattended"
    # a variable set only for this command: the token is kept, as `tiffin init` always did
    token = (home / "secrets" / "telegram-token.txt").read_text(encoding="utf-8").strip()
    assert token == GOOD_TOKEN
    assert_no_secret_shown(keys.screen)


def test_unattended_pairing_waits_for_the_code_and_moves_the_offset_on(home, frozen_clock) -> None:
    code, keys = first_run(
        home,
        frozen_clock,
        argv=["--non-interactive", "--owner-name", "Alex", "--web-mode", "off", "--no-catalogue"],
        environ={"TIFFIN_TELEGRAM_TOKEN": GOOD_TOKEN},
    )
    assert code == 0, keys.screen
    assert f"Send this code to @example_tiffin_bot as a message of its own:  {OWNER_CODE}" in (
        keys.screen
    )
    assert owner_chat(home, frozen_clock) == CHAT_ID
    with Db.open(home / "tiffin.db", clock=frozen_clock) as db:
        assert db.state_get("update_offset") == "2"


def test_unattended_owner_chat_id_pairs_without_a_message(home, frozen_clock) -> None:
    code, keys = first_run(
        home,
        frozen_clock,
        argv=[
            "--non-interactive",
            "--owner-name",
            "Alex",
            "--owner-chat-id",
            "777",
            "--web-mode",
            "off",
            "--no-catalogue",
        ],
        environ={"TIFFIN_TELEGRAM_TOKEN": GOOD_TOKEN},
        world=World(),
    )
    assert code == 0, keys.screen
    assert owner_chat(home, frozen_clock) == 777


def test_unattended_without_a_needed_answer_stops_before_writing(
    home, frozen_clock, monkeypatch, capsys
) -> None:
    keys = Keyboard([])
    monkeypatch.setattr(
        wizard_module,
        "default_deps",
        lambda: deps(World(), keys, frozen_clock, environ={"TIFFIN_HOME": str(home)}),
    )
    code = cli.main(["setup", "--non-interactive", "--web-mode", "off"])
    assert code == 2
    assert "--owner-name is needed with --non-interactive" in capsys.readouterr().err
    assert not (home / "tiffin.toml").exists()


def test_unattended_with_a_refused_token_stops_with_exit_2_and_writes_nothing(
    home, frozen_clock, monkeypatch, capsys
) -> None:
    keys = Keyboard([])
    monkeypatch.setattr(
        wizard_module,
        "default_deps",
        lambda: deps(World(), keys, frozen_clock, environ={"TIFFIN_HOME": str(home)}),
    )
    code = cli.main(
        ["init", "--non-interactive", "--owner-name", "Alex", "--telegram-token", BAD_TOKEN]
    )
    assert code == 2
    err = capsys.readouterr().err
    assert "Telegram does not know that token" in err
    assert not (home / "tiffin.toml").exists()
    assert_no_secret_shown(err, keys.screen)


def test_unattended_rerun_changes_only_what_its_flags_name(home, frozen_clock) -> None:
    installed(home, frozen_clock)
    before = settings(home)
    code, keys = rerun(home, frozen_clock, argv=["--non-interactive", "--web-mode", "off"])
    assert code == 0, keys.screen
    after = settings(home)
    assert after["web"]["enabled"] is False
    assert after["web"]["origin"] == before["web"]["origin"], "the address is kept for later"
    assert {k: v for k, v in after.items() if k != "web"} == {
        k: v for k, v in before.items() if k != "web"
    }


def test_unattended_rerun_with_no_flags_shows_the_settings_and_checks(home, frozen_clock) -> None:
    installed(home, frozen_clock)
    before = (home / "tiffin.toml").read_text(encoding="utf-8")
    code, keys = rerun(home, frozen_clock, argv=["--non-interactive"])
    assert code == 0, keys.screen
    assert "Nothing to change. Current settings:" in keys.screen
    assert "Checking everything" in keys.screen
    assert (home / "tiffin.toml").read_text(encoding="utf-8") == before


def test_a_token_from_a_file_variable_is_used_and_not_copied(home, frozen_clock, tmp_path) -> None:
    mounted = tmp_path / "telegram_token"
    mounted.write_text(GOOD_TOKEN + "\n", encoding="utf-8")
    code, keys = first_run(
        home,
        frozen_clock,
        argv=[
            "--non-interactive",
            "--owner-name",
            "Alex",
            "--web-mode",
            "off",
            "--pair",
            "later",
            "--no-catalogue",
        ],
        environ={"TIFFIN_TELEGRAM_TOKEN_FILE": str(mounted)},
    )
    assert code == 0, keys.screen
    assert not (home / "secrets" / "telegram-token.txt").exists()


def test_connect_and_init_are_the_same_setup(home, frozen_clock) -> None:
    installed(home, frozen_clock)
    keys = Keyboard([])
    code = setup(
        ["connect", "web", "--non-interactive", "--web-mode", "local"],
        deps(World(), keys, frozen_clock, environ={"TIFFIN_HOME": str(home)}),
    )
    assert code == 0, keys.screen
    config = load(root=home, environ={})
    assert config.get("web.origin") == "http://127.0.0.1:7788"
    assert config.get("web.identity_mode") == "telegram"


def test_no_terminal_and_no_flags_says_how_to_run_unattended(home, frozen_clock) -> None:
    def closed(prompt: str) -> str:
        raise EOFError

    the_deps = deps(World(), Keyboard([]), frozen_clock, environ={"TIFFIN_HOME": str(home)})
    the_deps.read = closed
    with pytest.raises(ConfigError, match="--non-interactive"):
        setup(["setup"], the_deps)


def test_a_service_is_offered_on_a_full_run_and_installed_when_asked(home, frozen_clock) -> None:
    services: list[Path] = []
    answers = list(PLAIN)
    answers[-1] = "y"
    code, keys = first_run(home, frozen_clock, *answers, installed=services)
    assert code == 0, keys.screen
    assert services == [home]
    assert "Tiffin is running in the background" in keys.screen


def test_a_home_folder_named_by_the_person_is_used(tmp_path, frozen_clock) -> None:
    chosen = tmp_path / "chosen"
    keys = Keyboard([str(chosen), *PLAIN])
    environ = {"APPDATA": str(tmp_path / "appdata"), "XDG_DATA_HOME": str(tmp_path / "xdg")}
    code = setup(["setup"], deps(World(sent=[OWNER_CODE]), keys, frozen_clock, environ=environ))
    assert code == 0, keys.screen
    assert (chosen / "tiffin.toml").is_file()
    assert keys.screen.count("Step 1 of 8") == 1
    expected = (
        f'$env:TIFFIN_HOME="{chosen}"' if sys.platform == "win32" else f"TIFFIN_HOME='{chosen}'"
    )
    assert expected in keys.screen


def test_ctrl_c_stops_and_says_nothing_was_saved(home, frozen_clock) -> None:
    def interrupt(prompt: str) -> str:
        raise KeyboardInterrupt

    keys = Keyboard([])
    the_deps = deps(World(), keys, frozen_clock, environ={"TIFFIN_HOME": str(home)})
    the_deps.read = interrupt
    assert setup(["setup"], the_deps) == 130
    assert "Setup stopped. Nothing was saved." in keys.screen
    assert not (home / "tiffin.toml").exists()


def test_unattended_model_check_runs_only_when_asked_for(home, frozen_clock) -> None:
    probes: list[str] = []
    argv = [
        "--non-interactive",
        "--owner-name",
        "Alex",
        "--model-provider",
        "openai",
        "--web-mode",
        "off",
        "--pair",
        "later",
        "--no-catalogue",
    ]
    environ = {"TIFFIN_TELEGRAM_TOKEN": GOOD_TOKEN, "TIFFIN_MODEL_API_KEY": MODEL_KEY}
    code, keys = first_run(
        home,
        frozen_clock,
        argv=argv,
        environ=environ,
        probe=lambda app: probes.append(app.config.get("model.provider")),
    )
    assert code == 0, keys.screen
    assert probes == [], "no request without --model-test yes"
    saved = (home / "secrets" / "model-api-key.txt").read_text(encoding="utf-8").strip()
    assert saved == MODEL_KEY, "a key in a plain variable is kept, as `tiffin init` did"
    code, keys = rerun(
        home,
        frozen_clock,
        argv=["--non-interactive", "--model-provider", "anthropic", "--model-test", "yes"],
        probe=lambda app: probes.append(app.config.get("model.provider")),
    )
    assert code == 0, keys.screen
    assert probes == ["anthropic"]
    assert re.search(r"model +answered during setup", keys.screen)
    assert_no_secret_shown(keys.screen)


@pytest.mark.parametrize("word", ["London", "new york", "europe/paris"])
def test_time_zones_are_found_from_a_plain_name(word: str) -> None:
    found = wizard_module.check_timezone(word)
    assert found in ("Europe/London", "America/New_York", "Europe/Paris")


def test_a_time_zone_that_does_not_exist_is_refused() -> None:
    with pytest.raises(wizard_module.Invalid, match="not a time zone"):
        wizard_module.check_timezone("Atlantis")
