import uuid
from sqlalchemy.dialects.postgresql import JSONB
from app.db import db, get_utc_now


class OcrQueue(db.Model):
    __tablename__ = 'ocr_queue'

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    expense_id = db.Column(db.String(36), db.ForeignKey('expenses.id', ondelete='CASCADE'), nullable=False, index=True)
    image_path = db.Column(db.String(500), nullable=False)
    status = db.Column(db.String(20), nullable=False, default='pending', index=True)
    extracted_data = db.Column(JSONB, nullable=True)
    error_message = db.Column(db.Text, nullable=True)
    attempts = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=get_utc_now)
    processed_at = db.Column(db.DateTime(timezone=True), nullable=True)

    def to_dict(self):
        return {
            'id': self.id,
            'expense_id': self.expense_id,
            'image_path': self.image_path,
            'status': self.status,
            'extracted_data': self.extracted_data,
            'error_message': self.error_message,
            'attempts': self.attempts,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'processed_at': self.processed_at.isoformat() if self.processed_at else None
        }
