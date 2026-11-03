"""What setup knows: the answers so far, laid over what is already saved.

Nothing here writes a file. `State.config()` checks the answers together with tiffin.toml and the
packaged defaults, and `State.app()` builds a Tiffin from them for the checks setup makes on the
way (pairing a chat, asking the model), before anything is saved.
"""

from __future__ import annotations

import argparse
import copy
import os
import shutil
import sqlite3
import time
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

from ..app import App
from ..clock import Clock
from ..config import REPO_ROOT, Config, deep_merge, load
from ..db import Db
from ..envfile import with_file_values
from ..errors import ConfigError
from ..http import Transport, urllib_transport
from ..secrets import Secrets
from ..term import Style
from .ask import _read_hidden

#: how long setup waits for a pairing code to arrive
PAIR_WAIT_S = 300.0


@dataclass
class Deps:
    """Everything setup reaches outside itself, so tests can stand in for each part."""

    transport: Transport = urllib_transport
    environ: Mapping[str, str] | None = None
    read: Callable[[str], str] = input
    read_secret: Callable[[str], str] = _read_hidden
    out: TextIO | None = None
    style: Style | None = None
    clock: Clock | None = None
    monotonic: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    which: Callable[[str], str | None] = shutil.which
    container: Callable[[], str | None] | None = None
    install_service: Callable[[Path, str | None], None] | None = None
    probe_model: Callable[[App], None] | None = None
    pair_wait_s: float = PAIR_WAIT_S
    #: columns to wrap at; None measures the terminal
    width: int | None = None


@dataclass
class Saved:
    """What the database already holds: names, paired chats and the size of the food list."""

    names: dict[str, str] = field(default_factory=dict)
    paired: dict[str, bool] = field(default_factory=dict)
    foods: int = 0


def _read_saved(path: Path) -> Saved:
    saved = Saved()
    if not path.is_file():
        return saved
    try:
        with Db(path, readonly=True) as db:
            for row in db.all("SELECT id, slug, display_name FROM persons WHERE erased_at IS NULL"):
                name = db.scalar(
                    "SELECT name FROM person_names WHERE person_id = ? ORDER BY id DESC LIMIT 1",
                    (row["id"],),
                )
                saved.names[str(row["slug"])] = str(name or row["display_name"] or "")
                saved.paired[str(row["slug"])] = (
                    db.scalar(
                        "SELECT COUNT(*) FROM telegram_identities WHERE person_id = ?",
                        (row["id"],),
                    )
                    > 0
                )
            saved.foods = int(db.scalar("SELECT COUNT(*) FROM ingredients") or 0)
    except sqlite3.Error:
        pass
    return saved


def _put(node: dict[str, Any], dotted: str, value: Any) -> None:
    *tables, key = dotted.split(".")
    for part in tables:
        node = node.setdefault(part, {})
    node[key] = value


def _drop(node: dict[str, Any], dotted: str) -> None:
    *tables, key = dotted.split(".")
    trail = []
    for part in tables:
        if part not in node:
            return
        trail.append((node, part))
        node = node[part]
    node.pop(key, None)
    for parent, part in reversed(trail):
        if not parent[part]:
            del parent[part]


class State:
    """The answers so far, laid over what is saved. Nothing here touches a file until save."""

    def __init__(self, home: Path, args: argparse.Namespace, deps: Deps, style: Style) -> None:
        self.args = args
        self.deps = deps
        self.style = style
        self.plain_env = dict(os.environ if deps.environ is None else deps.environ)
        self.environ = with_file_values(self.plain_env)
        self.defaults = tomllib.loads(
            (REPO_ROOT / "config" / "defaults.toml").read_text(encoding="utf-8")
        )
        self.overlay: dict[str, Any] = {}
        self.secrets: dict[str, str] = {}
        self.names: dict[str, str] = {}
        self.rehome(home)
        #: every part is being set up (a first run, or "all of them"): the food list and the
        #: background service are offered at the end only then
        self.full = False
        env = args.env or self.environ.get("TIFFIN_ENV") or self.get("env.default") or "prod"
        if env not in ("dev", "prod"):
            raise ConfigError(f"TIFFIN_ENV must be dev or prod, not {env!r}")
        self.env = str(env)
        self.container = (deps.container or _container_runtime)()
        self.bot: str | None = None
        self.model_tested: bool | None = None
        self.codes: dict[str, str] = {}
        self.service_installed = False
        #: the Telegram step ran: an unattended run pairs the owner once the settings are saved
        self.pairing_due = False
        #: chats paired during this run (kept even when setup stops before saving)
        self.paired_now: set[str] = set()

    def rehome(self, home: Path) -> None:
        """Read what is saved in `home`. Only before the first answer, which goes with it."""
        self.home = home
        self.toml_path = home / "tiffin.toml"
        self.first_run = not self.toml_path.is_file()
        #: a first run, still true once its settings are saved
        self.fresh = self.first_run
        #: the parts set up in this run (not skipped)
        self.ran: set[str] = set()
        try:
            self.file_data: dict[str, Any] = (
                tomllib.loads(self.toml_path.read_text(encoding="utf-8-sig"))
                if not self.first_run
                else {}
            )
        except (OSError, tomllib.TOMLDecodeError) as e:
            raise ConfigError(
                f"{self.toml_path} cannot be read ({e}); fix it or move it aside, then run "
                "tiffin setup again"
            ) from None
        self.saved = _read_saved(home / "tiffin.db")

    # -- settings --------------------------------------------------------------------------
    def get(self, dotted: str, *, after: bool = True) -> Any:
        node: Any = deep_merge(self.defaults, self.file_data)
        if after:
            node = deep_merge(node, self.overlay)
        for part in dotted.split("."):
            node = node.get(part) if isinstance(node, Mapping) else None
        return node

    def set(self, dotted: str, value: Any) -> None:
        """Record an answer. On a re-run an answer equal to what is saved is not a change."""
        if not self.first_run and self.get(dotted, after=False) == value:
            _drop(self.overlay, dotted)
        else:
            _put(self.overlay, dotted, value)

    def config(self) -> Config:
        return load(self.env, root=self.home, environ=self.environ, extra=self.overlay)

    def snapshot(self) -> tuple[Any, ...]:
        return copy.deepcopy((self.overlay, self.secrets, self.names))

    def restore(self, snap: tuple[Any, ...]) -> None:
        self.overlay, self.secrets, self.names = copy.deepcopy(snap)

    # -- secrets ---------------------------------------------------------------------------
    @property
    def token_name(self) -> str:
        return "telegram_token" if self.env == "prod" else "telegram_token_dev"

    def env_secret(self, name: str) -> str | None:
        """The environment variable that supplies this secret, if one does. Tiffin reads it
        before any file, so setup neither asks for another nor replaces it."""
        var = f"TIFFIN_{name.upper()}"
        if self.plain_env.get(var, "").strip():
            return var
        if self.plain_env.get(var + "_FILE", "").strip() and self.environ.get(var, "").strip():
            return var + "_FILE"
        return None

    def saved_secret(self, name: str) -> str | None:
        return Secrets(self.home / "secrets", {}).optional(name)

    def secret(self, name: str, *, after: bool = True) -> str | None:
        """What Tiffin will use: an answer given now, else the environment, else the file."""
        if after and name in self.secrets:
            return self.secrets[name]
        return Secrets(self.home / "secrets", self.environ).optional(name)

    def secrets_view(self) -> Secrets:
        environ = dict(self.environ)
        for name, value in self.secrets.items():
            environ[f"TIFFIN_{name.upper()}"] = value
        return Secrets(self.home / "secrets", environ)

    def app(self) -> App:
        config = self.config()
        clock = self.deps.clock or Clock(str(config.get("env.tz")))
        return App(config, self.secrets_view(), clock=clock, transport=self.deps.transport)

    def two(self, *, after: bool = True) -> bool:
        return bool(self.get("household.partner_expected", after=after)) or bool(
            self.saved.paired.get("partner")
        )

    def name(self, slug: str, *, after: bool = True) -> str:
        if after and slug in self.names:
            return self.names[slug]
        return self.saved.names.get(slug) or str(
            self.get(f"persons.{slug}.display_name", after=after) or ""
        )


def _container_runtime() -> str | None:
    from ..doctor import container_runtime  # noqa: PLC0415 (doctor pulls in every job)

    return container_runtime()
