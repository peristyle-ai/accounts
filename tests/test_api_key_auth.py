"""Tests for API-key authentication and the storage format of the key.

    @api_key_required  ->  X-API-Key header

Why these tests exist
---------------------
`accounts.user_payment_settings.api_key` is DOCUMENTED as Fernet
ciphertext and the model's setter encrypts -- but every row seeded before
the F-05 encryption refactor still held the PLAINTEXT hex token. Two
things were broken by that mismatch, and both were invisible until you
tried to use them:

  1. `decrypt_pii()` raises `InvalidToken` on a legacy row, so
     `api_key_plain`, `User.get_api_key()` and `to_dict(masked=False)`
     all CRASHED on the live rows. The only reason nothing had noticed
     is that almost nothing called them.

  2. `@api_key_required` matched the plaintext against the RAW column.
     The moment a row was encrypted through the setter, that comparison
     stopped matching and EVERY API client 401'd at once -- payroll
     wrapper, invoicing script, cron jobs, STP export. Rotating a key (which
     the open-source cleanup requires) was therefore a way to break the
     whole payroll cycle in one click.

The auth path now has ONE format: the column holds Fernet ciphertext,
`find_by_api_key` decrypts before comparing. Legacy plaintext rows were
migrated in place (preserving the plaintext, so no client changed), and
the tolerant read path was then removed -- a row that is not valid
ciphertext now yields None instead of authenticating.

Safety
------
The suite runs against the LIVE production Postgres (see conftest.py).
The fixtures here create a throwaway user and delete it on teardown,
including on failure. No production row is modified.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import create_app
from app.models import db
from app.models.user import User
from app.models.user_payment_settings import UserPaymentSettings
from app.shared.pii import is_encrypted_pii


TEST_EMAIL = 'apikey-test@example.com'
TEST_PASSWORD = 'not-a-real-password'

# A cheap, user-scoped endpoint: it echoes the authenticated user.
ENDPOINT = '/api/auth/business'

API_KEY = 'a' * 64

# A value that is NOT a Fernet ciphertext. The column only ever holds
# ciphertext, so a row holding this can never authenticate -- it is the
# pre-encryption format, and it is no longer supported.
NON_CIPHERTEXT_VALUE = 'b' * 64


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


@pytest.fixture
def test_user(app):
    """A throwaway user with a payment_settings row. Deleted on teardown."""
    from app.models.user_profile import UserProfile
    with app.app_context():
        user = User.query.filter_by(email=TEST_EMAIL).first()
        if user is None:
            user = User(email=TEST_EMAIL, display_name='API Key Test', role='member')
            user.set_password(TEST_PASSWORD)
            db.session.add(user)
            db.session.commit()
            db.session.add(UserProfile(user_id=user.id))
            db.session.commit()
        # Make sure the payment_settings row exists (it holds the key).
        if not user.payment_settings:
            user.payment_settings = UserPaymentSettings(user_id=user.id)
            db.session.commit()
        user_id = user.id

    yield user_id

    with app.app_context():
        User.query.filter_by(email=TEST_EMAIL).delete()
        db.session.commit()


def _set_key(app, user_id, value, encrypted=True):
    """Store `value` on the user's payment_settings row.

    encrypted=True  -> through the setter (Fernet ciphertext at rest)
    encrypted=False -> straight into the column, bypassing the setter
                       (a value that is NOT ciphertext -- unsupported)
    """
    with app.app_context():
        ups = UserPaymentSettings.query.filter_by(user_id=user_id).first()
        if encrypted:
            ups.api_key_plain = value
        else:
            ups.api_key = value
        db.session.commit()


def _stored(app, user_id):
    with app.app_context():
        ups = UserPaymentSettings.query.filter_by(user_id=user_id).first()
        return ups.api_key


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

class TestAuthentication:
    def test_no_key_is_rejected(self, client):
        assert client.get(ENDPOINT).status_code == 401

    def test_wrong_key_is_rejected(self, client):
        r = client.get(ENDPOINT, headers={'X-API-Key': 'not-a-real-key'})
        assert r.status_code == 401

    def test_a_non_ciphertext_row_does_not_authenticate(self, app, client, test_user):
        """The pre-encryption storage format is no longer supported.

        A row holding a raw token can never authenticate: it is not
        ciphertext, so there is nothing to decrypt-and-compare.
        """
        _set_key(app, test_user, NON_CIPHERTEXT_VALUE, encrypted=False)
        r = client.get(ENDPOINT, headers={'X-API-Key': NON_CIPHERTEXT_VALUE})
        assert r.status_code == 401

    def test_encrypted_key_authenticates(self, app, client, test_user):
        """A row holding a Fernet ciphertext must work."""
        _set_key(app, test_user, API_KEY)
        r = client.get(ENDPOINT, headers={'X-API-Key': API_KEY})
        assert r.status_code == 200
        assert r.get_json()['user']['email'] == TEST_EMAIL

    def test_the_ciphertext_is_not_accepted_as_a_key(self, app, client, test_user):
        """The stored ciphertext is not a credential.

        It lives in the DB and is surfaced by the `User.api_key`
        property, so it must not authenticate.
        """
        _set_key(app, test_user, API_KEY)
        ciphertext = _stored(app, test_user)
        assert ciphertext != API_KEY
        r = client.get(ENDPOINT, headers={'X-API-Key': ciphertext})
        assert r.status_code == 401

    def test_a_key_belonging_to_another_user_does_not_authenticate(self, app, client, test_user):
        _set_key(app, test_user, API_KEY)
        r = client.get(ENDPOINT, headers={'X-API-Key': 'c' * 64})
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# Rotation
# ---------------------------------------------------------------------------

class TestRotation:
    def test_rotation_invalidates_the_old_key(self, app, client, test_user):
        """The whole point of the fix: re-encrypting a key must not 401
        the client that is still using it."""
        _set_key(app, test_user, API_KEY)

        # Rotate through the encrypting setter -- this is what the HMI
        # "Generate New API Key" button does.
        with app.app_context():
            user = User.query.filter_by(email=TEST_EMAIL).first()
            new_key = user.generate_api_key()
            db.session.commit()

        assert new_key != API_KEY
        assert client.get(ENDPOINT, headers={'X-API-Key': API_KEY}).status_code == 401
        assert client.get(ENDPOINT, headers={'X-API-Key': new_key}).status_code == 200

    def test_rotation_keeps_the_new_key_usable_after_a_restart(self, app, client, test_user):
        """The key must survive being read back from the DB, not just the
        in-memory object it was set on."""
        with app.app_context():
            user = User.query.filter_by(email=TEST_EMAIL).first()
            new_key = user.generate_api_key()
            db.session.commit()
            db.session.expire_all()
            assert user.get_api_key() == new_key

        assert client.get(ENDPOINT, headers={'X-API-Key': new_key}).status_code == 200


# ---------------------------------------------------------------------------
# Storage format helpers
# ---------------------------------------------------------------------------

class TestStorageHelpers:
    def test_api_key_plain_on_a_non_ciphertext_row_returns_none(self, app, test_user):
        """This used to raise InvalidToken and take the caller down.

        It now returns None: the value is unreadable, so there is no
        key. It must not raise, because this accessor sits on the
        API-key page and inside to_dict().
        """
        _set_key(app, test_user, NON_CIPHERTEXT_VALUE, encrypted=False)
        with app.app_context():
            ups = UserPaymentSettings.query.filter_by(user_id=test_user).first()
            assert ups.api_key_plain is None
            assert ups.api_key_is_encrypted is False

    def test_api_key_plain_on_an_encrypted_row_decrypts(self, app, test_user):
        _set_key(app, test_user, API_KEY)
        with app.app_context():
            ups = UserPaymentSettings.query.filter_by(user_id=test_user).first()
            assert ups.api_key_plain == API_KEY
            assert ups.api_key_is_encrypted is True

    def test_get_api_key_on_a_non_ciphertext_row_returns_none(self, app, test_user):
        _set_key(app, test_user, NON_CIPHERTEXT_VALUE, encrypted=False)
        with app.app_context():
            user = User.query.filter_by(email=TEST_EMAIL).first()
            assert user.get_api_key() is None

    def test_to_dict_unmasked_on_a_non_ciphertext_row_does_not_raise(self, app, test_user):
        _set_key(app, test_user, NON_CIPHERTEXT_VALUE, encrypted=False)
        with app.app_context():
            ups = UserPaymentSettings.query.filter_by(user_id=test_user).first()
            d = ups.to_dict(masked=False)
            assert d['api_key'] is None

    def test_to_dict_masked_never_returns_the_key(self, app, test_user):
        _set_key(app, test_user, API_KEY)
        with app.app_context():
            ups = UserPaymentSettings.query.filter_by(user_id=test_user).first()
            d = ups.to_dict(masked=True)
            assert 'api_key' not in d
            assert d['api_key_set'] is True

    def test_find_by_api_key_returns_none_for_an_empty_key(self, app):
        with app.app_context():
            assert UserPaymentSettings.find_by_api_key(None) is None
            assert UserPaymentSettings.find_by_api_key('') is None

    def test_find_by_api_key_returns_none_for_an_unknown_key(self, app, test_user):
        _set_key(app, test_user, API_KEY)
        with app.app_context():
            assert UserPaymentSettings.find_by_api_key('c' * 64) is None


# ---------------------------------------------------------------------------
# The live rows
# ---------------------------------------------------------------------------

class TestLiveRows:
    def test_no_live_row_raises_when_decrypted(self, app):
        """Guard the original bug: every stored key must be readable.

        Every live row must hold valid ciphertext that decrypts to a
        non-empty key. This test fails loudly if a row is corrupted or
        still in the retired pre-encryption format.
        """
        with app.app_context():
            rows = UserPaymentSettings.query.filter(
                UserPaymentSettings.api_key.isnot(None)
            ).all()
            for row in rows:
                plain = row.api_key_plain      # must not raise
                assert plain, (
                    f'user {row.user_id} has a key that is not valid ciphertext '
                    f'(stored={row.api_key[:12]!r})'
                )
                assert is_encrypted_pii(row.api_key) == row.api_key_is_encrypted
                assert row.api_key_is_encrypted, (
                    f'user {row.user_id} still holds a non-ciphertext key'
                )
