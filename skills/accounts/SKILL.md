# accounts Skill

A structured interface for AI agents to interact with the accounts REST API.

By default targets `http://127.0.0.1:5061` (the OSS upstream default port for local dev). For remote / hosted deployments, override via the `base_url=` constructor kwarg or the `ACCOUNTS_BASE_URL` environment variable. The legacy `PY_PG_ACCOUNTS_BASE_URL` env var is also honoured for back-compat with the peristyle-ai install. **Do not hard-code the peristyle-side port (`5075`/`5076`) here** — that lives in the peristyle-side systemd unit and `.env`, and the skill is meant to remain deployment-agnostic.


## Quick Start

```python
from skills.accounts.accounting_skill import AccountingSkill

skill = AccountingSkill(
    api_key="your-api-key-here",
    base_url="http://127.0.0.1:5061",  # default; override via env or kwarg for remote/hosted deployments
)

# Create an expense
expense = skill.create_expense(
    vendor_name="Bunnings",
    ex_gst_amount=100.00,
    expense_date="2026-03-15",
    gst_type=0.1,
)

# List invoices
invoices = skill.list_invoices(start_date="2026-01-01", end_date="2026-03-31")

# Generate an invoice PDF
skill.download_invoice_pdf(invoice_id=invoices[0]["id"], save_path="./invoice.pdf")
```

## What this skill covers

72 public methods across these areas. Read the linked doc BEFORE using any non-trivial feature.

| Area | Methods | Documentation |
|---|---|---|
| Business profile | 5 | [below](#business-profile) |
| Expenses | 8 | [below](#expenses) |
| Customers | 5 | [below](#customers) |
| Invoices (incl. mark-paid/sent/confirmed, PDF) | 12 | [below](#invoices) |
| Account categories | 4 | [below](#account-categories) |
| Reports (P&L, BAS computed, monthly, yearly) | 4 | [below](#reports) |
| Statement of Account | 2 | [statements.md](statements.md) |
| Bank transactions | 5 | [bank-transactions.md](bank-transactions.md) |
| BAS lodgement (audit/reconciliation) | 5 | [BAS.md](BAS.md) |
| Payroll (employees, pay events, super) | ~17 | [payroll.md](payroll.md) |
| OCR queue | 3 | [ocr-queue.md](ocr-queue.md) |

## Authentication

State-changing API requests require an `X-API-Key` header:

```python
headers = {"X-API-Key": "your-api-key-here"}
```

The API accepts two credentials, and they are **not** interchangeable:

| Credential | May do |
|---|---|
| `X-API-Key` header | everything -- reads and writes |
| Session cookie (HMI/PWA login) | **reads only** (`GET`/`HEAD`/`OPTIONS`) |

A cookie-authenticated `POST`/`PUT`/`PATCH`/`DELETE` is refused with
**403** and a message naming the `X-API-Key` requirement. The cookie is
*ambient* -- the browser attaches it to every request, including ones
another site can cause -- so it is not allowed to change state. The HMI
links to the API with plain `<a href>` GET links, so nothing in the UI
needs changing. (2026-10-25 security sweep S7.)

Set in the env via `PY_PG_ACCOUNTS_API_KEY` and the skill picks it up. To override per-instance:

```python
skill = AccountingSkill(api_key="...", base_url="http://127.0.0.1:5061")  # or set ACCOUNTS_BASE_URL / PY_PG_ACCOUNTS_BASE_URL
```

**Storage and rotation.** The key is stored Fernet-encrypted in
`accounts.user_payment_settings.api_key` and the header must carry the
**plaintext** — the stored ciphertext is not accepted as a credential. The
auth path (`UserPaymentSettings.find_by_api_key`) resolves either storage
format, so rotating a key is safe: generate a new one, update every client
that carries it, and the old key stops working immediately. Legacy rows
that still hold a plaintext token continue to work.

### Getting an API key

1. Admin creates account (open registration is disabled — see below)
2. User logs in via HMI at `/login`
3. Generate API key at `/api-key`

**There is no self-service registration.** The `/register`, `/verify/<token>` and `/api/auth/register` routes do not exist -- they return 404. Accounts must be created by the deployment's administrator directly in the users table (see AGENTS.md for instructions); the user then generates their own API key at `/api-key`. There is no email-verification gate.

## Business profile

Logo + business details + bank details + payment terms. All on the `users` table, shown on invoice PDFs.

```python
skill.update_business_details(
    business_name="Acme Pty Ltd",
    abn="12345678901",
    address="123 Main St\nSydney NSW 2000",
    contact_email="billing@acme.com",
    contact_number="+61 2 1234 5678",
    bank_name="Commonwealth Bank",
    account_name="Acme Pty Ltd",
    bsb="062-001",
    account_number="12345678",
    payment_terms=14,  # days, used in PDF "Net X days" footer
)
skill.upload_logo("/path/to/logo.png")
```

To clear a field, pass empty string. No separate delete endpoint.

## Expenses

Each expense has `source` field tracking origin: `api` | `browser` | `pwa`.

```python
expense = skill.create_expense(
    vendor_name="Office Supplies",
    ex_gst_amount=100.00,
    expense_date="2026-03-15",
    gst_type=0.1,
    description="Pens and paper",
)
skill.upload_expense_attachment(expense_id=expense["id"], file_path="/path/receipt.jpg")
```

See [ocr-queue.md](ocr-queue.md) for how PWA uploads flow through OCR + manual review.

## Customers

Customers are required for invoice creation. Stored separately so customer info is consistent across invoices.

```python
customer = skill.create_customer(
    name="Acme Corp",
    contact_email="billing@acme.com",
    abn="98765432109",
    gst=True,
)
```

Cannot delete a customer with existing invoices.

## Invoices

```python
invoice = skill.create_invoice(
    customer_id=customer["id"],   # REQUIRED
    client_name="Acme Corp",
    ex_gst_amount=1500.00,
    invoice_date="2026-03-15",
    gst_type=0.1,
    due_date="2026-03-29",       # invoice_date + payment_terms
    description="Consulting for Q1",
)

# Lifecycle
skill.mark_invoice_sent(invoice_id=invoice["id"])
skill.download_invoice_pdf(invoice_id=invoice["id"], save_path="./invoice.pdf")
skill.mark_invoice_paid(
    invoice_id=invoice["id"],
    payment_date="2026-03-28",
)  # See WARNING below about provenance

# Sending the invoice email is OUTSIDE this skill — use the local-email skill
# (skills/local-email/scripts/mail_api.py) with a pre-built MIME message.
```

**Invoice statuses:** `draft` → `sent` → `paid` (or `cancelled`, `overdue`).

**⚠️ For new code, prefer `create_bank_transaction()` over `mark_invoice_paid()`** so the bank-side provenance (transaction ID, reference, method) is recorded. `mark_invoice_paid()` is kept for backwards compat — see [bank-transactions.md](bank-transactions.md).

## Account categories

Chart of accounts for classifying expenses and invoices.

```python
category = skill.create_account_category(name="Office Supplies", description="Stationery")
categories = skill.list_account_categories()
```

## Reports

```python
# P&L for a date range
report = skill.get_profit_loss_report(start_date="2026-01-01", end_date="2026-03-31")

# Quarterly BAS — COMPUTED figures (1A, 1B, net)
bas = skill.get_quarterly_bas_report(year=2025, quarter=4)
# Returns: gst_collected, gst_paid, gst_owing
# Australian FY: Apr-Jun 2026 = FY2025/26 Q4 (year=2025, quarter=4)

# Monthly / yearly
report = skill.get_monthly_report(year=2026, month=3)
report = skill.get_yearly_finances_report(year=2025)
```

**Important:** these are COMPUTED figures from invoices + expenses. The actual ATO settlement often differs — use `create_bas_lodgement()` to record the FACT of lodgement with the ATO. See [BAS.md](BAS.md).

## Field reference

### GST types
- `0` — no GST
- `0.1` — 10% GST (Australia)

### Date format
All dates use ISO 8601: `YYYY-MM-DD` (or full ISO-8601 datetime for fields like `lodged_at`, `settled_at`).

### Status enums

| Resource | Allowed values |
|---|---|
| Invoice | `draft`, `sent`, `paid`, `overdue`, `cancelled` |
| PayEvent | `draft`, `finalized`, `paid`, `cancelled` |
| SuperPayment | `pending`, `paid`, `reconciled` |
| Employee employment_status | `active`, `terminated` |
| Employee pay_frequency | `weekly`, `fortnightly`, `monthly` |
| Bank txn method | `osko`, `bpay`, `direct_credit`, `cheque`, `cash`, `other` |
| Bank txn raw_source | `manual`, `bank_feed_csv`, `screenshot_ocr` |

## Architecture

- **Base URL**: `http://127.0.0.1:5061` (local dev default). Override via `base_url=` parameter or `PY_PG_ACCOUNTS_BASE_URL` env var for remote / hosted deployments.
- **Auth**: `X-API-Key` header for state-changing requests; a session cookie authenticates reads only
- **Response format**: JSON everywhere
- **PDF generation**: server-side, ReportLab
- **OCR pipeline** (since 2026-08-04): local tesseract + regex parser. Vision LLM pipeline deprecated.
- **PII encryption**: payroll PII fields (TFN, bank details) encrypted at rest via Fernet, key in `PAYROLL_PII_KEY` env var

## Security (2026-09-20 hardening)

- **SECRET_KEY**: Required, no default. App won't start without it.
- **CSRF**: All HMI/PWA forms protected via Flask-WTF. The API is exempt from
  the CSRF *token*, but the ambient session cookie may only authenticate
  reads -- every state-changing API call must carry `X-API-Key`. That is the
  equivalent protection: a credential the browser will not attach on another
  site's behalf.
- **Session cookies**: Secure, HttpOnly, SameSite=Lax.
- **Rate limiting**: Login endpoints — 5 failures per IP per 15 min → 15-min lockout.
- **Registration**: Disabled. Admin-created accounts only.
- **Full audit**: the hardening above is summarised in `AGENTS.md` (section
  "Security Hardening"). The original `SECURITY_SWEEP.md` was an internal
  audit document and was not published with this repository.

## Common workflows

| Workflow | Doc |
|---|---|
| Weekly recurring invoice for a customer | `scripts/` cron — see the deployment's own schedule |
| Record a received bank payment | [bank-transactions.md](bank-transactions.md) |
| End-of-quarter BAS lodgement | [BAS.md](BAS.md) |
| Generate customer statement of account | [statements.md](statements.md) |
| Weekly payroll run | [payroll.md](payroll.md) |
| OCR queue manual review | [ocr-queue.md](ocr-queue.md) |

## Error handling

The skill uses `requests` and raises `HTTPError` on non-2xx:

```python
try:
    invoice = skill.create_invoice(...)
except requests.exceptions.HTTPError as e:
    print(f"API error: {e.response.json()}")
```

## Requirements

```bash
pip install requests
```

No other dependencies.
