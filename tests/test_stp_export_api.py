"""Endpoint tests for the Single Touch Payroll (STP) export.

    GET /api/exports/payroll/one-touch-payroll

Why these tests exist
---------------------
`tests/test_stp_csv.py` covers the BUILDER (column order, date formats,
pay-run grouping). It never exercised the ENDPOINT -- so the things that
actually broke in production were untested:

  * The endpoint returning a single CSV vs a ZIP depending on how many
    pay runs the requested range covers. The ATO portal rejects a file
    whose "Pay or update date" differs between records, which is why
    the endpoint groups by pay run and zips the rest.
  * The `status` default of `paid,finalized` (drafts excluded).
  * The date-range / employee_id filters.
  * User scoping -- one user must never see another user's pay events.
  * Pay events whose employee has no TFN on file (skipped, with a
    reason) vs an export where EVERY event is skipped (400, not 404).
  * The activity log row the export writes.

Safety
------
The suite runs against the LIVE production Postgres (see conftest.py).
Every fixture here creates throwaway rows and deletes them on teardown,
including on failure. All test pay runs are dated 2099-01/02 so they can
never collide with a real pay run, and every query is bounded by an
explicit date range.
"""

import csv
import io
import sys
import zipfile
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import create_app
from app.shared.stp_csv import STP_CSV_COLUMNS


ENDPOINT = '/api/exports/payroll/one-touch-payroll'

# Far-future pay runs. Nothing real will ever land on these dates, so a
# test export can never pick up (or be picked up by) production data.
RUN_A = date(2099, 1, 8)     # paid
RUN_B = date(2099, 1, 15)    # finalized
RUN_C = date(2099, 1, 22)    # draft  -- must be excluded by default
RUN_D = date(2099, 2, 5)     # only event's employee has no TFN

RANGE_ALL = 'from_date=2099-01-01&to_date=2099-12-31'

TEST_TFN = '123456789'
TEST_DOB = date(1990, 5, 17)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def app():
    app = create_app()
    app.config['TESTING'] = True
    with app.app_context():
        yield app


@pytest.fixture
def client(app):
    return app.test_client()


def _api_key_for(user_id):
    """The PLAINTEXT API key for a user, as the auth decorator expects it.

    The X-API-Key header must carry the plaintext. The stored column is
    Fernet ciphertext for rows written through the setter, and legacy
    plaintext for rows seeded before the encryption refactor;
    `api_key_plain` is tolerant of both.
    """
    from tests.conftest import api_key_plain_for
    return api_key_plain_for(user_id)


@pytest.fixture
def business_user_id(app):
    """The business user -- the one that owns the pay events."""
    from app.models.user import User
    from tests.conftest import business_email
    u = User.query.filter_by(email=business_email()).first()
    if u is None:
        pytest.skip('business user not found (set TEST_BUSINESS_EMAIL)')
    return u.id


@pytest.fixture
def auth_headers(app, business_user_id):
    key = _api_key_for(business_user_id)
    if not key:
        pytest.skip('no API key on the business user')
    return {'X-API-Key': key}


@pytest.fixture
def other_user_headers(app, business_user_id):
    """A different user's key -- used to prove user scoping."""
    from app.models.user import User
    other = User.query.filter(User.id != business_user_id).first()
    if other is None:
        pytest.skip('no second user to scope against')
    key = _api_key_for(other.id)
    if not key:
        pytest.skip('second user has no API key')
    return {'X-API-Key': key}


def _make_employee(user_id, legal_name, with_tfn=True, payroll_number=99):
    from app.models.employee import Employee
    emp = Employee(
        user_id=user_id,
        legal_name=legal_name,
        preferred_name='Zoe',
        middle_name='Q',
        address_line1='1 Test Street',
        address_2='',
        city='Brisbane',
        state='QLD',
        postcode='4000',
        country='Australia',
        country_code='au',
        phone='0400000000',
        email_personal='zoe@example.invalid',
        start_date=date(2020, 1, 1),
        hired_date=date(2020, 1, 1),
        employment_status='active',
        pay_frequency='weekly',
        super_fund_name='AustralianSuper',
        super_fund_member_no='999999999',
        payroll_number=payroll_number,
        tax_treatment_code='RTXXXX',
        basis_of_employment_code='F',
    )
    if with_tfn:
        emp.tfn_plain = TEST_TFN
        emp.date_of_birth_plain = TEST_DOB
    return emp


def _make_pay_event(user_id, employee_id, payment_date, status,
                    gross='1900.00', payg='391.87'):
    from app.models.pay_event import PayEvent
    return PayEvent(
        user_id=user_id,
        employee_id=employee_id,
        payment_date=payment_date,
        pay_period_start=payment_date - __import__('datetime').timedelta(days=7),
        pay_period_end=payment_date - __import__('datetime').timedelta(days=1),
        pay_frequency='weekly',
        gross_amount=Decimal(gross),
        payg_tax_amount=Decimal(payg),
        net_amount=Decimal(gross) - Decimal(payg),
        super_ote_amount=Decimal(gross),
        super_payable_amount=Decimal('228.00'),
        status=status,
        # bank_reference is unique per (user_id, employee_id) --
        # uq_pay_event_bank_ref -- so derive it from the pay run date.
        bank_reference=f'ZZTEST-{payment_date.isoformat()}',
    )


@pytest.fixture
def stp_dataset(app, business_user_id):
    """Throwaway employees + pay events, deleted on teardown.

    Layout:
        emp_with_tfn    -- events on RUN_A (paid), RUN_B (finalized),
                           RUN_C (draft)
        emp_no_tfn      -- event on RUN_A (skipped) and on RUN_D
                           (the only event in its pay run -> 400 case)
    """
    from app.models import db
    from app.models.activity_log import ActivityLog
    from app.models.employee import Employee
    from app.models.pay_event import PayEvent

    emp_ok = _make_employee(business_user_id, 'ZZ STP Test Employee', with_tfn=True, payroll_number=9901)
    emp_no_tfn = _make_employee(business_user_id, 'ZZ STP No Tfn Employee', with_tfn=False, payroll_number=9902)
    db.session.add_all([emp_ok, emp_no_tfn])
    db.session.flush()

    events = [
        _make_pay_event(business_user_id, emp_ok.id, RUN_A, 'paid'),
        _make_pay_event(business_user_id, emp_ok.id, RUN_B, 'finalized'),
        _make_pay_event(business_user_id, emp_ok.id, RUN_C, 'draft'),
        _make_pay_event(business_user_id, emp_no_tfn.id, RUN_A, 'paid'),
        _make_pay_event(business_user_id, emp_no_tfn.id, RUN_D, 'paid'),
    ]
    db.session.add_all(events)
    db.session.commit()

    ids = {'emp_ok': emp_ok.id, 'emp_no_tfn': emp_no_tfn.id}
    event_ids = [e.id for e in events]

    try:
        yield ids
    finally:
        # Pay events first (FK to employees), then the employees, then any
        # activity log rows the export wrote.
        ActivityLog.query.filter(
            ActivityLog.action == 'export_payroll_one-touch-payroll',
            ActivityLog.record_id.like('2099%'),
        ).delete(synchronize_session=False)
        PayEvent.query.filter(PayEvent.id.in_(event_ids)).delete(synchronize_session=False)
        Employee.query.filter(Employee.id.in_(ids.values())).delete(synchronize_session=False)
        db.session.commit()


def _parse_csv(raw_bytes):
    """Parse a CSV body into (header, rows). Asserts the shape we expect."""
    assert not raw_bytes.startswith(b'\xef\xbb\xbf'), 'CSV must not carry a UTF-8 BOM'
    text = raw_bytes.decode('utf-8')
    assert '\r\n' in text, 'CSV must use CRLF line endings'
    parsed = list(csv.reader(io.StringIO(text, newline='')))
    return parsed[0], parsed[1:]


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

class TestAuth:
    def test_requires_an_api_key(self, client, stp_dataset):
        r = client.get(f'{ENDPOINT}?{RANGE_ALL}')
        assert r.status_code == 401, r.data
        assert 'error' in r.get_json()

    def test_rejects_an_unknown_api_key(self, client, stp_dataset):
        r = client.get(f'{ENDPOINT}?{RANGE_ALL}',
                       headers={'X-API-Key': 'not-a-real-key'})
        assert r.status_code == 401, r.data

    def test_accepts_a_valid_api_key(self, client, auth_headers, stp_dataset):
        r = client.get(f'{ENDPOINT}?{RANGE_ALL}', headers=auth_headers)
        assert r.status_code == 200, r.data


# ---------------------------------------------------------------------------
# Parameter validation
# ---------------------------------------------------------------------------

class TestParameterValidation:
    def test_malformed_from_date_is_400(self, client, auth_headers, stp_dataset):
        r = client.get(f'{ENDPOINT}?from_date=not-a-date', headers=auth_headers)
        assert r.status_code == 400, r.data

    def test_malformed_to_date_is_400(self, client, auth_headers, stp_dataset):
        r = client.get(f'{ENDPOINT}?to_date=01/01/2099', headers=auth_headers)
        assert r.status_code == 400, r.data

    def test_invalid_status_is_400(self, client, auth_headers, stp_dataset):
        r = client.get(f'{ENDPOINT}?{RANGE_ALL}&status=bogus', headers=auth_headers)
        assert r.status_code == 400, r.data

    def test_empty_status_falls_back_to_the_default(self, client, auth_headers, stp_dataset):
        # ?status= parses to no statuses, which must fall back to
        # paid,finalized -- NOT to an empty filter that would match
        # everything including drafts.
        r = client.get(f'{ENDPOINT}?{RANGE_ALL}&status=', headers=auth_headers)
        assert r.status_code == 200, r.data
        header, rows = _parse_csv(r.data) if r.mimetype == 'text/csv' else (None, None)
        if header is not None:
            pay_dates = {row[header.index('Pay or update date')] for row in rows}
            assert '22/01/2099' not in pay_dates, 'draft pay run leaked into the export'


# ---------------------------------------------------------------------------
# Single pay run -> CSV
# ---------------------------------------------------------------------------

class TestSinglePayRunReturnsCsv:
    def _get(self, client, headers):
        r = client.get(f'{ENDPOINT}?from_date=2099-01-08&to_date=2099-01-08',
                       headers=headers)
        assert r.status_code == 200, r.data
        return r

    def test_content_type_is_csv(self, client, auth_headers, stp_dataset):
        r = self._get(client, auth_headers)
        assert r.mimetype == 'text/csv'
        # Flask appends the charset itself for text/* mimetypes, so the
        # header must carry exactly one -- "text/csv; charset=utf-8;
        # charset=utf-8" was what a hardcoded charset produced.
        content_type = r.headers.get('Content-Type', '')
        assert content_type == 'text/csv; charset=utf-8', content_type
        assert content_type.count('charset') == 1, content_type

    def test_filename_names_the_pay_run(self, client, auth_headers, stp_dataset):
        r = self._get(client, auth_headers)
        disposition = r.headers.get('Content-Disposition', '')
        assert 'stp-payrun-2099-01-08.csv' in disposition, disposition

    def test_has_93_columns(self, client, auth_headers, stp_dataset):
        r = self._get(client, auth_headers)
        header, rows = _parse_csv(r.data)
        assert len(header) == 93
        assert all(len(row) == 93 for row in rows)

    def test_header_matches_the_builder_constant(self, client, auth_headers, stp_dataset):
        r = self._get(client, auth_headers)
        header, _ = _parse_csv(r.data)
        assert tuple(header) == STP_CSV_COLUMNS

    def test_only_the_usable_employee_is_included(self, client, auth_headers, stp_dataset):
        # The pay run has two events: one whose employee has a TFN, one
        # whose employee does not. Only the former is emitted.
        r = self._get(client, auth_headers)
        header, rows = _parse_csv(r.data)
        assert len(rows) == 1
        tfn_idx = header.index('Employee TFN')
        assert rows[0][tfn_idx] == TEST_TFN
        family_idx = header.index('Family name')
        assert rows[0][family_idx] == 'Employee'

    def test_identity_columns_are_populated(self, client, auth_headers, stp_dataset):
        r = self._get(client, auth_headers)
        header, rows = _parse_csv(r.data)
        row = dict(zip(header, rows[0]))
        assert row['Given name'] == 'Zoe'
        assert row['Family name'] == 'Employee'
        assert row['Middle name'] == 'Q'
        assert row['Date of birth'] == '17/05/1990'
        assert row['Payroll number'] == '9901'
        assert row['Tax treatment code'] == 'RTXXXX'
        assert row['Basis of employment code'] == 'F'

    def test_amount_columns_match_the_pay_event(self, client, auth_headers, stp_dataset):
        r = self._get(client, auth_headers)
        header, rows = _parse_csv(r.data)
        row = dict(zip(header, rows[0]))
        assert row['Employee gross pay'] == '1900'
        assert row['Employee tax'] == '391.87'
        assert row['Super guarantee amount'] == '228'
        assert row['SG earnings amount'] == '1900'

    def test_dates_are_australian_format(self, client, auth_headers, stp_dataset):
        r = self._get(client, auth_headers)
        header, rows = _parse_csv(r.data)
        row = dict(zip(header, rows[0]))
        # Pay or update date == the pay run's payment date.
        assert row['Pay or update date'] == '8/01/2099'
        assert row['Payrun date time'] == '8/01/2099 00:00'
        assert row['Pay period start date'] == '1/01/2099'
        assert row['Pay period end date'] == '7/01/2099'

    def test_employer_constants_come_from_config(self, client, auth_headers, stp_dataset):
        # Compared against the LOADED CONFIG rather than hardcoded
        # literals. That proves the endpoint maps each employer constant
        # onto the right CSV column -- which is what this test is for --
        # without copying real business identifiers (the ABN, the BMS ID)
        # into a second file that then has to be scrubbed again when the
        # repo is opened up. The builder's own test
        # (test_load_employer_from_config_reads_stp_block) covers the
        # config -> employer dict step.
        from app.shared.stp_csv import load_employer_from_config
        employer = load_employer_from_config()
        r = self._get(client, auth_headers)
        header, rows = _parse_csv(r.data)
        row = dict(zip(header, rows[0]))
        assert row['Entity ABN'] == employer['entity_abn']
        assert row['Product ID'] == employer['product_id']
        assert row['Branch ID'] == employer['branch_id']
        assert row['BMS ID'] == employer['bms_id']
        assert row['Income stream code'] == employer['income_stream_code']
        assert row['Income stream country code'] == employer['income_stream_country_code']
        assert row['Final EOY pay indicator'] == employer['final_eoy_pay_indicator']

    def test_pay_or_update_date_is_identical_on_every_record(self, client, auth_headers,
                                                              stp_dataset):
        # The portal's first validation rule. One file == one pay run.
        r = self._get(client, auth_headers)
        header, rows = _parse_csv(r.data)
        idx = header.index('Pay or update date')
        assert len({row[idx] for row in rows}) == 1


# ---------------------------------------------------------------------------
# Multiple pay runs -> ZIP
# ---------------------------------------------------------------------------

class TestMultiplePayRunsReturnZip:
    def _get(self, client, headers):
        r = client.get(f'{ENDPOINT}?{RANGE_ALL}', headers=headers)
        assert r.status_code == 200, r.data
        return r

    def test_content_type_is_zip(self, client, auth_headers, stp_dataset):
        r = self._get(client, auth_headers)
        assert r.mimetype == 'application/zip'

    def test_zip_holds_one_csv_per_pay_run(self, client, auth_headers, stp_dataset):
        r = self._get(client, auth_headers)
        with zipfile.ZipFile(io.BytesIO(r.data)) as zf:
            names = sorted(zf.namelist())
        assert names == ['stp-payrun-2099-01-08.csv', 'stp-payrun-2099-01-15.csv']

    def test_each_csv_in_the_zip_is_self_consistent(self, client, auth_headers, stp_dataset):
        # Each file must carry ONE pay-or-update date -- the portal
        # rejects a file whose records disagree. This is the exact rule
        # that rejected our first combined file.
        r = self._get(client, auth_headers)
        with zipfile.ZipFile(io.BytesIO(r.data)) as zf:
            for name in zf.namelist():
                header, rows = _parse_csv(zf.read(name))
                idx = header.index('Pay or update date')
                values = {row[idx] for row in rows}
                assert len(values) == 1, f'{name} has mixed pay dates: {values}'

    def test_different_pay_runs_get_different_dates(self, client, auth_headers, stp_dataset):
        r = self._get(client, auth_headers)
        seen = set()
        with zipfile.ZipFile(io.BytesIO(r.data)) as zf:
            for name in zf.namelist():
                header, rows = _parse_csv(zf.read(name))
                idx = header.index('Pay or update date')
                seen.update(row[idx] for row in rows)
        assert seen == {'8/01/2099', '15/01/2099'}

    def test_each_csv_has_93_columns(self, client, auth_headers, stp_dataset):
        r = self._get(client, auth_headers)
        with zipfile.ZipFile(io.BytesIO(r.data)) as zf:
            for name in zf.namelist():
                header, rows = _parse_csv(zf.read(name))
                assert len(header) == 93, name
                assert all(len(row) == 93 for row in rows), name

    def test_zip_filename_is_labelled_with_the_range(self, client, auth_headers, stp_dataset):
        r = self._get(client, auth_headers)
        disposition = r.headers.get('Content-Disposition', '')
        assert 'stp-payruns-2099-01-01_to_2099-12-31.zip' in disposition, disposition


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------

class TestFiltering:
    def test_default_status_excludes_drafts(self, client, auth_headers, stp_dataset):
        # RUN_C is a draft. Querying its date with the default status
        # must find nothing.
        r = client.get(f'{ENDPOINT}?from_date=2099-01-22&to_date=2099-01-22',
                       headers=auth_headers)
        assert r.status_code == 404, r.data
        assert 'error' in r.get_json()

    def test_status_draft_includes_drafts(self, client, auth_headers, stp_dataset):
        r = client.get(f'{ENDPOINT}?from_date=2099-01-22&to_date=2099-01-22&status=draft',
                       headers=auth_headers)
        assert r.status_code == 200, r.data
        header, rows = _parse_csv(r.data)
        assert len(rows) == 1

    def test_date_range_is_inclusive_at_both_ends(self, client, auth_headers, stp_dataset):
        # RUN_A (8 Jan) and RUN_B (15 Jan); a range covering exactly
        # 8 Jan..15 Jan must include both.
        r = client.get(f'{ENDPOINT}?from_date=2099-01-08&to_date=2099-01-15',
                       headers=auth_headers)
        assert r.status_code == 200, r.data
        assert r.mimetype == 'application/zip'

    def test_date_range_excludes_pay_runs_outside_it(self, client, auth_headers, stp_dataset):
        r = client.get(f'{ENDPOINT}?from_date=2099-01-08&to_date=2099-01-08',
                       headers=auth_headers)
        assert r.status_code == 200, r.data
        header, rows = _parse_csv(r.data)
        idx = header.index('Pay or update date')
        assert {row[idx] for row in rows} == {'8/01/2099'}

    def test_employee_id_filter_selects_one_employee(self, client, auth_headers, stp_dataset):
        r = client.get(
            f'{ENDPOINT}?{RANGE_ALL}&employee_id={stp_dataset["emp_ok"]}',
            headers=auth_headers,
        )
        assert r.status_code == 200, r.data
        with zipfile.ZipFile(io.BytesIO(r.data)) as zf:
            for name in zf.namelist():
                header, rows = _parse_csv(zf.read(name))
                idx = header.index('Employee TFN')
                assert {row[idx] for row in rows} == {TEST_TFN}

    def test_employee_id_filter_for_an_employee_with_no_tfn_is_400(self, client,
                                                                   auth_headers,
                                                                   stp_dataset):
        # Events matched, but none of them had usable employee details.
        r = client.get(
            f'{ENDPOINT}?{RANGE_ALL}&employee_id={stp_dataset["emp_no_tfn"]}',
            headers=auth_headers,
        )
        assert r.status_code == 400, r.data
        body = r.get_json()
        assert body.get('skipped'), body


# ---------------------------------------------------------------------------
# Empty results and skipped employees
# ---------------------------------------------------------------------------

class TestEmptyAndSkipped:
    def test_no_matching_pay_events_is_404(self, client, auth_headers, stp_dataset):
        r = client.get(f'{ENDPOINT}?from_date=2098-01-01&to_date=2098-12-31',
                       headers=auth_headers)
        assert r.status_code == 404, r.data
        body = r.get_json()
        assert 'error' in body
        assert 'No pay events match' in body['error']

    def test_all_events_skipped_is_400_not_404(self, client, auth_headers, stp_dataset):
        # RUN_D's only employee has no TFN. Events were found, but no
        # usable rows -- that is a 400 (with the reason), not a 404.
        r = client.get(f'{ENDPOINT}?from_date=2099-02-05&to_date=2099-02-05',
                       headers=auth_headers)
        assert r.status_code == 400, r.data
        body = r.get_json()
        assert 'skipped' in body
        assert body['skipped'], body
        assert 'TFN' in body['skipped'][0]['reason']

    def test_404_error_body_mentions_the_status_filter(self, client, auth_headers,
                                                        stp_dataset):
        r = client.get(f'{ENDPOINT}?from_date=2098-01-01&to_date=2098-12-31',
                       headers=auth_headers)
        body = r.get_json()
        assert 'paid' in body['error'] and 'finalized' in body['error']


# ---------------------------------------------------------------------------
# User scoping
# ---------------------------------------------------------------------------

class TestUserScoping:
    def test_another_user_cannot_see_these_pay_events(self, client, other_user_headers,
                                                       stp_dataset):
        # The events belong to the business user. A different user's key
        # must not see them.
        r = client.get(f'{ENDPOINT}?{RANGE_ALL}', headers=other_user_headers)
        assert r.status_code == 404, r.data
        assert 'No pay events match' in r.get_json()['error']

    def test_user_id_filter_cannot_be_bypassed_with_a_bad_status(self, client,
                                                                 other_user_headers,
                                                                 stp_dataset):
        r = client.get(f'{ENDPOINT}?{RANGE_ALL}&status=paid,finalized,draft,cancelled',
                       headers=other_user_headers)
        assert r.status_code == 404, r.data


# ---------------------------------------------------------------------------
# Activity log
# ---------------------------------------------------------------------------

class TestActivityLog:
    def test_export_writes_an_activity_log_row(self, app, client, auth_headers,
                                                stp_dataset):
        from app.models.activity_log import ActivityLog
        with app.app_context():
            before = ActivityLog.query.filter(
                ActivityLog.action == 'export_payroll_one-touch-payroll',
                ActivityLog.record_id == '2099-01-01_to_2099-12-31',
            ).count()

        r = client.get(f'{ENDPOINT}?{RANGE_ALL}', headers=auth_headers)
        assert r.status_code == 200, r.data

        with app.app_context():
            after = ActivityLog.query.filter(
                ActivityLog.action == 'export_payroll_one-touch-payroll',
                ActivityLog.record_id == '2099-01-01_to_2099-12-31',
            ).all()
            assert len(after) == before + 1
            values = after[-1].new_values
            assert values['payrun_count'] == 2
            assert values['row_count'] == 2
            assert values['skipped'] == 2
            assert values['pay_event_count'] == 4
            assert values['total_gross_cents'] == 380000
            assert values['source'] == 'api'
