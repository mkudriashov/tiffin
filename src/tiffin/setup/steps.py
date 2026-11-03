"""The seven parts of setup. Each explains itself, asks, checks the answers as it goes and
records them in the state; what each part looks like in the list and the review is here too.
"""

from __future__ import annotations

import argparse
import base64
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..clock import Clock, detected_timezone
from ..config import Budget
from ..errors import ConfigError, ModelError, ModelInvalid, TelegramConflict, TelegramError
from ..http import TransportError, get
from ..telegram.api import TelegramApi
from ..term import plural
from .ask import (
    RETAILERS,
    Invalid,
    Prompter,
    Quit,
    Redo,
    check_base_url,
    check_country,
    check_currency,
    check_folder,
    check_https_address,
    check_name,
    check_retailers,
    check_root_path,
    check_timezone,
    check_token,
    check_words,
    one_of,
)
from .state import State

COUNTRY_CURRENCY = {
    "GB": "GBP",
    "IE": "EUR",
    "FR": "EUR",
    "DE": "EUR",
    "NL": "EUR",
    "BE": "EUR",
    "ES": "EUR",
    "IT": "EUR",
    "PT": "EUR",
    "AT": "EUR",
    "FI": "EUR",
    "GR": "EUR",
    "US": "USD",
    "CA": "CAD",
    "AU": "AUD",
    "NZ": "NZD",
    "CH": "CHF",
    "SE": "SEK",
    "NO": "NOK",
    "DK": "DKK",
}


ZONE_COUNTRY = {
    "Europe/London": "GB",
    "Europe/Dublin": "IE",
    "Europe/Paris": "FR",
    "Europe/Berlin": "DE",
    "Europe/Amsterdam": "NL",
    "Europe/Brussels": "BE",
    "Europe/Madrid": "ES",
    "Europe/Rome": "IT",
    "Europe/Lisbon": "PT",
    "Europe/Vienna": "AT",
    "Europe/Helsinki": "FI",
    "Europe/Athens": "GR",
    "Europe/Zurich": "CH",
    "Europe/Stockholm": "SE",
    "Europe/Oslo": "NO",
    "Europe/Copenhagen": "DK",
    "Australia/Sydney": "AU",
    "Australia/Melbourne": "AU",
    "Pacific/Auckland": "NZ",
    "America/Toronto": "CA",
    "America/Vancouver": "CA",
    "America/New_York": "US",
    "America/Chicago": "US",
    "America/Denver": "US",
    "America/Los_Angeles": "US",
}


PROVIDERS = ("none", "claude-cli", "anthropic", "openai", "openai-compatible")


LOOPBACK = ("127.0.0.1", "localhost", "::1")


def _host(url: str) -> str:
    try:
        return urlsplit(url).netloc or url
    except ValueError:
        return url


def web_mode(s: State, *, after: bool = True) -> str:
    if not s.get("web.enabled", after=after):
        return "off"
    if s.get("web.identity_mode", after=after) == "header":
        return "tailscale"
    try:
        host = urlsplit(str(s.get("web.origin", after=after))).hostname or ""
    except ValueError:
        host = ""
    return "local" if host in LOOPBACK else "proxy"


def show_home(s: State, after: bool) -> str:
    g = lambda key: s.get(key, after=after)  # noqa: E731
    return (
        f"{g('env.tz')}, {g('env.country')}; {g('units.weight')} and {g('units.height')}; "
        f"prices in {g('units.currency')}"
    )


def show_people(s: State, after: bool) -> str:
    owner = s.name("owner", after=after) or "no name yet"
    if not s.two(after=after):
        return f"just you ({owner})"
    partner = s.name("partner", after=after)
    return f"you ({owner}) and a partner" + (f" ({partner})" if partner else "")


def show_telegram(s: State, after: bool) -> str:
    token = s.secret(s.token_name, after=after)
    same_token = token == s.secret(s.token_name, after=not after)
    if not token:
        first = "no bot token yet"
    elif s.bot and (after or same_token):
        first = f"@{s.bot}, token {s.style.mask(token)}"
    else:
        first = f"bot token {s.style.mask(token)}"
    parts = [first, "your chat paired" if s.saved.paired.get("owner") else "your chat not paired"]
    if s.two(after=after):
        parts.append(
            "your partner's chat paired"
            if s.saved.paired.get("partner")
            else "your partner's chat not paired"
        )
    return "; ".join(parts)


def show_model(s: State, after: bool) -> str:
    provider = str(s.get("model.provider", after=after))
    if provider == "none":
        return "none: you plan meals yourself"
    if provider == "claude-cli":
        return "Claude, through Claude Code on this computer"
    key = s.secret("model_api_key", after=after)
    keyed = f"key {s.style.mask(key)}" if key else "no key"
    if provider == "openai-compatible":
        return f"a service at {_host(str(s.get('model.base_url', after=after)))} ({keyed})"
    return f"{'Anthropic' if provider == 'anthropic' else 'OpenAI'} ({keyed})"


def show_training(s: State, after: bool) -> str:
    keys = {slug: s.secret(f"icu_key_{slug}", after=after) for slug in ("owner", "partner")}
    held = [
        f"{'you' if slug == 'owner' else 'your partner'} (key {s.style.mask(key)})"
        for slug, key in keys.items()
        if key
    ]
    return "intervals.icu for " + " and ".join(held) if held else "not connected"


def show_web(s: State, after: bool) -> str:
    mode = web_mode(s, after=after)
    if mode == "off":
        return "off: Telegram only"
    origin = str(s.get("web.origin", after=after)).rstrip("/")
    address = origin + (str(s.get("web.root_path", after=after)).rstrip("/") or "") + "/"
    if mode == "local":
        return f"on this computer only, at {address}"
    words = f"at {address}" + (" through Tailscale" if mode == "tailscale" else "")
    if s.get("web.bind_trusted_network", after=after):
        words += ", listening on the container's own network"
    front = str(s.get("hosting.web_front", after=after) or "")
    words += f"; records held on {s.get('hosting.records', after=after)}"
    return words + (f", page passes through {front}" if front else "")


def show_shopping(s: State, after: bool) -> str:
    retailers = [str(r) for r in s.get("shop.retailers", after=after) or []]
    if not retailers:
        return "typical packs from Tiffin's food list"
    names = [RETAILERS.get(r, r) for r in retailers]
    return "typical packs, and products read from " + ", ".join(names)


def step_home(s: State, p: Prompter) -> None:
    a = s.args
    p.explain(
        "Tiffin keeps its settings, database, backups and secret keys in one folder, and plans "
        "your days, reminders and menus in your time zone."
    )
    if s.first_run and p.interactive and not s.container and not _home_given(a, s.plain_env):
        folder = Path(p.ask("Folder for Tiffin's data", str(s.home), check=check_folder))
        if folder != s.home:
            a.home = str(folder)  # chosen now: not asked again
            if (folder / "tiffin.toml").is_file():
                raise MoveHome(folder)  # settings are already there: show them instead
            s.rehome(folder)
    else:
        p.say(f"  Folder: {s.home}")
    first = s.first_run
    tz = p.ask(
        "Time zone",
        a.timezone or (detected_timezone() if first else str(s.get("env.tz"))),
        flag="--timezone",
        check=check_timezone,
    )
    country = p.ask(
        "Country (two letters)",
        a.country or (ZONE_COUNTRY.get(tz, "GB") if first else str(s.get("env.country"))),
        flag="--country",
        check=check_country,
    )
    if country != "GB":
        p.result(
            "warn",
            "Tiffin's food list is British (UK CoFID data): its food names and pack sizes have "
            f"not been checked for {country}. Look through them before you plan.",
        )
    same = not first and country == s.get("env.country", after=False)
    currency = p.ask(
        "Currency for prices you type in",
        a.currency or (str(s.get("units.currency")) if same else COUNTRY_CURRENCY.get(country, "")),
        flag="--currency",
        check=check_currency,
    )
    imperial = country == "US"
    weight = p.ask(
        "Body weight in kg or lb",
        a.weight_unit or (str(s.get("units.weight")) if same else "lb" if imperial else "kg"),
        flag="--weight-unit",
        check=one_of("kg", "lb"),
    )
    height = p.ask(
        "Height in cm or in",
        a.height_unit or (str(s.get("units.height")) if same else "in" if imperial else "cm"),
        flag="--height-unit",
        check=one_of("cm", "in"),
    )
    for key, value in (
        ("env.tz", tz),
        ("env.country", country),
        ("units.currency", currency),
        ("units.weight", weight),
        ("units.height", height),
    ):
        s.set(key, value)


def step_people(s: State, p: Prompter) -> None:
    a = s.args
    p.explain(
        "Tiffin is for one person or a couple. Each of you has your own chat with the bot, your "
        "own targets and your own private records."
    )
    who = p.choose(
        "Who will use Tiffin",
        [("one", "Just me"), ("two", "Me and a partner")],
        a.partner or ("two" if s.two() else "one"),
        flag="--partner",
        aliases={"yes": "two", "no": "one"},
    )
    owner_now = s.name("owner", after=False)
    name = p.ask(
        "What should Tiffin call you",
        a.owner_name or s.name("owner"),
        flag="--owner-name",
        check=check_name,
    )
    s.set("persons.owner.display_name", name)
    s.names.pop("owner", None)
    if name != owner_now:
        s.names["owner"] = name
    if who == "two":
        partner_now = s.name("partner", after=False)
        partner = p.ask(
            "Your partner's name",
            a.partner_name or s.name("partner"),
            flag="--partner-name",
            required=False,
            empty="Enter to let them choose it in Telegram",
            check=check_name,
        )
        s.names.pop("partner", None)
        if partner:
            s.set("persons.partner.display_name", partner)
            if partner != partner_now:
                s.names["partner"] = partner
    elif s.saved.paired.get("partner"):
        p.result(
            "info",
            "Your partner's chat stays paired. This only changes what setup and tiffin doctor "
            "expect.",
        )
    s.set("household.partner_expected", who == "two")


def _api(s: State, token: str) -> TelegramApi:
    # at most two seconds between tries, so a missing connection is reported in seconds
    return TelegramApi(
        token,
        transport=s.deps.transport,
        sleeper=lambda delay: s.deps.sleep(min(delay, 2.0)),
        poll_timeout_s=10,
    )


def _telegram_problem(error: Exception) -> str:
    text = str(error)
    if isinstance(error, TransportError) or "gave up" in text:
        return "Could not reach Telegram. Check this computer's internet connection and try again."
    if re.search(r": (401|404)\b", text):
        return "Telegram does not know that token. Copy it again from @BotFather."
    return f"Telegram refused it ({text})."


def step_telegram(s: State, p: Prompter) -> None:
    a = s.args
    name = s.token_name
    from_env = s.env_secret(name)
    saved = s.saved_secret(name)
    if from_env:
        p.explain(
            f"The bot token comes from {from_env} in the environment. Tiffin always uses that "
            "one, so it is not asked for here."
        )
    elif not saved and not a.telegram_token:
        p.explain(
            "Tiffin talks to you through a Telegram bot of your own. To make one: in Telegram, "
            "open @BotFather and send /newbot, then answer its questions (a name, then a "
            "username). It replies with a token. Copy the token and paste it here. Treat it "
            "like a password."
        )
    else:
        p.explain("Tiffin talks to you through your own Telegram bot.")
    given = a.telegram_token or ""
    while True:
        if from_env:
            answer, candidate = None, s.environ[f"TIFFIN_{name.upper()}"]
        else:
            answer = p.ask_secret(
                "Bot token",
                saved=s.style.mask(saved) if saved else None,
                given=given,
                flag="--telegram-token",
            )
            candidate = answer or saved or ""
        try:
            token = check_token(candidate)
            p.say(f"  Checking it with Telegram{s.style.ellipsis}")
            bot = _api(s, token).get_me()
        except (Invalid, TelegramError, TransportError) as e:
            problem = str(e) if isinstance(e, Invalid) else _telegram_problem(e)
            p.result("fail", problem)
            if not p.interactive:
                raise ConfigError(f"Telegram: {problem}") from None
            if from_env:
                if not p.confirm("Carry on without a working bot token", False):
                    raise Quit from None
                break
            given = ""
            p.say("  Paste the token again, or type back or quit.")
            continue
        s.bot = str(bot.get("username") or "")
        p.result("ok", f"Connected to @{s.bot}.")
        s.secrets.pop(name, None)
        if answer and answer != saved:
            s.secrets[name] = token
        _keep_copy(s, name, from_env, token)
        s.pairing_due = True
        _pair_people(s, p, token)
        return
    s.secrets.pop(name, None)


def _keep_copy(s: State, name: str, from_env: str | None, value: str) -> None:
    """A secret in a plain variable set for this one command is kept in the secrets folder when
    the folder has none, as `tiffin init` always did, so `tiffin run` finds it without the
    variable. A `_FILE` variable names where the secret lives (a container's secret), so it is
    never copied, and a saved copy is never replaced from the environment."""
    if from_env == f"TIFFIN_{name.upper()}" and not s.saved_secret(name):
        s.secrets[name] = value


def _running(s: State) -> bool:
    """A Tiffin is running on this home: its lock is held and its heartbeat is fresh."""
    from ..doctor import _lock_held  # noqa: PLC0415
    from ..heartbeat import Heartbeat  # noqa: PLC0415

    age = Heartbeat(s.home / "state").age_s()
    return age is not None and age < 180 and _lock_held(s.home / "state" / "tiffin.lock")


def _new_code(s: State, slug: str, hours: int) -> str:
    app = s.app()
    db = app.bootstrap()
    try:
        return app.registry(db).create_pairing_code(slug, ttl_hours=hours)
    finally:
        db.close()


def _wait_for_pairing(s: State, token: str, slug: str) -> bool:
    """Wait for `slug`'s code to reach the bot. When Tiffin is running here it pairs the chat
    itself, so this watches the database; otherwise it reads the bot's messages."""
    app = s.app()
    db = app.bootstrap()
    try:
        registry = app.registry(db)
        deadline = s.deps.monotonic() + s.deps.pair_wait_s
        if _running(s):
            while s.deps.monotonic() < deadline:
                if registry.require(slug).chat_id is not None:
                    s.paired_now.add(slug)
                    return True
                s.deps.sleep(2)
            return False
        api = _api(s, token)
        offset = int(db.state_get("update_offset") or 0)
        try:
            while s.deps.monotonic() < deadline:
                for update in api.get_updates(offset, timeout_s=10):
                    offset = max(offset, int(update["update_id"]) + 1)
                    message = update.get("message") or {}
                    chat = message.get("chat") or {}
                    if chat.get("type") != "private":
                        continue
                    person = registry.try_pair(int(chat["id"]), str(message.get("text") or ""))
                    if person is not None:
                        s.saved.paired[person.slug] = True
                        s.paired_now.add(person.slug)
                        if person.slug == slug:
                            return True
        finally:
            if offset:
                db.state_set("update_offset", str(offset))
        return False
    finally:
        db.close()


def _pair_by_id(s: State, chat_id: int) -> None:
    app = s.app()
    db = app.bootstrap()
    try:
        registry = app.registry(db)
        if registry.try_pair(chat_id, registry.create_pairing_code("owner", ttl_hours=1)) is None:
            raise ConfigError("--owner-chat-id: that chat could not be paired")
    finally:
        db.close()
    s.saved.paired["owner"] = True
    s.paired_now.add("owner")


def _wait_and_say(s: State, p: Prompter, token: str, slug: str) -> bool:
    who = "your" if slug == "owner" else "your partner's"
    code = _new_code(s, slug, 1)
    p.say(f"  Send this code to @{s.bot} as a message of its own:  {s.style.bold(code)}")
    if slug == "partner":
        p.say("  (from your partner's own Telegram)")
    wait = plural(max(1, round(s.deps.pair_wait_s / 60)), "minute")
    p.say(f"  Waiting for it, up to {wait} (Ctrl+C stops waiting){s.style.ellipsis}")
    try:
        paired = _wait_for_pairing(s, token, slug)
    except KeyboardInterrupt:
        p.say("")
        p.result("warn", "Stopped waiting.")
        return False
    except TelegramConflict:
        p.result(
            "fail",
            "Another program is reading this bot's messages, probably another copy of Tiffin. "
            "Stop it, then try again.",
        )
        return False
    except (TelegramError, TransportError) as e:
        p.result("fail", _telegram_problem(e))
        return False
    if paired:
        p.result("ok", f"Paired with {who} chat.")
        s.saved.paired[slug] = True
        return True
    p.result("warn", "No code arrived in time.")
    return False


def _pair_people(s: State, p: Prompter, token: str) -> None:
    """Pair whoever is not paired yet. Unattended, this waits until the settings are saved
    (`_pair_unattended`), so a missing pairing message never costs the other answers."""
    a = s.args
    if not p.interactive:
        return
    if s.saved.paired.get("owner"):
        p.result("ok", "Your chat is paired.")
    elif a.owner_chat_id:
        _pair_by_id(s, int(a.owner_chat_id))
        p.result("ok", "Your chat is paired, from --owner-chat-id.")
    else:
        p.explain(
            "Now pair your own chat, so the bot knows it is you. A pairing is kept straight away, "
            "even if you stop before the end.",
        )
        while not s.saved.paired.get("owner"):
            options = [("now", "Send a code to the bot now")]
            if s.env == "prod":
                options.append(("later", "Later: give me a code to send once Tiffin is running"))
            if p.choose("Pair your chat", options, "now") == "later":
                s.codes["owner"] = _new_code(s, "owner", 48)
                p.result("info", f"Your code is {s.codes['owner']}. It is shown again at the end.")
                break
            _wait_and_say(s, p, token, "owner")
    if not s.two() or s.saved.paired.get("partner"):
        if s.two():
            p.result("ok", "Your partner's chat is paired.")
        return
    if s.env != "prod":
        p.result("info", "Development mode pairs only your own chat.")
        return
    p.explain(
        "Your partner pairs their own chat by sending a one-time code to the bot from their own "
        "Telegram."
    )
    choice = p.choose(
        "Pair your partner",
        [
            ("code", "Give me a code for them to send once Tiffin is running"),
            ("now", "They are here: wait for their code now"),
            ("later", "Not now (later, run tiffin pair partner)"),
        ],
        "code",
    )
    if choice == "code":
        s.codes["partner"] = _new_code(s, "partner", 48)
        p.result("info", f"Their code is {s.codes['partner']}. It is shown again at the end.")
    elif choice == "now":
        _wait_and_say(s, p, token, "partner")


PROVIDER_CHOICES = (
    ("none", "No model: you plan meals yourself, and everything else still works"),
    ("claude-cli", "Claude, through Claude Code on this computer (uses your Claude plan)"),
    ("anthropic", "Anthropic, with an API key (pay as you go)"),
    ("openai", "OpenAI, with an API key (pay as you go)"),
    ("openai-compatible", "Another service that works like OpenAI's, such as one you run yourself"),
)


TEST_COST = {
    "claude-cli": "It counts towards your Claude plan's usage.",
    "anthropic": "On a pay-as-you-go account it costs a fraction of a penny.",
    "openai": "On a pay-as-you-go account it costs a fraction of a penny.",
    "openai-compatible": "A service you run yourself charges nothing; a paid one may charge a "
    "fraction of a penny.",
}


def _probe(s: State) -> None:
    app = s.app()
    app.config.paths.ensure()
    if s.deps.probe_model is not None:
        s.deps.probe_model(app)
        return
    from ..model.provider import configured_provider  # noqa: PLC0415

    backend = configured_provider(app.config, app.secrets, app.transport, clock=app.clock)
    if backend is None:
        raise ConfigError("no model is chosen")
    answer = backend.complete_json(
        "doctor",
        'Return {"ok": true}.',
        {
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
            "additionalProperties": False,
        },
        Budget(20, 1, None),
    )
    if answer.data != {"ok": True}:
        raise ModelInvalid("unexpected answer")


def _model_problem(error: Exception) -> str:
    text = str(error)
    if isinstance(error, ConfigError):
        return text
    if re.search(r"HTTP 40[13]\b", text):
        return "The key was not accepted. Check it, or make a new one."
    if "HTTP 404" in text:
        return "The address or the model name was not found."
    if "HTTP 429" in text or "Quota" in type(error).__name__:
        return "The account is out of credit or over its limit."
    if "timed out" in text.lower() or "Timeout" in type(error).__name__:
        return "The model took too long to answer."
    if "connection failed" in text:
        return "Could not connect to the service. Check its address and that it is running."
    if isinstance(error, OSError):
        return "Claude Code could not be started. Check that the claude command works here."
    if isinstance(error, ModelInvalid):
        return "It answered, but not in the form Tiffin needs. Small models can find this hard."
    if isinstance(error, ModelError):
        return f"It did not answer ({text})."
    return f"It did not answer ({type(error).__name__})."


def _claude_ready(s: State, p: Prompter) -> None:
    exe = str(s.get("models.claude_exe") or "claude")
    if s.deps.which(exe):
        p.result("ok", "Found Claude Code (the claude command).")
    else:
        p.result(
            "warn",
            "The claude command is not installed here. Install Claude Code and sign in to it, or "
            "choose another option; until then the model's work stays manual.",
        )
    signed_in = s.environ.get("TIFFIN_CLAUDE_OAUTH_TOKEN") or s.environ.get(
        "CLAUDE_CODE_OAUTH_TOKEN"
    )
    if s.container and not signed_in:
        p.result(
            "warn",
            "Inside a container Claude Code needs a sign-in token: run claude setup-token on a "
            "computer where you are signed in, and give the result to the container as "
            "TIFFIN_CLAUDE_OAUTH_TOKEN_FILE.",
        )


def step_model(s: State, p: Prompter) -> None:
    a = s.args
    p.explain(
        "Tiffin can ask an AI model to suggest menus, estimate meals you did not plan and answer "
        "questions in the chat. The model never does the sums: Tiffin works out every portion "
        "and nutrient itself. Without one, you plan meals yourself on the web page or with "
        "/plan in the bot."
    )
    current = str(s.get("model.provider", after=False))
    provider = p.choose(
        "Which should Tiffin use",
        PROVIDER_CHOICES,
        # a first run suggests no model: the packaged default would spend a Claude plan unasked
        a.model_provider or ("none" if s.first_run else str(s.get("model.provider"))),
        flag="--model-provider",
        aliases={"claude": "claude-cli", "compatible": "openai-compatible"},
    )
    same = provider == current and not s.first_run
    s.set("model.provider", provider)
    if provider == "none":
        s.set("model.model", "")
        s.set("model.base_url", "")
        s.model_tested = None
        p.result("info", "Menus and estimates will be yours to make; Tiffin still does the sums.")
        return
    model = a.model_name or (str(s.get("model.model", after=False)) if same else "")
    base = ""
    if provider == "claude-cli":
        _claude_ready(s, p)
    if provider == "openai-compatible":
        base = p.ask(
            "Address of the service",
            a.model_base_url or (str(s.get("model.base_url", after=False)) if same else ""),
            flag="--model-base-url",
            check=check_base_url,
        )
        model = p.ask(
            "Name of the model on that service",
            model,
            flag="--model-name",
            shown="keep the saved name" if model and not a.model_name else "",
            check=lambda text: text.strip(),
        )
    s.set("model.model", model)
    s.set("model.base_url", base)
    if provider != "claude-cli":
        s.secrets.pop("model_api_key", None)
        from_env = s.env_secret("model_api_key")
        if from_env:
            p.say(f"  The key comes from {from_env} in the environment.")
            _keep_copy(s, "model_api_key", from_env, s.environ["TIFFIN_MODEL_API_KEY"])
        else:
            saved = s.saved_secret("model_api_key")
            answer = p.ask_secret(
                "Key for that service, if it needs one"
                if provider == "openai-compatible"
                else f"{'Anthropic' if provider == 'anthropic' else 'OpenAI'} API key",
                saved=s.style.mask(saved) if saved else None,
                given=a.model_key or "",
                flag="--model-key",
                required=provider != "openai-compatible",
            )
            s.secrets.pop("model_api_key", None)
            if answer and answer != saved:
                s.secrets["model_api_key"] = answer
    try:
        s.config()
    except ConfigError as e:
        p.result("fail", str(e))
        if not p.interactive:
            raise
        raise Redo from None
    if not same and not s.first_run:
        p.result("info", "Changing the model asks each person for their consent again.")
    p.explain(
        "Each person decides in Telegram whether their data may go to the model: the bot asks, "
        "and /consent changes it."
    )
    changed = not same or "model_api_key" in s.secrets or model != s.get("model.model", after=False)
    _test_model(s, p, provider, suggest=changed)


def _test_model(s: State, p: Prompter, provider: str, *, suggest: bool) -> None:
    """One tiny request, only after saying what it may cost, and only if the person agrees
    (unattended: only with --model-test yes). Suggested when something about the model changed."""
    a = s.args
    p.explain(
        "Tiffin can check the model now by sending it one very short request. "
        + TEST_COST[provider]
    )
    wanted = p.confirm("Check it now", suggest) if p.interactive else a.model_test == "yes"
    if not wanted:
        s.model_tested = None
        return
    while True:
        p.say(f"  Asking the model{s.style.ellipsis}")
        try:
            _probe(s)
        except Exception as e:  # any failure is said in words; the person decides what next
            problem = _model_problem(e)
            p.result("fail", problem)
            if not p.interactive:
                raise ConfigError(f"model check: {problem}") from None
            choice = p.choose(
                "What now",
                [
                    ("again", "Try again"),
                    ("change", "Change these answers"),
                    ("keep", "Keep them anyway (tiffin doctor checks again later)"),
                ],
                "again",
            )
            if choice == "change":
                raise Redo from None
            if choice == "keep":
                s.model_tested = False
                return
            continue
        s.model_tested = True
        p.result("ok", "The model answered.")
        return


def _icu_problem(s: State, key: str) -> str | None:
    """None when intervals.icu accepts the key; otherwise what to do about it."""
    day = (s.deps.clock or Clock(str(s.get("env.tz")))).today_local().isoformat()
    auth = base64.b64encode(f"API_KEY:{key}".encode()).decode("ascii")
    try:
        response = get(
            s.deps.transport,
            f"{s.get('icu.base_url')}/athlete/0/wellness?oldest={day}&newest={day}",
            headers={"Authorization": f"Basic {auth}"},
            timeout_s=15,
        )
    except TransportError:
        return "Could not reach intervals.icu. Check the internet connection and try again."
    if response.status == 200:
        return None
    if response.status in (401, 403):
        return "intervals.icu did not accept that key. Copy it again from Developer Settings."
    return f"intervals.icu answered with an error (HTTP {response.status}). Try again later."


def step_training(s: State, p: Prompter) -> None:
    a = s.args
    p.explain(
        "Optional. Tiffin can read your activities and daily wellness, such as weight, from "
        "intervals.icu, a free service you can link to Garmin and other watches. Skip this if "
        "you do not use it."
    )
    people = [a.person] if a.person else ["owner", "partner"] if s.two() else ["owner"]
    if p.interactive and not any(s.secret(f"icu_key_{slug}") for slug in people):
        p.explain(
            "To get a key: sign in to intervals.icu, open Settings and find Developer Settings "
            "near the bottom of the page. Make an API key there, then paste it here."
        )
    for slug in people:
        name = f"icu_key_{slug}"
        whose = "Your" if slug == "owner" else "Your partner's"
        s.secrets.pop(name, None)
        from_env = s.env_secret(name)
        if from_env:
            key = s.environ[f"TIFFIN_{name.upper()}"]
            problem = _icu_problem(s, key)
            p.result(
                "fail" if problem else "ok",
                f"{whose} key comes from {from_env}: " + (problem or "intervals.icu accepts it."),
            )
            if problem and not p.interactive:
                raise ConfigError(f"{from_env}: {problem}")
            if not problem:
                _keep_copy(s, name, from_env, key)
            continue
        saved = s.saved_secret(name)
        given = a.icu_key if a.icu_key and slug == (a.person or "owner") else ""
        while True:
            answer = p.ask_secret(
                f"{whose} intervals.icu API key",
                saved=s.style.mask(saved) if saved else None,
                given=given,
                flag="--icu-key",
                required=False,
            )
            if answer is None:
                p.result(
                    "ok" if saved else "info",
                    "Kept the saved key."
                    if saved
                    else "Skipped; add it later with tiffin setup training.",
                )
                break
            p.say(f"  Checking it with intervals.icu{s.style.ellipsis}")
            problem = _icu_problem(s, answer)
            if problem is None:
                if answer != saved:
                    s.secrets[name] = answer
                p.result("ok", "intervals.icu accepted the key.")
                break
            p.result("fail", problem)
            if not p.interactive:
                raise ConfigError(f"--icu-key: {problem}")
            given = ""
            p.say("  Paste it again, press Enter to skip, or type back.")


WEB_CHOICES = (
    ("off", "Off: use Telegram only"),
    ("local", "On this computer only"),
    ("proxy", "At your own web address, behind a web server you run (such as Caddy or nginx)"),
    ("tailscale", "On your Tailscale network, through Tailscale Serve"),
)


def step_web(s: State, p: Prompter) -> None:
    a = s.args
    p.explain(
        "Optional. A web page with today's plan, menus, shopping lists and progress. Nobody has "
        "a password: the bot sends each person a link that signs them in."
    )
    now = web_mode(s, after=False)
    choices = list(WEB_CHOICES)
    if s.container:
        choices[1] = ("local", "Inside this container only (nothing outside it can reach it)")
    mode = p.choose(
        "Where should the page be",
        choices,
        a.web_mode or ("local" if s.first_run else web_mode(s)),
        flag="--web-mode",
    )
    port_key = "web.port_prod" if s.env == "prod" else "web.port_dev"
    if mode == "off":
        s.set("web.enabled", False)
        p.result("info", "Telegram only. /plan in the bot does what the page's planner does.")
        return
    same = mode == now and not s.first_run
    s.set("web.enabled", True)
    if mode == "local":
        origin = a.web_origin or (
            str(s.get("web.origin", after=False)) if same else f"http://127.0.0.1:{s.get(port_key)}"
        )
        parts = urlsplit(origin)
        if parts.scheme not in ("http", "https") or parts.hostname not in LOOPBACK:
            raise ConfigError(
                "--web-origin: on this computer only, the address is http://127.0.0.1:<port>"
            )
        try:
            port = parts.port
        except ValueError:
            raise ConfigError("--web-origin: the port is not a number") from None
        if port:
            s.set(port_key, port)
        root = a.web_root_path or parts.path.rstrip("/")
        if same and not a.web_root_path and not parts.path.strip("/"):
            root = str(s.get("web.root_path", after=False))
        s.set("web.origin", f"{parts.scheme}://{parts.netloc}")
        s.set("web.root_path", check_root_path(root) if root else "")
        s.set("web.identity_mode", "telegram")
        s.set("web.trusted_header_proxy", False)
        s.set("web.host", "127.0.0.1")
        s.set("web.bind_trusted_network", False)
        _check_config(s, p)
        address = f"{parts.scheme}://{parts.netloc}{s.get('web.root_path')}/"
        p.result("info", f"Open {address} in a browser here, then send /web to the bot to sign in.")
        return
    tailscale = mode == "tailscale"
    current_origin = str(s.get("web.origin", after=False)) if same else ""
    address = p.ask(
        "This computer's Tailscale address" if tailscale else "Web address people will open",
        a.web_origin or current_origin,
        flag="--web-origin",
        check=check_https_address,
    )
    parts = urlsplit(address)
    origin = f"https://{parts.netloc}"
    root_default = (
        a.web_root_path
        or parts.path
        or (str(s.get("web.root_path", after=False)) if same else "/tiffin" if tailscale else "")
    )
    root = p.ask(
        "Path on that address, if Tiffin is not at the top",
        root_default,
        flag="--web-root-path",
        required=False,
        empty="none: at the top",
        check=check_root_path,
    )
    listen = "loopback"
    if not tailscale:
        listen_now = "network" if s.get("web.bind_trusted_network", after=False) else "loopback"
        listen = p.choose(
            "Where does your web server run",
            [
                ("loopback", "On this computer (Tiffin listens on 127.0.0.1 only)"),
                (
                    "network",
                    "In another container (Tiffin listens on the private network they share)",
                ),
            ],
            a.web_listen or ("network" if s.container else listen_now),
            flag="--web-listen",
        )
    s.set("web.origin", origin)
    s.set("web.root_path", root)
    s.set("web.identity_mode", "header" if tailscale else "telegram")
    s.set("web.trusted_header_proxy", tailscale)
    s.set("web.host", "0.0.0.0" if listen == "network" else "127.0.0.1")
    s.set("web.bind_trusted_network", listen == "network")
    p.explain(
        "Each person's consent message says where their records are kept and what the page "
        "passes through on its way to them, so make both true for this machine. For example: "
        "a rented server in the EU, run by Alex."
    )
    records = p.ask(
        "Your Tiffin records are held on",
        a.hosting_records or str(s.get("hosting.records")),
        flag="--hosting-records",
        check=check_words(True),
    )
    front = p.ask(
        "On its way to a browser, the page passes through (for example Cloudflare)",
        a.hosting_web_front
        or str(s.get("hosting.web_front") or ("Tailscale" if tailscale else "")),
        flag="--hosting-web-front",
        required=False,
        empty="nothing",
        check=check_words(False),
    )
    s.set("hosting.records", records)
    s.set("hosting.web_front", front)
    _check_config(s, p)
    port = s.get(port_key)
    path = f" and keep the path {root}" if root else ""
    if tailscale:
        p.result("info", f"In Tailscale Serve, send {root or '/'} to http://127.0.0.1:{port}.")
        p.result(
            "info",
            "Then tell Tiffin each person's Tailscale login: tiffin link-tailscale owner "
            "you@example.com",
        )
    elif listen == "loopback":
        p.result("info", f"Point your web server at http://127.0.0.1:{port}{path}.")
    else:
        p.result(
            "info",
            f"Point your web server at port {port} of this container (in Docker Compose, the "
            f"service's name and that port){path}.",
        )
        p.result(
            "warn", "Never publish that port to the internet: only the web server may reach it."
        )


def _check_config(s: State, p: Prompter) -> None:
    try:
        s.config()
    except ConfigError as e:
        p.result("fail", str(e))
        if not p.interactive:
            raise
        raise Redo from None


def step_shopping(s: State, p: Prompter) -> None:
    a = s.args
    p.explain(
        "Shopping lists use typical pack sizes from Tiffin's own food list, so they work "
        "anywhere with nothing to set up."
    )
    current = list(s.get("shop.retailers", after=False) or [])
    if s.get("env.country") != "GB" and not current and not a.retailers:
        p.result(
            "info",
            "Reading real products from supermarket websites is only available for UK shops, so "
            "there is nothing more to set here.",
        )
        s.set("shop.retailers", [])
        s.set("shop.retailer", "")
        s.set("chrome.shop_refresh_enabled", False)
        return
    p.explain(
        "Optionally, Tiffin can also read real products and prices from UK supermarket websites. "
        "It reads their pages in Chrome on this computer, through Claude Code and its Chrome "
        "extension, and only ever reads: it never clicks, types or buys."
    )
    links = a.product_links or ("yes" if current else "no")
    if not p.confirm("Read supermarket products", links == "yes"):
        s.set("shop.retailers", [])
        s.set("shop.retailer", "")
        s.set("chrome.shop_refresh_enabled", False)
        return
    chosen = p.ask(
        "Which shops, in order of preference (" + ", ".join(RETAILERS) + ")",
        a.retailers or ", ".join(current) or "sainsburys",
        flag="--retailers",
        check=check_retailers,
    )
    retailers = [r.strip() for r in chosen.split(",")]
    s.set("shop.retailers", retailers)
    s.set("shop.retailer", retailers[0])
    s.set("chrome.shop_refresh_enabled", True)
    if s.get("model.provider") != "claude-cli" or not s.deps.which("claude"):
        p.result(
            "warn",
            "This needs Claude Code on this computer, signed in, and Chrome with Claude's "
            "extension.",
        )


@dataclass(frozen=True)
class Section:
    key: str
    title: str
    run: Callable[[State, Prompter], None]
    show: Callable[[State, bool], str]
    flags: tuple[str, ...]
    #: what skipping the step on a first run sets, so a skip never leaves a packaged default
    #: (a model, a placeholder web address) switched on without being asked for
    on_first_skip: Mapping[str, Any] = field(default_factory=dict)
    #: what a skip on a first run means, in words
    skipped: str = "Skipped."


SECTION_LIST: tuple[Section, ...] = (
    Section(
        "home",
        "Home and time zone",
        step_home,
        show_home,
        ("timezone", "country", "currency", "weight_unit", "height_unit"),
    ),
    Section(
        "people", "People", step_people, show_people, ("owner_name", "partner", "partner_name")
    ),
    Section(
        "telegram",
        "Telegram",
        step_telegram,
        show_telegram,
        ("telegram_token", "owner_chat_id", "pair"),
    ),
    Section(
        "model",
        "AI model",
        step_model,
        show_model,
        ("model_provider", "model_name", "model_key", "model_base_url", "model_test"),
        {"model.provider": "none", "model.model": "", "model.base_url": ""},
        "Skipped: no model for now; tiffin setup model adds one.",
    ),
    Section("training", "Training data", step_training, show_training, ("icu_key", "icu_athlete")),
    Section(
        "web",
        "Web page",
        step_web,
        show_web,
        (
            "web_mode",
            "web_origin",
            "web_root_path",
            "web_listen",
            "hosting_records",
            "hosting_web_front",
        ),
        {"web.enabled": False},
        "Skipped: the web page is off; tiffin setup web turns it on.",
    ),
    Section(
        "shopping",
        "Shopping",
        step_shopping,
        show_shopping,
        ("product_links", "retailers"),
        {"shop.retailers": [], "shop.retailer": "", "chrome.shop_refresh_enabled": False},
    ),
)


BY_KEY = {section.key: section for section in SECTION_LIST}


SECTION_NAMES = tuple(BY_KEY)


#: what `tiffin setup <part>` accepts; intervals is the name `tiffin connect` used
SECTIONS = frozenset({*SECTION_NAMES, "intervals"})


REQUIRED = frozenset({"people", "telegram"})


class MoveHome(Exception):
    def __init__(self, home: Path) -> None:
        super().__init__(str(home))
        self.home = home


def _home_given(args: argparse.Namespace, environ: Mapping[str, str]) -> bool:
    return bool(args.home or environ.get("TIFFIN_HOME"))
