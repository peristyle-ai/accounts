## What

This branch is the **invoice tax basis** work, rebased onto the current
`main`:

| file | change |
|---|---|
| `app/models/invoice.py` | the invoice carries its currency, its tax basis and its terms |
| `app/api/routes.py` | the routes read and validate those fields |
| `app/shared/tax.py` | the GST rules, in one place |
| `app/shared/pdf.py` | the invoice PDF prints the tax treatment it carries |
| `schema/init.sql` | the columns |
| `schema/migrations/2026-11-02_add_invoice_gst_basis.sql` | the columns, for an existing install |
| `tests/test_invoice_tax_basis.py` | the tax-basis rules |
| `tests/test_quote_tax_and_recurring.py` | the tax and recurring-line rules |

The invoice description is deliberately left alone: it carries intentional
`<br/>` markup that the line-item parser relies on.

## Why the branch was rebased (2026-11-03)

This branch was cut before the identifier scrub and before the quotes
feature landed, so its history carried a mailbox and its content
duplicated work that is already on `main` (#5's scrub, #6's quotes, #8's
migrations). A straight rebase conflicted in the quotes files.

So the branch was rebuilt as **`main` + this work only**:

- `feat(invoices): the invoice states its currency, its tax basis and its
  terms` — the author is preserved; the feature commit applied cleanly onto
  `main`'s code, so nothing in the tax or PDF logic was rewritten by hand.
- `chore(pr): this text`.

The title and body that were on the branch described the **quotes**
feature, which landed as #6 — they are replaced here with what the branch
actually carries.

## Verified

- identifier scan GREEN on the branch tree (184 files, 0 findings);
- no mailbox anywhere in the branch's history;
- the repository's own pre-push guard allows the ref;
- the branch carries the published `main`.
