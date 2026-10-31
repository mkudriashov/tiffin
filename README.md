# Tiffin

Tiffin is a self-hosted nutrition, menu, shopping and training app for one person or a household of two. It runs a Telegram bot, scheduled jobs and an optional web page in one Python process, with a SQLite database in your own data directory.

**Status: early, 0.1.0.**

## What it does

- Plans meals from your own energy and nutrient targets, then calculates portions from ingredient data in code.
- Builds aisle-sorted, pack-aware shopping lists from the included catalogue, keeps track of what is already at home and supports batch cooking. No browser, retailer, product link or price is required.
- Offers weekly dish choices, recipe cards and shared weekend menus for two people, with separate portions and private targets.
- Logs meals and daily confirmations through Telegram, including off-plan meals and corrections.
- Reads Garmin-linked activity and wellness data through intervals.icu; shows training, weight trends, progress and tape measurements.

The AI model suggests or selects from numbered candidates. It does not calculate nutrients. Each person decides whether their data may be sent to the configured model provider; without consent or a model, model-assisted work becomes manual.

The web page lets you add recipes and plan meals yourself from catalogue foods. With the page off, `/plan` in Telegram provides the same manual recipe, menu and shopping path. Saving a manual menu calculates its nutrient totals and shopping list without an AI model.

## Screenshots

| Today | Menu | Shopping | Progress | Telegram |
| --- | --- | --- | --- | --- |
| <img src="docs/images/today-phone.png" alt="Today page" width="145"> | <img src="docs/images/menu-phone.png" alt="Menu page" width="145"> | <img src="docs/images/shopping-phone.png" alt="Shopping page" width="145"> | <img src="docs/images/progress-phone.png" alt="Progress page" width="145"> | <img src="docs/images/telegram.png" alt="Example Telegram conversations" width="145"> |

<p align="center">
  <img src="docs/images/menu-wide.png" alt="Menu page on a wide screen" width="800">
</p>

[Browse every screen](docs/screenshots.md), or run `uv run tiffin demo` to explore the web page with made-up data. Today and Menu use the same fictional captures as the portfolio; the other screen captures use the disposable demo. The demo needs no bot, model key or network connection and deletes its data when stopped. To make local screenshots, run `python tools/screenshots.py` after installing Playwright and its Chromium browser.

## Quick start

Install Python 3.14 and [uv](https://docs.astral.sh/uv/), then from this checkout:

```sh
uv sync
uv run tiffin init
uv run tiffin catalogue build
uv run tiffin doctor
```

The wizard walks through creating your bot with @BotFather, pairing your Telegram chat, choosing a country and model, and optionally connecting intervals.icu, the web page and retailer links. Start the service as instructed by the wizard and send `/start` to your bot.

The catalogue supplies typical unbranded packs. To use a product you buy instead, open a shopping line on the page and choose **Use my product**, or run `uv run tiffin product add chicken_breast "Chicken breast 500 g" --pack-g 500`. Price, shop and HTTPS link are optional. Your product is used on future lists; each person's entries are private.

The bundled food data and optional retailer readers are UK based. GB is the supported country for the food catalogue; other country settings are accepted, with a setup warning, but their food names and packs are not validated.

For Docker, initialise the persistent volume, then start the service:

```sh
docker compose run --rm tiffin uv run --no-sync tiffin init --non-interactive --owner-name Alex --country GB --timezone Europe/London --web-mode off --model-provider none
docker compose up -d --build
```

Supply the bot token through `TIFFIN_TELEGRAM_TOKEN` before running the non-interactive Docker command. It waits for a pairing code sent to the bot. See [Docker setup](docs/deployment.md#docker) for details. The image does not contain Claude CLI; choose an API provider, a reachable local model or `none`. Shopping lists still work from the catalogue.

## Requirements

Python 3.14, uv, a Telegram bot token and a writable data directory are required for a normal installation. A model is optional; supported connections are Claude CLI, Anthropic, OpenAI and OpenAI-compatible endpoints. An intervals.icu account is optional for Garmin-linked training and wellness data. The web page is optional and listens on loopback by default.

## Privacy and configuration

Your database, logs, exports, backups and secret files stay under `TIFFIN_HOME` (or your platform's user data directory). Telegram receives the messages you send to and receive from your bot. The chosen AI provider receives a person's relevant data only while that person has granted consent for that provider. intervals.icu receives authorised API requests and returns activity and wellness data only if you connect it. The optional shop reader can access retailer sites when enabled. See [privacy](docs/privacy.md) for detail.

`tiffin init` writes local overrides to `TIFFIN_HOME/tiffin.toml`; the complete defaults are packaged in `config/defaults.toml`. Secrets belong in `TIFFIN_HOME/secrets/` or supported environment variables, never in TOML. See [configuration](docs/configuration.md) and [model providers](docs/models.md).

## Guides

- [Getting started](docs/getting-started.md)
- [Configuration reference](docs/configuration.md)
- [Model providers](docs/models.md)
- [Web access](docs/web-access.md)
- [Deployment and backups](docs/deployment.md)
- [Architecture](docs/architecture.md)
- [Privacy](docs/privacy.md)
- [Contributing](docs/contributing.md)

The code is licensed under [Apache-2.0](LICENSE). The included CoFID food data has a separate [Open Government Licence notice](NOTICE).

Contributions are welcome via [pull requests](https://github.com/mkudriashov/tiffin/pulls). [Open an issue](https://github.com/mkudriashov/tiffin/issues) for bugs or suggestions; read the [contributing guide](docs/contributing.md) first.
