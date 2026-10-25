"""PayslipDelivery — audit trail of every payslip email send.

Multiple rows per pay_event (work inbox + personal inbox). `delivery_id` is
the mail server's email_id once the message has been queued.
"""

import uuid
from sqlalchemy.dialects.postgresql import JSONB

from app.db import db, get_utc_now


# Allowed delivery statuses.
PAYSLIP_DELIVERY_STATUSES = ('queued', 'sent', 'delivered', 'failed', 'bounced')
PAYSLIP_RECIPIENT_KINDS = ('work', 'personal', 'other')


class PayslipDelivery(db.Model):
    __tablename__ = 'payslip_deliveries'

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    pay_event_id = db.Column(db.String(36), db.ForeignKey('pay_events.id', ondelete='CASCADE'), nullable=False, index=True)
    recipient_email = db.Column(db.String(255), nullable=False)
    recipient_kind = db.Column(db.String(20), nullable=False, default='work')
    sent_from = db.Column(db.String(255), nullable=False)
    sent_at = db.Column(db.DateTime(timezone=True), nullable=False, default=get_utc_now)
    subject = db.Column(db.String(500), nullable=True)
    attachment_count = db.Column(db.Integer, nullable=False, default=0)
    attachment_paths = db.Column(JSONB, nullable=False, default=list)
    delivery_status = db.Column(db.String(20), nullable=False, default='queued')
    delivery_id = db.Column(db.String(64), nullable=True)
    error_message = db.Column(db.Text, nullable=True)
    mail_api_response = db.Column(JSONB, nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=get_utc_now)

    def to_dict(self):
        return {
            'id': self.id,
            'pay_event_id': self.pay_event_id,
            'recipient_email': self.recipient_email,
            'recipient_kind': self.recipient_kind,
            'sent_from': self.sent_from,
            'sent_at': self.sent_at.isoformat() if self.sent_at else None,
            'subject': self.subject,
            'attachment_count': self.attachment_count,
            'attachment_paths': self.attachment_paths or [],
            'delivery_status': self.delivery_status,
            'delivery_id': self.delivery_id,
            'error_message': self.error_message,
            'mail_api_response': self.mail_api_response,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }
