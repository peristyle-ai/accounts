"""Tests for storing the quote PDF that was transmitted (`feat/quotes`).

The quote PDF is rendered on demand, so two renders are different files.
The rendered PDF is therefore not a record of what the customer holds --
only the STORED bytes are. These tests lock in that:

  1. The upload route stores the bytes against the quote and records the
     path.
  2. Only permitted file types are accepted.
  3. The stored path is what the tenant-scoping check resolves ownership
     from -- so the file is served to the quote's tenant and to nobody
     else. Without the Quote entry in upload_access, the file would be
     REFUSED (ownership cannot be established) and the route would be
     useless.
  4. Another tenant's quote is a 404, not a 403.

The suite runs against the LIVE database, so every test creates its own
throwaway rows and removes them afterwards.
"""

import io
import sys
import weakref
from pathlib import Path

import pytest
from sqlalchemy import event, text

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import create_app
from app.db import db


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
    """Track created quotes AND the files they own, and remove both."""
    created = []
    yield created
    with app.app_context():
        from app.models.quote import Quote
        for qid in created:
            q = Quote.query.filter_by(id=qid).first()
            if q is None:
                continue
            if q.attachment_path:
                p = Path(q.attachment_path)
                p.unlink(missing_ok=True)
                try:
                    p.parent.rmdir()
                except OSError:
                    pass
            db.session.delete(q)
        db.session.commit()


@pytest.fixture
def create_quote(client, auth_headers, customer_id, made_quotes):
    def _create(**overrides):
        payload = {
            'customer_id': customer_id,
            'client_name': 'ZZ Upload Test Co (delete me)',
            'lines': [{'description': 'A.I system development',
                       'amount': '1000.00'}],
            'quote_date': '2026-11-01',
        }
        payload.update(overrides)
        r = client.post('/api/quotes', json=payload, headers=auth_headers)
        if r.status_code == 201:
            made_quotes.append(r.get_json()['quote']['id'])
        return r
    return _create


def _pdf_bytes():
    """A file that IS a PDF, by magic number -- the route checks the
    extension, and the test asserts on the bytes that were stored."""
    return b'%PDF-1.4\n% a quote document\n'


def _upload(client, auth_headers, qid, filename='quote.pdf', data=None):
    return client.post(
        f'/api/quotes/{qid}/upload',
        data={'file': (io.BytesIO(data if data is not None else _pdf_bytes()),
                       filename)},
        headers=auth_headers,
        content_type='multipart/form-data',
    )


def test_upload_stores_the_bytes_and_records_the_path(client, auth_headers,
                                                       create_quote):
    r = create_quote()
    qid = r.get_json()['quote']['id']
    r = _upload(client, auth_headers, qid)
    assert r.status_code == 200, r.data
    q = r.get_json()['quote']
    assert q['attachment_path'], 'the path must be recorded'

    stored = Path(q['attachment_path'])
    assert stored.exists(), 'the file must be on disk'
    assert stored.read_bytes() == _pdf_bytes(), 'the EXACT bytes must be stored'


def test_the_stored_path_resolves_an_owner(client, auth_headers, create_quote):
    """Without this the file cannot be served at all: ownership is
    resolved from the record that owns it, and an unresolvable file is
    REFUSED rather than served."""
    from app.shared.upload_access import resolve_upload_owner
    r = create_quote()
    qid = r.get_json()['quote']['id']
    _upload(client, auth_headers, qid)
    path = client.get(f'/api/quotes/{qid}',
                      headers=auth_headers).get_json()['quote']['attachment_path']
    owner = resolve_upload_owner(path)
    assert owner is not None, 'the quote must own its uploaded PDF'


def test_a_disallowed_file_type_is_refused(client, auth_headers, create_quote):
    r = create_quote()
    qid = r.get_json()['quote']['id']
    r = _upload(client, auth_headers, qid, filename='quote.exe')
    assert r.status_code == 400, r.data
    q = client.get(f'/api/quotes/{qid}',
                   headers=auth_headers).get_json()['quote']
    assert not q['attachment_path'], 'nothing may be stored'


def test_no_file_is_refused(client, auth_headers, create_quote):
    r = create_quote()
    qid = r.get_json()['quote']['id']
    r = client.post(f'/api/quotes/{qid}/upload', headers=auth_headers)
    assert r.status_code == 400, r.data


def test_another_tenants_quote_is_not_found(client, auth_headers, create_quote):
    """A quote belonging to another tenant is a 404, not a 403 -- matching
    the rest of the API (the response must not confirm that it exists)."""
    r = create_quote()
    qid = r.get_json()['quote']['id']
    from tests.test_access_control_sweep import _api_key, _make_user
    with client.application.app_context():
        other_id = _make_user('upload-sweep-other@example.invalid', 'member')
        other_key = _api_key(client.application, other_id)
        try:
            r = client.post(f'/api/quotes/{qid}/upload',
                            data={'file': (io.BytesIO(_pdf_bytes()), 'quote.pdf')},
                            headers={'X-API-Key': other_key},
                            content_type='multipart/form-data')
            assert r.status_code == 404, r.data
        finally:
            with client.application.app_context():
                from app.models.user import User
                User.query.filter_by(email='upload-sweep-other@example.invalid').delete()
                db.session.commit()
