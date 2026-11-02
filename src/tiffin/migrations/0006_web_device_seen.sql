BEGIN;
CREATE TABLE IF NOT EXISTS web_device_seen (
    id INTEGER PRIMARY KEY,
    device_id INTEGER NOT NULL REFERENCES web_devices(id),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_web_device_seen ON web_device_seen(device_id, id DESC);
COMMIT;
