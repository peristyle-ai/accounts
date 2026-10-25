"""Tests for PUT /api/payroll/super-payments/<id>.

Why this endpoint exists
------------------------
`super_payments.fund_member_snapshot` is captured when the row is created.
Before the employee's AustralianSuper member number was corrected from the
placeholder `123456789` to the real `123456789`, every pending super
payment was created carrying the placeholder as its snapshot. There was no
API route to correct a pending row, so the only ways forward were direct
SQL (which the accounts-repo rule forbids -- "fix the API, don't work
around a missing endpoint") or recreating the payment and losing its
remittance history.

This route lets a pending row be corrected. A paid or reconciled row is
immutable: it records what was actually remitted to the fund, so editing it
would falsify the audit trail.

Uses a fake super payment + monkeypatched session, so it needs no database.
"""

import os
import sys
import types
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


class FakeSuperPayment:
    """Minimal stand-in for app.models.super_payment.SuperPayment."""

    def __init__(self, status='pending'):
        self.id = 'sp-test-1'
        self.status = status
        self.fund_name_snapshot = 'AustralianSuper'
        self.fund_member_snapshot = '123456789'
        self.payment_reference = None
        self.remittance_date = None
        self.amount = None
        self.notes = None

    def to_dict(self):
        return {
            'id': self.id,
            'status': self.status,
            'fund_name_snapshot': self.fund_name_snapshot,
            'fund_member_snapshot': self.fund_member_snapshot,
            'payment_reference': self.payment_reference,
            'remittance_date': self.remittance_date,
            'amount': self.amount,
            'notes': self.notes,
        }


class FakeSession:
    def __init__(self):
        self.commits = 0

    def commit(self):
        self.commits += 1


@pytest.fixture
def payroll_mod(monkeypatch):
    """Import app.api.payroll with a fake db session + log_activity."""
    from app.api import payroll as pm

    session = FakeSession()
    monkeypatch.setattr(pm.db, 'session', session, raising=False)
    monkeypatch.setattr(pm, 'log_activity', lambda **kw: None, raising=False)
    return pm, session


def _call(monkeypatch, pm, session, payload, status='pending'):
    """Invoke the endpoint with a fake super payment, return (sp, body, session)."""
    sp = FakeSuperPayment(status=status)
    monkeypatch.setattr(pm, '_get_owned_super_payment',
                        lambda uid, pid: sp, raising=False)
    monkeypatch.setattr(pm, 'validate_date_string', lambda v, **kw: v,
                        raising=False)
    monkeypatch.setattr(pm, 'validate_decimal', lambda v, **kw: v,
                        raising=False)

    from flask import Flask
    app = Flask(__name__)
    app.config['TESTING'] = True

    with app.test_request_context(
        '/api/payroll/super-payments/sp-test-1',
        method='PUT', json=payload,
    ):
        from flask import request
        request.current_user = types.SimpleNamespace(id=69)
        view = getattr(pm.update_super_payment, '__wrapped__',
                       pm.update_super_payment)
        response, status = view('sp-test-1')
    return sp, (response, status), session


def test_corrects_fund_member_snapshot_on_pending_payment(monkeypatch, payroll_mod):
    """The core case: a pending row gets the corrected member number."""
    pm, session = payroll_mod
    sp, body, sess = _call(monkeypatch, pm, session,
                            {'fund_member_snapshot': '123456789'})
    assert sp.fund_member_snapshot == '123456789'
    assert sess.commits >= 1, \
        'the correction must be committed, not just set on the ORM object'


def test_rejects_edit_of_paid_payment(monkeypatch, payroll_mod):
    """A paid row records what was actually remitted -- it is immutable."""
    pm, session = payroll_mod
    sp, body, sess = _call(monkeypatch, pm, session,
                            {'fund_member_snapshot': '123456789'},
                            status='paid')
    assert body[1] == 409
    assert sp.fund_member_snapshot == '123456789'
    assert sess.commits == 0


def test_rejects_edit_of_reconciled_payment(monkeypatch, payroll_mod):
    pm, session = payroll_mod
    sp, body, sess = _call(monkeypatch, pm, session,
                            {'fund_member_snapshot': '123456789'},
                            status='reconciled')
    assert body[1] == 409
    assert sess.commits == 0


def test_rejects_empty_payload(monkeypatch, payroll_mod):
    pm, session = payroll_mod
    sp, body, sess = _call(monkeypatch, pm, session, {})
    assert body[1] == 400
    assert sess.commits == 0


def test_rejects_overlong_member_snapshot(monkeypatch, payroll_mod):
    pm, session = payroll_mod
    sp, body, sess = _call(monkeypatch, pm, session,
                            {'fund_member_snapshot': 'x' * 51})
    assert body[1] == 400
    assert sess.commits == 0


def test_member_snapshot_can_be_cleared(monkeypatch, payroll_mod):
    """Passing null should clear the snapshot rather than store the string 'None'."""
    pm, session = payroll_mod
    sp, body, sess = _call(monkeypatch, pm, session,
                            {'fund_member_snapshot': None})
    assert sp.fund_member_snapshot is None
    assert sess.commits >= 1


def test_updates_notes(monkeypatch, payroll_mod):
    pm, session = payroll_mod
    sp, body, sess = _call(monkeypatch, pm, session,
                            {'notes': 'member number corrected 2026-10-22'})
    assert sp.notes == 'member number corrected 2026-10-22'
    assert sess.commits >= 1
