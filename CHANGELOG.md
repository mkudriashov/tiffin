# Changelog

## 0.1.0

- First packaged release with Telegram onboarding, meal planning, shopping, training and an optional web page.
- Added provider consent, offline manual degradation, operator health checks and a disposable example demo.

## Unreleased

- Container: `web.bind_trusted_network` (off by default) allows binding beyond loopback behind a proxy; `CLAUDE_CODE_OAUTH_TOKEN` now reaches the Claude CLI child.
- Sign-in: Telegram is the default; `header` mode needs `web.trusted_header_proxy`; the sign-in link opens a confirm button instead of being used by a preview; cookies are `SameSite=Lax`; devices are signed out after 14 idle days (schema 6, `web_device_seen`); `web.signed_out_redirect`; the manifest is fetched with credentials.
