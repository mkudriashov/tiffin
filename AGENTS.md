# Tiffin — contributor conventions

A self-hosted nutrition, menu, shopping and training app for one person or a household of two.
Python 3.14 (uv). One process, `tiffin run`: Telegram long-poll, one worker per person, a scheduler
and an optional web page. British English everywhere.

## Conventions that are enforced, not suggested

- **Append-only.** Every table not in `db.PROJECTION_TABLES` / `db.STATE_TABLES` is a ledger with
  no-update/no-delete triggers. Corrections are new rows; readers take the latest row.
- **UTC space-form stamps** (`clock.utc_stamp()`), never ISO-T. Days are ISO dates in the
  household's time zone; `clock.today_local()` is the only "today". Never `datetime.now()` outside
  `clock.py`.
- **Degrade to manual, never to silence.** Every job run writes a `run_ledger` row
  (`ok | degraded | failed | skipped`); problems show on the owner's status strip.
- **Per-person scoping comes from identity, never from input** (the Telegram chat, or the signed-in
  web session). No route, tool or handler accepts a person from path/query/body/arguments.
- **The model never does arithmetic.** Macros come from grams × per-100 g in code; the model
  returns indexes into numbered candidate lists, never names or numbers that are then trusted.
- **Privacy.** A person's data goes to an AI provider only while that person has granted consent.
  No consent or no model means the job degrades to manual.
- **Secrets are files** in the secrets directory, never in the repo, config, DB or logs. Logs carry
  `person_id` integers only, never names or message bodies.
- **Tests run on fixtures, never a real database**, with fakes for Telegram, intervals.icu and the
  model, and `FrozenClock` for time. Green before anything is called done.
- **No emoji in messages. Food is on plan or off plan, never good or bad.**

## Commands

    uv sync
    .venv\Scripts\python.exe -m pytest -q
    .venv\Scripts\ruff.exe check . ; .venv\Scripts\ruff.exe format --check .
    .venv\Scripts\pyright.exe
