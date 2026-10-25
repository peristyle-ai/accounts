"""AustralianSuper employer contribution CSV builder.

AustralianSuper's Employer Portal accepts a CSV upload with one row per
employee per pay period. The exact column layout isn't formally documented
in a public spec (it's published in their employer portal help), but the
data fields align with the ATO SuperStream data standards plus their own
contribution columns.

Columns we emit (one row per employee per pay event):

    MemberNumber           AustralianSuper member number (employee.super_fund_member_no)
    FirstName              Employee given name (preferred_name or first word of legal_name)
    LastName               Employee surname
    DateOfBirth            YYYY-MM-DD (ISO format — what the portal accepts)
    PayPeriodStart         YYYY-MM-DD
    PayPeriodEnd           YYYY-MM-DD
    PaymentDate            YYYY-MM-DD
    OrdinaryTimeEarnings   Decimal dollars (matches pay_event.super_ote_amount)
    SuperGuaranteeAmount   Decimal dollars (employer SGC for the period)
    SalarySacrifice        Decimal dollars (employee extra; 0 unless we add it later)
    TotalContribution      = SuperGuaranteeAmount + SalarySacrifice
    FundName               defaults to system_settings.DEFAULT_FUND_NAME

Note: the actual portal may want slightly different headers depending on the
fund/portal version. If the operator's accountant finds the upload is rejected
because of a header mismatch, the fix is to update COLUMN_ORDER / headers
in this file. The data mapping is stable.

Why CSV and not Excel XLSX: the portal accepts both, but CSV is simpler
to debug and round-trip. XLSX is a future enhancement.

History: added 2026-09-17 per the operator's request for an exportable
contribution file that the accountant can upload to AustralianSuper's
employer portal.
"""

import csv
import io
from decimal import Decimal


# Column order — must match what AustralianSuper's portal expects. If you
# tweak this list, the writer below picks up the new layout automatically.
COLUMN_ORDER = (
    'MemberNumber',
    'FirstName',
    'LastName',
    'DateOfBirth',
    'PayPeriodStart',
    'PayPeriodEnd',
    'PaymentDate',
    'OrdinaryTimeEarnings',
    'SuperGuaranteeAmount',
    'SalarySacrifice',
    'TotalContribution',
    'FundName',
)


def _split_name(legal_name, preferred_name=None):
    """Best-effort split of "First Last" into (first, last).

    - `first` prefers preferred_name (the go-by name) when it has whitespace,
      otherwise falls back to the first token of legal_name.
    - `last` is always derived from legal_name (the legal record), since
      preferred_name alone often doesn't include the surname (e.g.
      preferred="Alice", legal="Alice Smith" → ("Alice", "Smith")).

    Single-token names return (token, '').
    """
    legal = (legal_name or '').strip()
    preferred = (preferred_name or '').strip()

    # First name
    if preferred and ' ' in preferred:
        first = preferred.split(None, 1)[0]
    elif preferred:
        first = preferred
    elif legal:
        first = legal.split(None, 1)[0]
    else:
        first = ''

    # Last name — always from legal_name
    if legal:
        tokens = legal.split()
        last = tokens[-1] if len(tokens) > 1 else ''
    else:
        last = ''

    return (first, last)


def _format_date_iso(d):
    return d.isoformat() if d else ''


def _format_amount(value):
    """Decimal dollars as a plain number string. AustralianSuper accepts
    either bare numbers or dollar strings — bare is safer."""
    if value is None or value == '':
        return ''
    return str(Decimal(str(value)).quantize(Decimal('0.01')))


def _get_setting(key):
    """Read a setting from the system_settings table.

    Lazy import so this module can be imported in environments where the
    SQLAlchemy session isn't yet configured (e.g. raw CLI tools).
    """
    from app.models.system_setting import get_setting
    return get_setting(key)


def build_super_csv(rows):
    """Build the AustralianSuper CSV from a list of contribution rows.

    Args:
      rows: iterable of dicts. Each dict should have:
        - 'member_number'    (str)
        - 'legal_name'       (str) — full name; we split it ourselves
        - 'preferred_name'   (str, optional)
        - 'date_of_birth'    (date or None) — TODO not yet on Employee
        - 'pay_period_start' (date)
        - 'pay_period_end'   (date)
        - 'payment_date'     (date)
        - 'ote_amount'       (Decimal/str)
        - 'sgc_amount'       (Decimal/str) — employer super guarantee
        - 'salary_sacrifice' (Decimal/str, optional, default 0)
        - 'fund_name'        (str, defaults to system_settings.DEFAULT_FUND_NAME)

    Returns: a CSV string (UTF-8 with BOM so Excel opens cleanly).
    """
    buf = io.StringIO()
    # BOM so Excel on Windows opens the file with the right encoding
    # (AustralianSuper's portal accepts UTF-8 BOM CSV).
    buf.write('\ufeff')

    writer = csv.DictWriter(buf, fieldnames=COLUMN_ORDER, extrasaction='ignore')
    writer.writeheader()

    for row in rows:
        first, last = _split_name(
            row.get('legal_name'),
            preferred_name=row.get('preferred_name'),
        )
        sgc = Decimal(str(row.get('sgc_amount', '0') or '0'))
        sacrifice = Decimal(str(row.get('salary_sacrifice', '0') or '0'))
        total = sgc + sacrifice

        writer.writerow({
            'MemberNumber': row.get('member_number') or '',
            'FirstName': first,
            'LastName': last,
            'DateOfBirth': _format_date_iso(row.get('date_of_birth')),
            'PayPeriodStart': _format_date_iso(row.get('pay_period_start')),
            'PayPeriodEnd': _format_date_iso(row.get('pay_period_end')),
            'PaymentDate': _format_date_iso(row.get('payment_date')),
            'OrdinaryTimeEarnings': _format_amount(row.get('ote_amount')),
            'SuperGuaranteeAmount': _format_amount(sgc),
            'SalarySacrifice': _format_amount(sacrifice),
            'TotalContribution': _format_amount(total),
            'FundName': row.get('fund_name') or _get_setting('DEFAULT_FUND_NAME'),
        })

    return buf.getvalue()
