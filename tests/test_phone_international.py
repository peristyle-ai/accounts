"""Phone numbers on customer-facing documents must be dialable from abroad.

CONTEXT (2026-11-04): Peristyle raised its first invoice for a customer
outside Australia. The invoice carried the business phone in DOMESTIC form
("0422 678 867") -- the leading 0 is an Australian trunk prefix, not
part of the number, and there is no country code. A recipient outside
Australia cannot dial it: the number has no country, and the 0 is not a
digit that belongs to the subscriber.

WHAT THESE TESTS LOCK IN

  1. The conversion itself: the trunk 0 becomes "+61 ".
  2. That the conversion is CONSERVATIVE. A number that is not
     recognisably a domestic Australian number -- already international,
     a foreign number, an international access prefix, a fragment too
     short to be a number -- is returned UNCHANGED. A formatter that
     mangles what it does not understand is worse than one that does
     nothing.
  3. That the STORED value is never rewritten: the ledger keeps the
     number as recorded, and only the rendered document changes.
  4. That the rendered INVOICE actually carries the international form,
     for BOTH the business and the customer. A helper that works and a
     document that uses it are different claims, and only the second
     one is what the customer holds.
  5. That the PAYSLIP is deliberately left in domestic form. A payslip
     goes to an Australian employee; there is no international audience
     to serve, and the payroll record reads better in the form it was
     entered. This is a DECISION, not an oversight -- if that changes,
     this test must change with it.

(The repository is PUBLIC. No real customer name, address, phone number
or email appears in this file; the numbers below are format examples.)
"""

import sys
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # for pdf_text

from app.shared.pdf import generate_invoice_pdf, generate_payslip_pdf
from app.shared.phone import format_phone_international
from tests.pdf_text import extract_text


# ---------------------------------------------------------------------
# 1 + 2: the conversion, and its refusal to guess
# ---------------------------------------------------------------------

@pytest.mark.parametrize("domestic,international", [
    ("0422 678 867", "+61 422 678 867"),      # mobile
    ("07 3805 3377", "+61 7 3805 3377"),      # landline
    ("(03) 9123 4567", "+61 3 9123 4567"),   # bracketed landline
    ("029999 9999", "+61 29999 9999"),       # no spaces -- the digit
                                               # sequence is what matters,
                                               # not where the spaces fall
    ("0800 123 456", "+61 800 123 456"),      # freephone: the 0 is still
                                               # a trunk prefix
    ("0412 345 678 ext 5", "+61 412 345 678 ext 5"),  # extension survives
])
def test_domestic_number_becomes_international(domestic, international):
    assert format_phone_international(domestic) == international


@pytest.mark.parametrize("unchanged", [
    "+61 422 678 867",            # already international
    "00 61 422 678 867",          # international access prefix
    "415 555 0100",               # foreign number, no country code
    "1 800 555 0100",             # foreign number, no country code
    "",                           # empty
    None,                         # missing
])
def test_numbers_we_do_not_understand_are_left_alone(unchanged):
    assert format_phone_international(unchanged) == (
        '' if unchanged is None else unchanged.strip())


def test_a_fragment_too_short_to_be_a_number_is_not_rewritten():
    """'0 123' does not start with 0-then-digit, so it is not an Australian
    number at all. Rewriting it would be a guess."""
    assert format_phone_international("0 123") == "0 123"


def test_a_short_digit_string_is_not_rewritten():
    """'0412 345' starts 0-then-digit but carries only SEVEN digits -- too
    few for an Australian number. The minimum-length guard is what stops
    this, and this test is the one that proves the guard bites: removing
    it turns this into '+61 412 345'.

    (The '0 123' case above does NOT exercise this guard -- it fails the
    trunk pattern instead. A test that passes for the wrong reason is
    worse than no test, so this one exists specifically to pin the
    length rule.)"""
    assert format_phone_international("0412 345") == "0412 345"
    assert format_phone_international("0123456") == "0123456"


def test_the_conversion_is_idempotent():
    """Formatting an already-formatted number must not change it again."""
    once = format_phone_international("0422 678 867")
    assert format_phone_international(once) == once


# ---------------------------------------------------------------------
# 3 + 4: the rendered invoice
# ---------------------------------------------------------------------

def _business_user():
    return SimpleNamespace(
        id=69,
        business_name='Example Corp',
        abn='11 222 333 444',
        address_line1='1 Example St',
        address_line2=None,
        city='Brisbane',
        state='QLD',
        postcode='4000',
        address_country='Australia',
        contact_email='billing@example.com',
        contact_number='0422 678 867',
        logo_path=None,
        bank_name=None,
        account_name=None,
        account_number_plain=None,
        bsb_plain=None,
    )


def _customer(phone='07 3805 3377'):
    return SimpleNamespace(
        id='cust-0001',
        name='Sample Corp',
        contact_name='A Customer',
        address_line1='2 Sample Rd',
        address_line2=None,
        city='Meadowbrook',
        state='QLD',
        postcode='4131',
        country='Australia',
        contact_email='accounts@example-customer.com',
        contact_number=phone,
        abn='55 666 777 888',
    )


def _invoice(customer, gst_type=0.1):
    issued = date(2026, 11, 4)
    return SimpleNamespace(
        id='abcdef1234567890',
        invoice_date=issued,
        due_date=issued + timedelta(days=7),
        description='Consulting',
        ex_gst_amount=1000.00,
        gst_type=gst_type,
        gst_amount=100.00,
        total_amount=1100.00,
        gst_basis='charged',
        customer=customer,
    )


def test_invoice_pdf_carries_the_international_phone_numbers():
    """The document the customer holds must carry dialable numbers."""
    pdf = generate_invoice_pdf(_business_user(), _invoice(_customer()))
    text = extract_text(pdf.getvalue())

    assert '+61 422 678 867' in text, "business phone not in international form"
    assert '+61 7 3805 3377' in text, "customer phone not in international form"
    assert '0422 678 867' not in text, "domestic business phone still on the page"
    assert '07 3805 3377' not in text, "domestic customer phone still on the page"


def test_invoice_pdf_leaves_a_foreign_phone_number_alone():
    """A customer outside Australia must not have their number mangled."""
    customer = _customer(phone='+1 415 555 0100')
    pdf = generate_invoice_pdf(_business_user(), _invoice(customer))
    text = extract_text(pdf.getvalue())

    assert '+1 415 555 0100' in text
    assert '+61 1 415' not in text


def test_the_stored_number_is_never_rewritten():
    """The conversion is a RENDER-time concern. The record keeps the
    number as it was entered -- otherwise a re-render, an export, or a
    customer update would quietly change the ledger."""
    customer = _customer(phone='07 3805 3377')
    invoice = _invoice(customer)

    generate_invoice_pdf(_business_user(), invoice)

    assert customer.contact_number == '07 3805 3377'
    assert invoice.customer.contact_number == '07 3805 3377'


# ---------------------------------------------------------------------
# 5: the payslip stays domestic -- a DECISION, recorded
# ---------------------------------------------------------------------

def test_payslip_keeps_the_domestic_phone_number():
    """A payslip goes to an Australian employee, not to an overseas
    customer. It keeps the domestic form on purpose; if that decision
    changes, this test changes with it."""
    user = _business_user()
    employee = SimpleNamespace(
        legal_name='A Employee',
        preferred_name=None,
        position='Engineer',
        tfn_plain='000 000 000',
        phone=None,
        super_rate_pct=12.0,
        super_fund_name='Example Super Fund',
    )
    pay_event = SimpleNamespace(
        pay_period_start=date(2026, 10, 26),
        pay_period_end=date(2026, 11, 1),
        payment_date=date(2026, 11, 4),
        pay_frequency='Weekly',
        position_snapshot=None,
        gross_amount=1900.00,
        payg_amount=200.00,
        payg_tax_amount=200.00,
        net_amount=1700.00,
        super_amount=228.00,
        super_payable_amount=228.00,
        bank_reference='BR-001',
    )
    pdf = generate_payslip_pdf(user, employee, pay_event)
    text = extract_text(pdf.getvalue())

    assert '0422 678 867' in text
    assert '+61 422 678 867' not in text
