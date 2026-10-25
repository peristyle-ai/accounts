"""PayEvent — one row per payslip issued.

Headline amounts (gross/payg/net/super_*) are the source of truth. Lines
(pay_event_lines) are a denormalised view for the PDF / UI.

Status state machine (see payroll_expansion.md §4):
    draft → finalized → paid
    draft → cancelled
"""

import uuid
from decimal import Decimal

from app.db import db, get_utc_now
from config import accounts_config


# Allowed status values — referenced by routes for validation.
PAY_EVENT_STATUSES = ('draft', 'finalized', 'paid', 'cancelled')


class PayEvent(db.Model):
    __tablename__ = 'pay_events'

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = db.Column(db.BigInteger, db.ForeignKey(accounts_config.users_fk, ondelete='CASCADE'), nullable=False, index=True)
    employee_id = db.Column(db.String(36), db.ForeignKey('employees.id', ondelete='RESTRICT'), nullable=False, index=True)

    payment_date = db.Column(db.Date, nullable=False)
    pay_period_start = db.Column(db.Date, nullable=False)
    pay_period_end = db.Column(db.Date, nullable=False)
    pay_frequency = db.Column(db.String(20), nullable=False)
    position_snapshot = db.Column(db.String(255), nullable=True)

    gross_amount = db.Column(db.Numeric(12, 2), nullable=False)
    payg_tax_amount = db.Column(db.Numeric(12, 2), nullable=False)
    net_amount = db.Column(db.Numeric(12, 2), nullable=False)

    super_ote_amount = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    super_payable_amount = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    super_paid_amount = db.Column(db.Numeric(12, 2), nullable=True)
    super_paid_date = db.Column(db.Date, nullable=True)

    bank_reference = db.Column(db.String(50), nullable=True)
    # Bank-side confirmation reference for the wage transfer (e.g. ING Osko
    # Confirmation ID). Distinct from bank_reference, which is the reference
    # that appears on the payee's statement.
    payment_reference = db.Column(db.String(100), nullable=True)
    status = db.Column(db.String(20), nullable=False, default='draft', index=True)
    notes = db.Column(db.Text, nullable=True)
    payslip_pdf_path = db.Column(db.String(500), nullable=True)

    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=get_utc_now)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=get_utc_now, onupdate=get_utc_now)

    # Relationships — backrefs on the child models reference this class.
    lines = db.relationship(
        'PayEventLine',
        backref='pay_event',
        cascade='all, delete-orphan',
        order_by='PayEventLine.sort_order',
        lazy='select',
    )
    deliveries = db.relationship(
        'PayslipDelivery',
        backref='pay_event',
        cascade='all, delete-orphan',
        order_by='PayslipDelivery.sent_at',
        lazy='select',
    )

    @staticmethod
    def compute_super_payable(super_ote_amount, super_rate_pct):
        """Return Decimal super_payable = super_ote × rate/100, quantised 0.01.

        Used by both the API (to fill `super_payable_amount` on create when
        omitted) and the PDF generator (to display).
        """
        if super_ote_amount is None or super_rate_pct is None:
            return Decimal('0.00')
        ote = Decimal(str(super_ote_amount))
        rate = Decimal(str(super_rate_pct))
        return (ote * rate / Decimal('100')).quantize(Decimal('0.01'))

    def to_dict(self):
        return {
            'id': self.id,
            'user_id': self.user_id,
            'employee_id': self.employee_id,
            'payment_date': self.payment_date.isoformat() if self.payment_date else None,
            'pay_period_start': self.pay_period_start.isoformat() if self.pay_period_start else None,
            'pay_period_end': self.pay_period_end.isoformat() if self.pay_period_end else None,
            'pay_frequency': self.pay_frequency,
            'position_snapshot': self.position_snapshot,
            'gross_amount': str(self.gross_amount),
            'payg_tax_amount': str(self.payg_tax_amount),
            'net_amount': str(self.net_amount),
            'super_ote_amount': str(self.super_ote_amount),
            'super_payable_amount': str(self.super_payable_amount),
            'super_paid_amount': str(self.super_paid_amount) if self.super_paid_amount is not None else None,
            'super_paid_date': self.super_paid_date.isoformat() if self.super_paid_date else None,
            'bank_reference': self.bank_reference,
            'payment_reference': self.payment_reference,
            'status': self.status,
            'notes': self.notes,
            'payslip_pdf_path': self.payslip_pdf_path,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }
