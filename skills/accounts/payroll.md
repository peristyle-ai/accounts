# Payroll

Per-business employee master + pay events (the actual payslips) + superannuation remittances + summary/dashboard endpoints. PII fields (TFN, bank BSB, bank account number) are encrypted at rest via Fernet (`PAYROLL_PII_KEY` in `.env`).

## High-level model

```
Employee (master record)
   |
   |---< PayEvent (one per payslip issued)
   |          |
   |          |---< PayEventLine (itemised earnings/deductions)
   |          |
   |          |---< PayslipDelivery (audit trail of every email sent)
   |
   |---< SuperPayment (one per actual fund remittance, covering one+ PayEvents)
```

## Resources

| Resource | Description |
|---|---|
| Employee | Master record per worker. PII-encrypted. Status: `active` \| `terminated`. Also carries STP reporting fields (`tax_treatment_code`, `basis_of_employment_code`, `country_code`, `payroll_number`, `hired_date`, `termination_date`, `termination_type`, `middle_name`, `address_2`, `previous_employee_id`, `previous_bms_id`) for Single Touch Payroll export. |
| PayEvent | One per payslip. Has headline amounts (gross, net, PAYG) + itemised lines. Status: `draft` \| `finalized` \| `paid` \| `cancelled`. |
| PayEventLine | Itemised breakdown of a PayEvent (earnings, tax, deductions, allowances, employer contributions). |
| PayslipDelivery | Audit row per recipient per send (one PayEvent can have multiple deliveries — work email + personal email + cc to manager). |
| SuperPayment | One per actual fund remittance. Covers one or more PayEvents. Status: `pending` \| `paid` \| `reconciled`. |

## Status flows

```
Employee:        active ----terminate()----> terminated

PayEvent:        draft ----finalize()----> finalized ----mark_paid()----> paid
                       \---cancel()------> cancelled

SuperPayment:    pending ----mark_paid()----> paid ----reconcile()----> reconciled
```

## API endpoints

### Employees (6)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/payroll/employees?status=active` | List (filter by status) |
| POST | `/api/payroll/employees` | Create |
| GET | `/api/payroll/employees/<id>` | Get one (PII fields decrypted for caller) |
| PUT | `/api/payroll/employees/<id>` | Update |
| POST | `/api/payroll/employees/<id>/terminate` | Set `employment_status='terminated'` + `end_date` |
| DELETE | `/api/payroll/employees/<id>` | Delete. **Only an `active` employee with zero pay events** — anything else is `409`. A terminated employee, or one with pay-event history, is kept rather than deleted. |

### Pay Events (11)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/payroll/pay-events?employee_id=&start_date=&end_date=&status=` | List |
| POST | `/api/payroll/pay-events` | Create draft |
| GET | `/api/payroll/pay-events/<id>` | Get one (with lines) |
| PUT | `/api/payroll/pay-events/<id>` | Update (only when status=draft) |
| POST | `/api/payroll/pay-events/<id>/finalize` | Lock amounts (draft → finalized) |
| POST | `/api/payroll/pay-events/<id>/mark-paid` | Mark as paid. Optional body: `payment_reference` (str, max 100) = bank-side confirmation reference for the wage transfer (e.g. ING Osko `Confirmation ID H6051146167`); `notes` (str); `super_paid_amount` (Decimal); `super_paid_date` (YYYY-MM-DD). **Idempotent and re-callable**: the endpoint commits whenever ANY field changed, not only on the status transition, so it can be used to attach a bank confirmation to an event that was already marked paid (bug fixed 2026-10-22, commit 302cd81 — previously the commit was inside `if pe.status != 'paid'`, so updates to an already-paid event were echoed in the response but never written to the DB). `payment_reference` is distinct from `bank_reference`: the latter is the reference shown on the payee's statement. |
| POST | `/api/payroll/pay-events/<id>/cancel` | Cancel (any status except cancelled) |
| GET | `/api/payroll/pay-events/<id>/pdf` | Download payslip PDF |
| POST | `/api/payroll/pay-events/<id>/generate-pdf` | Generate the payslip PDF without sending it. |
| DELETE | `/api/payroll/pay-events/<id>` | Delete. **Draft only** — a finalized, paid or cancelled event is `409`. |
| POST | `/api/payroll/pay-events/<id>/reopen` | Reopen a **finalized** event back to draft so the amounts can be corrected (added 2026-10-22). Finalizing locks the amounts, and without this there was no correction path except hand-editing the database. A **paid** event cannot be reopened — it records that money moved. |
| POST | `/api/payroll/pay-events/<id>/send-payslip` | Email the PDF + record PayslipDelivery. Optional body: `force` (bool) to override the 5-minute idempotency window. **Requires a working mail config** (see "Mail server config" below). A missing or placeholder config is **503**; a delivery that fails is **502**. A send that fails is still recorded in `payslip_deliveries` with `delivery_status='failed'`. (Before 2026-10-25 a failed send still answered 200 — that silent-success behaviour is gone.) |

### Super Payments (6)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/payroll/super-payments` | List |
| POST | `/api/payroll/super-payments` | Create (remittance record). Required body: `employee_id` (UUID), `pay_event_ids` (list of UUID), `amount` (Decimal), `remittance_date` (YYYY-MM-DD), `payment_reference` (str). Optional: `fund_name_snapshot`, `fund_member_snapshot` (auto-snapshotted from the employee record if omitted), `clearing_house` (str), `note` (str). The API snapshots fund name + member number at create time so historical payments stay accurate even if the employee later changes funds. |
| GET | `/api/payroll/super-payments/<id>` | Get one |
| POST | `/api/payroll/super-payments/<id>/mark-paid` | pending → paid. Body: `paid_date` (YYYY-MM-DD), `payment_reference` (str). **Cascades to update `pay_event.super_paid_date` and `pay_event.super_paid_amount` on every pay_event_id linked to this super_payment** (verified 2026-10-18 on the 14 Oct Sample Employee event — super_paid_date went null → 2026-10-18, super_paid_amount went null → 228.00). |
| POST | `/api/payroll/super-payments/<id>/reconcile` | paid → reconciled |
| PUT | `/api/payroll/super-payments/<id>` | Correct a **pending** super payment. Body: any of `fund_member_snapshot`, `fund_name_snapshot`, `notes`, `remittance_date`, `amount`. Added 2026-10-22. `fund_member_snapshot` is captured when the row is created, so a pending row created before the employee's fund member number was corrected still carries the old value. **Paid and reconciled rows are immutable** — they record what was actually remitted to the fund, so editing them would falsify the audit trail; the endpoint returns 409 for those. Added because there was no way to correct a pending row other than direct SQL, which the accounts-repo rule forbids. |

### Exports (4)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/exports/payroll/aba` | Wage ABA (Direct Entry file) for the employee's bank. Query params as for `/super-csv` plus optional `process_date` (the date the bank should release the payments). `400` if the payer has no bank details configured. See "Wage ABA" below. |
| GET | `/api/exports/payroll/super-csv` | Superannuation contribution CSV. Query params: `status` (csv, default `finalized`), `from_date`/`to_date` (YYYY-MM-DD, inclusive), `process_date`. `404`/error if no pay events match. The filename is `peristyle-super-<period>.csv`. |
| GET | `/api/exports/payroll/preview` | JSON summary of what the export endpoints would produce (counts and totals) without generating a file. Same filters as the exports, minus pay events already covered by a SuperPayment. |
| GET | `/api/exports/payroll/one-touch-payroll?[from_date=YYYY-MM-DD][&to_date=YYYY-MM-DD][&employee_id=<uuid>][&status=paid,finalized]` | Single Touch Payroll (STP) Phase 1 pay-event CSV for upload to https://portal.singletouch.com.au/Support/Csv2020. **One CSV per pay run** — the portal rejects a file whose `Pay or update date` differs between records ("Inconsistent values found for 'Pay or update date'. This value must be the same for every record."), so a pay event submission covers exactly one payment date. Query params: `from_date`/`to_date` (inclusive, filter on `pay_event.payment_date`), optional `employee_id` (filter to one worker), optional `status` (default `paid,finalized` — drafts excluded). **Status codes:** `404` no pay events matched the filters (wrong range/status, or the data belongs to another user — the endpoint is user-scoped); `400` events matched but none had usable employee details (missing TFN), with a per-event `skipped` list giving the reason; `400` on a malformed `from_date`/`to_date` or an unrecognised `status`; `500` if a generated file would fail the portal's consistency check. A `404` here is a filter outcome, not a dead route. Response: a range covering ONE pay run returns that file as `text/csv` (no BOM, CRLF line endings, 93 columns, filename `stp-payrun-YYYY-MM-DD.csv`); a range covering MULTIPLE pay runs returns a ZIP containing one CSV per pay run (`stp-payruns-<from>_to_<to>.zip`). One row per pay event. Employer constants come from `config.yaml` (`stp.entity_abn`, `stp.bms_id`, `stp.product_id`, `stp.branch_id`, `stp.payrun_time`, `stp.income_stream_code`); per-employee fields from `accounts.employees` (`tax_treatment_code`, `basis_of_employment_code`, `country_code`, `payroll_number`, `hired_date`, `middle_name`, `address_2`, `termination_date`, `termination_type`, added in migration `migrate_employees_stp_fields.sql` 2026-10-18). Bulk generation without the API: `scripts/gen_stp_payrun_csvs.py` (writes one CSV per pay run and reports any file that would fail the portal's consistency check). **Email wrapper:** `scripts/singletouch_export.py` downloads the CSV(s) from this endpoint and emails them to the operator for review — the recipient comes from `PAYROLL_FILES_TO` in `.env` (or `--recipient` on the command line). There is no built-in default address; if neither is set the script refuses to run rather than mailing a placeholder. It is a thin wrapper — it does NOT rebuild the CSV, so the file that lands in the inbox is byte-identical to the live endpoint output. Handles both response shapes (a single pay run returns `text/csv`; multiple pay runs return a ZIP, which it unpacks and attaches as SEPARATE attachments, because the portal requires one pay date per file). Read-only against the API — it changes no pay event status and uploads nothing to the portal (the portal upload stays a manual human step). |

### AusSuper mapped CSV (AustralianSuper contribution file)

The 25-column `aussuper-mapped-*.csv` file uploaded to AustralianSuper's
contribution portal carries the fund member number in its **`Employee ID`**
column, and that value is read from `accounts.employees.super_fund_member_no`
at build time — not from the super payment's snapshot. So a correction to the
employee record propagates into every newly generated file automatically.

#### What the member number means (researched 2026-10-22)

AustralianSuper's **member number** is the identifier AustralianSuper assigns
to *Sample Employee*, as opposed to the USI (`STA0100AU`) which identifies the *fund*
and the ABN (`65714394898`) which identifies the fund's legal entity. It is
the key that routes a contribution to her specific account rather than to the
fund generally.

Per AustralianSuper's own published guidance (Payday Super response-code
table and the "How SuperStream works" ATO page):

* `SUPER.GEN.CNTRBTN.1` — *"Member registration request message was
  successfully processed. If there is a member number supplied with this
  message, ensure you update the member number into the payroll system."*
  This is the mechanism that closes the loop: the fund returns the member
  number back to the employer when it is missing or wrong, and the employer
  must write it into their payroll system. the employee's real number
  (`123456789`) was read off her super app on 2026-10-22 and must now be
  what we lodge.
* `SUPER.GEN.CNTRBTN.5` — *"Member TFN required for this Contribution Type."*
  The TFN is mandatory; the member number identifies the account.
* AusSuper's data-quality guidance is explicit that where an optional field
  cannot be completed accurately, **leave it blank rather than using
  placeholder information**. A wrong member number is therefore worse than
  none: it can create or mis-target a member record.

**Consequence for us.** The placeholder `123456789` that was lodged in every
file before 2026-10-22 was accepted by the portal but was not the employee's
account. Whether those earlier contributions landed against her real
AustralianSuper account (via TFN matching) or against a placeholder-keyed
member record needs confirming with AustralianSuper.

#### Employer ID vs Employer ABN — two different numbers

AustralianSuper assigns the *employer* its own **Employer ID** (here:
`123456789`). AusSuper's guidance: *"we also recommend keeping a record of
any employer IDs that are provided by the funds that you're paying into, as
these can often be needed when investigating a payment or a contribution
issue."* That is now stored as the `DEFAULT_FUND_EMPLOYER_ID` system setting.

Note the two are **not** interchangeable:

| Value | Meaning | Where it goes |
|---|---|---|
| `53000000022` | the employer's **ABN** | `Employer ID` column of the mapped CSV (the file mapper wants the ABN) |
| `123456789` | the employer's **AustralianSuper Employer ID** | `DEFAULT_FUND_EMPLOYER_ID` setting — for reconciliation and fund correspondence |
| `123456789` | the employee's **member number** | `Employee ID` column of the mapped CSV |
| `65714394898` | AustralianSuper's own **ABN** (the fund) | `ABN` column |
| `STA0100AU` | AustralianSuper's **USI** (the fund) | `USI` / `Fund ID (ABN/USI)` columns |

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/exports/payroll/super-csv?[from_date=][&to_date=][&status=]` | Simpler 13-column AusSuper CSV (`MemberNumber,FirstName,LastName,DateOfBirth,PayPeriodStart,...`). **Default `status=finalized`** — the weekly flow generates the file in the `prepare` phase, while the pay event is finalised but not yet paid, so a week that has already been marked paid needs `&status=paid` to appear. |
| — | `scripts/regen_aussuper_csvs.py` | Rebuild every pay event's `aussuper-mapped-*.csv` from the database, plus an optional combined FY file (`--fy 2026`). Use this after changing an employee's fund member number: previously generated files keep the old value and are stale. Filenames follow the current convention — `aussuper-mapped-<ddmmmyyyy>.csv` (day zero-padded) and `aussuper-mapped-FY<yyyy-yy>-<GivenName>.csv` for the FY file. |

**Filename history.** Files produced before 2026-10-18 by the retired
`build_aussuper_mapped_csv_for_event.py` used an unpadded day
(`aussuper-mapped-1oct2026.csv`); the current script zero-pads
(`aussuper-mapped-01oct2026.csv`). Stale files from the old convention were
moved to `payroll/superseded/` on 2026-10-22 rather than deleted.


### Summary (5)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/payroll/summary` | Top-level dashboard |
| GET | `/api/payroll/summary/ytd` | Year-to-date (Australian FY) |
| GET | `/api/payroll/summary/super-owing` | Outstanding super per employee |
| GET | `/api/payroll/summary/payg-withheld` | PAYG totals (for BAS) |
| GET | `/api/payroll/summary/recent-deliveries?limit=20` | Recent payslip email audit |

## Common workflows

### Weekly payroll run example

A typical weekly payroll run for a full-time Australian employee under the National Employment Standards (NES):

```python
from datetime import date, timedelta
from decimal import Decimal
from skills.accounts.accounting_skill import AccountingSkill

skill = AccountingSkill(api_key="...")

# 1. Look up the employee
employee = skill.get_employee(employee_id="<your-employee-uuid>")
legal_name = employee['legal_name']

# 2. Compute the period (Mon to Sun, paid the following Monday)
today = date.today()
last_monday = today - timedelta(days=today.weekday() + 7)  # previous Monday
last_sunday = last_monday + timedelta(days=6)              # previous Sunday
payment_date = last_monday + timedelta(days=7)              # this Monday

# 3. Rate + hours config (substitute your own)
hourly_rate = 50.00
standard_week_hours = 38
gross = Decimal(hourly_rate) * Decimal(standard_week_hours)   # 1900.00
# tax withheld (compute via PAYG tables — out of scope here)
payg = compute_payg_tax(gross)
net = gross - payg
super_rate = Decimal('0.12')   # 12% OTE
super_payable = (gross * super_rate).quantize(Decimal('0.01'))  # 228.00

# 4. Create draft pay event
pay_event = skill.create_pay_event(
    employee_id=employee['id'],
    payment_date=payment_date.isoformat(),
    pay_period_start=last_monday.isoformat(),
    pay_period_end=last_sunday.isoformat(),
    gross_amount=float(gross),
    net_amount=float(net),
    payg_tax_amount=float(payg),
    pay_frequency='weekly',
    super_ote_amount=float(gross),
    super_payable_amount=float(super_payable),
    bank_reference=f"EMP {last_monday.strftime('%Y%m%d')}",
    lines=[
        {
            'line_type': 'ordinary_hours',
            'description': 'Ordinary hours (38h @ $50/h)',
            'quantity': 38, 'rate': 50.00,
            'amount': float(gross), 'is_taxable': True, 'sort_order': 1,
        },
        {
            'line_type': 'tax',
            'description': 'PAYG tax withheld',
            'amount': float(payg), 'is_taxable': False, 'sort_order': 2,
        },
        {
            'line_type': 'net_pay',
            'description': 'Net pay',
            'amount': float(net), 'is_taxable': False, 'sort_order': 3,
        },
        {
            'line_type': 'employer_super',
            'description': 'Super 12% OTE',
            'amount': float(super_payable), 'is_taxable': False, 'sort_order': 4,
        },
    ],
)

# 5. Finalize (locks the amounts)
finalized = skill.finalize_pay_event(pay_event_id=pay_event['id'])

# 6. Generate PDF
skill.generate_payslip_pdf(
    pay_event_id=finalized['id'],
    save_path=f"./payslips/{employee['id'][:8]}_{payment_date.isoformat()}.pdf",
)

# 7. Send to employee (defaults to email_work, 'work' recipient_kind)
delivery = skill.send_payslip(pay_event_id=finalized['id'])

# 8. Mark paid (after the bank debit clears)
paid = skill.mark_pay_event_paid(pay_event_id=finalized['id'])

# 9. (Optional) Record the super remittance when you actually pay the fund
# ... usually done weekly or quarterly depending on the fund
```

### Quarterly super reconciliation

```python
# At end of quarter, find what's owing
owing = skill.super_owing()
for emp_id, data in owing['employees'].items():
    print(f"{data['employee_name']}: ${data['total_owing']} "
          f"({len(data['unremitted_pay_events'])} unremitted pay events)")

# Group unremitted pay events by employee and create a SuperPayment per fund
for emp in owing['employees'].values():
    skill.create_super_payment(
        employee_id=emp['employee_id'],
        remittance_date='2026-09-28',
        amount=emp['total_owing'],
        pay_event_ids=[pe['id'] for pe in emp['unremitted_pay_events']],
        fund_name_snapshot=emp['super_fund_name'],     # snapshot at remittance
        fund_member_snapshot=emp['super_fund_member_no'],
        payment_reference=f"Q4-{emp['employee_id'][:8]}",
    )

# After the super fund clears the payment, mark each as paid then reconciled
for sp in skill.list_super_payments(status='pending')['super_payments']:
    skill.mark_super_paid(sp_id=sp['id'])
    # ... when the quarterly statement arrives from the fund:
    skill.reconcile_super(sp_id=sp['id'])
```

### Find payslip email delivery failures

```python
recent = skill.recent_payslip_deliveries(limit=50)
failures = [d for d in recent if d.get('delivery_status') == 'failed']
for f in failures:
    print(f"Pay event {f['pay_event_id'][:8]}: {f['recipient_email']} - {f.get('error_message')}")
```

### Terminate an employee

```python
skill.terminate_employee(
    employee_id="...",
    end_date="2026-09-30",
    reason="Resigned - moving to NSW",
)
# Sets employment_status='terminated' and end_date. Existing pay events stay.
```

## PII handling

PII columns (tfn, bank_bsb, bank_account_number) are encrypted via Fernet in `app/shared/pii.py`. The encryption key is `PAYROLL_PII_KEY` in `.env` (must be set or the model refuses to load — see `raise RuntimeError` in pii.py).

The API decrypts PII on GET responses (so callers see plaintext bank details etc.). When you PUT back the same value, it's re-encrypted with the current key. **Never log or store PII in plaintext outside the API.**

## Locked fields

`PayEvent` headline amounts (gross, net, payg, super_ote, super_payable) are **immutable after finalize**. To change a finalized pay event, cancel it and create a new one. Lines are similarly locked.

This is enforced at the API layer — PUT returns 400 if you try to mutate a finalized event.

## PayEvent line types

| line_type | Use |
|---|---|
| `ordinary_hours` | Standard hours (e.g. 38h/week for AU NES) |
| `overtime_hours` | Overtime at 1.5x or 2x |
| `leave_loading` | Annual leave loading (17.5% in AU) |
| `allowance` | Carers, mobile, uniform, etc. |
| `bonus` | Discretionary |
| `tax` | PAYG withholding (negative amount on net pay calc) |
| `deduction` | Salary sacrifice, child support garnishment |
| `employer_super` | Super contribution from employer |
| `net_pay` | Final take-home (informational; the actual net_amount headline is the truth) |

`is_taxable=False` lines don't count toward super OTE.

## PAYG tax computation

The skill doesn't compute PAYG tax — that's a separate concern (PAYG withholding schedules vary by residency, tax-free threshold declaration, STSL/HECS debt, etc.). The payroll pipeline expects you to compute `payg_tax_amount` externally and pass it in.

PAYG tax must be computed externally using the ATO withholding schedules (residents, HELP/HECS debt, tax-free threshold declaration, etc.). The skill expects you to pass in the computed `payg_tax_amount`.

## Mail server config (payslip sending)

`POST /api/payroll/pay-events/<id>/send-payslip` talks to the mail server over
HTTP. The settings live in the accounts repo `.env` (gitignored):

```
MAIL_SERVER_API=https://mail.example.com:5003
MAIL_SERVER_USER=<mailbox login, e.g. payroll@yourcompany.com.au>
MAIL_SERVER_PASSWORD=<password for that mailbox>
MAIL_SERVER_CA_BUNDLE=/opt/accounts/workspace/ssl/py_pg_email.crt
```

The settings are resolved once, up front, by `app/shared/mail_config.py`.
**There is no default host.** An unset, incomplete or still-placeholder
configuration is a CONFIGURATION error and is answered **HTTP 503** with a
message naming the variable to set -- no delivery row is created, because no
delivery was attempted.

Gotchas learned 2026-10-22 — **historical**. These caused every payslip
send to fail with "Connection refused" **while the API still returned HTTP
200**. That silent-success behaviour was removed on 2026-10-25: a failed send
is now **502** and an unusable configuration is **503** (see the response
contract below). The underlying facts below still hold:

- The mail server binds to the host you configure, **not** `127.0.0.1:5003`
  by default. Set `MAIL_SERVER_API` to its LAN address or hostname.
- It has been **HTTPS-only since 2026-09-13** with a self-signed cert, so
  `requests` needs an explicit `verify=` pointing at `MAIL_SERVER_CA_BUNDLE`.
  The endpoint passes `verify` on both the `/auth/login` and
  `/api/emails/mime` calls; if `MAIL_SERVER_CA_BUNDLE` is unset it falls back
  to the system trust store (correct for a public-CA endpoint).
- `MAIL_SERVER_USER` and `MAIL_SERVER_PASSWORD` must both be set, otherwise
  the auth step is skipped and the POST comes back 401 "Token is missing".
- A configuration still pointing at a documentation domain
  (`example.com`, `example.org`, `example.net`, `.test`, `.example`,
  `.invalid` -- all reserved by RFC 2606/6761) is REFUSED. Such a host can
  never be a real mail server, and dialling it used to surface as an opaque
  per-recipient delivery error rather than a configuration error.

**Response contract (changed 2026-10-25):**

| Outcome | HTTP | Body |
|---|---|---|
| All deliveries sent | 200 | `sent_count`, `failed_count=0`, `message` |
| Some or all deliveries failed | **502** | `sent_count`, `failed_count`, `errors[]` |
| Mail server not configured | **503** | `error` naming the variable to set |

A failed send is recorded in `payslip_deliveries` with
`delivery_status='failed'` and the error message. **Check
`deliveries[].delivery_status` as well as the HTTP status** -- the counts in
the body tell you what actually happened. Verify at the mail server with
`GET /api/emails/<id>/attachments` (returns the PDF with its file size).

## Related

- See `SKILL.md` for general skill setup
- `get_payg_withheld()` in the skill returns PAYG totals for BAS reporting — feed into `BAS.md` workflow
- See `bank-transactions.md` for recording the bank debit that settles the super payment
### STP email wrapper (`scripts/singletouch_export.py`)

Fetches the STP CSV(s) for a date range from
`GET /api/exports/payroll/one-touch-payroll` and emails them to the operator for
review / portal upload.

```bash
# From the accounts repo
python3 scripts/singletouch_export.py --from-date 2026-06-01 --to-date 2026-06-30
# Single pay run (returns text/csv, no ZIP)
python3 scripts/singletouch_export.py --from-date 2026-06-22 --to-date 2026-06-22
# Fetch + save but do not send
python3 scripts/singletouch_export.py --from-date 2026-06-01 --to-date 2026-06-30 --dry-run
```

Options: `--from-date` / `--to-date` (YYYY-MM-DD, inclusive), `--employee-id`
(filter to one worker), `--recipient` (default: `PAYROLL_FILES_TO` from `.env`; there is no built-in default address),
`--out-dir` (default the operating agent's workspace `payroll/stp/`), `--env` (repeatable;
defaults to the operating agent's workspace `.env` then this repo's `.env`), `--dry-run`.

The operating agent's workspace copy of `scripts/singletouch_export.py` is a thin
shim that delegates here, because the workspace `pie/` tree is excluded by the
harness `.gitignore` and would have no git history to roll back to.

Gotchas encoded in the script:
- The accounts API call uses the **system** trust store (`accounts.example.com`
  is a public Cloudflare origin); only the mail server needs the custom CA
  (the workspace's `ssl/py_pg_email.crt`) because its cert is self-signed. Using the
  mail CA for both breaks the accounts call with `CERTIFICATE_VERIFY_FAILED`.
- Cloudflare blocks Python's default urllib User-Agent (403, code 1010) — a
  browser UA plus the `X-API-Key` header is required.
- The mail server's `/api/emails/mime` rejects any byte >= 0x80, so the body
  and subject are ASCII-only. The CSV rides as base64, which is fine.
- A 404 from the export endpoint usually means **user scoping**, not a broken
  route: pay events belong to a `user_id`, so the API key must be the one for
  the user who owns them.

- See `Exports` section above for the Single Touch Payroll (`/api/exports/payroll/one-touch-payroll`) CSV export — uploaded to https://portal.singletouch.com.au/Support/Csv2020
- STP files are **one CSV per pay run** (the portal requires `Pay or update date` to be identical across every record). A multi-run range returns a ZIP of per-run CSVs. `app/shared/stp_csv.py` exposes `build_stp_csvs_by_payrun()` and `payrun_consistency_errors()` (the guard that checks every file-level column is identical across records before a file is served).

### The weekly run (`scripts/run_weekly_payroll.py`)

The end-to-end run for Sample Employee. Three phases:

| Phase | What it does |
|---|---|
| `--phase prepare` | Creates (or reuses) the pay event for the week that ended the previous Monday, finalizes it, generates **every upload file** -- the wage ABA, the super ABA, the AustralianSuper mapped CSV and the STP CSV -- and emails them to the operator. Does **NOT** mark the pay event paid and does **NOT** email the payslip. |
| `--phase complete` | Marks the prepared pay event paid (with `--payment-reference`) and emails the payslip. Run once the bank transfer has cleared. |
| `--phase full` | The original single-shot behaviour (default). |

The split exists so a **timer can never mark a week paid that was not**. The
`weekly-payroll.timer` (Tue 07:00 AEST) runs `prepare` only.

```bash
# Prepare next week's files and email them
python3 scripts/run_weekly_payroll.py --phase prepare --email-files-to you@your-company.com

# Once the transfer has cleared
python3 scripts/run_weekly_payroll.py --phase complete --payment-reference H6051146167

# Explicit period (otherwise inferred from today's date)
python3 scripts/run_weekly_payroll.py --phase prepare \
    --period-start 2026-10-20 --period-end 2026-10-26 --payment-date 2026-10-28
```

Other options: `--days` / `--rate` / `--net` (the standard full week is 5 days
at $50/hr, net $1,508.13), `--bank-ref`, `--skip-mark-paid`,
`--skip-payslip-email`, `--payslip-recipient-kinds` (overrides the
`PAYSLIP_RECIPIENT_MODES` setting for one call),
`--aba-process-date` (the ABA "date to be processed").

Idempotent: re-running for the same week reuses the existing pay event and
the existing SuperPayment rather than creating duplicates.

### Wage ABA

The Direct Entry file that pays the employee. Two artefacts, two names:

| Produced by | Filename |
|---|---|
| `scripts/run_weekly_payroll.py` | `peristyle-wage-<ddmmmyyyy>.aba` |
| `GET /api/exports/payroll/aba` | `peristyle-payroll-<period>.aba` |

The weekly script fetches the file from the endpoint (it does not rebuild it
locally), so the bytes uploaded to the bank are the same ones the endpoint
serves. The `peristyle-` prefix is the product's brand, not part of the ABA
format — it is a filename and can be renamed freely as long as the bank
gets a file with the right contents.

The lodgement reference is the pay event's own `bank_reference`
(e.g. `20260101XX`) -- the same string that appears on the payee's statement,
so an ABA-uploaded payment reconciles with a manual transfer. Older pay
events without a `bank_reference` fall back to
`bank_reference_prefix` + period-end in DDMMYY (e.g. `JP 191026`).

The payer details come from `accounts.user_payment_settings` (business bank
account + NAB Direct Entry Credit User ID). The payee details come from the
employee record's encrypted bank columns.

### Payslip recipients

`POST /api/payroll/pay-events/<id>/send-payslip` sends to `email_work` and
`email_personal` by default. The `PAYSLIP_RECIPIENT_MODES` system setting
narrows that at runtime:

```bash
curl -X PUT https://accounts.example.com/api/settings/PAYSLIP_RECIPIENT_MODES \
     -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
     -d '{"value":"personal"}'
```

Values: `work,personal` (default) | `work` | `personal`. Currently `personal`
-- Sample Employee's payslips go to her personal Gmail only, not the
business's records copy. A per-call override is possible with a POST body
of `{"recipient_kinds": ["personal"]}`.

### Correcting a prepared week

The prepare phase finalizes the pay event so the export endpoints (which
skip drafts) will include it. Finalizing locks the amounts. If the prepared
week turns out to have different hours:

```bash
# finalized -> draft, then edit, then re-finalize
curl -X POST https://accounts.example.com/api/payroll/pay-events/<id>/reopen ...
```

Only a `finalized` event can be reopened. A **paid** event cannot -- it is a
statement that money moved, so it stays put.

### Mail server client (`scripts/mailclient.py`)

Shared by `run_weekly_payroll.py` and `singletouch_export.py`. Encodes the
hard-won facts: HTTPS-only with a self-signed cert (CA at
the workspace's `ssl/py_pg_email.crt`), `/api/emails/mime` needs a Bearer token from
`/auth/login`, the endpoint rejects any byte >= 0x80 in the submitted MIME
(body and subject must be ASCII), and Cloudflare 403s Python's default
urllib UA with code 1010.
