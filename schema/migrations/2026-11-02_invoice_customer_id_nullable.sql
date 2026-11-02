-- Migration: make invoices.customer_id nullable
-- Date: 2026-11-02
--
-- The HMI "New Invoice" form (/invoices/new) collects a free-text
-- client_name and has no customer picker, but the live database still
-- had invoices.customer_id as NOT NULL. Every submit therefore died with
-- a 500:
--
--   psycopg2.errors.NotNullViolation: null value in column
--   "customer_id" of relation "invoices" violates not-null constraint
--
-- The canonical schema (schema/init.sql, both larkins/main and
-- origin/main) already declares it nullable:
--
--   customer_id VARCHAR(36) REFERENCES customers(id) ON DELETE RESTRICT,
--
-- This live database was created from an older init.sql and never picked
-- the change up, so the schema had drifted from the canonical definition.
-- This migration brings the database back in line with init.sql.
--
-- The API's create_invoice sets customer_id explicitly and is unaffected;
-- app/models/invoice.py is updated to nullable=True alongside this file.
--
-- Run as: psql -d enp -f schema/migrations/2026-11-02_invoice_customer_id_nullable.sql

BEGIN;

ALTER TABLE invoices ALTER COLUMN customer_id DROP NOT NULL;

COMMIT;
