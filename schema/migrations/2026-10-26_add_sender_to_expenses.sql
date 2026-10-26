-- 2026-10-26: who sent the receipt, and when.
--
-- Jessica's request (action plan + HMI review page):
--   "in review page can we also have columns for who was the sender and
--    when it was sent (not just receipt date)"
--
-- The receipts arrive over WhatsApp/Telegram. Until now the sender phone
-- only reached `.state/ingestion-audit.log` (operator_id), never the
-- expense row — so the HMI could not show who sent what.
--
--   submitted_by   VARCHAR(120)  display name, resolved from the staff
--                                roster (data/staff.json in the xemo
--                                workspace) at ingest time
--   submitted_phone VARCHAR(32)  raw sender phone (E.164-ish, '+61...')
--   submitted_at   TIMESTAMPTZ   when the receipt was sent/received
--
-- All nullable + additive. Idempotent. Safe on a live DB.
--
-- Note: the expenses table lives in the `accounts` schema (per-app schema
-- pattern), so ALTER/CREATE INDEX must be schema-qualified.

BEGIN;

ALTER TABLE accounts.expenses
    ADD COLUMN IF NOT EXISTS submitted_by VARCHAR(120),
    ADD COLUMN IF NOT EXISTS submitted_phone VARCHAR(32),
    ADD COLUMN IF NOT EXISTS submitted_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_expenses_submitted_at
    ON accounts.expenses (submitted_at);

CREATE INDEX IF NOT EXISTS idx_expenses_submitted_phone
    ON accounts.expenses (submitted_phone);

COMMIT;
