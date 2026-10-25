import uuid
from decimal import Decimal

from app.db import db, get_utc_now
from config import accounts_config


class Expense(db.Model):
    __tablename__ = 'expenses'

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = db.Column(db.BigInteger, db.ForeignKey(accounts_config.users_fk, ondelete='CASCADE'), nullable=False, index=True)
    account_category_id = db.Column(db.String(36), db.ForeignKey('account_categories.id', ondelete='SET NULL'), nullable=True)
    source = db.Column(db.String(20), nullable=False, default='browser', index=True)
    vendor_name = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text, nullable=True)
    currency = db.Column(db.String(3), nullable=False, default='AUD')
    original_currency_amount = db.Column(db.Numeric(12, 2), nullable=True)
    exchange_rate = db.Column(db.Numeric(10, 6), nullable=True)
    ex_gst_amount = db.Column(db.Numeric(12, 2), nullable=False)
    gst_amount = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    gst_type = db.Column(db.Numeric(3, 1), nullable=False, default=0)
    total_amount = db.Column(db.Numeric(12, 2), nullable=False)
    expense_date = db.Column(db.Date, nullable=False)
    attachment_path = db.Column(db.String(500), nullable=True)
    requires_review = db.Column(db.Boolean, nullable=False, default=False, index=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=get_utc_now)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=get_utc_now, onupdate=get_utc_now)

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
            'source': self.source,
            'vendor_name': self.vendor_name,
            'description': self.description,
            'currency': self.currency,
            'original_currency_amount': str(self.original_currency_amount) if self.original_currency_amount else None,
            'exchange_rate': str(self.exchange_rate) if self.exchange_rate else None,
            'ex_gst_amount': str(self.ex_gst_amount),
            'gst_amount': str(self.gst_amount),
            'gst_type': str(self.gst_type),
            'total_amount': str(self.total_amount),
            'expense_date': self.expense_date.isoformat() if self.expense_date else None,
            'attachment_path': self.attachment_path,
            'requires_review': self.requires_review,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None
        }
