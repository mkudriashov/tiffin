# Product proposals

This is the prioritised decision list for a public release. **S** means one small area, **M** spans components, and **L** needs a new workflow, data source or migration. Delivered decisions are recorded below the open items.

## Highest priority open items

| Priority | What | Why | Cost | Risk | Recommendation |
| --- | --- | --- | --- | --- | --- |
| 1 | Validate installation and first use on clean Windows, macOS, Linux and Docker systems; publish a canonical project URL and current screenshots. | The offline smoke script covers the CLI, no-model demo, web routes, database integrity and backup restore, but it cannot prove package download, bot pairing, background services, HTTPS sign-in or upgrade on those systems. | M | Platform packaging and external services can make a written walkthrough stale. | **Do** before public release. |
| 3 | Make safe web access easier to configure and verify on a laptop or small server. | Telegram device sign-in, the Caddy guide and proxy tests exist, but a remote user still has to supply an HTTPS origin and configure the proxy correctly. Header mode relies on a proxy that removes client-supplied identity headers. | M | An unsafe proxy or origin can expose private data; session, CSRF and origin checks must remain effective. | **Do** a guided setup and live deployment check before calling remote web access easy. |

Priority 1 progress: the offline smoke check now invokes the installed console entrypoint. Clean-machine installation, bot pairing, background services and upgrade still require networked platform runs. Screenshot capture was skipped because Playwright and Chromium are absent here. The repository is published at `https://github.com/mkudriashov/tiffin`.

Priority 3 progress: the web guide now includes a repeatable HTTPS and identity-header probe in `tools/check_web_access.py`. A live proxy and device sign-in check remain outstanding.

## Stage 12 follow-up

| What | Why | Cost | Risk | Recommendation |
| --- | --- | --- | --- | --- |
| Guide manual recipe entry through Telegram prompts | `/plan` now makes a first menu possible without a web page or model, but typing food codes and a delimited recipe in one message is cumbersome on a phone. | M | A resumable editor must keep person scoping and the shared manual validation while handling interrupted chats. | **Do** after the first-menu path has been used on a real phone. |

## Other open decisions

| Priority | What | Why | Cost | Risk | Recommendation |
| --- | --- | --- | --- | --- | --- |
| 6 | Support the optional shop reader with API model providers. | The reader currently needs Claude CLI and its Chrome integration even when another provider handles the rest of tiffin. | L | Retailer pages change and an overly broad browser gate could expose unrelated pages. | **Discuss** after the core installation paths are proven. |
| 7 | Support three-decimal currencies for manually entered product prices. | Zero-decimal currencies are accepted and shown as whole amounts, but prices are stored in hundredths, so currencies with three fractional digits cannot be represented accurately. | M | A storage migration must preserve the meaning of old prices and list snapshots. | **Discuss** when a supported country needs it. |
| 8 | Replace the Plotly bundle with smaller charts. | The lazy-loaded trends bundle adds more than a megabyte when that page opens. | M | Hover details, labels, accessibility and axis accuracy can regress. | **Discuss** after release basics. |
| 9 | Decide whether model-written Sunday prose is a supported feature. | Its prompt, schema, budget and helper exist, while the scheduled summary sends template prose. | M | Enabling it adds consent, cost and failure paths; retiring it changes a documented model task. | **Discuss** the intended behaviour, then wire it with manual degradation or remove the unused path. |

## Decisions already implemented

These proposals are retained here so that they are not raised or implemented twice. Their existing behaviour still needs regression coverage.

| Priority | Delivered | What was done |
| --- | --- | --- |
| 2 | Telegram-only manual planning | Added `/plan` food search, recipe entry and menu entry through the shared manual validation and shopping builder. |
| 4 | API model spending policy | Kept provider billing controls as the monetary ceiling; the daily request limit remains a count, as stated in the model guide. |
| 5 | Catalogue scope | Kept GB as the supported bundled food catalogue scope; other locale settings remain available without claiming local food data. |

| What | Why | Cost | Risk | Recommendation |
| --- | --- | --- | --- | --- |
| Configurable household time zone | A fixed London calendar gave the wrong day elsewhere. | M | Partial conversion could split day boundaries. | **Do not** duplicate: `env.tz` drives the clock and web timestamps. |
| Catalogue-first shopping with an optional retailer adapter | Shopping must work without a browser. | L | Generic packs may become stale. | **Do not** duplicate: generic packs and manual products work without the reader. |
| Neutral retailer defaults and country setup | A new install should not assume a UK shop. | M | Existing shop settings must keep working. | **Do not** duplicate: no retailer is selected by default and setup asks for country. |
| Short first Telegram conversation | A long interview delayed the first useful day. | L | Optional answers need defaults and resumable prompts. | **Do not** duplicate: targets come first and optional questions follow later. |
| Model provider choice | Claude CLI alone was an extra prerequisite and unavailable in Docker. | L | Providers differ in schemas and tools. | **Do not** duplicate: Claude CLI, Anthropic, OpenAI, compatible endpoints and manual mode are configurable. |
| Provider-specific consent | Consent to one processor or endpoint must not authorise another. | M | Provider changes pause assisted work until consent is renewed. | **Do not** duplicate: consent includes the configured provider scope. |
| Planned leftover policy | Lists should use packs opened in the same shop window and honour reported stock. | M | Historical list snapshots keep their old totals. | **Do not** duplicate: the current list builder uses one policy. |
| Operator-set daily API request limit | Paid calls needed an enforceable bound even without provider pricing. | M | Request counts cannot guarantee a spending ceiling. | **Do not** duplicate: attempts are reserved atomically before sending and documented as a count. |
| Manual web menu and recipe editor | People without model access or consent need to plan food and produce a list. | L | A second calculation path could diverge. | **Do not** duplicate: the signed-in page uses shared nutrient and shopping calculations. |
| GB catalogue scope in setup | Accepting any country did not mean the food data was localised. | S | A warning alone does not make other catalogues available. | **Do not** duplicate: setup and the guides state the GB limitation. |
| Caddy HTTPS guide and web session tests | Remote web access needed a safe documented path beyond Tailscale. | M | A real proxy can differ from the test configuration. | **Do not** duplicate: the guide and tests are in place; finish the live check in priority 3. |
