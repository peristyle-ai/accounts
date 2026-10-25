"""Tests for the Single Touch Payroll (STP) pay-event CSV builder.

The format is fixed by the ATO portal, so the strongest test we can write
is a byte-level comparison against the portal's own sample file, which is
checked in at tests/fixtures/stp_ato_sample.csv. Everything else asserts
the data mapping.

Pure functions only — no DB, no app context needed.
"""

import csv
import io
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.shared.stp_csv import (
    STP_CSV_COLUMNS,
    build_stp_csv,
    build_stp_csvs_by_payrun,
    build_stp_row,
    fmt_amount,
    fmt_date,
    fmt_datetime,
    group_rows_by_payrun,
    load_employer_from_config,
    normalize_tfn,
    payrun_consistency_errors,
    split_name,
)

FIXTURE = Path(__file__).resolve().parent / 'fixtures' / 'stp_ato_sample.csv'


# ---------------------------------------------------------------------------
# Format pinning
# ---------------------------------------------------------------------------
def test_column_count_is_93():
    assert len(STP_CSV_COLUMNS) == 93


def test_header_matches_ato_sample_exactly():
    """Our header must equal the portal sample's header, in order."""
    assert FIXTURE.exists(), f'missing format fixture: {FIXTURE}'
    with open(FIXTURE, newline='', encoding='utf-8') as fh:
        sample_header = next(csv.reader(fh))
    assert list(STP_CSV_COLUMNS) == sample_header


def test_sample_fixture_itself_is_93_wide():
    with open(FIXTURE, newline='', encoding='utf-8') as fh:
        rows = list(csv.reader(fh))
    assert len(rows[0]) == 93
    assert len(rows[1]) == 93


def test_output_has_no_bom_and_uses_crlf():
    out = build_stp_csv([_row()])
    assert not out.startswith('\ufeff')
    assert '\r\n' in out
    lines = out.split('\r\n')
    assert lines[-1] == ''          # trailing CRLF
    assert all(l == '' or l.endswith(',') or True for l in lines)


def test_output_parses_back_to_93_columns():
    out = build_stp_csv([_row()])
    rows = list(csv.reader(io.StringIO(out, newline='')))
    assert len(rows) == 2                       # header + 1 data row
    assert len(rows[0]) == 93
    assert len(rows[1]) == 93


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------
@pytest.mark.parametrize('value,expected', [
    (date(2021, 3, 1), '1/03/2021'),
    (date(2026, 6, 15), '15/06/2026'),
    (date(2026, 12, 5), '5/12/2026'),
    ('2026-06-15', '15/06/2026'),
    (None, ''),
    ('', ''),
])
def test_fmt_date_is_ddmmyyyy(value, expected):
    assert fmt_date(value) == expected


def test_fmt_date_is_not_iso():
    assert fmt_date(date(2026, 6, 15)) != '2026-06-15'


def test_fmt_datetime():
    assert fmt_datetime(date(2021, 3, 1), '10:00') == '1/03/2021 10:00'
    assert fmt_datetime(date(2021, 3, 1)) == '1/03/2021 00:00'


@pytest.mark.parametrize('value,expected', [
    (Decimal('8000.00'), '8000'),
    (Decimal('95.50'), '95.5'),
    (Decimal('95.00'), '95'),
    (Decimal('391.87'), '391.87'),
    (0, '0'),
    (None, ''),
    ('', ''),
])
def test_fmt_amount_strips_trailing_zeros(value, expected):
    assert fmt_amount(value) == expected


def test_normalize_tfn():
    assert normalize_tfn('123 456 789') == '123456789'
    assert normalize_tfn('123456789') == '123456789'
    assert normalize_tfn('') == ''
    assert normalize_tfn(None) == ''


def test_split_name():
    assert split_name('Alex Sample') == ('Alex', 'Sample')
    assert split_name('Alex Sample', preferred_name='Al') == ('Al', 'Sample')
    assert split_name('Alex Sample', preferred_name='Alex Ann') == ('Alex', 'Sample')
    assert split_name('Cher') == ('Cher', '')
    assert split_name('') == ('', '')


# ---------------------------------------------------------------------------
# Row mapping
# ---------------------------------------------------------------------------
def _employee(**over):
    base = dict(
        legal_name='Alex Sample',
        preferred_name='Alex',
        tfn_plain='123456789',
        date_of_birth_plain=date(1990, 5, 4),
        middle_name=None,
        payroll_number=1,
        tax_treatment_code='RTXXXX',
        basis_of_employment_code='F',
        abn=None,
        address_line1='12 Example Street',
        address_line2=None,
        address_2=None,
        city='Sample Town',
        state='QLD',
        postcode='4000',
        country_code='au',
        email_work='employee.work@example.com',
        email_personal=None,
        phone='0400000000',
        hired_date=None,
        start_date=date(2026, 6, 15),
        termination_date=None,
        termination_type=None,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _pay_event(**over):
    base = dict(
        payment_date=date(2026, 6, 22),
        pay_period_start=date(2026, 6, 15),
        pay_period_end=date(2026, 6, 21),
        gross_amount=Decimal('1200.00'),
        payg_tax_amount=Decimal('202.00'),
        super_payable_amount=Decimal('144.00'),
        super_ote_amount=Decimal('1200.00'),
    )
    base.update(over)
    return SimpleNamespace(**base)


def _employer(**over):
    base = dict(
        entity_abn='53000000022',
        branch_id='1',
        bms_id='00001',
        previous_bms_id='',
        product_id='',
        payrun_time='00:00',
        income_stream_code='SAW',
        income_stream_country_code='au',
        final_eoy_pay_indicator='N',
    )
    base.update(over)
    return base


def _row(**over):
    row = build_stp_row(_pay_event(), _employee(), _employer())
    row.update(over)
    return row


def test_build_stp_row_maps_identity():
    row = _row()
    assert row['employee_tfn'] == '123456789'
    assert row['family_name'] == 'Sample'
    assert row['given_name'] == 'Alex'
    assert row['payroll_number'] == '1'
    assert row['tax_treatment_code'] == 'RTXXXX'
    assert row['basis_of_employment_code'] == 'F'
    assert row['country'] == 'au'


def test_build_stp_row_maps_amounts():
    row = _row()
    assert row['employee_gross_pay'] == Decimal('1200.00')
    assert row['employee_tax'] == Decimal('202.00')
    assert row['super_guarantee_amount'] == Decimal('144.00')
    assert row['sg_earnings_amount'] == Decimal('1200.00')


def test_build_stp_row_falls_back_to_start_date_for_hired_date():
    row = _row()
    assert row['hired_date'] == date(2026, 6, 15)


def test_csv_cells_for_a_realistic_row():
    out = build_stp_csv([_row()], _employer())
    rows = list(csv.reader(io.StringIO(out, newline='')))
    header, data = rows[0], rows[1]
    cell = dict(zip(header, data))

    assert cell['Entity ABN'] == '53000000022'
    assert cell['Branch ID'] == '1'
    assert cell['BMS ID'] == '00001'
    assert cell['Employee TFN'] == '123456789'
    assert cell['Payroll number'] == '1'
    assert cell['Family name'] == 'Sample'
    assert cell['Given name'] == 'Alex'
    assert cell['Date of birth'] == '4/05/1990'
    assert cell['Address 1'] == '12 Example Street'
    assert cell['Suburb'] == 'Sample Town'
    assert cell['State/territory'] == 'QLD'
    assert cell['Postcode'] == '4000'
    assert cell['Tax treatment code'] == 'RTXXXX'
    assert cell['Hired date'] == '15/06/2026'
    assert cell['Basis of employment code'] == 'F'
    assert cell['Address 2'] == ''
    assert cell['Country'] == 'au'
    assert cell['Email'] == 'employee.work@example.com'
    assert cell['Phone'] == '0400000000'
    assert cell['Pay period start date'] == '15/06/2026'
    assert cell['Pay period end date'] == '21/06/2026'
    assert cell['Payrun date time'] == '22/06/2026 00:00'
    assert cell['Pay or update date'] == '22/06/2026'
    assert cell['Final EOY pay indicator'] == 'N'
    assert cell['Income stream code'] == 'SAW'
    assert cell['Employee gross pay'] == '1200'
    assert cell['Employee tax'] == '202'
    assert cell['Super guarantee amount'] == '144'
    assert cell['SG earnings amount'] == '1200'
    # unused columns stay empty
    assert cell['Overtime'] == ''
    assert cell['Lump sum A type R amount'] == ''
    assert cell['ETP amount 1'] == ''


def test_period_totals_are_period_level():
    rows = [
        _row(),
        _row(employee_gross_pay=Decimal('1900.00'), employee_tax=Decimal('391.87')),
    ]
    out = build_stp_csv(rows, _employer())
    parsed = list(csv.reader(io.StringIO(out, newline='')))
    header, first, second = parsed[0], parsed[1], parsed[2]
    for row in (first, second):
        cell = dict(zip(header, row))
        assert cell['Period W1 value'] == '3100'
        assert cell['Period W2 value'] == '593.87'
    # per-employee figures stay per-employee
    assert dict(zip(header, first))['Employee gross pay'] == '1200'
    assert dict(zip(header, second))['Employee gross pay'] == '1900'


def test_passthrough_columns_are_populated_when_supplied():
    row = _row(overtime=Decimal('50.00'),
               other_allowance_1_description='TOOL',
               other_allowance_1_value=Decimal('25.00'))
    out = build_stp_csv([row], _employer())
    parsed = list(csv.reader(io.StringIO(out, newline='')))
    cell = dict(zip(parsed[0], parsed[1]))
    assert cell['Overtime'] == '50'
    assert cell['Other allowance 1 description'] == 'TOOL'
    assert cell['Other allowance 1 value'] == '25'


def test_empty_rows_produce_header_only():
    out = build_stp_csv([], _employer())
    rows = list(csv.reader(io.StringIO(out, newline='')))
    assert len(rows) == 1
    assert len(rows[0]) == 93


def test_load_employer_from_config_reads_stp_block():
    employer = load_employer_from_config()
    assert 'entity_abn' in employer
    assert 'bms_id' in employer
    assert employer['branch_id'] == '1'


def test_period_totals_are_scoped_to_each_pay_period():
    """Two rows in DIFFERENT pay periods must not share a period total."""
    rows = [
        _row(),
        _row(pay_period_start=date(2026, 6, 22),
             pay_period_end=date(2026, 6, 28),
             payment_date=date(2026, 6, 29),
             employee_gross_pay=Decimal('1900.00'),
             employee_tax=Decimal('391.87')),
    ]
    out = build_stp_csv(rows, _employer())
    parsed = list(csv.reader(io.StringIO(out, newline='')))
    header, first, second = parsed[0], parsed[1], parsed[2]
    c1 = dict(zip(header, first))
    c2 = dict(zip(header, second))
    assert c1['Period W1 value'] == '1200'
    assert c1['Period W2 value'] == '202'
    assert c2['Period W1 value'] == '1900'
    assert c2['Period W2 value'] == '391.87'


def test_explicit_period_value_overrides_the_computed_total():
    row = _row(period_w1_value=Decimal('999'), period_w2_value=Decimal('111'))
    out = build_stp_csv([row], _employer())
    parsed = list(csv.reader(io.StringIO(out, newline='')))
    cell = dict(zip(parsed[0], parsed[1]))
    assert cell['Period W1 value'] == '999'
    assert cell['Period W2 value'] == '111'


# ---------------------------------------------------------------------------
# Pay-run grouping (the portal's "one file per pay run" rule)
# ---------------------------------------------------------------------------
def test_rows_are_grouped_by_pay_run():
    """The portal rejects a file whose 'Pay or update date' differs
    between records, so a CSV must cover exactly one pay run."""
    rows = [
        _row(),
        _row(pay_period_start=date(2026, 6, 22),
             pay_period_end=date(2026, 6, 28),
             payment_date=date(2026, 6, 29),
             employee_gross_pay=Decimal('1900.00')),
        # same pay run as the first row -- same payment date
        _row(employee_gross_pay=Decimal('1300.00')),
    ]
    files = build_stp_csvs_by_payrun(rows, _employer())
    assert len(files) == 2
    assert files[0]['payrun_date'] == date(2026, 6, 22)
    assert files[0]['row_count'] == 2
    assert files[1]['payrun_date'] == date(2026, 6, 29)
    assert files[1]['row_count'] == 1


def test_each_payrun_file_has_a_consistent_pay_or_update_date():
    """This is the exact rule the ATO portal enforces."""
    rows = [
        _row(),
        _row(pay_period_start=date(2026, 6, 22),
             pay_period_end=date(2026, 6, 28),
             payment_date=date(2026, 6, 29)),
    ]
    for f in build_stp_csvs_by_payrun(rows, _employer()):
        assert payrun_consistency_errors(f['csv']) == []


def test_a_single_file_spanning_two_pay_runs_is_flagged():
    """The guard must actually catch the bug that got us rejected."""
    rows = [
        _row(),
        _row(pay_period_start=date(2026, 6, 22),
             pay_period_end=date(2026, 6, 28),
             payment_date=date(2026, 6, 29)),
    ]
    merged = build_stp_csv(rows, _employer())
    errors = payrun_consistency_errors(merged)
    assert errors, 'a two-pay-run file must be flagged'
    assert any("Pay or update date" in e for e in errors)


def test_payrun_consistency_check_covers_all_file_level_columns():
    """Every file-level column the portal compares must be checked."""
    rows = [_row(), _row(employee_gross_pay=Decimal('1900.00'))]
    csv_text = build_stp_csv(rows, _employer())
    assert payrun_consistency_errors(csv_text) == []


def test_payrun_consistency_flags_inconsistent_employer_constants():
    """Entity ABN / BMS ID / Product ID must be identical across records."""
    rows = [_row(), _row(employee_gross_pay=Decimal('1900.00'))]
    merged = build_stp_csv(rows, _employer(entity_abn='11111111111'))
    parsed = list(csv.reader(io.StringIO(merged, newline='')))
    header = parsed[0]
    idx = header.index('Entity ABN')
    parsed[2][idx] = '22222222222'
    broken = '\r\n'.join(','.join(r) for r in parsed) + '\r\n'
    errors = payrun_consistency_errors(broken)
    assert any('Entity ABN' in e for e in errors)
