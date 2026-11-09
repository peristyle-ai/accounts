"""The account TITLE a customer-facing document prints.

CONTEXT (2026-11-05): Peristyle's bank account is held by a sole
trader, and the account title the RECEIVING bank has on record is the
registered one -- the person's name trading as the business -- not the
trading name alone. A document that printed the trading name in its
domestic payment block and the registered name in its international
block gave the payer two different names for one account, and where
the payer's bank matches on name, that is a returned payment.

So the document prints the registered title in BOTH blocks. That is a
DOCUMENT change and nothing else: the stored account name is what the
payroll ABA files carry as the payer name, in a 26-character field,
and the registered title is longer than that. Printing the title on a
document must not silently move a payroll artefact that the bank has
already accepted -- these tests pin that too.

(The repository is PUBLIC. No real account name, title, address or
bank detail appears in this file; the values are synthetic examples.)
"""

import sys
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # for pdf_text

from app.shared.aba import build_header_record
from app.shared.pdf import generate_invoice_pdf, generate_statement_of_account_pdf
from app.shared.payments import account_title
from tests.pdf_text import extract_text

# Synthetic examples, and the placeholders the identifier scanner's
# allowlist already carries: a quoted two-word capitalised pair that
# is NOT on that list is a finding, and the pre-push guard runs the
# PUBLISHED scanner over the tree being pushed.
#
# The registered title is deliberately longer than the ABA's
# 26-character payer-name field, and deliberately ENDS with the
# trading name, so the tests can prove the two are not the same
# string and that an assertion anchored on the trading name alone
# would fail for the wrong reason.
TRADING_NAME = 'Example Corp'
REGISTERED_TITLE = 'Example Person trading as Example Corp'
BANK_NAME = 'Examplebank'
BSB = '000-000'
ACCOUNT_NUMBER = '12345678'


def _user(with_registered_title=True):
    """A tenant that has configured the registered account title."""
    settings = SimpleNamespace(
        international_account_name=REGISTERED_TITLE if with_registered_title else None,
        international_branch_address='12 Example Street',
        international_swift_code_plain='EXAMPLUS33XXX',
        international_business_address='34 Example Street',
    )
    return SimpleNamespace(
        business_name=TRADING_NAME,
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
        bank_name=BANK_NAME,
        account_name=TRADING_NAME,
        account_number_plain=ACCOUNT_NUMBER,
        bsb_plain=BSB,
        de_user_id_plain='000000',
        payment_settings=settings,
    )


def _customer():
    return SimpleNamespace(
        name='Examplerecipient',
        contact_name='Examplecontact',
        address_line1='5 Example Avenue',
        address_line2=None,
        city='Exampletown',
        state='NY',
        postcode='10001',
        country='United States',
        contact_email='billing@example-customer.com',
        contact_number='+1 212 555 0100',
        abn=None,
        invoice_cc_emails=[],
    )


def _invoice(customer=None):
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
        customer=customer if customer is not None else _customer(),
    )


def _bank_details_block(text):
    """The domestic Bank Details section of the rendered document.

    The whole document is not enough: the international block carries an
    Account Name line too, and a test that only searched the whole
    text would pass because of THAT line -- the wrong line, for the
    wrong block. A test that passes for the wrong reason is worse than
    no test, so the section is cut out explicitly.
    """
    lines = text.split('\n')
    try:
        start = lines.index('Bank Details:')
    except ValueError:
        raise AssertionError('no Bank Details section in the document')
    rest = lines[start + 1:]
    end = len(rest)
    for i, line in enumerate(rest):
        if line.endswith(':') and 'Bank Details:' not in line:
            end = i
            break
    return rest[:end]


# ---------------------------------------------------------------------
# The document prints the registered title
# ---------------------------------------------------------------------

def test_the_domestic_block_prints_the_registered_title():
    """Michael's change: the domestic Bank Details block must not give
    the payer a different name for the same account."""
    pdf = generate_invoice_pdf(_user(), _invoice())
    block = _bank_details_block(extract_text(pdf.getvalue()))

    assert f'Account Name: {REGISTERED_TITLE}' in block, (
        'the domestic block does not carry the registered title')
    assert f'Account Name: {TRADING_NAME}' not in block, (
        'the domestic block still prints the trading name')


def test_the_invoice_carries_one_title_in_both_blocks():
    """One account, one title. The domestic block and the international
    block must not disagree about whose account it is."""
    text = extract_text(generate_invoice_pdf(_user(), _invoice()).getvalue())

    assert text.count(f'Account Name: {REGISTERED_TITLE}') == 2, (
        'the two blocks do not carry the same title')
    # Anchored on the LABEL, because the trading name is a SUFFIX of
    # the registered title -- 'Account Name: Peristyle' is not a
    # substring of 'Account Name: ... T/AS PERISTYLE', but the bare
    # trading name is. A bare-substring assertion would fail for the
    # wrong reason.
    domestic = text.split('Bank Details:')[1].split(
        'International Payment Details:')[0]
    assert f'Account Name: {TRADING_NAME}' not in domestic, (
        'the trading name is still printed as the account name')


def test_the_statement_of_account_prints_the_registered_title():
    """A statement asks for money just as an invoice does."""
    pdf = generate_statement_of_account_pdf(
        _user(), _customer(), [_invoice()], date(2026, 11, 5))
    text = extract_text(pdf.getvalue())

    # The statement sets the label in bold, and ReportLab draws a bold
    # run and its value as SEPARATE runs, so the extracted text has
    # them on separate lines. Flatten the runs before matching.
    flat = ' '.join(text.split())
    assert f'Account name: {REGISTERED_TITLE}' in flat, (
        'the statement does not carry the registered title')
    assert f'Account name: {TRADING_NAME}' not in flat


# ---------------------------------------------------------------------
# The label on the international block
# ---------------------------------------------------------------------

def test_the_international_address_is_labelled_the_registered_business_address():
    """The address the sending bank checks is the REGISTERED one, and
    saying so on the document tells the reader it is not the address
    the business operates from."""
    text = extract_text(generate_invoice_pdf(_user(), _invoice()).getvalue())

    assert 'Registered Business Address: 34 Example Street' in text
    assert 'Business Postal Address' not in text, (
        'the old label is still on the document')


# ---------------------------------------------------------------------
# The payroll artefacts must not move
# ---------------------------------------------------------------------

def test_the_aba_payer_name_is_still_the_stored_account_name():
    """A document change must not move a payroll artefact.

    The ABA header carries the payer name in a 26-character field and
    the remitter name in a 16-character one. The registered title is
    longer than both, so printing it there would cut it mid-word in a
    file the bank has already accepted. So the ABA keeps the stored
    account name -- and this test fails if anyone routes the title
    into it.
    """
    user = _user()
    record = build_header_record(user, date(2026, 11, 5))

    payer = record[30:56]
    assert payer == TRADING_NAME.ljust(26), (
        'the ABA payer name moved off the stored account name')
    assert REGISTERED_TITLE[:26] not in payer, (
        'the ABA payer name was cut from the registered title')


# ---------------------------------------------------------------------
# The helper itself
# ---------------------------------------------------------------------

def test_the_title_falls_back_to_the_account_name_when_none_is_configured():
    """A tenant that has not configured a registered title is
    unaffected: its documents carry the account name they always did."""
    assert account_title(_user(with_registered_title=False)) == TRADING_NAME


def test_the_title_is_the_registered_one_when_configured():
    assert account_title(_user()) == REGISTERED_TITLE


def test_a_title_containing_markup_cannot_break_the_render():
    """ReportLab parses a Paragraph's text as markup, so an unescaped
    value can corrupt the document."""
    user = _user()
    user.payment_settings.international_account_name = 'A & B <Holdings>'

    pdf = generate_invoice_pdf(user, _invoice())
    text = extract_text(pdf.getvalue())

    assert 'A & B' in text, 'the ampersand was mangled'
