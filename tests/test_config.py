from __future__ import annotations

from pathlib import Path

import pytest

from tiffin import config as cfg
from tiffin.errors import ConfigError
from tiffin.secrets import Secrets


def test_defaults_load_under_tmp_home(tmp_path: Path) -> None:
    c = cfg.load(root=tmp_path, environ={})
    assert c.env == "prod" and c.is_prod
    assert c.paths.db == tmp_path / "tiffin.db"
    assert c.paths.state == tmp_path / "state"
    assert c.web_port == 7788
    assert c.person_chat_id("owner") is None
    assert c.person_chat_id("partner") is None
    assert c.budget("chat").turns == 8
    assert c.get("guardrails.max_loss_kg_week") == 0.8
    assert c.get("shop.retailers") == [] and c.get("shop.retailer") == ""
    assert c.get("env.country") == "GB" and c.get("units.currency") == "GBP"


def test_overlay_writer_escapes_control_characters(tmp_path: Path) -> None:
    path = tmp_path / "tiffin.toml"
    cfg.update_override(path, {"model": {"model": 'line one\n"line two"'}})
    assert cfg.load(root=tmp_path, environ={}).get("model.model") == 'line one\n"line two"'


def test_bad_schedule_time_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "tiffin.toml").write_text('[schedule]\nbackup = "25:00"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match=r"schedule\.backup"):
        cfg.load(root=tmp_path, environ={})


def test_legacy_single_retailer_override_still_works(tmp_path: Path) -> None:
    from tiffin.jobs import shop_refresh

    (tmp_path / "tiffin.toml").write_text('[shop]\nretailer = "tesco"\n', encoding="utf-8")
    conf = cfg.load(root=tmp_path, environ={})
    assert shop_refresh.retailer_order(conf) == ["tesco"]


def test_prod_paths(tmp_path: Path) -> None:
    c = cfg.load(root=tmp_path, environ={"TIFFIN_ENV": "prod"})
    assert c.is_prod
    assert c.paths.db == tmp_path / "tiffin.db"
    assert c.web_port == 7788


def test_override_file_deep_merges(tmp_path: Path) -> None:
    (tmp_path / "tiffin.toml").write_text(
        '[persons.partner]\ndisplay_name = "Sam"\nchat_id = 1000002\n[web]\nport_dev = 7790\n',
        encoding="utf-8",
    )
    c = cfg.load("dev", root=tmp_path, environ={})
    assert c.person_chat_id("partner") == 1000002
    assert c.person_chat_id("owner") is None  # untouched sibling table
    assert c.web_port == 7790
    assert c.get("web.port_prod") == 7788


def test_model_provider_config_and_secret_boundary(tmp_path: Path) -> None:
    overlay = tmp_path / "tiffin.toml"
    overlay.write_text(
        '[model]\nprovider = "openai-compatible"\nmodel = "local-example"\n'
        'base_url = "http://localhost:11434/v1"\n[model.tasks]\nintent = "smaller-example"\n',
        encoding="utf-8",
    )
    c = cfg.load(root=tmp_path, environ={})
    assert c.get("model.provider") == "openai-compatible"
    assert c.get("model.tasks.intent") == "smaller-example"
    old_scope = c.model_scope()
    assert old_scope.startswith("openai-compatible@")
    overlay.write_text(
        '[model]\nprovider = "openai-compatible"\nmodel = "local-example"\n'
        'base_url = "http://localhost:1234/v1"\n',
        encoding="utf-8",
    )
    assert cfg.load(root=tmp_path, environ={}).model_scope() != old_scope
    overlay.write_text('[model]\napi_key = "never-here"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match=r"model\.api_key"):
        cfg.load(root=tmp_path, environ={})


def test_bad_env_and_missing_key(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        cfg.load(root=tmp_path, environ={"TIFFIN_ENV": "staging"})
    c = cfg.load(root=tmp_path, environ={})
    with pytest.raises(ConfigError):
        c.get("nope.nothing")
    assert c.get("nope.nothing", 7) == 7


def test_invalid_override_is_a_config_error(tmp_path: Path) -> None:
    (tmp_path / "tiffin.toml").write_text("this is = not toml [", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid"):
        cfg.load(root=tmp_path, environ={})


def test_ensure_creates_env_dirs(tmp_path: Path) -> None:
    c = cfg.load(root=tmp_path, environ={})
    c.paths.ensure()
    for p in (
        c.paths.state,
        c.paths.logs,
        c.paths.backups,
        c.paths.claude_cwd,
    ):
        assert p.is_dir()


def test_home_environment_and_secret_environment(tmp_path: Path) -> None:
    home = tmp_path / "instance"
    config = cfg.load(environ={"TIFFIN_HOME": str(home)})
    assert config.paths.db == home / "tiffin.db"
    assert config.paths.secrets == home / "secrets"
    assert config.paths.icu_streams == home / "archives" / "icu" / "streams"
    secrets = Secrets(config.paths.secrets, {"TIFFIN_TELEGRAM_TOKEN": "example-token"})
    assert secrets.telegram_token("prod") == "example-token"


def test_local_config_reports_invalid_values(tmp_path: Path) -> None:
    (tmp_path / "tiffin.toml").write_text("[web]\nport_prod = 90000\n", encoding="utf-8")
    with pytest.raises(ConfigError, match=r"web\.port_prod"):
        cfg.load(root=tmp_path, environ={})


@pytest.mark.parametrize(
    "root_path", ["//other.example", "/tiffin/../admin", "/tiffin//x", "/tiffin/%2e%2e"]
)
def test_web_mount_rejects_ambiguous_paths(tmp_path: Path, root_path: str) -> None:
    (tmp_path / "tiffin.toml").write_text(f'[web]\nroot_path = "{root_path}"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match=r"web\.root_path"):
        cfg.load(root=tmp_path, environ={})


@pytest.mark.parametrize("field", ["web.origin", "model.base_url"])
def test_invalid_url_reports_config_error(tmp_path: Path, field: str) -> None:
    section, key = field.split(".")
    (tmp_path / "tiffin.toml").write_text(f'[{section}]\n{key} = "http://[bad"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match=field.replace(".", r"\.")):
        cfg.load(root=tmp_path, environ={})


def test_dump_toml_round_trips_the_shapes_a_box_overlay_holds() -> None:
    import tomllib

    from tiffin.config import dump_toml

    data = {
        "top": 1,
        "env": {"default": "dev", "tz": "Europe/London"},
        "budgets": {"chat": [180, 8, 0.6]},
        "persons": {"owner": {"chat_id": 1000001, "display_name": "Alex"}},
        "web": {"secure_cookies": True},
    }
    assert tomllib.loads(dump_toml(data)) == data


def test_dump_toml_refuses_what_it_cannot_represent() -> None:
    from tiffin.config import dump_toml
    from tiffin.errors import ConfigError

    with pytest.raises(ConfigError, match="cannot write"):
        dump_toml({"when": __import__("datetime").date(2026, 9, 13)})


def test_update_override_merges_rather_than_replacing(tmp_path) -> None:
    import tomllib

    from tiffin.config import update_override

    path = tmp_path / "tiffin.toml"
    update_override(path, {"persons": {"owner": {"display_name": "Alex"}}})
    update_override(path, {"persons": {"partner": {"display_name": "Sam"}}})
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    assert data["persons"]["owner"]["display_name"] == "Alex"
    assert data["persons"]["partner"]["display_name"] == "Sam"


HAND_WRITTEN = """\
# My Tiffin, on the small server
[web]
host = "0.0.0.0"   # the proxy is another container
root_path = "/tiffin"

# where people sign in
[persons.owner]
display_name = "Alex"
"""


def test_edit_override_changes_only_the_lines_it_names(tmp_path) -> None:
    """`tiffin setup` keeps a person's own notes: comments, blank lines and the order stay."""
    from tiffin.config import edit_override

    path = tmp_path / "tiffin.toml"
    path.write_text(HAND_WRITTEN, encoding="utf-8")
    edit_override(path, {"web": {"root_path": "", "origin": "https://tiffin.example.com"}})
    assert path.read_text(encoding="utf-8") == HAND_WRITTEN.replace(
        'root_path = "/tiffin"', 'root_path = ""\norigin = "https://tiffin.example.com"'
    )


def test_edit_override_keeps_a_changed_line_s_comment_and_adds_new_tables(tmp_path) -> None:
    import tomllib

    from tiffin.config import edit_override

    path = tmp_path / "tiffin.toml"
    path.write_text(HAND_WRITTEN, encoding="utf-8")
    edit_override(path, {"web": {"host": "127.0.0.1"}, "model": {"provider": "none"}})
    text = path.read_text(encoding="utf-8")
    assert 'host = "127.0.0.1"  # the proxy is another container' in text
    assert text.endswith('\n[model]\nprovider = "none"\n')
    assert tomllib.loads(text)["persons"]["owner"]["display_name"] == "Alex"


@pytest.mark.parametrize(
    "awkward",
    [
        'web = { root_path = "/tiffin" }\n',  # an inline table
        '[shop]\nretailers = [\n  "tesco",\n]\n[web]\nroot_path = "/tiffin"\n',  # a long list
        '[web]\nroot_path = "/tiffin"\n[web.extra]\n[web]\n',  # a table twice: not TOML at all
    ],
)
def test_edit_override_writes_the_whole_file_when_a_line_edit_is_not_safe(
    tmp_path, awkward: str
) -> None:
    import tomllib

    from tiffin.config import edit_override

    path = tmp_path / "tiffin.toml"
    path.write_text(awkward, encoding="utf-8")
    try:
        before = tomllib.loads(awkward)
    except tomllib.TOMLDecodeError:
        with pytest.raises(ConfigError, match="config file invalid"):
            edit_override(path, {"web": {"root_path": ""}})
        assert path.read_text(encoding="utf-8") == awkward, "nothing written"
        return
    edit_override(path, {"web": {"root_path": "", "enabled": False}, "shop": {"retailers": []}})
    after = tomllib.loads(path.read_text(encoding="utf-8"))
    expected = cfg.deep_merge(
        before, {"web": {"root_path": "", "enabled": False}, "shop": {"retailers": []}}
    )
    assert after == expected


def test_edit_override_starts_a_new_file_with_its_header(tmp_path) -> None:
    from tiffin.config import edit_override

    path = tmp_path / "home" / "tiffin.toml"
    edit_override(path, {"env": {"tz": "Europe/London"}})
    assert path.read_text(encoding="utf-8") == (
        "# Local overrides for tiffin; packaged defaults hold the rest.\n"
        '[env]\ntz = "Europe/London"\n'
    )
