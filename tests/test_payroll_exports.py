"""Tests for the payroll export pure-function builders.

Covers:
- ABA file format correctness (record length, field positions, sums)
- ABA edge cases (multiple payments, different amounts, BSB normalisation)
- AustralianSuper CSV correctness (column order, splits, totals)

The pure-function tests don't need DB. Endpoint tests are in
tests/test_stp_export_api.py (STP) and the payroll export tests.
"""

import io
import os
import sys
import csv
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ENV = Path(__file__).resolve().parent.parent / '.env'
if ENV.exists():
    for line in ENV.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, _, v = line.partition('=')
        os.environ.setdefault(k.strip(), v.strip())

from app.shared.aba import (
    build_header_record,
    build_detail_record,
    build_total_record,
    build_aba_file,
    RECORD_LENGTH,
    _bsb_format,
    _format_amount_cents,
)
from app.shared.super_csv import build_super_csv, COLUMN_ORDER, _split_name as _super_split_name
from app.shared.saff_csv import build_saff_csv, _normalize_tfn, COLUMNS as SAFF_COLUMNS


# App context fixture — the SAFF/Super CSV builders now read fund
# identifiers and the business name from the system_settings table, which
# requires a live app context. Tests that call these builders get one
# via this autouse fixture.
@pytest.fixture(autouse=True)
def _app_context():
    from app import create_app
    app = create_app()
    with app.app_context():
        yield app


# ---------------------------------------------------------------------------
# ABA format
# ---------------------------------------------------------------------------

def _fake_user():
    # The payer bank details are encrypted at rest (see
    # schema/migrate_encrypt_user_bank.sql), so the ABA builder reads the
    # decrypted `*_plain` properties, not the raw ciphertext columns.
    # The fixture was written before that refactor and was never updated
    # -- it only surfaced once the search_path fix let these tests run
    # again instead of erroring out at collection.
    return SimpleNamespace(
        account_name='Example Co',
        business_name=None,
        bank_name='NAB',
        bsb_plain='000-000',
        account_number_plain='12345678',
        de_user_id_plain='000000',
    )


class TestTraceFields:
    """The trace fields (positions 81-120) identify the ORIGINATING account.

    These are the fields the bank reconciles the debit against. The ABA
    files NAB actually accepted for this payer carried them populated;
    a file with a blank trace account cannot be tied back to the payer.
    """

    def _record(self, **kw):
        from app.shared.aba import build_detail_record
        args = dict(
            bsb='123-456',
            account_number='87654321',
            account_name='Jane Doe',
            amount='1500.50',
            lodgement_ref='JP 170926',
            payer_bsb='111-222',
            payer_account_number='13572468',
            payer_name='Example Co',
        )
        args.update(kw)
        return build_detail_record(**args)

    def test_trace_account_number_is_populated(self):
        record = self._record()
        # Positions 88-96: the payer's account number.
        assert record[87:96] == ' 13572468'

    def test_name_of_remitter_is_populated(self):
        record = self._record()
        # Positions 97-112: the payer's name.
        assert record[96:112] == 'Example Co      '

    def test_withholding_tax_field_is_zero_filled(self):
        record = self._record()
        # Positions 113-120: amount of withholding tax, zero for payroll.
        assert record[112:120] == '00000000'

    def test_trace_bsb_is_still_the_payers(self):
        record = self._record()
        assert record[80:87] == '111-222'

    def test_record_still_120_chars(self):
        assert len(self._record()) == 120

    def test_blank_payer_details_do_not_break_the_record(self):
        """A payer with no account details still yields a valid record."""
        record = self._record(payer_bsb='', payer_account_number='',
                               payer_name='')
        assert len(record) == 120
        assert record[87:96] == ' ' * 9
        assert record[96:112] == ' ' * 16
        assert record[112:120] == '00000000'

    def test_build_aba_file_carries_payer_trace(self):
        """The whole-file builder passes the payer's identity through."""
        from app.shared.aba import build_aba_file
        data = build_aba_file(
            _fake_user(),
            [{'bsb': '123-456', 'account_number': '87654321',
              'account_name': 'Jane Doe', 'amount': '1500.50',
              'lodgement_ref': 'JP 170926'}],
            date(2026, 9, 22),
        )
        detail = data.decode('ascii').split('\r\n')[1]
        # _fake_user(): account_number_plain='12345678', account_name='Example Co'
        assert detail[87:96] == ' 12345678'
        assert detail[96:112] == 'Example Co      '


class TestBsaFormat:
    """BSB normalisation: accept multiple input formats, emit NNN-NNN."""

    def test_accepts_6_digits(self):
        assert _bsb_format('123456') == '123-456'

    def test_accepts_with_hyphen(self):
        assert _bsb_format('123-456') == '123-456'

    def test_accepts_with_space(self):
        assert _bsb_format('123 456') == '123-456'

    def test_strips_non_digits(self):
        assert _bsb_format('12-34-56') == '123-456'

    def test_rejects_invalid(self):
        assert _bsb_format('12345') == ''
        assert _bsb_format('abcdef') == ''
        assert _bsb_format('') == ''


class TestFormatAmountCents:
    def test_basic(self):
        assert _format_amount_cents('1900.00') == '0000190000'

    def test_zero(self):
        assert _format_amount_cents('0.00') == '0000000000'

    def test_decimal_quantised(self):
        # Decimal uses ROUND_HALF_EVEN (banker's rounding), so .005 → .00
        # (rounds to even). Verifies the builder doesn't accidentally
        # double-charge by silently up-rounding .5 boundaries.
        assert _format_amount_cents('1900.005') == '0000190000'
        # But .006 rounds up normally.
        assert _format_amount_cents('1900.006') == '0000190001'

    def test_rejects_negative(self):
        with pytest.raises(ValueError):
            _format_amount_cents('-1.00')

    def test_rejects_overflow(self):
        with pytest.raises(ValueError):
            _format_amount_cents('1000000000.00')


class TestHeaderRecord:
    def test_length(self):
        record = build_header_record(_fake_user(), date(2026, 9, 22))
        assert len(record) == RECORD_LENGTH

    def test_record_type(self):
        record = build_header_record(_fake_user(), date(2026, 9, 22))
        assert record[0] == '0'

    def test_reel_sequence(self):
        record = build_header_record(_fake_user(), date(2026, 9, 22))
        # positions 19-20 (1-indexed) = indices 18-20 (slice end exclusive)
        assert record[18:20] == '01'

    def test_fi_abbreviation_is_nab(self):
        record = build_header_record(_fake_user(), date(2026, 9, 22))
        assert record[20:23] == 'NAB'

    def test_user_name_left_justified(self):
        record = build_header_record(_fake_user(), date(2026, 9, 22))
        # positions 31-56 (26 chars). Name starts at the left, padding is
        # all spaces.
        assert record[30:56] == 'Example Co' + ' ' * 16
        assert record[30:56].rstrip() == 'Example Co'

    def test_description_is_payroll(self):
        record = build_header_record(_fake_user(), date(2026, 9, 22))
        assert record[62:74].rstrip() == 'PAYROLL'

    def test_date_ddmmyy(self):
        record = build_header_record(_fake_user(), date(2026, 9, 22))
        assert record[74:80] == '220926'


class TestDetailRecord:
    def _pay(self, **overrides):
        defaults = {
            'bsb': '123-456',
            'account_number': '12345678',
            'account_name': 'Alice Smith',
            'amount': '998.00',
            'lodgement_ref': 'JP 170926',
        }
        defaults.update(overrides)
        return defaults

    def test_length(self):
        record = build_detail_record(**self._pay())
        assert len(record) == RECORD_LENGTH

    def test_record_type(self):
        record = build_detail_record(**self._pay())
        assert record[0] == '1'

    def test_bsb_field(self):
        record = build_detail_record(**self._pay(bsb='123456'))
        assert record[1:8] == '123-456'

    def test_account_number_right_justified(self):
        record = build_detail_record(**self._pay(account_number='12345678'))
        assert record[8:17] == ' 12345678'  # leading space (right-justified to 9)

    def test_indicator_blank(self):
        record = build_detail_record(**self._pay())
        assert record[17:18] == ' '

    def test_transaction_code_53_for_payroll(self):
        record = build_detail_record(**self._pay())
        assert record[18:20] == '53'

    def test_amount_in_cents(self):
        record = build_detail_record(**self._pay(amount='998.00'))
        assert record[20:30] == '0000099800'

    def test_amount_one_dollar(self):
        record = build_detail_record(**self._pay(amount='1.00'))
        assert record[20:30] == '0000000100'

    def test_account_name_truncated(self):
        long_name = 'A' * 50
        record = build_detail_record(**self._pay(account_name=long_name))
        # 32 chars max for name field (positions 31-62)
        assert record[30:62] == 'A' * 32

    def test_lodgement_ref_truncated(self):
        long_ref = 'X' * 30
        record = build_detail_record(**self._pay(lodgement_ref=long_ref))
        # 18 chars max (positions 63-80)
        assert record[62:80] == 'X' * 18


class TestTotalRecord:
    def test_length(self):
        record = build_total_record(count=3, total_amount_cents=250000)
        assert len(record) == RECORD_LENGTH

    def test_record_type(self):
        record = build_total_record(count=3, total_amount_cents=250000)
        assert record[0] == '7'

    def test_bsb_filler(self):
        record = build_total_record(count=3, total_amount_cents=250000)
        assert record[1:8] == '999-999'

    def test_credit_equals_net_for_credit_only(self):
        record = build_total_record(count=3, total_amount_cents=250000)
        assert record[20:30] == '0000250000'  # net
        assert record[30:40] == '0000250000'  # credit
        assert record[40:50] == '0000000000'  # debit

    def test_count_field(self):
        record = build_total_record(count=42, total_amount_cents=100000)
        assert record[74:80] == '000042'


class TestBuildAbaFile:
    def _payments(self):
        return [
            {
                'bsb': '123-456',
                'account_number': '12345678',
                'account_name': 'Alice Smith',
                'amount': '998.00',
                'lodgement_ref': 'JP 170926',
            },
            {
                'bsb': '654-321',
                'account_number': '87654321',
                'account_name': 'Jane Doe',
                'amount': '1500.50',
                'lodgement_ref': 'JD 170926',
            },
        ]

    def test_returns_bytes(self):
        data = build_aba_file(_fake_user(), self._payments(), date(2026, 9, 22))
        assert isinstance(data, bytes)

    def test_crlf_terminator(self):
        data = build_aba_file(_fake_user(), self._payments(), date(2026, 9, 22))
        assert data.endswith(b'\r\n')

    def test_records_for_two_payments(self):
        data = build_aba_file(_fake_user(), self._payments(), date(2026, 9, 22))
        # 1 header + 2 detail records + 1 total = 4 lines
        lines = data.rstrip(b'\r\n').split(b'\r\n')
        assert len(lines) == 4
        assert lines[0][0:1] == b'0'
        assert lines[1][0:1] == b'1'
        assert lines[2][0:1] == b'1'
        assert lines[3][0:1] == b'7'

    def test_total_record_sums_correctly(self):
        data = build_aba_file(_fake_user(), self._payments(), date(2026, 9, 22))
        lines = data.rstrip(b'\r\n').split(b'\r\n')
        # 99800 + 150050 = 249850 cents. Total is the LAST line.
        assert lines[-1][20:30] == b'0000249850'
        assert lines[-1][30:40] == b'0000249850'
        assert lines[-1][74:80] == b'000002'

    def test_empty_payments_still_produces_valid_file(self):
        data = build_aba_file(_fake_user(), [], date(2026, 9, 22))
        lines = data.rstrip(b'\r\n').split(b'\r\n')
        assert len(lines) == 2  # header + total, no detail records
        assert lines[1][74:80] == b'000000'
        assert lines[1][20:30] == b'0000000000'


# ---------------------------------------------------------------------------
# AustralianSuper CSV
# ---------------------------------------------------------------------------

class TestSuperCsvSplitName:
    def test_full_name_with_preferred_first(self):
        first, last = _super_split_name('Alice Smith', preferred_name='Alice')
        assert first == 'Alice'
        assert last == 'Smith'

    def test_preferred_with_full_name(self):
        first, last = _super_split_name('Alice Smith', preferred_name='Al Smith')
        assert first == 'Al'
        assert last == 'Smith'

    def test_no_preferred_falls_back_to_legal(self):
        first, last = _super_split_name('Jane Smith')
        assert first == 'Jane'
        assert last == 'Smith'

    def test_single_name(self):
        first, last = _super_split_name('Madonna')
        assert first == 'Madonna'
        assert last == ''

    def test_empty(self):
        first, last = _super_split_name('')
        assert (first, last) == ('', '')
        first, last = _super_split_name(None)
        assert (first, last) == ('', '')


class TestBuildSuperCsv:
    def _row(self, **overrides):
        defaults = {
            'member_number': '999999999',
            'legal_name': 'Alice Smith',
            'preferred_name': 'Al',
            'date_of_birth': None,
            'pay_period_start': date(2026, 9, 1),
            'pay_period_end': date(2026, 9, 7),
            'payment_date': date(2026, 9, 9),
            'ote_amount': Decimal('1900.00'),
            'sgc_amount': Decimal('228.00'),
            'salary_sacrifice': Decimal('0'),
            'fund_name': 'AustralianSuper',
        }
        defaults.update(overrides)
        return defaults

    def test_column_order_matches_const(self):
        csv_text = build_super_csv([self._row()])
        reader = csv.DictReader(io.StringIO(csv_text.lstrip('\ufeff')))
        assert tuple(reader.fieldnames) == COLUMN_ORDER

    def test_utf8_bom(self):
        csv_text = build_super_csv([self._row()])
        assert csv_text.startswith('\ufeff')

    def test_name_splitting(self):
        csv_text = build_super_csv([self._row()])
        reader = csv.DictReader(io.StringIO(csv_text.lstrip('\ufeff')))
        row = next(reader)
        assert row['FirstName'] == 'Al'
        assert row['LastName'] == 'Smith'

    def test_dates_iso_format(self):
        csv_text = build_super_csv([self._row()])
        reader = csv.DictReader(io.StringIO(csv_text.lstrip('\ufeff')))
        row = next(reader)
        assert row['PayPeriodStart'] == '2026-09-01'
        assert row['PayPeriodEnd'] == '2026-09-07'
        assert row['PaymentDate'] == '2026-09-09'

    def test_total_contribution_is_sgc_plus_sacrifice(self):
        csv_text = build_super_csv([self._row(
            sgc_amount=Decimal('228.00'),
            salary_sacrifice=Decimal('50.00'),
        )])
        reader = csv.DictReader(io.StringIO(csv_text.lstrip('\ufeff')))
        row = next(reader)
        assert row['SuperGuaranteeAmount'] == '228.00'
        assert row['SalarySacrifice'] == '50.00'
        assert row['TotalContribution'] == '278.00'

    def test_empty_date_of_birth(self):
        csv_text = build_super_csv([self._row()])
        reader = csv.DictReader(io.StringIO(csv_text.lstrip('\ufeff')))
        row = next(reader)
        assert row['DateOfBirth'] == ''

    def test_fund_name_default(self):
        """With no fund_name on the row, the configured fund name is used.

        Asserted against the SETTING, not a literal: the default fund is
        deployment configuration, so a fresh install seeds a placeholder
        while a real install seeds its own fund.
        """
        from app.models.system_setting import get_setting
        row = self._row()
        del row['fund_name']
        csv_text = build_super_csv([row])
        reader = csv.DictReader(io.StringIO(csv_text.lstrip('\ufeff')))
        assert next(reader)['FundName'] == get_setting('DEFAULT_FUND_NAME')

    def test_amounts_quantised(self):
        # .005 rounds to .00 with banker's rounding (default for Decimal);
        # .006 rounds up to .01. Same behaviour as the ABA builder.
        csv_text = build_super_csv([self._row(
            ote_amount=Decimal('1900.006'),
            sgc_amount=Decimal('228.006'),
        )])
        reader = csv.DictReader(io.StringIO(csv_text.lstrip('\ufeff')))
        row = next(reader)
        assert row['OrdinaryTimeEarnings'] == '1900.01'
        assert row['SuperGuaranteeAmount'] == '228.01'

    def test_one_row_per_input(self):
        csv_text = build_super_csv([
            self._row(),
            self._row(member_number='987654321', legal_name='Jane Doe'),
        ])
        reader = csv.DictReader(io.StringIO(csv_text.lstrip('\ufeff')))
        rows = list(reader)
        assert len(rows) == 2
        assert rows[0]['MemberNumber'] == '999999999'
        assert rows[1]['MemberNumber'] == '987654321'


# ---------------------------------------------------------------------------
# SAFF tests (added 2026-09-17 alongside the live SAFF builder)
# ---------------------------------------------------------------------------



class TestNormalizeTfn:
    def test_bare_digits_pass_through(self):
        assert _normalize_tfn('999999999') == '999999999'

    def test_spaces_stripped(self):
        assert _normalize_tfn('999 999 999') == '999999999'

    def test_hyphens_stripped(self):
        assert _normalize_tfn('999-999-999') == '999999999'

    def test_dots_stripped(self):
        assert _normalize_tfn('999.999.999') == '999999999'

    def test_empty_returns_empty(self):
        assert _normalize_tfn('') == ''
        assert _normalize_tfn(None) == ''

    def test_wrong_length_returns_empty(self):
        # Anything that doesn't collapse to exactly 9 digits is dropped
        # rather than emitted in a malformed shape. Caller can decide what
        # to do with the empty value (skip the row, error out, etc.).
        assert _normalize_tfn('12345') == ''
        assert _normalize_tfn('9999999990') == ''
        assert _normalize_tfn('abc') == ''


class TestBuildSaff:
    def _row(self, **overrides):
        """Build a single-row SAFF input. The function under test only
        cares about these keys; pass in overrides as needed."""
        from decimal import Decimal
        from datetime import date
        defaults = {
            'member_number': '999999999',
            'family_name': 'Smith',
            'given_name': 'Alice',
            'tfn': '999999999',
            'date_of_birth': date(2000, 1, 1),
            'pay_period_start': date(2026, 9, 8),
            'pay_period_end': date(2026, 9, 14),
            'transaction_date': date(2026, 9, 17),
            'payment_reference': 'PAY 17-09-26',
            'sgc_amount': Decimal('228.00'),
            'additional_amount': Decimal('0'),
            'voluntary_amount': Decimal('0'),
        }
        defaults.update(overrides)
        return defaults

    def test_file_structure_three_rows_before_data(self):
        """Header (VERSION/1.0/...), section line, column line, then data."""
        saff = build_saff_csv([self._row()])
        lines = saff.splitlines()
        assert len(lines) == 4
        assert lines[0].startswith('VERSION,1.0,NEGATIVES SUPPORTED,false,FILE ID,')
        # Section line should be present but ignored
        assert ',' in lines[1]
        # Column line — the known column names
        assert 'FundMemberNumber' in lines[2]
        assert 'EmployerSuperGuaranteeAmount' in lines[2]

    def test_data_row_carries_tfn_and_dob(self):
        saff = build_saff_csv([self._row()])
        rows = self._parse_data_rows(saff)
        assert len(rows) == 1
        row = rows[0]
        # TFN is the bare 9-digit string
        assert row['TFN'] == '999999999'
        # DOB is the ISO date
        assert row['DateOfBirth'] == '2000-01-01'

    def test_fund_abn_and_usi_are_populated(self):
        """The fund's ABN / USI / name come from system_settings.

        Asserted against the SETTINGS, not literals: those are deployment
        configuration, so they differ between a fresh install (placeholder)
        and a real one (the fund you actually pay into).
        """
        from app.models.system_setting import get_setting
        saff = build_saff_csv([self._row()])
        rows = self._parse_data_rows(saff)
        row = rows[0]
        assert row['PayeeABN'] == get_setting('DEFAULT_FUND_ABN')
        assert row['PayeeUSI'] == get_setting('DEFAULT_FUND_USI')
        assert row['PayeeOrganisationName'] == get_setting('DEFAULT_FUND_NAME')

    def test_spaces_in_tfn_input_are_normalised(self):
        """Pasting '999 999 999' into the export should still produce the
        bare 9-digit form expected by clearing houses."""
        saff = build_saff_csv([self._row(tfn='999 999 999')])
        rows = self._parse_data_rows(saff)
        assert rows[0]['TFN'] == '999999999'

    def test_missing_tfn_emits_empty(self):
        saff = build_saff_csv([self._row(tfn=None)])
        rows = self._parse_data_rows(saff)
        assert rows[0]['TFN'] == ''

    def test_missing_dob_emits_empty(self):
        saff = build_saff_csv([self._row(date_of_birth=None)])
        rows = self._parse_data_rows(saff)
        assert rows[0]['DateOfBirth'] == ''

    def test_total_contribution_sums_components(self):
        saff = build_saff_csv([self._row(
            sgc_amount='100.00',
            additional_amount='25.00',
            voluntary_amount='10.00',
        )])
        rows = self._parse_data_rows(saff)
        row = rows[0]
        assert row['EmployerSuperGuaranteeAmount'] == '100.00'
        assert row['EmployerAdditionalAmount'] == '25.00'
        assert row['MemberVoluntaryContributionAmount'] == '10.00'
        assert row['TotalContributionAmount'] == '135.00'

    def test_file_id_is_in_header_row(self):
        saff = build_saff_csv([self._row()], file_id='TEST-ID-12345')
        lines = saff.splitlines()
        assert 'FILE ID,TEST-ID-12345' in lines[0]

    def test_multiple_rows_produce_multiple_data_lines(self):
        from decimal import Decimal
        saff = build_saff_csv([
            self._row(member_number='111111111'),
            self._row(member_number='222222222'),
            self._row(member_number='333333333'),
        ])
        rows = self._parse_data_rows(saff)
        assert len(rows) == 3
        assert rows[0]['FundMemberNumber'] == '111111111'
        assert rows[1]['FundMemberNumber'] == '222222222'
        assert rows[2]['FundMemberNumber'] == '333333333'

    def test_amounts_quantised_to_two_dp(self):
        saff = build_saff_csv([self._row(sgc_amount='228.005')])
        rows = self._parse_data_rows(saff)
        # Decimal uses banker's rounding (.005 → .00)
        assert rows[0]['EmployerSuperGuaranteeAmount'] == '228.00'

    def test_pay_period_dates_iso_format(self):
        saff = build_saff_csv([self._row()])
        rows = self._parse_data_rows(saff)
        assert rows[0]['PayPeriodStartDate'] == '2026-09-08'
        assert rows[0]['PayPeriodEndDate'] == '2026-09-14'
        assert rows[0]['TransactionDate'] == '2026-09-17'

    def _parse_data_rows(self, saff_text):
        import csv as csvmod
        import io as iomod
        lines = saff_text.splitlines()
        # DictReader needs the header row (index 2) + the data rows (3+).
        # We skip the VERSION/section-header rows at indices 0 and 1.
        reader = csvmod.DictReader(iomod.StringIO('\n'.join(lines[2:])))
        return list(reader)
