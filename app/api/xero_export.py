"""
API endpoints for Xero-ready CSV exports.

GET /api/exports/xero[?year=YYYY|from_date=YYYY-MM-DD&to_date=YYYY-MM-DD]

Returns a single ZIP containing three CSV files that the user's accountant
can ingest straight into Xero via Xero's standard CSV import:

    xero_invoices.csv   — Xero Invoice template (sales / AR)
    xero_bills.csv      — Xero Bill template (purchases / AP) — sourced from
                          our Expense table since we don't have a separate
                          vendor bills flow. BillNumber is the expense UUID
                          short prefix; ContactName is the vendor name.
    xero_contacts.csv   — Xero Contact template (customers + vendors)

Defaults: last completed Australian financial year (Jul 1 → Jun 30). Pass
`year=2025` for FY2025/26 (Jul 2025 → Jun 2026). Pass explicit `from_date` /
`to_date` to override.

Auth: requires the standard `X-API-Key` header. Exports are scoped to the
authenticated user — invoices and expenses filter on user_id. Customers are
not user-scoped (consistent with /api/customers), so the contacts file
contains every customer we know about. Vendor names (from expenses) are
added to the contacts file even though they're not in our customers table.

Address handling (2026-09-17 refactor): we now read from structured columns
(address_line1, address_line2, city, state, postcode, country) on the
Customer model. The legacy free-text `address` column was dropped
entirely; the splitter is kept in app/shared/address.py only as a
reference and for the historical backfill.

Note on Xero column conventions:
- Dates are DD/MM/YYYY (Xero's AU default; works in their import wizard).
- Currency is AUD (the business trades in Australian dollars).
- Amounts are bare numbers (no $ symbol, no thousands separators).
- UTF-8 with BOM so Excel on Windows opens it cleanly.
- Unknown columns are ignored by Xero on import — we add Status,
  AccountCategory, and InvoiceDescription as helpful extras for the
  accountant to review before kicking off the import.

Why this exists (2026-09-17): the operator asked for an export route so an
accountant can ingest a full year of the business's bookkeeping into Xero.
The previous workflow had no way to do this — accountants were stuck
looking at PDF invoices one-by-one. This is the single-shot ingestion
path.

Module structure follows the existing api/* convention (Blueprint + decorators).
"""

import csv
import io
import zipfile
from datetime import date
from decimal import Decimal, InvalidOperation

from flask import Blueprint, request, jsonify, send_file

from app.models import db
from app.models.customer import Customer
from app.models.expense import Expense
from app.models.invoice import Invoice
from app.shared.decorators import api_key_required


xero_export_bp = Blueprint('xero_export', __name__, url_prefix='/api/exports')


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _aus_fy_dates(year):
    """Return (start, end) for an Australian financial year.

    year=2025  → FY2025/26  → 2025-07-01 .. 2026-06-30
    """
    return date(year, 7, 1), date(year + 1, 6, 30)


def _parse_date(value, field_name):
    if not value:
        return None
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        from flask import abort
        abort(400, description=f'{field_name} must be YYYY-MM-DD')


def _xero_date(d):
    """DD/MM/YYYY — Xero's AU default for CSV imports."""
    if d is None:
        return ''
    return d.strftime('%d/%m/%Y')


def _money(value):
    """Strip $, commas, whitespace; return as plain decimal string. Xero's
    CSV importer is picky about the bare-number format."""
    if value is None:
        return ''
    s = str(value).strip().replace('$', '').replace(',', '').strip()
    if not s:
        return ''
    try:
        return str(Decimal(s).quantize(Decimal('0.01')))
    except (InvalidOperation, ValueError):
        return s


def _xero_tax_type(gst_type, kind):
    """Map our gst_type (0, 0.1, etc.) to Xero's TaxType string.

    kind='income' for invoices, 'expense' for bills.
    """
    try:
        g = Decimal(str(gst_type or 0))
    except (InvalidOperation, ValueError):
        g = Decimal('0')

    if g == Decimal('0.1'):
        return 'GST on Income' if kind == 'income' else 'GST on Expenses'
    if g == Decimal('0'):
        return 'GST Free Income' if kind == 'income' else 'GST Free Expenses'
    # Anything weird (1.0 etc.) — leave blank so the accountant maps it manually
    return ''


def _contact_row(customer):
    """Build a Xero Contact template row from a Customer record.

    Reads structured address fields directly. No parsing or fallback —
    the legacy free-text `address` column was dropped in the 2026-09-17
    refactor.
    """
    return {
        'ContactName': customer.name,
        'EmailAddress': customer.contact_email or '',
        'POAddressLine1': customer.address_line1 or '',
        'POAddressLine2': customer.address_line2 or '',
        'POCity': customer.city or '',
        'PORegion': customer.state or '',
        'POPostalCode': customer.postcode or '',
        'POCountry': customer.country or 'Australia',
        'PhoneNumber': customer.contact_number or '',
        'AccountNumber': '',
    }


def _vendor_contact_row(vendor_name):
    """Vendor names from expenses don't have structured addresses — we only
    know the name. The Xero contact row leaves the address fields empty;
    the accountant fills them in or lets Xero de-duplicate against existing
    contacts."""
    return {
        'ContactName': vendor_name,
        'EmailAddress': '',
        'POAddressLine1': '',
        'POAddressLine2': '',
        'POCity': '',
        'PORegion': '',
        'POPostalCode': '',
        'POCountry': 'Australia',
        'PhoneNumber': '',
        'AccountNumber': '',
    }


# ---------------------------------------------------------------------------
# CSV column layouts — Xero's standard templates, plus helpful extras.
# Unknown columns are ignored by Xero on import, so the extras (Status,
# AccountCategory) are safe to include for the accountant to review.
# ---------------------------------------------------------------------------

INVOICE_COLUMNS = [
    'ContactName', 'EmailAddress',
    'POAddressLine1', 'POAddressLine2', 'POCity', 'PORegion', 'POPostalCode', 'POCountry',
    'InvoiceNumber', 'Reference',
    'InvoiceDate', 'DueDate',
    'Total', 'TaxTotal', 'Currency',
    'Description', 'Quantity', 'UnitAmount', 'AccountCode', 'TaxType',
    'Status', 'AccountCategory',
]

BILL_COLUMNS = [
    'ContactName', 'EmailAddress',
    'POAddressLine1', 'POAddressLine2', 'POCity', 'PORegion', 'POPostalCode', 'POCountry',
    'BillNumber', 'Reference',
    'BillDate', 'DueDate',
    'Total', 'TaxTotal', 'Currency',
    'Description', 'Quantity', 'UnitAmount', 'AccountCode', 'TaxType',
    'Status', 'AccountCategory',
]

CONTACT_COLUMNS = [
    'ContactName', 'EmailAddress',
    'POAddressLine1', 'POAddressLine2', 'POCity', 'PORegion', 'POPostalCode', 'POCountry',
    'PhoneNumber', 'AccountNumber',
]


# ---------------------------------------------------------------------------
# CSV builders
# ---------------------------------------------------------------------------

def _build_invoices_csv(user_id, start_date, end_date, include_drafts=True):
    """One row per invoice. Quantity is always 1 (we don't track line items
    at sub-invoice granularity). UnitAmount = ex_gst_amount."""
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=INVOICE_COLUMNS, extrasaction='ignore')
    writer.writeheader()

    query = (
        Invoice.query
        .filter(
            Invoice.user_id == user_id,
            Invoice.invoice_date >= start_date,
            Invoice.invoice_date <= end_date,
            Invoice.status != 'cancelled',
        )
        .order_by(Invoice.invoice_date.asc(), Invoice.id.asc())
    )

    count = 0
    for inv in query.all():
        if not include_drafts and inv.status == 'draft':
            continue

        customer = inv.customer
        email = customer.contact_email if customer else ''
        name = (customer.name if customer else inv.client_name) or inv.client_name

        # Read address straight from the customer's structured fields.
        if customer:
            line1 = customer.address_line1 or ''
            line2 = customer.address_line2 or ''
            city = customer.city or ''
            region = customer.state or ''
            postcode = customer.postcode or ''
            country = customer.country or 'Australia'
        else:
            line1 = line2 = city = region = postcode = ''
            country = 'Australia'

        writer.writerow({
            'ContactName': name,
            'EmailAddress': email or '',
            'POAddressLine1': line1,
            'POAddressLine2': line2,
            'POCity': city,
            'PORegion': region,
            'POPostalCode': postcode,
            'POCountry': country,
            'InvoiceNumber': inv.id[:8].upper(),
            'Reference': inv.id,
            'InvoiceDate': _xero_date(inv.invoice_date),
            'DueDate': _xero_date(inv.due_date) if inv.due_date else '',
            'Total': _money(inv.total_amount),
            'TaxTotal': _money(inv.gst_amount),
            'Currency': 'AUD',
            'Description': inv.description or '',
            'Quantity': '1',
            'UnitAmount': _money(inv.ex_gst_amount),
            'AccountCode': '',  # We don't track Xero account codes
            'TaxType': _xero_tax_type(inv.gst_type, 'income'),
            'Status': inv.status,
            'AccountCategory': inv.account_category.name if inv.account_category else '',
        })
        count += 1

    return out.getvalue(), count


def _build_bills_csv(user_id, start_date, end_date):
    """One row per expense. Same shape as invoices, with BillNumber/BillDate
    columns and a 'GST on Expenses' tax-type."""
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=BILL_COLUMNS, extrasaction='ignore')
    writer.writeheader()

    query = (
        Expense.query
        .filter(
            Expense.user_id == user_id,
            Expense.expense_date >= start_date,
            Expense.expense_date <= end_date,
        )
        .order_by(Expense.expense_date.asc(), Expense.id.asc())
    )

    count = 0
    for exp in query.all():
        writer.writerow({
            'ContactName': exp.vendor_name,
            'EmailAddress': '',
            'POAddressLine1': '',
            'POAddressLine2': '',
            'POCity': '',
            'PORegion': '',
            'POPostalCode': '',
            'POCountry': 'Australia',
            'BillNumber': exp.id[:8].upper(),
            'Reference': exp.id,
            'BillDate': _xero_date(exp.expense_date),
            'DueDate': '',
            'Total': _money(exp.total_amount),
            'TaxTotal': _money(exp.gst_amount),
            'Currency': exp.currency or 'AUD',
            'Description': exp.description or exp.vendor_name,
            'Quantity': '1',
            'UnitAmount': _money(exp.ex_gst_amount),
            'AccountCode': '',
            'TaxType': _xero_tax_type(exp.gst_type, 'expense'),
            'Status': 'logged',  # expenses are paid at point-of-capture
            'AccountCategory': exp.account_category.name if exp.account_category else '',
        })
        count += 1

    return out.getvalue(), count


def _build_contacts_csv(user_id, start_date, end_date):
    """All customers + every distinct vendor from the period's expenses.
    Dedup on lowercased name so the same contact isn't emitted twice."""
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=CONTACT_COLUMNS, extrasaction='ignore')
    writer.writeheader()

    seen = set()

    # Customers (user-scoped)
    for cust in Customer.query.filter_by(user_id=user_id).order_by(Customer.name.asc()).all():
        key = cust.name.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        writer.writerow(_contact_row(cust))

    # Distinct vendors from this period's expenses (user-scoped)
    vendor_rows = (
        db.session.query(Expense.vendor_name)
        .filter(
            Expense.user_id == user_id,
            Expense.expense_date >= start_date,
            Expense.expense_date <= end_date,
        )
        .distinct()
        .all()
    )
    for (vendor,) in vendor_rows:
        if not vendor:
            continue
        key = vendor.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        writer.writerow(_vendor_contact_row(vendor.strip()))

    return out.getvalue(), len(seen)


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------

def resolve_export_range(from_date_str=None, to_date_str=None, year_str=None):
    """Resolve the export date range from query-string / form inputs.

    Returns (from_date, to_date, period_label, error_or_None).

    - If from_date_str and to_date_str are both provided, use them.
    - Else if year_str is provided, use that FY.
    - Else default to the last completed financial year.

    period_label is used in the download filename, e.g. 'FY2025/26' or
    '2026-01-01_to_2026-06-30'.
    """
    if from_date_str or to_date_str:
        from_d = _parse_date(from_date_str, 'from_date')
        to_d = _parse_date(to_date_str, 'to_date')
        if not from_d or not to_d:
            return (None, None, None, 'Both from_date and to_date are required when using an explicit range')
        if from_d > to_d:
            return (None, None, None, 'from_date must be on or before to_date')
        return (from_d, to_d, f'{from_d.isoformat()}_to_{to_d.isoformat()}', None)

    if year_str:
        try:
            year = int(year_str)
        except (ValueError, TypeError):
            return (None, None, None, 'year must be an integer (e.g. 2025 for FY2025/26)')
    else:
        # Last completed FY: regardless of which half of the calendar we're
        # in, the most recently finished FY started in (year - 1).
        year = date.today().year - 1

    from_d, to_d = _aus_fy_dates(year)
    return (from_d, to_d, f'FY{year}/{str(year + 1)[-2:]}', None)


def build_xero_zip(user, start_date, end_date, include_drafts=True):
    """Build the Xero export ZIP for a single user.

    Returns (zip_bytes, counts_dict) where counts_dict has keys
    'invoices', 'bills', 'contacts'. Does NOT log activity — callers do
    that so they can attach their own request metadata.
    """
    invoices_csv, inv_count = _build_invoices_csv(
        user.id, start_date, end_date, include_drafts=include_drafts,
    )
    bills_csv, bill_count = _build_bills_csv(user.id, start_date, end_date)
    contacts_csv, contact_count = _build_contacts_csv(user.id, start_date, end_date)

    # Pack into a zip — UTF-8 BOM on each CSV so Excel on Windows opens cleanly
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode='w', compression=zipfile.ZIP_DEFLATED) as zf:
        for name, content in (
            ('xero_invoices.csv', invoices_csv),
            ('xero_bills.csv', bills_csv),
            ('xero_contacts.csv', contacts_csv),
        ):
            zf.writestr(name, '\ufeff' + content)
    return buf.getvalue(), {
        'invoices': inv_count,
        'bills': bill_count,
        'contacts': contact_count,
    }


def log_xero_export(user, period_label, counts, include_drafts, source):
    """Record an export_xero activity log entry.

    `source` is the route that produced the export ('api' or 'hmi').
    """
    from app.models.activity_log import ActivityLog
    db.session.add(ActivityLog(
        user_id=user.id,
        action='export_xero',
        table_name='invoices',  # loose; we touch invoices+expenses+customers
        record_id=period_label,
        new_values={
            **counts,
            'include_drafts': include_drafts,
            'source': source,
        },
    ))
    db.session.commit()


@xero_export_bp.route('/xero', methods=['GET'])
@api_key_required
def export_xero():
    """Generate a Xero-ready CSV export for the requested period.

    Query parameters:
      year          (int)   Australian financial year start (e.g. 2025 →
                             FY2025/26). Defaults to last completed FY.
      from_date     (str)   ISO date — overrides year if provided
      to_date       (str)   ISO date — overrides year if provided
      include_drafts (bool) If false, skip invoices with status='draft'.
                             Default true.

    Returns: application/zip containing xero_invoices.csv, xero_bills.csv,
    and xero_contacts.csv. Filename includes the FY/range so multiple
    downloads don't collide.
    """
    user = request.current_user

    from_d, to_d, period_label, error = resolve_export_range(
        from_date_str=request.args.get('from_date'),
        to_date_str=request.args.get('to_date'),
        year_str=request.args.get('year'),
    )
    if error:
        return jsonify({'error': error}), 400

    include_drafts = request.args.get('include_drafts', 'true').lower() != 'false'

    zip_bytes, counts = build_xero_zip(user, from_d, to_d, include_drafts=include_drafts)
    log_xero_export(user, period_label, counts, include_drafts, source='api')

    return send_file(
        io.BytesIO(zip_bytes),
        mimetype='application/zip',
        as_attachment=True,
        download_name=f'peristyle-xero-export-{period_label}.zip',
    )


# Convenience: expose a JSON summary endpoint so the user can preview what's
# in the export without downloading the zip. Helpful for sanity-checking
# before sending to the accountant.
@xero_export_bp.route('/xero/preview', methods=['GET'])
@api_key_required
def export_xero_preview():
    """JSON summary of what /api/exports/xero would emit, without building
    the CSV. Useful for sanity-checking the date range."""
    user = request.current_user

    # Same date-range resolution as the real export
    if request.args.get('from_date') or request.args.get('to_date'):
        from_d = _parse_date(request.args.get('from_date'), 'from_date')
        to_d = _parse_date(request.args.get('to_date'), 'to_date')
        if not from_d or not to_d:
            return jsonify({'error': 'Both from_date and to_date are required when using explicit range'}), 400
    else:
        year_str = request.args.get('year')
        if year_str:
            try:
                year = int(year_str)
            except (ValueError, TypeError):
                return jsonify({'error': 'year must be an integer'}), 400
        else:
            year = date.today().year - 1
        from_d, to_d = _aus_fy_dates(year)

    invoice_count = (
        Invoice.query
        .filter(
            Invoice.user_id == user.id,
            Invoice.invoice_date >= from_d,
            Invoice.invoice_date <= to_d,
            Invoice.status != 'cancelled',
        )
        .count()
    )
    expense_count = (
        Expense.query
        .filter(
            Expense.user_id == user.id,
            Expense.expense_date >= from_d,
            Expense.expense_date <= to_d,
        )
        .count()
    )

    return jsonify({
        'from_date': from_d.isoformat(),
        'to_date': to_d.isoformat(),
        'invoice_count': invoice_count,
        'expense_count': expense_count,
        'download_url': f'/api/exports/xero?year={from_d.year if from_d.month >= 7 else from_d.year - 1}',
    }), 200
