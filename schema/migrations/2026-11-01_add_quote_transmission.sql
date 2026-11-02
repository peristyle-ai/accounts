-- Add the transmission record to `quotes` (2026-11-01, `feat/quotes`).
--
-- WHY: a quote is a DOCUMENT, and "it was sent" is a claim about a
-- document that left the building. The existing columns (`status`,
-- `sent_at`) record only that the system was told it went out, and
-- `sent_at` was forced to the moment of the update -- so the time and
-- the artefact of the transmission were both lost.
--
-- Three questions, three columns:
--
--   sent_via            HOW it left, and -- the distinction that matters --
--                       whether the SYSTEM sent it or a person did. A
--                       system send leaves a record in the mail server's
--                       Sent folder; a person sending it from their own
--                       client leaves nothing to check. Recording only
--                       "sent" makes those two indistinguishable, and
--                       they are not the same evidential fact.
--   sent_to             WHERE it went. The customer's contact address is a
--                       standing record of the customer, not a record of
--                       this transmission; it can differ, and it can
--                       change later.
--   sent_document_sha256 WHICH BYTES went out. The quote PDF is rendered
--                       on demand, so re-rendering it produces different
--                       bytes (document id and creation timestamp). Two
--                       renders of the same quote are different files.
--                       Without a hash of the transmitted artefact there
--                       is no way to prove later what the recipient
--                       actually holds.
--
-- Nullable: a quote raised before this column existed has no
-- transmission record, and that is the truth about it.
--
-- Idempotent: safe to run against a database that already has the
-- columns. The search_path is set by the migration runner (init_db.py)
-- to <ACCOUNTS_SCHEMA>, public, so the unqualified names resolve.

ALTER TABLE quotes ADD COLUMN IF NOT EXISTS sent_via VARCHAR(32);
ALTER TABLE quotes ADD COLUMN IF NOT EXISTS sent_to VARCHAR(500);
ALTER TABLE quotes ADD COLUMN IF NOT EXISTS sent_document_sha256 VARCHAR(64);
