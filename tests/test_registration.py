"""Tests for the HMI auth flow (login, logout, registration policy).

History:
  * 2026-10-23 -- the previous version assumed open self-service
    registration and posted forms without a CSRF token. Both assumptions
    were wrong: registration had been disabled, and `CSRFProtect` is
    enabled for the HMI and PWA blueprints (the REST API blueprint is
    exempt).
  * 2026-10-25 -- the registration routes were REMOVED rather than
    left returning a "disabled" error. There is no /register, no
    /verify/<token> and no /resend-verification, so an anonymous
    visitor has no way to create an account or reach an
    email-verification flow. Accounts are created by the maintaining
    administrator directly in the users table.

The test user is therefore created directly through the model rather than
by going through /register.
"""

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

from app import create_app
from app.models import db
from app.models.user import User


TEST_EMAIL = 'hmi-test@example.com'
TEST_PASSWORD = 'not-a-real-password'

_CSRF_RE = re.compile(rb'name="csrf_token"[^>]*value="([^"]+)"')


@pytest.fixture
def app():
    app = create_app()
    app.config['TESTING'] = True
    # No hardcoded fallback: a default DSN with a literal password in
    # source invites a real credential being committed later, and it
    # silently pointed the suite at a different database than the one
    # conftest already configured from .env. Fail loudly instead.
    dsn = os.environ.get('DATABASE_URL')
    if not dsn:
        raise RuntimeError(
            'DATABASE_URL is not set. The test suite runs against the '
            'configured Postgres -- set DATABASE_URL in .env (see '
            '.env.example) before running pytest.'
        )
    app.config['SQLALCHEMY_DATABASE_URI'] = dsn

    with app.app_context():
        yield app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def cleanup(app):
    """Delete any test user created during the test. Wrapped in
    try/except so that the test result is preserved -- 2026-09-29
    previous version used a bare `yield` followed by a delete, which
    skipped cleanup if the test raised an unexpected exception.
    """
    yield
    try:
        with app.app_context():
            User.query.filter_by(email=TEST_EMAIL).delete()
            db.session.commit()
    except Exception:
        # Don't mask the original test failure with a teardown error.
        pass


def _csrf_token(client, path='/login'):
    """Fetch a form and pull the CSRF token out of it."""
    resp = client.get(path)
    match = _CSRF_RE.search(resp.data)
    assert match, f'no csrf_token found in {path} (status {resp.status_code})'
    return match.group(1).decode()


def _create_user(app):
    with app.app_context():
        user = User.query.filter_by(email=TEST_EMAIL).first()
        if user is None:
            user = User(email=TEST_EMAIL, display_name='HMI Test')
            user.set_password(TEST_PASSWORD)
            db.session.add(user)
            db.session.commit()
    return TEST_EMAIL


def _login(client, email, password):
    token = _csrf_token(client, '/login')
    return client.post(
        '/login',
        data={'email': email, 'password': password, 'csrf_token': token},
        follow_redirects=True,
    )


# ---------------------------------------------------------------------------
# Registration policy
#
# There is NO self-service registration. The routes do not exist at all,
# so an anonymous visitor cannot create an account or reach an
# email-verification flow. Accounts are created by the maintaining
# administrator directly in the users table, and they generate their own
# API key from the HMI.
# ---------------------------------------------------------------------------

def test_register_route_does_not_exist(client):
    """GET /register is a 404 -- there is no registration form."""
    response = client.get('/register')
    assert response.status_code == 404


def test_post_register_does_not_exist(client):
    """POST /register is a 404 and creates nothing, whatever is sent."""
    token = _csrf_token(client, '/login')
    response = client.post(
        '/register',
        data={
            'email': TEST_EMAIL,
            'password': TEST_PASSWORD,
            'confirm_password': TEST_PASSWORD,
            'csrf_token': token,
        },
        follow_redirects=False,
    )
    assert response.status_code == 404
    with client.application.app_context():
        assert User.query.filter_by(email=TEST_EMAIL).first() is None


def test_api_register_does_not_exist(client):
    """POST /api/auth/register is a 404 -- no anonymous account creation."""
    response = client.post(
        '/api/auth/register',
        json={'email': TEST_EMAIL, 'password': TEST_PASSWORD, 'country': 'AU'},
    )
    assert response.status_code == 404
    with client.application.app_context():
        assert User.query.filter_by(email=TEST_EMAIL).first() is None


def test_verification_route_does_not_exist(client):
    """The email-verification flow is gone, token route and all."""
    response = client.get('/verify/some-token-value')
    assert response.status_code == 404


def test_resend_verification_route_does_not_exist(client):
    """There is nothing to resend."""
    token = _csrf_token(client, '/login')
    response = client.post('/resend-verification', data={'csrf_token': token})
    assert response.status_code == 404


def test_api_key_page_has_no_verification_gate(client, cleanup, app):
    """An admin-created user can generate an API key straight away.

    There is no email-verification state to satisfy, so the API-key page
    must not gate the button on it.
    """
    _create_user(app)
    _login(client, TEST_EMAIL, TEST_PASSWORD)
    response = client.get('/api-key', follow_redirects=True)
    assert response.status_code == 200
    body = response.data.lower()
    assert b'verify your email' not in body
    assert b'resend verification' not in body
    assert b'email not verified' not in body
    # The generate button is present and not disabled.
    assert b'Generate New API Key' in response.data


# ---------------------------------------------------------------------------
# Login / logout
# ---------------------------------------------------------------------------

def test_login_page_loads(client):
    response = client.get('/login')
    assert response.status_code == 200
    assert b'Login' in response.data


def test_login_success(client, cleanup, app):
    _create_user(app)
    client.get('/logout')
    response = _login(client, TEST_EMAIL, TEST_PASSWORD)
    assert response.status_code == 200
    assert b'Dashboard' in response.data or b'Welcome' in response.data


def test_login_invalid_credentials(client, cleanup, app):
    _create_user(app)
    client.get('/logout')
    response = _login(client, TEST_EMAIL, 'wrongpassword')
    assert b'invalid' in response.data.lower() or b'error' in response.data.lower()


def test_login_requires_csrf_token(client, cleanup, app):
    """Without the CSRF token the POST is rejected with 400."""
    _create_user(app)
    response = client.post(
        '/login',
        data={'email': TEST_EMAIL, 'password': TEST_PASSWORD},
    )
    assert response.status_code == 400


def test_logout(client, cleanup, app):
    _create_user(app)
    _login(client, TEST_EMAIL, TEST_PASSWORD)
    response = client.get('/logout', follow_redirects=True)
    assert response.status_code == 200


def test_dashboard_requires_login(client):
    response = client.get('/', follow_redirects=False)
    assert response.status_code == 302
    assert '/login' in response.location


def test_dashboard_accessible_after_login(client, cleanup, app):
    _create_user(app)
    _login(client, TEST_EMAIL, TEST_PASSWORD)
    response = client.get('/')
    assert response.status_code == 200
    assert b'Dashboard' in response.data or b'Welcome' in response.data
