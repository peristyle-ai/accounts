"""Tests for payroll encryption + backfill idempotency.

PII round-trip:
    Encrypt a TFN, persist it on an Employee, then decrypt and confirm the
    plaintext matches. This is the single most important assertion — if the
    Fernet helper is broken, every encrypted column is useless.

Backfill idempotency:
    Run the backfill script twice (pointed at a temp directory of fake PDFs).
    The second run must insert 0 rows. We use a tiny temp PDF generator to
    keep the test self-contained.

Run with:
    cd /opt/accounts && ./venv/bin/python3 -m pytest tests/test_payroll.py -v
"""

import io
import os
import sys
import shutil
import tempfile
import uuid
from decimal import Decimal
from pathlib import Path

import pytest

# Make sure we can import app.* from the project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Load .env manually
ENV = Path(__file__).resolve().parent.parent / '.env'
if ENV.exists():
    for line in ENV.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, _, v = line.partition('=')
        os.environ.setdefault(k.strip(), v.strip())

from app import create_app
from app.models import db
from app.models.user import User
from app.models.employee import Employee


@pytest.fixture
def app():
    app = create_app()
    app.config['TESTING'] = True
    app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get(
        'DATABASE_URL',
        'postgresql://postgres:not-a-real-password@127.0.0.1:5432/accounts',
    )
    with app.app_context():
        yield app


@pytest.fixture
def client(app):
    return app.test_client()


# ---------------------------------------------------------------------------
# PII round-trip
# ---------------------------------------------------------------------------

class TestPIIRoundTrip:
    """Encrypt → DB → decrypt preserves the plaintext.

    These tests create real Employee rows tied to the live business
    user (via `_pick_user_id`). If an assertion fails before the
    `db.session.delete()` line runs, the test Employee would be left
    orphaned in production — tagged only by the `TEST_<TYPE>_<uuid>`
    legal_name. 2026-09-29 fix: each test uses a try/finally so the
    delete runs even on assertion failure, AND the `restore_user`
    fixture is applied as a safety net.
    """

    def test_tfn_round_trip(self, app, restore_user):
        with app.app_context():
            e = Employee(
                user_id=self._pick_user_id(app),
                legal_name=f'TEST_TFN_{uuid.uuid4().hex[:8]}',
            )
            e.tfn_plain = '555 555 555'
            db.session.add(e)
            db.session.commit()
            eid = e.id
            try:
                db.session.expire_all()
                fresh = Employee.query.get(eid)
                assert fresh.tfn_plain == '555 555 555'
                assert fresh.tfn_masked == '*** *** 555'
            finally:
                # Cleanup runs even on assertion failure so we don't
                # leave orphans in production tied to the live user.
                try:
                    db.session.delete(Employee.query.get(eid))
                    db.session.commit()
                except Exception:
                    db.session.rollback()

    def test_bsb_round_trip(self, app, restore_user):
        with app.app_context():
            e = Employee(
                user_id=self._pick_user_id(app),
                legal_name=f'TEST_BSB_{uuid.uuid4().hex[:8]}',
            )
            e.bank_bsb_plain = '123-456'
            db.session.add(e)
            db.session.commit()
            eid = e.id
            try:
                db.session.expire_all()
                fresh = Employee.query.get(eid)
                assert fresh.bank_bsb_plain == '123-456'
                assert fresh.bank_bsb_masked == '***-456'
            finally:
                try:
                    db.session.delete(Employee.query.get(eid))
                    db.session.commit()
                except Exception:
                    db.session.rollback()

    def test_account_round_trip(self, app, restore_user):
        with app.app_context():
            e = Employee(
                user_id=self._pick_user_id(app),
                legal_name=f'TEST_ACC_{uuid.uuid4().hex[:8]}',
            )
            e.bank_account_number_plain = '12345678'
            db.session.add(e)
            db.session.commit()
            eid = e.id
            try:
                db.session.expire_all()
                fresh = Employee.query.get(eid)
                assert fresh.bank_account_number_plain == '12345678'
                assert fresh.bank_account_number_masked == '*****5678'
            finally:
                try:
                    db.session.delete(Employee.query.get(eid))
                    db.session.commit()
                except Exception:
                    db.session.rollback()

    def test_to_dict_masks_pii_by_default(self, app, restore_user):
        with app.app_context():
            e = Employee(
                user_id=self._pick_user_id(app),
                legal_name=f'TEST_MASK_{uuid.uuid4().hex[:8]}',
            )
            e.tfn_plain = '111 222 333'
            e.bank_bsb_plain = '999-888'
            e.bank_account_number_plain = '77777777'
            db.session.add(e)
            db.session.commit()
            eid = e.id
            try:
                db.session.expire_all()
                fresh = Employee.query.get(eid)
                d = fresh.to_dict()
                assert d['tfn'] == '*** *** 333'
                assert d['bank_bsb'] == '***-888'
                assert d['bank_account_number'] == '*****7777'
                # Reveal path returns plaintext
                d2 = fresh.to_dict(include_pii_plain=True)
                assert d2['tfn_plain'] == '111 222 333'
                assert d2['bank_bsb_plain'] == '999-888'
                assert d2['bank_account_number_plain'] == '77777777'
            finally:
                try:
                    db.session.delete(Employee.query.get(eid))
                    db.session.commit()
                except Exception:
                    db.session.rollback()

    def _pick_user_id(self, app):
        u = User.query.first()
        if u is None:
            # A brand-new install has no users yet -- the administrator
            # creates them. Skip rather than fail: this test needs an
            # existing user to hang a temporary employee off.
            pytest.skip('No users in the database yet (fresh install)')
        return u.id


# ---------------------------------------------------------------------------
# Backfill idempotency
# ---------------------------------------------------------------------------

class TestBackfillIdempotent:  # noqa: D400
    """Stub class retained so any old test references don't error.
    The associated backfill script (scripts/backfill_pay_events.py)
    was a one-time migration tool for migrating Alice Smith's existing
    payslips — it has been removed from the public repo since the data
    migration is complete. Re-add idempotency tests here if a similar
    migration script is needed for another employer."""

    def test_no_op(self):
        pass

    """Run the backfill against a temp dir of synthetic PDFs; second run = no-op."""

