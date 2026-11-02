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
    profile_snap = None

    with app.app_context():
        u = User.query.filter_by(email=BUSINESS_EMAIL).first()
        if u is not None:
            snap = {col.name: getattr(u, col.name) for col in u.__table__.columns}
            # The business ADDRESS lives in accounts.user_profiles, NOT
            # in the users table -- `User.address_line1` is a property
            # that reads through the profile. So snapshotting
            # User.__table__.columns alone snapshots NOTHING of the
            # address, and the test that mutates it leaves the
            # production row holding whatever the test wrote.
            #
            # This is the bug this fixture was introduced to fix, and
            # the fix did not reach the address: the docstring above
            # describes a corruption that had already happened once.
            # The suite runs against LIVE production Postgres, so a
            # snapshot that misses a column is a snapshot that
            # corrupts production.
            profile = getattr(u, 'profile', None)
            if profile is not None:
                profile_snap = {
                    col.name: getattr(profile, col.name)
                    for col in profile.__table__.columns
                }

    yield

    if snap is None:
        return

    with app.app_context():
        u = User.query.filter_by(email=BUSINESS_EMAIL).first()
        if u is None:
            return
        for k, v in snap.items():
            setattr(u, k, v)
        if profile_snap is not None:
            profile = getattr(u, 'profile', None)
            if profile is not None:
                for k, v in profile_snap.items():
                    setattr(profile, k, v)
        db.session.commit()


# ---------------------------------------------------------------------------
# The suite must leave PRODUCTION exactly as it found it (2026-11-03)
# ---------------------------------------------------------------------------
#
# The suite runs against LIVE production Postgres, so a row a test creates
# and does not delete is a row in the business's ledger. It happened:
# 1,029 junk customers and 8 junk invoices accumulated in production from
# the address-refactor and invoice-tax suites, and they were visible to
# anyone reading the customer list.
#
# The fixes below are deliberately at the SESSION level rather than in
# each test file, because a per-test cleanup is only as good as the
# tests that remembered to write one -- and the leaking tests predate
# the rule.
#
#   1. `restore_user` also snapshots the PROFILE row. `User.address_line1`
#      is a property that reads through `accounts.user_profiles`, so
#      snapshotting `User.__table__.columns` snapshots NOTHING of the
#      address. The fixture was introduced to stop exactly this
#      corruption and did not reach the address.
#   2. A session-scoped guard deletes every customer/invoice/quote the
#      session created, so a test that forgets to clean up cannot leak.
#      It recognises residue by the test-name prefixes the suite uses,
#      never by a real customer's name, and it refuses to sweep anything
#      that is not unmistakably a draft.
#   3. The guard ASSERTS the business address is unchanged at the end of
#      the session, and fails loudly if it is not.

#: Name prefixes the suite uses for rows it creates. Anything starting
#: with one of these is test residue. They are deliberately distinctive
#: so a real customer can never be swept by accident.
TEST_RESIDUE_PREFIXES = (
    'Test Refactor',
    'Update Test',
    'Update Bad',
    'Legacy Update',
    'Legacy Reject',
    'Bad State',
    'Bad PC',
    'US Test',
    'ZZ Test Co',
    'ZZ Invoice Tax Co',
    'ZZ Invoice Tax Test',
    'ZZ Quote Tax Co',
    'ZZ Quote Test Co',
    'ZZ Transmission Test Co',
    'ZZ Upload Test Co',
    'ZZ STP Test Employee',
    'ZZ STP No Tfn Employee',
)


def _is_residue_name(name):
    """True if `name` is a row the suite created."""
    if not name:
        return False
    return any(name.startswith(p) for p in TEST_RESIDUE_PREFIXES)


def _model(name):
    """The model class for `name`, or None if this build does not have it
    (e.g. Quote before the quotes feature was merged)."""
    try:
        import app.models as models
    except Exception:
        return None
    cls = getattr(models, name, None)
    if cls is None:
        try:
            from app.models.quote import Quote
            cls = Quote
        except Exception:
            return None
    return cls


def _rows(name):
    """All rows of a model, or [] if the model is absent from this build."""
    cls = _model(name)
    if cls is None:
        return []
    try:
        return cls.query.all()
    except Exception:
        return []


@pytest.fixture(scope='session', autouse=True)
def sweep_test_residue():
    """Delete every customer/invoice/quote this session created.

    Session-scoped and autouse, so it runs for EVERY test session --
    including one that runs a single file. It is the backstop for tests
    that create rows and forget to delete them.

    It refuses to touch anything that is not unmistakably test residue:
    only DRAFT invoices, only rows whose name matches a test prefix, and
    only rows this session actually created. A real customer's row cannot
    match a test prefix, so a real customer cannot be swept.
    """
    from app import create_app
    from app.models import db
    from app.models.customer import Customer

    app = create_app()
    with app.app_context():
        before_customers = {c.id for c in Customer.query.all()}
        before_invoices = {i.id for i in _rows('Invoice')}
        before_quotes = {q.id for q in _rows('Quote')}
        before_logs = {lg.id for lg in _rows('ActivityLog')}
        address_before = _business_address()

    # Yield the pre-session snapshot so a test can tell a REAL row (one
    # that existed before this session) from a row another test in this
    # session just created. Without this, a test that reads "all
    # customers" from the live database also sees its own session's
    # temporary rows -- and would call a test row a real customer.
    yield {
        'customers': before_customers,
        'invoices': before_invoices,
        'quotes': before_quotes,
        'address_before': address_before,
    }

    removed = {'customers': 0, 'invoices': 0, 'quotes': 0, 'activity_logs': 0}

    with app.app_context():
        # Quotes first: quotes.customer_id is ON DELETE RESTRICT.
        for q in _rows('Quote'):
            if q.id in before_quotes:
                continue
            if not _is_residue_name(getattr(q, 'client_name', '') or ''):
                continue
            if getattr(q, 'converted_invoice_id', None):
                continue  # converted -- business state, not residue
            db.session.delete(q)
            removed['quotes'] += 1

        for i in _rows('Invoice'):
            if i.id in before_invoices:
                continue
            if getattr(i, 'status', None) != 'draft':
                # Only DRAFT invoices are swept. A sent/paid invoice is
                # business state, whatever it is called.
                continue
            if not _is_residue_name(getattr(i, 'client_name', '') or ''):
                continue
            db.session.delete(i)
            removed['invoices'] += 1

        db.session.commit()

        swept_ids = set()

        for c in Customer.query.all():
            if c.id in before_customers:
                continue
            if not _is_residue_name(c.name or ''):
                continue
            if c.invoices.first() is not None:
                continue
            db.session.delete(c)
            swept_ids.add(c.id)
            removed['customers'] += 1

        db.session.commit()

        # The audit rows that recorded the test writes are residue too:
        # without this the log grows without bound on every suite run
        # (164 rows per run here), and it records the creation of rows
        # that no longer exist.
        #
        # The rule is deliberately narrow: a log row is swept only if the
        # row it describes DID NOT EXIST before this session and does not
        # exist now. A log about a row that existed before the session
        # survives, and so does a log about a row that exists now -- so a
        # real business action taken during the session keeps its audit
        # trail.
        from app.models.activity_log import ActivityLog

        live = {
            'customers': {c.id for c in Customer.query.all()},
            'invoices': {i.id for i in _rows('Invoice')},
            'quotes': {q.id for q in _rows('Quote')},
        }
        pre = {
            'customers': before_customers,
            'invoices': before_invoices,
            'quotes': before_quotes,
        }
        stale_logs = ActivityLog.query.filter(
            ActivityLog.table_name.in_(['customers', 'invoices', 'quotes']),
            ~ActivityLog.id.in_(before_logs),
        ).all()
        doomed = [
            lg for lg in stale_logs
            if lg.record_id not in pre.get(lg.table_name, set())
            and lg.record_id not in live.get(lg.table_name, set())
        ]
        for lg in doomed:
            db.session.delete(lg)
        db.session.commit()
        removed['activity_logs'] = len(doomed)

        address_after = _business_address()

    if any(removed.values()):
        print(
            f'\n[conftest] swept test residue from the live database: {removed}',
            flush=True,
        )

    # The address is the corruption that already happened TWICE. If the
    # session changed it and nothing restored it, the suite has just
    # damaged production again -- so say so, loudly, and fail.
    if address_before is not None and address_after != address_before:
        raise AssertionError(
            'the test suite changed the PRODUCTION business address and did '
            f'not restore it:\n  before: {address_before}\n  after:  {address_after}\n'
            'The suite runs against live production Postgres. Fix the test '
            'that mutates it, or use the `restore_user` fixture.'
        )


def _business_address():
    """The business user's printable address, or None if it cannot be read."""
    try:
        from app.models.user import User
        u = User.query.filter_by(email=business_email()).first()
        if u is None:
            return None
        return (
            getattr(u, 'address_line1', None),
            getattr(u, 'address_line2', None),
            getattr(u, 'city', None),
            getattr(u, 'state', None),
            getattr(u, 'postcode', None),
            getattr(u, 'address_country', None),
        )
    except Exception:
        return None
