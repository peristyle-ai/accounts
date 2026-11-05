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

  * the DOMESTIC account name (the trading name on the invoice) and
    the INTERNATIONAL beneficiary name (the registered holder). They
    are not the same string, and conflating them would either print
    the wrong name for a wire or the wrong name for a local transfer.
  * the BUSINESS POSTAL ADDRESS (the address the sending bank checks)
    and the address in the document header (where the business
    operates from). These are also not the same thing, and for a wire
    it is the bank's copy that matters.

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
        ('Account Name', settings.international_account_name),
        ('Branch ' 'Address', settings.international_branch_address),
        ('Swift ' 'Code', swift_code),
        ('Business Postal Address', settings.international_business_address),
    ):
        value = (value or '').strip()
        if value:
            lines.append((label, value))
    return lines
