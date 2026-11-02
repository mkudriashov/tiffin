# Deployment, backups and upgrades

Set `TIFFIN_HOME` to an absolute directory owned by the service account. By default tiffin uses the platform's user data directory. The directory contains `tiffin.toml`, `tiffin.db`, `secrets/`, `backups/`, `logs/`, `exports/` and state files. Keep the whole directory private and persistent. Run one tiffin process per home: the process lock prevents a second poller from using the same bot and database.

## Services

`deploy/tiffin.service` is a Linux systemd user service; adjust its executable and working directory, copy it to `~/.config/systemd/user/`, then run `systemctl --user enable --now tiffin`. `deploy/org.example.tiffin.plist` is a macOS LaunchAgent template; adjust paths and load it with `launchctl bootstrap`. On Windows, create a Task Scheduler task whose executable is `.venv\Scripts\tiffin.exe`, arguments are `run` and working directory is the checkout. Run `tiffin doctor` after starting it.

## Docker

The `Dockerfile` builds from pinned base images (version and digest) and installs the Claude CLI at a pinned version. The image runs as a non-root user (build argument `TIFFIN_UID`, default 10010; `user:` in compose overrides it), keeps `HOME` and `TMPDIR` under `/data`, writes no bytecode, works with a read-only root filesystem and a tmpfs `/tmp`, and starts `tiffin run` directly. Its health check (`python -m tiffin.healthcheck`) asks the page's own `<web.root_path>/api/health` over loopback, or reads the heartbeat when the page is off.

`docker-compose.yml` is a hardened example: read-only root, all capabilities dropped, `no-new-privileges`, memory, CPU and process limits, log rotation, no published port, and secrets mounted as files. Create `secrets/telegram-token.txt` (and `secrets/claude-oauth-token.txt`, from `claude setup-token`, if the Claude CLI is your model; it is passed to the CLI as `TIFFIN_CLAUDE_OAUTH_TOKEN_FILE`), make them readable by the container user, then run the wizard against the volume and start the service:

```sh
docker compose run --rm tiffin tiffin init --web-mode proxy --web-origin https://tiffin.example.com
docker compose up -d --build
```

A proxy in another container reaches the page over the compose network, so the volume's `tiffin.toml` needs `host = "0.0.0.0"` and `bind_trusted_network = true` under `[web]`. Do that only when that network is reachable by the proxy alone, and never publish the port. Any secret can also be supplied as a file with `TIFFIN_<NAME>_FILE` (for example `TIFFIN_TELEGRAM_TOKEN_FILE=/run/secrets/telegram_token`). Run one container per data volume and per bot token; stop an old poller on another machine first. Set `hosting.records` and `hosting.web_front` in `tiffin.toml` so the consent text names the machine and anything the page passes through. Everything above has been checked as text only; build and start it on a Linux host before relying on it.

The compose example restarts the container with `on-failure:5`, not `unless-stopped`. If another copy of the same Telegram bot is already polling (an older install still running), Tiffin exits straight away each time, and an unlimited restart would loop and begin its jobs again on every start. Five tries, then it stays stopped and says why in its logs. Stop the old copy first.

## Missed messages

`tiffin status` prints a `missed_slots` list: each expected morning or evening message that has not gone 15 minutes after its time, with the job, the slot, the person's internal id, the due time (UTC), a reason (`no_run`, `run_failed`, `stuck`, `ok_without_message`, `send_failed`) and `late` (still inside its catch-up window) or `missed` (past it). Empty means every message went or was withheld on purpose: a job that had nothing to say records a skipped run with its reason, a paused person and a quiet-window drop leave a dropped message, and those are never listed. `tiffin doctor` shows the same as "expected messages" (red for missed, warning for late). A monitor can run `docker compose exec -T tiffin tiffin status` and alert when `missed_slots` is not empty.

## Backups and upgrades

The scheduler writes backups under `TIFFIN_HOME/backups/`; `ops.backup_keep_days` controls retention. Back up the complete home, including the database, configuration and secrets, to storage you control. For a manual copy of a running SQLite database, use SQLite's backup API or stop the service first; copying only the live `tiffin.db` may omit WAL changes. Restore into a stopped instance and run `tiffin doctor` before resuming.

Before upgrading, make a backup and read [CHANGELOG](../CHANGELOG.md). Stop the process, update the checkout, run `uv sync --frozen`, then start it. Schema migrations run on startup. Check `tiffin doctor` and the owner's status strip for degraded or failed jobs after an upgrade.

## Repeatable install check

After `uv sync --frozen`, run `uv run --no-sync python tools/platform_smoke.py` on each platform you support. It uses a disposable directory inside the checkout and no external credentials. It checks the installed command, a no-model example household, web routes, SQLite integrity, backup and restore. Run the fixture suite as well with `uv run --no-sync pytest -q -p no:cacheprovider`.

Before publishing a platform walkthrough, complete the remaining live checks on a clean machine: create a bot and pair it, start the background service, open a Telegram web link through the intended HTTPS proxy, then exercise backup restore and upgrade. Docker also needs a fresh-volume build and pairing check. The offline smoke check cannot prove those external paths.
