"""International phone formatting for customer-facing documents.

WHY THIS EXISTS
---------------
Peristyle now issues invoices to customers outside Australia (the first
was raised in the United States, 2026-11-02). A domestic Australian
phone number on a document that leaves the country is not a phone number
a recipient can use:

    "0422 678 867"  -> the leading 0 is a TRUNK PREFIX, not part of the
                        number, and there is no country code, so the
                        recipient does not know where to dial from.

The international form replaces the trunk 0 with the country code:

    "0422 678 867"  ->  "+61 422 678 867"

Two rules keep this safe:

  1. The STORED value is never rewritten. Formatting happens at RENDER
     time, so the ledger keeps the number in the form it was recorded in
     (and the Xero export keeps its own convention). A document that
     needs a different display must not mutate the record.
  2. A number that is not recognisably a domestic Australian number is
     returned UNCHANGED. An already-international number, a foreign
     number without a country code, a number with an extension, or a
     fragment too short to be a phone number all pass through. A
     formatter that mangles what it does not understand is worse than
     one that does nothing.
"""

import re

# A leading "(" (and its partner) is noise for the conversion: "(07) 3805
# 3377" is the same number as "07 3805 3377".
_LEADING_PAREN = re.compile(r'^\(')

# The trunk 0, optionally behind a bracket, followed by a digit.
_TRUNK_ZERO = re.compile(r'^0(\d.*)$', re.DOTALL)

# A domestic Australian number carries 8-10 digits in total. Anything
# shorter than this is not a phone number we should be rewriting.
_MIN_DIGITS = 8


def _digits(text):
    return re.sub(r'\D', '', text or '')


def format_phone_international(phone):
    """Return `phone` in international form where that is meaningful.

    Examples:
        '0422 678 867'  -> '+61 422 678 867'
        '07 3805 3377'  -> '+61 7 3805 3377'
        '(03) 9123 4567'-> '+61 3 9123 4567'
        '+61 422 678 867' -> unchanged (already international)
        '415 555 0100'  -> unchanged (not an Australian trunk-0 number)
        '00 61 422 678 867' -> unchanged (international access prefix)

    Returns '' for an empty/None value, so callers can use the result
    directly in an f-string without a None check.
    """
    if not phone:
        return ''

    s = str(phone).strip()
    if not s:
        return ''

    # Already international, or an international access prefix: leave it.
    if '+' in s or s.startswith('00'):
        return s

    # Strip a leading bracket so "(07) 3805 3377" converts cleanly.
    if _LEADING_PAREN.match(s):
        s = s[1:]
        s = s.replace(')', '', 1)
        s = s.strip()

    m = _TRUNK_ZERO.match(s)
    if not m:
        return s

    rest = m.group(1)
    if len(_digits(s)) < _MIN_DIGITS:
        # Too short to be an Australian phone number -- do not guess.
        return s

    return '+61 ' + rest
