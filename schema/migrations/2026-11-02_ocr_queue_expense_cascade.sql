-- Migration: ocr_queue.expense_id -> ON DELETE CASCADE
-- Date: 2026-11-02
--
-- The canonical schema (schema/init.sql, both larkins/main and
-- origin/main) already declares:
--
--   expense_id VARCHAR(36) NOT NULL REFERENCES expenses(id) ON DELETE CASCADE,
--
-- but the live database's FK had no ON DELETE clause. Deleting an
-- expense that had been through the OCR pipeline therefore blew up:
--
--   sqlalchemy.exc.IntegrityError: ForeignKeyViolation
--   update or delete on table "expenses" violates foreign key
--   constraint "ocr_queue_expense_id_fkey" on table "ocr_queue"
--
-- That broke every delete path for those rows:
--   - DELETE /api/expenses/<id>
--   - POST /expenses/<id>/delete   (the HMI Delete button)
--   - POST /expenses/<id>/reject   (the HMI Reject button)
--
-- i.e. a receipt captured through the PWA could not be deleted or
-- rejected from the HMI -- both buttons returned HTTP 500.
--
-- This migration brings the live FK in line with init.sql, so deleting
-- an expense removes its OCR job with it (which is also what you want:
-- the worker should not keep retrying an expense that no longer exists).
--
-- Run as: psql -d enp -f schema/migrations/2026-11-02_ocr_queue_expense_cascade.sql

BEGIN;

ALTER TABLE ocr_queue DROP CONSTRAINT IF EXISTS ocr_queue_expense_id_fkey;

ALTER TABLE ocr_queue
    ADD CONSTRAINT ocr_queue_expense_id_fkey
    FOREIGN KEY (expense_id) REFERENCES expenses(id) ON DELETE CASCADE;

COMMIT;
