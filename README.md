# Peristyle Accounts

Open-source accounting system for Australian small businesses. Tracks income
(GST), expenses (with attachments and OCR), customers, invoices, payments,
payroll, super contributions, and quarterly BAS lodgement. Includes a
browser-based HMI for manual entry, a REST API for programmatic access, a
PWA for receipt capture on mobile, and a structured Python skill for AI
agents.

## Features

- **Income & expenses** — full CRUD, vendor/amount/GST categorisation, file
  attachments.
- **Invoicing** — draft → sent → paid lifecycle with PDFs and email delivery.
- **Customers & suppliers** — structured addresses, contact details,
  statement-of-account PDFs.
- **Bank transactions** — receive-side ledger with bank provenance
  (transaction ID, BSB/account, Osko/BPay/direct-credit method).
- **Pending payment reconciliation** — link inbound Xero payment advice to
  invoices once the bank clears.
- **Quarterly BAS** — lodgement workflow with ATO-aligned GST settlement.
- **Payroll** — employees, pay events, encrypted PII at rest, payslip PDF
  generation and email delivery, super contribution exports.
- **OCR queue** — local tesseract pipeline for receipt scanning with
  manual-review fallback.
- **Reports** — profit & loss, quarterly BAS, monthly, yearly.
- **Xero export** — one-shot CSV bundle (invoices, bills, contacts) for
  importing a full financial year into Xero.

## Architecture

```
app/
  api/        Flask blueprint with REST endpoints
  hmi/        Flask blueprint with browser UI
  pwa/        Progressive Web App for receipt capture (under HMI)
  models/     SQLAlchemy ORM models
  shared/     cross-cutting helpers (PDF generation, address parsing, …)

processes/
  ocr_processor.py    background worker that drains the OCR queue

skills/
  accounts/          Python skill for AI agents (see SKILL.md there)

schema/
  init.sql            canonical DDL (mirrors what db.create_all() produces)

scripts/              one-shot utility scripts (backfills, exports, …)
tests/                pytest suite
```

## Quick start

```bash
# Install Python deps + system binaries
pip install -r requirements.txt
sudo apt install tesseract-ocr tesseract-ocr-eng poppler-utils

# Create the database
createdb accounts

# Configure. BOTH files are git-ignored - never commit either one.
cp .env.example .env               # DB creds, mail server, encryption key
cp config.yaml.example config.yaml # app settings (schema, STP identifiers)

# Initialize the schema
python run.py --init-db

# Start both servers (default ports; override via CLI flags)
python run.py
#   --api-port 5061   (default; override to taste)
#   --hmi-port 5062   (default; override to taste)
#   --host 127.0.0.1  (default; pass 0.0.0.0 to expose on the LAN)
```

`config.yaml` and `.env` are both **git-ignored**, and both may contain
credentials. `config.yaml.example` is the tracked template: copy it, fill
in your own values, and never commit the copy.

The default bind address is `127.0.0.1`. The API and HMI each take an
explicit `--api-port` / `--hmi-port` flag (no port is hard-coded into the
application — set it per deployment, e.g. `5061`/`5062` for the OSS
upstream default, or any other free port for a hosted install). All
secrets (DB password, mail server credentials, payroll encryption key)
are read from the `.env` file; the systemd unit files load it via
`EnvironmentFile=`.

## Documentation

- **`AGENTS.md`** — full system reference: every endpoint, every model
  field, every background process.
- **`skills/accounts/SKILL.md`** — agent-facing quick start (Python
  skill overview).
- **`skills/accounts/*.md`** — feature-specific workflows: BAS,
  bank transactions, statements, payroll, OCR queue.
- **`config.yaml.example`** — every supported config knob.
- **`schema/init.sql`** — the canonical DDL, mirroring what
  `db.create_all()` produces.

## AI agent skill

```python
from skills.accounts.accounting_skill import AccountingSkill

skill = AccountingSkill(
    api_key="...",
    base_url=os.environ.get("ACCOUNTS_BASE_URL", "http://127.0.0.1:5061"),
)
expense = skill.create_expense(
    vendor_name="Acme",
    ex_gst_amount=100.00,
    expense_date="2026-03-15",
)
```

Override the base URL via the `ACCOUNTS_BASE_URL` environment variable
or the `base_url=` constructor kwarg. (Legacy `PY_PG_ACCOUNTS_BASE_URL`
is also honoured for back-compat.)

### Database schema

By default every table lives in the `public` schema of the database named
by `DB_NAME`, and the app owns its own `users` table. Two optional
settings change that:

| Setting | Default | Effect |
|---|---|---|
| `ACCOUNTS_SCHEMA` | `public` | Which schema the business tables install into. Set it to share one database with other applications and keep the tables namespaced. |
| `USERS_TABLE` | `<ACCOUNTS_SCHEMA>.users` | Which table holds the auth rows. Point it at an existing table (e.g. `public.users`) when a separate identity provider owns authentication. |

Both are read from the environment first, then `config.yaml`. When you set
`USERS_TABLE` to an external table, that table must already exist and
have at least `id`, `email` and `password_hash` columns; the app will not
create it for you.

Install into a dedicated schema with:

```bash
python init_db.py --mode=schema      # ACCOUNTS_SCHEMA=my_schema
python init_db.py --mode=standalone  # public schema (default)
```

`init_db.py` applies the canonical DDL in `schema/`. `run.py --init-db`
does the same job via SQLAlchemy's `create_all()`, which is usually
simpler for a first install. Both are idempotent.

### The auth table

The `users` table holds authentication only - `id`, `email`,
`password_hash`, `display_name`, `role`, `is_active` and timestamps.
Everything else about the business owner (business name, ABN, postal
address, contact details, logo, bank details, API key) lives in two
tables keyed by `user_id`:

- `user_profiles` - business profile and contact details
- `user_payment_settings` - bank details and the API key, with the
  sensitive columns encrypted at rest (Fernet) and masked in API
  responses by default


## Development

```bash
# Activate venv
source venv/bin/activate

# Run tests
pytest tests/
```

## License

MIT — see [LICENSE](LICENSE).
