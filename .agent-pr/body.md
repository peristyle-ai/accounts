## What

Peristyle now issues invoices to customers outside Australia. The business
phone was printed in DOMESTIC form on every customer-facing document. The
leading `0` of an Australian number is a trunk prefix, not part of the
number, and there is no country code -- so a recipient outside Australia
cannot dial what the invoice says.

The conversion happens at RENDER time only. The stored value is never
rewritten: the ledger keeps the number as it was recorded, the Xero export
keeps its own convention, and only the document changes.

| document | business phone | customer phone |
|---|---|---|
| invoice | international | international |
| statement of account | international | international |
| quotation | international | international |
| pay slip | **domestic (unchanged)** | n/a |

The pay slip is a deliberate exception: it goes to an Australian employee,
there is no international audience to serve, and the payroll record reads
better in the form it was entered. There is a test that pins that decision,
so it cannot drift into the customer documents by accident.

## The rule

The converter is conservative. It replaces a leading trunk `0` with `+61`
only when the string looks like a domestic Australian number. A number that
is already international, a foreign number without a country code, an
international access prefix, or a fragment too short to be a phone number
is returned **unchanged** -- a formatter that mangles what it does not
understand is worse than one that does nothing.

It is also idempotent: formatting an already-formatted number changes
nothing.

## Files

| file | change |
|---|---|
| `app/shared/phone.py` | **new**: the converter, with its rules stated in the module docstring |
| `app/shared/pdf.py` | the six render sites (3 documents x business/customer phone) use the converter |
| `tests/test_phone_international.py` | **new**: 19 tests -- the conversion, the refusals, idempotence, the rendered invoice's TEXT, that the stored number is untouched, and that the pay slip stays domestic |

## Verification

- Full suite: **562 passed, 2 skipped**.
- The suite runs against LIVE production Postgres. Counts before and after
  the run are identical (6 customers, 21 invoices, 4 quotes, 1 draft) and
  the business address is intact.
- **Four injected weakenings each fail the suite**: the render call
  removed, the converter neutered, the converter made to mangle foreign
  numbers, and the minimum-length guard removed. The last of those was
  caught by the mutation run rather than by the test as first written --
  the short-fragment case had been passing for the wrong reason, and the
  test now pins the length rule specifically.

Note: the statement of account's customer phone was not being escaped for
reportlab markup (the invoice and quotation paths were). It is now, on
the same line as the change.
