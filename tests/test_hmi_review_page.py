"""Tests for the HMI expense review queue and the receipt helpers.

The HMI work recovered from the public remote (feat/hmi-expense-receipt,
feat/hmi-review-page) shipped with NO tests at all, and it did not even
parse -- the branch carried unresolved merge-conflict markers, and
schema/init.sql was invalid SQL (the expenses table was not created).
The code is now repaired and deployed, so it needs the coverage it never
had.

Three things are covered:

1. receipt_url() -- the bug the branch was fixing. attachment_path is
   stored ABSOLUTE for every API-created row (UPLOAD_FOLDER is absolute),
   so the old `.replace('uploads/','')` template hack produced
   "/uploads//home/..." and every receipt preview 404'd. receipt_url()
   must normalise the path relative to the upload root and return None
   when the file is gone -- a dead link is worse than no link.

2. receipt_filename() -- display name only. It must never leak a
   filesystem path into the page.

3. The /review page -- the queue the operator actually goes to. It must
   require a login, show only rows flagged requires_review, and show only
   the SESSION USER's rows.

Safety: the suite runs against the LIVE production Postgres. These tests
create a throwaway user and expenses, and delete every one of them on
teardown. No production row is read for writing, and no production row
is modified.

The `requires_review` flag is set on THROWAWAY rows only; production
expenses are never touched.
"""

import os
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import create_app
from app.models import db
from app.models.expense import Expense
from app.models.user import User
from app.models.user_profile import UserProfile
from app.models.user_payment_settings import UserPaymentSettings

TEST_EMAIL = 'hmi-review-test@example.com'
TEST_PASSWORD = 'testpassword123'


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
def test_user(app):
    """A throwaway ACTIVE 'member' user, deleted on teardown."""
    with app.app_context():
        user = User.query.filter_by(email=TEST_EMAIL).first()
        if user is None:
            user = User(email=TEST_EMAIL, display_name='HMI Review Test',
                        role='member')
            user.set_password(TEST_PASSWORD)
            db.session.add(user)
            db.session.commit()
            db.session.add(UserProfile(user_id=user.id))
            db.session.commit()
        if not user.payment_settings:
            user.payment_settings = UserPaymentSettings(user_id=user.id)
            db.session.commit()
        user.is_active = True
        user.role = 'member'
        db.session.commit()
        user_id = user.id

    yield user_id

    with app.app_context():
        Expense.query.filter_by(user_id=user_id).delete()
        User.query.filter_by(email=TEST_EMAIL).delete()
        db.session.commit()


def _make_expense(user_id, **kwargs):
    """Create a throwaway expense row and return it."""
    defaults = dict(
        user_id=user_id,
        source='test',
        vendor_name='Test Vendor',
        description='review queue test row',
        currency='AUD',
        ex_gst_amount=Decimal('100.00'),
        gst_amount=Decimal('10.00'),
        gst_type=Decimal('1.0'),
        total_amount=Decimal('110.00'),
        expense_date=date(2026, 10, 20),
        requires_review=True,
    )
    defaults.update(kwargs)
    row = Expense(**defaults)
    db.session.add(row)
    db.session.commit()
    return row


OTHER_EMAIL = 'hmi-review-other@example.com'


@pytest.fixture
def other_user(app):
    """A SECOND throwaway user, so the ownership scoping can be tested."""
    with app.app_context():
        user = User.query.filter_by(email=OTHER_EMAIL).first()
        if user is None:
            user = User(email=OTHER_EMAIL, display_name='HMI Review Other',
                        role='member')
            user.set_password(TEST_PASSWORD)
            db.session.add(user)
            db.session.commit()
            db.session.add(UserProfile(user_id=user.id))
            db.session.commit()
        user.is_active = True
        user.role = 'member'
        db.session.commit()
        user_id = user.id

    yield user_id

    with app.app_context():
        Expense.query.filter_by(user_id=user_id).delete()
        User.query.filter_by(email=OTHER_EMAIL).delete()
        db.session.commit()


@pytest.fixture
def cookie_client(app, test_user):
    """A test client holding a valid HMI session cookie."""
    c = app.test_client()
    with c.session_transaction() as s:
        s['user_id'] = test_user
        s['user_email'] = TEST_EMAIL
    return c


# ---------------------------------------------------------------------------
# receipt_url / receipt_filename
# ---------------------------------------------------------------------------

class TestReceiptHelpers:
    """receipt_url() must never produce a dead link.

    The live data is stored RELATIVE ("uploads/receipts/<uuid>.jpg" -- 58
    such rows); the OLD deployment stored ABSOLUTE paths, and the old
    `.replace('uploads/','')` template hack mangled those into
    "/uploads//home/..." so every receipt preview 404'd. Both forms must
    resolve, and a path whose file is gone must return None.
    """

    def test_none_for_a_row_with_no_attachment(self, app):
        from app.hmi.routes import receipt_url
        with app.app_context():
            assert receipt_url(None) is None
            assert receipt_url('') is None

    def test_none_when_the_file_is_gone(self, app):
        """A stored path with no file on disk must NOT render a dead link."""
        from app.hmi.routes import receipt_url
        with app.test_request_context():
            assert receipt_url('uploads/receipts/definitely-not-here.jpg') is None

    def _write(self, app, rel):
        root = os.path.abspath(app.config.get('UPLOAD_FOLDER', 'uploads'))
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as fh:
            fh.write(b'\x89PNG test receipt')
        return path

    def test_relative_stored_path_resolves(self, app):
        """The form the live database actually holds."""
        from app.hmi.routes import receipt_url
        with app.app_context():
            path = self._write(app, 'receipts/review-test-relative.jpg')
        try:
            with app.test_request_context():
                url = receipt_url('uploads/receipts/review-test-relative.jpg')
            assert url is not None
            assert url.count('/uploads/') == 1, f"doubled upload prefix in {url!r}"
            assert 'receipts/review-test-relative.jpg' in url
        finally:
            os.remove(path)

    def test_absolute_path_under_the_upload_root_resolves(self, app, tmp_path):
        """THE BUG the branch was fixing: an ABSOLUTE stored path.

        The old code did attachment_path.replace('uploads/', '') on an
        absolute path and produced "/uploads//home/..." -> 404. An
        absolute path under the upload root must normalise to a relative
        one and produce a real, single-prefixed URL.

        The upload root here is deliberately NOT called "uploads": the
        relative-path branch strips a literal "uploads/" marker, so a
        root with a different name is the only case where the
        absolute-path handling is actually needed. (With the default
        root the two branches agree, which is why a mutation of the
        absolute branch alone is NOT caught by a test using it.)
        """
        from app.hmi.routes import receipt_url
        root = tmp_path / 'receipt-store'
        (root / 'receipts').mkdir(parents=True)
        path = root / 'receipts' / 'review-test-absolute.jpg'
        path.write_bytes(b'\x89PNG test receipt')

        old_upload_folder = app.config.get('UPLOAD_FOLDER')
        app.config['UPLOAD_FOLDER'] = str(root)
        try:
            with app.test_request_context():
                url = receipt_url(str(path))
            assert url is not None, "absolute path under the upload root must resolve"
            assert url.endswith('/uploads/receipts/review-test-absolute.jpg')
            assert '//' not in url.replace('http://', '').replace('https://', '')
        finally:
            app.config['UPLOAD_FOLDER'] = old_upload_folder

    def test_filename_is_a_basename_only(self, app):
        """The display name must never leak a filesystem path."""
        from app.hmi.routes import receipt_filename
        with app.app_context():
            absolute = os.path.abspath(
                os.path.join(app.config.get('UPLOAD_FOLDER', 'uploads'), 'x.pdf'))
            assert receipt_filename(absolute) == 'x.pdf'
            assert receipt_filename('uploads/receipts/y.pdf') == 'y.pdf'
            assert receipt_filename(None) is None


# ---------------------------------------------------------------------------
# The /review page
# ---------------------------------------------------------------------------

class TestReviewPage:
    def test_requires_login(self, app):
        """An anonymous visitor is redirected, not shown the queue."""
        c = app.test_client()
        r = c.get('/review')
        assert r.status_code in (302, 401, 403), r.status_code
        assert '/review' not in (r.headers.get('Location') or '')

    def test_shows_only_requires_review_rows(self, app, cookie_client, test_user):
        with app.app_context():
            flagged = _make_expense(test_user, vendor_name='Flagged Vendor')
            plain = _make_expense(test_user, vendor_name='Plain Vendor',
                                  requires_review=False)
            flagged_id, plain_id = flagged.id, plain.id

        r = cookie_client.get('/review')
        assert r.status_code == 200
        body = r.get_data(as_text=True)
        assert 'Flagged Vendor' in body
        assert 'Plain Vendor' not in body
        assert flagged_id in body
        assert plain_id not in body

    def test_shows_only_the_session_users_rows(self, app, cookie_client,
                                              test_user, other_user):
        """A row belonging to somebody else must never appear."""
        with app.app_context():
            mine = _make_expense(test_user, vendor_name='Mine Vendor')
            other = _make_expense(other_user, vendor_name='Other Vendor')
            mine_id, other_id = mine.id, other.id

        r = cookie_client.get('/review')
        assert r.status_code == 200
        body = r.get_data(as_text=True)
        assert mine_id in body
        assert other_id not in body
        assert 'Other Vendor' not in body

    def test_shows_the_sender_and_sent_columns(self, app, cookie_client, test_user):
        """The point of the page: WHO sent it and WHEN.

        The sender label is the display name when there is one, and the
        raw phone as the fallback -- it is a label, not both.
        """
        sent = datetime(2026, 10, 25, 9, 30, tzinfo=timezone.utc)
        with app.app_context():
            _make_expense(test_user, vendor_name='Sender Vendor',
                          submitted_by='Test Sender',
                          submitted_phone='+61400000000',
                          submitted_at=sent)

        r = cookie_client.get('/review')
        assert r.status_code == 200
        body = r.get_data(as_text=True)
        assert 'Test Sender' in body
        assert '2026-10-25' in body, "the sent timestamp must be rendered"

    def test_falls_back_to_the_phone_when_there_is_no_display_name(self, app,
                                                                  cookie_client,
                                                                  test_user):
        with app.app_context():
            _make_expense(test_user, vendor_name='Phone Only Vendor',
                          submitted_phone='+61400000000',
                          submitted_at=datetime(2026, 10, 25, 9, 30,
                                                tzinfo=timezone.utc))

        r = cookie_client.get('/review')
        assert r.status_code == 200
        body = r.get_data(as_text=True)
        assert '+61400000000' in body

    def test_expenses_page_links_to_the_review_queue(self, app, cookie_client, test_user):
        """The old 'Show Requires Review' filter is gone; a link to /review is not."""
        with app.app_context():
            _make_expense(test_user, vendor_name='Linked Vendor')

        r = cookie_client.get('/expenses')
        assert r.status_code == 200
        body = r.get_data(as_text=True)
        assert '/review' in body
        assert 'Showing: Requires Review Only' not in body
