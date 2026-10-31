"""Single Touch Payroll (STP) Phase 1 pay-event CSV builder.

Destination: https://portal.singletouch.com.au  (ATO / STP payroll portal)

This is NOT the same artifact as the AustralianSuper contribution files
(`aussuper-mapped-*.csv`, built by `app.shared.mapped_csv`) — those report
superannuation contributions to the fund, these report pay events to the ATO.

Format
------
93 columns, one row per employee per pay event. Column order is fixed by
the portal and must not change. The canonical sample (ATO example row,
"Harry Staur", entity ABN 62483468038) is 93 columns wide, uses CRLF line
endings and carries NO UTF-8 BOM — we match it byte-for-byte in those
respects so the portal accepts the file first time.

Dates are `D/MM/YYYY` (Australian), NOT ISO. Times are `D/MM/YYYY HH:MM`.
Amounts are plain decimal numbers with trailing zeros stripped, e.g. 8000
renders as "8000" and 95.50 renders as "95.5" — this matches every value
in the sample row.

Minimal-first scope
-------------------
The builder emits the full 93-column shape, but only these columns are
populated for a plain salary payment:

    Entity ABN, Period W1 value, Period W2 value, Branch ID, BMS ID,
    Payrun date time, Pay or update date, Product ID, Employee TFN,
    Payroll number, Family name, Given name, Date of birth, Address 1,
    Suburb, State/territory, Postcode, Tax treatment code, Middle name,
    Hired date, Termination date, Termination type, Basis of employment
    code, Address 2, Country, Email, Phone, Pay period start date,
    Pay period end date, Final EOY pay indicator, Income stream code,
    Employee gross pay, Employee tax, Income stream country code,
    Super guarantee amount, SG earnings amount

Every other column (allowances, leave, lump sums, ETPs, FBT, salary
sacrifice, garnishee/deductions) is emitted as an empty cell, but is
populated from the row dict when the key is present — so extending to
those later is a data-mapping change, not a format change.

Open questions (raised with the operator 2026-10-20, still outstanding)
------------------------------------------------------------------
1. "Period W1 value" / "Period W2 value" are read here as the PERIOD
   totals (sum of gross / tax across the exported rows), repeating on
   each row, while "Employee gross pay" / "Employee tax" are the
   per-employee figures. For a single-employee export the two pairs are
   equal. Needs confirmation against the portal spec.
2. "Payroll number" is emitted as `employee.payroll_number` (a stable
   per-employee integer, currently 1 for Sample Employee). The sample uses an
   alphanumeric value ('AAA626') and the operator described a derived
   "initials + daily counter" form for a *Payroll ID* — but this 93-column
   format has no separate Payroll ID column, so the stable integer wins.
   If the portal rejects it, override via the row key `payroll_number`.
3. `Entity ABN`, `Product ID` and `BMS ID` come from config.yaml and are
   placeholders until the operator obtains the ATO-issued values.
"""

import csv
import io
from datetime import date, datetime
from decimal import Decimal

# ---------------------------------------------------------------------------
# Column order — fixed by the portal. DO NOT reorder.
# ---------------------------------------------------------------------------
STP_CSV_COLUMNS = (
    'Entity ABN',
    'Period W1 value',
    'Period W2 value',
    'Branch ID',
    'BMS ID',
    'Payrun date time',
    'Period CS garnishee Total',
    'Period CS deduction Total',
    'Pay or update date',
    'Previous BMS ID',
    'Product ID',
    'Employee TFN',
    'Payroll number',
    'Family name',
    'Given name',
    'Date of birth',
    'Address 1',
    'Suburb',
    'State/territory',
    'Postcode',
    'Tax treatment code',
    'Employee ABN',
    'Middle name',
    'Hired date',
    'Termination date',
    'Termination type',
    'Basis of employment code',
    'Address 2',
    'Country',
    'Email',
    'Phone',
    'Tax offset amount',
    'Pay period start date',
    'Pay period end date',
    'Final EOY pay indicator',
    'Income stream code',
    'Employee gross pay',
    'Employee tax',
    'Income stream country code',
    'Exempt foreign income',
    'Foreign tax paid',
    'Overtime',
    'Bonus commission',
    'Directors fees',
    'Employee CDEP',
    'Other leave',
    'Cashout leave',
    'Term unused leave',
    'Parental leave',
    'Workers comp leave',
    'Defence leave',
    'Kilometer allowance',
    'Transport allowance',
    'Laundry allowance',
    'Meal allowance',
    'Travel allowance',
    'Tool allowance',
    'Tasks allowance',
    'Qualifications allowance',
    'Other allowance 1 description',
    'Other allowance 1 value',
    'Other allowance 2 description',
    'Other allowance 2 value',
    'Other allowance 3 description',
    'Other allowance 3 value',
    'Salsac Super',
    'Salsac Other',
    'Lump sum A type R amount',
    'Lump sum A type T amount',
    'Lump sum B amount',
    'Lump sum D amount',
    'Lump sum E amount 1',
    'Lump sum E financial year 1',
    'Lump sum W amount',
    'Union fees',
    'Workplace giving',
    'CS garnishee',
    'CS deduction',
    'Super guarantee amount',
    'SG earnings amount',
    'RESC',
    'FBT taxable reportable amount',
    'FBT exempt reportable amount',
    'ETP code 1',
    'ETP paid date 1',
    'ETP tax free amount 1',
    'ETP amount 1',
    'ETP tax 1',
    'ETP code 2',
    'ETP paid date 2',
    'ETP tax free amount 2',
    'ETP amount 2',
    'ETP tax 2',
)

assert len(STP_CSV_COLUMNS) == 93, 'STP column count drifted from 93'

# ---------------------------------------------------------------------------
# Optional passthrough columns: emitted as-is when the row dict provides
# the key, empty otherwise. Keeps the door open for allowances/leave/lump
# sums without another format change.
# ---------------------------------------------------------------------------
_PASSTHROUGH_AMOUNTS = (
    'Tax offset amount',
    'Exempt foreign income',
    'Foreign tax paid',
    'Overtime',
    'Bonus commission',
    'Directors fees',
    'Employee CDEP',
    'Other leave',
    'Cashout leave',
    'Term unused leave',
    'Parental leave',
    'Workers comp leave',
    'Defence leave',
    'Kilometer allowance',
    'Transport allowance',
    'Laundry allowance',
    'Meal allowance',
    'Travel allowance',
    'Tool allowance',
    'Tasks allowance',
    'Qualifications allowance',
    'Other allowance 1 value',
    'Other allowance 2 value',
    'Other allowance 3 value',
    'Salsac Super',
    'Salsac Other',
    'Lump sum A type R amount',
    'Lump sum A type T amount',
    'Lump sum B amount',
    'Lump sum D amount',
    'Lump sum E amount 1',
    'Lump sum E financial year 1',
    'Lump sum W amount',
    'Union fees',
    'Workplace giving',
    'CS garnishee',
    'CS deduction',
    'RESC',
    'FBT taxable reportable amount',
    'FBT exempt reportable amount',
    'ETP code 1',
    'ETP paid date 1',
    'ETP tax free amount 1',
    'ETP amount 1',
    'ETP tax 1',
    'ETP code 2',
    'ETP paid date 2',
    'ETP tax free amount 2',
    'ETP amount 2',
    'ETP tax 2',
)

_PASSTHROUGH_TEXT = (
    'Other allowance 1 description',
    'Other allowance 2 description',
    'Other allowance 3 description',
)

# Row-dict key -> column header, for the passthrough columns.
_PASSTHROUGH_KEYS = {
    'tax_offset_amount': 'Tax offset amount',
    'exempt_foreign_income': 'Exempt foreign income',
    'foreign_tax_paid': 'Foreign tax paid',
    'overtime': 'Overtime',
    'bonus_commission': 'Bonus commission',
    'directors_fees': 'Directors fees',
    'employee_cdep': 'Employee CDEP',
    'other_leave': 'Other leave',
    'cashout_leave': 'Cashout leave',
    'term_unused_leave': 'Term unused leave',
    'parental_leave': 'Parental leave',
    'workers_comp_leave': 'Workers comp leave',
    'defence_leave': 'Defence leave',
    'kilometer_allowance': 'Kilometer allowance',
    'transport_allowance': 'Transport allowance',
    'laundry_allowance': 'Laundry allowance',
    'meal_allowance': 'Meal allowance',
    'travel_allowance': 'Travel allowance',
    'tool_allowance': 'Tool allowance',
    'tasks_allowance': 'Tasks allowance',
    'qualifications_allowance': 'Qualifications allowance',
    'other_allowance_1_description': 'Other allowance 1 description',
    'other_allowance_1_value': 'Other allowance 1 value',
    'other_allowance_2_description': 'Other allowance 2 description',
    'other_allowance_2_value': 'Other allowance 2 value',
    'other_allowance_3_description': 'Other allowance 3 description',
    'other_allowance_3_value': 'Other allowance 3 value',
    'salsac_super': 'Salsac Super',
    'salsac_other': 'Salsac Other',
    'lump_sum_a_type_r_amount': 'Lump sum A type R amount',
    'lump_sum_a_type_t_amount': 'Lump sum A type T amount',
    'lump_sum_b_amount': 'Lump sum B amount',
    'lump_sum_d_amount': 'Lump sum D amount',
    'lump_sum_e_amount_1': 'Lump sum E amount 1',
    'lump_sum_e_financial_year_1': 'Lump sum E financial year 1',
    'lump_sum_w_amount': 'Lump sum W amount',
    'union_fees': 'Union fees',
    'workplace_giving': 'Workplace giving',
    'cs_garnishee': 'CS garnishee',
    'cs_deduction': 'CS deduction',
    'resc': 'RESC',
    'fbt_taxable_reportable_amount': 'FBT taxable reportable amount',
    'fbt_exempt_reportable_amount': 'FBT exempt reportable amount',
    'etp_code_1': 'ETP code 1',
    'etp_paid_date_1': 'ETP paid date 1',
    'etp_tax_free_amount_1': 'ETP tax free amount 1',
    'etp_amount_1': 'ETP amount 1',
    'etp_tax_1': 'ETP tax 1',
    'etp_code_2': 'ETP code 2',
    'etp_paid_date_2': 'ETP paid date 2',
    'etp_tax_free_amount_2': 'ETP tax free amount 2',
    'etp_amount_2': 'ETP amount 2',
    'etp_tax_2': 'ETP tax 2',
}


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------
def fmt_date(value):
    """Format a date as `D/MM/YYYY`. Accepts date, datetime or ISO string.

    Day is NOT zero-padded, month IS -- exactly as the ATO sample renders
    it ('5/06/1962', '1/03/2021', '6/02/2006'). Returns '' for None/empty.
    """
    if value is None or value == '':
        return ''
    if isinstance(value, datetime):
        value = value.date()
    if isinstance(value, date):
        return f'{value.day}/{value.month:02d}/{value.year}'
    text = str(value).strip()
    if not text:
        return ''
    try:
        parsed = date.fromisoformat(text[:10])
    except ValueError:
        return text
    return f'{parsed.day}/{parsed.month:02d}/{parsed.year}'


def fmt_datetime(value, time_str='00:00'):
    """Format a date + `HH:MM` as `D/MM/YYYY HH:MM`."""
    d = fmt_date(value)
    if not d:
        return ''
    return f'{d} {time_str}'


def fmt_amount(value):
    """Format a monetary value as a plain number string.

    Trailing zeros are stripped so 8000.00 -> '8000' and 95.50 -> '95.5',
    matching the ATO sample row exactly. None/'' -> ''.
    """
    if value is None or value == '':
        return ''
    try:
        amount = Decimal(str(value))
    except Exception:
        return str(value)
    quantized = amount.quantize(Decimal('0.01'))
    if quantized == quantized.to_integral_value():
        return str(quantized.quantize(Decimal('1')))
    return str(quantized.normalize())


def normalize_tfn(value):
    """Strip a TFN down to its 9 digits. '' when nothing usable."""
    if not value:
        return ''
    digits = ''.join(ch for ch in str(value) if ch.isdigit())
    return digits or ''


def split_name(legal_name, preferred_name=None):
    """Split a full name into (given, family).

    Given name prefers `preferred_name` when it has more than one token
    (then the first token is used), else the first token of `legal_name`.
    Family name is always the LAST token of `legal_name` — the legal
    record — because a go-by name often omits the surname.
    """
    legal = (legal_name or '').strip()
    preferred = (preferred_name or '').strip()
    if preferred and ' ' in preferred:
        given = preferred.split(None, 1)[0]
    elif preferred:
        given = preferred
    elif legal:
        given = legal.split(None, 1)[0]
    else:
        given = ''
    family = ''
    if legal:
        tokens = legal.split()
        family = tokens[-1] if len(tokens) > 1 else ''
    return given, family


# ---------------------------------------------------------------------------
# Employer-level constants
# ---------------------------------------------------------------------------
DEFAULT_EMPLOYER = {
    'entity_abn': '',
    'branch_id': '1',
    'bms_id': '',
    'previous_bms_id': '',
    'product_id': '',
    'payrun_time': '00:00',
    'income_stream_code': 'SAW',
    'income_stream_country_code': 'au',
    'final_eoy_pay_indicator': 'N',
}


def load_employer_from_config():
    """Read the `stp:` block of config.yaml into an employer dict.

    Falls back to DEFAULT_EMPLOYER for anything absent. Never raises —
    a missing/invalid config yields defaults rather than a 500.
    """
    employer = dict(DEFAULT_EMPLOYER)
    try:
        from config import accounts_config
        stp = accounts_config.get('stp', default={}) or {}
    except Exception:
        stp = {}
    for key in ('entity_abn', 'branch_id', 'bms_id', 'previous_bms_id',
                'product_id', 'payrun_time', 'income_stream_code',
                'income_stream_country_code', 'final_eoy_pay_indicator',
                'lodgement_ref_prefix'):
        value = stp.get(key)
        if value not in (None, ''):
            employer[key] = str(value)
    return employer


# ---------------------------------------------------------------------------
# Row construction
# ---------------------------------------------------------------------------
def build_stp_row(pay_event, employee, employer=None, extra=None):
    """Map a PayEvent + Employee pair to an STP row dict.

    Both arguments are duck-typed — anything with the right attributes
    works, so this stays unit-testable without the ORM. `extra` is an
    optional dict of overrides (allowances, leave, lump sums...).

    The Employee model's PII is encrypted at rest, so we read
    `tfn_plain` / `date_of_birth_plain` when present and fall back to
    the masked properties otherwise.
    """
    employer = {**DEFAULT_EMPLOYER, **(employer or {})}
    extra = extra or {}

    legal_name = getattr(employee, 'legal_name', '') or ''
    preferred_name = getattr(employee, 'preferred_name', None)
    given, family = split_name(legal_name, preferred_name)

    tfn = getattr(employee, 'tfn_plain', None)
    if tfn is None:
        tfn = getattr(employee, 'tfn', '') or ''
    dob = getattr(employee, 'date_of_birth_plain', None)
    if dob is None:
        dob = getattr(employee, 'date_of_birth', None)

    payment_date = getattr(pay_event, 'payment_date', None)
    period_start = getattr(pay_event, 'pay_period_start', None)
    period_end = getattr(pay_event, 'pay_period_end', None)

    hired_date = getattr(employee, 'hired_date', None) or getattr(employee, 'start_date', None)
    address_2 = getattr(employee, 'address_2', None) or getattr(employee, 'address_line2', None)
    country_code = getattr(employee, 'country_code', None) or 'au'
    email = (getattr(employee, 'email_work', None)
             or getattr(employee, 'email_personal', None) or '')

    payroll_number = getattr(employee, 'payroll_number', None)
    if payroll_number is not None:
        payroll_number = str(payroll_number)

    row = {
        # -- identity --
        'employee_tfn': normalize_tfn(tfn),
        'family_name': family,
        'given_name': given,
        'date_of_birth': dob,
        'middle_name': getattr(employee, 'middle_name', None) or '',
        'payroll_number': payroll_number or '',
        'tax_treatment_code': getattr(employee, 'tax_treatment_code', None) or 'RTXXXX',
        'basis_of_employment_code': getattr(employee, 'basis_of_employment_code', None) or 'F',
        'employee_abn': getattr(employee, 'abn', None) or '',
        # -- address / contact --
        'address_1': getattr(employee, 'address_line1', None) or '',
        'address_2': address_2 or '',
        'suburb': getattr(employee, 'city', None) or '',
        'state_territory': getattr(employee, 'state', None) or '',
        'postcode': getattr(employee, 'postcode', None) or '',
        'country': country_code,
        'email': email,
        'phone': getattr(employee, 'phone', None) or '',
        # -- employment --
        'hired_date': hired_date,
        'termination_date': getattr(employee, 'termination_date', None),
        'termination_type': getattr(employee, 'termination_type', None) or '',
        # -- period --
        'payment_date': payment_date,
        'pay_period_start': period_start,
        'pay_period_end': period_end,
        'final_eoy_pay_indicator': employer.get('final_eoy_pay_indicator', 'N'),
        'income_stream_code': employer.get('income_stream_code', 'SAW'),
        'income_stream_country_code': employer.get('income_stream_country_code', 'au'),
        # -- amounts --
        'employee_gross_pay': getattr(pay_event, 'gross_amount', None),
        'employee_tax': getattr(pay_event, 'payg_tax_amount', None),
        'super_guarantee_amount': getattr(pay_event, 'super_payable_amount', None),
        'sg_earnings_amount': getattr(pay_event, 'super_ote_amount', None),
    }
    row.update(extra)
    return row


def _values_for_row(row, employer, period_w1, period_w2, cs_garnishee_total, cs_deduction_total):
    """Turn a row dict into a {column_header: cell} dict."""
    values = {col: '' for col in STP_CSV_COLUMNS}

    values['Entity ABN'] = employer.get('entity_abn', '')
    values['Period W1 value'] = fmt_amount(period_w1)
    values['Period W2 value'] = fmt_amount(period_w2)
    values['Branch ID'] = employer.get('branch_id', '1')
    values['BMS ID'] = employer.get('bms_id', '')
    values['Payrun date time'] = fmt_datetime(row.get('payment_date'), employer.get('payrun_time', '00:00'))
    values['Period CS garnishee Total'] = fmt_amount(cs_garnishee_total)
    values['Period CS deduction Total'] = fmt_amount(cs_deduction_total)
    values['Pay or update date'] = fmt_date(row.get('payment_date'))
    values['Previous BMS ID'] = employer.get('previous_bms_id', '')
    values['Product ID'] = employer.get('product_id', '')

    values['Employee TFN'] = row.get('employee_tfn', '')
    values['Payroll number'] = row.get('payroll_number', '')
    values['Family name'] = row.get('family_name', '')
    values['Given name'] = row.get('given_name', '')
    values['Date of birth'] = fmt_date(row.get('date_of_birth'))
    values['Address 1'] = row.get('address_1', '')
    values['Suburb'] = row.get('suburb', '')
    values['State/territory'] = row.get('state_territory', '')
    values['Postcode'] = row.get('postcode', '')
    values['Tax treatment code'] = row.get('tax_treatment_code', '')
    values['Employee ABN'] = row.get('employee_abn', '')
    values['Middle name'] = row.get('middle_name', '')
    values['Hired date'] = fmt_date(row.get('hired_date'))
    values['Termination date'] = fmt_date(row.get('termination_date'))
    values['Termination type'] = row.get('termination_type', '')
    values['Basis of employment code'] = row.get('basis_of_employment_code', '')
    values['Address 2'] = row.get('address_2', '')
    values['Country'] = row.get('country', '')
    values['Email'] = row.get('email', '')
    values['Phone'] = row.get('phone', '')
    values['Pay period start date'] = fmt_date(row.get('pay_period_start'))
    values['Pay period end date'] = fmt_date(row.get('pay_period_end'))
    values['Final EOY pay indicator'] = row.get('final_eoy_pay_indicator', 'N')
    values['Income stream code'] = row.get('income_stream_code', 'SAW')
    values['Employee gross pay'] = fmt_amount(row.get('employee_gross_pay'))
    values['Employee tax'] = fmt_amount(row.get('employee_tax'))
    values['Income stream country code'] = row.get('income_stream_country_code', 'au')
    values['Super guarantee amount'] = fmt_amount(row.get('super_guarantee_amount'))
    values['SG earnings amount'] = fmt_amount(row.get('sg_earnings_amount'))

    for key, column in _PASSTHROUGH_KEYS.items():
        if key in row and row[key] not in (None, ''):
            if column in _PASSTHROUGH_TEXT:
                values[column] = str(row[key])
            else:
                values[column] = fmt_amount(row[key])

    return values


def build_stp_csv(rows, employer=None):
    """Build the STP pay-event CSV.

    Args:
      rows: list of row dicts (see `build_stp_row` for the key contract).
      employer: dict of employer-level constants; see DEFAULT_EMPLOYER.
               `load_employer_from_config()` produces one from config.yaml.

    Returns:
      A CSV string: 93-column header + one line per row, CRLF line
      endings, no UTF-8 BOM (matches the portal's sample file).
    """
    employer = {**DEFAULT_EMPLOYER, **(employer or {})}
    rows = list(rows or [])

    def _total(group, key):
        total = Decimal('0')
        for row in group:
            value = row.get(key)
            if value in (None, ''):
                continue
            try:
                total += Decimal(str(value))
            except Exception:
                continue
        return total

    # "Period W1 value" / "Period W2 value" are period-level figures, so
    # they are totalled WITHIN each pay period, not across the whole file.
    # A file spanning several periods gets one set of period totals per
    # period; a row's own figures still live in "Employee gross pay" /
    # "Employee tax". A row may override its period totals explicitly via
    # the 'period_w1_value' / 'period_w2_value' keys.
    groups = {}
    for row in rows:
        key = (row.get('pay_period_start'), row.get('pay_period_end'))
        groups.setdefault(key, []).append(row)

    period_totals = {}
    for key, group in groups.items():
        period_totals[key] = (
            _total(group, 'employee_gross_pay'),
            _total(group, 'employee_tax'),
            _total(group, 'cs_garnishee'),
            _total(group, 'cs_deduction'),
        )

    buf = io.StringIO(newline='')
    writer = csv.DictWriter(buf, fieldnames=list(STP_CSV_COLUMNS),
                            extrasaction='ignore', lineterminator='\r\n')
    writer.writeheader()
    for row in rows:
        key = (row.get('pay_period_start'), row.get('pay_period_end'))
        w1, w2, garnishee, deduction = period_totals.get(key, (Decimal('0'),) * 4)
        if row.get('period_w1_value') not in (None, ''):
            w1 = row['period_w1_value']
        if row.get('period_w2_value') not in (None, ''):
            w2 = row['period_w2_value']
        writer.writerow(_values_for_row(row, employer, w1, w2, garnishee, deduction))
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Pay-run grouping
# ---------------------------------------------------------------------------
# The ATO portal validates each uploaded file independently and REJECTS a
# file whose "Pay or update date" is not identical across every record
# ("Inconsistent values found for 'Pay or update date'. This value must be
# the same for every record."). A pay event submission is therefore scoped
# to ONE pay run -- the file holds every employee paid on that run, and
# nothing else.
#
# Our export helper can be pointed at an arbitrary date range, which
# routinely spans many pay runs (a whole financial year is 50+). Building
# one CSV for the whole range produced exactly the file the portal
# rejected, so the builder now groups by pay run and the endpoint serves
# one CSV per run (zipped when a range covers more than one).
def group_rows_by_payrun(rows):
    """Group row dicts by pay run (the `payment_date`).

    Returns a list of (payrun_date, rows) tuples, ordered by pay run date
    so the output is deterministic. Rows with a missing payment_date are
    grouped together under None -- the caller decides what to do with
    them (the endpoint rejects them, since a pay event with no pay run
    date cannot be submitted).
    """
    groups = {}
    for row in rows or []:
        key = row.get('payment_date')
        groups.setdefault(key, []).append(row)
    return sorted(groups.items(), key=lambda kv: (kv[0] is None, kv[0]))


def build_stp_csvs_by_payrun(rows, employer=None):
    """Build one STP CSV per pay run.

    Returns a list of dicts:
        {
          'payrun_date': date | None,
          'row_count':   int,
          'csv':         str,   # 93-column header + rows, CRLF, no BOM
        }
    """
    employer = {**DEFAULT_EMPLOYER, **(employer or {})}
    out = []
    for payrun_date, group in group_rows_by_payrun(rows):
        out.append({
            'payrun_date': payrun_date,
            'row_count': len(group),
            'csv': build_stp_csv(group, employer),
        })
    return out


def payrun_consistency_errors(csv_text):
    """Return a list of portal-validation problems in a built CSV.

    The portal's first-line check is that every file-level field is
    identical across all records. "Pay or update date" is the one that
    bit us; "Payrun date time" and the employer constants are checked
    the same way, so they are verified here too. An empty list means the
    file should pass that validation.
    """
    parsed = list(csv.reader(io.StringIO(csv_text, newline='')))
    if not parsed:
        return ['file is empty']
    header, data = parsed[0], parsed[1:]
    if not data:
        return []

    errors = []
    for column in ('Pay or update date', 'Payrun date time', 'Entity ABN',
                    'Branch ID', 'BMS ID', 'Product ID'):
        if column not in header:
            errors.append(f'missing column {column!r}')
            continue
        idx = header.index(column)
        values = {row[idx] for row in data if len(row) > idx}
        if len(values) > 1:
            errors.append(
                f'Inconsistent values found for {column!r}: '
                f'{sorted(values)} - this value must be the same for every record.'
            )
    return errors
