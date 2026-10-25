"""Regression tests for the 2026-10-25 security sweep (authentication).

Four defects were found by sweeping the live service, not by reading code:

  1. `User.is_active` was defined but consulted NOWHERE. Deactivating an
     account did nothing -- a retired account still returned HTTP 200 from
     `/api/auth/login` with the correct password. Proven live against the
     running service.

  2. `/pwa/login` had NO rate limiter, while `/login` (HMI) and
     `/api/auth/login` did. 12 wrong passwords produced 12 HTTP 200s.

  3. The rate limiter's bucket was keyed on a client IP taken from
     `CF-Connecting-IP` / `X-Forwarded-For` -- both client-supplied
     headers. Rotating a fake IP per request reset the bucket every time,
     so the lockout never engaged: unlimited password guessing. Proven
     live (10 wrong passwords, 10 HTTP 200s, no lockout).

  4. The limiter was keyed on the IP ALONE, so even a correctly derived
     IP was not enough -- the account itself must be limited, otherwise
     rotating the target email defeats the lockout just as easily.

The fixes: `is_active` is now enforced on every auth path, the PWA login
uses the same limiter as the HMI, `X-Forwarded-For` is ignored unless a
deployment explicitly trusts proxy headers, and the limiter now counts
failures per IP **and** per account.

Safety: the suite runs against the LIVE production Postgres. These tests
create a throwaway user and delete it on teardown. No production row is
modified. The rate limiter's counters are module-global, so they are
reset before and after each test.
"""

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import create_app
from app.models import db
from app.models.user import User
from app.models.user_profile import UserProfile
from app.models.user_payment_settings import UserPaymentSettings
from app.shared import rate_limiter

TEST_EMAIL = 'sec-sweep-test@example.com'
TEST_PASSWORD = 'not-a-real-password'

_CSRF_RE = re.compile(rb'name="csrf_token"[^>]*value="([^"]+)"')


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


@pytest.fixture(autouse=True)
def _reset_limiter():
    """The limiter's counters are module-global; clear them around each test."""
    rate_limiter._attempts.clear()
    rate_limiter._lockouts.clear()
    yield
    rate_limiter._attempts.clear()
    rate_limiter._lockouts.clear()


@pytest.fixture
def test_user(app):
    """A throwaway ACTIVE user with a payment_settings row."""
    with app.app_context():
        user = User.query.filter_by(email=TEST_EMAIL).first()
        if user is None:
            user = User(email=TEST_EMAIL, display_name='Sec Sweep Test', role='member')
            user.set_password(TEST_PASSWORD)
            db.session.add(user)
            db.session.commit()
            db.session.add(UserProfile(user_id=user.id))
            db.session.commit()
        if not user.payment_settings:
            user.payment_settings = UserPaymentSettings(user_id=user.id)
            db.session.commit()
        user.is_active = True
        db.session.commit()
        user_id = user.id

    yield user_id

    with app.app_context():
        User.query.filter_by(email=TEST_EMAIL).delete()
        db.session.commit()


def _set_active(app, user_id, active):
    with app.app_context():
        u = db.session.get(User, user_id)
        u.is_active = active
        db.session.commit()


def _csrf_token(client, path='/login'):
    resp = client.get(path)
    match = _CSRF_RE.search(resp.data)
    assert match, f'no csrf_token found in {path} (status {resp.status_code})'
    return match.group(1).decode()


# ---------------------------------------------------------------------------
# 1. Deactivated accounts must not authenticate
# ---------------------------------------------------------------------------

class TestDeactivatedAccount:
    def test_api_login_rejects_deactivated(self, app, client, test_user):
        _set_active(app, test_user, False)
        r = client.post('/api/auth/login', json={
            'email': TEST_EMAIL, 'password': TEST_PASSWORD})
        assert r.status_code == 401, 'deactivated account authenticated via API'

    def test_api_login_accepts_active(self, app, client, test_user):
        _set_active(app, test_user, True)
        r = client.post('/api/auth/login', json={
            'email': TEST_EMAIL, 'password': TEST_PASSWORD})
        assert r.status_code == 200, 'active account was rejected'

    def test_hmi_login_rejects_deactivated(self, app, client, test_user):
        _set_active(app, test_user, False)
        tok = _csrf_token(client, '/login')
        r = client.post('/login', data={
            'csrf_token': tok, 'email': TEST_EMAIL, 'password': TEST_PASSWORD},
            follow_redirects=False)
        assert r.status_code == 200  # re-renders the login page
        assert b'Logged in successfully' not in r.data, \
            'deactivated account logged in via the HMI'

    def test_pwa_login_rejects_deactivated(self, app, client, test_user):
        _set_active(app, test_user, False)
        tok = _csrf_token(client, '/pwa/login')
        r = client.post('/pwa/login', data={
            'csrf_token': tok, 'email': TEST_EMAIL, 'password': TEST_PASSWORD},
            follow_redirects=False)
        # A successful login redirects (302) to the PWA index; a rejected one
        # re-renders the login form (200). Asserting on the body would NOT
        # catch it -- the flash lives in the session, not the response.
        assert r.status_code == 200, \
            f'deactivated account logged in via the PWA (status {r.status_code})'
        assert 'Location' not in r.headers, \
            'deactivated account was redirected after a successful PWA login'

    def test_session_cookie_rejected_for_deactivated(self, app, client, test_user):
        """A session minted before deactivation must stop working."""
        client2 = app.test_client()
        with client2.session_transaction() as s:
            s['user_id'] = test_user
            s['user_email'] = TEST_EMAIL
        assert client2.get('/api/auth/business').status_code == 200

        _set_active(app, test_user, False)
        assert client2.get('/api/auth/business').status_code == 401, \
            'deactivated account still authenticated with a live session'

    def test_api_key_rejected_for_deactivated(self, app, client, test_user):
        with app.app_context():
            ups = UserPaymentSettings.query.filter_by(user_id=test_user).first()
            ups.api_key_plain = 'k' * 64
            db.session.commit()
        _set_active(app, test_user, False)
        r = client.get('/api/auth/business', headers={'X-API-Key': 'k' * 64})
        assert r.status_code == 401, 'deactivated account authenticated with an API key'

    def test_pwa_session_rejected_for_deactivated(self, app, client, test_user):
        client2 = app.test_client()
        with client2.session_transaction() as s:
            s['pwa_user_id'] = test_user
            s['pwa_user_email'] = TEST_EMAIL
        assert client2.get('/pwa/').status_code == 200

        _set_active(app, test_user, False)
        r = client2.get('/pwa/')
        assert r.status_code == 302, \
            'deactivated account still had a working PWA session'


# ---------------------------------------------------------------------------
# 2 & 3 & 4. The rate limiter must actually lock out
# ---------------------------------------------------------------------------

class TestRateLimiter:
    def test_pwa_login_locks_out(self, app, client, test_user):
        """The PWA login had no limiter at all."""
        tok = _csrf_token(client, '/pwa/login')
        codes = []
        for _ in range(6):
            r = client.post('/pwa/login', data={
                'csrf_token': tok, 'email': TEST_EMAIL, 'password': 'wrong'})
            codes.append(r.status_code)
        assert 429 in codes, f'PWA login never locked out: {codes}'

    def test_api_login_locks_out(self, app, client, test_user):
        codes = []
        for _ in range(6):
            r = client.post('/api/auth/login', json={
                'email': TEST_EMAIL, 'password': 'wrong'})
            codes.append(r.status_code)
        assert 429 in codes, f'API login never locked out: {codes}'

    def test_rotating_forwarded_for_does_not_bypass(self, app, client, test_user):
        """The original bypass: a fresh X-Forwarded-For per request reset
        the bucket, so the lockout never engaged."""
        codes = []
        for i in range(8):
            r = client.post('/api/auth/login', headers={
                'X-Forwarded-For': f'10.0.0.{i}'},
                json={'email': TEST_EMAIL, 'password': 'wrong'})
            codes.append(r.status_code)
        assert 429 in codes, \
            f'rotating X-Forwarded-For bypassed the lockout: {codes}'

    def test_rotating_cf_header_does_not_bypass(self, app, client, test_user):
        codes = []
        for i in range(8):
            r = client.post('/api/auth/login', headers={
                'CF-Connecting-IP': f'172.16.0.{i}'},
                json={'email': TEST_EMAIL, 'password': 'wrong'})
            codes.append(r.status_code)
        assert 429 in codes, \
            f'rotating CF-Connecting-IP bypassed the lockout: {codes}'

    def test_x_forwarded_for_ignored_by_default(self, app, client):
        """A deployment must not silently trust a client-supplied header."""
        import os
        os.environ.pop('TRUST_PROXY_HEADERS', None)
        assert rate_limiter._trust_proxy_headers() is False

    def test_successful_login_clears_counters(self, app, client, test_user):
        for _ in range(4):
            client.post('/api/auth/login', json={
                'email': TEST_EMAIL, 'password': 'wrong'})
        r = client.post('/api/auth/login', json={
            'email': TEST_EMAIL, 'password': TEST_PASSWORD})
        assert r.status_code == 200
        # The counter was cleared, so another 4 failures must not lock out.
        codes = []
        for _ in range(4):
            r = client.post('/api/auth/login', json={
                'email': TEST_EMAIL, 'password': 'wrong'})
            codes.append(r.status_code)
        assert 429 not in codes, f'success did not reset the counter: {codes}'

    def test_account_limit_is_independent_of_ip(self):
        """The account bucket is keyed on the email, so a different IP
        against the same account still trips the lockout."""
        rate_limiter.record_attempt('1.1.1.1', success=False, account='a@b.com')
        rate_limiter.record_attempt('1.1.1.1', success=False, account='a@b.com')
        rate_limiter.record_attempt('2.2.2.2', success=False, account='a@b.com')
        rate_limiter.record_attempt('3.3.3.3', success=False, account='a@b.com')
        rate_limiter.record_attempt('4.4.4.4', success=False, account='a@b.com')
        assert rate_limiter.is_locked_out('9.9.9.9', account='a@b.com'), \
            'per-account lockout did not trip'
        assert not rate_limiter.is_locked_out('9.9.9.9', account='z@z.com'), \
            'per-account lockout leaked to another account'
