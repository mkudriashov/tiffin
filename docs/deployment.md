# Deployment, backups and upgrades

Set `TIFFIN_HOME` to an absolute directory owned by the service account. By default tiffin uses the platform's user data directory. The directory contains `tiffin.toml`, `tiffin.db`, `secrets/`, `backups/`, `logs/`, `exports/` and state files. Keep the whole directory private and persistent. Run one tiffin process per home: the process lock prevents a second poller from using the same bot and database.

## Services

`deploy/tiffin.service` is a Linux systemd user service; adjust its executable and working directory, copy it to `~/.config/systemd/user/`, then run `systemctl --user enable --now tiffin`. `deploy/org.example.tiffin.plist` is a macOS LaunchAgent template; adjust paths and load it with `launchctl bootstrap`. On Windows, create a Task Scheduler task whose executable is `.venv\Scripts\tiffin.exe`, arguments are `run` and working directory is the checkout. Run `tiffin doctor` after starting it.

## Docker

The included `Dockerfile` and `docker-compose.yml` use a persistent named volume and expose no port by default. Run the wizard against the volume before starting the service:

```sh
docker compose run --rm tiffin uv run --no-sync tiffin init --web-mode off --model-provider none
docker compose up -d --build
```

Supply `TIFFIN_TELEGRAM_TOKEN` in the environment or put the token in `/data/secrets/telegram-token.txt` in the volume. Send the printed pairing code to the bot while init waits. The image does not include Claude CLI; configure an API provider or reachable compatible server for model work. Publish the web port only behind a suitable HTTPS or Tailscale proxy.

## Backups and upgrades

The scheduler writes backups under `TIFFIN_HOME/backups/`; `ops.backup_keep_days` controls retention. Back up the complete home, including the database, configuration and secrets, to storage you control. For a manual copy of a running SQLite database, use SQLite's backup API or stop the service first; copying only the live `tiffin.db` may omit WAL changes. Restore into a stopped instance and run `tiffin doctor` before resuming.

Before upgrading, make a backup and read [CHANGELOG](../CHANGELOG.md). Stop the process, update the checkout, run `uv sync --frozen`, then start it. Schema migrations run on startup. Check `tiffin doctor` and the owner's status strip for degraded or failed jobs after an upgrade.

## Repeatable install check

After `uv sync --frozen`, run `uv run --no-sync python tools/platform_smoke.py` on each platform you support. It uses a disposable directory inside the checkout and no external credentials. It checks the installed command, a no-model example household, web routes, SQLite integrity, backup and restore. Run the fixture suite as well with `uv run --no-sync pytest -q -p no:cacheprovider`.

Before publishing a platform walkthrough, complete the remaining live checks on a clean machine: create a bot and pair it, start the background service, open a Telegram web link through the intended HTTPS proxy, then exercise backup restore and upgrade. Docker also needs a fresh-volume build and pairing check. The offline smoke check cannot prove those external paths.
