"""Mapped CSV builder for AustralianSuper direct uploads.

When AustralianSuper's portal rejects a SAFF file with a generic
"invalid file" error, the next thing to try is their "Mapped CSV file"
feature. Per the AusSuper help docs (Sep 2026):

    > Mapped CSV file: If your payroll system does not produce a SAFF file,
    > you can use the File Mapper to map your file to the SuperStream
    > standard. Once mapped, the file can be uploaded directly to the
    > portal.

We emit a CSV with the human-readable column names that AusSuper's
File Mapper expects (matched against common AU payroll systems — Xero,
MYOB Exo, Employment Hero, KeyPay, etc.). Use this when the SAFF format
itself isn't accepted.

Column naming conventions (matched against MYOB Exo Payroll, Xero SAFF,
and similar AU payroll systems). The exact source-header names were
verified against the AusSuper File Mapper page (2026-09-17 screenshot).

Key insight: AusSuper's File Mapper auto-matches source columns to
destination fields by exact name. So our source columns should be NAMED
THE SAME as the destination fields they map to. The "Employee"/"Fund"
prefixes that Xero/MYOB use (`Employee First Name`, `Fund ABN`) don't
match what AusSuper's parser expects — it wants `Given Name`, `ABN`.

Destination field (AusSuper)        Source header we use (must match)
------------------------------------ -----------------------------
Family Name*                        "Family Name"
Given Name*                         "Given Name"
Sex Code*                           "Sex Code"
Birth Date*                         "Birth Date"
Address Details Line 1 Text*        "Address Details Line 1 Text"
Locality Name Text*                 "Locality Name Text"
State or Territory Code*            "State or Territory Code"
Postcode Text*                      "Postcode Text"
ABN (fund)*                         "ABN"              (AustralianSuper's ABN)
USI (fund)*                         "USI"              (AustralianSuper's USI)
Organisational Name Text*           "Organisational Name Text"
Fund ID (ABN/USI)*                  "Fund ID (ABN/USI)"
Superannuation Guarantee Amount*     "Superannuation Guarantee Amount"
Personal Contributions Amount*       "Personal Contributions Amount"
Salary Sacrificed Amount*           "Salary Sacrificed Amount"
Total*                              "Total"
Employer ID (employer's ABN)        "Employer ID"
Employee ID (member number)         "Employee ID"
# NOTE: "Employer ID" here is the EMPLOYER'S ABN, NOT the employer ID the
# fund assigns to you. AustralianSuper separately gives the employer an
# Employer ID (123456789) - that lives in the DEFAULT_FUND_EMPLOYER_ID
# setting and is for reconciliation only, never in this file. See
# skills/accounts/payroll.md for the full identifier map.
Pay Period Start Date               "Pay Period Start Date"
Pay Period End Date                 "Pay Period End Date"
Transaction Date                    "Transaction Date"
Payment Reference                  "Payment Reference"
Employer Additional                 "Employer Additional"  (discretionary top-ups)

Why this exists (2026-09-17): AusSuper's portal rejected the SAFF file
we generated with "invalid file" without detail. The Mapped CSV path is
their recommended fallback for non-native SAFF emitters. If the File
Mapper accepts this format, future exports can use it as a more
portable alternative to SAFF.
"""

import csv
import io
from datetime import date
from decimal import Decimal


def _get_setting(key):
    """Read a setting from the system_settings table.

    Imported lazily so the module can be imported in environments where
    the SQLAlchemy session isn't yet configured (e.g. raw CLI tools).
    """
    from app.models.system_setting import get_setting
    return get_setting(key)


MAPPED_COLUMNS = (
    # --- Employer ---
    'Employer ID',
    # --- Employee (all use destination-name spelling for auto-mapping) ---
    'Employee ID',
    'Family Name',
    'Given Name',
    'Employee TFNs',            # NOTE: plural — matches AusSuper's parser
    'Birth Date',
    'Sex Code',
    'Address Details Line 1 Text',
    'Locality Name Text',
    'State or Territory Code',
    'Postcode Text',
    # --- Fund ---
    'Fund Details / Organisational Name Text', # exact dropdown label from AusSuper
    'ABN',                      # AusSuper's ABN (the receiving fund)
    'USI',                      # AusSuper's USI
    'Fund ID (ABN/USI)',
    # --- Member contributions ---
    'Pay Period Start Date',
    'Pay Period End Date',
    'Transaction Date',
    'Superannuation Guarantee Amount',
    'Employer Additional',
    'Personal Contributions Amount',
    'Salary Sacrificed Amount',
    'Total',
    'Phone',                    # not a destination in AusSuper's mandatory list,
                               # but the file warns when it's missing. Including
                               # it as a column lets the warning clear without
                               # manual mapping.
    # --- Optional ---
    'Payment Reference',
)


def _format_amount(value):
    if value is None or value == '':
        return ''
    return str(Decimal(str(value)).quantize(Decimal('0.01')))


def _format_date_iso(d):
    if d is None or d == '':
        return ''
    if isinstance(d, str):
        return d
    return d.isoformat()


def _normalize_tfn(value):
    """Same as in saff_csv — bare 9 digits, no spaces/hyphens."""
    if value is None or value == '':
        return ''
    import re
    digits = re.sub(r'\D', '', str(value))
    if len(digits) != 9:
        return ''
    return digits


def build_mapped_csv(rows, employer_id=None):
    """Build an AustralianSuper-friendly Mapped CSV.

    Args:
      rows: iterable of dicts (same shape as build_saff_csv accepts).
            See app/shared/saff_csv.py for the full key list.
      employer_id: employer's ABN (the value that goes in the mandatory
                   'Employer ID' column). Required — we don't pull it from
                   a row field because it's the same for every row in the
                   file.

    Returns: CSV string (UTF-8 with BOM so Excel on Windows opens cleanly
             and the File Mapper doesn't choke on encoding).
    """
    from decimal import Decimal

    if not employer_id:
        raise ValueError(
            "employer_id is required (AusSuper's File Mapper needs an "
            "Employer ID — that's the employer's ABN)."
        )

    buf = io.StringIO()
    buf.write('\ufeff')  # UTF-8 BOM
    writer = csv.DictWriter(buf, fieldnames=list(MAPPED_COLUMNS), extrasaction='ignore')
    writer.writeheader()

    for row in rows:
        sgc = Decimal(str(row.get('sgc_amount', '0') or '0'))
        additional = Decimal(str(row.get('additional_amount', '0') or '0'))
        voluntary = Decimal(str(row.get('voluntary_amount', '0') or '0'))
        total = sgc + additional + voluntary

        writer.writerow({
            'Employer ID': employer_id,
            'Employee ID': row.get('member_number', ''),
            'Family Name': row.get('family_name', ''),
            'Given Name': row.get('given_name', ''),
            'Employee TFNs': _normalize_tfn(row.get('tfn')),
            'Birth Date': _format_date_iso(row.get('date_of_birth')),
            'Sex Code': row.get('sex', ''),
            'Address Details Line 1 Text': row.get('address_line1', ''),
            'Locality Name Text': row.get('city', ''),
            'State or Territory Code': row.get('state', ''),
            'Postcode Text': row.get('postcode', ''),
            # Fund identifiers — read from system_settings table (see
            # app/models/system_setting.py). Caller can override per-row
            # via the `fund_name` row dict key, but ABN/USI are sourced
            # globally so they're consistent across all employees
            # contributing to the same fund.
            'Fund Details / Organisational Name Text': (
                row.get('fund_name') or _get_setting('DEFAULT_FUND_NAME')
            ),
            'ABN': _get_setting('DEFAULT_FUND_ABN'),
            'USI': _get_setting('DEFAULT_FUND_USI'),
            'Fund ID (ABN/USI)': _get_setting('DEFAULT_FUND_USI'),
            'Pay Period Start Date': _format_date_iso(row.get('pay_period_start')),
            'Pay Period End Date': _format_date_iso(row.get('pay_period_end')),
            'Transaction Date': _format_date_iso(row.get('transaction_date')),
            'Superannuation Guarantee Amount': _format_amount(sgc),
            'Employer Additional': _format_amount(additional),
            'Personal Contributions Amount': '0.00',
            'Salary Sacrificed Amount': _format_amount(voluntary),
            'Total': _format_amount(total),
            'Phone': row.get('phone', ''),
            'Payment Reference': row.get('payment_reference', ''),
        })

    return buf.getvalue()
