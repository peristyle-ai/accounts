"""Tests for the quote TRANSMISSION record (`feat/quotes` branch, 2026-11-01).

A quote is a document, and "it was sent" is a claim about a document that
left the building. These tests lock in what the record must be able to say
about that transmission:

  1. The transmission is recorded as three separate facts -- HOW it left
     (`sent_via`), WHERE it went (`sent_to`), and WHICH BYTES went out
     (`sent_document_sha256`). `email_system` and `email_manual` are
     different evidential facts and must not collapse into "sent".
  2. `sent_at` is the time of the TRANSMISSION and can be back-dated. A
     person who sent a quote an hour ago and records it now must not date
     the transmission by when they remembered.
  3. Nothing is inferred. A caller that does not supply a field leaves it
     unset rather than having it guessed.
  4. Invalid values are refused, and a refused request does not mutate the
     row (validate first, then assign).

The suite runs against the LIVE database, so every test creates its own
throwaway rows and removes them afterwards.
"""

import sys
import weakref
from pathlib import Path

import pytest
from sqlalchemy import event, text

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import create_app
from app.db import db


# A LOCK TIMEOUT, so a row-lock defect FAILS this suite instead of hanging
# it.
#
# The defect this module exists to catch -- assigning to the ORM object
# before the validation that can reject the request -- does not fail
# cleanly. The rejected request has already mutated the row; the next
# query flushes it and takes a lock; the teardown then waits on that lock
# FOREVER. Against a live database that is worse than a failure: it leaves
# an idle-in-transaction session holding locks, the suite never finishes,
# and the process has to be killed by hand.
#
# `lock_timeout` is set on the engine's 'begin' event, which fires at the
# start of EVERY transaction on EVERY connection. Setting it on one
# session is not enough: the connection that ends up doing the waiting
# DELETE may be a different one from the pool, and a plain `SET` is
# reset by ROLLBACK (the same reason app/db.py re-applies `search_path`
# on 'begin' rather than on 'connect').
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
            'client_name': 'ZZ Transmission Test Co (delete me)',
            'lines': [
                {'description': 'A.I system development', 'amount': '1000.00'},
            ],
            'quote_date': '2026-11-01',
        }
        payload.update(overrides)
        r = client.post('/api/quotes', json=payload, headers=auth_headers)
        if r.status_code == 201:
            made_quotes.append(r.get_json()['quote']['id'])
        return r
    return _create


def _mark_sent(client, auth_headers, qid, **body):
    return client.post(f'/api/quotes/{qid}/mark-sent', json=body,
                       headers=auth_headers)


# --- 1. the transmission is three separate facts --------------------------

def test_mark_sent_records_the_whole_transmission(client, auth_headers,
                                                   create_quote):
    r = create_quote()
    qid = r.get_json()['quote']['id']
    sha = 'a' * 64
    r = _mark_sent(client, auth_headers, qid,
                   sent_via='email_manual',
                   sent_to='someone@example.com',
                   sent_document_sha256=sha)
    assert r.status_code == 200, r.data
    q = r.get_json()['quote']
    assert q['status'] == 'sent'
    assert q['sent_via'] == 'email_manual'
    assert q['sent_to'] == 'someone@example.com'
    assert q['sent_document_sha256'] == sha


def test_system_and_manual_sends_are_different_facts(client, auth_headers,
                                                      create_quote):
    """The whole point of sent_via: these are not the same evidence."""
    r = create_quote()
    qid = r.get_json()['quote']['id']
    r = _mark_sent(client, auth_headers, qid, sent_via='email_system')
    assert r.status_code == 200, r.data
    assert r.get_json()['quote']['sent_via'] == 'email_system'

    r = create_quote()
    qid2 = r.get_json()['quote']['id']
    r = _mark_sent(client, auth_headers, qid2, sent_via='email_manual')
    assert r.status_code == 200, r.data
    assert r.get_json()['quote']['sent_via'] == 'email_manual'


def test_unknown_sent_via_is_refused(client, auth_headers, create_quote):
    r = create_quote()
    qid = r.get_json()['quote']['id']
    r = _mark_sent(client, auth_headers, qid, sent_via='carrier_pigeon')
    assert r.status_code == 400, r.data
    # And the refusal did not mark it sent.
    r = client.get(f'/api/quotes/{qid}', headers=auth_headers)
    assert r.get_json()['quote']['status'] == 'draft'


def test_malformed_sha256_is_refused(client, auth_headers, create_quote):
    r = create_quote()
    qid = r.get_json()['quote']['id']
    for bad in ('abc', 'z' * 64, 'a' * 63, 'a' * 65):
        r = _mark_sent(client, auth_headers, qid, sent_document_sha256=bad)
        assert r.status_code == 400, (bad, r.data)
    r = client.get(f'/api/quotes/{qid}', headers=auth_headers)
    assert r.get_json()['quote']['status'] == 'draft'


# --- 2. sent_at is the TRANSMISSION time and can be back-dated -----------

def test_sent_at_can_be_back_dated(client, auth_headers, create_quote):
    """Recording a transmission later must not date it later."""
    r = create_quote()
    qid = r.get_json()['quote']['id']
    r = _mark_sent(client, auth_headers, qid,
                   sent_at='2026-11-01T09:30:00+10:00')
    assert r.status_code == 200, r.data
    sent_at = r.get_json()['quote']['sent_at']
    assert sent_at.startswith('2026-11-01T09:30:00')


def test_sent_at_defaults_to_now_when_not_given(client, auth_headers,
                                                 create_quote):
    r = create_quote()
    qid = r.get_json()['quote']['id']
    r = _mark_sent(client, auth_headers, qid)
    assert r.status_code == 200, r.data
    assert r.get_json()['quote']['sent_at'] is not None


def test_invalid_sent_at_is_refused(client, auth_headers, create_quote):
    r = create_quote()
    qid = r.get_json()['quote']['id']
    r = _mark_sent(client, auth_headers, qid, sent_at='not-a-timestamp')
    assert r.status_code == 400, r.data
    r = client.get(f'/api/quotes/{qid}', headers=auth_headers)
    assert r.get_json()['quote']['status'] == 'draft'


# --- 3. nothing is inferred ----------------------------------------------

def test_nothing_is_inferred_when_the_body_is_empty(client, auth_headers,
                                                     create_quote):
    """A caller that says only "sent" gets "sent" -- no invented detail."""
    r = create_quote()
    qid = r.get_json()['quote']['id']
    r = _mark_sent(client, auth_headers, qid)
    assert r.status_code == 200, r.data
    q = r.get_json()['quote']
    assert q['sent_via'] is None
    assert q['sent_to'] is None
    assert q['sent_document_sha256'] is None


def test_a_quote_with_no_transmission_record_says_so(client, auth_headers,
                                                      create_quote):
    """Before it is sent, a quote carries no transmission facts at all."""
    r = create_quote()
    qid = r.get_json()['quote']['id']
    q = client.get(f'/api/quotes/{qid}',
                   headers=auth_headers).get_json()['quote']
    assert q['sent_via'] is None
    assert q['sent_to'] is None
    assert q['sent_document_sha256'] is None
    assert q['sent_at'] is None


# --- 4. the record survives the write ------------------------------------

def test_the_transmission_record_is_readable_afterwards(client, auth_headers,
                                                         create_quote):
    """Not just in the POST response: the stored row must carry it."""
    r = create_quote()
    qid = r.get_json()['quote']['id']
    _mark_sent(client, auth_headers, qid,
               sent_via='email_manual',
               sent_to='someone@example.com',
               sent_document_sha256='b' * 64)
    q = client.get(f'/api/quotes/{qid}',
                   headers=auth_headers).get_json()['quote']
    assert q['sent_via'] == 'email_manual'
    assert q['sent_to'] == 'someone@example.com'
    assert q['sent_document_sha256'] == 'b' * 64


def test_a_rejected_update_leaves_the_row_untouched(client, auth_headers,
                                                     create_quote):
    """Validate first, then assign: a 400 must not have mutated anything."""
    r = create_quote()
    qid = r.get_json()['quote']['id']
    _mark_sent(client, auth_headers, qid, sent_via='email_manual',
               sent_to='keep@example.com', sent_document_sha256='c' * 64)
    before = client.get(f'/api/quotes/{qid}',
                         headers=auth_headers).get_json()['quote']

    r = _mark_sent(client, auth_headers, qid, sent_via='nope')
    assert r.status_code == 400, r.data

    after = client.get(f'/api/quotes/{qid}',
                       headers=auth_headers).get_json()['quote']
    assert after['sent_via'] == before['sent_via']
    assert after['sent_to'] == before['sent_to']
    assert after['sent_document_sha256'] == before['sent_document_sha256']
