"""SuperPayment — one row per actual fund remittance.

Covers one or more pay_events (the `pay_event_ids` JSON array).
"""

import uuid
from sqlalchemy.dialects.postgresql import JSONB

from app.db import db, get_utc_now
from config import accounts_config


# Allowed statuses.
SUPER_PAYMENT_STATUSES = ('pending', 'paid', 'reconciled')


class SuperPayment(db.Model):
    __tablename__ = 'super_payments'

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = db.Column(db.BigInteger, db.ForeignKey(accounts_config.users_fk, ondelete='CASCADE'), nullable=False, index=True)
    employee_id = db.Column(db.String(36), db.ForeignKey('employees.id', ondelete='RESTRICT'), nullable=False, index=True)

    remittance_date = db.Column(db.Date, nullable=False)
    amount = db.Column(db.Numeric(12, 2), nullable=False)
    pay_event_ids = db.Column(JSONB, nullable=False, default=list)

    fund_name_snapshot = db.Column(db.String(255), nullable=True)
    fund_member_snapshot = db.Column(db.String(50), nullable=True)
    payment_reference = db.Column(db.String(100), nullable=True)
    status = db.Column(db.String(20), nullable=False, default='pending')
    notes = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=get_utc_now)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=get_utc_now, onupdate=get_utc_now)

    def to_dict(self):
        return {
            'id': self.id,
            'user_id': self.user_id,
            'employee_id': self.employee_id,
            'remittance_date': self.remittance_date.isoformat() if self.remittance_date else None,
            'amount': str(self.amount),
            'pay_event_ids': self.pay_event_ids or [],
            'fund_name_snapshot': self.fund_name_snapshot,
            'fund_member_snapshot': self.fund_member_snapshot,
            'payment_reference': self.payment_reference,
            'status': self.status,
            'notes': self.notes,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }
