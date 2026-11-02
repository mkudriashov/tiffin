BEGIN;
CREATE TABLE IF NOT EXISTS links (
    id          INTEGER PRIMARY KEY,
    a_person    INTEGER NOT NULL REFERENCES persons(id),
    b_person    INTEGER NOT NULL REFERENCES persons(id),
    code_sha256 TEXT NOT NULL UNIQUE,
    expires_at  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    CHECK (a_person <> b_person)
);
CREATE TABLE IF NOT EXISTS link_events (
    id         INTEGER PRIMARY KEY,
    link_id    INTEGER NOT NULL REFERENCES links(id),
    person_id  INTEGER REFERENCES persons(id),
    event      TEXT NOT NULL CHECK (event IN ('offered', 'accepted', 'declined', 'unlinked', 'expired')),
    method     TEXT NOT NULL CHECK (method IN ('telegram', 'web', 'cli', 'migrated', 'system')),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_link_events_link ON link_events(link_id, id DESC);
CREATE UNIQUE INDEX IF NOT EXISTS ux_link_events_accepted ON link_events(link_id) WHERE event = 'accepted';
CREATE TABLE IF NOT EXISTS link_attempts (
    id         INTEGER PRIMARY KEY,
    person_id  INTEGER NOT NULL REFERENCES persons(id),
    ok         INTEGER NOT NULL CHECK (ok IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_link_attempts ON link_attempts(person_id, id DESC);
CREATE TABLE IF NOT EXISTS shopping_list_closures (
    id         INTEGER PRIMARY KEY,
    list_id    INTEGER NOT NULL UNIQUE REFERENCES shopping_lists(id),
    link_id    INTEGER REFERENCES links(id),
    created_at TEXT NOT NULL
);
ALTER TABLE shared_menus ADD COLUMN link_id INTEGER REFERENCES links(id);
ALTER TABLE shopping_lists ADD COLUMN link_id INTEGER REFERENCES links(id);
COMMIT;
