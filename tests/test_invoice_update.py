"""Tests for PUT /api/invoices/<id> field coverage.

Context (2026-10-23): the `notes` column exists on the Invoice model
(added with the payment_reference / payment_method / notes migration in
commit 553485e) but `update_invoice` never accepted it in the request
body. A PUT containing `notes` returned HTTP 200 and silently ignored the
field -- so provenance notes ("payment received via periodic arrangement")
could not be recorded through the API, and the only alternative was a
direct SQL write.

These tests lock in:
  1. `notes` is settable via PUT.
  2. `notes` is clearable (set to None) via PUT.
  3. Unrelated fields in the same request still work (no regression).
  4. A PUT that omits `notes` leaves the stored value untouched.

The suite runs against the LIVE database, so each test creates its own
throwaway invoice and deletes it afterwards.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import create_app


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
        from app.models.user import User
        user = User.query.filter_by(email=business_email()).first()
        if not user:
            pytest.skip('No user with API key found in DB')
        key = api_key_plain_for(user.id)
        if not key:
            pytest.skip('No user with API key found in DB')
        return {'X-API-Key': key}


@pytest.fixture
def customer_id(app):
    """Any existing customer -- the invoice row only borrows the FK."""
    with app.app_context():
        from app.models.customer import Customer
        c = Customer.query.first()
        if not c:
            pytest.skip('No customer in DB')
        return c.id


@pytest.fixture
def invoice_id(client, auth_headers, customer_id):
    """Create a throwaway draft invoice; delete it on teardown."""
    payload = {
        'client_name': 'ZZ Test Co (delete me)',
        'customer_id': customer_id,
        'description': 'temporary invoice for notes-field test',
        'ex_gst_amount': 10.0,
        'gst_type': 0.1,
        'invoice_date': '2026-01-01',
    }
    r = client.post('/api/invoices', json=payload, headers=auth_headers)
    assert r.status_code in (200, 201), r.data
    body = r.get_json()
    inv = body.get('invoice', body)
    iid = inv['id']
    yield iid
    client.delete(f'/api/invoices/{iid}', headers=auth_headers)


def _get(client, auth_headers, iid):
    r = client.get(f'/api/invoices/{iid}', headers=auth_headers)
    assert r.status_code == 200, r.data
    body = r.get_json()
    return body.get('invoice', body)


def test_notes_is_settable(client, auth_headers, invoice_id):
    r = client.put(
        f'/api/invoices/{invoice_id}',
        json={'notes': 'Payment received via periodic arrangement.'},
        headers=auth_headers,
    )
    assert r.status_code == 200, r.data
    assert _get(client, auth_headers, invoice_id)['notes'] == \
        'Payment received via periodic arrangement.'


def test_notes_is_clearable(client, auth_headers, invoice_id):
    client.put(
        f'/api/invoices/{invoice_id}',
        json={'notes': 'temporary'},
        headers=auth_headers,
    )
    r = client.put(
        f'/api/invoices/{invoice_id}',
        json={'notes': None},
        headers=auth_headers,
    )
    assert r.status_code == 200, r.data
    assert _get(client, auth_headers, invoice_id)['notes'] is None


def test_notes_alongside_other_fields(client, auth_headers, invoice_id):
    r = client.put(
        f'/api/invoices/{invoice_id}',
        json={
            'notes': 'both fields at once',
            'description': 'updated description',
        },
        headers=auth_headers,
    )
    assert r.status_code == 200, r.data
    stored = _get(client, auth_headers, invoice_id)
    assert stored['notes'] == 'both fields at once'
    assert stored['description'] == 'updated description'


def test_omitting_notes_leaves_value_untouched(client, auth_headers, invoice_id):
    client.put(
        f'/api/invoices/{invoice_id}',
        json={'notes': 'keep me'},
        headers=auth_headers,
    )
    r = client.put(
        f'/api/invoices/{invoice_id}',
        json={'description': 'touch something else'},
        headers=auth_headers,
    )
    assert r.status_code == 200, r.data
    assert _get(client, auth_headers, invoice_id)['notes'] == 'keep me'
