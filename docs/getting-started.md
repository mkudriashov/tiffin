# Getting started

## Install and set up

Install Python 3.14 and uv, clone the repository and run `uv sync`. Then run `uv run tiffin setup` from the checkout. It is a guided set-up in eight steps:

1. **Home and time zone.** The folder for Tiffin's settings, database, backups and secret keys (your user data folder unless you choose another or set `TIFFIN_HOME`), then your time zone, country, currency and units.
2. **People.** Just you, or you and a partner, and what Tiffin should call you.
3. **Telegram.** Your bot's token, checked with Telegram, then your own chat paired by a one-time code you send to the bot. With a partner, it can make a code for them too.
4. **AI model.** None, Claude through Claude Code on this computer, Anthropic, OpenAI or another service that works like OpenAI's. Setup can check it with one very short request; it says first what that may cost, and you can say no.
5. **Training data.** An intervals.icu key for each person, checked with intervals.icu. Optional.
6. **Web page.** Off, on this computer only, at your own address behind a web server, or on your Tailscale network. For an address others can reach it also asks the words the consent message uses for where the records are held.
7. **Shopping.** Lists work from Tiffin's own food list; reading products from UK supermarket websites is optional.
8. **Save.** A review of everything, then nothing is written until you say yes.

Each question shows its suggested answer in [brackets]; Enter takes it. Type `back` for the previous step, `skip` to leave an optional step as it is, or `quit` to stop without saving. A token or key is never shown as you type it, and only its last four characters are ever printed. Settings go in `tiffin.toml`, and tokens and keys go in `secrets/` as files only you can read. At the end setup offers to load the food list and to start Tiffin in the background when you log in, runs the same checks as `tiffin doctor` and lists what to do next.

Run `uv run tiffin setup` again at any time. It shows what is set and lets you change one part, or go straight to one with `uv run tiffin setup telegram` (or `home`, `people`, `model`, `training`, `web`, `shopping`). The rest of `tiffin.toml`, its comments included, stays as it was, a copy of the previous file is kept under `state/setup/`, and a saved token or key is kept unless you type a new one. `tiffin init` and `tiffin connect <part>` still work and run the same set-up.

For a script or a container, add `--non-interactive` and give the answers as flags (`uv run tiffin setup --help` lists them) and the secrets as environment variables: `TIFFIN_TELEGRAM_TOKEN`, `TIFFIN_MODEL_API_KEY`, `TIFFIN_ICU_KEY_OWNER` and `TIFFIN_ICU_KEY_PARTNER`, or the same names ending `_FILE` that name a file holding the value. Nothing is asked; a missing or wrong answer stops setup with exit code 2 before anything is written. On an existing install it changes only the parts its flags name. The model is checked only with `--model-test yes`. Your chat is paired by waiting for the code (as before), by `--pair later`, which prints a code to send once Tiffin is running, or by `--owner-chat-id`.

Use `uv run tiffin doctor --offline` to check the local set-up without contacting anything.

## Create a Telegram bot

In Telegram, open the verified `@BotFather` account and send `/newbot`. Answer its questions (a name, then a username), then copy the token it gives you into setup when asked. Treat this token like a password. Setup checks the token with Telegram and gives you a one-time pairing code. Send the code as a plain message to your new bot while setup is waiting. Send `/start` when setup finishes. To repeat this step, run `uv run tiffin setup telegram`.

For a second person, run `uv run tiffin pair partner` and give the code to Sam. Each person then uses their own Telegram chat. Shared weekends require both people's agreement through `/consent`.

Joining and linking are two separate steps. `tiffin pair partner` only lets a second person use their own Telegram chat and account. Until you are **linked**, every weekend menu is your own. To link, one of you sends `/link new` (or uses More on the page) and gives the code to the other, who sends `/link` with that code or types it into their own page. Codes have eight characters, work once, run out after 48 hours, and five wrong tries in an hour stop further guesses. Once linked, each of you still says yes to sharing weekend menus with `/consent`; linked people see only the plates and macros of dishes they cook together, never the other's targets, weight, logs, notes or avoid list. Either of you can unlink at any time, at once, without the other's agreement (`/link unlink`, or More): a weekend already built stays as two ordinary menus, later weekends are built apart, the open joint shopping list is closed and each of you gets your own copy of its lines, and the other person is told in neutral words. What the other person has already seen cannot be taken back. A Tiffin install holds two people at most.

If you are moving an existing pair into a new install, `tiffin.links.record_migrated(db, owner_id, partner_id)` records them as linked (method `migrated`) without asking either again and without writing any consent rows.

## Choose a model

Setup can configure Claude CLI, Anthropic, OpenAI, an OpenAI-compatible endpoint or no model. API keys go in the secrets directory, never in `tiffin.toml`. Each person reviews `/consent` before their data is sent to the chosen provider. See [models](models.md) for endpoints and cost controls. Run `uv run tiffin setup model` to change the provider later; a provider change requires fresh consent.

## Connect intervals.icu, if wanted

Create an intervals.icu account and connect your Garmin account there. In intervals.icu, open Settings and find Developer Settings near the bottom of the page; make an API key there. Paste it when `tiffin setup` asks; the key says which athlete it belongs to, so no athlete ID is needed. Use `tiffin setup training` to do this later. Tiffin reads wellness and activity data through intervals.icu; it does not connect directly to Garmin. Each person's connection is separate. `tiffin icu-backfill owner --days 180` can pull older history after setup.

## Start using it

Setup loads the included food catalogue when it is empty (or run `uv run tiffin catalogue build` yourself), then start Tiffin with `uv run tiffin run`, unless you chose the background service setup offers. In Telegram, use `/start`. If a model is configured, decide whether to allow it to process your data. Then enter sex for the energy formula, date of birth, height, current weight, activity outside training, and a rate of weight loss (zero to maintain). Your first targets appear immediately. Defaults apply to menus, cooking, training, measurements and message times until you answer optional questions on later days. One daytime prompt offers up to three questions each day, each with a Skip button. Use `/followup` to resume; `/set` and the page let you change settings later. `/menu`, `/shopping`, `/batch` and `/web` are available once their related data or page is ready. `tiffin doctor` checks the running process and recent jobs; `tiffin status` summarises liveness, people, messages and the last runs (as JSON for scripts, or with `--json`).

With no model or without model consent, open **Menu → Plan meals yourself** on the web page. Add a recipe using foods from your catalogue, ingredient grams and cooking steps. Then choose dishes for each meal, set portions, and save a menu for one day or repeat it for up to seven days. Tiffin calculates nutrients from the food data and makes a shopping list from the saved menu. You can save different menus for different days. A manual menu is your own choice; review its nutrient totals against your targets on the Menu page. Telegram `/cook repeat` can also carry an earlier menu into the next cook without a model.

The included food data is UK CoFID data, and the optional retailer readers are UK based. **GB is the supported country for food names, nutrient coverage, generic packs and retailer links.** Setup accepts other two-letter country codes for time zone, body units and currency, but warns that the food catalogue has not been validated for those countries. Review ingredients and packs before planning there. If the currency is not known, enter its three-letter code yourself. Product links are optional. With no retailer selected, a whole week's menu produces an aisle-sorted shopping list with needed quantities and generic catalogue packs, including on a server or in Docker. The list can be forwarded from Telegram or the page without prices or links.

Shopping lists use the food a plan has already opened within the same shop window before adding another pack: for example, a Monday pack can cover Wednesday's meal in that shop. A later shop never subtracts planned surplus from an earlier shop window. If you have it, use **I have it** on the list; your answer removes that food from the list for its days. Foods you mark as always at home also stay off your lists.

To use a product you buy, open a shopping line on the page and choose **Use my product**. Enter its name and pack size in grams; price, shop and HTTPS link can be left empty. On the local command line, use `uv run tiffin product add chicken_breast "Chicken breast 500 g" --pack-g 500` and optionally `--price 4.50 --shop "Example shop" --url https://example.com/product`. A manual product takes effect on the next list for that person.

With the web page off and no model, send `/plan` to the paired bot. It shows how to find food codes, save a recipe, list recipe numbers and create a one to seven day menu. Saving the menu builds its shopping list; `/shopping` shows it. The bot uses the same recipe validation, nutrient calculation and shopping builder as the web page.

Manual prices use the configured currency. Currencies with no fractional unit, including JPY, accept whole prices; GBP, EUR, USD and other currencies currently accept up to two decimal places. Stored prices and older shopping lists remain in hundredths of the currency, so changing currency later does not reinterpret their amounts.

If you enable a UK retailer reader, this machine needs logged-in Chrome with the Claude extension and Claude CLI. The reader only reads pages; the browser gate refuses clicks, typing and JavaScript. The old `shop.retailer` and `shop.retailers` settings remain supported for existing installations.

For a no-credentials preview, run `uv run tiffin demo` and open the printed loopback address. The data is made up and removed when the demo exits.
