"""Pure-function builder for SAFF (SuperStream Alternative File Format) files.

Used by the AustralianSuper export endpoint. The SAFF is the ATO's CSV
specification that employers and payroll software can produce and upload
to a clearing house or directly to a super fund's portal (AustralianSuper
accepts direct uploads via their Employer Portal).

Spec sources (best effort — full standard isn't publicly browsable):
- QuickSuper SuperStream SAFF v1.0 (Westpac) — defines the 4-row file
  structure and columns A-AC.
- ATO Contributions Message Implementation Guide v1.4 — defines the
  underlying data elements (Schedule 4(a) of the Superannuation Data and
  Payment Standard).
- Lightning Payroll / Dayforce docs — confirm the ATO Header+Section+
  Columns+Data structure and the typical contribution column names.

File structure (per QuickSuper spec):
  Row 1: Header values (label/value pairs): VERSION, 1.0,
         NEGATIVES SUPPORTED, false, FILE ID, <unique-id>
  Row 2: Section headings (ignored by clearing houses — we leave blank)
  Row 3: Column headings
  Row 4+: One per contribution (a single employee can have one row per
          pay period; multiple amounts can be on the same row by summing
          or splitting into multiple rows — we emit one row per pay event).

Why this exists (2026-09-17): the previous AustralianSuper export
attempt used a custom 12-column CSV that the AusSuper portal rejected
("we only accept SAFF"). This module generates the actual SAFF format
so the upload goes through.

Known limitations:
- We don't store Date of Birth on Employee yet, so it's blank in the
  SAFF. AusSuper may or may not require it — to be confirmed after
  first upload. If needed, add dob to Employee + populate here.
- We don't store TFN (not yet a payroll feature).
- We default to AustralianSuper as the fund name. For multi-fund employers
  the fund details would come from the Employee record.
"""

import csv
import io
import os
from datetime import date


# AustralianSuper identifiers (used for the payee/receiver context).
# Default fund identifiers — read at runtime from system_settings
# (see app/models/system_setting.py for lookup precedence). The
# AusSuper defaults are seeded into the table by seed_defaults() on app
# boot, so existing installations get the same values without manual
# setup. New deployments can override via the table or via env vars
# (`SETTING_DEFAULT_FUND_ABN` / `SETTING_DEFAULT_FUND_USI` /
# `SETTING_DEFAULT_FUND_NAME`).

def _resolve_payee_defaults():
    """Read payee ABN/USI/org from system_settings, with env-var fallback.

    Called on every build_saff_csv() invocation so admin changes via
    PUT /api/settings/<key> take effect immediately without restart.
    """
    from app.models.system_setting import get_setting
    return {
        'payee_abn': get_setting('DEFAULT_FUND_ABN'),
        'payee_usi': get_setting('DEFAULT_FUND_USI'),
        'payee_org_name': get_setting('DEFAULT_FUND_NAME'),
    }


# SAFF column layout. This is a pragmatic subset of the ATO Standard's
# Schedule 4(a) — enough to identify the employee + the contribution. We
# include the QuickSuper-spec columns A-U first so the file passes
# structural validation, then the contribution-relevant columns at the end.
#
# IMPORTANT: column ORDER matters — AustralianSuper's portal reads fields
# by position (via their File Mapper or direct column-name match). If they
# reject the upload with a specific column-position error, the helper here
# is the one place to fix.
COLUMNS = (
    # --- QuickSuper-spec fields A-AC (1-29). Clearing houses ignore most
    # of these; we still emit them for compatibility with strict validators.
    'ID',                              # A: optional internal tracking id
    'SourceEntityID',                  # B
    'SourceEntityIDType',              # C
    'SourceElectronicServiceAddress',  # D
    'ElectronicErrorMessaging',        # E
    'SenderABN',                       # F: ignored by QuickSuper, kept for compat
    'SenderOrganisationName',          # G
    'SenderFamilyName',                # H
    'SenderGivenName',                 # I
    'SenderOtherGivenName',            # J
    'SenderEmailAddress',              # K
    'SenderTelephoneMinimalNumber',    # L
    'PayerABN',                        # M: ignored by QuickSuper, kept for compat
    'PayerOrganisationName',           # N
    'PayerBSBNumber',                  # O
    'PayerAccountNumber',              # P
    'PayerAccountNameText',            # Q
    'PayeeABN',                        # R: AustralianSuper's ABN
    'PayeeUSI',                        # S: AustralianSuper's USI
    'PayeeOrganisationName',           # T: 'AustralianSuper'
    'PayeeTargetElectronicServiceAddress',  # U: blank (not an SMSF)

    # --- Employee section (AufEmp / common SuperStream elements).
    'TFN',                             # encrypted at rest, plaintext here
    'FamilyName',                      # employee family name
    'GivenName',                       # employee given name
    'OtherGivenName',                  # employee middle name (blank)
    'DateOfBirth',                     # YYYY-MM-DD (added 2026-09-17)
    'Sex',                             # M / F / X — ATO standard code (added 2026-09-17)
    'EmploymentStartDate',             # YYYY-MM-DD (not stored; blank)
    'AddressLine1',                    # street address (added 2026-09-17)
    'AddressLine2',                    # blank (added 2026-09-17)
    'Suburb',                          # locality (added 2026-09-17)
    'State',                           # 2-3 char state code (added 2026-09-17)
    'Postcode',                        # 4 digit AU postcode (added 2026-09-17)
    'EmailAddress',                    # blank — not stored on Employee
    'PhoneNumber',                     # mobile/landline (added 2026-09-17)

    # --- Contribution details (the parts AustralianSuper cares about).
    'PayrollNumberIdentifier',         # internal batch id (we use period end)
    'FundMemberNumber',                # ← critical: identifies the member
    'PayPeriodStartDate',              # YYYY-MM-DD
    'PayPeriodEndDate',                # YYYY-MM-DD
    'TransactionDate',                 # YYYY-MM-DD (date the contribution is paid)
    'PaymentReference',                # reference shown on member statement
    'EmployerSuperGuaranteeAmount',   # 12% SGC — main column
    'EmployerAdditionalAmount',        # discretionary employer top-ups (0)
    'MemberVoluntaryContributionAmount',  # salary sacrifice (0)
    'TotalContributionAmount',         # SGC + additional + voluntary
    'PayrollPaymentFrequency',         # 'Weekly' / 'Fortnightly' / etc.
)


def _is_empty(value):
    return value is None or (isinstance(value, str) and not value.strip())


def _format_amount(value):
    """Format a dollar amount as a plain decimal with 2 dp."""
    if value is None or value == '':
        return ''
    from decimal import Decimal
    return str(Decimal(str(value)).quantize(Decimal('0.01')))


def _normalize_tfn(value):
    """Normalise a TFN to 9 bare digits (no spaces, hyphens, or dots).

    SAFF best practice is bare 9 digits. We accept any reasonable input shape
    (spaces, hyphens, or dots — see ATO convention for TFN formatting) and
    emit bare digits. Returns blank if the value is missing or has the wrong
    number of digits.
    """
    if value is None or value == '':
        return ''
    import re
    digits = re.sub(r'\D', '', str(value))
    if len(digits) != 9:
        return ''
    return digits


def _format_date_iso(d):
    if d is None or d == '':
        return ''
    if isinstance(d, str):
        # Already-formatted ISO string (common when rows come from the DB).
        return d
    return d.isoformat()


def build_saff_csv(rows, file_id=None, options=None):
    """Build a SAFF-formatted CSV (string).

    Args:
      rows: iterable of contribution rows. Each dict should have:
            - 'member_number'      (str) — required, identifies the fund member
            - 'family_name'         (str) — required for matching
            - 'given_name'          (str)
            - 'tfn'                 (str, optional) — 9 digits, no spaces. SAFF
                                     best practice is to include it; the ATO
                                     doesn't strictly require it for a single
                                     employer contributing to one fund but it
                                     helps the clearing house match the row
                                     to the right member.
            - 'date_of_birth'       (date, optional) — ISO format. Many funds
                                     require this for unambiguous matching.
            - 'pay_period_start'    (date)
            - 'pay_period_end'      (date)
            - 'transaction_date'    (date)
            - 'payment_reference'   (str, optional)
            - 'sgc_amount'          (Decimal/str) — Super Guarantee, required
            - 'additional_amount'   (Decimal/str, optional, default 0)
            - 'voluntary_amount'    (Decimal/str, optional, default 0)
      file_id: optional duplicate-check ID (max 20 chars, alphanumeric+_-.).
               If not provided, we generate one from the current timestamp.
      options: dict of overrides:
            - 'payer_abn':         str
            - 'payer_org_name':    str
            - 'payer_bsb':         str (NNN-NNN)
            - 'payer_account':     str
            - 'payer_account_name':str
            - 'sender_abn':        str
            - 'payee_abn':         str (defaults to AustralianSuper's ABN)
            - 'payee_usi':         str (defaults to AustralianSuper's USI)
            - 'payee_org_name':    str (defaults 'AustralianSuper')
            - 'payroll_frequency': str (defaults 'Weekly')

    Returns: a CSV string (UTF-8 without BOM — clearing houses prefer raw
             ASCII/Latin-1; SAFF is fine with UTF-8 for the data rows but
             header labels are ASCII-only per spec).
    """
    from decimal import Decimal

    options = options or {}

    # Default fund identifiers — read from system_settings table.
    payee_defaults = _resolve_payee_defaults()
    payee_abn = options.get('payee_abn') or payee_defaults['payee_abn']
    payee_usi = options.get('payee_usi') or payee_defaults['payee_usi']
    payee_org_name = options.get('payee_org_name') or payee_defaults['payee_org_name']

    # Payer (employer) identifiers. payer_abn/bsb/account come from the
    # User row at call time (passed in via options). payer_org_name and
    # payer_account_name default to the BUSINESS_NAME setting so they
    # don't have to be threaded through every caller.
    from app.models.system_setting import get_setting as _gs
    business_name = _gs('BUSINESS_NAME')
    payer_abn = options.get('payer_abn', '')
    payer_org_name = options.get('payer_org_name') or business_name
    payer_bsb = options.get('payer_bsb', '')
    payer_account = options.get('payer_account', '')
    payer_account_name = options.get('payer_account_name') or business_name

    sender_abn = options.get('sender_abn', payer_abn)
    payroll_frequency = options.get('payroll_frequency', 'Weekly')

    # FILE ID: timestamp-based if not provided. Max 20 chars per spec.
    if not file_id:
        from datetime import datetime
        file_id = datetime.now().strftime('%Y%m%d%H%M%S')[:20]

    buf = io.StringIO()

    # Row 1: Header values (label/value pairs)
    # Per QuickSuper spec — these are validated literally.
    header_row = (
        f'VERSION,1.0,'
        f'NEGATIVES SUPPORTED,false,'
        f'FILE ID,{file_id}'
    )
    buf.write(header_row + '\r\n')

    # Row 2: Section headings — ignored by the validator but required
    # to be present per the spec. We emit the standard section names from
    # the QuickSuper doc so a strict reader recognises the file structure.
    buf.write(','.join([
        'LINE ID', '', 'HEADER', '', '', '', '', '', '', '', '',
        'SENDER', '', '', '', '', '', '',
        'PAYER', '', '', '', '', '',
        'PAYEE/RECEIVER', '', '', '', '',
        'EMPLOYEE', '', '', '', '', '', '', '', '', '', '', '', '',
        'CONTRIBUTION', '', '', '', '', '', '', '', '',
    ]) + '\r\n')

    # Row 3: Column headings
    writer = csv.DictWriter(buf, fieldnames=list(COLUMNS), extrasaction='ignore')
    writer.writeheader()

    # Rows 4+: One per contribution
    for row in rows:
        sgc = Decimal(str(row.get('sgc_amount', '0') or '0'))
        additional = Decimal(str(row.get('additional_amount', '0') or '0'))
        voluntary = Decimal(str(row.get('voluntary_amount', '0') or '0'))
        total = sgc + additional + voluntary

        # ID: a unique-ish per-row identifier for support/debugging
        row_id = f'PAY-{_format_date_iso(row.get("pay_period_end"))}-{row.get("member_number")}'

        writer.writerow({
            'ID': row_id,
            'SourceEntityID': '',
            'SourceEntityIDType': '',
            'SourceElectronicServiceAddress': '',
            'ElectronicErrorMessaging': '',
            'SenderABN': sender_abn,
            'SenderOrganisationName': payer_org_name,
            'SenderFamilyName': '',
            'SenderGivenName': '',
            'SenderOtherGivenName': '',
            'SenderEmailAddress': '',
            'SenderTelephoneMinimalNumber': '',
            'PayerABN': payer_abn,
            'PayerOrganisationName': payer_org_name,
            'PayerBSBNumber': payer_bsb,
            'PayerAccountNumber': payer_account,
            'PayerAccountNameText': payer_account_name,
            'PayeeABN': payee_abn,
            'PayeeUSI': payee_usi,
            'PayeeOrganisationName': payee_org_name,
            'PayeeTargetElectronicServiceAddress': '',
            'TFN': _normalize_tfn(row.get('tfn')),
            'FamilyName': row.get('family_name', ''),
            'GivenName': row.get('given_name', ''),
            'OtherGivenName': '',
            'DateOfBirth': _format_date_iso(row.get('date_of_birth')),
            'Sex': row.get('sex', ''),
            'EmploymentStartDate': _format_date_iso(row.get('employment_start_date')),
            'AddressLine1': row.get('address_line1', ''),
            'AddressLine2': row.get('address_line2', ''),
            'Suburb': row.get('city', ''),
            'State': row.get('state', ''),
            'Postcode': row.get('postcode', ''),
            'EmailAddress': '',
            'PhoneNumber': row.get('phone', ''),
            'PayrollNumberIdentifier': _format_date_iso(row.get('pay_period_end')).replace('-', ''),
            'FundMemberNumber': row.get('member_number', ''),
            'PayPeriodStartDate': _format_date_iso(row.get('pay_period_start')),
            'PayPeriodEndDate': _format_date_iso(row.get('pay_period_end')),
            'TransactionDate': _format_date_iso(row.get('transaction_date')),
            'PaymentReference': row.get('payment_reference', ''),
            'EmployerSuperGuaranteeAmount': _format_amount(sgc),
            'EmployerAdditionalAmount': _format_amount(additional),
            'MemberVoluntaryContributionAmount': _format_amount(voluntary),
            'TotalContributionAmount': _format_amount(total),
            'PayrollPaymentFrequency': payroll_frequency,
        })

    return buf.getvalue()
