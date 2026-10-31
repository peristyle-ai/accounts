# Agent Documentation

This document provides a summary of the accounting system for AI agents interacting with this codebase.

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env with database credentials and API keys

# Run database migrations (auto-creates tables)
python run.py --init-db

# Start servers (binds to 127.0.0.1 by default — override with --host for LAN)
python run.py
```

The API server defaults to port **5061** and the HMI to port **5062**. Both bind to `127.0.0.1` (loopback) by default. For LAN/hosted deployments, pass `--host=<your-ip>` (e.g. `--host=0.0.0.0` to bind on all interfaces).

Throughout this document, `localhost:5061` is used as the API base URL in examples — substitute your actual host for remote deployments.

## Architecture

### Servers
- **API Server**: `localhost:5061` (REST API for agent access)
- **HMI (Browser UI)**: `localhost:5062` (Human interface for manual data entry)

### Key Components
1. **Flask Application**: Python web framework
2. **PostgreSQL Database**: Stores all data
3. **SQLAlchemy ORM**: Database abstraction layer
4. **OCR Queue Processor**: Background worker for receipt scanning

## Authentication

### Registration (Disabled)

Open registration is **disabled** as of the 2026-09-20 security hardening. Both the API (`POST /auth/register`) and HMI (`/register`) endpoints return 403/redirect with "Contact an administrator".

To create a new account, an admin must insert directly into the database:

```sql
-- Generate a bcrypt hash for the password first:
-- python3 -c "from werkzeug.security import generate_password_hash; print(generate_password_hash('thepassword'))"

INSERT INTO users (email, password_hash, country, email_verified)
VALUES ('user@example.com', '<bcrypt_hash>', 'AU', TRUE);
```

Or via Python shell:
```python
from app import create_app
from app.models import db
from app.models.user import User

app = create_app()
with app.app_context():
    user = User(email='user@example.com', country='AU')
    user.set_password('thepassword')
    user.email_verified = True
    db.session.add(user)
    db.session.commit()
    print(f'Created user {user.id}')
```

### For API Access
1. Admin creates account (see above)
2. User logs in via HMI at `/login`
3. User generates API key at `/api-key`
4. API key is passed in requests for authentication

### API Authentication
```python
headers = {"X-API-Key": "user_api_key_here"}
```

The same API endpoints also accept HMI session cookies (set after login) — but a cookie may only authenticate **reads**. Any state-changing request (`POST`/`PUT`/`PATCH`/`DELETE`) must carry the `X-API-Key` header; a cookie-authenticated write is refused with 403. The cookie is *ambient* — the browser attaches it automatically, including to requests another site can cause — so it is not allowed to change state. The HMI links to the API with plain GET links, so nothing in the UI needs changing.

### For HMI Access
- Standard email/password login at `/login`
- Session-based authentication
- Rate limited: 5 failed attempts per IP per 15 min → 15-min lockout

### Security Hardening (2026-09-20)

- **SECRET_KEY**: Required — no default. App raises `RuntimeError` on startup if not set or set to `'dev-secret-key'`.
- **CSRF**: Flask-WTF CSRFProtect on all HMI/PWA forms. API blueprints are exempt from the CSRF *token*, but the ambient session cookie authenticates reads only — every state-changing API call must carry `X-API-Key`.
- **Session cookies**: `Secure`, `HttpOnly`, `SameSite=Lax`. `SESSION_COOKIE_SECURE` defaults to `True` (override via env for local dev without HTTPS).
- **Rate limiting**: Login endpoints limited to 5 failed attempts per IP per 15-minute window.
- **Registration**: Disabled on both API and HMI.

The list above is the published summary of the audit. The original
`SECURITY_SWEEP.md` was an internal document and was not published with this
repository.

## Database Schema

### users
| Column | Type | Notes |
|--------|------|-------|
| id | UUID | Primary key |
| email | VARCHAR(255) | Unique |
| password_hash | VARCHAR(255) | bcrypt |
| ~~api_key~~ | -- | REMOVED. The API key lives in `accounts.user_payment_settings.api_key` (Fernet encrypted, VARCHAR(500)). Use the `api_key_plain` accessor; never read the raw column. |
| email_verified | BOOLEAN | Must be TRUE before API key generation |
| verification_token | VARCHAR(64) | For email verification |
| business_name | VARCHAR(255) | Shown on PDF invoices |
| abn | VARCHAR(20) | Australian Business Number, shown on PDF |
| address | TEXT | Business address, shown on PDF |
| contact_email | VARCHAR(255) | Shown on PDF |
| contact_number | VARCHAR(50) | Shown on PDF |
| logo_path | VARCHAR(500) | Path to logo, top-left on PDF |
| bank_name | VARCHAR(255) | Bank name, shown on PDF |
| account_name | VARCHAR(255) | Account holder name, shown on PDF |
| account_number | VARCHAR(50) | Bank account number, shown on PDF |
| bsb | VARCHAR(20) | Bank State Branch, shown on PDF |
| payment_terms | INTEGER | Default payment terms in days (default: 14), shown on PDF |

### expenses
| Column | Type | Notes |
|--------|------|-------|
| id | UUID | Primary key |
| user_id | UUID | Foreign key |
| vendor_name | VARCHAR(255) | OCR extracted |
| ex_gst_amount | NUMERIC(12,2) | Before GST |
| gst_amount | NUMERIC(12,2) | GST amount |
| gst_type | NUMERIC(3,1) | 0 or 0.1 |
| total_amount | NUMERIC(12,2) | Including GST |
| expense_date | DATE | Date of expense |
| attachment_path | VARCHAR(500) | Path to receipt image |
| requires_review | BOOLEAN | TRUE if OCR failed - needs manual review |
| source | VARCHAR(20) | 'api', 'browser', or 'pwa' |

### invoices
| Column | Type | Notes |
|--------|------|-------|
| id | UUID | Primary key |
| user_id | UUID | Foreign key |
| customer_id | UUID | Foreign key to customers (REQUIRED) |
| client_name | VARCHAR(255) | Client name for display |
| ex_gst_amount | NUMERIC(12,2) | Before GST |
| gst_amount | NUMERIC(12,2) | GST amount |
| gst_type | NUMERIC(3,1) | 0 or 0.1 |
| total_amount | NUMERIC(12,2) | Including GST |
| invoice_date | DATE | Invoice date |
| due_date | DATE | Due date |
| attachment_path | VARCHAR(500) | Path to attachment |
| status | VARCHAR(20) | draft, sent, paid, overdue, cancelled (default: draft) |
| payment_date | DATE | Date payment was received |
| amount_paid | NUMERIC(12,2) | Amount paid (for partial payments) |
| sent_at | TIMESTAMPTZ | Timestamp when invoice was sent |
| confirmed_received_at | TIMESTAMPTZ | Timestamp when customer confirmed receipt |
| paid_at | TIMESTAMPTZ | Timestamp when payment was received |

### quotes
Added 2026-10-30 (`feat/quotes`). A quote is a PROPOSAL -- it is a separate
table from invoices, not a status on invoices, because the two have
different lifecycles (a quote expires; an invoice is paid or overdue).

| Column | Type | Notes |
|--------|------|-------|
| id | UUID | Primary key |
| user_id | BIGINT | Tenant owner |
| customer_id | UUID | Foreign key to customers (REQUIRED) |
| quote_number | VARCHAR(32) | Human reference, e.g. Q-2026-0001 (unique per tenant) |
| client_name | VARCHAR(255) | Client name for display |
| lines | JSONB | `[{"description": str, "amount": str}]` -- amounts are strings, never floats |
| ex_gst_amount | NUMERIC(12,2) | DERIVED from the lines |
| gst_amount | NUMERIC(12,2) | DERIVED |
| gst_type | NUMERIC(3,1) | 0 or 0.1 |
| total_amount | NUMERIC(12,2) | DERIVED |
| quote_date | DATE | When the quote was issued |
| valid_until | DATE | Expiry (defaults to quote_date + 30 days) |
| status | VARCHAR(20) | draft, sent, accepted, declined, expired (default: draft) |
| notes | TEXT | Free text shown on the PDF |
| sent_at / accepted_at / declined_at | TIMESTAMPTZ | Lifecycle timestamps |
| converted_invoice_id | UUID | The invoice this quote became; set once, so it cannot be invoiced twice |

**Amounts are never accepted raw.** The API takes line items and derives
ex-GST, GST and total. A caller that also sends `ex_gst_amount` has it
ignored -- the quote is the lines, and the totals are arithmetic.

Endpoints (all under `/api/quotes`, all `api_key_required`):
`GET/POST ''`, `GET/PUT/DELETE '/<id>'`, `GET '/<id>/pdf'`,
`POST '/<id>/mark-sent'`, `POST '/<id>/accept'`, `POST '/<id>/decline'`,
`POST '/<id>/convert-to-invoice'`. Lifecycle transitions are separate
endpoints, so an illegal transition (accepted -> draft) cannot be
expressed; it is a 409.

### customers
| Column | Type | Notes |
|--------|------|-------|
| id | UUID | Primary key |
| name | VARCHAR(255) | Required |
| contact_name | VARCHAR(255) | Contact person name |
| address | TEXT | Postal address |
| contact_email | VARCHAR(255) | Email |
| abn | VARCHAR(20) | Australian Business Number |
| contact_number | VARCHAR(50) | Phone |
| gst | BOOLEAN | GST registered (default TRUE) |

### ocr_queue
| Column | Type | Notes |
|--------|------|-------|
| id | UUID | Primary key |
| expense_id | UUID | Foreign key to expenses |
| image_path | VARCHAR(500) | Receipt image path |
| status | VARCHAR(20) | pending/processing/completed/failed |
| extracted_data | JSONB | OCR results |
| attempts | INTEGER | Retry count |

## OCR Queue System

Receipts uploaded via PWA are processed asynchronously:

1. PWA uploads image → Creates expense with "Pending OCR" placeholder
2. OCR job queued in `ocr_queue` table with status "pending"
3. `ocr_processor.py` polls queue, runs **local tesseract OCR** + regex parser
4. Extracted data updates expense record
5. Job status set to "completed" (auto) or "failed" (manual review needed)

### Tesseract Pipeline

- **Stage 1**: tesseract OCR (local) — image → raw text
- **Stage 2**: regex parser (local) — raw text → {vendor, date, amounts, gst}
- **Stage 3**: manual review (agent, optional) — if auto-parse fails, image + raw
  text saved to `ocr_queue.extracted_data` for an agent to pick up via
  the `image` tool and PATCH the expense back via the API.

### System Dependencies

```bash
# Required on the host running the OCR processor
sudo apt install tesseract-ocr          # English by default
sudo apt install tesseract-ocr-eng      # explicit
sudo apt install poppler-utils          # PDF receipts (pdftoppm)
```

### OCR Config (config.yaml)

```yaml
ocr:
  tesseract_lang: "eng"
  min_confidence: "low"          # 'high' is strict, 'low' accepts partial parses
  stale_timeout_seconds: 300
  manual_review_enabled: true
```

### Running OCR Processor

```bash
# As systemd service (recommended)
systemctl --user start ocr-processor

# Or manually
python processes/ocr_processor.py --config config.yaml

# One-shot batch (test mode)
python processes/ocr_processor.py --config config.yaml --once
```

### Manual Review Workflow

When tesseract can't parse a receipt, the job is marked `failed` and the raw
OCR text is saved to `ocr_queue.extracted_data.raw_text`. An agent picks
these up via the API:

```bash
# List jobs needing manual review
curl -s "http://localhost:5061/api/ocr-queue?needs_review=1" \
  -H "X-API-Key: <api_key>" | jq

# Inspect one job in detail (raw text + parsed fields)
curl -s "http://localhost:5061/api/ocr-queue/<job_id>" \
  -H "X-API-Key: <api_key>" | jq

# After manual review, mark the job done
curl -s -X POST "http://localhost:5061/api/ocr-queue/<job_id>/mark-completed" \
  -H "X-API-Key: <api_key>"
```

The agent uses its image-capable vision tool on `image_path` to extract clean
fields, then PATCHes the expense:

```bash
curl -X PUT "http://localhost:5061/api/expenses/<expense_id>" \
  -H "X-API-Key: <api_key>" \
  -H "Content-Type: application/json" \
  -d '{
    "vendor_name": "Bunnings",
    "ex_gst_amount": 45.50,
    "gst_amount": 4.55,
    "gst_type": 0.1,
    "total_amount": 50.05,
    "expense_date": "2026-08-04"
  }'
```

PUT auto-clears `requires_review=false` when the supplied vendor_name is real
(not "REQUIRES REVIEW") or any amount/date is provided.

## Common Operations

All examples below use `localhost:5061` as the API base URL — substitute your actual host for remote deployments (e.g. `https://accounts.example.com`).

### Create an Expense (via API)
```bash
curl -X POST http://localhost:5061/api/expenses \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your_api_key" \
  -d '{
    "vendor_name": "Bunnings",
    "ex_gst_amount": 45.50,
    "gst_type": 0.1,
    "expense_date": "2024-03-15",
    "description": "Hardware supplies"
  }'
```

### List Expenses
```bash
curl http://localhost:5061/api/expenses \
  -H "X-API-Key: your_api_key"
```

### Generate Report
```bash
curl "http://localhost:5061/api/reports/quarterly-bas?quarter=1&year=2024" \
  -H "X-API-Key: your_api_key"
```

### Create a Customer
```bash
curl -X POST http://localhost:5061/api/customers \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your_api_key" \
  -d '{
    "name": "Acme Corp",
    "address": "123 Main St, Sydney NSW 2000",
    "contact_email": "billing@acme.com",
    "abn": "12345678901",
    "contact_number": "+61 2 1234 5678",
    "gst": true
  }'
```

### Create an Invoice (requires customer_id)
```bash
curl -X POST http://localhost:5061/api/invoices \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your_api_key" \
  -d '{
    "customer_id": "customer-uuid-here",
    "client_name": "Acme Corp",
    "ex_gst_amount": 1000.00,
    "gst_type": 0.1,
    "invoice_date": "2024-03-15",
    "due_date": "2024-04-15"
  }'
```

### Download Invoice PDF
```bash
curl -o invoice.pdf "http://localhost:5061/api/invoices/<invoice-id>/pdf" \
  -H "X-API-Key: your_api_key"
```

### Update Business Profile
```bash
curl -X PUT http://localhost:5061/api/auth/business \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your_api_key" \
  -d '{
    "business_name": "Acme Pty Ltd",
    "abn": "12345678901",
    "address": "123 Main St\nSydney NSW 2000",
    "contact_email": "billing@acme.com",
    "contact_number": "+61 2 1234 5678",
    "bank_name": "Commonwealth Bank",
    "account_name": "Acme Pty Ltd",
    "bsb": "062-001",
    "account_number": "12345678",
    "payment_terms": 14
  }'
```

### Upload Business Logo
```bash
curl -X POST http://localhost:5061/api/auth/logo \
  -H "X-API-Key: your_api_key" \
  -F "file=@/path/to/logo.png"
```

## Project Structure

```
accounts/
├── app/
│   ├── __init__.py          # Flask app factory
│   ├── api/routes.py       # REST API endpoints
│   ├── hmi/routes.py        # Browser UI routes
│   ├── pwa/routes.py        # PWA routes
│   └── models/              # SQLAlchemy models
│       ├── user.py
│       ├── expense.py
│       ├── invoice.py
│       ├── customer.py
│       ├── ocr_queue.py
│       └── activity_log.py
├── processes/
│   └── ocr_processor.py     # OCR queue worker
├── schema/
│   └── init.sql             # Database schema
├── .env                     # Secrets (git-ignored)
├── run.py                   # Entry point
└── venv/                    # Python environment
```

## Important Notes

1. **No self-service registration**: there is no `/register`, no `/verify/<token>` and no `/api/auth/register` route -- they return 404. The maintaining administrator creates accounts directly in the users table; each user then generates their own API key at `/api-key`. There is no email-verification gate.

2. **OCR Processing**: Receipts uploaded via PWA require OCR processing. The expense will show "Pending OCR" until processed.

3. **OCR Pipeline**: Local tesseract + regex parser. Manual review of low-confidence parses can be done by an agent with a native vision tool via the API.

4. **Financial Year**: Australian (July 1 - June 30)

5. **GST Types**: 0 (no GST) or 0.1 (10%)

6. **Tests run against the live production database** (2026-09-29). There is no separate test DB. Any test that mutates a real row can silently corrupt production state. Use the snapshot/restore fixtures in `tests/conftest.py`:

   - `restore_user` — snapshots `payroll@example.com` on entry, restores on exit (even on assertion failure). Use in any test that mutates the business's own address, bank details, contact info, etc.
   - `snapshot_row(model, pk)` — registers a snapshot for any other row. Use for customers, employees, system settings, etc.

   **Never** hardcode placeholder values in test cleanup (e.g. `u.address_line1 = '1 Example St'`). That pattern silently overwrites production state every time the test suite runs. See `tests/test_address_refactor.py::test_business_address_update_normalises_state` for an example of the wrong pattern (deleted 2026-09-29) and the right pattern (uses `restore_user`).

## Troubleshooting

### OCR jobs not processing
```bash
# Check OCR processor status
systemctl --user status ocr-processor

# Check for pending jobs
psql -d accounts -c "SELECT * FROM ocr_queue WHERE status = 'pending';"
```

### Database connection issues
```bash
# Check PostgreSQL
systemctl --user status postgresql

# Test connection
psql -d accounts -c "SELECT 1;"
```

### View application logs
```bash
journalctl --user -u accounts -n 50
```
