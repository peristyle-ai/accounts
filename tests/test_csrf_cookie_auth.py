"""Regression tests for S7 -- the CSRF-exempt API accepted cookie auth.

The 2026-10-25 security sweep found (S7) that every API blueprint is
exempt from Flask-WTF's CSRF protection on the stated grounds that
"API blueprints use X-API-Key header auth, not cookies". The premise
was false: `api_key_required` resolves a caller from the session cookie
as well, so a cookie ALONE could perform a state-changing request.

Proven live before the fix: a session cookie alone performed a POST that
returned 201 Created. No CSRF token, no X-API-Key.

The fix (Option A of docs/csrf_cookie_auth_proposal.md): the session
cookie authenticates READS only. Any state-changing API call must carry
the X-API-Key header, which a browser will not attach on another site's
behalf.

This matters here because the API is about to be published as open
source, and because the API is served on the same host as the HMI
(cookies ignore ports), so a same-site attacker is a realistic threat.

Safety: the suite runs against the LIVE production Postgres. These tests
create a throwaway user and delete it on teardown. No production row is
modified, and no route body is reached by a refused request -- the guard
runs in the decorator, before the handler.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import create_app
from app.models import db
from app.models.user import User
from app.models.user_profile import UserProfile
from app.models.user_payment_settings import UserPaymentSettings

TEST_EMAIL = 'csrf-cookie-test@example.com'
TEST_PASSWORD = 'not-a-real-password'
TEST_API_KEY = 'k' * 64


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
    """A bare client: no session cookie, no API key."""
    return app.test_client()


@pytest.fixture
def test_user(app):
    """A throwaway ACTIVE 'member' user with a payment_settings row."""
    with app.app_context():
        user = User.query.filter_by(email=TEST_EMAIL).first()
        if user is None:
            user = User(email=TEST_EMAIL, display_name='CSRF Cookie Test',
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
        User.query.filter_by(email=TEST_EMAIL).delete()
        db.session.commit()


def _cookie_client(app, user_id):
    """A test client holding a valid HMI session cookie."""
    c = app.test_client()
    with c.session_transaction() as s:
        s['user_id'] = user_id
        s['user_email'] = TEST_EMAIL
    return c


@pytest.fixture
def cookie_client(app, test_user):
    """A test client that holds a valid HMI session cookie."""
    return _cookie_client(app, test_user)


def _give_api_key(app, user_id):
    with app.app_context():
        ups = UserPaymentSettings.query.filter_by(user_id=user_id).first()
        ups.api_key_plain = TEST_API_KEY
        db.session.commit()


# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------

class TestCookieAuthIsReadOnly:
    """A cookie-authenticated caller may read, never write."""

    def test_cookie_get_is_allowed(self, app, cookie_client):
        r = cookie_client.get('/api/account-categories')
        assert r.status_code == 200, \
            'cookie GET was refused (%s): reads must still work' % r.status_code

    def test_cookie_post_is_refused(self, app, cookie_client):
        r = cookie_client.post('/api/account-categories',
                               json={'name': 'csrf-test'})
        assert r.status_code == 403, \
            'cookie POST was NOT refused (status %s)' % r.status_code

    def test_cookie_put_is_refused(self, app, cookie_client):
        r = cookie_client.put('/api/account-categories/1',
                              json={'name': 'csrf-test'})
        assert r.status_code == 403, \
            'cookie PUT was NOT refused (status %s)' % r.status_code

    def test_cookie_delete_is_refused(self, app, cookie_client):
        r = cookie_client.delete('/api/account-categories/1')
        assert r.status_code == 403, \
            'cookie DELETE was NOT refused (status %s)' % r.status_code

    def test_cookie_patch_is_refused(self, app, cookie_client):
        r = cookie_client.patch('/api/invoice-reminders/1', json={'x': 1})
        assert r.status_code == 403, \
            'cookie PATCH was NOT refused (status %s)' % r.status_code

    def test_refusal_names_the_requirement(self, app, cookie_client):
        r = cookie_client.post('/api/account-categories',
                               json={'name': 'csrf-test'})
        assert r.status_code == 403
        body = r.get_json()
        assert 'X-API-Key' in (body or {}).get('error', ''), \
            'the refusal must say what is required: %s' % body

    def test_refusal_is_logged(self, app, cookie_client):
        """A refusal is a security event -- it must be recorded, not silently
        dropped (the activity log has NOT NULL columns; a NULL there would
        make the INSERT fail and lose the record without any error)."""
        from app.models.activity_log import ActivityLog
        with app.app_context():
            before = ActivityLog.query.filter_by(
                action='cookie_write_refused').count()

        r = cookie_client.post('/api/account-categories',
                               json={'name': 'csrf-test'})
        assert r.status_code == 403

        with app.app_context():
            after = ActivityLog.query.filter_by(
                action='cookie_write_refused').count()
            row = ActivityLog.query.filter_by(
                action='cookie_write_refused').order_by(
                    ActivityLog.created_at.desc()).first()
        assert after == before + 1, \
            'the refusal was not written to the activity log'
        assert row.table_name and row.record_id, \
            'the refusal row has empty NOT NULL columns -- it would not persist'


# ---------------------------------------------------------------------------
# No regressions
# ---------------------------------------------------------------------------

class TestApiKeyStillWorks:
    """The API key path must be untouched by the guard."""

    def test_api_key_post_reaches_the_route(self, app, client, test_user):
        _give_api_key(app, test_user)
        r = client.post('/api/account-categories', json={},
                        headers={'X-API-Key': TEST_API_KEY})
        # The route body rejects an empty payload with 400. The point is
        # that it got there: a 403 would mean the guard refused a request
        # that carried a proper credential.
        assert r.status_code == 400, \
            'API-key POST was blocked (status %s)' % r.status_code

    def test_api_key_get_still_works(self, app, client, test_user):
        _give_api_key(app, test_user)
        r = client.get('/api/account-categories',
                       headers={'X-API-Key': TEST_API_KEY})
        assert r.status_code == 200


class TestNoCredentialFallthrough:
    def test_no_credentials_is_401(self, app, client):
        r = client.post('/api/account-categories', json={'name': 'x'})
        assert r.status_code == 401

    def test_bad_api_key_does_not_fall_back_to_cookie(self, app, cookie_client):
        """A wrong X-API-Key must not silently authenticate via the
        session cookie -- that would be a credential-confusion bypass."""
        r = cookie_client.post('/api/account-categories', json={'name': 'x'},
                               headers={'X-API-Key': 'wrong-key'})
        assert r.status_code == 401, \
            'wrong API key fell back to the cookie (status %s)' % r.status_code
        assert 'API key' in (r.get_json() or {}).get('error', ''), \
            'the rejection must name the credential that failed: %s' % r.get_json()


class TestAuthMethodIsRecorded:
    def test_auth_method_is_set_for_both_credentials(self, app, cookie_client,
                                                      test_user):
        from app.shared.decorators import (AUTH_API_KEY, AUTH_COOKIE,
                                           api_key_required)
        seen = {}

        @app.route('/__probe')
        @api_key_required
        def probe():
            from flask import request
            seen['method'] = getattr(request, 'auth_method', None)
            return {'ok': True}

        cookie_client.get('/__probe')
        assert seen.get('method') == AUTH_COOKIE, \
            'cookie request did not record its auth method: %s' % seen

        _give_api_key(app, test_user)
        cookie_client.get('/__probe', headers={'X-API-Key': TEST_API_KEY})
        assert seen.get('method') == AUTH_API_KEY, \
            'API-key request did not record its auth method: %s' % seen
