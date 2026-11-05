## What

Peristyle now issues invoices to customers outside Australia. A bank
OUTSIDE Australia cannot route a payment to an Australian account from a
BSB and an account number alone. It also needs the receiving bank's
SWIFT/BIC code, the branch address, the registered beneficiary name, and
the business postal address its compliance checks are run against.
Without them the payment comes back.

So a document addressed to a recipient outside Australia now carries a
second payment block, after the domestic one:

```
Bank Details:
Bank: <bank>
Account Name: <the account title the receiving bank holds>
BSB: <bsb>
Account Number: <account number>

International Payment Details:
Account Name: <the account title the receiving bank holds>
Branch Address: <the receiving branch's address>
Swift Code: <the SWIFT/BIC code>
Registered Business Address: <the address the sending bank checks>
```

(The real values are deliberately NOT in this description: this
repository is public. They live in the tenant's database row.)

## One account, one title

Both blocks print the SAME account name: the title the RECEIVING bank
holds for the account. A document that gave the payer one name in the
domestic block and another in the international block gave them two
answers for one account, and where the payer's bank matches on name,
that is a returned payment.

The REGISTERED BUSINESS ADDRESS is a different fact and stays separate:
the address in the document header is where the business operates from,
the one in the payment block is what the sending bank checks.

The domestic block is still printed. The two answer different
questions: `Bank Details` is how to pay from inside Australia, the
international block is how to pay from outside it.

## What this did NOT change

The stored `account_name` is untouched, and that is deliberate. It is
what the payroll ABA files carry as the payer name, in a 26-character
field, and the remitter name in a 16-character one. The registered
title is longer than both and would be cut mid-word in a file the bank
has already accepted. A document change must not move a payroll
artefact -- `tests/test_account_title.py` pins that the ABA payer name
is still the stored account name.

## Where the values live, and why not in this repository

In the DATABASE (`accounts.user_payment_settings`), not in source. This
repository is PUBLIC, and a SWIFT code, a branch address and a
registered beneficiary name are exactly the kind of thing that must not
be published. The tenant sets them through the route they already set
their domestic bank details through:

```
PUT /api/auth/business
{"international_swift_code": "...", "international_account_name": "...",
 "international_branch_address": "...", "international_business_address": "..."}
```

The SWIFT code is stored Fernet-encrypted at rest, the same treatment the
BSB gets: it identifies the account to the receiving bank. It comes back
MASKED from the API, like the BSB and the account number do. The names
and addresses are plaintext, the same class of value as `account_name`
and the business address.

## Which documents get it

- **invoice** -- yes
- **statement of account** -- yes (it asks for money just as an invoice
  does, so an overseas recipient needs the same wire instructions)
- **quotation** -- no. A quotation deliberately carries no bank details
  at all: it is a proposal, not a request for money.

The block appears only for a recipient KNOWN to be outside Australia, and
only when the tenant has configured it. An EMPTY country is unknown, not
overseas -- the same call `tax.is_overseas` makes, so the invoice's GST
note and its payment instructions cannot disagree. A domestic payer is
not offered a wire transfer they do not need, and might take by mistake
at the cost of a slow, fee-bearing international payment.

The SWIFT code is the marker that international payments are configured
at all: without it the instruction is not actionable, so a partially
configured tenant gets no block rather than a block missing the one
field that makes it work.

## Tests

| file | what |
|---|---|
| `tests/test_invoice_international_payments.py` | **new**: 12 tests -- the block appears with every field, only for an overseas recipient, not for a domestic one, not for an unknown country, not when unconfigured, the domestic details survive, the stored account name is not rewritten, values are escaped, the SWIFT code is stored encrypted and returned masked, the route accepts them, and the statement of account carries the block |
| `tests/test_account_title.py` | **new**: 8 tests -- the domestic block prints the registered title, both blocks carry one title, the statement does too, the international address is labelled the registered business address, the ABA payer name is STILL the stored account name, the title falls back when none is configured, and a title containing markup cannot break the render |

Every test was checked against a MUTATION of the behaviour it claims to
pin: removing the invoice block, dropping the SWIFT-code marker check,
treating an empty country as overseas, dropping the branch address, and
substituting the domestic account name into the international block are
each caught. For the title: reverting the helper to the trading name,
printing the stored account name in the domestic block, reverting the
label, and routing the title into the ABA payer name are each caught.

## Schema

`schema/migrations/2026-11-05_add_international_payment_details.sql` --
four nullable columns on `accounts.user_payment_settings`, plus the
matching `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` in `schema/init.sql`
for a fresh install. Idempotent.

Applied to the live database before this branch was deployed, with a
`pg_dump` taken first. The migration is applied by hand with the
search_path set explicitly, which is why the file says so.
