-- Migration: international (wire transfer) payment details
-- Date: 2026-11-05
--
-- WHY
-- ---
-- Peristyle now invoices customers outside Australia. A bank OUTSIDE
-- Australia cannot route a payment to an Australian account from a BSB
-- and an account number alone: it also needs the receiving bank's
-- SWIFT/BIC code, the branch address, the registered beneficiary name,
-- and the business postal address its compliance checks are run
-- against. Without them the payment comes back.
--
-- These are SEPARATE from the domestic bank details on purpose:
--   * the domestic account_name is the trading name on the invoice,
--     while international_account_name is the name the RECEIVING bank
--     holds. A wire quoting the wrong one is rejected.
--   * the address in the document header is where the business operates
--     from, while international_business_address is what the sending
--     bank checks.
-- Merging them would put a wrong name or a wrong address on a wire.
--
-- The SWIFT code is Fernet-encrypted at rest, the same treatment the BSB
-- gets: it identifies the account to the receiving bank. The names and
-- addresses are plaintext, the same class of value as account_name and
-- the business address.
--
-- The VALUES are not in this file and must not be: the repository is
-- PUBLIC. They are set on the tenant row through the API.
--
-- Idempotent: safe to run against a database that already has them.
--
-- Run as:
--   psql -d peristyle -c "SET search_path TO accounts, public" \
--         -f schema/migrations/2026-11-05_add_international_payment_details.sql
--
-- (The search_path must be set explicitly: init_db.py sets it for
-- schema/*.sql, but these migration files are applied by hand.)

BEGIN;

ALTER TABLE user_payment_settings
    ADD COLUMN IF NOT EXISTS international_account_name VARCHAR(255);

ALTER TABLE user_payment_settings
    ADD COLUMN IF NOT EXISTS international_branch_address VARCHAR(500);

ALTER TABLE user_payment_settings
    ADD COLUMN IF NOT EXISTS international_swift_code VARCHAR(500);

ALTER TABLE user_payment_settings
    ADD COLUMN IF NOT EXISTS international_business_address VARCHAR(500);

COMMENT ON COLUMN user_payment_settings.international_swift_code IS
    'Fernet ciphertext of the SWIFT/BIC code. Decrypt via the international_swift_code_plain accessor. NULL until the tenant supplies it.';

COMMENT ON COLUMN user_payment_settings.international_account_name IS
    'The registered beneficiary name as the RECEIVING bank holds it. Distinct from account_name, which is the trading name printed on the invoice.';

COMMIT;
