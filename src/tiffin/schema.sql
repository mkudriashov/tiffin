-- tiffin schema. Three table classes (see db.py):
--   ledgers     append-only; db.migrate() adds BEFORE UPDATE / BEFORE DELETE RAISE(ABORT) triggers
--   projections rebuilt each run, no triggers (icu_wellness, icu_activity)
--   state       mutable, listed explicitly (bot_state, conversation_state, run_ledger, icu_sync_state)
-- Every person-scoped row carries person_id. Readers take the latest row (views v_*_latest).
-- Stamps are UTC space-form; days are ISO dates in the configured time zone.

CREATE TABLE IF NOT EXISTS schema_version (
    version    INTEGER NOT NULL,
    applied_at TEXT    NOT NULL
);

-- ---------------------------------------------------------------- people, identity, consent
CREATE TABLE IF NOT EXISTS persons (
    id              INTEGER PRIMARY KEY,
    slug            TEXT NOT NULL UNIQUE CHECK (slug IN ('owner', 'partner')),
    display_name    TEXT NOT NULL,
    tone            TEXT NOT NULL DEFAULT 'neutral' CHECK (tone IN ('blunt', 'neutral', 'warm')),
    tailscale_login TEXT UNIQUE COLLATE NOCASE,
    erased_at       TEXT,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS telegram_identities (
    id         INTEGER PRIMARY KEY,
    person_id  INTEGER NOT NULL REFERENCES persons(id),
    chat_id    INTEGER NOT NULL UNIQUE,
    method     TEXT NOT NULL CHECK (method IN ('config', 'pairing_code')),
    created_at TEXT NOT NULL
);
-- A person may have successive Telegram identities; the newest row is active.
CREATE TRIGGER IF NOT EXISTS trg_ident_cap BEFORE INSERT ON telegram_identities
WHEN (
    SELECT COUNT(DISTINCT person_id) FROM telegram_identities
    WHERE person_id <> NEW.person_id
) >= 2
BEGIN SELECT RAISE(ABORT, 'two people hard cap'); END;

-- Web identities are separate ledger rows because persons is append-only.
CREATE TABLE IF NOT EXISTS tailscale_identities (
    id         INTEGER PRIMARY KEY,
    person_id  INTEGER NOT NULL REFERENCES persons(id),
    login      TEXT NOT NULL COLLATE NOCASE,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_tailscale_person ON tailscale_identities(person_id, id DESC);
CREATE INDEX IF NOT EXISTS ix_tailscale_login ON tailscale_identities(login COLLATE NOCASE, id DESC);

-- persons is append-only with a UNIQUE slug, so display_name can only ever be written once.
-- A name set later - by the person or by the operator - is a row here, newest wins.
CREATE TABLE IF NOT EXISTS person_names (
    id         INTEGER PRIMARY KEY,
    person_id  INTEGER NOT NULL REFERENCES persons(id),
    name       TEXT NOT NULL,
    set_by     TEXT NOT NULL DEFAULT 'self' CHECK (set_by IN ('self', 'operator')),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_person_names ON person_names(person_id, id DESC);

CREATE TABLE IF NOT EXISTS pairing_codes (
    id          INTEGER PRIMARY KEY,
    person_id   INTEGER NOT NULL REFERENCES persons(id),
    code_sha256 TEXT NOT NULL UNIQUE,
    expires_at  TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pairing_uses (
    id              INTEGER PRIMARY KEY,
    pairing_code_id INTEGER NOT NULL UNIQUE REFERENCES pairing_codes(id),
    chat_id         INTEGER NOT NULL,
    used_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS web_login_links (
    id INTEGER PRIMARY KEY,
    person_id INTEGER NOT NULL REFERENCES persons(id),
    token_hash TEXT NOT NULL UNIQUE,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS web_link_uses (
    id INTEGER PRIMARY KEY,
    link_id INTEGER NOT NULL UNIQUE REFERENCES web_login_links(id),
    used_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS web_devices (
    id INTEGER PRIMARY KEY,
    person_id INTEGER NOT NULL REFERENCES persons(id),
    token_hash TEXT NOT NULL UNIQUE,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS web_device_events (
    id INTEGER PRIMARY KEY,
    device_id INTEGER NOT NULL REFERENCES web_devices(id),
    action TEXT NOT NULL CHECK (action = 'sign_out'),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS web_device_seen (
    id INTEGER PRIMARY KEY,
    device_id INTEGER NOT NULL REFERENCES web_devices(id),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_web_device_seen ON web_device_seen(device_id, id DESC);

CREATE TABLE IF NOT EXISTS consents (
    id               INTEGER PRIMARY KEY,
    person_id        INTEGER NOT NULL REFERENCES persons(id),
    kind             TEXT NOT NULL DEFAULT 'model_processing',
    granted          INTEGER NOT NULL CHECK (granted IN (0, 1)),
    wording_version  TEXT NOT NULL,
    message_in_id    INTEGER,
    created_at       TEXT NOT NULL
);

-- ---------------------------------------------------------------- profile, targets
CREATE TABLE IF NOT EXISTS profiles (
    id                INTEGER PRIMARY KEY,
    person_id         INTEGER NOT NULL REFERENCES persons(id),
    effective_from    TEXT NOT NULL,
    sex               TEXT NOT NULL CHECK (sex IN ('m', 'f')),
    birth_date        TEXT NOT NULL,
    height_cm         REAL NOT NULL CHECK (height_cm BETWEEN 120 AND 230),
    activity_factor   REAL NOT NULL CHECK (activity_factor BETWEEN 1.1 AND 2.0),
    steps_floor       INTEGER NOT NULL DEFAULT 8000,
    quiet_start       TEXT NOT NULL DEFAULT '22:30',
    quiet_end         TEXT NOT NULL DEFAULT '07:00',
    confirm_time      TEXT NOT NULL DEFAULT '20:30',
    morning_time      TEXT NOT NULL DEFAULT '07:30',
    nudge_time        TEXT NOT NULL DEFAULT '16:00',
    goal_weight_kg    REAL,
    goal_date         TEXT,
    rate_pct_per_week REAL NOT NULL DEFAULT 0.7 CHECK (rate_pct_per_week BETWEEN 0.0 AND 1.0),
    kcal_floor        INTEGER NOT NULL,
    protein_g_per_kg  REAL NOT NULL DEFAULT 2.0,
    fat_g_per_kg_min  REAL NOT NULL DEFAULT 0.7,
    fibre_g_min       INTEGER NOT NULL DEFAULT 25,
    max_loss_kg_week  REAL NOT NULL DEFAULT 0.8,
    variety_json      TEXT NOT NULL DEFAULT '{}',
    segment_days      TEXT NOT NULL DEFAULT '[[0,1,2,3,4]]',
    cycle_tracking    INTEGER NOT NULL DEFAULT 0 CHECK (cycle_tracking IN (0, 1)),
    meals_per_day     INTEGER NOT NULL DEFAULT 4,
    pre_bed_protein   INTEGER NOT NULL DEFAULT 0 CHECK (pre_bed_protein IN (0, 1)),
    tone              TEXT NOT NULL DEFAULT 'neutral' CHECK (tone IN ('blunt', 'neutral', 'warm')),
    refused_foods     TEXT NOT NULL DEFAULT '[]',
    liked_foods       TEXT NOT NULL DEFAULT '[]',
    pantry_json       TEXT NOT NULL DEFAULT '[]',
    equipment_json    TEXT NOT NULL DEFAULT '[]',
    -- which meal slots are cooked in one batch on the cook day; breakfast is usually
    -- made fresh, so the default is the two meals that come out of a pan
    batch_slots       TEXT NOT NULL DEFAULT '["lunch","dinner"]',
    -- whether they freeze what they batch (migration 0017). Without it `/batch` read
    -- `recipes.keep_days` against the fridge and told somebody who cooks once a week and
    -- freezes to cook Thursday's and Friday's portions fresh.
    freezes           INTEGER NOT NULL DEFAULT 0 CHECK (freezes IN (0, 1)),
    -- how often Tiffin talks to them (migration 0035): daily, light (the default: the
    -- morning message only when the day's food is new) or weekly (one Sunday check-in)
    rhythm            TEXT NOT NULL DEFAULT 'light' CHECK (rhythm IN ('daily', 'light', 'weekly')),
    -- what Skip on a meal does to the rest of the day (migration 0043): share its calories over
    -- the plates left, or leave the rest as planned and drop the skipped meal's calories
    skip_mode         TEXT NOT NULL DEFAULT 'share' CHECK (skip_mode IN ('share', 'drop')),
    -- weekly choices (migration 0044): how many dishes each weekday meal is offered each Sunday,
    -- picked on the page and turned into the week's menu (food/choices.py) - 0 is off, the menu
    choices           INTEGER NOT NULL DEFAULT 0 CHECK (choices IN (0, 2, 3, 4)),
    -- how they plan (migration 0045): 'auto' menus built for them, 'choose' picked on Sunday from
    -- `choices` dishes a meal, 'off' targets and tracking only. NULL on rows from before it:
    -- 'choose' when `choices` is above 0, else 'auto' (food/planning.py). /set plan, or More.
    planning          TEXT CHECK (planning IS NULL OR planning IN ('auto', 'choose', 'off')),
    created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_profiles_person ON profiles(person_id, effective_from);

CREATE TABLE IF NOT EXISTS targets (
    id          INTEGER PRIMARY KEY,
    person_id   INTEGER NOT NULL REFERENCES persons(id),
    week_start  TEXT NOT NULL,
    kcal        INTEGER NOT NULL,
    protein_g   INTEGER NOT NULL,
    fat_g       INTEGER NOT NULL,
    carb_g      INTEGER NOT NULL,
    fibre_g     INTEGER NOT NULL,
    source      TEXT NOT NULL CHECK (source IN ('formula', 'calibration', 'proposal', 'auto_adjust', 'manual')),
    basis_json  TEXT NOT NULL DEFAULT '{}',
    proposal_id INTEGER,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_targets_person ON targets(person_id, week_start);

-- ---------------------------------------------------------------- body
CREATE TABLE IF NOT EXISTS weigh_ins (
    id         INTEGER PRIMARY KEY,
    person_id  INTEGER NOT NULL REFERENCES persons(id),
    day        TEXT NOT NULL,
    weight_kg  REAL NOT NULL CHECK (weight_kg BETWEEN 30 AND 250),
    source     TEXT NOT NULL CHECK (source IN ('icu_wellness', 'manual')),
    icu_raw_id INTEGER,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_weigh_ins_person ON weigh_ins(person_id, day);

CREATE TABLE IF NOT EXISTS trend_points (
    id          INTEGER PRIMARY KEY,
    person_id   INTEGER NOT NULL REFERENCES persons(id),
    day         TEXT NOT NULL,
    trend_kg    REAL NOT NULL,
    alpha       REAL NOT NULL,
    n           INTEGER NOT NULL,
    weigh_in_id INTEGER NOT NULL REFERENCES weigh_ins(id),
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_trend_person ON trend_points(person_id, day);

CREATE TABLE IF NOT EXISTS tape_measures (
    id         INTEGER PRIMARY KEY,
    person_id  INTEGER NOT NULL REFERENCES persons(id),
    day        TEXT NOT NULL,
    site       TEXT NOT NULL CHECK (site IN
                 ('waist', 'hip', 'chest', 'arm', 'thigh', 'overarm', 'shoulder', 'sleeve',
                  'neck')),
    cm         REAL NOT NULL CHECK (cm BETWEEN 15 AND 200),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cycle_phases (
    id         INTEGER PRIMARY KEY,
    person_id  INTEGER NOT NULL REFERENCES persons(id),
    day_start  TEXT NOT NULL,
    phase      TEXT NOT NULL CHECK (phase IN ('menstrual', 'follicular', 'ovulation', 'luteal')),
    created_at TEXT NOT NULL
);

-- ---------------------------------------------------------------- intervals.icu mirror
CREATE TABLE IF NOT EXISTS icu_raw (
    id             INTEGER PRIMARY KEY,
    person_id      INTEGER NOT NULL REFERENCES persons(id),
    resource       TEXT NOT NULL CHECK (resource IN ('wellness', 'activity', 'streams_index')),
    source_id      TEXT NOT NULL,
    revision       INTEGER NOT NULL,
    endpoint_class TEXT NOT NULL,
    state          TEXT NOT NULL CHECK (state IN ('live', 'deleted')),
    payload        TEXT,
    payload_sha256 TEXT NOT NULL,
    fetched_at     TEXT NOT NULL,
    endpoint       TEXT NOT NULL,
    sync_id        TEXT NOT NULL,
    UNIQUE (person_id, resource, source_id, revision)
);
CREATE INDEX IF NOT EXISTS ix_icu_raw_head ON icu_raw(person_id, resource, source_id, revision DESC);

CREATE TABLE IF NOT EXISTS icu_stream_index (
    id              INTEGER PRIMARY KEY,
    person_id       INTEGER NOT NULL REFERENCES persons(id),
    icu_activity_id TEXT NOT NULL,
    revision        INTEGER NOT NULL,
    sidecar         TEXT NOT NULL,
    sha256          TEXT NOT NULL,
    bytes           INTEGER NOT NULL,
    samples         INTEGER NOT NULL,
    created_at      TEXT NOT NULL,
    UNIQUE (person_id, icu_activity_id, revision)
);

CREATE TABLE IF NOT EXISTS icu_type_map (
    icu_type TEXT PRIMARY KEY,
    kind     TEXT NOT NULL
);

-- projections (rebuilt per sync; no triggers)
CREATE TABLE IF NOT EXISTS icu_wellness (
    person_id  INTEGER NOT NULL REFERENCES persons(id),
    day        TEXT NOT NULL,
    weight_kg  REAL,
    steps      INTEGER,
    sleep_s    INTEGER,
    hrv        REAL,
    resting_hr INTEGER,
    ctl        REAL,
    atl        REAL,
    tsb        REAL,
    icu_raw_id INTEGER NOT NULL,
    PRIMARY KEY (person_id, day)
);
CREATE TABLE IF NOT EXISTS icu_activity (
    person_id     INTEGER NOT NULL REFERENCES persons(id),
    icu_id        TEXT NOT NULL,
    day           TEXT NOT NULL,
    start_utc     TEXT,
    icu_type      TEXT,
    kind          TEXT,
    name          TEXT,
    moving_s      INTEGER,
    elapsed_s     INTEGER,
    calories      INTEGER,
    avg_hr        INTEGER,
    max_hr        INTEGER,
    load          REAL,
    hr_zones_json TEXT,
    icu_raw_id    INTEGER NOT NULL,
    PRIMARY KEY (person_id, icu_id)
);

-- ---------------------------------------------------------------- training
CREATE TABLE IF NOT EXISTS training_plans (
    id             INTEGER PRIMARY KEY,
    person_id      INTEGER NOT NULL REFERENCES persons(id),
    effective_from TEXT NOT NULL,
    note           TEXT,
    source_text    TEXT,
    model_used     TEXT,
    created_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS planned_sessions (
    id             INTEGER PRIMARY KEY,
    plan_id        INTEGER NOT NULL REFERENCES training_plans(id),
    person_id      INTEGER NOT NULL REFERENCES persons(id),
    weekday        INTEGER NOT NULL CHECK (weekday BETWEEN 0 AND 6),
    kind           TEXT NOT NULL CHECK (kind IN ('climb','ropes','ride','run','strength','mobility','swim','class','walk','other','rest')),
    target_minutes INTEGER NOT NULL DEFAULT 0 CHECK (target_minutes BETWEEN 0 AND 600),
    start_time     TEXT,
    note           TEXT
);
CREATE TABLE IF NOT EXISTS session_matches (
    id                 INTEGER PRIMARY KEY,
    person_id          INTEGER NOT NULL REFERENCES persons(id),
    day                TEXT NOT NULL,
    planned_session_id INTEGER REFERENCES planned_sessions(id),
    icu_id             TEXT,
    status             TEXT NOT NULL CHECK (status IN ('matched', 'confirmed', 'extra', 'rejected', 'missed', 'manual')),
    minutes            INTEGER,
    message_in_id      INTEGER,
    created_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_session_matches ON session_matches(person_id, day);

-- ---------------------------------------------------------------- food catalogue, recipes, menus
CREATE TABLE IF NOT EXISTS ingredients (
    id             INTEGER PRIMARY KEY,
    code           TEXT NOT NULL,
    version        INTEGER NOT NULL,
    name           TEXT NOT NULL,
    cofid_code     TEXT,
    kcal_100       REAL NOT NULL,
    protein_100    REAL NOT NULL,
    fat_100        REAL NOT NULL,
    carb_100       REAL NOT NULL,
    fibre_100      REAL,
    pack_g         REAL NOT NULL,
    pack_label     TEXT NOT NULL,
    aisle          TEXT NOT NULL,
    pantry         INTEGER NOT NULL DEFAULT 0 CHECK (pantry IN (0, 1)),
    use_whole_pack INTEGER NOT NULL DEFAULT 0 CHECK (use_whole_pack IN (0, 1)),
    approved       INTEGER NOT NULL DEFAULT 1 CHECK (approved IN (0, 1)),
    person_scope   TEXT NOT NULL DEFAULT 'both' CHECK (person_scope IN ('both', 'owner', 'partner')),
    created_at     TEXT NOT NULL,
    pack_sizes_g   TEXT NOT NULL DEFAULT '[]',
    UNIQUE (code, version)
);
CREATE TABLE IF NOT EXISTS ingredient_aliases (
    id              INTEGER PRIMARY KEY,
    ingredient_code TEXT NOT NULL,
    alias           TEXT NOT NULL,
    source          TEXT NOT NULL CHECK (source IN ('yaml', 'manual')),
    created_at      TEXT NOT NULL,
    UNIQUE (ingredient_code, alias)
);
CREATE TABLE IF NOT EXISTS ingredient_preferences (
    id              INTEGER PRIMARY KEY,
    person_id       INTEGER NOT NULL REFERENCES persons(id),
    ingredient_code TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('liked', 'ok', 'refused')),
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recipes (
    id           INTEGER PRIMARY KEY,
    name         TEXT NOT NULL,
    servings     INTEGER NOT NULL DEFAULT 1 CHECK (servings BETWEEN 1 AND 20),
    prep_minutes INTEGER NOT NULL CHECK (prep_minutes BETWEEN 0 AND 120),
    method_json  TEXT NOT NULL,
    source       TEXT NOT NULL CHECK (source IN ('model', 'manual')),
    model_used   TEXT,
    person_scope TEXT NOT NULL DEFAULT 'both' CHECK (person_scope IN ('both', 'owner', 'partner')),
    keep_days    INTEGER NOT NULL DEFAULT 3,
    freezable    INTEGER NOT NULL DEFAULT 0 CHECK (freezable IN (0, 1)),
    created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS recipe_ingredients (
    id            INTEGER PRIMARY KEY,
    recipe_id     INTEGER NOT NULL REFERENCES recipes(id),
    ingredient_id INTEGER NOT NULL REFERENCES ingredients(id),
    grams         REAL NOT NULL CHECK (grams > 0)
);
CREATE INDEX IF NOT EXISTS ix_recipe_ingredients_recipe ON recipe_ingredients(recipe_id);
CREATE TABLE IF NOT EXISTS menus (
    id             INTEGER PRIMARY KEY,
    person_id      INTEGER NOT NULL REFERENCES persons(id),
    segment_start  TEXT NOT NULL,
    segment_end    TEXT NOT NULL,
    status         TEXT NOT NULL CHECK (status IN ('proposed', 'accepted', 'repeated', 'superseded')),
    source_menu_id INTEGER,
    model_used     TEXT,
    message_out_id INTEGER,
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_menus_person ON menus(person_id, segment_start);
CREATE TABLE IF NOT EXISTS menu_days (
    id        INTEGER PRIMARY KEY,
    menu_id   INTEGER NOT NULL REFERENCES menus(id),
    day       TEXT NOT NULL,
    kcal      INTEGER NOT NULL,
    protein_g REAL NOT NULL,
    fat_g     REAL NOT NULL,
    carb_g    REAL NOT NULL,
    fibre_g   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_menu_days_menu_day ON menu_days(menu_id, day);
CREATE TABLE IF NOT EXISTS meal_slots (
    id                 INTEGER PRIMARY KEY,
    menu_day_id        INTEGER NOT NULL REFERENCES menu_days(id),
    slot               TEXT NOT NULL CHECK (slot IN ('breakfast', 'lunch', 'dinner', 'snack', 'pre_bed')),
    recipe_id          INTEGER NOT NULL REFERENCES recipes(id),
    portion_multiplier REAL NOT NULL,
    portion_g          REAL NOT NULL,
    kcal               INTEGER NOT NULL,
    protein_g          REAL NOT NULL,
    fat_g              REAL NOT NULL,
    carb_g             REAL NOT NULL,
    fibre_g            REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_meal_slots_day_slot ON meal_slots(menu_day_id, slot);
-- A shared weekend links two per-person menus.
CREATE TABLE IF NOT EXISTS shared_menus (
    id               INTEGER PRIMARY KEY,
    segment_start    TEXT NOT NULL,
    segment_end      TEXT NOT NULL,
    source_shared_id INTEGER REFERENCES shared_menus(id),
    model_used       TEXT,
    created_at       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS shared_menu_members (
    id             INTEGER PRIMARY KEY,
    shared_menu_id INTEGER NOT NULL REFERENCES shared_menus(id),
    person_id      INTEGER NOT NULL REFERENCES persons(id),
    menu_id        INTEGER NOT NULL UNIQUE REFERENCES menus(id),
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_shared_members_menu ON shared_menu_members(menu_id);
-- Whole-food units are keyed by code, independent of ingredient nutrition versions.
CREATE TABLE IF NOT EXISTS ingredient_units (
    id         INTEGER PRIMARY KEY,
    code       TEXT NOT NULL,
    unit_name  TEXT NOT NULL,
    unit_g     REAL NOT NULL CHECK (unit_g > 0),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_ingredient_units_code ON ingredient_units(code, id DESC);
-- The latest emphasis for a person and day applies while its menu is in force.
CREATE TABLE IF NOT EXISTS menu_emphasis (
    id           INTEGER PRIMARY KEY,
    person_id    INTEGER NOT NULL REFERENCES persons(id),
    day          TEXT NOT NULL,
    base_menu_id INTEGER NOT NULL REFERENCES menus(id),
    menu_id      INTEGER NOT NULL REFERENCES menus(id),
    levels_json  TEXT NOT NULL,
    shortfall    TEXT,
    created_at   TEXT NOT NULL,
    -- 0040: the slots at a level this person chose, not one that followed the other's change on
    -- a day cooked together; JSON list of meal_slots.slot, NULL before 0040
    own_json     TEXT
);
CREATE INDEX IF NOT EXISTS ix_menu_emphasis_day ON menu_emphasis(person_id, day, id DESC);
-- Weekend mode selects joint or separate cooking; the latest row is in force.
-- It is not consent: `consents.shared_meals` stays each person's own yes to the other seeing
-- their plate, and without both there is nothing to switch.
CREATE TABLE IF NOT EXISTS weekend_modes (
    id         INTEGER PRIMARY KEY,
    mode       TEXT NOT NULL CHECK (mode IN ('together', 'apart')),
    person_id  INTEGER REFERENCES persons(id),
    created_at TEXT NOT NULL
);
-- What a person's weekend treats lean to; the latest row per person is in force. `set_by` says
-- whether they chose it on their own page or it was set for them on somebody's word.
CREATE TABLE IF NOT EXISTS treat_tastes (
    id         INTEGER PRIMARY KEY,
    person_id  INTEGER NOT NULL REFERENCES persons(id),
    taste      TEXT NOT NULL CHECK (taste IN ('sweet', 'savoury', 'both')),
    set_by     TEXT NOT NULL DEFAULT 'person' CHECK (set_by IN ('person', 'operator')),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_treat_tastes_person ON treat_tastes(person_id, id DESC);
CREATE TABLE IF NOT EXISTS menu_decisions (
    id            INTEGER PRIMARY KEY,
    menu_id       INTEGER NOT NULL REFERENCES menus(id),
    decision      TEXT NOT NULL CHECK (decision IN ('accepted', 'rejected', 'lapsed')),
    message_in_id INTEGER,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shopping_lists (
    id             INTEGER PRIMARY KEY,
    menu_id        INTEGER REFERENCES menus(id),
    -- range (migration 0043): one person's plates in force from from_day to to_day, across the
    kind           TEXT NOT NULL CHECK (kind IN ('segment', 'joint_weekend', 'range')),
    person_id      INTEGER REFERENCES persons(id),
    message_out_id INTEGER,
    created_at     TEXT NOT NULL,
    -- whose food a joint weekend list is for, a JSON list of person ids (migration 0033); only
    -- they read and tick it. NULL on a person's own list, and on joint lists from before
    owner_ids      TEXT,
    -- the first day a list buys for when it was built again from a chosen day (migration 0034:
    -- "shop on Monday, cook Tuesday to Friday"); NULL buys for the whole segment
    from_day       TEXT,
    -- a range list's last day, and the plates it was built from (day, meal, dish, size): a
    -- skip, a swap or a rebuild changes them, and the list is built again
    to_day         TEXT,
    basis          TEXT
);
CREATE TABLE IF NOT EXISTS shopping_items (
    id            INTEGER PRIMARY KEY,
    list_id       INTEGER NOT NULL REFERENCES shopping_lists(id),
    ingredient_id INTEGER NOT NULL REFERENCES ingredients(id),
    grams_needed  REAL NOT NULL,
    packs         INTEGER NOT NULL,
    leftover_g    REAL NOT NULL DEFAULT 0,
    aisle         TEXT NOT NULL,
    -- the list as sent: the pack it was built against and what it cost then, so reading an
    -- old list back does not silently redisplay today's catalogue guess or today's price
    pack_g        REAL,
    pack_label    TEXT,
    product_name  TEXT,
    price_pence   INTEGER,
    -- and the shop it was seen in, and what was credited from the fridge (migration 0018):
    -- both were on the item and neither was stored, so a reread lost where to go and why the
    -- list buys less than the week eats
    retailer      TEXT,
    credit_g      REAL NOT NULL DEFAULT 0,
    product_url   TEXT,
    currency      TEXT NOT NULL DEFAULT 'GBP'
);
CREATE TABLE IF NOT EXISTS shopping_ticks (
    id         INTEGER PRIMARY KEY,
    item_id    INTEGER NOT NULL REFERENCES shopping_items(id),
    person_id  INTEGER NOT NULL REFERENCES persons(id),
    ticked     INTEGER NOT NULL CHECK (ticked IN (0, 1)),
    created_at TEXT NOT NULL
);
-- Record food already at home and each person's main-protein budget.
CREATE TABLE IF NOT EXISTS shopping_have (
    id              INTEGER PRIMARY KEY,
    person_id       INTEGER NOT NULL REFERENCES persons(id),
    ingredient_code TEXT NOT NULL,
    from_day        TEXT NOT NULL,
    to_day          TEXT NOT NULL,
    have            INTEGER NOT NULL CHECK (have IN (0, 1)),
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_shopping_have ON shopping_have(person_id, from_day, to_day);
CREATE TABLE IF NOT EXISTS pack_prefs (
    id         INTEGER PRIMARY KEY,
    person_id  INTEGER NOT NULL REFERENCES persons(id),
    mains      INTEGER NOT NULL CHECK (mains BETWEEN 0 AND 7),
    source     TEXT NOT NULL CHECK (source IN ('page', 'chat', 'operator')),
    created_at TEXT NOT NULL
);
-- how a person's week is planned, in their own word; the latest row wins (food/weekplan.py)
CREATE TABLE IF NOT EXISTS week_prefs (
    id         INTEGER PRIMARY KEY,
    person_id  INTEGER NOT NULL REFERENCES persons(id),
    prefs_json TEXT NOT NULL,
    source     TEXT NOT NULL CHECK (source IN ('page', 'chat', 'operator')),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_week_prefs ON week_prefs(person_id, id DESC);
CREATE TABLE IF NOT EXISTS leftovers (
    id            INTEGER PRIMARY KEY,
    person_id     INTEGER NOT NULL REFERENCES persons(id),
    ingredient_id INTEGER NOT NULL REFERENCES ingredients(id),
    grams         REAL NOT NULL,
    from_menu_id  INTEGER REFERENCES menus(id),
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ratings (
    id            INTEGER PRIMARY KEY,
    person_id     INTEGER NOT NULL REFERENCES persons(id),
    recipe_id     INTEGER NOT NULL REFERENCES recipes(id),
    score         INTEGER NOT NULL CHECK (score BETWEEN 1 AND 5),
    note          TEXT,
    message_in_id INTEGER,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_ratings_person_recipe ON ratings(person_id, recipe_id, id DESC);
-- Batch completion taps are append-only; the latest tap wins.
-- Swaps record which dish was removed and which was chosen.
CREATE TABLE IF NOT EXISTS dish_events (
    id         INTEGER PRIMARY KEY,
    person_id  INTEGER NOT NULL REFERENCES persons(id),
    dish       TEXT NOT NULL,
    recipe_id  INTEGER REFERENCES recipes(id),
    kind       TEXT NOT NULL CHECK (kind IN ('swapped_out', 'swapped_in')),
    day        TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_dish_events_person ON dish_events(person_id, dish);
-- Ingredient substitutions preserve both sides of the change.
CREATE TABLE IF NOT EXISTS ingredient_events (
    id         INTEGER PRIMARY KEY,
    person_id  INTEGER NOT NULL REFERENCES persons(id),
    code       TEXT NOT NULL,
    recipe_id  INTEGER REFERENCES recipes(id),
    kind       TEXT NOT NULL CHECK (kind IN ('swapped_out', 'swapped_in')),
    day        TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_ingredient_events_person ON ingredient_events(person_id, code);
-- Candidate dishes for one meal.
CREATE TABLE IF NOT EXISTS built_dishes (
    id         INTEGER PRIMARY KEY,
    person_id  INTEGER NOT NULL REFERENCES persons(id),
    recipe_id  INTEGER NOT NULL REFERENCES recipes(id),
    day        TEXT NOT NULL,
    slot       TEXT NOT NULL CHECK (slot IN ('breakfast', 'lunch', 'dinner', 'snack', 'pre_bed')),
    model_used TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_built_dishes_person ON built_dishes(person_id, slot, day);
-- Weekly choices offer dishes for each weekday meal, sized to that
-- meal's share of the day; the latest pick for a day and meal is in force, and an outcome
-- row says how the week became a menu (food/choices.py)
CREATE TABLE IF NOT EXISTS choice_sets (
    id          INTEGER PRIMARY KEY,
    person_id   INTEGER NOT NULL REFERENCES persons(id),
    week_start  TEXT NOT NULL,
    first_day   TEXT NOT NULL,
    last_day    TEXT NOT NULL,
    per_slot    INTEGER NOT NULL CHECK (per_slot BETWEEN 2 AND 4),
    slots_json  TEXT NOT NULL,
    shares_json TEXT NOT NULL,
    model_used  TEXT,
    detail      TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_choice_sets_person ON choice_sets(person_id, first_day);

CREATE TABLE IF NOT EXISTS choice_options (
    id                 INTEGER PRIMARY KEY,
    set_id             INTEGER NOT NULL REFERENCES choice_sets(id),
    day                TEXT NOT NULL,
    slot               TEXT NOT NULL CHECK (slot IN ('breakfast', 'lunch', 'dinner', 'snack', 'pre_bed')),
    position           INTEGER NOT NULL,
    recipe_id          INTEGER NOT NULL REFERENCES recipes(id),
    portion_multiplier REAL NOT NULL,
    kcal               INTEGER NOT NULL,
    protein_g          REAL NOT NULL,
    fat_g              REAL NOT NULL,
    carb_g             REAL NOT NULL,
    fibre_g            REAL NOT NULL,
    origin             TEXT NOT NULL CHECK (origin IN ('new', 'known')),
    why                TEXT,
    created_at         TEXT NOT NULL,
    -- an option for a batch-cooked meal is for its whole cook, `day` to `last_day` (migration
    -- 0046); NULL for an option for `day` alone
    last_day           TEXT
);
CREATE INDEX IF NOT EXISTS ix_choice_options_set ON choice_options(set_id, day, slot);

CREATE TABLE IF NOT EXISTS choice_picks (
    id         INTEGER PRIMARY KEY,
    set_id     INTEGER NOT NULL REFERENCES choice_sets(id),
    option_id  INTEGER NOT NULL REFERENCES choice_options(id),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_choice_picks_set ON choice_picks(set_id);

CREATE TABLE IF NOT EXISTS choice_outcomes (
    id         INTEGER PRIMARY KEY,
    set_id     INTEGER NOT NULL REFERENCES choice_sets(id),
    how        TEXT NOT NULL CHECK (how IN ('chosen', 'kept', 'closed', 'failed')),
    menu_ids   TEXT NOT NULL DEFAULT '[]',
    detail     TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_choice_outcomes_set ON choice_outcomes(set_id);
-- Proposal draws retain the values used to rank candidate dishes.
CREATE TABLE IF NOT EXISTS proposal_log (
    id         INTEGER PRIMARY KEY,
    person_id  INTEGER NOT NULL REFERENCES persons(id),
    day        TEXT NOT NULL,
    slot       TEXT,
    dish       TEXT NOT NULL,
    source     TEXT NOT NULL CHECK (source IN ('favourite', 'alternative')),
    mu         REAL NOT NULL,
    sampled    REAL NOT NULL,
    chosen     INTEGER NOT NULL CHECK (chosen IN (0, 1)),
    created_at TEXT NOT NULL,
    -- 0041: the menu it was weighed for - a weekend's own version kept undecided for the switch
    -- logs too, and is eaten only if the switch puts it in force; NULL before 0041
    menu_id    INTEGER REFERENCES menus(id)
);
CREATE INDEX IF NOT EXISTS ix_proposal_log_person ON proposal_log(person_id, day);
-- Store each weekly review whether or not a message carries it.
CREATE TABLE IF NOT EXISTS weekly_reviews (
    id         INTEGER PRIMARY KEY,
    person_id  INTEGER NOT NULL REFERENCES persons(id),
    week_end   TEXT NOT NULL,
    full_html  TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_weekly_reviews_person ON weekly_reviews(person_id, week_end);
CREATE TABLE IF NOT EXISTS batch_ticks (
    id         INTEGER PRIMARY KEY,
    person_id  INTEGER NOT NULL REFERENCES persons(id),
    menu_id    INTEGER NOT NULL REFERENCES menus(id),
    recipe_id  INTEGER NOT NULL REFERENCES recipes(id),
    done       INTEGER NOT NULL CHECK (done IN (0, 1)),
    created_at TEXT NOT NULL
);

-- ---------------------------------------------------------------- days, estimates
CREATE TABLE IF NOT EXISTS day_confirmations (
    id            INTEGER PRIMARY KEY,
    person_id     INTEGER NOT NULL REFERENCES persons(id),
    day           TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('confirmed', 'unconfirmed', 'paused')),
    basis         TEXT NOT NULL CHECK (basis IN ('menu', 'mixed', 'none')),
    kcal          REAL,
    protein_g     REAL,
    fat_g         REAL,
    carb_g        REAL,
    fibre_g       REAL,
    note          TEXT,
    message_in_id INTEGER,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_day_conf ON day_confirmations(person_id, day);

-- A menu cannot write a day that is already confirmed.
CREATE TRIGGER IF NOT EXISTS trg_menu_days_confirmed
BEFORE INSERT ON menu_days
WHEN EXISTS (
    SELECT 1 FROM menus m
    JOIN day_confirmations c ON c.person_id = m.person_id AND c.day = NEW.day
    WHERE m.id = NEW.menu_id
      AND c.status = 'confirmed'
      AND c.id = (SELECT MAX(x.id) FROM day_confirmations x
                  WHERE x.person_id = c.person_id AND x.day = c.day)
)
BEGIN
    SELECT RAISE(ABORT, 'menu_days: that day is already confirmed');
END;

CREATE TABLE IF NOT EXISTS estimates (
    id                 INTEGER PRIMARY KEY,
    person_id          INTEGER NOT NULL REFERENCES persons(id),
    day                TEXT NOT NULL,
    description        TEXT NOT NULL,
    items_json         TEXT NOT NULL DEFAULT '[]',
    kcal               REAL NOT NULL,
    protein_g          REAL,
    fat_g              REAL,
    carb_g             REAL,
    fibre_g            REAL,
    model_used         TEXT,
    status             TEXT NOT NULL CHECK (status IN ('pending', 'accepted', 'discarded')),
    source_estimate_id INTEGER,
    message_in_id      INTEGER,
    created_at         TEXT NOT NULL,
    -- 0028: the planned meals this food replaced, JSON list of meal_slots.slot; '[]' = extra
    replaced_slots_json TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS ix_estimates ON estimates(person_id, day);

-- ---------------------------------------------------------------- messaging
CREATE TABLE IF NOT EXISTS messages_out (
    id                  INTEGER PRIMARY KEY,
    person_id           INTEGER REFERENCES persons(id),
    chat_id             INTEGER NOT NULL,
    kind                TEXT NOT NULL,
    due_key             TEXT NOT NULL,
    text_html           TEXT NOT NULL,
    payload_json        TEXT NOT NULL DEFAULT '{}',
    model_used          TEXT,
    telegram_message_id INTEGER,
    status              TEXT NOT NULL CHECK (status IN ('sent', 'failed', 'dropped_quiet', 'dropped_paused', 'dropped_dev')),
    error               TEXT,
    sent_at             TEXT,
    created_at          TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_messages_out_sent ON messages_out(person_id, kind, due_key) WHERE status = 'sent';
CREATE INDEX IF NOT EXISTS ix_messages_out_person ON messages_out(person_id, created_at);

CREATE TABLE IF NOT EXISTS messages_in (
    id          INTEGER PRIMARY KEY,
    update_id   INTEGER NOT NULL UNIQUE,
    person_id   INTEGER REFERENCES persons(id),
    chat_id     INTEGER NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN
                  ('text', 'voice', 'callback', 'command', 'document', 'other', 'stranger')),
    raw_json    TEXT,
    received_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS inbound_outcomes (
    id            INTEGER PRIMARY KEY,
    message_in_id INTEGER NOT NULL REFERENCES messages_in(id),
    outcome       TEXT NOT NULL CHECK (outcome IN ('handled', 'ignored', 'error', 'duplicate', 'paired')),
    detail        TEXT,
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS voice_transcripts (
    id            INTEGER PRIMARY KEY,
    message_in_id INTEGER NOT NULL REFERENCES messages_in(id),
    file_id       TEXT NOT NULL,
    duration_s    INTEGER,
    text          TEXT NOT NULL,
    model         TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS intents (
    id            INTEGER PRIMARY KEY,
    person_id     INTEGER NOT NULL REFERENCES persons(id),
    kind          TEXT NOT NULL CHECK (kind IN
                    ('log_offplan', 'confirm_day', 'rate_dish', 'set_pause', 'restock',
                     'set_plan', 'set_profile', 'set_food_prefs')),
    payload_json  TEXT NOT NULL,
    model_used    TEXT,
    message_in_id INTEGER,
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS intent_decisions (
    id            INTEGER PRIMARY KEY,
    intent_id     INTEGER NOT NULL UNIQUE REFERENCES intents(id),
    decision      TEXT NOT NULL CHECK (decision IN ('accepted', 'discarded', 'lapsed')),
    message_in_id INTEGER,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS proposals (
    id             INTEGER PRIMARY KEY,
    person_id      INTEGER NOT NULL REFERENCES persons(id),
    kind           TEXT NOT NULL,
    payload_json   TEXT NOT NULL,
    reason_line    TEXT NOT NULL,
    model_used     TEXT,
    message_out_id INTEGER,
    expires_at     TEXT NOT NULL,
    created_at     TEXT NOT NULL
);
-- A declined proposal may later be accepted, so proposal_id is not unique.
-- `ProposalFlow.decide` reads the LATEST row (ORDER BY id DESC) and only ever appends a second
-- one on a declined -> accepted catch-up; an already-accepted decision still stands (the effect
-- it applied cannot be safely unwound), so it never grows a second row.
CREATE TABLE IF NOT EXISTS proposal_decisions (
    id            INTEGER PRIMARY KEY,
    proposal_id   INTEGER NOT NULL REFERENCES proposals(id),
    decision      TEXT NOT NULL CHECK (decision IN ('accepted', 'declined', 'lapsed')),
    message_in_id INTEGER,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tdee_calibrations (
    id               INTEGER PRIMARY KEY,
    person_id        INTEGER NOT NULL REFERENCES persons(id),
    week_start       TEXT NOT NULL,
    usable_days      INTEGER NOT NULL,
    intake_kcal_mean REAL,
    trend_delta_kg   REAL,
    tdee_measured    REAL,
    tdee_blended     REAL,
    confidence       TEXT CHECK (confidence IN ('low', 'medium', 'high')),
    skipped_reason   TEXT,
    created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pause_periods (
    id         INTEGER PRIMARY KEY,
    person_id  INTEGER NOT NULL REFERENCES persons(id),
    start_day  TEXT NOT NULL,
    end_day    TEXT NOT NULL,
    reason     TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS web_events (
    id           INTEGER PRIMARY KEY,
    person_id    INTEGER NOT NULL REFERENCES persons(id),
    kind         TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    at           TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS exports (
    id            INTEGER PRIMARY KEY,
    person_slug   TEXT NOT NULL,
    path          TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    at            TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS erasures (
    id              INTEGER PRIMARY KEY,
    person_slug     TEXT NOT NULL,
    export_path     TEXT,
    row_counts_json TEXT NOT NULL,
    files_json      TEXT NOT NULL,
    at              TEXT NOT NULL
);

-- onboarding answers are a ledger too: each answer is a row, the interview resumes from the last
CREATE TABLE IF NOT EXISTS onboarding_answers (
    id            INTEGER PRIMARY KEY,
    person_id     INTEGER NOT NULL REFERENCES persons(id),
    step          TEXT NOT NULL,
    answer_json   TEXT NOT NULL,
    message_in_id INTEGER,
    created_at    TEXT NOT NULL
);

-- ---------------------------------------------------------------- state (mutable)
CREATE TABLE IF NOT EXISTS bot_state (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS conversation_state (
    person_id  INTEGER PRIMARY KEY REFERENCES persons(id),
    state_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS run_ledger (
    run_id     TEXT PRIMARY KEY,
    job        TEXT NOT NULL,
    slot       TEXT NOT NULL,
    person_id  INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL,
    ended_at   TEXT,
    status     TEXT NOT NULL CHECK (status IN ('running', 'ok', 'degraded', 'failed', 'skipped')),
    detail     TEXT,
    error      TEXT,
    UNIQUE (job, slot, person_id)
);
CREATE TABLE IF NOT EXISTS icu_sync_state (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);

-- ---------------------------------------------------------------- latest-row views (the read contract)
CREATE VIEW IF NOT EXISTS v_profile_current AS
SELECT p.* FROM profiles p
WHERE p.id = (SELECT id FROM profiles q WHERE q.person_id = p.person_id
              AND q.effective_from <= date('now', 'localtime')
              ORDER BY q.effective_from DESC, q.id DESC LIMIT 1);

CREATE VIEW IF NOT EXISTS v_targets_latest AS
SELECT t.* FROM targets t
WHERE t.id = (SELECT id FROM targets q WHERE q.person_id = t.person_id AND q.week_start = t.week_start
              ORDER BY q.id DESC LIMIT 1);

CREATE VIEW IF NOT EXISTS v_day_latest AS
SELECT d.* FROM day_confirmations d
WHERE d.id = (SELECT id FROM day_confirmations q WHERE q.person_id = d.person_id AND q.day = d.day
              ORDER BY q.id DESC LIMIT 1);

CREATE VIEW IF NOT EXISTS v_trend_latest AS
SELECT t.* FROM trend_points t
WHERE t.id = (SELECT id FROM trend_points q WHERE q.person_id = t.person_id AND q.day = t.day
              ORDER BY q.id DESC LIMIT 1);

CREATE VIEW IF NOT EXISTS v_weigh_in_latest AS
SELECT w.* FROM weigh_ins w
WHERE w.id = (SELECT id FROM weigh_ins q WHERE q.person_id = w.person_id AND q.day = w.day
              ORDER BY q.id DESC LIMIT 1);

CREATE VIEW IF NOT EXISTS v_pause_current AS
SELECT p.* FROM pause_periods p
WHERE p.id = (SELECT id FROM pause_periods q WHERE q.person_id = p.person_id ORDER BY q.id DESC LIMIT 1);

-- "Current" is per DAY, not per segment: a Tue-Fri accept must not blank the Monday of the
-- Mon-Fri menu it partially replaces, and consumers that ask about one day (accepted_menu_id,
-- current_menu) tell the two apart with `ORDER BY decided_at DESC, id DESC LIMIT 1`. So this
-- view can legitimately hold more than one row per person when their ranges overlap - a menu is
-- dropped only when another one's range fully contains it (an exact rebuild, or a wider one
-- swallowing it whole) AND that other one was decided more recently. `decided_at` is the latest
-- menu_decisions row's timestamp, falling back to the menu's own created_at for a status-only
-- accept ('repeated' rows from the lapse path never get a decision row at all); `created_at` is
-- second-precision, so `id` breaks ties between menus created in the same second.
CREATE VIEW IF NOT EXISTS v_menu_current AS
SELECT m.*,
       COALESCE(
           (SELECT d.created_at FROM menu_decisions d WHERE d.menu_id = m.id ORDER BY d.id DESC LIMIT 1),
           m.created_at
       ) AS decided_at
FROM menus m
WHERE (
        m.status IN ('accepted', 'repeated')
        OR (
            SELECT d.decision FROM menu_decisions d
            WHERE d.menu_id = m.id ORDER BY d.id DESC LIMIT 1
        ) = 'accepted'
      )
  -- a resized day (menu_emphasis) feeds its day through v_menu_days_current, but it is not a
  -- stretch of days: a shopping list, a batch cook or a builder asking for the menu from Monday
  -- still gets the week. It is left out here, as a menu and as one that supersedes another.
  AND NOT EXISTS (SELECT 1 FROM menu_emphasis e WHERE e.menu_id = m.id)
  AND NOT EXISTS (
        SELECT 1 FROM menus y
        WHERE y.person_id = m.person_id
          AND y.id != m.id
          AND NOT EXISTS (SELECT 1 FROM menu_emphasis e2 WHERE e2.menu_id = y.id)
          -- y fully contains m's range, not merely overlaps it: a Tue-Fri accept overlaps a
          -- Mon-Fri menu without containing all of it, so the Mon-Fri one stays current for the
          -- Monday y never touched
          AND y.segment_start <= m.segment_start
          AND y.segment_end >= m.segment_end
          AND (
            y.status IN ('accepted', 'repeated')
            OR (
                SELECT d2.decision FROM menu_decisions d2
                WHERE d2.menu_id = y.id ORDER BY d2.id DESC LIMIT 1
            ) = 'accepted'
          )
          AND (
            COALESCE(
                (SELECT d3.created_at FROM menu_decisions d3 WHERE d3.menu_id = y.id ORDER BY d3.id DESC LIMIT 1),
                y.created_at
            )
            > COALESCE(
                (SELECT d4.created_at FROM menu_decisions d4 WHERE d4.menu_id = m.id ORDER BY d4.id DESC LIMIT 1),
                m.created_at
            )
            OR (
                COALESCE(
                    (SELECT d5.created_at FROM menu_decisions d5 WHERE d5.menu_id = y.id ORDER BY d5.id DESC LIMIT 1),
                    y.created_at
                )
                = COALESCE(
                    (SELECT d6.created_at FROM menu_decisions d6 WHERE d6.menu_id = m.id ORDER BY d6.id DESC LIMIT 1),
                    m.created_at
                )
                AND y.id > m.id
            )
          )
      );

-- The menu day in force for each person and day. A later menu can cover part of an earlier one's
-- range - a rebuilt Tuesday to Friday over a Monday to Friday week, or one day's plates resized
-- inside a weekend (menu_emphasis) - and both stay in v_menu_current, because neither contains
-- the other. A reader of one day wants exactly one menu day: the one decided last. Built on the
-- tables with v_menu_current's own "in force" rule.
CREATE VIEW IF NOT EXISTS v_menu_days_current AS
WITH cur AS (
    SELECT m.id, m.person_id, m.segment_start, m.segment_end, m.status,
           COALESCE(
               (SELECT d.created_at FROM menu_decisions d WHERE d.menu_id = m.id
                ORDER BY d.id DESC LIMIT 1),
               m.created_at
           ) AS decided_at
    FROM menus m
    WHERE m.status IN ('accepted', 'repeated')
       OR (SELECT d.decision FROM menu_decisions d WHERE d.menu_id = m.id
           ORDER BY d.id DESC LIMIT 1) = 'accepted'
)
SELECT md.*, c.person_id AS person_id, c.decided_at AS decided_at,
       c.segment_start AS segment_start, c.segment_end AS segment_end, c.status AS status
FROM cur c JOIN menu_days md ON md.menu_id = c.id
WHERE NOT EXISTS (
    SELECT 1 FROM cur c2 JOIN menu_days md2 ON md2.menu_id = c2.id
    WHERE c2.person_id = c.person_id AND md2.day = md.day AND c2.id != c.id
      AND (c2.decided_at > c.decided_at OR (c2.decided_at = c.decided_at AND c2.id > c.id))
);

-- Sunday summary / web page read contract. week_start is the Monday.
CREATE VIEW IF NOT EXISTS v_week_summary AS
WITH d AS (
    SELECT person_id, day, status,
           date(day, '-' || ((CAST(strftime('%w', day) AS INTEGER) + 6) % 7) || ' days') AS week_start
    FROM v_day_latest
),
weeks AS (SELECT DISTINCT person_id, week_start FROM d)
SELECT w.person_id, w.week_start,
       (SELECT COUNT(*) FROM d WHERE d.person_id = w.person_id AND d.week_start = w.week_start
          AND d.status = 'confirmed') AS confirmed_days,
       (SELECT COUNT(*) FROM d WHERE d.person_id = w.person_id AND d.week_start = w.week_start
          AND (d.status = 'confirmed' OR (d.status = 'unconfirmed' AND EXISTS (
              SELECT 1 FROM estimates e WHERE e.person_id = d.person_id AND e.day = d.day
                AND e.status = 'accepted')))) AS usable_days,
       (SELECT COUNT(*) FROM d WHERE d.person_id = w.person_id AND d.week_start = w.week_start
          AND d.status = 'paused') AS paused_days,
       (SELECT t1.trend_kg FROM v_trend_latest t1 WHERE t1.person_id = w.person_id
          AND t1.day <= date(w.week_start, '+6 days') ORDER BY t1.day DESC LIMIT 1)
       - (SELECT t0.trend_kg FROM v_trend_latest t0 WHERE t0.person_id = w.person_id
          AND t0.day < w.week_start ORDER BY t0.day DESC LIMIT 1) AS trend_delta,
       (SELECT COUNT(*) FROM session_matches sm WHERE sm.person_id = w.person_id
          AND sm.day BETWEEN w.week_start AND date(w.week_start, '+6 days')
          AND sm.status IN ('matched', 'confirmed', 'manual', 'extra')
          AND sm.id = (SELECT MAX(id) FROM session_matches x WHERE x.person_id = sm.person_id
                       AND x.day = sm.day AND COALESCE(x.planned_session_id, -1) = COALESCE(sm.planned_session_id, -1)
                       AND COALESCE(x.icu_id, '') = COALESCE(sm.icu_id, ''))) AS sessions_done,
       (SELECT COUNT(*) FROM planned_sessions ps WHERE ps.person_id = w.person_id AND ps.kind <> 'rest'
          AND ps.plan_id = (SELECT id FROM training_plans tp WHERE tp.person_id = w.person_id
                            AND tp.effective_from <= date(w.week_start, '+6 days')
                            ORDER BY tp.effective_from DESC, tp.id DESC LIMIT 1)) AS sessions_planned
FROM weeks w;

-- Documents sent to the bot.
CREATE TABLE IF NOT EXISTS documents (
    id          INTEGER PRIMARY KEY,
    person_id   INTEGER NOT NULL REFERENCES persons(id),
    file_name   TEXT NOT NULL,
    mime_type   TEXT,
    sha256      TEXT NOT NULL,
    n_bytes     INTEGER NOT NULL,
    page_count  INTEGER NOT NULL DEFAULT 0,
    n_chars     INTEGER NOT NULL DEFAULT 0,
    truncated   INTEGER NOT NULL DEFAULT 0 CHECK (truncated IN (0, 1)),
    text        TEXT NOT NULL,
    message_in_id INTEGER,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_documents_person ON documents(person_id, id DESC);
CREATE INDEX IF NOT EXISTS ix_documents_sha ON documents(person_id, sha256);

CREATE TABLE IF NOT EXISTS document_facts (
    id          INTEGER PRIMARY KEY,
    document_id INTEGER NOT NULL REFERENCES documents(id),
    person_id   INTEGER NOT NULL REFERENCES persons(id),
    kind        TEXT NOT NULL CHECK (kind IN
                  ('targets', 'recipe', 'sessions', 'refused', 'liked', 'note', 'unreadable',
                   'profile')),
    payload_json TEXT NOT NULL,
    label       TEXT NOT NULL,
    model_used  TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_document_facts_doc ON document_facts(document_id, id);

CREATE TABLE IF NOT EXISTS document_decisions (
    id          INTEGER PRIMARY KEY,
    document_id INTEGER NOT NULL REFERENCES documents(id),
    decision    TEXT NOT NULL CHECK (decision IN ('accepted', 'rejected', 'lapsed')),
    applied_json TEXT,
    message_in_id INTEGER,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_document_decisions ON document_decisions(document_id, id DESC);

CREATE TABLE IF NOT EXISTS person_notes (
    id          INTEGER PRIMARY KEY,
    person_id   INTEGER NOT NULL REFERENCES persons(id),
    text        TEXT NOT NULL,
    -- chat: their own words, to the bot or in a rebuild note ("too much chia seed")
    -- operator: written on the person's behalf from the shell (`tiffin prefs`, migration 0042)
    source      TEXT NOT NULL CHECK (source IN ('document', 'chat', 'onboarding', 'operator')),
    document_id INTEGER REFERENCES documents(id),
    active      INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_person_notes ON person_notes(person_id, id DESC);

CREATE VIEW IF NOT EXISTS v_person_notes_active AS
SELECT n.*
FROM person_notes n
WHERE n.active = 1
  AND n.id = (
        SELECT MAX(x.id) FROM person_notes x
        WHERE x.person_id = n.person_id AND x.text = n.text
      );

-- Retail products matched to catalogue foods.
CREATE TABLE IF NOT EXISTS products (
    id             INTEGER PRIMARY KEY,
    ingredient_code TEXT NOT NULL,
    retailer       TEXT NOT NULL CHECK (retailer IN
                     ('tesco', 'sainsburys', 'wholefoods', 'marks_spencer', 'ocado', 'other')),
    retailer_sku   TEXT NOT NULL,
    name           TEXT NOT NULL,
    url            TEXT,
    pack_g         REAL CHECK (pack_g IS NULL OR pack_g > 0),
    pack_label     TEXT,
    price_pence    INTEGER CHECK (price_pence IS NULL OR price_pence >= 0),
    price_per_kg_pence INTEGER,
    -- the label's own per-100 g figures; NULL means the page did not show that row, which is
    -- not the same as zero and must never be read as zero
    kcal_100       REAL,
    protein_100    REAL,
    fat_100        REAL,
    carb_100       REAL,
    fibre_100      REAL,
    salt_100       REAL,
    seen_at        TEXT NOT NULL,
    source         TEXT NOT NULL CHECK (source IN ('chrome', 'openfoodfacts', 'manual')),
    created_at     TEXT NOT NULL,
    -- why the nutrition rows above are NULL when they are: a shop that publishes "cooked as per
    -- instructions" for a food this catalogue holds raw has told us something real, and it is
    -- not the same as never having looked. The pack and the price are still worth keeping.
    -- Last, because migration 0026 adds it with ALTER TABLE and a fresh install has to match.
    nutrition_basis TEXT
);
CREATE INDEX IF NOT EXISTS ix_products_code ON products(ingredient_code, id DESC);
CREATE INDEX IF NOT EXISTS ix_products_sku ON products(retailer, retailer_sku, id DESC);

-- A person's own purchase option. Later rows replace earlier ones for that food.
CREATE TABLE IF NOT EXISTS manual_products (
    id              INTEGER PRIMARY KEY,
    person_id       INTEGER NOT NULL REFERENCES persons(id),
    ingredient_code TEXT NOT NULL,
    name            TEXT NOT NULL,
    pack_g          REAL NOT NULL CHECK (pack_g > 0),
    price_pence     INTEGER CHECK (price_pence IS NULL OR price_pence >= 0),
    currency        TEXT NOT NULL DEFAULT 'GBP',
    shop            TEXT,
    url             TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_manual_products_person
    ON manual_products(person_id, ingredient_code, id DESC);

-- Foods a shop does not stock are skipped by the refresh queue for
-- MISS_DAYS, so three unstocked foods do not eat a twelve-food batch every week forever.
CREATE TABLE IF NOT EXISTS shop_misses (
    id              INTEGER PRIMARY KEY,
    ingredient_code TEXT NOT NULL,
    retailer        TEXT NOT NULL CHECK (retailer IN
                      ('tesco', 'sainsburys', 'wholefoods', 'marks_spencer', 'ocado', 'other')),
    reason          TEXT,
    seen_at         TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    -- 'unstocked' is a fact about the shop and parks the food for MISS_DAYS. 'unread' is a fact
    -- about the session (out of turns, blocked, never searched) and parks nothing. 'basis' is a
    -- fact about the PAGE: the food is there, its table is for the cooked state, and the pack
    -- and price are still wanted - so it is not parked either. Last, because 0024 adds the
    -- column with ALTER TABLE and 0027 rebuilds in this order; a fresh install has to match.
    kind            TEXT NOT NULL DEFAULT 'unstocked'
                      CHECK (kind IN ('unstocked', 'unread', 'basis')),
    -- which shop reader made the verdict (products.READER_VERSION; migration 0033): a verdict
    -- from an older reader is asked again, not believed for MISS_DAYS. NULL = before readers
    reader          TEXT
);
CREATE INDEX IF NOT EXISTS ix_shop_misses ON shop_misses(retailer, ingredient_code, id DESC);

-- a property of the food and how it is cooked, so it is worth keeping: the next cook can say
-- "about 400 g cooked, 80 g a box" before the pan is on. Append-only, latest row wins, and no
-- stored factor - it is cooked_g / raw_g. Macros are untouched: the catalogue is raw per 100 g
-- and water leaving a pan takes no calories with it; this only divides food between boxes.
CREATE TABLE IF NOT EXISTS cook_yields (
    id              INTEGER PRIMARY KEY,
    person_id       INTEGER NOT NULL REFERENCES persons(id),
    ingredient_code TEXT NOT NULL,
    recipe_id       INTEGER REFERENCES recipes(id),
    raw_g           REAL NOT NULL CHECK (raw_g > 0),
    cooked_g        REAL NOT NULL CHECK (cooked_g > 0),
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_cook_yields
    ON cook_yields(person_id, ingredient_code, id DESC);

CREATE TABLE IF NOT EXISTS product_choices (
    id              INTEGER PRIMARY KEY,
    person_id       INTEGER NOT NULL REFERENCES persons(id),
    ingredient_code TEXT NOT NULL,
    product_id      INTEGER REFERENCES products(id),
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_product_choices
    ON product_choices(person_id, ingredient_code, id DESC);

CREATE VIEW IF NOT EXISTS v_products_latest AS
SELECT p.*
FROM products p
WHERE p.id = (
    SELECT MAX(x.id) FROM products x
    WHERE x.retailer = p.retailer AND x.retailer_sku = p.retailer_sku
);

CREATE VIEW IF NOT EXISTS v_product_choice_latest AS
SELECT c.person_id, c.ingredient_code, pr.*
FROM product_choices c
JOIN products pr ON pr.id = c.product_id
WHERE c.id = (
    SELECT MAX(x.id) FROM product_choices x
    WHERE x.person_id = c.person_id AND x.ingredient_code = c.ingredient_code
);
