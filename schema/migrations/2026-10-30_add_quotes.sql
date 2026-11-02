-- Add the `quotes` table (2026-10-30, `feat/quotes` branch).
--
-- Idempotent: safe to run against a database that already has the table.
-- The search_path is set by the migration runner (init_db.py) to
-- <ACCOUNTS_SCHEMA>, public, so the unqualified names resolve correctly.

CREATE TABLE IF NOT EXISTS quotes (
    id VARCHAR(36) PRIMARY KEY DEFAULT uuid_generate_v4()::text,
    user_id BIGINT NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    customer_id VARCHAR(36) NOT NULL REFERENCES customers(id) ON DELETE RESTRICT,
    quote_number VARCHAR(32) NOT NULL,
    client_name VARCHAR(255) NOT NULL,
    lines JSONB NOT NULL DEFAULT '[]'::jsonb,
    ex_gst_amount NUMERIC(12, 2) NOT NULL,
    gst_amount NUMERIC(12, 2) NOT NULL DEFAULT 0,
    gst_type NUMERIC(3, 1) NOT NULL DEFAULT 0,
    total_amount NUMERIC(12, 2) NOT NULL,
    quote_date DATE NOT NULL,
    valid_until DATE,
    status VARCHAR(20) NOT NULL DEFAULT 'draft',
    notes TEXT,
    attachment_path VARCHAR(500),
    sent_at TIMESTAMPTZ,
    accepted_at TIMESTAMPTZ,
    declined_at TIMESTAMPTZ,
    converted_invoice_id VARCHAR(36),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_quotes_user_id ON quotes(user_id);
CREATE INDEX IF NOT EXISTS idx_quotes_customer_id ON quotes(customer_id);
CREATE INDEX IF NOT EXISTS idx_quotes_quote_date ON quotes(quote_date);
CREATE INDEX IF NOT EXISTS idx_quotes_status ON quotes(status);
CREATE INDEX IF NOT EXISTS idx_quotes_quote_number ON quotes(quote_number);
CREATE UNIQUE INDEX IF NOT EXISTS idx_quotes_user_quote_number
    ON quotes(user_id, quote_number);
