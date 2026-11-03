"""Wiring: config + secrets -> DB, Telegram API, outbox, registry, handlers. One App per process."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from threading import Lock
from typing import TYPE_CHECKING, Any

from . import __version__, log
from .clock import DEFAULT_CLOCK, Clock
from .config import Config, load
from .db import Db
from .envfile import with_file_values
from .http import Transport, urllib_transport
from .persons import PersonRegistry
from .secrets import Secrets
from .telegram.api import TelegramApi
from .telegram.handlers import Handlers
from .telegram.outbox import Outbox

if TYPE_CHECKING:
    from .model.gateway import Backend, Gateway

logger = log.get("tiffin.app")


GatewayFactory = Callable[["App", Db], "Gateway | None"]


class App:
    def __init__(
        self,
        config: Config,
        secrets: Secrets,
        *,
        clock: Clock = DEFAULT_CLOCK,
        transport: Transport = urllib_transport,
        gateway_factory: GatewayFactory | None = None,
    ):
        self.config = config
        self.secrets = secrets
        self.clock = (
            Clock(str(config.get("env.tz")))
            if clock is DEFAULT_CLOCK and config.get("env.tz") != "Europe/London"
            else clock
        )
        self.transport = transport
        from .food import shopping  # noqa: PLC0415 (food imports the db layer)

        shopping.set_count_on_home(bool(config.get("shopping.count_on_home", False)))
        self._api: TelegramApi | None = None
        self.gateway_factory = gateway_factory
        self._provider: Backend | None = None
        self._provider_ready = False
        self._provider_lock = Lock()
        self.version = __version__
        #: where a model build runs. The Runtime sets it to the person's own "builds" lane, so a
        #: menu build - usually two to four minutes of model calls - never holds that person's
        #: other messages. None runs it in the caller.
        self.background: Callable[[int, Callable[[Db], object]], None] | None = None

    def run_build(self, person_id: int, fn: Callable[[Db], object], db: Db) -> bool:
        """`fn` on the person's build lane when there is one (True: it runs later, on its own
        connection), or now on `db` (False)."""
        if self.background is None:
            fn(db)
            return False
        self.background(person_id, fn)
        return True

    @classmethod
    def from_env(
        cls,
        env: str | None = None,
        *,
        environ: Mapping[str, str] | None = None,
        clock: Clock = DEFAULT_CLOCK,
        transport: Transport = urllib_transport,
    ) -> App:
        config = load(env, environ=environ)
        actual_clock = Clock(str(config.get("env.tz"))) if clock is DEFAULT_CLOCK else clock
        return cls(
            config,
            Secrets(config.paths.secrets, with_file_values(environ)),
            clock=actual_clock,
            transport=transport,
        )

    # -- factories (one Db per thread) -----------------------------------------------------
    def open_db(self) -> Db:
        return Db.open(self.config.paths.db, clock=self.clock)

    @property
    def admin_chat_id(self) -> int | None:
        return self.config.person_chat_id("owner")

    def api(self) -> TelegramApi:
        if self._api is None:
            tg = self.config.section("telegram")
            self._api = TelegramApi(
                self.secrets.telegram_token(self.config.env),
                transport=self.transport,
                clock=self.clock,
                poll_timeout_s=int(tg.get("poll_timeout_s", 50)),
                http_timeout_extra_s=int(tg.get("http_timeout_extra_s", 15)),
                max_chars=int(tg.get("max_chars", 4000)),
            )
        return self._api

    def registry(self, db: Db) -> PersonRegistry:
        return PersonRegistry(db, self.config, clock=self.clock)

    def outbox(self, db: Db) -> Outbox:
        return Outbox(
            self.api(),
            db,
            env=self.config.env,
            admin_chat_id=self.admin_chat_id,
            state_dir=self.config.paths.state,
            clock=self.clock,
            alert_cooldown_min=int(self.config.get("telegram.alert_cooldown_min", 180)),
            link_signer=self._page_link_signer(db),
        )

    def _page_link_signer(self, db: Db) -> Any:
        """Links to the page in a message sign the recipient in (web.identity.sign_page_links);
        None when the page is off or has no public address, so nothing is issued for nothing."""
        if not self.config.get("web.enabled", True) or not str(self.config.get("web.origin", "")):
            return None
        from .web.identity import sign_page_links  # noqa: PLC0415 (web imports this module)

        return lambda person, text: sign_page_links(self, db, person, text)

    def gateway(self, db: Db) -> Gateway | None:
        """Bind a provider to this caller's database and consent gate."""
        if self.gateway_factory is not None:
            return self.gateway_factory(self, db)

        from .model.gateway import Gateway as RealGateway  # noqa: PLC0415
        from .model.provider import configured_provider  # noqa: PLC0415

        with self._provider_lock:
            if not self._provider_ready:
                try:
                    self._provider = configured_provider(
                        self.config, self.secrets, self.transport, clock=self.clock
                    )
                except Exception as e:
                    logger.warning(
                        "model provider unavailable type=%s",
                        type(e).__name__,
                        extra={"event": "no_model"},
                    )
                self._provider_ready = True
        return RealGateway.from_config(self.config, self._provider, db, clock=self.clock)

    def handlers(self, db: Db) -> Handlers:
        from . import onboarding  # noqa: PLC0415
        from .telegram import (  # noqa: PLC0415
            batchflow,
            chat,
            choiceflow,
            dayflow,
            foodflow,
            intents,
            interview,
            linkflow,
            measures,
            menuflow,
            notesflow,
            packflow,
            settings,
            weekflow,
        )

        outbox = self.outbox(db)
        registry = self.registry(db)
        h = Handlers(
            db, outbox, registry, clock=self.clock, env=self.config.env, version=self.version
        )
        h.model_provider = self.config.model_scope()
        from .telegram.handlers import install_web_command  # noqa: PLC0415

        install_web_command(h, self)
        onboarding.install(
            h, onboarding.Onboarding(db, outbox, registry, self.config, clock=self.clock)
        )
        dayflow.install(h, self.config, clock=self.clock)
        measures.install(h, clock=self.clock)
        settings.install(h, clock=self.clock)
        intents.install(h, clock=self.clock)
        menuflow.install(h, self, clock=self.clock)
        foodflow.install(h, config=self.config, clock=self.clock, app=self)
        batchflow.install(h, clock=self.clock)
        choiceflow.install(h, self, clock=self.clock)
        weekflow.install(h, clock=self.clock)
        notesflow.install(h, clock=self.clock)
        packflow.install(h)
        linkflow.install(h)
        interview.install(h, clock=self.clock)
        chat.install(h, lambda: self.gateway(db), clock=self.clock, config=self.config)
        return h

    # -- startup -----------------------------------------------------------------------
    def setup_logging(self, *, console: bool = True) -> None:
        self.config.paths.ensure()
        log.setup(self.config.paths.logs, console=console, clock=self.clock)

    def bootstrap(self) -> Db:
        """Ensure directories, migrate the DB, create persons from config. Returns a Db."""
        self.config.paths.ensure()
        db = self.open_db()
        self.registry(db).ensure_from_config()
        return db
