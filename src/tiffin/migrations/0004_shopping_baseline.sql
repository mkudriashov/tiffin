ALTER TABLE ingredients ADD COLUMN pack_sizes_g TEXT NOT NULL DEFAULT '[]';
ALTER TABLE shopping_items ADD COLUMN product_url TEXT;
ALTER TABLE shopping_items ADD COLUMN currency TEXT NOT NULL DEFAULT 'GBP';

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
