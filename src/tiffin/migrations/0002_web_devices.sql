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
