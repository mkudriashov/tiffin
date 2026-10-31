# Install and run

Python 3.14 and `uv` are required. From the checkout, run `uv sync`, then
`uv run tiffin init`. The wizard pairs the owner's Telegram chat, writes the
configuration and offers to install the background service. `uv run tiffin run`
runs it in the foreground.
Build the included food catalogue with `uv run tiffin catalogue build` before
planning menus. `tiffin doctor` reports the process, database, recent jobs,
backup and disk health.
The wizard pairs the owner. Use `tiffin pair partner` if a second person joins.

`TIFFIN_HOME` selects the data directory. Without it the app uses
`%APPDATA%\\tiffin` on Windows, `~/Library/Application Support/tiffin` on macOS,
or `$XDG_DATA_HOME/tiffin` (falling back to `~/.local/share/tiffin`) on Linux.
The directory holds `tiffin.toml`, `tiffin.db`, `backups/`, `state/`, `logs/`,
`exports/`, `dryrun/`, `archives/icu/streams/` and `secrets/`. Put local settings
in `tiffin.toml`; packaged defaults are in `config/defaults.toml`. A development
instance uses its own `TIFFIN_HOME` and `TIFFIN_ENV=dev`, with
`TIFFIN_TELEGRAM_TOKEN_DEV` or `secrets/telegram-token-dev.txt`.

For a Linux user service, copy `deploy/tiffin.service` to
`~/.config/systemd/user/tiffin.service`, adjust `ExecStart` to the installed
entrypoint, then run `systemctl --user enable --now tiffin`.
For macOS, copy `deploy/org.example.tiffin.plist` to `~/Library/LaunchAgents/`,
adjust its executable path, then run `launchctl bootstrap gui/$(id -u)
~/Library/LaunchAgents/org.example.tiffin.plist`.
For Windows, run this in PowerShell from the checkout to register a logon task:

```powershell
$action = New-ScheduledTaskAction -Execute (Resolve-Path .venv\Scripts\tiffin.exe).Path -Argument 'run' -WorkingDirectory (Get-Location).Path
$trigger = New-ScheduledTaskTrigger -AtLogOn
Register-ScheduledTask -TaskName tiffin -Action $action -Trigger $trigger
```

Set the task's restart policy in Task Scheduler. `docker compose up -d --build`
uses a named volume and exposes no ports by default; set
`TIFFIN_TELEGRAM_TOKEN` in the shell before starting it, or put the token in
`/data/secrets/telegram-token.txt` in the volume.
For a new Docker volume, run the wizard first:

```text
docker compose run --rm tiffin uv run --no-sync tiffin init --web-mode off --model-provider none
```

It prints a one-time code. Send that code to the bot while the command waits,
then start the service with `docker compose up -d --build`.
The optional supermarket reader is disabled by default. Enable
`chrome.shop_refresh_enabled` and `chrome.shop_fill_enabled` in `tiffin.toml`
only after configuring its browser profile.

The wizard records the household's time zone. AI tasks are manual when no model
is configured. The Docker image does not install the Claude CLI.
