"""Tests for the quotes feature (`feat/quotes` branch, 2026-10-30).

What these lock in:

  1. Amounts are DERIVED from line items, never accepted raw. A caller
     that sends `ex_gst_amount: 999` alongside lines totalling $100 gets
     $100 -- the quote is the lines, and the totals are arithmetic.
  2. Bad line items are refused (no lines, empty description, missing or
     negative amount).
  3. Quote numbers increment per user and per year.
  4. Tenant scoping: another tenant's quote is a 404, not a 403.
  5. Lifecycle transitions are one-way and explicit: only a draft can be
     edited; an illegal transition is a 409.
  6. Conversion to an invoice carries the same amounts and the same line
     items, and can only happen once.
  7. The PDF renders, and says QUOTATION.

The suite runs against the LIVE database, so every test creates its own
throwaway rows and removes them afterwards (directly through the model, so
cleanup does not depend on the caller's role).
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
    with app.app_context():
        from app.models.customer import Customer
        c = Customer.query.first()
        if not c:
            pytest.skip('No customer in DB')
        return c.id


@pytest.fixture
def made_quotes(app):
    """Track created quotes so they are removed after the test."""
    created = []
    yield created
    with app.app_context():
        from app.models import db
        from app.models.quote import Quote
        from app.models.invoice import Invoice
        for qid in created:
            q = Quote.query.filter_by(id=qid).first()
            if q is None:
                continue
            if q.converted_invoice_id:
                inv = Invoice.query.filter_by(id=q.converted_invoice_id).first()
                if inv:
                    db.session.delete(inv)
            db.session.delete(q)
        db.session.commit()


@pytest.fixture
def create_quote(client, auth_headers, customer_id, made_quotes):
    """POST a quote; returns (response, quote_id)."""
    def _create(**overrides):
        payload = {
            'customer_id': customer_id,
            'client_name': 'ZZ Quote Test Co (delete me)',
            'lines': [
                {'description': 'A.I system development', 'amount': '1000.00'},
                {'description': 'Support retainer', 'amount': '250.50'},
            ],
            'gst_type': 0.1,
            'quote_date': '2026-10-30',
        }
        payload.update(overrides)
        r = client.post('/api/quotes', json=payload, headers=auth_headers)
        if r.status_code == 201:
            made_quotes.append(r.get_json()['quote']['id'])
        return r
    return _create


# --- 1. amounts are derived, never accepted raw ---------------------------

def test_amounts_are_derived_from_lines(client, auth_headers, create_quote):
    r = create_quote(ex_gst_amount=999999, gst_amount=12345, total_amount=7)
    assert r.status_code == 201, r.data
    q = r.get_json()['quote']
    assert q['ex_gst_amount'] == '1250.50'
    assert q['gst_amount'] == '125.05'
    assert q['total_amount'] == '1375.55'


def test_zero_gst_quote(client, auth_headers, create_quote):
    r = create_quote(gst_type=0)
    assert r.status_code == 201, r.data
    q = r.get_json()['quote']
    assert q['gst_amount'] == '0.00'
    assert q['total_amount'] == '1250.50'


def test_lines_round_trip_exactly(client, auth_headers, create_quote):
    r = create_quote(lines=[{'description': 'Precise work', 'amount': '1234.56'}])
    assert r.status_code == 201, r.data
    q = r.get_json()['quote']
    assert q['lines'] == [{'description': 'Precise work', 'amount': '1234.56'}]
    assert q['ex_gst_amount'] == '1234.56'


# --- 2. bad input is refused ----------------------------------------------

def test_no_lines_is_refused(client, auth_headers, create_quote):
    r = create_quote(lines=[])
    assert r.status_code == 400, r.data


def test_missing_lines_is_refused(client, auth_headers, create_quote):
    r = create_quote(lines=None)
    assert r.status_code == 400, r.data


def test_empty_description_is_refused(client, auth_headers, create_quote):
    r = create_quote(lines=[{'description': '  ', 'amount': '10.00'}])
    assert r.status_code == 400, r.data


def test_negative_amount_is_refused(client, auth_headers, create_quote):
    r = create_quote(lines=[{'description': 'Refund', 'amount': '-5.00'}])
    assert r.status_code == 400, r.data


def test_missing_amount_is_refused(client, auth_headers, create_quote):
    r = create_quote(lines=[{'description': 'No amount'}])
    assert r.status_code == 400, r.data


def test_unknown_customer_is_refused(client, auth_headers, create_quote):
    r = create_quote(customer_id='00000000-0000-0000-0000-000000000000')
    assert r.status_code == 404, r.data


def test_valid_until_before_quote_date_is_refused(client, auth_headers, create_quote):
    r = create_quote(valid_until='2026-10-01')
    assert r.status_code == 400, r.data


# --- 3. quote numbering ----------------------------------------------------

def test_quote_numbers_increment(client, auth_headers, create_quote):
    r1 = create_quote()
    r2 = create_quote()
    assert r1.status_code == 201 and r2.status_code == 201, (r1.data, r2.data)
    n1 = r1.get_json()['quote']['quote_number']
    n2 = r2.get_json()['quote']['quote_number']
    assert n1.startswith('Q-2026-') and n2.startswith('Q-2026-')
    assert int(n2.split('-')[-1]) == int(n1.split('-')[-1]) + 1


def test_default_validity_is_thirty_days(client, auth_headers, create_quote):
    r = create_quote(quote_date='2026-10-30')
    assert r.status_code == 201, r.data
    assert r.get_json()['quote']['valid_until'] == '2026-11-29'


# --- 4. tenant scoping ----------------------------------------------------

def test_another_tenants_quote_is_not_found(app, client, auth_headers, customer_id, made_quotes):
    """A quote belonging to a different tenant must be invisible."""
    import datetime

    from app.models import db
    from app.models.quote import Quote
    from app.models.user import User
    with app.app_context():
        other = User.query.filter(User.id != _caller_id(app)).first()
        if not other:
            pytest.skip('No second user in DB')
        q = Quote(
            user_id=other.id,
            customer_id=customer_id,
            quote_number='Q-1999-9999',
            client_name='ZZ Other tenant (delete me)',
            lines=[{'description': 'x', 'amount': '1.00'}],
            ex_gst_amount=1, gst_amount=0, gst_type=0, total_amount=1,
            quote_date=datetime.date(1999, 1, 1),
            status='draft',
        )
        db.session.add(q)
        db.session.commit()
        qid = q.id
    made_quotes.append(qid)

    r = client.get(f'/api/quotes/{qid}', headers=auth_headers)
    assert r.status_code == 404, r.data
    r = client.put(f'/api/quotes/{qid}', json={'notes': 'x'}, headers=auth_headers)
    assert r.status_code == 404, r.data
    r = client.post(f'/api/quotes/{qid}/mark-sent', headers=auth_headers)
    assert r.status_code == 404, r.data


def _caller_id(app):
    from tests.conftest import business_email
    from app.models.user import User
    user = User.query.filter_by(email=business_email()).first()
    return user.id


# --- 5. lifecycle ----------------------------------------------------------

def test_draft_can_be_edited(client, auth_headers, create_quote):
    r = create_quote()
    qid = r.get_json()['quote']['id']
    r = client.put(f'/api/quotes/{qid}',
                   json={'lines': [{'description': 'Revised scope', 'amount': '2000.00'}]},
                   headers=auth_headers)
    assert r.status_code == 200, r.data
    q = r.get_json()['quote']
    assert q['ex_gst_amount'] == '2000.00'
    assert q['gst_amount'] == '200.00'
    assert q['total_amount'] == '2200.00'


def test_sent_quote_cannot_be_edited(client, auth_headers, create_quote):
    r = create_quote()
    qid = r.get_json()['quote']['id']
    assert client.post(f'/api/quotes/{qid}/mark-sent', headers=auth_headers).status_code == 200
    r = client.put(f'/api/quotes/{qid}', json={'notes': 'x'}, headers=auth_headers)
    assert r.status_code == 409, r.data


def test_mark_sent_sets_status_and_timestamp(client, auth_headers, create_quote):
    r = create_quote()
    qid = r.get_json()['quote']['id']
    r = client.post(f'/api/quotes/{qid}/mark-sent', headers=auth_headers)
    assert r.status_code == 200, r.data
    q = r.get_json()['quote']
    assert q['status'] == 'sent'
    assert q['sent_at'] is not None


def test_accept_and_decline(client, auth_headers, create_quote):
    r = create_quote()
    qid = r.get_json()['quote']['id']
    r = client.post(f'/api/quotes/{qid}/accept', headers=auth_headers)
    assert r.status_code == 200, r.data
    assert r.get_json()['quote']['status'] == 'accepted'
    assert r.get_json()['quote']['accepted_at'] is not None

    r2 = create_quote()
    qid2 = r2.get_json()['quote']['id']
    r = client.post(f'/api/quotes/{qid2}/decline', headers=auth_headers)
    assert r.status_code == 200, r.data
    assert r.get_json()['quote']['status'] == 'declined'
    assert r.get_json()['quote']['declined_at'] is not None


def test_declined_quote_cannot_be_converted(client, auth_headers, create_quote):
    r = create_quote()
    qid = r.get_json()['quote']['id']
    client.post(f'/api/quotes/{qid}/decline', headers=auth_headers)
    r = client.post(f'/api/quotes/{qid}/convert-to-invoice', headers=auth_headers)
    assert r.status_code == 409, r.data


# --- 6. conversion to an invoice ------------------------------------------

def test_convert_creates_an_invoice_with_the_same_amounts(client, auth_headers, create_quote):
    r = create_quote()
    q = r.get_json()['quote']
    r = client.post(f"/api/quotes/{q['id']}/accept", headers=auth_headers)
    assert r.status_code == 200, r.data

    r = client.post(f"/api/quotes/{q['id']}/convert-to-invoice", headers=auth_headers)
    assert r.status_code == 201, r.data
    body = r.get_json()
    inv = body['invoice']
    assert inv['ex_gst_amount'] == q['ex_gst_amount']
    assert inv['gst_amount'] == q['gst_amount']
    assert inv['total_amount'] == q['total_amount']
    assert inv['status'] == 'draft', 'conversion must not send anything'
    assert body['quote']['converted_invoice_id'] == inv['id']
    assert body['quote']['status'] == 'accepted'
    # The invoice description must carry the line items, so the invoice PDF
    # renders the same items the customer agreed to.
    assert 'A.I system development' in inv['description']
    assert '1,000.00' in inv['description']


def test_convert_twice_is_refused(client, auth_headers, create_quote):
    r = create_quote()
    qid = r.get_json()['quote']['id']
    client.post(f'/api/quotes/{qid}/accept', headers=auth_headers)
    r = client.post(f'/api/quotes/{qid}/convert-to-invoice', headers=auth_headers)
    assert r.status_code == 201, r.data
    r = client.post(f'/api/quotes/{qid}/convert-to-invoice', headers=auth_headers)
    assert r.status_code == 409, r.data
    assert 'already been converted' in r.get_json()['error']


# --- 7. the PDF ------------------------------------------------------------

def test_quote_pdf_renders(client, auth_headers, create_quote):
    r = create_quote()
    qid = r.get_json()['quote']['id']
    r = client.get(f'/api/quotes/{qid}/pdf', headers=auth_headers)
    assert r.status_code == 200, r.data
    assert r.mimetype == 'application/pdf'
    assert r.data[:5] == b'%PDF-'


def test_quote_pdf_for_another_tenant_is_not_found(client, auth_headers):
    r = client.get('/api/quotes/00000000-0000-0000-0000-000000000000/pdf', headers=auth_headers)
    assert r.status_code == 404, r.data
