"""The `tiffin run` process: poller thread, one worker per person (+ a system worker),
scheduler thread, heartbeat.

Every thread owns its own Db connection. Inbound messages and due job slots are handed to
the person's worker queue, so a slow model call never blocks the poll loop and one person's
work never blocks the other's. The web app hooks into the same Runtime (`add_thread`).
"""

from __future__ import annotations

import os
import queue
import signal
import sys
import threading
import time
from collections.abc import Callable
from typing import Any

from . import building, ledger, log
from .app import App
from .db import Db
from .errors import TelegramConflict
from .heartbeat import Heartbeat, PidFile
from .jobs import all_jobs
from .scheduler import Job, Scheduler
from .telegram.poller import CONFLICT_GRACE_S, OFFSET_KEY, Inbound, Poller

logger = log.get("tiffin.runtime")
SYSTEM = 0
#: how long a stop waits for work in flight before the process ends. Compose's
#: `stop_grace_period` must be longer, or the container is killed first.
STOP_GRACE_S = 20.0

WorkItem = Inbound | Callable[[Db], object] | None


#: commands that are a SELECT and a render, and write nothing but their own reply.
#:
#: One queue per person and strict FIFO meant every one of these sat behind whatever was in
#: front of it, and what is in front of it is a six-minute menu build. From the phone, a tiffin
#: thinking and a tiffin dead look identical - which is the reason /cook answers "this takes a
#: minute" at all, and nothing else could get a word in while it did. These go down their own
#: lane, so a status read is answered while the build runs.
#:
#: Bare forms only. `/set evening 21:00` writes, and `/cook` is the
#: thing being waited for; any of those keeps the ordered lane, where a person's handlers still
#: run one at a time and the cooldown stamp still means what it says.
LIGHT_COMMANDS = frozenset({"/today", "/menu", "/shopping", "/week", "/help", "/ping"})


def is_light(inbound: Inbound) -> bool:
    """True for a bare read-only command, which may answer out of order."""
    if inbound.kind not in ("text", "command") or not inbound.text:
        return False
    said = inbound.text.strip()
    return said.split("@")[0].casefold() in LIGHT_COMMANDS


class Worker(threading.Thread):
    def __init__(self, app: App, person_id: int, stop: threading.Event, *, lane: str = "main"):
        super().__init__(name=f"worker-{person_id}-{lane}", daemon=True)
        self.app = app
        self.person_id = person_id
        self.stop_event = stop
        self.queue: queue.Queue[WorkItem] = queue.Queue()
        self.handled = 0

    def run(self) -> None:
        db = self.app.open_db()
        handlers = self.app.handlers(db)
        try:
            while not self.stop_event.is_set():
                try:
                    item = self.queue.get(timeout=1.0)
                except queue.Empty:
                    continue
                if item is None:
                    break
                if isinstance(item, Inbound):
                    self._handle(db, handlers, item)
                else:
                    self._run_job(db, item)
        finally:
            db.close()

    def _handle(self, db: Db, handlers, item: Inbound) -> None:
        with log.context(person_id=item.person.id):
            try:
                outcome = handlers.handle(item)
                detail = None
            except Exception as e:
                logger.error(
                    "handler failed type=%s", type(e).__name__, extra={"event": "handler_error"}
                )
                outcome, detail = "error", type(e).__name__
                self._say_it_broke(handlers, item, detail)
            db.insert(
                "inbound_outcomes", message_in_id=item.message_in_id, outcome=outcome, detail=detail
            )
            self.handled += 1

    def _say_it_broke(self, handlers, item: Inbound, detail: str) -> None:
        """Notify the person and owner when a handler fails.

        Sending is best-effort so another failure cannot stop the worker. The message avoids
        promising that nothing was saved because a handler may write before it sends.
        """
        try:
            handlers.outbox.reply(
                item.person,
                "Something went wrong handling that and I could not finish it. "
                "Send /today to see where the day stands before you try again.",
            )
        except Exception:
            logger.exception("could not tell the person", extra={"event": "handler_error_reply"})
        try:
            handlers.outbox.alert(handlers.registry.admin(), f"handler_error:{item.kind}", detail)
        except Exception:
            logger.exception("could not alert the admin", extra={"event": "handler_error_alert"})

    def _run_job(self, db: Db, fn: Callable[[Db], object]) -> None:
        try:
            fn(db)
        except Exception:
            logger.exception("job callable failed", extra={"event": "job_error"})
        self.handled += 1


class Runtime:
    def __init__(
        self,
        app: App,
        *,
        heartbeat_s: int = 30,
        tick_s: int = 30,
        jobs: list[Job] | None = None,
        serve_web: bool = True,
        conflict_grace_s: float = CONFLICT_GRACE_S,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        self.app = app
        self.heartbeat_s = heartbeat_s
        self.tick_s = tick_s
        self.serve_web = serve_web
        self.conflict_grace_s = conflict_grace_s
        self.sleeper = sleeper
        self.stop_event = threading.Event()
        #: person_id for the ordered lane, (person_id, lane) for the read-only one
        self.workers: dict[Any, Worker] = {}
        self._lock = threading.Lock()
        self.threads: list[threading.Thread] = []
        self.exit_code = 0
        #: set by the poller once Telegram has answered (or failed without a 409). The
        #: scheduler waits for it, so a process whose token another poller still holds
        #: never starts a job of its own.
        self.poll_ready = threading.Event()
        self.heartbeat = Heartbeat(app.config.paths.state, app.clock)
        self.pidfile = PidFile(app.config.paths.state, app.clock)
        self.poller: Poller | None = None
        if jobs is None:
            jobs = all_jobs(app.config)
        self.scheduler = Scheduler(app, jobs, self.dispatch_job, tick_s=tick_s)
        # a menu build asked for is minutes of model calls: it runs on the person's own build
        # lane, so their check-in answer, a swap or a /confirm is never queued behind it. The
        # scheduled build still runs as its job, and the `building` mark keeps the two apart
        app.background = self.dispatch_build

    # -- dispatch ----------------------------------------------------------------------
    def _worker(self, person_id: int, *, lane: str = "main") -> Worker:
        with self._lock:
            key = person_id if lane == "main" else (person_id, lane)
            w = self.workers.get(key)
            if w is None:
                w = Worker(self.app, person_id, self.stop_event, lane=lane)
                self.workers[key] = w
                w.start()
        return w

    def dispatch(self, inbound: Inbound) -> None:
        lane = "reads" if is_light(inbound) else "main"
        self._worker(inbound.person.id, lane=lane).queue.put(inbound)

    def dispatch_job(self, person_id: int, fn: Callable[[Db], object]) -> None:
        self._worker(person_id).queue.put(fn)

    def dispatch_build(self, person_id: int, fn: Callable[[Db], object]) -> None:
        """A model build, on the person's build lane: one build at a time per person, in order,
        and nothing else of theirs waits for it."""
        self._worker(person_id, lane="builds").queue.put(fn)

    # -- threads -----------------------------------------------------------------------
    def add_thread(self, name: str, target: Callable[[threading.Event], None]) -> threading.Thread:
        t = threading.Thread(name=name, target=self._guarded, args=(name, target), daemon=True)
        self.threads.append(t)
        return t

    def _guarded(self, name: str, target: Callable[[threading.Event], None]) -> None:
        try:
            target(self.stop_event)
        except TelegramConflict as e:
            if self.stop_event.is_set():
                return  # our own poll, ended by the stop
            logger.error(
                "%s conflict type=%s", name, type(e).__name__, extra={"event": "telegram_conflict"}
            )
            self.exit_code = 3
            self.stop_event.set()
        except Exception:
            logger.exception("%s crashed", name, extra={"event": "thread_crash"})
            self.exit_code = 1
            self.stop_event.set()

    def _poll_loop(self, stop: threading.Event) -> None:
        db = self.app.open_db()
        registry = self.app.registry(db)
        cfg = self.app.config
        allowed = None if cfg.is_prod else frozenset(x for x in [self.app.admin_chat_id] if x)
        self.poller = Poller(
            self.app.api(),
            db,
            registry,
            self.dispatch,
            clock=self.app.clock,
            allowed_chat_ids=allowed,
            pairing_enabled=cfg.is_prod,
            ready=self.poll_ready,
            conflict_grace_s=self.conflict_grace_s,
        )
        try:
            # the "/" menu in Telegram, from the same table /help renders - once per start, and
            # never a reason for the bot not to run
            from .telegram.handlers import bot_commands  # noqa: PLC0415

            self.app.api().set_my_commands(bot_commands(self.app.handlers(db)))
        except Exception:
            logger.warning("setMyCommands failed", extra={"event": "commands_not_set"})
        try:
            self.poller.run_forever(stop)
        finally:
            db.close()

    def _heartbeat_loop(self, stop: threading.Event) -> None:
        while not stop.is_set():
            self.heartbeat.beat()
            stop.wait(self.heartbeat_s)

    def _scheduler_loop(self, stop: threading.Event) -> None:
        while not stop.is_set() and not self.poll_ready.wait(0.05):
            pass
        if not stop.is_set():
            self.scheduler.run_forever(stop)

    def _web_loop(self, stop: threading.Event) -> None:
        from .web.app import serve  # noqa: PLC0415 (fastapi + uvicorn are heavy)

        serve(self.app, stop, dispatch=self.dispatch_job)

    # -- lifecycle -----------------------------------------------------------------------
    def start(self) -> None:
        self.pidfile.write(exe=os.path.abspath(sys.executable))
        self.heartbeat.beat()
        db = self.app.open_db()
        try:
            # a restart is exactly when a run gets abandoned mid-flight
            ledger.close_abandoned(db, clock=self.app.clock)
        except Exception:
            logger.exception("abandoned runs not closed", extra={"event": "abandoned_error"})
        try:
            self._forget_builds(db)
        except Exception:
            logger.exception("build marks not cleared", extra={"event": "build_error"})
        finally:
            db.close()
        self.add_thread("poller", self._poll_loop)
        self.add_thread("heartbeat", self._heartbeat_loop)
        self.add_thread("scheduler", self._scheduler_loop)
        if self.serve_web and self.app.config.get("web.enabled", True):
            self.add_thread("web", self._web_loop)
        for t in self.threads:
            t.start()
        logger.info(
            "runtime started env=%s version=%s jobs=%d",
            self.app.config.env,
            self.app.version,
            len(self.scheduler.jobs),
            extra={"event": "start"},
        )

    def _forget_builds(self, db: Db) -> None:
        """No build survives a restart, so none is left marked as running: /cook and the page
        said for a quarter of an hour that a menu nobody was building would come. Whoever asked
        for the one that died is told; a scheduled one is the run ledger's, and retried."""
        registry = self.app.registry(db)
        for person_id, was in building.forget_all(db, clock=self.app.clock):
            if was["asked"] and was["kind"] == building.MENU:
                # the /cook cooldown stamped as it started says a menu is coming too: the bare
                # /cook the message below tells them to send was refused as "still working" on
                # the build that died
                db.execute("DELETE FROM bot_state WHERE k = ?", (f"cook_asked:{person_id}",))
            person = registry.by_id(person_id)
            if person is None or not was["asked"]:
                continue
            said = building.interrupted(was)
            if was["kind"] == building.MENU:
                building.say(db, person_id, was["id"], said)
            self.app.outbox(db).reply(person, said)

    def request_stop(self) -> None:
        """Ask the runtime to stop; safe in a signal handler (it only sets an event)."""
        self.stop_event.set()

    def _release_poll(self) -> None:
        """End the long poll Telegram still holds open for us, so the next process (a restart, a
        backup's stop and start) is not met with a 409. Best effort."""
        if self.poller is None:
            return
        try:
            db = self.app.open_db()
            try:
                offset = int(db.state_get(OFFSET_KEY, "0") or 0)
            finally:
                db.close()
            self.app.api().release_poll(offset)
        except Exception:
            logger.warning("poll not released", extra={"event": "release_poll_failed"})

    def stop(self) -> None:
        """Stop cleanly: no new work, the poll released, work in flight given STOP_GRACE_S.

        What a job still running at the end leaves: its run_ledger row stays 'running' (the
        scheduler retries the slot an hour later if its window is open; a message already sent is
        never sent twice) and a build someone asked for keeps its mark, which the next start
        reports to them as interrupted."""
        self.stop_event.set()
        for w in self.workers.values():
            w.queue.put(None)
        self._release_poll()
        deadline = time.monotonic() + STOP_GRACE_S
        alive: list[str] = []
        for t in self.threads + list(self.workers.values()):
            t.join(timeout=max(0.0, deadline - time.monotonic()))
            if t.is_alive():
                alive.append(t.name)
        if alive:
            logger.warning(
                "stopped with %d thread(s) still busy: %s",
                len(alive),
                ", ".join(alive),
                extra={"event": "stop_with_work"},
            )
        self.pidfile.remove()
        logger.info("runtime stopped exit=%s", self.exit_code, extra={"event": "stop"})

    def run(self) -> int:
        self.start()
        try:
            while not self.stop_event.is_set():
                self.stop_event.wait(1.0)
        except KeyboardInterrupt:
            logger.info("interrupted", extra={"event": "interrupt"})
        finally:
            self.stop()
        return self.exit_code


def install_signal_handlers(rt: Runtime) -> None:
    """SIGTERM (docker stop), SIGINT and, on Windows, SIGBREAK stop the runtime cleanly.

    Python as PID 1 gets no default action for SIGTERM, so without a handler `docker stop`
    waits its whole grace period and then kills the process mid-job. The handler only sets the
    stop event, which is safe from a signal handler, and works the same under an init."""

    def handler(_signum: int, _frame: object) -> None:
        rt.request_stop()

    for name in ("SIGTERM", "SIGINT", "SIGBREAK"):
        number = getattr(signal, name, None)
        if number is not None:
            try:
                signal.signal(number, handler)
            except ValueError:  # not the main thread (a test): nothing to install
                return
