"""
Pending payment reconciliation log.

Records every remittance advice (e.g. Xero "Payment has been made" email)
received for a customer. Each row sits in `pending` status until a human
confirms the money actually landed in the bank account.

Why this exists:
- A remittance advice is the PAYER'S claim ("we paid you $X for invoice Y").
  It is NOT proof the money landed. Until the bank statement shows the
  funds, the invoice must stay unpaid.
- Without this gate, we were marking invoices as paid based on the Xero
  email alone, which led to wrongly marking invoices as paid that the
  payer had not actually settled (Sept 2026 incident — see MEMORY).
- The workflow:
    1. Script reads Xero emails from `payroll@example.com` Inbox.
    2. Extracts the reference / amount / date from the PDF (pdftotext).
    3. Looks up the matching invoice by `invoice_ref_token` (first 8 chars
       of the UUID, uppercased).
    4. Creates a row here (status=pending) and moves the email to a
       "Pending Reconciliation" folder so it doesn't pollute the Inbox.
    5. Heartbeat / cron surfaces the pending list to the operator.
    6. the operator checks the bank (screenshot or statement) and either
       `confirm` (status=confirmed, invoice auto-marked paid, email
       moved to Processed) or `reject` (status=rejected, email moved
       to Processed with notes).

Added 2026-09-08 after an invoice was reconciled from a PDF statement
that had already been sent.
"""
import uuid

from app.db import db, get_utc_now
from config import accounts_config


PENDING_RECONCILIATION_STATUSES = (
    'pending',     # remittance received, awaiting bank confirmation
    'confirmed',   # bank verified — invoice marked paid
    'rejected',    # bank shows no match OR reference doesn't match any invoice
    'stale',       # too old (e.g. >30 days pending); surfaced for review
)


class PendingPaymentReconciliation(db.Model):
    __tablename__ = 'pending_payment_reconciliations'

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))

    # Scoping (multi-tenant like the rest of the app)
    user_id = db.Column(db.BigInteger, db.ForeignKey(accounts_config.users_fk, ondelete='CASCADE'), nullable=False, index=True)
    customer_id = db.Column(db.String(36), db.ForeignKey('customers.id', ondelete='CASCADE'), nullable=False, index=True)
    invoice_id = db.Column(db.String(36), db.ForeignKey('invoices.id', ondelete='SET NULL'), nullable=True, index=True)

    # Source — link back to the email / artefact
    source_email_id   = db.Column(db.String(64),  nullable=True, index=True)
    source_sender     = db.Column(db.String(255), nullable=True)
    source_subject    = db.Column(db.String(500), nullable=True)
    pdf_attachment_path = db.Column(db.String(500), nullable=True)

    # Fields extracted from the remittance advice PDF
    payer_name      = db.Column(db.String(255), nullable=True)
    payer_abn       = db.Column(db.String(20),  nullable=True)
    payment_date    = db.Column(db.Date, nullable=True)
    sent_date       = db.Column(db.Date, nullable=True)   # when the remittance was generated
    reference_text  = db.Column(db.String(500), nullable=True)  # "5C1A4C6F - SOFTWARE"
    invoice_ref_token = db.Column(db.String(8), nullable=True, index=True)  # "5C1A4C6F"
    amount          = db.Column(db.Numeric(12, 2), nullable=True)
    amount_currency = db.Column(db.String(3), nullable=False, default='AUD')

    # Status workflow
    status = db.Column(db.String(20), nullable=False, default='pending', index=True)

    # Reconciliation (filled when the operator confirms via /confirm endpoint)
    reconciled_at = db.Column(db.DateTime(timezone=True), nullable=True)
    reconciled_by = db.Column(db.String(255), nullable=True)  # email or 'agent'
    bank_reference = db.Column(db.String(255), nullable=True)
    bank_screenshot_path = db.Column(db.String(500), nullable=True)
    notes = db.Column(db.Text, nullable=True)

    # Audit
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=get_utc_now)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=get_utc_now, onupdate=get_utc_now)

    def to_dict(self):
        return {
            'id':                  self.id,
            'user_id':             self.user_id,
            'customer_id':         self.customer_id,
            'invoice_id':          self.invoice_id,
            'invoice_number':      self.invoice_id[:8].upper() if self.invoice_id else None,
            'source_email_id':     self.source_email_id,
            'source_sender':       self.source_sender,
            'source_subject':      self.source_subject,
            'pdf_attachment_path': self.pdf_attachment_path,
            'payer_name':          self.payer_name,
            'payer_abn':           self.payer_abn,
            'payment_date':        self.payment_date.isoformat() if self.payment_date else None,
            'sent_date':           self.sent_date.isoformat() if self.sent_date else None,
            'reference_text':      self.reference_text,
            'invoice_ref_token':   self.invoice_ref_token,
            'amount':              str(self.amount) if self.amount is not None else None,
            'amount_currency':     self.amount_currency,
            'status':              self.status,
            'reconciled_at':       self.reconciled_at.isoformat() if self.reconciled_at else None,
            'reconciled_by':       self.reconciled_by,
            'bank_reference':      self.bank_reference,
            'bank_screenshot_path': self.bank_screenshot_path,
            'notes':               self.notes,
            'created_at':          self.created_at.isoformat() if self.created_at else None,
            'updated_at':          self.updated_at.isoformat() if self.updated_at else None,
        }
