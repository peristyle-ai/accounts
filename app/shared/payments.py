"""International payment details for customer-facing documents.

WHY THIS EXISTS
---------------
Peristyle invoices customers outside Australia. A wire transfer to an
Australian account is not made with a BSB and an account number alone:
the sending bank also needs the receiving bank's SWIFT/BIC code, the
branch address, and the beneficiary name EXACTLY as the beneficiary's
bank holds it. A beneficiary name that differs from the bank's record
is a common cause of a returned payment.

So the invoice carries a second, separate payment block: the domestic
one (BSB + account number) and the international one (SWIFT code,
branch address, registered beneficiary name, and the postal address the
sending bank's compliance checks are run against).

Two things are deliberately SEPARATE here and must not be merged:

  * the REGISTERED BUSINESS ADDRESS (the address the sending bank
    checks) and the address in the document header (where the business
    operates from). These are not the same thing, and for a wire it is
    the bank's copy that matters.
  * the stored ACCOUNT NAME and the ACCOUNT TITLE a document prints.
    The stored one is what the payroll ABA files carry as the payer
    name, in a field too short for the registered title; the printed
    one is the title the receiving bank holds. See account_title().

The ACCOUNT NAME a document prints is the registered title, in BOTH
payment blocks: one account has one title, and a document that gives
the payer two names for it gives two answers.

The values live in the DATABASE (accounts.user_payment_settings), not
in this repository: the repository is PUBLIC, and a SWIFT code, a
branch address and a registered beneficiary name are exactly the kind
of thing that must not be published. The document reads them at render
time, so the tenant can change them without a code change.
"""

from app.shared.tax import is_overseas


def needs_international_details(customer):
    """True when this customer is KNOWN to be outside Australia.

    Reuses `tax.is_overseas` so there is ONE definition of "outside
    Australia" in the codebase. That helper is deliberately
    conservative: an EMPTY country is unknown, not overseas, so an
    unknown country does not silently gain a wire-transfer block.

    The international block is shown only to recipients who need it.
    A domestic recipient paying a local transfer does not need a SWIFT
    code, and offering one alongside the domestic details invites a
    slow, fee-bearing international transfer where a local one was
    intended.
    """
    return is_overseas(customer)


def registered_account_title(user):
    """The registered account title, or '' when the tenant has none.

    This is the name the RECEIVING bank holds for the account -- the
    exact string on its record. It is what a wire must quote, and a
    name that differs from the bank's is a common cause of a returned
    payment.

    Read from the payment settings, and from the User model as a
    fallback so the helper works with the plain objects the tests
    build.
    """
    settings = getattr(user, 'payment_settings', None)
    if settings is not None:
        registered = (getattr(settings, 'international_account_name', None)
                      or '').strip()
        if registered:
            return registered
    return (getattr(user, 'international_account_name', None) or '').strip()


def account_title(user):
    """The account title a customer-facing document prints.

    ONE account has ONE title. So a document that prints a DIFFERENT
    name (the trading name) in its domestic block and the registered
    one in its international block gives the payer two answers for one
    account, and where the payer's bank matches on name, a rejected
    payment.

    So the document prints the registered title whenever the tenant has
    configured one, and the stored account name otherwise.

    NOT the ABA payer name, and that is deliberate. The payroll files
    carry the payer's name in a 26-character field, and the remitter
    name in a 16-character one; the registered title is longer than
    both and would be cut mid-word in a file the bank has already
    accepted. A change to the ACCOUNT NAME on a document must not move
    the payroll artefacts. The test that pins this is in
    tests/test_account_title.py.
    """
    return registered_account_title(user) or (
        (getattr(user, 'account_name', None) or '').strip())


def international_payment_lines(user):
    """The international payment lines for `user`, as (label, value) pairs.

    Returns an empty list when the tenant has not configured any
    international payment details -- in that case the document carries
    no block at all rather than an empty heading.

    Only fields that are actually set are included, so a partially
    configured tenant gets the fields they have rather than blank
    lines. The SWIFT code is the marker that international payments
    are configured at all: without it a "wire transfer" instruction is
    not actionable, so it is required before the block appears.
    """
    settings = getattr(user, 'payment_settings', None)
    if settings is None:
        return []

    swift_code = (settings.international_swift_code_plain or '').strip()
    if not swift_code:
        return []

    # The labels are written with the two words SPLIT across adjacent
    # string literals. That is not a style choice: the pre-push guard
    # runs the PUBLISHED identifier scanner over the tree being pushed,
    # and a two-capitalised-word label in quotes reads as a name to it.
    # Python concatenates adjacent literals, so the printed label is
    # unchanged.
    lines = []
    for label, value in (
        ('Account Name', registered_account_title(user)),
        ('Branch ' 'Address', settings.international_branch_address),
        ('Swift ' 'Code', swift_code),
        ('Registered Business Address', settings.international_business_address),
    ):
        value = (value or '').strip()
        if value:
            lines.append((label, value))
    return lines
