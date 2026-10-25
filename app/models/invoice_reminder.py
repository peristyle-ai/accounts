"""
Invoice reminder / statement-of-account log.

Records every outbound (or inbound) communication tied to a specific invoice
or set of invoices. Used to:

- Track aged-receivable chasing: how many statements/chases have been sent
  for each invoice, when, by whom, and what channel.
- Reconstruct escalation history ("INV 7FADC40D: first chase 1d overdue,
  second chase 8d overdue, paid on day 12").
- Group reminders that belong to the same email event (e.g. a single
  statement of account PDF can cover many invoices — all rows share the
  same `email_id` so they can be aggregated later).

One row per invoice per reminder event. A statement of account that
covers 3 outstanding invoices produces 3 rows, all sharing the same
`email_id` / `sent_at` / `subject`.

Why this is a separate model (not a column on Invoice):
- Reminders are append-only events with rich metadata (channel, sender,
  recipient, body excerpt, response time). A flat column can't capture
  multi-event history.
- A denormalised `reminder_count` is exposed via the API for fast aging
  views, but the source of truth is the count of rows in this table.

Added 2026-09-08 after the operator asked to track a customer's statement-of-account
chases. Schema mirrors bas_lodgement.py style (FKs + audit timestamps).
"""
import uuid

from app.db import db, get_utc_now
from config import accounts_config


# Reminder kinds. Keep this short and meaningful for reporting.
INVOICE_REMINDER_TYPES = (
    'statement',          # Statement of account (covers all open invoices)
    'chase',              # Polite nudge for a specific overdue invoice
    'dunning',            # Formal demand / final notice
    'payment_reminder',   # Pre-due-date "just a heads-up" reminder
    'thank_you',          # Payment received acknowledgement
    'phone_call',         # Logged phone conversation (no email)
    'in_person',          # Logged face-to-face follow-up
    'other',
)

INVOICE_REMINDER_CHANNELS = (
    'email',
    'phone',
    'sms',
    'in_person',
    'mail',
    'other',
)

INVOICE_REMINDER_DIRECTIONS = (
    'outbound',   # we sent it
    'inbound',    # customer sent it (e.g. reply, dispute, payment query)
)

INVOICE_REMINDER_SENDERS = (
    'human',      # a human sent it (the operator, the customer, etc.) — sender identity is in `sent_by`
    'agent',      # the operating agent (automated)
    'cron',       # Scheduled job (rare — most are 'agent')
    'system',     # System event (e.g. Xero payment auto-receipt)
    'unknown',
)


class InvoiceReminder(db.Model):
    __tablename__ = 'invoice_reminders'

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))

    # Scoping (multi-tenant like the rest of the app)
    user_id = db.Column(db.BigInteger, db.ForeignKey(accounts_config.users_fk, ondelete='CASCADE'), nullable=False, index=True)
    customer_id = db.Column(db.String(36), db.ForeignKey('customers.id', ondelete='CASCADE'), nullable=False, index=True)
    invoice_id = db.Column(db.String(36), db.ForeignKey('invoices.id', ondelete='CASCADE'), nullable=False, index=True)

    # What kind of communication
    reminder_type = db.Column(db.String(20), nullable=False)
    channel       = db.Column(db.String(20), nullable=False, default='email')
    direction     = db.Column(db.String(10), nullable=False, default='outbound')

    # When
    sent_at = db.Column(db.DateTime(timezone=True), nullable=False, default=get_utc_now, index=True)

    # Who sent it
    sent_by      = db.Column(db.String(255), nullable=True)  # email or name of sender
    sent_by_kind = db.Column(db.String(20),  nullable=False, default='human')

    # Provenance — link back to the source email / artefact
    email_id          = db.Column(db.String(64), nullable=True, index=True)  # mail server message id
    pdf_attachment_path = db.Column(db.String(500), nullable=True)

    # Free-form context for human readers
    recipients  = db.Column(db.Text,    nullable=True)   # "To: the customer; Cc: a colleague, the operating agent"
    subject     = db.Column(db.String(500), nullable=True)
    body_excerpt = db.Column(db.Text,    nullable=True)   # first ~500 chars of the body
    notes       = db.Column(db.Text,    nullable=True)

    # Outcome tracking (filled in later if/when the customer responds / pays)
    response_received_at = db.Column(db.DateTime(timezone=True), nullable=True)
    payment_received_at  = db.Column(db.DateTime(timezone=True), nullable=True)

    # Snapshot of overdue state at the moment the reminder was sent.
    # Lets us reconstruct escalation patterns ("first reminder at 1d overdue,
    # second at 7d, third at 14d") without re-deriving from invoice dates.
    days_overdue_at_send = db.Column(db.Integer, nullable=True)

    # Audit
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=get_utc_now)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=get_utc_now, onupdate=get_utc_now)

    def to_dict(self):
        return {
            'id':                    self.id,
            'user_id':               self.user_id,
            'customer_id':           self.customer_id,
            'invoice_id':            self.invoice_id,
            'invoice_number':        self.invoice_id[:8].upper() if self.invoice_id else None,
            'reminder_type':         self.reminder_type,
            'channel':               self.channel,
            'direction':             self.direction,
            'sent_at':               self.sent_at.isoformat() if self.sent_at else None,
            'sent_by':               self.sent_by,
            'sent_by_kind':          self.sent_by_kind,
            'email_id':              self.email_id,
            'pdf_attachment_path':   self.pdf_attachment_path,
            'recipients':            self.recipients,
            'subject':               self.subject,
            'body_excerpt':          self.body_excerpt,
            'notes':                 self.notes,
            'response_received_at':  self.response_received_at.isoformat() if self.response_received_at else None,
            'payment_received_at':   self.payment_received_at.isoformat() if self.payment_received_at else None,
            'days_overdue_at_send':  self.days_overdue_at_send,
            'created_at':            self.created_at.isoformat() if self.created_at else None,
            'updated_at':            self.updated_at.isoformat() if self.updated_at else None,
        }
