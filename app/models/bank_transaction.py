"""
Bank transaction model.

Records payments received into the business bank account with full provenance
from the bank's transaction record: transaction ID, reference string, payer
name/account, amount, settlement date. Optional FK back to invoice so a
single payment can be linked to the invoice it pays (or left null for
non-invoice receipts like interest, refunds, owner contributions).

Why a separate model (added 2026-09-07): the Invoice model had no place to
store bank-side provenance — the transaction ID, the reference string the
payer used, the method (Osko/BPay/direct credit), etc. Without that we
can't reconcile the bank statement against invoices, and the BAS report
can't show real settlement dates. Snapshotting the provenance at receipt
time also gives us an audit trail if the bank statement later changes.

Schema:
  bank_transactions
    id                  UUID PK
    user_id             FK -> public.users.id (indexed)
    invoice_id          FK -> invoices.id (nullable, indexed) — the invoice this pays
    transaction_id      bank-side transaction ID, e.g. 'CTBAAUSNXXXN...'
    reference           payer-supplied reference, e.g. '5C1A4C6F - SOFTWARE'
    method              'osko' | 'bpay' | 'direct_credit' | 'cheque' | 'cash' | 'other'
    payer_name          who sent the money
    payer_account       payer-side account if known
    amount              amount received (always positive)
    currency            ISO-4217 3-char, defaults to 'AUD'
    transaction_date    date the bank shows it as processed (what goes on the BAS)
    settled_at          precise timestamp if known
    notes               free-text notes
    raw_source          where this row came from: 'manual' | 'bank_feed_csv' | 'screenshot_ocr'
    created_at          when we recorded it
"""
import uuid
from decimal import Decimal

from app.db import db, get_utc_now
from config import accounts_config


# Allowed method values - keep narrow so analytics can group cleanly.
BANK_TXN_METHODS = ('osko', 'bpay', 'direct_credit', 'cheque', 'cash', 'other')

# Allowed raw_source values
BANK_TXN_SOURCES = ('manual', 'bank_feed_csv', 'screenshot_ocr')


class BankTransaction(db.Model):
    __tablename__ = 'bank_transactions'

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = db.Column(db.BigInteger, db.ForeignKey(accounts_config.users_fk, ondelete='CASCADE'), nullable=False, index=True)
    invoice_id = db.Column(db.String(36), db.ForeignKey('invoices.id', ondelete='SET NULL'), nullable=True, index=True)

    # Bank-side provenance
    transaction_id = db.Column(db.String(64), nullable=True)   # 'CTBAAUSNXXXN20260831050082279008000'
    reference = db.Column(db.String(255), nullable=True)      # '5C1A4C6F - SOFTWARE'
    method = db.Column(db.String(32), nullable=True)           # see BANK_TXN_METHODS

    # Payer info
    payer_name = db.Column(db.String(255), nullable=True)
    payer_account = db.Column(db.String(64), nullable=True)

    # Money
    amount = db.Column(db.Numeric(12, 2), nullable=False)
    currency = db.Column(db.String(3), nullable=False, default='AUD')

    # Timing
    transaction_date = db.Column(db.Date, nullable=False, index=True)
    settled_at = db.Column(db.DateTime(timezone=True), nullable=True)

    # Provenance of the record itself
    notes = db.Column(db.Text, nullable=True)
    raw_source = db.Column(db.String(32), nullable=False, default='manual')
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=get_utc_now)

    def to_dict(self) -> dict:
        return {
            'id': self.id,
            'user_id': self.user_id,
            'invoice_id': self.invoice_id,
            'transaction_id': self.transaction_id,
            'reference': self.reference,
            'method': self.method,
            'payer_name': self.payer_name,
            'payer_account': self.payer_account,
            'amount': str(self.amount) if self.amount is not None else None,
            'currency': self.currency,
            'transaction_date': self.transaction_date.isoformat() if self.transaction_date else None,
            'settled_at': self.settled_at.isoformat() if self.settled_at else None,
            'notes': self.notes,
            'raw_source': self.raw_source,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }
