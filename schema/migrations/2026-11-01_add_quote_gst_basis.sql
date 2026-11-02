-- Add `gst_basis` to `quotes` (2026-11-01, `feat/quotes` branch).
--
-- WHY: a quote is a DOCUMENT. What it says about tax must be the reason
-- that applied when the quote was raised, not whatever the customer's
-- record says when the PDF is next rendered. Without this column, editing
-- a customer (say, giving them an ABN) would silently change the wording
-- on quotes already issued.
--
-- Idempotent: safe to run against a database that already has the column.
-- The search_path is set by the migration runner (init_db.py) to
-- <ACCOUNTS_SCHEMA>, public, so the unqualified names resolve correctly.
--
-- The values are the BASIS_* codes in app/shared/tax.py:
--   charged, not_registered, outside_australia,
--   supplier_unregistered, not_applied
--
-- Nullable: a quote raised before this column existed has no basis, and
-- the PDF falls back to deriving it from the customer record.

ALTER TABLE quotes ADD COLUMN IF NOT EXISTS gst_basis VARCHAR(32);
