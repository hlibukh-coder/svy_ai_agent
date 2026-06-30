-- BAS sync tables migration
-- Run once: psql -U postgres -d your_db -f sync/migration.sql

CREATE TABLE IF NOT EXISTS products (
    ref_key    TEXT PRIMARY KEY,
    name       TEXT,
    code       TEXT,            -- Артикул / SKU (the product "index")
    bas_code   TEXT,            -- 1C internal Код (e.g. "НФ-00000670")
    deleted    BOOLEAN DEFAULT false,
    price      NUMERIC DEFAULT 0,
    stock      NUMERIC DEFAULT 0,
    updated_at TIMESTAMPTZ DEFAULT now()
);
-- Backfill for existing DBs created before bas_code was added.
ALTER TABLE products ADD COLUMN IF NOT EXISTS bas_code TEXT;

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
    amount         NUMERIC DEFAULT 0,
    channel        TEXT,           -- channel the AI order came from (telegram/whatsapp/email/viber)
    account_id     INTEGER         -- which connected account produced the sale
);

CREATE TABLE IF NOT EXISTS stock (
    product_ref_key TEXT PRIMARY KEY,
    quantity        NUMERIC DEFAULT 0
);

CREATE TABLE IF NOT EXISTS order_items (
    id              SERIAL PRIMARY KEY,
    order_ref_key   TEXT NOT NULL,
    product_ref_key TEXT,
    qty             NUMERIC DEFAULT 0,
    price           NUMERIC DEFAULT 0,
    amount          NUMERIC DEFAULT 0,
    CONSTRAINT fk_order_items_order FOREIGN KEY (order_ref_key)
        REFERENCES orders(ref_key) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS sync_state (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Outreach log: who the proactive campaigns already contacted, so we never
-- re-message the same client for the same campaign (and never re-pitch the same
-- new product twice). `ref` holds the product ref_key for the newproduct campaign.
CREATE TABLE IF NOT EXISTS outreach_log (
    id             SERIAL PRIMARY KEY,
    client_ref_key TEXT NOT NULL,
    campaign       TEXT NOT NULL,      -- 'reorder' | 'inactive' | 'newproduct'
    ref            TEXT DEFAULT '',    -- product ref_key (newproduct) or ''
    sent_at        TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_outreach_lookup
    ON outreach_log (client_ref_key, campaign, ref, sent_at DESC);

-- Website price-feed catalog (YML/Rozetka XML from svyou.ua). Complements BAS:
-- retail price, stock, rich description, image and product page URL, keyed to BAS
-- products via vendor_code = products.code (Артикул). Refreshed from the XML feed.
CREATE TABLE IF NOT EXISTS site_offers (
    offer_id     TEXT PRIMARY KEY,   -- <offer id="…">
    vendor_code  TEXT,               -- <vendorCode> = Артикул (link to products.code)
    name         TEXT,
    url          TEXT,
    category_id  TEXT,
    price        NUMERIC DEFAULT 0,
    currency     TEXT DEFAULT 'UAH',
    vendor       TEXT,
    picture      TEXT,
    description  TEXT,
    available    BOOLEAN DEFAULT true,
    stock        NUMERIC DEFAULT 0,
    updated_at   TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_site_offers_vendor_code ON site_offers (vendor_code);
CREATE INDEX IF NOT EXISTS idx_site_offers_name ON site_offers USING gin(to_tsvector('simple', name));

-- Indexes for fast lookups
CREATE INDEX IF NOT EXISTS idx_clients_phone      ON clients (phone);
CREATE INDEX IF NOT EXISTS idx_orders_client      ON orders  (client_ref_key);
CREATE INDEX IF NOT EXISTS idx_orders_date        ON orders  (date DESC);
CREATE INDEX IF NOT EXISTS idx_products_code      ON products (code);
CREATE INDEX IF NOT EXISTS idx_products_bas_code  ON products (bas_code);
CREATE INDEX IF NOT EXISTS idx_products_name      ON products USING gin(to_tsvector('simple', name));
CREATE INDEX IF NOT EXISTS idx_order_items_order  ON order_items (order_ref_key);
