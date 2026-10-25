"""
BAS (Business Activity Statement) lodgement model.

Tracks Australian Taxation Office (ATO) BAS lodgements per quarter
for the business. The actual financial data (income, expenses,
GST collected/paid) is aggregated from invoices/expenses at query time
via /api/reports/quarterly-bas; this model just records the FACT of
lodgement with the ATO receipt ID, timestamp, and any manual
adjustments/credits that don't appear in the aggregated view.

Why a separate model: the BAS report endpoint returns the COMPUTED
net GST (1A - 1B). The actual amount ATO settles may differ due to
prior-period credits, deferred BAS, label adjustments, instalment
interest, etc. We need to record the FINAL settled amount separately
for reconciliation.
"""
import uuid
from decimal import Decimal

from app.db import db, get_utc_now
from config import accounts_config


class BasLodgement(db.Model):
    __tablename__ = 'bas_lodgements'

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = db.Column(db.BigInteger, db.ForeignKey(accounts_config.users_fk, ondelete='CASCADE'), nullable=False, index=True)

    # Period
    financial_year = db.Column(db.String(9), nullable=False)  # e.g. 'FY2025/26'
    quarter = db.Column(db.Integer, nullable=False)  # 1..4 (Australian FY: Q1=Jul-Sep, Q4=Apr-Jun)
    period_start = db.Column(db.Date, nullable=False)  # first day of quarter
    period_end = db.Column(db.Date, nullable=False)  # last day of quarter

    # ATO lodgement facts
    ato_receipt_id = db.Column(db.String(64), nullable=True)  # e.g. '9021291175'
    ato_account_name = db.Column(db.String(255), nullable=True)  # e.g. 'EXAMPLE, SAMPLE NAME'
    lodged_at = db.Column(db.DateTime(timezone=True), nullable=False)
    lodgement_method = db.Column(db.String(20), nullable=False, default='online')  # 'online' | 'paper' | 'agent'

    # Snapshot of computed BAS figures at lodgement time
    # (matches /api/reports/quarterly-bas output for the same period)
    gst_collected = db.Column(db.Numeric(12, 2), nullable=True)  # 1A
    gst_paid = db.Column(db.Numeric(12, 2), nullable=True)       # 1B
    computed_net_gst = db.Column(db.Numeric(12, 2), nullable=True)  # 1A - 1B before ATO adjustments

    # Final settled amount — this is what the operator actually paid/was refunded.
    # positive = credit from ATO (ATO owes the operator)
    # negative = amount owed to ATO (the operator paid)
    # zero = no movement
    final_amount = db.Column(db.Numeric(12, 2), nullable=False)
    final_amount_type = db.Column(db.String(10), nullable=False)  # 'credit' | 'owe' | 'zero'

    # Prior-period credits/debits carried into this lodgement (often the
    # reason final_amount != computed_net_gst)
    prior_credit_carried = db.Column(db.Numeric(12, 2), nullable=True, default=0)
    other_adjustments = db.Column(db.Numeric(12, 2), nullable=True, default=0)
    adjustments_note = db.Column(db.Text, nullable=True)

    # Audit
    notes = db.Column(db.Text, nullable=True)
    screenshot_path = db.Column(db.String(500), nullable=True)  # optional ATO screenshot
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=get_utc_now)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=get_utc_now, onupdate=get_utc_now)

    def to_dict(self):
        return {
            'id': self.id,
            'user_id': self.user_id,
            'financial_year': self.financial_year,
            'quarter': self.quarter,
            'period_start': self.period_start.isoformat() if self.period_start else None,
            'period_end': self.period_end.isoformat() if self.period_end else None,
            'ato_receipt_id': self.ato_receipt_id,
            'ato_account_name': self.ato_account_name,
            'lodged_at': self.lodged_at.isoformat() if self.lodged_at else None,
            'lodgement_method': self.lodgement_method,
            'gst_collected': str(self.gst_collected) if self.gst_collected is not None else None,
            'gst_paid': str(self.gst_paid) if self.gst_paid is not None else None,
            'computed_net_gst': str(self.computed_net_gst) if self.computed_net_gst is not None else None,
            'final_amount': str(self.final_amount),
            'final_amount_type': self.final_amount_type,
            'prior_credit_carried': str(self.prior_credit_carried) if self.prior_credit_carried is not None else None,
            'other_adjustments': str(self.other_adjustments) if self.other_adjustments is not None else None,
            'adjustments_note': self.adjustments_note,
            'notes': self.notes,
            'screenshot_path': self.screenshot_path,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }