# Bank Transactions

Records received payments with full bank-side provenance: transaction ID, reference string, method (Osko/BPay/etc.), payer info, amount, settlement date. Optional FK back to invoice so each bank transaction can be linked to the invoice it pays.

**Why this exists:** Before this table, the only payment fields on an invoice were `amount_paid` / `payment_date` / `paid_at`. The bank-side provenance (transaction ID, reference, method) had nowhere to go, so we couldn't reconcile bank statements against invoices or do real BAS settlement-date tracking.

## Schema (12 columns)

```
bank_transactions
  id                  UUID PK
  user_id             FK -> users.id
  invoice_id          FK -> invoices.id (nullable)
  transaction_id      bank-side txn ID, e.g. CTBAAUSNXXXN...
  reference           payer-supplied, e.g. 'INV-XXX - SOFTWARE'
  method              osko | bpay | direct_credit | cheque | cash | other
  payer_name          who sent the money
  payer_account       payer-side account if known
  amount              always positive
  currency            ISO-4217, default AUD
  transaction_date    date the bank shows it as processed (BAS date)
  settled_at          precise timestamp if known
  notes               free-text
  raw_source          manual | bank_feed_csv | screenshot_ocr
  created_at          when we recorded it
```

No `updated_at` — this is an immutable audit row. If the bank record was wrong, **delete and re-record**.

## API

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/bank-transactions` | Record a new txn (optionally auto-mark invoice paid) |
| GET | `/api/bank-transactions` | List (filter invoice_id/method/date_from/date_to) |
| GET | `/api/bank-transactions/<id>` | Get one |
| PUT | `/api/bank-transactions/<id>` | Update metadata (mutable: method, notes, payer_*) |
| DELETE | `/api/bank-transactions/<id>` | Delete (does NOT unmark invoice paid) |

## POST behaviour

The POST endpoint has three modes depending on the linked invoice's status:

| invoice.status | What happens |
|---|---|
| `draft` or `sent` | Create bank txn AND atomically mark invoice `paid` (sets `payment_date`, `amount_paid`, `paid_at`) |
| `paid` | Create bank txn linked to the existing invoice. Does **not** mutate the invoice. (Backfill / late reconciliation.) |
| `cancelled` | 409 — can't record payment on cancelled invoice |
| not supplied | Create a free-standing bank txn (non-invoice receipts like interest, refunds, owner contributions) |

All-or-nothing: if any DB operation fails, the whole POST rolls back.

## Common workflows

### Record a payment + auto-mark invoice paid (most common)

When you receive a bank notification and want to mark the matching invoice paid atomically:

```python
from skills.accounts.accounting_skill import AccountingSkill
skill = AccountingSkill(api_key="...")

result = skill.create_bank_transaction(
    amount=2131.80,
    transaction_date="2026-09-01",
    invoice_id="5c1a4c6f-...",   # the matching invoice
    transaction_id="CTBAAUSNXXXN20260831050082279008000",
    reference="5C1A4C6F - SOFTWARE",
    method="osko",
    payer_name="EXAMPLE CUSTOMER PTY LTD",
    payer_account="000-000 000000000",
    notes="Weekly invoice INV-XXXXXXXX paid via Osko",
)
# result['message'] == 'Bank transaction recorded and invoice marked paid'
# result['invoice']['status'] == 'paid'
```

### Backfill an already-paid invoice (no invoice mutation)

If you recorded a payment via the old `/api/invoices/<id>/mark-paid` endpoint **before** this table existed, you can still record the bank transaction now and link it to the existing paid invoice:

```python
result = skill.create_bank_transaction(
    amount=2131.80,
    transaction_date="2026-09-01",
    invoice_id="5c1a4c6f-...",
    transaction_id="CTBAAUSNXXXN20260831050082279008000",
    reference="5C1A4C6F - SOFTWARE",
    method="osko",
    payer_name="EXAMPLE CUSTOMER PTY LTD",
    payer_account="000-000 000000000",
    notes="Backfilled: invoice was already marked paid via legacy /mark-paid endpoint.",
)
# result['message'] == 'Bank transaction recorded (linked to already-paid invoice)'
```

The invoice's `paid_at` / `amount_paid` / `payment_date` are NOT overwritten — the bank txn just records provenance.

### Record a partial payment

For partial payments, do the mark-paid step manually first (with partial amount) then record the bank txn without invoice_id (or with the second invoice for full settlement):

```python
# Step 1: mark invoice partially paid
skill.mark_invoice_paid(
    invoice_id="inv-123",
    payment_date="2026-09-15",
    amount_paid=500.00,   # partial
)
# Step 2: record the bank txn that covered the partial payment
skill.create_bank_transaction(
    amount=500.00,
    transaction_date="2026-09-15",
    invoice_id="inv-123",
    reference="PARTIAL PAYMENT - INV-123",
    method="direct_credit",
)
```

### Record a non-invoice receipt (interest, refund, owner contribution)

Just omit `invoice_id`:

```python
skill.create_bank_transaction(
    amount=12.50,
    transaction_date="2026-09-30",
    payer_name="CBA",
    reference="INTEREST SEP 2026",
    method="direct_credit",
    notes="Monthly account interest",
)
# Creates a free-standing bank txn, not linked to any invoice
```

### List all bank txns for one invoice

```python
txns = skill.list_bank_transactions(invoice_id="5c1a4c6f-...")
for t in txns['bank_transactions']:
    print(f"{t['transaction_date']}: ${t['amount']} via {t['method']} - {t['reference']}")
```

### Reconciliation query

To find any bank transactions that haven't been linked to an invoice yet (useful for catching missed payments):

```python
all_txns = skill.list_bank_transactions()
unlinked = [t for t in all_txns['bank_transactions'] if not t['invoice_id']]
print(f"{len(unlinked)} bank transactions without an invoice link")
```

## Method values

`BANK_TXN_METHODS = ('osko', 'bpay', 'direct_credit', 'cheque', 'cash', 'other')`

Use lowercase. POST returns 400 if you pass an unknown method.

## What `transaction_date` means

This is the date the bank shows as processed — it's the date that goes on the BAS as the settlement date. Distinct from `settled_at` (precise timestamp if known) and `created_at` (when we recorded it in accounts, which can lag by days).

## POST a duplicate

POST does NOT check for duplicate bank transactions by transaction_id. If you import the same bank feed twice, you'll get two rows with the same `transaction_id`. The duplicate check is the agent's job:

```python
# Before recording, check if we already have this bank txn
existing = skill.list_bank_transactions(date_from="2026-09-01", date_to="2026-09-01")
for t in existing['bank_transactions']:
    if t.get('transaction_id') == 'CTBAAUSNXXXN...':
        print("Already recorded, skipping")
        break
```

## DELETE gotcha

DELETE removes the bank txn row but does NOT unmark the linked invoice as paid. The invoice payment record (`amount_paid` / `payment_date` / `paid_at`) is independent. If you want to undo both, you'd have to:
1. DELETE the bank txn
2. Reset the invoice fields via direct SQL (no API for "unmark paid" yet)

Split lifecycles are deliberate — we don't want to lose the bank record when an invoice gets re-opened.

## Related

- See `BAS.md` for how bank txn dates feed into the BAS lodgement workflow
- See `SKILL.md` for general skill setup
