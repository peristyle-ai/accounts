"""Regression tests for POST /api/payroll/pay-events/<id>/mark-paid.

The bug these guard against
---------------------------
`mark_pay_event_paid` used to run `db.session.commit()` only inside the
`if pe.status != 'paid'` branch. Calling the endpoint on an event that was
ALREADY paid therefore set the mutable fields (super_paid_amount,
super_paid_date, payment_reference, notes) on the ORM object, echoed them
back in the JSON response, and then discarded them -- the database was never
written. The response said "success" while the data was silently lost.

The endpoint now tracks a `changed` flag and commits whenever any field
actually changed, regardless of the status transition.

These tests use a fake pay event + monkeypatched session so they need no
database.
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


class FakePayEvent:
    """Minimal stand-in for app.models.pay_event.PayEvent."""

    def __init__(self, status='paid'):
        self.id = 'pe-test-1'
        self.status = status
        self.super_paid_amount = None
        self.super_paid_date = None
        self.payment_reference = None
        self.notes = None

    def to_dict(self):
        return {
            'id': self.id,
            'status': self.status,
            'super_paid_amount': self.super_paid_amount,
            'super_paid_date': self.super_paid_date,
            'payment_reference': self.payment_reference,
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


def _call(monkeypatch, pm, session, payload, status='paid'):
    """Invoke the endpoint with a fake pay event, return (body, session)."""
    pe = FakePayEvent(status=status)
    monkeypatch.setattr(pm, '_get_owned_pay_event',
                        lambda uid, pid: pe, raising=False)
    monkeypatch.setattr(pm, 'validate_date_string', lambda v, **kw: v,
                        raising=False)

    from flask import Flask
    app = Flask(__name__)
    app.config['TESTING'] = True

    with app.test_request_context(
        '/api/payroll/pay-events/pe-test-1/mark-paid',
        method='POST', json=payload,
    ):
        # Stand in for the api_key_required decorator's request.current_user.
        # Call the undecorated view so the auth decorator does not short-circuit
        # with a 401 before we reach the handler.
        from flask import request
        request.current_user = types.SimpleNamespace(id=69)
        view = getattr(pm.mark_pay_event_paid, '__wrapped__', pm.mark_pay_event_paid)
        response, status = view('pe-test-1')
    return pe, (response, status), session


def test_marks_finalized_event_paid_and_commits(monkeypatch, payroll_mod):
    pm, session = payroll_mod
    pe, body, sess = _call(monkeypatch, pm, session, {}, status='finalized')
    assert pe.status == 'paid'
    assert sess.commits >= 1


def test_payment_reference_persisted_on_already_paid_event(monkeypatch, payroll_mod):
    """The core regression: an already-paid event must still persist fields."""
    pm, session = payroll_mod
    pe, body, sess = _call(monkeypatch, pm, session,
                            {'payment_reference': 'H6051146167'},
                            status='paid')
    assert pe.payment_reference == 'H6051146167'
    # Critically: the DB must have been written, not just the ORM object.
    assert sess.commits >= 1, \
        'mark-paid on an already-paid event must still commit; otherwise the ' \
        'field is set in memory, echoed in the response, and silently lost.'


def test_notes_persisted_on_already_paid_event(monkeypatch, payroll_mod):
    pm, session = payroll_mod
    pe, body, sess = _call(monkeypatch, pm, session,
                            {'notes': 'Paid via ING Osko'},
                            status='paid')
    assert pe.notes == 'Paid via ING Osko'
    assert sess.commits >= 1


def test_super_paid_amount_persisted_on_already_paid_event(monkeypatch, payroll_mod):
    pm, session = payroll_mod
    pe, body, sess = _call(monkeypatch, pm, session,
                            {'super_paid_amount': '228.00'},
                            status='paid')
    assert str(pe.super_paid_amount) == '228.00'
    assert sess.commits >= 1


def test_no_commit_when_nothing_changed(monkeypatch, payroll_mod):
    """A no-op call on an already-paid event should not hammer the DB."""
    pm, session = payroll_mod
    pe, body, sess = _call(monkeypatch, pm, session, {}, status='paid')
    assert pe.status == 'paid'
    assert sess.commits == 0


def test_payment_reference_too_long_rejected(monkeypatch, payroll_mod):
    pm, session = payroll_mod
    pe, body, sess = _call(monkeypatch, pm, session,
                            {'payment_reference': 'x' * 101},
                            status='paid')
    assert body[1] == 400
    assert sess.commits == 0
