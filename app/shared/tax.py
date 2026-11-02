"""GST treatment rules for customer-facing documents.

Kept in ONE place so the API and the PDF can never disagree about a
document's tax treatment. A quote that says "no GST" on its face and
carries 10% in its totals (or the reverse) is a defective artefact, and
the cheapest way to prevent that is to have exactly one function that
answers the question.

Australian GST on a supply depends on two INDEPENDENT facts:

  1. Is the SUPPLIER registered? A business without an ABN cannot charge
     GST at all -- there is no registration to charge against.
  2. Is the RECIPIENT an Australian GST registrant? A customer who is not
     registered must not be charged Australian GST: the resulting tax
     invoice is not usable by them, and it is wrong.

`derive_gst_type` answers "what should this document carry by default?".
`check_gst_type` answers "is an explicitly-requested rate allowed?".
`gst_note` answers "what does the document say about it?".
"""

from decimal import Decimal

# The only two rates the system supports: nothing, or the standard rate.
GST_RATE = Decimal('0.1')
NO_GST = Decimal('0')

# Country values that mean "inside the Australian GST net".
_AUSTRALIAN = ('australia', 'au')


def _country(customer):
    return (getattr(customer, 'country', '') or '').strip().lower()


def is_australian(customer):
    return _country(customer) in _AUSTRALIAN


def is_overseas(customer):
    """A customer that is KNOWN to be outside Australia.

    An EMPTY country is NOT overseas: it is unknown, and unknown must
    not silently become "no GST" -- that is how tax that is owed gets
    lost. `derive_gst_type` treats an unknown country as Australian for
    charging purposes, so this helper must agree with it.
    """
    country = _country(customer)
    return bool(country) and country not in _AUSTRALIAN


def customer_is_registrant(customer):
    """Is this customer registered for Australian GST?"""
    return bool(getattr(customer, 'gst', False))


def supplier_is_registered(user):
    """Is the business issuing the document GST-registered?"""
    return bool((getattr(user, 'abn', '') or '').strip())


def derive_gst_type(customer, user):
    """The GST rate this document should carry when the caller does not say.

    Conservative by construction: GST is charged only when the supplier is
    registered, the customer is a registrant, and the customer is inside
    Australia. Every "no" is a reason the document must not carry GST.
    """
    if not supplier_is_registered(user):
        return NO_GST
    if not customer_is_registrant(customer):
        return NO_GST
    if is_overseas(customer):
        return NO_GST
    return GST_RATE


def check_gst_type(customer, user, gst_type):
    """Return an error message if an explicitly-requested rate is not allowed.

    An explicit rate of zero is always allowed -- the caller is entitled to
    quote without GST. A NON-zero rate requires a registered supplier and a
    registered recipient.
    """
    if Decimal(str(gst_type)) == NO_GST:
        return None
    if not supplier_is_registered(user):
        return ('This business is not registered for GST (no ABN on the '
                'business profile), so GST cannot be charged.')
    if not customer_is_registrant(customer):
        return ("This customer is not registered for GST, so Australian GST "
                "cannot be charged to them. Set the customer's GST "
                "registration and ABN on their record first, or quote with "
                "no GST.")
    if is_overseas(customer):
        # A customer who IS an Australian GST registrant but is located
        # outside Australia: the country decides, so the flag cannot
        # put Australian GST on an overseas document.
        return ("This customer is located outside Australia, so Australian GST "
                "cannot be charged to them.")
    return None


# Why a document carries the GST treatment it carries. Stored on the
# document at the moment it was raised, so the reason on the page cannot
# change under the customer's feet if their record changes later.
BASIS_CHARGED = 'charged'
BASIS_NOT_REGISTERED = 'not_registered'
BASIS_OUTSIDE_AUSTRALIA = 'outside_australia'
BASIS_SUPPLIER_UNREGISTERED = 'supplier_unregistered'
BASIS_NOT_APPLIED = 'not_applied'


def gst_basis(customer, user, gst_type):
    """The reason this document carries the GST treatment it carries."""
    if Decimal(str(gst_type)) != NO_GST:
        return BASIS_CHARGED
    if not supplier_is_registered(user):
        return BASIS_SUPPLIER_UNREGISTERED
    if not customer_is_registrant(customer):
        if not is_australian(customer):
            return BASIS_OUTSIDE_AUSTRALIA
        return BASIS_NOT_REGISTERED
    return BASIS_NOT_APPLIED


# What each basis means on the page. The wording names the actual reason,
# because a document that says "the customer is not Australian GST
# registered" to a customer in California is stating something that is
# not quite true and invites a question we would rather not be asked.
_BASIS_NOTES = {
    BASIS_NOT_REGISTERED: ('No Australian GST has been applied: the customer is '
                            'not registered for GST.'),
    BASIS_OUTSIDE_AUSTRALIA: ('No Australian GST has been applied: the customer '
                               'is located outside Australia.'),
    BASIS_SUPPLIER_UNREGISTERED: ('No Australian GST has been applied: this '
                                   'business is not registered for GST.'),
    BASIS_NOT_APPLIED: 'No Australian GST has been applied.',
}


def gst_note(customer, user, gst_type):
    """The sentence to print when GST is not charged (empty string if it is).

    Use `gst_note_for_basis` when the basis was stored with the document.
    """
    return _BASIS_NOTES.get(gst_basis(customer, user, gst_type), '')


def gst_note_for_basis(basis):
    """The sentence for a STORED basis.

    A quote is a document: what it says about tax must not change if the
    customer record is updated afterwards. So the basis is captured when
    the quote is raised and the page is rendered from that, not from the
    customer's current record.
    """
    return _BASIS_NOTES.get(basis, '')
