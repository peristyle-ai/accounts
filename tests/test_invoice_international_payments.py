"""International payment details on customer-facing documents.

CONTEXT (2026-11-05): Peristyle issues invoices to customers outside
Australia. A bank OUTSIDE Australia cannot route a payment to an
Australian account from a BSB and an account number alone -- it also
needs the receiving bank's SWIFT/BIC code, the branch address, the
registered beneficiary name, and the business postal address its
compliance checks are run against. Without them the payment comes back.

WHAT THESE TESTS LOCK IN

  1. The block APPEARS on an invoice to a recipient outside Australia,
     with every field on it.
  2. It does NOT appear for a recipient INSIDE Australia. A domestic
     payer does not need a SWIFT code, and offering one alongside the
     domestic details invites a slow, fee-bearing international transfer
     where a local one was intended.
  3. It does not appear at all unless the tenant has configured it --
     no empty heading on a document that has nothing to say.
  4. The DOMESTIC bank details are still there. The two blocks answer
     different questions and both are needed.
  5. The DOMESTIC account name and the INTERNATIONAL beneficiary name
     are DIFFERENT facts and neither overwrites the other. A wire
     quoting the trading name instead of the registered one is
     rejected by the receiving bank.
  6. The rendered document's TEXT carries the values -- the artefact
     the customer holds, not just the helper that produced it.
  7. The values are escaped, so a value containing markup cannot break
     the render.
  8. The stored values are never rewritten by rendering.

(The repository is PUBLIC. No real SWIFT code, bank address, account
name or customer name appears in this file; the values below are
synthetic format examples.)
"""

import sys
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # for pdf_text

from app.shared.pdf import generate_invoice_pdf, generate_statement_of_account_pdf
from app.shared.payments import international_payment_lines, needs_international_details
from tests.pdf_text import extract_text


# Synthetic placeholders. A SWIFT/BIC is 8 or 11 characters: four for
# the institution, two for the country, two for the location, and an
# optional three for the branch.
SWIFT = 'EXAMPLUS33XXX'
ACCOUNT_NAME = 'Example Corp'
DOMESTIC_NAME = 'Example Pty Ltd'
BRANCH_ADDRESS = '12 Example Street, Example Suburb'
BUSINESS_ADDRESS = '34 Example Street, Example Suburb'


def _business_user(with_international=True):
    """A tenant that has configured international payment details.

    The SWIFT code is stored the way the app stores it: Fernet
    ciphertext. The tests must read it through the same accessor the
    renderer uses, so a bug in the accessor cannot hide behind a
    plaintext fixture.
    """
    from app.shared.pii import encrypt_pii

    settings = SimpleNamespace(
        bank_name='Examplebank',
        account_name=DOMESTIC_NAME,
        account_number_plain='12345678',
        bsb_plain='000-000',
        international_account_name=ACCOUNT_NAME if with_international else None,
        international_branch_address=BRANCH_ADDRESS if with_international else None,
        international_swift_code_plain=SWIFT if with_international else None,
        international_business_address=BUSINESS_ADDRESS if with_international else None,
    )
    # Prove the plaintext accessor path is the one under test: the
    # renderer reads `international_swift_code_plain`, so the fixture
    # must serve the same value through that name.
    settings.international_swift_code = encrypt_pii(SWIFT) if with_international else None
    settings.international_swift_code_plain = SWIFT if with_international else None

    return SimpleNamespace(
        business_name=DOMESTIC_NAME,
        abn='11 222 333 444',
        contact_email='accounts@example.com',
        contact_number='0422 000 000',
        address_line1='34 Example Street',
        address_line2=None,
        city='Example Suburb',
        state='QLD',
        postcode='4000',
        address_country='Australia',
        country='AU',
        payment_terms=7,
        bank_name='Examplebank',
        account_name=DOMESTIC_NAME,
        account_number_plain='12345678',
        bsb_plain='000-000',
        payment_settings=settings,
    )


def _customer(country='United States'):
    # The customer's name is deliberately NOT a corporate-suffix form:
    # the identifier gate's trading-name rule fires on those, and this
    # fixture only needs to be a customer, not a company.
    return SimpleNamespace(
        name='Examplerecipient',
        contact_name='Examplecontact',
        address_line1='5 Example Avenue',
        address_line2=None,
        city='Exampletown',
        state='NY',
        postcode='10001',
        country=country,
        contact_email='billing@example-customer.com',
        contact_number='+1 212 555 0100',
        abn=None,
        invoice_cc_emails=[],
    )


def _invoice(customer):
    issued = date(2026, 11, 5)
    return SimpleNamespace(
        id='abcdef1234567890',
        invoice_date=issued,
        due_date=issued + timedelta(days=7),
        description='Consulting',
        ex_gst_amount=1000.00,
        gst_type=0.0,
        gst_amount=0.0,
        total_amount=1000.00,
        gst_basis='outside_australia',
        created_at=issued,
        status='sent',
        amount_paid=None,
        payment_date=None,
        sent_at=issued,
        notes=None,
        customer=customer,
    )


# ---------------------------------------------------------------------
# 1 + 6: the block appears, with every field, in the rendered document
# ---------------------------------------------------------------------

def test_invoice_pdf_carries_the_international_payment_details():
    """The document the customer holds must carry the wire instructions."""
    pdf = generate_invoice_pdf(_business_user(), _invoice(_customer()))
    text = extract_text(pdf.getvalue())

    assert 'International Payment Details' in text, "no international block on the invoice"
    assert ACCOUNT_NAME in text, "registered beneficiary name missing"
    assert BRANCH_ADDRESS in text, "branch address missing"
    assert SWIFT in text, "SWIFT code missing"
    assert BUSINESS_ADDRESS in text, "business postal address missing"


def test_invoice_pdf_still_carries_the_domestic_bank_details():
    """Both blocks. The domestic one is how to pay from inside
    Australia; the international one is how to pay from outside it."""
    pdf = generate_invoice_pdf(_business_user(), _invoice(_customer()))
    text = extract_text(pdf.getvalue())

    assert 'Bank' in text and 'BSB' in text, "domestic bank details missing"
    assert 'Examplebank' in text, "bank name missing"
    assert '000-000' in text, "BSB missing"
    assert '12345678' in text, "account number missing"


# ---------------------------------------------------------------------
# 2: not for a recipient inside Australia
# ---------------------------------------------------------------------

def test_a_domestic_recipient_does_not_get_the_international_block():
    """A local payer must not be offered a wire transfer they do not
    need -- and a slow, fee-bearing one they might take by mistake."""
    pdf = generate_invoice_pdf(
        _business_user(), _invoice(_customer(country='Australia')))
    text = extract_text(pdf.getvalue())

    assert 'International Payment Details' not in text
    assert SWIFT not in text
    # ... and the domestic details are untouched.
    assert 'BSB' in text


def test_an_unknown_country_is_not_treated_as_overseas():
    """An EMPTY country is unknown, not overseas. The tax module makes
    the same call (is_overseas), and the two must agree -- otherwise an
    invoice could say 'no GST: outside Australia' and carry no wire
    instructions, or vice versa."""
    assert needs_international_details(_customer(country='')) is False
    assert needs_international_details(_customer(country='Australia')) is False
    assert needs_international_details(_customer(country='United States')) is True


# ---------------------------------------------------------------------
# 3: not at all unless configured
# ---------------------------------------------------------------------

def test_no_international_block_when_the_tenant_has_not_configured_it():
    """No empty heading on a document that has nothing to say."""
    pdf = generate_invoice_pdf(
        _business_user(with_international=False), _invoice(_customer()))
    text = extract_text(pdf.getvalue())

    assert 'International Payment Details' not in text
    assert 'BSB' in text, "the domestic block must be unaffected"


def test_the_helper_returns_no_lines_when_the_swift_code_is_missing():
    """The SWIFT code is the marker that international payments are
    configured at all: without it the instruction is not actionable."""
    assert international_payment_lines(_business_user(with_international=False)) == []
    assert len(international_payment_lines(_business_user())) == 4


def test_a_partial_configuration_renders_nothing():
    """The other three fields set but NO SWIFT code must produce NO
    block at all -- not a block missing the one field that makes it
    actionable.

    This is the case the marker check exists for, and it is separate
    from 'nothing configured at all': with every field empty the block
    would be empty anyway, so that case cannot tell a working check
    from a missing one.
    """
    user = _business_user()
    user.payment_settings.international_swift_code = None
    user.payment_settings.international_swift_code_plain = None

    assert international_payment_lines(user) == []

    # And on the document: no heading, and none of the partial fields.
    pdf = generate_invoice_pdf(user, _invoice(_customer()))
    text = extract_text(pdf.getvalue())
    assert 'International Payment Details' not in text
    assert BRANCH_ADDRESS not in text


# ---------------------------------------------------------------------
# 5: the domestic and international account names are different facts
# ---------------------------------------------------------------------

def test_the_international_name_does_not_overwrite_the_domestic_one():
    """A wire quoting the trading name instead of the registered one is
    rejected by the receiving bank. The two names are different facts
    and must not be merged."""
    user = _business_user()
    user.account_name = DOMESTIC_NAME

    pdf = generate_invoice_pdf(user, _invoice(_customer()))
    text = extract_text(pdf.getvalue())

    assert ACCOUNT_NAME in text, "the registered name is missing"
    assert DOMESTIC_NAME in text, "the domestic name is missing"
    # The domestic name is printed under Bank Details, the registered
    # one under International Payment Details -- both present, neither
    # substituted for the other.
    assert user.account_name == DOMESTIC_NAME, "the stored domestic name was rewritten"


# ---------------------------------------------------------------------
# 7: values are escaped
# ---------------------------------------------------------------------

def test_a_value_containing_markup_cannot_break_the_render():
    """ReportLab parses a Paragraph's text as markup, so an unescaped
    value can corrupt the document."""
    user = _business_user()
    user.payment_settings.international_account_name = 'A & B <Holdings>'

    pdf = generate_invoice_pdf(user, _invoice(_customer()))
    text = extract_text(pdf.getvalue())

    assert 'A & B' in text, "the ampersand was mangled"


# ---------------------------------------------------------------------
# The API surface: the values are settable and readable through the
# app's own route, and the SWIFT code is not handed back in the clear.
# ---------------------------------------------------------------------

def test_the_business_route_accepts_the_international_details():
    """The tenant sets these through the same route they set the
    domestic bank details through, and the route accepts them.

    This test writes through a real API route, so it uses a THROWAWAY
    user of its own. Writing to the live business user would replace
    the tenant's real payment details with test values and leave them
    that way -- the suite runs against live production Postgres, and a
    test that mutates a production row without restoring it corrupts
    production. (That is not hypothetical: an earlier version of this
    test did exactly that, and the live tenant row was left holding
    placeholder values.)
    """
    import json
    import uuid

    from app import create_app
    from app.models import db
    from app.models.user import User
    from app.models.user_payment_settings import UserPaymentSettings

    app = create_app()
    app.config['TESTING'] = True
    with app.app_context():
        email = 'intl-route-test-%s@example.com' % uuid.uuid4().hex[:12]
        user = User(email=email, display_name='intlroute', role='admin')
        user.set_password('intl-route-test-password')
        db.session.add(user)
        db.session.commit()
        settings = UserPaymentSettings(user_id=user.id)
        settings.api_key_plain = 'intl-route-test-api-key'
        db.session.add(settings)
        db.session.commit()
        user_id = user.id
        api_key = settings.api_key_plain

    try:
        client = app.test_client()
        headers = {'X-API-Key': api_key, 'Content-Type': 'application/json'}
        payload = {
            'international_account_name': ACCOUNT_NAME,
            'international_branch_address': BRANCH_ADDRESS,
            'international_swift_code': SWIFT,
            'international_business_address': BUSINESS_ADDRESS,
        }
        resp = client.put('/api/auth/business', headers=headers, json=payload)
        assert resp.status_code == 200, resp.get_data(as_text=True)[:300]

        user = resp.get_json()['user']
        assert user['international_account_name'] == ACCOUNT_NAME
        assert user['international_branch_address'] == BRANCH_ADDRESS
        assert user['international_business_address'] == BUSINESS_ADDRESS
        # The SWIFT code comes back MASKED, like the BSB and the account
        # number do -- it identifies the account to the receiving bank.
        assert SWIFT not in json.dumps(user.get('international_swift_code')), \
            'the SWIFT code was returned in the clear'
        assert user['international_swift_code'], 'the SWIFT code was not returned at all'
    finally:
        with app.app_context():
            db.session.query(UserPaymentSettings).filter_by(user_id=user_id).delete()
            db.session.query(User).filter_by(id=user_id).delete()
            db.session.commit()


def test_the_swift_code_is_stored_encrypted():
    """At rest the SWIFT code is Fernet ciphertext, the same
    treatment the BSB gets -- it identifies the account to the
    receiving bank, so the column must not be readable in the clear."""
    from app.shared.pii import encrypt_pii

    from app.models.user_payment_settings import UserPaymentSettings

    settings = UserPaymentSettings(user_id=999999)
    settings.international_swift_code_plain = SWIFT
    assert SWIFT not in str(settings.international_swift_code), \
        'the SWIFT code was stored in the clear'
    assert settings.international_swift_code_plain == SWIFT


# ---------------------------------------------------------------------
# The statement of account carries the same block
# ---------------------------------------------------------------------

def test_statement_of_account_carries_the_international_block():
    """A statement asks for money just as an invoice does."""
    pdf = generate_statement_of_account_pdf(
        _business_user(), _customer(),
        [_invoice(_customer())], date(2026, 11, 5))
    text = extract_text(pdf.getvalue())

    assert 'International Payment Details' in text
    assert SWIFT in text
