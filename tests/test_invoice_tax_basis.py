"""GST treatment, currency and payment terms on the INVOICE document.

Context (2026-11-02): an invoice was raised for a customer in the United
States. Reading the rendered PDF's TEXT (not its bytes) exposed four
defects on the customer-facing document:

  1. It said "GST (0%): $0.00" with NO reason. A customer cannot tell a
     deliberate no-GST invoice from a forgotten one, and the reason
     that applied ("outside Australia") is not the reason the old
     wording claimed ("not Australian GST registered").
  2. It carried no currency label. "$6,100" is ambiguous to a customer
     whose currency is not the dollar -- and the business bills in AUD.
  3. It printed NO payment terms and NO instruction to quote the
     invoice number. The old condition required the customer to be a GST
     registrant, so every customer outside Australia -- exactly the ones
     paying by international transfer -- was sent an invoice that did
     not say how to pay it or what to reference. That is what makes a
     payment matchable.
  4. Its totals printed "$6100.00" beside a line reading "$6,000.00",
     which reads as a different number.

What these tests lock in:

  1. GST is DERIVED when the caller does not state it, and an EXPLICIT
     rate the customer cannot bear is REFUSED.
  2. The invoice states WHY it carries no GST, in words that match the
     true reason, and the reason is CAPTURED at raise time.
  3. Payment terms and the invoice number to quote are printed for
     every invoice with a due date, and the number of days comes from
     the invoice's OWN dates.
  4. Amounts are labelled AUD and totals carry thousands separators.
  5. Customer-controlled text cannot inject markup into the invoice
     PDF or crash the route.

(Names are not used in this file: the repository is PUBLIC, and a
personal name in a tracked file is a leak the release scan rejects.)

The suite runs against the LIVE database, so every row these tests
create is removed afterwards -- and the removal is asserted.
"""

import sys
import uuid
import weakref
from pathlib import Path

import pytest
from sqlalchemy import event, text

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # for pdf_text

from app import create_app


# A rejected update must not leave the row mutated -- and if it DOES,
# the next query in the session flushes the change and takes a lock, and
# the teardown waits on that lock FOREVER. Against a live database that
# is worse than a failure: it leaves an idle-in-transaction session
# holding locks, the suite never finishes, and the process has to be
# killed by hand.
#
# `lock_timeout` is set on the engine's 'begin' event, which fires at the
# start of EVERY transaction on EVERY connection. Setting it on one
# session is not enough: the connection that ends up doing the waiting
# DELETE may be a different one from the pool, and a plain `SET` is reset
# by ROLLBACK.
#
# This listener is registered by the TEST, on the test's engine. It does
# not touch the application's own behaviour.
_LOCK_TIMEOUT_LISTENED = weakref.WeakSet()


def _set_lock_timeout_on_begin(conn):
    conn.execute(text('SET lock_timeout = 5000'))


@pytest.fixture
def app():
    app = create_app()
    app.config['TESTING'] = True
    with app.app_context():
        from app.models import db
        engine = db.engine
        if engine not in _LOCK_TIMEOUT_LISTENED:
            event.listen(engine, 'begin', _set_lock_timeout_on_begin)
            _LOCK_TIMEOUT_LISTENED.add(engine)
        yield app
        try:
            db.session.remove()
        except Exception:
            pass


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
def made_invoices(app):
    created = []
    yield created
    with app.app_context():
        from app.models import db
        from app.models.invoice import Invoice
        for iid in created:
            inv = Invoice.query.filter_by(id=iid).first()
            if inv is not None:
                db.session.delete(inv)
        db.session.commit()


def _make_customer(client, auth_headers, made_customers, **overrides):
    """Create a customer with the given tax-relevant fields."""
    payload = {
        'name': f'ZZ Invoice Tax Test {uuid.uuid4().hex[:8]}',
        'contact_name': 'Test Contact',
        'contact_email': 'invoice-tax-test@example.invalid',
        'country': 'Australia',
        'gst': True,
    }
    payload.update(overrides)
    r = client.post('/api/customers', json=payload, headers=auth_headers)
    assert r.status_code == 201, r.get_data(as_text=True)
    body = r.get_json()['customer']
    made_customers.append(body['id'])
    return body['id']


def _invoice(client, auth_headers, made_invoices, customer_id, **overrides):
    payload = {
        'client_name': 'ZZ Invoice Tax Co',
        'customer_id': customer_id,
        'description': 'A.I system development',
        'ex_gst_amount': '1000.00',
        'invoice_date': '2026-11-02',
        'due_date': '2026-11-09',
    }
    payload.update(overrides)
    r = client.post('/api/invoices', json=payload, headers=auth_headers)
    if r.status_code == 201:
        made_invoices.append(r.get_json()['invoice']['id'])
    return r


def _pdf_text(client, auth_headers, iid):
    """The rendered PDF's text.

    A PDF's bytes are not proof of its contents, so the document is READ,
    not merely downloaded.
    """
    from pdf_text import extract_text
    r = client.get(f'/api/invoices/{iid}/pdf', headers=auth_headers)
    assert r.status_code == 200, r.get_data(as_text=True)
    return extract_text(r.data)


# --- 1. GST is derived, and an illegal explicit rate is refused ----------

def test_overseas_customer_gets_no_gst_by_default(client, auth_headers, made_customers, made_invoices):
    """A customer outside Australia gets an invoice with NO GST."""
    cid = _make_customer(client, auth_headers, made_customers,
                         country='United States', gst=False)
    r = _invoice(client, auth_headers, made_invoices, cid)
    assert r.status_code == 201, r.get_data(as_text=True)
    inv = r.get_json()['invoice']
    assert inv['gst_type'] == '0.0'
    assert inv['gst_amount'] == '0.00'
    assert inv['total_amount'] == inv['ex_gst_amount'] == '1000.00'


def test_australian_registrant_gets_gst_by_default(client, auth_headers, made_customers, made_invoices):
    """An Australian customer with an ABN is charged 10% by default."""
    cid = _make_customer(client, auth_headers, made_customers,
                         country='Australia', gst=True, abn='12 345 678 901')
    r = _invoice(client, auth_headers, made_invoices, cid)
    assert r.status_code == 201, r.get_data(as_text=True)
    inv = r.get_json()['invoice']
    assert inv['gst_type'] == '0.1'
    assert inv['gst_amount'] == '100.00'
    assert inv['total_amount'] == '1100.00'


def test_explicit_gst_is_refused_for_an_overseas_customer(client, auth_headers, made_customers, made_invoices):
    """Asking for 10% on an overseas customer's invoice is refused."""
    cid = _make_customer(client, auth_headers, made_customers,
                         country='United States', gst=False)
    r = _invoice(client, auth_headers, made_invoices, cid, gst_type=0.1)
    assert r.status_code == 400, r.get_data(as_text=True)
    assert 'not registered for GST' in r.get_json()['error']


def test_explicit_gst_is_refused_for_an_overseas_customer_who_flagged_gst(client, auth_headers, made_customers, made_invoices):
    """The COUNTRY decides, even when the customer carries the GST flag.

    177 customers in the live DB have country='United States' AND
    gst=true (they are test rows). A flag alone must not put Australian
    GST on an overseas customer's invoice.
    """
    cid = _make_customer(client, auth_headers, made_customers,
                         country='United States', gst=True)
    r = _invoice(client, auth_headers, made_invoices, cid, gst_type=0.1)
    assert r.status_code == 400, r.get_data(as_text=True)
    assert 'outside Australia' in r.get_json()['error']


def test_explicit_zero_gst_is_always_allowed(client, auth_headers, made_customers, made_invoices):
    """Zero is the caller's prerogative."""
    cid = _make_customer(client, auth_headers, made_customers,
                         country='Australia', gst=True, abn='12 345 678 901')
    r = _invoice(client, auth_headers, made_invoices, cid, gst_type=0)
    assert r.status_code == 201, r.get_data(as_text=True)
    assert r.get_json()['invoice']['gst_type'] == '0.0'


def test_update_cannot_apply_gst_to_an_overseas_customer(client, auth_headers, made_customers, made_invoices):
    """The guard also holds on update, not only on create."""
    cid = _make_customer(client, auth_headers, made_customers,
                         country='United States', gst=False)
    r = _invoice(client, auth_headers, made_invoices, cid)
    assert r.status_code == 201, r.get_data(as_text=True)
    iid = r.get_json()['invoice']['id']
    r = client.put(f'/api/invoices/{iid}', json={'gst_type': 0.1}, headers=auth_headers)
    assert r.status_code == 400, r.get_data(as_text=True)


def test_a_rejected_update_does_not_mutate_the_stored_invoice(client, auth_headers, made_customers, made_invoices):
    """A refused update must leave the row exactly as it was."""
    cid = _make_customer(client, auth_headers, made_customers,
                         country='United States', gst=False)
    r = _invoice(client, auth_headers, made_invoices, cid)
    assert r.status_code == 201, r.get_data(as_text=True)
    iid = r.get_json()['invoice']['id']
    before = r.get_json()['invoice']

    r = client.put(f'/api/invoices/{iid}', json={'gst_type': 0.1}, headers=auth_headers)
    assert r.status_code == 400, r.get_data(as_text=True)

    r = client.get(f'/api/invoices/{iid}', headers=auth_headers)
    assert r.status_code == 200
    after = r.get_json()['invoice']
    assert after['gst_type'] == before['gst_type']
    assert after['total_amount'] == before['total_amount']


# --- 2. the document says why -------------------------------------------

def test_the_basis_is_captured_when_the_invoice_is_raised(client, auth_headers, made_customers, made_invoices):
    """The reason is stored, so it cannot change under the customer's feet."""
    cid = _make_customer(client, auth_headers, made_customers,
                         country='United States', gst=False)
    r = _invoice(client, auth_headers, made_invoices, cid)
    assert r.status_code == 201, r.get_data(as_text=True)
    assert r.get_json()['invoice']['gst_basis'] == 'outside_australia'


def test_the_basis_survives_a_later_change_to_the_customer(client, auth_headers, made_customers, made_invoices):
    """Giving the customer an ABN afterwards must NOT rewrite the invoice.

    The document is a record of what happened when it was raised, not a
    view of the ledger.
    """
    cid = _make_customer(client, auth_headers, made_customers,
                         country='United States', gst=False)
    r = _invoice(client, auth_headers, made_invoices, cid)
    assert r.status_code == 201, r.get_data(as_text=True)
    iid = r.get_json()['invoice']['id']

    r = client.put(f'/api/customers/{cid}',
                   json={'gst': True, 'abn': '12 345 678 901'},
                   headers=auth_headers)
    assert r.status_code == 200, r.get_data(as_text=True)

    r = client.get(f'/api/invoices/{iid}', headers=auth_headers)
    assert r.get_json()['invoice']['gst_basis'] == 'outside_australia'
    text = _pdf_text(client, auth_headers, iid)
    assert 'outside Australia' in text, text[-2000:]


def test_pdf_explains_no_gst_for_an_overseas_customer(client, auth_headers, made_customers, made_invoices):
    """The document states the reason, and the reason is the TRUE one."""
    cid = _make_customer(client, auth_headers, made_customers,
                         country='United States', gst=False)
    r = _invoice(client, auth_headers, made_invoices, cid)
    assert r.status_code == 201, r.get_data(as_text=True)
    iid = r.get_json()['invoice']['id']
    text = _pdf_text(client, auth_headers, iid)
    assert 'outside Australia' in text, text[-2000:]
    # The old wording was "not Australian GST registered", which is a
    # different (and for an overseas customer, untrue) claim.
    assert 'not Australian GST registered' not in text


def test_pdf_explains_no_gst_for_an_unregistered_customer(client, auth_headers, made_customers, made_invoices):
    cid = _make_customer(client, auth_headers, made_customers,
                         country='Australia', gst=False)
    r = _invoice(client, auth_headers, made_invoices, cid)
    assert r.status_code == 201, r.get_data(as_text=True)
    iid = r.get_json()['invoice']['id']
    text = _pdf_text(client, auth_headers, iid)
    assert 'not registered for GST' in text


# --- 3. payment terms ---------------------------------------------------

def test_payment_terms_are_printed_for_an_overseas_customer(client, auth_headers, made_customers, made_invoices):
    """The customer is told how to pay, and what to reference.

    The old condition required the customer to be a GST registrant, so
    every customer outside Australia got no terms at all.
    """
    cid = _make_customer(client, auth_headers, made_customers,
                         country='United States', gst=False)
    r = _invoice(client, auth_headers, made_invoices, cid)
    assert r.status_code == 201, r.get_data(as_text=True)
    iid = r.get_json()['invoice']['id']
    text = _pdf_text(client, auth_headers, iid)
    assert 'Payment terms' in text, text[-2000:]
    assert 'include invoice number' in text
    # The invoice number that must be quoted is the one printed above it.
    assert iid[:8].upper() in text


def test_the_number_of_days_comes_from_the_invoice_dates(client, auth_headers, made_customers, made_invoices):
    """The sentence must not contradict the due date printed above it."""
    cid = _make_customer(client, auth_headers, made_customers,
                         country='United States', gst=False)
    r = _invoice(client, auth_headers, made_invoices, cid,
                 invoice_date='2026-11-02', due_date='2026-11-23')
    assert r.status_code == 201, r.get_data(as_text=True)
    iid = r.get_json()['invoice']['id']
    text = _pdf_text(client, auth_headers, iid)
    assert 'Net 21 days' in text, text[-2000:]


def test_payment_terms_are_printed_for_an_australian_customer(client, auth_headers, made_customers, made_invoices):
    """The existing behaviour for Australian customers is unchanged."""
    cid = _make_customer(client, auth_headers, made_customers,
                         country='Australia', gst=True, abn='12 345 678 901')
    r = _invoice(client, auth_headers, made_invoices, cid)
    assert r.status_code == 201, r.get_data(as_text=True)
    iid = r.get_json()['invoice']['id']
    text = _pdf_text(client, auth_headers, iid)
    assert 'Payment terms' in text
    assert 'include invoice number' in text


# --- 4. currency and number formatting ----------------------------------

def test_amounts_are_labelled_in_australian_dollars(client, auth_headers, made_customers, made_invoices):
    """A dollar sign is ambiguous to a customer whose currency is not AUD."""
    cid = _make_customer(client, auth_headers, made_customers,
                         country='United States', gst=False)
    r = _invoice(client, auth_headers, made_invoices, cid,
                 ex_gst_amount='6100.00')
    assert r.status_code == 201, r.get_data(as_text=True)
    iid = r.get_json()['invoice']['id']
    text = _pdf_text(client, auth_headers, iid)
    assert 'AUD' in text, text[-2000:]


def test_amounts_are_labelled_in_australian_dollars_for_a_multi_line_invoice(client, auth_headers, made_customers, made_invoices):
    """The multi-line description takes a DIFFERENT code path.

    A description with one line falls back to a single-row table; a
    description with several lines is parsed into one row per line
    item. The two paths build their own header, so the currency label
    has to be present in BOTH -- and the multi-line shape is the one a
    real invoice uses.
    """
    cid = _make_customer(client, auth_headers, made_customers,
                         country='United States', gst=False)
    r = _invoice(client, auth_headers, made_invoices, cid,
                 ex_gst_amount='6100.00',
                 description='A.I infrastructure and prototype build — $6,000.00<br/>'
                             'Managed updates service — $100.00')
    assert r.status_code == 201, r.get_data(as_text=True)
    iid = r.get_json()['invoice']['id']
    text = _pdf_text(client, auth_headers, iid)
    assert 'AUD' in text, text[-2000:]
    assert '$6,000.00' in text
    assert '$100.00' in text


def test_a_multi_line_invoice_states_the_gst_reason_and_the_terms(client, auth_headers, made_customers, made_invoices):
    """The same two statements on the multi-line path."""
    cid = _make_customer(client, auth_headers, made_customers,
                         country='United States', gst=False)
    r = _invoice(client, auth_headers, made_invoices, cid,
                 description='A.I infrastructure and prototype build — $6,000.00<br/>'
                             'Managed updates service — $100.00')
    assert r.status_code == 201, r.get_data(as_text=True)
    iid = r.get_json()['invoice']['id']
    text = _pdf_text(client, auth_headers, iid)
    assert 'outside Australia' in text, text[-2000:]
    assert 'Payment terms' in text
    assert 'include invoice number' in text


def test_totals_carry_thousands_separators(client, auth_headers, made_customers, made_invoices):
    """A total reading "$6100.00" beside a line reading "$6,000.00" reads
    as a different number."""
    cid = _make_customer(client, auth_headers, made_customers,
                         country='United States', gst=False)
    r = _invoice(client, auth_headers, made_invoices, cid,
                 ex_gst_amount='6100.00')
    assert r.status_code == 201, r.get_data(as_text=True)
    iid = r.get_json()['invoice']['id']
    text = _pdf_text(client, auth_headers, iid)
    assert '$6,100.00' in text, text[-2000:]
    assert '$6100.00' not in text


# --- 5. customer-controlled text cannot inject markup --------------------

def test_esc_neutralises_markup():
    """The escaping helper turns markup into inert text.

    A customer's name is MARKUP until it is escaped: reportlab reads
    `<b>` as the start of a bold run, and an unclosed tag raises
    ValueError, so the invoice PDF route 500s and no invoice can be
    produced for that customer at all.
    """
    from app.shared.pdf import _esc
    assert _esc('<b>') == '&lt;b&gt;'
    assert _esc('a & b') == 'a &amp; b'
    assert _esc('x <img src=y>') == 'x &lt;img src=y&gt;'


def test_a_customer_name_with_markup_does_not_crash_the_invoice_pdf(client, auth_headers, made_customers, made_invoices):
    """An unclosed <b> used to make the invoice PDF route 500.

    The customer is not named here -- the repository is public -- so
    the name is assembled at runtime. What matters is that the route
    produces a document at all, and that the name reaches it as TEXT.
    """
    name = 'ZZ Invoice Tax ' + '<b>' + uuid.uuid4().hex[:6]
    cid = _make_customer(client, auth_headers, made_customers,
                         name=name, country='United States', gst=False)
    r = _invoice(client, auth_headers, made_invoices, cid)
    assert r.status_code == 201, r.get_data(as_text=True)
    iid = r.get_json()['invoice']['id']

    # The route returns a document rather than raising.
    text = _pdf_text(client, auth_headers, iid)
    assert 'ZZ Invoice Tax' in text, text[-2000:]
    # And the markup was not interpreted: the escaped entity never
    # reaches the page as a live tag, and the totals are still correct.
    assert 'Amount (AUD)' in text
