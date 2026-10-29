#!/usr/bin/env python3
"""
Generate an NAB-format ABA file for a super clearing-house payment.

Reads the destination (BSB, account number, account name) from the
``payee_directory`` table — set up 2026-09-21 so Wrkr Pay and any
future org-scoped bank destinations live in the schema rather than
being pasted ad-hoc each time.

Default use-case: ``super_clearing_house`` (Wrkr Pay).

Usage:
    # Default — uses today's date as process_date
    source venv/bin/activate && set -a && . ./.env && set +a && \
        python3 scripts/gen_super_aba.py

    # Override parameters
    PROCESS_DATE=2026-09-22 OUT_PATH=/tmp/x.aba \
        python3 scripts/gen_super_aba.py

    # AMOUNT and LODGEMENT_REF are REQUIRED (no defaults) -- see below.

    # Override the payer (PAYER_EMAIL is required - there is no default) or
    # destination label
    PAYER_EMAIL=you@yourcompany.com.au PAYEE_LABEL='ATO (BAS)' \
        python3 scripts/gen_super_aba.py

Output:
- Writes the ABA file to OUT_PATH (default: /tmp/peristyle-super-DEFAULT.aba)
- Prints each record to stdout for visual verification
- Does NOT mark any SuperPayment as paid — that's a separate workflow
"""
import os
import sys
from datetime import date
from decimal import Decimal

# --- bootstrap (same shape as run.py) ----------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))                    # accounts/

env_path = os.path.join(os.path.dirname(HERE), '.env')
if os.path.isfile(env_path):
    with open(env_path) as _f:
        for line in _f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, v = line.split('=', 1)
            os.environ.setdefault(k.strip(), v.strip())

# --- imports -----------------------------------------------------------
from app import create_app
from app.models.user import User
from app.models.payee_directory import Payee
from app.shared.aba import build_aba_file

# --- constants (overridable via env) -----------------------------------
# The payer email identifies the business and is written into the ABA file.
# There is no default: the old one was `payroll@example.com`, a reserved
# documentation domain -- a placeholder in a file destined for a bank.
PAYER_EMAIL = os.environ.get('PAYER_EMAIL')
PAYEE_LABEL = os.environ.get('PAYEE_LABEL', 'Wrkr Super (clearing house)')
PAYEE_USE_CASE = os.environ.get('PAYEE_USE_CASE', 'super_clearing_house')

# Default to today; override via PROCESS_DATE env var if needed (NAB
# typically needs +1 BD lead time for first-time ABA uploads).
PROCESS_DATE = (date.fromisoformat(os.environ['PROCESS_DATE'])
                if os.environ.get('PROCESS_DATE')
                else date.today())

# The ABA format takes only one detail record per file, so the amount
# and the lodgement reference are stated per run.
#
# AMOUNT and LODGEMENT_REF are REQUIRED -- there is no default.
#
# The old defaults were AMOUNT='228.00' and LODGEMENT_REF='JESS SUPER
# 140914'. The amount happened to be right for one employee's weekly
# contribution and the lodgement reference was a SEPTEMBER artefact:
# a run that forgot to pass them wrote a stale reference into a file
# destined for a bank. This is the same class as the old
# `payroll@example.com` sender default -- a placeholder, never a real
# address -- so both are now required.
#
# The source of truth is the `accounts.super_payments` row (its amount
# and its pay event's period end). The caller states the values it is
# remitting; the script refuses to guess them.
AMOUNT_RAW = os.environ.get('AMOUNT')
LODGEMENT_REF = (os.environ.get('LODGEMENT_REF') or '').strip()
OUT_PATH = os.environ.get('OUT_PATH', '/tmp/peristyle-super-DEFAULT.aba')

# --- run ---------------------------------------------------------------
# Validate BEFORE building the app: a refusal must not need a database
# connection, so the check is exercisable in a bare test environment.
if not AMOUNT_RAW:
    sys.exit('AMOUNT is required -- it is the amount being remitted. '
             'There is no default: read it from the super_payments row '
             '(or the pay event) and pass it explicitly.')
if not LODGEMENT_REF:
    sys.exit('LODGEMENT_REF is required -- it is the remittance reference '
             'that appears on the bank statement and lets the payment be '
             'matched back. There is no default: it must name THIS '
             'remittance.')
try:
    AMOUNT = Decimal(AMOUNT_RAW)
except Exception:
    sys.exit(f'AMOUNT is not a valid amount: {AMOUNT_RAW!r}')
if AMOUNT <= 0:
    sys.exit(f'AMOUNT must be greater than zero, got {AMOUNT}')

app = create_app()
with app.app_context():
    if not PAYER_EMAIL:
        sys.exit('PAYER_EMAIL is required -- it identifies the payer and is '
                 'written into the ABA file. Set PAYER_EMAIL in the accounts '
                 'repo .env.')
    payer = User.query.filter_by(email=PAYER_EMAIL).first()
    if payer is None:
        sys.exit(f'Payer {PAYER_EMAIL!r} not found')
    if not payer.bsb_plain or not payer.account_number_plain:
        sys.exit(f'Payer {PAYER_EMAIL!r} missing business bank details (bsb/account).')

    payee = (Payee.query
             .filter_by(user_id=payer.id, label=PAYEE_LABEL, is_active=True)
             .first())
    if payee is None:
        sys.exit(f'No active Payee labelled {PAYEE_LABEL!r} for {PAYER_EMAIL}')
    if payee.use_case and payee.use_case != PAYEE_USE_CASE:
        print(f'  [note] Payee use_case is {payee.use_case!r}, expected {PAYEE_USE_CASE!r} — proceeding anyway')

    payment = {
        'bsb':             payee.bsb_plain,
        'account_number':  payee.account_number_plain,
        'account_name':    payee.account_name,
        'amount':          AMOUNT,
        'lodgement_ref':   LODGEMENT_REF,
    }

    aba_bytes = build_aba_file(payer, [payment], processing_date=PROCESS_DATE)

# Split into the three records for visual confirmation
records = aba_bytes.decode('ascii').rstrip('\r\n').split('\r\n')
print(f'=== ABA file generated ({len(records)} records, {len(aba_bytes)} bytes) ===')
print(f'process_date : {PROCESS_DATE.isoformat()}')
print(f'payer        : {payer.account_name}  bsb={payer.bsb_plain}  acct={payer.account_number_plain}')
print(f'payee        : {payee.account_name}  (label={payee.label!r}, use_case={payee.use_case})')
print(f'              bsb={payee.bsb_plain}  acct={payee.account_number_plain}  ${AMOUNT}')
print(f'lodgement_ref: {LODGEMENT_REF!r}')
print()
for i, r in enumerate(records):
    print(f'record {i+1} ({len(r):>3} chars): {r}')
print()

# Per-record length check (Cemtex: every record = 120 chars + CRLF)
bad = [i for i, r in enumerate(records) if len(r) != 120]
if bad:
    sys.exit(f'FAIL: records at index {bad} are not exactly 120 chars')
print('OK: all records are exactly 120 chars.')

# Write
with open(OUT_PATH, 'wb') as f:
    f.write(aba_bytes)
print(f'Wrote: {OUT_PATH}  ({os.path.getsize(OUT_PATH)} bytes, mode 0600)')
os.chmod(OUT_PATH, 0o600)
