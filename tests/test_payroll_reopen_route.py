"""Regression tests for POST /api/payroll/pay-events/<id>/reopen.

The bug these guard against
---------------------------
The reopen route read ``request.remote_address`` when writing its
activity-log row. Flask's Request object has no such attribute -- the
attribute is ``remote_addr`` -- so the lookup raised AttributeError and
the endpoint answered **HTTP 500 on every call**.

The failure was nastier than a plain 500, because of the ORDER of the
two commits: the status change was committed to the database FIRST, and
the activity row was written and committed SECOND. So a 500 from this
route meant the pay event had ALREADY moved to ``draft`` on disk. A
caller that trusted the 500 would think the reopen had been refused and
retry -- and get a 409 on the retry, because the event was no longer
finalized. The status and the log row are now committed together, so
the answer and the store agree.

These tests use a fake pay event + a fake session so they need no
database. The route is exercised through a real Flask request context,
because the bug lived in a Flask Request attribute -- a test that only
read the source would not have caught it.
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

    def __init__(self, status='finalized'):
        self.id = 'pe-reopen-1'
        self.status = status

    def to_dict(self):
        return {'id': self.id, 'status': self.status}


class FakeSession:
    """Records commits and the rows added to the session."""

    def __init__(self):
        self.commits = 0
        self.added = []

    def commit(self):
        self.commits += 1

    def add(self, obj):
        self.added.append(obj)


@pytest.fixture
def payroll_mod(monkeypatch):
    from app.api import payroll as pm

    session = FakeSession()
    monkeypatch.setattr(pm.db, 'session', session, raising=False)
    return pm, session


def _call(monkeypatch, pm, session, status='finalized'):
    """Invoke the reopen view inside a real Flask request context."""
    pe = FakePayEvent(status=status)
    monkeypatch.setattr(pm, '_get_owned_pay_event',
                        lambda uid, pid: pe, raising=False)

    from flask import Flask, request
    app = Flask(__name__)
    app.config['TESTING'] = True

    with app.test_request_context(
        '/api/payroll/pay-events/pe-reopen-1/reopen',
        method='POST',
        environ_base={'REMOTE_ADDR': '203.0.113.9'},
    ):
        request.current_user = types.SimpleNamespace(id=69)
        view = getattr(pm.reopen_pay_event, '__wrapped__', pm.reopen_pay_event)
        response, status_code = view('pe-reopen-1')
    return pe, (response, status_code), session


def test_reopen_succeeds_and_does_not_500(monkeypatch, payroll_mod):
    """The route must answer 200. It used to raise AttributeError -> 500."""
    pm, session = payroll_mod
    pe, (response, status_code), session = _call(monkeypatch, pm, session)

    assert status_code == 200, (
        f"reopen returned {status_code}: {response!r} -- the route must not "
        "raise. Flask's Request has remote_addr, not remote_address."
    )
    assert pe.status == 'draft'


def test_reopen_uses_the_real_remote_address(monkeypatch, payroll_mod):
    """The activity row must carry the caller's IP, read off the request."""
    pm, session = payroll_mod
    logged = []
    monkeypatch.setattr(pm, 'log_activity',
                        lambda **kw: logged.append(kw), raising=False)

    pe, (response, status_code), session = _call(monkeypatch, pm, session)

    assert status_code == 200
    assert logged, "no activity row was written"
    assert logged[0].get('ip_address') == '203.0.113.9', (
        "the activity row must carry the request's remote address, not None "
        f"(got {logged[0].get('ip_address')!r})"
    )


def test_status_change_and_activity_row_commit_together(monkeypatch, payroll_mod):
    """One commit for the whole operation.

    If logging raises, the status change must NOT already be on disk --
    otherwise a 500 would mean "the reopen happened but was refused".
    """
    pm, session = payroll_mod

    def boom(**kw):
        raise RuntimeError('log_activity failed')

    monkeypatch.setattr(pm, 'log_activity', boom, raising=False)

    with pytest.raises(RuntimeError):
        _call(monkeypatch, pm, session)

    assert session.commits == 0, (
        "the pay event's status was committed before the activity row was "
        "written, so a failed write left the row changed on disk while the "
        "endpoint reported a 500. Commit them together."
    )


def test_non_finalized_event_is_refused(monkeypatch, payroll_mod):
    """A paid or draft event must still be refused with 409."""
    pm, session = payroll_mod
    pe, (response, status_code), session = _call(monkeypatch, pm, session,
                                                 status='paid')

    assert status_code == 409
    assert pe.status == 'paid'
    assert session.commits == 0
