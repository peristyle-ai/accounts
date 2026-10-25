import uuid

from app.db import db, get_utc_now


class AccountCategory(db.Model):
    __tablename__ = 'account_categories'

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=get_utc_now)

    expenses = db.relationship('Expense', backref='account_category', lazy='dynamic')
    invoices = db.relationship('Invoice', backref='account_category', lazy='dynamic')

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'description': self.description,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }
