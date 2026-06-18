-- BAS sync tables migration
-- Run once: psql -U postgres -d your_db -f sync/migration.sql

CREATE TABLE IF NOT EXISTS products (
    ref_key    TEXT PRIMARY KEY,
    name       TEXT,
    code       TEXT,
    deleted    BOOLEAN DEFAULT false,
    price      NUMERIC DEFAULT 0,
    stock      NUMERIC DEFAULT 0,
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS clients (
    ref_key    TEXT PRIMARY KEY,
    name       TEXT,
    code       TEXT,
    phone      TEXT,
    company    TEXT,
    city       TEXT,
    deleted    BOOLEAN DEFAULT false,
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS orders (
    ref_key        TEXT PRIMARY KEY,
    number         TEXT,
    date           DATE,
    client_ref_key TEXT,
    amount         NUMERIC DEFAULT 0
);

CREATE TABLE IF NOT EXISTS stock (
    product_ref_key TEXT PRIMARY KEY,
    quantity        NUMERIC DEFAULT 0
);

-- Indexes for fast lookups
CREATE INDEX IF NOT EXISTS idx_clients_phone   ON clients (phone);
CREATE INDEX IF NOT EXISTS idx_orders_client   ON orders  (client_ref_key);
CREATE INDEX IF NOT EXISTS idx_orders_date     ON orders  (date DESC);
CREATE INDEX IF NOT EXISTS idx_products_code   ON products (code);
CREATE INDEX IF NOT EXISTS idx_products_name   ON products USING gin(to_tsvector('simple', name));
