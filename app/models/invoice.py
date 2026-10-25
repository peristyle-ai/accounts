import uuid
from decimal import Decimal

from app.db import db, get_utc_now
from config import accounts_config


class Invoice(db.Model):
    __tablename__ = 'invoices'

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = db.Column(db.BigInteger, db.ForeignKey(accounts_config.users_fk, ondelete='CASCADE'), nullable=False, index=True)
    account_category_id = db.Column(db.String(36), db.ForeignKey('account_categories.id', ondelete='SET NULL'), nullable=True)
    customer_id = db.Column(db.String(36), db.ForeignKey('customers.id', ondelete='RESTRICT'), nullable=False, index=True)
    client_name = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text, nullable=True)
    ex_gst_amount = db.Column(db.Numeric(12, 2), nullable=False)
    gst_amount = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    gst_type = db.Column(db.Numeric(3, 1), nullable=False, default=0)
    total_amount = db.Column(db.Numeric(12, 2), nullable=False)
    invoice_date = db.Column(db.Date, nullable=False)
    due_date = db.Column(db.Date, nullable=True)
    attachment_path = db.Column(db.String(500), nullable=True)
    status = db.Column(db.String(20), nullable=False, default='draft', index=True)
    payment_date = db.Column(db.Date, nullable=True)
    amount_paid = db.Column(db.Numeric(12, 2), nullable=True)
    sent_at = db.Column(db.DateTime(timezone=True), nullable=True)
    confirmed_received_at = db.Column(db.DateTime(timezone=True), nullable=True)
    paid_at = db.Column(db.DateTime(timezone=True), nullable=True)
    # 2026-10-07: capture bank-side provenance for payments (NAB ref, OSKO, etc).
    # Same shape as super_payment.payment_reference — keeps reconciliation tidy.
    payment_reference = db.Column(db.String(100), nullable=True)
    payment_method    = db.Column(db.String(50),  nullable=True)  # e.g. 'nab_osko', 'transfer_credit'
    notes             = db.Column(db.Text,         nullable=True)  # free-form payment context
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=get_utc_now)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=get_utc_now, onupdate=get_utc_now)

    # Reverse relation: every reminder/chase logged against this invoice.
    # Added 2026-09-08 alongside the invoice_reminders table.
    reminders = db.relationship(
        'InvoiceReminder',
        backref='invoice',
        lazy='dynamic',
        cascade='all, delete-orphan',
    )

    @staticmethod
    def calculate_gst(ex_gst_amount, gst_type):
        ex_gst = Decimal(str(ex_gst_amount))
        gst = Decimal(str(gst_type))
        return (ex_gst * gst).quantize(Decimal('0.01'))

    @staticmethod
    def calculate_total(ex_gst_amount, gst_amount):
        return Decimal(str(ex_gst_amount)) + Decimal(str(gst_amount))

    def to_dict(self):
        return {
            'id': self.id,
            'user_id': self.user_id,
            'account_category_id': self.account_category_id,
            'account_category_name': self.account_category.name if self.account_category else None,
            'customer_id': self.customer_id,
            'customer_name': self.customer.name if self.customer else None,
            'client_name': self.client_name,
            'description': self.description,
            'ex_gst_amount': str(self.ex_gst_amount),
            'gst_amount': str(self.gst_amount),
            'gst_type': str(self.gst_type),
            'total_amount': str(self.total_amount),
            'invoice_date': self.invoice_date.isoformat() if self.invoice_date else None,
            'due_date': self.due_date.isoformat() if self.due_date else None,
            'attachment_path': self.attachment_path,
            'status': self.status,
            'payment_date': self.payment_date.isoformat() if self.payment_date else None,
            'amount_paid': str(self.amount_paid) if self.amount_paid is not None else None,
            'sent_at': self.sent_at.isoformat() if self.sent_at else None,
            'confirmed_received_at': self.confirmed_received_at.isoformat() if self.confirmed_received_at else None,
            'paid_at': self.paid_at.isoformat() if self.paid_at else None,
            'payment_reference': self.payment_reference,
            'payment_method':    self.payment_method,
            'notes':             self.notes,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
            'reminder_count': self.reminders.count() if hasattr(self, 'reminders') else 0,
        }
