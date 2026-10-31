"""Quotation / estimate records.

Added 2026-10-30 on the `feat/quotes` branch.

A quote is a PROPOSAL: it has line items, an ex-GST/GST/total breakdown and a
validity date, but no money has moved. The money-moving counterpart is the
existing `invoices` table. The two are deliberately separate models rather
than one table with a status, because a quote and an invoice have different
lifecycles (a quote expires; an invoice is paid or overdue) and different
required fields (a quote needs `valid_until`; an invoice needs `due_date`).

Line items are stored STRUCTURALLY in a JSONB column, not encoded into a
text description the way invoices do it (see `Invoice.description` and
`_parse_line_items` in app/shared/pdf.py). Reason: a quote's line items are
the thing the customer negotiates, so they must round-trip exactly -- you
cannot rebuild a line item table from a string you have to parse. JSONB also
means adding a per-line field later does not need a migration.

A line may be RECURRING -- a monthly retainer rather than a one-off -- in
which case it carries `"recurring": true` and `"period": "monthly"`. The
stored line is still just a dict; the recurrence is expressed in the JSONB,
so no schema change was needed to support it. The quote's totals cover the
FIRST period (the one-off work plus the first month of the retainer), and
the PDF shows the one-off and recurring subtotals separately so the customer
never has to guess which is which.

Amounts are computed, never accepted raw: the API takes line items and
derives ex_gst / gst / total so the arithmetic cannot drift from the lines
the customer actually sees.
"""

import uuid
from decimal import Decimal

from sqlalchemy.dialects.postgresql import JSONB

from app.db import db, get_utc_now
from config import accounts_config


QUOTE_STATUSES = ('draft', 'sent', 'accepted', 'declined', 'expired')

# The recurrence periods a quote line may carry. A retainer is quoted for a
# period, and the period is part of what the customer is agreeing to.
RECURRING_PERIODS = ('monthly', 'quarterly', 'annually')

# Statuses from which a quote may still be edited in place.
EDITABLE_STATUSES = ('draft',)


class Quote(db.Model):
    __tablename__ = 'quotes'

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = db.Column(db.BigInteger, db.ForeignKey(accounts_config.users_fk, ondelete='CASCADE'), nullable=False, index=True)
    customer_id = db.Column(db.String(36), db.ForeignKey('customers.id', ondelete='RESTRICT'), nullable=False, index=True)

    # Human-facing reference, e.g. "Q-2026-0007". Unique per user.
    quote_number = db.Column(db.String(32), nullable=False, index=True)

    client_name = db.Column(db.String(255), nullable=False)

    # JSONB array of {"description": str, "amount": str} -- amounts as strings
    # so they never round-trip through a float.
    lines = db.Column(JSONB, nullable=False, default=list)

    ex_gst_amount = db.Column(db.Numeric(12, 2), nullable=False)
    gst_amount = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    gst_type = db.Column(db.Numeric(3, 1), nullable=False, default=0)

    # WHY the document carries the GST treatment it carries: one of the
    # BASIS_* codes in app/shared/tax.py. Stored with the quote so the
    # reason printed on the page is the reason that applied when the
    # quote was raised -- a document must not change its tax wording
    # because the customer record was updated afterwards.
    gst_basis = db.Column(db.String(32), nullable=True)

    total_amount = db.Column(db.Numeric(12, 2), nullable=False)

    quote_date = db.Column(db.Date, nullable=False)
    valid_until = db.Column(db.Date, nullable=True)

    status = db.Column(db.String(20), nullable=False, default='draft', index=True)
    notes = db.Column(db.Text, nullable=True)
    attachment_path = db.Column(db.String(500), nullable=True)

    sent_at = db.Column(db.DateTime(timezone=True), nullable=True)
    accepted_at = db.Column(db.DateTime(timezone=True), nullable=True)
    declined_at = db.Column(db.DateTime(timezone=True), nullable=True)

    # Set when the quote has been converted into an invoice, so the same
    # scope can never be invoiced twice by accident.
    converted_invoice_id = db.Column(db.String(36), nullable=True, index=True)

    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=get_utc_now)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=get_utc_now, onupdate=get_utc_now)

    customer = db.relationship('Customer', backref=db.backref('quotes', lazy='dynamic'))

    # -- amount arithmetic --------------------------------------------------

    @staticmethod
    def calculate_gst(ex_gst_amount, gst_type):
        ex_gst = Decimal(str(ex_gst_amount))
        gst = Decimal(str(gst_type))
        return (ex_gst * gst).quantize(Decimal('0.01'))

    @staticmethod
    def calculate_total(ex_gst_amount, gst_amount):
        return Decimal(str(ex_gst_amount)) + Decimal(str(gst_amount))

    @staticmethod
    def normalise_lines(lines):
        """Validate and normalise raw line items into the stored shape.

        Returns (normalised, error). `normalised` is a list of dicts with
        string amounts; `error` is None on success.

        Amounts are kept as STRINGS in storage (not floats) so that
        $1,000.10 does not become 1000.0999999999999 on the way back out.

        A line may be recurring (a retainer rather than a one-off), which
        adds `"recurring": true` and `"period": "<period>"` to the stored
        dict. Every recurring line in one quote must share a period: a
        quote that mixes a monthly retainer with an annual one cannot
        show a single "recurring" subtotal, and quietly picking one would
        be worse than refusing it.
        """
        if not isinstance(lines, list) or not lines:
            return None, 'At least one line item is required'

        out = []
        total = Decimal('0')
        recurring_period = None
        for raw in lines:
            if not isinstance(raw, dict):
                return None, 'Each line item must be an object'
            description = str(raw.get('description', '')).strip()
            if not description:
                return None, 'Each line item needs a description'
            if len(description) > 500:
                return None, 'Line item description is too long'
            amount_raw = raw.get('amount')
            if amount_raw in (None, ''):
                return None, f'Line item "{description}" needs an amount'
            try:
                amount = Decimal(str(amount_raw))
            except Exception:
                return None, f'Line item "{description}" has an invalid amount'
            if amount < 0:
                return None, f'Line item "{description}" has a negative amount'
            amount = amount.quantize(Decimal('0.01'))
            total += amount

            line = {'description': description, 'amount': str(amount)}

            recurring = raw.get('recurring', False)
            if recurring is not None and not isinstance(recurring, bool):
                return None, f'Line item "{description}" has an invalid recurring flag'
            if recurring:
                period = str(raw.get('period') or 'monthly').strip().lower()
                if period not in RECURRING_PERIODS:
                    return None, (f'Line item "{description}" has an invalid '
                                  f'recurrence period. Must be one of: '
                                  f'{", ".join(sorted(RECURRING_PERIODS))}')
                if recurring_period is None:
                    recurring_period = period
                elif recurring_period != period:
                    return None, ('Recurring line items must all share the same '
                                  'period (this quote mixes '
                                  f'"{recurring_period}" and "{period}")')
                line['recurring'] = True
                line['period'] = period

            out.append(line)

        if not out:
            return None, 'At least one line item is required'
        return out, None

    @staticmethod
    def split_lines(lines):
        """Split stored lines into (one_off_lines, recurring_lines).

        Both are lists of the stored line dicts. A line is recurring when
        it carries `"recurring": true`.
        """
        one_off = []
        recurring = []
        for line in (lines or []):
            (recurring if line.get('recurring') else one_off).append(line)
        return one_off, recurring

    @staticmethod
    def line_total(lines):
        """The sum of a list of stored lines, as a Decimal."""
        total = Decimal('0')
        for line in (lines or []):
            total += Decimal(str(line.get('amount', '0')))
        return total.quantize(Decimal('0.01'))

    @classmethod
    def next_quote_number(cls, user_id, as_of_date):
        """Return the next "Q-YYYY-NNNN" quote number for this user.

        Derived from the highest number already issued in the same year, so
        two quotes created in the same second cannot collide.
        """
        prefix = f'Q-{as_of_date.year}-'
        existing = db.session.query(cls.quote_number).filter(
            cls.user_id == user_id,
            cls.quote_number.like(f'{prefix}%'),
        ).all()
        highest = 0
        for (number,) in existing:
            tail = (number or '')[len(prefix):]
            if tail.isdigit():
                highest = max(highest, int(tail))
        return f'{prefix}{highest + 1:04d}'

    @property
    def one_off_amount(self):
        """The part of the quote that is a one-off (the ex-GST total of the
        non-recurring lines)."""
        one_off, _recurring = self.split_lines(self.lines)
        return self.line_total(one_off)

    @property
    def recurring_amount(self):
        """The recurring portion of the quote (the ex-GST total of the
        recurring lines), per period."""
        _one_off, recurring = self.split_lines(self.lines)
        return self.line_total(recurring)

    @property
    def recurring_period(self):
        """The period the recurring lines run on (None if none recur)."""
        _one_off, recurring = self.split_lines(self.lines)
        if not recurring:
            return None
        return recurring[0].get('period')

    def to_dict(self):
        return {
            'id': self.id,
            'user_id': self.user_id,
            'customer_id': self.customer_id,
            'customer_name': self.customer.name if self.customer else None,
            'quote_number': self.quote_number,
            'client_name': self.client_name,
            'lines': self.lines or [],
            'one_off_amount': str(self.one_off_amount),
            'recurring_amount': str(self.recurring_amount),
            'recurring_period': self.recurring_period,
            'ex_gst_amount': str(self.ex_gst_amount),
            'gst_amount': str(self.gst_amount),
            'gst_type': str(self.gst_type),
            'gst_basis': self.gst_basis,
            'total_amount': str(self.total_amount),
            'quote_date': self.quote_date.isoformat() if self.quote_date else None,
            'valid_until': self.valid_until.isoformat() if self.valid_until else None,
            'status': self.status,
            'notes': self.notes,
            'attachment_path': self.attachment_path,
            'sent_at': self.sent_at.isoformat() if self.sent_at else None,
            'accepted_at': self.accepted_at.isoformat() if self.accepted_at else None,
            'declined_at': self.declined_at.isoformat() if self.declined_at else None,
            'converted_invoice_id': self.converted_invoice_id,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }
