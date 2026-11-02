## What

The quotations feature: a quote is a PROPOSAL, separate from an invoice
because the two have different lifecycles (a quote expires; an invoice is
paid or overdue) and different required fields (a quote needs a validity
date; an invoice needs a due date).

| file | change |
|---|---|
| `app/models/quote.py` | the model, and the amount arithmetic |
| `app/api/quotes.py` | the routes, and the lifecycle transitions |
| `app/shared/tax.py` | **new**: the GST rules, in one place |
| `app/shared/pdf.py` | the quote PDF |
| `schema/init.sql` | the table |
| `schema/migrations/2026-10-30_add_quotes.sql` | the table, for an existing install |
| `schema/migrations/2026-11-01_add_quote_gst_basis.sql` | why the document carries the tax treatment it carries |
| `schema/migrations/2026-11-01_add_quote_transmission.sql` | the transmission record |
| `tests/test_quotes.py` | 22 tests |
| `tests/test_quote_tax_and_recurring.py` | the tax and recurring-line rules |
| `tests/test_quote_transmission.py` | the transmission record |

Also carries the identifier gate and the scrub of two customer
identifiers that were on a public page (PR #5's branch), so this branch's
tree carries no identifier and the pre-push guard actually runs.

## The model

**Line items are JSONB**, not encoded into a text description the way
invoices do it. The line items are what the customer negotiates, so they
must round-trip exactly -- you cannot rebuild a line item table from a
string you have to parse.

**Amounts are derived, never accepted raw.** The API takes line items and
derives ex-GST / GST / total. If the arithmetic could drift from the
lines the customer sees, the quote is not a quote -- it is a number.

**Transitions are separate endpoints**, so an illegal one (accepted ->
draft) cannot be expressed. It is a 409.

**GST is derived, in one place** (`app/shared/tax.py`), consulted by both
the API and the PDF. A quote that says "no GST" on its face and carries
10% in its totals is a defective artefact, and the cheapest way to prevent
that is one function that answers the question. The BASIS is stored with
the quote, so a document cannot change its tax wording after it is issued.

**A line may be RECURRING** -- a monthly retainer rather than a one-off.
The totals then cover the first period and the PDF shows the one-off and
recurring subtotals separately, because a single "TOTAL: $7,100" beside a
$100/month line reads as a one-off price.

**The transmission record** (`sent_via`, `sent_to`, `sent_document_sha256`)
is the part added last, and it exists because "it was sent" is a claim
about a document that left the building. `status` and `sent_at` said only
that the system was told it went out. The three new columns say HOW it
left -- and that is the distinction that carries the evidential weight,
because `email_system` leaves a delivery record in the mail server and
`email_manual` leaves this row as the only evidence there is -- WHERE it
went, and WHICH BYTES went out. The quote PDF is rendered on demand, so
two renders are different files; without the hash of the transmitted
artefact there is no way to prove later what the recipient holds.

## Verification

* `pytest` -- 472 passed, 2 skipped.
* `scripts/check_no_identifiers.py` -- GREEN on this tree.
* `scripts/pre_push_guard_selftest.sh` -- 11 cases, 11 passed.
* Six injected regressions were each confirmed to FAIL the suite:
  accepting any `sent_via`, accepting any sha256, ignoring the supplied
  `sent_at`, assigning to the ORM object before validating, inferring the
  channel instead of leaving it unset, and responding with the
  transmission record without storing it.

## Not deployed

The quotes API is not in production yet -- this branch is the PR. The
table exists in the live database and the API returns 404 until it
merges.
