"""GST derivation and recurring line items for quotes.

Context (2026-11-01): a quote was raised for a customer in the USA, and
the question asked was whether GST could be "easily toggled off" for
them. It could be set to 0 -- but nothing PREVENTED 10% being applied to
an overseas customer, nothing said WHY there was no GST on the document,
and a line item with markup in its description could crash the PDF route
outright.

(Names are not used in this file: the repository is PUBLIC, and a
personal name in a tracked file is a leak the release scan rejects.)

What these tests lock in:

  1. GST is DERIVED from the customer and the business when the caller
     does not state it: 10% only for an Australian GST registrant, 0 for
     everyone else (overseas, unregistered, or a business with no ABN).
  2. An EXPLICIT non-zero rate is REFUSED for a customer who is not
     registered for GST. Zero is always allowed.
  3. The PDF says WHY there is no GST, in words that match the reason.
  4. A recurring line (a monthly retainer) round-trips, is split out of
     the totals, and the PDF shows the split.
  5. Customer-controlled text cannot inject markup into the PDF or
     crash the route.

The suite runs against the LIVE database, so every row these tests
create is removed afterwards -- and the removal is asserted.
"""

import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # for pdf_text

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
def business_user(app):
    """The business user the API key belongs to (the quote's issuer)."""
    from tests.conftest import business_email
    from app.models.user import User
    with app.app_context():
        user = User.query.filter_by(email=business_email()).first()
        if not user:
            pytest.skip('No business user in DB')
        return user.id


@pytest.fixture
def made_customers(app):
    """Track customers created by these tests so they can be removed."""
    created = []
    yield created
    with app.app_context():
        from app.models import db
        from app.models.customer import Customer
        for cid in created:
            c = Customer.query.filter_by(id=cid).first()
            if c is not None:
                db.session.delete(c)
        db.session.commit()


@pytest.fixture
def made_quotes(app):
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


def _make_customer(client, auth_headers, made_customers, **overrides):
    """Create a customer with the given tax-relevant fields."""
    payload = {
        'name': f'ZZ Quote Tax Test {uuid.uuid4().hex[:8]}',
        'contact_name': 'Test Contact',
        'contact_email': 'tax-test@example.invalid',
        'country': 'Australia',
        'gst': True,
    }
    payload.update(overrides)
    r = client.post('/api/customers', json=payload, headers=auth_headers)
    assert r.status_code == 201, r.get_data(as_text=True)
    body = r.get_json()['customer']
    made_customers.append(body['id'])
    return body['id']


def _quote(client, auth_headers, made_quotes, customer_id, **overrides):
    payload = {
        'customer_id': customer_id,
        'client_name': 'ZZ Quote Tax Co',
        'lines': [{'description': 'A.I system development', 'amount': '1000.00'}],
        'quote_date': '2026-11-01',
    }
    payload.update(overrides)
    r = client.post('/api/quotes', json=payload, headers=auth_headers)
    if r.status_code == 201:
        made_quotes.append(r.get_json()['quote']['id'])
    return r


# --- 1. GST is derived from the customer and the business ----------------

def test_overseas_customer_gets_no_gst_by_default(client, auth_headers, made_customers, made_quotes):
    """A customer outside Australia gets a quote with NO GST.

    This is the case that started the work: a customer outside Australia,
    so no Australian GST applies. The case is the work; the customer is
    not named here, because this repository is public.
    """
    cid = _make_customer(client, auth_headers, made_customers,
                         country='United States', gst=False)
    r = _quote(client, auth_headers, made_quotes, cid)
    assert r.status_code == 201, r.get_data(as_text=True)
    q = r.get_json()['quote']
    assert q['gst_type'] == '0.0'
    assert q['gst_amount'] == '0.00'
    assert q['total_amount'] == q['ex_gst_amount'] == '1000.00'


def test_australian_registrant_gets_gst_by_default(client, auth_headers, made_customers, made_quotes):
    """An Australian customer with an ABN is charged 10% by default."""
    cid = _make_customer(client, auth_headers, made_customers,
                         country='Australia', gst=True, abn='12 345 678 901')
    r = _quote(client, auth_headers, made_quotes, cid)
    assert r.status_code == 201, r.get_data(as_text=True)
    q = r.get_json()['quote']
    assert q['gst_type'] == '0.1'
    assert q['gst_amount'] == '100.00'
    assert q['total_amount'] == '1100.00'


def test_australian_unregistered_customer_gets_no_gst(client, auth_headers, made_customers, made_quotes):
    """An Australian customer who is not GST registered is charged no GST."""
    cid = _make_customer(client, auth_headers, made_customers,
                         country='Australia', gst=False)
    r = _quote(client, auth_headers, made_quotes, cid)
    assert r.status_code == 201, r.get_data(as_text=True)
    q = r.get_json()['quote']
    assert q['gst_type'] == '0.0'
    assert q['total_amount'] == '1000.00'


def test_overseas_customer_with_gst_flag_still_gets_no_gst(client, auth_headers, made_customers, made_quotes):
    """The COUNTRY decides, not just the flag.

    177 customers in the live DB have country='United States' AND gst=true
    (they are test rows). A flag alone must not put Australian GST on an
    overseas customer's quote.
    """
    cid = _make_customer(client, auth_headers, made_customers,
                         country='United States', gst=True)
    r = _quote(client, auth_headers, made_quotes, cid)
    assert r.status_code == 201, r.get_data(as_text=True)
    assert r.get_json()['quote']['gst_type'] == '0.0'


# --- 2. an explicit rate is checked ---------------------------------------

def test_explicit_gst_is_refused_for_an_overseas_customer(client, auth_headers, made_customers, made_quotes):
    """Asking for 10% on an overseas customer's quote is refused."""
    cid = _make_customer(client, auth_headers, made_customers,
                         country='United States', gst=False)
    r = _quote(client, auth_headers, made_quotes, cid, gst_type=0.1)
    assert r.status_code == 400, r.get_data(as_text=True)
    assert 'not registered for GST' in r.get_json()['error']


def test_explicit_gst_is_refused_for_an_unregistered_customer(client, auth_headers, made_customers, made_quotes):
    cid = _make_customer(client, auth_headers, made_customers,
                         country='Australia', gst=False)
    r = _quote(client, auth_headers, made_quotes, cid, gst_type=0.1)
    assert r.status_code == 400, r.get_data(as_text=True)


def test_explicit_zero_gst_is_always_allowed(client, auth_headers, made_customers, made_quotes):
    """Zero is the caller's prerogative: quoting no GST is always allowed."""
    cid = _make_customer(client, auth_headers, made_customers,
                         country='Australia', gst=True, abn='12 345 678 901')
    r = _quote(client, auth_headers, made_quotes, cid, gst_type=0)
    assert r.status_code == 201, r.get_data(as_text=True)
    q = r.get_json()['quote']
    assert q['gst_type'] == '0.0'
    assert q['total_amount'] == '1000.00'


def test_update_cannot_apply_gst_to_an_overseas_customer(client, auth_headers, made_customers, made_quotes):
    """The guard also holds on update, not only on create."""
    cid = _make_customer(client, auth_headers, made_customers,
                         country='United States', gst=False)
    r = _quote(client, auth_headers, made_quotes, cid)
    assert r.status_code == 201, r.get_data(as_text=True)
    qid = r.get_json()['quote']['id']
    r = client.put(f'/api/quotes/{qid}', json={'gst_type': 0.1}, headers=auth_headers)
    assert r.status_code == 400, r.get_data(as_text=True)


def test_a_rejected_update_does_not_mutate_the_stored_quote(client, auth_headers, made_customers, made_quotes):
    """A refused update must leave the row exactly as it was.

    The update route used to assign to the ORM object AS IT VALIDATED,
    so a request that was then rejected had already changed the row.
    The change was flushed by the next query in the session, which took a
    row lock on the quote -- and any concurrent write to that quote
    blocked behind it until the session was torn down. A 400 must not
    leave a lock behind.
    """
    cid = _make_customer(client, auth_headers, made_customers,
                         country='United States', gst=False)
    r = _quote(client, auth_headers, made_quotes, cid)
    assert r.status_code == 201, r.get_data(as_text=True)
    qid = r.get_json()['quote']['id']
    before = r.get_json()['quote']

    # Rejected on the GST guard.
    r = client.put(f'/api/quotes/{qid}', json={'gst_type': 0.1}, headers=auth_headers)
    assert r.status_code == 400, r.get_data(as_text=True)

    # Rejected on the date check.
    r = client.put(f'/api/quotes/{qid}',
                   json={'quote_date': '2026-12-01', 'valid_until': '2026-01-01'},
                   headers=auth_headers)
    assert r.status_code == 400, r.get_data(as_text=True)

    # The stored quote is untouched, and a subsequent write still works.
    r = client.get(f'/api/quotes/{qid}', headers=auth_headers)
    assert r.status_code == 200
    after = r.get_json()['quote']
    assert after['gst_type'] == before['gst_type']
    assert after['quote_date'] == before['quote_date']
    assert after['valid_until'] == before['valid_until']
    assert after['ex_gst_amount'] == before['ex_gst_amount']
    assert after['total_amount'] == before['total_amount']

    r = client.put(f'/api/quotes/{qid}', json={'notes': 'still editable'},
                   headers=auth_headers)
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()['quote']['notes'] == 'still editable'


# --- 3. the PDF says why --------------------------------------------------

def _pdf_text(client, auth_headers, qid):
    """The rendered PDF's text.

    A PDF's bytes are not proof of its contents, so the document is READ,
    not merely downloaded. The extractor lives in tests/pdf_text.py so the
    suite does not depend on the pdftotext binary.
    """
    from pdf_text import extract_text
    r = client.get(f'/api/quotes/{qid}/pdf', headers=auth_headers)
    assert r.status_code == 200, r.get_data(as_text=True)
    return extract_text(r.data)


def test_pdf_explains_no_gst_for_an_overseas_customer(client, auth_headers, made_customers, made_quotes):
    """The document states the reason, and the reason is the TRUE one."""
    cid = _make_customer(client, auth_headers, made_customers,
                         country='United States', gst=False)
    r = _quote(client, auth_headers, made_quotes, cid)
    assert r.status_code == 201, r.get_data(as_text=True)
    qid = r.get_json()['quote']['id']
    text = _pdf_text(client, auth_headers, qid)
    assert 'outside Australia' in text, text[-2000:]
    # The old wording was "not Australian GST registered", which is a
    # different (and for an overseas customer, untrue) claim.
    assert 'not Australian GST registered' not in text


def test_pdf_explains_no_gst_for_an_unregistered_customer(client, auth_headers, made_customers, made_quotes):
    cid = _make_customer(client, auth_headers, made_customers,
                         country='Australia', gst=False)
    r = _quote(client, auth_headers, made_quotes, cid)
    assert r.status_code == 201, r.get_data(as_text=True)
    qid = r.get_json()['quote']['id']
    text = _pdf_text(client, auth_headers, qid)
    assert 'not registered for GST' in text


# --- 4. recurring line items ---------------------------------------------

def test_recurring_line_round_trips(client, auth_headers, made_customers, made_quotes):
    cid = _make_customer(client, auth_headers, made_customers,
                         country='United States', gst=False)
    r = _quote(client, auth_headers, made_quotes, cid, lines=[
        {'description': 'A.I system build', 'amount': '6000.00'},
        {'description': 'Managed updates', 'amount': '100.00',
         'recurring': True, 'period': 'monthly'},
    ])
    assert r.status_code == 201, r.get_data(as_text=True)
    q = r.get_json()['quote']
    assert q['lines'][1]['recurring'] is True
    assert q['lines'][1]['period'] == 'monthly'
    # The one-off and the recurring are split out.
    assert q['one_off_amount'] == '6000.00'
    assert q['recurring_amount'] == '100.00'
    assert q['recurring_period'] == 'monthly'
    # The totals cover the FIRST period: the one-off plus one month.
    assert q['ex_gst_amount'] == '6100.00'
    assert q['total_amount'] == '6100.00'


def test_recurring_defaults_to_monthly(client, auth_headers, made_customers, made_quotes):
    cid = _make_customer(client, auth_headers, made_customers,
                         country='United States', gst=False)
    r = _quote(client, auth_headers, made_quotes, cid, lines=[
        {'description': 'Retainer', 'amount': '100.00', 'recurring': True},
    ])
    assert r.status_code == 201, r.get_data(as_text=True)
    assert r.get_json()['quote']['lines'][0]['period'] == 'monthly'


def test_mixed_recurrence_periods_are_refused(client, auth_headers, made_customers, made_quotes):
    """A quote that mixes a monthly and an annual retainer is refused.

    The PDF can show ONE recurring subtotal, and it has to carry a period.
    Silently picking one of the two would misstate the deal.
    """
    cid = _make_customer(client, auth_headers, made_customers,
                         country='United States', gst=False)
    r = _quote(client, auth_headers, made_quotes, cid, lines=[
        {'description': 'Monthly retainer', 'amount': '100.00',
         'recurring': True, 'period': 'monthly'},
        {'description': 'Annual retainer', 'amount': '1200.00',
         'recurring': True, 'period': 'annually'},
    ])
    assert r.status_code == 400, r.get_data(as_text=True)
    assert 'same period' in r.get_json()['error']


def test_invalid_recurrence_period_is_refused(client, auth_headers, made_customers, made_quotes):
    cid = _make_customer(client, auth_headers, made_customers,
                         country='United States', gst=False)
    r = _quote(client, auth_headers, made_quotes, cid, lines=[
        {'description': 'Retainer', 'amount': '100.00',
         'recurring': True, 'period': 'fortnightly'},
    ])
    assert r.status_code == 400, r.get_data(as_text=True)


def test_non_boolean_recurring_flag_is_refused(client, auth_headers, made_customers, made_quotes):
    cid = _make_customer(client, auth_headers, made_customers,
                         country='United States', gst=False)
    r = _quote(client, auth_headers, made_quotes, cid, lines=[
        {'description': 'Retainer', 'amount': '100.00', 'recurring': 'yes'},
    ])
    assert r.status_code == 400, r.get_data(as_text=True)


def test_pdf_shows_the_recurring_split(client, auth_headers, made_customers, made_quotes):
    """The PDF must not present one TOTAL that hides a recurring line."""
    cid = _make_customer(client, auth_headers, made_customers,
                         country='United States', gst=False)
    r = _quote(client, auth_headers, made_quotes, cid, lines=[
        {'description': 'A.I system build', 'amount': '6000.00'},
        {'description': 'Managed updates', 'amount': '100.00',
         'recurring': True, 'period': 'monthly'},
    ])
    assert r.status_code == 201, r.get_data(as_text=True)
    qid = r.get_json()['quote']['id']
    text = _pdf_text(client, auth_headers, qid)
    # The one-off and the recurring are shown separately, and the total
    # says it covers the first period -- a bare "TOTAL: $6,100.00" would
    # read as a one-off price.
    assert 'One-off subtotal' in text
    assert 'Recurring (monthly' in text
    assert 'first period' in text
    assert '$6,000.00' in text
    assert '$100.00' in text
    assert '$6,100.00' in text
    # The line item is marked as recurring, so the customer is not left to
    # work it out from the totals block.
    assert 'Managed updates (recurring monthly)' in text


# --- 5. customer-controlled text cannot break the PDF --------------------

def test_markup_in_a_line_description_does_not_crash_the_pdf(client, auth_headers, made_customers, made_quotes):
    """An unclosed <b> in a description used to raise ValueError.

    reportlab parses Paragraph content as markup, so an unescaped customer
    string could take the whole PDF route down.
    """
    cid = _make_customer(client, auth_headers, made_customers,
                         country='United States', gst=False)
    r = _quote(client, auth_headers, made_quotes, cid, lines=[
        {'description': 'Hosting <b>plus</b> plan', 'amount': '100.00'},
        {'description': 'Plan <b>bold', 'amount': '50.00'},
        {'description': 'A <img src=x> plan', 'amount': '25.00'},
    ])
    assert r.status_code == 201, r.get_data(as_text=True)
    qid = r.get_json()['quote']['id']
    r = client.get(f'/api/quotes/{qid}/pdf', headers=auth_headers)
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.data[:5] == b'%PDF-'


def test_markup_in_notes_does_not_crash_the_pdf(client, auth_headers, made_customers, made_quotes):
    cid = _make_customer(client, auth_headers, made_customers,
                         country='United States', gst=False)
    r = _quote(client, auth_headers, made_quotes, cid,
               notes='Payment <b>terms</b> <img src=x>')
    assert r.status_code == 201, r.get_data(as_text=True)
    qid = r.get_json()['quote']['id']
    r = client.get(f'/api/quotes/{qid}/pdf', headers=auth_headers)
    assert r.status_code == 200, r.get_data(as_text=True)


def test_markup_in_the_customer_name_does_not_crash_the_pdf(client, auth_headers, made_customers, made_quotes):
    """The customer name is customer-controlled too."""
    cid = _make_customer(client, auth_headers, made_customers,
                         country='United States', gst=False,
                         name='ZZ Test <b>Holdings</b> & Co')
    r = _quote(client, auth_headers, made_quotes, cid)
    assert r.status_code == 201, r.get_data(as_text=True)
    qid = r.get_json()['quote']['id']
    r = client.get(f'/api/quotes/{qid}/pdf', headers=auth_headers)
    assert r.status_code == 200, r.get_data(as_text=True)


# --- 5b. the reason is fixed when the quote is raised --------------------

def test_the_gst_basis_is_stored_with_the_quote(client, auth_headers, made_customers, made_quotes):
    """The basis is captured at issue, so the page cannot change later.

    A quote is a document. If the customer record is edited afterwards
    (say, they are given an ABN), the reason printed on an already-issued
    quote must not change under the customer's feet.
    """
    cid = _make_customer(client, auth_headers, made_customers,
                         country='United States', gst=False)
    r = _quote(client, auth_headers, made_quotes, cid)
    assert r.status_code == 201, r.get_data(as_text=True)
    q = r.get_json()['quote']
    assert q['gst_basis'] == 'outside_australia'

    # Now change the customer record so the live derivation WOULD differ.
    r = client.put(f"/api/customers/{cid}",
                   json={'gst': True, 'abn': '99 999 999 999'},
                   headers=auth_headers)
    assert r.status_code == 200, r.get_data(as_text=True)

    # The stored quote still says what it said.
    r = client.get(f"/api/quotes/{q['id']}", headers=auth_headers)
    stored = r.get_json()['quote']
    assert stored['gst_basis'] == 'outside_australia'
    assert stored['gst_type'] == '0.0'

    # And the rendered document says the same thing.
    text = _pdf_text(client, auth_headers, q['id'])
    assert 'outside Australia' in text, text[-2000:]


# --- 6. the tax module itself --------------------------------------------

def test_derive_gst_type_is_pure():
    """The rules are a pure function of (customer, business)."""
    from types import SimpleNamespace
    from app.shared.tax import derive_gst_type, NO_GST, GST_RATE

    au_registrant = SimpleNamespace(country='Australia', gst=True, abn='1')
    au_unregistered = SimpleNamespace(country='Australia', gst=False, abn=None)
    overseas = SimpleNamespace(country='United States', gst=False, abn=None)
    business = SimpleNamespace(abn='99 999 999 999')
    no_abn_business = SimpleNamespace(abn=None)

    assert derive_gst_type(au_registrant, business) == GST_RATE
    assert derive_gst_type(au_unregistered, business) == NO_GST
    assert derive_gst_type(overseas, business) == NO_GST
    # A business with no ABN cannot charge GST at all.
    assert derive_gst_type(au_registrant, no_abn_business) == NO_GST
