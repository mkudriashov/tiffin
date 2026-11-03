# Configuration reference

`tiffin setup` writes only local overrides to `TIFFIN_HOME/tiffin.toml`, changing only the lines it sets, and keeps a copy of the previous file under `state/setup/`. Unspecified keys use the packaged defaults below. Never put a token or API key in TOML; use `TIFFIN_HOME/secrets/` or the supported environment variable. This table is generated from `config/defaults.toml` and checked by a test.

## env

Runtime mode, country and local time zone. Days use this zone; timestamps stay UTC.

| Key | Default | Meaning |
| --- | --- | --- |
| `env.default` | `"prod"` | Default. |
| `env.tz` | `"Europe/London"` | Tz. |
| `env.country` | `"GB"` | Two-letter country code; setup pre-fills it from the time zone. GB is the supported country for the bundled food catalogue. |

## persons

Initial names and optional chat IDs. A chat ID should normally be paired by the wizard.

| Key | Default | Meaning |
| --- | --- | --- |
| `persons.owner.display_name` | `""` | Display name. |
| `persons.owner.chat_id` | `0` | Optional owner chat ID for scripted setup; normally paired by a code. |
| `persons.partner.display_name` | `""` | Display name. |
| `persons.partner.chat_id` | `0` | Optional partner chat ID for scripted setup; normally paired by a code. |

## telegram

Polling timeouts, message limits and owner alert delivery.

| Key | Default | Meaning |
| --- | --- | --- |
| `telegram.poll_timeout_s` | `50` | Poll timeout s. |
| `telegram.http_timeout_extra_s` | `15` | Http timeout extra s. |
| `telegram.max_chars` | `4000` | Max chars. |
| `telegram.admin_slug` | `"owner"` | Admin slug. |
| `telegram.alert_cooldown_min` | `180` | Alert cooldown min. |

## web

Loopback web listener, URL and identity mode. See [web access](web-access.md).

| Key | Default | Meaning |
| --- | --- | --- |
| `web.enabled` | `true` | Enabled. |
| `web.host` | `"127.0.0.1"` | Host. |
| `web.bind_trusted_network` | `false` | `true` lets the page bind an address other than loopback, for a container on a network only your reverse proxy can reach. Never publish that port directly. |
| `web.port_prod` | `7788` | Port prod. |
| `web.port_dev` | `7789` | Port dev. |
| `web.root_path` | `"/tiffin"` | URL mount path with ordinary segments; use an empty string when served at `/`. |
| `web.identity_mode` | `"telegram"` | `telegram` (default) uses a one-use bot link; `header` trusts Tailscale Serve and needs `trusted_header_proxy`; `signed_link` is retained for existing installations. |
| `web.trusted_header_proxy` | `false` | Must be `true` for `identity_mode = "header"`: it says a proxy you control sets the login header and removes any copy a client sends. |
| `web.signed_out_redirect` | `""` | Where a signed-out visitor to the page root is sent: a path beginning with `/` or an `https://` URL. Empty shows the sign-in instructions. |
| `web.origin` | `"https://tiffin.example.ts.net"` | Browser-facing HTTP(S) origin, with no path or credentials. HTTPS is required away from loopback for Telegram sign-in. |
| `web.secure_cookies` | `true` | Secure cookies for development HTTPS; production HTTPS always uses Secure cookies. |

## units

Display units for body measurements and currency for manual product prices.

| Key | Default | Meaning |
| --- | --- | --- |
| `units.weight` | `"kg"` | Weight. |
| `units.height` | `"cm"` | Height. |
| `units.currency` | `"GBP"` | ISO currency code for prices entered by hand. |

## shopping

Whether a shopping list counts on food already at home. Off by default: every list buys everything its menu eats.

| Key | Default | Meaning |
| --- | --- | --- |
| `shopping.count_on_home` | `false` | `false`: every shopping list buys everything its menu eats and counts on nothing already at home, so you check your cupboards while shopping. `true`: a list buys less for a pack opened on an earlier day of the same shop window and for stock someone has reported. A person's own "always at home" list keeps a food off either way. |

## hosting

Plain words for the consent text about the machine running tiffin, so what people agree to is true of it.

| Key | Default | Meaning |
| --- | --- | --- |
| `hosting.records` | `"this computer"` | Where the records are held, finishing "Your tiffin records are held on ..."; for example `a rented server in the EU, run by Sam`. |
| `hosting.web_front` | `""` | Anything the web page passes through on its way to a browser, such as `Cloudflare`; empty if nothing does. Named in the consent text. |

## household

Whether a second person is expected during onboarding.

| Key | Default | Meaning |
| --- | --- | --- |
| `household.partner_expected` | `false` | Partner expected. |

## chrome

Optional Claude CLI browser profile and shop-reader switches.

| Key | Default | Meaning |
| --- | --- | --- |
| `chrome.profile_directory` | `"Default"` | Profile directory. |
| `chrome.shop_refresh_enabled` | `false` | Enable optional retailer product refresh through Claude CLI browser tools. |
| `chrome.shop_fill_enabled` | `false` | Allow the optional shop reader to fill missing products. |

## shop

No retailer is selected by default. Catalogue packs work on their own. Retailer links are optional; `{q}` is the encoded search term.

| Key | Default | Meaning |
| --- | --- | --- |
| `shop.retailers` | `[]` | Enabled retailer adapters in preferred order; empty means catalogue only. |
| `shop.retailer` | `""` | Legacy single-retailer setting; still read when retailers is empty. |
| `shop.batch` | `6` | Batch. |
| `shop.bought_elsewhere` | `[]` | Bought elsewhere. |
| `shop.search_urls.tesco` | `"https://www.tesco.com/groceries/en-GB/search?query={q}"` | Tesco. |
| `shop.search_urls.sainsburys` | `"https://www.sainsburys.co.uk/gol-ui/SearchResults/{q}"` | Sainsburys. |
| `shop.search_urls.ocado` | `"https://www.ocado.com/search?q={q}"` | Ocado. |
| `shop.search_urls.marks_spencer` | `"https://www.ocado.com/search?q=M%26S%20{q}"` | Marks spencer. |
| `shop.search_urls.wholefoods` | `"https://www.amazon.co.uk/s?k=whole+foods+market+{q}"` | Wholefoods. |

## models

Claude CLI executable/model settings and spending controls. The USD cap applies only to Claude CLI.

| Key | Default | Meaning |
| --- | --- | --- |
| `models.claude_model` | `"opus"` | Claude model. |
| `models.cap_usd` | `false` | When true, API providers degrade to manual because their spending cap cannot be enforced here. |
| `models.chrome_model` | `"haiku"` | Chrome model. |
| `models.claude_exe` | `"claude"` | Claude exe. |
| `models.claude_block_initial_min` | `30` | Claude block initial min. |
| `models.claude_block_max_min` | `300` | Claude block max min. |
| `models.shuffle_catalogue` | `true` | Shuffle catalogue. |

## model

Chosen provider and endpoint. API keys belong in the secrets directory; see [models](models.md).

| Key | Default | Meaning |
| --- | --- | --- |
| `model.provider` | `"claude-cli"` | `claude-cli`, `anthropic`, `openai`, `openai-compatible` or `none`. |
| `model.model` | `""` | Model. |
| `model.base_url` | `""` | API root for `openai-compatible`, without credentials. |
| `model.auth_style` | `"bearer"` | `bearer`, or `api-key` for a compatible endpoint that requires that header. |
| `model.api_requests_per_day` | `50` | Maximum API HTTP attempts per household local day, including retries and doctor probes. This is a request count, not a USD cap. |

## budgets

Each task has `[timeout seconds, maximum turns, maximum USD]`. USD enforcement is provider-specific.

| Key | Default | Meaning |
| --- | --- | --- |
| `budgets.menu_propose` | `[420, 3, 2.5]` | Menu propose. |
| `budgets.menu_shared` | `[300, 3, 3.0]` | Menu shared. |
| `budgets.menu_one_dish` | `[180, 3, 2.0]` | Menu one dish. |
| `budgets.menu_choices` | `[420, 3, 4.0]` | Menu choices. |
| `budgets.offplan_estimate` | `[90, 1, 0.3]` | Offplan estimate. |
| `budgets.sunday_prose` | `[120, 1, 0.4]` | Sunday prose. |
| `budgets.chat` | `[180, 8, 0.6]` | Chat. |
| `budgets.shop_refresh` | `[1800, 60, 2.0]` | Shop refresh. |
| `budgets.plan_parse` | `[90, 1, 0.3]` | Plan parse. |
| `budgets.intent` | `[60, 1, 0.2]` | Intent. |

## schedule

Scheduler times use the household time zone; intervals are seconds or minutes as named.

| Key | Default | Meaning |
| --- | --- | --- |
| `schedule.tick_s` | `30` | Seconds between scheduler checks. |
| `schedule.icu_sync_minute` | `10` | Minute of each hour for intervals.icu sync. |
| `schedule.day_close` | `"00:05"` | Daily close time. |
| `schedule.menu_propose_days_before` | `2` | How far ahead to build a menu. |
| `schedule.menu_propose_time` | `"18:00"` | Main menu build time; the 12:00 retry remains fixed. |
| `schedule.shopping_list_time` | `"18:00"` | Personal shopping list time. |
| `schedule.shopping_joint_time` | `"18:00"` | Shared shopping list time. |
| `schedule.weekly_calibration` | `"17:30"` | Weekly calibration time. |
| `schedule.sunday_summary` | `"19:00"` | Sunday summary time. |
| `schedule.backup` | `"03:30"` | Daily backup and log pruning time. |

## guardrails

Limits for targets, calibration and menu sizing. Change only with an understanding of the solver.

| Key | Default | Meaning |
| --- | --- | --- |
| `guardrails.rate_pct_min` | `0.5` | Rate pct min. |
| `guardrails.rate_pct_max` | `1.0` | Rate pct max. |
| `guardrails.max_loss_kg_week` | `0.8` | Max loss kg week. |
| `guardrails.auto_adjust_kcal` | `150` | Auto adjust kcal. |
| `guardrails.auto_adjust_protein_g` | `10` | Auto adjust protein g. |
| `guardrails.min_usable_days` | `5` | Min usable days. |
| `guardrails.ewma_alpha` | `0.1` | Ewma alpha. |
| `guardrails.kcal_tolerance_pct` | `5` | Kcal tolerance pct. |
| `guardrails.fibre_min` | `25` | Fibre min. |
| `guardrails.fibre_max` | `35` | Fibre max. |
| `guardrails.kcal_per_kg_fat` | `7700` | Kcal per kg fat. |
| `guardrails.mains_per_window` | `2` | Mains per window. |

## icu

intervals.icu API URL, sync lookback windows and rate budget.

| Key | Default | Meaning |
| --- | --- | --- |
| `icu.base_url` | `"https://intervals.icu/api/v1"` | Base url. |
| `icu.wellness_days` | `14` | Wellness days. |
| `icu.activity_days` | `3` | Activity days. |
| `icu.wellness_days_warm` | `90` | Wellness days warm. |
| `icu.activity_days_warm` | `30` | Activity days warm. |
| `icu.budget_per_hour` | `60` | Budget per hour. |

## ops

Heartbeat, log and backup retention settings.

| Key | Default | Meaning |
| --- | --- | --- |
| `ops.heartbeat_s` | `30` | Seconds between process heartbeats. |
| `ops.heartbeat_stale_s` | `180` | Age in seconds at which health checks mark a heartbeat stale. |
| `ops.log_keep_days` | `90` | Days to keep tiffin log files; old files are removed by the backup job. |
| `ops.backup_keep_days` | `30` | Days to keep dated database backups. |

## Local overrides

Place only changed values in `tiffin.toml`, for example:

```toml
[env]
tz = "Europe/London"
[web]
identity_mode = "telegram"
origin = "http://127.0.0.1:7788"
```

Optional `[model.tasks]` entries name a model for a task whose key appears under `[budgets]`; no task overrides are set by default. `TIFFIN_HOME` chooses the data directory and `TIFFIN_ENV` chooses `dev` or `prod`.
