# Accounting Software Specification

## Overview
Open-source accounting software built with Python, Flask, and PostgreSQL. Designed for Australian small businesses with GST tracking and BAS reporting capabilities.

## Architecture

### Four Main Interface Components

1. **API Server** (Flask on `${API_HOST:127.0.0.1}:5061`)
   - Serves persistent AI agent interactions via REST API
   - Provides skill-based interface for expense/invoice CRUD operations
   - Supports file uploads (images, PDFs)
   - Local network only

2. **Browser HMI** (Flask on `${API_HOST:127.0.0.1}:5062`)
   - Human-machine interface for manual data entry
   - Full CRUD for expenses and invoices
   - Report generation (Quarterly P&L with GST, Yearly P&L)
   - Local network only

3. **PWA** (Flask on `/pwa` route under HMI - `${API_HOST:127.0.0.1}:5062/pwa`)
   - Progressive Web App for receipt capture
   - Mobile-friendly interface for taking photos of receipts
   - Upload directly as new expenses

4. **Agent Skill** (`skills/` folder)
   - Provides AI agent access to database operations
   - Wraps API calls for LLM/agent consumption

## Database Schema

### Tables

#### users
| Column | Type | Description |
|--------|------|-------------|
| id | UUID | Primary key |
| email | VARCHAR(255) | Unique email address |
| password_hash | VARCHAR(255) | Bcrypt hashed password |
| country | VARCHAR(50) | Country code (default: 'AU') |
| api_key | VARCHAR(64) | Unique API key for agent access |
| email_verified | BOOLEAN | Email verification status (default: FALSE) |
| verification_token | VARCHAR(64) | Token for email verification |
| business_name | VARCHAR(255) | Business name (appears on PDF invoices) |
| abn | VARCHAR(20) | Australian Business Number (appears on PDF) |
| address | TEXT | Business address (appears on PDF) |
| contact_email | VARCHAR(255) | Business contact email (appears on PDF) |
| contact_number | VARCHAR(50) | Business contact phone (appears on PDF) |
| logo_path | VARCHAR(500) | Path to uploaded business logo (top-left on PDF) |
| bank_name | VARCHAR(255) | Bank name (shown on PDF for payment details) |
| account_name | VARCHAR(255) | Account holder name (shown on PDF) |
| account_number | VARCHAR(50) | Bank account number (shown on PDF) |
| bsb | VARCHAR(20) | Bank State Branch number (shown on PDF) |
| payment_terms | INTEGER | Default payment terms in days (default: 14) - shown on PDF and used to set due dates |
| created_at | TIMESTAMPTZ | Creation timestamp |
| updated_at | TIMESTAMPTZ | Last update timestamp |

#### account_categories
| Column | Type | Description |
|--------|------|-------------|
| id | UUID | Primary key |
| name | VARCHAR(100) | Account type name |
| description | TEXT | Account description |
| created_at | TIMESTAMPTZ | Creation timestamp |

#### expenses
| Column | Type | Description |
|--------|------|-------------|
| id | UUID | Primary key |
| user_id | UUID | Foreign key to users |
| account_category_id | UUID | Foreign key to account_categories |
| vendor_name | VARCHAR(255) | Vendor/supplier name |
| description | TEXT | Expense description |
| ex_gst_amount | NUMERIC(12,2) | Amount excluding GST |
| gst_amount | NUMERIC(12,2) | GST amount |
| gst_type | NUMERIC(3,1) | GST type (0 or 0.1) |
| total_amount | NUMERIC(12,2) | Total including GST |
| expense_date | DATE | Date of expense |
| attachment_path | VARCHAR(500) | Path to uploaded file |
| created_at | TIMESTAMPTZ | Creation timestamp |
| updated_at | TIMESTAMPTZ | Last update timestamp |

#### invoices
| Column | Type | Description |
|--------|------|-------------|
| id | UUID | Primary key |
| user_id | UUID | Foreign key to users |
| account_category_id | UUID | Foreign key to account_categories |
| customer_id | UUID | Foreign key to customers (required) |
| client_name | VARCHAR(255) | Client name (for display) |
| description | TEXT | Invoice description |
| ex_gst_amount | NUMERIC(12,2) | Amount excluding GST |
| gst_amount | NUMERIC(12,2) | GST amount |
| gst_type | NUMERIC(3,1) | GST type (0 or 0.1) |
| total_amount | NUMERIC(12,2) | Total including GST |
| invoice_date | DATE | Invoice date |
| due_date | DATE | Payment due date |
| attachment_path | VARCHAR(500) | Path to uploaded file |
| status | VARCHAR(20) | Invoice status: draft, sent, paid, overdue, cancelled (default: draft) |
| payment_date | DATE | Date payment was received |
| amount_paid | NUMERIC(12,2) | Amount that was paid (for partial payments) |
| sent_at | TIMESTAMPTZ | Timestamp when invoice was sent to customer |
| confirmed_received_at | TIMESTAMPTZ | Timestamp when customer confirmed receipt |
| paid_at | TIMESTAMPTZ | Timestamp when payment was received |
| created_at | TIMESTAMPTZ | Creation timestamp |
| updated_at | TIMESTAMPTZ | Last update timestamp |

#### customers
| Column | Type | Description |
|--------|------|-------------|
| id | UUID | Primary key |
| name | VARCHAR(255) | Customer name (required) |
| contact_name | VARCHAR(255) | Contact person name |
| address | TEXT | Customer address |
| contact_email | VARCHAR(255) | Contact email address |
| abn | VARCHAR(20) | Australian Business Number |
| contact_number | VARCHAR(50) | Phone/contact number |
| gst | BOOLEAN | Whether customer is GST registered (default: TRUE) |
| created_at | TIMESTAMPTZ | Creation timestamp |
| updated_at | TIMESTAMPTZ | Last update timestamp |

#### activity_logs
| Column | Type | Description |
|--------|------|-------------|
| id | UUID | Primary key |
| user_id | UUID | Foreign key to users (nullable for system) |
| action | VARCHAR(50) | CREATE, UPDATE, DELETE |
| table_name | VARCHAR(50) | Affected table |
| record_id | UUID | Affected record ID |
| old_values | JSONB | Previous values (for updates/deletes) |
| new_values | JSONB | New values (for creates/updates) |
| ip_address | VARCHAR(45) | Client IP address |
| created_at | TIMESTAMPTZ | Action timestamp |

#### ocr_queue
| Column | Type | Description |
|--------|------|-------------|
| id | UUID | Primary key |
| expense_id | UUID | Foreign key to expenses |
| image_path | VARCHAR(500) | Path to receipt image |
| status | VARCHAR(20) | pending, processing, completed, failed |
| extracted_data | JSONB | OCR extracted data |
| error_message | TEXT | Error message if failed |
| attempts | INTEGER | Number of processing attempts |
| created_at | TIMESTAMPTZ | Creation timestamp |
| processed_at | TIMESTAMPTZ | Processing completion timestamp |

## Business Rules

### GST Handling
- GST types: 0 (no GST), 0.1 (10% GST)
- All monetary columns use NUMERIC(12,2) to avoid rounding errors
- GST calculated as: `ex_gst_amount * gst_type`
- Total calculated as: `ex_gst_amount + gst_amount`

### Australian Financial Year
- Year runs July 1 to June 30
- Q1: July 1 - September 30
- Q2: October 1 - December 31
- Q3: January 1 - March 31
- Q4: April 1 - June 30

### Accounting Method
- Default: Accrual basis
- Income/expenses recorded when incurred, not when paid

## API Endpoints

### Authentication
- `POST /api/auth/register` - Register new user
- `POST /api/auth/login` - Login (returns JWT or session)
- `POST /api/auth/api-key` - Generate API key
- `GET /api/auth/business` - Get business profile (user details)
- `PUT /api/auth/business` - Update business profile (name, ABN, address, contact)
- `POST /api/auth/logo` - Upload business logo
- `DELETE /api/auth/logo` - Delete business logo

### Expenses
- `GET /api/expenses` - List expenses (with date filters)
- `POST /api/expenses` - Create expense
- `GET /api/expenses/<id>` - Get single expense
- `PUT /api/expenses/<id>` - Update expense
- `DELETE /api/expenses/<id>` - Delete expense
- `POST /api/expenses/<id>/upload` - Upload attachment

### Invoices
- `GET /api/invoices` - List invoices (with date filters)
- `POST /api/invoices` - Create invoice (requires customer_id)
- `GET /api/invoices/<id>` - Get single invoice
- `PUT /api/invoices/<id>` - Update invoice
- `DELETE /api/invoices/<id>` - Delete invoice
- `POST /api/invoices/<id>/upload` - Upload attachment
- `POST /api/invoices/<id>/mark-paid` - Mark invoice as paid (sets status, payment_date, amount_paid, paid_at)
- `POST /api/invoices/<id>/mark-sent` - Mark invoice as sent (sets status='sent', sent_at timestamp)
- `POST /api/invoices/<id>/mark-confirmed` - Mark invoice as confirmed received (sets confirmed_received_at timestamp)
- `GET /api/invoices/<id>/pdf` - Generate and download PDF of invoice

### Customers
- `GET /api/customers` - List customers
- `GET /api/customers/<id>` - Get single customer
- `POST /api/customers` - Create customer (name required)
- `PUT /api/customers/<id>` - Update customer
- `DELETE /api/customers/<id>` - Delete customer (fails if invoices exist)

### Account Categories
- `GET /api/account-categories` - List categories
- `POST /api/account-categories` - Create category
- `PUT /api/account-categories/<id>` - Update category
- `DELETE /api/account-categories/<id>` - Delete category

### Reports
- `GET /api/reports/profit-loss` - P&L report (params: start_date, end_date)
- `GET /api/reports/monthly` - Monthly P&L report (params: year, month)
- `GET /api/reports/quarterly-bas` - Quarterly BAS summary
- `GET /api/reports/yearly-finances` - Yearly P&L for July-June

### Activity Logs
- `GET /api/activity-logs` - List activity logs (admin only)

## HMI Routes (Browser Interface)

### Pages
- `/` - Dashboard
- `/expenses` - Expense list and management
- `/expenses/new` - Create expense
- `/expenses/<id>/edit` - Edit expense
- `/invoices` - Invoice list and management
- `/invoices/new` - Create invoice
- `/invoices/<id>/edit` - Edit invoice
- `/reports` - Report generation interface
- `/reports/monthly` - Monthly P&L report
- `/reports/quarterly-bas` - Quarterly BAS report
- `/reports/yearly-pnl` - Yearly P&L report
- `/account-categories` - Manage account categories
- `/login` - User login
- `/logout` - User logout
- `/api-key` - API key management (requires login)
- `/api-key/generate` - Generate new API key (POST, requires email verification)
- `/resend-verification` - Resend verification email (POST)
- `/verify/<token>` - Verify email with token
- `/activity-logs` - View activity logs

## PWA Routes

### Endpoints
- `/pwa` - PWA main page
- `/pwa/capture` - Camera capture interface (photo-only upload, no required fields)
- `/pwa/upload` - Process and upload receipt photo
- `/pwa/login` - PWA login
- `/pwa/logout` - PWA logout

### OCR Queue System
The PWA receipt upload creates an expense with placeholder values and queues the image for OCR processing. The OCR processor extracts:
- vendor_name
- expense_date
- ex_gst_amount
- gst_amount
- gst_type
- description

#### OCR Queue Routes (for PWA users)
- `/pwa/api-key` - View API key management page
- `/pwa/api-key/generate` - Generate new API key (requires email verification)
- `/pwa/resend-verification` - Resend verification email

Note: Users must verify email before accessing API key generation.

## File Storage
- Uploaded files stored in `uploads/` directory
- Subdirectories: `expenses/`, `invoices/`, `receipts/`
- Supported formats: JPEG, PNG, PDF
- Max file size: 10MB

## Configuration

### config.yaml (untracked)
```yaml
database:
  host: localhost
  port: 5432
  name: accounts
  user: postgres

app:
  host: "127.0.0.1"  # override via API_HOST env var for LAN binding
  api_port: 5061
  hmi_port: 5062
  secret_key: "change-me-in-production"
  upload_folder: "uploads"
  max_content_length: 10485760  # 10MB

# vision section removed 2026-08-04 — was a local Ollama-hosted gemma4:31b
# endpoint that has since been retired. OCR now uses local tesseract.

accounting:
  default_gst_type: 0.1
  financial_year_start_month: 7  # July
  financial_year_start_day: 1
  financial_year_end_month: 6  # June
  financial_year_end_day: 30
```

### .env (untracked)
```
DB_HOST=localhost
DB_PORT=5432
DB_NAME=accounts
DB_USER=postgres
DB_PASSWORD=<password>
TWELVE_DATA_API_KEY=<api_key>
```

## Security Considerations

1. API key authentication for agent access (requires email verification)
2. Password hashing with bcrypt
3. No credentials hardcoded - use environment variables and config files
4. Input validation on all endpoints
5. SQL injection prevention via parameterized queries (SQLAlchemy ORM)
6. CSRF protection on forms
7. File upload validation (type, size)
8. Email verification required before API key generation

## Email Verification Flow

1. User registers → verification_token generated and stored
2. User visits `/verify/<token>` → email_verified set to TRUE, token cleared
3. User can generate API key only after email verification
4. User can resend verification from `/api-key` page if not verified
5. Login to HMI does NOT require email verification

## Technology Stack

- **Language**: Python 3.10+
- **Virtual Environment**: `venv` (named "venv" in project root)
- **Web Framework**: Flask
- **ORM**: SQLAlchemy with Flask-SQLAlchemy
- **Database**: PostgreSQL
- **Templates**: Jinja2
- **File Uploads**: Flask-WTF / native Flask
- **Authentication**: Flask-Login for HMI, API keys for agents
- **Password Hashing**: bcrypt

## Project Structure
```
accounts/
├── .env                     # Database credentials (untracked)
├── .env.example            # Template for .env (tracked)
├── .gitignore
├── config.yaml             # Application config (untracked)
├── config.yaml.example     # Template for config (tracked)
├── AGENTS.md               # Agent documentation
├── app/
│   ├── __init__.py
│   ├── api/                # API server
│   │   ├── __init__.py
│   │   ├── routes.py
│   │   ├── auth.py
│   │   └── reports.py
│   ├── hmi/                # Browser HMI
│   │   ├── __init__.py
│   │   ├── routes.py
│   │   └── templates/
│   │       ├── base.html
│   │       ├── dashboard.html
│   │       ├── api_key.html
│   │       └── pwa/
│   ├── pwa/                # PWA
│   │   ├── __init__.py
│   │   └── routes.py
│   ├── models/            # SQLAlchemy models
│   │   ├── __init__.py
│   │   ├── user.py
│   │   ├── expense.py
│   │   ├── invoice.py
│   │   ├── customer.py
│   │   ├── account_category.py
│   │   ├── activity_log.py
│   │   └── ocr_queue.py
│   └── shared/            # Shared utilities
│       ├── __init__.py
│       ├── decorators.py
│       └── validators.py
├── processes/              # Background workers
│   └── ocr_processor.py    # OCR queue processor
├── skills/                 # Agent skills
│   └── accounting_skill.py
├── systemd/                # Systemd service files (tracked)
│   ├── accounts.service
│   └── ocr-processor.service
├── schema/                 # PostgreSQL schema files (tracked)
│   └── init.sql
├── venv/                   # Python virtual environment (untracked)
├── install.sh              # Installation script (tracked)
├── uploads/                # File uploads (untracked)
│   ├── expenses/
│   ├── invoices/
│   └── receipts/
├── requirements.txt
└── run.py                  # Application entry point
```

## Systemd Services

The application runs as systemd user services using the project `venv`.

### Main Application Service: `systemd/accounts.service`
```
[Unit]
Description=Python PostgreSQL Accounting System
After=postgresql.service network-online.target
Wants=postgresql.service network-online.target

[Service]
Type=simple
Environment="PATH=%h/accounts/venv/bin:/usr/local/bin:/usr/bin:/bin"
EnvironmentFile=-%h/.env   # secrets + API_HOST (optional)
WorkingDirectory=%h/accounts
ExecStartPre=/bin/sleep 5
ExecStart=%h/accounts/venv/bin/python3 run.py   # --host defaults to 127.0.0.1
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
```

### OCR Processor Service: `systemd/ocr-processor.service`
```
[Unit]
Description=OCR Queue Processor for Receipt Scanning
After=postgresql.service network-online.target
Wants=postgresql.service network-online.target

[Service]
Type=simple
Environment="PATH=%h/accounts/venv/bin:/usr/local/bin:/usr/bin:/bin"
WorkingDirectory=%h/accounts
ExecStart=%h/accounts/venv/bin/python3 %h/accounts/processes/ocr_processor.py --config %h/accounts/config.yaml --poll-interval 5
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
```

To install and manage services:
```bash
# Main app
systemctl --user enable accounts.service
systemctl --user start accounts.service
systemctl --user status accounts.service
journalctl --user -u accounts.service

# OCR processor
systemctl --user enable ocr-processor.service
systemctl --user start ocr-processor.service
systemctl --user status ocr-processor.service
journalctl --user -u ocr-processor.service
```

## PostgreSQL Schema

Database schema is stored in `schema/init.sql` for manual setup or automation.

## Installation

Run `install.sh` to:
1. Copy `.env.example` to `.env`
2. Copy `config.yaml.example` to `config.yaml`
3. Initialize the PostgreSQL schema
4. Install the systemd user service
5. Display reminder to modify configuration files
