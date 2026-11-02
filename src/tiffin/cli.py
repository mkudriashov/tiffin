"""`tiffin` command line. Exit codes: 0 clean, 2 config error, 3 lock or poller conflict."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any

from . import __version__
from .errors import ConfigError, TiffinError

if TYPE_CHECKING:
    from .app import App

_LOCK_HANDLE: IO[bytes] | None = None


def _app_from_env(env: str | None = None) -> App:
    from .app import App  # noqa: PLC0415

    return App.from_env(env)


def acquire_lock(path: Path) -> bool:
    """Exclusive process lock; released by the OS when the process dies."""
    global _LOCK_HANDLE  # noqa: PLW0603 - the handle must outlive this call
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt  # noqa: PLC0415

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl  # noqa: PLC0415

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return False
    _LOCK_HANDLE = fh
    return True


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tiffin", description="nutrition, menu, shopping and training for one or two people"
    )
    p.add_argument("--env", choices=["dev", "prod"], help="instance mode (default: prod)")
    sub = p.add_subparsers(dest="cmd", required=True)
    from .setup import add_arguments  # noqa: PLC0415

    init = sub.add_parser("init", help="set up this tiffin and pair your Telegram chat")
    init.set_defaults(command="init")
    add_arguments(init)
    connect = sub.add_parser("connect", help="set up one connection again")
    connect.add_argument("target", choices=["telegram", "intervals", "model", "web"])
    connect.set_defaults(command="connect")
    add_arguments(connect)
    sub.add_parser("run", help="poll Telegram, run jobs and serve the configured page")
    sub.add_parser("status", help="liveness and last runs as JSON")
    sub.add_parser("version")
    demo = sub.add_parser("demo", help="serve a throwaway example household locally")
    demo.add_argument("--telegram-transcript", type=Path, metavar="FILE")
    demo.add_argument("--port", type=int, default=7790)
    doc = sub.add_parser("doctor", help="health checks; exit 1 on any red")
    doc.add_argument("--strict", action="store_true", help="warnings fail too")
    doc.add_argument("--offline", action="store_true", help="skip network connection checks")
    devices = sub.add_parser("devices", help="list or sign out web devices")
    devices.add_argument("--sign-out", type=int, metavar="ID", help="sign out a device by list ID")
    pair = sub.add_parser("pair", help="print a one-time pairing code for a person")
    pair.add_argument("slug", choices=["owner", "partner"])
    pair.add_argument("--hours", type=int, default=48)
    link = sub.add_parser(
        "link-tailscale", help="bind a Tailscale login to a person so the web page knows them"
    )
    link.add_argument("slug", choices=["owner", "partner"])
    link.add_argument("login", help="the Tailscale login, e.g. alex@example.com")
    inv = sub.add_parser(
        "invite-shared",
        help="show (and with --send, send) the shared-weekend invitation to one person",
    )
    inv.add_argument("slug", choices=["owner", "partner"])
    inv.add_argument("--send", action="store_true", help="actually message them; default prints")
    nm = sub.add_parser("name", help="set a person's display name (they can also send /name)")
    nm.add_argument("slug", choices=["owner", "partner"])
    nm.add_argument("name", nargs="+", help="what to call them, e.g. Sam")
    cat = sub.add_parser(
        "catalogue", help="build the ingredient catalogue the menu generator draws from"
    )
    cat.add_argument("action", choices=["build"])
    cat.add_argument("--yaml", help="default: config/ingredients.yaml")
    cat.add_argument("--csv", help="default: data/cofid/cofid_2021_proximates.csv")
    product = sub.add_parser("product", help="add a product you buy for a catalogue food")
    product.add_argument("action", choices=["add"])
    product.add_argument("code", help="catalogue food code")
    product.add_argument("name", help="product name")
    product.add_argument("--pack-g", type=float, required=True, help="pack size in grams")
    product.add_argument("--price", help="optional pack price in your configured currency")
    product.add_argument("--shop", default="", help="optional shop name")
    product.add_argument("--url", default="", help="optional HTTPS product link")
    back = sub.add_parser(
        "icu-backfill", help="one wide pull from intervals.icu, for history older than the windows"
    )
    back.add_argument("slug", choices=["owner", "partner"])
    back.add_argument("--days", type=int, default=180, help="how far back to ask for (default 180)")
    shop = sub.add_parser(
        "shop-backfill",
        help="read real supermarket products for the whole catalogue now, instead of "
        "a slice a week",
    )
    shop.add_argument(
        "--retailer", default=None, help="default: the first of the shop.retailers config list"
    )
    # six: a session has 60 turns and a food takes about seven, so twelve ran out half way
    shop.add_argument("--batch", type=int, default=6, help="foods per pass (default 6)")
    shop.add_argument(
        "--passes",
        type=int,
        default=1,
        help="how many passes to run (default 1); each is one "
        "Chrome session against its own budget",
    )
    cook = sub.add_parser(
        "cook",
        help="build a menu proposal for a person now; they accept it themselves",
    )
    cook.add_argument("slug", choices=["owner", "partner"])
    cook.add_argument(
        "--only-list",
        type=int,
        default=None,
        help="use only the foods on this shopping list (cook from what was bought)",
    )
    cook.add_argument("--note", default="", help="what to ask for, in words")
    cook.add_argument(
        "--day",
        default=None,
        help="the first day to plan, YYYY-MM-DD (default: tomorrow); its cook segment from then on",
    )
    cook.add_argument(
        "--take-today",
        action="store_true",
        help="on their word: plan today afresh though it began under another menu (nothing "
        "shopped or cooked from it yet); a recorded day is never taken",
    )
    pref = sub.add_parser(
        "prefs",
        help="show, or change on their word, a person's food lists and standing notes "
        "(every later menu reads them); with no changes it prints them",
    )
    pref.add_argument("slug", choices=["owner", "partner"])
    for flag, what in (
        ("--avoid", "a food or kind to keep off every menu (comma separated, repeatable)"),
        ("--allow", "take an entry off the avoid list"),
        ("--like", "a food to have more often"),
        ("--unlike", "take an entry off the likes list"),
    ):
        pref.add_argument(flag, action="append", default=[], help=what)
    pref.add_argument(
        "--note",
        action="append",
        default=[],
        help="a standing note for every menu, in their words, e.g. 'less rice' (repeatable)",
    )
    pref.add_argument("--drop-note", action="append", default=[], help="take a standing note off")
    pref_check = sub.add_parser("prefs-check", help="audit food preferences and menu decisions")
    pref_check.add_argument("--days", type=int, default=14)
    pref_check.add_argument("--until", help="last day, YYYY-MM-DD (default: today)")
    pref.add_argument(
        "--fine",
        action="append",
        default=[],
        help="a food made from one they avoid that is fine all the same (houmous after "
        "chickpeas): kept, and never asked about again",
    )
    wk = sub.add_parser(
        "weekplan",
        help="show, or change on their word, how a person's week is planned (food/weekplan.py): "
        "tiffin weekplan partner --set plan_day=6 --set shops=[[0,1,2],[3,4]]",
    )
    wk.add_argument("slug", choices=["owner", "partner"])
    wk.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="FIELD=JSON",
        help="a field and its value as JSON (6, true, null, [[0,1,2],[3,4]]); repeatable",
    )
    wk.add_argument(
        "--mains",
        type=int,
        choices=[0, 2, 3],
        default=None,
        help="meats or fish a week, their answer to /packs (0: no limit)",
    )
    ivw = sub.add_parser(
        "interview",
        help="send a person the opening of /questions (what they want from Tiffin), once; "
        "they start it themselves. --answers prints what they have said",
    )
    ivw.add_argument("slug", choices=["owner", "partner"])
    ivw.add_argument("--answers", action="store_true", help="print their answers; send nothing")
    ivw.add_argument(
        "--round", type=int, default=1, help="which round's opening to send (default 1)"
    )
    st = sub.add_parser(
        "set",
        help="change one of a person's settings on their word, as /set does in the bot: "
        "tiffin set partner cook-days daily",
    )
    st.add_argument("slug", choices=["owner", "partner"])
    st.add_argument("field", help="a /set name: cook-days, batch, variety, meals, rhythm ...")
    st.add_argument("value", nargs="+", help="what the bot's buttons would send, e.g. daily")
    st.add_argument(
        "--starts",
        choices=["monday", "tomorrow"],
        default="monday",
        help="how someone plans (plan, cook-days, batch, freeze, choices) changes from next "
        "Monday unless tomorrow is asked for; anything else counts from today",
    )
    res = sub.add_parser(
        "reset-onboarding",
        help="forget what a person told the bot so they can do the interview again; "
        "intervals.icu and the message log are kept",
    )
    res.add_argument("slug", choices=["owner", "partner"])
    res.add_argument("--yes", action="store_true", help="do it; without this it only reports")
    dry = sub.add_parser(
        "dry-run", help="render every message due on a date to a file, sending nothing"
    )
    dry.add_argument("--date", required=True, help="YYYY-MM-DD in the household time zone")
    dry.add_argument("--person", choices=["owner", "partner"])
    dry.add_argument("--out", help="path (.html or .txt); default TIFFIN_HOME/dryrun/<date>.html")
    return p


def cmd_run(args: argparse.Namespace) -> int:
    import sqlite3  # noqa: PLC0415

    app = _app_from_env(args.env)
    try:
        app.setup_logging(console=True)
    except OSError as exc:
        raise ConfigError(
            f"cannot write TIFFIN_HOME at {app.config.paths.root}; choose a writable directory"
        ) from exc
    name = "telegram_token" if app.config.is_prod else "telegram_token_dev"
    if not app.secrets.exists(name):
        raise ConfigError(
            f"Telegram token missing: set TIFFIN_{name.upper()} or create "
            f"{app.secrets.path(name)} (get a token from BotFather)"
        )
    if not acquire_lock(app.config.paths.state / "tiffin.lock"):
        print("another tiffin holds the lock; exit 3", file=sys.stderr)
        return 3
    try:
        app.bootstrap().close()
    except (OSError, sqlite3.OperationalError) as exc:
        raise ConfigError(
            f"cannot create database under TIFFIN_HOME at {app.config.paths.root}"
        ) from exc
    from .runtime import Runtime  # noqa: PLC0415

    rt = Runtime(
        app,
        heartbeat_s=int(app.config.get("ops.heartbeat_s", 30)),
        tick_s=int(app.config.get("schedule.tick_s", 30)),
    )
    return rt.run()


def cmd_status(args: argparse.Namespace) -> int:
    import datetime as dt  # noqa: PLC0415

    from .clock import stamp  # noqa: PLC0415
    from .heartbeat import Heartbeat, PidFile  # noqa: PLC0415

    app = _app_from_env(args.env)
    paths = app.config.paths
    hb = Heartbeat(paths.state, app.clock)
    out: dict[str, Any] = {
        "env": app.config.env,
        "version": app.version,
        "db": str(paths.db),
        "db_exists": paths.db.exists(),
        "heartbeat_age_s": hb.age_s(),
        "pid": (PidFile(paths.state).read() or {}).get("pid"),
    }
    if paths.db.exists():
        db = app.open_db()
        try:
            out["persons"] = [
                {"slug": p.slug, "has_chat_id": p.chat_id is not None, "erased": p.erased}
                for p in app.registry(db).active()
            ]
            out["last_runs"] = [
                dict(r)
                for r in db.all(
                    "SELECT job, slot, person_id, status, detail FROM run_ledger "
                    "ORDER BY started_at DESC LIMIT 10"
                )
            ]
            from . import expected  # noqa: PLC0415
            from .jobs import all_jobs  # noqa: PLC0415

            # every expected morning and evening message that has not gone by its grace deadline:
            # slot, internal person id and due time (UTC), a reason and `late` or `missed`.
            # Empty when all is well. No names, no content.
            out["missed_slots"] = [
                m.as_dict()
                for m in expected.missing(db, app.registry(db), all_jobs(app.config), app.clock)
            ]
            out["messages_out_24h"] = dict(
                (r["status"], r["n"])
                for r in db.all(
                    "SELECT status, COUNT(*) AS n FROM messages_out "
                    "WHERE created_at >= ? GROUP BY status",
                    (stamp(app.clock.now_utc() - dt.timedelta(days=1)),),
                )
            )
        finally:
            db.close()
    print(json.dumps(out, indent=1, default=str))
    return 0


def cmd_pair(args: argparse.Namespace) -> int:
    app = _app_from_env(args.env)
    db = app.bootstrap()
    try:
        code = app.registry(db).create_pairing_code(args.slug, ttl_hours=args.hours)
    finally:
        db.close()
    print(f"pairing code for {args.slug} ({app.config.env}), valid {args.hours} h: {code}")
    print("They send it as a plain message to the bot. Shown once; only its hash is stored.")
    return 0


def cmd_devices(args: argparse.Namespace) -> int:
    app = _app_from_env(args.env)
    db = app.bootstrap()
    try:
        if args.sign_out is not None:
            row = db.one(
                "SELECT d.id FROM web_devices d "
                "LEFT JOIN web_device_events e ON e.device_id = d.id "
                "WHERE d.id = ? AND e.id IS NULL",
                (args.sign_out,),
            )
            if row is None:
                raise ConfigError("device not found or already signed out")
            db.insert("web_device_events", device_id=args.sign_out, action="sign_out")
            print(f"Signed out device {args.sign_out}.")
        else:
            rows = db.all(
                "SELECT d.id, p.slug, d.created_at FROM web_devices d "
                "JOIN persons p ON p.id = d.person_id "
                "LEFT JOIN web_device_events e ON e.device_id = d.id "
                "WHERE d.expires_at > ? AND e.id IS NULL ORDER BY d.id DESC",
                (app.clock.utc_stamp(),),
            )
            if not rows:
                print("No signed-in devices.")
            for row in rows:
                print(f"{row['id']:>4}  {row['slug']:<8}  since {row['created_at']} UTC")
    finally:
        db.close()
    return 0


def cmd_link_tailscale(args: argparse.Namespace) -> int:
    """Bind a login to the person identified by the operator, never by a web request."""
    app = _app_from_env(args.env)
    db = app.bootstrap()
    try:
        registry = app.registry(db)
        try:
            registry.attach_tailscale_login(args.slug, args.login)
        except ValueError as e:
            raise ConfigError(str(e)) from e
        current = registry.require(args.slug)
    finally:
        db.close()
    print(f"{args.slug} ({app.config.env}) now answers to that login on the web page.")
    print(f"person_id {current.id}; the binding is append-only, so a rebind is another row.")
    return 0


#: what the invitation says - the whole of what the other person will see, and what they won't
SHARED_INVITE = (
    "<b>A shared weekend menu?</b>\n"
    "On Saturday and Sunday you would get one menu with {other}, cooked together, each of you with "
    "your own portion sized to your own targets.\n"
    "On those shared dishes you both see both plates: the grams and the kcal of each. Never your "
    "targets, your weight, or anything you have logged.\n"
    "You can switch it off any time with /consent."
)


def cmd_invite_shared(args: argparse.Namespace) -> int:
    """Invite one person to the shared weekend. Prints unless --send: it is a message to them."""
    from .telegram.api import inline_keyboard  # noqa: PLC0415

    app = _app_from_env(args.env)
    db = app.bootstrap()
    try:
        registry = app.registry(db)
        person = registry.require(args.slug)
        other = registry.require("owner" if args.slug == "partner" else "partner")
        if person.chat_id is None or other.chat_id is None:
            raise ConfigError("both people must be paired before sharing weekends")
        from .telegram.render import esc  # noqa: PLC0415

        text = SHARED_INVITE.format(other=esc(other.display_name or "your partner"))
        keyboard = inline_keyboard(
            [[("Yes, share weekends", "k:consent:share:1"), ("No thanks", "k:consent:share:0")]]
        )
        if not args.send:
            print(text.replace("<b>", "").replace("</b>", ""))
            print("\n(not sent; add --send to message them)")
            return 0
        result = app.outbox(db).send(person, "shared_invite", app.clock.utc_stamp(), text, keyboard)
    finally:
        db.close()
    print(f"{args.slug}: {result.status}")
    return 0 if result.sent else 1


def cmd_name(args: argparse.Namespace) -> int:
    """Name a person. The bootstrap could write display_name once and never again."""
    app = _app_from_env(args.env)
    db = app.bootstrap()
    try:
        registry = app.registry(db)
        try:
            cleaned = registry.set_display_name(args.slug, " ".join(args.name))
        except ValueError as e:
            raise ConfigError(str(e)) from e
    finally:
        db.close()
    print(f"{args.slug} ({app.config.env}) is now called {cleaned}.")
    print("Append-only: a correction is another row, and the newest one is used.")
    return 0


def cmd_catalogue(args: argparse.Namespace) -> int:
    """Join config/ingredients.yaml with the CoFID table into `ingredients` rows.

    Nothing else fills that table, and an empty catalogue means every menu job skips with
    "catalogue empty", so the whole food half of Tiffin is inert until this has run once.
    Safe to re-run: a code whose numbers are unchanged inserts nothing.
    """
    from .config import REPO_ROOT  # noqa: PLC0415
    from .food import catalogue  # noqa: PLC0415

    app = _app_from_env(args.env)
    yaml_path = Path(args.yaml) if args.yaml else app.config.paths.config_dir / "ingredients.yaml"
    csv_path = (
        Path(args.csv) if args.csv else REPO_ROOT / "data" / "cofid" / "cofid_2021_proximates.csv"
    )
    for label, path in (("ingredients.yaml", yaml_path), ("CoFID csv", csv_path)):
        if not path.is_file():
            raise ConfigError(f"no {label} at {path}")
    db = app.bootstrap()
    try:
        result = catalogue.build(db, yaml_path, csv_path)
        latest = catalogue.latest_versions(db)
        total = len(latest)
        approved = sum(1 for i in latest if i.approved)
    finally:
        db.close()
    print(
        f"{len(result.inserted)} new or changed, {len(result.unchanged)} unchanged, "
        f"{result.aliases_added} aliases"
    )
    print(f"catalogue now {total} ingredients, {approved} approved and offerable to the model")
    if result.missing_cofid:
        print(f"no CoFID row (skipped): {', '.join(result.missing_cofid[:12])}")
    if result.fibre_null:
        print(f"fibre unknown, stored unapproved: {', '.join(result.fibre_null[:12])}")
    return 0


def cmd_shop_backfill(args: argparse.Namespace) -> int:
    """Read real supermarket products for the catalogue now, rather than a slice a week.

    The scheduled job walks twelve foods each Sunday, which is the right cadence for keeping
    prices fresh and the wrong one for starting: until it has been round the whole catalogue a
    shopping list still names a generic food. This runs the same job, back to back, so a list
    can say "Tesco British Chicken Breast Fillets 650G" instead of "Chicken breast, raw".

    Each pass is one Chrome session against its own budget, and it drives the real browser, so
    it is deliberately one pass at a time unless asked for more.
    """
    from .jobs import shop_refresh  # noqa: PLC0415 (jobs import the scheduler)

    app = _app_from_env(args.env)
    db = app.bootstrap()
    try:
        retailer = args.retailer or shop_refresh.default_retailer(app.config)
        if not retailer:
            raise ConfigError("no retailer adapter is configured; run tiffin init to enable one")
        outbox = app.outbox(db)
        budget = app.config.budget("shop_refresh")
        left = shop_refresh.remaining(_bare_ctx(app, db, outbox, shop_refresh), retailer)
        fresh = shop_refresh.FRESH_DAYS
        print(f"{retailer}: {left} foods with no product read in the last {fresh} days")
        if not left:
            return 0
        mins = budget.timeout_s * args.passes // 60
        spend = "no cost cap" if budget.usd is None else f"up to {budget.usd * args.passes:.2f} USD"
        print(f"{args.passes} pass(es) x {args.batch} foods, {spend} / {mins} min")
        for n in range(1, args.passes + 1):
            detail, left = shop_refresh.backfill_once(
                app, db, outbox, clock=app.clock, retailer=retailer, batch=args.batch
            )
            print(f"pass {n}/{args.passes}: {detail or '(nothing)'} | {left} left")
            if not left:
                break
    finally:
        db.close()
    return 0


def cmd_product(args: argparse.Namespace) -> int:
    """Store the owner's purchase option from the local command line."""
    from .food import products  # noqa: PLC0415

    app = _app_from_env(args.env)
    currency = str(app.config.get("units.currency", "GBP"))
    price_pence = None
    if args.price is not None:
        try:
            price_pence = products.price_hundredths(args.price, currency)
        except ValueError as exc:
            raise ConfigError(str(exc)) from None
    db = app.bootstrap()
    try:
        owner = app.registry(db).admin()
        try:
            product_id = products.record_manual(
                db,
                owner.id,
                ingredient_code=args.code,
                name=args.name,
                pack_g=args.pack_g,
                price_pence=price_pence,
                shop=args.shop,
                url=args.url,
                currency=currency,
            )
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc
    finally:
        db.close()
    print(f"Product saved (#{product_id}). The next shopping list will use it.")
    return 0


def _bare_ctx(app: App, db: Any, outbox: Any, mod: Any) -> Any:
    """A JobContext with no ledger handle, for the read-only `remaining` count."""
    from .scheduler import JobContext  # noqa: PLC0415

    return JobContext(
        app=app,
        db=db,
        job=mod.JOB,
        person=None,
        profile=None,
        day=app.clock.today_local(),
        due_local=app.clock.now_local(),
        now_local=app.clock.now_local(),
        slot="count",
        handle=None,  # type: ignore[arg-type]
        outbox=outbox,
        clock=app.clock,
    )


def cmd_cook(args: argparse.Namespace) -> int:
    """Build a menu PROPOSAL for a person now, optionally only from one shopping list's foods.

    so there is nothing new to buy. It is a proposal - she accepts it herself; nobody accepts on
    her behalf. If it cannot be built from those foods it says so here and messages nobody.
    """
    from . import targets as targets_mod  # noqa: PLC0415 (pulls in the food layer)
    from .clock import parse_day  # noqa: PLC0415
    from .jobs import menu_propose  # noqa: PLC0415 (jobs import the scheduler)

    app = _app_from_env(args.env)
    db = app.bootstrap()
    try:
        person = app.registry(db).require(args.slug)
        today = app.clock.today_local()
        only: frozenset[str] = frozenset()
        if args.only_list is not None:
            only = frozenset(
                str(r["code"])
                for r in db.all(
                    "SELECT DISTINCT i.code FROM shopping_items si "
                    "JOIN ingredients i ON i.id = si.ingredient_id "
                    "JOIN shopping_lists sl ON sl.id = si.list_id "
                    "WHERE si.list_id = ? AND (sl.person_id = ? OR sl.person_id IS NULL)",
                    (args.only_list, person.id),
                )
            )
            if not only:
                raise ConfigError(f"list {args.only_list} is empty or not {args.slug}'s")
        said = menu_propose.propose_now(
            app,
            db,
            person,
            targets_mod.profile_on(db, person.id, today),
            app.outbox(db),
            clock=app.clock,
            replace=True,
            note=args.note,
            only_codes=only,
            quiet_failure=True,
            day=parse_day(args.day) if args.day else None,
            take_today=args.take_today,
        )
    finally:
        db.close()
    print(said or f"A proposal went to {args.slug} ({app.config.env}); they accept it themselves.")
    return 0


def cmd_weekplan(args: argparse.Namespace) -> int:
    """Show or amend how a person plans their week."""
    import json  # noqa: PLC0415

    from .food import weekplan  # noqa: PLC0415

    changes: dict[str, object] = {}
    for item in args.set:
        key, sep, raw = item.partition("=")
        if not sep:
            raise ConfigError(f"--set wants FIELD=JSON, not {item!r}")
        try:
            changes[key.strip()] = json.loads(raw)
        except ValueError as e:
            raise ConfigError(f"{key}: {raw!r} is not JSON") from e
    known = set(weekplan.WeekPlan.__dataclass_fields__)
    unknown = sorted(set(changes) - known)
    if unknown:
        raise ConfigError(f"unknown field(s) {unknown}; fields: {sorted(known)}")
    app = _app_from_env(args.env)
    db = app.bootstrap()
    try:
        person = app.registry(db).require(args.slug)
        if changes:
            try:
                with db.tx():
                    weekplan.change(db, person.id, source="operator", **changes)
            except ValueError as e:
                raise ConfigError(str(e)) from e
        if args.mains is not None:
            from .food import packs  # noqa: PLC0415

            with db.tx():
                print(packs.set_mains(db, person.id, args.mains, source="operator"))
        print(f"{args.slug} ({app.config.env}): {weekplan.as_json(weekplan.of(db, person.id))}")
    finally:
        db.close()
    return 0


def cmd_interview(args: argparse.Namespace) -> int:
    """
    questions ... she answers and then you build"). Dev sends only to the owner, as everything does.
    `--answers` reads what was said, for whoever builds from it; it sends nothing."""
    from .telegram import interview  # noqa: PLC0415

    app = _app_from_env(args.env)
    db = app.bootstrap()
    try:
        person = app.registry(db).require(args.slug)
        if args.answers:
            for r, round_ in enumerate(interview.ROUNDS, start=1):
                said = interview.answered(db, person.id, round_)
                print(f"Round {r}: {len(said)} of {len(round_.questions)} answered or skipped")
                for n, q in enumerate(round_.questions, start=1):
                    if q.key in said:
                        answer = said[q.key]
                        shown = answer if answer is not None else "(skipped)"
                        print(f"{n}. {q.text}\n   {shown}")
            return 0
        if not 1 <= args.round <= len(interview.ROUNDS):
            raise ConfigError(f"there are {len(interview.ROUNDS)} rounds")
        result = interview.send_intro(app.outbox(db), person, interview.ROUNDS[args.round - 1])
    finally:
        db.close()
    print(f"round {args.round} opening to {args.slug} ({app.config.env}): {result.status}")
    return 0


def cmd_prefs(args: argparse.Namespace) -> int:
    """A person's food lists and standing notes, changed on their word, passed on by the owner.

    avoid list), some a food to have more often (likes), and the rest - "less rice", "cooked warm
    vegetables, no salads", "simple recipes" - a standing note every menu build now reads. The
    bot is how she says these herself; this is the same doors (`prefs.amend`, `amend_notes`)
    from the shell, so the answer and the rows are the ones she would get. Everything written
    is hers to read: /avoid and the page show the food lists; tiffin prefs shows the notes.
    """
    from .food import catalogue, prefs  # noqa: PLC0415 (pulls in the food layer)

    app = _app_from_env(args.env)
    db = app.bootstrap()
    try:
        person = app.registry(db).require(args.slug)
        for list_name, add, remove in (
            ("avoid", args.avoid, args.allow),
            ("likes", args.like, args.unlike),
        ):
            add_terms, remove_terms = _terms(add), _terms(remove)
            if not add_terms and not remove_terms:
                continue
            try:
                # a list edited as a list, not words picked out of a sentence: an entry naming
                # no food yet is kept and said, as `/set avoid + olives` does
                changed = prefs.amend(
                    db,
                    person.id,
                    list_name,
                    add=add_terms,
                    remove=remove_terms,
                    clock=app.clock,
                    strict=False,
                )
            except LookupError as e:
                raise ConfigError(
                    f"{args.slug} has no profile yet: they finish /onboard first"
                ) from e
            print(f"{list_name}: {changed.said()}")
            for ask in changed.asks:
                print(f"  to ask them: {prefs.ask_said(ask)}")
        # the answer to "houmous is made from chickpeas: keep it off too?", given on their word -
        # the same row their own "Houmous is fine" tap writes, so it is never asked again
        for term in _terms(args.fine):
            food = next(
                (
                    i
                    for i in prefs.scoped(db, person.id)
                    if catalogue.refused_matches(catalogue.normalise(term), i)
                ),
                None,
            )
            if food is None:
                print(f"fine: no food of theirs is called {term!r}")
                continue
            kept = prefs.answer(db, person.id, food.code, keep_off=False, clock=app.clock)
            print(f"fine: {kept.said()}")
        if args.note or args.drop_note:
            noted = prefs.amend_notes(
                db,
                person.id,
                add=args.note,
                drop=args.drop_note,
                source="operator",
                clock=app.clock,
            )
            for label, items in (
                ("notes added", noted.added),
                ("notes taken off", noted.dropped),
                ("already standing", noted.already),
                ("no such note", noted.missing),
                (
                    f"not written (over {prefs.MAX_NOTE} characters or {prefs.MAX_NOTES} notes)",
                    noted.refused,
                ),
            ):
                if items:
                    print(f"{label}: " + "; ".join(items))
        today = app.clock.today_local()
        shown = prefs.view(db, person.id, today)
        print(f"\n{args.slug} ({app.config.env}), as every menu from now on reads it:")
        print(
            "  avoid: "
            + ("; ".join(prefs.kept(a["term"], a["foods"]) for a in shown["avoid"]) or "(nothing)")
        )
        if shown["avoid_foods"]:
            print("  avoided by a tap: " + ", ".join(f["name"] for f in shown["avoid_foods"]))
        print("  likes: " + (", ".join(shown["likes"]) or "(nothing)"))
        notes = prefs.standing_notes(db, person.id)
        print("  standing notes: " + ("; ".join(notes) or "(none)"))
        for ask in shown["asks"]:
            print(f"  still to ask them: {prefs.ask_said(ask)} (--fine, or --avoid)")
        for dish in shown["still_on_menu"]:
            print(
                f"  still on a coming menu: {dish['name']}, {dish['slot']} "
                f"{', '.join(dish['days'])} (holds {', '.join(dish['holds'])})"
            )
    finally:
        db.close()
    return 0


def cmd_set(args: argparse.Namespace) -> int:
    """`/set` from the shell, on someone's word: the same fields, the interview's own parsing, and
    `tiffin prefs`, which says what each entry keeps out."""
    from . import targets as targets_mod  # noqa: PLC0415 (pulls in the food layer)
    from .onboarding import STEP_BY_ID, Invalid  # noqa: PLC0415 (pulls in the interview)
    from .telegram.settings import BY_NAME, write_setting  # noqa: PLC0415

    field = BY_NAME.get(args.field.strip().lower().replace("_", "-"))
    if field is None:
        raise ConfigError(f"no setting called {args.field!r}: {', '.join(sorted(BY_NAME))}")
    if field.amend:
        raise ConfigError(f"{field.name} is a list: use `tiffin prefs {args.slug}`")
    app = _app_from_env(args.env)
    db = app.bootstrap()
    try:
        person = app.registry(db).require(args.slug)
        current = targets_mod.profile_on(db, person.id, app.clock.today_local())
        if current is None:
            raise ConfigError(f"{args.slug} has no profile yet: they finish /onboard first")
        value = " ".join(args.value)
        try:
            parsed = STEP_BY_ID[field.step].parse(value, dict(current))
        except Invalid as e:
            raise ConfigError(f"{field.name}: {e}") from e
        start = write_setting(db, person.id, field, parsed, clock=app.clock, starts=args.starts)
    finally:
        db.close()
    print(
        f"{args.slug} ({app.config.env}): {field.name} ({field.describe}) is now {value}, "
        f"from {start:%a} {start.day} {start:%b}."
    )
    return 0


def _terms(values: Sequence[str]) -> list[str]:
    """Repeatable, comma-separated arguments as one list: --avoid "lentils, beans" --avoid liver."""
    return [t.strip() for v in values for t in v.split(",") if t.strip()]


def cmd_icu_backfill(args: argparse.Namespace) -> int:
    """The plan's "cold" tier: recover history from before Tiffin, or after an outage.

    The hourly sync only ever looks back 14 days for wellness and 3 for activities, so a weight
    or a ride older than that has no other way in. Safe to re-run: `icu_raw` dedupes on the
    payload hash, so an unchanged day writes nothing.
    """
    from .integrations import icu as icu_mod  # noqa: PLC0415

    app = _app_from_env(args.env)
    db = app.bootstrap()
    try:
        person = app.registry(db).require(args.slug)
        key = app.secrets.icu_key(person.slug)
        if not key:
            where = app.secrets.path(f"icu_key_{args.slug}")
            raise ConfigError(f"no intervals.icu key for {args.slug} at {where}")
        section = app.config.section("icu")
        result = icu_mod.sync_person(
            db,
            person,
            key,
            state_dir=app.config.paths.state,
            streams_dir=app.config.paths.icu_streams,
            days_wellness=args.days,
            days_activity=args.days,
            clock=app.clock,
            transport=app.transport,
            base_url=str(section.get("base_url", icu_mod.BASE_URL)),
            budget_per_hour=int(section.get("budget_per_hour", 60)),
        )
        print(f"{args.slug}: {result.summary()}")
        weights = db.all(
            "SELECT COUNT(*) AS n, MIN(day) AS first, MAX(day) AS last FROM v_weigh_in_latest "
            "WHERE person_id = ?",
            (person.id,),
        )[0]
        print(f"weigh-ins now held: {weights['n']} ({weights['first']} to {weights['last']})")
    finally:
        db.close()
    return 0


def cmd_reset_onboarding(args: argparse.Namespace) -> int:
    """Report first, act only on --yes.

    A reset is not reversible from inside Tiffin, and the difference between the two
    lists below is the difference between redoing an interview and losing six months of
    weight history - so the default is to print what would go and stop.
    """
    from . import reset  # noqa: PLC0415

    app = _app_from_env(args.env)
    db = app.open_db()
    try:
        report = (
            reset.preview(db, args.slug)
            if not args.yes
            else reset.reset_onboarding(db, args.slug, clock=app.clock)
        )
        print(json.dumps(report.as_dict(), indent=2))
        if not args.yes:
            print(
                f"\nnothing was changed. {report.total} rows would go; "
                f"add --yes to do it.\nkept: " + ", ".join(sorted(reset.KEPT)),
                file=sys.stderr,
            )
            return 0
        print(
            f"\nreset {args.slug}: {report.total} rows removed. Send /onboard to the bot.",
            file=sys.stderr,
        )
    finally:
        db.close()
    return 0


def cmd_dry_run(args: argparse.Namespace) -> int:
    from . import dryrun  # noqa: PLC0415 (pulls in every job)
    from .clock import parse_day  # noqa: PLC0415

    app = _app_from_env(args.env)
    day = parse_day(args.date)
    messages = dryrun.render_day(app, day, person_slug=args.person)
    out = Path(args.out) if args.out else app.config.paths.dryrun / f"{day}.html"
    dryrun.write(day, messages, out)
    print(f"{len(messages)} messages rendered to {out}")
    print(dryrun.to_text(messages))
    return 0


def cmd_prefs_check(args: argparse.Namespace) -> int:
    from . import prefs_check  # noqa: PLC0415
    from .clock import parse_day  # noqa: PLC0415
    from .db import Db  # noqa: PLC0415

    app = _app_from_env(args.env)
    if not app.config.paths.db.exists():
        raise ConfigError(f"database missing: {app.config.paths.db}; run tiffin run first")
    if args.days < 1:
        raise ConfigError("--days must be positive")
    try:
        until = parse_day(args.until) if args.until else app.clock.today_local()
    except ValueError as exc:
        raise ConfigError("--until must be YYYY-MM-DD") from exc
    with Db(app.config.paths.db, readonly=True) as db:
        prefs_check.run(db, until, args.days)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.cmd == "run":
            return cmd_run(args)
        if args.cmd == "demo":
            from .demo import run  # noqa: PLC0415

            return run(args)
        if args.cmd == "init":
            from .setup import run  # noqa: PLC0415

            return run(args)
        if args.cmd == "connect":
            from .setup import run  # noqa: PLC0415

            return run(args)
        if args.cmd == "status":
            return cmd_status(args)
        if args.cmd == "pair":
            return cmd_pair(args)
        if args.cmd == "devices":
            return cmd_devices(args)
        if args.cmd == "link-tailscale":
            return cmd_link_tailscale(args)
        if args.cmd == "name":
            return cmd_name(args)
        if args.cmd == "invite-shared":
            return cmd_invite_shared(args)
        if args.cmd == "catalogue":
            return cmd_catalogue(args)
        if args.cmd == "product":
            return cmd_product(args)
        if args.cmd == "icu-backfill":
            return cmd_icu_backfill(args)
        if args.cmd == "shop-backfill":
            return cmd_shop_backfill(args)
        if args.cmd == "cook":
            return cmd_cook(args)
        if args.cmd == "prefs":
            return cmd_prefs(args)
        if args.cmd == "prefs-check":
            return cmd_prefs_check(args)
        if args.cmd == "weekplan":
            return cmd_weekplan(args)
        if args.cmd == "interview":
            return cmd_interview(args)
        if args.cmd == "set":
            return cmd_set(args)
        if args.cmd == "dry-run":
            return cmd_dry_run(args)
        if args.cmd == "reset-onboarding":
            return cmd_reset_onboarding(args)
        if args.cmd == "doctor":
            from . import doctor  # noqa: PLC0415

            try:
                app = _app_from_env(args.env)
            except ConfigError as exc:
                print(f"fail config          {exc}; fix tiffin.toml or run tiffin init")
                return 1
            return doctor.main(app, strict=args.strict, online=not args.offline)
        if args.cmd == "version":
            print(__version__)
            return 0
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    except TiffinError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
