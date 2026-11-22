-- 2026-11-23: the three fields the create route has been passing since
-- 2026-09-24 / 2026-09-25, and the model never grew.
--
-- POST /api/expenses answers HTTP 500 on EVERY call without them: the route
-- passes project_code / project_name / attachment_sha256 to the Expense
-- constructor, and SQLAlchemy raises
--   TypeError: 'project_code' is an invalid keyword argument for Expense
-- for a kwarg that is not a mapped attribute. The route is the documented
-- way to add an expense -- the PWA's "add expense" button and the mail
-- intake both POST to it -- so a missing column here takes out expense
-- creation for the whole installation.
--
--   project_code       VARCHAR(50)   CRM lead ref ('LD-YYYY-NNNNNN'),
--                                  optional, NULL for overhead
--   project_name       VARCHAR(255)  human-readable label, denormalised
--                                  from the CRM lead.name at ingest time
--   attachment_sha256  VARCHAR(64)   sha256 of the receipt bytes, for
--                                  receipt dedup (ingest_receipt)
--
-- All nullable + additive. Idempotent. Safe on a live DB.
--
-- The expenses table lives in the `accounts` schema (per-app schema
-- pattern), so ALTER/CREATE INDEX must be schema-qualified.

BEGIN;

ALTER TABLE accounts.expenses
    ADD COLUMN IF NOT EXISTS project_code VARCHAR(50),
    ADD COLUMN IF NOT EXISTS project_name VARCHAR(255),
    ADD COLUMN IF NOT EXISTS attachment_sha256 VARCHAR(64);

CREATE INDEX IF NOT EXISTS idx_expenses_project_code
    ON accounts.expenses (project_code);

CREATE INDEX IF NOT EXISTS idx_expenses_attachment_sha256
    ON accounts.expenses (attachment_sha256);

COMMIT;
