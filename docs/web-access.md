# Web access

The web page is optional. It binds to `127.0.0.1` by default; `web.host` stays a loopback address unless you opt in with `web.bind_trusted_network = true`, for a container whose network only your reverse proxy can reach (see the last section). The default production port is 7788 and the default mount path is `/tiffin`. For ordinary local access, choose Telegram sign-in in `tiffin init`, open the local origin in a browser and send `/web` to your paired bot. The bot returns a five-minute, one-use link; the page then uses a device session. `tiffin devices` lists sessions and `tiffin devices --sign-out ID` revokes one.

Tailscale Serve can expose the loopback page to your tailnet. In header identity mode, the trusted Serve proxy supplies `Tailscale-User-Login`; bind each person's login with `tiffin link-tailscale owner alex@example.com` or `tiffin link-tailscale partner sam@example.com`. Binding the same login again transfers access to the newly named person. Configure Serve so clients cannot supply that header themselves. Do not expose header mode through a proxy that forwards client-supplied identity headers.

For a small server with a domain, configure `tiffin init --web-mode proxy --web-origin https://tiffin.example.com` (replace the example domain with your own). Keep tiffin bound to `127.0.0.1`. A Caddy site on the same server can use:

```caddyfile
tiffin.example.com {
    reverse_proxy 127.0.0.1:7788
}
```

Point the domain at the server and allow HTTPS traffic to Caddy, then start tiffin and the proxy. Caddy obtains a certificate for a reachable public domain. Send `/web` to the paired bot, open the one-use link and confirm that `tiffin devices` lists the new session. Sign out that session with `tiffin devices --sign-out ID` and confirm the page requires a fresh link. The proxy should pass the original host and scheme so the public origin matches the cookie and CSRF checks. Use Telegram sign-in; header identity mode requires a trusted proxy that removes client-supplied identity headers. For a path-based proxy, match `web.root_path` to the public path and verify both the link and page assets. See [configuration](configuration.md) for the web keys and [privacy](privacy.md) before exposing the page.

For a new remote deployment, run `uv run --no-sync python tools/check_web_access.py https://tiffin.example.com` from the checkout after starting the proxy. Add `--root-path /tiffin` if the configured mount path is `/tiffin`. The check reaches the public HTTPS route, verifies that an unsigned visitor cannot read `/api/me`, and sends a forged identity header to check that it grants no access. Then send `/web`, open the one-use link, and verify that the signed-in page loads and `tiffin devices` shows the new session. Finally, revoke that session and verify that its page access ends. Repeat this check after changing the proxy, origin or mount path.

`tiffin demo` is a separate temporary loopback server with example data. Its sign-in bypass is available only through the demo application class, never through `tiffin.toml` or the normal `tiffin run` command.

## Sign-in details

- **Telegram sign-in is the default.** `web.identity_mode = "header"` is refused unless `web.trusted_header_proxy = true`, which says that a proxy you control sets the login header and strips any copy a client sends. `tiffin init --web-mode tailscale` sets both.
- **The link opens a page with a button.** Opening `?k=...` shows a "Sign in on this device" page and spends nothing, so a chat app's link preview cannot use up the one use. The button posts the token back; only then is the device signed in. The page sends `Referrer-Policy: no-referrer`, is never cached and is not indexed.
- **The cookie is `HttpOnly`, `Secure` on HTTPS, scoped to `web.root_path` and `SameSite=Lax`.** `Strict` would hide it from a page opened by a link in another app, so the page would look signed out. Writes still need a CSRF token signed to that session with a key the server keeps (`secrets/web-csrf-key`, made on first use) and the configured origin. No cookie carries the token, so another app on the same address cannot plant one. Public `/api/health` answers the status word only.
- **A device lasts 90 days and is signed out after 14 days without a request.** Use is recorded at most every 12 hours (the `web_device_seen` ledger), and `tiffin devices` and the page's device list show only devices still in.
- **One origin.** Writes are accepted only from `web.origin` (and refused when the browser marks them cross-site). When the domain changes, change `web.origin` and the proxy together, then sign in again.
- **Signed-out visitors.** `web.signed_out_redirect` sends a signed-out visitor to the page root to a path (`/tiffin/overview/`) or an `https://` address of your choice; empty shows the plain instructions. The API and every other page keep refusing.
- **Behind a container proxy.** Set `web.host = "0.0.0.0"` and `web.bind_trusted_network = true` only when the container's network is reachable by the proxy alone. The proxy may strip or keep the `web.root_path` prefix; both work.
- **One Telegram poller per bot.** A second `tiffin run` on the same data directory stops with "another tiffin holds the lock" (exit 3), and a second poller on another machine gets Telegram's conflict error. Stop the old one first.
