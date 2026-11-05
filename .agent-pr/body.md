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
Account Name: <the trading name on the invoice>
BSB: <bsb>
Account Number: <account number>

International Payment Details:
Account Name: <the registered beneficiary name>
Branch Address: <the receiving branch's address>
Swift Code: <the SWIFT/BIC code>
Business Postal Address: <the address the sending bank checks>
```

(The real values are deliberately NOT in this description: this
repository is public. They live in the tenant's database row.)

## The two blocks are deliberately different facts

The domestic account name is the trading name on the invoice. The
international one is the name the RECEIVING bank holds, and a wire
quoting the wrong one is rejected. Same for the business postal address:
the address in the document header is where the business operates from;
the one in the payment block is what the sending bank checks. Merging
them would put a wrong name or a wrong address on a wire, so they are
separate columns and neither overwrites the other.

The domestic block is still printed. The two answer different
questions: `Bank Details` is how to pay from inside Australia, the
international block is how to pay from outside it.

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
| `tests/test_invoice_international_payments.py` | **new**: 12 tests -- the block appears with every field, only for an overseas recipient, not for a domestic one, not for an unknown country, not when unconfigured, the domestic details survive, the two account names stay distinct, values are escaped, the SWIFT code is stored encrypted and returned masked, the route accepts them, and the statement of account carries the block |

Every test was checked against a MUTATION of the behaviour it claims to
pin: removing the invoice block, dropping the SWIFT-code marker check,
treating an empty country as overseas, dropping the branch address, and
substituting the domestic account name into the international block are
each caught.

## Schema

`schema/migrations/2026-11-05_add_international_payment_details.sql` --
four nullable columns on `accounts.user_payment_settings`, plus the
matching `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` in `schema/init.sql`
for a fresh install. Idempotent.

Applied to the live database before this branch was deployed, with a
`pg_dump` taken first. The migration is applied by hand with the
search_path set explicitly, which is why the file says so.
