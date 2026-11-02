# Changelog

## 0.1.0

- First packaged release with Telegram onboarding, meal planning, shopping, training and an optional web page.
- Added provider consent, offline manual degradation, operator health checks and a disposable example demo.

## Unreleased

- Container: `web.bind_trusted_network` (off by default) allows binding beyond loopback behind a proxy; `CLAUDE_CODE_OAUTH_TOKEN` now reaches the Claude CLI child.
- Sign-in: Telegram is the default; `header` mode needs `web.trusted_header_proxy`; the sign-in link opens a confirm button instead of being used by a preview; cookies are `SameSite=Lax`; devices are signed out after 14 idle days (schema 6, `web_device_seen`); `web.signed_out_redirect`; the manifest is fetched with credentials.
- Container: hardened Dockerfile and compose example, `python -m tiffin.healthcheck`, doctor recognises a container, `TIFFIN_<NAME>_FILE` secrets, `TIFFIN_CLAUDE_OAUTH_TOKEN` for the Claude CLI on a server.
- Linking: two people connect with a code (`/link`, or More on the page) before they can share weekends; sharing needs a link and both people's yes (`linked_and_both_consented`). Either can unlink at once; built weekends stay as two ordinary menus and the open joint shopping list closes into one copy each. Schema 7: `links`, `link_events`, `link_attempts`, `shopping_list_closures`, `link_id` on `shared_menus` and `shopping_lists`. `links.record_migrated` records an existing pair.
- Consent text now names the machine (`[hosting]`), Telegram, the model destination and anything fronting the page; new grants are recorded as `v4`, earlier ones are honoured unchanged.
- Review fixes: a weekend stored while no link is active is stored as two ordinary menus (a `NULL` link means only "from before links"); the page's write token is signed to the session with a server key (`web-csrf-key`), not a cookie; public `/api/health` returns the status word only; `env = "dev"` is refused with a public https `web.origin`; `tiffin doctor` warns when a public https address is paired with default `[hosting]` words; erasing a linked person gives the other their copy of the open joint list; the compose example uses `restart: on-failure:5`.
