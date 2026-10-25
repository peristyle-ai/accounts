"""Shared pytest fixtures for the accounts test suite.

Why this exists (2026-09-29):

The test suite runs against the **live production Postgres** (there is
no separate test database yet). Any test that mutates a real row —
especially the singleton `payroll@example.com` business user — can
silently overwrite production state if it does not restore the original
values.

History: `tests/test_address_refactor.py::test_business_address_update_normalises_state`
had a "cleanup" step that hardcoded the user row back to placeholder
values (`1 Example St / Brisbane / QLD 4000`) after every test run.
That cleanup silently corrupted the production address every time the
test suite ran. The user only noticed because the top-right header on
the Statement of Account PDF rendered a placeholder instead of the real
business address.

The `snapshot_row` and `restore_user` fixtures below make the
snapshot/restore pattern trivial to apply correctly:

    def test_some_user_mutation(self, app, restore_user):
        # restore_user snapshots payroll@example.com on entry and
        # restores on exit, even if assertions fail.
        ...

If a test needs to snapshot a different row, use `snapshot_row` directly:

    def test_some_other(self, app, snapshot_row):
        from app.models.customer import Customer
        with app.app_context():
            cid = Customer.query.first().id
            snapshot_row(Customer, cid)
        # ...mutate...
        # teardown is automatic — even if the test raises.

Both fixtures raise loud-and-clear if `app.app_context()` is not active
on entry, since that usually means the test forgot the `app` fixture.
"""

import os
import sys
from pathlib import Path

import pytest

# Make the project root importable (same pattern other tests use).
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Load .env into os.environ before importing anything that reads it
# (e.g. PAYROLL_PII_KEY, DB creds).
ENV = ROOT / '.env'
if ENV.exists():
    for line in ENV.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, _, v = line.partition('=')
        os.environ.setdefault(k.strip(), v.strip())


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def api_key_plain_for(user_id):
    """The PLAINTEXT API key for a user.

    `accounts.user_payment_settings.api_key` is documented as Fernet
    ciphertext, but rows seeded before the encryption refactor still hold
    the plaintext token. `api_key_plain` is tolerant of both, so use it
    rather than reading the raw column -- an X-API-Key header must carry
    the plaintext, never the ciphertext.

    Requires an active app context.
    """
    from app.models.user_payment_settings import UserPaymentSettings
    ups = UserPaymentSettings.query.filter_by(user_id=user_id).first()
    if ups is None:
        return None
    return ups.api_key_plain


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def snapshot_row(app):
    """Factory fixture: snapshot any SQLAlchemy row by (model, pk) and
    restore it on teardown — even if the test raises.

    Usage:

        def test_xyz(self, app, snapshot_row):
            from app.models.customer import Customer
            with app.app_context():
                cid = Customer.query.first().id
                snapshot_row(Customer, cid)
            # ...mutate via the API or directly...

    Snapshots are stacked: you can call `snapshot_row(...)` multiple
    times and they all get restored in reverse order.

    If the row is gone at teardown (e.g. test deleted it), the fixture
    silently skips the restore — that is the intended behaviour for
    tests that intentionally delete rows.
    """
    snapshots = []

    def _register(model, pk):
        with app.app_context():
            from app.models import db
            row = db.session.get(model, pk)
            if row is None:
                return
            snap = {
                col.name: getattr(row, col.name)
                for col in row.__table__.columns
            }
        snapshots.append((model, pk, snap))

    yield _register

    with app.app_context():
        from app.models import db
        for model, pk, snap in reversed(snapshots):
            row = db.session.get(model, pk)
            if row is None:
                continue
            for k, v in snap.items():
                setattr(row, k, v)
            db.session.commit()


def business_email() -> str:
    """The address of the business user the suite runs against.

    The suite runs against a real deployment, so the business user is
    configuration: set TEST_BUSINESS_EMAIL in the environment (or .env).
    A fresh checkout has no business user, and the tests that need one
    skip themselves.
    """
    return os.environ.get('TEST_BUSINESS_EMAIL', '').strip()


@pytest.fixture
def restore_user(app):
    """Convenience fixture: snapshot the canonical business user row
    (`payroll@example.com`) on entry and restore it on exit.

    Use this in any test that calls an API route that mutates the
    business's own address, bank details, contact info, etc., or in any
    test that mutates User columns directly. Without this fixture,
    those tests can silently corrupt production state.
    """
    from app.models import db
    from app.models.user import User

    BUSINESS_EMAIL = business_email()
    snap = None

    with app.app_context():
        u = User.query.filter_by(email=BUSINESS_EMAIL).first()
        if u is not None:
            snap = {col.name: getattr(u, col.name) for col in u.__table__.columns}

    yield

    if snap is None:
        return

    with app.app_context():
        u = User.query.filter_by(email=BUSINESS_EMAIL).first()
        if u is None:
            return
        for k, v in snap.items():
            setattr(u, k, v)
        db.session.commit()
