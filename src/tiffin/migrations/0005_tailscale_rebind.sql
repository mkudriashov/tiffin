BEGIN;
CREATE TABLE IF NOT EXISTS tailscale_identities (
    id INTEGER PRIMARY KEY,
    person_id INTEGER NOT NULL REFERENCES persons(id),
    login TEXT NOT NULL UNIQUE COLLATE NOCASE,
    created_at TEXT NOT NULL
);
CREATE TABLE tailscale_identities_new (
    id         INTEGER PRIMARY KEY,
    person_id  INTEGER NOT NULL REFERENCES persons(id),
    login      TEXT NOT NULL COLLATE NOCASE,
    created_at TEXT NOT NULL
);
INSERT INTO tailscale_identities_new (id, person_id, login, created_at)
SELECT id, person_id, login, created_at FROM tailscale_identities;
DROP TABLE tailscale_identities;
ALTER TABLE tailscale_identities_new RENAME TO tailscale_identities;
CREATE INDEX ix_tailscale_person ON tailscale_identities(person_id, id DESC);
CREATE INDEX ix_tailscale_login ON tailscale_identities(login COLLATE NOCASE, id DESC);
COMMIT;
