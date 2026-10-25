"""PayEventLine — itemised breakdown of a pay_event.

For Alice Smith today this is a 3-line breakdown (Gross / PAYG / Net). For
future employees: allowances, leave, overtime, deductions.

Headline amounts on pay_events remain the source of truth.
"""

import uuid

from app.db import db, get_utc_now


# Allowed line types — referenced by routes for validation.
PAY_EVENT_LINE_TYPES = ('earning', 'tax', 'deduction', 'allowance', 'employer_contribution')


class PayEventLine(db.Model):
    __tablename__ = 'pay_event_lines'

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    pay_event_id = db.Column(db.String(36), db.ForeignKey('pay_events.id', ondelete='CASCADE'), nullable=False, index=True)
    line_type = db.Column(db.String(30), nullable=False)
    description = db.Column(db.String(255), nullable=True)
    quantity = db.Column(db.Numeric(10, 2), nullable=True)
    rate = db.Column(db.Numeric(10, 2), nullable=True)
    amount = db.Column(db.Numeric(12, 2), nullable=False)
    is_taxable = db.Column(db.Boolean, nullable=False, default=True)
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=get_utc_now)

    def to_dict(self):
        return {
            'id': self.id,
            'pay_event_id': self.pay_event_id,
            'line_type': self.line_type,
            'description': self.description,
            'quantity': str(self.quantity) if self.quantity is not None else None,
            'rate': str(self.rate) if self.rate is not None else None,
            'amount': str(self.amount),
            'is_taxable': bool(self.is_taxable),
            'sort_order': self.sort_order,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }
