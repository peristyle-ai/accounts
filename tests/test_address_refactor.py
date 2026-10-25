"""Tests for the structured-address write paths.

Covers the customer and user (business) create/update routes:

- Structured fields accepted on create; legacy `address` blob auto-reassembled
- Structured fields accepted on update; legacy `address` blob auto-reassembled
- State normalised to uppercase
- Postcode whitespace stripped
- Invalid state rejected (400) with a helpful message
- Invalid postcode rejected (400) with a helpful message
- Non-AU country skips state/postcode validation
- Backwards compat: legacy `address` blob still accepted as the sole input

Run with:
    cd /opt/accounts && ./venv/bin/python3 -m pytest tests/test_address_refactor.py -v
"""

import os
import sys
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


from app import create_app
from app.models import db
from app.models.user import User


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
def auth_headers(app):
    from tests.conftest import api_key_plain_for, business_email
    with app.app_context():
        user = User.query.filter_by(email=business_email()).first()
        if not user:
            pytest.skip('No user with API key found in DB')
        key = api_key_plain_for(user.id)
        if not key:
            pytest.skip('No user with API key found in DB')
        return {'X-API-Key': key}


def _unique_name(prefix):
    """Build a unique customer name so the test is repeatable."""
    import uuid
    return f'{prefix} {uuid.uuid4().hex[:8]}'


class TestCustomerCreateStructured:
    def test_accepts_structured_fields_and_reassembles_blob(self, client, auth_headers):
        r = client.post(
            '/api/customers',
            json={
                'name': _unique_name('Test Refactor'),
                'address_line1': '99 Test Street',
                'city': 'Testville',
                'state': 'qld',  # lowercase — should be normalised
                'postcode': '  4000  ',  # whitespace — should be stripped
            },
            headers=auth_headers,
        )
        assert r.status_code == 201, r.get_data(as_text=True)
        body = r.get_json()
        c = body['customer']
        # Structured fields persisted
        assert c['address_line1'] == '99 Test Street'
        assert c['city'] == 'Testville'
        # Normalised
        assert c['state'] == 'QLD'
        assert c['postcode'] == '4000'
        # The legacy `address` field is no longer in the response — it was
        # dropped from the schema along with the column.
        assert 'address' not in c

    def test_invalid_state_rejected(self, client, auth_headers):
        r = client.post(
            '/api/customers',
            json={'name': _unique_name('Bad State'), 'state': 'ZZ', 'postcode': '4000'},
            headers=auth_headers,
        )
        assert r.status_code == 400
        assert 'state must be one of' in r.get_json()['error']

    def test_invalid_postcode_rejected(self, client, auth_headers):
        r = client.post(
            '/api/customers',
            json={'name': _unique_name('Bad PC'), 'state': 'QLD', 'postcode': 'abc'},
            headers=auth_headers,
        )
        assert r.status_code == 400
        assert '4 digits' in r.get_json()['error']

    def test_legacy_field_rejected(self, client, auth_headers):
        """After the 2026-09-17 second pass, the legacy free-text `address`
        field is no longer accepted on writes — it would have nowhere to go
        since the column was dropped from the schema."""
        r = client.post(
            '/api/customers',
            json={
                'name': _unique_name('Legacy Reject'),
                'address': '1 Legacy Lane, Brisbane QLD 4000',
            },
            headers=auth_headers,
        )
        assert r.status_code == 400
        assert 'legacy `address` field is no longer supported' in r.get_json()['error']

        # Same on update
        r2 = client.post(
            '/api/customers',
            json={'name': _unique_name('Legacy Update')},
            headers=auth_headers,
        )
        cid = r2.get_json()['customer']['id']
        r = client.put(
            f'/api/customers/{cid}',
            json={'address': '1 Legacy Lane'},
            headers=auth_headers,
        )
        assert r.status_code == 400
        assert 'legacy `address` field is no longer supported' in r.get_json()['error']

    def test_non_au_country_skips_validation(self, client, auth_headers):
        """For non-AU addresses we accept any state string and don't enforce
        4-digit postcodes (e.g. US ZIP codes are 5 digits)."""
        r = client.post(
            '/api/customers',
            json={
                'name': _unique_name('US Test'),
                'address_line1': '1 Infinite Loop',
                'city': 'Cupertino',
                'state': 'CA',
                'postcode': '95014',
                'country': 'United States',
            },
            headers=auth_headers,
        )
        assert r.status_code == 201, r.get_data(as_text=True)
        c = r.get_json()['customer']
        assert c['state'] == 'CA'  # not uppercased (CA is already valid)
        assert c['postcode'] == '95014'
        assert c['country'] == 'United States'


class TestCustomerUpdateStructured:
    def test_update_structured_fields_reassembles_blob(self, client, auth_headers):
        # Create a fresh customer
        name = _unique_name('Update Test')
        r = client.post(
            '/api/customers',
            json={'name': name},
            headers=auth_headers,
        )
        assert r.status_code == 201
        cid = r.get_json()['customer']['id']

        # Update with structured fields
        r = client.put(
            f'/api/customers/{cid}',
            json={
                'address_line1': '5 Updated Ave',
                'city': 'Newtown',
                'state': 'nsw',  # lowercase
                'postcode': '2042',
            },
            headers=auth_headers,
        )
        assert r.status_code == 200, r.get_data(as_text=True)
        c = r.get_json()['customer']
        assert c['address_line1'] == '5 Updated Ave'
        assert c['city'] == 'Newtown'
        assert c['state'] == 'NSW'  # normalised
        assert c['postcode'] == '2042'
        assert 'address' not in c  # legacy field gone

    def test_update_with_invalid_state_rejected(self, client, auth_headers):
        # Create a fresh customer
        r = client.post(
            '/api/customers',
            json={'name': _unique_name('Update Bad')},
            headers=auth_headers,
        )
        cid = r.get_json()['customer']['id']

        # Try to update with a bad state — should be 400, not 200.
        r = client.put(
            f'/api/customers/{cid}',
            json={'state': 'QQ', 'postcode': '4000'},
            headers=auth_headers,
        )
        assert r.status_code == 400
        assert 'state must be one of' in r.get_json()['error']

        # Confirm the bad state didn't sneak through (read it back)
        r = client.get(f'/api/customers/{cid}', headers=auth_headers)
        c = r.get_json()['customer']
        assert c['state'] is None  # unchanged


class TestBusinessUpdateStructured:
    """The business owner's address (User.address_*) has its own route
    (`/api/auth/business` PUT). It uses the same validators.

    The `restore_user` fixture (tests/conftest.py) snapshots the
    `payroll@example.com` user row on entry and restores it on exit, so
    any mutations made via the API during the test cannot leak into
    production.
    """

    def test_business_address_update_normalises_state(self, client, auth_headers, restore_user):
        r = client.put(
            '/api/auth/business',
            json={
                'address_line1': '99 New St',
                'city': 'Brisbane',
                'state': 'qld',
                'postcode': '4000',
                'address_country': 'Australia',
            },
            headers=auth_headers,
        )
        assert r.status_code == 200, r.get_data(as_text=True)
        body = r.get_json()['user']
        assert body['state'] == 'QLD'
        assert body['postcode'] == '4000'
        # Legacy `address` field is gone from the response
        assert 'address' not in body


class TestRenderAddressSystemSettingsFallback:
    """render_address() should fall back to the BUSINESS_ADDRESS_*
    system_settings entries when the structured fields on the object
    are empty. Added 2026-09-29 after the top-right header on the
    Statement of Account PDF was found to render a placeholder
    ('1 Example St') because the user row had placeholder values
    instead of the real business address.

    The user row remains the per-tenant override; the system_settings
    defaults are the safety net so a fresh install or wiped user row
    never falls back to a placeholder string.
    """

    def test_full_user_overrides_settings(self, app):
        from types import SimpleNamespace
        from app.shared.address import render_address
        from app.models.system_setting import seed_defaults

        with app.app_context():
            seed_defaults()
            u = SimpleNamespace(
                address_line1='Tenant Specific',
                address_line2=None,
                city='West End',
                state='QLD',
                postcode='4101',
                address_country='Australia',
                country='AU',
            )
            rendered = render_address(u)
            assert 'Tenant Specific' in rendered
            assert 'West End QLD 4101' in rendered
            # Settings must NOT bleed through when the user has them.
            assert 'Fisher St' not in rendered

    def test_empty_user_falls_back_to_settings(self, app):
        from types import SimpleNamespace
        from app.shared.address import render_address
        from app.models.system_setting import seed_defaults

        with app.app_context():
            seed_defaults()
            u = SimpleNamespace(
                address_line1=None,
                address_line2=None,
                city=None,
                state=None,
                postcode=None,
                address_country=None,
                country=None,
            )
            rendered = render_address(u)
            # The system_settings defaults take over. Asserted against the
            # SETTINGS, not literals: the default business address is
            # deployment configuration and differs between a fresh install
            # and a real one.
            from app.models.system_setting import get_setting
            assert get_setting('BUSINESS_ADDRESS_LINE1') in rendered, rendered
            assert (
                f"{get_setting('BUSINESS_CITY')} {get_setting('BUSINESS_STATE')} "
                f"{get_setting('BUSINESS_POSTCODE')}"
            ) in rendered, rendered

    def test_partial_user_fills_gaps_from_settings(self, app):
        from types import SimpleNamespace
        from app.shared.address import render_address
        from app.models.system_setting import seed_defaults

        with app.app_context():
            seed_defaults()
            u = SimpleNamespace(
                address_line1='Custom Suite',
                address_line2=None,
                city=None,         # missing — should pull from settings
                state='NSW',       # tenant-specific state
                postcode=None,     # missing — should pull from settings
                address_country='Australia',
                country='AU',
            )
            rendered = render_address(u)
            assert 'Custom Suite' in rendered
            # Missing fields pulled from system_settings. Asserted against
            # the SETTING, not a literal: the default business address is
            # deployment configuration and differs between a fresh install
            # and a real one.
            from app.models.system_setting import get_setting
            assert get_setting('BUSINESS_CITY') in rendered
            assert get_setting('BUSINESS_POSTCODE') in rendered
            # Tenant-specific state still wins over the settings default
            assert 'NSW' in rendered

    def test_render_outside_app_context_does_not_raise(self):
        """Tests, scripts, and REPL sessions call render_address without
        a Flask app context. The fallback path must skip the system_settings
        DB lookup gracefully and return what the object has, instead of
        raising RuntimeError.
        """
        from types import SimpleNamespace
        from app.shared.address import render_address

        u = SimpleNamespace(
            address_line1='No Context',
            address_line2=None,
            city='Sample Suburb',
            state='QLD',
            postcode='4000',
            address_country='Australia',
            country='AU',
        )
        # Should not raise even though no app context is active.
        rendered = render_address(u)
        assert 'No Context' in rendered
        assert 'Sample Suburb QLD 4000' in rendered
