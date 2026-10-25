"""Tests for the send-payslip endpoint's RESPONSE CONTRACT.

The bug these guard against
---------------------------
Two separate defects made a failed payslip send look like a success:

1. The mail-server settings were resolved INSIDE the per-recipient loop,
   so a configuration error was recorded as a per-recipient delivery
   failure. The endpoint still answered HTTP 200 with the message
   "Payslip sent to N recipient(s)" -- N being the number of TARGETS,
   not the number of deliveries that actually succeeded. An operator
   reading the log could not distinguish "the payslip went out" from
   "the mail server was misconfigured and nothing went anywhere".

2. There was no way to tell a partial failure from a total one, because
   the response body carried no counts.

The endpoint now:
  - resolves the mail settings ONCE, up front, and answers 503 with a
    message naming the variable to set if they are not configured;
  - counts what actually happened (sent_count / failed_count);
  - answers 502 when any delivery failed, so a caller cannot mistake
    failure for success.

These tests exercise the endpoint with the mail server stubbed out. The
PDF is generated for real (the endpoint needs it) and the delivery rows
are cleaned up afterwards.
"""

import os
import sys
from datetime import date, timedelta
from pathlib import Path

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

from app import create_app  # noqa: E402
from app.models import db  # noqa: E402
from app.models.employee import Employee  # noqa: E402
from app.models.pay_event import PayEvent  # noqa: E402
from app.models.payslip_delivery import PayslipDelivery  # noqa: E402
from app.models.user import User  # noqa: E402
from tests.conftest import api_key_plain_for, business_email  # noqa: E402


@pytest.fixture
def app():
    app = create_app()
    app.config['TESTING'] = True
    with app.app_context():
        yield app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def business_user_id(app):
    u = User.query.filter_by(email=business_email()).first()
    if u is None:
        pytest.skip('business user not found (set TEST_BUSINESS_EMAIL)')
    return u.id


@pytest.fixture
def auth_headers(app, business_user_id):
    key = api_key_plain_for(business_user_id)
    if not key:
        pytest.skip('no API key on the business user')
    return {'X-API-Key': key}


@pytest.fixture
def pay_event(app, business_user_id):
    """A finalized pay event with one employee, cleaned up afterwards."""
    from tests.conftest import restore_user  # noqa: F401  (documents intent)

    legal = f'TEST_PAYSLIP_CONTRACT_{os.getpid()}'
    with app.app_context():
        emp = Employee(
            user_id=business_user_id,
            legal_name=legal,
            preferred_name='Test',
            email_work='employee.work@example.com',
            email_personal='employee.personal@example.com',
            default_gross_amount=1900.00,
        )
        # tfn is stored as Fernet ciphertext; the plaintext kwarg would
        # write undecryptable data into the column.
        emp.tfn_plain = '123456789'
        db.session.add(emp)
        db.session.flush()
        emp_id = emp.id

        pe = PayEvent(
            user_id=business_user_id,
            employee_id=emp_id,
            pay_period_start=date(2099, 1, 1),
            pay_period_end=date(2099, 1, 7),
            payment_date=date(2099, 1, 8),
            gross_amount=1900.00,
            payg_tax_amount=391.87,
            net_amount=1508.13,
            super_payable_amount=228.00,
            bank_reference='TESTCONTRACT',
            pay_frequency='weekly',
            status='finalized',
        )
        db.session.add(pe)
        db.session.commit()
        pe_id = pe.id

    yield pe_id

    with app.app_context():
        PayslipDelivery.query.filter_by(pay_event_id=pe_id).delete(
            synchronize_session=False)
        PayEvent.query.filter_by(id=pe_id).delete(synchronize_session=False)
        db.session.flush()
        Employee.query.filter_by(id=emp_id).delete(synchronize_session=False)
        db.session.commit()


class _FakeResponse:
    def __init__(self, ok, payload=None, status_code=200, text=''):
        self._ok = ok
        self._payload = payload or {}
        self.status_code = status_code
        self.text = text
        self.content = text.encode() if isinstance(text, str) else text

    @property
    def ok(self):
        return self._ok

    def json(self):
        return self._payload


@pytest.fixture
def stub_mail(monkeypatch):
    """Stub out `requests` as used by the send-payslip endpoint.

    Returns a controller so a test can choose whether the send succeeds.
    """
    import app.api.payroll as payroll_mod

    state = {'ok': True, 'calls': [], 'auth_ok': True}

    class _FakeRequests:
        @staticmethod
        def post(url, json=None, timeout=None, verify=None, headers=None):
            state['calls'].append({'url': url, 'json': json})
            if url.endswith('/auth/login'):
                if state['auth_ok']:
                    return _FakeResponse(True, {'token': 'test-token'})
                return _FakeResponse(False, status_code=401, text='denied')
            if state['ok']:
                return _FakeResponse(True, {'email_id': 'test-email-id'})
            return _FakeResponse(False, status_code=502, text='upstream down')

    monkeypatch.setitem(__import__('sys').modules, 'requests', _FakeRequests)
    return state


def _configure_mail(app, api='https://mail.corp-host:5003'):
    """Put a valid mail-server configuration into the app config."""
    app.config['MAIL_SERVER_API'] = api
    app.config['MAIL_SERVER_USER'] = 'mailbox@example.test'
    app.config['MAIL_SERVER_PASSWORD'] = 'secret'
    app.config['MAIL_SERVER_CA_BUNDLE'] = ''


class TestUnconfiguredMailServer:
    """A configuration error must NOT look like a send."""

    def test_missing_api_returns_503(self, app, client, auth_headers,
                                      pay_event, stub_mail, monkeypatch):
        app.config.pop('MAIL_SERVER_API', None)
        monkeypatch.delenv('MAIL_SERVER_API', raising=False)
        r = client.post(f'/api/payroll/pay-events/{pay_event}/send-payslip',
                        headers=auth_headers)
        assert r.status_code == 503, r.get_json()
        body = r.get_json()
        assert 'MAIL_SERVER_API' in body['error']

    def test_placeholder_api_returns_503(self, app, client, auth_headers,
                                        pay_event, stub_mail):
        _configure_mail(app, api='https://mail.example.com:5003')
        r = client.post(f'/api/payroll/pay-events/{pay_event}/send-payslip',
                        headers=auth_headers)
        assert r.status_code == 503, r.get_json()
        assert 'mail.example.com' in r.get_json()['error']

    def test_no_delivery_row_is_created(self, app, client, auth_headers,
                                        pay_event, stub_mail, monkeypatch):
        """No delivery was attempted, so no row may claim one was."""
        app.config.pop('MAIL_SERVER_API', None)
        monkeypatch.delenv('MAIL_SERVER_API', raising=False)
        client.post(f'/api/payroll/pay-events/{pay_event}/send-payslip',
                    headers=auth_headers)
        with app.app_context():
            rows = PayslipDelivery.query.filter_by(
                pay_event_id=pay_event).count()
        assert rows == 0


class TestSuccessfulSend:
    def test_returns_200_with_counts(self, app, client, auth_headers,
                                     pay_event, stub_mail):
        _configure_mail(app)
        r = client.post(f'/api/payroll/pay-events/{pay_event}/send-payslip',
                        headers=auth_headers)
        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        assert body['sent_count'] == 1
        assert body['failed_count'] == 0
        assert 'sent' in body['message']

    def test_delivery_row_is_sent(self, app, client, auth_headers,
                                  pay_event, stub_mail):
        _configure_mail(app)
        client.post(f'/api/payroll/pay-events/{pay_event}/send-payslip',
                    headers=auth_headers)
        with app.app_context():
            row = PayslipDelivery.query.filter_by(
                pay_event_id=pay_event).first()
        assert row is not None
        assert row.delivery_status == 'sent'
        assert row.delivery_id == 'test-email-id'


class TestFailedSend:
    """A failed delivery must not be reported as a success."""

    def test_total_failure_returns_502(self, app, client, auth_headers,
                                       pay_event, stub_mail):
        _configure_mail(app)
        stub_mail['ok'] = False
        r = client.post(f'/api/payroll/pay-events/{pay_event}/send-payslip',
                        headers=auth_headers)
        assert r.status_code == 502, r.get_json()
        body = r.get_json()
        assert body['sent_count'] == 0
        assert body['failed_count'] == 1
        assert 'NOT sent' in body['error']

    def test_failed_delivery_is_recorded_as_failed(self, app, client,
                                                   auth_headers, pay_event,
                                                   stub_mail):
        _configure_mail(app)
        stub_mail['ok'] = False
        client.post(f'/api/payroll/pay-events/{pay_event}/send-payslip',
                    headers=auth_headers)
        with app.app_context():
            row = PayslipDelivery.query.filter_by(
                pay_event_id=pay_event).first()
        assert row is not None
        assert row.delivery_status == 'failed'
        assert row.error_message

    def test_errors_are_listed(self, app, client, auth_headers, pay_event,
                               stub_mail):
        _configure_mail(app)
        stub_mail['ok'] = False
        r = client.post(f'/api/payroll/pay-events/{pay_event}/send-payslip',
                        headers=auth_headers)
        errors = r.get_json()['errors']
        assert len(errors) == 1
        assert errors[0]['delivery_status'] == 'failed'
        assert errors[0]['error_message']
